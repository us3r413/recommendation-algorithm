# Tech Stack

## Status

ETL / data preparation phase. Two ETL scripts exist and produce derived tables. Core pipeline modules (`querytoRequirement`, `grabFromDatabase`, `ranking`) are designed but not yet implemented. `src/` is empty.

## Stack

- **Language**: Python
- **Data format**: CSV (all datasets and derived tables are CSV)
- **Query engine**: DuckDB — used in `grabFromDatabase` for fast SQL filtering over ~1M job listings
- **LLM**: Used in `querytoRequirement` to parse natural language queries into structured tags; model TBD (OpenAI API or local Ollama)
- **Graph database**: Deferred — Neo4j / Amazon Neptune considered for Graph RAG but not adopted for current phase (competition timeline constraint)

## Key Function Signatures

```python
querytoRequirement(query: str) -> list[str]
    # LLM-powered: spell-check, abbreviation expansion (pt→兼職), returns tags list
    # e.g. "台北後端 pt工作" -> ["後端工程師", "兼職", "台北市"]

grabFromDatabase(tags: list[str]) -> list[dict]
    # Semantic expansion via 職務對照表.CodeAlike, then DuckDB SQL filter on 職缺.csv
    # JOINs 瀏覽次數.csv to attach popularity score to each candidate

ranking(candidates: list[dict], talent_no: int) -> list[dict]  # top 10
    # talentNo = 0  → sort by 瀏覽次數.score (popularity)
    # talentNo ≠ 0  → personalised score from userBehaviorFeature.csv
    #   final_score = personal_score × 0.7 + popularity_score × 0.3
    #   cold-start (total_events < 3) → fall back to popularity ranking
```

## Personalised Ranking Score

```python
personal_score = (
    location_match(candidate, feature)  × 0.4 +
    category_match(candidate, feature)  × 0.4 +
    salary_match(candidate, feature)    × 0.2
)
```
Feature source: `userBehaviorFeature.csv` — one row per authenticated user.

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

## Common Commands

```bash
pip install -r requirements.txt

# Regenerate derived tables (run in order)
python dataset/genViewCount.py
python dataset/userAnalysis.py
```
