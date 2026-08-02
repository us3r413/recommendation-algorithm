"""
graph_builder.py — Build the interaction graph directly from raw CSV logs.

Based on eval/build_graph_train.py. Reads raw behaviour logs (職缺瀏覽, 主動應徵)
directly instead of the derived userBehaviorEvents.csv, giving explicit control
over the date window used for graph construction.

Graph structure:
  - User nodes: node_type="User", talentNo
  - Job nodes:  node_type="Job", job_id, city, category_mid, salary_lower
  - Edges:      user→job with aggregated weight (view=1, apply=3)

Usage:
    # Build graph locally (uses pickle cache):
    python -m src.graph_builder

    # Force rebuild:
    python -m src.graph_builder --rebuild
"""

from __future__ import annotations

import csv
import os
import pickle
import sys
import time

import networkx as nx
from dotenv import load_dotenv

load_dotenv()

try:
    csv.field_size_limit(sys.maxsize)
except OverflowError:
    csv.field_size_limit(2**31 - 1)

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------

DATASET = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dataset")
VIEW_CSV = os.path.join(DATASET, "職缺瀏覽_20260601_20260607.csv")
APPLY_CSV = os.path.join(DATASET, "主動應徵_0601-0607.csv")
JOBS_CSV = os.path.join(DATASET, "職缺.csv")
GRAPH_CACHE_PATH = os.path.join(DATASET, "graph_cache.pkl")

# Date window for graph construction (inclusive)
GRAPH_SINCE = os.environ.get("GRAPH_SINCE", "2026-06-01")
GRAPH_UNTIL = os.environ.get("GRAPH_UNTIL", "2026-06-07")

WEIGHT_VIEW = 1
WEIGHT_APPLY = 3


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------


def user_id(talent_no: int) -> str:
    """Create a unique node ID for a user."""
    return f"user:{talent_no}"


def job_id(jid: int) -> str:
    """Create a unique node ID for a job."""
    return f"job:{jid}"


def skill_id(name: str) -> str:
    """Create a unique node ID for a skill."""
    return f"skill:{name}"


def city_id(name: str) -> str:
    """Create a unique node ID for a city."""
    return f"city:{name}"


def category_id(name: str) -> str:
    """Create a unique node ID for a category."""
    return f"category:{name}"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

_graph_cache: nx.DiGraph | None = None


def get_graph(rebuild: bool = False) -> nx.DiGraph:
    """Return the cached interaction graph.

    Load order:
      1. In-memory cache (instant)
      2. Pickle file on disk
      3. Build from raw CSVs, then save to pickle
    """
    global _graph_cache
    if _graph_cache is not None and not rebuild:
        return _graph_cache

    if os.path.exists(GRAPH_CACHE_PATH) and not rebuild:
        _graph_cache = _load_from_pickle()
        return _graph_cache

    _graph_cache = build_graph()
    _save_to_pickle(_graph_cache)
    return _graph_cache


def _load_from_pickle() -> nx.DiGraph:
    """Load the graph from the pickle cache file."""
    start = time.perf_counter()
    print(f"[GraphBuilder] Loading graph from cache ({GRAPH_CACHE_PATH})...")
    with open(GRAPH_CACHE_PATH, "rb") as f:
        G = pickle.load(f)
    elapsed = time.perf_counter() - start
    print(f"[GraphBuilder] Graph loaded in {elapsed:.1f}s")
    print(f"  Nodes: {G.number_of_nodes():,}, Edges: {G.number_of_edges():,}")
    return G


def _save_to_pickle(G: nx.DiGraph) -> None:
    """Save the graph to a pickle cache file."""
    start = time.perf_counter()
    print(f"[GraphBuilder] Saving graph to cache ({GRAPH_CACHE_PATH})...")
    with open(GRAPH_CACHE_PATH, "wb") as f:
        pickle.dump(G, f, protocol=pickle.HIGHEST_PROTOCOL)
    elapsed = time.perf_counter() - start
    size_mb = os.path.getsize(GRAPH_CACHE_PATH) / (1024 * 1024)
    print(f"[GraphBuilder] Cache saved in {elapsed:.1f}s ({size_mb:.1f} MB)")


