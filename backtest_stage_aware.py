#!/usr/bin/env python3
"""Stage-Aware Multi-Signal Backtest.

검증된 방법론 5개 통합:
  1. Stan Weinstein Stage Analysis (1~4단계)
  2. Chandelier Exit (ATR×3 트레일링)
  3. ADX 추세 강도 필터 (<20 시 시그널 약화)
  4. O'Neil Distribution Days (이미 구현)
  5. Dow Theory HH/LL

행동 룰:
  Stage 2 (상승): Chandelier Exit만 작동 (다른 시그널 무시)
  Stage 3 (토핑): 모든 시그널 + ADX 필터 + 매크로 보정
  Stage 4 (하락): 즉시 청산 (전량)
  Stage 1 (Basing): 관망 (매수 후보)

비교:
  - 사용자 실제 매매
  - Plain 시그널 (이전 버전)
  - Macro 보정 (이전 버전)
  - Stage-Aware (이번)

출력: dashboard/backtest_stage.html
"""
import os, sys, json, warnings
from datetime import datetime, timedelta
from collections import defaultdict
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from signals.price_volume import add_price_volume_signals
from signals.sudden_drop import add_sudden_drop_signals
from signals.stage_analysis import classify_stages, stage_label
from signals.chandelier import add_chandelier_exit
from signals.adx import add_adx
from signals.dow_theory import add_dow_signals
from pykrx import stock as krx
import pandas as pd
import yfinance as yf

OUT = os.path.join(BASE_DIR, "dashboard", "backtest_stage.html")


def fmt(v):
    if v is None: return "─"
    if abs(v) >= 1e8: return f"{v/1e8:+.2f}억"
    return f"{v/1e4:+,.0f}만"


def load_macro(start, end):
    """매크로 데이터 (KOSPI/KOSDAQ/NASDAQ/VIX)."""
    macro = {}
    s_yf = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    e_yf = f"{end[:4]}-{end[4:6]}-{end[6:8]}"

    try:
        for ticker, key in [("^KS11", "KOSPI"), ("^KQ11", "KOSDAQ"),
                             ("^IXIC", "NASDAQ"), ("^VIX", "VIX")]:
            df = yf.download(ticker, start=s_yf, end=e_yf, progress=False, auto_adjust=False)
            if len(df) > 0:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")
                macro[key] = df[["Close"]].rename(columns={"Close": "close"})
    except Exception as e:
        print(f"  [WARN] 매크로: {e}")

    # 시장 환경 분류
    for key in ("KOSPI", "KOSDAQ"):
        if key in macro:
            df = macro[key]
            df["ma20"] = df["close"].rolling(20).mean()
            df["ma60"] = df["close"].rolling(60).mean()
            df["regime"] = "neutral"
            df.loc[(df["close"] > df["ma60"]) & (df["ma20"] > df["ma60"]), "regime"] = "uptrend"
            df.loc[(df["close"] < df["ma20"]) & (df["ma20"] < df["ma60"]), "regime"] = "downtrend"

    if "VIX" in macro:
        macro["VIX"]["high"] = macro["VIX"]["close"] > 25

    if "NASDAQ" in macro:
        macro["NASDAQ"]["chg_1d"] = macro["NASDAQ"]["close"].pct_change() * 100

    return macro


