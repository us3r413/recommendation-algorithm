"""
graph_ranker.py — Ability-based graph ranking using Neptune (Gremlin) or networkx.

Two ranking modes:

1. Signed-in users (ability-based collaborative filtering):
     User → HAS_SKILL → Skills
     Skills ← REQUIRES ← Candidate Jobs (skill overlap scoring)
     + User → PREFERS_CITY match with Job → LOCATED_IN → City
     + Co-user signal: users with similar skills also interacted with these jobs

2. Anonymous users (query-skill matching):
     Query tags → matched Skills
     Skills ← REQUIRES ← Candidate Jobs (skill overlap scoring)
     + City match from query tags

Formula:
    graph_score = skill_overlap × 0.5 + city_match × 0.3 + co_user_signal × 0.2
    final_score = graph_score × 0.7 + normalised_popularity × 0.3

Falls back to popularity ranking if graph provides no signal.

Dual-target:
  - USE_NEPTUNE=true  → Gremlin traversal queries against Neptune
  - USE_NEPTUNE=false → networkx traversal (local fallback)
"""

import os
from collections import defaultdict

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

USE_NEPTUNE = os.environ.get("USE_NEPTUNE", "false").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Blending weights: graph vs popularity
GRAPH_WEIGHT = 0.7
POPULARITY_WEIGHT = 0.3

# Graph score component weights
SKILL_OVERLAP_WEIGHT = 0.5
CITY_MATCH_WEIGHT = 0.3
CO_USER_WEIGHT = 0.2

# Traversal limits
MAX_CO_USERS = 50
MAX_SEED_JOBS = 50


# ---------------------------------------------------------------------------
# NetworkX-based ranking (local fallback)
# ---------------------------------------------------------------------------


def _nx_skill_overlap_scores(
    G,
    user_skills: dict[str, float],
    candidate_job_ids: set[int],
) -> dict[int, float]:
    """Score candidates by skill overlap with user's skills (networkx).

    For each candidate job, compute:
      overlap = sum of user_skill_strength for each shared skill / total job skills

    Returns dict mapping job_id → normalised overlap score [0, 1].
    """
    from src.graph_builder import job_id, get_job_skills

    scores: dict[int, float] = {}
    if not user_skills:
        return scores

    for jid in candidate_job_ids:
        jnode = job_id(jid)
        if jnode not in G:
            continue
        job_skills = get_job_skills(G, jnode)
        if not job_skills:
            continue

        overlap_score = 0.0
        for skill in job_skills:
            if skill in user_skills:
                overlap_score += user_skills[skill]

        # Normalise by number of job skills
        scores[jid] = overlap_score / len(job_skills)

    return scores


def _nx_city_match_scores(
    G,
    user_cities: dict[str, float],
    candidates: list[dict],
) -> dict[int, float]:
    """Score candidates by city match with user's preferred cities (networkx).

    Returns dict mapping job_id → city match score [0, 1].
    """
    scores: dict[int, float] = {}
    if not user_cities:
        return scores

    for c in candidates:
        jid = c.get("職缺編號")
        if jid is None:
            continue
        job_city = c.get("工作城市", "")
        if job_city in user_cities:
            scores[int(jid)] = user_cities[job_city]

    return scores


def _nx_co_user_scores(
    G,
    talent_no: int,
    candidate_job_ids: set[int],
) -> dict[int, float]:
    """Score candidates by co-user collaborative filtering (networkx).

    Finds users who share skills with the target user, then checks
    which candidate jobs those co-users interacted with.

    Returns dict mapping job_id → co-user score (normalised).
    """
    from src.graph_builder import user_id, job_id, get_user_skills

    uid = user_id(talent_no)
    if uid not in G:
        return {}

    # Get target user's skills
    target_skills = set(get_user_skills(G, uid).keys())
    if not target_skills:
        return {}

    # Find co-users: other users who share at least one skill
    co_users: set[str] = set()
    from src.graph_builder import skill_id
    for skill in target_skills:
        snode = skill_id(skill)
        if snode not in G:
            continue
        for pred in G.predecessors(snode):
            edge_data = G.edges[pred, snode]
            if edge_data.get("edge_type") == "HAS_SKILL" and pred != uid:
                co_users.add(pred)
                if len(co_users) >= MAX_CO_USERS:
                    break
        if len(co_users) >= MAX_CO_USERS:
            break

    if not co_users:
        return {}

    # For each co-user, find which candidate jobs they interacted with
    job_scores: dict[int, float] = defaultdict(float)
    for co_uid in co_users:
        for successor in G.successors(co_uid):
            edge_data = G.edges[co_uid, successor]
            if edge_data.get("edge_type") in ("VIEWED", "APPLIED"):
                node_data = G.nodes.get(successor, {})
                jid_val = node_data.get("jobId")
                if jid_val and int(jid_val) in candidate_job_ids:
                    weight = edge_data.get("weight", 1)
                    job_scores[int(jid_val)] += weight

    return dict(job_scores)


