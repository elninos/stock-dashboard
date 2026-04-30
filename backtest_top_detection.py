#!/usr/bin/env python3
"""고점 판별 백테스트.

핵심: 고점에서만 시그널 발동 → 추세 종목 보호 + 진짜 고점 잡기

Stage-Aware와 비교:
  Stage-Aware: 강세 종목 시그널 무시, 토핑/하락 시 매도
  Top-Detection: 고점권 진입 시에만 시그널 활성화

목표:
  파마리서치 (71만원→32만원), 대한광통신 (1027→633) 같은 케이스를
  사전에 잡았는가?

출력: dashboard/backtest_top.html
"""
import os, sys, warnings
from datetime import datetime, timedelta
from collections import defaultdict
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from signals.price_volume import add_price_volume_signals
from signals.sudden_drop import add_sudden_drop_signals
from signals.dow_theory import add_dow_signals
from signals.top_detector import add_top_detection, find_top_signals
from pykrx import stock as krx
import pandas as pd

OUT = os.path.join(BASE_DIR, "dashboard", "backtest_top.html")


def fmt(v):
    if v is None: return "─"
    if abs(v) >= 1e8: return f"{v/1e8:+.2f}억"
    return f"{v/1e4:+,.0f}만"


def simulate_top_detection(buys, df, last_price):
    """고점 시그널 기반 시뮬.

    행동 룰:
      score ≥ 8: 1/2 매도 (강한 고점)
      score ≥ 5: 1/3 매도 (고점 가능)
      score ≥ 3: 1/4 매도 (주의)
      Cooldown 60일 (강한 시그널은 30일)
    """
    idx_obj = df.index
    df_dates = idx_obj.strftime("%Y-%m-%d").tolist() if hasattr(idx_obj, "strftime") else [str(d) for d in idx_obj]

    events = []
    for b in buys:
        events.append({"date": b["date"], "type": "buy", "data": b})
    for i, date_str in enumerate(df_dates):
        score = df["top_score"].iat[i]
        if score >= 3:
            events.append({"date": date_str, "type": "sell_candidate", "idx": i, "score": float(score)})
    events.sort(key=lambda x: x["date"])

    holding = []
    cumulative_buy_cost = 0
    realized_pnl = 0
    sim_sells = []
    last_sell_date = None

    for ev in events:
        if ev["type"] == "buy":
            b = ev["data"]
            holding.append({"qty": b["qty"], "price": b["price"]})
            cumulative_buy_cost += b["qty"] * b["price"]
        else:
            current_qty = sum(l["qty"] for l in holding)
            if current_qty <= 0: continue

            score = ev["score"]

            # Cooldown
            if last_sell_date:
                d_now = datetime.strptime(ev["date"], "%Y-%m-%d")
                d_last = datetime.strptime(last_sell_date, "%Y-%m-%d")
                cooldown = 30 if score >= 8 else 60
                if (d_now - d_last).days < cooldown:
                    continue

            if score >= 8:
                ratio = 0.5
            elif score >= 5:
                ratio = 1/3
            else:
                ratio = 0.25

            sell_qty = int(current_qty * ratio)
            if sell_qty <= 0: continue

            row = df.iloc[ev["idx"]]
            sell_price = float(row["close"])

            sold_cost = 0
            remain = sell_qty
            while remain > 0 and holding:
                lot = holding[0]
                take = min(remain, lot["qty"])
                sold_cost += take * lot["price"]
                lot["qty"] -= take
                remain -= take
                if lot["qty"] <= 0:
                    holding.pop(0)
            sold_revenue = sell_qty * sell_price
            pnl = sold_revenue - sold_cost
            realized_pnl += pnl
            sim_sells.append({
                "date": ev["date"], "qty": sell_qty, "price": sell_price,
                "score": score, "ratio": ratio, "pnl": pnl,
                "near_high": float(row.get("near_high60_pct", 0)) * 100,
                "reasons": row.get("top_reasons", []),
            })
            last_sell_date = ev["date"]

    remain_qty = sum(l["qty"] for l in holding)
    remain_cost = sum(l["qty"] * l["price"] for l in holding)
    remain_value = remain_qty * last_price

    return {
        "sim_sells": sim_sells,
        "realized_pnl": realized_pnl,
        "remain_qty": remain_qty,
        "remain_value": remain_value,
        "unrealized": remain_value - remain_cost,
        "total_pnl": realized_pnl + remain_value - remain_cost,
        "n_sells": len(sim_sells),
    }


