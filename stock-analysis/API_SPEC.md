# API_SPEC.md — stock-analysis 프로젝트 API 명세서

> **대상 독자:** 이 프로젝트를 처음 접하는 다른 에이전트
> **검증일:** 2026-04-30 (327260 RF머트리얼즈 기준 9/9 endpoint live 통과)
> **인증 키 위치:** `pipelines/config.py` 또는 환경변수 `KIS_APP_KEY`/`KIS_APP_SECRET`

---

## 1. 개요

한국투자증권(KIS) OpenAPI를 통해 국내주식 종목별 데이터를 일별 수집·캐싱하는 파이프라인. 9가지 데이터 타입을 1순위 데이터 소스로 사용하며, 결과는 `data/kis_cache/{ticker}_{type}.{ext}`에 저장된다.

- **Base URL (실전):** `https://openapi.koreainvestment.com:9443`
- **Base URL (모의):** `https://openapivts.koreainvestment.com:29443` (`config.py`에서 `IS_MOCK=True`)
- **공식 문서:** `~/Downloads/한국투자증권_오픈API_전체문서_20260427_030000.xlsx`

---

## 2. 프로젝트 .py 파일 구조

| 파일 | 역할 | 사용 API |
|---|---|---|
| [pipelines/config.py](pipelines/config.py) | KIS 인증 정보 보관 (APP_KEY, APP_SECRET, ACCOUNT_NO, IS_MOCK 플래그) | — |
| [pipelines/fetch_daily.py](pipelines/fetch_daily.py) | **메인 fetch 스크립트.** 9개 타입을 KIS API로 조회 → `data/kis_cache/`에 저장. 캐시 TTL/stale 관리, 토큰 캐시, rate-limit 백오프 포함 | 1~10번 모두 |
| [pipelines/patch_fetch.py](pipelines/patch_fetch.py) | **(레거시·미사용)** 과거 fetch_daily.py 패치용 스크립트. 잘못된 endpoint(404)가 하드코딩되어 있어 더 이상 실행하면 안 됨. 참고용으로만 보관 | — |

> **주의:** `patch_fetch.py`는 deprecated. 실행 시 `fetch_daily.py`를 잘못된 코드로 덮어쓴다.

---

## 3. 인증 (OAuth2)

### API #0 — 접근토큰 발급
| 항목 | 값 |
|---|---|
| 메서드 | `POST` |
| URL | `/oauth2/tokenP` |
| 헤더 | `Content-Type: application/json` |
| 바디 | `{"grant_type": "client_credentials", "appkey": ..., "appsecret": ...}` |
| 응답 | `{"access_token": "...", "expires_in": 86400}` (유효 1일) |
| 코드 | [fetch_daily.py:138 `get_access_token()`](pipelines/fetch_daily.py:138) |
| 캐시 | `pipelines/token_cache.json` (만료 5분 전 갱신) |

### 공통 헤더 (모든 GET 호출)
```python
{
    "Content-Type": "application/json",
    "Authorization": f"Bearer {access_token}",
    "appkey":    APP_KEY,
    "appsecret": APP_SECRET,
    "tr_id":     "<endpoint별 다름>",
    "custtype":  "P",   # B=법인, P=개인
}
```
정의: [fetch_daily.py:175 `kis_headers()`](pipelines/fetch_daily.py:175)

### 공통 GET 래퍼
[fetch_daily.py:186 `kis_get()`](pipelines/fetch_daily.py:186) — 5xx 발생 시 0.4s/0.8s 백오프로 최대 2회 재시도. `rt_cd != "0"`이면 `ValueError(KIS API 오류 [{rt_cd}]: {msg1})` 발생.

---

## 4. API 엔드포인트 요약 (10개)

