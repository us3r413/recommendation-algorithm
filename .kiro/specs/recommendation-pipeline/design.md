# Design Document

## Feature: recommendation-pipeline

---

## Overview

三階段職缺推薦管線，接收使用者的自然語言搜尋字串與使用者編號，回傳前 10 筆最相關的職缺。三個核心函式依序串聯：

```
recommend(query: str, talent_no: int) -> list[dict]
    ↓
querytoRequirement(query)      → tags: list[str]
    ↓
grabFromDatabase(tags)         → candidates: list[dict]
    ↓
ranking(candidates, talent_no) → top10: list[dict]
```

整體設計原則：各模組職責單一、介面簡單、可獨立測試；DuckDB 處理所有大規模 CSV 過濾，Python 層只做輕量的 in-memory 邏輯。

---

## Architecture

```
src/
├── pipeline.py          # recommend() entry point, wires three stages
├── query_parser.py      # querytoRequirement()
├── retriever.py         # grabFromDatabase()
├── ranker.py            # ranking()
└── utils/
    ├── abbreviations.py # Abbreviation expansion table
    └── tag_parser.py    # Tag classification helpers (city / salary / job-title)
```

### Data Flow

```
query (str)
    │
    ▼
[query_parser.py]
    ├── abbreviation_expand(query)         → expanded_query
    ├── ollama.chat(prompt + expanded_query) → raw_json
    ├── json.loads(raw_json) + schema_validate  ─→ (retry ×3 on failure)
    └── fallback: whitespace_split(expanded_query)
    │
    ▼
tags: list[str]  e.g. ["後端工程師", "兼職", "台北市", "薪資>=35000"]
    │
    ▼
[retriever.py]
    ├── classify_tags(tags)                → {cities, salary_min, job_terms}
    ├── semantic_expand(job_terms, 職務對照表) → expanded_job_terms (deduplicated)
    ├── duckdb.sql(職缺.csv + 瀏覽次數.csv)  → filtered rows
    └── build_candidates(rows)             → list[dict]
    │
    ▼
candidates: list[dict]  (職缺.csv columns + score)
    │
    ▼
[ranker.py]
    ├── talent_no == 0 ?                   → sort by score desc
    ├── lookup userBehaviorFeature.csv
    │   ├── not found / is_cold_start?     → sort by score desc
    │   └── normal user?                   → compute personal_score + final_score
    └── return top 10, strip computed fields
    │
    ▼
result: list[dict]  (raw 職缺.csv columns only, no score)
```

---

## Components

### 1. `query_parser.py` — QueryParser

**責任：** 將自然語言 query 轉換成標準化 tag list。

#### 1.1 Abbreviation Expansion

在呼叫 LLM 前，先對 query 做規則式展開：

```python
ABBREVIATION_MAP = {
    "pt":  "兼職",
    "ft":  "全職",
    "ue":  "使用者體驗設計師",
    "ui":  "介面設計師",
    "rd":  "研發工程師",
    "qa":  "品質保證工程師",
    "pm":  "產品經理",
    "hr":  "人力資源",
    "be":  "後端工程師",
    "fe":  "前端工程師",
    "ml":  "機器學習工程師",
    "ai":  "人工智慧",
    "devops": "開發維運工程師",
}

def abbreviation_expand(query: str) -> str:
    """Case-insensitive word-boundary substitution."""
    tokens = query.split()
    expanded = [ABBREVIATION_MAP.get(t.lower(), t) for t in tokens]
    return " ".join(expanded)
```

#### 1.2 LLM Call and Schema Validation

```python
SYSTEM_PROMPT = """
你是一個職缺搜尋的標籤提取器。
輸入：使用者搜尋字串（已做縮寫展開）
輸出：只回傳一個 JSON 陣列（array of strings），不要加任何說明文字。
標籤規則：
- 地點標籤：直接輸出城市名稱，例如「台北市」
- 薪資標籤：格式為「薪資>=<整數>」，例如「薪資>=35000」；k 代表千，35k → 35000
- 職務標籤：正式職稱，例如「後端工程師」、「兼職」
- 每個標籤都是純字串，不含子物件或 key-value 結構
"""

def querytoRequirement(query: str) -> list[str]:
    expanded = abbreviation_expand(query)
    last_exc = None
    for attempt in range(3):
        try:
            response = ollama.chat(
                model="llama3",   # configurable via env var OLLAMA_MODEL
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": expanded},
                ],
            )
            raw = response["message"]["content"].strip()
            parsed = json.loads(raw)
            if (isinstance(parsed, list)
                    and all(isinstance(t, str) for t in parsed)):
                return parsed
        except Exception as exc:
            last_exc = exc
    # Fallback: whitespace tokenisation with abbreviation expansion applied
    return expanded.split()
```

