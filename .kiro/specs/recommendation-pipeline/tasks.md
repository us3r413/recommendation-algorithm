# Implementation Plan: recommendation-pipeline

## Overview

依序實作三階段職缺推薦管線：QueryParser → Retriever → Ranker，最後以 `pipeline.py` 串接三個模組。每個模組獨立可測試，共用工具函式放在 `utils/` 底下。

## Tasks

- [x] 1. 建立專案結構與共用工具模組
  - 在 `src/` 下建立 `utils/` 目錄
  - 建立 `src/utils/abbreviations.py`，定義 `ABBREVIATION_MAP` 與 `abbreviation_expand(query: str) -> str`
  - 建立 `src/utils/tag_parser.py`，定義 `classify_tags(tags: list[str]) -> dict` 與 `SALARY_TAG_RE`，以及 `_load_known_cities()` 的 stub（回傳空集合，稍後在 retriever 中補完）
  - 建立空的 `src/__init__.py`（若需要）
  - _Requirements: 1.1, 1.2, 2.1, 3.2, 3.3_

  - [x] 1.1 實作 `abbreviation_expand`
    - 依照 `ABBREVIATION_MAP` 做 case-insensitive token 替換
    - _Requirements: 1.2_

  - [x] 1.2 為 `abbreviation_expand` 撰寫 property test
    - **Property 2: Abbreviations are always expanded in the output**
    - **Validates: Requirements 1.2, 1.5**
    - 使用 `hypothesis` 生成含已知縮寫 token 的字串，驗證展開結果一定包含對應中文詞

  - [x] 1.3 實作 `classify_tags`
    - 解析 city、salary、job_terms 三類標籤
    - _Requirements: 2.1, 3.2, 3.3_

- [x] 2. 實作 QueryParser (`src/query_parser.py`)
  - 依照設計文件的 `querytoRequirement` 規格實作，包含 LLM 呼叫、3 次重試、schema 驗證與 fallback
  - _Requirements: 1.1–1.8_

  - [x] 2.1 實作 `querytoRequirement` 核心邏輯
    - 呼叫 `abbreviation_expand`，再透過 `ollama` SDK 呼叫 LLM
    - 以 `json.loads` + `isinstance` 做 schema 驗證
    - _Requirements: 1.1, 1.3, 1.6, 1.8_

  - [x] 2.2 實作重試與 fallback 邏輯
    - 失敗最多重試 3 次；全部失敗後回傳 `abbreviation_expand(query).split()`
    - _Requirements: 1.4, 1.5_

  - [x] 2.3 為 `querytoRequirement` 撰寫 property test（Property 1）
    - **Property 1: QueryParser always returns a list of plain strings**
    - **Validates: Requirements 1.1, 1.8**
    - 使用 `hypothesis` 對任意 UTF-8 字串驗證回傳值型別

  - [x] 2.4 為 fallback 撰寫 property test（Property 5）
    - **Property 5: Fallback tokenisation preserves abbreviation expansion**
    - **Validates: Requirements 1.5**
    - Mock LLM 全部失敗，驗證結果等於 `abbreviation_expand(query).split()`

  - [x] 2.5 為有效 LLM 回應撰寫 property test（Property 3）
    - **Property 3: Valid LLM JSON is returned as-is**
    - **Validates: Requirements 1.3**
    - Mock LLM 回傳有效 JSON 陣列，驗證結果與 LLM 輸出完全一致

  - [x] 2.6 為薪資標籤格式撰寫 property test（Property 4）
    - **Property 4: Salary tags are correctly formatted**
    - **Validates: Requirements 1.7**
    - 針對含薪資數字的查詢，驗證結果中恰好一個標籤符合 `^薪資>=\d+$`

- [x] 3. Checkpoint — QueryParser 完成
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. 實作 Retriever (`src/retriever.py`)
  - 依照設計文件實作 `grabFromDatabase`，包含語意展開、DuckDB SQL 過濾與 LEFT JOIN
  - _Requirements: 2.1–2.4, 3.1–3.7_

  - [x] 4.1 實作 `_load_known_cities` 與 `_load_job_lookup`（含快取）
    - 從 `dataset/城市對照表.csv` 讀取城市集合；從 `dataset/職務對照表.csv` 讀取 pandas DataFrame
    - 補完 `src/utils/tag_parser.py` 中的 `_load_known_cities` stub
    - _Requirements: 2.1, 3.2_

  - [x] 4.2 實作 `semantic_expand`
    - 對 job_terms 查詢 CodeNameA/B/C 與 CodeAlike，回傳去重後的展開詞彙集
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 4.3 為 `semantic_expand` 撰寫 property test（Property 6 & 7 & 8）
    - **Property 6: Matched tags are expanded with all CodeAlike terms**
    - **Property 7: Unmatched tags pass through unchanged**
    - **Property 8: Expanded job-title terms are deduplicated**
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4**

  - [x] 4.4 實作 `grabFromDatabase` — DuckDB 查詢與 LEFT JOIN
    - 依 classified 結果動態組合 WHERE 子句（參數化查詢）
    - LEFT JOIN `瀏覽次數.csv`，COALESCE score 為 0.0
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [x] 4.5 為城市過濾撰寫 property test（Property 9）
    - **Property 9: City filter is applied correctly**
    - **Validates: Requirements 3.2**
    - 使用 hypothesis 生成含城市標籤的 tag list，驗證結果中每筆的 `工作城市` 都在城市集合內

  - [x] 4.6 為薪資過濾撰寫 property test（Property 10）
    - **Property 10: Salary filter is applied correctly**
    - **Validates: Requirements 3.3**

  - [x] 4.7 為職務名稱過濾撰寫 property test（Property 11）
    - **Property 11: Job-title filter uses case-insensitive substring matching**
    - **Validates: Requirements 3.4**

  - [x] 4.8 為 score 欄位撰寫 property test（Property 12 & 13）
    - **Property 12: Every candidate has a score field, unmatched listings get 0.0**
    - **Property 13: Candidates contain all 職缺.csv columns plus score**
    - **Validates: Requirements 3.5, 3.6**

