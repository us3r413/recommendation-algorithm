#!/bin/bash
# setup-ec2.sh — Run on the EC2 instance to install dependencies and start the API server.
#
# This script is idempotent: safe to re-run on redeployment.
# It will stop any existing uvicorn process before restarting.

set -euo pipefail

APP_DIR="$HOME/recommendation-algorithm"
LOG_FILE="$APP_DIR/api.log"
PID_FILE="$APP_DIR/api.pid"

echo "============================================"
echo " EC2 Setup — Job Recommendation API"
echo "============================================"
echo ""

cd "$APP_DIR"

# --- 1. Install Python 3.11 (if not already installed) ---
echo "[1/5] Checking Python 3.11..."
if ! command -v python3.11 &> /dev/null; then
    echo "  Installing Python 3.11..."
    if command -v dnf &> /dev/null; then
        sudo dnf install python3.11 python3.11-pip -y
    elif command -v apt-get &> /dev/null; then
        sudo apt-get update
        sudo apt-get install python3.11 python3.11-pip python3.11-venv -y
    else
        echo "  ERROR: Unsupported package manager. Install Python 3.11 manually."
        exit 1
    fi
else
    echo "  Python 3.11 already installed: $(python3.11 --version)"
fi

# --- 2. Install pip dependencies ---
echo ""
echo "[2/5] Installing pip dependencies..."
python3.11 -m pip install --upgrade pip --quiet
python3.11 -m pip install -r requirements.txt --quiet
echo "  Done."

# --- 3. Verify dataset files exist ---
echo ""
echo "[3/5] Verifying dataset files..."
REQUIRED_FILES=(
    "dataset/職缺.csv"
    "dataset/職務對照表.csv"
    "dataset/城市對照表.csv"
    "dataset/瀏覽次數.csv"
    "dataset/userBehaviorFeature.csv"
    "dataset/userBehaviorEvents.csv"
)

MISSING=0
for f in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$f" ]; then
        echo "  MISSING: $f"
        MISSING=$((MISSING + 1))
    fi
done

if [ $MISSING -gt 0 ]; then
    echo ""
    echo "  WARNING: $MISSING required dataset file(s) missing."
    echo "  The API will fail on first request. Upload them and re-run this script."
    echo ""
fi

# --- 4. Stop existing server (if running) ---
echo "[4/5] Stopping existing server (if any)..."
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "  Stopping PID $OLD_PID..."
        kill "$OLD_PID"
        sleep 2
        # Force kill if still alive
        if kill -0 "$OLD_PID" 2>/dev/null; then
            kill -9 "$OLD_PID"
        fi
        echo "  Stopped."
    else
        echo "  Stale PID file (process not running). Cleaning up."
    fi
    rm -f "$PID_FILE"
else
    # Also try to find and kill any orphaned uvicorn on port 8000
    EXISTING_PID=$(lsof -ti :8000 2>/dev/null || true)
    if [ -n "$EXISTING_PID" ]; then
        echo "  Killing existing process on port 8000 (PID: $EXISTING_PID)..."
        kill "$EXISTING_PID" 2>/dev/null || true
        sleep 2
    else
        echo "  No existing server found."
    fi
fi

# --- 5. Start the API server ---
echo ""
echo "[5/5] Starting API server..."
nohup python3.11 -m uvicorn app:app --host 0.0.0.0 --port 8000 > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
echo "  Server started (PID: $NEW_PID)"
echo "  Log file: $LOG_FILE"

# Wait a moment and verify it's running
sleep 3
if kill -0 "$NEW_PID" 2>/dev/null; then
    echo ""
    echo "============================================"
    echo " Server is running!"
    echo "============================================"
    echo ""
    echo " API:     http://$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo '<PUBLIC_IP>'):8000"
    echo " Health:  /health"
    echo " Docs:    /docs"
    echo ""
else
    echo ""
    echo "  ERROR: Server failed to start. Check log:"
    echo ""
    tail -20 "$LOG_FILE"
    exit 1
fi
