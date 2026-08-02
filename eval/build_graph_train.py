"""
build_graph_train.py — Build the interaction graph from TRAIN-PERIOD events only.

Why this exists
---------------
`src/graph_builder.py` reads `userBehaviorEvents.csv` with no date filter, so a
graph built with it spans the whole week — including the test day (06-06) and
the label window (06-07). Using that graph for evaluation means the ranker can
see the very clicks it is being scored on. The 命題 penalty for this is severe:

    參賽者不得使用 test set 進行任何調參、模型選擇或圖譜建構（違者該指標項不計分）

The damage is also unrecoverable after the fact: `graph_cache.pkl` stores
summed edge weights with no timestamps, so test-period edges cannot be removed
from an existing graph. It has to be rebuilt from filtered events.

The full-week graph remains the correct choice for production serving — there is
no leakage concern when actually serving users. This script produces a separate
train-only artefact used exclusively by the ablation harness.

Usage:
    python eval/build_graph_train.py
    python eval/build_graph_train.py --until 2026-06-05 --rebuild
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import pickle
import sys
import time

csv.field_size_limit(sys.maxsize)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, ROOT)

DATASET = os.path.join(ROOT, "dataset")
VIEW_CSV = os.path.join(DATASET, "職缺瀏覽_20260601_20260607.csv")
APPLY_CSV = os.path.join(DATASET, "主動應徵_0601-0607.csv")
JOBS_CSV = os.path.join(DATASET, "職缺.csv")
OUT_GRAPH = os.path.join(DATASET, "graph_cache_train.pkl")

WEIGHT_VIEW = 1
WEIGHT_APPLY = 3


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-01")
    ap.add_argument("--until", default="2026-06-05",
                    help="last train day (inclusive); events after this are dropped")
    ap.add_argument("--out", default=OUT_GRAPH)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    if os.path.exists(args.out) and not args.rebuild:
        log(f"{args.out} already exists — use --rebuild to overwrite")
        return 0

    import networkx as nx

    since, until = args.since, args.until
    log(f"Building interaction graph from events in [{since} .. {until}]")

    # --- 1. Aggregate edge weights straight from the raw logs ---------------
    # Going directly to the raw logs avoids depending on userBehaviorEvents.csv,
    # which is generated without a date filter.
    edge_w: dict[tuple[int, int], int] = {}
    users: set[int] = set()
    jobs_seen: set[int] = set()

    for name, path, job_col, time_col, weight in [
        ("view", VIEW_CSV, "employeeNo", "dateIn", WEIGHT_VIEW),
        ("apply", APPLY_CSV, "empNo", "datein", WEIGHT_APPLY),
    ]:
        t0 = time.time()
        total = kept = 0
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                total += 1
                day = (row.get(time_col) or "")[:10]
                if not (since <= day <= until):
                    continue
                talent_raw = (row.get("talentNo") or "").strip()
                if not talent_raw or talent_raw == "0":
                    continue          # anonymous — not a stable identity
                try:
                    talent_no = int(talent_raw)
                    job_id = int(float(row.get(job_col) or 0))
                except (ValueError, TypeError):
                    continue
                if job_id <= 0:
                    continue
                users.add(talent_no)
                jobs_seen.add(job_id)
                key = (talent_no, job_id)
                edge_w[key] = edge_w.get(key, 0) + weight
                kept += 1
        log(f"  {name}: {total:,} rows -> {kept:,} in window ({time.time()-t0:.0f}s)")

    log(f"  aggregated {len(edge_w):,} unique (user, job) edges")

    # --- 2. Job metadata for the nodes that actually appear -----------------
    t0 = time.time()
    meta: dict[int, tuple[str, str]] = {}
    with open(JOBS_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            try:
                jid = int(row["職缺編號"])
            except (ValueError, KeyError, TypeError):
                continue
            if jid in jobs_seen:
                meta[jid] = ((row.get("工作城市") or "").strip(),
                             (row.get("職務中類") or "").strip())
    log(f"  job metadata for {len(meta):,} jobs ({time.time()-t0:.0f}s)")

    # --- 3. Assemble the graph ---------------------------------------------
    t0 = time.time()
    G = nx.DiGraph()
    for jid in jobs_seen:
        city, cat = meta.get(jid, ("", ""))
        G.add_node(f"job:{jid}", node_type="Job", job_id=jid,
                   city=city, category_mid=cat)
    for talent_no in users:
        G.add_node(f"user:{talent_no}", node_type="User", talentNo=talent_no)
    for (talent_no, jid), w in edge_w.items():
        G.add_edge(f"user:{talent_no}", f"job:{jid}", weight=w)
    log(f"  graph assembled ({time.time()-t0:.0f}s)")

    with open(args.out, "wb") as fh:
        pickle.dump(G, fh, protocol=pickle.HIGHEST_PROTOCOL)

    size_mb = os.path.getsize(args.out) / (1024 * 1024)
    log("=" * 60)
    log(f"WROTE {args.out}  ({size_mb:.0f} MB)")
    log(f"  nodes {G.number_of_nodes():,}  (users {len(users):,}, jobs {len(jobs_seen):,})")
    log(f"  edges {G.number_of_edges():,}")
    log(f"  train window: {since} .. {until}  (test day 06-06 excluded)")
    log("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
