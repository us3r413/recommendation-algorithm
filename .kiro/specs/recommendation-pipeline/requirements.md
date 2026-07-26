# Requirements Document

## Introduction

本文件定義三階段職缺推薦管線（recommendation pipeline）的功能需求。該管線接收使用者輸入的搜尋字串與使用者編號，經語意解析、資料庫檢索、個人化排序三個階段，從約 100 萬筆職缺中回傳最符合需求的前 10 筆職缺。

The pipeline is implemented as three Python functions:
- `querytoRequirement(query: str) -> list[str]` — LLM-powered query parsing
- `grabFromDatabase(tags: list[str]) -> list[dict]` — DuckDB-based retrieval with popularity enrichment
- `ranking(candidates: list[dict], talent_no: int) -> list[dict]` — top-10 selection with anonymous and personalised paths

## Glossary

- **Pipeline**: The three-stage recommendation system composed of `querytoRequirement`, `grabFromDatabase`, and `ranking`.
- **Query**: A free-text string entered by a job seeker describing the job they are looking for.
- **Tag**: A normalised string token extracted from a Query. Examples: `"後端工程師"`, `"兼職"`, `"台北市"`, `"薪資>=35000"`.
- **Tag List**: A flat Python `list[str]` of Tags produced by `querytoRequirement` and consumed by `grabFromDatabase`.
- **Candidate**: A single job listing `dict` containing all fields from `職缺.csv` plus a `score` field populated from `瀏覽次數.csv`.
- **Candidate Set**: The `list[dict]` of Candidates returned by `grabFromDatabase` and consumed by `ranking`.
- **QueryParser**: The component implementing `querytoRequirement`.
- **Retriever**: The component implementing `grabFromDatabase`.
- **Ranker**: The component implementing `ranking`.
- **LLM**: A large language model accessed via the local Ollama Python SDK, used inside `querytoRequirement` for semantic parsing.
- **DuckDB**: An in-process SQL query engine used inside `grabFromDatabase` to filter `職缺.csv` at high speed.
- **Popularity Score** (`score`): The time-decayed weighted event score for a job listing, pre-computed by `genViewCount.py` and stored in `瀏覽次數.csv`. Formula: `Σ e^(-0.1 · Δt) × weight`, where apply weight = 3, view weight = 1, Δt = days before the most recent event in the dataset.
- **Personal Score**: A per-user relevance score computed at query time using features from `userBehaviorFeature.csv`. Formula: `location_match × 0.4 + category_match × 0.4 + salary_match × 0.2`.
- **Final Score**: The blended score used for personalised ranking. Formula: `personal_score × 0.7 + popularity_score × 0.3`.
- **Anonymous User**: A user with `talentNo = 0`; no personalisation is applied.
- **Authenticated User**: A user with `talentNo ≠ 0`; personalisation is applied unless the user is in Cold-Start state.
- **Cold-Start User**: An Authenticated User whose `total_events < 3` in `userBehaviorFeature.csv`; treated identically to an Anonymous User for ranking purposes.
- **Normal User**: An Authenticated User whose `total_events ≥ 3`; personalised Final Score is applied.
- **CodeAlike**: The `CodeAlike` column in `職務對照表.csv` containing comma-separated synonymous job titles used for semantic expansion.
- **Salary Tag**: A Tag encoding a minimum salary constraint, formatted as `"薪資>=<integer>"` (e.g. `"薪資>=35000"`).

---

## Requirements

### Requirement 1 — Query Parsing (`querytoRequirement`)

**User Story:** As a job seeker, I want my free-text search query to be understood semantically, so that I receive relevant job recommendations even when I use abbreviations, colloquial terms, or minor typos.

#### Acceptance Criteria

