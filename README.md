# 職缺推薦演算法

根據使用者的搜尋查詢（及登入狀態），從約 100 萬筆職缺中推薦最相關的 10 筆職缺。

---

## 目錄

- [系統架構](#系統架構)
- [API Endpoint](#api-endpoint)
- [資料流程](#資料流程)
- [環境設定](#環境設定)
- [資料集準備](#資料集準備)
- [部署](#部署)
- [執行範例](#執行範例)
- [Benchmark 重現步驟](#benchmark-重現步驟)
- [資料／模型／索引版本說明](#資料模型索引版本說明)
- [專案結構](#專案結構)
- [有圖譜 vs 無圖譜設計決策](#有圖譜-vs-無圖譜設計決策)
- [設計文件](#設計文件)

---

## 系統架構

系統採用三階段 Pipeline 設計，所有模組皆為 Python 實作，資料引擎使用 DuckDB（in-process，無需啟動額外服務）。

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Recommendation Pipeline                       │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   使用者查詢 (ks/c0/d0) + talentNo                                  │
│        │                                                             │
│        ▼                                                             │
│   ┌──────────────────────────┐                                       │
│   │  querytoRequirement()    │  ← AWS Bedrock (Claude)               │
│   │  • 縮寫展開 (rule-based) │  ← src/utils/abbreviations.py        │
│   │  • LLM 語意解析          │                                       │
│   │  • 後處理: 城市正規化     │                                       │
│   │  • Fallback: 空白分詞    │                                       │
│   └────────────┬─────────────┘                                       │
│                │ tags: list[str]                                      │
│                ▼                                                      │
│   ┌──────────────────────────┐   ┌────────────────────┐             │
│   │  grabFromDatabase()      │──▶│ 職務對照表.csv      │ CodeAlike   │
│   │  • 標籤分類              │   └────────────────────┘             │
│   │  • 語意擴展              │   ┌────────────────────┐             │
│   │  • DuckDB SQL 篩選       │──▶│ 職缺.csv (~1M rows)│             │
│   │  • LEFT JOIN 熱門分數    │   └────────────────────┘             │
│   │  • c0/d0 代碼解析        │   ┌────────────────────┐             │
│   └────────────┬─────────────┘──▶│ 瀏覽次數.csv       │             │
│                │                  └────────────────────┘             │
│                │ candidates: list[dict]                               │
│                ▼                                                      │
│   ┌──────────────────────────┐   ┌────────────────────────────┐     │
│   │  ranking()               │──▶│ userBehaviorFeature.csv     │     │
│   │  • talentNo=0 → 熱門排序 │   └────────────────────────────┘     │
│   │  • cold-start → 熱門排序 │   ┌────────────────────────────┐     │
│   │  • 正常用戶 → 個人化排序  │──▶│ graph_cache.pkl (optional) │     │
│   │  • Graph toggle → 圖排序 │   └────────────────────────────┘     │
│   └────────────┬─────────────┘                                       │
│                │                                                      │
│                ▼                                                      │
│          Top 10 職缺結果 → rank + empStr                             │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 第一階段：語意解析（querytoRequirement）

- 縮寫展開：規則式替換（例如 `pt` → 兼職、`fe` → 前端工程師、`35k以上` → 薪資>=35000）
- 呼叫 AWS Bedrock Claude 將自然語言查詢解析為結構化標籤
- 後處理：城市名稱正規化（台北 → 台北市）、薪資格式統一
- 若 LLM 回應格式錯誤，最多重試 3 次後 fallback 為空白分詞

### 第二階段：資料庫檢索（grabFromDatabase）

- 標籤分類：城市 / 薪資門檻 / 職缺屬性（全職/兼職）/ 職務關鍵字
- 透過 `職務對照表.csv` 的 CodeAlike 欄位進行語意擴展
- 支援 `c0`（城市代碼）和 `d0`（職務類別代碼）額外篩選條件
- 使用 DuckDB 對 `職缺.csv`（~100 萬筆）執行參數化 SQL 查詢
- LEFT JOIN `瀏覽次數.csv` 為每筆候選職缺附加熱門分數

### 第三階段：排序（ranking）

- **匿名用戶**（talentNo = 0）：依 `瀏覽次數.score` 熱門度排序
- **登入用戶**（talentNo ≠ 0）：
  - 冷啟動（歷史事件 < 3）：退回熱門排序
  - 正常用戶：`final_score = personal_score × 0.7 + popularity_score × 0.3`
    - `personal_score = 地點匹配 × 0.4 + 職類匹配 × 0.4 + 薪資匹配 × 0.2`

---

## API Endpoint

### Base URL

```
http://35.85.148.23:8000
```

### `POST /search`（比賽評測用）

輸入欄位比照 `userSearchLog` 搜尋紀錄格式，輸出 `rank` 與 `empStr`，供計算 NDCG@10、Hit@1、Hit@10。

**Request Body (JSON):**

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `ks` | string | 否 | 搜尋關鍵字（例：`"前端工程師"`） |
| `c0` | string | 否 | 地區代碼，逗號分隔（例：`"100100,100200"`） |
| `d0` | string | 否 | 職務代碼，逗號分隔（例：`"140214,140213"`） |
| `talentNo` | int | 否 | 求職者編號，0=匿名 |

**Response:**

```json
{
  "rank": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
  "empStr": "113124505,98863698,132506498,132533929,130274154,103899856,92212438,130231281,132752278,132753720"
}
```

**範例呼叫:**

```bash
# 關鍵字搜尋（匿名）
curl -X POST http://35.85.148.23:8000/search \
  -H "Content-Type: application/json" \
  -d '{"ks": "前端工程師", "c0": "100100", "d0": "", "talentNo": 0}'

# 地區+職務代碼篩選
curl -X POST http://35.85.148.23:8000/search \
  -H "Content-Type: application/json" \
  -d '{"ks": "", "c0": "100100,100200", "d0": "140214,140213", "talentNo": 0}'

# 登入用戶（個人化排序）
curl -X POST http://35.85.148.23:8000/search \
  -H "Content-Type: application/json" \
  -d '{"ks": "行銷企劃", "c0": "", "d0": "", "talentNo": 138}'
```

### `POST /recommend`（詳細結果，除錯用）

**Request Body (JSON):**

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `query` | string | 否 | 搜尋關鍵字（例：`"台北 前端工程師 35k以上"`） |
| `talent_no` | int | 否 | 用戶 ID，0=匿名 |
| `c0` | string[] | 否 | 城市代碼陣列（例：`["100100"]`） |
| `d0` | string[] | 否 | 職務類別代碼陣列（例：`["140214"]`） |

**Response:**

```json
{
  "results": [
    {
      "職缺編號": 12345678,
      "職務名稱": "前端工程師",
      "工作城市": "台北市",
      "薪資下限": 45000,
      "職務小類": "前端工程師",
      "score": 5.23
    }
  ],
  "count": 10
}
```

### `GET /health`

```json
{"status": "ok"}
```

### Swagger UI

瀏覽器開 `http://35.85.148.23:8000/docs` 即可互動測試所有 endpoint。

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
```

### 線上推薦流程

```
HTTP POST /search
  ├── ks: "前端工程師"
  ├── c0: "100100"
  ├── d0: ""
  └── talentNo: 12345
        │
        ▼
  pipeline.recommend(query, talent_no, c0, d0)
        │
        ├── [1] querytoRequirement(ks)
        │       → ["前端工程師", "台北市"]
        │
        ├── [2] grabFromDatabase(tags, c0, d0)
        │       → N candidates with score
        │
        └── [3] ranking(candidates, talent_no)
                → Top 10 results
        │
        ▼
  Response: { rank: [1..10], empStr: "id1,id2,...,id10" }
```

---

## 環境設定

### 系統需求

- Python 3.11+
- 有效的 AWS 帳號（具備 `bedrock-runtime:InvokeModel` 權限）

### 安裝

```bash
git clone https://github.com/<your-org>/recommendation-algorithm.git
cd recommendation-algorithm
pip install -r requirements.txt
```

### 環境變數

在專案根目錄建立 `.env` 檔案：

```env
BEDROCK_MODEL_ID=anthropic.claude-3-5-sonnet-20241022-v2:0
AWS_DEFAULT_REGION=us-west-2
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
AWS_SESSION_TOKEN=your-session-token

# Graph RAG toggle (true/false)
GRAPH_FOR_SIGNED_IN_USER=false
GRAPH_FOR_ANONYMOUS_USER=false
```

| 變數名稱 | 說明 | 預設值 |
|----------|------|--------|
| `BEDROCK_MODEL_ID` | Bedrock 模型 ID | `anthropic.claude-3-5-sonnet-20241022-v2:0` |
| `AWS_DEFAULT_REGION` | AWS 區域 | `us-west-2` |
| `AWS_ACCESS_KEY_ID` | AWS Access Key | （必填） |
| `AWS_SECRET_ACCESS_KEY` | AWS Secret Key | （必填） |
| `AWS_SESSION_TOKEN` | AWS Session Token | （臨時憑證時必填） |
| `GRAPH_FOR_SIGNED_IN_USER` | 登入用戶啟用圖譜排序 | `false` |
| `GRAPH_FOR_ANONYMOUS_USER` | 匿名用戶啟用圖譜排序 | `false` |

---

## 資料集準備

### 原始資料（需自行放置於 `dataset/`，git-ignored）

| 檔案 | 說明 | 規模 |
|------|------|------|
| `職缺.csv` | 職缺主表 | ~1,000,000 筆 |
| `職缺瀏覽_20260601_20260607.csv` | 瀏覽行為紀錄 | ~8,467,232 筆 |
| `主動應徵_0601-0607.csv` | 應徵行為紀錄 | 包含於上述統計 |
| `userSearchLog_20260601_20260607.csv` | 搜尋紀錄 | — |
| `城市對照表.csv` | 城市代碼對照表 | — |
| `職務對照表.csv` | 職務類別對照表 | — |

### 產生衍生資料表

```bash
python dataset/genViewCount.py       # → dataset/瀏覽次數.csv
python dataset/userAnalysis.py       # → dataset/userBehaviorFeature.csv
                                     #   dataset/userBehaviorEvents.csv
python -m src.graph_builder          # → dataset/graph_cache.pkl (optional, ~4-5 min first time)
```

| 衍生表 | 說明 | 規模 |
|--------|------|------|
| `瀏覽次數.csv` | 職缺熱門分數 | 288,319 筆 |
| `userBehaviorFeature.csv` | 用戶偏好特徵 | 166,539 用戶 |
| `userBehaviorEvents.csv` | 用戶行為事件 | 5,091,661 事件 |
| `graph_cache.pkl` | networkx 圖快取（可選） | 441K nodes, 3.2M edges |

---

## 部署

### 快速部署（AWS EC2）

```powershell
# 1. 建立 EC2 instance（CloudFormation）
aws cloudformation create-stack --stack-name job-recommend-api `
  --template-body file://infra/cloudformation.yaml `
  --parameters ParameterKey=KeyPairName,ParameterValue=your-key-pair-name

# 2. 部署程式碼 + 資料
.\infra\deploy-to-ec2.ps1 -KeyFile "path\to\key.pem" -RemoteHost "<PUBLIC_IP>"

# 3. 後續更新（只傳程式碼）
.\infra\deploy-to-ec2.ps1 -KeyFile "path\to\key.pem" -RemoteHost "<PUBLIC_IP>" -SkipDataset
```

### 部署檔案

```
infra/
├── cloudformation.yaml   # AWS CloudFormation 模板 — EC2 + Security Group
├── deploy-to-ec2.ps1     # PowerShell 部署腳本 — SCP + 遠端安裝
└── setup-ec2.sh          # EC2 安裝啟動腳本 — Python + pip + uvicorn
```

詳見 [deploy.md](deploy.md)。

---

## 執行範例

### 本機 Debug 模式

```bash
python main.py
```

`main.py` 包含多個 debug 範例（匿名 + 登入用戶），會印出每階段耗時與 Top 10 結果：

```
============================================================
Query: '台北 前端工程師'  |  talent_no: 138
============================================================

[Stage 1] querytoRequirement → tags:  (1.41s)
  ['台北市', '前端工程師']

[Stage 2] grabFromDatabase → 1008 candidates  (1.20s)

[Stage 3] ranking → 10 results  (0.36s)

  Total: 2.97s
```

### Python API

```python
from src.pipeline import recommend

# 匿名用戶 — 純文字查詢
results = recommend("台北 前端工程師 35k以上", talent_no=0)

# 登入用戶（帶個人化排序）
results = recommend("後端 pt工作", talent_no=12345)

# 搭配 c0/d0 — 模擬 userSearchLog 重播
results = recommend("", talent_no=0, c0=["100100"], d0=["140214", "140213"])
```

### 啟動 API Server（本機）

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

---

## Benchmark 重現步驟

### 前置條件

1. 完成「環境設定」和「資料集準備」章節中的所有步驟
2. 確保 `.env` 中的 AWS 憑證有效

### 重現 Pipeline 端到端效能測試

```bash
pip install -r requirements.txt
python dataset/genViewCount.py
python dataset/userAnalysis.py
python main.py
```

### 重現單元測試

```bash
pytest
```

測試涵蓋：
- `src/tests/test_query_parser.py` — LLM 解析 + fallback 邏輯
- `src/tests/test_retriever.py` — DuckDB 查詢 + 語意擴展
- `src/tests/test_ranker.py` — 排序邏輯（熱門 / 個人化 / 冷啟動）
- `src/tests/test_pipeline.py` — 端到端整合測試
- `src/utils/test_abbreviations.py` — 縮寫展開（含 Hypothesis 屬性測試）
- `src/utils/test_tag_parser.py` — 標籤分類

### 效能基準參考值

| 階段 | 典型延遲 | 備註 |
|------|----------|------|
| Stage 1 (querytoRequirement) | 1–3s | 受 Bedrock API 延遲影響 |
| Stage 2 (grabFromDatabase) | 50–200ms | DuckDB 查詢 ~1M 列 CSV |
| Stage 3 (ranking) | <10ms | 純記憶體排序 |
| 端到端 | 1–3.5s | 瓶頸在 LLM 呼叫 |

---

## 資料／模型／索引版本說明

### 資料版本

| 資料集 | 時間範圍 | 說明 |
|--------|----------|------|
| 職缺瀏覽 / 主動應徵 / 搜尋紀錄 | 2026-06-01 ~ 2026-06-07 | 一週行為資料 |
| 職缺.csv | 截至 2026-06-07 | 有效職缺快照 |
| 城市對照表 / 職務對照表 | 靜態 | 平台標準代碼表 |

### 模型版本

| 元件 | 模型 / 版本 | 用途 |
|------|-------------|------|
| LLM | `anthropic.claude-3-5-sonnet-20241022-v2:0` (via AWS Bedrock) | 查詢語意解析 |
| 查詢引擎 | DuckDB 1.3.0 | SQL 篩選 ~1M 職缺 |
| 圖引擎 | networkx 3.4.2 | 用戶-職缺互動圖（可選） |

### 排序超參數

| 參數 | 值 | 說明 |
|------|-----|------|
| α (個人化權重) | 0.7 | final = personal×0.7 + popularity×0.3 |
| 地點匹配權重 | 0.4 | personal_score 中的 location 佔比 |
| 職類匹配權重 | 0.4 | personal_score 中的 category 佔比 |
| 薪資匹配權重 | 0.2 | personal_score 中的 salary 佔比 |
| 冷啟動門檻 | 3 | total_events < 3 → fallback to popularity |
| λ (時間衰退) | 0.1 | 約 7 天後分數衰退至 50% |

---

## 專案結構

```
recommendation-algorithm/
├── app.py                        # FastAPI 應用（/search, /recommend, /health）
├── main.py                       # 本機執行入口（debug 模式，含計時）
├── dataset/                      # 原始資料 + 衍生資料 + ETL 腳本
│   ├── genViewCount.py           # 產生瀏覽次數.csv
│   ├── userAnalysis.py           # 產生用戶行為特徵
│   └── README.md                 # 資料表 schema 完整說明
├── src/                          # 核心程式碼
│   ├── pipeline.py               # 三階段 Pipeline 入口 (recommend())
│   ├── query_parser.py           # Stage 1: Bedrock LLM 語意解析
│   ├── retriever.py              # Stage 2: DuckDB 檢索 + 語意擴展
│   ├── ranker.py                 # Stage 3: 排序（熱門 / 個人化）
│   ├── graph_builder.py          # networkx 圖建構（可選）
│   ├── graph_ranker.py           # 圖譜排序邏輯（可選）
│   ├── utils/                    # 工具模組
│   │   ├── abbreviations.py      # 縮寫展開規則
│   │   └── tag_parser.py         # 標籤分類
│   └── tests/                    # 單元與整合測試
├── infra/                        # 部署基礎設施
│   ├── cloudformation.yaml       # AWS CloudFormation 模板
│   ├── deploy-to-ec2.ps1         # PowerShell 部署腳本
│   └── setup-ec2.sh              # EC2 安裝啟動腳本
├── 設計文件/                     # 設計文件（含演算法推導）
├── Dockerfile                    # Docker 容器化（可選）
├── .env                          # 環境變數（git-ignored）
├── requirements.txt              # 依賴版本鎖定
└── deploy.md                     # 部署指南
```

---

## 有圖譜 vs 無圖譜設計決策

本專案同時實作了兩種排序路徑：**無圖譜（特徵匹配）** 與 **有圖譜（networkx 協同過濾）**。透過 `.env` 環境變數切換，預設為無圖譜方案。

### 開關機制

```env
GRAPH_FOR_SIGNED_IN_USER=false   # true → 登入用戶使用圖譜排序
GRAPH_FOR_ANONYMOUS_USER=false   # true → 匿名用戶使用圖譜 degree 排序
```

### 方案比較

| 面向 | 有圖譜 (networkx, 可選) | 無圖譜（預設方案） |
|------|--------------------|--------------------|
| **個人化方式** | 協同過濾（鄰居互動權重） | 特徵向量匹配（city/category/salary） |
| **冷啟動處理** | 需額外邏輯 | 自然 fallback 至熱門排序 |
| **查詢延遲** | 10-100ms | <10ms |
| **記憶體開銷** | ~500MB | <50MB |
| **可解釋性** | 中 | 高（權重公式透明） |

### 決策理由（預設關閉圖譜）

1. 一週行為資料互動密度低，圖推薦效果有限
2. 21.8% 用戶為冷啟動，圖上無法提供有效推薦
3. 圖快取約 500MB，對小型部署造成壓力
4. 需更長時間資料累積才能驗證品質提升

---

## 設計文件

詳細的演算法設計推導與方案評估位於 `設計文件/` 目錄：

- `draft3rewrite.md` — 當前版本設計參考（含 AI 建議）
- `proposal_outline.md` — 比賽提案大綱
- `user_behavior_analysis_proposal.md` — 用戶行為分析方案比較（採納 Option 3）

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
```

| 套件 | 用途 |
|------|------|
| `duckdb` | 高速 SQL 查詢引擎（in-process，查詢 ~1M CSV） |
| `pandas` | 資料載入與預處理 |
| `boto3` | AWS SDK（存取 Bedrock LLM） |
| `python-dotenv` | 環境變數載入 |
| `networkx` | 圖譜建構與查詢（可選功能） |
| `fastapi` | Web API 框架 |
| `uvicorn` | ASGI 伺服器 |
| `hypothesis` | 屬性測試框架 |
