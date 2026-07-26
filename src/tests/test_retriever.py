"""Property-based tests for semantic_expand and grabFromDatabase.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 3.2, 3.3, 3.4**

Property 6: Matched tags are expanded with all CodeAlike terms
Property 7: Unmatched tags pass through unchanged
Property 8: Expanded job-title terms are deduplicated
Property 9: City filter is applied correctly
Property 10: Salary filter is applied correctly
Property 11: Job-title filter uses case-insensitive substring matching
"""

import os
import tempfile
from unittest.mock import patch

import pandas as pd
import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from src.retriever import semantic_expand

# ---------------------------------------------------------------------------
# Mock data — a controlled 職務對照表 DataFrame
# ---------------------------------------------------------------------------

MOCK_LOOKUP_DF = pd.DataFrame({
    "CodeNameA": ["軟體工程師", "前端工程師", "資料工程師"],
    "CodeNameB": ["Software Engineer", "Frontend Engineer", "Data Engineer"],
    "CodeNameC": ["", "", ""],
    "CodeAlike": [
        "軟體工程師,軟體設計師,程式設計師",
        "前端工程師,網頁工程師,Web Developer",
        "資料工程師,大數據工程師",
    ],
})

# Pre-computed sets for assertions
_ROW_ALIKE = {
    "軟體工程師": {"軟體工程師", "軟體設計師", "程式設計師"},
    "前端工程師": {"前端工程師", "網頁工程師", "Web Developer"},
    "資料工程師": {"資料工程師", "大數據工程師"},
}

# All terms that can trigger a match (CodeNameA, CodeNameB, CodeNameC, CodeAlike)
_ALL_MATCHABLE_TERMS = set(MOCK_LOOKUP_DF["CodeNameA"].tolist())
_ALL_MATCHABLE_TERMS.update(MOCK_LOOKUP_DF["CodeNameB"].tolist())
# CodeNameC values are empty strings, skip them
for alike_str in MOCK_LOOKUP_DF["CodeAlike"].tolist():
    _ALL_MATCHABLE_TERMS.update(t.strip() for t in alike_str.split(",") if t.strip())


def _mock_load_job_lookup():
    """Return the mock DataFrame instead of reading CSV."""
    return MOCK_LOOKUP_DF


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Strategy: a term that matches at least one row (by CodeNameA)
_matched_term = st.sampled_from(["軟體工程師", "前端工程師", "資料工程師"])

# Strategy: a term that does NOT match any row
_unmatched_term = st.text(
    alphabet=st.characters(whitelist_categories=("Lo",), min_codepoint=0x4E00, max_codepoint=0x9FFF),
    min_size=4,
    max_size=8,
).filter(lambda s: not any(
    s in val for col in ["CodeNameA", "CodeNameB", "CodeNameC", "CodeAlike"]
    for val in MOCK_LOOKUP_DF[col].dropna().tolist()
))

# Strategy: a list mixing matched and unmatched terms
_mixed_terms = st.lists(
    st.one_of(_matched_term, _unmatched_term),
    min_size=1,
    max_size=6,
)


# ---------------------------------------------------------------------------
# Property 6 — Matched tags are expanded with all CodeAlike terms
# ---------------------------------------------------------------------------

@given(
    matched=st.sampled_from(["軟體工程師", "前端工程師", "資料工程師"]),
    extra_unmatched=st.lists(_unmatched_term, min_size=0, max_size=3),
)
@settings(max_examples=200)
@patch("src.retriever._load_job_lookup", side_effect=_mock_load_job_lookup)
def test_property6_matched_tags_expanded_with_all_code_alike_terms(
    mock_lookup, matched: str, extra_unmatched: list[str]
) -> None:
    """**Validates: Requirements 2.1, 2.2**

    For any tag that matches a row in the lookup table, ALL comma-separated
    CodeAlike terms from that row SHALL appear in the expanded result.
    """
    input_terms = [matched] + extra_unmatched
    result = semantic_expand(input_terms)
    result_set = set(result)

    # All CodeAlike terms for the matched row must be in result
    expected_alike = _ROW_ALIKE[matched]
    for term in expected_alike:
        assert term in result_set, (
            f"Expected CodeAlike term '{term}' in result when input contains "
            f"'{matched}'. Got: {result}"
        )