| # | 타입 | API 명 | URL | TR_ID | 코드 위치 |
|---|---|---|---|---|---|
| 0 | auth | 접근토큰 발급 | `/oauth2/tokenP` | — | [fetch_daily.py:138](pipelines/fetch_daily.py:138) |
| 1 | ohlcv | 국내주식기간별시세(일/주/월/년) | `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice` | `FHKST03010100` | [fetch_daily.py:213](pipelines/fetch_daily.py:213) |
| 2 | supply | 종목별 투자자매매동향(일별) | `/uapi/domestic-stock/v1/quotations/inquire-investor` | `FHKST01010900` | [fetch_daily.py:339](pipelines/fetch_daily.py:339) |
| 3 | short | 국내주식 공매도 일별추이 | `/uapi/domestic-stock/v1/quotations/daily-short-sale` | `FHPST04830000` | [fetch_daily.py:384](pipelines/fetch_daily.py:384) |
| 4 | broker | 주식현재가 회원사 | `/uapi/domestic-stock/v1/quotations/inquire-member` | `FHKST01010600` | [fetch_daily.py:428](pipelines/fetch_daily.py:428) |
| 5 | financial | 국내주식 손익계산서 | `/uapi/domestic-stock/v1/finance/income-statement` | `FHKST66430200` | [fetch_daily.py:469](pipelines/fetch_daily.py:469) |
| 6 | (financial 부속) | 주식현재가 시세 | `/uapi/domestic-stock/v1/quotations/inquire-price` | `FHKST01010100` | financial valuation, market 당일등락, info fallback |
| 7 | market | 국내주식업종기간별시세(일/주/월/년) | `/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice` | `FHKUP03500100` | [fetch_daily.py:528](pipelines/fetch_daily.py:528) |
| 8 | info | 상품기본조회 | `/uapi/domestic-stock/v1/quotations/search-stock-info` | `CTPF1002R` | [fetch_daily.py:596](pipelines/fetch_daily.py:596) |
| 9 | news | 종합 시황/공시(제목) | `/uapi/domestic-stock/v1/quotations/news-title` | `FHKST01011800` | [fetch_daily.py:644](pipelines/fetch_daily.py:644) |

> `technical` 타입은 별도 API 호출 없음 — `ohlcv` CSV에서 RSI/MA/MACD/BB를 계산만 한다 ([fetch_daily.py:251](pipelines/fetch_daily.py:251)).

---

## 5. 엔드포인트 상세 명세

### 5.1 ohlcv — 일봉 시세
- **TR_ID:** `FHKST03010100`
- **URL:** `/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice`
- **Query Params:**
  | 키 | 값 | 설명 |
  |---|---|---|
  | `FID_COND_MRKT_DIV_CODE` | `J` | 주식 |
  | `FID_INPUT_ISCD` | `327260` | 종목코드 |
  | `FID_INPUT_DATE_1` | `20251031` | 시작일 |
  | `FID_INPUT_DATE_2` | `20260430` | 종료일 |
  | `FID_PERIOD_DIV_CODE` | `D` | 일/주/월/년 (`D/W/M/Y`) |
  | `FID_ORG_ADJ_PRC` | `0` | 0=수정주가, 1=원주가 |
- **응답:** `output1` (현재가/등락 dict), `output2` (일봉 array, 최대 100건)
- **사용 필드:** `stck_bsop_date, stck_oprc, stck_hgpr, stck_lwpr, stck_clpr, acml_vol`
- **출력 캐시:** `data/kis_cache/{ticker}_ohlcv.csv` (CSV: `date,open,high,low,close,volume`)

### 5.2 supply — 외인/기관 수급
- **TR_ID:** `FHKST01010900`
- **URL:** `/uapi/domestic-stock/v1/quotations/inquire-investor`
- **Query Params:** `FID_COND_MRKT_DIV_CODE=J`, `FID_INPUT_ISCD={ticker}`
- **응답:** `output` (array, API는 30거래일 반환, 코드는 최신 20일만 사용)
- **사용 필드:**
  - `stck_bsop_date`: 영업일 (YYYYMMDD)
  - `frgn_ntby_qty`: 외국인 순매수 수량
  - `orgn_ntby_qty`: 기관 순매수 수량
  - `prsn_ntby_qty`: **개인 순매수 수량** (⚠️ `indv_ntby_qty`는 존재하지 않는 필드)
- **출력 캐시:** `data/kis_cache/{ticker}_supply.json`

### 5.3 short — 공매도 일별추이 ⚠️
- **TR_ID:** `FHPST04830000`
- **URL:** `/uapi/domestic-stock/v1/quotations/daily-short-sale` ← **유사명 endpoint(`inquire-daily-short-selling`)는 404. 정확히 `daily-short-sale`**
- **Query Params:** `FID_COND_MRKT_DIV_CODE=J`, `FID_INPUT_ISCD`, `FID_INPUT_DATE_1`, `FID_INPUT_DATE_2`
- **응답:** `output1` (현재가 dict), `output2` (array, 최대 30~40건, **최신이 [0]**)
- **사용 필드:**
  - `stck_bsop_date`: 영업일 (YYYYMMDD)
  - `ssts_cntg_qty`: 당일 공매도 수량
  - `ssts_vol_rlim`: 당일 공매도 거래량 비중 (%)
  - `acml_ssts_cntg_qty`: 누적 공매도 수량 (잔고 근사)
  - `acml_ssts_cntg_qty_rlim`: 누적 공매도 비중 (%)
