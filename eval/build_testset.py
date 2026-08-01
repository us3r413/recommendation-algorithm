"""
build_testset.py — Build a time-split evaluation set with graded relevance labels.

Split design (no official split was published by the organiser):

    train      2026-06-01 .. 2026-06-05   features / graph / popularity may use this
    test query 2026-06-06                 queries are sampled from this day only
    label      2026-06-06 .. 2026-06-07   forward observation window for labels

06-07 is deliberately reserved as label-observation-only so that every test query
has >= 24h of forward behaviour to be labelled against. Using the final day as a
query source would mis-label late-evening searches as "nothing relevant".

Graded relevance (per 命題 spec):
    2 = the user applied to the job after the search
    1 = the user viewed the job after the search
    0 = no observed interaction
Max value wins per (query, job) pair.

Attribution rule:
    A behaviour event is attributed to a search if it happened in
        [search_time, min(search_time + WINDOW, next_search_by_same_user)]
    with a floor of MIN_WINDOW seconds so that rapid query refinement does not
    collapse the window to zero.

    Events are NOT restricted to the job ids in empStr. Restricting to what the
    incumbent ranker exposed would bake its exposure bias into the labels and
    make it impossible to be rewarded for surfacing a better job it never showed.
    The fraction of labelled jobs that *were* in empStr is reported so the bias
    can be reasoned about.

Usage:
    python eval/build_testset.py                      # defaults
    python eval/build_testset.py --target 200         # smaller/faster set
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import os
import random
import sys
import time

csv.field_size_limit(sys.maxsize)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
DATASET = os.path.join(ROOT, "dataset")

SEARCH_CSV = os.path.join(DATASET, "userSearchLog_20260601_20260607.csv")
VIEW_CSV = os.path.join(DATASET, "職缺瀏覽_20260601_20260607.csv")
APPLY_CSV = os.path.join(DATASET, "主動應徵_0601-0607.csv")

GRADE_APPLY = 2
GRADE_VIEW = 1


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def parse_ts(s: str) -> float | None:
    """Parse '2026-06-01 00:00:00.063' to a POSIX timestamp."""
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.strip()).timestamp()
    except ValueError:
        return None


def day_bounds(date_str: str) -> tuple[float, float]:
    d = dt.date.fromisoformat(date_str)
    start = dt.datetime.combine(d, dt.time.min).timestamp()
    end = dt.datetime.combine(d, dt.time.max).timestamp()
    return start, end


# ---------------------------------------------------------------------------
# Pass 1 — sample candidate queries from the test day
# ---------------------------------------------------------------------------


def scan_search_log(test_date: str, n_candidates: int, seed: int):
    """Reservoir-sample candidate queries and collect per-user search times.

    Returns:
        candidates: list of dicts (the reservoir sample)
        user_search_times: dict talentNo -> sorted list of timestamps on test day
    """
    t_start, t_end = day_bounds(test_date)
    rng = random.Random(seed)

    reservoir: list[dict] = []
    user_search_times: dict[int, list[float]] = collections.defaultdict(list)

    seen = 0          # signed-in, non-empty-ks rows on the test day
    total_rows = 0
    t0 = time.time()

    with open(SEARCH_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            total_rows += 1
            if total_rows % 1_000_000 == 0:
                log(f"  ...scanned {total_rows:,} search rows "
                    f"({time.time()-t0:.0f}s), kept {seen:,} candidates")

            ts_raw = row.get("search_time") or ""
            if ts_raw[:10] != test_date:
                continue

            talent_raw = (row.get("talentNo") or "0").strip()
            if not talent_raw or talent_raw == "0":
                continue          # anonymous — behaviour cannot be attributed
            ks = (row.get("ks") or "").strip()
            if not ks:
                continue

            ts = parse_ts(ts_raw)
            if ts is None or not (t_start <= ts <= t_end):
                continue

            try:
                talent_no = int(talent_raw)
            except ValueError:
                continue

            user_search_times[talent_no].append(ts)

            emp_raw = (row.get("empStr") or "").strip()
            shown = [e for e in (x.strip() for x in emp_raw.split(",")) if e]

            rec = {
                "talentNo": talent_no,
                "ks": ks,
                "c0": (row.get("c0") or "").strip(),
                "d0": (row.get("d0") or "").strip(),
                "search_time": ts_raw.strip(),
                "_ts": ts,
                "n_shown": len(shown),
                "shown_head": shown[:20],   # top of the incumbent ranking
            }

            # Reservoir sampling — bounded memory, deterministic under seed
            seen += 1
            if len(reservoir) < n_candidates:
                reservoir.append(rec)
            else:
                j = rng.randrange(seen)
                if j < n_candidates:
                    reservoir[j] = rec

    for v in user_search_times.values():
        v.sort()

    log(f"  search log: {total_rows:,} rows total, {seen:,} eligible on {test_date}, "
        f"sampled {len(reservoir):,} ({time.time()-t0:.0f}s)")
    return reservoir, user_search_times


# ---------------------------------------------------------------------------
# Pass 2 — collect behaviour events for the sampled users in the label window
# ---------------------------------------------------------------------------


def scan_behaviour(users: set[int], label_start: float, label_end: float):
    """Return dict talentNo -> list of (ts, job_id, grade), sorted by ts."""
    events: dict[int, list[tuple[float, int, int]]] = collections.defaultdict(list)

    specs = [
        ("view", VIEW_CSV, "employeeNo", "dateIn", "talentNo", GRADE_VIEW),
        ("apply", APPLY_CSV, "empNo", "datein", "talentNo", GRADE_APPLY),
    ]

    for name, path, job_col, time_col, user_col, grade in specs:
        t0 = time.time()
        kept = 0
        total = 0
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                total += 1
                ts_raw = row.get(time_col) or ""
                if ts_raw[:4] != "2026":
                    continue
                talent_raw = (row.get(user_col) or "").strip()
                if not talent_raw or talent_raw == "0":
                    continue
                try:
                    talent_no = int(talent_raw)
                except ValueError:
                    continue
                if talent_no not in users:
                    continue
                ts = parse_ts(ts_raw)
                if ts is None or not (label_start <= ts <= label_end):
                    continue
                try:
                    job_id = int(float(row.get(job_col) or 0))
                except (ValueError, TypeError):
                    continue
                if job_id <= 0:
                    continue
                events[talent_no].append((ts, job_id, grade))
                kept += 1
        log(f"  {name} log: {total:,} rows, kept {kept:,} for sampled users "
            f"({time.time()-t0:.0f}s)")

    for v in events.values():
        v.sort()
    return events


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def attribute(candidates, user_search_times, events, window_sec, min_window_sec,
              label_end):
    """Attach graded labels to each candidate query. Returns labelled queries."""
    out = []
    for rec in candidates:
        talent_no = rec["talentNo"]
        t = rec["_ts"]

        # Window end: next search by this user, capped by the fixed window,
        # floored by min_window_sec, and never past the label horizon.
        searches = user_search_times.get(talent_no, [])
        next_search = None
        for s in searches:
            if s > t:
                next_search = s
                break

        w_end = t + window_sec
        if next_search is not None:
            w_end = min(w_end, max(next_search, t + min_window_sec))
        w_end = min(w_end, label_end)

        labels: dict[int, int] = {}
        for ts, job_id, grade in events.get(talent_no, ()):
            if ts < t:
                continue
            if ts > w_end:
                break
            labels[job_id] = max(labels.get(job_id, 0), grade)

        if not labels:
            continue

        shown_set = {int(x) for x in rec["shown_head"] if x.isdigit()}
        out.append({
            "qid": f"q{len(out):05d}",
            "talentNo": talent_no,
            "ks": rec["ks"],
            "c0": rec["c0"],
            "d0": rec["d0"],
            "search_time": rec["search_time"],
            "window_sec": round(w_end - t, 1),
            "n_shown": rec["n_shown"],
            "labels": {str(k): v for k, v in sorted(labels.items())},
            "n_labels": len(labels),
            "n_apply": sum(1 for g in labels.values() if g == GRADE_APPLY),
            "n_in_shown_head": sum(1 for k in labels if k in shown_set),
        })
    return out


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test-date", default="2026-06-06")
    ap.add_argument("--label-until", default="2026-06-07")
    ap.add_argument("--window-min", type=float, default=30.0,
                    help="attribution window in minutes (default 30)")
    ap.add_argument("--min-window-sec", type=float, default=120.0,
                    help="floor on the window when the user searches again soon")
    ap.add_argument("--target", type=int, default=500,
                    help="number of labelled queries to keep")
    ap.add_argument("--candidates", type=int, default=40000,
                    help="candidate queries to sample before labelling")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=os.path.join(SCRIPT_DIR, "testset.jsonl"))
    args = ap.parse_args()

    for p in (SEARCH_CSV, VIEW_CSV, APPLY_CSV):
        if not os.path.exists(p):
            print(f"ERROR: missing input file: {p}", file=sys.stderr)
            return 1

    log("=" * 68)
    log(f"Building test set — queries from {args.test_date}, "
        f"labels through {args.label_until}")
    log(f"window={args.window_min}min  target={args.target}  seed={args.seed}")
    log("=" * 68)

    log("Pass 1/3: scanning search log (2.4 GB, this is the slow one)...")
    candidates, user_search_times = scan_search_log(
        args.test_date, args.candidates, args.seed)
    if not candidates:
        print("ERROR: no eligible queries found on the test date.", file=sys.stderr)
        return 1

    users = {c["talentNo"] for c in candidates}
    log(f"Pass 2/3: scanning behaviour logs for {len(users):,} sampled users...")
    label_start, _ = day_bounds(args.test_date)
    _, label_end = day_bounds(args.label_until)
    events = scan_behaviour(users, label_start, label_end)

    log("Pass 3/3: attributing behaviour to queries...")
    labelled = attribute(candidates, user_search_times, events,
                         args.window_min * 60.0, args.min_window_sec, label_end)

    rng = random.Random(args.seed)
    rng.shuffle(labelled)
    kept = labelled[:args.target]
    for i, q in enumerate(kept):
        q["qid"] = f"q{i:05d}"

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for q in kept:
            fh.write(json.dumps(q, ensure_ascii=False) + "\n")

    # ---- summary -----------------------------------------------------------
    n_lab = [q["n_labels"] for q in kept]
    n_app = sum(q["n_apply"] for q in kept)
    in_shown = sum(q["n_in_shown_head"] for q in kept)
    tot_lab = sum(n_lab)
    with_c0 = sum(1 for q in kept if q["c0"])
    with_d0 = sum(1 for q in kept if q["d0"])

    log("=" * 68)
    log(f"WROTE {len(kept):,} labelled queries -> {args.out}")
    log(f"  candidates sampled      : {len(candidates):,}")
    log(f"  had >=1 label           : {len(labelled):,} "
        f"({len(labelled)/max(len(candidates),1)*100:.1f}%)")
    log(f"  labelled jobs total     : {tot_lab:,}")
    log(f"  labels per query        : mean {tot_lab/max(len(kept),1):.2f}, "
        f"max {max(n_lab) if n_lab else 0}")
    log(f"  grade-2 (apply) labels  : {n_app:,} ({n_app/max(tot_lab,1)*100:.1f}%)")
    log(f"  labels in incumbent top-20: {in_shown:,} "
        f"({in_shown/max(tot_lab,1)*100:.1f}%)  <- exposure-bias indicator")
    log(f"  queries with c0 filter  : {with_c0:,}")
    log(f"  queries with d0 filter  : {with_d0:,}")
    log("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
