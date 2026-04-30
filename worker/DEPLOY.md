# Stock Dashboard API — Cloudflare Worker 배포 가이드

## 구조

```
worker/
├── src/
│   ├── index.js      # 메인 Worker (fetch + scheduled 핸들러)
│   ├── briefing.js   # Telegram/Blog 수집 + Claude 요약
│   ├── news.js       # Google News 수집 + Claude 요약
│   ├── prices.js     # 주가 + 환율 수집
│   └── utils.js      # stripHtml, callClaude 유틸
├── scripts/
│   └── upload-kv.js  # stock_map + stock_list → KV 업로드
├── wrangler.toml
└── package.json
```

## 최초 배포 절차

### 1. Wrangler 설치 & 로그인

```bash
cd worker
npm install
npx wrangler login
```

### 2. KV namespace 생성

```bash
npx wrangler kv namespace create "STOCK_KV"
# → { id: "abc123..." } 출력

# 개발/preview용 (선택)
npx wrangler kv namespace create "STOCK_KV" --preview
# → { id: "xyz789..." } 출력
```

출력된 ID를 `wrangler.toml`에 입력:

```toml
[[kv_namespaces]]
binding = "KV"
id = "abc123..."           # 위에서 복사
preview_id = "xyz789..."   # preview용 (없으면 id와 동일하게)
```

### 3. Anthropic API 키 설정

```bash
npx wrangler secret put ANTHROPIC_API_KEY
# 프롬프트에 키 입력 (sk-ant-...)
```

### 4. KV 초기 데이터 업로드

프로젝트 루트에 `stock_map.json`과 `transactions.json`이 있어야 합니다.

```bash
node scripts/upload-kv.js
# stock_map (종목 코드/국가 정보) + stock_list (현재 보유 종목) 업로드
```

### 5. 배포

```bash
npm run deploy
# 또는
npx wrangler deploy
```

### 6. 동작 확인

```bash
# Worker URL 확인 후 테스트
WORKER_URL="https://stock-dashboard-api.<your-subdomain>.workers.dev"

# 상태 확인
curl $WORKER_URL/api/status

# 수동 업데이트 트리거
curl -X POST $WORKER_URL/api/trigger

# 잠시 후 데이터 확인 (Claude 요약 포함 ~2-3분 소요)
curl $WORKER_URL/api/prices | jq .
curl $WORKER_URL/api/news | jq .
curl $WORKER_URL/api/briefing | jq .
```

## 이후 업데이트

### 코드 변경 후 재배포

```bash
npm run deploy
```

### stock_map / 보유 종목 변경 후

```bash
node scripts/upload-kv.js
curl -X POST $WORKER_URL/api/trigger   # 즉시 갱신
```

## Cron 스케줄

`wrangler.toml`의 `crons = ["0 */2 * * *"]` → 매 2시간 정각 자동 실행 (UTC 기준)

KST 기준: 09:00, 11:00, 13:00, 15:00, 17:00, 19:00, 21:00, 23:00, 01:00, 03:00, 05:00, 07:00

## 로그 확인

```bash
npx wrangler tail
```

## 환경 변수 / Secrets 목록

| 이름 | 종류 | 설명 |
|------|------|------|
| `ANTHROPIC_API_KEY` | Secret | Claude API 키 |
| `KV` | KV Binding | Workers KV namespace |

## KV 키 목록

| 키 | 내용 |
|----|------|
| `stock_map` | `{ 종목명: { code, nation, market } }` |
| `stock_list` | `["삼성전자", "SK하이닉스", ...]` |
| `prices` | 최신 주가 + 환율 |
| `briefing` | Claude 브리핑 요약 (daily/weekly/biweekly/monthly) |
| `briefing_raw` | 수집된 원본 포스트 (30일치) |
| `news` | Claude 뉴스 요약 |
| `news_raw` | 수집된 원본 뉴스 기사 |
| `_status` | 마지막 업데이트 상태 |
