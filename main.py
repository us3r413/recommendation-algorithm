import time

from src.query_parser import querytoRequirement
from src.retriever import grabFromDatabase
from src.ranker import ranking


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
    print()
    for i, job in enumerate(results, 1):
        print(f"  {i:2}. {job.get('職務名稱', 'N/A')}")
        print(f"      城市: {job.get('工作城市', 'N/A')}  薪資下限: {job.get('薪資下限', 'N/A')}  職務小類: {job.get('職務小類', 'N/A')}")
    print()


# --- Example 1: Natural language only (anonymous) ---
debug_recommend("台北 前端工程師 35k以上", talent_no=0)

# --- Example 2: With c0 city filter (台北市=100100, 新北市=100200) ---
debug_recommend("軟體工程師", talent_no=0, c0=["100100", "100200"])

# --- Example 3: With c0 + d0 (台北市=100100, 前端工程師=140214, 網站程式設計師=140213) ---
debug_recommend("", talent_no=0, c0=["100100"], d0=["140214", "140213"])

# --- Example 4: Logged-in user with c0 + d0 (simulating userSearchLog replay) ---
debug_recommend("遠端", talent_no=53213129, c0=["100100", "100200", "100900"], d0=["160213", "120403"])