- **샘플:**
  ```json
  {"stck_bsop_date":"20260430", "ssts_cntg_qty":"0", "ssts_vol_rlim":"0.00",
   "acml_ssts_cntg_qty":"199508", "acml_ssts_cntg_qty_rlim":"1.40"}
  ```
- **출력 캐시:** `data/kis_cache/{ticker}_short.json`

### 5.4 broker — 거래원
- **TR_ID:** `FHKST01010600`
- **URL:** `/uapi/domestic-stock/v1/quotations/inquire-member`
- **Query Params:** `FID_COND_MRKT_DIV_CODE=J`, `FID_INPUT_ISCD`, `FID_INPUT_DATE_1` (오늘 YYYYMMDD)
- **응답:** `output` (dict — 5위까지 1~5번 접미사로 평탄화된 단일 dict)
- **사용 필드 (필드명 끝에 1~5 숫자):**
  - **매도 (seln=賣ln, sell):** `seln_mbcr_name{i}` (회원사명), `total_seln_qty{i}` (수량), `seln_mbcr_no{i}` (코드)
  - **매수 (shnu=收ㄴ, buy):** `shnu_mbcr_name{i}` (회원사명), `total_shnu_qty{i}` (수량), `shnu_mbcr_no{i}` (코드)
  - **점유율/증감:** `seln_mbcr_rlim{i}`, `shnu_mbcr_rlim{i}` (%), `seln_qty_icdc{i}`, `shnu_qty_icdc{i}` (전일대비)
  - **외국계 표시:** `seln_mbcr_glob_yn_{i}`, `shnu_mbcr_glob_yn_{i}` (`Y`/`N`), `glob_total_seln_qty`, `glob_total_shnu_qty`
- **함정:** 수량 필드는 `seln_vol{i}`/`shnu_vol{i}`가 **아니라** `total_seln_qty{i}`/`total_shnu_qty{i}`
- **출력 캐시:** `data/kis_cache/{ticker}_broker.json` (`top_buy`=매수 회원사 = `shnu_*`, `top_sell`=매도 회원사 = `seln_*`)

### 5.5 financial — 손익계산서 ⚠️
- **TR_ID:** `FHKST66430200` ← **300은 재무비율, 200이 손익계산서**
- **URL:** `/uapi/domestic-stock/v1/finance/income-statement`
- **Query Params:** `FID_DIV_CLS_CODE=1` (1=분기, 0=연간), `FID_COND_MRKT_DIV_CODE=J`, `FID_INPUT_ISCD`
- **응답:** `output` (array, 최대 27분기). **모든 수치가 string-float (`"641.00"`)**
- **사용 필드:**
  - `stac_yymm`: 분기 (YYYYMM)
  - `sale_account`: 매출 (억원)
  - `bsop_prti`: 영업이익
  - `thtr_ntin`: 순이익 (참고: 응답에 없으면 `op_prfi` 또는 `spec_prfi` 사용 검토)
  - `sale_account_yoy`, `sale_account_qoq`: YoY/QoQ (현재 응답에 없어 0.0 저장 중)
- **샘플:**
  ```json
  {"stac_yymm":"202512", "sale_account":"641.00", "bsop_prti":"74.00", "op_prfi":"83.00"}
  ```
- **출력 캐시:** `data/kis_cache/{ticker}_financial.json`
- **부수 호출:** PER/PBR/ROE는 `inquire-price` (5.6) 의 output에서 추출

### 5.6 inquire-price — 주식현재가 시세 (부속)
- **TR_ID:** `FHKST01010100`
- **URL:** `/uapi/domestic-stock/v1/quotations/inquire-price`
- **Query Params:** `FID_COND_MRKT_DIV_CODE=J`, `FID_INPUT_ISCD`
- **응답:** `output` (dict, 70여개 필드)
- **자주 쓰는 필드:**
  - `stck_prpr` (현재가), `prdy_ctrt` (전일대비율 %)
  - `per`, `pbr`, `roe` (밸류에이션)
  - `hts_kor_isnm` (종목명), `bstp_kor_isnm` (업종 단명, 예: `전기·전자`)
  - `rprs_mrkt_kor_name` (시장명 — `KOSPI`/`KOSDAQ` 직접)
  - `lstn_stcn` (상장주식수) — ⚠️ `lstg_stqty`는 존재하지 않는 필드
  - `marg_rate` (증거금률 %), `acml_tr_pbmn` (누적거래대금)
