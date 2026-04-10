#!/usr/bin/env python3
"""
기준일별 포트폴리오 평가금액 계산 (기간별 수익률용)

기준일: 전월말, 전분기말, 전년말(YTD), 1년 전(T12M)
각 기준일에 보유 중이던 종목의 종가를 조회해서 포트폴리오 가치를 계산한다.
- 한국주식: pykrx
- 해외주식: yfinance
- 환율: yfinance (KRW=X 등)

Output: historical_portfolio_values.json
{
  "_updated": "2026-04-10",
  "_key_dates": {"ytd": "2025-12-31", "qtd": "2026-03-31", ...},
  "2025-12-31": {"portfolio_value": 950000000, "stocks": {...}},
  ...
}

Usage:
  python3 fetch_historical_prices.py            # 오늘 날짜 기준 계산 (캐시 있으면 스킵)
  python3 fetch_historical_prices.py --force    # 강제 재계산
"""

import json, os, sys, time
from collections import defaultdict
from datetime import datetime, timedelta, date

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
TXS_FILE    = os.path.join(BASE_DIR, "transactions.json")
PRICES_FILE = os.path.join(BASE_DIR, "prices.json")
OUTPUT_FILE = os.path.join(BASE_DIR, "historical_portfolio_values.json")

# 환율 코드 (→ KRW)
FX_TICKERS = {
    "USD": "KRW=X",
    "JPY": "JPYKRW=X",
    "HKD": "HKDKRW=X",
    "CNY": "CNYKRW=X",
}
NATION_CURRENCY = {
    "KOR": "KRW", "USA": "USD", "JPN": "JPY",
    "HKG": "HKD", "CHN": "CNY",
}
YAHOO_SUFFIX = {"JPN": ".T", "CHN": ".SS", "HKG": ".HK", "USA": ""}

# ───────────────────────── 기준일 계산 ─────────────────────────

def get_key_dates():
    today = date.today()
    y = today.year
    q = (today.month - 1) // 3 + 1

    # 전월말
    mtd_base = today.replace(day=1) - timedelta(days=1)

    # 전분기말
    q_start_month = (q - 1) * 3 + 1
    if q_start_month == 1:
        qtd_base = date(y - 1, 12, 31)
    else:
        qtd_base = date(y, q_start_month, 1) - timedelta(days=1)

    # 전년말 (YTD 기준)
    ytd_base = date(y - 1, 12, 31)

    # 1년 전 전날 (T12M 기준)
    try:
        t12m_base = today.replace(year=y - 1) - timedelta(days=1)
    except ValueError:
        t12m_base = today.replace(year=y - 1, day=28) - timedelta(days=1)

    return {"mtd": mtd_base, "qtd": qtd_base, "ytd": ytd_base, "t12m": t12m_base}

# ───────────────────────── FIFO 보유량 재구성 ─────────────────────────

def build_holdings_at(txs_sorted, target_date_str):
    """target_date_str 당일 종가 기준 보유 종목 {name: {qty, cost, fifo}}"""
    holdings = defaultdict(lambda: {"qty": 0.0, "cost": 0.0, "fifo": []})
    for tx in txs_sorted:
        if tx["date"] > target_date_str:
            break
        stock = tx.get("stock", "")
        if not stock or tx["type"] not in ("buy", "sell", "transfer_in", "transfer_out"):
            continue
        h = holdings[stock]
        if tx["type"] in ("buy", "transfer_in"):
            qty   = tx.get("qty", 0)
            price = tx.get("price", 0) or (tx.get("amount", 0) / qty if qty else 0)
            h["qty"]  += qty
            h["cost"] += tx.get("amount", qty * price)
            h["fifo"].append({"qty": qty, "price": price})
        else:  # sell / transfer_out
            qty = tx.get("qty", 0)
            rem = qty
            while rem > 0 and h["fifo"]:
                b    = h["fifo"][0]
                take = min(rem, b["qty"])
                h["cost"] -= take * b["price"]
                b["qty"]  -= take
                rem       -= take
                if b["qty"] == 0:
                    h["fifo"].pop(0)
            h["qty"] -= qty
            if h["qty"] <= 0:
                h["qty"] = 0; h["cost"] = 0; h["fifo"] = []

    return {n: h for n, h in holdings.items() if h["qty"] > 0.001}

# ───────────────────────── 가격 조회 ─────────────────────────

def krx_price_at(krx, code, target: date):
    """pykrx로 기준일 또는 직전 거래일 종가 반환"""
    for i in range(10):
        d = target - timedelta(days=i)
        d_str = d.strftime("%Y%m%d")
        try:
            df = krx.get_market_ohlcv_by_date(d_str, d_str, code)
            if not df.empty and int(df["종가"].iloc[-1]) > 0:
                return int(df["종가"].iloc[-1]), d
        except Exception:
            pass
        time.sleep(0.15)
    return None, None

