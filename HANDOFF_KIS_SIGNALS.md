# KIS API 시그널 고도화 — 인수인계 문서

작성일: 2026-04-29 (4차 업데이트)

---

## 전체 흐름 요약

세션 1: KRX Open API 탐색 → 9분류 데이터 소스 탐색 → KIS API 전면 검토 →  
`kis_investor.py` 9분류 교체 + `kis_loan.py` / `kis_market.py` / `naver_market.py` 신규 생성

세션 2: `/why` 커맨드에 KIS 9분류 + NAVER 매크로 컨텍스트 연동

세션 3: KRX Open API 7개 추가 엔드포인트 승인 완료 → 동작 검증 + path 오타 수정

세션 4: KIS API 공식 Excel 문서 분석 → 파라미터 버그 3개 발견·수정 (`fetch_market_investor`, `fetch_short_top`, `fetch_investor_flow`) → "구독 제한"으로 오진단됐던 기능 전부 복구

---

## 완료된 작업

### 1. `signals/kis_investor.py` 전면 재작성 (세션 1)

**변경 핵심**: `FHKST01010900` (3분류) → `FHPTJ04160001` (9분류)

```python
from signals.kis_investor import fetch_investor_flow, analyze_investor_signal

rows = fetch_investor_flow('010170')   # market="J" — KOSPI/KOSDAQ 모두 동작
sig  = analyze_investor_signal('010170')
```

**새 반환 필드** (기존 3개 → 13개):

| 접두어 | 의미 |
|--------|------|
| `frgn_` | 외국인 |
| `prsn_` | 개인 |
| `orgn_` | 기관계 |
| `scrt_` | 증권 |
| `ivtr_` | 투신 |
| `pe_fund_` | **사모** (숨은 수급 핵심) |
| `bank_` | 은행 |
| `insu_` | 보험 |
| `mrbn_` | 종금 |
| `fund_` | 연기금 |
| `etc_` | 기타 |
| `etc_corp_` | **기타법인** (숨은 수급 핵심) |
| `etc_orgt_` | 기타단체 |

각 접두어에 `_qty` (주수), `_amt` (백만원) 두 종류.

**`analyze_investor_signal()` 주요 출력 필드**:
```python
sig['frgn_5d']     # 외국인 5일 순매수 (억원)
sig['frgn_20d']    # 외국인 20일 순매수 (억원)
sig['orgn_5d']     # 기관 5일
sig['smart_5d']    # 외인+기관 합산 5일
sig['hidden_5d']   # 사모+기타법인 5일 순매수 (억원)
sig['hidden_20d']  # 사모+기타법인 20일 순매수 (억원)
sig['pe_fund_5d']  # 사모 단독
sig['etc_corp_5d'] # 기타법인 단독
sig['buy_signals'] # 자동 생성 문자열 리스트
sig['sell_signals']
```

**신규 함수 2개** (장중 한정):
```python
fetch_investor_estimate('010170')   # HHPTJ04160200 — 외인기관 추정가집계 (장중 5구간)
fetch_foreign_flow('010170')        # FHKST644400C0 — 외국계 창구 누적 순매수 (장중)
```

---

### 2. `signals/kis_loan.py` 신규 생성 (세션 1)

**주의**: `HHPST074500C0`는 종목별이 아닌 **시장 전체 대차거래 집계** 반환.
(MKSC_SHRN_ISCD 파라미터 무시됨 — 테스트로 확인)

```python
from signals.kis_loan import fetch_market_loan, analyze_market_loan

rows = fetch_market_loan(market='1')         # "1"=KOSPI, "2"=KOSDAQ
sig  = analyze_market_loan(market='1')
# → {balance_qty, balance_amt, balance_5d_pct, balance_20d_pct, new_5d, repay_5d, triggers}
```

**해석**:
- `balance_5d_pct >= 5%` → 공매도 예고 (score +2)
- `repay > new * 2` → 숏커버링 진행 중 (긍정)

---

### 3. `signals/kis_market.py` 신규 생성 (세션 1) + 파라미터 수정 (세션 4)

