"""시장환경 분석: KOSPI/KOSDAQ MA20 위치 및 변동성.

pykrx get_index_ohlcv_by_date 버그 회피 → 프록시 ETF 사용:
  KOSPI  → KODEX 200 (069500)
  KOSDAQ → KODEX 코스닥150 (229200)
"""
from datetime import datetime, timedelta


def _get_proxy_ohlcv(etf_code: str, days: int = 60):
    from pykrx import stock as krx
    to = datetime.now()
    frm = to - timedelta(days=days)
    return krx.get_market_ohlcv_by_date(frm.strftime("%Y%m%d"), to.strftime("%Y%m%d"), etf_code)


def analyze_market() -> dict:
    """KOSPI + KOSDAQ 환경 등급 반환."""
    result = {}
    # (label, proxy ETF code, display name)
    items = [("kospi", "069500"), ("kosdaq", "229200")]

    for label, code in items:
        try:
            df = _get_proxy_ohlcv(code, days=60)
            if df is None or len(df) < 20:
                result[label] = {"regime": "중립", "error": "데이터 부족"}
                continue

            close = df["종가"].astype(float)
            price = close.iloc[-1]
            ma20  = close.rolling(20).mean().iloc[-1]
            vol5  = close.pct_change().rolling(5).std().iloc[-1] * 100

            gap_pct = (price - ma20) / ma20 * 100

            if gap_pct > 3:
                regime = "강세"
            elif gap_pct < -3:
                regime = "약세"
            else:
                regime = "중립"

            result[label] = {
                "level": int(price),
                "ma20": round(ma20),
                "ma20_gap_pct": round(gap_pct, 1),
                "vol5d_pct": round(vol5, 2),
                "regime": regime,
            }
        except Exception as e:
            result[label] = {"regime": "중립", "error": str(e)}

    # 종합 환경 등급 (KOSPI 기준)
    kospi_regime = result.get("kospi", {}).get("regime", "중립")
    result["overall"] = kospi_regime
    return result
