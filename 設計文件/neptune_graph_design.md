# Neptune 能力知識圖譜設計

## 概述

使用 Amazon Neptune 建構以「能力/技能」為核心的知識圖譜，取代原有的 user-job 二部圖。
LLM 負責從職缺描述中萃取結構化技能，圖譜記錄職缺所需技能、所在城市、以及使用者的互動行為。

查詢時透過 Gremlin 遍歷圖譜，實現：
- 「擁有技能 X 且偏好城市 Y 的人應徵了職缺 A，而職缺 B 也需要類似技能且在類似地點」→ 推薦職缺 B

---

## 圖譜 Schema

### Vertex Labels

| Label | 說明 | 核心屬性 |
|-------|------|----------|
| `Job` | 職缺 | `jobId` (int), `title` (str), `city` (str), `categoryMid` (str), `salaryLower` (float) |
| `Skill` | 技能/能力 | `name` (str), `source` (str: "structured" \| "llm") |
| `City` | 城市 | `name` (str) |
| `Category` | 職務中類 | `name` (str) |
| `User` | 使用者 | `talentNo` (int) |

### Edge Labels

| Label | From → To | 說明 | 屬性 |
|-------|-----------|------|------|
| `REQUIRES` | Job → Skill | 職缺需要此技能 | `confidence` (float, 0-1) |
| `LOCATED_IN` | Job → City | 職缺所在城市 | — |
| `IN_CATEGORY` | Job → Category | 職缺所屬職務中類 | — |
| `VIEWED` | User → Job | 使用者瀏覽過此職缺 | `weight` (int), `lastTime` (str) |
| `APPLIED` | User → Job | 使用者應徵過此職缺 | `weight` (int), `lastTime` (str) |
| `PREFERS_CITY` | User → City | 使用者偏好城市（推導自歷史） | `strength` (float) |
| `HAS_SKILL` | User → Skill | 使用者具備此技能（推導自應徵的職缺） | `strength` (float) |

### 隱含關係（查詢時動態遍歷，不存邊）

| 關係 | 遍歷路徑 | 說明 |
|------|----------|------|
| Job 相似（技能） | Job→Skill←Job | 共享技能的職缺互為相似 |
| Job 相似（地點） | Job→City←Job | 同城市的職缺互為相似 |
| User 相似 | User→Job←User | 互動相同職缺的使用者互為相似 |
| User 適合 Job | User→Skill←Job | 使用者技能與職缺需求匹配 |

---

## 混合技能萃取策略 (Hybrid Extraction)

### 優先級

1. **結構化欄位**（免費、即時）：
   - `電腦技能資料` → 逗號分隔，直接拆分為 Skill 節點
   - `工作技能` → 逗號分隔，直接拆分為 Skill 節點
   - `專業證照` → 逗號分隔，直接拆分為 Skill 節點
   - `confidence = 1.0`（來源明確）

2. **LLM 萃取**（針對三欄位皆為空的職缺）：
   - 輸入：`職務名稱` + `職務內容`（截斷至 500 字）
   - 輸出：最多 5 個技能標籤
   - `confidence = 0.8`（LLM 推斷）
   - 使用 Bedrock Claude，批次處理

### 規模估算

| 類型 | 數量 | 處理方式 |
|------|------|----------|
| 有結構化欄位的職缺 | ~382,758 | 直接解析，無成本 |
| 需 LLM 萃取的職缺 | ~835,877 | 批次 LLM，但僅處理有互動的（~288K 中扣除有結構化的） |
| 實際需 LLM 的職缺 | 估計 ~150K-200K | Bedrock 批次處理 |

### LLM Prompt 模板

```
你是職缺技能萃取器。根據以下職缺標題和內容，提取最多5個核心技能/能力標籤。

職缺標題：{title}
職缺內容：{content[:500]}

回傳格式（JSON array）：["技能1", "技能2", ...]

規則：
- 只輸出具體、可量化的技能（如 Python、專案管理、會計）
- 不要輸出軟實力（如 團隊合作、溝通能力）
- 不要輸出職稱本身
- 如果無法判斷具體技能，回傳空陣列 []
```

---

## User 節點屬性推導

### HAS_SKILL 邊

使用者的技能從其**應徵**過的職缺推導：
```
User --APPLIED--> Job --REQUIRES--> Skill
→ 為 User 建立 HAS_SKILL 邊到該 Skill
→ strength = 應徵次數中包含該 Skill 的比例
```

### PREFERS_CITY 邊

從 `userBehaviorFeature.csv` 的 `preferred_city_1/2/3` 直接建立：
```
User --PREFERS_CITY--> City
→ strength = 1.0 / 0.7 / 0.4（依排名遞減）
```

---

## 查詢時 Gremlin 遍歷策略

### 已登入使用者推薦

```gremlin
// 找到使用者的技能 → 需要相同技能的候選職缺 → 加分
g.V().has('User', 'talentNo', TALENT_NO)
  .out('HAS_SKILL').as('userSkills')
  .in('REQUIRES').has('Job', 'jobId', within(CANDIDATE_IDS))
  .group().by('jobId').by(count())
  .order(local).by(values, desc)
  .limit(local, 10)
```

### 匿名使用者推薦（基於查詢技能）

```gremlin
// 從查詢標籤對應的 Skill → 需要這些技能的職缺 → 排序
g.V().has('Skill', 'name', within(QUERY_SKILLS))
  .in('REQUIRES').has('Job', 'jobId', within(CANDIDATE_IDS))
  .group().by('jobId').by(count())
  .order(local).by(values, desc)
  .limit(local, 10)
```

---

## 分數公式

### 已登入使用者

```
graph_score = skill_overlap × 0.5 + city_match × 0.3 + co_user_signal × 0.2

final_score = graph_score × 0.7 + normalised_popularity × 0.3
```

- `skill_overlap`：使用者技能與職缺技能的重疊數 / 職缺技能總數
- `city_match`：1.0 if 職缺城市 ∈ 使用者偏好城市 else 0.0
- `co_user_signal`：有多少相似使用者（共享技能）也互動了此職缺（正規化後）

### 匿名使用者

```
graph_score = query_skill_overlap × 0.7 + city_match × 0.3

final_score = graph_score × 0.7 + normalised_popularity × 0.3
```

---

## 本地開發 Fallback

當 `USE_NEPTUNE=false`（預設）時：
- 使用 networkx 在本地建構相同 schema 的圖
- 相同的遍歷邏輯，但用 networkx API 實作
- 資料同樣從 CSV + LLM 萃取結果載入

當 `USE_NEPTUNE=true` 時：
- 連線至 Neptune endpoint
- 透過 gremlinpython 執行 Gremlin 查詢
- IAM SigV4 認證

---

## 環境變數

```env
# Neptune 連線
NEPTUNE_ENDPOINT=your-neptune-cluster.cluster-xxxxx.us-west-2.neptune.amazonaws.com
NEPTUNE_PORT=8182
USE_NEPTUNE=false

# Graph ranking toggle
GRAPH_FOR_SIGNED_IN_USER=true
GRAPH_FOR_ANONYMOUS_USER=true
```

---

## 檔案結構

```
src/
├── neptune_client.py      # Neptune 連線管理 + Gremlin 查詢封裝
├── graph_builder.py       # 混合萃取 + 圖譜建構（Neptune or networkx）
├── graph_ranker.py        # Gremlin 遍歷推薦邏輯（含 networkx fallback）
├── skill_extractor.py     # LLM 技能萃取模組
└── ...
```
