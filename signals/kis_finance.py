"""KIS API — 재무제표 / 재무비율.

펀더멘털 분석용:
  - 손익계산서 (분기/연간 매출, 영업이익, 순이익)
  - 대차대조표 (자산, 부채, 자본)
  - 재무비율 (ROE, ROA, 부채비율, PER, PBR)
"""
from .kis_api import get_client, rate_limit


def fetch_balance_sheet(stock_code: str, period: str = "0") -> list:
    """대차대조표.

    period: 0=연간, 1=분기
    """
    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/finance/balance-sheet",
        tr_id="FHKST66430100",
        params={
            "FID_DIV_CLS_CODE": period,
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code,
        },
    )
    if res.get("rt_cd") != "0":
        return []

    return res.get("output", [])


def fetch_income_statement(stock_code: str, period: str = "0") -> list:
    """손익계산서.

    period: 0=연간, 1=분기
    """
    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/finance/income-statement",
        tr_id="FHKST66430200",
        params={
            "FID_DIV_CLS_CODE": period,
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code,
        },
    )
    if res.get("rt_cd") != "0":
        return []

    return res.get("output", [])


def fetch_financial_ratio(stock_code: str, period: str = "0") -> list:
    """재무비율 (ROE, ROA, 부채비율 등)."""
    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/finance/financial-ratio",
        tr_id="FHKST66430300",
        params={
            "FID_DIV_CLS_CODE": period,
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code,
        },
    )
    if res.get("rt_cd") != "0":
        return []

    return res.get("output", [])


def fetch_profit_ratio(stock_code: str, period: str = "0") -> list:
    """수익성 비율."""
    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/finance/profit-ratio",
        tr_id="FHKST66430400",
        params={
            "FID_DIV_CLS_CODE": period,
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code,
        },
    )
    if res.get("rt_cd") != "0":
        return []

    return res.get("output", [])