def _nx_query_skill_scores(
    G,
    query_skills: list[str],
    candidate_job_ids: set[int],
) -> dict[int, float]:
    """Score candidates by overlap with query-derived skills (for anonymous users).

    For each candidate, count how many of the query skills it requires.
    Returns dict mapping job_id → score [0, 1].
    """
    from src.graph_builder import job_id, get_job_skills

    if not query_skills:
        return {}

    scores: dict[int, float] = {}
    query_skill_set = set(query_skills)

    for jid in candidate_job_ids:
        jnode = job_id(jid)
        if jnode not in G:
            continue
        job_skills = get_job_skills(G, jnode)
        if not job_skills:
            continue
        overlap = len(job_skills & query_skill_set)
        if overlap > 0:
            scores[jid] = overlap / len(query_skill_set)

    return scores


# ---------------------------------------------------------------------------
# Neptune/Gremlin-based ranking
# ---------------------------------------------------------------------------


def _filter_existing_vertex_ids(g, vertex_ids: list[str]) -> list[str]:
    """Filter a list of vertex IDs to only those that exist in Neptune.

    Queries Neptune in batches to avoid oversized requests.
    Returns only the IDs that actually exist as vertices.
    """
    from gremlin_python.process.graph_traversal import __ as AnonymousTraversal

    existing: list[str] = []
    batch_size = 200
    for i in range(0, len(vertex_ids), batch_size):
        batch = vertex_ids[i : i + batch_size]
        try:
            # Use hasId() which safely filters without throwing on missing IDs
            found = g.V().hasId(*batch).id_().toList()
            existing.extend(found)
        except Exception:
            # If batch query fails, try individual IDs
            for vid in batch:
                try:
                    if g.V(vid).hasNext():
                        existing.append(vid)
                except Exception:
                    pass
    return existing


def _gremlin_skill_overlap_scores(
    g,
    talent_no: int,
    candidate_job_ids: set[int],
) -> dict[int, float]:
    """Score candidates by skill overlap using Gremlin traversal.

    Traversal:
      User → HAS_SKILL → Skill ← REQUIRES ← Job (in candidates)
      Group by job, count overlapping skills.
    """
    from src.graph_builder import user_id

    uid = user_id(talent_no)

    # Check that the user vertex exists
    try:
        if not g.V(uid).hasNext().next():
            return {}
    except Exception:
        return {}

    candidate_ids = [f"job:{jid}" for jid in candidate_job_ids]
    # Filter to only job vertices that exist in Neptune
    candidate_ids = _filter_existing_vertex_ids(g, candidate_ids)
    if not candidate_ids:
        return {}

    try:
        # Get user's skills and find candidate jobs that require them
        results = (
            g.V(uid)
            .outE("HAS_SKILL").inV().as_("skill")
            .inE("REQUIRES").outV()
            .hasId(*candidate_ids)
            .group()
            .by("jobId")
            .by(g.select("skill").count())  # type: ignore
            .next()
        )

        if isinstance(results, dict):
            max_score = max(results.values()) if results else 1
            return {int(k): v / max_score for k, v in results.items()}
    except Exception:
        pass

    return {}


def _gremlin_co_user_scores(
    g,
    talent_no: int,
    candidate_job_ids: set[int],
) -> dict[int, float]:
    """Score candidates using co-user signal via Gremlin.

    Traversal:
      User → HAS_SKILL → Skill ← HAS_SKILL ← OtherUser → APPLIED/VIEWED → Job (in candidates)
    """
    from src.graph_builder import user_id

    uid = user_id(talent_no)

    # Check that the user vertex exists
    try:
        if not g.V(uid).hasNext().next():
            return {}
    except Exception:
        return {}

    candidate_ids = [f"job:{jid}" for jid in candidate_job_ids]
    # Filter to only job vertices that exist in Neptune
    candidate_ids = _filter_existing_vertex_ids(g, candidate_ids)
    if not candidate_ids:
        return {}

    try:
        results = (
            g.V(uid)
            .out("HAS_SKILL")
            .in_("HAS_SKILL")
            .where(g.P.neq(uid))  # type: ignore
            .limit(MAX_CO_USERS)
            .outE("APPLIED", "VIEWED")
            .inV()
            .hasId(*candidate_ids)
            .groupCount()
            .by("jobId")
            .next()
        )

        if isinstance(results, dict):
            return {int(k): float(v) for k, v in results.items()}
    except Exception:
        pass

    return {}


