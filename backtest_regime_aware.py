#!/usr/bin/env python3
"""Regime-Aware 백테스트 — 시장 환경 + 종목 추세 + 평가손익에 따라 동적 임계값.

새 변형:
  v8  Macro Regime: KOSPI 12개월 추세에 따라 임계값 조정
  v9  Stock Stage:  종목 60일 MA 기준 stage별 다른 룰
  v10 Position:     평가이익률에 따라 모드 전환
  v11 Hybrid:       v8+v9+v10 결합
  v12 Time-Decay:   신고가 이후 시간에 따라 시그널 가중치 감소

데이터:
  - 16종목 × 1년 거래원
  - KOSPI 1년 (yfinance)
  - 매일 환경 평가
"""
import os, sys, warnings, json
from datetime import datetime, timedelta
from collections import defaultdict
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from signals.kis_member_daily import (
    build_broker_mapping, fetch_all_brokers_daily, aggregate_to_dataframe,
)
from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from pykrx import stock as krx
import pandas as pd
import yfinance as yf

OUT = os.path.join(BASE_DIR, "dashboard", "backtest_regime.html")


def fmt(v):
    if v is None: return "─"
    if abs(v) >= 1e8: return f"{v/1e8:+.2f}억"
    return f"{v/1e4:+,.0f}만"


def classify(name):
    if not name: return "small"
    for kw in ["JP모간","모간","골드만","메릴린치","UBS","CLSA","씨티","BNP","노무라","맥쿼리","다이와","외국계","홍콩상하이","도이치"]:
        if kw in name: return "foreign"
    for kw in ["키움","토스","카카오","상상인"]:
        if kw in name: return "retail"
    for kw in ["NH투자","KB증권","한국증권","한국투자","삼성증권","한화","미래에셋","신한","하나"]:
        if kw in name: return "large"
    return "small"


