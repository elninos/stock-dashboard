#!/usr/bin/env python3
"""Fetch current stock prices via KIS API.

Stock discovery (신규 종목 매핑)은 Naver 검색 그대로 사용 — 가격 조회만 KIS API로 교체.
"""
import os
import sys
import time
from urllib.parse import quote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from config import (
    TRANSACTIONS_FILE, PRICES_FILE, STOCK_MAP_FILE,
    TIMEOUT_SHORT, TIMEOUT_MEDIUM,
)
from file_io import load_json, save_json, now_kst
from http_client import http_get_json
from signals.kis_api import get_client, rate_limit

# Stocks to skip (delisted, liquidated, negligible)
SKIP_STOCKS = {"", "Unknown"}

# nation + market → KIS 해외주식 거래소 코드 (EXCD)
def _resolve_excd(nation: str, market: str = "") -> str:
    """nation/market → KIS EXCD 매핑.

    USA: 나스닥→NAS, 뉴욕→NYS, AMEX→AMS (기본 NAS)
    JPN: TSE / HKG: HKS / CHN: 상해→SHS, 심천→SZS
    """
    m = market or ""
    if nation == "USA":
        if "나스닥" in m or "NASDAQ" in m.upper():
            return "NAS"
        if "뉴욕" in m or "NYSE" in m.upper():
            return "NYS"
        if "AMEX" in m.upper() or "아멕스" in m:
            return "AMS"
        return "NAS"
    if nation == "JPN":
        return "TSE"
    if nation == "HKG":
        return "HKS"
    if nation == "CHN":
        if "심천" in m or "SHENZHEN" in m.upper():
            return "SZS"
        return "SHS"
    return ""


def _strip_suffix(code: str) -> str:
    """'2809.HK', '601012.SS' 등에서 거래소 suffix 제거."""
    if "." in code:
        return code.split(".")[0]
    return code


def load_stock_map():
    """Load saved stock code mappings."""
    return load_json(STOCK_MAP_FILE, default={})


def save_stock_map(stock_map):
    """Save stock code mappings."""
    save_json(STOCK_MAP_FILE, stock_map)


def search_naver(name):
    """Search Naver Finance for stock code and market info.
    Returns: {"code": "005930", "nation": "KOR", "market": "코스피"} or None
    """
    url = f"https://ac.stock.naver.com/ac?q={quote(name)}&target=stock"
    data = http_get_json(url, timeout=TIMEOUT_SHORT)
    if not data:
        return None
    items = data.get("items", [])
    if not items:
        return None
    # Try exact name match first
    for item in items:
        if item["name"] == name:
            return {
                "code": item["code"],
                "nation": item.get("nationCode", ""),
                "market": item.get("typeName", ""),
            }
    # Partial match: check if search name is contained in result
    for item in items:
        if name in item["name"] or item["name"] in name:
            return {
                "code": item["code"],
                "nation": item.get("nationCode", ""),
                "market": item.get("typeName", ""),
            }
    # Fallback: return first result
    item = items[0]
    return {
        "code": item["code"],
        "nation": item.get("nationCode", ""),
        "market": item.get("typeName", ""),
    }


def fetch_kis_kr_price(code):
    """KIS API FHKST01010100 — 한국주식 현재가 (원)."""
    rate_limit()
    try:
        res = get_client().get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
        )
    except Exception:
        return None
    if res.get("rt_cd") != "0":
        return None
    out = res.get("output") or {}
    try:
        price = int(float(out.get("stck_prpr", "0")))
    except (ValueError, TypeError):
        return None
    return price if price > 0 else None


def fetch_kis_foreign_price(code, nation, market=""):
    """KIS API HHDFS76200200 — 해외주식 현재가 + 환율.

    Returns:
        (price, fx_rate) — price는 현지 통화 기준, fx_rate는 KRW/현지통화.
        실패 시 (None, None).
    """
    excd = _resolve_excd(nation, market)
    if not excd:
        return None, None
    symb = _strip_suffix(code)
    rate_limit()
    try:
        res = get_client().get(
            "/uapi/overseas-price/v1/quotations/price-detail",
            "HHDFS76200200",
            {"AUTH": "", "EXCD": excd, "SYMB": symb},
        )
    except Exception:
        return None, None
    if res.get("rt_cd") != "0":
        return None, None
    out = res.get("output") or {}
    try:
        price = float(out.get("last", "0"))
    except (ValueError, TypeError):
        return None, None
    if price <= 0:
        return None, None
    try:
        fx = float(out.get("t_rate", "0")) or None
    except (ValueError, TypeError):
        fx = None
    return round(price, 2), fx


def main():
    # Load existing data
    stock_map = load_stock_map()
    prices = load_json(PRICES_FILE, default={})

    # Find all stocks from transactions
    txs = load_json(TRANSACTIONS_FILE, default=[])
    all_stocks = set(
        tx["stock"] for tx in txs
        if tx["type"] in ("buy", "sell", "dividend", "lending_fee")
        and tx["stock"] not in SKIP_STOCKS
    )

    # Phase 1: Auto-discover new stocks
    new_stocks = [s for s in all_stocks if s not in stock_map]
    if new_stocks:
        print(f"=== 신규 종목 {len(new_stocks)}개 자동 매핑 ===")
        for name in sorted(new_stocks):
            result = search_naver(name)
            if result:
                stock_map[name] = result
                print(f"  OK {name} → {result['code']} ({result['market']})")
            else:
                stock_map[name] = {"code": "", "nation": "", "market": "NOT_FOUND"}
                print(f"  FAIL {name}")
            time.sleep(0.2)
        save_stock_map(stock_map)
        print()

    # Phase 2: Fetch prices
    updated = 0
    failed = []

    print("=== 가격 조회 ===")
    for name in sorted(all_stocks):
        info = stock_map.get(name)
        if not info or not info.get("code"):
            failed.append(name)
            continue

        code = info["code"]
        nation = info.get("nation", "")
        market = info.get("market", "")
        price = None
        fx = None

        if nation == "KOR":
            price = fetch_kis_kr_price(code)
        else:
            price, fx = fetch_kis_foreign_price(code, nation, market)

        if price:
            entry = {"code": code, "price": price, "nation": nation, "market": market}
            if fx:
                entry["fx_rate"] = fx
            prices[name] = entry
            print(f"  OK {name} ({code}): {price:,}")
            updated += 1
        else:
            failed.append(name)
            print(f"  FAIL {name} ({code})")

    # Save with timestamp
    prices["_updated_at"] = now_kst()
    save_json(PRICES_FILE, prices)

    print(f"\n=== Summary ===")
    print(f"Total stocks: {len(all_stocks)}")
    print(f"Updated: {updated}")
    print(f"Failed: {len(failed)}")
    if failed:
        print(f"  {', '.join(failed)}")
    print(f"Saved to {PRICES_FILE}")


if __name__ == "__main__":
    main()
