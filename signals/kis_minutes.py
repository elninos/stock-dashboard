"""KIS API — 분봉 데이터.

당일/일별 분봉 OHLCV.
장중 분배 패턴 분석용.
"""
from .kis_api import get_client, rate_limit


def fetch_minute_chart(stock_code: str, time_str: str = "153000",
                       past_data: bool = True) -> list:
    """주식 당일 분봉 (1분봉, 30개씩).

    time_str: 조회 기준 시각 (HHMMSS, 153000=15:30)
    past_data: True=과거 30분, False=실시간
    """
    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
        tr_id="FHKST03010200",
        params={
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_HOUR_1": time_str,
            "FID_PW_DATA_INCU_YN": "Y" if past_data else "N",
        },
    )
    if res.get("rt_cd") != "0":
        return []

    rows = res.get("output2", [])
    out = []
    for r in rows:
        try:
            out.append({
                "datetime":  r.get("stck_bsop_date") + " " + r.get("stck_cntg_hour", ""),
                "date":      r.get("stck_bsop_date"),
                "time":      r.get("stck_cntg_hour"),
                "open":      int(r.get("stck_oprc", 0)),
                "high":      int(r.get("stck_hgpr", 0)),
                "low":       int(r.get("stck_lwpr", 0)),
                "close":     int(r.get("stck_prpr", 0)),
                "volume":    int(r.get("cntg_vol", 0)),
                "amount":    int(r.get("acml_tr_pbmn", 0)),
            })
        except Exception:
            pass
    return out


def fetch_daily_minutes(stock_code: str, date: str, interval: str = "1") -> list:
    """과거 특정 일자 분봉 (1분/3분/5분/30분).

    interval: 1, 3, 5, 10, 30, 60 등
    date: YYYYMMDD
    """
    client = get_client()
    rate_limit()

    # KIS API에 일자별 분봉 조회는 inquire-time-dailychartprice 또는 별도 API
    res = client.get(
        "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice",
        tr_id="FHKST03010230",
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": date,
            "FID_INPUT_HOUR_1": "153000",
            "FID_PW_DATA_INCU_YN": "Y",
            "FID_FAKE_TICK_INCU_YN": "N",
        },
    )
    if res.get("rt_cd") != "0":
        return []

    out = []
    for r in res.get("output2", []):
        try:
            out.append({
                "date":   r.get("stck_bsop_date"),
                "time":   r.get("stck_cntg_hour"),
                "open":   int(r.get("stck_oprc", 0)),
                "high":   int(r.get("stck_hgpr", 0)),
                "low":    int(r.get("stck_lwpr", 0)),
                "close":  int(r.get("stck_prpr", 0)),
                "volume": int(r.get("cntg_vol", 0)),
            })
        except Exception:
            pass
    return out
