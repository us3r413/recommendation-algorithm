import pandas as pd

FEATURES_PATH = "dataset/userBehaviorFeature.csv"
_features_cache: pd.DataFrame | None = None


def _get_user_feature(talent_no: int) -> dict | None:
    """Look up a user's feature row from userBehaviorFeature.csv.

    Lazy-loads and caches the CSV on first call. Returns None if no
    matching row exists for the given talent_no.
    """
    global _features_cache
    if _features_cache is None:
        _features_cache = pd.read_csv(FEATURES_PATH)
    row = _features_cache[_features_cache["talentNo"] == talent_no]
    if row.empty:
        return None
    return row.iloc[0].to_dict()


def _popularity_rank(candidates: list[dict], raw_fields: list[str]) -> list[dict]:
    """Rank candidates by popularity score descending, tie-break by 職缺最後修改時間 descending.

    Returns the top 10 candidates with only raw_fields (score stripped).
    """
    sorted_candidates = sorted(
        candidates,
        key=lambda c: (c.get("score", 0.0), c.get("職缺最後修改時間", "")),
        reverse=True,
    )
    top10 = sorted_candidates[:10]
    return [{f: c[f] for f in raw_fields} for c in top10]


def _compute_personal_score(candidate: dict, feature: dict) -> float:
    """Compute personal relevance score for a candidate given user features.

    Formula: location_match × 0.4 + category_match × 0.4 + salary_match × 0.2

    Each component is 0.0 or 1.0 (binary match), except salary_match which
    is 0.5 when salary_floor is null/NaN.
    """
    # location_match: 1.0 if candidate's city matches any of user's top 3 cities
    pref_cities = {
        feature.get("preferred_city_1"),
        feature.get("preferred_city_2"),
        feature.get("preferred_city_3"),
    } - {None, ""}
    location_match = 1.0 if candidate.get("工作城市") in pref_cities else 0.0

    # category_match: 1.0 if candidate's mid-category matches any of user's top 3
    pref_cats = {
        feature.get("preferred_category_1"),
        feature.get("preferred_category_2"),
        feature.get("preferred_category_3"),
    } - {None, ""}
    category_match = 1.0 if candidate.get("職務中類") in pref_cats else 0.0

    # salary_match: 0.5 if salary_floor is null/NaN; else 1.0 if candidate >= floor
    salary_floor = feature.get("salary_floor")
    if salary_floor is None or pd.isna(salary_floor):
        salary_match = 0.5
    else:
        salary_match = 1.0 if (candidate.get("薪資下限") or 0) >= salary_floor else 0.0

    return location_match * 0.4 + category_match * 0.4 + salary_match * 0.2


def _personalised_rank(
    candidates: list[dict], feature: dict, raw_fields: list[str]
) -> list[dict]:
    """Rank candidates by personalised final_score descending.

    final_score = personal_score × 0.7 + normalised_popularity × 0.3

    Normalised popularity = score / max_score (or 0.0 if max_score == 0).
    Returns top 10 with only raw_fields (all computed fields stripped).
    """
    max_score = max((c.get("score", 0.0) for c in candidates), default=0.0)

    def final_score(c: dict) -> float:
        personal = _compute_personal_score(c, feature)
        pop = (c.get("score", 0.0) / max_score) if max_score > 0.0 else 0.0
        return personal * 0.7 + pop * 0.3

    sorted_candidates = sorted(candidates, key=final_score, reverse=True)
    top10 = sorted_candidates[:10]
    return [{f: c[f] for f in raw_fields} for c in top10]


def ranking(candidates: list[dict], talent_no: int) -> list[dict]:
    """Route candidates to the appropriate ranking path.

    - Empty candidates → return []
    - talent_no == 0 → popularity ranking
    - No matching feature row → popularity ranking (treat as cold-start)
    - is_cold_start == True → popularity ranking
    - Otherwise → personalised ranking

    Returns at most 10 items with computed fields stripped.
    """
    if not candidates:
        return []

    raw_fields = [k for k in candidates[0].keys() if k != "score"]

    if talent_no == 0:
        return _popularity_rank(candidates, raw_fields)

    feature = _get_user_feature(talent_no)
    if feature is None or feature.get("is_cold_start", True):
        return _popularity_rank(candidates, raw_fields)

    return _personalised_rank(candidates, feature, raw_fields)
