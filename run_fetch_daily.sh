#!/bin/bash
# Stock Dashboard - Daily Fetch Wrapper
# launchd에서 호출되는 실행 스크립트

SCRIPT_DIR="/Users/r/Documents/Claude/stock-dashboard"
LOG_FILE="$SCRIPT_DIR/logs/fetch_daily.log"

# Homebrew Python 경로 포함
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:$PATH"

# .env 환경변수 로드
set -a
source "$SCRIPT_DIR/.env"
set +a

echo "========================================" >> "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] fetch_daily 시작" >> "$LOG_FILE"

cd "$SCRIPT_DIR"
python3 pipelines/fetch_daily.py >> "$LOG_FILE" 2>&1

EXIT_CODE=$?
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 완료 (exit: $EXIT_CODE)" >> "$LOG_FILE"
