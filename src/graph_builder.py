"""
graph_builder.py — Build an in-memory user-job interaction graph using networkx.

Reads userBehaviorEvents.csv and 職缺.csv, constructs a directed graph with:
  Vertices: User (talentNo), Job (job_id + metadata)
  Edges:    VIEWED (weight=1), APPLIED (weight=3)

The graph is built once and cached to disk as a pickle file for fast
subsequent loads (~seconds instead of ~minutes).

No external services or credentials required — runs entirely in-process.

Usage:
    # Pre-build the graph cache (run once after ETL):
    python -m src.graph_builder

    # Force rebuild (e.g. after new data):
    python -m src.graph_builder --rebuild
"""

import os
import pickle
import time

import networkx as nx
import pandas as pd

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------

EVENTS_PATH = "dataset/userBehaviorEvents.csv"
JOBS_PATH = "dataset/職缺.csv"
GRAPH_CACHE_PATH = "dataset/graph_cache.pkl"


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------


def user_id(talent_no: int) -> str:
    """Create a unique node ID for a user."""
    return f"user:{talent_no}"


def job_id(jid: int) -> str:
    """Create a unique node ID for a job."""
    return f"job:{jid}"


# ---------------------------------------------------------------------------
# Graph construction (cached singleton)
# ---------------------------------------------------------------------------

_graph_cache: nx.DiGraph | None = None


def get_graph() -> nx.DiGraph:
    """Return the cached interaction graph.

    Load order:
      1. In-memory cache (instant)
      2. Pickle file on disk (~5-10s)
      3. Build from CSVs (~4-5 min), then save to pickle for next time

    The graph is a directed graph where:
      - User nodes have attributes: node_type="User", talentNo (int)
      - Job nodes have attributes: node_type="Job", job_id (int), city (str), category_mid (str)
      - Edges go from User → Job with attributes: weight (int, summed)
    """
    global _graph_cache
    if _graph_cache is not None:
        return _graph_cache

    # Try loading from pickle cache
    if os.path.exists(GRAPH_CACHE_PATH):
        _graph_cache = _load_from_pickle()
        return _graph_cache

    # Build from scratch and save
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
    print(f"[GraphBuilder] Graph loaded from cache in {elapsed:.1f}s")
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


def build_graph() -> nx.DiGraph:
    """Build the interaction graph from CSV files.

    Returns:
        A networkx DiGraph with User and Job nodes connected by
        weighted interaction edges.
    """
    start = time.perf_counter()

    print("[GraphBuilder] Loading userBehaviorEvents.csv...")
    events = pd.read_csv(EVENTS_PATH)
    events = events[events["talentNo"].notna() & (events["talentNo"] != 0)]
    print(f"  {len(events):,} authenticated events")

    print("[GraphBuilder] Loading job metadata from 職缺.csv...")
    jobs = pd.read_csv(
        JOBS_PATH,
        usecols=["職缺編號", "工作城市", "職務中類", "薪資下限"],
    )
    jobs = jobs.rename(columns={
        "職缺編號": "job_id",
        "工作城市": "city",
        "職務中類": "category_mid",
        "薪資下限": "salary_lower",
    })
    jobs["salary_lower"] = pd.to_numeric(jobs["salary_lower"], errors="coerce")

    # Only include jobs that have at least one interaction
    event_job_ids = set(events["job_id"].dropna().astype(int).unique())
    jobs = jobs[jobs["job_id"].isin(event_job_ids)]

    G = nx.DiGraph()

    # Add job nodes
    print("[GraphBuilder] Adding job nodes...")
    for _, row in jobs.iterrows():
        jid = int(row["job_id"])
        G.add_node(
            job_id(jid),
            node_type="Job",
            job_id=jid,
            city=str(row["city"]) if pd.notna(row["city"]) else "",
            category_mid=str(row["category_mid"]) if pd.notna(row["category_mid"]) else "",
        )

    # Aggregate edge weights per (user, job) pair
    print("[GraphBuilder] Aggregating interaction edges...")
    weight_map = {"apply": 3, "view": 1}
    edge_weights: dict[tuple[str, str], int] = {}

    users_seen: set[int] = set()

    for _, row in events.iterrows():
        talent_no = int(row["talentNo"])
        jid = int(row["job_id"])
        w = weight_map.get(row["event_type"], 1)

        uid = user_id(talent_no)
        jnode = job_id(jid)

        users_seen.add(talent_no)

        key = (uid, jnode)
        edge_weights[key] = edge_weights.get(key, 0) + w

    # Add user nodes
    print("[GraphBuilder] Adding user nodes...")
    for talent_no in users_seen:
        G.add_node(user_id(talent_no), node_type="User", talentNo=talent_no)

    # Add edges
    print("[GraphBuilder] Adding edges...")
    for (uid, jnode), weight in edge_weights.items():
        G.add_edge(uid, jnode, weight=weight)

    elapsed = time.perf_counter() - start
    print(f"[GraphBuilder] Graph built in {elapsed:.1f}s")
    print(f"  Users: {len(users_seen):,}")
    print(f"  Jobs: {len(jobs):,}")
    print(f"  Edges: {len(edge_weights):,}")

    return G


# ---------------------------------------------------------------------------
# Utility: get reverse neighbors (jobs → users who interacted with them)
# ---------------------------------------------------------------------------


def get_users_who_interacted(G: nx.DiGraph, job_node: str) -> list[tuple[str, int]]:
    """Get all users who have an edge to this job node, with their edge weight.

    Returns list of (user_node_id, weight).
    """
    result = []
    for pred in G.predecessors(job_node):
        data = G.edges[pred, job_node]
        result.append((pred, data.get("weight", 1)))
    return result


# ---------------------------------------------------------------------------
# CLI: build (or rebuild) and report stats
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    rebuild = "--rebuild" in sys.argv

    if rebuild and os.path.exists(GRAPH_CACHE_PATH):
        print(f"[GraphBuilder] Removing old cache...")
        os.remove(GRAPH_CACHE_PATH)

    G = get_graph()
    print(f"\nGraph stats:")
    print(f"  Nodes: {G.number_of_nodes():,}")
    print(f"  Edges: {G.number_of_edges():,}")
    users = [n for n, d in G.nodes(data=True) if d.get("node_type") == "User"]
    jobs = [n for n, d in G.nodes(data=True) if d.get("node_type") == "Job"]
    print(f"  User nodes: {len(users):,}")
    print(f"  Job nodes: {len(jobs):,}")