- **호출처:** financial(밸류에이션), market(KOSPI 등락), info(fallback) — 3곳에서 재사용

### 5.7 market — 업종/지수 기간별 시세
- **TR_ID:** `FHKUP03500100`
- **URL:** `/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice`
- **Query Params:**
  | 키 | 값 |
  |---|---|
  | `FID_COND_MRKT_DIV_CODE` | `U` (업종) |
  | `FID_INPUT_ISCD` | `0001`=KOSPI, `1001`=KOSDAQ |
  | `FID_INPUT_DATE_1`/`_2` | 기간 (YYYYMMDD) |
  | `FID_PERIOD_DIV_CODE` | `D` |
- **응답:** `output1` (지수 현재값 dict), `output2` (일별 array)
- **사용 필드:** `stck_bsop_date, bstp_nmix_prpr (지수 종가), bstp_nmix_oprc/hgpr/lwpr`
- **현재 사용처:** 종목 1개월 수익률 vs KOSPI 1개월 수익률 → 상대강도(RS)
- **출력 캐시:** `data/kis_cache/{ticker}_market.json`

### 5.8 info — 상품기본조회
- **TR_ID:** `CTPF1002R`
- **URL:** `/uapi/domestic-stock/v1/quotations/search-stock-info`
- **Query Params:** `PRDT_TYPE_CD=300` (주식), `PDNO={ticker}`
- **응답:** `output` (dict, 60+ 필드)
- **사용 필드:**
  - `pdno`: 종목코드 (`00000A327260` 형태, prefix 붙음 — 노출 시 마지막 6자리만 사용)
  - `prdt_abrv_name`: 종목 약명 (예: `RF머트리얼즈`)
  - `mket_id_cd`: 시장코드 (`STK`=KOSPI, `KSQ`=KOSDAQ, `KNX`=KONEX) — 코드에서 `MKT_MAP`으로 표시명 변환
  - `std_idst_clsf_cd_name`: 표준산업분류명 (예: `전자부품 제조업`)
  - `lstg_stqt`: 상장주식수 (필드명에 `y` 없음 주의)
- **Fallback:** 5xx 시 `inquire-price` (5.6)에서:
  - `hts_kor_isnm` (종목명), `rprs_mrkt_kor_name` (시장명, 이미 `KOSDAQ` 형태), `bstp_kor_isnm` (업종 단명, 예: `전기·전자`), `lstn_stcn` (상장주식수, **`lstg_stqty`가 아님**)
- **샘플 출력:**
  ```json
  {"ticker":"327260", "name":"RF머트리얼즈", "market":"KOSDAQ",
   "sector":"전자부품 제조업", "shares_outstanding":8495135}
  ```
- **출력 캐시:** `data/kis_cache/{ticker}_info.json`

### 5.9 news — 종합 시황/공시(제목) ⚠️
- **TR_ID:** `FHKST01011800` ← **`YNAS9001R`은 존재하지 않는 코드**
- **URL:** `/uapi/domestic-stock/v1/quotations/news-title`
- **Query Params (8개 모두 필수, 종목코드 외엔 빈 문자열):**
  | 키 | 값 |
  |---|---|
  | `FID_NEWS_OFER_ENTP_CODE` | `""` |
  | `FID_COND_MRKT_CLS_CODE` | `""` ← **`MRKT_DIV`가 아니라 `MRKT_CLS`** |
  | `FID_INPUT_ISCD` | `327260` (공백시 전체) |
  | `FID_TITL_CNTT` | `""` |
  | `FID_INPUT_DATE_1` | `""` (현재 기준) |
  | `FID_INPUT_HOUR_1` | `""` |
  | `FID_RANK_SORT_CLS_CODE` | `""` |
  | `FID_INPUT_SRNO` | `""` |
