"""Property-based tests for querytoRequirement.

Validates: Requirements 1.1, 1.7, 1.8
"""

import json
import re
from unittest.mock import patch, MagicMock
from io import BytesIO

from hypothesis import given, settings
from hypothesis import strategies as st

from src.query_parser import querytoRequirement

SALARY_TAG_RE = re.compile(r"^薪資>=(\d+)$")


def _make_bedrock_response(content_list: list[str]) -> dict:
    """Create a mock Bedrock invoke_model response."""
    body_payload = json.dumps({
        "content": [{"type": "text", "text": json.dumps(content_list)}],
    }).encode()
    return {"body": BytesIO(body_payload)}


# ---------------------------------------------------------------------------
# Property 1: QueryParser always returns a list of plain strings
# ---------------------------------------------------------------------------


@given(query=st.text())
@settings(max_examples=200)
def test_property1_llm_success_always_returns_list_of_str(query: str) -> None:
    """**Validates: Requirements 1.1, 1.8**

    When the LLM succeeds and returns a valid JSON array of strings,
    querytoRequirement MUST return a list where every element is a str.
    """
    mock_client = MagicMock()
    mock_client.invoke_model.return_value = _make_bedrock_response(["tag1", "tag2"])

    with patch("src.query_parser.boto3.client", return_value=mock_client):
        result = querytoRequirement(query)

    assert isinstance(result, list), f"Expected list, got {type(result)}"
    for item in result:
        assert isinstance(item, str), f"Expected str element, got {type(item)}: {item!r}"


@given(query=st.text())
@settings(max_examples=200)
def test_property1_llm_failure_always_returns_list_of_str(query: str) -> None:
    """**Validates: Requirements 1.1, 1.8**

    When the LLM fails (raises an exception), querytoRequirement MUST still
    return a list where every element is a str (via the fallback path).
    """
    mock_client = MagicMock()
    mock_client.invoke_model.side_effect = Exception("connection refused")

    with patch("src.query_parser.boto3.client", return_value=mock_client):
        result = querytoRequirement(query)

    assert isinstance(result, list), f"Expected list, got {type(result)}"
    for item in result:
        assert isinstance(item, str), f"Expected str element, got {type(item)}: {item!r}"


# ---------------------------------------------------------------------------
# Property 4: Salary tags are correctly formatted
# ---------------------------------------------------------------------------


@given(salary_value=st.integers(min_value=20000, max_value=200000))
@settings(max_examples=100)
def test_property4_salary_tag_correctly_formatted(salary_value: int) -> None:
    """**Validates: Requirements 1.7**

    When the LLM returns a salary tag in the format 薪資>=<integer>, the
    result should contain exactly one element matching ^薪資>=\\d+$ and the
    integer value in that tag should match the expected salary value.
    """
    mock_client = MagicMock()
    mock_client.invoke_model.return_value = _make_bedrock_response(
        [f"薪資>={salary_value}", "後端工程師"]
    )

    with patch("src.query_parser.boto3.client", return_value=mock_client):
        result = querytoRequirement(f"薪水{salary_value}以上 後端")

    # Exactly one tag should match the salary regex
    salary_tags = [tag for tag in result if SALARY_TAG_RE.match(tag)]
    assert len(salary_tags) == 1, (
        f"Expected exactly 1 salary tag, got {len(salary_tags)}: {result}"
    )

    # The integer in the salary tag should match the expected value
    matched_value = int(SALARY_TAG_RE.match(salary_tags[0]).group(1))
    assert matched_value == salary_value, (
        f"Expected salary value {salary_value}, got {matched_value}"
    )


# ---------------------------------------------------------------------------
# Property 3: Valid LLM JSON is returned as-is
# **Validates: Requirements 1.3**
# ---------------------------------------------------------------------------

import json


@given(
    generated_list=st.lists(
        st.text(min_size=1, max_size=20), min_size=1, max_size=10
    )
)
@settings(max_examples=200)
def test_property3_valid_llm_json_returned_as_is(generated_list: list[str]) -> None:
    """**Validates: Requirements 1.3**

    When the LLM returns a valid JSON array of strings, querytoRequirement
    must return that exact array (same elements, same order).
    """
    mock_client = MagicMock()
    mock_client.invoke_model.return_value = _make_bedrock_response(generated_list)

    with patch("src.query_parser.boto3.client", return_value=mock_client):
        result = querytoRequirement("any query")

    assert result == generated_list


# ---------------------------------------------------------------------------
# Property 5: Fallback tokenisation preserves abbreviation expansion
# ---------------------------------------------------------------------------

from src.utils.abbreviations import abbreviation_expand


@given(query=st.text(min_size=1, max_size=50).filter(lambda s: s.strip()))
@settings(max_examples=200)
def test_property5_fallback_equals_expanded_split(query: str) -> None:
    """**Validates: Requirements 1.5**

    When all 3 LLM attempts fail, the returned Tag List SHALL equal
    `abbreviation_expand(query).split()` — the whitespace-tokenised form
    of the abbreviation-expanded query, with no other transformations applied.
    """
    mock_client = MagicMock()
    mock_client.invoke_model.side_effect = Exception("mock failure")

    with patch("src.query_parser.boto3.client", return_value=mock_client):
        result = querytoRequirement(query)

    expected = abbreviation_expand(query).split()
    assert result == expected, (
        f"Fallback mismatch for query={query!r}: "
        f"got {result!r}, expected {expected!r}"
    )
