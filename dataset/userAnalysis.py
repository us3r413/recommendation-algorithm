"""
userAnalysis.py
---------------
Generates two CSV files (Option 3 / dual-file design) from:
  - 職缺瀏覽_20260601_20260607.csv   (view behaviour log)
  - 主動應徵_0601-0607.csv           (application behaviour log)
  - 職缺.csv                         (job listings master — for city / category / salary)

Output 1 — userBehaviorFeature.csv   (one row per authenticated user)
  talentNo, preferred_city_1..3, preferred_category_1..3,
  salary_floor, total_events, last_active

Output 2 — userBehaviorEvents.csv    (one row per view / apply event)
  talentNo, event_type, job_id, event_time, job_city, job_category_mid

IMPORTANT:
  talentNo = 0  →  anonymous / signed-out user.
  All rows with talentNo = 0 are EXCLUDED from both output files.
  Multiple rows with talentNo = 0 must NOT be treated as the same person.

Preference city weighting:
  city_weight = apply_count(city) × WEIGHT_APPLY + view_count(city) × WEIGHT_VIEW
  Top 3 cities by city_weight are stored as preferred_city_1/2/3.

salary_floor:
  25th percentile of 薪資下限 across all jobs the user applied to.
  null when the user has fewer than MIN_APPLY_FOR_SALARY apply events.
"""

import os
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WEIGHT_VIEW  = 1
WEIGHT_APPLY = 3          # apply events count 3× toward city/category preference
MIN_APPLY_FOR_SALARY = 3  # minimum apply events required to compute salary_floor
COLD_START_THRESHOLD = 3  # users with total_events < this are considered cold-start

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

VIEW_CSV    = os.path.join(SCRIPT_DIR, "職缺瀏覽_20260601_20260607.csv")
APPLY_CSV   = os.path.join(SCRIPT_DIR, "主動應徵_0601-0607.csv")
JOB_CSV     = os.path.join(SCRIPT_DIR, "職缺.csv")

OUT_FEATURE = os.path.join(SCRIPT_DIR, "userBehaviorFeature.csv")
OUT_EVENTS  = os.path.join(SCRIPT_DIR, "userBehaviorEvents.csv")


# ---------------------------------------------------------------------------
# Load raw data
# ---------------------------------------------------------------------------
print("Loading job listings master...")
jobs = pd.read_csv(
    JOB_CSV,
    usecols=["職缺編號", "工作城市", "職務中類", "薪資下限"],
    dtype={"職缺編號": "Int64"},
)
jobs = jobs.rename(columns={
    "職缺編號": "job_id",
    "工作城市": "job_city",
    "職務中類": "job_category_mid",
    "薪資下限": "salary_lower",
})
# Coerce salary to numeric (some entries may be text like "面議")
jobs["salary_lower"] = pd.to_numeric(jobs["salary_lower"], errors="coerce")

print("Loading view log...")
views = pd.read_csv(
    VIEW_CSV,
    usecols=["employeeNo", "dateIn", "talentNo"],
    dtype={"talentNo": "Int64", "employeeNo": "Int64"},
)
views = views.rename(columns={"employeeNo": "job_id", "dateIn": "event_time"})
views["event_type"] = "view"

print("Loading application log...")
applies = pd.read_csv(
    APPLY_CSV,
    usecols=["empNo", "datein", "talentNo"],
    dtype={"talentNo": "Int64", "empNo": "Int64"},
)
applies = applies.rename(columns={"empNo": "job_id", "datein": "event_time"})
applies["event_type"] = "apply"


# ---------------------------------------------------------------------------
# Parse timestamps
# ---------------------------------------------------------------------------
views["event_time"]   = pd.to_datetime(views["event_time"],   errors="coerce")
applies["event_time"] = pd.to_datetime(applies["event_time"], errors="coerce")

views   = views.dropna(subset=["event_time"])
applies = applies.dropna(subset=["event_time"])


# ---------------------------------------------------------------------------
# Combine events and filter out anonymous users (talentNo = 0 or null)
# ---------------------------------------------------------------------------
events = pd.concat([views, applies], ignore_index=True)

before = len(events)
events = events[events["talentNo"].notna() & (events["talentNo"] != 0)]
print(f"Filtered out {before - len(events):,} anonymous/signed-out rows (talentNo = 0 or null).")
print(f"Remaining authenticated events: {len(events):,}")


# ---------------------------------------------------------------------------
# Enrich events with job metadata (city, category, salary)
# ---------------------------------------------------------------------------
print("Joining job metadata...")
events = events.merge(jobs, on="job_id", how="left")


# ---------------------------------------------------------------------------
# Output 2: userBehaviorEvents.csv  (save before aggregation)
# ---------------------------------------------------------------------------
event_out = events[[
    "talentNo", "event_type", "job_id",
    "event_time", "job_city", "job_category_mid",
]].copy()

