"""KIS API — 시장 전체 대차거래추이 (매크로 지표).

API: HHPST074500C0 (일별 대차거래추이)

종목코드 파라미터는 무시됨 — 시장 전체 집계를 반환.
신용잔고 + 대차잔고 추이로 시장 레버리지 수준을 파악.

해석:
  대차잔고 급증 → 공매도 예고, 시장 약세 가능
  대차잔고 급감 → 숏커버링, 잠재적 숏스퀴즈
  신규/상환 비율 ≥ 2 → 대차 신규 설정 우위
"""
from datetime import datetime, timedelta
from .kis_api import get_client, rate_limit, cached_call, smart_ttl


def fetch_market_loan(start: str = None, end: str = None, market: str = "1") -> list:
    """시장 전체 대차거래 일별 추이.

    market: "1"=KOSPI, "2"=KOSDAQ
    start/end: YYYYMMDD (None이면 최근 60일)

    반환: [{date, index_price, new_qty, repay_qty, balance_chg,
            balance_qty, balance_amt, volume}]
    - index_price: 해당일 KOSPI/KOSDAQ 지수
    - balance_qty/balance_amt: 시장 전체 대차잔고 (주/원)
    - new_qty/repay_qty: 당일 신규/상환
    """
    if not end:
        end = datetime.now().strftime("%Y%m%d")
    if not start:
        start = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")

    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/quotations/daily-loan-trans",
        tr_id="HHPST074500C0",
        params={
            "MRKT_DIV_CLS_CODE": market,
            "MKSC_SHRN_ISCD":    "",   # 종목코드 무관 — 항상 시장 집계 반환
            "START_DATE":        start,
            "END_DATE":          end,
            "CTS":               "",
        },
    )
    if res.get("rt_cd") != "0":
        return []

    out = []
    for row in res.get("output1", []):
        try:
            out.append({
                "date":        row.get("bsop_date"),
                "index_price": float(row.get("stck_prpr", 0)),    # KOSPI/KOSDAQ 지수
                "change_sign": row.get("prdy_vrss_sign", ""),
                "change":      float(row.get("prdy_vrss", 0)),
                "change_pct":  float(row.get("prdy_ctrt", 0)),
                "volume":      int(row.get("acml_vol", 0)),        # 시장 거래량
                "new_qty":     int(row.get("new_stcn", 0)),        # 당일 대차 신규
                "repay_qty":   int(row.get("rdmp_stcn", 0)),       # 당일 대차 상환
                "balance_chg": int(row.get("prdy_rmnd_vrss", 0)),  # 전일 대비 증감
                "balance_qty": int(row.get("rmnd_stcn", 0)),       # 대차잔고 (주)
                "balance_amt": int(row.get("rmnd_amt", 0)),        # 대차잔고 금액 (백만원)
            })
        except Exception:
            pass
    return out


def analyze_market_loan(market: str = "1") -> dict:
    """시장 전체 대차거래 분석 — 매크로 레버리지 시그널 (캐싱 적용)."""
    cache_key = f"mkt_loan_{market}"
    data = cached_call(
        "mktloan", cache_key, smart_ttl("mktloan"),
        lambda: fetch_market_loan(market=market),
    )
    if not data:
        return {"available": False, "error": "데이터 없음"}

    data.sort(key=lambda x: x["date"])
    last = data[-1]

    balance_qty = last.get("balance_qty", 0)

    def bal_chg_pct(n):
        if len(data) < n + 1:
            return None
        prev = data[-(n + 1)].get("balance_qty", 0)
        return ((balance_qty / prev - 1) * 100) if prev > 0 else 0

    chg_5d  = bal_chg_pct(5)
    chg_20d = bal_chg_pct(20)

    new_5d   = sum(d.get("new_qty",   0) for d in data[-5:])
    repay_5d = sum(d.get("repay_qty", 0) for d in data[-5:])
    net_5d   = sum(d.get("balance_chg", 0) for d in data[-5:])

    triggers = []
    score = 0

    if chg_5d is not None and chg_5d >= 5:
        score += 2
        triggers.append(f"시장 대차잔고 5일 +{chg_5d:.1f}% (공매도 예고)")
    if chg_20d is not None and chg_20d >= 10:
        score += 1
        triggers.append(f"시장 대차잔고 20일 +{chg_20d:.1f}% 증가")
    if new_5d > 0 and repay_5d > 0 and (new_5d / max(repay_5d, 1)) >= 2:
        score += 1
        triggers.append(f"5일 신규/상환 {new_5d/max(repay_5d,1):.1f}배 (대차 신규 우위)")
    if chg_5d is not None and chg_5d <= -5:
        triggers.append(f"시장 대차잔고 5일 {chg_5d:.1f}% 감소 (숏커버링)")
    if repay_5d > new_5d * 2 and repay_5d > 0:
        triggers.append(f"5일 순상환 {(repay_5d - new_5d)/1e8:.2f}억주 (숏스퀴즈 가능)")

    market_name = "KOSPI" if market == "1" else "KOSDAQ"
    return {
        "available":       True,
        "market":          market_name,
        "n_days":          len(data),
        "last_date":       last.get("date"),
        "index_price":     last.get("index_price", 0),
        "balance_qty":     balance_qty,
        "balance_amt":     last.get("balance_amt", 0),
        "balance_5d_pct":  chg_5d,
        "balance_20d_pct": chg_20d,
        "new_5d":          new_5d,
        "repay_5d":        repay_5d,
        "net_5d":          net_5d,
        "score":           score,
        "triggers":        triggers,
        "alert":           score >= 2,
        "data":            data,
    }
