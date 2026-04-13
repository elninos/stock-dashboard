#!/usr/bin/env python3
"""
Fetch stock prices using pykrx (Korean stocks) + yfinance (foreign stocks).

Replaces the Naver-based fetching for Korean stocks with pykrx library,
while keeping Yahoo Finance (via yfinance) for foreign stocks.

Outputs:
  - prices.json      : same format as original fetch_prices.py for compatibility
  - price_history.json: daily closing prices for WoW/MoM/YoY calculation

Usage:
  python3 fetch_prices_pykrx.py              # normal run
  python3 fetch_prices_pykrx.py --compare    # compare pykrx vs old Naver method
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

from config import (
    PRICES_FILE, TRANSACTIONS_FILE, STOCK_MAP_FILE,
    TIMEOUT_SHORT, TIMEOUT_MEDIUM,
)
from file_io import load_json, save_json, now_kst
from http_client import http_get_json

# price_history.json is specific to this script (not in config yet)
PRICE_HISTORY_FILE = os.path.join(BASE_DIR, "price_history.json")

SKIP_STOCKS = {"", "Unknown"}

# ---------------------------------------------------------------------------
# Common helpers (shared between methods)
# ---------------------------------------------------------------------------

def load_stock_map():
    """Load saved stock code mappings."""
    return load_json(STOCK_MAP_FILE, default={})


def save_stock_map(stock_map):
    """Save stock code mappings."""
    save_json(STOCK_MAP_FILE, stock_map)


def search_naver(name):
    """Search Naver Finance for stock code and market info."""
    from urllib.parse import quote
    url = f"https://ac.stock.naver.com/ac?q={quote(name)}&target=stock"
    data = http_get_json(url, timeout=TIMEOUT_SHORT)
    if not data:
        return None
    items = data.get("items", [])
    if not items:
        return None
    for item in items:
        if item["name"] == name:
            return {"code": item["code"], "nation": item.get("nationCode", ""), "market": item.get("typeName", "")}
    for item in items:
        if name in item["name"] or item["name"] in name:
            return {"code": item["code"], "nation": item.get("nationCode", ""), "market": item.get("typeName", "")}
    item = items[0]
    return {"code": item["code"], "nation": item.get("nationCode", ""), "market": item.get("typeName", "")}


def get_all_stocks():
    """Get all stock names from transactions."""
    txs = load_json(TRANSACTIONS_FILE, default=[])
    return set(
        tx["stock"] for tx in txs
        if tx["type"] in ("buy", "sell", "dividend", "lending_fee")
        and tx["stock"] not in SKIP_STOCKS
    )


def ensure_stock_map(all_stocks, stock_map):
    """Auto-discover new stocks via Naver search."""
    new_stocks = [s for s in all_stocks if s not in stock_map]
    if new_stocks:
        print(f"=== 신규 종목 {len(new_stocks)}개 자동 매핑 ===")
        for name in sorted(new_stocks):
            result = search_naver(name)
            if result:
                stock_map[name] = result
                print(f"  OK {name} -> {result['code']} ({result['market']})")
            else:
                stock_map[name] = {"code": "", "nation": "", "market": "NOT_FOUND"}
                print(f"  FAIL {name}")
            time.sleep(0.2)
        save_stock_map(stock_map)
        print()
    return stock_map


# ===========================================================================
# METHOD A: pykrx for Korean stocks
# ===========================================================================

def fetch_korean_prices_pykrx(korean_stocks, stock_map):
    """
    Fetch Korean stock prices and 1-year history using pykrx.

    Args:
        korean_stocks: list of (name, code) tuples for KOR stocks
        stock_map: full stock map dict

    Returns:
        prices: dict {name: {"code": ..., "price": ..., "nation": "KOR"}}
        history: dict {name: {"YYYY-MM-DD": close_price, ...}}
        failed: list of names that failed
    """
    try:
        from pykrx import stock as krx
    except ImportError:
        print("ERROR: pykrx is not installed. Run: pip3 install pykrx")
        sys.exit(1)

    prices = {}
    history = {}
    failed = []

    today = datetime.now()
    # Use yesterday if market hasn't closed yet (before 16:00 KST)
    to_date = today.strftime("%Y%m%d")
    from_date = (today - timedelta(days=365)).strftime("%Y%m%d")

    for name, code in korean_stocks:
        try:
            # Fetch 1-year daily OHLCV
            df = krx.get_market_ohlcv_by_date(from_date, to_date, code)

            if df.empty:
                failed.append(name)
                print(f"  FAIL {name} ({code}) - no data from pykrx")
                continue

            # Current price = last closing price
            last_close = int(df["종가"].iloc[-1])

            if last_close <= 0:
                failed.append(name)
                print(f"  FAIL {name} ({code}) - price is 0")
                continue

            prices[name] = {"code": code, "price": last_close, "nation": "KOR"}

            # Build daily history {date_str: close_price}
            stock_history = {}
            for date_idx, row in df.iterrows():
                date_str = date_idx.strftime("%Y-%m-%d")
                stock_history[date_str] = int(row["종가"])
            history[name] = stock_history

            print(f"  OK {name} ({code}): {last_close:,}  [{len(stock_history)} days]")

        except Exception as e:
            failed.append(name)
            print(f"  FAIL {name} ({code}) - {e}")

        # pykrx rate limit: be gentle
        time.sleep(0.3)

    return prices, history, failed


# ===========================================================================
# METHOD B: yfinance for foreign stocks
# ===========================================================================

def fetch_foreign_prices_yfinance(foreign_stocks, stock_map):
    """
    Fetch foreign stock prices and 1-year history using yfinance.

    Args:
        foreign_stocks: list of (name, code, nation) tuples

    Returns:
        prices: dict
        history: dict
        failed: list
    """
    try:
        import yfinance as yf
    except ImportError:
        print("ERROR: yfinance is not installed. Run: pip3 install yfinance")
        sys.exit(1)

    prices = {}
    history = {}
    failed = []

    # Nation -> Yahoo ticker suffix
    yahoo_suffix = {
        "JPN": ".T",
        "CHN": ".SS",
        "HKG": ".HK",
        "USA": "",
    }

    for name, code, nation in foreign_stocks:
        try:
            # Build Yahoo ticker
            suffix = yahoo_suffix.get(nation, "")
            if suffix and not code.endswith(suffix):
                ticker_str = code + suffix
            else:
                ticker_str = code

            ticker = yf.Ticker(ticker_str)

            # Fetch 1-year history
            df = ticker.history(period="1y")

            if df.empty:
                failed.append(name)
                print(f"  FAIL {name} ({ticker_str}) - no data from yfinance")
                continue

            last_close = round(float(df["Close"].iloc[-1]), 2)

            if last_close <= 0:
                failed.append(name)
                print(f"  FAIL {name} ({ticker_str}) - price is 0")
                continue

            prices[name] = {"code": code, "price": last_close, "nation": nation}

            # Build daily history
            stock_history = {}
            for date_idx, row in df.iterrows():
                date_str = date_idx.strftime("%Y-%m-%d")
                stock_history[date_str] = round(float(row["Close"]), 2)
            history[name] = stock_history

            print(f"  OK {name} ({ticker_str}): {last_close:,}  [{len(stock_history)} days]")

        except Exception as e:
            failed.append(name)
            print(f"  FAIL {name} ({code}) - {e}")

        time.sleep(0.2)

    return prices, history, failed


# ===========================================================================
# OLD METHOD: Naver API (for --compare mode)
# ===========================================================================

def fetch_naver_kr_price(code):
    """Fetch Korean stock price from Naver Finance mobile API."""
    data = http_get_json(f"https://m.stock.naver.com/api/stock/{code}/basic", timeout=TIMEOUT_SHORT)
    if not data:
        return None
    price_str = data.get("closePrice", "0").replace(",", "")
    price = int(float(price_str))
    return price if price > 0 else None


def fetch_naver_foreign_price(code):
    """Fetch foreign stock price from Naver Finance."""
    data = http_get_json(f"https://m.stock.naver.com/api/stock/{code}/basic", timeout=TIMEOUT_SHORT)
    if not data:
        return None
    price_str = data.get("closePrice", "0").replace(",", "")
    price = float(price_str)
    return price if price > 0 else None


def fetch_yahoo_price(ticker):
    """Fetch stock price from Yahoo Finance API (fallback for old method)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
    data = http_get_json(url, timeout=TIMEOUT_MEDIUM)
    if not data:
        return None
    try:
        price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return round(price, 2)
    except Exception:
        return None