1. THE QueryParser SHALL accept a single UTF-8 string `query` as its only input parameter and return a `list[str]` as its only output.
2. WHEN the `query` contains a known abbreviation (e.g. `pt`, `ue`, `ui`), THE QueryParser SHALL expand the abbreviation to its full Chinese equivalent before passing the text to the LLM (e.g. `pt` → `兼職`, `ue` → `使用者體驗設計師`, `ui` → `介面設計師`).
3. WHEN the LLM returns a valid JSON array of strings, THE QueryParser SHALL return that array as the Tag List.
4. WHEN the LLM call fails or returns output that does not parse as a JSON array of strings, THE QueryParser SHALL retry the LLM call up to 3 times before falling back.
5. IF all LLM retries are exhausted without a valid response, THEN THE QueryParser SHALL return a Tag List derived from the original `query` by tokenising on whitespace, with abbreviation expansion applied.
6. THE QueryParser SHALL invoke the LLM via the `ollama` Python SDK with a prompt that instructs the model to output a JSON array of strings and nothing else.
7. WHEN a salary constraint is present in the `query` (e.g. "薪水 35k 以上"), THE QueryParser SHALL include a Salary Tag formatted as `"薪資>=<integer>"` in the Tag List (e.g. `"薪資>=35000"`).
8. THE QueryParser SHALL NOT include explanatory text, metadata keys, or nested objects in the returned Tag List — each element SHALL be a plain string.

---

### Requirement 2 — Semantic Expansion (`grabFromDatabase` — expansion phase)

**User Story:** As a job seeker, I want jobs with semantically equivalent titles to appear in my results, so that I do not miss relevant listings due to naming variations.

#### Acceptance Criteria

1. WHEN `grabFromDatabase` receives a Tag List, THE Retriever SHALL load `職務對照表.csv` and look up each Tag against `CodeNameA`, `CodeNameB`, `CodeNameC`, and `CodeAlike` columns.
2. WHEN a Tag matches a row in `職務對照表.csv`, THE Retriever SHALL expand that Tag to include all comma-separated terms in the corresponding `CodeAlike` field, adding them to the set of job-title search terms.
3. IF a Tag does not match any row in `職務對照表.csv`, THEN THE Retriever SHALL use the Tag as-is for the job-title filter without expansion.
4. THE Retriever SHALL deduplicate expanded job-title terms before constructing the SQL query.

---

### Requirement 3 — Job Listing Retrieval (`grabFromDatabase` — query phase)

**User Story:** As a job seeker, I want the system to quickly retrieve all matching job listings from the full dataset, so that the ranking stage has a relevant pool to work from.

#### Acceptance Criteria

1. THE Retriever SHALL use DuckDB to query `職缺.csv` directly via SQL for all filtering operations.
2. WHEN the Tag List contains one or more city terms (terms matching `工作城市` values in `職缺.csv`), THE Retriever SHALL filter job listings to those whose `工作城市` is in the set of city terms extracted from the Tag List.
3. WHEN the Tag List contains one or more Salary Tags, THE Retriever SHALL filter job listings to those whose `薪資下限` is greater than or equal to the integer value specified in the Salary Tag.
4. WHEN the Tag List contains one or more job-title terms (after semantic expansion), THE Retriever SHALL filter job listings where `職務名稱` contains at least one of the expanded job-title terms using case-insensitive substring matching.
5. THE Retriever SHALL perform a LEFT JOIN of the filtered job listings with `瀏覽次數.csv` on `職缺編號`, attaching the `score` field to each result row; job listings with no matching row in `瀏覽次數.csv` SHALL receive a `score` of `0.0`.
6. THE Retriever SHALL return the result as a `list[dict]` where each dict contains all columns from the matched `職缺.csv` row plus the `score` field.
7. WHILE the DuckDB query executes, THE Retriever SHALL NOT load the entire `職缺.csv` into Python memory — all filtering SHALL be performed within the DuckDB engine.

---

### Requirement 4 — Anonymous and Cold-Start Ranking (`ranking` — popularity path)

**User Story:** As an anonymous visitor or a new user with little history, I want to see the most popular matching jobs, so that I get useful results even without a personalised profile.

#### Acceptance Criteria