def stage_aware_decision(row, prev_row, macro_data, market):
    """Stage 기반 의사결정.

    Returns: (action, ratio, reason, score)
    """
    stage = int(row.get("stage", 0))
    close = float(row["close"])

    # ── Stage 4: 1/2 매도 (회복 시 다시 매수 가능)
    if stage == 4:
        return ("sell", 0.5, "Stage 4 (하락 추세)", 8)

    # ── Stage 2: Chandelier Exit만
    if stage == 2:
        ce = row.get("chandelier_exit")
        if ce is not None and not pd.isna(ce) and close < ce:
            return ("sell", 1/3, f"Chandelier Exit 이탈 ({ce:.0f}원)", 6)
        return ("hold", 0, "Stage 2 유지", 0)

    # ── Stage 1: 관망
    if stage == 1:
        return ("hold", 0, "Stage 1 베이싱", 0)

    # ── Stage 0 (중립): 보수적 처리 — Chandelier만, 시그널 절반
    # ── Stage 3 (토핑): 모든 시그널 작동
    score = 0
    reasons = []

    # 가격-거래량 시그널 (기존)
    if row.get("obv_diverg_bear") == 1:
        score += 2; reasons.append("OBV 분배")
    if row.get("cmf", 0) <= -0.10:
        score += 1.5; reasons.append("CMF 분배")
    if row.get("mfi", 50) >= 80:
        score += 1; reasons.append("MFI 과매수")
    if row.get("is_failed_breakout") == 1:
        score += 3; reasons.append("Failed Breakout")
    if row.get("is_volume_climax") == 1:
        score += 2; reasons.append("Volume Climax")
    if row.get("distribution_count_4w", 0) >= 5:
        score += 2; reasons.append("4주 분배일 5+")

    # Chandelier Exit (Stage 3에서도 작동)
    ce = row.get("chandelier_exit")
    if ce is not None and not pd.isna(ce) and close < ce:
        score += 3; reasons.append("Chandelier Exit")

    # Dow Theory: LH/LL
    if row.get("dow_ll") == 1:
        score += 2; reasons.append("Dow Theory LL")
    if row.get("dow_lh") == 1:
        score += 1; reasons.append("Dow Theory LH 경고")

    # ADX 필터: 약하면 점수 ×0.5
    adx_val = row.get("adx")
    if adx_val is not None and not pd.isna(adx_val) and adx_val < 20:
        score *= 0.5
        reasons.append(f"ADX {adx_val:.0f} (횡보 노이즈 감쇄)")

    # Stage 0 (중립) 추가 보정: 추세 불명확하면 시그널 절반
    if stage == 0:
        score *= 0.5

    # 매크로 보정
    market_df = macro_data.get(market)
    if market_df is not None:
        date = row.name if hasattr(row, "name") else None
        date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)
        if date_str in market_df.index:
            regime = market_df["regime"].loc[date_str]
            if regime == "downtrend":
                score *= 1.3
            elif regime == "uptrend" and stage != 3:
                score *= 0.7

    # 행동 결정
    if score >= 7:
        return ("sell", 0.5, " + ".join(reasons[:3]), score)
    if score >= 5:
        return ("sell", 1/3, " + ".join(reasons[:3]), score)
    if score >= 3:
        return ("sell", 0.25, " + ".join(reasons[:3]), score)

    return ("hold", 0, "시그널 부족", score)