def fetch_old_method(all_stocks, stock_map):
    """Fetch prices using old Naver-based method (same as fetch_prices.py)."""
    prices = {}
    failed = []

    yahoo_suffix = {
        "JPN": ".T",
        "CHN": ".SS",
        "HKG": ".HK",
    }

    for name in sorted(all_stocks):
        info = stock_map.get(name)
        if not info or not info.get("code"):
            failed.append(name)
            continue

        code = info["code"]
        nation = info.get("nation", "")
        price = None

        if nation == "KOR":
            price = fetch_naver_kr_price(code)
        else:
            price = fetch_naver_foreign_price(code)
            if price is None:
                suffix = yahoo_suffix.get(nation, "")
                yahoo_ticker = code + suffix if suffix and not code.endswith(suffix) else code
                price = fetch_yahoo_price(yahoo_ticker)

        if price:
            prices[name] = {"code": code, "price": price, "nation": nation}
        else:
            failed.append(name)
        time.sleep(0.2)

    return prices, failed


# ===========================================================================
# Main
# ===========================================================================

def run_fetch():
    """Main fetch: pykrx for KOR, yfinance for foreign."""
    stock_map = load_stock_map()
    all_stocks = get_all_stocks()
    stock_map = ensure_stock_map(all_stocks, stock_map)

    # Separate Korean vs foreign stocks
    korean_stocks = []
    foreign_stocks = []
    skipped = []

    for name in sorted(all_stocks):
        info = stock_map.get(name)
        if not info or not info.get("code"):
            skipped.append(name)
            continue

        code = info["code"]
        nation = info.get("nation", "")

        if nation == "KOR":
            korean_stocks.append((name, code))
        elif nation in ("USA", "JPN", "CHN", "HKG"):
            foreign_stocks.append((name, code, nation))
        else:
            # Unknown nation: treat as Korean if code is numeric (6 digits)
            if code.isdigit() and len(code) == 6:
                korean_stocks.append((name, code))
            else:
                foreign_stocks.append((name, code, nation or "USA"))

    # Load existing price history for merging
    existing_history = load_json(PRICE_HISTORY_FILE, default={})

    all_prices = {}
    all_history = existing_history.copy()

    # --- Korean stocks via pykrx ---
    if korean_stocks:
        print(f"=== 한국 주식 ({len(korean_stocks)}개) - pykrx ===")
        kr_prices, kr_history, kr_failed = fetch_korean_prices_pykrx(korean_stocks, stock_map)
        all_prices.update(kr_prices)
        all_history.update(kr_history)
        if kr_failed:
            skipped.extend(kr_failed)
        print()

    # --- Foreign stocks via yfinance ---
    if foreign_stocks:
        print(f"=== 해외 주식 ({len(foreign_stocks)}개) - yfinance ===")
        fg_prices, fg_history, fg_failed = fetch_foreign_prices_yfinance(foreign_stocks, stock_map)
        all_prices.update(fg_prices)
        all_history.update(fg_history)
        if fg_failed:
            skipped.extend(fg_failed)
        print()

    # --- Save prices.json (same format as original) ---
    save_json(PRICES_FILE, all_prices)

    # --- Save price_history.json ---
    save_json(PRICE_HISTORY_FILE, all_history)

    # --- Summary ---
    print("=== Summary ===")
    print(f"Total stocks: {len(all_stocks)}")
    print(f"Updated: {len(all_prices)}")
    print(f"Skipped/Failed: {len(skipped)}")
    if skipped:
        print(f"  {', '.join(sorted(set(skipped)))}")
    print(f"Saved prices to {PRICES_FILE}")
    print(f"Saved history to {PRICE_HISTORY_FILE}")