- [x] 5. Checkpoint — Retriever 完成
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. 實作 Ranker (`src/ranker.py`)
  - 依照設計文件實作 `ranking`，包含匿名/冷啟動路徑與個人化排序路徑
  - _Requirements: 4.1–4.6, 5.1–5.10_

  - [x] 6.1 實作 `_get_user_feature` 與 feature table 快取
    - 懶加載 `userBehaviorFeature.csv`，按 talentNo 查詢
    - _Requirements: 4.3, 4.4, 5.1_

  - [x] 6.2 實作 `_popularity_rank`
    - 以 `(score, 職缺最後修改時間)` 雙鍵降序排列，回傳前 10 筆（stripped of score）
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [x] 6.3 為匿名/冷啟動排序撰寫 property test（Property 14）
    - **Property 14: Anonymous and cold-start users are ranked by popularity**
    - **Validates: Requirements 4.2, 4.3, 4.4**

  - [x] 6.4 實作 `_compute_personal_score`
    - 依 location_match × 0.4 + category_match × 0.4 + salary_match × 0.2 計算
    - 處理 `salary_floor` 為 null（→ 0.5）與 NaN（pandas）
    - _Requirements: 5.2, 5.3, 5.4, 5.5, 5.6_

  - [x] 6.5 為 personal score 公式撰寫 property test（Property 15）
    - **Property 15: Personal score formula is computed correctly**
    - **Validates: Requirements 5.2, 5.3, 5.4, 5.5, 5.6**

  - [x] 6.6 實作 `_personalised_rank`
    - 正規化 popularity score、計算 final_score，降序排列，回傳前 10 筆（stripped of score）
    - _Requirements: 5.7, 5.8, 5.9, 5.10_

  - [x] 6.7 為 final score 公式撰寫 property test（Property 16）
    - **Property 16: Final score formula is applied with normalised popularity**
    - **Validates: Requirements 5.7, 5.8**

  - [x] 6.8 實作 `ranking` routing 邏輯
    - talent_no == 0 → popularity path；feature 不存在或 is_cold_start → popularity path；否則 personalised path
    - 空 candidates 直接回傳 `[]`
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

  - [x] 6.9 為輸出長度撰寫 property test（Property 17）
    - **Property 17: Output length is min(len(candidates), 10)**
    - **Validates: Requirements 4.1, 6.3**

  - [x] 6.10 為無洩漏計算欄位撰寫 property test（Property 18）
    - **Property 18: No computed fields leak into ranking output**
    - **Validates: Requirements 4.6, 5.10**

  - [x] 6.11 為個人化排序順序撰寫 property test（Property 19）
    - **Property 19: Normal users are ranked by final_score descending**
    - **Validates: Requirements 5.9**

- [x] 7. Checkpoint — Ranker 完成
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. 串接管線 (`src/pipeline.py`)
  - 實作 `recommend(query: str, talent_no: int) -> list[dict]`，依序呼叫三個模組
  - _Requirements: 6.1, 6.2, 6.3, 6.4_

  - [x] 8.1 實作 `recommend` entry point
    - `tags = querytoRequirement(query)` → `candidates = grabFromDatabase(tags)` → `return ranking(candidates, talent_no)`
    - 不攔截任何例外（除文件化的 fallback 外）
    - _Requirements: 6.1, 6.4_

  - [x] 8.2 撰寫整合測試
    - 以小型 mock CSV（職缺.csv、瀏覽次數.csv、userBehaviorFeature.csv）測試完整 `recommend` 呼叫
    - 驗證回傳筆數 ≤ 10、無計算欄位洩漏、空 candidates 回傳 `[]`
    - _Requirements: 6.1, 6.2, 6.3, 6.4_

- [x] 9. Final Checkpoint — 完整管線
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- 標示 `*` 的子任務為選填，可跳過以加快 MVP 開發速度
- 每個任務都對應具體需求條款，方便追溯
- Property test 使用 `hypothesis` 函式庫；需確認已列入 `requirements.txt`
- Checkpoint 任務確保每個階段逐步驗證，不累積未測試的變動
- 所有 CSV 路徑預設相對於執行時的工作目錄（專案根目錄）

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.3"] },
    { "id": 1, "tasks": ["1.2", "2.1", "4.1"] },
    { "id": 2, "tasks": ["2.2", "4.2"] },
    { "id": 3, "tasks": ["2.3", "2.4", "2.5", "2.6", "4.3", "4.4", "6.1"] },
    { "id": 4, "tasks": ["4.5", "4.6", "4.7", "4.8", "6.2", "6.4"] },
    { "id": 5, "tasks": ["6.3", "6.5", "6.6"] },
    { "id": 6, "tasks": ["6.7", "6.8"] },
    { "id": 7, "tasks": ["6.9", "6.10", "6.11", "8.1"] },
    { "id": 8, "tasks": ["8.2"] }
  ]
}
```
