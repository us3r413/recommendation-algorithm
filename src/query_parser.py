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

SYSTEM_PROMPT = """你是一個台灣求職網站的職缺搜尋標籤提取器。
使用者位於台灣，資料庫中的職缺全部以中文（繁體）記錄。
輸入：使用者搜尋字串（已做縮寫展開），可能包含中文、英文或中英混合。
輸出：只回傳一個 JSON 陣列（array of strings），不要加任何說明文字。

重要：如果使用者輸入英文或其他外語，你必須將其翻譯成對應的繁體中文職務名稱或關鍵字。
資料庫只有中文，英文關鍵字無法匹配任何職缺。

翻譯範例：
- "frontend engineer" → "前端工程師"
- "backend" → "後端工程師"
- "fast food" / "fastfood" → "速食"
- "cook" / "chef" → "廚師"
- "marketing" → "行銷"
- "data analyst" → "資料分析師"
- "part time" / "part-time" → "兼職"
- "full time" → "全職"
- "intern" / "internship" → "實習"
- "remote" → "遠端"
- "restaurant" → "餐廳"
- "waiter" / "waitress" / "server" → "外場服務員"

標籤規則：
- 地點標籤：必須是完整城市名稱，加上「市」或「縣」，例如「台北市」「新北市」「高雄市」「台中市」
  - 「台北」→「台北市」
  - 「高雄」→「高雄市」
  - 「新北」→「新北市」
  - "Taipei" → "台北市"
  - "Kaohsiung" → "高雄市"
  - "Taichung" → "台中市"
- 薪資標籤：格式必須為「薪資>=數字」，例如「薪資>=35000」
  - 「35k以上」→「薪資>=35000」
  - 「月薪4萬」→「薪資>=40000」
  - k 代表千
- 職務標籤：必須是繁體中文正式職稱，例如「後端工程師」「前端工程師」「兼職」
- 每個標籤都是純字串，全部使用繁體中文（地點和薪資標籤除外）

範例：
輸入：「後端工程師 台北 兼職」
輸出：["後端工程師", "台北市", "兼職"]

輸入：「前端工程師 35k以上 台北」
輸出：["前端工程師", "台北市", "薪資>=35000"]

輸入：「fastfood cook」
輸出：["速食", "廚師"]

輸入：「Taipei frontend engineer part time」
輸出：["前端工程師", "台北市", "兼職"]

輸入：「data analyst remote」
輸出：["資料分析師", "遠端"]
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
# Public API
# ---------------------------------------------------------------------------

def querytoRequirement(query: str) -> list[str]:
    """Parse a free-text search query into a flat list of tag strings.

    Performs abbreviation expansion first, then calls the LLM via the ollama
    SDK.  The LLM is expected to return a JSON array of plain strings.  If the
    response fails JSON parsing or schema validation, the call is retried up to
    3 times in total.  After all retries are exhausted, the function falls back
    to rule-based parsing of the abbreviation-expanded query.

    All results are post-processed to normalise city names and salary formats.

    Args:
        query: A raw UTF-8 search string entered by the user.

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

    model_id = os.environ.get(
        "BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0"
    )
    bedrock = boto3.client("bedrock-runtime")

    for _attempt in range(3):
        try:
            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 256,
                "system": SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": expanded},
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
                return _post_process_tags(parsed)
        except Exception:
            pass  # network error, JSON parse error, schema mismatch → retry

    # Fallback: whitespace tokenisation with abbreviation expansion applied
    return _post_process_tags(expanded.split())
