# 部署指南：AWS EC2

將推薦 API 部署到 EC2，讓評審透過公開 endpoint 呼叫。

---

## 方案選擇

| 方案 | 優點 | 缺點 |
|------|------|------|
| **A. 直接跑 (推薦)** | 最快上線、簡單除錯 | 需手動管理 process |
| B. Docker | 環境一致、可移植 | image 很大（含 dataset） |

比賽 demo 建議用方案 A，5 分鐘即可上線。

---

## 前置需求

- AWS 帳號，有權限建立 EC2 instance
- 一組 key pair（`.pem` 檔）用於 SSH
- Security Group 開放 port 8000（或你選的 port）

---

## 方案 A：直接在 EC2 跑（推薦）

### 1. 開 EC2 Instance

- **AMI**: Amazon Linux 2023 或 Ubuntu 22.04
- **Instance type**: `t3.large`（2 vCPU, 8GB RAM）— 1M CSV + pandas 需要足夠記憶體
  - 若啟用 graph：建議 `t3.xlarge`（16GB RAM）
- **Storage**: 30 GB gp3（裝得下 dataset）
- **Security Group**: 開放 inbound TCP port 8000 (source: 0.0.0.0/0)

### 2. SSH 進入 & 安裝環境

```bash
ssh -i your-key.pem ec2-user@<PUBLIC_IP>

# Amazon Linux 2023
sudo dnf install python3.11 python3.11-pip git -y

# Ubuntu 22.04
# sudo apt update && sudo apt install python3.11 python3.11-pip python3.11-venv git -y
```

### 3. 上傳專案

選項一：git clone（如果 repo 是 private，用 deploy key 或 HTTPS token）

```bash
git clone https://github.com/<your-org>/recommendation-algorithm.git
cd recommendation-algorithm
```

選項二：從本機 scp 上傳

```bash
# 在本機執行（Windows PowerShell）
scp -i your-key.pem -r ./* ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/
```

### 4. 上傳 dataset（如果 git-ignored）

```bash
# 在本機執行 — 上傳必要的 CSV 檔案
scp -i your-key.pem dataset/職缺.csv ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/dataset/
scp -i your-key.pem dataset/職務對照表.csv ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/dataset/
scp -i your-key.pem dataset/城市對照表.csv ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/dataset/
scp -i your-key.pem dataset/瀏覽次數.csv ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/dataset/
scp -i your-key.pem dataset/userBehaviorFeature.csv ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/dataset/
scp -i your-key.pem dataset/userBehaviorEvents.csv ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/dataset/

# 可選：上傳 graph cache（~500MB，避免在 server 上重建）
scp -i your-key.pem dataset/graph_cache.pkl ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/dataset/
```

### 5. 安裝依賴 & 設定 .env

```bash
cd ~/recommendation-algorithm
pip3.11 install -r requirements.txt

# 建立 .env（填入你的 AWS 憑證）
cat > .env << 'EOF'
BEDROCK_MODEL_ID=anthropic.claude-3-5-sonnet-20241022-v2:0
AWS_DEFAULT_REGION=us-west-2
AWS_ACCESS_KEY_ID=your-key
AWS_SECRET_ACCESS_KEY=your-secret
AWS_SESSION_TOKEN=your-token
GRAPH_FOR_SIGNED_IN_USER=false
GRAPH_FOR_ANONYMOUS_USER=false
EOF
```

### 6. 啟動 API

```bash
# 前台測試（先確認能跑）
python3.11 -m uvicorn app:app --host 0.0.0.0 --port 8000

# 背景常駐（nohup）
nohup python3.11 -m uvicorn app:app --host 0.0.0.0 --port 8000 > api.log 2>&1 &

# 查看 log
tail -f api.log
```

### 7. 驗證

```bash
# Health check
curl http://<PUBLIC_IP>:8000/health

# 推薦測試
curl -X POST http://<PUBLIC_IP>:8000/recommend \
  -H "Content-Type: application/json" \
  -d '{"query": "台北 前端工程師 35k以上", "talent_no": 0}'
```

---

## 方案 B：Docker 部署（可選）

### 1. 在 EC2 上安裝 Docker

```bash
# Amazon Linux 2023
sudo dnf install docker -y
sudo systemctl start docker
sudo usermod -aG docker ec2-user
# 重新登入讓 group 生效
```

### 2. 上傳專案 + dataset（同方案 A 步驟 3-4）

### 3. Build & Run

```bash
cd ~/recommendation-algorithm
docker build -t job-recommend .
docker run -d --name api -p 8000:8000 job-recommend
docker logs -f api
```

---

## 給評審的 API 文件

### Base URL

```
http://<PUBLIC_IP>:8000
```

### Endpoints

#### `GET /health`

回傳服務狀態。

```json
{"status": "ok"}
```

#### `POST /recommend`

推薦 Top 10 職缺。

**Request Body (JSON):**

| 欄位 | 類型 | 必填 | 說明 |
|------|------|------|------|
| `query` | string | 否 | 搜尋關鍵字（例：`"台北 前端工程師 35k以上"`） |
| `talent_no` | int | 否 | 用戶 ID，0=匿名，非零=登入用戶 |
| `c0` | string[] | 否 | 城市代碼篩選（例：`["100100"]`） |
| `d0` | string[] | 否 | 職務類別代碼篩選（例：`["140214"]`） |

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
      ...
    }
  ],
  "count": 10
}
```

**範例呼叫:**

```bash
# 匿名搜尋
curl -X POST http://<PUBLIC_IP>:8000/recommend \
  -H "Content-Type: application/json" \
  -d '{"query": "台北 前端工程師 35k以上", "talent_no": 0}'

# 登入用戶（個人化排序）
curl -X POST http://<PUBLIC_IP>:8000/recommend \
  -H "Content-Type: application/json" \
  -d '{"query": "行銷企劃", "talent_no": 138}'

# 城市+職務代碼篩選
curl -X POST http://<PUBLIC_IP>:8000/recommend \
  -H "Content-Type: application/json" \
  -d '{"query": "", "talent_no": 0, "c0": ["100100", "100200"], "d0": ["140214"]}'
```

---

## 注意事項

1. **AWS Session Token 過期**：如果用臨時憑證，記得定期更新 `.env` 中的 token 並重啟服務
2. **記憶體**：`t3.large` (8GB) 足夠跑無圖譜模式。啟用圖譜需 `t3.xlarge` (16GB)
3. **首次請求較慢**：第一次呼叫 `/recommend` 時 DuckDB 需載入 CSV，後續會被快取
4. **Security Group**：比賽結束後記得關閉 port 8000 或停止 instance
5. **Interactive docs**：FastAPI 自帶 Swagger UI，瀏覽器開 `http://<PUBLIC_IP>:8000/docs` 即可互動測試
