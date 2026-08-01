# 職缺推薦演算法

根據使用者的搜尋查詢（及登入狀態），從約 100 萬筆職缺中推薦最相關的 10 筆職缺。

---

## 目錄

- [系統架構](#系統架構)
- [API Endpoint](#api-endpoint)
- [知識圖譜（Neptune）](#知識圖譜neptune)
- [資料流程](#資料流程)
- [環境設定](#環境設定)
- [資料集準備](#資料集準備)
- [部署](#部署)
- [執行範例](#執行範例)
- [Benchmark 重現步驟](#benchmark-重現步驟)
- [資料／模型／索引版本說明](#資料模型索引版本說明)
- [專案結構](#專案結構)
- [設計文件](#設計文件)

---

## 系統架構

系統採用三階段 Pipeline + 能力知識圖譜設計。查詢引擎使用 DuckDB（in-process），圖譜引擎使用 Amazon Neptune（Gremlin），本地開發可 fallback 至 networkx。

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Recommendation Pipeline                       │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   使用者查詢 (ks/c0/d0) + talentNo                                  │
│        │                                                             │
│        ▼                                                             │
│   ┌──────────────────────────┐                                       │
│   │  querytoRequirement()    │  ← AWS Bedrock (Claude 3.5 Haiku)     │
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
│   │  ranking()               │──▶│ Amazon Neptune (Gremlin)    │     │
│   │  • relevance_hits 優先   │   │ 能力知識圖譜               │     │
│   │  • 技能重疊匹配          │   └────────────────────────────┘     │
│   │  • 城市偏好匹配          │   ┌────────────────────────────┐     │
│   │  • 協同過濾（共同技能）  │──▶│ userBehaviorFeature.csv     │     │
│   │  • Fallback: 熱門排序    │   └────────────────────────────┘     │
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

### 第三階段：排序（ranking）— 能力圖譜排序

使用 Amazon Neptune 知識圖譜進行能力基礎排序：

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

---

## API Endpoint

### Base URL

```
http://35.85.148.23:8000
```

### `POST /api/v1/jobs/search`

**Request Body (JSON):**

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `request_id` | string | 否 | 請求 ID |
| `query` | string | 否 | 搜尋關鍵字 |
| `talent_no` | int | 否 | 求職者編號，0=匿名 |
| `location_code` | string[] | 否 | 城市代碼列表 |
| `duty_code` | string[] | 否 | 職務類別代碼列表 |

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

## 知識圖譜（Neptune）

### 圖譜架構

以「能力/技能」為核心的知識圖譜，使用 Amazon Neptune + Gremlin 查詢語言。

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

1. **結構化欄位優先**（382,758 筆職缺）：直接解析 `電腦技能資料`、`工作技能`、`專業證照`
2. **LLM 萃取**（其餘職缺）：使用 Bedrock Claude 從 `職務名稱` + `職務內容` 提取技能

結果快取於 `dataset/job_skills_cache.csv`。

### 推薦邏輯（Gremlin 遍歷）

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

### 本地 Fallback

`USE_NEPTUNE=false` 時，相同邏輯以 networkx 在本機執行（無需 Neptune 連線）。

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
  graph_builder.py ─────▶ Neptune 圖譜 or networkx 快取
                            Job, Skill, City, User 節點 + 邊
```

### 資料時間範圍

- 訓練資料：2026-06-01 ~ 2026-06-05（圖譜建構僅使用 ≤6/05 的事件）
- 評估資料：2026-06-06 ~ 2026-06-07（held out）

---

## 環境設定

### 系統需求

- Python 3.11+
- AWS 帳號（需 `bedrock-runtime:InvokeModel` + Neptune 存取權限）
- Amazon Neptune cluster（或使用 networkx 本地模式）

### 安裝

```bash
git clone https://github.com/us3r413/recommendation-algorithm.git
cd recommendation-algorithm
pip install -r requirements.txt
```

### 環境變數

```env
BEDROCK_MODEL_ID=us.anthropic.claude-3-5-haiku-20241022-v1:0
AWS_DEFAULT_REGION=us-west-2
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
AWS_SESSION_TOKEN=your-session-token

# Graph ranking toggle
GRAPH_FOR_SIGNED_IN_USER=true
GRAPH_FOR_ANONYMOUS_USER=true

# Neptune connection
USE_NEPTUNE=true
NEPTUNE_ENDPOINT=your-cluster.neptune.amazonaws.com
NEPTUNE_PORT=8182
```

| 變數名稱 | 說明 | 預設值 |
|----------|------|--------|
| `BEDROCK_MODEL_ID` | Bedrock 模型 ID | `us.anthropic.claude-3-5-haiku-20241022-v1:0` |
| `AWS_DEFAULT_REGION` | AWS 區域 | `us-west-2` |
| `USE_NEPTUNE` | 啟用 Neptune 圖譜 | `false` |
| `NEPTUNE_ENDPOINT` | Neptune cluster endpoint | — |
| `NEPTUNE_PORT` | Neptune port | `8182` |
| `GRAPH_FOR_SIGNED_IN_USER` | 登入用戶啟用圖譜排序 | `true` |
| `GRAPH_FOR_ANONYMOUS_USER` | 匿名用戶啟用圖譜排序 | `true` |

---

## 資料集準備

### 原始資料（需自行放置於 `dataset/`，git-ignored）

| 檔案 | 說明 | 規模 |
|------|------|------|
| `職缺.csv` | 職缺主表 | ~1,000,000 筆 |
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

# 3. 圖譜建構
python -m src.graph_builder              # Neptune (USE_NEPTUNE=true) 或 networkx
python -m src.graph_builder --rebuild    # 強制重建
```

---

## 部署

### EC2 部署

```bash
# SSH into EC2 (same VPC as Neptune)
aws ec2-instance-connect ssh --instance-id i-03c39c7e44e7a039d --region us-west-2

# Setup
cd ~/recommendation-algorithm
git pull
pip3.11 install -r requirements.txt

# Set env vars
export USE_NEPTUNE=true
export NEPTUNE_ENDPOINT=db-neptune-1.cluster-cl8ocu4ecpw9.us-west-2.neptune.amazonaws.com
# ... (other env vars)

# Start API
nohup python3.11 -u -m uvicorn api:app --host 0.0.0.0 --port 8000 > api.log 2>&1 &

# Build/rebuild graph (background)
nohup python3.11 -u -m src.graph_builder > graph_build.log 2>&1 &
```

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
uvicorn api:app --host 0.0.0.0 --port 8000
```

---

## Benchmark 重現步驟

```bash
pip install -r requirements.txt
python dataset/genViewCount.py
python dataset/userAnalysis.py
python -m src.skill_extractor
python -m src.graph_builder
python main.py
```

### 效能基準

| 階段 | 典型延遲 | 備註 |
|------|----------|------|
| Stage 1 (querytoRequirement) | 1–2s（首次）/ ~0ms（快取） | Haiku 比 Sonnet 快 3-5x |
| Stage 2 (grabFromDatabase) | 0.5–2s | DuckDB in-memory |
| Stage 3 (ranking + Neptune) | 50–200ms | Gremlin 遍歷 |
| 端到端（首次） | 2–4s | 瓶頸在 LLM |
| 端到端（快取命中） | 0.5–2s | 跳過 LLM |

---

## 資料／模型／索引版本說明

### 模型版本

| 元件 | 模型 / 版本 | 用途 |
|------|-------------|------|
| LLM | Claude 3.5 Haiku (via AWS Bedrock) | 查詢語意解析 + 技能萃取 |
| 查詢引擎 | DuckDB 1.3.0 | SQL 篩選 ~1M 職缺 |
| 圖譜引擎 | Amazon Neptune + Gremlin | 能力知識圖譜遍歷 |
| 本地圖譜 | networkx 3.4.2 | 開發/測試用 fallback |

### 排序超參數

| 參數 | 值 | 說明 |
|------|-----|------|
| Graph weight | 0.7 | final = graph×0.7 + popularity×0.3 |
| Skill overlap weight | 0.5 | graph_score 中技能重疊佔比 |
| City match weight | 0.3 | graph_score 中城市匹配佔比 |
| Co-user weight | 0.2 | graph_score 中協同過濾佔比 |
| λ (時間衰退) | 0.1 | 約 7 天後分數衰退至 50% |
| 冷啟動門檻 | 3 | total_events < 3 → fallback |

---

## 專案結構

```
recommendation-algorithm/
├── api.py                         # FastAPI 應用（/api/v1/jobs/search, /health）
├── main.py                        # 本機 debug 入口
├── dataset/                       # 原始 + 衍生資料 + ETL 腳本
│   ├── genViewCount.py            # 產生瀏覽次數.csv
│   ├── userAnalysis.py            # 產生用戶行為特徵
│   └── README.md                  # 資料表 schema 說明
├── src/                           # 核心程式碼
│   ├── pipeline.py                # 三階段 Pipeline 入口
│   ├── query_parser.py            # Stage 1: LLM 語意解析
│   ├── retriever.py               # Stage 2: DuckDB 檢索
│   ├── ranker.py                  # Stage 3: 排序路由
│   ├── neptune_client.py          # Neptune 連線管理（IAM SigV4）
│   ├── graph_builder.py           # 圖譜建構（Neptune + networkx）
│   ├── graph_ranker.py            # 能力圖譜排序邏輯
│   ├── skill_extractor.py         # 混合技能萃取（結構化 + LLM）
│   ├── utils/                     # 工具模組
│   └── tests/                     # 單元與整合測試
├── 設計文件/                      # 設計文件
│   ├── draft3rewrite.md           # Pipeline 設計參考
│   └── neptune_graph_design.md    # Neptune 圖譜 schema 設計
├── .env                           # 環境變數（git-ignored）
└── requirements.txt               # Python 依賴
```

---

## 設計文件

- `設計文件/neptune_graph_design.md` — Neptune 能力知識圖譜完整 schema 設計
- `設計文件/draft3rewrite.md` — Pipeline 三階段設計參考
- `設計文件/user_behavior_analysis_proposal.md` — 用戶行為分析方案（採納 Option 3）

---

## 依賴版本

```
duckdb==1.3.0
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
| `boto3` | AWS SDK（Bedrock + Neptune IAM） |
| `networkx` | 本地圖譜 fallback |
| `gremlinpython` | Neptune Gremlin 客戶端 |
| `fastapi` + `uvicorn` | Web API |
