"""
graph_ranker_interaction.py — Interaction-graph ranking, for evaluation only.

Why this module exists
----------------------
`src/graph_ranker.py` was refactored into an ability (skill) graph ranker. The
skill-graph route was subsequently dropped, but the 命題 still requires a
"有圖譜 vs 無圖譜" ablation, and 圖譜之具體結構 is explicitly left to the
participants:

    圖譜之具體結構（節點與邊之定義、是否帶權重等）由參賽者自行設計

This module restores the earlier user–job interaction graph ranking (commit
ca67a76) so that arm can still be measured. It is deliberately kept out of
`src/` — it is evaluation scaffolding, not part of the serving path — and it is
self-contained so it does not depend on whichever graph `src/graph_builder.py`
happens to build today.

Graph consumed: `dataset/graph_cache_train.pkl`, built by
`eval/build_graph_train.py` from train-period events only (06-01..06-05).

Schema and a worked traversal trace: 設計文件/graph_schema_and_trace.md
"""

from __future__ import annotations

import os
import pickle
from collections import defaultdict

import networkx as nx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRAPH_PATH = os.path.join(ROOT, "dataset", "graph_cache_train.pkl")

# Blend weights — unchanged from the original implementation.
GRAPH_WEIGHT = 0.7
POPULARITY_WEIGHT = 0.3

# Fan-out caps that bound worst-case traversal cost per query.
MAX_CO_USERS_PER_JOB = 30
MAX_SEED_JOBS = 50

_graph: nx.DiGraph | None = None


def user_node(talent_no: int) -> str:
    return f"user:{talent_no}"


def job_node(jid: int) -> str:
    return f"job:{jid}"


def get_graph() -> nx.DiGraph:
    """Load the train-period interaction graph (cached after first call)."""
    global _graph
    if _graph is None:
        with open(GRAPH_PATH, "rb") as fh:
            _graph = pickle.load(fh)
    return _graph


# ---------------------------------------------------------------------------
# Signed-in users: 2-hop collaborative filtering
# ---------------------------------------------------------------------------


def _signed_in_scores(G: nx.DiGraph, talent_no: int,
                      candidate_ids: set[int]) -> dict[int, float]:
    """you → your jobs → co-users → their other jobs, accumulating edge weight."""
    uid = user_node(talent_no)
    if uid not in G:
        return {}

    seeds = list(G.successors(uid))
    if not seeds:
        return {}
    if len(seeds) > MAX_SEED_JOBS:
        seeds.sort(key=lambda j: G.edges[uid, j].get("weight", 1), reverse=True)
        seeds = seeds[:MAX_SEED_JOBS]
    seed_set = set(seeds)

    scores: dict[int, float] = defaultdict(float)
    for seed in seeds:
        co_users = []
        for pred in G.predecessors(seed):
            if pred == uid or G.nodes[pred].get("node_type") != "User":
                continue
            co_users.append(pred)
            if len(co_users) >= MAX_CO_USERS_PER_JOB:
                break

        for co_user in co_users:
            w_seed = G.edges[co_user, seed].get("weight", 1)
            for co_job in G.successors(co_user):
                if co_job in seed_set or not co_job.startswith("job:"):
                    continue
                try:
                    jid = int(co_job.split(":", 1)[1])
                except (ValueError, IndexError):
                    continue
                if jid in candidate_ids:
                    scores[jid] += w_seed * G.edges[co_user, co_job].get("weight", 1)
    return dict(scores)


# ---------------------------------------------------------------------------
# Anonymous users: weighted in-degree ("crowd wisdom")
# ---------------------------------------------------------------------------


def _anonymous_scores(G: nx.DiGraph, candidate_ids: set[int]) -> dict[int, float]:
    """Total weighted interaction each candidate job received from real users."""
    scores: dict[int, float] = {}
    for jid in candidate_ids:
        jnode = job_node(jid)
        if jnode not in G:
            continue
        total = 0.0
        for pred in G.predecessors(jnode):
            total += G.edges[pred, jnode].get("weight", 1)
        if total > 0:
            scores[jid] = total
    return scores


# ---------------------------------------------------------------------------
# Blending
# ---------------------------------------------------------------------------


def _blend(candidates: list[dict], graph_scores: dict[int, float]) -> list[dict]:
    """final = normalised_graph × 0.7 + normalised_popularity × 0.3.

    Relevance still leads the sort: the retriever emits `relevance_hits` (how
    many query terms a job matched), and ranking on graph signal alone would
    throw that away — a job nobody searched for should not outrank an exact
    title match just because it is well connected.
    """
    raw_fields = [k for k in candidates[0].keys()
                  if k not in ("score", "relevance_hits")]

    if not graph_scores:
        ordered = sorted(
            candidates,
            key=lambda c: (c.get("relevance_hits", 0), c.get("score", 0.0),
                           c.get("職缺最後修改時間", "")),
            reverse=True,
        )
        return [{f: c[f] for f in raw_fields} for c in ordered[:10]]

    max_g = max(graph_scores.values()) or 1.0
    max_p = max((c.get("score", 0.0) for c in candidates), default=1.0) or 1.0

    def key(c: dict):
        jid = c.get("職缺編號")
        g = graph_scores.get(int(jid), 0.0) / max_g if jid is not None else 0.0
        p = c.get("score", 0.0) / max_p
        return (c.get("relevance_hits", 0), g * GRAPH_WEIGHT + p * POPULARITY_WEIGHT)

    ordered = sorted(candidates, key=key, reverse=True)
    return [{f: c[f] for f in raw_fields} for c in ordered[:10]]


# ---------------------------------------------------------------------------
# Public API — mirrors the shape ranking() expects
# ---------------------------------------------------------------------------


def graph_ranking(candidates: list[dict], talent_no: int) -> list[dict]:
    if not candidates:
        return []
    ids = {int(c["職缺編號"]) for c in candidates if c.get("職缺編號") is not None}
    try:
        scores = _signed_in_scores(get_graph(), talent_no, ids)
    except Exception:
        scores = {}
    return _blend(candidates, scores)


def graph_ranking_anonymous(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []
    ids = {int(c["職缺編號"]) for c in candidates if c.get("職缺編號") is not None}
    try:
        scores = _anonymous_scores(get_graph(), ids)
    except Exception:
        scores = {}
    return _blend(candidates, scores)
