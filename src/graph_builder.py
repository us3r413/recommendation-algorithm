"""
graph_builder.py — Build an ability-based knowledge graph.

Constructs a graph with vertex types: Job, Skill, City, Category, User
and edge types: REQUIRES, LOCATED_IN, IN_CATEGORY, VIEWED, APPLIED,
                PREFERS_CITY, HAS_SKILL

Dual-target:
  - USE_NEPTUNE=true  → ingest into Amazon Neptune via Gremlin
  - USE_NEPTUNE=false → build in-memory networkx graph (local fallback)

Data sources:
  - dataset/job_skills_cache.csv (from skill_extractor.py)
  - dataset/職缺.csv (job metadata: city, category)
  - dataset/userBehaviorEvents.csv (user-job interactions)
  - dataset/userBehaviorFeature.csv (user preferences)

Usage:
    # Build networkx graph locally (default):
    python -m src.graph_builder

    # Build and ingest into Neptune:
    USE_NEPTUNE=true python -m src.graph_builder

    # Force rebuild (ignore caches):
    python -m src.graph_builder --rebuild
"""

import os
import pickle
import time

import networkx as nx
import pandas as pd
from dotenv import load_dotenv

from src.skill_extractor import load_skills_cache

load_dotenv()

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------

JOBS_PATH = "dataset/職缺.csv"
EVENTS_PATH = "dataset/userBehaviorEvents.csv"
FEATURES_PATH = "dataset/userBehaviorFeature.csv"
GRAPH_CACHE_PATH = "dataset/ability_graph_cache.pkl"

USE_NEPTUNE = os.environ.get("USE_NEPTUNE", "false").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# ID helpers (for both networkx and Neptune vertex IDs)
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
# NetworkX graph construction (local fallback)
# ---------------------------------------------------------------------------

_graph_cache: nx.DiGraph | None = None


def get_graph() -> nx.DiGraph:
    """Return the cached ability knowledge graph (networkx).

    Load order:
      1. In-memory cache (instant)
      2. Pickle file on disk (~5-10s)
      3. Build from CSVs, then save to pickle

    The graph contains:
      - Job nodes: node_type="Job", jobId, title, city, categoryMid
      - Skill nodes: node_type="Skill", name
      - City nodes: node_type="City", name
      - Category nodes: node_type="Category", name
      - User nodes: node_type="User", talentNo
      - Edges: REQUIRES, LOCATED_IN, IN_CATEGORY, VIEWED, APPLIED,
               PREFERS_CITY, HAS_SKILL (with properties)
    """
    global _graph_cache
    if _graph_cache is not None:
        return _graph_cache

    if os.path.exists(GRAPH_CACHE_PATH):
        _graph_cache = _load_from_pickle()
        return _graph_cache

    _graph_cache = build_graph_networkx()
    _save_to_pickle(_graph_cache)
    return _graph_cache


def _load_from_pickle() -> nx.DiGraph:
    """Load the graph from the pickle cache file."""
    start = time.perf_counter()
    print(f"[GraphBuilder] Loading ability graph from cache ({GRAPH_CACHE_PATH})...")
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


