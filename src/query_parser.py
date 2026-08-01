"""
query_parser.py — QueryParser stage of the recommendation pipeline.

Converts a free-text user search query into a flat list of normalised tag
strings suitable for `grabFromDatabase`.

Steps:
  1. Abbreviation expansion (rule-based, no I/O)
  2. LLM call via AWS Bedrock (Claude)
  3. JSON schema validation: the response must be a JSON array of plain strings
  4. Post-processing: normalise city names, parse salary patterns
  5. Up to 3 attempts total; on exhaustion fall back to rule-based parsing

The model ID defaults to ``"anthropic.claude-3-5-sonnet-20241022-v2:0"`` and
is overridable via the ``BEDROCK_MODEL_ID`` environment variable.
"""

import json
import os
import re

import boto3
from dotenv import load_dotenv

from src.utils.abbreviations import abbreviation_expand

load_dotenv()

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是台灣求職網站的搜尋標籤提取與語意擴展器。資料庫全中文（繁體）。
輸出：只回傳 JSON array of strings，無說明文字。

規則：
1. 英文/外語→翻譯成繁體中文
2. 模糊/類別詞→展開成台灣求職網站職缺標題中會出現的具體品牌名、公司名、同義詞
3. 具體職稱（如「前端工程師」「軟體工程師」「會計師」）→直接保留，不需擴展
4. 地點→完整城市名（台北→台北市, Taipei→台北市）
5. 薪資→「薪資>=數字」（35k以上→薪資>=35000, 月薪4萬→薪資>=40000）
6. 結合篩選條件的語意理解（見下方說明）

判斷是否擴展的原則：
- 需要擴展：產業/業態類別詞（速食、便利商店、咖啡廳、外送平台、電商）
- 不需擴展：明確職稱（前端工程師、後端工程師、資料分析師、UI設計師、會計）
- 不需擴展：明確公司名（台積電、Google、鴻海）

篩選條件的語意理解：
使用者搜尋時可能同時套用「城市篩選」或「職務類別篩選」。當提供這些篩選條件時，
你必須結合搜尋文字+篩選條件來理解使用者的真正意圖，產出語意相符的標籤。
- 不要重複輸出篩選條件本身（系統會自動套用），只輸出搜尋文字的語意擴展結果
- 但要讓擴展結果與篩選條件的語意一致

範例：
- 搜尋「越南」＋職務篩選=[軟體工程師]
  意圖：在越南工作的軟體工程師職缺（海外工作）
  → ["越南","海外","外派","東南亞"] （不要輸出工廠、作業員等無關標籤）
- 搜尋「日本」＋職務篩選=[餐飲服務人員]
  意圖：在日本的餐飲工作
  → ["日本","海外","東北亞","外派"]
- 搜尋「台積電」＋城市篩選=[台南市]
  意圖：台積電在台南的職缺
  → ["台積電","TSMC"]
- 搜尋「兼職」＋職務篩選=[門市/店員/專櫃人員]
  意圖：門市相關的兼職工作
  → ["兼職","打工","計時"]

語意擴展範例（僅限模糊類別詞）：
- 速食/fast food → ["麥當勞","肯德基","漢堡王","摩斯","頂呱呱","速食"]
- 便利商店 → ["7-11","全家","萊爾富","OK超商","便利商店"]
- 咖啡/coffee → ["星巴克","路易莎","cama","咖啡"]
- 外送/delivery → ["Uber Eats","foodpanda","外送"]
- 手搖 → ["五十嵐","清心","迷客夏","可不可","手搖"]

不擴展範例（具體職稱直接保留）：
- 前端工程師 → ["前端工程師"]
- software engineer → ["軟體工程師"]
- frontend engineer → ["前端工程師"]
- data analyst → ["資料分析師"]
- UI designer → ["UI設計師"]

