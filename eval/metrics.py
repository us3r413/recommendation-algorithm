"""
metrics.py — Ranking metrics for the ablation harness.

Implements NDCG@k, Hit@1, Hit@k, Precision@k and MRR@k over graded relevance
labels (0 / 1 / 2), matching the measures named in the 命題 文件.

Gain convention
---------------
The organiser states that scoring uses pytrec_eval, which wraps trec_eval.
trec_eval's ndcg uses a LINEAR gain (gain = relevance value) with a log2(rank+1)
discount, so `linear` is the default here for comparability.

The exponential form (gain = 2^rel - 1), common in Learning-to-Rank literature,
is available via gain="exponential". Whichever is used must be stated in the
report — the two are not comparable to each other.

    DCG@k  = SUM_{i=1..k}  gain(rel_i) / log2(i + 1)
    IDCG@k = DCG@k of the ideal ordering of all labelled documents
    NDCG@k = DCG@k / IDCG@k     (0 when IDCG == 0)

Run `python eval/metrics.py` to execute the self-test.
"""

from __future__ import annotations

import math

DEFAULT_K = 10


def _gain(rel: float, mode: str) -> float:
    if mode == "linear":
        return float(rel)
    if mode == "exponential":
        return float(2 ** rel - 1)
    raise ValueError(f"unknown gain mode: {mode!r}")


def dcg(rels: list[float], k: int, gain: str = "linear") -> float:
    """Discounted cumulative gain over the first k positions."""
    return sum(_gain(r, gain) / math.log2(i + 2)
               for i, r in enumerate(rels[:k]))


def ndcg_at_k(ranked_ids: list[str], labels: dict[str, int],
              k: int = DEFAULT_K, gain: str = "linear") -> float:
    """Normalised DCG@k.

    Args:
        ranked_ids: job ids in predicted order, best first.
        labels: job id -> graded relevance (only relevant ids need be present).
        k: cutoff.
        gain: "linear" (trec_eval) or "exponential" (LTR convention).
    """
    got = [labels.get(j, 0) for j in ranked_ids[:k]]
    ideal = sorted(labels.values(), reverse=True)
    idcg = dcg(ideal, k, gain)
    if idcg <= 0.0:
        return 0.0
    return dcg(got, k, gain) / idcg


def hit_at_k(ranked_ids: list[str], labels: dict[str, int],
             k: int = DEFAULT_K) -> float:
    """1.0 if any of the top-k is relevant (label > 0), else 0.0."""
    return 1.0 if any(labels.get(j, 0) > 0 for j in ranked_ids[:k]) else 0.0


def precision_at_k(ranked_ids: list[str], labels: dict[str, int],
                   k: int = DEFAULT_K) -> float:
    """Fraction of the top-k that is relevant.

    The denominator is always k, even when fewer than k results are returned —
    this is the convention stated in the workshop briefing.
    """
    hits = sum(1 for j in ranked_ids[:k] if labels.get(j, 0) > 0)
    return hits / k


def mrr_at_k(ranked_ids: list[str], labels: dict[str, int],
             k: int = DEFAULT_K) -> float:
    """Reciprocal rank of the first relevant result within the top k."""
    for i, j in enumerate(ranked_ids[:k]):
        if labels.get(j, 0) > 0:
            return 1.0 / (i + 1)
    return 0.0


def evaluate_per_query(results: dict[str, list[str]],
                       qrels: dict[str, dict[str, int]],
                       k: int = DEFAULT_K,
                       gain: str = "linear") -> dict[str, dict[str, float]]:
    """Per-query scores, keyed by qid.

    Needed for anything the macro average cannot express: paired significance
    tests between arms, and stratified reporting (e.g. short keyword queries vs
    long multi-title queries, where a single mean hides opposite effects).

    Only queries with at least one relevant document are included, matching
    evaluate().
    """
    out: dict[str, dict[str, float]] = {}
    for qid, lab in qrels.items():
        if not any(v > 0 for v in lab.values()):
            continue
        ranked = results.get(qid, [])
        out[qid] = {
            f"NDCG@{k}": ndcg_at_k(ranked, lab, k, gain),
            "Hit@1": hit_at_k(ranked, lab, 1),
            f"Hit@{k}": hit_at_k(ranked, lab, k),
            f"P@{k}": precision_at_k(ranked, lab, k),
            f"MRR@{k}": mrr_at_k(ranked, lab, k),
        }
    return out


