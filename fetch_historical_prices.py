#!/usr/bin/env python3
"""
기준일별 포트폴리오 평가금액 계산 (기간별 수익률용)

기준일: 전월말, 전분기말, 전년말(YTD), 1년 전(T12M)
각 기준일에 보유 중이던 종목의 종가를 조회해서 포트폴리오 가치를 계산한다.
- 한국주식: KRX Open API
- 해외주식: KIS HHDFS76240000 (해외주식 기간별시세)
- 환율: KIS HHDFS76200200 응답의 t_rate (현재 환율 — 역사 환율 미제공)

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

import os, sys, time
from collections import defaultdict
from datetime import datetime, timedelta, date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from config import TRANSACTIONS_FILE as TXS_FILE, PRICES_FILE, HIST_PORTFOLIO_FILE as OUTPUT_FILE
from file_io import load_json, save_json
from signals.kis_api import get_client, rate_limit
from signals.krx_open_api import get_kospi_daily, get_kosdaq_daily
from fetch_prices import _resolve_excd, _strip_suffix

NATION_CURRENCY = {
    "KOR": "KRW", "USA": "USD", "JPN": "JPY",
    "HKG": "HKD", "CHN": "CNY",
}

# 통화별 KIS 환율 조회용 sample 티커 (각 거래소에서 활동적인 티커 1개)
FX_SAMPLE = {
    "USD": ("AAPL", "USA", "나스닥"),
    "JPY": ("7203", "JPN", "도쿄"),       # Toyota
    "HKD": ("0700", "HKG", "홍콩"),       # Tencent
    "CNY": ("600519", "CHN", "상해"),     # Kweichow Moutai
}

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

def krx_price_at(code, target: date):
    """KRX Open API로 기준일 또는 직전 거래일 종가 반환.

    KOSPI/KOSDAQ 일별 매매정보(전종목)를 호출하여 해당 종목 추출.
    캐시 활용 — 같은 날짜 재호출 시 즉시.
    """
    for i in range(10):
        d = target - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        d_str = d.strftime("%Y%m%d")
        rows = get_kospi_daily(d_str) + get_kosdaq_daily(d_str)
        for r in rows:
            if r.get("ISU_CD") == code:
                try:
                    close = int(str(r.get("TDD_CLSPRC", "0")).replace(",", ""))
                except (ValueError, TypeError):
                    continue
                if close > 0:
                    return close, d
        time.sleep(0.05)
    return None, None


def kis_overseas_price_at(code, nation, market, target: date):
    """KIS HHDFS76240000 — 해외주식 기간별시세.

    GUBN=0(일봉), BYMD=조회기준일(이날 포함 직전 100일).
    응답에서 target 또는 직전 거래일 종가 반환.
    """
    excd = _resolve_excd(nation, market)
    if not excd:
        return None, None
    symb = _strip_suffix(code)
    bymd = target.strftime("%Y%m%d")
    rate_limit()
    try:
        res = get_client().get(
            "/uapi/overseas-price/v1/quotations/dailyprice",
            "HHDFS76240000",
            {"AUTH": "", "EXCD": excd, "SYMB": symb,
             "GUBN": "0", "BYMD": bymd, "MODP": "1"},
        )
    except Exception:
        return None, None
    if res.get("rt_cd") != "0":
        return None, None
    rows = res.get("output2") or []
    target_str = target.strftime("%Y%m%d")
    # output2는 보통 최신일이 앞. target 이하 중 가장 최근 영업일 종가 찾기.
    best = None
    for r in rows:
        d = str(r.get("xymd") or r.get("stck_bsop_date") or "")
        if not d or d > target_str:
            continue
        try:
            close = float(str(r.get("clos") or r.get("stck_clpr") or "0").replace(",", ""))
        except (ValueError, TypeError):
            continue
        if close <= 0:
            continue
        # d가 더 최신이면 갱신
        if best is None or d > best[0]:
            best = (d, close)
    if not best:
        return None, None
    d_str, close = best
    actual_date = date(int(d_str[:4]), int(d_str[4:6]), int(d_str[6:8]))
    return round(close, 4), actual_date


def fx_rate_at(currency, target: date, fx_cache):
    """KIS HHDFS76200200 응답의 t_rate 활용.

    NOTE: KIS는 현재 환율만 제공. 역사 환율은 별도 소스 없으므로
    현재 환율을 기준일에도 적용 (기준일 평가가치 계산은 근사치).
    """
    if currency == "KRW":
        return 1.0
    if currency in fx_cache:
        return fx_cache[currency]
    sample = FX_SAMPLE.get(currency)
    if not sample:
        fx_cache[currency] = 1300.0
        return 1300.0
    code, nation, market = sample
    excd = _resolve_excd(nation, market)
    symb = _strip_suffix(code)
    rate_limit()
    try:
        res = get_client().get(
            "/uapi/overseas-price/v1/quotations/price-detail",
            "HHDFS76200200",
            {"AUTH": "", "EXCD": excd, "SYMB": symb},
        )
        out = res.get("output") or {}
        rate = float(out.get("t_rate", "0")) or None
    except Exception:
        rate = None
    fx_cache[currency] = rate
    return rate

# ───────────────────────── 메인 ─────────────────────────

def main(force=False):
    today_str = date.today().strftime("%Y-%m-%d")

    # 캐시 유효 체크
    if not force:
        existing = load_json(OUTPUT_FILE, default={})
        if existing.get("_updated") == today_str:
            print("✓ 오늘 이미 계산됨. 스킵 (--force로 강제 재계산)")
            return

    print("=== 기준일별 포트폴리오 평가금액 계산 ===\n")

    txs = sorted(load_json(TXS_FILE, default=[]), key=lambda x: x["date"])
    prices_meta = load_json(PRICES_FILE, default={})

    key_dates = get_key_dates()
    # 중복 날짜 제거 (예: mtd==qtd이면 한 번만 계산)
    unique_dates = {}
    for label, d in key_dates.items():
        unique_dates[d.strftime("%Y-%m-%d")] = d

    print("기준일:")
    for label, d in key_dates.items():
        print(f"  {label:6s}: {d.strftime('%Y-%m-%d')}")
    print()

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
                price_native, actual_date = krx_price_at(code, target_date)
                note = f"krx_{actual_date}" if actual_date else "krx_fail"
            else:
                market = meta.get("market", "")
                price_native, actual_date = kis_overseas_price_at(code, nation, market, target_date)
                note = f"kis_{actual_date}" if actual_date else "kis_fail"

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

    save_json(OUTPUT_FILE, output)
    print(f"\n✓ 저장 완료: {OUTPUT_FILE}")


if __name__ == "__main__":
    force = "--force" in sys.argv
    main(force)
