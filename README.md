# 職缺推薦演算法

根據使用者的搜尋查詢（及登入狀態），從約 100 萬筆職缺中推薦最相關的 10 筆職缺。

## 系統架構

系統採用三階段 Pipeline 設計：

```
使用者查詢 (str)
     ↓
querytoRequirement()   — LLM 語意解析 → 標籤列表
     ↓
grabFromDatabase()     — 語意擴展 + DuckDB SQL 篩選 + JOIN 熱門分數
     ↓
ranking()              — 熱門排序（匿名）或個人化排序（登入用戶）
     ↓
Top 10 職缺
```

### 第一階段：語意解析（querytoRequirement）

- 縮寫展開：規則式替換（例如 `pt` → 兼職、`fe` → 前端工程師、`35k以上` → 薪資>=35000）
- 呼叫 LLM 將自然語言查詢解析為結構化標籤
- 後處理：城市名稱正規化（台北 → 台北市）、薪資格式統一
- 若 LLM 回應格式錯誤，最多重試 3 次後 fallback 為空白分詞

### 第二階段：資料庫檢索（grabFromDatabase）

- 標籤分類：城市 / 薪資門檻 / 職缺屬性（全職/兼職）/ 職務關鍵字
- 透過 `職務對照表.csv` 的 CodeAlike 欄位進行語意擴展（例如：前端工程師 → 軟體工程師、網站程式設計師）
- 使用 DuckDB 對 `職缺.csv`（~100 萬筆）執行參數化 SQL 查詢
- LEFT JOIN `瀏覽次數.csv` 為每筆候選職缺附加熱門分數

### 第三階段：排序（ranking）

- **匿名用戶**（talentNo = 0）：依 `瀏覽次數.score` 熱門度排序
- **登入用戶**（talentNo ≠ 0）：
  - 冷啟動（歷史事件 < 3）：退回熱門排序
  - 正常用戶：`final_score = personal_score × 0.7 + popularity_score × 0.3`
    - `personal_score = 地點匹配 × 0.4 + 職類匹配 × 0.4 + 薪資匹配 × 0.2`

## 使用的 LLM 模型

- **Ollama**（本地推論）
- 預設模型：`llama3`（可在 `.env` 檔案中透過 `OLLAMA_MODEL` 變數覆蓋）
- 建議使用非推理型模型（如 `qwen2.5:7b`、`llama3`），推理型模型（如 `deepseek-r1`）延遲較高
- 用途：將使用者搜尋字串解析為結構化標籤（城市、薪資、職務名稱）
- 若 LLM 不可用或回應格式錯誤，系統會自動 fallback 至規則式解析

## 執行步驟

### 1. 安裝相依套件

```bash
pip install -r requirements.txt
```

主要套件：
- `duckdb` — 高速 SQL 查詢引擎
- `pandas` — 資料處理
- `ollama` — LLM Python SDK
- `python-dotenv` — 環境變數載入（讀取 `.env`）

### 2. 安裝並啟動 Ollama

前往 [ollama.com](https://ollama.com) 下載安裝 Ollama，然後拉取模型：

```bash
ollama pull llama3
```

確認 Ollama 服務正在運行（安裝後預設會自動啟動）。

### 3. 設定環境變數

在專案根目錄建立 `.env` 檔案：

```env
OLLAMA_MODEL=llama3
```

可替換為你已安裝的任何 Ollama 模型（如 `qwen2.5:7b`、`deepseek-r1:7b`）。

### 4. 準備資料集

確保 `dataset/` 資料夾中包含以下原始 CSV 檔案：

- `職缺.csv` — 職缺主表
- `城市對照表.csv` — 城市代碼對照表
- `職務對照表.csv` — 職務類別對照表
- `職缺瀏覽_20260601_20260607.csv` — 瀏覽行為紀錄
- `主動應徵_0601-0607.csv` — 應徵行為紀錄

### 5. 產生衍生資料表

```bash
python dataset/genViewCount.py
python dataset/userAnalysis.py
```

這會產生：
- `dataset/瀏覽次數.csv` — 職缺熱門分數
- `dataset/userBehaviorFeature.csv` — 用戶偏好特徵
- `dataset/userBehaviorEvents.csv` — 用戶行為事件紀錄

### 6. 執行推薦引擎

```bash
python main.py
```

### 7. 自訂查詢

編輯 `main.py` 中的查詢參數：

```python
from src.pipeline import recommend

# 匿名用戶
results = recommend("台北 前端工程師 35k以上", talent_no=0)

# 登入用戶（帶個人化排序）
results = recommend("後端 pt工作", talent_no=12345)
```

## 專案結構

```
recommendation-algorithm/
├── dataset/              # 原始資料 + 衍生資料 + ETL 腳本
│   ├── genViewCount.py   # 產生瀏覽次數.csv
│   └── userAnalysis.py   # 產生用戶行為特徵
├── src/                  # 核心程式碼
│   ├── pipeline.py       # 三階段 Pipeline 入口
│   ├── query_parser.py   # 第一階段：LLM 語意解析
│   ├── retriever.py      # 第二階段：DuckDB 檢索
│   ├── ranker.py         # 第三階段：排序
│   └── utils/            # 工具模組（縮寫展開、標籤分類）
├── main.py               # 執行入口
├── .env                  # 環境變數設定（git-ignored）
├── requirements.txt      # Python 相依套件
└── README.md
```

## 環境變數

設定於 `.env` 檔案（專案根目錄），由 `python-dotenv` 自動載入。

| 變數名稱 | 說明 | 預設值 |
|----------|------|--------|
| `OLLAMA_MODEL` | Ollama 使用的模型名稱 | `llama3` |
