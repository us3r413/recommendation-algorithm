"""
graph_ranker.py — Interaction-graph ranking using 2-hop collaborative filtering.

Two ranking modes:

1. Signed-in users (2-hop collaborative filtering):
     you → your seed jobs → co-users who also interacted → their other jobs
     Accumulates edge weights along the path as the graph score.

2. Anonymous users (weighted in-degree / "crowd wisdom"):
     Total weighted interactions each candidate job received from all users.
     Jobs with more (and heavier) interactions score higher.

Blending formula:
    final = relevance_hits (primary sort)
          + (normalised_graph × 0.7 + normalised_popularity × 0.3) (secondary)

Falls back to pure popularity ranking if the graph provides no signal.

Based on eval/graph_ranker_interaction.py — the proven approach that actually
produces real ranking signals from the User→Job interaction graph.
"""

from __future__ import annotations

import logging
from collections import defaultdict

import networkx as nx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Blend weights: graph signal vs popularity
GRAPH_WEIGHT = 0.7
POPULARITY_WEIGHT = 0.3

# Fan-out caps to bound worst-case traversal cost per query
MAX_CO_USERS_PER_JOB = 30
MAX_SEED_JOBS = 50


# ---------------------------------------------------------------------------
# Node ID helpers (must match graph_builder.py)
# ---------------------------------------------------------------------------


def _user_node(talent_no: int) -> str:
    return f"user:{talent_no}"


def _job_node(jid: int) -> str:
    return f"job:{jid}"


# ---------------------------------------------------------------------------
# Signed-in users: 2-hop collaborative filtering
# ---------------------------------------------------------------------------


def _signed_in_scores(
    G: nx.DiGraph, talent_no: int, candidate_ids: set[int]
) -> dict[int, float]:
    """Compute graph scores via 2-hop CF traversal.

    Traversal: you → your seed jobs → co-users → their other jobs (if in candidates)

    The score for each candidate accumulates the product of edge weights along
    the co-user path, giving higher scores to jobs that many similar users
    interacted with heavily.
    """
    uid = _user_node(talent_no)
    if uid not in G:
        return {}

    # Get user's seed jobs (jobs they interacted with), sorted by weight
    seeds = list(G.successors(uid))
    if not seeds:
        return {}
    if len(seeds) > MAX_SEED_JOBS:
        seeds.sort(key=lambda j: G.edges[uid, j].get("weight", 1), reverse=True)
        seeds = seeds[:MAX_SEED_JOBS]
    seed_set = set(seeds)

    scores: dict[int, float] = defaultdict(float)
    for seed in seeds:
        # Find co-users who also interacted with this seed job
        co_users = []
        for pred in G.predecessors(seed):
            if pred == uid or G.nodes[pred].get("node_type") != "User":
                continue
            co_users.append(pred)
            if len(co_users) >= MAX_CO_USERS_PER_JOB:
                break

        # For each co-user, check their other jobs
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
    """Total weighted interactions each candidate job received from all users.

    Jobs that more users viewed/applied to (especially applied, weight=3) get
    higher graph scores.
    """
    scores: dict[int, float] = {}
    for jid in candidate_ids:
        jnode = _job_node(jid)
        if jnode not in G:
            continue
        total = 0.0
        for pred in G.predecessors(jnode):
            total += G.edges[pred, jnode].get("weight", 1)
        if total > 0:
            scores[jid] = total
    return scores


# ---------------------------------------------------------------------------
# Blending: graph score + popularity → final ranking
# ---------------------------------------------------------------------------


def _blend(candidates: list[dict], graph_scores: dict[int, float]) -> list[dict]:
    """Blend graph scores with popularity, respecting relevance_hits as primary sort.

    final_score = normalised_graph × 0.7 + normalised_popularity × 0.3
    Primary sort: relevance_hits (more matched terms = higher priority)
    Secondary sort: final_score

    Falls back to pure popularity if graph_scores is empty.
    """
    raw_fields = [k for k in candidates[0].keys() if k not in ("score", "relevance_hits")]

    # No graph signal → popularity fallback
    if not graph_scores:
        ordered = sorted(
            candidates,
            key=lambda c: (
                c.get("relevance_hits", 0),
                c.get("score", 0.0),
                c.get("職缺最後修改時間", ""),
            ),
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
# Public API — called from src/ranker.py
# ---------------------------------------------------------------------------


def graph_ranking(candidates: list[dict], talent_no: int) -> list[dict]:
    """Rank candidates for a signed-in user using 2-hop collaborative filtering.

    Loads the interaction graph from graph_builder, computes CF scores for
    candidate jobs, then blends with popularity.

    Args:
        candidates: List of candidate dicts from grabFromDatabase().
        talent_no: The signed-in user's ID (must be != 0).

    Returns:
        Top 10 candidates ranked by blended score.
    """
    if not candidates:
        return []

    candidate_ids = {
        int(c["職缺編號"]) for c in candidates if c.get("職缺編號") is not None
    }

    try:
        from src.graph_builder import get_graph

        G = get_graph()
        logger.info(
            "[GraphRanker] signed_in: talent_no=%d, graph nodes=%d, edges=%d, candidates=%d",
            talent_no, G.number_of_nodes(), G.number_of_edges(), len(candidate_ids),
        )
        scores = _signed_in_scores(G, talent_no, candidate_ids)
        logger.info(
            "[GraphRanker] signed_in: jobs_with_signal=%d / %d",
            len(scores), len(candidate_ids),
        )
    except Exception as e:
        logger.warning("[GraphRanker] signed_in failed: %s", e)
        scores = {}

    return _blend(candidates, scores)


def graph_ranking_anonymous(candidates: list[dict]) -> list[dict]:
    """Rank candidates for an anonymous user using weighted in-degree.

    Jobs with more total user interactions (weighted: apply=3, view=1) get
    boosted relative to their popularity score.

    Args:
        candidates: List of candidate dicts from grabFromDatabase().

    Returns:
        Top 10 candidates ranked by blended score.
    """
    if not candidates:
        return []

    candidate_ids = {
        int(c["職缺編號"]) for c in candidates if c.get("職缺編號") is not None
    }

    try:
        from src.graph_builder import get_graph

        G = get_graph()
        logger.info(
            "[GraphRanker] anonymous: graph nodes=%d, edges=%d, candidates=%d",
            G.number_of_nodes(), G.number_of_edges(), len(candidate_ids),
        )
        scores = _anonymous_scores(G, candidate_ids)
        logger.info(
            "[GraphRanker] anonymous: jobs_with_signal=%d / %d",
            len(scores), len(candidate_ids),
        )
    except Exception as e:
        logger.warning("[GraphRanker] anonymous failed: %s", e)
        scores = {}

    return _blend(candidates, scores)