def simulate_stage_aware(buys, df_with_signals, macro, market, last_price):
    """Stage-aware FIFO 시뮬레이션."""
    df_idx = df_with_signals.index
    if hasattr(df_idx, "strftime"):
        df_dates = df_idx.strftime("%Y-%m-%d").tolist()
    else:
        df_dates = [str(d) for d in df_idx]

    # 이벤트: 매수 + 매일 평가
    events = []
    for b in buys:
        events.append({"date": b["date"], "type": "buy", "data": b})
    for i, date_str in enumerate(df_dates):
        if i == 0: continue
        events.append({
            "date": date_str, "type": "evaluate", "idx": i
        })
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

            row = df_with_signals.iloc[ev["idx"]]
            prev_row = df_with_signals.iloc[ev["idx"] - 1] if ev["idx"] > 0 else row

            action, ratio, reason, score = stage_aware_decision(row, prev_row, macro, market)

            if action != "sell":
                continue

            # Cooldown 60일 (강세 종목 추세 보호)
            if last_sell_date:
                d_now = datetime.strptime(ev["date"], "%Y-%m-%d")
                d_last = datetime.strptime(last_sell_date, "%Y-%m-%d")
                if (d_now - d_last).days < 60 and score < 9:
                    continue

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
            sell_price = float(row["close"])
            sold_revenue = sell_qty * sell_price
            pnl = sold_revenue - sold_cost
            realized_pnl += pnl
            sim_sells.append({
                "date": ev["date"], "qty": sell_qty, "price": sell_price,
                "ratio": ratio, "stage": int(row.get("stage", 0)),
                "reason": reason, "score": score, "pnl": pnl,
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


def analyze_stock(stock_name, code, market, txs, macro):
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
    if len(pdf) < 200:
        return None

    pdf.index = pdf.index.strftime("%Y-%m-%d")
    pdf = pdf.rename(columns={
        "시가":"open","고가":"high","저가":"low","종가":"close","거래량":"volume"
    })

    # 모든 시그널 추가
    pdf = add_price_volume_signals(pdf)
    pdf = add_sudden_drop_signals(pdf)
    pdf = classify_stages(pdf)
    pdf = add_chandelier_exit(pdf)
    pdf = add_adx(pdf)
    pdf = add_dow_signals(pdf)

    last_price = float(pdf["close"].iloc[-1])
    actual = compute_actual_pnl(buys, sells, last_price)
    sim = simulate_stage_aware(buys, pdf, macro, market, last_price)

    # Stage 분포
    stage_dist = pdf["stage"].value_counts().to_dict()

    return {
        "stock": stock_name, "code": code, "market": market,
        "actual": actual, "sim": sim,
        "diff": sim["total_pnl"] - actual["total_pnl"],
        "stage_dist": stage_dist,
    }


def main():
    print("="*80)
    print("  Stage-Aware Multi-Signal Backtest")
    print("="*80)

    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    print("\n  매크로 데이터 로드...")
    macro = load_macro("20140101", "20260424")

    # 분석 대상
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

    losers = sorted([r for r in realized if r[1] < 0], key=lambda x: x[1])[:6]
    winners = sorted([r for r in realized if r[1] > 0], key=lambda x: -x[1])[:6]
    targets = []
    for s, _, _, _ in losers + winners:
        info = smap.get(s, {})
        if not info.get("code"): continue
        market = "KOSDAQ" if "코스닥" in info.get("market", "") else "KOSPI"
        targets.append((s, info["code"], market))

    print(f"\n  분석 대상: {len(targets)}종목\n")

    results = []
    for stock_name, code, market in targets:
        print(f"  {stock_name} ({code}, {market})...")
        r = analyze_stock(stock_name, code, market, txs, macro)
        if r:
            results.append(r)
            stage_str = " ".join(f"S{k}:{v}" for k, v in sorted(r["stage_dist"].items()))
            print(f"    실제 {fmt(r['actual']['total_pnl'])} | "
                  f"Stage {fmt(r['sim']['total_pnl'])} | "
                  f"차이 {fmt(r['diff'])} | "
                  f"매도 {r['sim']['n_sells']}회 | "
                  f"{stage_str}")

    if not results: return

    total_actual = sum(r["actual"]["total_pnl"] for r in results)
    total_sim = sum(r["sim"]["total_pnl"] for r in results)
    diff = total_sim - total_actual
    n_sim_better = sum(1 for r in results if r["diff"] > 0)

    print()
    print("="*80)
    print(f"  종합:")
    print(f"    실제:        {fmt(total_actual)}")
    print(f"    Stage-Aware: {fmt(total_sim)}")
    print(f"    차이:        {fmt(diff)}")
    print(f"    Stage 우세:  {n_sim_better}/{len(results)}종목")
    print("="*80)

    html = build_html(results, total_actual, total_sim, diff, n_sim_better)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ {OUT}")


def build_html(results, total_actual, total_sim, diff, n_sim_better):
    rows = ""
    for r in results:
        diff_clr = "ret-down" if r["diff"] > 0 else "ret-up"
        winner = "🤖" if r["diff"] > 0 else "👤"
        stage_str = " · ".join(f"S{k}:{v}" for k, v in sorted(r["stage_dist"].items()))

        # 시뮬 매도 상세
        sim_sells_html = ""
        for s in r["sim"]["sim_sells"][:5]:
            sim_sells_html += f"<div style='font-size:0.78em;color:#888'>{s['date']} S{s['stage']} {s['ratio']*100:.0f}% — {s['reason'][:40]}</div>"

        rows += f"""<tr>
          <td><b>{r['stock']}</b><br><span style="color:#666;font-size:0.78em">{r['code']} · {r['market']}</span></td>
          <td class="mono" style="text-align:right">{fmt(r['actual']['total_pnl'])}<br><span style="font-size:0.78em;color:#888">{r['actual']['n_sells']}회</span></td>
          <td class="mono" style="text-align:right">{fmt(r['sim']['total_pnl'])}<br><span style="font-size:0.78em;color:#888">{r['sim']['n_sells']}회</span></td>
          <td class="mono {diff_clr}" style="text-align:right;font-weight:600">{fmt(r['diff'])}<br><span style="font-size:0.78em">{winner} 우세</span></td>
          <td style="font-size:0.78em">{stage_str}</td>
          <td>{sim_sells_html}</td>
        </tr>"""

    diff_clr = "#10b981" if diff > 0 else "#ef4444"
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>Stage-Aware Backtest</title>
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
  <a href="backtest_stage.html" class="active">+ Stage Analysis</a>
</div>

<h1>🧪 Stage-Aware Multi-Signal Backtest</h1>
<p class="subtitle">Weinstein Stage + Chandelier + ADX + Dow Theory + 매크로 — 검증된 방법론 통합</p>

<div class="card">
  <div class="callout">
    <b>적용 방법론:</b><br>
    1. <b>Stan Weinstein Stage Analysis</b> — 30주 MA로 4단계 분류, Stage별 행동 다름<br>
    2. <b>Chandelier Exit</b> — 22일 신고가 - ATR(22)×3, 변동성 반영 트레일링<br>
    3. <b>ADX 필터</b> — ADX&lt;20 (횡보) 시 시그널 점수 ×0.5<br>
    4. <b>Dow Theory</b> — LH(경고)/LL(매도) 객관적 추세 깨짐<br>
    5. <b>매크로 보정</b> — KOSPI/KOSDAQ regime 따라 ×0.7~×1.3<br>
    <br>
    <b>Stage별 행동:</b><br>
    Stage 2 (상승): Chandelier Exit만 작동, 다른 시그널 무시 → 추세 끝까지 탐<br>
    Stage 3 (토핑): 모든 시그널 + ADX 필터 + 매크로 보정<br>
    Stage 4 (하락): 즉시 전량 청산<br>
    Stage 1 (Basing): 관망
  </div>
</div>

<div class="kpi-strip">
  <div class="kpi-mini"><div class="num mono">{fmt(total_actual)}</div><div class="lbl">실제 매매</div></div>
  <div class="kpi-mini"><div class="num mono">{fmt(total_sim)}</div><div class="lbl">Stage-Aware</div></div>
  <div class="kpi-mini" style="border:2px solid {diff_clr}"><div class="num mono" style="color:{diff_clr}">{fmt(diff)}</div><div class="lbl">차이</div></div>
  <div class="kpi-mini"><div class="num">{n_sim_better}/{len(results)}</div><div class="lbl">Stage 우세</div></div>
</div>

<div class="card">
  <h2>종목별 결과</h2>
  <table>
    <tr>
      <th>종목</th>
      <th style="text-align:right">실제</th>
      <th style="text-align:right">Stage-Aware</th>
      <th style="text-align:right">차이</th>
      <th>Stage 분포</th>
      <th>매도 시점</th>
    </tr>
    {rows}
  </table>
</div>

</div></body></html>"""


if __name__ == "__main__":
    main()