**Key design decisions:**
- Model name is configurable via `OLLAMA_MODEL` environment variable, defaulting to `"llama3"`.
- Schema validation checks: `isinstance(parsed, list) and all(isinstance(t, str) for t in parsed)`.
- Fallback returns `expanded.split()` — preserving abbreviation expansion even without LLM.
- Exception types intentionally broad: network errors, JSON parse errors, and schema failures all trigger retry.

---

### 2. `retriever.py` — Retriever

**責任：** 從 ~100 萬筆職缺中以 DuckDB SQL 快速過濾並附加熱門度分數。

#### 2.1 Tag Classification

```python
import re

SALARY_TAG_RE = re.compile(r'^薪資>=(\d+)$')

def classify_tags(tags: list[str]) -> dict:
    """
    Returns:
        {
            "cities":      list[str],   # 工作城市 filter values
            "salary_min":  int | None,  # 薪資下限 >= this value
            "job_terms":   list[str],   # job title search terms (pre-expansion)
        }
    """
    cities, job_terms = [], []
    salary_min = None
    known_cities = _load_known_cities()   # cached set from 城市對照表.csv
    for tag in tags:
        m = SALARY_TAG_RE.match(tag)
        if m:
            salary_min = int(m.group(1))
        elif tag in known_cities:
            cities.append(tag)
        else:
            job_terms.append(tag)
    return {"cities": cities, "salary_min": salary_min, "job_terms": job_terms}
```

#### 2.2 Semantic Expansion

```python
def semantic_expand(job_terms: list[str], lookup_path: str) -> list[str]:
    """
    Look up each term against CodeNameA/B/C and CodeAlike in 職務對照表.csv.
    Returns a deduplicated list of all matched + original terms.
    """
    df = _load_job_lookup(lookup_path)   # cached pandas DataFrame
    expanded = set()
    for term in job_terms:
        # Match against CodeNameA, CodeNameB, CodeNameC
        mask = (
            df["CodeNameA"].str.contains(term, na=False) |
            df["CodeNameB"].str.contains(term, na=False) |
            df["CodeNameC"].str.contains(term, na=False) |
            df["CodeAlike"].str.contains(term, na=False)
        )
        matched = df[mask]
        if not matched.empty:
            for alike in matched["CodeAlike"].dropna():
                expanded.update(t.strip() for t in alike.split(",") if t.strip())
        else:
            expanded.add(term)   # pass-through if no match
    return list(expanded)
```

#### 2.3 DuckDB Query

```python
import duckdb

JOBS_PATH = "dataset/職缺.csv"
VIEWS_PATH = "dataset/瀏覽次數.csv"

def grabFromDatabase(tags: list[str]) -> list[dict]:
    classified = classify_tags(tags)
    expanded_job_terms = semantic_expand(
        classified["job_terms"],
        "dataset/職務對照表.csv"
    )

    conditions = []
    params = {}

    if classified["cities"]:
        placeholders = ", ".join(f"$city_{i}" for i in range(len(classified["cities"])))
        conditions.append(f"j.工作城市 IN ({placeholders})")
        for i, city in enumerate(classified["cities"]):
            params[f"city_{i}"] = city

    if classified["salary_min"] is not None:
        conditions.append("j.薪資下限 >= $salary_min")
        params["salary_min"] = classified["salary_min"]

    if expanded_job_terms:
        job_likes = " OR ".join(
            f"j.職務名稱 ILIKE $jt_{i}"
            for i in range(len(expanded_job_terms))
        )
        conditions.append(f"({job_likes})")
        for i, term in enumerate(expanded_job_terms):
            params[f"jt_{i}"] = f"%{term}%"

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    sql = f"""
        SELECT j.*, COALESCE(p.score, 0.0) AS score
        FROM '{JOBS_PATH}' j
        LEFT JOIN '{VIEWS_PATH}' p ON j.職缺編號 = p.職缺編號
        {where_clause}
    """

    con = duckdb.connect()
    result = con.execute(sql, params).fetchall()
    columns = [desc[0] for desc in con.description]
    con.close()

    return [dict(zip(columns, row)) for row in result]
```

