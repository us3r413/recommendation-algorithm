"""
run_ablation.py — "with graph vs without graph" / "with LLM vs without LLM"
ablation harness, one command, reproducible.

    python eval/run_ablation.py                 # all available arms
    python eval/run_ablation.py --arms full,no_llm
    python eval/run_ablation.py --limit 50      # quick smoke run

Leakage guards
--------------
* Popularity is read from 瀏覽次數_train.csv (events 06-01..06-05 only).
* Queries come from 06-06 only; labels are observed over 06-06..06-07.
* talent_no is forced to 0 because the official API contract carries no
  talentNo — the personalisation path cannot fire during judging, so scoring
  with it would overstate the system.  --use-talent enables it for the
  product-story arm, reported separately.

The silent-fallback trap
------------------------
querytoRequirement() falls back to rule-based parsing whenever the Bedrock call
fails — including when credentials are simply missing. If that happened during
an evaluation, the "with LLM" arm would silently become identical to the
"without LLM" arm, and the ablation would appear to prove that generative AI
contributes nothing. That is the exact conclusion this harness exists to test,
so it must never be reached by accident. probe_llm() therefore verifies a live
Bedrock call BEFORE any LLM arm runs, and refuses to run the arm otherwise.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)

TESTSET = os.path.join(SCRIPT_DIR, "testset.jsonl")
TAG_CACHE = os.path.join(SCRIPT_DIR, "llm_tag_cache.json")
POP_TRAIN = os.path.join(ROOT, "dataset", "瀏覽次數_train.csv")
JOBS_PARQUET = os.path.join(ROOT, "dataset", "職缺.parquet")
JOBS_CSV = os.path.join(ROOT, "dataset", "職缺.csv")

from eval.metrics import evaluate, evaluate_per_query  # noqa: E402

# Train-only interaction graph (see eval/build_graph_train.py for why the
# production full-week graph must not be used here).
GRAPH_TRAIN = os.path.join(ROOT, "dataset", "graph_cache_train.pkl")

# ---------------------------------------------------------------------------
# Arm definitions
# ---------------------------------------------------------------------------

ARMS: dict[str, dict] = {
    # The system as deployed: LLM parsing + synonym expansion + popularity rank
    "full":       dict(llm=True,  expand=True,  graph=False, rank="popularity",
                       desc="完整系統（LLM + 語意擴展 + 熱門度排序）"),
    # THE headline ablation the 命題 asks for: generative AI removed
    "no_llm":     dict(llm=False, expand=True,  graph=False, rank="popularity",
                       desc="移除 LLM（退回規則式分詞）"),
    # Diagnostic: is CodeAlike expansion helping or flooding the candidate set?
    "no_expand":  dict(llm=True,  expand=False, graph=False, rank="popularity",
                       desc="移除語意擴展（僅原始關鍵字）"),
    # Interaction-graph ranking instead of raw popularity
    "graph":      dict(llm=True,  expand=True,  graph=True,  rank="popularity",
                       desc="啟用互動圖譜排序"),
    # Control: no ranking at all — isolates what the ranker contributes
    "no_rank":    dict(llm=True,  expand=True,  graph=False, rank="none",
                       desc="不排序（檢索原始順序，對照組）"),

    # Conditional expansion — see make_hybrid_parser() for the rationale.
    "hybrid":     dict(llm=True,  expand=True,  graph=False, rank="popularity",
                       hybrid=True,
                       desc="條件式擴展（字面查詢過少時才用 LLM 擴展）"),

    # --- Credential-free diagnostics -------------------------------------
    # These isolate the retrieval and ranking stages without needing Bedrock,
    # so the pipeline can be diagnosed before AWS credentials are available.
    # Their reference point is `no_llm`, not `full`.
    "no_llm_no_expand": dict(llm=False, expand=False, graph=False,
                             rank="popularity",
                             desc="規則式解析 + 移除語意擴展"),
    "no_llm_no_rank":   dict(llm=False, expand=True, graph=False, rank="none",
                             desc="規則式解析 + 不排序（對照組）"),
}


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Query parsing backends
# ---------------------------------------------------------------------------


def make_rule_parser():
    """The documented fallback path of querytoRequirement(), verbatim."""
    from src.query_parser import _post_process_tags
    from src.utils.abbreviations import abbreviation_expand

    def parse(q: str) -> list[str]:
        expanded = abbreviation_expand(q)
        if not expanded.strip():
            return []
        return _post_process_tags(expanded.split())

    return parse


def make_llm_parser(cache: dict):
    from src.query_parser import querytoRequirement

    def parse(q: str) -> list[str]:
        if q in cache:
            return cache[q]
        tags = querytoRequirement(q)
        cache[q] = tags
        return tags

    return parse


HYBRID_MIN_CANDIDATES = 200


def make_hybrid_parser(cache: dict, retriever):
    """Use the literal query first; fall back to LLM expansion only if it is sparse.

    Measured behaviour of the always-expand prompt: it trades precision for
    recall on short keywords, and NDCG@10 punishes precision loss at the top.
    「現領」 (12.8% of the test set) goes from 142 candidates to 10,468 once the
    LLM adds 日薪/當日領/即領/日結/週領/現金 — every synonym is correct, but the
    job the user actually clicked is now buried, and `relevance_hits` will even
    prefer a job matching three loose synonyms over an exact 現領 match.

    Expansion is what you want in the opposite case: when the literal terms
    retrieve almost nothing, recall is the binding constraint and the LLM's
    world knowledge (飲料店 → 五十嵐/清心/迷客夏) is the only way to find
    anything at all. So gate on candidate count rather than applying it always.
    """
    llm_parse = make_llm_parser(cache)
    rule_parse = make_rule_parser()

    def parse_with_counts(q: str, c0, d0):
        literal = rule_parse(q)
        if literal:
            n = len(retriever.grabFromDatabase(literal, c0=c0, d0=d0))
            if n >= HYBRID_MIN_CANDIDATES:
                return literal, "literal"
        return llm_parse(q), "expanded"

    return parse_with_counts


def probe_llm() -> tuple[bool, str]:
    """Verify a live Bedrock call. Returns (ok, message)."""
    try:
        import boto3
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, ".env"))
        model_id = os.environ.get(
            "BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0")
        client = boto3.client("bedrock-runtime")
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}],
        })
        client.invoke_model(modelId=model_id, contentType="application/json",
                            accept="application/json", body=body)
        return True, f"Bedrock OK ({model_id})"
    except Exception as e:  # noqa: BLE001 - any failure means the arm is invalid
        return False, f"{type(e).__name__}: {str(e)[:160]}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def load_testset(path: str, limit: int | None):
    queries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                queries.append(json.loads(line))
    if limit:
        queries = queries[:limit]
    qrels = {q["qid"]: {k: int(v) for k, v in q["labels"].items()} for q in queries}
    return queries, qrels


def run_arm(name: str, cfg: dict, queries: list[dict], use_talent: bool,
            tag_cache: dict) -> tuple[dict[str, list[str]], float]:
    """Execute one arm over the whole test set. Returns (results, seconds)."""
    import src.retriever as R
    import src.ranker as K

    # --- leakage + speed configuration -------------------------------------
    R.VIEWS_PATH = POP_TRAIN
    R.JOBS_PATH = JOBS_PARQUET if os.path.exists(JOBS_PARQUET) else JOBS_CSV

    # --- semantic expansion toggle -----------------------------------------
    if not hasattr(R, "_orig_semantic_expand"):
        R._orig_semantic_expand = R.semantic_expand
    R.semantic_expand = (R._orig_semantic_expand if cfg["expand"]
                         else (lambda terms: list(terms)))

    # --- graph toggle -------------------------------------------------------
    # ranking() reads these globals at call time and lazily imports
    # src.graph_ranker. That module now targets the ability (skill) graph, which
    # the team dropped; the interaction-graph arm is served instead from
    # eval/graph_ranker_interaction.py, injected here so src/ stays untouched.
    K.GRAPH_FOR_ANONYMOUS = bool(cfg["graph"])
    K.USE_GRAPH_RAG = bool(cfg["graph"])
    if cfg["graph"]:
        import eval.graph_ranker_interaction as GRI
        sys.modules["src.graph_ranker"] = GRI

    hybrid = cfg.get("hybrid", False)
    if hybrid:
        hybrid_parse = make_hybrid_parser(tag_cache, R)
        parse = None
    else:
        parse = make_llm_parser(tag_cache) if cfg["llm"] else make_rule_parser()

    results: dict[str, list[str]] = {}
    routed = {"literal": 0, "expanded": 0}
    t0 = time.time()
    for i, q in enumerate(queries, 1):
        c0 = [c.strip() for c in (q["c0"] or "").split(",") if c.strip()] or None
        d0 = [d.strip() for d in (q["d0"] or "").split(",") if d.strip()] or None
        talent = int(q["talentNo"]) if use_talent else 0
        try:
            if hybrid:
                tags, route = hybrid_parse(q["ks"], c0, d0)
                routed[route] += 1
            else:
                tags = parse(q["ks"])
            cands = R.grabFromDatabase(tags, c0=c0, d0=d0)
            if cfg["rank"] == "none":
                top = cands[:10]
            else:
                top = K.ranking(cands, talent)
            results[q["qid"]] = [str(int(r["職缺編號"])) for r in top
                                 if r.get("職缺編號") is not None]
        except Exception as e:  # noqa: BLE001
            log(f"    query {q['qid']} failed: {type(e).__name__}: {e}")
            results[q["qid"]] = []
        if i % 50 == 0:
            rate = (time.time() - t0) / i
            log(f"    {name}: {i}/{len(queries)}  ({rate:.2f}s/query, "
                f"eta {rate*(len(queries)-i)/60:.1f}min)")
    if hybrid:
        log(f"    routing: {routed['literal']} literal / "
            f"{routed['expanded']} expanded")
    return results, time.time() - t0


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--testset", default=TESTSET)
    ap.add_argument("--arms", default="", help="comma list; default = all available")
    ap.add_argument("--limit", type=int, default=None, help="only N queries")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--gain", choices=["linear", "exponential"], default="linear",
                    help="NDCG gain convention (linear matches trec_eval/pytrec_eval)")
    ap.add_argument("--use-talent", action="store_true",
                    help="use the real talentNo (NOT the official contract)")
    ap.add_argument("--out-json", default=os.path.join(SCRIPT_DIR, "ablation_results.json"))
    ap.add_argument("--out-md", default=os.path.join(SCRIPT_DIR, "ABLATION_REPORT.md"))
    args = ap.parse_args()

    if not os.path.exists(args.testset):
        print(f"ERROR: missing test set {args.testset}\n"
              f"Run: python eval/build_testset.py", file=sys.stderr)
        return 1
    if not os.path.exists(POP_TRAIN):
        print(f"ERROR: missing {POP_TRAIN}\n"
              f"Run: python eval/build_popularity.py", file=sys.stderr)
        return 1

    queries, qrels = load_testset(args.testset, args.limit)
    log(f"Loaded {len(queries)} queries, "
        f"{sum(len(v) for v in qrels.values())} relevance labels")

    requested = [a.strip() for a in args.arms.split(",") if a.strip()] or list(ARMS)
    for a in requested:
        if a not in ARMS:
            print(f"ERROR: unknown arm {a!r}. Known: {', '.join(ARMS)}", file=sys.stderr)
            return 1

    # --- LLM availability gate (see module docstring) -----------------------
    needs_llm = any(ARMS[a]["llm"] for a in requested)
    llm_ok, llm_msg = (False, "not probed")
    if needs_llm:
        log("Probing Bedrock before running any LLM arm...")
        llm_ok, llm_msg = probe_llm()
        log(f"  {'✓' if llm_ok else '✗'} {llm_msg}")
        if not llm_ok:
            skipped = [a for a in requested if ARMS[a]["llm"]]
            log("  LLM arms will be SKIPPED (not silently downgraded): "
                + ", ".join(skipped))
            log("  Provide .env with valid AWS credentials to run them.")
            requested = [a for a in requested if not ARMS[a]["llm"]]
            if not requested:
                print("ERROR: no runnable arms.", file=sys.stderr)
                return 1

    tag_cache: dict = {}
    if os.path.exists(TAG_CACHE):
        with open(TAG_CACHE, encoding="utf-8") as fh:
            tag_cache = json.load(fh)
        log(f"Loaded {len(tag_cache)} cached LLM parses")

    report: dict[str, dict] = {}
    per_query: dict[str, dict] = {}
    for name in requested:
        cfg = ARMS[name]
        log(f"--- arm: {name} — {cfg['desc']}")
        if cfg["graph"] and not os.path.exists(GRAPH_TRAIN):
            log(f"    SKIPPED: {GRAPH_TRAIN} not built "
                "(python eval/build_graph_train.py)")
            continue
        res, secs = run_arm(name, cfg, queries, args.use_talent, tag_cache)
        m = evaluate(res, qrels, args.k, args.gain)
        per_query[name] = evaluate_per_query(res, qrels, args.k, args.gain)
        m["seconds"] = round(secs, 1)
        m["sec_per_query"] = round(secs / max(len(queries), 1), 3)
        m["desc"] = cfg["desc"]
        report[name] = m
        log(f"    NDCG@{args.k}={m[f'NDCG@{args.k}']:.4f}  "
            f"Hit@1={m['Hit@1']:.4f}  Hit@{args.k}={m[f'Hit@{args.k}']:.4f}  "
            f"({secs:.0f}s)")

    if tag_cache:
        with open(TAG_CACHE, "w", encoding="utf-8") as fh:
            json.dump(tag_cache, fh, ensure_ascii=False)

    meta = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "n_queries": len(queries),
        "k": args.k,
        "gain": args.gain,
        "use_talent": args.use_talent,
        "llm_available": llm_ok,
        "llm_probe": llm_msg,
        "train_period": "2026-06-01..2026-06-05",
        "test_query_day": "2026-06-06",
        "label_window": "2026-06-06..2026-06-07",
    }
    with open(args.out_json, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "arms": report, "per_query": per_query},
                  fh, ensure_ascii=False, indent=2)

    write_markdown(args.out_md, meta, report, args.k)
    log(f"WROTE {args.out_json}")
    log(f"WROTE {args.out_md}")

    print()
    print(render_table(report, args.k))
    return 0


def render_table(report: dict, k: int) -> str:
    if not report:
        return "(no arms were run)"
    # Reference arm for the relative-change column: the full system when it was
    # run, otherwise the first arm present (so the column is never blank).
    ref_name = "full" if "full" in report else next(iter(report))
    ref = report[ref_name][f"NDCG@{k}"]

    hdr = (f"| 設定 | NDCG@{k} | vs 基準 | Hit@1 | Hit@{k} | P@{k} | "
           f"MRR@{k} | 秒/查詢 |")
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|"
    lines = [hdr, sep]
    for name, m in report.items():
        if name == ref_name:
            delta = "基準"
        elif ref > 0:
            delta = f"{(m[f'NDCG@{k}']-ref)/ref*100:+.1f}%"
        else:
            delta = "—"
        lines.append(
            f"| `{name}` {m['desc']} | {m[f'NDCG@{k}']:.4f} | {delta} | "
            f"{m['Hit@1']:.4f} | {m[f'Hit@{k}']:.4f} | {m[f'P@{k}']:.4f} | "
            f"{m[f'MRR@{k}']:.4f} | {m['sec_per_query']:.2f} |")
    return "\n".join(lines)


def write_markdown(path: str, meta: dict, report: dict, k: int) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Ablation 報告：有圖譜 vs 無圖譜 / 有 LLM vs 無 LLM\n\n")
        fh.write(f"產生時間：{meta['generated']}\n\n")
        fh.write("## 評估設定\n\n")
        fh.write("| 項目 | 值 |\n|---|---|\n")
        fh.write(f"| 訓練期 | {meta['train_period']} |\n")
        fh.write(f"| 測試查詢日 | {meta['test_query_day']} |\n")
        fh.write(f"| 標註觀察窗口 | {meta['label_window']} |\n")
        fh.write(f"| 查詢數 | {meta['n_queries']} |\n")
        fh.write(f"| NDCG gain 慣例 | {meta['gain']}（linear 對齊 trec_eval/pytrec_eval）|\n")
        fh.write(f"| talentNo | {'使用真實值' if meta['use_talent'] else '一律 0（官方合約無此欄位）'} |\n")
        fh.write(f"| LLM 可用 | {meta['llm_available']} — {meta['llm_probe']} |\n\n")
        fh.write("## 結果\n\n")
        fh.write(render_table(report, k) + "\n\n")
        ref_name = "full" if "full" in report else (next(iter(report)) if report else "—")
        fh.write(f"> 「vs 基準」欄為相對於 `{ref_name}` 的 NDCG 相對變化。\n\n")
        fh.write("## 相關性標註定義\n\n")
        fh.write("- 2 = 搜尋後投遞履歷；1 = 搜尋後點閱；0 = 無互動\n")
        fh.write("- 歸因窗口：搜尋後 30 分鐘內，並於該使用者下一次搜尋時截斷（下限 2 分鐘）\n")
        fh.write("- 未限制於 `empStr`（既有系統當時的曝光清單），以免將既有排序的曝光偏差寫進標準答案\n\n")
        fh.write("## 已知限制\n\n")
        fh.write("- 主辦方未公告官方 train/test 切分，本表採自訂時序切分\n")
        fh.write("- 測試查詢日為週六，與平日流量分布不同\n")
        fh.write("- label = 0 不等同不相關，受觀察窗口與既有排序曝光偏差影響\n")
        fh.write("- 未採用 position bias correction（IPS / Doubly Robust）\n")


if __name__ == "__main__":
    raise SystemExit(main())