@given(
    matched_terms=st.lists(
        st.sampled_from(["軟體工程師", "前端工程師", "資料工程師"]),
        min_size=1,
        max_size=3,
        unique=True,
    ),
)
@settings(max_examples=200)
@patch("src.retriever._load_job_lookup", side_effect=_mock_load_job_lookup)
def test_property6_multiple_matched_tags_all_expanded(
    mock_lookup, matched_terms: list[str]
) -> None:
    """**Validates: Requirements 2.1, 2.2**

    When multiple tags match different rows, ALL CodeAlike terms from ALL
    matched rows SHALL appear in the result.
    """
    result = semantic_expand(matched_terms)
    result_set = set(result)

    for matched in matched_terms:
        expected_alike = _ROW_ALIKE[matched]
        for term in expected_alike:
            assert term in result_set, (
                f"Expected CodeAlike term '{term}' for matched input '{matched}' "
                f"in result. Got: {result}"
            )


# ---------------------------------------------------------------------------
# Property 7 — Unmatched tags pass through unchanged
# ---------------------------------------------------------------------------

@given(
    unmatched_terms=st.lists(_unmatched_term, min_size=1, max_size=5, unique=True),
)
@settings(max_examples=200)
@patch("src.retriever._load_job_lookup", side_effect=_mock_load_job_lookup)
def test_property7_unmatched_tags_pass_through_unchanged(
    mock_lookup, unmatched_terms: list[str]
) -> None:
    """**Validates: Requirements 2.3**

    For any tag that does NOT match any row in the lookup table, the tag
    itself SHALL appear unchanged in the expanded result.
    """
    result = semantic_expand(unmatched_terms)
    result_set = set(result)

    for term in unmatched_terms:
        assert term in result_set, (
            f"Unmatched term '{term}' should pass through unchanged. "
            f"Got: {result}"
        )


@given(
    unmatched=_unmatched_term,
    matched=st.sampled_from(["軟體工程師", "前端工程師", "資料工程師"]),
)
@settings(max_examples=200)
@patch("src.retriever._load_job_lookup", side_effect=_mock_load_job_lookup)
def test_property7_unmatched_preserved_alongside_matched(
    mock_lookup, unmatched: str, matched: str
) -> None:
    """**Validates: Requirements 2.3**

    When mixing matched and unmatched terms, unmatched terms still pass
    through unchanged.
    """
    result = semantic_expand([unmatched, matched])
    result_set = set(result)

    assert unmatched in result_set, (
        f"Unmatched term '{unmatched}' should appear in result even when "
        f"matched terms are present. Got: {result}"
    )


# ---------------------------------------------------------------------------
# Property 8 — Expanded job-title terms are deduplicated
# ---------------------------------------------------------------------------

@given(terms=_mixed_terms)
@settings(max_examples=300)
@patch("src.retriever._load_job_lookup", side_effect=_mock_load_job_lookup)
def test_property8_expanded_terms_are_deduplicated(
    mock_lookup, terms: list[str]
) -> None:
    """**Validates: Requirements 2.4**

    For any combination of input tags, the result list SHALL contain no
    duplicate strings.
    """
    result = semantic_expand(terms)
    assert len(result) == len(set(result)), (
        f"Result contains duplicates. Result: {result}"
    )


@given(
    repeat_count=st.integers(min_value=2, max_value=5),
    matched=st.sampled_from(["軟體工程師", "前端工程師", "資料工程師"]),
)
@settings(max_examples=100)
@patch("src.retriever._load_job_lookup", side_effect=_mock_load_job_lookup)
def test_property8_duplicated_input_still_deduplicated(
    mock_lookup, repeat_count: int, matched: str
) -> None:
    """**Validates: Requirements 2.4**

    Even when the same matched term appears multiple times in the input,
    the result SHALL have no duplicates.
    """
    input_terms = [matched] * repeat_count
    result = semantic_expand(input_terms)
    assert len(result) == len(set(result)), (
        f"Result contains duplicates when input has repeated term '{matched}'. "
        f"Result: {result}"
    )


