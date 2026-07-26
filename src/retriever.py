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
            df["CodeNameA"].str.contains(term, na=False)
            | df["CodeNameB"].str.contains(term, na=False)
            | df["CodeNameC"].str.contains(term, na=False)
            | df["CodeAlike"].str.contains(term, na=False)
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

    Args:
        d0_codes: List of numeric job category code strings (e.g. ["160213", "120403"]).

    Returns:
        List of job category name strings (e.g. ["包裝員／作業員", "..."]).
        Unknown codes are silently dropped.
    """
    df = _load_job_lookup()
    names = []
    for code in d0_codes:
        try:
            code_int = int(code)
            row = df[df["CodeNo"] == code_int]
            if not row.empty:
                names.append(row.iloc[0]["CodeNameA"])
        except (ValueError, TypeError):
            pass
    return names


# ---------------------------------------------------------------------------
# Main retrieval function
# ---------------------------------------------------------------------------

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
    expanded_job_terms = semantic_expand(classified["job_terms"])

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

    # Job-title filter (case-insensitive substring via ILIKE)
    # Only apply if no d0 filter is active (d0 is more precise)
    if expanded_job_terms and not d0_names:
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
    try:
        result = con.execute(sql, params).fetchall()
        columns = [desc[0] for desc in con.description]
    finally:
        con.close()

    return [dict(zip(columns, row)) for row in result]
