#!/usr/bin/env python3
"""Fetch current stock prices with auto-discovery of new stocks via Naver Search API."""
import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse

PRICES_FILE = os.path.join(os.path.dirname(__file__), "prices.json")
TRANSACTIONS_FILE = os.path.join(os.path.dirname(__file__), "transactions.json")
STOCK_MAP_FILE = os.path.join(os.path.dirname(__file__), "stock_map.json")

# Stocks to skip (delisted, liquidated, negligible)
SKIP_STOCKS = {"", "Unknown"}


def load_stock_map():
    """Load saved stock code mappings."""
    if os.path.exists(STOCK_MAP_FILE):
        with open(STOCK_MAP_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_stock_map(stock_map):
    """Save stock code mappings."""
    with open(STOCK_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(stock_map, f, ensure_ascii=False, indent=2)


def search_naver(name):
    """Search Naver Finance for stock code and market info.
    Returns: {"code": "005930", "nation": "KOR", "market": "코스피"} or None
    """
    url = f"https://ac.stock.naver.com/ac?q={urllib.parse.quote(name)}&target=stock"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
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
    except Exception:
        return None


def fetch_naver_kr_price(code):
    """Fetch Korean stock price from Naver Finance mobile API."""
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            price_str = data.get("closePrice", "0").replace(",", "")
            price = int(float(price_str))
            return price if price > 0 else None
    except Exception:
        return None


def fetch_naver_foreign_price(code):
    """Fetch foreign stock price from Naver Finance."""
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            price_str = data.get("closePrice", "0").replace(",", "")
            price = float(price_str)
            return price if price > 0 else None
    except Exception:
        return None


def fetch_yahoo_price(ticker):
    """Fetch stock price from Yahoo Finance API (fallback)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
            return round(price, 2)
    except Exception:
        return None


def main():
    # Load existing data
    stock_map = load_stock_map()
    prices = {}
    if os.path.exists(PRICES_FILE):
        with open(PRICES_FILE, encoding="utf-8") as f:
            prices = json.load(f)

    # Find all stocks from transactions
    with open(TRANSACTIONS_FILE, encoding="utf-8") as f:
        txs = json.load(f)
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
            prices[name] = {"code": code, "price": price, "nation": nation}
            print(f"  OK {name} ({code}): {price:,}")
            updated += 1
        else:
            failed.append(name)
            print(f"  FAIL {name} ({code})")
        time.sleep(0.2)

    # Save
    with open(PRICES_FILE, "w", encoding="utf-8") as f:
        json.dump(prices, f, ensure_ascii=False, indent=2)

    print(f"\n=== Summary ===")
    print(f"Total stocks: {len(all_stocks)}")
    print(f"Updated: {updated}")
    print(f"Failed: {len(failed)}")
    if failed:
        print(f"  {', '.join(failed)}")
    print(f"Saved to {PRICES_FILE}")


if __name__ == "__main__":
    main()
