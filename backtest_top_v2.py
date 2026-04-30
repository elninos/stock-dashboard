#!/usr/bin/env python3
"""고점 판별 백테스트 v2 — 강화된 시그널 + 더 많은 종목.

v1 vs v2 차이:
  v1: OBV 단발 +2, CMF 단발 +1
  v2: + OBV 누적 (5일 3회+ → +5, 10일 5회+ → +7)
      + CMF 지속 (7일 연속 → +4, 14일 연속 → +6)
      + MFI 지속 (5일 연속 → +3, 10일 연속 → +4)

테스트 대상:
  1. 기존 11종목 (큰 손익)
  2. 좀비 종목 (HLB제넥스, 코아스템켐온 등)
  3. 현재 평가이익 종목 (두산, 이오테크닉스, 리노공업 등)
  4. 파마리서치, 대한광통신 (2번 매도 — 1차/2차 고점)

출력: dashboard/backtest_top_v2.html
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
from signals.top_detector import add_top_detection
from pykrx import stock as krx
import pandas as pd

OUT = os.path.join(BASE_DIR, "dashboard", "backtest_top_v2.html")


def fmt(v):
    if v is None: return "─"
    if abs(v) >= 1e8: return f"{v/1e8:+.2f}억"
    return f"{v/1e4:+,.0f}만"


def simulate_top_detection_v2(buys, df, last_price, min_score=3):
    """v2: 임계값 5로 상향, 더 보수적."""
    idx_obj = df.index
    df_dates = idx_obj.strftime("%Y-%m-%d").tolist() if hasattr(idx_obj, "strftime") else [str(d) for d in idx_obj]

    events = []
    for b in buys:
        events.append({"date": b["date"], "type": "buy", "data": b})
    for i, date_str in enumerate(df_dates):
        score = df["top_score"].iat[i]
        if score >= min_score:
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
            if last_sell_date:
                d_now = datetime.strptime(ev["date"], "%Y-%m-%d")
                d_last = datetime.strptime(last_sell_date, "%Y-%m-%d")
                cooldown = 30 if score >= 10 else 60
                if (d_now - d_last).days < cooldown:
                    continue

            # 비율: 점수 ≥10 → 1/2, ≥7 → 1/3, ≥5 → 1/4
            if score >= 10: ratio = 0.5
            elif score >= 7: ratio = 1/3
            else: ratio = 0.25

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
                "reasons": list(row.get("top_reasons", [])),
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


def analyze_stock(stock_name, code, txs, label="?"):
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
    pdf = add_top_detection(pdf)

    last_price = float(pdf["close"].iloc[-1])
    actual = compute_actual_pnl(buys, sells, last_price)
    sim = simulate_top_detection_v2(buys, pdf, last_price, min_score=3)

    # 절대 고점 + 사전 시그널
    peak_idx = pdf["close"].idxmax()
    peak_price = float(pdf["close"].max())

    # 고점 ±30일 사전 시그널
    pre_peak_signals = []
    pdf_dates = pdf.index.strftime("%Y-%m-%d").tolist() if hasattr(pdf.index, "strftime") else [str(d) for d in pdf.index]
    if peak_idx in pdf_dates:
        peak_pos = pdf_dates.index(peak_idx)
        for i in range(max(0, peak_pos - 30), peak_pos + 1):
            if pdf["top_score"].iat[i] >= 5:
                pre_peak_signals.append({
                    "date": pdf_dates[i],
                    "score": float(pdf["top_score"].iat[i]),
                    "days_before": peak_pos - i,
                    "reasons": list(pdf["top_reasons"].iat[i])[:3],
                })

    earliest = max(pre_peak_signals, key=lambda x: x["days_before"]) if pre_peak_signals else None
    strongest = max(pre_peak_signals, key=lambda x: x["score"]) if pre_peak_signals else None

    return {
        "stock": stock_name, "code": code, "label": label,
        "actual": actual, "sim": sim,
        "diff": sim["total_pnl"] - actual["total_pnl"],
        "peak_date": peak_idx, "peak_price": peak_price,
        "n_pre_peak": len(pre_peak_signals),
        "earliest_pre_peak": earliest,
        "strongest_pre_peak": strongest,
    }


def main():
    print("="*80)
    print("  Top Detection v2 — 강화 시그널 + 다중 종목 백테스트")
    print("="*80)

    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    # 분석 대상 = (그룹별로 분류)
    groups = {
        "큰_손실": [],
        "큰_이익_실현": [],
        "현재_평가이익_큰종목": [],
        "좀비_종목": [],
    }

    pnl_by_stock = defaultdict(lambda: {"cost": 0, "rev": 0, "buy_n": 0, "sell_n": 0,
                                          "first_buy": "", "last_buy": ""})
    for t in txs:
        if t.get("type") not in ("buy", "sell"): continue
        s = t["stock"]
        d = t["date"]
        if t["type"] == "buy":
            pnl_by_stock[s]["cost"] += t.get("amount", 0)
            pnl_by_stock[s]["buy_n"] += 1
            if not pnl_by_stock[s]["first_buy"]:
                pnl_by_stock[s]["first_buy"] = d
            pnl_by_stock[s]["last_buy"] = d
        else:
            pnl_by_stock[s]["rev"] += t.get("amount", 0)
            pnl_by_stock[s]["sell_n"] += 1

    # 큰 손실/이익 (실현)
    realized = []
    for s, v in pnl_by_stock.items():
        if v["sell_n"] == 0 or v["buy_n"] < 2: continue
        if smap.get(s, {}).get("nation") != "KOR": continue
        pnl = v["rev"] - v["cost"]
        realized.append((s, pnl, v))

    losers = sorted([r for r in realized if r[1] < 0], key=lambda x: x[1])[:5]
    winners = sorted([r for r in realized if r[1] > 0], key=lambda x: -x[1])[:5]

    for s, _, _ in losers:
        groups["큰_손실"].append((s, smap[s]["code"]))
    for s, _, _ in winners:
        groups["큰_이익_실현"].append((s, smap[s]["code"]))

    # 좀비: 매수 5+ AND 1년+ 미매수
    today = datetime(2026, 4, 26)
    for s, v in pnl_by_stock.items():
        if v["buy_n"] >= 5 and v["last_buy"] and v["sell_n"] / v["buy_n"] < 0.3:
            last = datetime.strptime(v["last_buy"], "%Y-%m-%d")
            if (today - last).days > 365:
                if smap.get(s, {}).get("nation") == "KOR" and smap.get(s, {}).get("code"):
                    groups["좀비_종목"].append((s, smap[s]["code"]))
                    if len(groups["좀비_종목"]) >= 5: break

    # 현재 보유 평가이익 큰 종목 (수동 지정)
    current_holdings = ["두산", "이오테크닉스", "리노공업", "에이피알", "토모큐브"]
    for s in current_holdings:
        if s in smap and smap[s].get("code") and pnl_by_stock.get(s, {}).get("buy_n", 0) >= 2:
            groups["현재_평가이익_큰종목"].append((s, smap[s]["code"]))

    # 중복 제거
    seen = set()
    targets = []
    for label, items in groups.items():
        for s, c in items:
            if s in seen: continue
            seen.add(s)
            targets.append((s, c, label))

    print(f"\n분석 대상: {len(targets)}종목\n")
    by_group = defaultdict(list)

    results = []
    for stock_name, code, label in targets:
        print(f"  [{label}] {stock_name} ({code})...")
        r = analyze_stock(stock_name, code, txs, label)
        if r:
            results.append(r)
            by_group[label].append(r)
            pre_str = "─"
            if r["earliest_pre_peak"]:
                e = r["earliest_pre_peak"]
                pre_str = f"{e['days_before']}일 전 점수{e['score']:.0f}"
            print(f"    실제 {fmt(r['actual']['total_pnl'])} | "
                  f"v2 {fmt(r['sim']['total_pnl'])} | "
                  f"차이 {fmt(r['diff'])} | "
                  f"매도 {r['sim']['n_sells']}회 | 사전:{pre_str}")

    if not results: return

    # 그룹별 통계
    print()
    print("="*80)
    print("  그룹별 결과")
    print("="*80)
    for label in ["큰_손실", "큰_이익_실현", "현재_평가이익_큰종목", "좀비_종목"]:
        if label not in by_group: continue
        rs = by_group[label]
        actual_sum = sum(r["actual"]["total_pnl"] for r in rs)
        sim_sum = sum(r["sim"]["total_pnl"] for r in rs)
        diff = sim_sum - actual_sum
        n_better = sum(1 for r in rs if r["diff"] > 0)
        n_pre = sum(1 for r in rs if r["earliest_pre_peak"])
        print(f"\n  [{label}] {len(rs)}종목")
        print(f"    실제 합계:   {fmt(actual_sum)}")
        print(f"    v2 합계:     {fmt(sim_sum)}")
        print(f"    차이:        {fmt(diff)}")
        print(f"    v2 우세:     {n_better}/{len(rs)}")
        print(f"    고점 사전:    {n_pre}/{len(rs)}")

    total_actual = sum(r["actual"]["total_pnl"] for r in results)
    total_sim = sum(r["sim"]["total_pnl"] for r in results)
    diff_total = total_sim - total_actual
    n_pre = sum(1 for r in results if r["earliest_pre_peak"])

    print()
    print("="*80)
    print(f"  종합:")
    print(f"    실제:   {fmt(total_actual)}")
    print(f"    v2:     {fmt(total_sim)}")
    print(f"    차이:   {fmt(diff_total)}")
    print(f"    고점 사전 캐치: {n_pre}/{len(results)}")
    print("="*80)

    html = build_html(by_group, total_actual, total_sim, diff_total, n_pre, len(results))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ {OUT}")


def build_html(by_group, total_actual, total_sim, diff_total, n_pre, n_total):
    sections = ""
    group_labels = {
        "큰_손실": "💀 큰 손실 종목",
        "큰_이익_실현": "💰 큰 이익 (실현)",
        "현재_평가이익_큰종목": "📊 현재 평가이익 큰 종목",
        "좀비_종목": "🧟 좀비 종목 (1년+ 미매수)",
    }

    for label_key in ["큰_손실", "큰_이익_실현", "현재_평가이익_큰종목", "좀비_종목"]:
        if label_key not in by_group: continue
        rs = by_group[label_key]
        rows = ""
        for r in rs:
            diff_clr = "ret-down" if r["diff"] > 0 else "ret-up"
            winner = "🤖" if r["diff"] > 0 else "👤"

            pre_text = "─"
            if r["earliest_pre_peak"]:
                e = r["earliest_pre_peak"]
                pre_text = f"<span style='color:#10b981'>{e['days_before']}일 전 (점수 {e['score']:.0f})</span>"
                if r["strongest_pre_peak"] and r["strongest_pre_peak"] != e:
                    s2 = r["strongest_pre_peak"]
                    pre_text += f"<br><span style='color:#888;font-size:0.78em'>최강: {s2['days_before']}일 전 점수 {s2['score']:.0f}</span>"

            sells_html = ""
            for s in r["sim"]["sim_sells"][:3]:
                rsn = ", ".join(s.get("reasons", [])[:2])
                sells_html += f"<div style='font-size:0.78em;color:#888'>{s['date']} 점{s['score']:.1f} {s['ratio']*100:.0f}% — {rsn[:50]}</div>"

            rows += f"""<tr>
              <td><b>{r['stock']}</b><br><span style="color:#666;font-size:0.78em">{r['code']}</span></td>
              <td class="mono" style="text-align:right">{r['peak_price']:,.0f}<br><span style="font-size:0.78em;color:#888">{r['peak_date']}</span></td>
              <td>{pre_text}</td>
              <td class="mono" style="text-align:right">{fmt(r['actual']['total_pnl'])}<br><span style="font-size:0.78em;color:#888">{r['actual']['n_sells']}회</span></td>
              <td class="mono" style="text-align:right">{fmt(r['sim']['total_pnl'])}<br><span style="font-size:0.78em;color:#888">{r['sim']['n_sells']}회</span></td>
              <td class="mono {diff_clr}" style="text-align:right;font-weight:600">{fmt(r['diff'])}<br><span style="font-size:0.78em">{winner}</span></td>
              <td>{sells_html}</td>
            </tr>"""

        actual_sum = sum(r["actual"]["total_pnl"] for r in rs)
        sim_sum = sum(r["sim"]["total_pnl"] for r in rs)
        diff_g = sim_sum - actual_sum
        diff_clr_g = "#10b981" if diff_g > 0 else "#ef4444"
        n_better = sum(1 for r in rs if r["diff"] > 0)
        n_pre_g = sum(1 for r in rs if r["earliest_pre_peak"])

        sections += f"""<div class="card">
          <h2>{group_labels.get(label_key, label_key)} ({len(rs)}종목)</h2>
          <div class="grid3" style="margin-bottom:14px">
            <div class="kpi"><div class="kpi-label">실제 합계</div><div class="kpi-value mono">{fmt(actual_sum)}</div></div>
            <div class="kpi"><div class="kpi-label">v2 시뮬</div><div class="kpi-value mono">{fmt(sim_sum)}</div></div>
            <div class="kpi" style="border:2px solid {diff_clr_g}"><div class="kpi-label">차이</div><div class="kpi-value mono" style="color:{diff_clr_g}">{fmt(diff_g)}</div></div>
          </div>
          <p class="desc">v2 우세 {n_better}/{len(rs)}종목 · 고점 사전 캐치 {n_pre_g}/{len(rs)}</p>
          <table>
            <tr>
              <th>종목</th>
              <th style="text-align:right">절대 고점</th>
              <th>고점 사전 캐치</th>
              <th style="text-align:right">실제</th>
              <th style="text-align:right">v2 시뮬</th>
              <th style="text-align:right">차이</th>
              <th>매도 시점</th>
            </tr>
            {rows}
          </table>
        </div>"""

    diff_clr = "#10b981" if diff_total > 0 else "#ef4444"
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>Top Detection v2 Backtest</title>
<link rel="stylesheet" href="assets/style.css">
<style>
.kpi-strip {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }}
.kpi-strip .kpi-mini {{ flex:1; min-width:160px; background:#181b23; border-radius:8px; padding:14px; text-align:center; }}
.kpi-strip .num {{ font-size:1.5em; font-weight:700; color:#fff; }}
.kpi-strip .lbl {{ font-size:0.78em; color:#888; margin-top:4px; }}
.grid3 {{ display:grid; grid-template-columns: 1fr 1fr 1fr; gap:10px; }}
.kpi {{ background:#1a1d26; border-radius:8px; padding:12px; text-align:center; }}
.kpi-label {{ font-size:0.78em; color:#888; margin-bottom:4px; }}
.kpi-value {{ font-size:1.3em; font-weight:700; color:#eee; }}
</style></head><body>
<div class="container">
<div class="nav">
  <a href="index.html">📊 전체</a>
  <a href="backtest_top.html">고점 v1</a>
  <a href="backtest_top_v2.html" class="active">🎯 고점 v2 강화</a>
  <a href="backtest_time.html">시기별</a>
</div>

<h1>🎯 Top Detection v2 — 강화 시그널</h1>
<p class="subtitle">OBV 누적 + CMF/MFI 지속 시그널 추가 + 그룹별 분석</p>

<div class="card">
  <div class="callout">
    <b>v1 대비 v2 변경점:</b><br>
    🆕 <b>OBV 분배 클러스터</b> (5일 내 3회+) → +5점<br>
    🆕 <b>OBV 분배 폭주</b> (10일 내 5회+) → +7점 (가장 강함)<br>
    🆕 <b>CMF 지속 분배</b> (-0.15↓ 7일 연속) → +4점<br>
    🆕 <b>CMF 강한 분배</b> (-0.20↓ 14일 연속) → +6점<br>
    🆕 <b>MFI 지속 과매수</b> (80+ 5일 연속) → +3점<br>
    🆕 <b>MFI 극단 과열</b> (80+ 10일 연속) → +4점<br>
    <br>
    <b>매도 룰 (v2):</b> 점수 ≥10 → 1/2 · ≥7 → 1/3 · ≥5 → 1/4 · 60일 cooldown
  </div>
</div>

<div class="kpi-strip">
  <div class="kpi-mini"><div class="num mono">{fmt(total_actual)}</div><div class="lbl">실제 합계</div></div>
  <div class="kpi-mini"><div class="num mono">{fmt(total_sim)}</div><div class="lbl">v2 합계</div></div>
  <div class="kpi-mini" style="border:2px solid {diff_clr}"><div class="num mono" style="color:{diff_clr}">{fmt(diff_total)}</div><div class="lbl">차이</div></div>
  <div class="kpi-mini"><div class="num" style="color:#10b981">{n_pre}/{n_total}</div><div class="lbl">고점 사전 캐치</div></div>
</div>

{sections}

</div></body></html>"""


if __name__ == "__main__":
    main()
