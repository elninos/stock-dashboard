"""KIS API — 신용잔고 + 프로그램 매매.

신용잔고:
  - 종목별 일별 신용잔고
  - 개인 과열도 측정 (역지표)

프로그램 매매:
  - 차익거래 / 비차익거래
  - 외국인 시스템 매도 감지
"""
from .kis_api import get_client, rate_limit


def fetch_daily_credit_balance(stock_code: str, start: str, end: str) -> list:
    """일별 신용잔고 추이."""
    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/quotations/daily-credit-balance",
        tr_id="FHPST04760000",
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
        },
    )
    if res.get("rt_cd") != "0":
        return []

    rows = res.get("output", []) or res.get("output2", [])
    out = []
    for r in rows:
        try:
            out.append({
                "date":           r.get("deal_date") or r.get("stck_bsop_date"),
                "credit_balance": int(r.get("ssts_bal_qty", 0)),  # 신용잔고
                "credit_amt":     int(r.get("ssts_tr_pbmn", 0)),
                "credit_pct":     float(r.get("ssts_bal_rlim", 0)),
            })
        except Exception:
            pass
    return out


def fetch_program_trade(stock_code: str = None, start: str = None, end: str = None) -> list:
    """프로그램 매매 추이 (시장 전체 또는 종목별).

    종목별: stock_code 지정
    시장 전체: stock_code 빈값
    """
    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily",
        tr_id="FHPPG04650201",
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code or "",
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
        },
    )
    if res.get("rt_cd") != "0":
        return []

    rows = res.get("output1", []) or res.get("output", [])
    out = []
    for r in rows:
        try:
            out.append({
                "date":         r.get("stck_bsop_date") or r.get("bsop_date"),
                "buy_qty":      int(r.get("whol_smtn_seln_vol", 0)),
                "sell_qty":     int(r.get("whol_smtn_shnu_vol", 0)),
                "net_qty":      int(r.get("whol_smtn_ntby_qty", 0)),
                "arb_buy":      int(r.get("arbt_smtn_seln_vol", 0)),
                "arb_sell":     int(r.get("arbt_smtn_shnu_vol", 0)),
            })
        except Exception:
            pass
    return out
