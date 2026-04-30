"""KIS API — 거래원 일별 매매 시계열 (FHPST04540000).

NH HTS [1502] 거래원 기간별분석의 KIS API 버전.
사용자 daily_flow를 자동으로 대체.

핵심:
  fetch_member_daily(stock_code, broker_code, start, end)
  → 단일 거래원의 일별 매매

  fetch_all_brokers_daily(stock_code, start, end)
  → 모든 활성 거래원 일별 매매 (자동 탐지)

거래원 코드 ↔ 이름 매핑:
  - 스냅샷 API (inquire-member, FHKST01010600)에 코드+이름 함께 제공
  - build_broker_mapping()으로 자동 구축
  - data/kis_cache/broker_names.json 에 저장
"""
import os, json, time
from .kis_api import get_client, rate_limit, CACHE_DIR

BROKER_NAMES_FILE = os.path.join(CACHE_DIR, "broker_names.json")


def load_broker_names() -> dict:
    """저장된 거래원 코드↔이름 매핑 로드."""
    if not os.path.exists(BROKER_NAMES_FILE):
        return {}
    try:
        with open(BROKER_NAMES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_broker_names(mapping: dict):
    with open(BROKER_NAMES_FILE, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)


def build_broker_mapping(stock_codes: list, force_rebuild: bool = False) -> dict:
    """여러 종목의 스냅샷 API를 호출해 거래원 코드↔이름 매핑 자동 구축.

    스냅샷에는 매수/매도 TOP 5 거래원의 코드(seln_mbcr_no, shnu_mbcr_no)와
    이름(seln_mbcr_name, shnu_mbcr_name)이 함께 제공됨.

    여러 종목 호출 시 다양한 거래원이 등장 → 매핑 점진 구축.
    """
    mapping = {} if force_rebuild else load_broker_names()
    initial_count = len(mapping)

    client = get_client()

    for stock_code in stock_codes:
        rate_limit()
        try:
            res = client.get(
                "/uapi/domestic-stock/v1/quotations/inquire-member",
                tr_id="FHKST01010600",
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": stock_code,
                },
            )
            if res.get("rt_cd") != "0":
                continue
            output = res.get("output", [])
            if not output: continue
            row = output[0] if isinstance(output, list) else output

            # 매도 거래원 1~5
            for i in range(1, 6):
                code = row.get(f"seln_mbcr_no{i}")
                name = row.get(f"seln_mbcr_name{i}", "").strip()
                if code and name:
                    mapping[code] = name

            # 매수 거래원 1~5
            for i in range(1, 6):
                code = row.get(f"shnu_mbcr_no{i}")
                name = row.get(f"shnu_mbcr_name{i}", "").strip()
                if code and name:
                    mapping[code] = name
        except Exception as e:
            continue

    new_count = len(mapping)
    if new_count > initial_count:
        save_broker_names(mapping)
    return mapping


def fetch_member_daily(stock_code: str, broker_code: str, start: str, end: str) -> list:
    """단일 거래원 × 단일 종목의 일별 매매.

    반환:
      [{date, close, buy, sell, net, total_vol}]
    """
    client = get_client()
    rate_limit()
    res = client.get(
        "/uapi/domestic-stock/v1/quotations/inquire-member-daily",
        tr_id="FHPST04540000",
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": stock_code,
            "FID_INPUT_ISCD_2": broker_code,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_SCTN_CLS_CODE": "",
        },
    )
    if res.get("rt_cd") != "0":
        return []

    rows = res.get("output", [])
    cleaned = []
    for r in rows:
        try:
            cleaned.append({
                "date":      r.get("stck_bsop_date"),
                "close":     int(r.get("stck_prpr", 0)),
                "buy":       int(r.get("total_shnu_qty", 0)),
                "sell":      int(r.get("total_seln_qty", 0)),
                "net":       int(r.get("ntby_qty", 0)),
                "total_vol": int(r.get("acml_vol", 0)),
            })
        except Exception:
            pass
    return cleaned


def fetch_all_brokers_daily(stock_code: str, start: str, end: str,
                             broker_codes: list = None,
                             min_vol: int = 100,
                             show_progress: bool = False) -> dict:
    """여러 거래원의 일별 매매 일괄 수집.

    broker_codes: 지정 안 하면 00001~00099 전체 시도
    min_vol: 매수+매도 합이 이 값 미만인 거래원은 제외 (노이즈 제거)

    반환: {broker_code: [{date, buy, sell, net, ...}]}
    """
    if broker_codes is None:
        broker_codes = [f"{i:05d}" for i in range(1, 100)]

    results = {}
    for i, code in enumerate(broker_codes):
        if show_progress and (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(broker_codes)} 거래원 호출 중...")
        rows = fetch_member_daily(stock_code, code, start, end)
        if not rows: continue
        total = sum(r["buy"] + r["sell"] for r in rows)
        if total >= min_vol:
            results[code] = rows
    return results


def aggregate_to_dataframe(results: dict, broker_names: dict = None):
    """{code: rows} 형식을 단일 DataFrame으로 변환.

    각 행: {date, broker_code, broker_name, buy, sell, net, close}
    """
    import pandas as pd
    if broker_names is None:
        broker_names = load_broker_names()

    records = []
    for code, rows in results.items():
        for r in rows:
            records.append({
                "date":         r["date"],
                "broker_code":  code,
                "broker_name":  broker_names.get(code, f"unknown_{code}"),
                "buy":          r["buy"],
                "sell":         r["sell"],
                "net":          r["net"],
                "close":        r.get("close", 0),
            })
    if not records:
        return None
    return pd.DataFrame(records)
