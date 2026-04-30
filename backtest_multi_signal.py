#!/usr/bin/env python3
"""다중 시그널 스코어링 백테스트.

사용자가 과거 거래한 종목들에 대해:
  1. 실제 매매 결과 (사용자 actual)
  2. 새 스코어링 시스템 적용 시 결과 (시뮬)
  3. 차이 비교

타겟 종목: 큰 손실 5개 + 큰 이익 5개 + 좀비 후보 일부
시그널: 가격/거래량 기반 (OBV/CMF/MFI/MA/Trailing/Failed Breakout)

스코어링 (목표가 없이, 트레일링 스탑 -25%):
  분배 시그널:    OBV bear (+2), CMF dist (+1.5)
  추세 깨짐:     MA20 break (+2), MA20<MA60 (+2), Trailing -25% (+3),
                Failed Breakout (+3)
  거래량:        Volume Climax (+2), Distribution Day 4주 5+ (+2)
  MFI 정점:     >=80 후 <75 (+1)

행동 룰:
  점수 ≥ 7: 1/2 매도
  점수 ≥ 5: 1/3 매도
  점수 ≥ 3: 1/4 매도
  매도 후 30일 cooldown (재진입 방지)

출력: dashboard/backtest_multi.html
"""
import os, sys, json, math, warnings
from datetime import datetime, timedelta
from collections import defaultdict
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from signals.price_volume import add_price_volume_signals
from signals.sudden_drop import add_sudden_drop_signals
from pykrx import stock as krx

OUT = os.path.join(BASE_DIR, "dashboard", "backtest_multi.html")


def fmt(v):
    if v is None: return "─"
    if abs(v) >= 1e8: return f"{v/1e8:+.2f}억"
    return f"{v/1e4:+,.0f}만"


def compute_signal_score(df):
    """일별 매도 시그널 점수 계산 (0~10).

    각 시그널 가중치 합산. 트레일링 스탑이 가장 강한 단일 시그널.
    """
    import pandas as pd
    df = df.copy()

    # 절대 신고가 추적 (보유 시작 후 신고가 시뮬레이션 위해)
    df["max_so_far"] = df["close"].cummax()
    df["from_max"] = (df["close"] / df["max_so_far"] - 1) * 100

    # ── 시그널별 점수 합산
    df["sig_score"] = 0.0

    # OBV 분배 다이버전스
    df["sig_score"] += df["obv_diverg_bear"].fillna(0) * 2

    # CMF 분배 (-0.10 이하 진입)
    df["cmf_dist"] = (df["cmf"] <= -0.10).astype(int)
    cmf_now_dist = (df["cmf"] <= -0.10) & (df["cmf"].shift(1) > -0.10)
    df["sig_score"] += cmf_now_dist.astype(int) * 1.5

    # MFI 과매수 후 하락 (80↑ → 75↓)
    mfi_top = (df["mfi"].shift(1) >= 80) & (df["mfi"] < 75)
    df["sig_score"] += mfi_top.astype(int) * 1

    # MA20 하향 이탈
    ma_break = (df["close"].shift(1) > df["close"].shift(1).rolling(20).mean().shift(1)) & \
               (df["close"] < df["close"].rolling(20).mean())
    df["sig_score"] += ma_break.astype(int) * 2

    # MA20 < MA60 전환 (중기 추세 하락)
    ma20 = df["close"].rolling(20).mean()
    ma60 = df["close"].rolling(60).mean()
    regime_break = (ma20.shift(1) > ma60.shift(1)) & (ma20 < ma60)
    df["sig_score"] += regime_break.astype(int) * 2

    # 트레일링 스탑 -25% (절대 고점 기준)
    df["trailing_25"] = (df["from_max"].shift(1) > -25) & (df["from_max"] <= -25)
    df["sig_score"] += df["trailing_25"].astype(int) * 3

    # Failed Breakout
    df["sig_score"] += df["is_failed_breakout"].fillna(0) * 3

    # Volume Climax
    df["sig_score"] += df["is_volume_climax"].fillna(0) * 2

    # Distribution Day 누적 (4주 5+)
    dist_alert = (df["distribution_count_4w"].fillna(0) >= 5)
    dist_alert_new = dist_alert & ~dist_alert.shift(1, fill_value=False)
    df["sig_score"] += dist_alert_new.astype(int) * 2

    df["sig_score"] = df["sig_score"].clip(0, 10)
    return df


