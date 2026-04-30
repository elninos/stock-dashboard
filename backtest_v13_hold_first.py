#!/usr/bin/env python3
"""v13: HOLD-First — 평가이익은 HOLD, 평가손실만 시그널.

핵심 가설:
  사용자 매매가 강한 이유 = 강세 종목 안 팔고 끝까지 들고감
  시그널의 가치 = 손실 종목 손실 회피

전략:
  매수 시점부터 평가이익 추적
  - 평가이익이 +30% 이상 한 번이라도 도달했으면: HOLD (시그널 무시)
  - 평가손실 -10% 이하면: 시그널 적극 적용 (손절)
  - 그 외: 표준 시그널

비교:
  v11_hybrid (가장 좋았던 것)
  v13_hold_first (새 시도)
  실제 매매
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

OUT = os.path.join(BASE_DIR, "dashboard", "backtest_v13.html")


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


def compute_signals(df, dates):
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

        foreign_buyers = sum(1 for (c,n), net in cur_df.groupby(["broker_code","broker_name"])["net"].sum().items()
                              if classify(n) == "foreign" and net > 0)
        if foreign_buyers >= 3 and cur_groups["foreign"] > 0:
            buy_score += 4 + min(foreign_buyers - 3, 3)

        daily[date] = {"buy": buy_score, "sell": sell_score}
    return daily


def simulate_v13(buys, df, prices, last_price):
    """HOLD-First 전략."""
    dates = sorted(df["date"].unique())
    daily_signals = compute_signals(df, dates)

    holding = []
    realized_pnl = 0
    sim_sells = []
    last_sell_date = None
    has_been_in_profit = False  # +30% 한번이라도 달성했나?
    max_pnl_pct = -999

    events = []
    for b in buys:
        events.append({"date": b["date"], "type": "buy", "data": b})
    for d in dates:
        events.append({"date": d, "type": "evaluate"})
    events.sort(key=lambda x: x["date"])

    for ev in events:
        if ev["type"] == "buy":
            b = ev["data"]
            holding.append({"qty": b["qty"], "price": b["price"]})
        else:
            current_qty = sum(l["qty"] for l in holding)
            if current_qty <= 0: continue

            sig = daily_signals.get(ev["date"], {"buy":0, "sell":0})
            avg = sum(l["qty"]*l["price"] for l in holding) / current_qty
            cur_price = prices.get(ev["date"], avg)
            pnl_pct = (cur_price / avg - 1) * 100

            max_pnl_pct = max(max_pnl_pct, pnl_pct)
            if max_pnl_pct >= 30:
                has_been_in_profit = True

            decision = None

            # CASE 1: 큰 손실 영역 — 적극 손절
            if pnl_pct <= -15 and sig["sell"] >= 5:
                decision = ("sell", 1.0, f"손절 -15% 이하")
            elif pnl_pct <= -10 and sig["sell"] >= 7:
                decision = ("sell", 0.5, f"손절 -10% 이하")

            # CASE 2: 한 번도 이익 안 본 종목 + 약한 시그널 — 적극 매도
            elif not has_been_in_profit and pnl_pct < 0 and sig["sell"] >= 5:
                decision = ("sell", 0.5, f"이익 못본 종목 손실 진행")

            # CASE 3: 강세 종목 (한번 +30% 달성) — 매우 보수적
            elif has_been_in_profit:
                # 트레일링 스탑만 — 최고점 -25% 이탈
                if pnl_pct <= max_pnl_pct - 25 and sig["sell"] >= 5:
                    decision = ("sell", 0.5, f"트레일링 (peak {max_pnl_pct:.0f}% → 현재 {pnl_pct:.0f}%)")
                # 또는 매우 강한 매도 시그널
                elif sig["sell"] >= 13:
                    decision = ("sell", 1/3, f"극강 시그널 {sig['sell']}")

            # CASE 4: 평가이익 +30% 미만 + 시그널 — 표준
            elif 0 <= pnl_pct < 30:
                if sig["sell"] >= 9 and sig["sell"] > sig["buy"]:
                    decision = ("sell", 1/3, f"표준 매도")

            if not decision: continue

            action, ratio, reason = decision

            if last_sell_date:
                d_now = datetime.strptime(ev["date"], "%Y-%m-%d")
                d_last = datetime.strptime(last_sell_date, "%Y-%m-%d")
                if (d_now - d_last).days < 30: continue

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
            sim_sells.append({"date": ev["date"], "ratio": ratio, "reason": reason,
                                "pnl_pct": pnl_pct, "qty": sell_qty})
            last_sell_date = ev["date"]

    rq = sum(l["qty"] for l in holding)
    rc = sum(l["qty"]*l["price"] for l in holding)
    rv = rq * last_price
    return {
        "realized_pnl": realized_pnl,
        "total_pnl": realized_pnl + rv - rc,
        "n_sells": len(sim_sells),
        "max_pnl_seen": max_pnl_pct,
        "sells": sim_sells,
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
    print("  v13: HOLD-First 백테스트")
    print("="*80)

    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

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

    print(f"\n분석 대상: {len(holdings)}종목")

    # 거래원 매핑
    mapping = build_broker_mapping([c for _, c in holdings[:10]])

    today = "20260424"
    data_start = (datetime.strptime(today,"%Y%m%d") - timedelta(days=365)).strftime("%Y%m%d")

    results = []
    for stock_name, code in holdings:
        print(f"  {stock_name} ({code})...")
        try:
            r_data = fetch_all_brokers_daily(code, data_start, today, min_vol=100)
            if not r_data: continue
            df = aggregate_to_dataframe(r_data, mapping)
            if df is None or df.empty: continue
            df["date"] = df["date"].apply(lambda d: f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(str(d))==8 else d)

            pdf = krx.get_market_ohlcv_by_date(data_start, today, code)
            pdf.index = pdf.index.strftime("%Y-%m-%d")

            s_iso = f"{data_start[:4]}-{data_start[4:6]}-{data_start[6:8]}"
            buys = [t for t in txs if t["stock"] == stock_name and t["type"] == "buy" and t["date"] >= s_iso]
            sells = [t for t in txs if t["stock"] == stock_name and t["type"] == "sell" and t["date"] >= s_iso]
            if not buys: continue

            last_price = float(pdf["종가"].iloc[-1])
            actual = actual_pnl(buys, sells, last_price)
            sim = simulate_v13(buys, df, pdf["종가"].to_dict(), last_price)

            results.append({
                "stock": stock_name, "code": code,
                "actual": actual, "sim": sim,
                "diff": sim["total_pnl"] - actual,
                "max_seen": sim["max_pnl_seen"],
            })

            ic = "✓" if sim["total_pnl"] > actual else " "
            print(f"    {ic} 실제 {fmt(actual):>10}  v13 {fmt(sim['total_pnl']):>10}  차이 {fmt(sim['total_pnl']-actual):>10}  매도 {sim['n_sells']}회 (peak {sim['max_pnl_seen']:.0f}%)")
        except Exception as e:
            print(f"    [ERR] {e}")

    if not results: return

    total_actual = sum(r["actual"] for r in results)
    total_sim = sum(r["sim"]["total_pnl"] for r in results)
    diff = total_sim - total_actual
    n_better = sum(1 for r in results if r["diff"] > 0)

    print("\n" + "="*80)
    print(f"  종합 ({len(results)}종목):")
    print(f"    실제: {fmt(total_actual)}")
    print(f"    v13:  {fmt(total_sim)}")
    print(f"    차이: {fmt(diff)}")
    print(f"    v13 우세: {n_better}/{len(results)}")
    print("="*80)

    # HTML
    html = build_html(results, total_actual, total_sim, diff, n_better)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ {OUT}")


def build_html(results, total_actual, total_sim, diff, n_better):
    rows = ""
    for r in sorted(results, key=lambda x: -x["diff"]):
        diff_clr = "ret-down" if r["diff"] > 0 else "ret-up"
        winner = "🤖" if r["diff"] > 0 else "👤"
        sells_str = "<br>".join(f"<span style='font-size:0.78em;color:#888'>{s['date']} {s['ratio']*100:.0f}% {s['reason']}</span>" for s in r["sim"]["sells"][:5])
        rows += f"""<tr>
          <td><b>{r['stock']}</b><br><span style="color:#666;font-size:0.78em">{r['code']}</span></td>
          <td class="mono" style="text-align:right">{fmt(r['actual'])}</td>
          <td class="mono" style="text-align:right">{fmt(r['sim']['total_pnl'])}<br><span style="font-size:0.78em;color:#888">{r['sim']['n_sells']}회</span></td>
          <td class="mono {diff_clr}" style="text-align:right;font-weight:600">{fmt(r['diff'])}<br><span style="font-size:0.78em">{winner}</span></td>
          <td class="mono" style="text-align:center">{r['max_seen']:.0f}%</td>
          <td>{sells_str}</td>
        </tr>"""

    diff_clr = "#10b981" if diff > 0 else "#ef4444"
    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<title>v13 HOLD-First</title>
<link rel="stylesheet" href="assets/style.css">
<style>
.kpi-strip {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }}
.kpi-strip .kpi-mini {{ flex:1; min-width:160px; background:#181b23; border-radius:8px; padding:12px; text-align:center; }}
.kpi-strip .num {{ font-size:1.4em; font-weight:700; color:#fff; }}
.kpi-strip .lbl {{ font-size:0.75em; color:#888; margin-top:4px; }}
</style></head><body><div class="container">

<div class="nav">
  <a href="index.html">📊 전체</a>
  <a href="backtest_regime.html">v8~v12</a>
  <a href="backtest_v13.html" class="active">🎯 v13 HOLD-First</a>
</div>

<h1>🎯 v13 HOLD-First — 강세 종목 보호 + 손실만 손절</h1>
<p class="subtitle">한번 +30% 달성 종목은 트레일링만, 손실 종목은 적극 손절</p>

<div class="card">
  <div class="callout">
    <b>v13 룰:</b><br>
    • <b>+30% 달성 종목</b> (강세 추세): 트레일링 -25%만 적용 + 매우 강한 시그널만 (점수 13+)<br>
    • <b>이익 못본 종목</b>: 손실 진행 시 적극 매도<br>
    • <b>큰 손실 (-15%↓)</b>: 즉시 손절<br>
    • <b>중간 손실 (-10%~-15%)</b>: 시그널 강하면 1/2 매도<br>
    • <b>중립 (0~30%)</b>: 표준 시그널<br>
  </div>
</div>

<div class="kpi-strip">
  <div class="kpi-mini" style="border:2px solid #6b7280">
    <div class="lbl">실제</div>
    <div class="num mono">{fmt(total_actual)}</div>
    <div class="lbl">{len(results)}종목</div>
  </div>
  <div class="kpi-mini" style="border:2px solid {diff_clr}">
    <div class="lbl">🎯 v13 HOLD-First</div>
    <div class="num mono">{fmt(total_sim)}</div>
    <div class="lbl mono" style="color:{diff_clr}">{fmt(diff)}</div>
    <div class="lbl">v13 우세 {n_better}/{len(results)}</div>
  </div>
</div>

<div class="card">
  <h2>종목별 결과 (개선순)</h2>
  <table>
    <tr>
      <th>종목</th>
      <th style="text-align:right">실제</th>
      <th style="text-align:right">v13</th>
      <th style="text-align:right">차이</th>
      <th style="text-align:center">최고이익</th>
      <th>매도 시점</th>
    </tr>
    {rows}
  </table>
</div>

</div></body></html>"""


if __name__ == "__main__":
    main()
