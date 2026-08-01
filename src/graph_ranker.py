"""
graph_ranker.py — Graph-based ranking using networkx (in-memory).

Two ranking modes:

1. Signed-in users (2-hop collaborative filtering):
   User → Jobs they interacted with
        → Other users who interacted with same jobs (co-users)
        → OTHER jobs those co-users liked
        = candidate recommendations from the graph

2. Anonymous users (graph-degree scoring):
   For each candidate job, compute its weighted in-degree from the graph
   (total interaction weight from all users). Jobs that attracted more
   engagement rank higher — a "crowd wisdom" signal beyond raw view count.

Formula (both modes):
  final_score = normalised_graph_score × 0.7 + normalised_popularity × 0.3

Falls back to popularity ranking if graph provides no signal.
"""

from collections import defaultdict

import networkx as nx

from src.graph_builder import get_graph, user_id, job_id

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Blending weights
GRAPH_WEIGHT = 0.7
POPULARITY_WEIGHT = 0.3

# Max co-users to consider per job (limit fan-out)
MAX_CO_USERS_PER_JOB = 30

# Max user's own jobs to traverse from (limit starting fan-out)
MAX_SEED_JOBS = 50


# ---------------------------------------------------------------------------
# Core traversal
# ---------------------------------------------------------------------------


def _get_graph_scores(
    G: nx.DiGraph,
    talent_no: int,
    candidate_job_ids: set[int],
) -> dict[int, float]:
    """2-hop collaborative filtering entirely in networkx.

    Steps:
      1. Find jobs the target user interacted with (seed jobs)
      2. For each seed job, find other users (co-users) who also interacted
      3. For each co-user, find their other jobs
      4. If those jobs are in the candidate set, accumulate their edge weight

    Args:
        G: The interaction graph.
        talent_no: Target user ID.
        candidate_job_ids: Set of job IDs from Stage 2.

    Returns:
        Dict mapping job_id → graph_score for jobs in candidate_job_ids.
    """
    uid = user_id(talent_no)

    # Check if user exists in graph
    if uid not in G:
        return {}

    # Step 1: Get seed jobs (jobs this user interacted with)
    seed_jobs = list(G.successors(uid))
    if not seed_jobs:
        return {}

    # Limit seed jobs to highest-weighted ones
    if len(seed_jobs) > MAX_SEED_JOBS:
        seed_jobs_weighted = [
            (j, G.edges[uid, j].get("weight", 1)) for j in seed_jobs
        ]
        seed_jobs_weighted.sort(key=lambda x: x[1], reverse=True)
        seed_jobs = [j for j, _ in seed_jobs_weighted[:MAX_SEED_JOBS]]

    seed_job_set = set(seed_jobs)

    # Step 2-3: For each seed job, find co-users → their other jobs
    job_scores: dict[int, float] = defaultdict(float)

    for seed_job in seed_jobs:
        # Find co-users (other users who interacted with this job)
        co_users = []
        for pred in G.predecessors(seed_job):
            if pred == uid:
                continue  # skip self
            if G.nodes[pred].get("node_type") != "User":
                continue
            co_users.append(pred)
            if len(co_users) >= MAX_CO_USERS_PER_JOB:
                break

        # For each co-user, find their other jobs
        for co_user in co_users:
            co_user_weight = G.edges[co_user, seed_job].get("weight", 1)

            for co_job in G.successors(co_user):
                if co_job in seed_job_set:
                    continue  # skip jobs user already interacted with

                # Extract numeric job_id
                if not co_job.startswith("job:"):
                    continue
                try:
                    jid = int(co_job.split(":", 1)[1])
                except (ValueError, IndexError):
                    continue

                # Only score if it's in our candidate set
                if jid in candidate_job_ids:
                    edge_w = G.edges[co_user, co_job].get("weight", 1)
                    # Score: co-user's affinity to seed × co-user's affinity to this job
                    job_scores[jid] += co_user_weight * edge_w

    return dict(job_scores)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def graph_ranking(candidates: list[dict], talent_no: int) -> list[dict]:
    """Rank candidates using graph-based collaborative filtering.

    Blends the graph recommendation score (2-hop traversal) with the
    popularity score (from Stage 2).

    Formula:
        final_score = normalised_graph_score × 0.7 + normalised_popularity × 0.3

    Falls back to pure popularity ranking if graph provides no signal.

    Args:
        candidates: List of candidate dicts from grabFromDatabase(), each
                    containing all 職缺.csv columns plus "score" (popularity).
        talent_no: The signed-in user's ID (must be != 0).

    Returns:
        Top 10 candidates ranked by blended score, with "score" field stripped.
    """
    if not candidates:
        return []

    raw_fields = [k for k in candidates[0].keys() if k != "score"]

    # Build candidate lookup
    candidate_map: dict[int, dict] = {}
    for c in candidates:
        jid = c.get("職缺編號")
        if jid is not None:
            candidate_map[int(jid)] = c

    candidate_job_ids = set(candidate_map.keys())

    # Get graph scores
    try:
        G = get_graph()
        graph_scores = _get_graph_scores(G, talent_no, candidate_job_ids)
    except Exception:
        graph_scores = {}

    # Fallback: no graph signal → popularity ranking
    if not graph_scores:
        sorted_candidates = sorted(
            candidates,
            key=lambda c: (c.get("score", 0.0), c.get("職缺最後修改時間", "")),
            reverse=True,
        )
        top10 = sorted_candidates[:10]
        return [{f: c[f] for f in raw_fields} for c in top10]

    # Normalise graph scores
    max_graph = max(graph_scores.values())
    if max_graph == 0:
        max_graph = 1.0

    # Normalise popularity scores
    max_pop = max((c.get("score", 0.0) for c in candidates), default=1.0)
    if max_pop == 0:
        max_pop = 1.0

    # Compute blended final score
    def final_score(c: dict) -> float:
        jid = c.get("職缺編號")
        g_score = graph_scores.get(int(jid), 0.0) / max_graph if jid else 0.0
        p_score = c.get("score", 0.0) / max_pop
        return g_score * GRAPH_WEIGHT + p_score * POPULARITY_WEIGHT

    sorted_candidates = sorted(candidates, key=final_score, reverse=True)
    top10 = sorted_candidates[:10]
    return [{f: c[f] for f in raw_fields} for c in top10]