def compute_actual_pnl(buys, sells, last_price):
    holding = []
    realized_pnl = 0
    cumulative_buy_cost = 0
    events = []
    for b in buys: events.append(("buy", b["date"], b))
    for s in sells: events.append(("sell", s["date"], s))
    events.sort(key=lambda x: x[1])

    for tp, d, t in events:
        if tp == "buy":
            holding.append({"qty": t["qty"], "price": t["price"]})
            cumulative_buy_cost += t["qty"] * t["price"]
        else:
            remain = t["qty"]
            sold_cost = 0
            while remain > 0 and holding:
                lot = holding[0]
                take = min(remain, lot["qty"])
                sold_cost += take * lot["price"]
                lot["qty"] -= take
                remain -= take
                if lot["qty"] <= 0:
                    holding.pop(0)
            realized_pnl += t["qty"] * t["price"] - sold_cost

    remain_qty = sum(l["qty"] for l in holding)
    remain_cost = sum(l["qty"] * l["price"] for l in holding)
    remain_value = remain_qty * last_price
    return {
        "realized_pnl": realized_pnl,
        "total_pnl": realized_pnl + remain_value - remain_cost,
        "n_sells": len(sells),
    }


def analyze_stock(stock_name, code, txs):
    s_trades = sorted(
        [t for t in txs if t["stock"] == stock_name and t["type"] in ("buy", "sell")],
        key=lambda x: x["date"]
    )
    if len(s_trades) < 3:
        return None

    buys = [t for t in s_trades if t["type"] == "buy"]
    sells = [t for t in s_trades if t["type"] == "sell"]

    first_buy = s_trades[0]["date"]
    start_dt = datetime.strptime(first_buy.replace("-", ""), "%Y%m%d") - timedelta(days=200)
    start = start_dt.strftime("%Y%m%d")
    end = "20260424"

    try:
        pdf = krx.get_market_ohlcv_by_date(start, end, code)
    except Exception:
        return None
    if len(pdf) < 100:
        return None

    pdf.index = pdf.index.strftime("%Y-%m-%d")
    pdf = pdf.rename(columns={
        "시가":"open","고가":"high","저가":"low","종가":"close","거래량":"volume"
    })

    pdf = add_price_volume_signals(pdf)
    pdf = add_sudden_drop_signals(pdf)
    pdf = add_dow_signals(pdf)
    pdf = add_top_detection(pdf)  # smart money 데이터 없음 (가격 기반만)

    last_price = float(pdf["close"].iloc[-1])
    actual = compute_actual_pnl(buys, sells, last_price)
    sim = simulate_top_detection(buys, pdf, last_price)
    top_signals = find_top_signals(pdf, min_score=3)

    # 절대 고점 정보
    peak_idx = pdf["close"].idxmax()
    peak_price = float(pdf["close"].max())

    # 고점 ±30일 내 시그널 발동 여부
    peak_dt = datetime.strptime(peak_idx, "%Y-%m-%d")
    pre_peak_signals = [
        s for s in top_signals
        if 0 <= (peak_dt - datetime.strptime(s["date"], "%Y-%m-%d")).days <= 30
    ]
    earliest_pre_peak = max(pre_peak_signals, key=lambda x: x["score"]) if pre_peak_signals else None

    return {
        "stock": stock_name, "code": code,
        "actual": actual, "sim": sim,
        "diff": sim["total_pnl"] - actual["total_pnl"],
        "n_top_signals": len(top_signals),
        "peak_date": peak_idx, "peak_price": peak_price,
        "pre_peak_count": len(pre_peak_signals),
        "earliest_pre_peak": earliest_pre_peak,
    }