- **응답:** `output` (array, 최대 40건)
- **사용 필드:**
  - `data_dt`: 작성일 (YYYYMMDD)
  - `data_tm`: 작성시간 (HHMMSS)
  - `hts_pbnt_titl_cntt`: **제목** (필드명 길이 주의)
  - `dorg`: 자료원 (인포스탁/연합뉴스/뉴스핌 등)
  - `news_ofer_entp_code`: 업체코드
  - `iscd1~5`: 관련 종목코드
- **샘플:**
  ```json
  {"data_dt":"20260430", "data_tm":"101933",
   "hts_pbnt_titl_cntt":"[장중수급포착] RF머트리얼즈, 외국인 6일 연속 순매수행진... 주가 +6.41%",
   "dorg":"뉴스핌", "iscd1":"327260"}
  ```
- **출력 캐시:** `data/kis_cache/{ticker}_news.json`

---

## 6. 캐시 레이어

### 파일 매핑 ([fetch_daily.py:87](pipelines/fetch_daily.py:87) `CACHE_FILE`)

모든 캐시는 `data/kis_cache/{ticker}_{type}.{ext}` 형식.

| 타입 | 확장자 | TTL |
|---|---|---|
| ohlcv | csv | 1일 |
| technical, supply, short, broker, market, news | json | 1일 |
| financial | json | 7일 |
| info | json | 7일 |

### 유효성 판정 ([fetch_daily.py:103](pipelines/fetch_daily.py:103) `is_cache_valid()`)
- 파일 미존재 → invalid
- ohlcv: 파일 mtime 기준
- json: `last_updated` 필드 (ISO8601) + `stale: true` 플래그 검사

### Stale 마킹 ([fetch_daily.py:124](pipelines/fetch_daily.py:124) `save_stale()`)
API 실패 시 기존 캐시에 `"stale": true` 추가. 다음 호출에서 강제 재조회.

---

## 7. 테스트 호출 (CLI)

### 단일 타입 테스트
```bash
cd /Users/r/Documents/Claude/stock-dashboard/stock-analysis

python3 pipelines/fetch_daily.py --ticker 327260 --type ohlcv     --force
python3 pipelines/fetch_daily.py --ticker 327260 --type technical --force
python3 pipelines/fetch_daily.py --ticker 327260 --type supply    --force
python3 pipelines/fetch_daily.py --ticker 327260 --type short     --force
python3 pipelines/fetch_daily.py --ticker 327260 --type broker    --force
python3 pipelines/fetch_daily.py --ticker 327260 --type financial --force
python3 pipelines/fetch_daily.py --ticker 327260 --type market    --force
python3 pipelines/fetch_daily.py --ticker 327260 --type info      --force
python3 pipelines/fetch_daily.py --ticker 327260 --type news      --force
```

### 전체 타입 일괄
```bash
python3 pipelines/fetch_daily.py --ticker 327260 --force
```

### 전체 종목 일괄 (`profiles/*.md` 기준)
```bash
python3 pipelines/fetch_daily.py --all
```

### 검증 결과 (2026-04-30 327260 기준)
```
▶ 327260  (fetch: ohlcv, technical, supply, short, broker, financial, market, info, news)
  ✅ ohlcv      → 100행 일봉
  ✅ technical  → RSI=57.8, MA5=90,120 (ohlcv 캐시에서 계산)
  ✅ supply     → 외인 매수 우위 (20일, 04-29 외인+45,955 / 기관-24,618 / 개인-21,628)
  ✅ short      → 20일 공매도, 누적잔고 199,508주 (1.40%)
  ✅ broker     → top_buy: 신한 63,186주 / top_sell: 신한 49,185주
  ✅ financial  → 4분기 (2025Q4 매출 641억, 영업이익 74억)
  ✅ market     → RS vs KOSPI = +8.8 (1M)
  ✅ info       → RF머트리얼즈 (KOSDAQ, 전자부품 제조업, 8,495,135주)
  ✅ news       → 20건 (최신: "RF머트리얼즈, 외국인 6일 연속 순매수")
```

