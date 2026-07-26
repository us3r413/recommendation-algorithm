"""Abbreviation expansion utilities for the recommendation pipeline query parser."""

ABBREVIATION_MAP: dict[str, str] = {
    "pt":     "兼職",
    "ft":     "全職",
    "ue":     "使用者體驗設計師",
    "ui":     "介面設計師",
    "rd":     "研發工程師",
    "qa":     "品質保證工程師",
    "pm":     "產品經理",
    "hr":     "人力資源",
    "be":     "後端工程師",
    "fe":     "前端工程師",
    "ml":     "機器學習工程師",
    "ai":     "人工智慧",
    "devops": "開發維運工程師",
}


def abbreviation_expand(query: str) -> str:
    """Case-insensitive abbreviation substitution with partial-token support.

    Splits the query on whitespace, then for each token:
      1. If the full token (lowercased) matches an abbreviation key, replace it.
      2. Otherwise, check if the token starts with an abbreviation key followed
         by non-ASCII characters (e.g. "pt工作" → "兼職"). The Chinese suffix
         is dropped if it's a generic/noise word (e.g. "工作"), otherwise kept.

    Args:
        query: The raw user search string, possibly containing abbreviations.

    Returns:
        The query with all known abbreviation tokens replaced by their full
        Chinese equivalents. Non-abbreviation tokens are returned unchanged.

    Examples:
        >>> abbreviation_expand("BE 台北 pt")
        '後端工程師 台北 兼職'
        >>> abbreviation_expand("DevOps engineer")
        '開發維運工程師 engineer'
        >>> abbreviation_expand("pt工作")
        '兼職'
        >>> abbreviation_expand("unknown token")
        'unknown token'
    """
    # Generic suffixes that add no search value when split off
    _NOISE_SUFFIXES = {"工作", "職位", "職缺", "機會", "工"}

    tokens = query.split()
    result = []

    for t in tokens:
        lower = t.lower()
        if lower in ABBREVIATION_MAP:
            result.append(ABBREVIATION_MAP[lower])
        else:
            # Check for abbreviation prefix followed by non-ASCII suffix
            matched = False
            for abbr, expansion in ABBREVIATION_MAP.items():
                if lower.startswith(abbr) and len(t) > len(abbr):
                    suffix = t[len(abbr):]
                    # Only split if suffix starts with a non-ASCII char (Chinese)
                    if ord(suffix[0]) > 127:
                        result.append(expansion)
                        if suffix not in _NOISE_SUFFIXES:
                            result.append(suffix)
                        matched = True
                        break
            if not matched:
                result.append(t)

    return " ".join(result)
