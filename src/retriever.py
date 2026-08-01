"""
retriever.py — Retriever stage of the recommendation pipeline.

Implements grabFromDatabase(tags) which:
1. Classifies tags into cities / salary_min / job_terms via classify_tags()
2. Semantically expands job_terms against 職務對照表.csv (CodeAlike)
3. Runs a parameterised DuckDB SQL query over 職缺.csv with a LEFT JOIN
   to 瀏覽次數.csv to attach popularity scores
4. Returns a list[dict] — one dict per matching job listing
"""

import re

import duckdb
import pandas as pd

from src.utils.tag_parser import classify_tags

# ---------------------------------------------------------------------------
# File paths (relative to the project root / working directory)
# ---------------------------------------------------------------------------

CITY_TABLE_PATH = "dataset/城市對照表.csv"
JOB_LOOKUP_PATH = "dataset/職務對照表.csv"
JOBS_PATH = "dataset/職缺.csv"
VIEWS_PATH = "dataset/瀏覽次數.csv"

# ---------------------------------------------------------------------------
# Module-level caches (lazy-loaded once per process)
# ---------------------------------------------------------------------------

_cities_cache: set | None = None
_city_code_map_cache: dict | None = None
_job_lookup_cache: pd.DataFrame | None = None


def _load_known_cities() -> set:
    """Return a cached set of valid city name strings.

    Reads 城市對照表.csv once and caches the result.  The city names are
    taken from the CodeNameA column for rows with CodeType == 2 (city-level
    granularity), which covers all 47 distinct 工作城市 values in 職缺.csv.
    """
    global _cities_cache
    if _cities_cache is None:
        df = pd.read_csv(CITY_TABLE_PATH)
        # CodeType 2 rows are city-level entries; CodeNameA holds the Chinese
        # city name that matches the 工作城市 values in 職缺.csv exactly.
        city_rows = df[df["CodeType"] == 2]
        _cities_cache = set(city_rows["CodeNameA"].dropna().astype(str).tolist())
    return _cities_cache


def _load_city_code_map() -> dict[int, str]:
    """Return a cached dict mapping city CodeNo → CodeNameA (city name).

    Used to resolve c0 numeric codes to city name strings for SQL filtering.
    Only includes CodeType == 2 (city-level) entries.
    """
    global _city_code_map_cache
    if _city_code_map_cache is None:
        df = pd.read_csv(CITY_TABLE_PATH)
        city_rows = df[df["CodeType"] == 2]
        _city_code_map_cache = dict(
            zip(city_rows["CodeNo"].astype(int), city_rows["CodeNameA"].astype(str))
        )
    return _city_code_map_cache


def _load_job_lookup() -> pd.DataFrame:
    """Return a cached pandas DataFrame of 職務對照表.csv.

    Reads the file once and caches the result.  The DataFrame contains
    CodeNameA, CodeNameB, CodeNameC, and CodeAlike columns used for
    semantic expansion of job-title terms.
    """
    global _job_lookup_cache
    if _job_lookup_cache is None:
        _job_lookup_cache = pd.read_csv(JOB_LOOKUP_PATH)
    return _job_lookup_cache


# ---------------------------------------------------------------------------
# Inject the real loader into tag_parser so classify_tags() can identify
# city tags correctly.  This replaces the stub defined in tag_parser.py.
# ---------------------------------------------------------------------------

import src.utils.tag_parser as _tag_parser_module  # noqa: E402

_tag_parser_module._load_known_cities = _load_known_cities  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Semantic expansion
# ---------------------------------------------------------------------------

def semantic_expand(job_terms: list[str]) -> list[str]:
    """Expand job-title terms using the 職務對照表.csv CodeAlike column.

    For each term, searches for substring matches in CodeNameA, CodeNameB,
    CodeNameC, and CodeAlike.  All values from matched CodeAlike entries
    (split by comma, <br>, or <br/>) are added to the result set.
    The original term is always included. Unmatched terms pass through unchanged.

    Returns:
        A deduplicated list of expanded job-title search terms.
    """
    df = _load_job_lookup()
    expanded: set[str] = set()

    # Split pattern for CodeAlike: comma, <br>, or <br/>
    split_re = re.compile(r'[,，]|<br\s*/?>') 

    for term in job_terms:
        # Always include the original term
        expanded.add(term)

        mask = (
            df["CodeNameA"].str.contains(term, na=False, regex=False)
            | df["CodeNameB"].str.contains(term, na=False, regex=False)
            | df["CodeNameC"].str.contains(term, na=False, regex=False)
            | df["CodeAlike"].str.contains(term, na=False, regex=False)
        )
        matched = df[mask]
        if not matched.empty:
            for alike in matched["CodeAlike"].dropna():
                parts = split_re.split(alike)
                expanded.update(t.strip() for t in parts if t.strip())

    return list(expanded)