def _gremlin_query_skill_scores(
    g,
    query_skills: list[str],
    candidate_job_ids: set[int],
) -> dict[int, float]:
    """Score candidates by query skill overlap using Gremlin (anonymous users).

    Traversal:
      Skill (matching query) ← REQUIRES ← Job (in candidates)
      Group by job, count matching skills.
    """
    from src.graph_builder import skill_id

    skill_ids = [skill_id(s) for s in query_skills]
    # Filter to only skill vertices that exist
    skill_ids = _filter_existing_vertex_ids(g, skill_ids)
    if not skill_ids:
        return {}

    candidate_ids = [f"job:{jid}" for jid in candidate_job_ids]
    # Filter to only job vertices that exist in Neptune
    candidate_ids = _filter_existing_vertex_ids(g, candidate_ids)
    if not candidate_ids:
        return {}

    try:
        results = (
            g.V(*skill_ids)
            .inE("REQUIRES").outV()
            .hasId(*candidate_ids)
            .groupCount()
            .by("jobId")
            .next()
        )

        if isinstance(results, dict):
            max_score = max(results.values()) if results else 1
            return {int(k): v / max_score for k, v in results.items()}
    except Exception:
        pass

    return {}


# ---------------------------------------------------------------------------
# Public API: graph_ranking (signed-in users)
# ---------------------------------------------------------------------------