**Key design decisions:**
- All filtering happens inside DuckDB — `職缺.csv` is never loaded into Python memory.
- Parameterised queries prevent SQL injection from tag values.
- `COALESCE(p.score, 0.0)` guarantees every record has a numeric `score` even without a matching row in `瀏覽次數.csv`.
- `ILIKE` provides case-insensitive substring matching compatible with Chinese text.
- If no filter conditions exist (empty tags), all records are returned — acceptable given the caller always provides at least one tag.

---

### 3. `ranker.py` — Ranker

**責任：** 對候選職缺排序，回傳前 10 筆，且不暴露任何內部計算欄位。

#### 3.1 Routing Logic

```python
import pandas as pd

FEATURES_PATH = "dataset/userBehaviorFeature.csv"
_features_cache: pd.DataFrame | None = None

def _get_user_feature(talent_no: int) -> dict | None:
    global _features_cache
    if _features_cache is None:
        _features_cache = pd.read_csv(FEATURES_PATH)
    row = _features_cache[_features_cache["talentNo"] == talent_no]
    if row.empty:
        return None
    return row.iloc[0].to_dict()

def ranking(candidates: list[dict], talent_no: int) -> list[dict]:
    if not candidates:
        return []

    raw_fields = [k for k in candidates[0].keys() if k != "score"]

    if talent_no == 0:
        return _popularity_rank(candidates, raw_fields)

    feature = _get_user_feature(talent_no)
    if feature is None or feature.get("is_cold_start", True):
        return _popularity_rank(candidates, raw_fields)

    return _personalised_rank(candidates, feature, raw_fields)
```

#### 3.2 Popularity Ranking Path

```python
def _popularity_rank(candidates: list[dict], raw_fields: list[str]) -> list[dict]:
    sorted_candidates = sorted(
        candidates,
        key=lambda c: (c.get("score", 0.0), c.get("職缺最後修改時間", "")),
        reverse=True,
    )
    top10 = sorted_candidates[:10]
    return [{f: c[f] for f in raw_fields} for c in top10]
```

#### 3.3 Personalised Ranking Path

```python
def _compute_personal_score(candidate: dict, feature: dict) -> float:
    # location_match
    pref_cities = {
        feature.get("preferred_city_1"),
        feature.get("preferred_city_2"),
        feature.get("preferred_city_3"),
    } - {None, ""}
    location_match = 1.0 if candidate.get("工作城市") in pref_cities else 0.0

    # category_match
    pref_cats = {
        feature.get("preferred_category_1"),
        feature.get("preferred_category_2"),
        feature.get("preferred_category_3"),
    } - {None, ""}
    category_match = 1.0 if candidate.get("職務中類") in pref_cats else 0.0

    # salary_match
    salary_floor = feature.get("salary_floor")
    if salary_floor is None or pd.isna(salary_floor):
        salary_match = 0.5
    else:
        salary_match = 1.0 if (candidate.get("薪資下限") or 0) >= salary_floor else 0.0

    return location_match * 0.4 + category_match * 0.4 + salary_match * 0.2


def _personalised_rank(
    candidates: list[dict], feature: dict, raw_fields: list[str]
) -> list[dict]:
    # Normalise popularity scores to [0.0, 1.0]
    max_score = max((c.get("score", 0.0) for c in candidates), default=0.0)

    def final_score(c: dict) -> float:
        personal = _compute_personal_score(c, feature)
        pop = (c.get("score", 0.0) / max_score) if max_score > 0.0 else 0.0
        return personal * 0.7 + pop * 0.3

    sorted_candidates = sorted(candidates, key=final_score, reverse=True)
    top10 = sorted_candidates[:10]
    return [{f: c[f] for f in raw_fields} for c in top10]
```

**Key design decisions:**
- `raw_fields` is derived once from the first candidate dict by excluding `"score"`, ensuring the output never leaks any computed field.
- `userBehaviorFeature.csv` is loaded lazily and cached in a module-level variable (acceptable for a single-process server; for multi-process use, pass path explicitly and cache per-process).
- Tie-breaking in `_popularity_rank` uses a tuple sort key `(score, 職缺最後修改時間)`, where string ISO timestamps sort correctly in descending order with `reverse=True`.
- `pd.isna(salary_floor)` handles both Python `None` and NumPy `NaN` coming from pandas CSV parsing.

---

### 4. `pipeline.py` — Entry Point

```python
from query_parser import querytoRequirement
from retriever import grabFromDatabase
from ranker import ranking


def recommend(query: str, talent_no: int) -> list[dict]:
    """
    Top-level entry point for the recommendation pipeline.
    Exceptions from any stage propagate to the caller.
    """
    tags = querytoRequirement(query)
    candidates = grabFromDatabase(tags)
    return ranking(candidates, talent_no)
```

