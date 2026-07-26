"""Property-based tests for the Ranker module.

Property 15: Personal score formula is computed correctly
Property 16: Final score formula is applied with normalised popularity
Property 18: No computed fields leak into ranking output
Property 19: Normal users are ranked by final_score descending

**Validates: Requirements 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 4.6, 5.9, 5.10**
"""

import math
from unittest.mock import patch

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from src.ranker import _compute_personal_score, _personalised_rank, ranking


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_feature_strategy = st.fixed_dictionaries({
    "preferred_city_1": st.sampled_from(["台北市", "台中市", "高雄市", None]),
    "preferred_city_2": st.sampled_from(["新北市", "桃園市", None]),
    "preferred_city_3": st.sampled_from(["新竹市", None]),
    "preferred_category_1": st.sampled_from(["軟體工程類", "管理類", "設計類", None]),
    "preferred_category_2": st.sampled_from(["資訊類", None]),
    "preferred_category_3": st.sampled_from(["行銷類", None]),
    "salary_floor": st.one_of(
        st.none(),
        st.just(float("nan")),
        st.floats(25000, 80000, allow_nan=False, allow_infinity=False),
    ),
})

_candidate_strategy = st.fixed_dictionaries({
    "工作城市": st.sampled_from(["台北市", "台中市", "高雄市", "新北市", "桃園市", "新竹市", "嘉義市"]),
    "職務中類": st.sampled_from(["軟體工程類", "管理類", "設計類", "資訊類", "行銷類", "其他"]),
    "薪資下限": st.one_of(st.none(), st.integers(20000, 100000)),
})


# ---------------------------------------------------------------------------
# Property 15 — Personal score formula is computed correctly
# ---------------------------------------------------------------------------

@given(candidate=_candidate_strategy, feature=_feature_strategy)
@settings(max_examples=500)
def test_property15_personal_score_formula_computed_correctly(
    candidate: dict, feature: dict
) -> None:
    """**Validates: Requirements 5.2, 5.3, 5.4, 5.5, 5.6**

    Property 15: For any normal user feature row and candidate dict,
    _compute_personal_score SHALL return exactly
    location_match * 0.4 + category_match * 0.4 + salary_match * 0.2
    """
    # Compute expected location_match
    pref_cities = {
        feature.get("preferred_city_1"),
        feature.get("preferred_city_2"),
        feature.get("preferred_city_3"),
    } - {None, ""}
    expected_location = 1.0 if candidate["工作城市"] in pref_cities else 0.0

    # Compute expected category_match
    pref_cats = {
        feature.get("preferred_category_1"),
        feature.get("preferred_category_2"),
        feature.get("preferred_category_3"),
    } - {None, ""}
    expected_category = 1.0 if candidate["職務中類"] in pref_cats else 0.0

    # Compute expected salary_match
    salary_floor = feature.get("salary_floor")
    if salary_floor is None or (isinstance(salary_floor, float) and math.isnan(salary_floor)):
        expected_salary = 0.5
    else:
        expected_salary = 1.0 if (candidate["薪資下限"] or 0) >= salary_floor else 0.0

    expected_score = expected_location * 0.4 + expected_category * 0.4 + expected_salary * 0.2

    actual_score = _compute_personal_score(candidate, feature)

    assert math.isclose(actual_score, expected_score, rel_tol=1e-9), (
        f"Expected personal_score={expected_score}, got {actual_score}. "
        f"location={expected_location}, category={expected_category}, "
        f"salary={expected_salary}. "
        f"Candidate={candidate}, Feature={feature}"
    )


# ---------------------------------------------------------------------------
# Candidate strategy with score field (for Property 16)
# ---------------------------------------------------------------------------

_candidate_with_score = st.fixed_dictionaries({
    "職缺編號": st.integers(1, 1000),
    "職務名稱": st.text(min_size=1, max_size=10),
    "工作城市": st.sampled_from(["台北市", "台中市", "高雄市", "新北市", "桃園市"]),
    "薪資下限": st.integers(25000, 80000),
    "薪資上限": st.integers(60000, 150000),
    "職務中類": st.sampled_from(["軟體工程類", "管理類", "設計類", "資訊類"]),
    "職缺最後修改時間": st.sampled_from(["2024-01-01", "2024-02-01", "2024-03-01"]),
    "廠商編號": st.text(min_size=1, max_size=5),
    "職務內容": st.text(min_size=1, max_size=20),
    "score": st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
})


# ---------------------------------------------------------------------------
# Property 16 — Final score formula is applied with normalised popularity
# ---------------------------------------------------------------------------