```python
from signals.kis_market import fetch_market_funds, fetch_market_investor, fetch_short_top, get_market_snapshot

# ✅ 동작: 고객예탁금, 신용융자잔고 등 60일 데이터
funds = fetch_market_funds()

# ✅ 동작 (세션 4 수정 후): 9분류 순매수 300영업일
inv = fetch_market_investor()   # output[0]이 최신 — 내림차순 반환

# ✅ 동작 (세션 4 수정 후): 공매도 상위 30종목
tops = fetch_short_top()

# ✅ 동작 (세션 4 수정 후)
snap = get_market_snapshot()
# → last_date, deposit, frgn_amt, frgn_reg_amt, frgn_nreg_amt, orgn_amt, prsn_amt, inv_date
```

---

### 4. `signals/naver_market.py` 신규 생성 (세션 1)

KIS 구독 제한 우회 — NAVER Finance front-api 활용 (로그인 불필요).

```python
from signals.naver_market import (
    fetch_market_investor_summary,   # daily/weekly/monthly 3분류 순매수
    fetch_investor_ranking,          # 투자자별 상위 10종목
    get_market_flow_snapshot,        # 캐싱된 스냅샷 + triggers
)
```

**한계**:
- 3분류만 제공 (외국인/기관/개인)
- 현재 기간 집계만 (과거 날짜 지정 불가)

---

### 5. `signals/krx_open_api.py` — 8개 엔드포인트 전부 동작 (세션 3) ✅

KRX Open API에서 추가 7개 신청건 모두 승인 (2026-04-28). 동작 검증 및 path 수정 완료.

| 엔드포인트 | 함수 | 행수 (2026-04-24 기준) |
|-----------|------|------------------------|
| `sto/stk_bydd_trd` | `get_kospi_daily(date)` | 949 |
| `sto/ksq_bydd_trd` | `get_kosdaq_daily(date)` | 1,821 |
| `sto/stk_isu_base_info` | `get_kospi_base_info(date)` | 949 |
| `sto/ksq_isu_base_info` | `get_kosdaq_base_info(date)` | 1,821 |
| `idx/kospi_dd_trd` | `get_kospi_index(date)` | 51 |
| `idx/kosdaq_dd_trd` | `get_kosdaq_index(date)` | 40 |
| `idx/krx_dd_trd` | `get_krx_index(date)` | 34 |
| `etp/etf_bydd_trd` | `get_etf_daily(date)` | 1,095 |

**path 수정**: `idx/ksdaq_dd_trd` → `idx/kosdaq_dd_trd` (`o` 빠진 오타였음).

**검증 도구**: `verify_krx_open_api.py YYYYMMDD` — 8개 엔드포인트 일괄 헬스체크.

**새로 발견된 활용 포인트**:
- ETF 행에 `NAV`, `OBJ_STKPRC_IDX` (추적지수 종가), `FLUC_RT_IDX` (괴리율) 포함 → ETF 차익거래/이상신호 시그널 가능
- KOSPI/KOSDAQ 지수 행에 `ACC_TRDVAL`, `MKTCAP` → 시장 전체 일별 거래대금/시총 매크로 지표
- `stk_isu_base_info`의 `LIST_DD` (상장일) → 신규상장 종목 자동 탐지 가능
- KRX 섹터지수에 "코리아 밸류업 지수", "KRX TMI" 등 신규지수 포함

**미신청/대기**:
- 신주인수권증서 일별매매정보 — 별도 신청 후 승인됨, 함수 미구현 (엔드포인트 path 미확인)
- 유가증권 종목기본정보 1년치 — 승인대기 중 (현재 1개월짜리만 사용 가능)

---

### 7. 파라미터 버그 수정 3건 (세션 4) ✅

**배경**: "구독 플랜 필요"로 오진단됐던 API들이 실제로는 파라미터 오류였음.  
KIS API 공식 Excel 문서 (`한국투자증권_오픈API_전체문서_20260427.xlsx`) 대조로 발견.

---

#### Bug 1 — `fetch_market_investor()` (FHPTJ04040000): `FID_INPUT_ISCD_1` 누락

| | Before | After |
|--|--------|-------|
| `FID_INPUT_ISCD_1` | `""` (빈값) | `"KSP"` / `"KSQ"` |
| 9분류 금액 필드 | **전부 0** | 실제 데이터 |
| `FID_INPUT_DATE_1` | `start` (60일 전) | `end` (오늘) |
| 데이터 방향 | output[-1]이 최신 (잘못됨) | **output[0]이 최신** (DATE_1 기준 역방향) |

