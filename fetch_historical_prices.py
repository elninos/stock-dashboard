#!/usr/bin/env python3
"""Fetch monthly closing prices for all stocks (2014~now) using pykrx + yfinance.
Outputs historical_prices.json: {stock_name: {"2024-01": price, "2024-02": price, ...}}
"""
import json
import os
import time
from datetime import datetime

DIR = os.path.dirname(__file__)
STOCK_MAP_FILE = os.path.join(DIR, "stock_map.json")
TRANSACTIONS_FILE = os.path.join(DIR, "transactions.json")
OUTPUT_FILE = os.path.join(DIR, "historical_prices.json")

START_DATE = "20140101"
END_DATE = datetime.now().strftime("%Y%m%d")


def get_active_stocks():
    """Find stocks that were actually held (had buys) from transactions."""
    with open(TRANSACTIONS_FILE, encoding="utf-8") as f:
        txs = json.load(f)
    stocks = set()
    for tx in txs:
        if tx["type"] in ("buy", "sell", "transfer_in", "transfer_out") and tx["stock"]:
            stocks.add(tx["stock"])
    stocks.discard("")
    stocks.discard("Unknown")
    return stocks


def fetch_kr_monthly(code, name):
    """Fetch Korean stock monthly closing prices via pykrx."""
    from pykrx import stock as krx
    try:
        df = krx.get_market_ohlcv_by_date(START_DATE, END_DATE, code)
        if df.empty:
            return {}
        monthly = df.resample("ME").last()
        result = {}
        for date, row in monthly.iterrows():
            mk = date.strftime("%Y-%m")
            price = int(row["종가"])
            if price > 0:
                result[mk] = price
        return result
    except Exception as e:
        print(f"    ERROR {name} ({code}): {e}")
        return {}


def fetch_foreign_monthly(code, name):
    """Fetch foreign stock monthly closing prices via yfinance."""
    import yfinance as yf
    try:
        ticker = yf.Ticker(code)
        hist = ticker.history(start="2014-01-01", interval="1mo")
        if hist.empty:
            # Try daily and resample
            hist = ticker.history(start="2014-01-01", interval="1d")
            if hist.empty:
                return {}
            hist = hist.resample("ME").last()
        result = {}
        for date, row in hist.iterrows():
            mk = date.strftime("%Y-%m")
            price = round(float(row["Close"]), 2)
            if price > 0:
                result[mk] = price
        return result
    except Exception as e:
        print(f"    ERROR {name} ({code}): {e}")
        return {}


def fetch_jpn_monthly(code, name):
    """Fetch Japanese stock via yfinance with .T suffix."""
    suffix = ".T" if not code.endswith(".T") else ""
    return fetch_foreign_monthly(code + suffix, name)


def main():
    with open(STOCK_MAP_FILE, encoding="utf-8") as f:
        stock_map = json.load(f)

    active_stocks = get_active_stocks()
    print(f"Active stocks: {len(active_stocks)}")

    # Load existing data to resume
    historical = {}
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            historical = json.load(f)

    kr_stocks = []
    foreign_stocks = []
    skipped = []

    for name in sorted(active_stocks):
        info = stock_map.get(name)
        if not info or not info.get("code"):
            skipped.append(name)
            continue
        if name in historical and len(historical[name]) > 0:
            continue  # Already fetched
        nation = info.get("nation", "")
        if nation == "KOR":
            kr_stocks.append((name, info["code"]))
        else:
            foreign_stocks.append((name, info["code"], nation))

    print(f"To fetch: {len(kr_stocks)} KR, {len(foreign_stocks)} foreign")
    print(f"Already fetched: {len(historical)}")
    if skipped:
        print(f"Skipped (no code): {len(skipped)}")

    # Fetch Korean stocks via pykrx
    if kr_stocks:
        print(f"\n=== Korean stocks ({len(kr_stocks)}) ===")
        for i, (name, code) in enumerate(kr_stocks):
            print(f"  [{i+1}/{len(kr_stocks)}] {name} ({code})...", end=" ", flush=True)
            data = fetch_kr_monthly(code, name)
            if data:
                historical[name] = data
                print(f"OK ({len(data)} months)")
            else:
                print("EMPTY")
            time.sleep(0.5)

            # Save periodically
            if (i + 1) % 20 == 0:
                with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
                    json.dump(historical, f, ensure_ascii=False, indent=2)

    # Fetch foreign stocks via yfinance
    if foreign_stocks:
        print(f"\n=== Foreign stocks ({len(foreign_stocks)}) ===")
        for i, (name, code, nation) in enumerate(foreign_stocks):
            print(f"  [{i+1}/{len(foreign_stocks)}] {name} ({code})...", end=" ", flush=True)
            if nation == "JPN":
                data = fetch_jpn_monthly(code, name)
            else:
                data = fetch_foreign_monthly(code, name)
            if data:
                historical[name] = data
                print(f"OK ({len(data)} months)")
            else:
                print("EMPTY")
            time.sleep(0.3)

    # Final save
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(historical, f, ensure_ascii=False, indent=2)

    # Summary
    total_with_data = sum(1 for v in historical.values() if v)
    print(f"\n=== Summary ===")
    print(f"Total stocks with history: {total_with_data}")
    print(f"Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