def evaluate(results: dict[str, list[str]], qrels: dict[str, dict[str, int]],
             k: int = DEFAULT_K, gain: str = "linear") -> dict[str, float]:
    """Aggregate metrics over a whole run.

    Args:
        results: qid -> ranked job ids.
        qrels:   qid -> {job id -> graded relevance}.
        k: cutoff for @k measures.
        gain: gain convention for NDCG.

    Returns:
        Dict of macro-averaged metrics plus counts. Queries with no relevant
        document are excluded from the averages (standard IR practice — they
        carry no signal and would only rescale every system identically).
    """
    scored = [q for q, lab in qrels.items() if any(v > 0 for v in lab.values())]
    if not scored:
        return {f"NDCG@{k}": 0.0, "Hit@1": 0.0, f"Hit@{k}": 0.0,
                f"P@{k}": 0.0, f"MRR@{k}": 0.0, "n_queries": 0,
                "n_empty_results": 0}

    acc = {f"NDCG@{k}": 0.0, "Hit@1": 0.0, f"Hit@{k}": 0.0,
           f"P@{k}": 0.0, f"MRR@{k}": 0.0}
    empty = 0
    for qid in scored:
        ranked = results.get(qid, [])
        if not ranked:
            empty += 1
        lab = qrels[qid]
        acc[f"NDCG@{k}"] += ndcg_at_k(ranked, lab, k, gain)
        acc["Hit@1"] += hit_at_k(ranked, lab, 1)
        acc[f"Hit@{k}"] += hit_at_k(ranked, lab, k)
        acc[f"P@{k}"] += precision_at_k(ranked, lab, k)
        acc[f"MRR@{k}"] += mrr_at_k(ranked, lab, k)

    n = len(scored)
    out = {m: v / n for m, v in acc.items()}
    out["n_queries"] = n
    out["n_empty_results"] = empty
    return out


# ---------------------------------------------------------------------------
# Self-test — hand-computed expected values
# ---------------------------------------------------------------------------


def _self_test() -> None:
    def close(a, b, tol=1e-6):
        assert abs(a - b) < tol, f"expected {b}, got {a}"

    labels = {"A": 2, "B": 1}

    # 1. Perfect ordering -> NDCG 1.0
    close(ndcg_at_k(["A", "B", "C"], labels), 1.0)

    # 2. Swapped: DCG = 1/log2(2) + 2/log2(3) = 1 + 1.261859507
    #    IDCG     = 2/log2(2) + 1/log2(3) = 2 + 0.630929754
    dcg_swapped = 1.0 + 2.0 / math.log2(3)
    idcg = 2.0 + 1.0 / math.log2(3)
    close(ndcg_at_k(["B", "A"], labels), dcg_swapped / idcg)

    # 3. Nothing relevant retrieved
    close(ndcg_at_k(["X", "Y", "Z"], labels), 0.0)
    close(hit_at_k(["X", "Y"], labels), 0.0)
    close(mrr_at_k(["X", "Y"], labels), 0.0)

    # 4. Single relevant (grade 1) at position 3
    #    DCG = 1/log2(4) = 0.5 ; IDCG = 1/log2(2) = 1.0
    close(ndcg_at_k(["X", "Y", "B"], {"B": 1}), 0.5)
    close(mrr_at_k(["X", "Y", "B"], {"B": 1}), 1.0 / 3.0)
    close(hit_at_k(["X", "Y", "B"], {"B": 1}, 10), 1.0)
    close(hit_at_k(["X", "Y", "B"], {"B": 1}, 1), 0.0)

    # 5. Precision@10 always divides by k
    close(precision_at_k(["A", "B"], labels, 10), 0.2)

    # 6. Exponential gain differs from linear
    lin = ndcg_at_k(["B", "A"], labels, 10, "linear")
    exp = ndcg_at_k(["B", "A"], labels, 10, "exponential")
    assert abs(lin - exp) > 1e-3, "gain conventions should differ here"
    #    exponential: DCG = (2^1-1)/1 + (2^2-1)/log2(3) = 1 + 3/1.5849625
    #                 IDCG = 3/1 + 1/log2(3)
    close(exp, (1.0 + 3.0 / math.log2(3)) / (3.0 + 1.0 / math.log2(3)))

    # 7. Cutoff is respected — relevant doc at position 11 must not count
    close(ndcg_at_k([f"X{i}" for i in range(10)] + ["A"], labels, 10), 0.0)
    close(hit_at_k([f"X{i}" for i in range(10)] + ["A"], labels, 10), 0.0)

    # 8. evaluate() drops queries with no relevant doc from the averages
    res = {"q1": ["A", "B"], "q2": ["X"]}
    qr = {"q1": {"A": 2, "B": 1}, "q2": {}}
    agg = evaluate(res, qr)
    assert agg["n_queries"] == 1, agg
    close(agg["NDCG@10"], 1.0)

    # 9. Empty result list is counted, scores zero
    agg2 = evaluate({"q1": []}, {"q1": {"A": 1}})
    close(agg2["NDCG@10"], 0.0)
    assert agg2["n_empty_results"] == 1

    print("metrics self-test: all 9 checks passed ✓")


if __name__ == "__main__":
    _self_test()
