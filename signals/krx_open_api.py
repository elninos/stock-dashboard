"""KRX Open API 연동 모듈.

Base URL : https://data-dbg.krx.co.kr/svc/apis
인증     : AUTH_KEY 헤더 (POST + JSON body)

승인된 API (2026-04-28 기준 전부 동작 확인):
  ✅ sto/stk_bydd_trd       — 유가증권 일별매매정보 (OHLCV + 시가총액 + 상장주식수)
  ✅ sto/ksq_bydd_trd       — 코스닥 일별매매정보
  ✅ sto/stk_isu_base_info  — 유가증권 종목기본정보 (상장일/액면가/주식수)
  ✅ sto/ksq_isu_base_info  — 코스닥 종목기본정보
  ✅ idx/kospi_dd_trd       — KOSPI 시리즈 일별시세
  ✅ idx/krx_dd_trd         — KRX 시리즈 일별시세 (섹터지수, 밸류업지수 포함)
  ✅ idx/kosdaq_dd_trd      — KOSDAQ 시리즈 일별시세
  ✅ etp/etf_bydd_trd       — ETF 일별매매정보 (NAV/추적지수/괴리율)

NOT available (Open API 범위 밖):
  - 투자자별 거래실적 (9분류) → data.krx.co.kr 별도 계정 필요
  - 공매도 잔고             → 동일
  - 프로그램 매매           → 동일
"""
import os, json, time, logging
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

AUTH_KEY = os.getenv("KRX_OPEN_API_KEY", "3DF5ED0699D0463CB90832A76BFD3258831CCA83")
BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"

CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "krx_open_cache"
)
os.makedirs(CACHE_DIR, exist_ok=True)


def _headers() -> dict:
    return {
        "AUTH_KEY":     AUTH_KEY.strip(),
        "Content-Type": "application/json",
        "Accept":       "application/json",
    }


def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.json")


def _fresh(path: str, hours: float = 12) -> bool:
    return os.path.exists(path) and (time.time() - os.path.getmtime(path)) / 3600 < hours


