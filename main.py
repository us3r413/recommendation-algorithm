import time

import pandas as pd

from src.query_parser import querytoRequirement
from src.retriever import grabFromDatabase
from src.ranker import ranking

# ---------------------------------------------------------------------------
# User history helpers
# ---------------------------------------------------------------------------

EVENTS_PATH = "dataset/userBehaviorEvents.csv"
FEATURES_PATH = "dataset/userBehaviorFeature.csv"

_events_cache: pd.DataFrame | None = None
_features_cache: pd.DataFrame | None = None


def _load_events() -> pd.DataFrame:
    global _events_cache
    if _events_cache is None:
        _events_cache = pd.read_csv(EVENTS_PATH)
    return _events_cache


def _load_features() -> pd.DataFrame:
    global _features_cache
    if _features_cache is None:
        _features_cache = pd.read_csv(FEATURES_PATH)
    return _features_cache


def print_user_history(talent_no: int):
    """Print a signed-in user's behaviour summary and recent events."""
    features = _load_features()
    row = features[features["talentNo"] == talent_no]
    if row.empty:
        print(f"\n[User History] talentNo={talent_no} — no feature record found")
        return

    feat = row.iloc[0]
    print(f"\n[User History] talentNo={talent_no}")
    print(f"  total_events: {int(feat['total_events'])}  |  cold_start: {feat['is_cold_start']}  |  last_active: {feat['last_active']}")
    cities = [feat.get(f"preferred_city_{i}") for i in range(1, 4) if pd.notna(feat.get(f"preferred_city_{i}"))]
    cats = [feat.get(f"preferred_category_{i}") for i in range(1, 4) if pd.notna(feat.get(f"preferred_category_{i}"))]
    print(f"  preferred_cities: {cities}")
    print(f"  preferred_categories: {cats}")
    salary = feat.get("salary_floor")
    print(f"  salary_floor: {salary if pd.notna(salary) else 'N/A'}")

    # Recent events (last 10)
    events = _load_events()
    user_events = events[events["talentNo"] == talent_no].sort_values("event_time", ascending=False).head(10)
    if user_events.empty:
        print("  recent_events: (none)")
    else:
        print(f"  recent_events (last {len(user_events)}):")
        for _, ev in user_events.iterrows():
            print(f"    {ev['event_type']:5}  job={int(ev['job_id'])}  city={ev['job_city']}  cat={ev['job_category_mid']}  time={ev['event_time']}")


def debug_recommend(query: str, talent_no: int, c0=None, d0=None):
    print(f"{'='*60}")
    print(f"Query: {query!r}  |  talent_no: {talent_no}")
    if c0:
        print(f"  c0 (city codes): {c0}")
    if d0:
        print(f"  d0 (job category codes): {d0}")
    print(f"{'='*60}")

    total_start = time.perf_counter()

    # Stage 1
    t0 = time.perf_counter()
    tags = querytoRequirement(query)
    t1 = time.perf_counter()
    print(f"\n[Stage 1] querytoRequirement → tags:  ({t1-t0:.2f}s)")
    print(f"  {tags}")

    # Stage 2
    t2 = time.perf_counter()
    candidates = grabFromDatabase(tags, c0=c0, d0=d0)
    t3 = time.perf_counter()
    print(f"\n[Stage 2] grabFromDatabase → {len(candidates)} candidates  ({t3-t2:.2f}s)")

    # Stage 3
    t4 = time.perf_counter()
    results = ranking(candidates, talent_no)
    t5 = time.perf_counter()
    print(f"\n[Stage 3] ranking → {len(results)} results  ({t5-t4:.2f}s)")

    total_end = time.perf_counter()
    print(f"\n  Total: {total_end-total_start:.2f}s")

    # Print user history for signed-in users before showing results
    if talent_no != 0:
        print_user_history(talent_no)

    print()
    for i, job in enumerate(results, 1):
        print(f"  {i:2}. {job.get('職務名稱', 'N/A')}")
        print(f"      城市: {job.get('工作城市', 'N/A')}  薪資下限: {job.get('薪資下限', 'N/A')}  職務小類: {job.get('職務小類', 'N/A')}")
    print()


# --- Preload graph only if graph ranking is enabled ---
from src.ranker import USE_GRAPH_RAG, GRAPH_FOR_ANONYMOUS
if USE_GRAPH_RAG or GRAPH_FOR_ANONYMOUS:
    from src.graph_builder import get_graph
    print("Preloading graph...")
    get_graph()
    print()

# # --- Example 1: Anonymous user, natural language query ---
# debug_recommend("台北 前端工程師 35k以上", talent_no=0)

# # --- Example 2: Anonymous user, broader query with city filter ---
# debug_recommend("行銷企劃", talent_no=0, c0=["100100"])

# # --- Example 3: Anonymous user, job category filter only ---
# debug_recommend("", talent_no=0, c0=["100100", "100200"], d0=["140214", "140213"])

# # --- Example 4: Anonymous user, keyword + city ---
# debug_recommend("台中 兼職 門市", talent_no=0)

# --- Example 5: Signed-in user, personalised ranking ---
debug_recommend("fastfood cook", talent_no=0)

# --- Example 6: Signed-in user, broader query ---
#debug_recommend("行銷企劃", talent_no=143)

# --- Example 7: Signed-in user, different preference ---
#debug_recommend("台中 兼職 門市", talent_no=301)

# --- Example 8: Signed-in user, with city filter ---
#debug_recommend("軟體工程師", talent_no=138, c0=["100100"])