def resolve_c0_codes(c0_codes: list[str]) -> list[str]:
    """Resolve c0 city codes to city name strings.

    Args:
        c0_codes: List of numeric city code strings (e.g. ["100100", "100200"]).

    Returns:
        List of city name strings (e.g. ["台北市", "新北市"]).
        Unknown codes are silently dropped.
    """
    code_map = _load_city_code_map()
    cities = []
    for code in c0_codes:
        try:
            name = code_map.get(int(code))
            if name:
                cities.append(name)
        except (ValueError, TypeError):
            pass
    return cities


def resolve_d0_codes(d0_codes: list[str]) -> list[str]:
    """Resolve d0 job category codes to job category name strings.

    The d0 codes from userSearchLog are 6-digit codes matching 職務對照表.CodeNo.
    These map to CodeNameA (職務小類), which corresponds to 職缺.csv's 職務小類 column.

    Supports both:
    - 小類 codes (e.g. "140213") → exact match to one CodeNameA
    - 中類 codes (e.g. "140200", last two digits "00") → expands to all 小類 under that 中類

    Args:
        d0_codes: List of numeric job category code strings (e.g. ["160213", "140200"]).

    Returns:
        List of job category name strings.
        Unknown codes are silently dropped.
    """
    df = _load_job_lookup()
    names = []
    for code in d0_codes:
        try:
            code_int = int(code)
            # Check if this is a 中類 code (last two digits are 00)
            if code_int % 100 == 0:
                # Expand: find all 小類 codes under this 中類 prefix
                # e.g. 140200 → match all codes 140201-140299
                prefix_min = code_int + 1
                prefix_max = code_int + 99
                matched = df[(df["CodeNo"] >= prefix_min) & (df["CodeNo"] <= prefix_max)]
                if not matched.empty:
                    names.extend(matched["CodeNameA"].dropna().tolist())
            else:
                # Exact 小類 match
                row = df[df["CodeNo"] == code_int]
                if not row.empty:
                    names.append(row.iloc[0]["CodeNameA"])
        except (ValueError, TypeError):
            pass
    # Deduplicate while preserving order
    return list(dict.fromkeys(names))


# ---------------------------------------------------------------------------
# Persistent DuckDB connection with pre-loaded tables (module-level singleton)
# ---------------------------------------------------------------------------

_db_con: duckdb.DuckDBPyConnection | None = None


def _get_db() -> duckdb.DuckDBPyConnection:
    """Return a persistent in-memory DuckDB connection with pre-loaded tables.

    On first call, reads 職缺.csv and 瀏覽次數.csv into in-memory tables and
    creates indexes for fast filtering. Subsequent calls reuse the connection.
    """
    global _db_con
    if _db_con is None:
        _db_con = duckdb.connect()
        # Load job listings into a persistent in-memory table
        _db_con.execute(f"""
            CREATE TABLE jobs AS SELECT * FROM '{JOBS_PATH}'
        """)
        # Load popularity scores
        _db_con.execute(f"""
            CREATE TABLE popularity AS SELECT * FROM '{VIEWS_PATH}'
        """)
        # Create indexes for common filter columns
        _db_con.execute("CREATE INDEX idx_jobs_city ON jobs(工作城市)")
        _db_con.execute("CREATE INDEX idx_jobs_salary ON jobs(薪資下限)")
        _db_con.execute("CREATE INDEX idx_jobs_type ON jobs(職缺屬性)")
        _db_con.execute("CREATE INDEX idx_jobs_subcategory ON jobs(職務小類)")
        _db_con.execute("CREATE INDEX idx_popularity_id ON popularity(職缺編號)")
    return _db_con