---

## Data Models

### Tag

A plain Python `str`. Four semantic categories are used at runtime but not encoded as separate types:

| Category | Example | Identified by |
|----------|---------|---------------|
| Job title | `"後端工程師"` | Not a city, not a salary tag |
| City | `"台北市"` | Matches city lookup table |
| Salary | `"薪資>=35000"` | Matches regex `^薪資>=(\d+)$` |
| Work type | `"兼職"` | Treated as job title term |

### Candidate

A `dict` containing all columns from `職缺.csv` plus one extra field:

| Field | Type | Source |
|-------|------|--------|
| `職缺編號` | int | 職缺.csv |
| `職務名稱` | str | 職缺.csv |
| `職務內容` | str | 職缺.csv |
| `薪資下限` | int | 職缺.csv |
| `薪資上限` | int | 職缺.csv |
| `職務中類` | str | 職缺.csv |
| `工作城市` | str | 職缺.csv |
| `職缺最後修改時間` | str | 職缺.csv |
| *(all other 職缺.csv columns)* | varies | 職缺.csv |
| `score` | float | 瀏覽次數.csv (COALESCE 0.0) |

### UserFeature

A `dict` read from one row of `userBehaviorFeature.csv`:

| Field | Type | Nullable |
|-------|------|----------|
| `talentNo` | int | No |
| `preferred_city_1/2/3` | str | Yes |
| `preferred_category_1/2/3` | str | Yes |
| `salary_floor` | float | Yes (null → salary_match = 0.5) |
| `total_events` | int | No |
| `is_cold_start` | bool | No |

---

## Error Handling

| Scenario | Behaviour |
|----------|-----------|
| LLM network failure (1st–3rd attempt) | Retry silently |
| LLM returns non-JSON or wrong schema (all 3 attempts) | Fallback: return `expanded_query.split()` |
| DuckDB SQL error | Propagate exception to caller |
| `userBehaviorFeature.csv` not found | Propagate `FileNotFoundError` to caller |
| `talent_no` not found in feature table | Treat as cold-start (popularity ranking) |
| Empty candidate set | `ranking()` returns `[]` immediately |
| All `score` values are `0.0` | Normalised popularity = 0.0; final score driven entirely by personal_score |
| Fewer than 10 candidates | Return all available candidates (no padding) |

---

## Performance Considerations

- **DuckDB in-process:** No separate service startup required. Query time on 1M-row CSV is expected in the low-hundreds-of-milliseconds range for typical filters.
- **Parameterised queries:** Prevents SQL injection; also allows DuckDB to cache query plans.
- **Feature table cache:** `userBehaviorFeature.csv` (166 K rows, ~166 KB estimated) is loaded once per process.
- **Abbreviation map lookup:** O(n) over whitespace-split tokens — negligible for typical query lengths.
- **Semantic expansion:** `職務對照表.csv` is small enough to hold in memory; loaded once per process.

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

---

### Property 1: QueryParser always returns a list of plain strings

*For any* UTF-8 input string `query`, `querytoRequirement(query)` SHALL return a value that is a `list` instance where every element is a `str` instance (not a dict, list, or other type).

**Validates: Requirements 1.1, 1.8**

---

### Property 2: Abbreviations are always expanded in the output

*For any* query string that contains a known abbreviation token (e.g. `"pt"`, `"ue"`, `"ui"`), the returned Tag List SHALL contain the corresponding expanded Chinese term (e.g. `"兼職"`, `"使用者體驗設計師"`, `"介面設計師"`), regardless of whether the LLM call succeeds or falls back.

**Validates: Requirements 1.2, 1.5**

---

### Property 3: Valid LLM JSON is returned as-is

*For any* LLM response that is a valid JSON array of strings, `querytoRequirement` SHALL return exactly that array (same elements, same order) as the Tag List.

**Validates: Requirements 1.3**

---

### Property 4: Salary tags are correctly formatted

