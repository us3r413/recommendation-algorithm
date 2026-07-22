# Product Overview

A job recommendation algorithm that returns the top 10 most relevant job listings for a given user search query.

## Core Goal

Given a user's search query (and optionally their login history), surface and rank the 10 best-matching job listings from the database.

## Three-Stage Pipeline

1. **Semantic Analysis** — Parse and normalize the search query: spell-check, expand abbreviations (e.g. `pt` → 兼職), and broaden semantically equivalent terms (e.g. 軟體設計 ↔ 軟體工程師). A knowledge graph (e.g. Neo4j, Amazon Neptune) may be used for semantic expansion.

2. **Retrieval** — Query the job listings database using the normalized requirements (output as JSON). Expand category codes to include synonyms and related terms before filtering.

3. **Ranking** — Order results by relevance:
   - **Anonymous user** (`talentNo = 0`): rank by job popularity (view count + application count with weights).
   - **Logged-in user**: incorporate past application/view history to personalize ranking; optionally use a behavior pattern graph or LLM for scoring.

## Target Users

Job seekers searching for positions on a job platform. Users may be anonymous or authenticated.
