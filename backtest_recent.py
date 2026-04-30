#!/usr/bin/env python3
"""2025년 이후 매매 종목 백테스트 + 다양한 보정치 시도.

이전과 차이:
  1. 2025-01 이후 매매한 종목만 (시장 환경 통일)
  2. 여러 변형 동시 시도:
     v3a: 평가이익률 기반 매도 비율 (이익 적으면 HOLD)
     v3b: 트레일링 스탑 우선 (Chandelier 이탈 시 강한 매도)
     v3c: 손절 강화 (평가손실 + 시그널 = 즉시 매도)
     v3d: 종합 (a + b + c)

각 변형의 결과 비교해서 최선 선택.

출력: dashboard/backtest_recent.html
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
from signals.chandelier import add_chandelier_exit
from pykrx import stock as krx
import pandas as pd

OUT = os.path.join(BASE_DIR, "dashboard", "backtest_recent.html")


def fmt(v):
    if v is None: return "─"
    if abs(v) >= 1e8: return f"{v/1e8:+.2f}억"
    return f"{v/1e4:+,.0f}만"


# ────────────────────────────────────────
# 다양한 매도 전략 (variants)
# ────────────────────────────────────────

def get_avg_cost(holding):
    """현재 보유 평균 단가."""
    qty = sum(l["qty"] for l in holding)
    if qty <= 0: return 0
    return sum(l["qty"] * l["price"] for l in holding) / qty


def variant_a(score, holding, current_price, row):
    """v3a: 평가이익률 기반 매도 비율 조정.

    평가이익 큼 → 적극 매도
    평가손실 → 손절 모드
    이익 적음 → HOLD (매도 가치 없음)
    """
    avg = get_avg_cost(holding)
    if avg <= 0: return None
    pnl_pct = (current_price / avg - 1) * 100

    # 평가손실 + 강한 시그널 = 손절
    if pnl_pct <= -10 and score >= 7:
        return ("sell", 0.5, f"손절: 평가{pnl_pct:.0f}% + 점수{score:.0f}")

    # 평가이익 부족 (30% 미만) + 약한 시그널 → 무시
    if pnl_pct < 30 and score < 8:
        return ("hold", 0, "이익 부족, HOLD")

    # 평가이익 큼
    if pnl_pct >= 100:
        if score >= 10: return ("sell", 0.5, f"큰이익+강신호 1/2 매도")
        if score >= 7:  return ("sell", 1/3, f"큰이익+중신호 1/3")
        if score >= 5:  return ("sell", 0.25, f"큰이익+약신호 1/4")
    elif pnl_pct >= 50:
        if score >= 10: return ("sell", 1/3, f"중이익+강신호 1/3")
        if score >= 7:  return ("sell", 0.25, f"중이익+중신호 1/4")
    elif pnl_pct >= 30:
        if score >= 10: return ("sell", 0.25, f"약이익+강신호 1/4")

    return ("hold", 0, "조건 미달")


def variant_b(score, holding, current_price, row):
    """v3b: 트레일링 스탑 우선.

    Chandelier Exit 이탈만 매도 트리거.
    그 외 시그널은 무시 (추세 끝까지 탐).
    """
    ce = row.get("chandelier_exit")
    avg = get_avg_cost(holding)
    if avg <= 0: return None

    # Chandelier Exit 이탈 = 매도
    if ce and not pd.isna(ce) and current_price < ce:
        pnl_pct = (current_price / avg - 1) * 100
        if pnl_pct >= 50:  # 큰 이익 보호
            return ("sell", 0.5, f"Chandelier 이탈 (이익+{pnl_pct:.0f}%) 1/2")
        elif pnl_pct >= 0:
            return ("sell", 1/3, f"Chandelier 이탈 1/3")
        else:
            return ("sell", 0.5, f"Chandelier 이탈 (손실{pnl_pct:.0f}%) 손절")

    # 매우 강한 시그널 (≥12)만 추가 매도
    if score >= 12:
        return ("sell", 0.25, f"극강 시그널 {score:.0f} 1/4")

    return ("hold", 0, "Chandelier 보유")


def variant_c(score, holding, current_price, row):
    """v3c: 손절 강화.

    평가손실 시 시그널 임계값 낮춤 → 빠른 손절.
    이익 종목은 보수적.
    """
    avg = get_avg_cost(holding)
    if avg <= 0: return None
    pnl_pct = (current_price / avg - 1) * 100

    # 손실 영역 — 적극 손절
    if pnl_pct <= -15 and score >= 5:
        return ("sell", 1.0, f"손절: 평가{pnl_pct:.0f}% + 점수{score:.0f}")
    if pnl_pct <= -10 and score >= 7:
        return ("sell", 0.5, f"손절: 1/2")
    if pnl_pct <= -5 and score >= 10:
        return ("sell", 0.5, f"방어 매도")

    # 이익 영역 — 보수적
    if pnl_pct >= 50:
        if score >= 12: return ("sell", 1/3, f"이익+극강 1/3")
        if score >= 10: return ("sell", 0.25, f"이익+강 1/4")
    elif pnl_pct >= 20:
        if score >= 12: return ("sell", 0.25, f"이익20+극강 1/4")

    return ("hold", 0, "조건 미달")


def variant_d(score, holding, current_price, row):
    """v3d: 종합 (a + b + c).

    트레일링 스탑 (절대 우선) + 평가이익률 보정 + 손절 강화.
    """
    ce = row.get("chandelier_exit")
    avg = get_avg_cost(holding)
    if avg <= 0: return None
    pnl_pct = (current_price / avg - 1) * 100

    # 1) Chandelier 이탈 = 무조건 매도 (트레일링 스탑)
    if ce and not pd.isna(ce) and current_price < ce:
        if pnl_pct >= 50:
            return ("sell", 0.5, f"Chandelier+큰이익 1/2")
        elif pnl_pct >= 0:
            return ("sell", 1/3, f"Chandelier 1/3")
        else:
            return ("sell", 1.0, f"Chandelier+손실 전량")

    # 2) 손실 + 강한 시그널 = 손절
    if pnl_pct <= -10 and score >= 7:
        return ("sell", 0.5, f"손절: 평가{pnl_pct:.0f}%")
    if pnl_pct <= -15 and score >= 5:
        return ("sell", 1.0, f"강한 손절")

    # 3) 큰 이익 + 강한 시그널 = 부분 익절
    if pnl_pct >= 100 and score >= 10:
        return ("sell", 0.5, f"큰이익+강 1/2")
    if pnl_pct >= 100 and score >= 7:
        return ("sell", 1/3, f"큰이익+중 1/3")
    if pnl_pct >= 50 and score >= 12:
        return ("sell", 1/3, f"중이익+극강 1/3")
    if pnl_pct >= 30 and score >= 14:  # 매우 강한 신호만
        return ("sell", 0.25, f"이익+초강 1/4")

    return ("hold", 0, "HOLD")


VARIANTS = {
    "v3a_pnl_aware":    variant_a,
    "v3b_trailing":     variant_b,
    "v3c_aggressive_stop": variant_c,
    "v3d_combined":     variant_d,
}


def simulate(buys, df, last_price, variant_fn, cooldown_days=30):
    """단일 변형 시뮬."""
    idx_obj = df.index
    df_dates = idx_obj.strftime("%Y-%m-%d").tolist() if hasattr(idx_obj, "strftime") else [str(d) for d in idx_obj]

    events = []
    for b in buys:
        events.append({"date": b["date"], "type": "buy", "data": b})
    for i, date_str in enumerate(df_dates):
        events.append({"date": date_str, "type": "evaluate", "idx": i})
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
            if current_qty <= 0:
                continue

            row = df.iloc[ev["idx"]]
            score = row["top_score"]
            current_price = float(row["close"])

            # Variant 결정
            decision = variant_fn(score, holding, current_price, row)
            if not decision or decision[0] == "hold":
                continue

            action, ratio, reason = decision

            # Cooldown
            if last_sell_date:
                d_now = datetime.strptime(ev["date"], "%Y-%m-%d")
                d_last = datetime.strptime(last_sell_date, "%Y-%m-%d")
                if (d_now - d_last).days < cooldown_days:
                    continue

            sell_qty = int(current_qty * ratio)
            if sell_qty <= 0:
                continue

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
            sold_revenue = sell_qty * current_price
            pnl = sold_revenue - sold_cost
            realized_pnl += pnl
            sim_sells.append({
                "date": ev["date"], "qty": sell_qty, "price": current_price,
                "score": float(score), "ratio": ratio, "pnl": pnl,
                "reason": reason,
            })
            last_sell_date = ev["date"]

    remain_qty = sum(l["qty"] for l in holding)
    remain_cost = sum(l["qty"] * l["price"] for l in holding)
    remain_value = remain_qty * last_price
    return {
        "sim_sells": sim_sells,
        "realized_pnl": realized_pnl,
        "remain_value": remain_value,
        "unrealized": remain_value - remain_cost,
        "total_pnl": realized_pnl + remain_value - remain_cost,
        "n_sells": len(sim_sells),
    }


def compute_actual_pnl(buys, sells, last_price):
    holding = []
    realized_pnl = 0
    events = []
    for b in buys: events.append(("buy", b["date"], b))
    for s in sells: events.append(("sell", s["date"], s))
    events.sort(key=lambda x: x[1])

    for tp, d, t in events:
        if tp == "buy":
            holding.append({"qty": t["qty"], "price": t["price"]})
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
    if len(s_trades) < 2:
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
    pdf = add_chandelier_exit(pdf)

    last_price = float(pdf["close"].iloc[-1])
    actual = compute_actual_pnl(buys, sells, last_price)

    # 모든 변형 시뮬
    sims = {}
    for name, fn in VARIANTS.items():
        sims[name] = simulate(buys, pdf, last_price, fn)

    return {
        "stock": stock_name, "code": code,
        "actual": actual, "sims": sims,
        "first_buy": first_buy,
        "n_buys": len(buys), "n_sells": len(sells),
    }


def main():
    print("="*80)
    print("  2025년 이후 매매 종목 — 다중 보정치 백테스트")
    print("="*80)

    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    # 2025년 이후 매매한 종목 추출
    recent_stocks = set()
    for t in txs:
        if t.get("type") not in ("buy", "sell"): continue
        if t["date"] >= "2025-01-01":
            recent_stocks.add(t["stock"])

    # KOR 종목 + 2회 이상 거래
    targets = []
    for s in recent_stocks:
        info = smap.get(s, {})
        if info.get("nation") != "KOR" or not info.get("code"):
            continue
        n_trades = sum(1 for t in txs if t["stock"] == s and t["type"] in ("buy", "sell"))
        if n_trades < 2: continue
        targets.append((s, info["code"]))

    print(f"\n2025년 이후 거래 종목: {len(targets)}개\n")

    results = []
    for stock_name, code in targets:
        r = analyze_stock(stock_name, code, txs)
        if r:
            results.append(r)
            actual_p = r["actual"]["total_pnl"]
            sims_str = "  ".join(
                f"{name[:8]}:{fmt(s['total_pnl']):>10}"
                for name, s in r["sims"].items()
            )
            print(f"  {stock_name:<14} 실제{fmt(actual_p):>10}  {sims_str}")

    if not results: return

    # 변형별 합계
    total_actual = sum(r["actual"]["total_pnl"] for r in results)
    variant_totals = {}
    for v_name in VARIANTS:
        variant_totals[v_name] = sum(r["sims"][v_name]["total_pnl"] for r in results)

    print()
    print("="*80)
    print(f"  종합 ({len(results)}종목):")
    print(f"    실제:                    {fmt(total_actual)}")
    for v_name, total in variant_totals.items():
        diff = total - total_actual
        n_better = sum(1 for r in results if r["sims"][v_name]["total_pnl"] > r["actual"]["total_pnl"])
        print(f"    {v_name:<22} {fmt(total):>10}  (차이 {fmt(diff)}, 우세 {n_better}/{len(results)})")
    print("="*80)

    # 최선의 variant 찾기
    best = max(variant_totals.items(), key=lambda x: x[1])
    print(f"\n  🏆 최선의 변형: {best[0]} = {fmt(best[1])}")

    html = build_html(results, total_actual, variant_totals)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ {OUT}")


def build_html(results, total_actual, variant_totals):
    rows = ""
    for r in results:
        actual_p = r["actual"]["total_pnl"]
        cells = f"""<td><b>{r['stock']}</b><br><span style="color:#666;font-size:0.78em">{r['code']}</span></td>
                  <td class="mono" style="text-align:right">{fmt(actual_p)}</td>"""
        best_v = max(r["sims"].items(), key=lambda x: x[1]["total_pnl"])
        for v_name, s in r["sims"].items():
            diff = s["total_pnl"] - actual_p
            clr = "ret-down" if diff > 0 else "ret-up" if diff < 0 else ""
            best_marker = " 🏆" if v_name == best_v[0] else ""
            cells += f"""<td class="mono {clr}" style="text-align:right">{fmt(s['total_pnl'])}{best_marker}<br><span style="font-size:0.78em;color:#888">{s['n_sells']}회</span></td>"""
        rows += f"<tr>{cells}</tr>"

    # 변형별 KPI
    variant_kpis = ""
    sorted_v = sorted(variant_totals.items(), key=lambda x: -x[1])
    for i, (v_name, total) in enumerate(sorted_v):
        diff = total - total_actual
        n_better = sum(1 for r in results if r["sims"][v_name]["total_pnl"] > r["actual"]["total_pnl"])
        diff_clr = "#10b981" if diff > 0 else "#ef4444"
        rank = "🏆" if i == 0 else f"#{i+1}"
        variant_kpis += f"""<div class="kpi-mini" style="border:2px solid {diff_clr if i == 0 else '#1f2230'}">
          <div class="lbl">{rank} {v_name}</div>
          <div class="num mono">{fmt(total)}</div>
          <div class="lbl" style="color:{diff_clr}">{fmt(diff)}</div>
          <div class="lbl">우세 {n_better}/{len(results)}</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>2025+ 백테스트 v3</title>