@given(
    candidates=st.lists(_candidate_with_score, min_size=2, max_size=15),
    feature=_feature_strategy,
)
@settings(max_examples=500)
def test_property16_final_score_with_nonzero_max_score(
    candidates: list, feature: dict
) -> None:
    """**Validates: Requirements 5.7, 5.8**

    Property 16 (non-zero case): For any normal user and candidate set where
    max(score) > 0, final_score SHALL equal
    personal_score * 0.7 + (score / max_score) * 0.3.
    Verify that _personalised_rank returns candidates in the correct order
    (descending by expected final_score).
    """
    # Ensure at least one candidate has a non-zero score
    assume(max(c["score"] for c in candidates) > 0.0)

    raw_fields = [k for k in candidates[0].keys() if k != "score"]

    # Compute expected final_score for each candidate
    max_score = max(c["score"] for c in candidates)

    expected_scores = []
    for c in candidates:
        personal = _compute_personal_score(c, feature)
        pop = c["score"] / max_score
        final = personal * 0.7 + pop * 0.3
        expected_scores.append((final, c))

    # Sort by expected final_score descending
    expected_scores.sort(key=lambda x: x[0], reverse=True)
    expected_top10 = expected_scores[:10]

    # Call the actual function
    result = _personalised_rank(candidates, feature, raw_fields)

    # Verify result length
    assert len(result) == min(len(candidates), 10)

    # Verify order matches expected final_score descending
    for i, (expected_final, expected_candidate) in enumerate(expected_top10):
        for field in raw_fields:
            assert result[i][field] == expected_candidate[field], (
                f"Mismatch at position {i}, field '{field}': "
                f"expected {expected_candidate[field]}, got {result[i][field]}. "
                f"Expected final_score={expected_final}"
            )


@given(
    candidates=st.lists(
        _candidate_with_score.map(lambda c: {**c, "score": 0.0}),
        min_size=2,
        max_size=15,
    ),
    feature=_feature_strategy,
)
@settings(max_examples=500)
def test_property16_final_score_with_zero_max_score(
    candidates: list, feature: dict
) -> None:
    """**Validates: Requirements 5.7, 5.8**

    Property 16 (zero case): When max(score) == 0.0 for all candidates,
    the popularity component SHALL be 0.0 for all candidates, meaning
    ordering is driven entirely by personal_score.
    """
    raw_fields = [k for k in candidates[0].keys() if k != "score"]

    # All scores are 0.0, so popularity component is 0.0 for everyone
    # final_score = personal_score * 0.7 + 0.0 * 0.3 = personal_score * 0.7
    expected_scores = []
    for c in candidates:
        personal = _compute_personal_score(c, feature)
        final = personal * 0.7 + 0.0 * 0.3
        expected_scores.append((final, c))

    # Sort by expected final_score descending
    expected_scores.sort(key=lambda x: x[0], reverse=True)
    expected_top10 = expected_scores[:10]

    # Call the actual function
    result = _personalised_rank(candidates, feature, raw_fields)

    # Verify result length
    assert len(result) == min(len(candidates), 10)

    # Verify order matches expected final_score descending (driven by personal_score)
    for i, (expected_final, expected_candidate) in enumerate(expected_top10):
        for field in raw_fields:
            assert result[i][field] == expected_candidate[field], (
                f"Mismatch at position {i}, field '{field}': "
                f"expected {expected_candidate[field]}, got {result[i][field]}. "
                f"Expected final_score={expected_final} (pure personal_score * 0.7)"
            )


# ---------------------------------------------------------------------------
# Property 17 — Output length is min(len(candidates), 10)
# ---------------------------------------------------------------------------

from unittest.mock import patch
from src.ranker import ranking

_candidate_with_score_p17 = st.fixed_dictionaries({
    "職缺編號": st.integers(1, 1000),
    "職務名稱": st.text(min_size=1, max_size=10),
    "工作城市": st.sampled_from(["台北市", "台中市"]),
    "薪資下限": st.integers(25000, 80000),
    "薪資上限": st.integers(60000, 150000),
    "職務中類": st.sampled_from(["軟體工程類", "管理類"]),
    "職缺最後修改時間": st.sampled_from(["2024-01-01", "2024-02-01"]),
    "廠商編號": st.text(min_size=1, max_size=5),
    "職務內容": st.text(min_size=1, max_size=20),
    "score": st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
})


