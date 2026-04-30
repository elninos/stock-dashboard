"""KIS API — 공매도 일별 추이.

매도 시그널 핵심:
  - 공매도 잔고율 ≥ 5%
  - 잔고 5일 +30% 급증
  - 당일 공매도 비중 ≥ 30%
"""
from datetime import datetime, timedelta
from .kis_api import get_client, rate_limit, cached_call, smart_ttl


def fetch_daily_short(stock_code: str, start: str = None, end: str = None) -> list:
    """공매도 일별 추이 조회.

    start/end: YYYYMMDD (None이면 최근 30일)
    반환: [{date, close, short_qty, short_amt, short_balance_qty, short_balance_amt, short_ratio, ...}]
    """
    client = get_client()
    rate_limit()

    if not end:
        end = datetime.now().strftime("%Y%m%d")
    if not start:
        start = (datetime.now() - timedelta(days=120)).strftime("%Y%m%d")

    res = client.get(
        "/uapi/domestic-stock/v1/quotations/daily-short-sale",
        tr_id="FHPST04830000",
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
        },
    )
    if res.get("rt_cd") != "0":
        return []

    # output2가 시계열 (output1은 종목 메타정보)
    out = []
    rows = res.get("output2", []) or res.get("output", [])
    for row in rows:
        try:
            out.append({
                "date":               row.get("stck_bsop_date"),
                "close":              int(row.get("stck_clpr", 0)),
                "volume":             int(row.get("acml_vol", 0)),
                # 일별 공매도
                "short_vol":          int(row.get("ssts_cntg_qty", 0)),
                "short_vol_amt":      int(row.get("ssts_tr_pbmn", 0)),
                "short_ratio":        float(row.get("ssts_vol_rlim", 0)),  # 거래량 대비 %
                # 잔고 (누적)
                "short_balance_qty":  int(row.get("acml_ssts_cntg_qty", 0)),
                "short_balance_amt":  int(row.get("acml_ssts_tr_pbmn", 0)),
                "short_balance_pct":  float(row.get("acml_ssts_cntg_qty_rlim", 0)),  # 잔고율 %
            })
        except Exception:
            pass
    return out


def analyze_short_signal(stock_code: str) -> dict:
    """공매도 데이터 분석 — 시그널 추출 (캐싱 적용)."""
    data = cached_call(
        "short", stock_code, smart_ttl("short"),
        lambda: fetch_daily_short(stock_code),
    )
    if not data:
        return {"available": False, "error": "데이터 없음"}

    # 최신순 → 시간순 정렬
    data.sort(key=lambda x: x["date"])

    last = data[-1]
    last_balance_pct = last.get("short_balance_pct", 0)
    last_short_ratio = last.get("short_ratio", 0)

    # 5일 변화율
    bal_5d_pct = None
    if len(data) >= 6:
        prev = data[-6].get("short_balance_qty", 0)
        cur = last.get("short_balance_qty", 0)
        bal_5d_pct = ((cur / prev - 1) * 100) if prev > 0 else 0

    # 시그널
    triggers = []
    score = 0
    if last_balance_pct >= 5.0:
        score += 1
        triggers.append(f"공매도 잔고율 {last_balance_pct:.2f}% (≥5%)")
    if bal_5d_pct is not None and bal_5d_pct >= 30:
        score += 2
        triggers.append(f"공매도 잔고 5일 +{bal_5d_pct:.0f}% 급증")
    if last_short_ratio >= 30.0:
        score += 1
        triggers.append(f"당일 공매도 비중 {last_short_ratio:.1f}%")

    return {
        "available": True,
        "n_days": len(data),
        "last_date": last.get("date"),
        "last_balance_pct": last_balance_pct,
        "last_short_ratio": last_short_ratio,
        "balance_5d_pct": bal_5d_pct,
        "score": score,
        "triggers": triggers,
        "alert": score >= 2,
        "data": data,
    }
