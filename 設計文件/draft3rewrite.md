# 詳細設計草稿（第三版）

---

## 系統規模概述

```
職缺總資料量：~1,000,000 筆
         ↓  大量過濾
最終推薦輸出：10 筆
```

資料量龐大，查詢效能是核心挑戰。本版採用 **DuckDB** 作為本地查詢引擎來加速 CSV 過濾，取代 pandas 逐行掃描的方式。

> **AI 建議：** DuckDB 對百萬列 CSV 的 aggregation 與 filter 查詢有顯著優勢，且無需啟動獨立服務（in-process）。建議在 `grabFromDatabase` 和熱門度評分計算兩個環節都使用 DuckDB SQL 語法，可將查詢時間從秒級降至毫秒級。若未來需要多人並發，可遷移至 MotherDuck（DuckDB 的雲端版本）而不需改寫查詢邏輯。

---

## 衍生資料表：職缺熱門度評分（`瀏覽次數.csv`）

### 來源

由 `職缺瀏覽_20260601_20260607.csv` 與 `主動應徵_0601-0607.csv` 彙整而成。原始職缺主表不含瀏覽次數，需另行計算。

### 採納方案：方案 A + 時間衰退（Time Decay）

```
score = Σ [ e^(-λ · Δt) × event_weight ]

event_weight：
  主動應徵  = 3
  職缺瀏覽  = 1
```

- `Δt`：事件距今的天數
- `λ`：衰退速率（建議初始值 `λ = 0.1`，即約 7 天後分數衰退至約 50%）
- 加入 time decay 可避免舊熱門職缺長期霸榜，讓近期活躍的職缺獲得更高排名

### 輸出欄位

| 欄位 | 說明 |
|------|------|
| `職缺編號` | 主鍵 |
| `瀏覽次數` | 原始瀏覽計數 |
| `主動應徵次數` | 原始應徵計數 |
| `score` | 加權衰退後的綜合熱門度分數 |

> **AI 建議：** `λ` 值可根據實際職缺上架週期調整。若職缺平均有效期為 30 天，建議 `λ = 0.05`；若為 7 天短期職缺，則 `λ = 0.15`。比賽初期可固定 `λ = 0.1` 作為基準值，日後再透過離線評估（如 NDCG）調整。

---

## 模組一：`querytoRequirement(query: str) -> list[str]`

### 功能

將使用者輸入的自然語言搜尋字串，透過 LLM 轉換成**標籤（tags）清單**，作為後續資料庫查詢的索引鍵。

> 注意：本版輸出格式從 JSON dict 改為 tags list，讓 `grabFromDatabase` 的查詢邏輯更簡潔。

### 處理步驟

1. **拼字修正（Spell Check）**：修正常見錯字，例如「軟體工成師」→「軟體工程師」
2. **語意正規化**：展開縮寫與俗稱
   - `pt` → 兼職
   - `ue` / `ui` → 使用者體驗設計師 / 介面設計師
3. **標籤提取**：LLM 將語意整理成結構化 tags

### 輸出範例

```python
# 輸入
"台北後端 pt工作 薪水35k以上"

# 輸出
["後端工程師", "兼職", "台北市", "薪資>=35000"]
```

> **AI 建議：** Tags 的格式建議定義一套標準化的 tag schema（例如 `location:台北市`、`type:兼職`、`role:後端工程師`、`salary_min:35000`），讓 `grabFromDatabase` 可以直接 dispatch 對應的查詢條件而不需要額外解析。比賽環境下可用 structured output（`response_format={"type": "json_object"}`）強制 LLM 輸出固定 schema，減少 parsing 錯誤。

---

## 模組二：`grabFromDatabase(tags: list[str]) -> list[dict]`

### 功能

根據 tags 從職缺主表中撈取符合條件的候選職缺，並附上熱門度分數。

### 處理步驟

1. **語意延伸（Semantic Expansion）**：
   - 透過 `職務對照表.csv` 的 `CodeAlike` 欄位展開同義職稱
   - 例：`後端工程師` → `後端工程師`, `軟體工程師`, `伺服器工程師`
   - 例：`兼職` → `兼職`, `打工`, `工讀`, `part-time`
