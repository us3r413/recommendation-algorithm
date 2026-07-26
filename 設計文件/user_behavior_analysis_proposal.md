# 使用者行為分析 CSV 設計提案

> 本文件提出三種 `userBehavior.csv`（或多檔）的設計方案，供 `ranking()` 模組的個人化排序使用。
> 所有方案皆由以下三張原始 log 彙整而來：
> - `職缺瀏覽_20260601_20260607.csv`（`organNo`, `employeeNo`, `dateIn`, `talentNo`）
> - `主動應徵_0601-0607.csv`（`LogTitle`, `empNo`, `empName`, `talentNo`, `datein`）
> - `userSearchLog_20260601_20260607.csv`（`talentNo`, `ks`, `c0`, `d0`, `search_time`, `empStr`）

---

## 前置說明：可提取的行為訊號

| 訊號 | 來源欄位 | 強度 |
|------|----------|------|
| 偏好地區 | `userSearchLog.c0` → 城市對照表 | 中（搜尋意圖） |
| 偏好職務類別 | `userSearchLog.d0` → 職務對照表 | 中（搜尋意圖） |
| 搜尋關鍵字 | `userSearchLog.ks` | 低（自由文字，雜訊多） |
| 實際瀏覽職缺 | `職缺瀏覽.employeeNo` → 職缺.csv | 中（瀏覽行為） |
| 主動應徵職缺 | `主動應徵.empNo` → 職缺.csv | 高（轉換行為） |

> **注意：** `talentNo = 0` 代表未登入使用者，所有方案皆需在生成時**濾除** `talentNo = 0` 的資料列，不可將多列 0 視為同一人。

---

## 方案一：寬表（Wide Table）— 單檔彙整特徵

### 概念

每位使用者一列，將行為特徵水平展開為欄位。`ranking()` 模組直接讀取後即可進行相似度評分，無需額外 join。

### CSV 欄位設計

| 欄位 | 型別 | 說明 |
|------|------|------|
| `talentNo` | int | 使用者主鍵（已濾除 0） |
| `top_locations` | str | 最常出現的工作城市，逗號分隔，最多 3 個（依應徵 > 瀏覽 > 搜尋排序） |
| `top_job_categories` | str | 最常出現的職務中類，逗號分隔，最多 3 個 |
| `apply_count` | int | 主動應徵總次數 |
| `view_count` | int | 職缺瀏覽總次數 |
| `search_count` | int | 搜尋總次數 |
| `salary_median` | float | 過去應徵職缺的薪資中位數（來自職缺.csv 的薪資下限） |
| `last_active` | datetime | 最近一次行為時間（三種 log 取最新） |

### 範例資料列

```
talentNo,top_locations,top_job_categories,apply_count,view_count,search_count,salary_median,last_active
28475343,台北市,軟體工程,5,12,3,40000,2026-06-07 18:30:00
53806235,新北市,餐飲服務,2,8,1,28000,2026-06-06 09:15:00
```

### 優點
- 結構簡單，`ranking()` 只需一次 `merge` 即可取得特徵
- 欄位固定，易於維護與除錯

### 缺點
- 特徵資訊已被壓縮（top 3 地區），損失細節
- 薪資中位數可能因應徵職缺少而不可靠

> **AI 建議（方案一）：** 適合作為初始實作版本，生成速度快且 `ranking()` 接介簡單。`salary_median` 若樣本數 < 3 筆，建議以 `null` 填入，`ranking()` 遇到 null 時 fallback 至純熱門度排序，避免冷啟動時的偏差。

---

## 方案二：事件表（Event Log）— 保留原始行為序列

### 概念

不彙整，保留每筆行為的詳細紀錄，每一列代表一個行為事件。`ranking()` 在查詢時動態計算特徵，彈性最高。

### CSV 欄位設計

| 欄位 | 型別 | 說明 |
|------|------|------|
| `talentNo` | int | 使用者主鍵 |
| `event_type` | str | 事件類型：`view` / `apply` / `search` |
| `event_time` | datetime | 行為發生時間 |
| `job_id` | int | 關聯職缺編號（search 事件可能為 null） |
| `job_city` | str | 職缺工作城市（join 自職缺.csv） |
| `job_category_mid` | str | 職缺職務中類（join 自職缺.csv） |
| `salary_lower` | float | 職缺薪資下限（join 自職缺.csv） |
| `search_keyword` | str | 搜尋關鍵字（僅 search 事件有值） |

### 範例資料列

```
talentNo,event_type,event_time,job_id,job_city,job_category_mid,salary_lower,search_keyword
28475343,apply,2026-06-03 14:22:00,79921285,台北市,軟體工程,40000,
28475343,view,2026-06-04 10:05:00,84999076,新北市,藥事醫療,30000,
28475343,search,2026-06-04 10:01:00,,,,, 軟體工程師
```

### 優點
- 完整保留時序資訊，可加入 time decay 等動態計算
- 可做行為序列分析（例：搜尋後 5 分鐘內瀏覽的職缺 = 高度相關）
- 易於後期擴充 Graph RAG（直接轉成邊資料）

### 缺點
- 檔案較大（行數 = 所有行為事件總數）
- `ranking()` 需要做 group-by 計算，查詢較慢

