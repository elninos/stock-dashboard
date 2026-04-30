# CODEBASE.md — 구현 관행 및 캐시 규칙

## 프로젝트 루트
- 루트 폴더: `stock-analysis/`
- 모든 상대 경로는 이 루트를 기준으로 해석한다.
- `fetch_daily.py` 내 루트 계산: `ROOT = Path(__file__).resolve().parent.parent`

## 캐시 규칙

### 유효기간
| 데이터 종류 | 유효기간 | 파일명 |
|------------|---------|--------|
| 일봉 OHLCV | 1일 (장 마감 후 갱신) | `{ticker}_ohlcv.csv` |
| 기술적 지표 | 1일 | `{ticker}_technical.json` |
| 수급 (외인·기관) | 1일 | `{ticker}_supply.json` |
| 공매도·대차잔고 | 1일 | `{ticker}_short.json` |
| 거래원·기관 창구 | 1일 | `{ticker}_broker.json` |
| 재무·실적 | 7일 (분기 업데이트) | `{ticker}_financial.json` |
| 시장환경·상대강도 | 1일 | `{ticker}_market.json` |
| 종목 기본정보 | 7일 | `{ticker}_info.json` |
| 뉴스 | 1일 | `{ticker}_news.json` |

모든 파일 위치: `data/kis_cache/`

### 캐시 히트 판정
- 파일 존재 + `last_updated` 필드가 유효기간 이내 → **캐시 히트** → 로컬 데이터 사용
- 파일 없거나 만료 → **캐시 미스** → `fetch_daily.py` 실행

### 캐시 미스 시 처리 순서
1. `pipelines/fetch_daily.py --ticker {ticker}` 실행
2. 스크립트 실패 또는 KIS 인증 없으면 → 웹 검색으로 보완
3. 웹 검색 데이터는 캐시에 저장하지 않음 (휘발성 처리)

---

## 파일 형식 규칙

### OHLCV CSV (`{ticker}_ohlcv.csv`)
```csv
date,open,high,low,close,volume
2026-04-28,12500,12800,12300,12650,1234567
```
- 날짜: `YYYY-MM-DD` / 가격: 원화 정수 / 컬럼 순서 고정

---

### 기술적 지표 JSON (`{ticker}_technical.json`)
```json
{
  "ticker": "010170",
  "last_updated": "2026-04-28T18:00:00",
  "date": "2026-04-28",
  "ma5": 12400, "ma20": 11800, "ma60": 10500, "ma120": 9800,
  "rsi14": 62.5,
  "macd": 320, "macd_signal": 280, "macd_hist": 40,
  "bb_upper": 13200, "bb_mid": 11800, "bb_lower": 10400,
  "vol_ratio_5d": 1.85
}
```

---

### 수급 JSON (`{ticker}_supply.json`)
```json
{
  "ticker": "010170",
  "last_updated": "2026-04-28T18:00:00",
  "recent_20d": [
    {
      "date": "2026-04-28",
      "foreign_net": 1500000000,
      "institution_net": -300000000,
      "individual_net": -1200000000
    }
  ],
  "summary": {
    "foreign_20d_cumul": 12000000000,
    "institution_20d_cumul": -2000000000,
    "trend": "외인 매수 우위"
  }
}
```
- 금액 단위: 원화 정수 (float 사용 금지)

---

### 공매도 JSON (`{ticker}_short.json`)
```json
{
  "ticker": "010170",
  "last_updated": "2026-04-28T18:00:00",
  "recent_20d": [
    {
      "date": "2026-04-28",
      "short_volume": 45000,
      "short_ratio": 3.2,
      "short_balance": 980000,
      "short_balance_ratio": 0.8,
      "margin_balance": 1200000
    }
  ],
  "alert": false
}
```
- `short_ratio`: 당일 거래량 대비 공매도 비율 (%)
- `short_balance_ratio`: 상장주식수 대비 공매도 잔고 비율 (%)
- `alert`: 전일 대비 잔고 20% 이상 증가 시 `true`

---