1. THE Ranker SHALL accept a Candidate Set `list[dict]` and an integer `talent_no` as its only input parameters and return a `list[dict]` of at most 10 items as its only output.
2. WHEN `talent_no` equals `0`, THE Ranker SHALL rank the Candidate Set by the `score` field in descending order and return the top 10 Candidates.
3. WHEN `talent_no` does not equal `0` and the corresponding row in `userBehaviorFeature.csv` has `is_cold_start` equal to `True`, THE Ranker SHALL rank the Candidate Set by the `score` field in descending order and return the top 10 Candidates.
4. IF `talent_no` does not equal `0` and no matching row exists in `userBehaviorFeature.csv`, THEN THE Ranker SHALL treat the user as a Cold-Start User and rank by popularity.
5. WHEN two or more Candidates have identical `score` values, THE Ranker SHALL break ties by `職缺最後修改時間` in descending order.
6. THE Ranker SHALL NOT include the `score` field or any internally computed score field in the returned list of dicts — output SHALL contain only the raw fields from `職缺.csv`.

---

### Requirement 5 — Personalised Ranking (`ranking` — personalised path)

**User Story:** As a logged-in user with sufficient history, I want my location, job category, and salary preferences to influence the ranking, so that the top results match my actual interests.

#### Acceptance Criteria

1. WHEN `talent_no` does not equal `0` and the corresponding row in `userBehaviorFeature.csv` has `is_cold_start` equal to `False`, THE Ranker SHALL compute a Personal Score and a Final Score for each Candidate.
2. THE Ranker SHALL compute `personal_score` using the formula: `location_match × 0.4 + category_match × 0.4 + salary_match × 0.2`, where each component is a real number in the range [0.0, 1.0].
3. THE Ranker SHALL compute `location_match` as `1.0` if the Candidate's `工作城市` matches any of the user's `preferred_city_1`, `preferred_city_2`, or `preferred_city_3` values from `userBehaviorFeature.csv`, and `0.0` otherwise.
4. THE Ranker SHALL compute `category_match` as `1.0` if the Candidate's `職務中類` matches any of the user's `preferred_category_1`, `preferred_category_2`, or `preferred_category_3` values from `userBehaviorFeature.csv`, and `0.0` otherwise.
5. WHEN the user's `salary_floor` in `userBehaviorFeature.csv` is not null, THE Ranker SHALL compute `salary_match` as `1.0` if the Candidate's `薪資下限` is greater than or equal to `salary_floor`, and `0.0` otherwise.
6. WHEN the user's `salary_floor` in `userBehaviorFeature.csv` is null, THE Ranker SHALL set `salary_match` to `0.5` for all Candidates.
7. THE Ranker SHALL compute `final_score` using the formula: `personal_score × 0.7 + popularity_score × 0.3`, where `popularity_score` is the Candidate's `score` field normalised to [0.0, 1.0] by dividing by the maximum `score` value in the Candidate Set.
8. WHEN the maximum `score` in the Candidate Set is `0.0`, THE Ranker SHALL set the normalised `popularity_score` to `0.0` for all Candidates.
9. THE Ranker SHALL rank the Candidate Set by `final_score` in descending order and return the top 10 Candidates.
10. THE Ranker SHALL NOT include the `score`, `personal_score`, `final_score`, or any other internally computed field in the returned list of dicts — output SHALL contain only the raw fields from `職缺.csv`.

---

### Requirement 6 — Pipeline Integration

**User Story:** As a developer integrating the pipeline, I want a single entry point that chains the three stages, so that I can call one function and receive the top 10 job listings.

#### Acceptance Criteria

1. THE Pipeline SHALL expose a top-level function `recommend(query: str, talent_no: int) -> list[dict]` that calls `querytoRequirement`, then `grabFromDatabase`, then `ranking` in sequence and returns the result of `ranking`.
2. WHEN `grabFromDatabase` returns an empty list, THE Pipeline SHALL pass the empty list to `ranking`, and `ranking` SHALL return an empty list.
3. WHEN `grabFromDatabase` returns fewer than 10 Candidates, THE Pipeline SHALL return all available Candidates ranked according to the applicable ranking path without padding.
4. THE Pipeline SHALL propagate exceptions raised by any stage to the caller without silently suppressing them, unless a documented fallback (LLM retry, cold-start fallback) applies.
