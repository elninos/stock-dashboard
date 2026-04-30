"""KIS API — 지수 OHLCV (KOSPI/KOSDAQ/섹터).

매크로 보정용. yfinance 백업.
"""
from .kis_api import get_client, rate_limit


# 주요 지수 코드
INDEX_CODES = {
    "0001": "KOSPI",
    "0002": "KOSPI 대형주",
    "0003": "KOSPI 중형주",
    "0004": "KOSPI 소형주",
    "1001": "KOSDAQ",
    "1002": "KOSDAQ 대형주",
    "1003": "KOSDAQ 중형주",
    "1004": "KOSDAQ 소형주",
    "2001": "KOSPI 200",
    # 섹터 (KRX)
    "0011": "KOSPI 음식료품",
    "0012": "KOSPI 섬유의복",
    "0013": "KOSPI 종이목재",
    "0014": "KOSPI 화학",
    "0015": "KOSPI 의약품",
    "0016": "KOSPI 비금속광물",
    "0017": "KOSPI 철강금속",
    "0018": "KOSPI 기계",
    "0019": "KOSPI 전기전자",
    "0020": "KOSPI 의료정밀",
    "0021": "KOSPI 운수장비",
    "0022": "KOSPI 유통업",
    "0023": "KOSPI 전기가스업",
    "0024": "KOSPI 건설업",
    "0025": "KOSPI 운수창고",
    "0026": "KOSPI 통신업",
    "0027": "KOSPI 금융업",
    "0028": "KOSPI 은행",
    "0029": "KOSPI 증권",
    "0030": "KOSPI 보험",
    "0031": "KOSPI 서비스업",
    "0032": "KOSPI 제조업",
}


def fetch_index_ohlcv(index_code: str, start: str, end: str, period: str = "D") -> list:
    """지수 일봉/주봉/월봉.

    period: D(일), W(주), M(월), Y(년)
    """
    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
        tr_id="FHKUP03500100",
        params={
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": index_code,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": period,
        },
    )
    if res.get("rt_cd") != "0":
        return []

    rows = res.get("output2", [])
    out = []
    for r in rows:
        try:
            out.append({
                "date":   r.get("stck_bsop_date"),
                "open":   float(r.get("bstp_nmix_oprc", 0)),
                "high":   float(r.get("bstp_nmix_hgpr", 0)),
                "low":    float(r.get("bstp_nmix_lwpr", 0)),
                "close":  float(r.get("bstp_nmix_prpr", 0)),
                "volume": int(r.get("acml_vol", 0)),
            })
        except Exception:
            pass
    return out