def build_graph(since: str | None = None, until: str | None = None) -> nx.DiGraph:
    """Build the interaction graph from raw CSV logs.

    Reads 職缺瀏覽 and 主動應徵 directly, filtering by date window.
    Then enriches Job nodes with metadata from 職缺.csv.

    Args:
        since: Start date (inclusive), defaults to GRAPH_SINCE env var.
        until: End date (inclusive), defaults to GRAPH_UNTIL env var.

    Returns:
        A networkx DiGraph with User and Job nodes + weighted interaction edges.
    """
    since = since or GRAPH_SINCE
    until = until or GRAPH_UNTIL

    start = time.perf_counter()
    print(f"[GraphBuilder] Building interaction graph [{since} .. {until}]")

    # --- 1. Aggregate edge weights from raw logs ---------------------------
    edge_w: dict[tuple[int, int], int] = {}
    users: set[int] = set()
    jobs_seen: set[int] = set()

    for name, path, job_col, time_col, weight in [
        ("view", VIEW_CSV, "employeeNo", "dateIn", WEIGHT_VIEW),
        ("apply", APPLY_CSV, "empNo", "datein", WEIGHT_APPLY),
    ]:
        t0 = time.time()
        total = kept = 0
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                total += 1
                day = (row.get(time_col) or "")[:10]
                if not (since <= day <= until):
                    continue
                talent_raw = (row.get("talentNo") or "").strip()
                if not talent_raw or talent_raw == "0":
                    continue  # anonymous — not a stable identity
                try:
                    talent_no = int(talent_raw)
                    job_id_val = int(float(row.get(job_col) or 0))
                except (ValueError, TypeError):
                    continue
                if job_id_val <= 0:
                    continue
                users.add(talent_no)
                jobs_seen.add(job_id_val)
                key = (talent_no, job_id_val)
                edge_w[key] = edge_w.get(key, 0) + weight
                kept += 1
        print(f"  {name}: {total:,} rows -> {kept:,} in window ({time.time()-t0:.0f}s)")

    print(f"  aggregated {len(edge_w):,} unique (user, job) edges")

    # --- 2. Job metadata for the nodes that actually appear -----------------
    t0 = time.time()
    meta: dict[int, tuple[str, str, float | None]] = {}
    with open(JOBS_CSV, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            try:
                jid = int(row["職缺編號"])
            except (ValueError, KeyError, TypeError):
                continue
            if jid in jobs_seen:
                city = (row.get("工作城市") or "").strip()
                cat = (row.get("職務中類") or "").strip()
                try:
                    salary = float(row.get("薪資下限") or 0) or None
                except (ValueError, TypeError):
                    salary = None
                meta[jid] = (city, cat, salary)
    print(f"  job metadata for {len(meta):,} jobs ({time.time()-t0:.0f}s)")

    # --- 3. Assemble the graph ---------------------------------------------
    t0 = time.time()
    G = nx.DiGraph()

    for jid in jobs_seen:
        city, cat, salary = meta.get(jid, ("", "", None))
        G.add_node(
            f"job:{jid}",
            node_type="Job",
            job_id=jid,
            jobId=jid,
            city=city,
            category_mid=cat,
            categoryMid=cat,
            salaryLower=salary if salary else 0,
        )

    for talent_no in users:
        G.add_node(f"user:{talent_no}", node_type="User", talentNo=talent_no)

    for (talent_no, jid), w in edge_w.items():
        edge_type = "APPLIED" if w >= WEIGHT_APPLY else "VIEWED"
        G.add_edge(f"user:{talent_no}", f"job:{jid}", weight=w, edge_type=edge_type)

    print(f"  graph assembled ({time.time()-t0:.0f}s)")

    elapsed = time.perf_counter() - start
    print(f"\n[GraphBuilder] Graph built in {elapsed:.1f}s")
    print(f"  nodes {G.number_of_nodes():,}  (users {len(users):,}, jobs {len(jobs_seen):,})")
    print(f"  edges {G.number_of_edges():,}")

    return G


# ---------------------------------------------------------------------------
# Utility functions (used by graph_ranker)
# ---------------------------------------------------------------------------


def get_job_skills(G: nx.DiGraph, jnode: str) -> set[str]:
    """Get all skills required by a job node.

    Looks for outgoing REQUIRES edges from the job node.
    Returns empty set if no skill nodes are present in this graph.
    """
    skills = set()
    for successor in G.successors(jnode):
        edge_data = G.edges[jnode, successor]
        if edge_data.get("edge_type") == "REQUIRES":
            name = G.nodes[successor].get("name", "")
            if name:
                skills.add(name)
    return skills


def get_user_skills(G: nx.DiGraph, uid: str) -> dict[str, float]:
    """Get all skills a user has (with strength).

    Looks for outgoing HAS_SKILL edges from the user node.
    Returns empty dict if no skill edges are present.
    """
    skills = {}
    for successor in G.successors(uid):
        edge_data = G.edges[uid, successor]
        if edge_data.get("edge_type") == "HAS_SKILL":
            name = G.nodes[successor].get("name", "")
            if name:
                skills[name] = edge_data.get("strength", 0.5)
    return skills


def get_user_preferred_cities(G: nx.DiGraph, uid: str) -> dict[str, float]:
    """Get user's preferred cities (with strength).

    Looks for outgoing PREFERS_CITY edges from the user node.
    Returns empty dict if no preference edges are present.
    """
    cities = {}
    for successor in G.successors(uid):
        edge_data = G.edges[uid, successor]
        if edge_data.get("edge_type") == "PREFERS_CITY":
            name = G.nodes[successor].get("name", "")
            if name:
                cities[name] = edge_data.get("strength", 0.5)
    return cities


def get_jobs_requiring_skill(G: nx.DiGraph, skill_name: str) -> set[str]:
    """Get all job node IDs that require a given skill."""
    snode = skill_id(skill_name)
    if snode not in G:
        return set()
    jobs = set()
    for pred in G.predecessors(snode):
        edge_data = G.edges[pred, snode]
        if edge_data.get("edge_type") == "REQUIRES":
            jobs.add(pred)
    return jobs


# ---------------------------------------------------------------------------
# CLI: build (or rebuild) and report stats
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    rebuild = "--rebuild" in sys.argv

    if rebuild and os.path.exists(GRAPH_CACHE_PATH):
        print(f"[GraphBuilder] Removing old cache...")
        os.remove(GRAPH_CACHE_PATH)

    G = get_graph(rebuild=rebuild)

    print(f"\nGraph stats:")
    print(f"  Total nodes: {G.number_of_nodes():,}")
    print(f"  Total edges: {G.number_of_edges():,}")

    # Count by type
    type_counts: dict[str, int] = {}
    for _, data in G.nodes(data=True):
        nt = data.get("node_type", "Unknown")
        type_counts[nt] = type_counts.get(nt, 0) + 1

    for nt, count in sorted(type_counts.items()):
        print(f"    {nt}: {count:,}")