def build_graph_networkx() -> nx.DiGraph:
    """Build the ability knowledge graph in networkx.

    Steps:
      1. Load job_skills_cache → create Skill nodes + REQUIRES edges
      2. Load 職缺.csv → create Job, City, Category nodes + LOCATED_IN, IN_CATEGORY edges
      3. Load userBehaviorEvents → create User nodes + VIEWED/APPLIED edges
      4. Load userBehaviorFeature → create PREFERS_CITY + HAS_SKILL edges

    Returns:
        A networkx DiGraph with the full ability knowledge graph.
    """
    start = time.perf_counter()
    G = nx.DiGraph()

    # --- Step 1: Skills cache → Skill nodes + REQUIRES edges ---
    print("[GraphBuilder] Loading skills cache...")
    skills_df = load_skills_cache()
    if skills_df.empty:
        print("  WARNING: No skills cache found. Run `python -m src.skill_extractor` first.")
        print("  Building graph without skill nodes...")
    else:
        print(f"  {len(skills_df):,} jobs with extracted skills")
        skill_set: set[str] = set()
        requires_edges = 0

        for _, row in skills_df.iterrows():
            jid = int(row["job_id"])
            skills_str = row["skills"]
            source = row["source"]
            confidence = 1.0 if source == "structured" else 0.8

            if pd.isna(skills_str) or not str(skills_str).strip():
                continue

            jnode = job_id(jid)
            for s in str(skills_str).split("|"):
                s = s.strip()
                if not s:
                    continue
                snode = skill_id(s)
                if s not in skill_set:
                    G.add_node(snode, node_type="Skill", name=s)
                    skill_set.add(s)
                G.add_edge(jnode, snode, edge_type="REQUIRES", confidence=confidence)
                requires_edges += 1

        print(f"  Skill nodes: {len(skill_set):,}")
        print(f"  REQUIRES edges: {requires_edges:,}")

    # --- Step 2: Job metadata → Job, City, Category nodes ---
    print("[GraphBuilder] Loading job metadata...")
    jobs = pd.read_csv(
        JOBS_PATH,
        usecols=["職缺編號", "職務名稱", "工作城市", "職務中類", "薪資下限"],
    )

    # Only include jobs that have interactions
    print("[GraphBuilder] Loading events for filtering...")
    events = pd.read_csv(EVENTS_PATH)
    events = events[events["talentNo"].notna() & (events["talentNo"] != 0)]
    # Only include events up to 2026-06-05 (6/06+ held out for evaluation)
    events["event_time"] = pd.to_datetime(events["event_time"], errors="coerce")
    events = events[events["event_time"].dt.date <= pd.Timestamp("2026-06-05").date()]
    event_job_ids = set(events["job_id"].dropna().astype(int).unique())
    jobs = jobs[jobs["職缺編號"].isin(event_job_ids)].copy()
    print(f"  {len(jobs):,} jobs with interactions")

    city_set: set[str] = set()
    category_set: set[str] = set()

    for _, row in jobs.iterrows():
        jid = int(row["職缺編號"])
        jnode = job_id(jid)
        title = str(row["職務名稱"]) if pd.notna(row["職務名稱"]) else ""
        city_val = str(row["工作城市"]).strip() if pd.notna(row["工作城市"]) else ""
        cat_val = str(row["職務中類"]).strip() if pd.notna(row["職務中類"]) else ""
        salary = pd.to_numeric(row["薪資下限"], errors="coerce") if pd.notna(row["薪資下限"]) else None

        # Add/update Job node
        G.add_node(
            jnode,
            node_type="Job",
            jobId=jid,
            title=title,
            city=city_val,
            categoryMid=cat_val,
            salaryLower=salary if salary and not pd.isna(salary) else 0,
        )

        # City node + LOCATED_IN edge
        if city_val:
            cnode = city_id(city_val)
            if city_val not in city_set:
                G.add_node(cnode, node_type="City", name=city_val)
                city_set.add(city_val)
            G.add_edge(jnode, cnode, edge_type="LOCATED_IN")

        # Category node + IN_CATEGORY edge
        if cat_val:
            catnode = category_id(cat_val)
            if cat_val not in category_set:
                G.add_node(catnode, node_type="Category", name=cat_val)
                category_set.add(cat_val)
            G.add_edge(jnode, catnode, edge_type="IN_CATEGORY")

    print(f"  City nodes: {len(city_set):,}")
    print(f"  Category nodes: {len(category_set):,}")

    # --- Step 3: User-job interactions → User nodes + VIEWED/APPLIED edges ---
    print("[GraphBuilder] Building user interaction edges...")
    weight_map = {"apply": 3, "view": 1}
    users_seen: set[int] = set()
    interaction_edges = 0

    # Aggregate per (user, job, event_type)
    edge_agg: dict[tuple[str, str, str], int] = {}

    for _, row in events.iterrows():
        talent_no = int(row["talentNo"])
        jid = int(row["job_id"])
        event_type = row["event_type"]
        w = weight_map.get(event_type, 1)

        uid = user_id(talent_no)
        jnode = job_id(jid)
        edge_label = "APPLIED" if event_type == "apply" else "VIEWED"

        users_seen.add(talent_no)
        key = (uid, jnode, edge_label)
        edge_agg[key] = edge_agg.get(key, 0) + w

    # Add User nodes
    for talent_no in users_seen:
        G.add_node(user_id(talent_no), node_type="User", talentNo=talent_no)

    # Add interaction edges
    for (uid, jnode, edge_label), weight in edge_agg.items():
        G.add_edge(uid, jnode, edge_type=edge_label, weight=weight)
        interaction_edges += 1

    print(f"  User nodes: {len(users_seen):,}")
    print(f"  Interaction edges: {interaction_edges:,}")

    # --- Step 4: User preferences → PREFERS_CITY + HAS_SKILL edges ---
    print("[GraphBuilder] Building user preference edges...")
    prefs_city_edges = 0
    has_skill_edges = 0

    if os.path.exists(FEATURES_PATH):
        features = pd.read_csv(FEATURES_PATH)
        strength_map = {1: 1.0, 2: 0.7, 3: 0.4}

        for _, feat in features.iterrows():
            talent_no = int(feat["talentNo"])
            uid = user_id(talent_no)

            if uid not in G:
                continue

            # PREFERS_CITY edges
            for rank in range(1, 4):
                col = f"preferred_city_{rank}"
                city_val = feat.get(col)
                if pd.notna(city_val) and str(city_val).strip():
                    cnode = city_id(str(city_val).strip())
                    if cnode in G:
                        G.add_edge(uid, cnode, edge_type="PREFERS_CITY", strength=strength_map[rank])
                        prefs_city_edges += 1

        # HAS_SKILL: infer from user's APPLIED jobs → those jobs' REQUIRES edges
        print("[GraphBuilder] Inferring user skills from applications...")
        for talent_no in users_seen:
            uid = user_id(talent_no)
            # Get jobs this user applied to
            applied_skills: dict[str, int] = {}
            applied_jobs = 0

            for successor in G.successors(uid):
                edge_data = G.edges[uid, successor]
                if edge_data.get("edge_type") == "APPLIED":
                    applied_jobs += 1
                    # Get skills of this job
                    for job_successor in G.successors(successor):
                        job_edge = G.edges[successor, job_successor]
                        if job_edge.get("edge_type") == "REQUIRES":
                            skill_name = G.nodes[job_successor].get("name", "")
                            if skill_name:
                                applied_skills[skill_name] = applied_skills.get(skill_name, 0) + 1

            # Create HAS_SKILL edges with strength = count / total_applied_jobs
            if applied_jobs > 0 and applied_skills:
                for s_name, count in applied_skills.items():
                    strength = count / applied_jobs
                    snode = skill_id(s_name)
                    if snode in G:
                        G.add_edge(uid, snode, edge_type="HAS_SKILL", strength=strength)
                        has_skill_edges += 1

    print(f"  PREFERS_CITY edges: {prefs_city_edges:,}")
    print(f"  HAS_SKILL edges: {has_skill_edges:,}")

    elapsed = time.perf_counter() - start
    print(f"\n[GraphBuilder] Ability graph built in {elapsed:.1f}s")
    print(f"  Total nodes: {G.number_of_nodes():,}")
    print(f"  Total edges: {G.number_of_edges():,}")

    return G


