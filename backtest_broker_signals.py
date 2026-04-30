#!/usr/bin/env python3
"""거래원 시그널 시스템 백테스트 — 임계값 최적화.

테스트 설정:
  - 12종목 × 1년치 거래원 데이터 (KIS API)
  - 사용자 매수일 그대로
  - 시그널 점수에 따라 자동 매도 시뮬
  - 여러 변형의 결과 비교

최적화 파라미터:
  1. 매도 점수 임계값 (5/7/9/11)
  2. 평가이익률 기준 (0/30/50/100)
  3. 매수 시그널 활용 여부 (Net Score)
  4. 매도 비율 (1/4, 1/3, 1/2)
  5. Cooldown 기간 (15/30/60일)

목표: 사용자 실제 결과 +α 또는 동등한 수준에서 더 안정적으로

출력: dashboard/backtest_broker_signals.html
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

OUT = os.path.join(BASE_DIR, "dashboard", "backtest_broker_signals.html")


def fmt(v):
    if v is None: return "─"
    if abs(v) >= 1e8: return f"{v/1e8:+.2f}억"
    return f"{v/1e4:+,.0f}만"


def classify(name):
    if not name: return "small"
    for kw in ["JP모간","모간","골드만","메릴린치","UBS","CLSA","씨티","BNP",
                 "노무라","맥쿼리","다이와","외국계","홍콩상하이","도이치"]:
        if kw in name: return "foreign"
    for kw in ["키움","토스","카카오","상상인"]:
        if kw in name: return "retail"
    for kw in ["NH투자","KB증권","한국증권","한국투자","삼성증권","한화","미래에셋","신한","하나"]:
        if kw in name: return "large"
    return "small"


def compute_signals_per_day(df, dates):
    """모든 일자에 대해 매수/매도 점수 계산."""
    daily = {}

    for i, date in enumerate(dates):
        if i < 10:
            daily[date] = {"buy": 0, "sell": 0, "buy_sigs": [], "sell_sigs": []}
            continue

        cur_dates = dates[max(0, i-4): i+1]      # 최근 5일
        prev_dates = dates[max(0, i-9): max(0, i-4)]  # 직전 5일

        cur_df = df[df["date"].isin(cur_dates)]
        prev_df = df[df["date"].isin(prev_dates)]

        buy_score = 0; sell_score = 0
        buy_sigs = []; sell_sigs = []

        # 매도 시그널: 매수→매도 전환
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
            sscore = n_rev * 2 + (3 if n_rev >= 2 else 0) + (2 if n_foreign >= 1 else 0)
            sell_score += sscore
            sell_sigs.append(f"reversal({n_rev})")

        # 분배 패턴
        cur_groups = defaultdict(int)
        for (code, name), net in cur_df.groupby(["broker_code","broker_name"])["net"].sum().items():
            cur_groups[classify(name)] += net
        if cur_groups["retail"] > 0 and (cur_groups["foreign"] + cur_groups["large"]) < 0:
            sell_score += 5
            sell_sigs.append("distribution")

        # 매수 시그널
        foreign_buyers = sum(1 for (c,n), net in cur_df.groupby(["broker_code","broker_name"])["net"].sum().items()
                              if classify(n) == "foreign" and net > 0)
        if foreign_buyers >= 3 and cur_groups["foreign"] > 0:
            buy_score += 4 + min(foreign_buyers - 3, 3)
            buy_sigs.append(f"foreign_consensus({foreign_buyers})")

        # 매수 일관성 (5일 연속 같은 TOP 3)
        daily_top3_sets = []
        for d in cur_dates:
            d_df = df[df["date"] == d]
            top3 = set(d_df.nlargest(3, "net")["broker_code"].values.tolist())
            if top3:
                daily_top3_sets.append(top3)
        if len(daily_top3_sets) >= 3:
            common = set.intersection(*daily_top3_sets)
            if len(common) >= 2:
                buy_score += 3
                buy_sigs.append("consistent_buyers")

        # 역분배 (외인 매수 + 개미 매도)
        if cur_groups["foreign"] > 0 and cur_groups["retail"] < 0:
            buy_score += 4
            buy_sigs.append("smart_buy")

        daily[date] = {
            "buy": buy_score, "sell": sell_score,
            "buy_sigs": buy_sigs, "sell_sigs": sell_sigs,
        }

    return daily


def simulate(buys, sells_actual, df, prices, last_price, config):
    """단일 변형 시뮬.

    config: {
      "sell_threshold_high": 9,
      "sell_threshold_mid": 7,
      "sell_threshold_low": 5,
      "ratio_high": 0.5,
      "ratio_mid": 1/3,
      "ratio_low": 0.25,
      "min_pnl_for_sell": 30,  # 평가이익 N% 이상에서만 매도
      "use_net_score": False,  # 매수 점수도 고려 (sell - buy)
      "cooldown_days": 30,
    }
    """
    dates = sorted(df["date"].unique())
    daily_signals = compute_signals_per_day(df, dates)

    holding = []
    cumulative_buy_cost = 0
    realized_pnl = 0
    sim_sells = []
    last_sell_date = None

    # 사용자 매수 + 일자별 평가
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
            cumulative_buy_cost += b["qty"] * b["price"]
        else:
            current_qty = sum(l["qty"] for l in holding)
            if current_qty <= 0: continue

            sig = daily_signals.get(ev["date"], {})
            sell_s = sig.get("sell", 0)
            buy_s = sig.get("buy", 0)

            # Net 점수 옵션
            if config.get("use_net_score"):
                effective_sell = sell_s - buy_s * 0.5
            else:
                effective_sell = sell_s

            # 평가이익률
            avg = sum(l["qty"]*l["price"] for l in holding) / current_qty
            cur_price = prices.get(ev["date"], avg)
            pnl_pct = (cur_price / avg - 1) * 100

            if pnl_pct < config.get("min_pnl_for_sell", 0):
                continue

            # 비율 결정
            if effective_sell >= config["sell_threshold_high"]:
                ratio = config["ratio_high"]
            elif effective_sell >= config["sell_threshold_mid"]:
                ratio = config["ratio_mid"]
            elif effective_sell >= config["sell_threshold_low"]:
                ratio = config["ratio_low"]
            else:
                continue

            # Cooldown
            if last_sell_date:
                d_now = datetime.strptime(ev["date"], "%Y-%m-%d")
                d_last = datetime.strptime(last_sell_date, "%Y-%m-%d")
                if (d_now - d_last).days < config.get("cooldown_days", 30):
                    continue

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
                if lot["qty"] <= 0:
                    holding.pop(0)
            sold_revenue = sell_qty * cur_price
            pnl = sold_revenue - sold_cost
            realized_pnl += pnl
            sim_sells.append({
                "date": ev["date"], "score": sell_s,
                "ratio": ratio, "pnl": pnl,
            })
            last_sell_date = ev["date"]

    remain_qty = sum(l["qty"] for l in holding)
    remain_cost = sum(l["qty"]*l["price"] for l in holding)
    remain_value = remain_qty * last_price

    return {
        "realized_pnl": realized_pnl,
        "remain_value": remain_value,
        "unrealized": remain_value - remain_cost,
        "total_pnl": realized_pnl + remain_value - remain_cost,
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
    rv = rq * last_price
    return pnl + rv - rc


def main():
    print("="*80)
    print("  거래원 시그널 백테스트 — 임계값 최적화")
    print("="*80)

    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    # 보유 종목
    qty = defaultdict(int)
    for t in txs:
        if t.get("type") == "buy":
            qty[t["stock"]] += t.get("qty", 0)
        elif t.get("type") == "sell":
            qty[t["stock"]] -= t.get("qty", 0)

    holdings = []
    EXCLUDE = ["KODEX","TIME","TIGER"]
    for s, q in qty.items():
        if q > 0:
            info = smap.get(s, {})
            if info.get("nation") == "KOR" and info.get("code") and \
               not any(kw in s for kw in EXCLUDE):
                holdings.append((s, info["code"]))

    print(f"\n분석 대상: {len(holdings)}종목")

    # 거래원 매핑
    mapping = build_broker_mapping([c for _, c in holdings[:10]])

    # 종목별 데이터 수집
    print("\n[데이터 수집]")
    stock_data = {}
    for stock_name, code in holdings:
        print(f"  {stock_name} ({code})...")
        try:
            today = "20260424"
            start = (datetime.strptime(today,"%Y%m%d") - timedelta(days=365)).strftime("%Y%m%d")
            results = fetch_all_brokers_daily(code, start, today, min_vol=100)
            if not results: continue
            df = aggregate_to_dataframe(results, mapping)
            if df is None or df.empty: continue
            df["date"] = df["date"].apply(lambda d: f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(str(d))==8 else d)

            pdf = krx.get_market_ohlcv_by_date(start, today, code)
            pdf.index = pdf.index.strftime("%Y-%m-%d")
            prices = pdf["종가"].to_dict()

            # 사용자 매매 (분석 기간 내)
            s_iso = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
            buys = [t for t in txs if t["stock"] == stock_name and t["type"] == "buy" and t["date"] >= s_iso]
            sells = [t for t in txs if t["stock"] == stock_name and t["type"] == "sell" and t["date"] >= s_iso]

            if not buys: continue
            stock_data[stock_name] = {
                "code": code, "df": df, "prices": prices,
                "buys": buys, "sells": sells,
                "last_price": float(pdf["종가"].iloc[-1]),
            }
        except Exception as e:
            print(f"    [ERR] {e}")

    print(f"\n  분석 가능: {len(stock_data)}종목")

    # 변형 정의
    variants = {
        "v1_baseline": {
            "sell_threshold_high": 9, "sell_threshold_mid": 7, "sell_threshold_low": 5,
            "ratio_high": 0.5, "ratio_mid": 1/3, "ratio_low": 0.25,
            "min_pnl_for_sell": 30, "cooldown_days": 30, "use_net_score": False,
        },
        "v2_aggressive": {
            "sell_threshold_high": 7, "sell_threshold_mid": 5, "sell_threshold_low": 3,
            "ratio_high": 0.5, "ratio_mid": 1/3, "ratio_low": 0.25,
            "min_pnl_for_sell": 30, "cooldown_days": 30, "use_net_score": False,
        },
        "v3_conservative": {
            "sell_threshold_high": 11, "sell_threshold_mid": 9, "sell_threshold_low": 7,
            "ratio_high": 0.5, "ratio_mid": 1/3, "ratio_low": 0.25,
            "min_pnl_for_sell": 50, "cooldown_days": 60, "use_net_score": False,
        },
        "v4_use_buy": {
            "sell_threshold_high": 9, "sell_threshold_mid": 7, "sell_threshold_low": 5,
            "ratio_high": 0.5, "ratio_mid": 1/3, "ratio_low": 0.25,
            "min_pnl_for_sell": 30, "cooldown_days": 30, "use_net_score": True,
        },
        "v5_high_pnl_only": {
            "sell_threshold_high": 9, "sell_threshold_mid": 7, "sell_threshold_low": 5,
            "ratio_high": 0.5, "ratio_mid": 1/3, "ratio_low": 0.25,
            "min_pnl_for_sell": 50, "cooldown_days": 30, "use_net_score": True,
        },
        "v6_small_partial": {
            "sell_threshold_high": 9, "sell_threshold_mid": 7, "sell_threshold_low": 5,
            "ratio_high": 1/3, "ratio_mid": 0.25, "ratio_low": 0.15,
            "min_pnl_for_sell": 50, "cooldown_days": 45, "use_net_score": True,
        },
        "v7_strict_only": {
            "sell_threshold_high": 11, "sell_threshold_mid": 9, "sell_threshold_low": 999,
            "ratio_high": 0.5, "ratio_mid": 1/3, "ratio_low": 0,
            "min_pnl_for_sell": 50, "cooldown_days": 60, "use_net_score": True,
        },
    }

    # 시뮬레이션
    print("\n[변형별 백테스트]")
    all_results = {}
    for v_name, config in variants.items():
        per_stock = {}
        for stock_name, data in stock_data.items():
            sim = simulate(data["buys"], data["sells"], data["df"],
                            data["prices"], data["last_price"], config)
            per_stock[stock_name] = sim
        all_results[v_name] = per_stock
        total = sum(s["total_pnl"] for s in per_stock.values())
        n_better = sum(1 for s, d in stock_data.items()
                        if per_stock[s]["total_pnl"] > actual_pnl(d["buys"], d["sells"], d["last_price"]))
        print(f"  {v_name}: 합계 {fmt(total)}, 우세 {n_better}/{len(per_stock)}")

    # 사용자 실제
    actuals = {s: actual_pnl(d["buys"], d["sells"], d["last_price"]) for s, d in stock_data.items()}
    actual_total = sum(actuals.values())
    print(f"\n  실제 매매 합계: {fmt(actual_total)}")

    # 최선 변형
    best_v = max(all_results.items(), key=lambda x: sum(s["total_pnl"] for s in x[1].values()))
    print(f"\n  🏆 최선 변형: {best_v[0]} = {fmt(sum(s['total_pnl'] for s in best_v[1].values()))}")

    # HTML
    html = build_html(stock_data, all_results, actuals, variants)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ {OUT}")


def build_html(stock_data, all_results, actuals, variants):
    actual_total = sum(actuals.values())
    variant_totals = {v: sum(s["total_pnl"] for s in r.values()) for v, r in all_results.items()}

    sorted_v = sorted(variant_totals.items(), key=lambda x: -x[1])

    # 변형 설명
    desc = {
        "v1_baseline":     "기본: 매도≥9 1/2, ≥7 1/3, ≥5 1/4 (이익 30%↑), cooldown 30일",
        "v2_aggressive":   "적극: 매도≥7 1/2, ≥5 1/3, ≥3 1/4 (이익 30%↑)",
        "v3_conservative": "보수: 매도≥11 1/2, ≥9 1/3, ≥7 1/4 (이익 50%↑), cooldown 60일",
        "v4_use_buy":      "Net Score: 매수 점수도 차감 (효과적 매도 = 매도 - 매수×0.5)",
        "v5_high_pnl_only": "큰 이익만: 평가이익 50%↑에서만 매도 + Net Score",
        "v6_small_partial": "소량 분할: 1/3, 1/4, 1/6씩 매도 + 이익 50%↑ + Net",
        "v7_strict_only":  "엄격: 점수≥9만 매도 + 이익 50%↑ + cooldown 60",
    }

    # KPI
    kpis = ""
    for i, (v_name, total) in enumerate(sorted_v):
        diff = total - actual_total
        diff_clr = "#10b981" if diff > 0 else "#ef4444"
        n_better = sum(1 for s in stock_data if all_results[v_name][s]["total_pnl"] > actuals[s])
        rank = "🏆" if i == 0 else f"#{i+1}"
        border = f"border:2px solid {diff_clr}" if i == 0 else ""
        kpis += f"""<div class="kpi-mini" style="{border}">
          <div class="lbl">{rank} {v_name}</div>
          <div class="num mono">{fmt(total)}</div>
          <div class="lbl mono" style="color:{diff_clr}">{fmt(diff)}</div>
          <div class="lbl">우세 {n_better}/{len(stock_data)}</div>
        </div>"""

    # 종목별 비교 테이블
    rows = ""
    for stock, d in sorted(stock_data.items()):
        a = actuals[stock]
        cells = f"""<td><b>{stock}</b><br><span style="color:#666;font-size:0.78em">{d['code']}</span></td>
                  <td class="mono" style="text-align:right">{fmt(a)}</td>"""
        best_v = max(all_results.items(), key=lambda x: x[1][stock]["total_pnl"])[0]
        for v_name, _ in sorted_v:
            r = all_results[v_name][stock]
            diff = r["total_pnl"] - a
            clr = "ret-down" if diff > 0 else "ret-up" if diff < 0 else ""
            best_marker = " 🏆" if v_name == best_v else ""
            cells += f"""<td class="mono {clr}" style="text-align:right">{fmt(r['total_pnl'])}{best_marker}<br>
                       <span style="font-size:0.78em;color:#888">{r['n_sells']}회</span></td>"""
        rows += f"<tr>{cells}</tr>"

    # 변형 설명 테이블
    desc_rows = ""
    for v_name, total in sorted_v:
        d = desc.get(v_name, "")
        config_str = ", ".join(f"{k}={v}" for k, v in variants[v_name].items())
        desc_rows += f"""<tr>
          <td><b>{v_name}</b></td>
          <td>{d}</td>
          <td class="mono" style="text-align:right">{fmt(total)}</td>
        </tr>"""

    # 헤더 변형 컬럼
    header_cols = ""
    for v_name, _ in sorted_v:
        header_cols += f'<th style="text-align:right">{v_name}</th>'

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>거래원 시그널 임계값 최적화</title>
<link rel="stylesheet" href="assets/style.css">
<style>
.kpi-strip {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }}
.kpi-strip .kpi-mini {{ flex:1; min-width:160px; background:#181b23; border-radius:8px; padding:12px; text-align:center; }}
.kpi-strip .num {{ font-size:1.3em; font-weight:700; color:#fff; }}
.kpi-strip .lbl {{ font-size:0.75em; color:#888; margin-top:4px; }}
</style></head><body>
<div class="container">

<div class="nav">
  <a href="index.html">📊 전체</a>
  <a href="holdings_action.html">행동 권고</a>
  <a href="backtest_broker_signals.html" class="active">🎯 시그널 최적화</a>
</div>

<h1>🎯 거래원 시그널 임계값 최적화 (백테스트)</h1>
<p class="subtitle">7개 변형 동시 비교 — 1년치 거래원 데이터로 최선 룰 찾기</p>

<div class="card">
  <div class="callout">
    <b>테스트 방법:</b><br>
    각 변형별로 동일 종목 + 동일 매수일 + 다른 매도 룰 적용.<br>
    합계 / 종목별 우세 / 매도 횟수 비교.<br>
    <br>
    <b>목표:</b> 사용자 실제 결과 +α 또는 동등 수준에서 더 안정적 룰 찾기
  </div>
</div>

<div class="kpi-strip">
  <div class="kpi-mini" style="border:2px solid #6b7280">
    <div class="lbl">실제 매매</div>
    <div class="num mono">{fmt(actual_total)}</div>
    <div class="lbl">{len(stock_data)}종목 합계</div>
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
  <h2>종목별 결과 비교</h2>
  <table>
    <tr><th>종목</th><th style="text-align:right">실제</th>{header_cols}</tr>
    {rows}
  </table>
</div>

</div></body></html>"""


if __name__ == "__main__":
    main()
