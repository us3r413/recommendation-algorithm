#!/usr/bin/env bash
#
# reproduce.sh — one-command reproduction of the ablation report.
#
#   bash eval/reproduce.sh              # full run (500 queries)
#   bash eval/reproduce.sh --quick      # 50 queries, for a fast sanity check
#
# Everything is deterministic given --seed (default 42): the query sample, the
# shuffle, and the ranking are all seeded or order-stable. Re-running produces
# byte-identical testset.jsonl.
#
# Requires dataset/ to contain the six organiser-provided CSVs. Derived tables
# are rebuilt here rather than shipped, so nothing depends on a stale artefact.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LIMIT_ARG=""
TARGET=500
if [[ "${1:-}" == "--quick" ]]; then
  LIMIT_ARG="--limit 50"
  TARGET=50
  echo ">>> QUICK MODE: 50 queries"
fi

# --- 0. Python environment --------------------------------------------------
if [[ ! -x .venv/bin/python ]]; then
  echo ">>> [0/4] Creating .venv and installing requirements.txt ..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
fi
PY=.venv/bin/python
echo ">>> Using $($PY -V)"

# --- 0b. Required inputs ----------------------------------------------------
missing=0
for f in "dataset/職缺.csv" \
         "dataset/職缺瀏覽_20260601_20260607.csv" \
         "dataset/主動應徵_0601-0607.csv" \
         "dataset/userSearchLog_20260601_20260607.csv" \
         "dataset/城市對照表.csv" \
         "dataset/職務對照表.csv"; do
  if [[ ! -f "$f" ]]; then
    echo "MISSING INPUT: $f" >&2
    missing=1
  fi
done
if [[ $missing -eq 1 ]]; then
  echo "Place the organiser-provided CSVs in dataset/ and re-run." >&2
  exit 1
fi

# --- 1. Columnar copy of the job table (speed only, results identical) ------
if [[ ! -f "dataset/職缺.parquet" ]]; then
  echo ">>> [1/4] Converting 職缺.csv to Parquet (one-off, ~5s) ..."
  $PY - <<'PYEOF'
import duckdb
duckdb.connect().execute(
    "COPY (SELECT * FROM 'dataset/職缺.csv') "
    "TO 'dataset/職缺.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)")
PYEOF
fi

# --- 2. Train-only popularity table (leakage guard) -------------------------
echo ">>> [2/4] Building train-only popularity table (events 06-01..06-05) ..."
$PY eval/build_popularity.py

# --- 3. Train-only interaction graph (leakage guard) ------------------------
# The production graph spans the whole week and therefore contains the test-day
# clicks; it cannot be used for evaluation. Rebuild from train-period events.
echo ">>> [3/5] Building train-only interaction graph ..."
$PY eval/build_graph_train.py

# --- 4. Test set with graded relevance labels -------------------------------
echo ">>> [4/5] Building test set (queries 06-06, labels 06-06..06-07) ..."
$PY eval/build_testset.py --target "$TARGET" --seed 42

# --- 5. Ablation ------------------------------------------------------------
echo ">>> [5/5] Running ablation ..."
# LLM arms are skipped automatically (not silently downgraded) when Bedrock is
# unreachable; see the probe_llm() note in run_ablation.py.
# shellcheck disable=SC2086
$PY eval/run_ablation.py $LIMIT_ARG

# Stratified comparison + paired significance test. Needs per-query scores for
# both arms; skipped with a message if those arms were not run.
echo ">>> Stratified analysis ..."
$PY eval/analyze_strata.py --json eval/ablation_results.json \
    --a no_expand --b no_llm_no_expand || \
    echo "    (skipped — run --arms no_expand,no_llm_no_expand for the stratified table)"

echo
echo ">>> DONE"
echo "    eval/ABLATION_REPORT.md     — report table + limitations"
echo "    eval/STRATIFIED_REPORT.md   — where the LLM actually contributes"
echo "    eval/ablation_results.json  — raw metrics + per-query scores"
echo "    eval/testset.jsonl          — queries + graded relevance labels"
