"""Integration tests for the recommendation pipeline (recommend entry point).

Tests the full pipeline flow: querytoRequirement → grabFromDatabase → ranking.

**Validates: Requirements 6.1, 6.2, 6.3, 6.4**
"""

import csv
import json
from unittest.mock import patch

import pytest

from src.pipeline import recommend


# ---------------------------------------------------------------------------
# Helper: write a CSV from columns + rows
# ---------------------------------------------------------------------------

def _write_csv(path: str, columns: list[str], rows: list[list]) -> None:
    """Write a CSV file from column names and row data."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Fixture: mock pipeline environment with small CSVs
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_pipeline_env(tmp_path):
    """Set up small mock CSVs and patch all module paths/caches.

    Creates:
        - 職缺.csv (5 rows)
        - 瀏覽次數.csv (3 rows - jobs 1,2,3 have scores)
        - 城市對照表.csv (3 cities)
        - 職務對照表.csv (2 rows)
        - userBehaviorFeature.csv (2 users: one normal, one cold-start)

    Patches:
        - retriever paths (JOBS_PATH, VIEWS_PATH, CITY_TABLE_PATH, JOB_LOOKUP_PATH)
        - ranker paths (FEATURES_PATH)
        - retriever and ranker caches
        - tag_parser city loader
    """
    # 1. Write 職缺.csv (5 rows)
    jobs_path = str(tmp_path / "職缺.csv")
    _write_csv(jobs_path, [
        "職缺編號", "職務名稱", "工作城市", "薪資下限", "薪資上限",
        "職務中類", "職缺最後修改時間", "廠商編號", "職務內容",
    ], [
        [1, "前端工程師", "台北市", 40000, 60000, "軟體工程類", "2024-06-01", "C001", "前端開發"],
        [2, "後端工程師", "台北市", 50000, 70000, "軟體工程類", "2024-06-02", "C002", "後端開發"],
        [3, "前端工程師", "台中市", 35000, 55000, "軟體工程類", "2024-06-03", "C003", "網頁設計"],
        [4, "產品經理", "台北市", 45000, 65000, "管理類", "2024-06-04", "C004", "產品規劃"],
        [5, "後端工程師", "高雄市", 42000, 62000, "軟體工程類", "2024-06-05", "C005", "API開發"],
    ])

    # 2. Write 瀏覽次數.csv (3 rows - jobs 1,2,3 have scores)
    views_path = str(tmp_path / "瀏覽次數.csv")
    _write_csv(views_path, ["職缺編號", "瀏覽次數", "主動應徵次數", "score"], [
        [1, 100, 5, 8.0],
        [2, 200, 10, 12.0],
        [3, 80, 3, 6.0],
    ])

    # 3. Write 城市對照表.csv (3 cities)
    city_path = str(tmp_path / "城市對照表.csv")
    _write_csv(city_path, ["CodeNo", "CodeType", "CodeNameA"], [
        [1, 2, "台北市"],
        [2, 2, "台中市"],
        [3, 2, "高雄市"],
    ])

    # 4. Write 職務對照表.csv (2 rows)
    # NOTE: CodeNameC must have non-empty values to avoid pandas treating the
    # column as float (NaN), which breaks .str.contains() in semantic_expand.
    lookup_path = str(tmp_path / "職務對照表.csv")
    _write_csv(lookup_path, ["CodeNameA", "CodeNameB", "CodeNameC", "CodeAlike"], [
        ["前端工程師", "Frontend Engineer", "前端", "前端工程師,網頁工程師"],
        ["後端工程師", "Backend Engineer", "後端", "後端工程師,伺服器工程師"],
    ])

    # 5. Write userBehaviorFeature.csv (2 users)
    features_path = str(tmp_path / "userBehaviorFeature.csv")
    _write_csv(features_path, [
        "talentNo", "preferred_city_1", "preferred_city_2", "preferred_city_3",
        "preferred_category_1", "preferred_category_2", "preferred_category_3",
        "salary_floor", "total_events", "last_active", "is_cold_start",
    ], [
        [100, "台北市", "", "", "軟體工程類", "", "", 40000.0, 50, "2024-06-07", False],
        [200, "台中市", "", "", "管理類", "", "", 30000.0, 2, "2024-06-05", True],
    ])

    # Patch all module-level paths and reset caches
    import src.retriever as retriever_mod
    import src.ranker as ranker_mod
    import src.utils.tag_parser as tp_mod

    # Save originals for cleanup
    orig = {
        "JOBS_PATH": retriever_mod.JOBS_PATH,
        "VIEWS_PATH": retriever_mod.VIEWS_PATH,
        "CITY_TABLE_PATH": retriever_mod.CITY_TABLE_PATH,
        "JOB_LOOKUP_PATH": retriever_mod.JOB_LOOKUP_PATH,
        "FEATURES_PATH": ranker_mod.FEATURES_PATH,
        "_cities_cache": retriever_mod._cities_cache,
        "_job_lookup_cache": retriever_mod._job_lookup_cache,
        "_features_cache": ranker_mod._features_cache,
    }

    # Patch paths
    retriever_mod.JOBS_PATH = jobs_path
    retriever_mod.VIEWS_PATH = views_path
    retriever_mod.CITY_TABLE_PATH = city_path
    retriever_mod.JOB_LOOKUP_PATH = lookup_path
    ranker_mod.FEATURES_PATH = features_path

    # Reset caches
    retriever_mod._cities_cache = None
    retriever_mod._job_lookup_cache = None
    ranker_mod._features_cache = None

    # Reset tag_parser city loader to use the patched retriever loader
    tp_mod._load_known_cities = retriever_mod._load_known_cities

    yield

    # Restore originals
    retriever_mod.JOBS_PATH = orig["JOBS_PATH"]
    retriever_mod.VIEWS_PATH = orig["VIEWS_PATH"]
    retriever_mod.CITY_TABLE_PATH = orig["CITY_TABLE_PATH"]
    retriever_mod.JOB_LOOKUP_PATH = orig["JOB_LOOKUP_PATH"]
    ranker_mod.FEATURES_PATH = orig["FEATURES_PATH"]
    retriever_mod._cities_cache = orig["_cities_cache"]
    retriever_mod._job_lookup_cache = orig["_job_lookup_cache"]
    ranker_mod._features_cache = orig["_features_cache"]


# ---------------------------------------------------------------------------
# Helper: mock LLM to return specific tags
# ---------------------------------------------------------------------------

def _mock_llm_response(tags: list[str]):
    """Create a mock return value for ollama.chat that returns the given tags."""
    return {"message": {"content": json.dumps(tags)}}


# ---------------------------------------------------------------------------
# Test 1: Anonymous user returns by popularity
# ---------------------------------------------------------------------------

def test_recommend_anonymous_returns_by_popularity(mock_pipeline_env):
    """Anonymous user (talent_no=0): results sorted by score descending, ≤ 10 items.

    LLM returns ["前端工程師"] which matches jobs 1 and 3 (via CodeAlike expansion
    includes "前端工程師" and "網頁工程師" — both match 職務名稱 "前端工程師").
    Job 1 has score 8.0, Job 3 has score 6.0 → expect job 1 first.
    """
    with patch(
        "src.query_parser.ollama.chat",
        return_value=_mock_llm_response(["前端工程師"]),
    ):
        results = recommend("前端工程師", talent_no=0)

    # Results should be ≤ 10
    assert len(results) <= 10
    # Should have results (jobs 1 and 3 match "前端工程師")
    assert len(results) >= 1
    # First result should be job 1 (higher score 8.0 vs 6.0)
    assert results[0]["職缺編號"] == 1
    if len(results) > 1:
        assert results[1]["職缺編號"] == 3


# ---------------------------------------------------------------------------
# Test 2: Personalised normal user
# ---------------------------------------------------------------------------

def test_recommend_personalised_normal_user(mock_pipeline_env):
    """Normal user (talent_no=100): personalisation affects order.

    LLM returns ["後端工程師", "台北市"]. After expansion via CodeAlike,
    job terms include "後端工程師" and "伺服器工程師".
    City filter: 台北市 → only jobs in 台北市 with 後端工程師 match.
    Job 2: 後端工程師 in 台北市, score=12.0
    (Job 5 is in 高雄市 so filtered out by city)

    User 100 prefers: 台北市, 軟體工程類, salary_floor=40000
    Job 2: location_match=1.0, category_match=1.0, salary_match=1.0
    personal_score = 1.0*0.4 + 1.0*0.4 + 1.0*0.2 = 1.0
    """
    with patch(
        "src.query_parser.ollama.chat",
        return_value=_mock_llm_response(["後端工程師", "台北市"]),
    ):
        results = recommend("後端工程師 台北市", talent_no=100)

    # Should return at least 1 result
    assert len(results) >= 1
    assert len(results) <= 10
    # The top result should be job 2 (the only 後端工程師 in 台北市)
    assert results[0]["職缺編號"] == 2


# ---------------------------------------------------------------------------
# Test 3: Cold-start user falls back to popularity
# ---------------------------------------------------------------------------

def test_recommend_cold_start_user(mock_pipeline_env):
    """Cold-start user (talent_no=200, is_cold_start=True): falls back to popularity.

    LLM returns ["前端工程師"]. Same query as test 1.
    User 200 is cold-start → should rank by score descending (same as anonymous).
    """
    with patch(
        "src.query_parser.ollama.chat",
        return_value=_mock_llm_response(["前端工程師"]),
    ):
        results = recommend("前端工程師", talent_no=200)

    # Should behave like anonymous: same order as test 1
    assert len(results) >= 1
    assert len(results) <= 10
    # Job 1 has higher score than job 3
    assert results[0]["職缺編號"] == 1


# ---------------------------------------------------------------------------
# Test 4: No results returns empty list
# ---------------------------------------------------------------------------

def test_recommend_no_results_returns_empty(mock_pipeline_env):
    """When LLM returns tags that match nothing, recommend returns [].

    LLM returns ["不存在的職缺"] — won't match any 職務名稱 in our mock data.
    """
    with patch(
        "src.query_parser.ollama.chat",
        return_value=_mock_llm_response(["不存在的職缺"]),
    ):
        results = recommend("不存在的職缺", talent_no=0)

    assert results == []


# ---------------------------------------------------------------------------
# Test 5: No computed fields in output
# ---------------------------------------------------------------------------

def test_recommend_no_computed_fields_in_output(mock_pipeline_env):
    """No 'score', 'personal_score', 'final_score' keys in any result dict."""
    computed_fields = {"score", "personal_score", "final_score"}

    with patch(
        "src.query_parser.ollama.chat",
        return_value=_mock_llm_response(["前端工程師"]),
    ):
        results = recommend("前端工程師", talent_no=0)

    assert len(results) > 0, "Expected non-empty results to validate field names"
    for result in results:
        leaked = computed_fields & set(result.keys())
        assert leaked == set(), (
            f"Computed fields {leaked} leaked into output. Keys: {list(result.keys())}"
        )

    # Also test with personalised path
    with patch(
        "src.query_parser.ollama.chat",
        return_value=_mock_llm_response(["後端工程師"]),
    ):
        results_personal = recommend("後端工程師", talent_no=100)

    for result in results_personal:
        leaked = computed_fields & set(result.keys())
        assert leaked == set(), (
            f"Computed fields {leaked} leaked into personalised output. "
            f"Keys: {list(result.keys())}"
        )


# ---------------------------------------------------------------------------
# Test 6: Fewer than 10 results returns all (no padding)
# ---------------------------------------------------------------------------

def test_recommend_fewer_than_10_returns_all(mock_pipeline_env):
    """When only 2 matches exist, returns exactly 2 (no padding to 10).

    LLM returns ["前端工程師"] → matches jobs 1 and 3 (only 2 jobs).
    """
    with patch(
        "src.query_parser.ollama.chat",
        return_value=_mock_llm_response(["前端工程師"]),
    ):
        results = recommend("前端工程師", talent_no=0)

    # Should return exactly 2 (the matching jobs), not padded to 10
    assert len(results) == 2
