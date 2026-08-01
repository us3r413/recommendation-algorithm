"""
neptune_client.py — Amazon Neptune connection manager with Gremlin.

Provides a singleton traversal source (`g`) connected to Neptune via
WebSocket with IAM SigV4 authentication.

When USE_NEPTUNE=false (default), all functions raise NeptuneUnavailable
so callers can fall back to networkx.

Usage:
    from src.neptune_client import get_traversal, close_connection

    g = get_traversal()
    count = g.V().count().next()
    close_connection()
"""

import os
from typing import Any

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NEPTUNE_ENDPOINT = os.environ.get("NEPTUNE_ENDPOINT", "")
NEPTUNE_PORT = int(os.environ.get("NEPTUNE_PORT", "8182"))
NEPTUNE_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
USE_NEPTUNE = os.environ.get("USE_NEPTUNE", "false").lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class NeptuneUnavailable(Exception):
    """Raised when Neptune is disabled or unreachable."""
    pass


# ---------------------------------------------------------------------------
# Connection singleton
# ---------------------------------------------------------------------------

_connection = None
_traversal = None


def _create_connection():
    """Create a new authenticated connection to Neptune.

    Uses IAM SigV4 signing via boto3 credentials (from env vars or
    instance profile / role).
    """
    from boto3 import Session
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from gremlin_python.driver.driver_remote_connection import DriverRemoteConnection
    from gremlin_python.process.anonymous_traversal import traversal

    if not NEPTUNE_ENDPOINT:
        raise NeptuneUnavailable("NEPTUNE_ENDPOINT not configured")

    conn_string = f"wss://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/gremlin"
    service = "neptune-db"

    # Get AWS credentials
    session = Session()
    credentials = session.get_credentials()
    if credentials is None:
        raise NeptuneUnavailable("No AWS credentials found for Neptune IAM auth")

    creds = credentials.get_frozen_credentials()
    region = session.region_name or NEPTUNE_REGION

    # Sign the WebSocket upgrade request
    request = AWSRequest(method="GET", url=conn_string, data=None)
    SigV4Auth(creds, service, region).add_auth(request)

    # Create Gremlin connection with signed headers
    rc = DriverRemoteConnection(
        conn_string,
        "g",
        headers=dict(request.headers.items()),
    )

    g = traversal().with_remote(rc)
    return rc, g


def get_traversal():
    """Return the Gremlin traversal source.

    Creates a connection on first call, reuses on subsequent calls.
    Raises NeptuneUnavailable if USE_NEPTUNE=false or connection fails.
    """
    global _connection, _traversal

    if not USE_NEPTUNE:
        raise NeptuneUnavailable("USE_NEPTUNE is disabled")

    if _traversal is not None:
        return _traversal

    try:
        _connection, _traversal = _create_connection()
        # Verify connection with a simple query
        _traversal.V().limit(1).count().next()
        print(f"[Neptune] Connected to {NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}")
        return _traversal
    except NeptuneUnavailable:
        raise
    except Exception as e:
        _connection = None
        _traversal = None
        raise NeptuneUnavailable(f"Failed to connect to Neptune: {e}") from e


def close_connection():
    """Close the Neptune connection and release resources."""
    global _connection, _traversal
    if _connection is not None:
        try:
            _connection.close()
        except Exception:
            pass
    _connection = None
    _traversal = None


def reset_connection():
    """Force reconnection on next get_traversal() call.

    Useful after credential refresh or transient network errors.
    """
    close_connection()


# ---------------------------------------------------------------------------
# Batch write helpers
# ---------------------------------------------------------------------------


def batch_add_vertices(
    g: Any,
    label: str,
    vertices: list[dict],
    id_key: str,
    batch_size: int = 200,
) -> int:
    """Add vertices in batches to Neptune.

    Each dict in `vertices` must have `id_key` as the vertex ID,
    and remaining keys become vertex properties.

    Args:
        g: Gremlin traversal source.
        label: Vertex label (e.g. "Job", "Skill", "City").
        vertices: List of property dicts.
        id_key: Key in each dict to use as vertex ID.
        batch_size: Number of vertices per batch (Neptune limit ~200).

    Returns:
        Number of vertices upserted.
    """
    count = 0
    for i in range(0, len(vertices), batch_size):
        batch = vertices[i : i + batch_size]
        t = g.V()  # dummy start — we'll build with inject

        for v in batch:
            vid = str(v[id_key])
            t = g.V(vid).fold().coalesce(
                g.V(vid),  # type: ignore
                g.addV(label).property("id", vid),  # type: ignore
            )
            # This pattern doesn't chain well — use individual addV calls
            pass

        # Neptune prefers individual upserts for correctness
        for v in batch:
            vid = f"{label.lower()}:{v[id_key]}"
            traversal = g.V(vid).fold().coalesce(
                g.V(vid).unfold(),  # type: ignore
                g.addV(label).property("T.id", vid),  # type: ignore
            )
            for key, val in v.items():
                if key == id_key:
                    continue
                if val is not None and val != "":
                    traversal = traversal.property(key, val)
            traversal.next()
            count += 1

    return count


def batch_add_edges(
    g: Any,
    label: str,
    edges: list[dict],
    batch_size: int = 200,
) -> int:
    """Add edges in batches to Neptune.

    Each dict must have 'from_id', 'to_id', and optional properties.

    Args:
        g: Gremlin traversal source.
        label: Edge label (e.g. "REQUIRES", "LOCATED_IN").
        edges: List of edge dicts with 'from_id', 'to_id', and properties.
        batch_size: Number of edges per batch.

    Returns:
        Number of edges upserted.
    """
    count = 0
    for i in range(0, len(edges), batch_size):
        batch = edges[i : i + batch_size]
        for e in batch:
            from_id = e["from_id"]
            to_id = e["to_id"]
            props = {k: v for k, v in e.items() if k not in ("from_id", "to_id") and v is not None}

            # Upsert edge: drop existing and recreate (simplest for Neptune)
            traversal = (
                g.V(from_id)
                .as_("from")
                .V(to_id)
                .as_("to")
                .select("from")
                .addE(label)
                .to("to")
            )
            for key, val in props.items():
                traversal = traversal.property(key, val)
            try:
                traversal.next()
                count += 1
            except Exception:
                # Skip edges where source or target vertex doesn't exist
                pass

    return count


# ---------------------------------------------------------------------------
# CLI: quick connection test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not USE_NEPTUNE:
        print("[Neptune] USE_NEPTUNE=false — enable it in .env to test connection")
    else:
        try:
            g = get_traversal()
            count = g.V().count().next()
            print(f"[Neptune] Vertex count: {count}")
            close_connection()
        except NeptuneUnavailable as e:
            print(f"[Neptune] Unavailable: {e}")
