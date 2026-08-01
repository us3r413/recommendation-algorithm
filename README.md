# 職缺推薦演算法

根據使用者的搜尋查詢（及登入狀態），從約 100 萬筆職缺中推薦最相關的 10 筆職缺。

---

## 目錄

- [系統架構](#系統架構)
- [資料流程](#資料流程)
- [環境設定](#環境設定)
- [資料集準備](#資料集準備)
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
│   使用者查詢 (str) + talentNo + [c0] + [d0]                         │
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
│          Top 10 職缺結果                                             │
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

- **匿名用戶**（talentNo = 0）：
  - `GRAPH_FOR_ANONYMOUS_USER=false`（預設）：依 `瀏覽次數.score` 熱門度排序
  - `GRAPH_FOR_ANONYMOUS_USER=true`：使用 networkx 圖譜 degree 排序
- **登入用戶**（talentNo ≠ 0）：
  - `GRAPH_FOR_SIGNED_IN_USER=true`：使用 networkx 圖譜協同過濾排序
  - `GRAPH_FOR_SIGNED_IN_USER=false`（預設）：
    - 冷啟動（歷史事件 < 3）：退回熱門排序
    - 正常用戶：`final_score = personal_score × 0.7 + popularity_score × 0.3`
      - `personal_score = 地點匹配 × 0.4 + 職類匹配 × 0.4 + 薪資匹配 × 0.2`

> 圖譜功能預設關閉。啟用時需先建構圖譜快取：`python -m src.graph_builder`

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
HTTP Request / CLI
  ├── query: "台北 前端工程師 35k以上"
  ├── talentNo: 12345
  ├── c0: ["100100"]  (optional, from search log)
  └── d0: ["140214"]  (optional, from search log)
        │
        ▼
  pipeline.recommend(query, talent_no, c0, d0)
        │
        ├── [1] querytoRequirement(query)
        │       → ["前端工程師", "台北市", "薪資>=35000"]
        │
        ├── [2] grabFromDatabase(tags, c0, d0)
        │       → N candidates with score
        │
        └── [3] ranking(candidates, talent_no)
                → Top 10 results
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

## 執行範例

### 基本使用

```bash
python main.py
```

`main.py` 包含多個 debug 範例（匿名 + 登入用戶），會印出每階段耗時與 Top 10 結果。登入用戶會額外印出用戶歷史紀錄（偏好城市、偏好職類、近期事件）：

```
============================================================
Query: '台北 前端工程師'  |  talent_no: 138
============================================================

[Stage 1] querytoRequirement → tags:  (1.41s)
  ['台北市', '前端工程師']

[Stage 2] grabFromDatabase → 1008 candidates  (1.20s)

[Stage 3] ranking → 10 results  (0.36s)

  Total: 2.97s

[User History] talentNo=138
  total_events: 9  |  cold_start: False  |  last_active: 2026-06-03 19:57:20.167
  preferred_cities: ['台中市']
  preferred_categories: ['行政後勤／總務', '業務推廣', '財務會計']
  salary_floor: N/A
  recent_events (last 9):
    view   job=77037910  city=台中市  cat=業務推廣  time=2026-06-03 19:57:20.167
    ...

   1. 軟體工程師
      城市: 台北市  薪資下限: 35000  職務小類: 軟體工程師
   2. ...
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

# 登入用戶 + c0/d0
results = recommend("遠端", talent_no=53213129, c0=["100100", "100200"], d0=["160213"])
```

---

## Benchmark 重現步驟

### 前置條件

1. 完成「環境設定」和「資料集準備」章節中的所有步驟
2. 確保 `.env` 中的 AWS 憑證有效

### 重現 Pipeline 端到端效能測試

```bash
# 1. 安裝相依套件
pip install -r requirements.txt

# 2. 產生衍生資料表（若尚未產生）
python dataset/genViewCount.py
python dataset/userAnalysis.py

# 3. 執行 main.py（含計時輸出）
python main.py
```

`main.py` 會印出每個階段的執行時間，可用於量測：
- Stage 1 (LLM 解析) 延遲
- Stage 2 (DuckDB 查詢) 延遲
- Stage 3 (排序) 延遲
- 端到端總延遲

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

> 注意：Stage 1 若 LLM 不可用會 fallback 至規則式解析，此時延遲 <10ms。

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

### 索引與衍生資料版本

| 索引 / 衍生表 | 產生方式 | 關鍵參數 |
|---------------|----------|----------|
| `瀏覽次數.csv` | `genViewCount.py` | λ=0.1（時間衰退）, apply=3, view=1 |
| `userBehaviorFeature.csv` | `userAnalysis.py` | top-3 cities/categories, cold-start threshold=3 |
| `graph_cache.pkl` | `python -m src.graph_builder` | 邊權重: apply=3, view=1; 441K nodes, 3.2M edges |
| 語意擴展索引 | `職務對照表.csv` CodeAlike 欄位 | 規則式，無需訓練 |

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
├── dataset/                      # 原始資料 + 衍生資料 + ETL 腳本
│   ├── genViewCount.py           # 產生瀏覽次數.csv
│   ├── userAnalysis.py           # 產生用戶行為特徵
│   └── README.md                 # 資料表 schema 完整說明
├── src/                          # 核心程式碼
│   ├── __init__.py
│   ├── pipeline.py               # 三階段 Pipeline 入口 (recommend())
│   ├── query_parser.py           # Stage 1: Bedrock LLM 語意解析
│   ├── retriever.py              # Stage 2: DuckDB 檢索 + 語意擴展
│   ├── ranker.py                 # Stage 3: 排序（熱門 / 個人化）
│   ├── graph_builder.py          # networkx 用戶-職缺互動圖建構（可選）
│   ├── graph_ranker.py           # 圖譜排序邏輯（可選，由 .env 開關控制）
│   ├── utils/                    # 工具模組
│   │   ├── abbreviations.py      # 縮寫展開規則 (13 組對照)
│   │   ├── tag_parser.py         # 標籤分類（城市/薪資/職類/關鍵字）
│   │   ├── test_abbreviations.py # 縮寫展開測試
│   │   └── test_tag_parser.py    # 標籤分類測試
│   └── tests/                    # 整合與單元測試
│       ├── test_pipeline.py
│       ├── test_query_parser.py
│       ├── test_ranker.py
│       └── test_retriever.py
├── 設計文件/                     # 設計文件（含演算法推導）
│   ├── draft3rewrite.md          # 當前版本設計參考
│   └── ...                       # 歷史版本草稿
├── main.py                       # 執行入口（debug 模式，含計時 + 用戶歷史）
├── conftest.py                   # pytest 共用設定
├── .env                          # 環境變數（git-ignored）
├── requirements.txt              # 依賴版本鎖定
└── README.md                     # 本檔案
```

---

## 有圖譜 vs 無圖譜設計決策

本專案同時實作了兩種排序路徑：**無圖譜（特徵匹配）** 與 **有圖譜（networkx 協同過濾）**。透過 `.env` 環境變數切換，預設為無圖譜方案。

### 圖譜模組

- `src/graph_builder.py`：讀取 `userBehaviorEvents.csv` + `職缺.csv`，建構 networkx DiGraph（用戶→職缺，邊權重 = apply×3 + view×1）。首次建構約 4-5 分鐘，之後序列化為 `dataset/graph_cache.pkl`（~5 秒載入）。
- `src/graph_ranker.py`：基於圖譜的排序邏輯（協同過濾 / degree 排序）

### 開關機制

```env
# .env
GRAPH_FOR_SIGNED_IN_USER=false   # true → 登入用戶使用圖譜排序
GRAPH_FOR_ANONYMOUS_USER=false   # true → 匿名用戶使用圖譜 degree 排序
```

當兩者皆為 `false` 時，圖譜完全不會載入（零額外開銷）。

### 方案比較

| 面向 | 有圖譜 (networkx, 可選) | 無圖譜（預設方案） |
|------|--------------------|--------------------|
| **架構** | networkx 用戶-職缺互動圖（in-process） | 預計算特徵表 + DuckDB SQL |
| **個人化方式** | 協同過濾（鄰居互動權重） | 特徵向量匹配（city/category/salary） |
| **冷啟動處理** | 需額外邏輯（圖中無邊） | 自然 fallback 至熱門排序 |
| **查詢延遲** | 10-100ms（in-process 圖查詢） | <10ms（記憶體排序） |
| **記憶體開銷** | ~500MB（圖快取載入） | <50MB（特徵表） |
| **啟用方式** | `.env` 設定 + `python -m src.graph_builder` | 預設啟用 |
| **可解釋性** | 中（鄰居路徑可追蹤） | 高（權重公式透明） |

### 決策理由（預設關閉圖譜）

1. **資料稀疏性**：一週行為資料（166,539 用戶 × 1M 職缺）互動密度極低，圖推薦效果有限
2. **冷啟動比例高**：36,174/166,539 (21.8%) 用戶為冷啟動，圖上無法提供有效推薦
3. **記憶體成本**：圖快取約 500MB，載入需 ~5 秒，對小型部署造成壓力
4. **效果未驗證**：需更長時間資料累積才能驗證圖方案的推薦品質提升

> 結論：兩種路徑皆已實作，透過 `.env` 開關切換。預設使用無圖譜方案，待資料累積或 A/B 測試後再決定是否開啟。

### 效能比較

| 指標 | 無圖譜（預設） | 有圖譜（啟用時） | 差異 |
|------|---------------|---------------|------|
| 圖載入時間 | 0s | ~5s（從 pickle） | 首次啟動成本 |
| Stage 3 延遲 | <10ms | 10-100ms | 圖查詢開銷 |
| 記憶體佔用 | ~50MB | ~500MB | 圖快取大小 |
| 正常用戶命中率 | 基於 top-3 偏好匹配 | 協同過濾探索更多候選 | 圖方案理論上更佳 |
| 冷啟動用戶 | fallback 熱門排序 | 同 | 無差異 |

---

## 設計文件

詳細的演算法設計推導與方案評估位於 `設計文件/` 目錄：

- `draft3rewrite.md` — 當前版本設計參考（含 AI 建議）
- `user_behavior_analysis_proposal.md` — 用戶行為分析方案比較（採納 Option 3）
- `draft.md` / `draft2.md` / `draft2rewrite.md` / `draft3.md` — 歷史版本

---

## 依賴版本

所有依賴已鎖定於 `requirements.txt`：

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
| `python-dotenv` | 環境變數載入（讀取 `.env`） |
| `networkx` | 圖譜建構與查詢（用戶-職缺互動圖，可選功能） |
| `fastapi` | Web API 框架 |
| `uvicorn` | ASGI 伺服器（搭配 FastAPI 使用） |
| `hypothesis` | 屬性測試框架 |
