import time

from src.query_parser import querytoRequirement
from src.retriever import grabFromDatabase
from src.ranker import ranking


def debug_recommend(query: str, talent_no: int):
    print(f"{'='*60}")
    print(f"Query: {query!r}  |  talent_no: {talent_no}")
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
    candidates = grabFromDatabase(tags)
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
        print(f"      城市: {job.get('工作城市', 'N/A')}  薪資下限: {job.get('薪資下限', 'N/A')}")
    print()


# Anonymous user
debug_recommend("台北 前端工程師 35k以上", talent_no=0)

# Logged-in user
debug_recommend("後端 pt工作", talent_no=12345)