def _post(endpoint: str, payload: dict, cache_key: str = "", hours: float = 12) -> list:
    """POST 요청 + 캐시. OutBlock_1 리스트 반환."""
    if cache_key:
        cp = _cache_path(cache_key)
        if _fresh(cp, hours):
            with open(cp, encoding="utf-8") as f:
                return json.load(f)

    url = f"{BASE_URL}/{endpoint}"
    try:
        resp = requests.post(url, headers=_headers(), json=payload, timeout=15)
    except Exception as e:
        logger.error("KRX Open API 요청 실패 (%s): %s", endpoint, e)
        return []

    if resp.status_code == 401:
        logger.warning("KRX Open API 미승인 또는 키 오류: %s", endpoint)
        return []
    if resp.status_code != 200:
        logger.error("KRX Open API 오류 %s: %s", resp.status_code, resp.text[:200])
        return []

    rows = resp.json().get("OutBlock_1", [])
    if cache_key and rows:
        with open(_cache_path(cache_key), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
    return rows


# ─────────────────────────────────────────────
# 1. 유가증권/코스닥 일별 매매정보
# ─────────────────────────────────────────────

def get_kospi_daily(date: str) -> list[dict]:
    """KOSPI 전 종목 일별 OHLCV + 시가총액 + 상장주식수.

    Args:
        date: YYYYMMDD

    Returns:
        list of {
            BAS_DD, ISU_CD, ISU_NM, MKT_NM, SECT_TP_NM,
            TDD_CLSPRC, CMPPREVDD_PRC, FLUC_RT,
            TDD_OPNPRC, TDD_HGPRC, TDD_LWPRC,
            ACC_TRDVOL, ACC_TRDVAL, MKTCAP, LIST_SHRS
        }
    """
    return _post("sto/stk_bydd_trd", {"basDd": date},
                 cache_key=f"stk_bydd_{date}")


def get_kosdaq_daily(date: str) -> list[dict]:
    """KOSDAQ 전 종목 일별 OHLCV + 시가총액."""
    return _post("sto/ksq_bydd_trd", {"basDd": date},
                 cache_key=f"ksq_bydd_{date}")


def get_stock_price(code: str, date: str) -> Optional[dict]:
    """단일 종목 당일 시세. 없으면 None."""
    # KOSPI
    rows = get_kospi_daily(date)
    for r in rows:
        if r.get("ISU_CD") == code:
            return r
    # KOSDAQ (승인 후 자동 동작)
    rows2 = get_kosdaq_daily(date)
    for r in rows2:
        if r.get("ISU_CD") == code:
            return r
    return None


# ─────────────────────────────────────────────
# 2. 종목 기본정보 (시장 구분, 상장일, 액면가 등)
# ─────────────────────────────────────────────

def get_kospi_base_info(date: str) -> list[dict]:
    """KOSPI 종목기본정보. 상장일, 액면가, 자본금 등."""
    return _post("sto/stk_isu_base_info", {"basDd": date},
                 cache_key=f"stk_base_{date}", hours=24)


def get_kosdaq_base_info(date: str) -> list[dict]:
    """KOSDAQ 종목기본정보."""
    return _post("sto/ksq_isu_base_info", {"basDd": date},
                 cache_key=f"ksq_base_{date}", hours=24)


# ─────────────────────────────────────────────
# 3. 지수 시세
# ─────────────────────────────────────────────

def get_kospi_index(date: str) -> list[dict]:
    """KOSPI 시리즈 일별시세 (KOSPI, KOSPI200, KRX100 등)."""
    return _post("idx/kospi_dd_trd", {"basDd": date},
                 cache_key=f"idx_kospi_{date}")


def get_krx_index(date: str) -> list[dict]:
    """KRX 시리즈 일별시세 (KRX반도체, KRX바이오 등 섹터지수)."""
    return _post("idx/krx_dd_trd", {"basDd": date},
                 cache_key=f"idx_krx_{date}")


def get_kosdaq_index(date: str) -> list[dict]:
    """KOSDAQ 시리즈 일별시세."""
    return _post("idx/kosdaq_dd_trd", {"basDd": date},
                 cache_key=f"idx_kosdaq_{date}")


# ─────────────────────────────────────────────
# 4. ETF
# ─────────────────────────────────────────────

def get_etf_daily(date: str) -> list[dict]:
    """ETF 일별매매정보."""
    return _post("etp/etf_bydd_trd", {"basDd": date},
                 cache_key=f"etf_bydd_{date}")


# ─────────────────────────────────────────────
# 5. 편의 함수 — 날짜 범위 수집
# ─────────────────────────────────────────────

def fetch_kospi_range(start: str, end: str, delay: float = 0.3) -> list[dict]:
    """KOSPI 일별 데이터 날짜 범위 수집.

    Args:
        start/end: YYYYMMDD
        delay: 요청 사이 딜레이(초)

    Returns: 전체 rows (날짜별 합산)
    """
    s = datetime.strptime(start, "%Y%m%d")
    e = datetime.strptime(end,   "%Y%m%d")
    all_rows = []
    cur = s
    while cur <= e:
        if cur.weekday() < 5:
            rows = get_kospi_daily(cur.strftime("%Y%m%d"))
            all_rows.extend(rows)
            if rows:
                time.sleep(delay)
        cur += timedelta(days=1)
    return all_rows


def mktcap_for_code(code: str, date: str) -> Optional[int]:
    """특정 종목의 시가총액 (원). 없으면 None."""
    row = get_stock_price(code, date)
    if row:
        try:
            return int(row["MKTCAP"])
        except (KeyError, ValueError):
            pass
    return None


# ─────────────────────────────────────────────
# CLI 테스트
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    date = sys.argv[1] if len(sys.argv) > 1 else "20260424"
    code = sys.argv[2] if len(sys.argv) > 2 else "010170"

    print(f"\n[KOSPI 일별매매정보] {date}")
    rows = get_kospi_daily(date)
    print(f"  → {len(rows)}개 종목")
    if rows:
        print(f"  컬럼: {list(rows[0].keys())}")

    print(f"\n[{code} 단일 종목]")
    r = get_stock_price(code, date)
    if r:
        import json
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("  데이터 없음")

    print(f"\n[KOSPI 지수]")
    idx = get_kospi_index(date)
    print(f"  → {len(idx)}개 지수")
    if idx:
        print(f"  첫 행: {idx[0]}")

    print(f"\n[KRX 섹터지수]")
    krx = get_krx_index(date)
    print(f"  → {len(krx)}개 지수 (승인 후)")
