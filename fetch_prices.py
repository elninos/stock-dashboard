#!/usr/bin/env python3
"""Fetch current stock prices with auto-discovery of new stocks via Naver Search API."""
import os
import time
from urllib.parse import quote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

from config import (
    TRANSACTIONS_FILE, PRICES_FILE, STOCK_MAP_FILE,
    TIMEOUT_SHORT, TIMEOUT_MEDIUM,
)
from file_io import load_json, save_json, now_kst
from http_client import http_get_json

# Stocks to skip (delisted, liquidated, negligible)
SKIP_STOCKS = {"", "Unknown"}


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


def fetch_naver_kr_price(code):
    """Fetch Korean stock price from Naver Finance mobile API."""
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    data = http_get_json(url, timeout=TIMEOUT_SHORT)
    if not data:
        return None
    price_str = data.get("closePrice", "0").replace(",", "")
    price = int(float(price_str))
    return price if price > 0 else None


def fetch_naver_foreign_price(code):
    """Fetch foreign stock price from Naver Finance."""
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    data = http_get_json(url, timeout=TIMEOUT_SHORT)
    if not data:
        return None
    price_str = data.get("closePrice", "0").replace(",", "")
    price = float(price_str)
    return price if price > 0 else None


def fetch_yahoo_price(ticker):
    """Fetch stock price from Yahoo Finance API (fallback)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
    data = http_get_json(url, timeout=TIMEOUT_MEDIUM)
    if not data:
        return None
    try:
        price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return round(price, 2)
    except Exception:
        return None


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
        price = None

        if nation == "KOR":
            # Korean stock: Naver KR API
            price = fetch_naver_kr_price(code)
        else:
            # Foreign stock: try Naver first, then Yahoo fallback
            price = fetch_naver_foreign_price(code)
            if price is None:
                # Yahoo fallback: add exchange suffix by nation
                yahoo_suffix = {
                    "JPN": ".T",    # Tokyo
                    "CHN": ".SS",   # Shanghai
                    "HKG": ".HK",   # Hong Kong
                }
                suffix = yahoo_suffix.get(nation, "")
                yahoo_ticker = code + suffix if suffix and not code.endswith(suffix) else code
                price = fetch_yahoo_price(yahoo_ticker)

        if price:
            market = info.get("market", "")
            prices[name] = {"code": code, "price": price, "nation": nation, "market": market}
            print(f"  OK {name} ({code}): {price:,}")
            updated += 1
        else:
            failed.append(name)
            print(f"  FAIL {name} ({code})")
        time.sleep(0.2)

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