# ---------------------------------------------------------------------------
# Anonymous user: graph-degree ranking
# ---------------------------------------------------------------------------


def _get_anonymous_graph_scores(
    G: nx.DiGraph,
    candidate_job_ids: set[int],
) -> dict[int, float]:
    """Score candidate jobs by their weighted in-degree in the graph.

    For each candidate job, sum up edge weights from all users who interacted
    with it. Jobs with more/stronger interactions from graph users score higher.

    This gives a "crowd wisdom" signal that's richer than raw view count because
    it weights applies (3) higher than views (1) and only counts authenticated
    user interactions.

    Args:
        G: The interaction graph.
        candidate_job_ids: Set of job IDs from Stage 2.

    Returns:
        Dict mapping job_id → graph_degree_score.
    """
    job_scores: dict[int, float] = {}

    for jid in candidate_job_ids:
        jnode = job_id(jid)
        if jnode not in G:
            continue

        # Sum all incoming edge weights (User → Job edges)
        total_weight = 0.0
        for pred in G.predecessors(jnode):
            total_weight += G.edges[pred, jnode].get("weight", 1)

        if total_weight > 0:
            job_scores[jid] = total_weight

    return job_scores


def graph_ranking_anonymous(candidates: list[dict]) -> list[dict]:
    """Rank candidates for anonymous users using graph-degree scoring.

    Blends the graph in-degree score (total weighted interactions from all
    users in the graph) with the popularity score from Stage 2.

    Formula:
        final_score = normalised_graph_degree × 0.7 + normalised_popularity × 0.3

    Falls back to pure popularity ranking if graph provides no signal.

    Args:
        candidates: List of candidate dicts from grabFromDatabase(), each
                    containing all 職缺.csv columns plus "score" (popularity).

    Returns:
        Top 10 candidates ranked by blended score, with "score" field stripped.
    """
    if not candidates:
        return []

    raw_fields = [k for k in candidates[0].keys() if k != "score"]

    # Build candidate job ID set
    candidate_job_ids: set[int] = set()
    for c in candidates:
        jid = c.get("職缺編號")
        if jid is not None:
            candidate_job_ids.add(int(jid))

    # Get graph-degree scores
    try:
        G = get_graph()
        graph_scores = _get_anonymous_graph_scores(G, candidate_job_ids)
    except Exception:
        graph_scores = {}

    # Fallback: no graph signal → popularity ranking
    if not graph_scores:
        sorted_candidates = sorted(
            candidates,
            key=lambda c: (c.get("score", 0.0), c.get("職缺最後修改時間", "")),
            reverse=True,
        )
        top10 = sorted_candidates[:10]
        return [{f: c[f] for f in raw_fields} for c in top10]

    # Normalise graph scores
    max_graph = max(graph_scores.values())
    if max_graph == 0:
        max_graph = 1.0

    # Normalise popularity scores
    max_pop = max((c.get("score", 0.0) for c in candidates), default=1.0)
    if max_pop == 0:
        max_pop = 1.0

    # Compute blended final score
    def final_score(c: dict) -> float:
        jid = c.get("職缺編號")
        g_score = graph_scores.get(int(jid), 0.0) / max_graph if jid else 0.0
        p_score = c.get("score", 0.0) / max_pop
        return g_score * GRAPH_WEIGHT + p_score * POPULARITY_WEIGHT

    sorted_candidates = sorted(candidates, key=final_score, reverse=True)
    top10 = sorted_candidates[:10]
    return [{f: c[f] for f in raw_fields} for c in top10]
