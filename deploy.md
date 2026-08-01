# 部署指南：AWS EC2

將推薦 API 部署到 EC2，讓評審透過公開 endpoint 呼叫。

---

## 快速部署（自動化腳本）

只需 3 步即可上線：

### 1. 建立 EC2 Instance（CloudFormation 一鍵建立）

```powershell
aws cloudformation create-stack `
  --stack-name job-recommend-api `
  --template-body file://infra/cloudformation.yaml `
  --parameters ParameterKey=KeyPairName,ParameterValue=your-key-pair-name

# 等待建立完成
aws cloudformation wait stack-create-complete --stack-name job-recommend-api

# 取得 Public IP
aws cloudformation describe-stacks --stack-name job-recommend-api `
  --query "Stacks[0].Outputs[?OutputKey=='PublicIP'].OutputValue" --output text
```

### 2. 部署程式碼 + 資料

```powershell
# 首次部署（含 dataset）
.\infra\deploy-to-ec2.ps1 -KeyFile "C:\path\to\your-key.pem" -Host "<PUBLIC_IP>"

# 後續只更新程式碼（跳過 dataset 上傳）
.\infra\deploy-to-ec2.ps1 -KeyFile "C:\path\to\your-key.pem" -Host "<PUBLIC_IP>" -SkipDataset
```

### 3. 驗證

```powershell
# Health check
curl http://<PUBLIC_IP>:8000/health

# 推薦 API
curl -X POST http://<PUBLIC_IP>:8000/recommend `
  -H "Content-Type: application/json" `
  -d '{"query": "台北 前端工程師 35k以上", "talent_no": 0}'
```

完成！瀏覽器開 `http://<PUBLIC_IP>:8000/docs` 可看到 Swagger 互動文件。

---

## 部署檔案說明

```
infra/
├── cloudformation.yaml   # AWS CloudFormation 模板 — 建立 EC2 + Security Group
├── deploy-to-ec2.ps1     # PowerShell 部署腳本 — SCP 上傳程式碼和資料
└── setup-ec2.sh          # EC2 上的安裝啟動腳本 — 安裝依賴、啟動 server
```

---

## 手動部署（不用腳本）

如果偏好手動操作，以下是完整步驟：

### 1. 開 EC2 Instance

- **AMI**: Amazon Linux 2023 或 Ubuntu 22.04
- **Instance type**: `t3.large`（2 vCPU, 8GB RAM）— 1M CSV + pandas 需要足夠記憶體
  - 若啟用 graph：建議 `t3.xlarge`（16GB RAM）
- **Storage**: 30 GB gp3（裝得下 dataset）
- **Security Group**: 開放 inbound TCP port 22 + 8000 (source: 0.0.0.0/0)

### 2. SSH 進入 & 安裝環境

```bash
ssh -i your-key.pem ec2-user@<PUBLIC_IP>

# Amazon Linux 2023
sudo dnf install python3.11 python3.11-pip git -y

# Ubuntu 22.04
# sudo apt update && sudo apt install python3.11 python3.11-pip python3.11-venv git -y
```

### 3. 上傳專案

```bash
# 在本機執行（Windows PowerShell）
scp -i your-key.pem -r ./* ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/
```

### 4. 上傳 dataset（git-ignored 的 CSV）

```bash
scp -i your-key.pem dataset/職缺.csv ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/dataset/
scp -i your-key.pem dataset/職務對照表.csv ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/dataset/
scp -i your-key.pem dataset/城市對照表.csv ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/dataset/
scp -i your-key.pem dataset/瀏覽次數.csv ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/dataset/
scp -i your-key.pem dataset/userBehaviorFeature.csv ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/dataset/
scp -i your-key.pem dataset/userBehaviorEvents.csv ec2-user@<PUBLIC_IP>:~/recommendation-algorithm/dataset/
```

### 5. 安裝依賴 & 設定 .env

```bash
cd ~/recommendation-algorithm
pip3.11 install -r requirements.txt

# 建立 .env（填入 AWS 憑證）
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
# 前台測試
python3.11 -m uvicorn app:app --host 0.0.0.0 --port 8000

# 背景常駐
nohup python3.11 -m uvicorn app:app --host 0.0.0.0 --port 8000 > api.log 2>&1 &
```

---

## API 文件

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
      "職務小類": "前端工程師"
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

## Docker 部署（可選）

如果偏好容器化：

```bash
# 在 EC2 上安裝 Docker
sudo dnf install docker -y
sudo systemctl start docker
sudo usermod -aG docker ec2-user

# Build & Run
cd ~/recommendation-algorithm
docker build -t job-recommend .
docker run -d --name api -p 8000:8000 job-recommend
```

---

## 清理資源

```powershell
# 刪除 CloudFormation stack（會同時刪除 EC2 instance 和 security group）
aws cloudformation delete-stack --stack-name job-recommend-api
```

---

## 注意事項

1. **AWS Session Token 過期**：如果用臨時憑證，記得定期更新 `.env` 中的 token 並重啟服務
2. **記憶體**：`t3.large` (8GB) 足夠跑無圖譜模式。啟用圖譜需 `t3.xlarge` (16GB)
3. **首次請求較慢**：第一次呼叫 `/recommend` 時 DuckDB 需載入 CSV，後續會被快取
4. **Security Group**：比賽結束後記得刪除 stack 或停止 instance
5. **Swagger UI**：瀏覽器開 `http://<PUBLIC_IP>:8000/docs` 即可互動測試
6. **重新部署**：修改程式碼後，用 `deploy-to-ec2.ps1 -SkipDataset` 快速更新