@patch("src.retriever._load_job_lookup", side_effect=_mock_load_job_lookup)
def test_property8_overlapping_code_alike_deduplicated(mock_lookup) -> None:
    """**Validates: Requirements 2.4**

    When multiple input terms match rows whose CodeAlike fields share
    overlapping terms, the result SHALL still be deduplicated.

    In our mock data, "軟體工程師" appears in its own CodeAlike. Passing it
    alongside another matched term tests the deduplication logic.
    """
    # Both "軟體工程師" and "前端工程師" produce distinct sets, but if we
    # add them both, the union should still have no duplicates.
    result = semantic_expand(["軟體工程師", "前端工程師", "軟體工程師"])
    assert len(result) == len(set(result)), (
        f"Result contains duplicates. Result: {result}"
    )


# ===========================================================================
# Property 12 & 13 — score field and column completeness for grabFromDatabase
# ===========================================================================
"""
Property 12: Every candidate has a score field, unmatched listings get 0.0
Property 13: Candidates contain all 職缺.csv columns plus score

**Validates: Requirements 3.5, 3.6**
"""

import os
import tempfile

from hypothesis import assume

from src.retriever import grabFromDatabase

# ---------------------------------------------------------------------------
# Shared fixture: temporary CSV files for grabFromDatabase tests
# ---------------------------------------------------------------------------

# Columns used in our mini 職缺.csv
_JOBS_COLUMNS = [
    "職缺編號", "職務名稱", "工作城市", "薪資下限", "薪資上限",
    "職務中類", "職缺最後修改時間", "廠商編號", "職務內容",
]

# 5-row job listings — job IDs 1..5
_JOBS_ROWS = [
    [1, "後端工程師", "台北市", 40000, 60000, "軟體工程類", "2026-06-01", "C001", "開發後端API"],
    [2, "前端工程師", "台北市", 35000, 55000, "軟體工程類", "2026-06-02", "C002", "切版與前端開發"],
    [3, "資料工程師", "高雄市", 38000, 58000, "軟體工程類", "2026-06-03", "C003", "資料管線建置"],
    [4, "產品經理", "台中市", 45000, 70000, "管理類", "2026-06-04", "C004", "產品規劃與管理"],
    [5, "後端工程師", "新竹市", 42000, 65000, "軟體工程類", "2026-06-05", "C005", "微服務開發"],
]

# 瀏覽次數.csv — only jobs 1, 2, 4 have scores; jobs 3 and 5 should get 0.0
_VIEWS_ROWS = [
    [1, 100, 5, 12.5],
    [2, 80, 3, 9.2],
    [4, 60, 2, 7.8],
]


def _write_csv(path: str, columns: list[str], rows: list[list]) -> None:
    """Write a simple CSV file from columns and rows."""
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)


@pytest.fixture
def temp_csv_env(tmp_path, monkeypatch):
    """Create temp CSV files and monkeypatch retriever paths.

    Yields:
        dict with keys "jobs_path" and "views_path" pointing to temp files.
    """
    # Write 職缺.csv
    jobs_path = str(tmp_path / "jobs.csv")
    _write_csv(jobs_path, _JOBS_COLUMNS, _JOBS_ROWS)

    # Write 瀏覽次數.csv (columns: 職缺編號, 瀏覽次數, 主動應徵次數, score)
    views_path = str(tmp_path / "views.csv")
    _write_csv(views_path, ["職缺編號", "瀏覽次數", "主動應徵次數", "score"], _VIEWS_ROWS)

    # Write a minimal 城市對照表.csv so _load_known_cities works
    city_path = str(tmp_path / "cities.csv")
    _write_csv(city_path, ["CodeType", "CodeNo", "CodeNameA"], [
        [2, 1, "台北市"],
        [2, 2, "高雄市"],
        [2, 3, "台中市"],
        [2, 4, "新竹市"],
    ])

    # Write a minimal 職務對照表.csv so semantic_expand works (empty — passthrough)
    lookup_path = str(tmp_path / "lookup.csv")
    _write_csv(lookup_path, ["CodeNameA", "CodeNameB", "CodeNameC", "CodeAlike"], [])

    # Patch the module-level paths in retriever.py
    import src.retriever as retriever_mod
    monkeypatch.setattr(retriever_mod, "JOBS_PATH", jobs_path)
    monkeypatch.setattr(retriever_mod, "VIEWS_PATH", views_path)
    monkeypatch.setattr(retriever_mod, "CITY_TABLE_PATH", city_path)
    monkeypatch.setattr(retriever_mod, "JOB_LOOKUP_PATH", lookup_path)

    # Clear caches so they reload from new paths
    monkeypatch.setattr(retriever_mod, "_cities_cache", None)
    monkeypatch.setattr(retriever_mod, "_job_lookup_cache", None)

    # Also reset tag_parser's city loader to use the patched retriever loader
    import src.utils.tag_parser as tp_mod
    monkeypatch.setattr(tp_mod, "_load_known_cities", retriever_mod._load_known_cities)

    return {"jobs_path": jobs_path, "views_path": views_path}