```python
iscd_name = "KSP" if market == "J" else "KSQ"
params = {
    "FID_COND_MRKT_DIV_CODE": "U",
    "FID_INPUT_ISCD":         iscd,           # "0001" / "1001"
    "FID_INPUT_DATE_1":       end,            # as-of (최신) — output[0]이 이 날짜
    "FID_INPUT_ISCD_1":       iscd_name,      # ← 이 값 없으면 9분류 전부 0
    "FID_INPUT_DATE_2":       start,
    "FID_INPUT_ISCD_2":       iscd,
}
```

신규 출력 필드: `frgn_reg_qty/amt`, `frgn_nreg_qty/amt`, `index_open`, `index_high`, `index_low`

---

#### Bug 2 — `fetch_short_top()` (FHPST04820000): `FID_COND_SCR_DIV_CODE` 오류

| | Before | After |
|--|--------|-------|
| `FID_COND_SCR_DIV_CODE` | `"20601"` | `"20482"` |
| 반환 행 수 | **0** | 최대 30종목 |
| `FID_INPUT_ISCD` | `market` 문자열 직접 | `iscd_map` 변환 (`"J"→"0001"`) |

```python
_cnt_map = {1:"0", 2:"1", 3:"2", 4:"3", 5:"4", 14:"9", 21:"14"}
iscd_map  = {"J": "0001", "Q": "1001", "A": "0000"}
params = {
    "FID_COND_MRKT_DIV_CODE": "J",      # 항상 J (주식)
    "FID_COND_SCR_DIV_CODE":  "20482",  # ← 20601은 오류값
    "FID_INPUT_ISCD":         iscd,
    "FID_PERIOD_DIV_CODE":    "D",
    "FID_INPUT_CNT_1":        cnt,
    ...
}
```

신규 출력 필드: `date_from`, `date_to`, `avg_price`

---

#### Bug 3 — `fetch_investor_flow()` (FHPTJ04160001): 파라미터 2개 오류

| | Before | After |
|--|--------|-------|
| `FID_INPUT_DATE_1` | `datetime.now().strftime("%Y%m%d")` | `""` (빈값 = 자동 최신) |
| `FID_ORG_ADJ_PRC` | `"0"` | `""` |
| `FID_ETC_CLS_CODE` | `"0"` | `"1"` |

**중요**: 오늘 날짜를 DATE_1에 넣으면 15:40 이전에는 0행 반환. 빈값으로 두면 API가 자동으로 가용한 최신 날짜 선택.

신규 출력 필드: `open`, `high`, `low`, `volume`, `turnover`, `frgn_reg_qty/amt`, `frgn_nreg_qty/amt`, `frgn_buy_vol/sell_vol`, `frgn_buy_amt/sell_amt`

`analyze_investor_signal()` 신규 반환 필드: `frgn_reg_5d`, `frgn_nreg_5d`

---

### 6. `analysis/why_rally.py` — KIS 9분류 + NAVER 매크로 연동 (세션 2) ✅

**변경 파일**: `analysis/why_rally.py`

**추가된 함수 2개**:
```python
def kis_investor_current(code: str) -> dict:
    """analyze_investor_signal() 래퍼 — graceful 실패 처리."""

def market_macro_context() -> dict:
    """get_market_flow_snapshot() 래퍼 — NAVER 시장 전체 수급."""
```

**`print_report()` 변경 내용**:

1. **상단 시장 수급 블록** (가격 요약 직후):
   ```
   [시장 수급 2026-04-28]  외국인 -6792억  기관 +989억  개인 +6657억  (NAVER, 당일)
   ▷ 분배 패턴: 외국인 -6792억 매도 ↔ 개인 6657억 매수
   ▷ 외국인 강도 높은 매도 (-6792억)
   ```

2. **`▣ 현재 수급 — KIS 9분류`** 섹션 (투자자 유형 구간 분석 직후):
   ```
   구분        5일      20일
   외국인    -641.8   -125.7
   기관계     -28.9   -690.0
   개인      +662.9      —
   사모        -3.7   -138.7
   기타법인    +7.8    -41.1
   ──────────────────────
   ▶ 숨은수급   +4.1   -179.8

   🔴 외국인 5일 -641.8억 순매도
   🔴 기관 20일 -690.0억 순매도
   ...
   ```

