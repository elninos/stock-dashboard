"""NAVER Finance front-api — 시장 투자자 동향 (로그인 불필요).

Base: https://m.stock.naver.com/front-api/market/

APIs:
  tradingTrend/graphInfo  — 시장 3분류 순매수 금액 (daily/weekly/monthly)
  tradingTrend/ranking    — 투자자별 순매수/순매도 상위 10 종목

한계:
  - 3분류 (외국인/기관/개인) 집계만 제공 (9분류 불가)
  - graphInfo는 당일 집계만 제공 (과거 날짜 지정 불가)
  - 공매도 순위는 이 API에서 제공하지 않음
"""
import time
import requests
from .kis_api import cached_call, smart_ttl

_BASE = "https://m.stock.naver.com/front-api/market"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)",
    "Referer":    "https://m.stock.naver.com/",
    "Accept":     "application/json",
}


def _get(path: str, params: dict = None) -> dict:
    try:
        resp = requests.get(
            f"{_BASE}/{path}",
            params=params or {},
            headers=_HEADERS,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("isSuccess"):
                return data.get("result", {})
    except Exception:
        pass
    return {}


def fetch_market_investor_summary(period: str = "daily") -> dict:
    """시장 전체 3분류 순매수 금액 (원).

    period: "daily" | "weekly" | "monthly"
    반환: {
        period, start_date, end_date,
        frgn_amt(외국인), orgn_amt(기관), prsn_amt(개인)  — 단위: 원
    }
    """
    result = _get("tradingTrend/graphInfo", {"periodType": period})
    if not result:
        return {}
    return {
        "period":     result.get("periodType", period),
        "start_date": result.get("startDate", ""),
        "end_date":   result.get("endDate", ""),
        "frgn_amt":   result.get("foreignerNetBuyAmount", 0),
        "orgn_amt":   result.get("organizationNetBuyAmount", 0),
        "prsn_amt":   result.get("individualNetBuyAmount", 0),
    }


def fetch_investor_ranking(
    investor: str = "foreigner",
    side: str = "trendBuy",
    period: str = "daily",
) -> list:
    """투자자별 순매수/순매도 상위 10 종목.

    investor: "foreigner" | "organization" | "individual"
    side:     "trendBuy"  (순매수) | "trendSell" (순매도)
    period:   "daily" | "weekly" | "monthly"
    반환: [{code, name, close, change_pct, volume, exchange}]
    """
    result = _get("tradingTrend/ranking", {
        "periodType":  period,
        "investorType": investor,
        "tradingType":  side,
    })
    stocks = result.get("stocks", []) if result else []

    out = []
    for s in stocks:
        try:
            out.append({
                "code":       s.get("itemCode", ""),
                "name":       s.get("itemName", ""),
                "close":      s.get("closePrice", "").replace(",", ""),
                "change_pct": float(s.get("fluctuationsRatio", 0)),
                "volume":     s.get("accumulatedTradingVolume", "").replace(",", ""),
                "exchange":   s.get("stockExchangeType", {}).get("name", ""),
            })
        except Exception:
            pass
    return out


def get_market_flow_snapshot() -> dict:
    """시장 수급 현황 스냅샷 (캐싱 적용). 대시보드 매크로 컨텍스트용."""
    summary = cached_call(
        "naver_mkt_flow", "daily", smart_ttl("naver_mkt_flow"),
        lambda: fetch_market_investor_summary("daily"),
    )
    if not summary:
        return {"available": False}

    frgn = summary.get("frgn_amt", 0)
    orgn = summary.get("orgn_amt", 0)
    prsn = summary.get("prsn_amt", 0)

    triggers = []
    if frgn > 0 and orgn > 0:
        triggers.append(f"외국인+기관 동반 매수 ({(frgn+orgn)/1e8:.0f}억)")
    elif frgn < -3e11 and prsn > 3e11:
        triggers.append(f"분배 패턴: 외국인 {frgn/1e8:.0f}억 매도 ↔ 개인 {prsn/1e8:.0f}억 매수")
    if abs(frgn) > 5e11:
        direction = "매수" if frgn > 0 else "매도"
        triggers.append(f"외국인 강도 높은 {direction} ({frgn/1e8:.0f}억)")

    return {
        "available":   True,
        "date":        summary.get("end_date", ""),
        "frgn_amt":    frgn,
        "orgn_amt":    orgn,
        "prsn_amt":    prsn,
        "frgn_100m":   frgn / 1e8,   # 억원 단위
        "orgn_100m":   orgn / 1e8,
        "prsn_100m":   prsn / 1e8,
        "triggers":    triggers,
    }