# ---------------------------------------------------------------------------
# Property 12 — Every candidate has a score field, unmatched listings get 0.0
# ---------------------------------------------------------------------------

# IDs that have entries in the views CSV
_IDS_WITH_SCORE = {1, 2, 4}


@given(
    tags=st.lists(
        st.sampled_from(["後端工程師", "前端工程師", "資料工程師", "產品經理"]),
        min_size=1,
        max_size=3,
        unique=True,
    ),
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property12_every_candidate_has_score_field(temp_csv_env, tags: list[str]) -> None:
    """**Validates: Requirements 3.5**

    Property 12: Every dict in the result has a "score" key with a float value.
    For jobs not in 瀏覽次數.csv, score must be 0.0.
    """
    results = grabFromDatabase(tags)
    assume(len(results) > 0)

    for candidate in results:
        # Every result must have a "score" key
        assert "score" in candidate, (
            f"Candidate missing 'score' key. Keys: {list(candidate.keys())}"
        )
        # Score must be a float (or int-like that can be treated as float)
        score = candidate["score"]
        assert isinstance(score, (int, float)), (
            f"Score should be numeric, got {type(score).__name__}: {score}"
        )
        # Jobs not in 瀏覽次數.csv must have score == 0.0
        job_id = candidate["職缺編號"]
        if job_id not in _IDS_WITH_SCORE:
            assert float(score) == 0.0, (
                f"Job {job_id} is not in 瀏覽次數.csv but has score={score}, "
                f"expected 0.0"
            )


# ---------------------------------------------------------------------------
# Property 13 — Candidates contain all 職缺.csv columns plus score
# ---------------------------------------------------------------------------

_EXPECTED_COLUMNS = set(_JOBS_COLUMNS) | {"score"}


@given(
    tags=st.lists(
        st.sampled_from(["後端工程師", "前端工程師", "資料工程師", "產品經理"]),
        min_size=1,
        max_size=3,
        unique=True,
    ),
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property13_candidates_contain_all_columns_plus_score(
    temp_csv_env, tags: list[str]
) -> None:
    """**Validates: Requirements 3.6**

    Property 13: Every dict in the result contains all column names from
    職缺.csv PLUS the "score" key, and no other keys.
    """
    results = grabFromDatabase(tags)
    assume(len(results) > 0)

    for candidate in results:
        candidate_keys = set(candidate.keys())
        assert candidate_keys == _EXPECTED_COLUMNS, (
            f"Expected keys {_EXPECTED_COLUMNS}, got {candidate_keys}. "
            f"Missing: {_EXPECTED_COLUMNS - candidate_keys}, "
            f"Extra: {candidate_keys - _EXPECTED_COLUMNS}"
        )


# ---------------------------------------------------------------------------
# Property 9 — City filter is applied correctly
# ---------------------------------------------------------------------------

# Test cities available in the mock data
_TEST_CITIES = ["台北市", "台中市", "高雄市"]

# Fixture CSV content
_MOCK_JOBS_CSV = """職缺編號,職務名稱,工作城市,薪資下限,薪資上限,職務中類,職缺最後修改時間,廠商編號,職務內容
1,前端工程師,台北市,40000,60000,軟體設計,2024-01-01,100,描述1
2,後端工程師,台中市,50000,70000,軟體設計,2024-01-02,101,描述2
3,行政助理,台北市,30000,40000,行政/總務,2024-01-03,102,描述3
4,資料工程師,高雄市,45000,65000,軟體設計,2024-01-04,103,描述4
5,前端工程師,台中市,35000,55000,軟體設計,2024-01-05,104,描述5
"""

_MOCK_VIEWS_CSV = """職缺編號,瀏覽次數,主動應徵次數,score
1,100,5,2.5
2,200,10,5.0
4,50,2,1.0
"""

_MOCK_CITY_TABLE_CSV = """CodeNo,CodeType,CodeNameA
1,2,台北市
2,2,台中市
3,2,高雄市
4,2,新北市
"""

_MOCK_JOB_LOOKUP_CSV = 'CodeNameA,CodeNameB,CodeNameC,CodeAlike\n前端工程師,Frontend Engineer,前端,"前端工程師,網頁工程師"\n後端工程師,Backend Engineer,後端,"後端工程師,伺服器工程師"\n'


@pytest.fixture
def mock_csv_env(tmp_path):
    """Create temporary CSV files and patch retriever paths and caches."""
    # Write mock CSV files
    jobs_path = tmp_path / "職缺.csv"
    views_path = tmp_path / "瀏覽次數.csv"
    city_table_path = tmp_path / "城市對照表.csv"
    job_lookup_path = tmp_path / "職務對照表.csv"

    jobs_path.write_text(_MOCK_JOBS_CSV.strip(), encoding="utf-8")
    views_path.write_text(_MOCK_VIEWS_CSV.strip(), encoding="utf-8")
    city_table_path.write_text(_MOCK_CITY_TABLE_CSV.strip(), encoding="utf-8")
    job_lookup_path.write_text(_MOCK_JOB_LOOKUP_CSV.strip(), encoding="utf-8")

    return {
        "jobs_path": str(jobs_path),
        "views_path": str(views_path),
        "city_table_path": str(city_table_path),
        "job_lookup_path": str(job_lookup_path),
    }


def _run_grab_with_mock(mock_csv_env, tags):
    """Helper: call grabFromDatabase with patched paths and reset caches."""
    import src.retriever as retriever_module

    # Reset caches so they reload from the temp files
    original_cities_cache = retriever_module._cities_cache
    original_job_lookup_cache = retriever_module._job_lookup_cache

    try:
        retriever_module._cities_cache = None
        retriever_module._job_lookup_cache = None

        with patch.object(retriever_module, "JOBS_PATH", mock_csv_env["jobs_path"]), \
             patch.object(retriever_module, "VIEWS_PATH", mock_csv_env["views_path"]), \
             patch.object(retriever_module, "CITY_TABLE_PATH", mock_csv_env["city_table_path"]), \
             patch.object(retriever_module, "JOB_LOOKUP_PATH", mock_csv_env["job_lookup_path"]):
            result = retriever_module.grabFromDatabase(tags)
    finally:
        # Restore caches to avoid polluting other tests
        retriever_module._cities_cache = original_cities_cache
        retriever_module._job_lookup_cache = original_job_lookup_cache

    return result


# Strategy: generate a non-empty list of cities from the test set
_city_tags = st.lists(
    st.sampled_from(_TEST_CITIES),
    min_size=1,
    max_size=3,
    unique=True,
)


@given(cities=_city_tags)
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property9_city_filter_applied_correctly(cities, mock_csv_env):
    """**Validates: Requirements 3.2**

    For any tag list containing one or more city terms, every dict in the
    returned Candidate Set SHALL have its 工作城市 field equal to one of
    the city terms extracted from the tag list.
    """
    # Tags consist only of city names — this isolates the city filter
    tags = list(cities)
    result = _run_grab_with_mock(mock_csv_env, tags)

    city_set = set(cities)
    for row in result:
        assert row["工作城市"] in city_set, (
            f"Expected 工作城市 to be in {city_set}, got '{row['工作城市']}'. "
            f"Tags: {tags}, Result row: {row}"
        )


@given(
    cities=_city_tags,
    job_term=st.sampled_from(["前端工程師", "後端工程師", "資料工程師", "行政助理"]),
)
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property9_city_filter_with_job_terms(cities, job_term, mock_csv_env):
    """**Validates: Requirements 3.2**

    When tags contain both city terms and job-title terms, the city filter
    still ensures every result's 工作城市 is in the specified city set.
    """
    tags = list(cities) + [job_term]
    result = _run_grab_with_mock(mock_csv_env, tags)

    city_set = set(cities)
    for row in result:
        assert row["工作城市"] in city_set, (
            f"Expected 工作城市 to be in {city_set}, got '{row['工作城市']}'. "
            f"Tags: {tags}, Result row job: {row.get('職務名稱')}"
        )


# ===========================================================================
# Property 11 — Job-title filter uses case-insensitive substring matching
# ===========================================================================
"""
Property 11: Job-title filter uses case-insensitive substring matching

**Validates: Requirements 3.4**

For any tag list containing job-title terms (after semantic expansion),
every dict in the returned Candidate Set SHALL have a 職務名稱 that contains
at least one of the expanded job-title terms (case-insensitive substring match).
"""


# ---------------------------------------------------------------------------
# Fixture for Property 11: jobs with specific names for partial-match testing
# ---------------------------------------------------------------------------

_P11_JOBS_COLUMNS = [
    "職缺編號", "職務名稱", "工作城市", "薪資下限", "薪資上限",
    "職務中類", "職缺最後修改時間", "廠商編號", "職務內容",
]

_P11_JOBS_ROWS = [
    [1, "前端工程師", "台北市", 35000, 55000, "軟體工程類", "2026-06-01", "C001", "前端開發"],
    [2, "後端工程師", "台北市", 40000, 60000, "軟體工程類", "2026-06-02", "C002", "後端開發"],
    [3, "行政助理", "高雄市", 28000, 35000, "行政類", "2026-06-03", "C003", "行政支援"],
    [4, "資料工程師", "台中市", 38000, 58000, "軟體工程類", "2026-06-04", "C004", "資料管線"],
    [5, "前端工程師", "新竹市", 36000, 56000, "軟體工程類", "2026-06-05", "C005", "前端開發"],
]

# Map each partial term to the set of 職務名稱 values it should match
_P11_TERM_TO_MATCHING_NAMES = {
    "前端": {"前端工程師"},
    "後端": {"後端工程師"},
    "工程師": {"前端工程師", "後端工程師", "資料工程師"},
    "資料": {"資料工程師"},
}


@pytest.fixture
def temp_csv_env_p11(tmp_path, monkeypatch):
    """Create temp CSV files for Property 11 with controlled job listings.

    Uses 5 specific jobs designed to test partial substring matching.
    The 職務對照表 is empty so semantic_expand passes terms through unchanged.
    """
    # Write 職缺.csv
    jobs_path = str(tmp_path / "jobs.csv")
    _write_csv(jobs_path, _P11_JOBS_COLUMNS, _P11_JOBS_ROWS)

    # Write 瀏覽次數.csv (minimal — one dummy row to establish column types)
    views_path = str(tmp_path / "views.csv")
    _write_csv(views_path, ["職缺編號", "瀏覽次數", "主動應徵次數", "score"], [
        [999, 10, 1, 5.0],
    ])

    # Write a minimal 城市對照表.csv
    city_path = str(tmp_path / "cities.csv")
    _write_csv(city_path, ["CodeType", "CodeNo", "CodeNameA"], [
        [2, 1, "台北市"],
        [2, 2, "高雄市"],
        [2, 3, "台中市"],
        [2, 4, "新竹市"],
    ])

    # Write an empty 職務對照表.csv — so semantic_expand passes terms through
    lookup_path = str(tmp_path / "lookup.csv")
    _write_csv(lookup_path, ["CodeNameA", "CodeNameB", "CodeNameC", "CodeAlike"], [])

    # Patch the module-level paths in retriever.py
    import src.retriever as retriever_mod
    monkeypatch.setattr(retriever_mod, "JOBS_PATH", jobs_path)
    monkeypatch.setattr(retriever_mod, "VIEWS_PATH", views_path)
    monkeypatch.setattr(retriever_mod, "CITY_TABLE_PATH", city_path)
    monkeypatch.setattr(retriever_mod, "JOB_LOOKUP_PATH", lookup_path)

    # Clear caches so they reload from new paths
    monkeypatch.setattr(retriever_mod, "_cities_cache", None)
    monkeypatch.setattr(retriever_mod, "_job_lookup_cache", None)

    # Also reset tag_parser's city loader to use the patched retriever loader
    import src.utils.tag_parser as tp_mod
    monkeypatch.setattr(tp_mod, "_load_known_cities", retriever_mod._load_known_cities)

    return {"jobs_path": jobs_path, "views_path": views_path}


# ---------------------------------------------------------------------------
# Property 11 — Tests
# ---------------------------------------------------------------------------

@given(
    search_term=st.sampled_from(["前端", "後端", "工程師", "資料"]),
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property11_job_title_filter_case_insensitive_substring(
    temp_csv_env_p11, search_term: str
) -> None:
    """**Validates: Requirements 3.4**

    Property 11: For any tag list containing job-title terms (after semantic
    expansion), every dict in the returned Candidate Set SHALL have a 職務名稱
    that contains at least one of the expanded job-title terms (case-insensitive
    substring match).
    """
    # Tags are just the search term — since 職務對照表 is empty,
    # semantic_expand will pass it through unchanged
    tags = [search_term]
    results = grabFromDatabase(tags)

    # There should be at least one result for each of our terms
    assert len(results) > 0, (
        f"Expected results for search term '{search_term}', got none."
    )

    # Every returned candidate's 職務名稱 must contain the search term
    # (case-insensitive substring)
    for candidate in results:
        job_name = candidate["職務名稱"]
        assert search_term.lower() in job_name.lower(), (
            f"Candidate 職務名稱='{job_name}' does not contain search term "
            f"'{search_term}' (case-insensitive). Tags={tags}"
        )


@given(
    terms=st.lists(
        st.sampled_from(["前端", "後端", "工程師", "資料"]),
        min_size=1,
        max_size=3,
        unique=True,
    ),
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property11_multiple_job_title_terms_or_matching(
    temp_csv_env_p11, terms: list[str]
) -> None:
    """**Validates: Requirements 3.4**

    Property 11: When multiple job-title terms are present, each returned
    candidate's 職務名稱 must contain at least ONE of the terms (OR logic,
    case-insensitive substring).
    """
    tags = terms
    results = grabFromDatabase(tags)

    # Should have results since our terms all match at least one job
    assert len(results) > 0, (
        f"Expected results for terms {terms}, got none."
    )

    for candidate in results:
        job_name = candidate["職務名稱"].lower()
        matches_any = any(term.lower() in job_name for term in terms)
        assert matches_any, (
            f"Candidate 職務名稱='{candidate['職務名稱']}' does not contain "
            f"any of the search terms {terms} (case-insensitive substring)."
        )


@given(
    search_term=st.sampled_from(["前端工程師"]),
)
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property11_full_job_title_exact_match(
    temp_csv_env_p11, search_term: str
) -> None:
    """**Validates: Requirements 3.4**

    Property 11: When tags contain a full job title like "前端工程師",
    all results have 職務名稱 containing that term.
    """
    tags = [search_term]
    results = grabFromDatabase(tags)

    assert len(results) > 0, (
        f"Expected results for '{search_term}', got none."
    )

    for candidate in results:
        job_name = candidate["職務名稱"]
        assert search_term.lower() in job_name.lower(), (
            f"Candidate 職務名稱='{job_name}' does not contain "
            f"'{search_term}' (case-insensitive)."
        )

    # Specifically for "前端工程師" we expect exactly 2 results (jobs 1 and 5)
    assert len(results) == 2, (
        f"Expected 2 results for '前端工程師', got {len(results)}."
    )


# ===========================================================================
# Property 10 — Salary filter is applied correctly
# ===========================================================================
"""
Property 10: Salary filter is applied correctly

**Validates: Requirements 3.3**
"""

# ---------------------------------------------------------------------------
# Fixture: temp CSV environment with known salary values for Property 10
# ---------------------------------------------------------------------------

# Job listings with specific salary floors for salary filter testing
_SALARY_JOBS_ROWS = [
    [101, "後端工程師", "台北市", 40000, 60000, "軟體工程類", "2026-06-01", "C001", "開發後端API"],
    [102, "前端工程師", "台北市", 50000, 75000, "軟體工程類", "2026-06-02", "C002", "切版與前端開發"],
    [103, "資料工程師", "高雄市", 30000, 45000, "軟體工程類", "2026-06-03", "C003", "資料管線建置"],
    [104, "產品經理", "台中市", 45000, 70000, "管理類", "2026-06-04", "C004", "產品規劃與管理"],
    [105, "後端工程師", "新竹市", 35000, 55000, "軟體工程類", "2026-06-05", "C005", "微服務開發"],
]

# Known salary floors: 40000, 50000, 30000, 45000, 35000
_SALARY_FLOORS = [40000, 50000, 30000, 45000, 35000]


@pytest.fixture
def salary_csv_env(tmp_path, monkeypatch):
    """Create temp CSV files with known salary values for Property 10 testing.

    Jobs:
        101: 薪資下限=40000
        102: 薪資下限=50000
        103: 薪資下限=30000
        104: 薪資下限=45000
        105: 薪資下限=35000
    """
    # Write 職缺.csv
    jobs_path = str(tmp_path / "jobs.csv")
    _write_csv(jobs_path, _JOBS_COLUMNS, _SALARY_JOBS_ROWS)

    # Write 瀏覽次數.csv (all jobs have a score for simplicity)
    views_path = str(tmp_path / "views.csv")
    _write_csv(views_path, ["職缺編號", "瀏覽次數", "主動應徵次數", "score"], [
        [101, 100, 5, 10.0],
        [102, 80, 3, 8.0],
        [103, 60, 2, 6.0],
        [104, 50, 1, 5.0],
        [105, 40, 1, 4.0],
    ])

    # Write a minimal 城市對照表.csv
    city_path = str(tmp_path / "cities.csv")
    _write_csv(city_path, ["CodeType", "CodeNo", "CodeNameA"], [
        [2, 1, "台北市"],
        [2, 2, "高雄市"],
        [2, 3, "台中市"],
        [2, 4, "新竹市"],
    ])

    # Write a minimal 職務對照表.csv (empty — passthrough)
    lookup_path = str(tmp_path / "lookup.csv")
    _write_csv(lookup_path, ["CodeNameA", "CodeNameB", "CodeNameC", "CodeAlike"], [])

    # Patch the module-level paths in retriever.py
    import src.retriever as retriever_mod
    monkeypatch.setattr(retriever_mod, "JOBS_PATH", jobs_path)
    monkeypatch.setattr(retriever_mod, "VIEWS_PATH", views_path)
    monkeypatch.setattr(retriever_mod, "CITY_TABLE_PATH", city_path)
    monkeypatch.setattr(retriever_mod, "JOB_LOOKUP_PATH", lookup_path)

    # Clear caches so they reload from new paths
    monkeypatch.setattr(retriever_mod, "_cities_cache", None)
    monkeypatch.setattr(retriever_mod, "_job_lookup_cache", None)

    # Also reset tag_parser's city loader to use the patched retriever loader
    import src.utils.tag_parser as tp_mod
    monkeypatch.setattr(tp_mod, "_load_known_cities", retriever_mod._load_known_cities)

    return {"jobs_path": jobs_path, "views_path": views_path}


# ---------------------------------------------------------------------------
# Property 10 — Salary filter is applied correctly
# ---------------------------------------------------------------------------

@given(
    salary_threshold=st.integers(min_value=25000, max_value=55000),
)
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property10_salary_filter_applied_correctly(
    salary_csv_env, salary_threshold: int
) -> None:
    """**Validates: Requirements 3.3**

    Property 10: For any tag list containing a Salary Tag "薪資>=N", every
    dict in the returned Candidate Set SHALL have 薪資下限 >= N.
    """
    tags = [f"薪資>={salary_threshold}"]
    results = grabFromDatabase(tags)

    for candidate in results:
        salary_floor = candidate["薪資下限"]
        assert salary_floor >= salary_threshold, (
            f"Candidate job {candidate['職缺編號']} has 薪資下限={salary_floor} "
            f"which is less than the filter threshold {salary_threshold}. "
            f"Tags: {tags}"
        )


@given(
    salary_threshold=st.integers(min_value=25000, max_value=55000),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property10_salary_filter_result_count_consistent(
    salary_csv_env, salary_threshold: int
) -> None:
    """**Validates: Requirements 3.3**

    The number of results returned should match the count of jobs whose
    薪資下限 >= threshold in the test dataset.
    """
    tags = [f"薪資>={salary_threshold}"]
    results = grabFromDatabase(tags)

    # Count how many jobs in our fixture have 薪資下限 >= threshold
    expected_count = sum(
        1 for row in _SALARY_JOBS_ROWS if row[3] >= salary_threshold
    )
    assert len(results) == expected_count, (
        f"Expected {expected_count} results for threshold {salary_threshold}, "
        f"got {len(results)}. Salary floors in dataset: {_SALARY_FLOORS}"
    )