def run_compare():
    """Compare pykrx vs old Naver method for Korean stocks."""
    stock_map = load_stock_map()
    all_stocks = get_all_stocks()
    stock_map = ensure_stock_map(all_stocks, stock_map)

    # Only compare Korean stocks
    korean_stocks = []
    for name in sorted(all_stocks):
        info = stock_map.get(name)
        if not info or not info.get("code"):
            continue
        if info.get("nation") == "KOR":
            korean_stocks.append((name, info["code"]))

    if not korean_stocks:
        print("No Korean stocks to compare.")
        return

    print(f"=== Comparing {len(korean_stocks)} Korean stocks: pykrx vs Naver ===\n")

    # Fetch via pykrx
    print("--- pykrx ---")
    pykrx_prices, _, pykrx_failed = fetch_korean_prices_pykrx(korean_stocks, stock_map)
    print()

    # Fetch via Naver (old method)
    print("--- Naver (old) ---")
    naver_prices = {}
    naver_failed = []
    for name, code in korean_stocks:
        price = fetch_naver_kr_price(code)
        if price:
            naver_prices[name] = price
            print(f"  OK {name} ({code}): {price:,}")
        else:
            naver_failed.append(name)
            print(f"  FAIL {name} ({code})")
        time.sleep(0.2)
    print()

    # Compare
    print("=== Price Differences ===")
    print(f"{'종목명':<25} {'pykrx':>12} {'Naver':>12} {'차이':>10} {'차이%':>8}")
    print("-" * 70)

    diffs = 0
    for name, code in korean_stocks:
        p_pykrx = pykrx_prices.get(name, {}).get("price")
        p_naver = naver_prices.get(name)

        if p_pykrx is None and p_naver is None:
            status = "BOTH FAIL"
            print(f"  {name:<23} {'FAIL':>12} {'FAIL':>12} {'':>10} {status:>8}")
        elif p_pykrx is None:
            status = "pykrx FAIL"
            print(f"  {name:<23} {'FAIL':>12} {p_naver:>12,} {'':>10} {status:>8}")
        elif p_naver is None:
            status = "Naver FAIL"
            print(f"  {name:<23} {p_pykrx:>12,} {'FAIL':>12} {'':>10} {status:>8}")
        else:
            diff = p_pykrx - p_naver
            if p_naver != 0:
                pct = (diff / p_naver) * 100
            else:
                pct = 0.0
            if diff != 0:
                diffs += 1
            print(f"  {name:<23} {p_pykrx:>12,} {p_naver:>12,} {diff:>+10,} {pct:>+7.2f}%")

    print("-" * 70)
    both_ok = sum(1 for n, _ in korean_stocks
                  if n in pykrx_prices and n in naver_prices)
    print(f"Both OK: {both_ok}, With differences: {diffs}")
    print(f"pykrx failed: {len(pykrx_failed)}, Naver failed: {len(naver_failed)}")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch stock prices (pykrx + yfinance)"
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Compare pykrx vs old Naver method for Korean stocks",
    )
    args = parser.parse_args()

    if args.compare:
        run_compare()
    else:
        run_fetch()


if __name__ == "__main__":
    main()
