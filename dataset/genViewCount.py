"""
genViewCount.py
---------------
Generates 瀏覽次數.csv from:
  - 職缺瀏覽_20260601_20260607.csv   (view behaviour log)
  - 主動應徵_0601-0607.csv           (application behaviour log)

Output columns:
  職缺編號, 瀏覽次數, 主動應徵次數, score

Score formula (Time Decay):
  score = Σ e^(-λ · Δt) × event_weight
  event_weight: 應徵 = WEIGHT_APPLY, 瀏覽 = WEIGHT_VIEW
  Δt: days between event and reference date (most recent event in dataset)
  λ:  decay rate (default 0.1  ≈ 50 % weight after ~7 days)
"""

import math
import os
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WEIGHT_VIEW  = 1
WEIGHT_APPLY = 3
LAMBDA       = 0.1   # time-decay rate; tune later with offline eval

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))

VIEW_CSV    = os.path.join(SCRIPT_DIR, "職缺瀏覽_20260601_20260607.csv")
APPLY_CSV   = os.path.join(SCRIPT_DIR, "主動應徵_0601-0607.csv")
OUTPUT_CSV  = os.path.join(SCRIPT_DIR, "瀏覽次數.csv")


# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
print("Loading view log...")
views = pd.read_csv(VIEW_CSV, usecols=["employeeNo", "dateIn"])
views = views.rename(columns={"employeeNo": "職缺編號", "dateIn": "event_time"})
views["event_weight"] = WEIGHT_VIEW

print("Loading application log...")
applies = pd.read_csv(APPLY_CSV, usecols=["empNo", "datein"])
applies = applies.rename(columns={"empNo": "職缺編號", "datein": "event_time"})
applies["event_weight"] = WEIGHT_APPLY


# ---------------------------------------------------------------------------
# Parse timestamps
# ---------------------------------------------------------------------------
views["event_time"]   = pd.to_datetime(views["event_time"],   errors="coerce")
applies["event_time"] = pd.to_datetime(applies["event_time"], errors="coerce")

# Drop rows where time couldn't be parsed
views   = views.dropna(subset=["event_time"])
applies = applies.dropna(subset=["event_time"])


# ---------------------------------------------------------------------------
# Combine events and compute time-decay score
# ---------------------------------------------------------------------------
events = pd.concat([views, applies], ignore_index=True)

# Reference date = most recent event across both logs
ref_date = events["event_time"].max()
print(f"Reference date (most recent event): {ref_date.date()}")

# Δt in fractional days
events["delta_days"] = (ref_date - events["event_time"]).dt.total_seconds() / 86400.0

# Per-event decayed score
events["decayed_score"] = events["event_weight"] * events["delta_days"].apply(
    lambda dt: math.exp(-LAMBDA * dt)
)


# ---------------------------------------------------------------------------
# Aggregate per job listing
# ---------------------------------------------------------------------------
print("Aggregating scores...")

view_counts  = (
    views.groupby("職缺編號")
         .size()
         .reset_index(name="瀏覽次數")
)

apply_counts = (
    applies.groupby("職缺編號")
           .size()
           .reset_index(name="主動應徵次數")
)

score_agg = (
    events.groupby("職缺編號")["decayed_score"]
          .sum()
          .reset_index(name="score")
)

# Full outer join so every job that had any event is included
result = (
    view_counts
    .merge(apply_counts, on="職缺編號", how="outer")
    .merge(score_agg,    on="職缺編號", how="outer")
)

# Fill missing counts with 0
result["瀏覽次數"]     = result["瀏覽次數"].fillna(0).astype(int)
result["主動應徵次數"] = result["主動應徵次數"].fillna(0).astype(int)
result["score"]        = result["score"].round(4)

# Sort by score descending
result = result.sort_values("score", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------
result.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
print(f"Done. {len(result):,} job listings written to: {OUTPUT_CSV}")
print(result.head(10).to_string(index=False))