def simulate_with_signals(buys, df_signals, last_price):
    """
    사용자 매수 + 시그널 기반 매도 시뮬레이션.

    매도 룰:
      score ≥ 7: 보유 1/2 매도
      score ≥ 5: 보유 1/3 매도
      score ≥ 3: 보유 1/4 매도
      매도 후 30일 cooldown
    """
    # 매수 → 매도 시뮬을 시간순으로
    events = []
    for b in buys:
        events.append({"date": b["date"], "type": "buy", "data": b})
    # 시그널 발동일을 매도 후보로
    idx_obj = df_signals.index
    if hasattr(idx_obj, "strftime"):
        df_idx = idx_obj.strftime("%Y-%m-%d").tolist()
    else:
        df_idx = [str(d) for d in idx_obj]
    for i, idx in enumerate(df_idx):
        score = df_signals["sig_score"].iloc[i]
        if score >= 3:
            events.append({
                "date": idx, "type": "sell_candidate",
                "data": {"score": float(score), "price": float(df_signals["close"].iloc[i])}
            })
    events.sort(key=lambda x: x["date"])

    holding = []  # [{qty, price, date}, ...]
    cumulative_buy_cost = 0
    realized_pnl = 0
    sim_sells = []
    last_sell_date = None  # cooldown용

    for ev in events:
        if ev["type"] == "buy":
            b = ev["data"]
            holding.append({"qty": b["qty"], "price": b["price"], "date": b["date"]})
            cumulative_buy_cost += b["qty"] * b["price"]
        else:
            # 매도 후보
            d = ev["date"]
            score = ev["data"]["score"]
            price = ev["data"]["price"]
            current_qty = sum(l["qty"] for l in holding)
            if current_qty <= 0:
                continue

            # Cooldown 체크
            if last_sell_date:
                d_now = datetime.strptime(d, "%Y-%m-%d")
                d_last = datetime.strptime(last_sell_date, "%Y-%m-%d")
                if (d_now - d_last).days < 30:
                    continue

            # 매도 비율
            if score >= 7:
                ratio = 0.5
            elif score >= 5:
                ratio = 1/3
            else:
                ratio = 0.25

            sell_qty = int(current_qty * ratio)
            if sell_qty <= 0:
                continue

            # FIFO
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
            sold_revenue = sell_qty * price
            pnl = sold_revenue - sold_cost
            realized_pnl += pnl
            sim_sells.append({
                "date": d, "qty": sell_qty, "price": price,
                "ratio": ratio, "score": score, "pnl": pnl,
            })
            last_sell_date = d

    # 잔여 보유 평가
    remain_qty = sum(l["qty"] for l in holding)
    remain_cost = sum(l["qty"] * l["price"] for l in holding)
    remain_value = remain_qty * last_price
    unrealized = remain_value - remain_cost

    return {
        "sim_sells": sim_sells,
        "realized_pnl": realized_pnl,
        "remain_qty": remain_qty,
        "remain_value": remain_value,
        "unrealized": unrealized,
        "total_pnl": realized_pnl + unrealized,
        "total_cost": cumulative_buy_cost,
    }


def compute_actual_pnl(buys, sells, last_price):
    """사용자 실제 매매 결과."""
    holding = []
    cumulative_buy_cost = 0
    realized_pnl = 0

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
            sold_revenue = t["qty"] * t["price"]
            realized_pnl += sold_revenue - sold_cost

    remain_qty = sum(l["qty"] for l in holding)
    remain_cost = sum(l["qty"] * l["price"] for l in holding)
    remain_value = remain_qty * last_price
    unrealized = remain_value - remain_cost

    return {
        "realized_pnl": realized_pnl,
        "remain_qty": remain_qty,
        "remain_value": remain_value,
        "unrealized": unrealized,
        "total_pnl": realized_pnl + unrealized,
        "total_cost": cumulative_buy_cost,
        "n_sells": len(sells),
    }


