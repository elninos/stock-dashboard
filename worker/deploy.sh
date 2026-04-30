#!/bin/bash
# Stock Dashboard API — 배포 자동화 스크립트
# 사용법: ./deploy.sh

set -e
WRANGLER="/Users/r/bin/wrangler"
NODE="/Users/r/bin/node"
WORKER_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "================================================"
echo " Stock Dashboard API — 배포 시작"
echo "================================================"

# ── Step 1: 로그인 확인 ───────────────────────────────────────────────
echo ""
echo "[1/5] Cloudflare 로그인 확인..."
if ! $WRANGLER whoami &>/dev/null; then
  echo "→ 브라우저 로그인이 필요합니다."
  $WRANGLER login
else
  echo "→ 이미 로그인됨: $($WRANGLER whoami 2>&1 | grep -oE '[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')"
fi

# ── Step 2: KV namespace 생성 ─────────────────────────────────────────
echo ""
echo "[2/5] KV namespace 확인/생성..."

# 기존 namespace 목록에서 STOCK_KV 찾기
KV_LIST=$($WRANGLER kv namespace list 2>/dev/null || echo "[]")
KV_ID=$(echo "$KV_LIST" | grep -A1 '"title": "STOCK_KV"' | grep '"id"' | sed 's/.*"id": "\([^"]*\)".*/\1/' || true)
KV_PREVIEW_ID=$(echo "$KV_LIST" | grep -A1 '"title": "STOCK_KV_preview"' | grep '"id"' | sed 's/.*"id": "\([^"]*\)".*/\1/' || true)

if [ -z "$KV_ID" ]; then
  echo "→ STOCK_KV 생성 중..."
  OUTPUT=$($WRANGLER kv namespace create "STOCK_KV" 2>&1)
  KV_ID=$(echo "$OUTPUT" | grep -oE '"id":\s*"[^"]+"' | head -1 | grep -oE '"[^"]*"$' | tr -d '"')
  echo "  ID: $KV_ID"
else
  echo "→ STOCK_KV 기존 사용: $KV_ID"
fi

if [ -z "$KV_PREVIEW_ID" ]; then
  echo "→ STOCK_KV_preview 생성 중..."
  OUTPUT=$($WRANGLER kv namespace create "STOCK_KV" --preview 2>&1)
  KV_PREVIEW_ID=$(echo "$OUTPUT" | grep -oE '"id":\s*"[^"]+"' | head -1 | grep -oE '"[^"]*"$' | tr -d '"')
  echo "  Preview ID: $KV_PREVIEW_ID"
else
  echo "→ STOCK_KV_preview 기존 사용: $KV_PREVIEW_ID"
fi

# wrangler.toml에 ID 자동 주입
sed -i '' \
  -e "s|id = \"REPLACE_WITH_KV_NAMESPACE_ID\"|id = \"$KV_ID\"|" \
  -e "s|preview_id = \"REPLACE_WITH_KV_PREVIEW_NAMESPACE_ID\"|preview_id = \"$KV_PREVIEW_ID\"|" \
  "$WORKER_DIR/wrangler.toml"

# 이미 ID가 들어있으면 sed는 변경 없이 통과
echo "→ wrangler.toml 업데이트 완료"

# ── Step 3: API 키 설정 ───────────────────────────────────────────────
echo ""
echo "[3/5] ANTHROPIC_API_KEY 설정..."
echo "→ 키를 입력하세요 (sk-ant-...):"
$WRANGLER secret put ANTHROPIC_API_KEY

# ── Step 4: KV 데이터 업로드 ─────────────────────────────────────────
echo ""
echo "[4/5] KV 데이터 업로드 (stock_map, stock_list)..."
$NODE "$WORKER_DIR/scripts/upload-kv.js"

# ── Step 5: 배포 ──────────────────────────────────────────────────────
echo ""
echo "[5/5] Worker 배포..."
$WRANGLER deploy

# ── 완료 ──────────────────────────────────────────────────────────────
echo ""
echo "================================================"
echo " 배포 완료!"
echo "================================================"
echo ""
echo "Worker URL을 확인하고 아래 명령으로 테스트:"
echo ""
echo "  # 상태 확인"
echo "  curl https://stock-dashboard-api.<subdomain>.workers.dev/api/status"
echo ""
echo "  # 즉시 업데이트 트리거"
echo "  curl -X POST https://stock-dashboard-api.<subdomain>.workers.dev/api/trigger"
echo ""
echo "  # 로그 실시간 확인"
echo "  /Users/r/bin/wrangler tail"