*For any* query containing a salary constraint expressed in Chinese numerals, digits, or `k`-notation (e.g. `"35k"`, `"40000"`, `"三萬五`"), the returned Tag List SHALL contain exactly one element matching the regex `^薪資>=\d+$` whose integer value is the correct minimum salary in TWD.

**Validates: Requirements 1.7**

---

### Property 5: Fallback tokenisation preserves abbreviation expansion

*For any* query string, when all 3 LLM attempts fail, the returned Tag List SHALL equal `abbreviation_expand(query).split()` — i.e. the whitespace-tokenised form of the abbreviation-expanded query, with no other transformations applied.

**Validates: Requirements 1.5**

---

### Property 6: Matched tags are expanded with all CodeAlike terms

*For any* tag that matches at least one row in `職務對照表.csv` (via CodeNameA, CodeNameB, CodeNameC, or CodeAlike), the set of expanded job-title search terms SHALL include every non-empty, trimmed string from the matched row's `CodeAlike` field.

**Validates: Requirements 2.1, 2.2**

---

### Property 7: Unmatched tags pass through unchanged

*For any* tag that does not match any row in `職務對照表.csv`, the tag itself SHALL appear in the set of expanded job-title search terms used for the DuckDB query.

**Validates: Requirements 2.3**

---

### Property 8: Expanded job-title terms are deduplicated

*For any* combination of input tags and lookup table contents, the list of job-title search terms passed to the DuckDB query SHALL contain no duplicate strings.

**Validates: Requirements 2.4**

---

### Property 9: City filter is applied correctly

*For any* tag list containing one or more city terms, every dict in the returned Candidate Set SHALL have its `工作城市` field equal to one of the city terms extracted from the tag list.

**Validates: Requirements 3.2**

---

### Property 10: Salary filter is applied correctly

*For any* tag list containing a Salary Tag `"薪資>=N"`, every dict in the returned Candidate Set SHALL have `薪資下限 >= N`.

**Validates: Requirements 3.3**

---

### Property 11: Job-title filter uses case-insensitive substring matching

*For any* tag list containing job-title terms (after semantic expansion), every dict in the returned Candidate Set SHALL have a `職務名稱` that contains at least one of the expanded job-title terms (case-insensitive substring match).

**Validates: Requirements 3.4**

---

### Property 12: Every candidate has a score field, unmatched listings get 0.0

*For any* call to `grabFromDatabase`, every dict in the returned list SHALL contain a `"score"` key whose value is a `float`. For any candidate whose `職缺編號` does not appear in `瀏覽次數.csv`, `score` SHALL equal `0.0`.

**Validates: Requirements 3.5**

---

### Property 13: Candidates contain all 職缺.csv columns plus score

*For any* call to `grabFromDatabase` that returns at least one result, every dict in the result SHALL contain all column names present in `職缺.csv` plus the `"score"` key, and no other keys.

**Validates: Requirements 3.6**

---

### Property 14: Anonymous and cold-start users are ranked by popularity

*For any* candidate set and any `talent_no` that is either `0`, maps to a row with `is_cold_start=True`, or has no matching row in `userBehaviorFeature.csv`, the returned list SHALL be a prefix of the candidate set sorted by `score` descending, with ties broken by `職缺最後修改時間` descending.

**Validates: Requirements 4.2, 4.3, 4.4**

---

### Property 15: Personal score formula is computed correctly

*For any* normal user feature row and candidate dict, `_compute_personal_score` SHALL return exactly `location_match × 0.4 + category_match × 0.4 + salary_match × 0.2`, where:
- `location_match ∈ {0.0, 1.0}` based on whether `工作城市` is in the user's top 3 preferred cities
- `category_match ∈ {0.0, 1.0}` based on whether `職務中類` is in the user's top 3 preferred categories
- `salary_match = 1.0` if `薪資下限 >= salary_floor` (when salary_floor is not null), `0.0` if below, `0.5` if salary_floor is null

**Validates: Requirements 5.2, 5.3, 5.4, 5.5, 5.6**

---

### Property 16: Final score formula is applied with normalised popularity

*For any* normal user and candidate set where `max(score) > 0`, `final_score` SHALL equal `personal_score × 0.7 + (score / max_score) × 0.3`. When `max(score) == 0.0` for all candidates, the popularity component SHALL be `0.0` for all candidates.

**Validates: Requirements 5.7, 5.8**

---

### Property 17: Output length is min(len(candidates), 10)

*For any* candidate set of size `n ≥ 0` and any `talent_no`, `ranking` SHALL return exactly `min(n, 10)` items.

**Validates: Requirements 4.1, 6.3**

---

### Property 18: No computed fields leak into ranking output

*For any* call to `ranking` via either the popularity path or the personalised path, no returned dict SHALL contain the keys `"score"`, `"personal_score"`, `"final_score"`, or any other key not present in the original `職缺.csv` columns.

**Validates: Requirements 4.6, 5.10**

---

### Property 19: Normal users are ranked by final_score descending

*For any* candidate set and a normal user (authenticated, `is_cold_start=False`), the returned list SHALL be a prefix of the candidates sorted by `final_score` descending.

**Validates: Requirements 5.9**
