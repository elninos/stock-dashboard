# ARCHITECTURE.md — 구조 및 데이터 흐름

## 폴더 구조

```
stock-analysis/
├── CLAUDE.md                        # 공통 분석 규칙 (최우선 참조)
├── ARCHITECTURE.md                  # 구조 및 데이터 흐름 (이 파일)
├── CODEBASE.md                      # 구현 관행 및 캐시 규칙
│
├── profiles/                        # 종목별 보정 파일
│   └── {ticker}.md                  # 예: 010170.md, 005930.md
│
├── data/
│   └── kis_cache/                   # KIS API 로컬 캐시
│       ├── {ticker}_ohlcv.csv       # 일봉 OHLCV (가격·거래량)
│       ├── {ticker}_technical.json  # 기술적 지표 (이동평균·RSI·MACD 등)
│       ├── {ticker}_supply.json     # 수급 (외인·기관·개인 순매수)
│       ├── {ticker}_short.json      # 공매도 잔고·비율·대차잔고
│       ├── {ticker}_broker.json     # 거래원·기관 창구별 흐름
│       ├── {ticker}_financial.json  # 재무·실적 (분기 매출·이익·밸류)
│       ├── {ticker}_market.json     # 시장환경·상대강도 (옵션)
│       ├── {ticker}_info.json       # 종목 기본정보
│       └── {ticker}_news.json       # 뉴스 캐시
│
├── pipelines/
│   ├── fetch_daily.py               # KIS API daily fetch 메인 스크립트
│   ├── config.py                    # KIS API 인증 설정 (gitignore 권장)
│   └── logs/                        # fetch 실행 로그
│
└── analysis/
    └── reports/                     # 분석 보고서 산출물
        ├── YYYYMMDD_{name}_{ticker}.md
        ├── YYYYMMDD_{name}_{ticker}.json
        └── YYYYMMDD_{name}_{ticker}_viewer.html
```

---

## 데이터 흐름

```
[CLAUDE.md + ARCHITECTURE.md + CODEBASE.md]  ← 분석 시작 전 반드시 로드
                    ↓
       [profiles/{ticker}.md]                 ← 종목 보정 규칙 (최우선)
                    ↓
       [data/kis_cache/]                      ← 로컬 캐시 (8종 파일)
        - _ohlcv.csv       (기술적 분석 기반)
        - _technical.json  (이동평균·RSI·MACD)
        - _supply.json     (수급 분석)
        - _short.json      (공매도 분석)
        - _broker.json     (거래원 분석)
        - _financial.json  (재무·실적)
        - _market.json     (시장환경·상대강도)
        - _info.json / _news.json
                    ↓
       [pipelines/fetch_daily.py]             ← 캐시 미스 시 KIS API 호출
                    ↓
       [웹 검색 (보조)]                        ← 캐시·fetch 실패 시만 사용
                    ↓
       [분석 엔진 (CLAUDE.md 6개 섹션)]       ← 기술적·수급·공매도·시장·거래원·재무
                    ↓
       [analysis/reports/]                    ← MD + JSON + viewer.html 저장
```

---

## 컴포넌트별 역할

| 컴포넌트 | 역할 | 변경 주체 |
|----------|------|-----------|
| `CLAUDE.md` | 분석 판정 기준·6개 섹션 규칙 | 사용자 직접 수정 |
| `ARCHITECTURE.md` | 구조·흐름 문서 | 구조 변경 시 업데이트 |
| `CODEBASE.md` | 캐시 규칙·파일 형식·코드 관행 | 구현 변경 시 업데이트 |
| `profiles/{ticker}.md` | 종목별 예외·맥락 | 종목 분석 중 축적 |
| `data/kis_cache/` | KIS API 응답 로컬 저장 (8종) | `fetch_daily.py` 자동 생성 |
| `pipelines/fetch_daily.py` | KIS API 호출·캐시 저장 스크립트 | 개발자 수정 |
| `analysis/reports/` | 최종 분석 보고서 (MD+JSON+HTML) | 분석 실행 시 자동 생성 |

---

## profiles 파일 형식 (표준 템플릿)

```markdown
# {종목명} ({ticker}) — 종목 보정

## 유형
복합형 (테마 + 실적)

## 핵심 모니터링 지표
- 수주잔고 / 분기 매출
- 외인 순매수 누적
- 공매도 비율 임계치: 5% 이상 시 경고

## 주요 이벤트 로그
| 날짜 | 이벤트 | 영향 |
|------|--------|------|
| YYYY-MM-DD | 내용 | +/-% |

## 예외 규칙
- 거래량 적음: 5% 조정 기준을 3%로 하향 적용
- 공매도 비율 정상 범위: 2% 이하 (업종 특성)
```

---

## 분석 실행 순서

1. `CLAUDE.md` + `ARCHITECTURE.md` + `CODEBASE.md` 읽기
2. `profiles/{ticker}.md` 읽기 (없으면 건너뜀)
3. `data/kis_cache/{ticker}_*.json/csv` 전체 확인 (8종)
4. 캐시 미스 항목 → `pipelines/fetch_daily.py --ticker {ticker}` 실행
5. 여전히 부족한 뉴스·매크로 → 웹 검색 보완
6. CLAUDE.md 6개 섹션 기준으로 분석 수행
7. `analysis/reports/` 에 MD + JSON + viewer.html 저장