event_out = event_out.sort_values(["talentNo", "event_time"]).reset_index(drop=True)
event_out.to_csv(OUT_EVENTS, index=False, encoding="utf-8-sig")
print(f"userBehaviorEvents.csv  →  {len(event_out):,} rows written.")


# ---------------------------------------------------------------------------
# Output 1: userBehaviorFeature.csv  (fully vectorised — no Python loops)
# ---------------------------------------------------------------------------
print("Computing per-user features (vectorised)...")

# Assign preference weight per event type
events["pref_weight"] = events["event_type"].map(
    {"apply": WEIGHT_APPLY, "view": WEIGHT_VIEW}
)

# --- Weighted city preference ---
# Sum weights per (talentNo, city), then rank within each user
city_weights = (
    events.dropna(subset=["job_city"])
    .groupby(["talentNo", "job_city"])["pref_weight"]
    .sum()
    .reset_index(name="city_w")
)
city_weights["city_rank"] = (
    city_weights.groupby("talentNo")["city_w"]
    .rank(method="first", ascending=False)
    .astype(int)
)
city_pivot = (
    city_weights[city_weights["city_rank"] <= 3]
    .pivot(index="talentNo", columns="city_rank", values="job_city")
    .rename(columns={1: "preferred_city_1", 2: "preferred_city_2", 3: "preferred_city_3"})
    .reset_index()
)
for col in ["preferred_city_1", "preferred_city_2", "preferred_city_3"]:
    if col not in city_pivot.columns:
        city_pivot[col] = None

# --- Weighted category preference ---
cat_weights = (
    events.dropna(subset=["job_category_mid"])
    .groupby(["talentNo", "job_category_mid"])["pref_weight"]
    .sum()
    .reset_index(name="cat_w")
)
cat_weights["cat_rank"] = (
    cat_weights.groupby("talentNo")["cat_w"]
    .rank(method="first", ascending=False)
    .astype(int)
)
cat_pivot = (
    cat_weights[cat_weights["cat_rank"] <= 3]
    .pivot(index="talentNo", columns="cat_rank", values="job_category_mid")
    .rename(columns={1: "preferred_category_1", 2: "preferred_category_2", 3: "preferred_category_3"})
    .reset_index()
)
for col in ["preferred_category_1", "preferred_category_2", "preferred_category_3"]:
    if col not in cat_pivot.columns:
        cat_pivot[col] = None

# --- Salary floor: 25th percentile of applied jobs per user ---
apply_salaries = (
    events[(events["event_type"] == "apply") & events["salary_lower"].notna()]
    [["talentNo", "salary_lower"]]
)
apply_salary_count = apply_salaries.groupby("talentNo")["salary_lower"].count().reset_index(name="apply_salary_count")
salary_floor = (
    apply_salaries.groupby("talentNo")["salary_lower"]
    .quantile(0.25)
    .round(0)
    .reset_index(name="salary_floor")
)
# Only keep salary_floor for users with enough apply events
salary_floor = salary_floor.merge(apply_salary_count, on="talentNo")
salary_floor.loc[salary_floor["apply_salary_count"] < MIN_APPLY_FOR_SALARY, "salary_floor"] = None
salary_floor = salary_floor[["talentNo", "salary_floor"]]

# --- Total events and last active ---
base = (
    events.groupby("talentNo")
    .agg(total_events=("event_type", "count"), last_active=("event_time", "max"))
    .reset_index()
)

# --- Merge all features ---
feature_df = (
    base
    .merge(city_pivot[["talentNo", "preferred_city_1", "preferred_city_2", "preferred_city_3"]], on="talentNo", how="left")
    .merge(cat_pivot[["talentNo", "preferred_category_1", "preferred_category_2", "preferred_category_3"]], on="talentNo", how="left")
    .merge(salary_floor, on="talentNo", how="left")
)

# Reorder columns
feature_df = feature_df[[
    "talentNo",
    "preferred_city_1", "preferred_city_2", "preferred_city_3",
    "preferred_category_1", "preferred_category_2", "preferred_category_3",
    "salary_floor", "total_events", "last_active",
]]

# Flag cold-start users for downstream convenience
feature_df["is_cold_start"] = feature_df["total_events"] < COLD_START_THRESHOLD

feature_df = feature_df.sort_values("talentNo").reset_index(drop=True)
feature_df.to_csv(OUT_FEATURE, index=False, encoding="utf-8-sig")
print(f"userBehaviorFeature.csv →  {len(feature_df):,} users written.")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
cold = feature_df["is_cold_start"].sum()
print(f"\n--- Summary ---")
print(f"Authenticated users  : {len(feature_df):,}")
print(f"Cold-start users     : {cold:,}  (total_events < {COLD_START_THRESHOLD})")
print(f"Normal users         : {len(feature_df) - cold:,}")
print(f"\nSample userBehaviorFeature.csv (first 5 rows):")
print(feature_df.head(5).to_string(index=False))
