"""
build_popularity.py — Time-aware popularity table for the ablation harness.

This is a train/test-aware variant of dataset/genViewCount.py. The original
aggregates the whole week; for evaluation that is leakage — it would let the
ranker see clicks that happened on the test day it is being scored on.

    score = SUM over events of  exp(-LAMBDA * delta_days) * event_weight
    event_weight: apply = 3, view = 1
    delta_days  : days between the event and the reference date

The reference date is the LAST DAY OF THE TRAIN PERIOD (not the last day in the
file), so decay is measured from the edge of what the model is allowed to know.

Usage:
    # train-only table for evaluation (default)
    python eval/build_popularity.py

    # full-week table, matching the production ETL
    python eval/build_popularity.py --until 2026-06-07 --out dataset/瀏覽次數.csv
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import math
import os
import sys
import time

csv.field_size_limit(sys.maxsize)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
DATASET = os.path.join(ROOT, "dataset")

VIEW_CSV = os.path.join(DATASET, "職缺瀏覽_20260601_20260607.csv")
APPLY_CSV = os.path.join(DATASET, "主動應徵_0601-0607.csv")

WEIGHT_VIEW = 1
WEIGHT_APPLY = 3
LAMBDA = 0.1          # ~50% decay after 7 days; same value as the production ETL


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", default="2026-06-01",
                    help="first day of the train period (inclusive)")
    ap.add_argument("--until", default="2026-06-05",
                    help="last day of the train period (inclusive). Events after "
                         "this date are dropped — this is the leakage guard.")
    ap.add_argument("--lam", type=float, default=LAMBDA)
    ap.add_argument("--out", default=os.path.join(DATASET, "瀏覽次數_train.csv"))
    args = ap.parse_args()

    since, until = args.since, args.until
    # Decay is measured from the end of the train window.
    ref = dt.datetime.combine(dt.date.fromisoformat(until), dt.time.max)

    log(f"Building popularity table from events in [{since} .. {until}]")
    log(f"  reference date for decay: {ref:%Y-%m-%d %H:%M}  lambda={args.lam}")

    views = collections.Counter()
    applies = collections.Counter()
    score = collections.defaultdict(float)

    specs = [
        ("view", VIEW_CSV, "employeeNo", "dateIn", WEIGHT_VIEW, views),
        ("apply", APPLY_CSV, "empNo", "datein", WEIGHT_APPLY, applies),
    ]

    for name, path, job_col, time_col, weight, counter in specs:
        if not os.path.exists(path):
            print(f"ERROR: missing {path}", file=sys.stderr)
            return 1
        t0 = time.time()
        total = kept = 0
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                total += 1
                raw = (row.get(time_col) or "").strip()
                day = raw[:10]
                if not (since <= day <= until):
                    continue
                try:
                    ts = dt.datetime.fromisoformat(raw)
                except ValueError:
                    continue
                try:
                    job = int(float(row.get(job_col) or 0))
                except (ValueError, TypeError):
                    continue
                if job <= 0:
                    continue
                delta_days = (ref - ts).total_seconds() / 86400.0
                score[job] += weight * math.exp(-args.lam * delta_days)
                counter[job] += 1
                kept += 1
        log(f"  {name}: {total:,} rows -> {kept:,} in window ({time.time()-t0:.0f}s)")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    rows = sorted(score.items(), key=lambda kv: kv[1], reverse=True)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["職缺編號", "瀏覽次數", "主動應徵次數", "score"])
        for job, s in rows:
            w.writerow([job, views.get(job, 0), applies.get(job, 0), round(s, 4)])

    log(f"WROTE {len(rows):,} jobs -> {args.out}")
    if rows:
        log(f"  top score {rows[0][1]:.2f}  median {rows[len(rows)//2][1]:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