def analyze_stock(stock_name, code, txs):
    """단일 종목 백테스트."""
    s_trades = sorted(
        [t for t in txs if t["stock"] == stock_name and t["type"] in ("buy", "sell")],
        key=lambda x: x["date"]
    )
    if len(s_trades) < 3:
        return None

    buys = [t for t in s_trades if t["type"] == "buy"]
    sells = [t for t in s_trades if t["type"] == "sell"]

    first_buy = s_trades[0]["date"]
    start = first_buy.replace("-", "")
    # 시그널 워밍업 위해 90일 일찍
    start_dt = datetime.strptime(start, "%Y%m%d") - timedelta(days=90)
    start = start_dt.strftime("%Y%m%d")
    end = "20260424"

    try:
        pdf = krx.get_market_ohlcv_by_date(start, end, code)
    except Exception as e:
        return None
    if len(pdf) < 100:
        return None

    pdf.index = pdf.index.strftime("%Y-%m-%d")
    pdf_renamed = pdf.rename(columns={
        "시가": "open", "고가": "high", "저가": "low",
        "종가": "close", "거래량": "volume"
    }).copy()

    # 시그널 계산
    pdf_renamed = add_price_volume_signals(pdf_renamed)
    pdf_renamed = add_sudden_drop_signals(pdf_renamed)
    pdf_renamed = compute_signal_score(pdf_renamed)

    last_price = float(pdf["종가"].iloc[-1])
    actual = compute_actual_pnl(buys, sells, last_price)
    sim = simulate_with_signals(buys, pdf_renamed, last_price)

    diff = sim["total_pnl"] - actual["total_pnl"]

    # 시그널 발동일 추출
    sig_idx = pdf_renamed[pdf_renamed["sig_score"] >= 3].index
    sig_days = sig_idx.strftime("%Y-%m-%d").tolist() if hasattr(sig_idx, "strftime") else [str(d) for d in sig_idx]

    return {
        "stock": stock_name, "code": code,
        "buys": buys, "sells": sells,
        "actual": actual, "sim": sim,
        "diff": diff,
        "n_signal_days": len(sig_days),
        "n_sim_sells": len(sim["sim_sells"]),
        "first_buy": first_buy,
        "last_price": last_price,
        "pdf": pdf_renamed,
    }


def main():
    print("="*80)
    print("  다중 시그널 스코어링 백테스트 — 사용자 과거 거래 기반")
    print("="*80)

    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    # 분석 대상 자동 추출 (큰 손실 + 큰 이익)
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
        realized.append((s, pnl, v["buy_n"], v["sell_n"]))

    # TOP 5 손실 + TOP 5 이익
    losers = sorted([r for r in realized if r[1] < 0], key=lambda x: x[1])[:6]
    winners = sorted([r for r in realized if r[1] > 0], key=lambda x: -x[1])[:6]
    targets = [(s, smap[s]["code"]) for s, _, _, _ in losers + winners
                if smap.get(s, {}).get("code")]

    print(f"\n분석 대상: {len(targets)}종목")
    print(f"  큰 손실: {[s for s, _, _, _ in losers]}")
    print(f"  큰 이익: {[s for s, _, _, _ in winners]}")
    print()

    results = []
    for stock_name, code in targets:
        print(f"  분석 중: {stock_name} ({code})...")
        r = analyze_stock(stock_name, code, txs)
        if r:
            results.append(r)
            print(f"    실제 {fmt(r['actual']['total_pnl'])} / "
                  f"시뮬 {fmt(r['sim']['total_pnl'])} / "
                  f"차이 {fmt(r['diff'])}")

    if not results:
        print("ERROR: 분석 가능한 종목 없음")
        return

    # 종합 통계
    total_actual = sum(r["actual"]["total_pnl"] for r in results)
    total_sim = sum(r["sim"]["total_pnl"] for r in results)
    diff_total = total_sim - total_actual
    n_sim_better = sum(1 for r in results if r["diff"] > 0)
    n_actual_better = len(results) - n_sim_better

    print()
    print("="*80)
    print(f"  종합: 실제 {fmt(total_actual)} / 시뮬 {fmt(total_sim)} / 차이 {fmt(diff_total)}")
    print(f"  시뮬 우세: {n_sim_better}/{len(results)}종목")
    print(f"  실제 우세: {n_actual_better}/{len(results)}종목")
    print("="*80)

    # HTML 생성
    html = build_html(results, total_actual, total_sim, diff_total, n_sim_better, n_actual_better)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ {OUT}")