<link rel="stylesheet" href="assets/style.css">
<style>
.kpi-strip {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }}
.kpi-strip .kpi-mini {{ flex:1; min-width:180px; background:#181b23; border-radius:8px; padding:14px; text-align:center; }}
.kpi-strip .num {{ font-size:1.4em; font-weight:700; color:#fff; }}
.kpi-strip .lbl {{ font-size:0.78em; color:#888; margin-top:4px; }}
</style></head><body>
<div class="container">
<div class="nav">
  <a href="index.html">📊 전체</a>
  <a href="backtest_top_v2.html">v2</a>
  <a href="backtest_recent.html" class="active">🎯 v3 다중 변형</a>
</div>

<h1>🎯 2025년 이후 매매 — 4가지 변형 동시 비교</h1>
<p class="subtitle">시장 환경 통일 + 다양한 보정치 시도</p>

<div class="card">
  <div class="callout">
    <b>4가지 변형:</b><br>
    <b>v3a (PnL-aware):</b> 평가이익률 기반 매도 비율. 이익 30% 미만이면 HOLD.<br>
    <b>v3b (Trailing):</b> Chandelier Exit 이탈만 매도. 추세 끝까지 탐.<br>
    <b>v3c (Aggressive Stop):</b> 손절 강화. 평가손실 시 빠른 매도.<br>
    <b>v3d (Combined):</b> Trailing + PnL + Stop 종합.
  </div>
</div>

<div class="kpi-strip">
  <div class="kpi-mini">
    <div class="num mono">{fmt(total_actual)}</div>
    <div class="lbl">실제 매매 ({len(results)}종목)</div>
  </div>
  {variant_kpis}
</div>

<div class="card">
  <h2>종목별 결과 (실제 vs 4개 변형)</h2>
  <table>
    <tr>
      <th>종목</th>
      <th style="text-align:right">실제</th>
      <th style="text-align:right">v3a (PnL)</th>
      <th style="text-align:right">v3b (Trailing)</th>
      <th style="text-align:right">v3c (Stop)</th>
      <th style="text-align:right">v3d (Combined)</th>
    </tr>
    {rows}
  </table>
</div>

</div></body></html>"""


if __name__ == "__main__":
    main()
