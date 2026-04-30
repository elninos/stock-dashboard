#!/usr/bin/env python3
"""v14: 시그널 정확도 측정 — 자동 매도가 아닌 알림 정확도.

평가 방법:
  1. 시그널 발동일 → 30일 후 가격 변화
  2. 적중률 (실제 빠진 비율)
  3. 손실 종목만 별도 평가
  4. 사용자가 시그널 따랐다면 손실 회피량

핵심 질문:
  "시그널이 정확한가?"  vs  "시그널이 PnL을 키우는가?"
  → 후자는 강세장에선 무의미. 전자가 진짜 가치.
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

OUT = os.path.join(BASE_DIR, "dashboard", "backtest_v14.html")


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


def find_signal_events(df, dates, prices):
    """시그널 발동일 + 30일 후 가격 변화 추적."""
    events = []
    for i, date in enumerate(dates):
        if i < 10: continue

        cur_dates = dates[max(0, i-4): i+1]
        prev_dates = dates[max(0, i-9): max(0, i-4)]
        cur_df = df[df["date"].isin(cur_dates)]
        prev_df = df[df["date"].isin(prev_dates)]

        sell_score = 0
        buy_score = 0

        # 매도 시그널
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

        # 매수 시그널
        foreign_buyers = sum(1 for (c,n), net in cur_df.groupby(["broker_code","broker_name"])["net"].sum().items()
                              if classify(n) == "foreign" and net > 0)
        if foreign_buyers >= 3 and cur_groups["foreign"] > 0:
            buy_score += 4 + min(foreign_buyers - 3, 3)

        if sell_score < 5 and buy_score < 5:
            continue

        # 30일 후 가격
        cur_price = prices.get(date)
        if not cur_price: continue
        # date_idx에서 30일 후 찾기
        date_idx = dates.index(date) if date in dates else -1
        future_30d_price = None
        if date_idx >= 0 and date_idx + 30 < len(dates):
            future_30d_price = prices.get(dates[date_idx + 30])

        if future_30d_price is None: continue
        chg_30d = (future_30d_price / cur_price - 1) * 100

        events.append({
            "date": date,
            "sell_score": sell_score,
            "buy_score": buy_score,
            "price": cur_price,
            "future_30d_price": future_30d_price,
            "chg_30d": chg_30d,
            "reversed": reversed_b,
        })

    return events


def evaluate_signals(events):
    """시그널 적중률 평가."""
    sell_strong = [e for e in events if e["sell_score"] >= 9]
    sell_mid = [e for e in events if 5 <= e["sell_score"] < 9]
    buy_strong = [e for e in events if e["buy_score"] >= 7]

    # 매도 시그널 적중 (30일 후 -5% 이하)
    def hit_rate(evs, threshold=-5):
        if not evs: return 0, 0
        n_hit = sum(1 for e in evs if e["chg_30d"] <= threshold)
        return n_hit, len(evs)

    sell_strong_hit, sell_strong_n = hit_rate(sell_strong, -5)
    sell_mid_hit, sell_mid_n = hit_rate(sell_mid, -3)
    buy_strong_hit, buy_strong_n = hit_rate(buy_strong, +5)
    buy_strong_hit = sum(1 for e in buy_strong if e["chg_30d"] >= 5)

    # 평균 수익률
    avg_30d_strong_sell = sum(e["chg_30d"] for e in sell_strong) / len(sell_strong) if sell_strong else 0
    avg_30d_mid_sell = sum(e["chg_30d"] for e in sell_mid) / len(sell_mid) if sell_mid else 0
    avg_30d_buy = sum(e["chg_30d"] for e in buy_strong) / len(buy_strong) if buy_strong else 0

    return {
        "sell_strong": {"n": sell_strong_n, "hit": sell_strong_hit,
                        "rate": sell_strong_hit/sell_strong_n*100 if sell_strong_n else 0,
                        "avg_30d": avg_30d_strong_sell},
        "sell_mid":    {"n": sell_mid_n, "hit": sell_mid_hit,
                        "rate": sell_mid_hit/sell_mid_n*100 if sell_mid_n else 0,
                        "avg_30d": avg_30d_mid_sell},
        "buy_strong":  {"n": buy_strong_n, "hit": buy_strong_hit,
                        "rate": buy_strong_hit/buy_strong_n*100 if buy_strong_n else 0,
                        "avg_30d": avg_30d_buy},
    }


def main():
    print("="*80)
    print("  v14: 시그널 정확도 측정")
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

    mapping = build_broker_mapping([c for _, c in holdings[:10]])

    today = "20260424"
    data_start = (datetime.strptime(today,"%Y%m%d") - timedelta(days=365)).strftime("%Y%m%d")

    all_events = []
    by_stock = {}
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
            prices = pdf["종가"].to_dict()
            dates = sorted(df["date"].unique())

            events = find_signal_events(df, dates, prices)
            for e in events:
                e["stock"] = stock_name
            all_events.extend(events)
            by_stock[stock_name] = events
            print(f"    시그널 이벤트: {len(events)}건")
        except Exception as e:
            print(f"    [ERR] {e}")

    # 전체 평가
    print("\n[전체 시그널 적중률]")
    overall = evaluate_signals(all_events)
    print(f"  매도 강신호 (≥9): {overall['sell_strong']['n']}건, 적중률(-5%↓) {overall['sell_strong']['rate']:.0f}%, 평균 {overall['sell_strong']['avg_30d']:+.1f}%")
    print(f"  매도 중신호 (5~8): {overall['sell_mid']['n']}건, 적중률(-3%↓) {overall['sell_mid']['rate']:.0f}%, 평균 {overall['sell_mid']['avg_30d']:+.1f}%")
    print(f"  매수 강신호 (≥7): {overall['buy_strong']['n']}건, 적중률(+5%↑) {overall['buy_strong']['rate']:.0f}%, 평균 {overall['buy_strong']['avg_30d']:+.1f}%")

    # 종목별
    print("\n[종목별 평가]")
    by_stock_summary = {}
    for stock, events in by_stock.items():
        if not events: continue
        ev = evaluate_signals(events)
        by_stock_summary[stock] = ev
        n_strong = ev["sell_strong"]["n"]
        rate_strong = ev["sell_strong"]["rate"]
        avg_strong = ev["sell_strong"]["avg_30d"]
        if n_strong > 0:
            print(f"  {stock:<15} 매도강신호 {n_strong}건, 적중 {rate_strong:.0f}%, 평균 {avg_strong:+.1f}%")

    # HTML
    html = build_html(overall, by_stock, by_stock_summary)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ {OUT}")


def build_html(overall, by_stock, by_stock_summary):
    rate_clr = lambda r: "#10b981" if r >= 60 else "#f59e0b" if r >= 40 else "#ef4444"

    overall_html = f"""
    <div class="grid3" style="margin-bottom:18px">
      <div class="kpi" style="border-left:4px solid {rate_clr(overall['sell_strong']['rate'])}">
        <div class="kpi-label">📉 매도 강신호 (≥9)</div>
        <div class="kpi-value mono" style="color:{rate_clr(overall['sell_strong']['rate'])}">{overall['sell_strong']['rate']:.0f}%</div>
        <div class="kpi-sub">{overall['sell_strong']['n']}건 / 적중 {overall['sell_strong']['hit']}건</div>
        <div class="kpi-sub">30일 평균 {overall['sell_strong']['avg_30d']:+.1f}%</div>
      </div>
      <div class="kpi" style="border-left:4px solid {rate_clr(overall['sell_mid']['rate'])}">
        <div class="kpi-label">📉 매도 중신호 (5~8)</div>
        <div class="kpi-value mono" style="color:{rate_clr(overall['sell_mid']['rate'])}">{overall['sell_mid']['rate']:.0f}%</div>
        <div class="kpi-sub">{overall['sell_mid']['n']}건 / 적중 {overall['sell_mid']['hit']}건</div>
        <div class="kpi-sub">30일 평균 {overall['sell_mid']['avg_30d']:+.1f}%</div>
      </div>
      <div class="kpi" style="border-left:4px solid {rate_clr(overall['buy_strong']['rate'])}">
        <div class="kpi-label">📈 매수 강신호 (≥7)</div>
        <div class="kpi-value mono" style="color:{rate_clr(overall['buy_strong']['rate'])}">{overall['buy_strong']['rate']:.0f}%</div>
        <div class="kpi-sub">{overall['buy_strong']['n']}건 / 적중 {overall['buy_strong']['hit']}건</div>
        <div class="kpi-sub">30일 평균 {overall['buy_strong']['avg_30d']:+.1f}%</div>
      </div>
    </div>"""

    rows = ""
    for stock, summary in sorted(by_stock_summary.items(), key=lambda x: -x[1]["sell_strong"]["n"]):
        ss = summary["sell_strong"]
        sm = summary["sell_mid"]
        bs = summary["buy_strong"]
        rows += f"""<tr>
          <td><b>{stock}</b></td>
          <td class="mono" style="text-align:center">{ss['n']}</td>
          <td class="mono" style="text-align:center;color:{rate_clr(ss['rate'])}">{ss['rate']:.0f}%</td>
          <td class="mono" style="text-align:right">{ss['avg_30d']:+.1f}%</td>
          <td class="mono" style="text-align:center">{sm['n']}</td>
          <td class="mono" style="text-align:center;color:{rate_clr(sm['rate'])}">{sm['rate']:.0f}%</td>
          <td class="mono" style="text-align:right">{sm['avg_30d']:+.1f}%</td>
          <td class="mono" style="text-align:center">{bs['n']}</td>
          <td class="mono" style="text-align:center;color:{rate_clr(bs['rate'])}">{bs['rate']:.0f}%</td>
        </tr>"""

    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<title>v14 시그널 정확도</title>
<link rel="stylesheet" href="assets/style.css">
<style>
.grid3 {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:14px; }}
.kpi {{ background:#1a1d26; border-radius:8px; padding:14px; }}
.kpi-label {{ font-size:0.85em; color:#888; margin-bottom:6px; }}
.kpi-value {{ font-size:1.6em; font-weight:700; }}
.kpi-sub {{ font-size:0.78em; color:#888; margin-top:4px; }}
</style></head><body><div class="container">

<div class="nav">
  <a href="index.html">📊 전체</a>
  <a href="backtest_regime.html">v8~v12</a>
  <a href="backtest_v13.html">v13</a>
  <a href="backtest_v14.html" class="active">🎯 v14 정확도</a>
</div>

<h1>🎯 v14 — 시그널 정확도 (적중률) 측정</h1>
<p class="subtitle">자동 매도 X, 알림 시스템으로서의 가치 평가</p>

<div class="card">
  <div class="callout">
    <b>평가 기준:</b><br>
    매도 강신호 (점수 ≥9): 30일 후 -5% 이하 = 적중<br>
    매도 중신호 (5~8): 30일 후 -3% 이하 = 적중<br>
    매수 강신호 (≥7): 30일 후 +5% 이상 = 적중<br>
    <br>
    <b>적중률 해석:</b> 60%↑ 신뢰 / 40~60% 보통 / 40%↓ 노이즈
  </div>
</div>

<div class="card">
  <h2>전체 시그널 정확도</h2>
  {overall_html}
</div>

<div class="card">
  <h2>종목별 시그널 정확도</h2>
  <table>
    <tr>
      <th rowspan="2">종목</th>
      <th colspan="3" style="text-align:center;border-right:1px solid #333">📉 매도 강신호 (≥9)</th>
      <th colspan="3" style="text-align:center;border-right:1px solid #333">📉 매도 중신호 (5~8)</th>
      <th colspan="2" style="text-align:center">📈 매수 강신호</th>
    </tr>
    <tr>
      <th style="text-align:center">건수</th>
      <th style="text-align:center">적중</th>
      <th style="text-align:right;border-right:1px solid #333">평균</th>
      <th style="text-align:center">건수</th>
      <th style="text-align:center">적중</th>
      <th style="text-align:right;border-right:1px solid #333">평균</th>
      <th style="text-align:center">건수</th>
      <th style="text-align:center">적중</th>
    </tr>
    {rows}
  </table>
</div>

</div></body></html>"""


if __name__ == "__main__":
    main()
