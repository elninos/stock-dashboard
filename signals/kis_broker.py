"""KIS API — 회원사(거래원) 매매동향.

응답: 매수/매도 거래원 TOP 5 + 외국계 합산.

필드:
  seln_mbcr_name{1-5}  : 매도 거래원 1~5위
  total_seln_qty{1-5}  : 매도 누적수량
  seln_mbcr_rlim{1-5}  : 매도 점유율 (%)
  shnu_mbcr_name{1-5}  : 매수 거래원 1~5위
  total_shnu_qty{1-5}  : 매수 누적수량
  shnu_mbcr_rlim{1-5}  : 매수 점유율 (%)
  seln_mbcr_glob_yn_{1-5}: 외국계 여부 (Y/N)
  glob_ntby_qty        : 외국계 순매수
  glob_shnu_rlim       : 외국계 매수 점유율 (%)
"""
from .kis_api import get_client, rate_limit, cached_call, smart_ttl


def fetch_broker_now(stock_code: str) -> dict:
    """현재 시점 매수/매도 거래원 TOP 5."""
    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/quotations/inquire-member",
        tr_id="FHKST01010600",
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
        },
    )
    if res.get("rt_cd") != "0":
        return {"available": False, "error": res.get("msg1", "조회 실패")}

    out_list = res.get("output", [])
    if not out_list:
        return {"available": False, "error": "응답 없음"}
    out = out_list[0]

    # 매수 거래원 TOP 5
    buy_brokers = []
    for i in range(1, 6):
        name = out.get(f"shnu_mbcr_name{i}", "").strip()
        if not name: continue
        buy_brokers.append({
            "rank":    i,
            "broker":  name,
            "qty":     int(out.get(f"total_shnu_qty{i}", 0)),
            "share":   float(out.get(f"shnu_mbcr_rlim{i}", 0)),
            "is_foreign": out.get(f"shnu_mbcr_glob_yn_{i}") == "Y",
        })

    # 매도 거래원 TOP 5
    sell_brokers = []
    for i in range(1, 6):
        name = out.get(f"seln_mbcr_name{i}", "").strip()
        if not name: continue
        sell_brokers.append({
            "rank":    i,
            "broker":  name,
            "qty":     int(out.get(f"total_seln_qty{i}", 0)),
            "share":   float(out.get(f"seln_mbcr_rlim{i}", 0)),
            "is_foreign": out.get(f"seln_mbcr_glob_yn_{i}") == "Y",
        })

    # 외국계 합산
    glob = {
        "buy_qty":     int(out.get("glob_total_shnu_qty", 0)),
        "sell_qty":    int(out.get("glob_total_seln_qty", 0)),
        "net_qty":     int(out.get("glob_ntby_qty", 0)),
        "buy_share":   float(out.get("glob_shnu_rlim", 0)),
        "sell_share":  float(out.get("glob_seln_rlim", 0)),
    }

    return {
        "available":    True,
        "buy_brokers":  buy_brokers,
        "sell_brokers": sell_brokers,
        "global":       glob,
        "total_volume": int(out.get("acml_vol", 0)),
    }


def analyze_broker_signal(stock_code: str) -> dict:
    """거래원 패턴 시그널 (캐싱 적용).

    매집 패턴: 외국계가 매수 TOP에 있고 점유율 합 높음
    분배 패턴: 키움/토스 등 개미 창구가 매수, 대형 증권사가 매도
    """
    res = cached_call(
        "broker", stock_code, smart_ttl("broker"),
        lambda: fetch_broker_now(stock_code),
    )
    if not res.get("available"):
        return res

    buys = res["buy_brokers"]
    sells = res["sell_brokers"]
    glob = res["global"]

    # 매수 측 외국계 점유율
    foreign_buy_share = sum(b["share"] for b in buys if b["is_foreign"])
    foreign_sell_share = sum(b["share"] for b in sells if b["is_foreign"])

    # 개미 창구 (키움/토스/카카오/상상인)
    RETAIL_BROKERS = {"키움증권", "토스증권", "카카오페이증권", "상상인증권"}
    LARGE_BROKERS = {"NH투자증권", "한국증권", "삼성증권", "한화투자증권",
                       "미래에셋증권", "신한증권", "하나금투", "KB증권"}

    retail_buy_share = sum(b["share"] for b in buys if b["broker"] in RETAIL_BROKERS)
    retail_sell_share = sum(b["share"] for b in sells if b["broker"] in RETAIL_BROKERS)
    large_sell_share = sum(b["share"] for b in sells if b["broker"] in LARGE_BROKERS)

    triggers = []

    # 매집 시그널
    if foreign_buy_share >= 20:
        triggers.append({
            "type": "buy",
            "label": f"📈 외국계 매수 점유율 {foreign_buy_share:.1f}% (매집 가능)",
        })

    # 분배 시그널 (개미 매수 + 대형 매도)
    if retail_buy_share >= 20 and large_sell_share >= 25:
        triggers.append({
            "type": "sell",
            "label": f"🚨 분배 패턴: 개미 매수 {retail_buy_share:.0f}% ↔ 대형 매도 {large_sell_share:.0f}%",
        })

    # 외국계 순매도
    if glob["net_qty"] < 0 and abs(glob["net_qty"]) > res["total_volume"] * 0.05:
        net_pct = glob["net_qty"] / res["total_volume"] * 100
        triggers.append({
            "type": "sell",
            "label": f"⚠️ 외국계 순매도 {abs(glob['net_qty']):,}주 (전체 거래량 {abs(net_pct):.1f}%)",
        })

    retail_sell_share = sum(b["share"] for b in sells if b["broker"] in RETAIL_BROKERS)
    large_buy_share = sum(b["share"] for b in buys if b["broker"] in LARGE_BROKERS)

    return {
        **res,
        "foreign_buy_share":  foreign_buy_share,
        "foreign_sell_share": foreign_sell_share,
        "retail_buy_share":   retail_buy_share,
        "retail_sell_share":  retail_sell_share,
        "large_buy_share":    large_buy_share,
        "large_sell_share":   large_sell_share,
        "triggers":           triggers,
    }
