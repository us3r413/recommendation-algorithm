"""
analyze_strata.py — Stratified comparison + paired significance test.

Why stratify
------------
A single macro average over all 500 queries hides the effect being measured.
The test set is dominated by very short keyword queries (median 3 characters:
「司機」「行政」「現領」), where rule-based tokenisation and LLM parsing produce
identical tags — those queries can only dilute any difference toward zero.

The LLM can only contribute where the rule-based fallback actually fails: long
queries and multi-title lists such as

    物業經理、社區經理、行政主管、高階主管特助
    工程師,軟體工程師,工程業務,醫材業務

The rule-based parser splits on whitespace only, so it treats each of those as a
single token that matches nothing. Reporting these strata separately shows where
the generative component earns its place instead of averaging it away.

Significance
------------
A paired bootstrap over queries (命題: "hackathon 規模下 paired test 統計顯著性
為加分非強制"). Paired because both arms are scored on exactly the same queries,
so the per-query difference is the natural unit.

Usage:
    python eval/analyze_strata.py                       # defaults
    python eval/analyze_strata.py --a no_expand --b no_llm_no_expand
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TESTSET = os.path.join(SCRIPT_DIR, "testset.jsonl")

# Separators users type between multiple job titles in one query.
_SEPARATORS = re.compile(r"[、,，.．;；/／|｜]")
SHORT_MAX_CHARS = 5


def classify(ks: str) -> str:
    """Split queries into the stratum where the LLM can plausibly matter."""
    s = ks.strip()
    if _SEPARATORS.search(s):
        return "multi"          # explicit multi-title list
    if " " in s:
        return "spaced"         # whitespace-delimited — rule parser handles it
    if len(s) <= SHORT_MAX_CHARS:
        return "short"          # single short keyword
    return "long"               # long unsegmented string


STRATA_ORDER = ["short", "spaced", "long", "multi"]
STRATA_LABEL = {
    "short": f"短關鍵字（≤{SHORT_MAX_CHARS} 字，無分隔）",
    "spaced": "空格分隔",
    "long": f"長字串（>{SHORT_MAX_CHARS} 字，無分隔）",
    "multi": "多職稱清單（頓號/逗號分隔）",
}


def paired_bootstrap(diffs: list[float], iters: int, seed: int):
    """Return (mean_diff, ci_low, ci_high, p_two_sided) over per-query diffs."""
    if not diffs:
        return 0.0, 0.0, 0.0, 1.0
    rng = random.Random(seed)
    n = len(diffs)
    observed = statistics.fmean(diffs)
    means = []
    for _ in range(iters):
        means.append(statistics.fmean([diffs[rng.randrange(n)] for _ in range(n)]))
    means.sort()
    lo = means[int(0.025 * iters)]
    hi = means[min(int(0.975 * iters), iters - 1)]
    # Two-sided p: proportion of resamples on the other side of zero, doubled.
    if observed >= 0:
        p = 2.0 * sum(1 for m in means if m <= 0.0) / iters
    else:
        p = 2.0 * sum(1 for m in means if m >= 0.0) / iters
    return observed, lo, hi, min(p, 1.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=os.path.join(SCRIPT_DIR, "ablation_perquery.json"),
                    help="ablation output containing a per_query section")
    ap.add_argument("--a", default="no_expand", help="arm A (expected better)")
    ap.add_argument("--b", default="no_llm_no_expand", help="arm B (baseline)")
    ap.add_argument("--metric", default="NDCG@10")
    ap.add_argument("--iters", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out-md", default=os.path.join(SCRIPT_DIR, "STRATIFIED_REPORT.md"))
    args = ap.parse_args()

    if not os.path.exists(args.json):
        print(f"ERROR: {args.json} not found — the per-query run has not finished.",
              file=sys.stderr)
        return 1

    with open(args.json, encoding="utf-8") as fh:
        data = json.load(fh)
    pq = data.get("per_query") or {}
    if args.a not in pq or args.b not in pq:
        print(f"ERROR: need per-query scores for both {args.a!r} and {args.b!r}; "
              f"found: {', '.join(pq) or '(none)'}", file=sys.stderr)
        return 1

    # qid -> query text, for stratification
    strata: dict[str, str] = {}
    with open(TESTSET, encoding="utf-8") as fh:
        for line in fh:
            q = json.loads(line)
            strata[q["qid"]] = classify(q["ks"])

    a, b = pq[args.a], pq[args.b]
    shared = [q for q in a if q in b]
    m = args.metric

    rows = []
    for st in STRATA_ORDER + ["ALL"]:
        qids = shared if st == "ALL" else [q for q in shared if strata.get(q) == st]
        if not qids:
            continue
        va = statistics.fmean(a[q][m] for q in qids)
        vb = statistics.fmean(b[q][m] for q in qids)
        diffs = [a[q][m] - b[q][m] for q in qids]
        obs, lo, hi, p = paired_bootstrap(diffs, args.iters, args.seed)
        rel = (va - vb) / vb * 100 if vb > 0 else float("nan")
        rows.append((st, len(qids), vb, va, rel, obs, lo, hi, p))

    # ---- console ----------------------------------------------------------
    print(f"\n分層比較：{args.a} (A) vs {args.b} (B)   指標 {m}")
    print(f"配對 bootstrap {args.iters:,} 次，seed={args.seed}\n")
    hdr = f"{'分層':<26}{'題數':>5}{'B(基準)':>10}{'A':>10}{'相對':>9}{'95% CI':>22}{'p':>8}"
    print(hdr)
    print("-" * len(hdr))
    for st, n, vb, va, rel, obs, lo, hi, p in rows:
        label = "全部" if st == "ALL" else STRATA_LABEL[st]
        sig = " *" if p < 0.05 else ""
        print(f"{label:<26}{n:>5}{vb:>10.4f}{va:>10.4f}{rel:>8.1f}%"
              f"{f'[{lo:+.4f}, {hi:+.4f}]':>22}{p:>8.3f}{sig}")
    print("\n* p < 0.05")

    # ---- markdown ---------------------------------------------------------
    with open(args.out_md, "w", encoding="utf-8") as fh:
        fh.write("# 分層分析：LLM 的貢獻集中在哪裡\n\n")
        fh.write(f"比較：`{args.a}`（A，有 LLM） vs `{args.b}`（B，規則式）　指標 **{m}**\n\n")
        fh.write(f"配對 bootstrap {args.iters:,} 次重抽樣，seed = {args.seed}。\n\n")
        fh.write("## 為何要分層\n\n")
        fh.write("測試集以極短關鍵字為主（長度中位數 3 字，如「司機」「行政」「現領」），"
                 "這類查詢的規則式分詞與 LLM 解析輸出**完全相同**，只會把差異稀釋為零。\n\n")
        fh.write("LLM 唯一能發揮作用的地方，是規則式真正失效之處 —— 長查詢與多職稱清單"
                 "（規則式只切空格，整串當一個詞，撈不到任何東西）。\n\n")
        fh.write("## 結果\n\n")
        fh.write("| 分層 | 題數 | B 規則式 | A 有 LLM | 相對變化 | 95% CI（差值） | p |\n")
        fh.write("|---|---:|---:|---:|---:|---:|---:|\n")
        for st, n, vb, va, rel, obs, lo, hi, p in rows:
            label = "**全部**" if st == "ALL" else STRATA_LABEL[st]
            sig = " \\*" if p < 0.05 else ""
            fh.write(f"| {label} | {n} | {vb:.4f} | {va:.4f} | {rel:+.1f}% | "
                     f"[{lo:+.4f}, {hi:+.4f}] | {p:.3f}{sig} |\n")
        fh.write("\n\\* p < 0.05（配對 bootstrap，雙尾）\n\n")
        fh.write("## 讀法\n\n")
        fh.write("- CI 不跨越 0 且 p < 0.05 → 該分層的差異在統計上站得住\n")
        fh.write("- CI 跨越 0 → 差異落在雜訊範圍內，不應宣稱有改善\n")
        fh.write("- 分層題數少時 CI 會很寬，這是樣本量的限制，非方法問題\n")
    print(f"\nWROTE {args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