def main():
    print("="*80)
    print("  고점 판별 백테스트 (Top Detection)")
    print("="*80)

    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    pnl_by_stock = defaultdict(lambda: {"cost": 0, "rev": 0, "buy_n": 0, "sell_n": 0})
    for t in txs:
        if t.get("type") not in ("buy", "sell"): continue
        s = t["stock"]
        if t["type"] == "buy":
            pnl_by_stock[s]["cost"] += t.get("amount", 0)
            pnl_by_stock[s]["buy_n"] += 1
        else:
            pnl_by_stock[s]["rev"] += t.get("amount", 0)
            pnl_by_stock[s]["sell_n"] += 1

    realized = []
    for s, v in pnl_by_stock.items():
        if v["sell_n"] == 0 or v["buy_n"] < 2: continue
        if smap.get(s, {}).get("nation") != "KOR": continue
        pnl = v["rev"] - v["cost"]
        realized.append((s, pnl))

    losers = sorted([r for r in realized if r[1] < 0], key=lambda x: x[1])[:6]
    winners = sorted([r for r in realized if r[1] > 0], key=lambda x: -x[1])[:6]
    targets = [(s, smap[s]["code"]) for s, _ in losers + winners
                if smap.get(s, {}).get("code")]

    print(f"\n분석 대상: {len(targets)}종목\n")

    results = []
    for stock_name, code in targets:
        print(f"  {stock_name} ({code})...")
        r = analyze_stock(stock_name, code, txs)
        if r:
            results.append(r)
            pre = "사전 시그널 ✓" if r["earliest_pre_peak"] else "사전 시그널 ✗"
            if r["earliest_pre_peak"]:
                e = r["earliest_pre_peak"]
                pre += f" ({e['date']} 점수 {e['score']:.0f})"
            print(f"    실제 {fmt(r['actual']['total_pnl'])} | "
                  f"Top {fmt(r['sim']['total_pnl'])} | "
                  f"차이 {fmt(r['diff'])} | "
                  f"매도 {r['sim']['n_sells']}회 | {pre}")

    if not results: return

    total_actual = sum(r["actual"]["total_pnl"] for r in results)
    total_sim = sum(r["sim"]["total_pnl"] for r in results)
    diff = total_sim - total_actual
    n_sim_better = sum(1 for r in results if r["diff"] > 0)
    n_pre_peak_caught = sum(1 for r in results if r["earliest_pre_peak"])

    print()
    print("="*80)
    print(f"  종합:")
    print(f"    실제:        {fmt(total_actual)}")
    print(f"    Top-Det:     {fmt(total_sim)}")
    print(f"    차이:        {fmt(diff)}")
    print(f"    Top 우세:    {n_sim_better}/{len(results)}종목")
    print(f"    고점 사전 캐치: {n_pre_peak_caught}/{len(results)}종목")
    print("="*80)

    html = build_html(results, total_actual, total_sim, diff, n_sim_better, n_pre_peak_caught)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ {OUT}")


