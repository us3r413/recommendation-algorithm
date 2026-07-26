"""
pipeline.py — Top-level entry point for the recommendation pipeline.

Chains the three pipeline stages:
  1. querytoRequirement (query parsing)
  2. grabFromDatabase (retrieval)
  3. ranking (ranking)
"""

from src.query_parser import querytoRequirement
from src.retriever import grabFromDatabase
from src.ranker import ranking


def recommend(
    query: str,
    talent_no: int,
    c0: list[str] | None = None,
    d0: list[str] | None = None,
) -> list[dict]:
    """Top-level entry point for the recommendation pipeline.

    Calls querytoRequirement, then grabFromDatabase, then ranking in sequence.
    Returns at most 10 job listing dicts (raw 職缺.csv columns, no score).

    Exceptions from any stage propagate to the caller without suppression
    (except the documented LLM fallback and cold-start fallback within each stage).

    Args:
        query: A free-text search string from the user.
        talent_no: The user's ID (0 for anonymous users).
        c0: Optional list of city code strings (e.g. ["100100", "100200"]).
            Resolved to city names and used as a city filter.
        d0: Optional list of job category code strings (e.g. ["160213", "120403"]).
            Resolved to 職務小類 names and used as a category filter.

    Returns:
        A list of at most 10 dicts, each representing a job listing.
    """
    tags = querytoRequirement(query)
    candidates = grabFromDatabase(tags, c0=c0, d0=d0)
    return ranking(candidates, talent_no)
