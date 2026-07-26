# 詳細設計草稿（第二版）

---

## 衍生資料表：職缺熱門度評分

### 背景

原始的 `職缺瀏覽_20260601_20260607.csv` 僅為行為 log，並不包含彙整後的熱門度數據。需要另外建立一張衍生表來記錄每個職缺的熱門程度，以供排序模組使用。

### 採納方案：方案 A — 綜合分數欄位

```
score = 主動應徵次數 × weight_apply + 瀏覽次數 × weight_view
```

- **主動應徵** 代表求職意願強烈，賦予較高權重（建議 `weight_apply = 3`，`weight_view = 1`，初始值可再調整）
- 產出欄位：`職缺編號`, `瀏覽次數`, `主動應徵次數`, `score`
- 捨棄方案 B（分開儲存），因為單一 score 欄位對排序模組更簡潔

> **AI 建議：** 可考慮加入時間衰退因子（Time Decay），讓近期的瀏覽與應徵行為擁有更高的分數貢獻，避免舊熱門職缺長期霸榜。例如：`score = Σ e^(-λ·Δt) × event_weight`。初期可用簡單版本（所有資料等權），待資料量足夠後再引入衰退機制。

---

## 模組一：`querytoRequirement(query: str) -> dict`

### 功能

將使用者輸入的自然語言搜尋字串，透過 LLM 轉換成結構化的 JSON 格式，作為後續資料庫查詢的依據。

### 處理步驟

1. **拼字修正（Spell Check）**：修正常見錯字，例如「軟體工成師」→「軟體工程師」
2. **語意正規化**：展開縮寫與俗稱，例如 `pt` → 兼職、`ue` → 使用者體驗設計師
3. **結構化輸出**：LLM 將語意整理成 JSON，欄位對應職缺查詢條件

### 輸出格式範例

```json
{
  "keywords": ["軟體工程師", "後端"],
  "location": ["台北市", "新北市"],
  "job_type": "全職",
  "salary_min": null,
  "experience": null
}
```

> **AI 建議：** LLM prompt 應明確指定輸出 schema，並加入 few-shot 範例以提升穩定性。同時建議設置 fallback 機制：若 LLM 回傳格式不符，退回至關鍵字直接比對模式，避免整個查詢流程因 LLM 失敗而中斷。可選用輕量化 local model（如 Ollama + Llama3）來降低延遲與成本。

---

## 模組二：`grabFromDatabase(requirements: dict) -> list`

### 功能

根據 `querytoRequirement` 的輸出，從職缺主表中撈取符合條件的職缺清單。

### 處理步驟

1. **語意延伸（Semantic Expansion）**：
   - 職稱：「軟體工程師」↔「軟體設計師」、「工程師」↔「開發人員」
   - 工時：「兼職」= 「打工」= 「工讀」= 「part-time」
   - 可透過 `職務對照表.csv` 的 `CodeAlike` 欄位輔助展開
2. **條件篩選**：依 keywords、location、job_type 等欄位過濾 `職缺.csv`
3. **回傳結果**：返回符合條件的職缺 list，帶入下一步排序

> **AI 建議：** 語意延伸部分除了規則式對照表，也可引入 embedding 相似度搜尋（如 `sentence-transformers` + FAISS），對職缺的 `職務名稱` 和 `職務內容` 做向量化，讓語意接近但用詞不同的職缺也能被召回，提升 recall rate。Knowledge Graph（Neo4j / Amazon Neptune）則適合處理層級關係，例如「後端工程師」屬於「軟體工程」大類。

---

## 模組三：`ranking(candidates: list, talent_no: int) -> list[top 10]`

### 功能

對候選職缺進行排序，回傳最終推薦的前 10 名職缺（有序）。

### 邏輯分支

#### 未登入使用者（`talentNo = 0`）

- 直接以職缺熱門度評分表（`score`）排序
- 簡單、快速、無需個人化資料

#### 已登入使用者

採納 **方案 C**：先建立使用者行為模式 log，再快速篩選

**實作方式：**

1. 從 `userSearchLog`、`職缺瀏覽`、`主動應徵` 彙整該使用者的歷史行為
2. 提取行為特徵（偏好地點、職務類別、薪資區間等）
3. 以特徵對候選職缺進行加權評分後排序

| 方案 | 說明 | 優缺點 |
|------|------|--------|
| A | 不符合歷史偏好直接剃除 | 簡單但可能過度過濾，降低多樣性 |
| B | 全部保留，以 weight 分數排序 | 較完整，但需要調參 |
| **C（採納）** | 建立行為模式 log 快速篩選後再排序 | 兼顧效率與個人化，可漸進優化 |

> **AI 建議：** 方案 C 的行為模式 log 可考慮以 **Graph RAG** 實作（Neo4j / Amazon Neptune），將求職者節點與職缺節點之間的行為（瀏覽、應徵）建成邊關係。這樣不只能做個人化推薦，還能支援協同過濾（Collaborative Filtering）——「和你行為相似的人也應徵了這些職缺」。初期若資料量不足，可先用簡單的 cosine similarity 在特徵向量上做近鄰查詢，待使用者資料量增長後再遷移至 graph 架構。

---

## 整體流程

```
使用者輸入 query
      ↓
querytoRequirement()   ← LLM 語意解析、結構化
      ↓
grabFromDatabase()     ← 語意延伸、條件篩選
      ↓
ranking()              ← 熱門度排序 or 個人化排序
      ↓
回傳 Top 10 職缺
```

---

## 待決定事項

- [ ] LLM 選型（雲端 API vs. local model）
- [ ] 語意延伸的實作方式（規則 vs. embedding vs. Knowledge Graph）
- [ ] 熱門度權重 `weight_apply` 與 `weight_view` 的初始值
- [ ] 方案 C 行為模式 log 的儲存格式與更新頻率
- [ ] 是否需要 A/B 測試框架來驗證排序效果
