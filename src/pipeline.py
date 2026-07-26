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


def recommend(query: str, talent_no: int) -> list[dict]:
    """Top-level entry point for the recommendation pipeline.

    Calls querytoRequirement, then grabFromDatabase, then ranking in sequence.
    Returns at most 10 job listing dicts (raw 職缺.csv columns, no score).

    Exceptions from any stage propagate to the caller without suppression
    (except the documented LLM fallback and cold-start fallback within each stage).

    Args:
        query: A free-text search string from the user.
        talent_no: The user's ID (0 for anonymous users).

    Returns:
        A list of at most 10 dicts, each representing a job listing.
    """
    tags = querytoRequirement(query)
    candidates = grabFromDatabase(tags)
    return ranking(candidates, talent_no)