> **AI 建議（方案二）：** 此格式是 Graph RAG 遷移的最佳起點——事件列幾乎可以直接對應 Neo4j 的邊（`talentNo` → `job_id`，邊屬性為 `event_type` 和 `event_time`）。若未來打算導入 Graph，建議採用此方案，避免之後重新 ETL。同時建議對 `talentNo` 和 `event_time` 建立 DuckDB index，讓 `ranking()` 查詢單一使用者的行為時仍能保持毫秒級。

---

## 方案三：雙檔設計（推薦）— 分離特徵表 + 輕量事件表

### 概念

將方案一與方案二的優點結合：
- `userBehaviorFeature.csv`：每人一列的預計算特徵，供 `ranking()` 快速讀取
- `userBehaviorEvents.csv`：精簡的事件 log（只保留應徵與瀏覽，不含搜尋），供進階分析使用

### `userBehaviorFeature.csv` 欄位

| 欄位 | 型別 | 說明 |
|------|------|------|
| `talentNo` | int | 使用者主鍵 |
| `preferred_city_1` | str | 第一偏好城市（依應徵 × 3 + 瀏覽 × 1 加權統計） |
| `preferred_city_2` | str | 第二偏好城市 |
| `preferred_city_3` | str | 第三偏好城市 |
| `preferred_category_1` | str | 第一偏好職務中類 |
| `preferred_category_2` | str | 第二偏好職務中類 |
| `preferred_category_3` | str | 第三偏好職務中類 |
| `salary_floor` | float | 偏好薪資下限（過去應徵職缺薪資下限的第 25 百分位） |
| `total_events` | int | 應徵 + 瀏覽 總事件數（冷啟動判斷用） |
| `last_active` | datetime | 最近行為時間 |

### `userBehaviorEvents.csv` 欄位（精簡版）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `talentNo` | int | 使用者主鍵 |
| `event_type` | str | `view` 或 `apply` |
| `job_id` | int | 職缺編號 |
| `event_time` | datetime | 行為時間 |
| `job_category_mid` | str | 職務中類（已 join） |
| `job_city` | str | 工作城市（已 join） |

### 偏好城市計算方式

```
city_weight(city) = apply_count(city) × 3 + view_count(city) × 1
top_3_cities = sort by city_weight DESC, take top 3
```

### `ranking()` 使用邏輯

```python
# 快速路徑（大多數情況）
feature = userBehaviorFeature[ userBehaviorFeature.talentNo == talent_no ]

if feature.total_events < 3:
    # 冷啟動：直接用熱門度排序
    return sort_by_popularity(candidates)

# 正常路徑：特徵相似度 + 熱門度混合
personal_score = (
    location_match(candidate, feature) × 0.4 +
    category_match(candidate, feature) × 0.4 +
    salary_match(candidate, feature)   × 0.2
)
final_score = personal_score × 0.7 + popularity_score × 0.3
return top_10(sort_by final_score DESC)
```

### 優點
- `ranking()` 只需查 Feature 表（一列），效能與方案一相同
- 事件表保留完整行為序列，方便後期進階分析或 Graph RAG 遷移
- 冷啟動閾值（`total_events < 3`）明確

### 缺點
- 需維護兩份衍生表，生成腳本較複雜

> **AI 建議（方案三）：** `salary_floor` 建議用第 25 百分位而非中位數——求職者通常搜尋薪資下限略高於實際願意接受的水位，第 25 百分位可更保守地估計其真實底線，避免過度過濾低薪職缺。另外，`preferred_city` 欄位可以加入一個 `preferred_city_any` 布林欄（是否願意接受任何城市），處理那些搜尋條件包含全台地區代碼的使用者。

---

## 方案比較

| 比較項目 | 方案一（寬表） | 方案二（事件表） | 方案三（雙檔，推薦） |
|----------|--------------|----------------|-------------------|
| `ranking()` 查詢速度 | ⚡ 最快 | 🐢 需 group-by | ⚡ 快（查 Feature 表） |
| 行為細節保留程度 | 低 | 高 | 中高 |
| 冷啟動處理 | 需額外邏輯 | 需額外邏輯 | 內建（`total_events`） |
| Graph RAG 遷移難度 | 高（需重建） | 低（直接對應邊） | 低（事件表對應邊） |
| 生成腳本複雜度 | 低 | 低 | 中 |
| 比賽初期實作難度 | ✅ 最低 | ✅ 低 | ⚠️ 中 |

---

## 建議實作順序

1. **第一階段**：先實作方案一（寬表），讓 `ranking()` 可快速串接，驗證個人化排序的效果
2. **第二階段**：生成方案三的 `userBehaviorEvents.csv`，為進階分析做準備
3. **第三階段（若有時間）**：合併為完整方案三雙檔，加入冷啟動邏輯

---

## 待決定事項

- [ ] 採用哪個方案（建議方案一起步，方案三為目標）
- [ ] 偏好城市的加權公式（`apply × 3 + view × 1` 是否合理）
- [ ] 冷啟動閾值（`total_events < 3` 是否足夠）
- [ ] `salary_floor` 用第 25 百分位還是中位數
- [ ] 是否需要為每個方案各寫一支 `genUserBehavior.py` 腳本