@given(
    candidates=st.lists(_candidate_with_score_p17, min_size=0, max_size=20),
    talent_no=st.sampled_from([0, 123, 456]),
)
@settings(max_examples=300)
def test_property17_output_length(candidates: list, talent_no: int) -> None:
    """**Validates: Requirements 4.1, 6.3**

    Property 17: For any candidate set of size n >= 0 and any talent_no,
    ranking SHALL return exactly min(n, 10) items.
    """
    with patch("src.ranker._get_user_feature", return_value=None):
        result = ranking(candidates, talent_no)
    assert len(result) == min(len(candidates), 10), (
        f"Expected {min(len(candidates), 10)} items, got {len(result)}. "
        f"Candidates size={len(candidates)}, talent_no={talent_no}"
    )


# ---------------------------------------------------------------------------
# Property 18 — No computed fields leak into ranking output
# ---------------------------------------------------------------------------

FORBIDDEN_KEYS = {"score", "personal_score", "final_score"}

# Raw fields that should be in the output (from 職缺.csv)
RAW_FIELDS = {"職缺編號", "職務名稱", "工作城市", "薪資下限", "薪資上限", "職務中類", "職缺最後修改時間", "廠商編號", "職務內容"}


@given(candidates=st.lists(_candidate_with_score, min_size=1, max_size=15))
@settings(max_examples=500)
def test_property18_no_computed_fields_popularity_path(candidates: list) -> None:
    """**Validates: Requirements 4.6, 5.10**

    Property 18 (popularity path): For any call to ranking with talent_no=0,
    no returned dict SHALL contain the keys "score", "personal_score",
    "final_score", or any other key not present in the original 職缺.csv columns.
    """
    with patch("src.ranker._get_user_feature", return_value=None):
        result = ranking(candidates, 0)
    for item in result:
        for key in FORBIDDEN_KEYS:
            assert key not in item, (
                f"Forbidden key '{key}' found in output item: {item}"
            )
        assert set(item.keys()) == RAW_FIELDS, (
            f"Output keys mismatch. Expected {RAW_FIELDS}, got {set(item.keys())}"
        )


@given(candidates=st.lists(_candidate_with_score, min_size=1, max_size=15))
@settings(max_examples=500)
def test_property18_no_computed_fields_personalised_path(candidates: list) -> None:
    """**Validates: Requirements 4.6, 5.10**

    Property 18 (personalised path): For any call to ranking with a normal user,
    no returned dict SHALL contain the keys "score", "personal_score",
    "final_score", or any other key not present in the original 職缺.csv columns.
    """
    feature = {
        "preferred_city_1": "台北市",
        "preferred_city_2": None,
        "preferred_city_3": None,
        "preferred_category_1": "軟體工程類",
        "preferred_category_2": None,
        "preferred_category_3": None,
        "salary_floor": 35000.0,
        "is_cold_start": False,
    }
    with patch("src.ranker._get_user_feature", return_value=feature):
        result = ranking(candidates, 999)
    for item in result:
        for key in FORBIDDEN_KEYS:
            assert key not in item, (
                f"Forbidden key '{key}' found in output item: {item}"
            )
        assert set(item.keys()) == RAW_FIELDS, (
            f"Output keys mismatch. Expected {RAW_FIELDS}, got {set(item.keys())}"
        )


# ---------------------------------------------------------------------------
# Property 19 — Normal users are ranked by final_score descending
# ---------------------------------------------------------------------------

_normal_feature = {
    "preferred_city_1": "台北市",
    "preferred_city_2": "新北市",
    "preferred_city_3": None,
    "preferred_category_1": "軟體工程類",
    "preferred_category_2": "資訊類",
    "preferred_category_3": None,
    "salary_floor": 35000.0,
    "is_cold_start": False,
}


@given(candidates=st.lists(_candidate_with_score, min_size=2, max_size=15))
@settings(max_examples=300)
def test_property19_normal_users_ranked_by_final_score_desc(candidates):
    """**Validates: Requirements 5.9**

    Property 19: For any candidate set and a normal user (authenticated,
    is_cold_start=False), the returned list SHALL be a prefix of the
    candidates sorted by final_score descending.
    """
    with patch("src.ranker._get_user_feature", return_value=_normal_feature):
        result = ranking(candidates, 999)

    # Compute expected final_scores
    max_score = max(c["score"] for c in candidates) if candidates else 0.0

    def compute_final(c):
        personal = _compute_personal_score(c, _normal_feature)
        pop = (c["score"] / max_score) if max_score > 0.0 else 0.0
        return personal * 0.7 + pop * 0.3

    # Sort candidates by final_score desc
    sorted_candidates = sorted(candidates, key=compute_final, reverse=True)
    expected_top10 = sorted_candidates[:10]

    # Strip score from expected
    raw_fields = [k for k in candidates[0].keys() if k != "score"]
    expected_result = [{f: c[f] for f in raw_fields} for c in expected_top10]

    assert result == expected_result