3. **`validity_check()`에 KIS 시그널 자동 추가**:
   - 외국인 5일 ±10억 이상 → 유효성 체크 항목 추가
   - hidden_5d ±5억 이상 → 사모+기타법인 체크 추가
   - smart_5d ±20억 이상 → 스마트머니 체크 추가

**`print_report()` 반환 dict에 추가**:
```python
'kis_9class': kis_sig,   # analyze_investor_signal() 전체 결과
'macro': macro,          # get_market_flow_snapshot() 전체 결과
```

---

## 파악된 제약 사항 최종 정리

| 기능 | 시도한 방법 | 결과 | 비고 |
|------|------------|------|------|
| 시장별 투자자 9분류 일별 | KIS FHPTJ04040000 | ✅ 동작 (세션 4 수정) | `FID_INPUT_ISCD_1="KSP/KSQ"` 필수 |
| 공매도 상위 종목 랭킹 | KIS FHPST04820000 | ✅ 동작 (세션 4 수정) | `FID_COND_SCR_DIV_CODE="20482"` |
| 공매도 상위 종목 랭킹 | data.krx.co.kr | 로그인 필요 (LOGOUT) | 없음 |
| 공매도 상위 종목 랭킹 | NAVER Finance | 엔드포인트 없음 | 없음 |
| 종목별 공매도 (per-stock) | KIS FHPST04830000 | ✅ 동작 | `kis_short.py` |
| 대차잔고 (시장 전체) | KIS HHPST074500C0 | ✅ 동작 | `kis_loan.py` |
| 투자자 3분류 시장 전체 | NAVER front-api | ✅ 동작 | `naver_market.py` (보조용) |

---

## 다음 스레드에서 할 일

### 우선순위 ①: ~~`kis_market.py` `get_market_snapshot()` NAVER로 교체~~ ✅ 완료 (세션 4)

`fetch_market_investor()`가 세션 4 파라미터 수정으로 복구됨.  
`get_market_snapshot()`이 이제 실제 9분류 데이터 반환 (`frgn_reg_amt`, `frgn_nreg_amt`, `pe_fund_amt`, `etc_corp_amt` 등).  
`naver_market.py`는 KIS 9분류와 병행 사용 가능 (3분류 당일 빠른 집계 용도로 유지).

**`analysis/why_rally.py`의 `market_macro_context()`**: 현재 NAVER 기반. KIS 9분류로 교체 또는 병행 여부는 다음 스레드에서 결정.

### 우선순위 ②: ~~KRX Open API 범위 확장~~ ✅ 완료 (세션 3)

위 "5. krx_open_api.py" 섹션 참고. 8개 엔드포인트 전부 동작 확인.

### 우선순위 ③: 새 KRX 데이터 활용처 확장 (신규)

승인된 엔드포인트들을 기존 모듈/대시보드와 연결:
- **ETF 차익거래 시그널** — 신규 모듈 `signals/krx_etf.py`? `NAV`/`OBJ_STKPRC_IDX` 괴리율 ±N% 트리거
- **신규상장 종목 워치리스트** — `stk_isu_base_info`의 `LIST_DD` 기반 30일 이내 상장 종목 추출
- **시장 매크로 지표 보강** — KOSPI 지수 행의 `ACC_TRDVAL`로 일별 거래대금 추세 → `kis_market.py`/매크로 위젯에 추가
- **`kis_index.py` 보완 검토** — KIS `FHKUP03500100` 42개 지수 vs KRX `idx/krx_dd_trd` 34개 섹터 비교, 중복/누락 정리
- **신주인수권증서 엔드포인트 path 확인 + 함수 추가**

---

## 인프라 정보

### KIS API 인증
```python
# 키 위치
~/.../GoogleDrive/...01.주식/.env.kis
# KIS_APPKEY, KIS_APPSECRET

# 토큰 캐시
/stock-dashboard/data/kis_cache/token.json
# host: https://openapi.koreainvestment.com:9443 (실거래 계정)
```