def load_kospi_regime(start, end):
    """KOSPI regime 시계열 (강세/횡보/하락)."""
    s_yf = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    e_yf = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
    df = yf.download("^KS11", start=s_yf, end=e_yf, progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")

    df["chg_3m"] = df["Close"].pct_change(63) * 100  # 3개월
    df["chg_6m"] = df["Close"].pct_change(126) * 100
    df["ma60"] = df["Close"].rolling(60).mean()

    regime = {}
    for date, row in df.iterrows():
        if pd.isna(row.get("chg_3m")):
            regime[date] = "neutral"
            continue
        if row["chg_3m"] >= 8 and row["Close"] > row["ma60"]:
            regime[date] = "bull"
        elif row["chg_3m"] <= -8:
            regime[date] = "bear"
        elif abs(row["chg_3m"]) <= 3:
            regime[date] = "sideways"
        else:
            regime[date] = "transition"

    return regime


def stock_stage(prices_df, date_str):
    """종목 stage (Weinstein 단순화).
    Stage 1: Basing, 2: Up, 3: Top, 4: Down
    """
    if date_str not in prices_df.index: return 0
    df_so_far = prices_df.loc[:date_str]
    if len(df_so_far) < 60: return 0

    cur = df_so_far["close"].iloc[-1]
    ma20 = df_so_far["close"].rolling(20).mean().iloc[-1]
    ma60 = df_so_far["close"].rolling(60).mean().iloc[-1]
    ma60_slope = (ma60 / df_so_far["close"].rolling(60).mean().iloc[-20] - 1) * 100 if len(df_so_far) >= 80 else 0

    if cur > ma60 and ma20 > ma60 and ma60_slope > 1:
        return 2  # Up
    if cur < ma20 and ma20 < ma60 and ma60_slope < -1:
        return 4  # Down
    if cur > ma60 and ma60_slope < 1:
        return 3  # Topping
    return 1  # Basing


def compute_signals_per_day(df, dates):
    """일자별 매수/매도 점수."""
    daily = {}
    for i, date in enumerate(dates):
        if i < 10:
            daily[date] = {"buy": 0, "sell": 0}
            continue

        cur_dates = dates[max(0, i-4): i+1]
        prev_dates = dates[max(0, i-9): max(0, i-4)]
        cur_df = df[df["date"].isin(cur_dates)]
        prev_df = df[df["date"].isin(prev_dates)]

        buy_score = 0; sell_score = 0

        # 매도: 매수→매도 전환
        prev_5d = prev_df.groupby(["broker_code","broker_name"])["net"].sum()
        prev_top3 = prev_5d.nlargest(3)
        reversed_b = []
        for (code, name), prev_net in prev_top3.items():
            if prev_net <= 0: continue
            cur_net = cur_df[cur_df["broker_code"] == code]["net"].sum()
            if cur_net < 0:
                reversed_b.append((name, classify(name)))
        if reversed_b:
            n_rev = len(reversed_b)
            n_foreign = sum(1 for _, g in reversed_b if g == "foreign")
            sell_score += n_rev * 2 + (3 if n_rev >= 2 else 0) + (2 if n_foreign >= 1 else 0)

        cur_groups = defaultdict(int)
        for (code, name), net in cur_df.groupby(["broker_code","broker_name"])["net"].sum().items():
            cur_groups[classify(name)] += net
        if cur_groups["retail"] > 0 and (cur_groups["foreign"] + cur_groups["large"]) < 0:
            sell_score += 5

        # 매수
        foreign_buyers = sum(1 for (c,n), net in cur_df.groupby(["broker_code","broker_name"])["net"].sum().items()
                              if classify(n) == "foreign" and net > 0)
        if foreign_buyers >= 3 and cur_groups["foreign"] > 0:
            buy_score += 4 + min(foreign_buyers - 3, 3)

        if cur_groups["foreign"] > 0 and cur_groups["retail"] < 0:
            buy_score += 4

        daily[date] = {"buy": buy_score, "sell": sell_score}
    return daily


# ────────────────────────────────────────
# 변형 결정 함수들
# ────────────────────────────────────────

def variant_v8_macro(sig, kospi_regime, date, pnl_pct):
    """v8: KOSPI regime 기반 임계값 조정."""
    regime = kospi_regime.get(date, "neutral")
    sell_s = sig["sell"]

    if regime == "bull":
        thresh_high, thresh_mid = 12, 10  # 매우 보수적
    elif regime == "bear":
        thresh_high, thresh_mid = 5, 3   # 적극
    else:
        thresh_high, thresh_mid = 9, 7

    if sell_s >= thresh_high and pnl_pct >= 50:
        return ("sell", 0.5, f"regime={regime}")
    if sell_s >= thresh_mid and pnl_pct >= 30:
        return ("sell", 1/3, f"regime={regime}")
    return None


def variant_v9_stage(sig, stage, pnl_pct):
    """v9: 종목 Stage 기반."""
    sell_s = sig["sell"]
    if stage == 2:  # Up — 시그널 거의 무시
        if sell_s >= 14 and pnl_pct >= 100:
            return ("sell", 1/3, "Stage2 + 극강")
        return None
    if stage == 4:  # Down — 적극 매도
        if sell_s >= 5:
            return ("sell", 0.5, "Stage4 청산")
    if stage == 3:  # Topping
        if sell_s >= 7 and pnl_pct >= 30:
            return ("sell", 1/3, "Stage3")
    if stage == 1:  # Basing
        if sell_s >= 11:
            return ("sell", 0.25, "Stage1")
    return None


def variant_v10_position(sig, pnl_pct):
    """v10: 평가손익률 기반."""
    sell_s = sig["sell"]
    buy_s = sig["buy"]

    # 큰 이익 — 보호 모드 (트레일링)
    if pnl_pct >= 100:
        if sell_s >= 11:  # 강한 신호만
            return ("sell", 1/3, "큰이익 보호")
        return None
    # 중간 이익 — 표준
    if pnl_pct >= 30:
        if sell_s >= 9 and sell_s > buy_s:
            return ("sell", 1/3, "중이익")
        if sell_s >= 7 and sell_s > buy_s + 2:
            return ("sell", 0.25, "중이익 약")
    # 손실 — 적극 손절
    if pnl_pct <= -10:
        if sell_s >= 5:
            return ("sell", 0.5, "손절")
    if pnl_pct <= -20:
        if sell_s >= 3:
            return ("sell", 1.0, "강손절")
    return None


def variant_v11_hybrid(sig, kospi_regime, stage, pnl_pct, date):
    """v11: 모두 결합 (가장 정교)."""
    regime = kospi_regime.get(date, "neutral")
    sell_s = sig["sell"]
    buy_s = sig["buy"]

    # Stage 2 (강세 추세) — Bull regime이면 거의 안 팜
    if stage == 2 and regime == "bull":
        if sell_s >= 14 and pnl_pct >= 100:
            return ("sell", 1/4, "Stage2+Bull 강신호")
        return None

    # Stage 4 (하락) — 무조건 청산 시작
    if stage == 4:
        if pnl_pct <= -10:
            return ("sell", 0.5, "Stage4 손절")
        if sell_s >= 5:
            return ("sell", 1/3, "Stage4 청산")

    # 큰 이익 + Bull = 보호
    if pnl_pct >= 100 and regime == "bull":
        if sell_s >= 12 and (sell_s - buy_s) >= 8:
            return ("sell", 1/3, "큰이익+Bull 강신호")
        return None

    # 큰 이익 + 하락/횡보 = 익절
    if pnl_pct >= 100 and regime in ("bear", "sideways"):
        if sell_s >= 7:
            return ("sell", 0.5, "큰이익+약세")

    # 중간 이익
    if pnl_pct >= 30:
        threshold = 11 if regime == "bull" else 7 if regime == "bear" else 9
        if sell_s >= threshold and sell_s > buy_s:
            return ("sell", 1/3, f"중이익 regime={regime}")

    # 손실
    if pnl_pct <= -10 and sell_s >= 5:
        return ("sell", 0.5, "손절")

    return None


def variant_v12_time_decay(sig, days_since_high, pnl_pct):
    """v12: 신고가 이후 경과 일수에 따라 가중치."""
    sell_s = sig["sell"]
    if days_since_high <= 5:
        weight = 1.5  # 신고가 직후 시그널 강화
    elif days_since_high <= 15:
        weight = 1.2
    elif days_since_high <= 30:
        weight = 1.0
    else:
        weight = 0.5  # 이미 빠진 후 시그널 약화

    effective = sell_s * weight
    if effective >= 13 and pnl_pct >= 50:
        return ("sell", 0.5, f"+{days_since_high}일 강신호")
    if effective >= 9 and pnl_pct >= 30:
        return ("sell", 1/3, f"+{days_since_high}일 중신호")
    return None


VARIANTS = {
    "v8_macro":  variant_v8_macro,
    "v9_stage":  variant_v9_stage,
    "v10_position": variant_v10_position,
    "v11_hybrid":   variant_v11_hybrid,
    "v12_time":  variant_v12_time_decay,
}


def simulate(buys, df, prices_df, prices, last_price, kospi_regime, variant_fn, variant_name, cooldown=30):
    dates = sorted(df["date"].unique())
    daily_signals = compute_signals_per_day(df, dates)

    holding = []
    realized_pnl = 0
    sim_sells = []
    last_sell_date = None

    # 사용자 매수 + 일자 평가
    events = []
    for b in buys:
        events.append({"date": b["date"], "type": "buy", "data": b})
    for d in dates:
        events.append({"date": d, "type": "evaluate"})
    events.sort(key=lambda x: x["date"])

    # 신고가 추적
    running_high = 0
    last_high_date = None

    for ev in events:
        if ev["type"] == "buy":
            b = ev["data"]
            holding.append({"qty": b["qty"], "price": b["price"]})
        else:
            current_qty = sum(l["qty"] for l in holding)
            if current_qty <= 0: continue

            sig = daily_signals.get(ev["date"], {"buy":0, "sell":0})
            if sig["sell"] == 0 and sig["buy"] == 0: continue

            avg = sum(l["qty"]*l["price"] for l in holding) / current_qty
            cur_price = prices.get(ev["date"], avg)
            pnl_pct = (cur_price / avg - 1) * 100

            # 신고가 추적
            if cur_price > running_high:
                running_high = cur_price
                last_high_date = ev["date"]
            days_since_high = 0
            if last_high_date:
                d_now = datetime.strptime(ev["date"], "%Y-%m-%d")
                d_high = datetime.strptime(last_high_date, "%Y-%m-%d")
                days_since_high = (d_now - d_high).days

            # 변형별 결정
            stage = stock_stage(prices_df, ev["date"])
            if variant_name == "v8_macro":
                decision = variant_fn(sig, kospi_regime, ev["date"], pnl_pct)
            elif variant_name == "v9_stage":
                decision = variant_fn(sig, stage, pnl_pct)
            elif variant_name == "v10_position":
                decision = variant_fn(sig, pnl_pct)
            elif variant_name == "v11_hybrid":
                decision = variant_fn(sig, kospi_regime, stage, pnl_pct, ev["date"])
            elif variant_name == "v12_time":
                decision = variant_fn(sig, days_since_high, pnl_pct)
            else:
                decision = None

            if not decision: continue
            action, ratio, reason = decision

            if last_sell_date:
                d_now = datetime.strptime(ev["date"], "%Y-%m-%d")
                d_last = datetime.strptime(last_sell_date, "%Y-%m-%d")
                if (d_now - d_last).days < cooldown: continue

            sell_qty = int(current_qty * ratio)
            if sell_qty <= 0: continue

            sold_cost = 0
            remain = sell_qty
            while remain > 0 and holding:
                lot = holding[0]
                take = min(remain, lot["qty"])
                sold_cost += take * lot["price"]
                lot["qty"] -= take
                remain -= take
                if lot["qty"] <= 0: holding.pop(0)
            sold_revenue = sell_qty * cur_price
            realized_pnl += sold_revenue - sold_cost
            sim_sells.append({"date": ev["date"], "ratio": ratio, "reason": reason})
            last_sell_date = ev["date"]

    rq = sum(l["qty"] for l in holding)
    rc = sum(l["qty"]*l["price"] for l in holding)
    rv = rq * last_price
    return {
        "realized_pnl": realized_pnl,
        "total_pnl": realized_pnl + rv - rc,
        "n_sells": len(sim_sells),
    }


def actual_pnl(buys, sells, last_price):
    holding = []; pnl = 0
    events = []
    for b in buys: events.append(("buy", b["date"], b))
    for s in sells: events.append(("sell", s["date"], s))
    events.sort(key=lambda x: x[1])
    for tp, _, t in events:
        if tp == "buy":
            holding.append({"qty": t["qty"], "price": t["price"]})
        else:
            remain = t["qty"]; sc = 0
            while remain > 0 and holding:
                lot = holding[0]
                take = min(remain, lot["qty"])
                sc += take * lot["price"]
                lot["qty"] -= take
                remain -= take
                if lot["qty"] <= 0: holding.pop(0)
            pnl += t["qty"] * t["price"] - sc
    rq = sum(l["qty"] for l in holding)
    rc = sum(l["qty"]*l["price"] for l in holding)
    return pnl + rq * last_price - rc


def main():
    print("="*80)
    print("  Regime-Aware 백테스트 (v8~v12)")
    print("="*80)

    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    # 보유 종목
    qty = defaultdict(int)
    for t in txs:
        if t.get("type") == "buy": qty[t["stock"]] += t.get("qty", 0)
        elif t.get("type") == "sell": qty[t["stock"]] -= t.get("qty", 0)
    holdings = []
    EXCLUDE = ["KODEX","TIME","TIGER"]
    for s, q in qty.items():
        if q > 0:
            info = smap.get(s, {})
            if info.get("nation") == "KOR" and info.get("code") and not any(kw in s for kw in EXCLUDE):
                holdings.append((s, info["code"]))

    # KOSPI regime
    today = "20260424"
    start = (datetime.strptime(today,"%Y%m%d") - timedelta(days=400)).strftime("%Y%m%d")
    print(f"\n[KOSPI regime 로드 ({start} ~ {today})]")
    kospi_regime = load_kospi_regime(start, today)
    regime_dist = defaultdict(int)
    for r in kospi_regime.values(): regime_dist[r] += 1
    print(f"  분포: {dict(regime_dist)}")

    # 거래원 매핑
    print("\n[거래원 매핑]")
    mapping = build_broker_mapping([c for _, c in holdings[:10]])

    # 종목 데이터 수집
    print(f"\n[데이터 수집: {len(holdings)}종목]")
    stock_data = {}
    for stock_name, code in holdings:
        print(f"  {stock_name} ({code})...")
        try:
            data_start = (datetime.strptime(today,"%Y%m%d") - timedelta(days=365)).strftime("%Y%m%d")
            results = fetch_all_brokers_daily(code, data_start, today, min_vol=100)
            if not results: continue
            df = aggregate_to_dataframe(results, mapping)
            if df is None or df.empty: continue
            df["date"] = df["date"].apply(lambda d: f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(str(d))==8 else d)

            pdf = krx.get_market_ohlcv_by_date(data_start, today, code)
            pdf.index = pdf.index.strftime("%Y-%m-%d")
            pdf_renamed = pdf.rename(columns={"종가":"close"})

            buys = [t for t in txs if t["stock"] == stock_name and t["type"] == "buy"
                     and t["date"] >= f"{data_start[:4]}-{data_start[4:6]}-{data_start[6:8]}"]
            sells = [t for t in txs if t["stock"] == stock_name and t["type"] == "sell"
                      and t["date"] >= f"{data_start[:4]}-{data_start[4:6]}-{data_start[6:8]}"]
            if not buys: continue

            stock_data[stock_name] = {
                "code": code, "df": df,
                "pdf": pdf_renamed, "prices": pdf["종가"].to_dict(),
                "buys": buys, "sells": sells,
                "last_price": float(pdf["종가"].iloc[-1]),
            }
        except Exception as e:
            print(f"    [ERR] {e}")
    print(f"\n  분석 가능: {len(stock_data)}종목")

    # 변형 시뮬
    print("\n[변형별 시뮬]")
    all_results = {}
    for v_name, v_fn in VARIANTS.items():
        per_stock = {}
        for stock_name, data in stock_data.items():
            sim = simulate(data["buys"], data["df"], data["pdf"], data["prices"],
                            data["last_price"], kospi_regime, v_fn, v_name)
            per_stock[stock_name] = sim
        all_results[v_name] = per_stock
        total = sum(s["total_pnl"] for s in per_stock.values())
        print(f"  {v_name}: {fmt(total)}")

    # 사용자 실제
    actuals = {s: actual_pnl(d["buys"], d["sells"], d["last_price"]) for s, d in stock_data.items()}
    actual_total = sum(actuals.values())
    print(f"\n  실제: {fmt(actual_total)}")

    best = max(all_results.items(), key=lambda x: sum(s["total_pnl"] for s in x[1].values()))
    best_total = sum(s["total_pnl"] for s in best[1].values())
    print(f"  🏆 {best[0]}: {fmt(best_total)} (실제 대비 {fmt(best_total - actual_total)})")

    # HTML
    html = build_html(stock_data, all_results, actuals, kospi_regime)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ {OUT}")


def build_html(stock_data, all_results, actuals, kospi_regime):
    actual_total = sum(actuals.values())
    variant_totals = {v: sum(s["total_pnl"] for s in r.values()) for v, r in all_results.items()}
    sorted_v = sorted(variant_totals.items(), key=lambda x: -x[1])

    desc = {
        "v8_macro":     "KOSPI regime 기반: bull→임계값12, bear→5, 횡보→9",
        "v9_stage":     "종목 Stage: 2(상승) 시그널무시, 3(토핑) 표준, 4(하락) 적극매도",
        "v10_position": "평가손익: +100%↑ 트레일링, 0~50% 표준, -10%↓ 적극손절",
        "v11_hybrid":   "Macro+Stage+Position 모두 결합 (가장 정교)",
        "v12_time":     "신고가 후 경과일에 따라 시그널 가중치 (0~5일 1.5x, 30일+ 0.5x)",
    }

    kpis = ""
    for i, (v_name, total) in enumerate(sorted_v):
        diff = total - actual_total
        clr = "#10b981" if diff > 0 else "#ef4444"
        n_better = sum(1 for s in stock_data if all_results[v_name][s]["total_pnl"] > actuals[s])
        rank = "🏆" if i == 0 else f"#{i+1}"
        border = f"border:2px solid {clr}" if i == 0 else ""
        kpis += f"""<div class="kpi-mini" style="{border}">
          <div class="lbl">{rank} {v_name}</div>
          <div class="num mono">{fmt(total)}</div>
          <div class="lbl mono" style="color:{clr}">{fmt(diff)}</div>
          <div class="lbl">우세 {n_better}/{len(stock_data)}</div>
        </div>"""

    # 종목별 비교
    rows = ""
    for stock, d in sorted(stock_data.items()):
        a = actuals[stock]
        cells = f"<td><b>{stock}</b><br><span style='color:#666;font-size:0.78em'>{d['code']}</span></td><td class='mono' style='text-align:right'>{fmt(a)}</td>"
        best_v = max(all_results.items(), key=lambda x: x[1][stock]["total_pnl"])[0]
        for v_name, _ in sorted_v:
            r = all_results[v_name][stock]
            diff = r["total_pnl"] - a
            clr = "ret-down" if diff > 0 else "ret-up" if diff < 0 else ""
            mk = " 🏆" if v_name == best_v else ""
            cells += f"<td class='mono {clr}' style='text-align:right'>{fmt(r['total_pnl'])}{mk}<br><span style='font-size:0.78em;color:#888'>{r['n_sells']}회</span></td>"
        rows += f"<tr>{cells}</tr>"

    desc_rows = ""
    for v_name, total in sorted_v:
        desc_rows += f"<tr><td><b>{v_name}</b></td><td>{desc.get(v_name,'')}</td><td class='mono' style='text-align:right'>{fmt(total)}</td></tr>"

    headers = "".join(f"<th style='text-align:right'>{v}</th>" for v, _ in sorted_v)

    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<title>Regime-Aware 백테스트</title>
<link rel="stylesheet" href="assets/style.css">
<style>
.kpi-strip {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }}
.kpi-strip .kpi-mini {{ flex:1; min-width:160px; background:#181b23; border-radius:8px; padding:12px; text-align:center; }}
.kpi-strip .num {{ font-size:1.3em; font-weight:700; color:#fff; }}
.kpi-strip .lbl {{ font-size:0.75em; color:#888; margin-top:4px; }}
</style></head><body><div class="container">

<div class="nav">
  <a href="index.html">📊 전체</a>
  <a href="backtest_broker_signals.html">v1~v7</a>
  <a href="backtest_regime.html" class="active">🎯 v8~v12</a>
</div>

<h1>🎯 Regime-Aware 백테스트 (v8~v12)</h1>
<p class="subtitle">시장환경 + 종목추세 + 평가손익에 따라 동적 매도 룰</p>

<div class="card">
  <div class="callout">
    <b>v8</b>: KOSPI 12주 추세 기반 (강세 12, 약세 5)<br>
    <b>v9</b>: 종목 Stage (Stage2 무시, Stage4 청산)<br>
    <b>v10</b>: 평가손익 (이익 100%↑ 트레일링, 손실 적극손절)<br>
    <b>v11</b>: 모두 결합 (Hybrid)<br>
    <b>v12</b>: 신고가 후 경과일 가중 (5일 이내 1.5배, 30일+ 0.5배)
  </div>
</div>

<div class="kpi-strip">
  <div class="kpi-mini" style="border:2px solid #6b7280">
    <div class="lbl">실제</div>
    <div class="num mono">{fmt(actual_total)}</div>
    <div class="lbl">{len(stock_data)}종목</div>
  </div>
  {kpis}
</div>

<div class="card">
  <h2>변형별 룰</h2>
  <table>
    <tr><th>변형</th><th>룰</th><th style="text-align:right">합계</th></tr>
    {desc_rows}
  </table>
</div>

<div class="card">
  <h2>종목별 결과</h2>
  <table>
    <tr><th>종목</th><th style="text-align:right">실제</th>{headers}</tr>
    {rows}
  </table>
</div>

</div></body></html>"""


if __name__ == "__main__":
    main()