# ---------------------------------------------------------------------------
# Neptune ingestion
# ---------------------------------------------------------------------------


def build_graph_neptune() -> None:
    """Ingest the ability knowledge graph into Amazon Neptune.

    Reads the same data sources as build_graph_networkx() but writes
    vertices and edges to Neptune via Gremlin instead of networkx.

    Requires USE_NEPTUNE=true and valid NEPTUNE_ENDPOINT in .env.
    """
    from src.neptune_client import get_traversal, NeptuneUnavailable
    from gremlin_python.process.graph_traversal import __ as AnonymousTraversal
    from gremlin_python.process.traversal import T

    try:
        g = get_traversal()
    except NeptuneUnavailable as e:
        print(f"[GraphBuilder] Neptune unavailable: {e}")
        print("[GraphBuilder] Falling back to networkx...")
        build_graph_networkx()
        return

    start = time.perf_counter()
    print("[GraphBuilder] Ingesting ability graph into Neptune...")

    # --- Load data ---
    skills_df = load_skills_cache()
    jobs = pd.read_csv(
        JOBS_PATH,
        usecols=["職缺編號", "職務名稱", "工作城市", "職務中類", "薪資下限"],
    )
    events = pd.read_csv(EVENTS_PATH)
    events = events[events["talentNo"].notna() & (events["talentNo"] != 0)]
    # Only include events up to 2026-06-05 (6/06+ held out for evaluation)
    events["event_time"] = pd.to_datetime(events["event_time"], errors="coerce")
    events = events[events["event_time"].dt.date <= pd.Timestamp("2026-06-05").date()]
    event_job_ids = set(events["job_id"].dropna().astype(int).unique())
    jobs = jobs[jobs["職缺編號"].isin(event_job_ids)].copy()

    # --- Skill vertices ---
    print("[Neptune] Adding Skill vertices...")
    skill_set: set[str] = set()
    if not skills_df.empty:
        for _, row in skills_df.iterrows():
            skills_str = row["skills"]
            if pd.isna(skills_str):
                continue
            for s in str(skills_str).split("|"):
                s = s.strip()
                if s:
                    skill_set.add(s)

    for s in skill_set:
        sid = skill_id(s)
        g.V(sid).fold().coalesce(
            AnonymousTraversal.unfold(),
            AnonymousTraversal.addV("Skill").property(T.id, sid).property("name", s)
        ).next()
    print(f"  {len(skill_set):,} Skill vertices")

    # --- City vertices ---
    print("[Neptune] Adding City vertices...")
    city_set: set[str] = set()
    for val in jobs["工作城市"].dropna().unique():
        city_val = str(val).strip()
        if city_val:
            city_set.add(city_val)

    for c in city_set:
        cid = city_id(c)
        g.V(cid).fold().coalesce(
            AnonymousTraversal.unfold(),
            AnonymousTraversal.addV("City").property(T.id, cid).property("name", c)
        ).next()
    print(f"  {len(city_set):,} City vertices")

    # --- Category vertices ---
    print("[Neptune] Adding Category vertices...")
    category_set: set[str] = set()
    for val in jobs["職務中類"].dropna().unique():
        cat_val = str(val).strip()
        if cat_val:
            category_set.add(cat_val)

    for cat in category_set:
        catid = category_id(cat)
        g.V(catid).fold().coalesce(
            AnonymousTraversal.unfold(),
            AnonymousTraversal.addV("Category").property(T.id, catid).property("name", cat)
        ).next()
    print(f"  {len(category_set):,} Category vertices")

    # --- Job vertices + edges to Skill/City/Category ---
    print("[Neptune] Adding Job vertices and edges...")
    job_count = 0
    for _, row in jobs.iterrows():
        jid = int(row["職缺編號"])
        jnode = job_id(jid)
        title = str(row["職務名稱"]) if pd.notna(row["職務名稱"]) else ""
        city_val = str(row["工作城市"]).strip() if pd.notna(row["工作城市"]) else ""
        cat_val = str(row["職務中類"]).strip() if pd.notna(row["職務中類"]) else ""

        # Upsert Job vertex
        g.V(jnode).fold().coalesce(
            AnonymousTraversal.unfold(),
            AnonymousTraversal.addV("Job").property(T.id, jnode).property("jobId", jid).property("title", title).property("city", city_val).property("categoryMid", cat_val)
        ).next()

        # LOCATED_IN edge
        if city_val:
            cid = city_id(city_val)
            g.V(jnode).addE("LOCATED_IN").to(AnonymousTraversal.V(cid)).next()

        # IN_CATEGORY edge
        if cat_val:
            catid = category_id(cat_val)
            g.V(jnode).addE("IN_CATEGORY").to(AnonymousTraversal.V(catid)).next()

        job_count += 1
        if job_count % 1000 == 0:
            print(f"    Jobs processed: {job_count:,}")

    print(f"  {job_count:,} Job vertices")

    # --- REQUIRES edges ---
    print("[Neptune] Adding REQUIRES edges...")
    requires_count = 0
    skipped_requires = 0
    # Use the set of job IDs that were actually added as vertices (present in both events AND 職缺.csv)
    ingested_job_ids = set(jobs["職缺編號"].astype(int))
    if not skills_df.empty:
        for _, row in skills_df.iterrows():
            jid = int(row["job_id"])

            # Skip jobs that weren't added as vertices
            if jid not in ingested_job_ids:
                skipped_requires += 1
                continue

            skills_str = row["skills"]
            source = row["source"]
            confidence = 1.0 if source == "structured" else 0.8

            if pd.isna(skills_str):
                continue

            jnode = job_id(jid)
            for s in str(skills_str).split("|"):
                s = s.strip()
                if not s:
                    continue
                sid = skill_id(s)
                try:
                    g.V(jnode).addE("REQUIRES").to(AnonymousTraversal.V(sid)).property("confidence", confidence).next()
                    requires_count += 1
                except Exception:
                    pass

            if requires_count % 5000 == 0 and requires_count > 0:
                print(f"    REQUIRES edges: {requires_count:,}")

    if skipped_requires:
        print(f"  (skipped {skipped_requires:,} jobs not in graph)")

    print(f"  {requires_count:,} REQUIRES edges")

    # --- User vertices + interaction edges ---
    print("[Neptune] Adding User vertices and interaction edges...")
    weight_map = {"apply": 3, "view": 1}
    users_seen: set[int] = set()
    edge_agg: dict[tuple[str, str, str], int] = {}

    for _, row in events.iterrows():
        talent_no = int(row["talentNo"])
        jid = int(row["job_id"])
        event_type = row["event_type"]
        w = weight_map.get(event_type, 1)
        uid = user_id(talent_no)
        jnode = job_id(jid)
        edge_label = "APPLIED" if event_type == "apply" else "VIEWED"
        users_seen.add(talent_no)
        key = (uid, jnode, edge_label)
        edge_agg[key] = edge_agg.get(key, 0) + w

    # Add user vertices
    for talent_no in users_seen:
        uid = user_id(talent_no)
        g.V(uid).fold().coalesce(
            AnonymousTraversal.unfold(),
            AnonymousTraversal.addV("User").property(T.id, uid).property("talentNo", talent_no)
        ).next()
    print(f"  {len(users_seen):,} User vertices")

    # Add interaction edges (only to jobs that exist as vertices)
    interaction_count = 0
    skipped_interactions = 0
    # Reuse ingested_job_ids (jobs present in both events AND 職缺.csv)
    valid_job_nodes = {job_id(jid) for jid in ingested_job_ids}

    for (uid, jnode, edge_label), weight in edge_agg.items():
        if jnode not in valid_job_nodes:
            skipped_interactions += 1
            continue
        try:
            g.V(uid).addE(edge_label).to(AnonymousTraversal.V(jnode)).property("weight", weight).next()
            interaction_count += 1
        except Exception:
            pass
        if interaction_count % 10000 == 0 and interaction_count > 0:
            print(f"    Interaction edges: {interaction_count:,}")

    print(f"  {interaction_count:,} interaction edges")
    if skipped_interactions:
        print(f"  (skipped {skipped_interactions:,} edges to non-existent job vertices)")

    elapsed = time.perf_counter() - start
    print(f"\n[GraphBuilder] Neptune ingestion complete in {elapsed:.1f}s")