def yf_price_at(ticker_str, target: date):
    """yfinance로 기준일 또는 직전 거래일 종가 반환"""
    import yfinance as yf
    start = (target - timedelta(days=14)).strftime("%Y-%m-%d")
    end   = (target + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        df = yf.download(ticker_str, start=start, end=end,
                         progress=False, auto_adjust=True)
        if not df.empty:
            close = df["Close"].iloc[-1]
            if hasattr(close, "iloc"):
                close = float(close.iloc[0])
            else:
                close = float(close)
            return round(close, 4), df.index[-1].date()
    except Exception:
        pass
    return None, None

def fx_rate_at(currency, target: date, fx_cache):
    """기준일 환율 (→ KRW). 캐시 활용."""
    if currency == "KRW":
        return 1.0
    key = f"{currency}_{target}"
    if key in fx_cache:
        return fx_cache[key]
    ticker_str = FX_TICKERS.get(currency)
    if not ticker_str:
        return 1300.0
    price, _ = yf_price_at(ticker_str, target)
    rate = price if price else None
    fx_cache[key] = rate
    return rate

# ───────────────────────── 메인 ─────────────────────────

def main(force=False):
    today_str = date.today().strftime("%Y-%m-%d")

    # 캐시 유효 체크
    if not force and os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            existing = json.load(f)
        if existing.get("_updated") == today_str:
            print("✓ 오늘 이미 계산됨. 스킵 (--force로 강제 재계산)")
            return

    print("=== 기준일별 포트폴리오 평가금액 계산 ===\n")

    with open(TXS_FILE, encoding="utf-8") as f:
        txs = sorted(json.load(f), key=lambda x: x["date"])

    with open(PRICES_FILE, encoding="utf-8") as f:
        prices_meta = json.load(f)

    key_dates = get_key_dates()
    # 중복 날짜 제거 (예: mtd==qtd이면 한 번만 계산)
    unique_dates = {}
    for label, d in key_dates.items():
        unique_dates[d.strftime("%Y-%m-%d")] = d

    print("기준일:")
    for label, d in key_dates.items():
        print(f"  {label:6s}: {d.strftime('%Y-%m-%d')}")
    print()

    try:
        from pykrx import stock as krx
    except ImportError:
        print("ERROR: pykrx 미설치. pip3 install pykrx")
        sys.exit(1)

    output = {
        "_updated": today_str,
        "_key_dates": {k: v.strftime("%Y-%m-%d") for k, v in key_dates.items()},
    }
    fx_cache = {}

    for date_str, target_date in sorted(unique_dates.items()):
        print(f"{'─'*60}")
        print(f"[{date_str}] 보유 종목 재구성 중...")

        holdings = build_holdings_at(txs, date_str)
        print(f"  보유 {len(holdings)}종목\n")

        total_value  = 0
        stocks_detail = {}
        failed = []

        for stock_name, h in sorted(holdings.items()):
            meta   = prices_meta.get(stock_name, {})
            if not isinstance(meta, dict):
                meta = {}
            code   = meta.get("code", "")
            nation = meta.get("nation") or "KOR"

            price_native = None
            actual_date  = None

            if not code:
                price_native = h["cost"] / h["qty"] if h["qty"] > 0 else 0
                note = "no_code→cost"
            elif nation == "KOR":
                price_native, actual_date = krx_price_at(krx, code, target_date)
                note = f"krx_{actual_date}" if actual_date else "krx_fail"
            else:
                suffix     = YAHOO_SUFFIX.get(nation, "")
                ticker_str = code if code.endswith(suffix) else code + suffix
                price_native, actual_date = yf_price_at(ticker_str, target_date)
                note = f"yf_{actual_date}" if actual_date else "yf_fail"

            if price_native is None:
                price_native = h["cost"] / h["qty"] if h["qty"] > 0 else 0
                note = "fallback→cost"
                failed.append(stock_name)

            currency = NATION_CURRENCY.get(nation, "USD")
            fx = fx_rate_at(currency, target_date, fx_cache)
            if fx is None:
                fx = 1300.0
                print(f"  ⚠ {currency}/KRW 환율 조회 실패, 1300원으로 대체")

            price_krw = price_native * fx
            value_krw = price_krw * h["qty"]
            total_value += value_krw

            stocks_detail[stock_name] = {
                "qty":          round(h["qty"], 4),
                "price_native": round(price_native, 4),
                "currency":     currency,
                "fx_rate":      round(fx, 4),
                "price_krw":    round(price_krw),
                "value_krw":    round(value_krw),
                "note":         note,
            }

            status = "✓" if "fail" not in note and "cost" not in note else "△"
            print(f"  {status} {stock_name}: {h['qty']:.2f}주 × {price_krw:,.0f}원 = {value_krw:,.0f}원  [{note}]")

        if failed:
            print(f"\n  ⚠ 가격 조회 실패 (취득단가 대체): {', '.join(failed)}")

        output[date_str] = {
            "portfolio_value": round(total_value),
            "stocks": stocks_detail,
        }
        print(f"\n  → 포트폴리오 평가금액: {total_value:,.0f}원")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n✓ 저장 완료: {OUTPUT_FILE}")


if __name__ == "__main__":
    force = "--force" in sys.argv
    main(force)