2. **DuckDB 查詢**：用展開後的條件對 `職缺.csv` 進行 SQL 過濾
3. **JOIN 熱門度分數**：將候選職缺與 `瀏覽次數.csv` 做 JOIN，補上 `score` 欄位
4. **回傳候選清單**：傳入 `ranking()` 進行最終排序

```sql
-- 範例 DuckDB 查詢
SELECT j.*, p.score
FROM '職缺.csv' j
LEFT JOIN '瀏覽次數.csv' p ON j.職缺編號 = p.職缺編號
WHERE j.工作城市 IN ('台北市', '新北市')
  AND j.工作類型 IN ('兼職', '打工', '工讀')
  AND j.職務名稱 ILIKE ANY(['%後端%', '%軟體工程%', '%伺服器%'])
```

> **AI 建議：** 若百萬筆資料下 ILIKE 仍偏慢，可預先對 `職缺.csv` 的 `職務名稱` 和 `工作內容` 欄位建立 full-text index（DuckDB 支援 FTS extension）。另外，對常用的 city/job-type 欄位建立 DuckDB persistent index 可進一步提速。

---

## 模組三：`ranking(candidates: list[dict], talent_no: int) -> list[dict]`

### 功能

對候選職缺排序，回傳前 10 名（有序）。

### 邏輯分支

#### 未登入使用者（`talentNo = 0`）

直接以 `瀏覽次數.csv` 的 `score` 欄位降冪排序，取前 10。

#### 已登入使用者 — 採納方案 C（行為模式快速篩選）

> 本版決定**暫不使用 Graph（Neo4j 等）**，因比賽時程內難以完整實作。改採輕量的行為模式特徵評分。

**實作步驟：**

1. 從 `userSearchLog`、`職缺瀏覽`、`主動應徵` 彙整使用者歷史行為
2. 提取行為偏好特徵：

   | 特徵 | 說明 |
   |------|------|
   | `preferred_locations` | 過去應徵或瀏覽最多的地區 |
   | `preferred_categories` | 過去應徵或瀏覽最多的職務類別 |
   | `salary_floor` | 過去應徵職缺的薪資中位數 |

3. 對每個候選職缺依特徵相似度打分數：

   ```python
   personal_score = (
       location_match_score × 0.4 +
       category_match_score × 0.4 +
       salary_match_score   × 0.2
   )
   ```

4. 最終分數 = `personal_score × α + popularity_score × (1 - α)`（建議 `α = 0.7`）
5. 降冪排序，取前 10

> **AI 建議：** 特徵權重（`α = 0.7`、`location 0.4` 等）是超參數，比賽初期可固定不調。若後續有離線評估資料，可用 grid search 找最佳組合。另外，新用戶（歷史行為少於 3 筆）建議 fallback 到純熱門度排序，避免因資料不足導致個人化分數失準——這個冷啟動問題（Cold Start Problem）在推薦系統中很常見，需要明確處理。

---

## 整體流程

```
使用者輸入 query
      ↓
querytoRequirement()   ← LLM 語意解析 → tags list
      ↓
grabFromDatabase()     ← 語意延伸 + DuckDB SQL 過濾 + JOIN 熱門度
      ↓
ranking()              ← 熱門度排序（未登入）or 個人化評分（已登入）
      ↓
回傳 Top 10 職缺
```

---

## 技術選型摘要

| 元件 | 選型 | 理由 |
|------|------|------|
| 查詢引擎 | DuckDB | 百萬列 CSV 毫秒級查詢，in-process 無需額外服務 |
| LLM | 待定（建議 OpenAI / Ollama） | structured output 支援較完善 |
| 語意延伸 | 規則式（`職務對照表` CodeAlike） | 比賽時程內可快速實作，不依賴外部服務 |
| 個人化排序 | 特徵相似度打分數 | 輕量可解釋，不需 Graph 或 ML 模型 |
| Graph RAG | 暫不採用 | 比賽環境下 implement 成本過高 |

---

## 待決定事項

- [ ] LLM 選型及 tag schema 格式定義
- [ ] Time decay 衰退速率 `λ` 的初始值
- [ ] 個人化排序的混合權重 `α` 初始值
- [ ] 冷啟動（歷史行為 < 3 筆）的 fallback 策略
- [ ] DuckDB FTS extension 是否需要在比賽環境中啟用