完整範例：
「fastfood cook」→["麥當勞","肯德基","漢堡王","摩斯","頂呱呱","速食","廚師","餐飲"]
「便利商店 打工」→["7-11","全家","萊爾富","OK超商","便利商店","兼職"]
「前端工程師 35k以上 台北」→["前端工程師","台北市","薪資>=35000"]
「software engineer taipei」→["軟體工程師","台北市"]
「coffee shop barista」→["星巴克","路易莎","cama","咖啡","咖啡師","吧台"]
"""

# ---------------------------------------------------------------------------
# City name normalisation map (short form → full form)
# ---------------------------------------------------------------------------

_CITY_SHORT_MAP: dict[str, str] = {
    "台北": "台北市", "新北": "新北市", "桃園": "桃園市",
    "台中": "台中市", "台南": "台南市", "高雄": "高雄市",
    "基隆": "基隆市", "新竹": "新竹市", "嘉義": "嘉義市",
    "新竹縣": "新竹縣", "苗栗": "苗栗縣", "彰化": "彰化縣",
    "南投": "南投縣", "雲林": "雲林縣", "嘉義縣": "嘉義縣",
    "屏東": "屏東縣", "宜蘭": "宜蘭縣", "花蓮": "花蓮縣",
    "台東": "台東縣", "澎湖": "澎湖縣", "金門": "金門縣",
    "連江": "連江縣",
}

# Matches salary-like patterns: "35k以上", "薪資35000", numbers with k, etc.
_SALARY_PATTERNS = [
    re.compile(r'^薪資>=(\d+)$'),           # already correct format
    re.compile(r'^(\d+)[kK]以上$'),          # "35k以上"
    re.compile(r'^(\d+)[kK]$'),              # "35k"
    re.compile(r'^薪資>=?(\d+)[kK]?$'),      # "薪資>=35k" or "薪資35000"
    re.compile(r'^月薪(\d+)萬$'),             # "月薪4萬"
]


def _post_process_tags(tags: list[str]) -> list[str]:
    """Normalise tags: fix city names, parse salary patterns."""
    result = []
    for tag in tags:
        # City normalisation
        if tag in _CITY_SHORT_MAP:
            result.append(_CITY_SHORT_MAP[tag])
            continue

        # Already a valid salary tag
        if re.match(r'^薪資>=\d+$', tag):
            result.append(tag)
            continue

        # Try to parse salary patterns
        salary_parsed = False
        # "35k以上" or "35K以上"
        m = re.match(r'^(\d+)[kK]以上$', tag)
        if m:
            result.append(f"薪資>={int(m.group(1)) * 1000}")
            salary_parsed = True
        if not salary_parsed:
            m = re.match(r'^(\d+)[kK]$', tag)
            if m:
                result.append(f"薪資>={int(m.group(1)) * 1000}")
                salary_parsed = True
        if not salary_parsed:
            m = re.match(r'^月薪(\d+)萬$', tag)
            if m:
                result.append(f"薪資>={int(m.group(1)) * 10000}")
                salary_parsed = True
        if not salary_parsed:
            # Check if it contains "以上" with a number (e.g. "35000以上")
            m = re.match(r'^(\d+)以上$', tag)
            if m:
                result.append(f"薪資>={int(m.group(1))}")
                salary_parsed = True

        if not salary_parsed:
            result.append(tag)

    return result


# ---------------------------------------------------------------------------
# LLM response cache (avoids repeated API calls for identical queries)
# ---------------------------------------------------------------------------

from functools import lru_cache


@lru_cache(maxsize=256)
def _llm_parse_cached(expanded: str, filter_context: str = "") -> list[str] | None:
    """Call LLM and cache the result. Returns None on failure (not cached).

    Args:
        expanded: The abbreviation-expanded query string.
        filter_context: Optional string describing active filters (city/category)
                        to help the LLM understand user intent.
    """
    model_id = os.environ.get(
        "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
    )
    bedrock = boto3.client("bedrock-runtime")

    # Build user message with optional filter context
    if filter_context:
        user_message = f"搜尋文字：{expanded}\n{filter_context}"
    else:
        user_message = expanded

    for _attempt in range(3):
        try:
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 150,
                "temperature": 0,
                "system": SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": user_message},
                ],
            })
            response = bedrock.invoke_model(
                modelId=model_id,
                contentType="application/json",
                accept="application/json",
                body=body,
            )
            response_body = json.loads(response["body"].read())
            raw = response_body["content"][0]["text"].strip()
            parsed = json.loads(raw)
            if (isinstance(parsed, list)
                    and all(isinstance(t, str) for t in parsed)):
                return parsed
        except Exception as e:
            import logging
            logging.debug(f"query_parser attempt {_attempt+1}: {type(e).__name__}: {e}")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def querytoRequirement(
    query: str,
    city_filters: list[str] | None = None,
    category_filters: list[str] | None = None,
) -> list[str]:
    """Parse a free-text search query into a flat list of tag strings.

    Performs abbreviation expansion first, then calls the LLM via AWS Bedrock.
    The LLM is expected to return a JSON array of plain strings.  If the
    response fails JSON parsing or schema validation, the call is retried up to
    3 times in total.  After all retries are exhausted, the function falls back
    to rule-based parsing of the abbreviation-expanded query.

    When city_filters or category_filters are provided, they are included as
    context in the LLM prompt so the model can understand the user's intent
    (e.g. "越南" + category_filters=["軟體工程師"] → overseas software jobs,
    not factory work).

    All results are post-processed to normalise city names and salary formats.

    Args:
        query: A raw UTF-8 search string entered by the user.
        city_filters: Optional resolved city name strings from c0 codes.
        category_filters: Optional resolved category name strings from d0 codes.

    Returns:
        A ``list[str]`` of tag strings.  Every element is a plain ``str``
        (never a dict, list, or other type).  Guaranteed to be non-empty when
        ``query`` is non-empty (fallback ensures at least one token).

    Examples:
        >>> querytoRequirement("BE 台北 pt")      # may vary by LLM
        ['後端工程師', '台北市', '兼職']
        >>> querytoRequirement("devops 35k以上")  # may vary by LLM
        ['開發維運工程師', '薪資>=35000']
    """
    expanded = abbreviation_expand(query)

    # Empty query → return empty tags (filters like c0/d0 handle retrieval)
    if not expanded.strip():
        return []

    # Build filter context string for the LLM
    filter_parts = []
    if city_filters:
        filter_parts.append(f"城市篩選={city_filters}")
    if category_filters:
        filter_parts.append(f"職務篩選={category_filters}")
    filter_context = "\n".join(filter_parts)

    result = _llm_parse_cached(expanded, filter_context)
    if result is not None:
        return _post_process_tags(result)

    # Fallback: whitespace tokenisation with abbreviation expansion applied
    return _post_process_tags(expanded.split())
