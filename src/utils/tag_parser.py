"""
tag_parser.py — Tag classification helpers for the recommendation pipeline.

Classifies a flat list of tags (produced by querytoRequirement) into four
categories:
  - cities:     tags that match known city names from 城市對照表.csv
  - salary_min: the integer floor from the first "薪資>=<N>" tag, or None
  - job_types:  tags matching 職缺屬性 values (全職/兼職/工讀/中高階/其他)
  - job_terms:  everything else (job titles, work types, etc.)

The `_load_known_cities()` function is intentionally a stub that returns an
empty set.  retriever.py will override / populate this once it loads
城市對照表.csv — the stub keeps this module free of I/O so it can be imported
and tested in isolation.
"""

import re

# Matches tags of the form "薪資>=35000", capturing the integer part.
SALARY_TAG_RE = re.compile(r'^薪資>=(\d+)$')

# Known 職缺屬性 values
KNOWN_JOB_TYPES = {"全職", "兼職", "工讀", "中高階", "其他"}


def _load_known_cities() -> set:
    """Stub — returns an empty set.

    Will be completed in retriever.py, which loads 城市對照表.csv and
    populates the city lookup used by classify_tags().
    """
    return set()


def classify_tags(tags: list[str]) -> dict:
    """Classify a flat tag list into cities, salary_min, job_types, and job_terms.

    Args:
        tags: A list of normalised tag strings produced by querytoRequirement,
              e.g. ["後端工程師", "台北市", "薪資>=35000", "兼職"].

    Returns:
        A dict with four keys:
            "cities"     (list[str])  — tags that match known city names
            "salary_min" (int | None) — integer salary floor from the first
                                        Salary Tag, or None if none present
            "job_types"  (list[str])  — tags matching 職缺屬性 values
            "job_terms"  (list[str])  — remaining tags used as job-title search
                                        terms (before semantic expansion)

    Notes:
        - If multiple Salary Tags are present, only the last one wins (last
          match overwrites salary_min).  In practice querytoRequirement should
          emit at most one.
        - Tag order within each output list is preserved from the input.
        - The city lookup delegates to _load_known_cities(), which is a stub
          returning an empty set until retriever.py replaces it.
    """
    cities: list[str] = []
    job_terms: list[str] = []
    job_types: list[str] = []
    salary_min: int | None = None

    known_cities = _load_known_cities()  # cached set from 城市對照表.csv

    for tag in tags:
        m = SALARY_TAG_RE.match(tag)
        if m:
            salary_min = int(m.group(1))
        elif tag in known_cities:
            cities.append(tag)
        elif tag in KNOWN_JOB_TYPES:
            job_types.append(tag)
        else:
            job_terms.append(tag)

    return {"cities": cities, "salary_min": salary_min, "job_types": job_types, "job_terms": job_terms}
