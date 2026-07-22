# Tech Stack

## Status

Early design/prototyping phase. No implementation code exists yet — only design documents and raw datasets. `requirements.txt` is currently empty.

## Planned / Likely Stack

- **Language**: Python (standard for data pipelines and ML workflows)
- **Data format**: CSV (all current datasets are CSV)
- **LLM integration**: Used in `querytoRequirement` to parse natural language queries into structured JSON
- **Graph database** (optional): Neo4j or Amazon Neptune for semantic expansion and user behavior pattern graphs (Graph RAG)

## Key Functions Being Designed

```
querytoRequirement(str) -> str (JSON)
    - LLM-powered query parsing, spell-check, semantic normalization

grabFromDatabase(str JSON) -> file log
    - Semantic expansion on category codes, job listing retrieval

ranking(file log) -> top 10 ranked
    - Popularity-based (anonymous) or history-based (logged-in user) ranking
```

## Popularity Score (瀏覽次數.csv)

Derived from `職缺瀏覽_20260601_20260607.csv` and `主動應徵_0601-0607.csv`:

- **Option A**: `score = 主動應徵 × weight + 瀏覽次數 × weight` (single score column)
- **Option B**: Store application count and view count as separate columns

## Common Commands

> No build/test commands are defined yet. Update this section once the project is initialized.

```bash
# Example — install dependencies once requirements.txt is populated
pip install -r requirements.txt
```
