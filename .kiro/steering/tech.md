# Tech Stack

## Status

Production-ready. All three pipeline stages implemented and deployed as a FastAPI service on AWS EC2. Graph RAG ranking (NetworkX local + Amazon Neptune remote) implemented as an optional enhancement. Evaluation framework with ablation tests completed.

## Stack

- **Language**: Python 3.11
- **Web framework**: FastAPI + Uvicorn
- **Data format**: CSV (all datasets and derived tables are CSV)
- **Query engine**: DuckDB — used in `grabFromDatabase` for fast SQL filtering over ~1M job listings
- **LLM**: AWS Bedrock (Claude Sonnet) — used in `querytoRequirement` for semantic parsing; model configurable via `BEDROCK_MODEL_ID` env var (default: `us.anthropic.claude-sonnet-4-6`)
- **Graph**: NetworkX (local, pickle-cached) for graph-based ranking; Amazon Neptune (Gremlin) as optional remote graph DB
- **Cloud**: AWS — EC2 (t3.large) for hosting, Bedrock for LLM, Neptune for graph (optional)
- **Containerisation**: Docker (python:3.11-slim base)
- **Infrastructure**: CloudFormation (EC2 + security group)

## Key Function Signatures

```python
querytoRequirement(query: str) -> list[str]
    # LLM-powered (Bedrock Claude): spell-check, abbreviation expansion (pt→兼職),
    # semantic expansion for category words (速食→麥當勞,肯德基,...), returns tags list
    # Fallback to rule-based tokenisation on LLM failure (3 retries)
    # Results cached via @lru_cache(maxsize=256)

grabFromDatabase(tags: list[str], c0: list[str] | None, d0: list[str] | None) -> list[dict]
    # Semantic expansion via 職務對照表.CodeAlike, then DuckDB SQL filter on 職缺.csv
    # JOINs 瀏覽次數.csv to attach popularity score to each candidate
    # c0/d0: optional city/category code filters (resolved to names internally)

ranking(candidates: list[dict], talent_no: int) -> list[dict]  # top 10
    # Routing:
    #   talentNo = 0 + GRAPH_FOR_ANONYMOUS=true → graph_ranking_anonymous()
    #   talentNo = 0 → popularity ranking (relevance_hits → score → recency)
    #   talentNo ≠ 0 + GRAPH_FOR_SIGNED_IN_USER=true → graph_ranking() (Neptune/NX)
    #   talentNo ≠ 0 + cold-start (total_events < 3) → popularity ranking
    #   talentNo ≠ 0 + normal → personalised score from userBehaviorFeature.csv

recommend(query: str, talent_no: int, c0: list[str]|None, d0: list[str]|None) -> list[dict]
    # Top-level pipeline: querytoRequirement → grabFromDatabase → ranking
```

## Personalised Ranking Score

```python
personal_score = (
    location_match(candidate, feature)  × 0.4 +
    category_match(candidate, feature)  × 0.4 +
    salary_match(candidate, feature)    × 0.2
)
final_score = personal_score × 0.7 + normalised_popularity × 0.3
```
Feature source: `userBehaviorFeature.csv` — one row per authenticated user.

## Graph RAG Ranking (optional)

Enabled via env vars `GRAPH_FOR_SIGNED_IN_USER=true` and/or `GRAPH_FOR_ANONYMOUS_USER=true`.

Graph model (NetworkX or Neptune):
- Vertices: User, Job, Skill, City, Category
- Edges: VIEWED, APPLIED, REQUIRES (job→skill), LOCATED_IN (job→city)
- Signals: skill overlap, city match, co-user collaborative filtering
- Pickle cache at `dataset/graph_cache.pkl` for fast local startup

## Popularity Score Formula (`瀏覽次數.csv`)

```
score = Σ  e^(-λ · Δt)  ×  event_weight
  λ = 0.1  (≈ 50 % decay after 7 days)
  event_weight: apply = 3, view = 1
  Δt: days before most recent event in dataset
```

## ETL Scripts

| Script | Output | Command |
|--------|--------|---------|
| `dataset/genViewCount.py` | `dataset/瀏覽次數.csv` | `python dataset/genViewCount.py` |
| `dataset/userAnalysis.py` | `dataset/userBehaviorFeature.csv`, `dataset/userBehaviorEvents.csv` | `python dataset/userAnalysis.py` |

## Dataset Scale

| Table | Rows |
|-------|------|
| 職缺.csv | ~1,000,000 |
| 職缺瀏覽 (raw) | ~8,467,232 (incl. anonymous) |
| 主動應徵 (raw) | included above |
| userBehaviorEvents.csv | 5,091,661 (authenticated only) |
| userBehaviorFeature.csv | 166,539 users (130,365 normal / 36,174 cold-start) |
| 瀏覽次數.csv | 288,319 job listings with at least one event |

## Environment Variables (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-6` | Bedrock model for query parsing |
| `GRAPH_FOR_SIGNED_IN_USER` | `false` | Enable graph ranking for authenticated users |
| `GRAPH_FOR_ANONYMOUS_USER` | `false` | Enable graph-degree ranking for anonymous users |
| `USE_NEPTUNE` | `false` | Use remote Neptune instead of local NetworkX |
| `NEPTUNE_ENDPOINT` | (empty) | Neptune cluster endpoint |
| `NEPTUNE_PORT` | `8182` | Neptune port |
| `AWS_DEFAULT_REGION` | `us-west-2` | AWS region for Neptune/Bedrock |

## Common Commands

```bash
pip install -r requirements.txt

# Regenerate derived tables (run in order)
python dataset/genViewCount.py
python dataset/userAnalysis.py

# Run API server locally
uvicorn app:app --host 0.0.0.0 --port 8000

# Run tests
pytest src/tests/ -v
pytest src/utils/ -v

# Docker build & run
docker build -t job-recommend .
docker run -p 8000:8000 job-recommend

# Deploy to EC2 (PowerShell)
.\infra\deploy-to-ec2.ps1
```