def build_html(results, total_actual, total_sim, diff_total, n_sim_better, n_actual_better):
    rows = ""
    for r in results:
        a = r["actual"]; s = r["sim"]
        diff_clr = "ret-down" if r["diff"] > 0 else "ret-up"
        winner = "🤖 시뮬" if r["diff"] > 0 else "👤 실제"
        sig_density = r["n_signal_days"] / len(r["pdf"]) * 100

        rows += f"""<tr>
          <td><b>{r['stock']}</b><br><span style="color:#666;font-size:0.78em">{r['code']}</span></td>
          <td class="mono" style="text-align:right">{fmt(a['total_pnl'])}<br><span style="font-size:0.78em;color:#888">{a['n_sells']}회 매도</span></td>
          <td class="mono" style="text-align:right">{fmt(s['total_pnl'])}<br><span style="font-size:0.78em;color:#888">{r['n_sim_sells']}회 매도</span></td>
          <td class="mono {diff_clr}" style="text-align:right;font-weight:600">{fmt(r['diff'])}<br><span style="font-size:0.78em">{winner}</span></td>
          <td class="mono" style="text-align:center">{r['n_signal_days']}일<br><span style="font-size:0.78em;color:#888">{sig_density:.1f}%</span></td>
        </tr>"""

    diff_clr = "#10b981" if diff_total > 0 else "#ef4444"

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>다중 시그널 백테스트</title>
<link rel="stylesheet" href="assets/style.css">
<style>
.kpi-strip {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }}
.kpi-strip .kpi-mini {{ flex:1; min-width:160px; background:#181b23; border-radius:8px; padding:14px; text-align:center; }}
.kpi-strip .num {{ font-size:1.5em; font-weight:700; color:#fff; }}
.kpi-strip .lbl {{ font-size:0.78em; color:#888; margin-top:4px; }}
</style>
</head>
<body>
<div class="container">
<div class="nav">
  <a href="index.html">📊 전체</a>
  <a href="backtest_multi.html" class="active">🧪 다중 시그널 백테스트</a>
</div>

<h1>🧪 다중 시그널 스코어링 백테스트</h1>
<p class="subtitle">목표가 없이, 가격/거래량 시그널로만 매도 → 사용자 실제 매매와 비교</p>

<div class="card">
  <div class="callout">
    <b>스코어링 룰:</b><br>
    OBV bear (+2) · CMF dist (+1.5) · MFI 정점 (+1) · MA20 break (+2) · 추세 깨짐 MA20<MA60 (+2) ·
    트레일링 -25% (+3) · Failed Breakout (+3) · Volume Climax (+2) · 분배일 4주 5+ (+2)<br>
    <br>
    <b>매도 룰 (목표가 없음):</b> 점수 ≥7 → 1/2 매도 · ≥5 → 1/3 · ≥3 → 1/4 · 매도 후 30일 cooldown
  </div>
</div>

<div class="kpi-strip">
  <div class="kpi-mini">
    <div class="num">{len(results)}</div>
    <div class="lbl">분석 종목</div>
  </div>
  <div class="kpi-mini">
    <div class="num mono">{fmt(total_actual)}</div>
    <div class="lbl">실제 매매 합계</div>
  </div>
  <div class="kpi-mini">
    <div class="num mono">{fmt(total_sim)}</div>
    <div class="lbl">🤖 시뮬 합계</div>
  </div>
  <div class="kpi-mini" style="border:2px solid {diff_clr}">
    <div class="num mono" style="color:{diff_clr}">{fmt(diff_total)}</div>
    <div class="lbl">차이</div>
  </div>
  <div class="kpi-mini">
    <div class="num">{n_sim_better}/{len(results)}</div>
    <div class="lbl">시뮬 우세 종목</div>
  </div>
</div>

<div class="card">
  <h2>종목별 결과</h2>
  <table>
    <tr>
      <th>종목</th>
      <th style="text-align:right">실제 결과</th>
      <th style="text-align:right">🤖 시뮬 결과</th>
      <th style="text-align:right">차이 / 우세</th>
      <th style="text-align:center">시그널 발동</th>
    </tr>
    {rows}
  </table>
</div>

<div class="card">
  <h2>해석</h2>
  <div class="callout">
    <b>이 백테스트가 보여주는 것:</b><br>
    • 시뮬 우세 = 시그널 따라 매도했으면 더 좋았을 종목 (대부분 손실 종목)<br>
    • 실제 우세 = 사용자 분할매수+장기보유가 더 좋았던 종목 (대부분 강세장 종목)<br>
    <br>
    <b>한계:</b><br>
    • 가격/거래량 시그널만 사용 (외인/공매도/정보 시그널 미포함)<br>
    • 실제 시스템에서는 외인+기관 매매, 공매도, 정보까지 합쳐 더 정밀한 점수<br>
    • 사용자 실제 매수일 그대로 사용 (매수 타이밍은 비교 안 함)
  </div>
</div>

</div>
</body>
</html>"""


if __name__ == "__main__":
    main()