def build_html(results, total_actual, total_sim, diff, n_sim_better, n_pre_peak):
    rows = ""
    for r in results:
        diff_clr = "ret-down" if r["diff"] > 0 else "ret-up"
        winner = "🤖" if r["diff"] > 0 else "👤"

        # 사전 캐치 정보
        pre = "─"
        pre_clr = ""
        if r["earliest_pre_peak"]:
            e = r["earliest_pre_peak"]
            peak_dt = datetime.strptime(r["peak_date"], "%Y-%m-%d")
            sig_dt = datetime.strptime(e["date"], "%Y-%m-%d")
            days_before = (peak_dt - sig_dt).days
            pre = f"✓ {days_before}일 전 (점수 {e['score']:.0f})"
            pre_clr = "color:#10b981"

        # 매도 시점 상세
        sells_html = ""
        for s in r["sim"]["sim_sells"][:5]:
            reasons_str = ", ".join(s.get("reasons", [])[:2]) if s.get("reasons") else "-"
            sells_html += f"<div style='font-size:0.78em;color:#888'>{s['date']} 신고가{s['near_high']:.0f}% 점수{s['score']:.1f} {s['ratio']*100:.0f}%</div>"

        rows += f"""<tr>
          <td><b>{r['stock']}</b><br><span style="color:#666;font-size:0.78em">{r['code']}</span></td>
          <td class="mono" style="text-align:right">{r['peak_price']:,.0f}원<br><span style="font-size:0.78em;color:#888">{r['peak_date']}</span></td>
          <td style="font-size:0.85em;{pre_clr}">{pre}</td>
          <td class="mono" style="text-align:right">{fmt(r['actual']['total_pnl'])}<br><span style="font-size:0.78em;color:#888">{r['actual']['n_sells']}회</span></td>
          <td class="mono" style="text-align:right">{fmt(r['sim']['total_pnl'])}<br><span style="font-size:0.78em;color:#888">{r['sim']['n_sells']}회</span></td>
          <td class="mono {diff_clr}" style="text-align:right;font-weight:600">{fmt(r['diff'])}<br><span style="font-size:0.78em">{winner}</span></td>
          <td>{sells_html}</td>
        </tr>"""

    diff_clr = "#10b981" if diff > 0 else "#ef4444"
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>Top Detection Backtest</title>
<link rel="stylesheet" href="assets/style.css">
<style>
.kpi-strip {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }}
.kpi-strip .kpi-mini {{ flex:1; min-width:160px; background:#181b23; border-radius:8px; padding:14px; text-align:center; }}
.kpi-strip .num {{ font-size:1.5em; font-weight:700; color:#fff; }}
.kpi-strip .lbl {{ font-size:0.78em; color:#888; margin-top:4px; }}
</style></head><body>
<div class="container">
<div class="nav">
  <a href="index.html">📊 전체</a>
  <a href="backtest_multi.html">시그널만</a>
  <a href="backtest_macro.html">+ 매크로</a>
  <a href="backtest_stage.html">Stage-Aware</a>
  <a href="backtest_top.html" class="active">🎯 고점 판별</a>
</div>

<h1>🎯 고점 판별 백테스트 (Top Detection)</h1>
<p class="subtitle">신고가권에서만 시그널 발동 — 추세 종목 보호 + 진짜 고점 캐치</p>

<div class="card">
  <div class="callout">
    <b>핵심 아이디어:</b><br>
    "추세 중간엔 시그널 무시, <b>신고가권에서만 발동</b>" <br>
    → 강세 종목 추세 보호 + 파마리서치/대한광통신 같은 단기 고점 캐치<br>
    <br>
    <b>고점권 정의:</b><br>
    60일 신고가의 90% 이상 OR 절대 신고가의 95% 이상<br>
    <br>
    <b>고점 시그널 9개 (가중치):</b><br>
    Bearish Divergence (+4) · Smart Money Reversal (+3) · Distribution Pattern (+3) ·
    Failed Breakout (+3) · Volume Climax (+2) · Distribution Days (+2) ·
    OBV Divergence (+2) · MFI 정점 (+1) · CMF 분배 진입 (+1) · Dow LH (+1.5)<br>
    <br>
    <b>매도 룰:</b> 점수 ≥8 → 1/2 매도 · ≥5 → 1/3 · ≥3 → 1/4 · 60일 cooldown
  </div>
</div>

<div class="kpi-strip">
  <div class="kpi-mini"><div class="num mono">{fmt(total_actual)}</div><div class="lbl">실제</div></div>
  <div class="kpi-mini"><div class="num mono">{fmt(total_sim)}</div><div class="lbl">Top-Detection</div></div>
  <div class="kpi-mini" style="border:2px solid {diff_clr}"><div class="num mono" style="color:{diff_clr}">{fmt(diff)}</div><div class="lbl">차이</div></div>
  <div class="kpi-mini"><div class="num">{n_sim_better}/{len(results)}</div><div class="lbl">Top 우세</div></div>
  <div class="kpi-mini"><div class="num" style="color:#10b981">{n_pre_peak}/{len(results)}</div><div class="lbl">고점 사전 캐치</div></div>
</div>

<div class="card">
  <h2>종목별 결과</h2>
  <table>
    <tr>
      <th>종목</th>
      <th style="text-align:right">절대 고점</th>
      <th>고점 사전 캐치</th>
      <th style="text-align:right">실제</th>
      <th style="text-align:right">Top-Det</th>
      <th style="text-align:right">차이</th>
      <th>매도 시점</th>
    </tr>
    {rows}
  </table>
</div>

</div></body></html>"""


if __name__ == "__main__":
    main()