# ---------------------------------------------------------------------------
# Utility functions (used by graph_ranker)
# ---------------------------------------------------------------------------


def get_job_skills(G: nx.DiGraph, jnode: str) -> set[str]:
    """Get all skills required by a job node."""
    skills = set()
    for successor in G.successors(jnode):
        edge_data = G.edges[jnode, successor]
        if edge_data.get("edge_type") == "REQUIRES":
            name = G.nodes[successor].get("name", "")
            if name:
                skills.add(name)
    return skills


def get_user_skills(G: nx.DiGraph, uid: str) -> dict[str, float]:
    """Get all skills a user has (with strength)."""
    skills = {}
    for successor in G.successors(uid):
        edge_data = G.edges[uid, successor]
        if edge_data.get("edge_type") == "HAS_SKILL":
            name = G.nodes[successor].get("name", "")
            if name:
                skills[name] = edge_data.get("strength", 0.5)
    return skills


def get_user_preferred_cities(G: nx.DiGraph, uid: str) -> dict[str, float]:
    """Get user's preferred cities (with strength)."""
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
    # Predecessors of skill node with REQUIRES edge
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
    import sys

    rebuild = "--rebuild" in sys.argv

    if rebuild:
        if os.path.exists(GRAPH_CACHE_PATH):
            print(f"[GraphBuilder] Removing old cache...")
            os.remove(GRAPH_CACHE_PATH)

    if USE_NEPTUNE:
        print("[GraphBuilder] Target: Amazon Neptune")
        build_graph_neptune()
    else:
        print("[GraphBuilder] Target: NetworkX (local)")
        G = get_graph()
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

        # Count edges by type
        edge_type_counts: dict[str, int] = {}
        for _, _, data in G.edges(data=True):
            et = data.get("edge_type", "Unknown")
            edge_type_counts[et] = edge_type_counts.get(et, 0) + 1

        print(f"  Edge types:")
        for et, count in sorted(edge_type_counts.items()):
            print(f"    {et}: {count:,}")