### 저수준 테스트 (단일 endpoint, 토큰 캐시 재사용)
```python
import sys, json, requests
sys.path.insert(0, 'pipelines')
import config as _cfg

tok = json.loads(open('pipelines/token_cache.json').read())['access_token']
BASE = 'https://openapi.koreainvestment.com:9443'

headers = {
    'Content-Type':'application/json',
    'Authorization': f'Bearer {tok}',
    'appkey': _cfg.KIS_APP_KEY,
    'appsecret': _cfg.KIS_APP_SECRET,
    'tr_id': 'FHPST04830000',  # ← endpoint별 변경
    'custtype': 'P',
}
params = {
    'FID_COND_MRKT_DIV_CODE': 'J',
    'FID_INPUT_ISCD': '327260',
    'FID_INPUT_DATE_1': '20260315',
    'FID_INPUT_DATE_2': '20260430',
}
r = requests.get(BASE + '/uapi/domestic-stock/v1/quotations/daily-short-sale',
                 headers=headers, params=params, timeout=15)
print(r.status_code, r.json().get('rt_cd'), r.json().get('msg1'))
print(json.dumps(r.json().get('output2', [])[:2], ensure_ascii=False, indent=2))
```

---

## 8. 주의사항·함정 (다른 에이전트가 자주 틀리는 지점)

| 항목 | 잘못된 값 | 올바른 값 |
|---|---|---|
| 손익계산서 TR_ID | `FHKST66430300` (재무비율) | `FHKST66430200` |
| 공매도 URL | `inquire-daily-short-selling` (404) | `daily-short-sale` |
| 지수 차트 URL | `inquire-index-daily-chartprice` | `inquire-daily-indexchartprice` |
| 지수 차트 TR_ID | `FHKUP03010200` | `FHKUP03500100` |
| 뉴스 TR_ID | `YNAS9001R` (존재하지 않음) | `FHKST01011800` |
| 뉴스 시장구분 키 | `FID_COND_MRKT_DIV_CODE` | `FID_COND_MRKT_CLS_CODE` |
| 뉴스 제목 필드 | `news_ttl` | `hts_pbnt_titl_cntt` |
| 뉴스 출처 필드 | `news_src_name` | `dorg` |
| **수급 개인 순매수** | `indv_ntby_qty` (없는 필드) | `prsn_ntby_qty` |
| **거래원 매도수량** | `seln_vol{i}` (없는 필드) | `total_seln_qty{i}` |
| **거래원 매수수량** | `shnu_vol{i}` (없는 필드) | `total_shnu_qty{i}` |
| **거래원 buy/sell 매핑** | `seln_*` → top_buy (역) | `shnu_*` → top_buy, `seln_*` → top_sell |
| 상품기본조회 상장주식수 | `lstg_stqty` | `lstg_stqt` (1글자 차이) |
| 상품기본조회 업종명 | `bstp_larg_div_name` (없는 필드) | `std_idst_clsf_cd_name` |
| 상품기본조회 시장코드 | 그대로 사용 (`KSQ`/`STK`) | `KSQ→KOSDAQ`, `STK→KOSPI` 매핑 필요 |
| inquire-price 상장주식수 | `lstg_stqty` | `lstn_stcn` (필드명 다름) |
| 손익계산서 수치 형식 | `int(v)` 직접 변환 | `int(float(v))` (응답이 `"641.00"` 형태) |

### Rate limiting
- KIS는 burst 호출 시 간헐 5xx를 반환한다.
- `kis_get()`은 5xx에 대해 0.4s/0.8s 백오프로 자동 재시도.
- main loop는 타입 간 `time.sleep(0.15)` 삽입 ([fetch_daily.py:766](pipelines/fetch_daily.py:766)).

### 모의투자 미지원 endpoint
- `daily-short-sale` (FHPST04830000), `news-title` (FHKST01011800), `search-stock-info` (CTPF1002R), `inquire-investor` (FHKST01010900) 등은 모의투자 미지원 — `IS_MOCK=True`면 실패할 수 있음.
- 시세성 endpoint(ohlcv, market, inquire-price)는 모의투자 지원.

### 데이터 단위
- 가격: 원화 정수
- 매출/이익: **억원 단위** (예: 641 = 641억)
- 비율: %
- 날짜: API 입출력은 `YYYYMMDD`, 캐시 저장 시 `YYYY-MM-DD`로 변환

---

## 9. 참고 문서

- [CLAUDE.md](CLAUDE.md) — 분석 공통 규칙, 데이터 우선순위
- [CODEBASE.md](CODEBASE.md) — 캐시 JSON 스키마 상세
- [ARCHITECTURE.md](ARCHITECTURE.md) — 전체 아키텍처
- KIS 전체문서 xlsx: `~/Downloads/한국투자증권_오픈API_전체문서_20260427_030000.xlsx`
- KIS Developers Portal: https://apiportal.koreainvestment.com