### 거래원 JSON (`{ticker}_broker.json`)
```json
{
  "ticker": "010170",
  "last_updated": "2026-04-28T18:00:00",
  "date": "2026-04-28",
  "top_buy": [
    {"broker": "미래에셋", "amount": 3200000000},
    {"broker": "키움", "amount": 1800000000}
  ],
  "top_sell": [
    {"broker": "삼성", "amount": 2100000000}
  ],
  "foreign_desk_concentration": true,
  "program_buy_ratio": 12.5
}
```

---

### 재무·실적 JSON (`{ticker}_financial.json`)
```json
{
  "ticker": "010170",
  "last_updated": "2026-04-28T18:00:00",
  "quarters": [
    {
      "period": "2026Q1",
      "revenue": 85000000000,
      "op_profit": 12000000000,
      "net_profit": 9500000000,
      "yoy_rev": 23.5,
      "qoq_rev": 8.2
    }
  ],
  "valuation": {
    "per": 18.5,
    "pbr": 2.1,
    "roe": 14.3,
    "sector_per_avg": 22.0
  },
  "next_earnings_date": "2026-07-25",
  "earnings_surprise_history": [
    {"period": "2026Q1", "actual_vs_estimate": 12.3}
  ]
}
```

---

### 시장환경 JSON (`{ticker}_market.json`)
```json
{
  "ticker": "010170",
  "last_updated": "2026-04-28T18:00:00",
  "rs_vs_kospi_1m": 8.5,
  "rs_vs_sector_1m": 3.2,
  "sector_index_trend": "상승",
  "peer_performance": "독주",
  "macro": {
    "kospi": -0.3,
    "usd_krw": 1380,
    "us_10y": 4.25
  }
}
```

---

### 종목 기본정보 JSON (`{ticker}_info.json`)
```json
{
  "ticker": "010170",
  "name": "대한광통신",
  "market": "KOSPI",
  "sector": "통신장비",
  "shares_outstanding": 12500000,
  "last_updated": "2026-04-28T18:00:00"
}
```

---

### 뉴스 캐시 JSON (`{ticker}_news.json`)
```json
{
  "ticker": "010170",
  "last_updated": "2026-04-28T18:00:00",
  "articles": [
    {
      "date": "2026-04-28",
      "title": "기사 제목",
      "source": "출처",
      "summary": "요약"
    }
  ]
}
```

---

## pipelines/fetch_daily.py 사용법

```bash
# 단일 종목 fetch
python pipelines/fetch_daily.py --ticker 010170

# 전체 종목 일괄 fetch (profiles/ 폴더 기준)
python pipelines/fetch_daily.py --all

# 강제 갱신 (캐시 무시)
python pipelines/fetch_daily.py --ticker 010170 --force

# 특정 데이터만 fetch
python pipelines/fetch_daily.py --ticker 010170 --type supply
python pipelines/fetch_daily.py --ticker 010170 --type short
python pipelines/fetch_daily.py --ticker 010170 --type broker
```

> ⚠️ KIS API 인증 필요: `pipelines/config.py` 또는 환경변수에 `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO` 설정

---

## 보고서 파일명 규칙

```
YYYYMMDD_{종목명}_{티커}.md
YYYYMMDD_{종목명}_{티커}.json
YYYYMMDD_{종목명}_{티커}_viewer.html
```

- 종목명 특수문자·공백 → 언더스코어로 치환
- 동일 일자 중복 생성 시: `_02`, `_03` 순번 추가
- 요약/상세 구분 시: `_summary`, `_full` 접미사

**예시:** `20260428_대한광통신_010170.md`

---

## 코드 관행

- Python 3.10+ 기준
- KIS API: `requests` 라이브러리 사용
- 날짜: `YYYY-MM-DD` 문자열 또는 `datetime.date` 통일
- 금액: 원화 정수 (float 사용 금지)
- 비율: 소수점 1자리 float (`3.2` 형식)
- 로그: `pipelines/logs/YYYYMMDD.log`
- 예외 처리: API 실패 시 stale 캐시 사용 가능 (파일에 `"stale": true` 표기)

---

## 디렉토리 확장 규칙

- `analysis/reports/` 월 30개 초과 시 → `analysis/reports/YYYY/MM/` 구조 전환
- `data/kis_cache/` 90일 초과 파일 → 분기별 정리 권장