def graph_ranking(candidates: list[dict], talent_no: int) -> list[dict]:
    """Rank candidates using ability-based graph collaborative filtering.

    For signed-in users:
      graph_score = skill_overlap × 0.5 + city_match × 0.3 + co_user × 0.2
      final_score = graph_score × 0.7 + normalised_popularity × 0.3

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

    raw_fields = [k for k in candidates[0].keys() if k not in ("score", "relevance_hits")]

    # Build candidate lookup
    candidate_map: dict[int, dict] = {}
    for c in candidates:
        jid = c.get("職缺編號")
        if jid is not None:
            candidate_map[int(jid)] = c
    candidate_job_ids = set(candidate_map.keys())

    # Get graph scores based on backend
    skill_scores: dict[int, float] = {}
    city_scores: dict[int, float] = {}
    co_user_scores: dict[int, float] = {}

    if USE_NEPTUNE:
        try:
            from src.neptune_client import get_traversal, NeptuneUnavailable
            g = get_traversal()
            skill_scores = _gremlin_skill_overlap_scores(g, talent_no, candidate_job_ids)
            co_user_scores = _gremlin_co_user_scores(g, talent_no, candidate_job_ids)
            # City match: still done locally (simple dict lookup)
            from src.graph_builder import get_graph, user_id, get_user_preferred_cities
            G = get_graph()
            uid = user_id(talent_no)
            user_cities = get_user_preferred_cities(G, uid)
            city_scores = _nx_city_match_scores(G, user_cities, candidates)
        except Exception:
            # Fall through to networkx
            USE_NEPTUNE_FALLBACK = True
        else:
            USE_NEPTUNE_FALLBACK = False
    else:
        USE_NEPTUNE_FALLBACK = True

    if not USE_NEPTUNE or (USE_NEPTUNE and "USE_NEPTUNE_FALLBACK" in dir() and USE_NEPTUNE_FALLBACK):
        # NetworkX fallback
        try:
            from src.graph_builder import get_graph, user_id, get_user_skills, get_user_preferred_cities
            G = get_graph()
            uid = user_id(talent_no)

            user_skills = get_user_skills(G, uid)
            user_cities = get_user_preferred_cities(G, uid)

            skill_scores = _nx_skill_overlap_scores(G, user_skills, candidate_job_ids)
            city_scores = _nx_city_match_scores(G, user_cities, candidates)
            co_user_scores = _nx_co_user_scores(G, talent_no, candidate_job_ids)
        except Exception:
            skill_scores = {}
            city_scores = {}
            co_user_scores = {}

    # If no graph signal at all → pure popularity fallback
    if not skill_scores and not city_scores and not co_user_scores:
        sorted_candidates = sorted(
            candidates,
            key=lambda c: (c.get("relevance_hits", 0), c.get("score", 0.0), c.get("職缺最後修改時間", "")),
            reverse=True,
        )
        top10 = sorted_candidates[:10]
        return [{f: c[f] for f in raw_fields} for c in top10]

    # Normalise each component
    def _normalise(scores: dict[int, float]) -> dict[int, float]:
        if not scores:
            return {}
        max_val = max(scores.values())
        if max_val == 0:
            return {}
        return {k: v / max_val for k, v in scores.items()}

    norm_skill = _normalise(skill_scores)
    norm_city = _normalise(city_scores)
    norm_co_user = _normalise(co_user_scores)

    # Normalise popularity
    max_pop = max((c.get("score", 0.0) for c in candidates), default=1.0)
    if max_pop == 0:
        max_pop = 1.0

    # Compute blended final score
    def final_score(c: dict) -> tuple:
        jid = c.get("職缺編號")
        if jid is None:
            return (0, 0.0)
        jid = int(jid)

        g_skill = norm_skill.get(jid, 0.0)
        g_city = norm_city.get(jid, 0.0)
        g_co_user = norm_co_user.get(jid, 0.0)

        graph_score = (
            g_skill * SKILL_OVERLAP_WEIGHT
            + g_city * CITY_MATCH_WEIGHT
            + g_co_user * CO_USER_WEIGHT
        )

        pop_score = c.get("score", 0.0) / max_pop
        combined = graph_score * GRAPH_WEIGHT + pop_score * POPULARITY_WEIGHT

        # Primary sort: relevance_hits, secondary: combined score
        return (c.get("relevance_hits", 0), combined)

    sorted_candidates = sorted(candidates, key=final_score, reverse=True)
    top10 = sorted_candidates[:10]
    return [{f: c[f] for f in raw_fields} for c in top10]


# ---------------------------------------------------------------------------
# Public API: graph_ranking_anonymous (anonymous users)
# ---------------------------------------------------------------------------


def graph_ranking_anonymous(
    candidates: list[dict],
    query_skills: list[str] | None = None,
) -> list[dict]:
    """Rank candidates for anonymous users using query-skill graph matching.

    For anonymous users:
      graph_score = query_skill_overlap × 0.7 + city_match × 0.3
      final_score = graph_score × 0.7 + normalised_popularity × 0.3

    If no query_skills provided, falls back to pure popularity.

    Args:
        candidates: List of candidate dicts from grabFromDatabase().
        query_skills: Skills extracted from the query (from querytoRequirement tags).

    Returns:
        Top 10 candidates ranked by blended score.
    """
    if not candidates:
        return []

    raw_fields = [k for k in candidates[0].keys() if k not in ("score", "relevance_hits")]

    # If no skills to match on, use popularity
    if not query_skills:
        sorted_candidates = sorted(
            candidates,
            key=lambda c: (c.get("relevance_hits", 0), c.get("score", 0.0), c.get("職缺最後修改時間", "")),
            reverse=True,
        )
        top10 = sorted_candidates[:10]
        return [{f: c[f] for f in raw_fields} for c in top10]

    # Build candidate set
    candidate_job_ids: set[int] = set()
    for c in candidates:
        jid = c.get("職缺編號")
        if jid is not None:
            candidate_job_ids.add(int(jid))

    # Get query-skill overlap scores
    query_skill_scores: dict[int, float] = {}

    if USE_NEPTUNE:
        try:
            from src.neptune_client import get_traversal
            g = get_traversal()
            query_skill_scores = _gremlin_query_skill_scores(g, query_skills, candidate_job_ids)
        except Exception:
            pass

    if not query_skill_scores:
        # NetworkX fallback
        try:
            from src.graph_builder import get_graph
            G = get_graph()
            query_skill_scores = _nx_query_skill_scores(G, query_skills, candidate_job_ids)
        except Exception:
            pass

    # If no graph signal → popularity fallback
    if not query_skill_scores:
        sorted_candidates = sorted(
            candidates,
            key=lambda c: (c.get("relevance_hits", 0), c.get("score", 0.0), c.get("職缺最後修改時間", "")),
            reverse=True,
        )
        top10 = sorted_candidates[:10]
        return [{f: c[f] for f in raw_fields} for c in top10]

    # Normalise
    max_skill = max(query_skill_scores.values()) if query_skill_scores else 1.0
    if max_skill == 0:
        max_skill = 1.0

    max_pop = max((c.get("score", 0.0) for c in candidates), default=1.0)
    if max_pop == 0:
        max_pop = 1.0

    def final_score(c: dict) -> tuple:
        jid = c.get("職缺編號")
        if jid is None:
            return (0, 0.0)
        jid = int(jid)

        g_skill = query_skill_scores.get(jid, 0.0) / max_skill
        graph_score = g_skill  # For anonymous, skill overlap is the primary signal

        pop_score = c.get("score", 0.0) / max_pop
        combined = graph_score * GRAPH_WEIGHT + pop_score * POPULARITY_WEIGHT

        return (c.get("relevance_hits", 0), combined)

    sorted_candidates = sorted(candidates, key=final_score, reverse=True)
    top10 = sorted_candidates[:10]
    return [{f: c[f] for f in raw_fields} for c in top10]