### KIS API 공통 호출 패턴
```python
from signals.kis_api import get_client, rate_limit, cached_call, smart_ttl

client = get_client()
rate_limit()   # ≥0.22초 간격 보장 (20 req/sec 제한)

res = client.get(
    "/uapi/domestic-stock/v1/quotations/...",
    tr_id="FHXXXXXXX",
    params={...},
)
if res.get("rt_cd") != "0":
    return []
rows = res.get("output2", [])  # 또는 "output", "output1"
```

### NAVER Finance API 공통 패턴
```python
import requests
BASE = "https://m.stock.naver.com/front-api/market"
headers = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0)",
    "Referer": "https://m.stock.naver.com/",
    "Accept": "application/json",
}
resp = requests.get(f"{BASE}/tradingTrend/graphInfo",
    params={"periodType": "daily"}, headers=headers, timeout=10)
result = resp.json().get("result", {})
```

### KRX Open API 인증
```python
AUTH_KEY = "3DF5ED0699D0463CB90832A76BFD3258831CCA83"  # signals/krx_open_api.py 하드코딩
BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
# POST + JSON body + AUTH_KEY 헤더
```

---

## 확인된 중요 사실

1. **FHPTJ04160001의 `FID_COND_MRKT_DIV_CODE`**: "J"만 유효. KOSPI/KOSDAQ 모두 "J"로 요청.

2. **HHPST074500C0**: 이름은 "종목별 대차거래추이"지만 실제로는 시장 전체 집계.
   `MKSC_SHRN_ISCD` 파라미터 완전 무시됨.

3. **data.krx.co.kr**: 모든 엔드포인트에서 로그인 없이 "LOGOUT" 반환 (2025년부터 유료화).
   pykrx의 투자자/공매도 함수들 전부 이 이유로 작동 안 함.

4. **`analysis/why_rally.py`의 `krx_phase_analysis()`**: 정의만 있고 `print_report()`에서 호출 안 함.
   pykrx 의존이라 사실상 사용 불가 — 삭제 또는 KIS 9분류로 교체 가능.

5. **⚠️ 오진단 수정** — FHPTJ04040000/FHPST04820000 "구독 제한"은 사실이 아님:
   - `FHPTJ04040000` → `FID_INPUT_ISCD_1=""` (빈값) 상태에서는 9분류 전부 0 반환. `"KSP"/"KSQ"` 필수.
   - `FHPST04820000` → `FID_COND_SCR_DIV_CODE="20601"` 오류값. `"20482"` 로 수정 시 30종목 반환.
   - 두 API 모두 HTTP 200 + `rt_cd: "0"` (성공 코드)를 반환하면서 데이터만 비어 있었음 → "구독 제한"처럼 보였지만 실제로는 파라미터 오류.

6. **KRX Open API 명명 규칙 함정**: KOSDAQ 지수는 `idx/kosdaq_dd_trd` (`o` 포함). 다른 KOSDAQ 엔드포인트들은 `ksq_*` 약어 사용 (`sto/ksq_bydd_trd`, `sto/ksq_isu_base_info`)인데, 지수만 풀네임. 잘못된 path는 HTTP 404 + `respMsg` 즉시 반환됨.

8. **FHPTJ04040000 날짜 방향**: `FID_INPUT_DATE_1`이 "as-of" (기준일), API가 그 날짜부터 **거꾸로** ~300영업일 반환. `output[0]`이 DATE_1 날짜 (최신), `output[-1]`이 가장 오래된 날짜. DATE_1에 과거 날짜를 넣으면 그보다 더 오래된 300일치가 나옴.

9. **FHPTJ04160001 DATE_1 빈값**: 종목별 투자자 API는 `FID_INPUT_DATE_1=""`이면 가용한 최신 날짜 자동 선택. 오늘 날짜를 넣으면 15:40 이전에는 0행 반환 (당일 집계가 그 시각 이후 확정).

7. **빈 OutBlock_1 vs 404 구분**: KRX Open API는 (a) 데이터 미반영(주말/장 종료 직후) 시 HTTP 200 + `OutBlock_1: []`, (b) path 오타 시 HTTP 404 + `respCode: "404"`. 빈 결과 디버깅 시 `requests.post`로 raw 응답 직접 확인 필요 — `_post()`는 둘 다 `[]` 반환해서 구분 불가.
