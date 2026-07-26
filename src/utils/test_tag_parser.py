"""Unit tests for tag_parser.classify_tags and SALARY_TAG_RE."""

import pytest
from src.utils.tag_parser import SALARY_TAG_RE, classify_tags


# ---------------------------------------------------------------------------
# SALARY_TAG_RE
# ---------------------------------------------------------------------------

class TestSalaryTagRe:
    def test_matches_valid_salary_tag(self):
        assert SALARY_TAG_RE.match("薪資>=35000") is not None

    def test_captures_integer_value(self):
        m = SALARY_TAG_RE.match("薪資>=35000")
        assert m.group(1) == "35000"

    def test_matches_zero(self):
        assert SALARY_TAG_RE.match("薪資>=0") is not None

    def test_does_not_match_without_prefix(self):
        assert SALARY_TAG_RE.match("35000") is None

    def test_does_not_match_float(self):
        assert SALARY_TAG_RE.match("薪資>=35000.5") is None

    def test_does_not_match_lt(self):
        assert SALARY_TAG_RE.match("薪資<=35000") is None

    def test_does_not_match_empty_string(self):
        assert SALARY_TAG_RE.match("") is None

    def test_does_not_match_partial(self):
        # Must match the full string (anchored with ^ and $)
        assert SALARY_TAG_RE.match("薪資>=35000 extra") is None


# ---------------------------------------------------------------------------
# classify_tags — salary tags
# ---------------------------------------------------------------------------

class TestClassifyTagsSalary:
    def test_single_salary_tag_extracted(self):
        result = classify_tags(["薪資>=35000"])
        assert result["salary_min"] == 35000

    def test_salary_min_none_when_absent(self):
        result = classify_tags(["後端工程師", "兼職"])
        assert result["salary_min"] is None

    def test_salary_tag_not_in_job_terms(self):
        result = classify_tags(["薪資>=40000"])
        assert "薪資>=40000" not in result["job_terms"]

    def test_salary_tag_not_in_cities(self):
        result = classify_tags(["薪資>=40000"])
        assert "薪資>=40000" not in result["cities"]

    def test_multiple_salary_tags_last_wins(self):
        # Unusual input; last tag should overwrite
        result = classify_tags(["薪資>=30000", "薪資>=50000"])
        assert result["salary_min"] == 50000


# ---------------------------------------------------------------------------
# classify_tags — job_terms (stub city lookup returns empty set)
# ---------------------------------------------------------------------------

class TestClassifyTagsJobTerms:
    def test_non_salary_non_city_goes_to_job_terms(self):
        result = classify_tags(["後端工程師", "兼職"])
        assert result["job_terms"] == ["後端工程師", "兼職"]

    def test_order_preserved(self):
        tags = ["機器學習工程師", "台北市", "薪資>=45000", "全職"]
        result = classify_tags(tags)
        # With stub city lookup, 台北市 falls into job_terms
        assert result["job_terms"] == ["機器學習工程師", "台北市", "全職"]

    def test_empty_input(self):
        result = classify_tags([])
        assert result == {"cities": [], "salary_min": None, "job_terms": []}

    def test_mixed_tags(self):
        tags = ["後端工程師", "薪資>=35000", "兼職"]
        result = classify_tags(tags)
        assert result["salary_min"] == 35000
        assert result["job_terms"] == ["後端工程師", "兼職"]
        assert result["cities"] == []


# ---------------------------------------------------------------------------
# classify_tags — cities (stub returns empty set, so always empty)
# ---------------------------------------------------------------------------

class TestClassifyTagsCities:
    def test_cities_empty_with_stub(self):
        # _load_known_cities() returns set() by design in this stub
        result = classify_tags(["台北市", "台中市"])
        assert result["cities"] == []

    def test_all_non_salary_tags_are_job_terms_with_stub(self):
        result = classify_tags(["台北市", "後端工程師"])
        assert set(result["job_terms"]) == {"台北市", "後端工程師"}


# ---------------------------------------------------------------------------
# classify_tags — return shape
# ---------------------------------------------------------------------------

class TestClassifyTagsReturnShape:
    def test_returns_dict_with_correct_keys(self):
        result = classify_tags([])
        assert set(result.keys()) == {"cities", "salary_min", "job_terms"}

    def test_cities_is_list(self):
        assert isinstance(classify_tags([])["cities"], list)

    def test_job_terms_is_list(self):
        assert isinstance(classify_tags([])["job_terms"], list)

    def test_salary_min_is_int_or_none(self):
        r1 = classify_tags(["薪資>=30000"])
        assert isinstance(r1["salary_min"], int)

        r2 = classify_tags([])
        assert r2["salary_min"] is None
