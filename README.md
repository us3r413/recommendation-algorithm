# 職缺推薦演算法

根據使用者的搜尋查詢（及登入狀態），從約 120 萬筆職缺中推薦最相關的 10 筆職缺。

---

## 決賽交付對照表

| # | 命題交付項目 | 位置 |
|---|---|---|
| 1 | Live Demo（部署網址） | [`http://35.85.148.23:8000/docs`](http://35.85.148.23:8000/docs)（互動式測試頁） |
| 2 | 生成式 AI 技術應用說明（含 LLM 失敗模式與防護） | [生成式 AI 技術應用說明](#生成式-ai-技術應用說明) |
| 3 | 數據及資料應用說明 | [資料流程](#資料流程)、[資料集準備](#資料集準備) |
| 4 | 系統功能說明（graph schema + 遍歷 trace 範例） | [`設計文件/graph_schema_and_trace.md`](設計文件/graph_schema_and_trace.md) |
| 5 | AWS 部署架構 | [部署](#部署) |
| 6 | 原始碼 + README + 依賴鎖定 + ablation script | 本檔案、[`requirements.txt`](requirements.txt)、[`eval/`](eval/) |

| 命題對 README 之明訂要求 | 位置 |
|---|---|
| 環境設定 | [環境設定](#環境設定) |
| 執行範例 | [執行範例](#執行範例) |
| benchmark 重現步驟 | [Benchmark 重現步驟](#benchmark-重現步驟) |
| random seed 說明 | [Random Seed 與決定性](#random-seed-與決定性) |
| 資料／模型／索引版本說明 | [資料／模型／索引版本說明](#資料模型索引版本說明) |
| 依賴版本鎖定檔 | [`requirements.txt`](requirements.txt) |
| 「有圖譜 vs 無圖譜」ablation，一鍵重現 | [`bash eval/reproduce.sh`](#一鍵重現評審執行此指令即可) |
| **NDCG@10 / Hit@1 / Hit@10 同步報告** | [評測結果摘要](#評測結果摘要) |
| **是否採用 position bias correction** | [Position Bias Correction](#position-bias-correction)（**明確標註未採用**） |

---

## 目錄

- [決賽交付對照表](#決賽交付對照表)
- [系統架構](#系統架構)
- [API Endpoint](#api-endpoint)
- [生成式 AI 技術應用說明](#生成式-ai-技術應用說明)
- [能力知識圖譜](#能力知識圖譜)
- [資料流程](#資料流程)
- [環境設定](#環境設定)
- [資料集準備](#資料集準備)
- [部署](#部署)
- [執行範例](#執行範例)
- [Benchmark 重現步驟](#benchmark-重現步驟)
- [離線評測與 Ablation](#離線評測與-ablation)
- [Random Seed 與決定性](#random-seed-與決定性)
- [Position Bias Correction](#position-bias-correction)
- [資料／模型／索引版本說明](#資料模型索引版本說明)
- [專案結構](#專案結構)
- [設計文件](#設計文件)

---

## 系統架構

系統採用三階段 Pipeline + 能力知識圖譜設計。查詢引擎使用 DuckDB（in-process），圖譜以 **networkx 於程式內遍歷**。

> **關於 Amazon Neptune**：`src/neptune_client.py` 保留了 Neptune（Gremlin）連線實作，
> 但本次交付**未使用 Neptune** —— Neptune 叢集部署未完成，`USE_NEPTUNE` 預設為 `false`，
> 所有圖譜遍歷皆由 networkx 在應用程式行程內執行。README 中提及 Neptune 之處均為
> 「可選後端」，非本次實際運行架構。

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Recommendation Pipeline                       │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   使用者查詢 (ks/c0/d0) + talentNo                                  │
│        │                                                             │
│        ▼                                                             │
│   ┌──────────────────────────┐                                       │
│   │  querytoRequirement()    │  ← AWS Bedrock (Claude Sonnet 4)      │
│   │  • 縮寫展開 (rule-based) │  ← src/utils/abbreviations.py        │
│   │  • LLM 語意解析+語意擴展 │  ← 翻譯、品牌展開、同義詞           │
│   │  • LRU 快取 (256 筆)     │  ← 重複查詢 ~0ms                     │
│   │  • 後處理: 城市正規化     │                                       │
│   │  • Fallback: 空白分詞    │                                       │
│   └────────────┬─────────────┘                                       │
│                │ tags: list[str]                                      │
│                ▼                                                      │
│   ┌──────────────────────────┐   ┌────────────────────┐             │
│   │  grabFromDatabase()      │──▶│ jobs (in-memory)    │ DuckDB      │
│   │  • 標籤分類              │   └────────────────────┘             │
│   │  • ILIKE + 職務小類匹配  │   ┌────────────────────┐             │
│   │  • relevance_hits 計數   │──▶│ popularity          │ DuckDB      │
│   │  • c0/d0 代碼解析        │   └────────────────────┘             │
│   └────────────┬─────────────┘                                       │
│                │ candidates: list[dict] (with relevance_hits)         │
│                ▼                                                      │
│   ┌──────────────────────────┐   ┌────────────────────────────┐     │
│   │  ranking()               │──▶│ 能力知識圖譜 (networkx)     │     │
│   │  • relevance_hits 優先   │   │ 需 GRAPH_FOR_* = true       │     │
│   │  • 熱門度（時間衰退）    │   └────────────────────────────┘     │
│   │  ── 以下需啟用圖譜 ──    │   ┌────────────────────────────┐     │
│   │  • 技能重疊 / 城市偏好   │──▶│ userBehaviorFeature.csv     │     │
│   │  • 協同過濾（共同技能）  │   └────────────────────────────┘     │
│   └────────────┬─────────────┘                                       │
│                │                                                      │
│                ▼                                                      │
│          Top 10 職缺結果                                             │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 第一階段：語意解析（querytoRequirement）

- 縮寫展開：規則式替換（例如 `pt` → 兼職、`fe` → 前端工程師、`35k以上` → 薪資>=35000）
- 呼叫 AWS Bedrock Claude 將自然語言查詢解析為結構化標籤
- 語意擴展：模糊/類別性關鍵字由 LLM 展開為具體品牌名與同義詞
- 多語言支援：英文輸入自動翻譯為繁體中文
- LRU 快取（256 筆）：相同查詢第二次起跳過 LLM
- 若 LLM 回應格式錯誤，最多重試 3 次後 fallback 為空白分詞

### 第二階段：資料庫檢索（grabFromDatabase）

- 標籤分類：城市 / 薪資門檻 / 職缺屬性（全職/兼職）/ 職務關鍵字
- DuckDB 預載入記憶體：啟動時將 CSV 載入為 in-memory table，查詢延遲 <1s
- LEFT JOIN `瀏覽次數.csv` 為每筆候選職缺附加熱門分數
- relevance_hits 計數：統計每筆候選職缺匹配的標籤數量

### 第三階段：排序（ranking）

**預設路徑（`GRAPH_FOR_*=false`，即目前部署設定）**

```
排序鍵 = (relevance_hits, 熱門度分數, 職缺最後修改時間)  降冪
```

`relevance_hits` 為候選職缺命中的查詢標籤數，**相關性優先於熱門度**；熱門度為時間衰退加權
（見[資料流程](#資料流程)）。已登入且非冷啟動之使用者改走 `userBehaviorFeature.csv` 的個人化路徑。

**圖譜路徑（需將 `GRAPH_FOR_SIGNED_IN_USER` / `GRAPH_FOR_ANONYMOUS_USER` 設為 `true`）**

以 networkx 遍歷能力知識圖譜計分：

- **已登入用戶**：
  ```
  graph_score = skill_overlap × 0.5 + city_match × 0.3 + co_user_signal × 0.2
  final_score = graph_score × 0.7 + normalised_popularity × 0.3
  ```
  - `skill_overlap`：使用者技能與職缺所需技能的重疊度
  - `city_match`：使用者偏好城市與職缺所在城市的匹配
  - `co_user_signal`：擁有相似技能的其他使用者也應徵了該職缺

- **匿名用戶**：
  ```
  graph_score = query_skill_overlap × 0.7 + city_match × 0.3
  final_score = graph_score × 0.7 + normalised_popularity × 0.3
  ```

- **Fallback**：圖譜無信號時退回熱門度排序

> ⚠️ **已知問題（由本專案離線評測發現）**：`src/ranker.py` 呼叫
> `graph_ranking_anonymous(candidates)` 時未傳入 `query_skills` 參數，而該函式在
> `query_skills` 為空時會直接退回熱門度排序。因此**匿名查詢路徑上的技能圖譜實際從未被啟用**，
> 這也是 ablation 中「有圖譜」與「無圖譜」兩組指標完全相同（NDCG@10 皆為 0.0190）的直接原因。
> 詳見 [`eval/EVALUATION_REPORT.md`](eval/EVALUATION_REPORT.md)。

---

## API Endpoint

### Base URL

```
http://35.85.148.23:8000
```

### `POST /api/v1/jobs/search`

**Request Body (JSON):**

欄位比照 `userSearchLog` 搜尋紀錄格式，對應關係如下：

| 欄位 | 對應 `userSearchLog` | 類型 | 必填 | 說明 |
|------|---|------|------|------|
| `query` | `ks`（搜尋關鍵字） | string | 是 | 自然語言或關鍵字查詢 |
| `location_code` | `c0`（地區條件） | string[] | 否 | 城市代碼列表 |
| `duty_code` | `d0`（職務條件） | string[] | 否 | 職務類別代碼列表 |
| `request_id` | — | string | 否 | 追蹤 ID |

> 命題原文以 `ks` / `c0` / `d0` 描述輸入欄位，並載明「確切 API schema、JSON 格式與範例將於
> 工作坊提供」。本 endpoint 採用工作坊公告之欄位名稱（`query` / `location_code` / `duty_code`），
> 語意與 `ks` / `c0` / `d0` 一一對應。內部 `/recommend` endpoint 則直接接受 `c0` / `d0` 原名。

**Response:**

```json
{
  "request_id": "req_0001",
  "result": [
    { "job_id": "132045128", "rank": 1 },
    { "job_id": "132111693", "rank": 2 },
    { "job_id": "130274154", "rank": 3 }
  ]
}
```

`result` 依相關性由高至低排序，`rank` 自 1 起連續、不重複，`job_id` 不重複；
每次查詢至少回傳 Top 10 以供計算 NDCG@10 / Hit@1 / Hit@10；查無結果時回傳 `"result": []`。
命題所述之 `empStr`（排序後職缺編號清單）即為 `result` 中依 `rank` 排列之 `job_id` 序列。

**範例呼叫:**

```bash
curl -X POST http://35.85.148.23:8000/api/v1/jobs/search \
  -H "Content-Type: application/json" \
  -d '{"request_id": "req_0001", "query": "後端工程師", "location_code": ["100100"], "duty_code": ["140200"]}'
```

### `GET /health`

```json
{"status": "ok"}
```

---

## 生成式 AI 技術應用說明

### LLM 在系統中的兩個位置

| 位置 | 模組 | 作用 |
|---|---|---|
| **① 查詢理解** | `src/query_parser.py` | 將自然語言查詢解析為結構化標籤（城市／薪資／工作性質／職務關鍵字），並進行同義詞與品牌名擴展、英中翻譯 |
| **② 技能萃取與圖譜建構** | `src/skill_extractor.py` | 由 `職務名稱` + `職務內容` 萃取技能、正規化同義寫法、分類，產出 `job_skills_cache.csv`（88,470 筆職缺 → 2,410 個正規化技能節點） |

技能萃取採**混合策略**：結構化欄位（`電腦技能資料`、`工作技能`、`專業證照`）優先直接解析，
無結構化欄位者才呼叫 LLM。此設計同時降低成本與幻覺風險 —— 有明確來源時不讓模型自由生成。

### LLM 已知失敗模式與防護機制

以下**均為本專案實測觀察到之現象**，非理論列舉。量化數據見
[`eval/EVALUATION_REPORT.md`](eval/EVALUATION_REPORT.md)。

| # | 失敗模式 | 實測現象 | 防護機制 | 狀態 |
|---|---|---|---|---|
| 1 | **同義詞過度擴展** | 查詢「現領」（測試集出現 64 次，12.8%）被擴展為「日薪／當日領／即領／日結／週領／現金」，候選集由 **142 筆暴增至 10,468 筆（74×）**，使用者實際點擊之職缺被稀釋出前 10 名 | **條件式擴展**：字面查詢候選數 ≥ 200 時不呼叫擴展，僅於字面檢索不足時才擴展 | ✅ 已實作並量測（NDCG@10 `full` 0.0118 → `hybrid` 0.0152，**+28.8%**） |
| 2 | **同義詞誤合併導致排序失真** | 排序鍵 `relevance_hits` 計算命中標籤**數量**，故一筆命中三個寬鬆同義詞之職缺會勝過一筆精確命中原始關鍵字者 —— 擴展主動將排序推離字面正解 | 已定位機制；建議改為**加權命中**（原始詞權重高於擴展詞） | ⚠️ 已記錄於建議行動，尚未實作 |
| 3 | **技能層級錯置** | LLM 萃取之技能為軟體／證照／工作任務層級（`Illustrator`、`SAP ERP`、`會計核算與帳務處理`），而使用者查詢為職稱層級（`司機`、`現領`、`門市人員`）。實測 `司機`／`現領`／`門市人員`／`作業員`／`廚師` 於 2,410 個技能節點中**無任何匹配** | 已量化並揭露；圖譜檢索需增加職稱層節點或層級對映 | ⚠️ 已記錄為限制 |
| 4 | **輸出格式不穩定（Markdown 包裝）** | 部分模型將 JSON 陣列包在 ` ```json … ``` ` 中，`json.loads` 直接拋錯並耗盡 3 次重試，**靜默退回規則式分詞** | `_extract_json_array()`：剝除 code fence、擷取首個 `[` 至末個 `]` 再解析 | ✅ 已修復 |
| 5 | **中英縮寫歧義** | `pt`、`fe`、`35k以上` 等縮寫語意依上下文而異，交由 LLM 判讀不穩定 | **規則式前處理**（`src/utils/abbreviations.py`）在呼叫 LLM 前先行展開，不依賴模型猜測 | ✅ 已實作 |
| 6 | **靜默降級（最危險）** | Bedrock 憑證失效或模型 EOL 時，`querytoRequirement()` 會退回空白分詞並正常回傳 —— 外觀與成功執行**無法區分**。若發生於 ablation，「有 AI」與「無 AI」兩組將變成同一件事，得出與事實相反之結論 | `eval/run_ablation.py` 之 `probe_llm()`：執行前實際呼叫一次 Bedrock，失敗即**略過該組並輸出訊息**，絕不產出可能無效的數字 | ✅ 已實作（實測攔截到一次真實設定錯誤） |
| 7 | **模型版本 EOL** | `claude-3-5-sonnet-20241022-v2:0` 回傳 `ResourceNotFoundException: model has reached end of life` | 固定於 inference profile ID（`us.` 前綴）並於 README 明列版本；`probe_llm()` 於評測前驗證可用性 | ✅ 已實作 |

### 決定性與可重現性

LLM 呼叫使用 `temperature=0`，並經 JSON schema 驗證 + 最多 3 次重試 + 規則式 fallback。
離線評測時所有解析結果快取於 `eval/llm_tag_cache.json`，確保重跑 ablation 時 LLM 輸出不變 ——
**組間差異僅來自受測變因，而非模型抽樣波動**。

---

## 能力知識圖譜

### 圖譜架構

以「能力/技能」為核心的知識圖譜，以 **networkx `DiGraph`** 實作並序列化為 pickle 快取。
節點與邊之定義如下（`src/graph_builder.py`）。完整 schema 與遍歷 trace 範例見
[`設計文件/graph_schema_and_trace.md`](設計文件/graph_schema_and_trace.md)。

**Vertex Labels:**

| Label | 說明 | 數量 |
|-------|------|------|
| `Job` | 職缺 | ~265,530 |
| `Skill` | 技能/能力 | ~2,410 |
| `City` | 城市 | 37 |
| `Category` | 職務中類 | 58 |
| `User` | 使用者 | ~166,000 |

**Edge Labels:**

| Label | From → To | 說明 |
|-------|-----------|------|
| `REQUIRES` | Job → Skill | 職缺需要此技能 |
| `LOCATED_IN` | Job → City | 職缺所在城市 |
| `IN_CATEGORY` | Job → Category | 職缺所屬職務中類 |
| `VIEWED` | User → Job | 使用者瀏覽過此職缺 |
| `APPLIED` | User → Job | 使用者應徵過此職缺 |
| `PREFERS_CITY` | User → City | 使用者偏好城市 |
| `HAS_SKILL` | User → Skill | 使用者具備此技能（從應徵歷史推導） |

### 混合技能萃取（Hybrid Extraction）

1. **結構化欄位優先**：直接解析 `電腦技能資料`、`工作技能`、`專業證照`
2. **LLM 萃取**：使用 Bedrock Claude 從 `職務名稱` + `職務內容` 提取技能

結果快取於 `dataset/job_skills_cache.csv`（88,470 筆職缺 → 2,410 個正規化技能）。

### 推薦邏輯（圖譜遍歷）

**已登入用戶：**
```
User → HAS_SKILL → Skill ← REQUIRES ← Candidate Job  (技能重疊)
User → PREFERS_CITY → City ← LOCATED_IN ← Candidate Job  (城市匹配)
User → HAS_SKILL → Skill ← HAS_SKILL ← OtherUser → APPLIED → Job  (協同過濾)
```

**匿名用戶：**
```
Query Tags → Skill ← REQUIRES ← Candidate Job  (查詢技能匹配)
```

### 執行後端

| 後端 | 開關 | 本次交付狀態 |
|------|------|------|
| networkx（行程內遍歷） | `USE_NEPTUNE=false`（預設） | ✅ **實際使用** |
| Amazon Neptune（Gremlin） | `USE_NEPTUNE=true` | ❌ 未使用；連線程式碼保留於 `src/neptune_client.py`，叢集未完成部署 |

兩者遍歷邏輯相同，Neptune 僅作為儲存後端。

---

## 資料流程

### ETL 流程（離線預處理）

```
職缺瀏覽_*.csv + 主動應徵_*.csv
        │
        ▼
  genViewCount.py ──────▶ 瀏覽次數.csv (288,319 筆)
        │                   score = Σ e^(-0.1·Δt) × weight
        │
職缺瀏覽_*.csv + 主動應徵_*.csv + 職缺.csv
        │
        ▼
  userAnalysis.py ──────▶ userBehaviorFeature.csv (166,539 用戶)
                  ──────▶ userBehaviorEvents.csv  (5,091,661 事件)

職缺.csv + userBehaviorEvents.csv
        │
        ▼
  skill_extractor.py ───▶ job_skills_cache.csv (89,470+ 筆)
        │                   混合萃取：結構化 + LLM
        │
        ▼
  graph_builder.py ─────▶ networkx 圖譜快取 (pickle)
                            Job, Skill, City, Category, User 節點 + 邊
```

> ⚠️ **目前 `src/graph_builder.py` 只建構 User–Job 互動層，不再產生 Skill / City / Category
> 節點**。既有的技能圖譜快取由先前版本產生，該版本程式碼未保留於本 repo —— 評審 clone 後
> **無法由本 repo 重建含技能層之圖譜**。此為已知交付缺口。

### 資料時間範圍

- 訓練資料：2026-06-01 ~ 2026-06-05（圖譜建構僅使用 ≤6/05 的事件）
- 評估資料：2026-06-06 ~ 2026-06-07（held out）

---

## 環境設定

### 系統需求

- Python 3.11+
- AWS 帳號（需 `bedrock-runtime:InvokeModel` 權限）
- 記憶體 ≥ 8 GB（DuckDB 職缺表常駐 + 圖譜快取）
- **不需要** Amazon Neptune —— 圖譜以 networkx 在本機執行

### 安裝

```bash
git clone https://github.com/us3r413/recommendation-algorithm.git
cd recommendation-algorithm
pip install -r requirements.txt
```

### 環境變數

```env
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6
AWS_DEFAULT_REGION=us-west-2
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
AWS_SESSION_TOKEN=your-session-token

# Graph ranking toggle（目前部署為關閉）
GRAPH_FOR_SIGNED_IN_USER=false
GRAPH_FOR_ANONYMOUS_USER=false

# 圖譜後端：預設 networkx。設為 true 才會嘗試連線 Neptune（本次交付未使用）
USE_NEPTUNE=false
```

| 變數名稱 | 說明 | 預設值 |
|----------|------|--------|
| `BEDROCK_MODEL_ID` | Bedrock 模型 ID | `us.anthropic.claude-sonnet-4-6` |
| `AWS_DEFAULT_REGION` | AWS 區域 | `us-west-2` |
| `USE_NEPTUNE` | 改用 Neptune 作為圖譜後端（本次未使用） | `false` |
| `GRAPH_FOR_SIGNED_IN_USER` | 登入用戶啟用圖譜排序 | `false` |
| `GRAPH_FOR_ANONYMOUS_USER` | 匿名用戶啟用圖譜排序 | `false` |

`NEPTUNE_ENDPOINT` / `NEPTUNE_PORT` 僅於 `USE_NEPTUNE=true` 時讀取，本次交付不需設定。

---

## 資料集準備

### 原始資料（需自行放置於 `dataset/`，git-ignored）

| 檔案 | 說明 | 規模 |
|------|------|------|
| `職缺.csv` | 職缺主表 | 1,218,635 筆 |
| `職缺瀏覽_20260601_20260607.csv` | 瀏覽行為紀錄 | ~8,467,232 筆 |
| `主動應徵_0601-0607.csv` | 應徵行為紀錄 | 包含於上述統計 |
| `城市對照表.csv` | 城市代碼對照表 | — |
| `職務對照表.csv` | 職務類別對照表 | — |

### 產生衍生資料表

```bash
# 1. 基礎 ETL
python dataset/genViewCount.py       # → 瀏覽次數.csv
python dataset/userAnalysis.py       # → userBehaviorFeature.csv + userBehaviorEvents.csv

# 2. 技能萃取
python -m src.skill_extractor            # 結構化欄位（快速）
python -m src.skill_extractor --llm      # + LLM 萃取（需 Bedrock）

# 3. 圖譜建構（networkx，輸出 dataset/graph_cache.pkl）
python -m src.graph_builder
python -m src.graph_builder --rebuild    # 強制重建

# 圖譜時間範圍由環境變數控制；評測用之訓練期圖譜須設 GRAPH_UNTIL=2026-06-05
GRAPH_SINCE=2026-06-01 GRAPH_UNTIL=2026-06-05 python -m src.graph_builder --rebuild
```

> ⚠️ `GRAPH_UNTIL` 預設為 `2026-06-07`，涵蓋評測之測試日與標註日。**離線評測不得使用該預設值
> 建出的圖譜**（命題規定使用 test 期資料建圖者該指標項不計分）。`eval/reproduce.sh` 會自行
> 以訓練期重建，不依賴此快取。

---

## 部署

### AWS 部署架構

```
                    評審 / 使用者
                          │  HTTPS/HTTP :8000
                          ▼
        ┌─────────────────────────────────────────┐
        │  AWS EC2  t3.large (2 vCPU / 8 GB)      │
        │  us-west-2                              │
        │  ┌───────────────────────────────────┐  │
        │  │ uvicorn + FastAPI (app.py)        │  │
        │  │   POST /api/v1/jobs/search        │  │
        │  │   GET  /health                    │  │
        │  ├───────────────────────────────────┤  │
        │  │ ① query_parser  ─────────────────────────▶ AWS Bedrock
        │  │ ② retriever  → DuckDB (in-memory) │  │    Claude Sonnet 4.6
        │  │ ③ ranker     → networkx 圖譜快取  │  │    (us-west-2)
        │  └───────────────────────────────────┘  │
        │  本機檔案：職缺.parquet / 瀏覽次數.csv  │
        │            graph_cache.pkl              │
        └─────────────────────────────────────────┘

  基礎設施定義：infra/cloudformation.yaml（EC2 + Security Group, port 8000）
```

**設計取捨**：資料與圖譜皆常駐於 EC2 行程內（DuckDB in-memory + networkx pickle），
不額外依賴 RDS / Neptune / OpenSearch —— 換取最低查詢延遲與最少故障點，
代價是單機記憶體上限（見 [效能基準](#效能基準) 之記憶體風險說明）。
唯一的外部相依為 AWS Bedrock。

### EC2 部署

```bash
# SSH into EC2 (t3.large)
aws ec2-instance-connect ssh --instance-id i-03c39c7e44e7a039d --region us-west-2

# Setup
cd ~/recommendation-algorithm
git pull
pip3.11 install -r requirements.txt

# Env vars — 臨時憑證數小時即過期，重啟前務必更新
export AWS_DEFAULT_REGION=us-west-2
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_SESSION_TOKEN=...
export BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6

# Start API
nohup python3.11 -u -m uvicorn app:app --host 0.0.0.0 --port 8000 > api.log 2>&1 &

# Build/rebuild graph (background)
nohup python3.11 -u -m src.graph_builder > graph_build.log 2>&1 &
```

### 部署後驗證

```bash
curl -s -X POST http://<host>:8000/api/v1/jobs/search \
  -H "Content-Type: application/json" \
  -d '{"query":"月薪四萬以上的台北兼職"}'
```

須回傳 10 筆結果。若回傳空陣列，代表 Bedrock 憑證失效導致 LLM 解析退回空白分詞
（整句被當成單一關鍵字，撈不到任何職缺）—— 更新憑證後重啟即可。

---

## 執行範例

### 本機 Debug 模式

```bash
python main.py
```

### Python API

```python
from src.pipeline import recommend

results = recommend("台北 前端工程師 35k以上", talent_no=0)
results = recommend("後端 pt工作", talent_no=12345)
```

### 啟動 API Server

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

---

## Benchmark 重現步驟

### 一鍵重現（評審執行此指令即可）

```bash
bash eval/reproduce.sh            # 完整 500 筆查詢
bash eval/reproduce.sh --quick    # 50 筆，快速驗證流程
```

此腳本會自動完成：建立虛擬環境 → 產生訓練期熱門度表 → 產生訓練期圖譜 →
建構帶分級標註之測試集 → 執行所有 ablation 組別 → 輸出報告。
僅需先將主辦方提供之 CSV 置於 `dataset/`。

### 手動建置正式服務（非評測用）

```bash
pip install -r requirements.txt
python dataset/genViewCount.py       # → 瀏覽次數.csv
python dataset/userAnalysis.py       # → userBehaviorFeature.csv
python -m src.skill_extractor        # → job_skills_cache.csv
python -m src.graph_builder          # → graph_cache.pkl
python main.py                       # 本機互動式 debug
```

### 效能基準

| 階段 | 典型延遲 | 備註 |
|------|----------|------|
| Stage 1 (querytoRequirement) | 1–3s（首次）/ ~0ms（快取） | Sonnet 4.6；可切換 Haiku 加速 |
| Stage 2 (grabFromDatabase) | 0.5–2s | DuckDB in-memory |
| Stage 3 (ranking) | 熱門度 <50ms / 圖譜 50–200ms | 圖譜為 networkx 行程內遍歷 |
| 端到端（首次） | 2–5s | 瓶頸在 LLM |
| 端到端（快取命中） | 0.5–2s | 跳過 LLM |

> ⚠️ **記憶體風險**：評測期間觀測到單一發散查詢可回傳逾百萬筆候選（最大 1,149,322 筆），
> 記憶體峰值達 **6.8 GB**，逼近 EC2 t3.large 的 8 GB 上限。建議於檢索 SQL 加入
> `ORDER BY score DESC LIMIT 5000`。

---

## 離線評測與 Ablation

命題交付項目要求提供「有圖譜 vs 無圖譜」雙重指標 ablation script，須能一鍵重現。
完整框架位於 [`eval/`](eval/)，方法說明見 [`eval/EXPERIMENT_DESIGN.md`](eval/EXPERIMENT_DESIGN.md)，
結果見 [`eval/EVALUATION_REPORT.md`](eval/EVALUATION_REPORT.md)。

```bash
bash eval/reproduce.sh            # 完整 500 筆查詢
bash eval/reproduce.sh --quick    # 50 筆，快速驗證流程
```

### 指定單一對照：有 AI vs 無 AI、有圖譜 vs 無圖譜

命題要求之兩組雙重指標對照，可分別以單一指令執行並直接讀出差異：

```bash
# ① 有生成式 AI vs 無生成式 AI（LLM 語意解析與擴展 開/關）
python eval/run_ablation.py --arms full,no_llm

# ② 有圖譜 vs 無圖譜（能力知識圖譜 開/關，解析方式固定為規則式以隔離變因）
python eval/run_ablation.py --arms graph_no_llm,no_llm

# 加上 --limit 50 可快速試跑
```

輸出為 Markdown 表格（同時寫入 `eval/ABLATION_REPORT.md`），格式如下：

| 設定 | NDCG@10 | vs 基準 | Hit@1 | Hit@10 |
|---|---:|---:|---:|---:|
| `no_llm` 移除 LLM（規則式分詞） | 0.0190 | 基準 | 0.0160 | 0.0420 |
| `full` LLM + 語意擴展 | 0.0118 | −37.9% | 0.0080 | 0.0300 |

**LLM 組不會靜默降級**：`run_ablation.py` 於執行前以 `probe_llm()` 實際呼叫一次 Bedrock，
失敗即**略過該組並輸出訊息**，而非退回規則式路徑後產出看似有效的數字 ——
否則「有 AI」與「無 AI」兩組會變成同一件事，得出的結論恰好與本 ablation 欲驗證者相反。

可用組別：

| 組別 | 說明 | 需要 Bedrock |
|------|------|---|
| `full` | LLM 解析 + 語意擴展 + 熱門度排序 | ✅ |
| `no_llm` | 規則式分詞（**無生成式 AI**） | — |
| `no_expand` | LLM 解析、關閉語意擴展 | ✅ |
| `hybrid` | 條件式擴展（字面候選不足時才擴展） | ✅ |
| `graph_no_llm` | 能力知識圖譜排序 + 規則式解析 | — |
| `graph_interaction` | User–Job 互動圖譜排序 + 規則式解析 | — |
| `no_rank` | 完全不排序（下界對照） | ✅ |

### 評測結果摘要

500 筆測試查詢、1,508 個分級標註。**NDCG@10 / Hit@1 / Hit@10 依命題要求同步報告。**

**① 有生成式 AI vs 無生成式 AI**

| 設定 | NDCG@10 | vs 無 AI | Hit@1 | Hit@10 | 秒/查詢 |
|---|---:|---:|---:|---:|---:|
| `no_llm` 規則式解析（**無生成式 AI**） | **0.0190** | 基準 | 0.0160 | 0.0420 | 0.87 |
| `hybrid` LLM + 條件式擴展 | 0.0152 | −20.1% | 0.0100 | 0.0400 | 1.71 |
| `no_expand` LLM 解析、無擴展 | 0.0146 | −23.1% | 0.0120 | 0.0320 | 2.77 |
| `full` LLM + 無條件擴展 | 0.0118 | −37.9% | 0.0080 | 0.0300 | 2.75 |

**② 有圖譜 vs 無圖譜**（解析方式固定為規則式，以隔離圖譜之貢獻）

| 設定 | NDCG@10 | vs 無圖譜 | Hit@1 | Hit@10 |
|---|---:|---:|---:|---:|
| `no_llm` **無圖譜**（熱門度排序） | 0.0190 | 基準 | 0.0160 | 0.0420 |
| `graph_no_llm` 能力知識圖譜 | 0.0190 | ±0.0% | 0.0160 | 0.0420 |
| `graph_interaction` 使用者–職缺互動圖譜 | 0.0188 | −0.8% | 0.0160 | 0.0420 |

**誠實聲明**

- **本評測集上，移除生成式 AI 模組後 NDCG@10 未退化，反而上升。** 我們如實回報，
  未挑選對己有利之組別呈現。
- 命題效果標準要求「NDCG@10 較 baseline 有可量化提升（建議 ≥ 5%）」。
  主辦方 reference baseline 未於本專案取得，且本評測集之標註來源與主辦方不同，
  **絕對數值不可跨評測集比較**；本表用途為組間相對比較。
- 檢定力不足：500 題中僅 **19 題**能區分 `full` 與 `no_llm`（其餘 481 題兩組結果完全相同，
  因測試集 95% 為極短職稱關鍵字，兩種解析輸出一致）。`full` vs `no_llm` 之配對 bootstrap
  p 值落於 **0.046–0.053**（隨 seed 變動、橫跨 0.05 判準），`hybrid` vs `no_llm` p = 0.265。
- 完整診斷、分層分析與已知限制見 [`eval/EVALUATION_REPORT.md`](eval/EVALUATION_REPORT.md)。

### 時序切分

主辦方未公告官方 train/test 切分（命題載明將由工作坊提供，實際僅公告資料範圍
`2026-06-01 ~ 06-07`），本專案採用下列自訂切分並明確揭露：

| 期間 | 範圍 | 用途 |
|------|------|------|
| 訓練期 | `06-01` ~ `06-05` | 熱門度、圖譜、所有特徵僅能使用本期資料 |
| 測試查詢 | `06-06` 單日 | 評測查詢來源 |
| 標註觀察窗口 | `06-06` ~ `06-07` | 僅用於觀察後續行為（標準答案） |

`06-07` 保留為標註窗口而非查詢來源，確保每筆測試查詢皆有 ≥24 小時之前瞻觀察期；
否則該日傍晚之查詢將因缺乏後續行為資料而被系統性誤標為「無相關結果」。

### 相關性標註

依命題定義採分級相關性：`2 = 搜尋後投遞履歷`、`1 = 搜尋後點閱`、`0 = 無互動`，
同一 `(query, job)` 對取最大值。歸因窗口為搜尋後 30 分鐘內，並於該使用者下一次搜尋時截斷（下限 2 分鐘）。

標註**未限制於 `empStr`**（既有系統當次曝光清單）。若加此限制，等同將既有排序的曝光偏差寫入標準答案，
受測系統在結構上將不可能超越既有系統。實測顯示標註之相關職缺僅 **16.0%** 出現於既有系統當次前 20 名。

### 防洩漏措施

1. 熱門度表由 `eval/build_popularity.py` 以訓練期事件重建；時間衰退參考日為訓練期最後一刻，
   而非資料檔最後一日 —— 後者會使衰退權重隱含「已知資料延伸至 06-07」之未來資訊。
2. 互動圖由 `eval/build_graph_train.py` 以訓練期事件重建（413,977 節點 / 2,563,512 邊）。
   生產環境之全週圖不參與評測，且既有 `graph_cache.pkl` 僅存加總後之邊權重、不含時間戳，
   無法事後移除測試期的邊。
3. 評測時 `talent_no` 一律為 0 —— 官方 API 合約不含 `talentNo`，個人化路徑不會於評測時觸發。

### 已知限制

- 測試查詢日為週六，流量為週一之 61%，職類分布與平日不同（七天資料之先天限制）。
- 本框架絕對數值不可與主辦方 baseline 直接比較；用途為**組別間相對比較**。
- `職缺.csv` 僅含 `職缺最後修改時間`（最新至 `2026-06-24`），無上架時間欄位，
  故無法還原職缺於測試日當天之文字版本。此限制對所有隊伍一致。

---

## Random Seed 與決定性

檢索（DuckDB 參數化查詢）與排序（確定性加權公式 + 穩定排序）流程**完全不含隨機性** ——
無隨機取樣、隨機初始化或隨機打散步驟，故推論路徑本身無需設定 random seed。

專案中僅有兩處非確定性來源，皆已控制：

| 來源 | 控制方式 |
|------|---------|
| 評測查詢抽樣（reservoir sampling）與打散 | `eval/build_testset.py --seed`，預設 `42`。相同 seed 下 `testset.jsonl` 逐位元一致 |
| LLM 生成 | `temperature=0` + JSON schema 驗證 + 最多 3 次重試 + 規則式 fallback；評測時解析結果快取於 `eval/llm_tag_cache.json` |

---

## Position Bias Correction

**本系統未採用** IPS（Inverse Propensity Scoring）或 Doubly Robust 等曝光偏差修正方法。
命題載明此項為「鼓勵但非強制」，此處依規定明確標註採用狀態。

已採行之替代緩解措施：

1. **訊號強弱分級** —— 主動應徵權重 3、職缺瀏覽權重 1。應徵行為需付出額外成本，
   受曝光位置影響顯著小於瀏覽。
2. **時間衰退**（λ = 0.1）—— 稀釋長期累積的位置優勢，避免既有排序的曝光偏差
   在熱門度分數中無限放大。
3. **評測標註不限制於 `empStr`** —— 避免將既有排序的曝光偏差寫入標準答案。
   實測顯示曝光偏差顯著存在（僅 16.0% 之相關標註曾出現於既有系統前 20 名）。

---

## 資料／模型／索引版本說明

### 資料版本

| 資料 | 版本／範圍 | 筆數 |
|---|---|---:|
| `職缺.csv` | 主辦方提供，`職缺最後修改時間` 最新至 `2026-06-24` | 1,218,635 |
| `職缺瀏覽_20260601_20260607.csv` | `2026-06-01` ~ `2026-06-07` | ~8,467,232 |
| `主動應徵_0601-0607.csv` | `2026-06-01` ~ `2026-06-07` | — |
| `userSearchLog_20260601_20260607.csv` | `2026-06-01` ~ `2026-06-07` | — |
| 熱門度表（評測用） | **僅 `06-01` ~ `06-05`**，時間衰退基準日設於訓練期最後一刻 | 286,632 職缺 |
| 測試集 `eval/testset.jsonl` | 查詢取自 `06-06`，標註觀察窗口 `06-06` ~ `06-07` | 500 查詢 / 1,508 標註 |

### 索引與衍生產物版本

| 產物 | 建構來源 | 規模 | 備註 |
|---|---|---|---|
| `職缺.parquet` | `職缺.csv`（ZSTD） | 1,218,635 列 | 僅加速，結果與 CSV 一致 |
| DuckDB in-memory table | 啟動時載入 Parquet | — | 無持久化索引檔 |
| `job_skills_cache.csv` | 結構化欄位 + LLM 萃取 | 88,470 職缺 / 2,410 技能 | LLM 萃取結果快取 |
| `graph_cache.pkl`（正式） | `GRAPH_SINCE=06-01`, `GRAPH_UNTIL=06-07` | — | **含測試期資料，不可用於評測** |
| 訓練期圖譜（評測用） | `06-01` ~ `06-05` | 413,977 節點 / 2,563,512 邊 | 由 `eval/reproduce.sh` 自動重建 |
| `eval/llm_tag_cache.json` | LLM 解析結果 | 329 筆 | 確保 ablation 重跑時 LLM 輸出一致 |

### 模型版本

| 元件 | 模型 / 版本 | 用途 |
|------|-------------|------|
| LLM | Claude Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`, via AWS Bedrock) | 查詢語意解析 + 技能萃取 |
| 查詢引擎 | DuckDB 1.3.0 | SQL 篩選 ~1.2M 職缺 |
| 圖譜引擎 | networkx 3.4.2 | 能力知識圖譜遍歷（**本次實際使用**） |
| 圖譜引擎（未使用） | Amazon Neptune + Gremlin | 程式碼保留，叢集未部署 |

### 排序超參數

| 參數 | 值 | 說明 |
|------|-----|------|
| Graph weight | 0.7 | final = graph×0.7 + popularity×0.3（僅 `GRAPH_FOR_*=true` 時生效） |
| Skill overlap weight | 0.5 | graph_score 中技能重疊佔比 |
| City match weight | 0.3 | graph_score 中城市匹配佔比 |
| Co-user weight | 0.2 | graph_score 中協同過濾佔比 |
| λ (時間衰退) | 0.1 | 約 7 天後分數衰退至 50% |
| 冷啟動門檻 | 3 | total_events < 3 → fallback |

---

## 專案結構

```
recommendation-algorithm/
├── app.py                         # FastAPI 應用（/api/v1/jobs/search, /health）
├── main.py                        # 本機 debug 入口
├── apitest.py                     # 遠端 API 測試腳本
├── conftest.py                    # Pytest 共用 fixtures
├── Dockerfile                     # Docker 容器化（python:3.11-slim + uvicorn）
├── .dockerignore                  # Docker build 排除項
├── deploy.md                      # 部署筆記
├── dataset/                       # 原始 + 衍生資料 + ETL 腳本
│   ├── genViewCount.py            # 產生瀏覽次數.csv
│   ├── userAnalysis.py            # 產生用戶行為特徵
│   └── README.md                  # 資料表 schema 說明
├── src/                           # 核心程式碼
│   ├── pipeline.py                # 三階段 Pipeline 入口
│   ├── query_parser.py            # Stage 1: LLM 語意解析
│   ├── retriever.py               # Stage 2: DuckDB 檢索
│   ├── ranker.py                  # Stage 3: 排序路由
│   ├── neptune_client.py          # Neptune 連線管理（IAM SigV4，本次未使用）
│   ├── graph_builder.py           # 圖譜建構（networkx → pickle 快取）
│   ├── graph_ranker.py            # 能力圖譜排序邏輯
│   ├── skill_extractor.py         # 混合技能萃取（結構化 + LLM）
│   ├── utils/                     # 工具模組（abbreviations, tag_parser）
│   └── tests/                     # 單元與整合測試
├── eval/                          # 離線評測框架
│   ├── reproduce.sh               # ★ 一鍵重現所有評測
│   ├── run_ablation.py            # Ablation 實驗執行器（含 probe_llm 防降級）
│   ├── metrics.py                 # 評測指標（NDCG/MRR/Hit@K，附 9 項自我驗證）
│   ├── build_testset.py           # 測試集產生（分級標註）
│   ├── build_popularity.py        # 訓練期熱門度表（防洩漏）
│   ├── build_graph_train.py       # 訓練期互動圖譜（防洩漏）
│   ├── graph_ranker_interaction.py# 互動圖譜排序（評測用，不動 src/）
│   ├── analyze_strata.py          # 分層分析 + 配對 bootstrap 顯著性檢定
│   ├── EXPERIMENT_DESIGN.md       # 實驗設計方法
│   └── EVALUATION_REPORT.md       # 評測結果報告
├── 專案白話說明/                  # 非技術背景可讀之專案說明
├── infra/                         # AWS 基礎設施 & 部署
│   ├── cloudformation.yaml        # EC2 + 安全群組（t3.large, port 8000）
│   ├── deploy-to-ec2.ps1         # PowerShell 部署腳本
│   └── setup-ec2.sh              # EC2 環境初始化
├── 設計文件/                      # 設計文件
│   ├── graph_schema_and_trace.md  # ★ 圖譜 schema + 遍歷 trace 範例
│   ├── draft3rewrite.md           # Pipeline 設計參考
│   └── neptune_graph_design.md    # Neptune schema 設計（未採用之後端）
├── .env                           # 環境變數（git-ignored）
└── requirements.txt               # Python 依賴
```

---

## 設計文件

- [`設計文件/graph_schema_and_trace.md`](設計文件/graph_schema_and_trace.md) — **節點／邊定義、權重、以及推論時的遍歷 trace 範例**（命題交付項目 4）
- [`eval/EXPERIMENT_DESIGN.md`](eval/EXPERIMENT_DESIGN.md) — 離線評測之實驗設計與方法
- [`eval/EVALUATION_REPORT.md`](eval/EVALUATION_REPORT.md) — Ablation 結果、診斷與已知限制
- [`專案白話說明/`](專案白話說明/) — 非技術背景可讀之完整專案說明
- `設計文件/neptune_graph_design.md` — Neptune schema 設計（**未採用之後端**，保留供參考）
- `設計文件/draft3rewrite.md` — Pipeline 三階段設計參考
- `設計文件/user_behavior_analysis_proposal.md` — 用戶行為分析方案（採納 Option 3）

---

## 依賴版本

```
duckdb==1.3.0
hypothesis==6.135.26
pandas==2.2.3
boto3>=1.35.0
python-dotenv==1.2.2
networkx==3.4.2
fastapi==0.115.6
uvicorn==0.34.0
gremlinpython==3.7.3
```

| 套件 | 用途 |
|------|------|
| `duckdb` | 高速 SQL 查詢引擎（~1M CSV） |
| `pandas` | 資料載入與預處理 |
| `boto3` | AWS SDK（Bedrock Runtime） |
| `networkx` | 能力知識圖譜建構與遍歷（**本次實際使用之圖譜引擎**） |
| `gremlinpython` | Neptune Gremlin 客戶端（`USE_NEPTUNE=true` 時才載入，本次未使用） |
| `fastapi` + `uvicorn` | Web API |
| `hypothesis` | Property-based 測試 |
| `python-dotenv` | 環境變數載入 |
