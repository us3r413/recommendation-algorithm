"""Property-based tests for abbreviation_expand.

**Validates: Requirements 1.2, 1.5**

Property 2: Abbreviations are always expanded in the output
  For any query string containing a known abbreviation token, the returned
  result SHALL contain the corresponding expanded Chinese term, regardless
  of surrounding context.
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.utils.abbreviations import ABBREVIATION_MAP, abbreviation_expand


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# All abbreviation keys (lowercase), e.g. ["pt", "ft", "ue", ...]
_ABBREV_KEYS: list[str] = list(ABBREVIATION_MAP.keys())

# Strategy: a single abbreviation key in any mix of upper/lower case
def _mixed_case(abbrev: str) -> st.SearchStrategy[str]:
    """Return a strategy that produces all-caps, all-lower, or title-case
    variants of the given abbreviation token."""
    return st.sampled_from([
        abbrev.lower(),
        abbrev.upper(),
        abbrev.capitalize(),
    ])

# Strategy: whitespace-separated non-abbreviation filler words (ASCII or CJK)
_filler = st.one_of(
    st.text(
        alphabet=st.characters(
            whitelist_categories=("Lu", "Ll", "Lo"),  # letters only, no spaces
            blacklist_characters=" \t\n\r",
        ),
        min_size=1,
        max_size=8,
    ).filter(lambda s: s.lower() not in ABBREVIATION_MAP),
)

# Strategy: a list of filler tokens (may be empty)
_filler_tokens = st.lists(_filler, min_size=0, max_size=4)


# ---------------------------------------------------------------------------
# Property 2 — Abbreviations are always expanded in the output
# ---------------------------------------------------------------------------

@given(
    abbrev=st.sampled_from(_ABBREV_KEYS),
    before=_filler_tokens,
    after=_filler_tokens,
    case_variant=st.integers(min_value=0, max_value=2),
)
@settings(max_examples=300)
def test_property2_abbreviation_always_expanded(
    abbrev: str,
    before: list[str],
    after: list[str],
    case_variant: int,
) -> None:
    """**Validates: Requirements 1.2, 1.5**

    For any query that contains a known abbreviation token (in any casing),
    abbreviation_expand SHALL return a string containing the full Chinese
    expansion for that abbreviation.
    """
    # Build a cased variant: 0=lower, 1=upper, 2=capitalize
    variants = [abbrev.lower(), abbrev.upper(), abbrev.capitalize()]
    token = variants[case_variant]

    tokens = before + [token] + after
    query = " ".join(tokens)

    result = abbreviation_expand(query)
    expected_expansion = ABBREVIATION_MAP[abbrev.lower()]

    assert expected_expansion in result, (
        f"Expected '{expected_expansion}' in result for query {query!r}, "
        f"got: {result!r}"
    )


# ---------------------------------------------------------------------------
# Property 2 (exhaustive) — all entries in ABBREVIATION_MAP are expanded
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("abbrev,expansion", ABBREVIATION_MAP.items())
def test_all_abbreviations_expanded_lowercase(abbrev: str, expansion: str) -> None:
    """**Validates: Requirements 1.2**

    Every entry in ABBREVIATION_MAP is expanded when used as a standalone
    lowercase token.
    """
    assert abbreviation_expand(abbrev) == expansion


@pytest.mark.parametrize("abbrev,expansion", ABBREVIATION_MAP.items())
def test_all_abbreviations_expanded_uppercase(abbrev: str, expansion: str) -> None:
    """**Validates: Requirements 1.2**

    Every entry in ABBREVIATION_MAP is expanded when used as an all-caps token.
    """
    assert abbreviation_expand(abbrev.upper()) == expansion


@pytest.mark.parametrize("abbrev,expansion", ABBREVIATION_MAP.items())
def test_all_abbreviations_expanded_titlecase(abbrev: str, expansion: str) -> None:
    """**Validates: Requirements 1.2**

    Every entry in ABBREVIATION_MAP is expanded when used as a title-cased token.
    """
    assert abbreviation_expand(abbrev.capitalize()) == expansion


# ---------------------------------------------------------------------------
# Property 2 — expansion survives surrounding tokens
# ---------------------------------------------------------------------------

@given(
    abbrev=st.sampled_from(_ABBREV_KEYS),
    prefix=st.text(
        alphabet=st.characters(whitelist_categories=("Lo",)),  # CJK characters
        min_size=1,
        max_size=6,
    ),
    suffix=st.text(
        alphabet=st.characters(whitelist_categories=("Lo",)),
        min_size=1,
        max_size=6,
    ),
)
@settings(max_examples=200)
def test_property2_expansion_with_surrounding_cjk_tokens(
    abbrev: str, prefix: str, suffix: str
) -> None:
    """**Validates: Requirements 1.2, 1.5**

    When the abbreviation token appears between CJK-character tokens,
    the expansion is still present in the result.
    """
    query = f"{prefix} {abbrev} {suffix}"
    result = abbreviation_expand(query)
    expected = ABBREVIATION_MAP[abbrev]
    assert expected in result, (
        f"Expected '{expected}' in result for query {query!r}, got: {result!r}"
    )


# ---------------------------------------------------------------------------
# Property 2 — non-abbreviation tokens are preserved unchanged
# ---------------------------------------------------------------------------

@given(
    token=st.text(
        alphabet=st.characters(
            whitelist_categories=("Lu", "Ll", "Lo"),
            blacklist_characters=" \t\n\r",
        ),
        min_size=1,
        max_size=10,
    ).filter(lambda s: s.lower() not in ABBREVIATION_MAP),
)
@settings(max_examples=200)
def test_non_abbreviation_tokens_pass_through(token: str) -> None:
    """**Validates: Requirements 1.2**

    Tokens that are not in ABBREVIATION_MAP are returned unchanged.
    """
    result = abbreviation_expand(token)
    assert result == token, (
        f"Non-abbreviation token {token!r} should pass through unchanged, "
        f"got: {result!r}"
    )