def grabFromDatabase(
    tags: list[str],
    c0: list[str] | None = None,
    d0: list[str] | None = None,
) -> list[dict]:
    """Retrieve matching job listings from 職缺.csv using DuckDB.

    Steps:
    1. Classify tags into cities, salary_min, job_types, and job_terms.
    2. If c0 codes are provided, resolve to city names and merge with tag-based cities.
    3. If d0 codes are provided, resolve to job category names and add as 職務小類 filter.
    4. Semantically expand job_terms via 職務對照表.csv CodeAlike.
    5. Build a parameterised SQL query with WHERE conditions for each filter.
    6. LEFT JOIN 瀏覽次數.csv to attach popularity score (COALESCE to 0.0).
    7. Return results as list[dict].

    Args:
        tags: A list of tag strings produced by querytoRequirement().
        c0: Optional list of city code strings (from userSearchLog c0 column).
            These are resolved to city names via 城市對照表.csv.
        d0: Optional list of job category code strings (from userSearchLog d0 column).
            These are resolved to 職務小類 names via 職務對照表.csv.

    Returns:
        A list of dicts, each containing all 職缺.csv columns plus "score".
    """
    classified = classify_tags(tags)

    conditions: list[str] = []
    params: dict = {}

    # City filter: merge tag-based cities with c0-resolved cities
    all_cities = list(classified["cities"])
    if c0:
        all_cities.extend(resolve_c0_codes(c0))
    # Deduplicate while preserving order
    all_cities = list(dict.fromkeys(all_cities))

    if all_cities:
        placeholders = ", ".join(
            f"$city_{i}" for i in range(len(all_cities))
        )
        conditions.append(f"j.工作城市 IN ({placeholders})")
        for i, city in enumerate(all_cities):
            params[f"city_{i}"] = city

    # Salary filter
    if classified["salary_min"] is not None:
        conditions.append("j.薪資下限 >= $salary_min")
        params["salary_min"] = classified["salary_min"]

    # Job type filter (職缺屬性: 全職/兼職/工讀/etc.)
    if classified["job_types"]:
        placeholders = ", ".join(
            f"$jtype_{i}" for i in range(len(classified["job_types"]))
        )
        conditions.append(f"j.職缺屬性 IN ({placeholders})")
        for i, jtype in enumerate(classified["job_types"]):
            params[f"jtype_{i}"] = jtype

    # d0 filter: 職務小類 exact match
    d0_names: list[str] = []
    if d0:
        d0_names = resolve_d0_codes(d0)
    if d0_names:
        placeholders = ", ".join(
            f"$d0_{i}" for i in range(len(d0_names))
        )
        conditions.append(f"j.職務小類 IN ({placeholders})")
        for i, name in enumerate(d0_names):
            params[f"d0_{i}"] = name

    # Job-title filter: LLM has already done semantic expansion, so each tag
    # is matched directly via ILIKE on 職務名稱 OR exact match on 職務小類.
    # relevance_hits counts how many tags each job matches for ranking.
    # Only apply if no d0 filter is active (d0 is more precise)
    if classified["job_terms"] and not d0_names:
        term_cases = []
        all_subconditions = []

        for i, term in enumerate(classified["job_terms"]):
            # Match either: title contains term OR subcategory equals term
            subconditions = [
                f"j.職務名稱 ILIKE $jt_{i}",
                f"j.職務小類 = $jsc_{i}",
            ]
            params[f"jt_{i}"] = f"%{term}%"
            params[f"jsc_{i}"] = term

            term_condition = f"({' OR '.join(subconditions)})"
            all_subconditions.append(term_condition)
            term_cases.append(f"CASE WHEN {term_condition} THEN 1 ELSE 0 END")

        # WHERE: match ANY term (broad retrieval)
        conditions.append(f"({' OR '.join(all_subconditions)})")

        # Build relevance_hits as sum of matched terms
        relevance_expr = " + ".join(term_cases)
    else:
        relevance_expr = None

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    # Add relevance_hits column if we have job term matching
    relevance_select = f", ({relevance_expr}) AS relevance_hits" if relevance_expr else ", 0 AS relevance_hits"

    sql = f"""
        SELECT j.*, COALESCE(p.score, 0.0) AS score{relevance_select}
        FROM jobs j
        LEFT JOIN popularity p ON j.職缺編號 = p.職缺編號
        {where_clause}
    """

    con = _get_db()
    result = con.execute(sql, params).fetchall()
    columns = [desc[0] for desc in con.description]
    return [dict(zip(columns, row)) for row in result]
