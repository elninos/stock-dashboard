#!/usr/bin/env python3
"""매크로 보정 백테스트 — 같은 시그널 + 매크로 환경 보정 추가.

기존 backtest_multi_signal.py와 동일하지만:
  - KOSPI/KOSDAQ 시장 환경 분류 (상승/하락/횡보)
  - 미국 나스닥 + VIX
  - USD/KRW 환율
이걸 시그널 점수에 보정 적용 → 같은 종목 동일 매수 → 다른 매도 결정.

비교:
  Plain Score: 기존 (보정 없음)
  Macro Score: 매크로 환경에 따라 가중치 조정

출력: dashboard/backtest_macro.html
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
import pandas as pd
import yfinance as yf

OUT = os.path.join(BASE_DIR, "dashboard", "backtest_macro.html")


def fmt(v):
    if v is None: return "─"
    if abs(v) >= 1e8: return f"{v/1e8:+.2f}억"
    return f"{v/1e4:+,.0f}만"


# ────────────────────────────────────────
# 매크로 데이터 로드 (한 번만)
# ────────────────────────────────────────
def load_macro_data(start: str, end: str):
    """매크로 데이터 로드 (KOSPI/KOSDAQ/나스닥/VIX/환율)."""
    print(f"  매크로 데이터 로드 ({start} ~ {end})")
    macro = {}

    # 한국 형식 (YYYYMMDD) → yfinance 형식 (YYYY-MM-DD)
    s_yf = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    e_yf = f"{end[:4]}-{end[4:6]}-{end[6:8]}"

    # KOSPI / KOSDAQ via yfinance
    try:
        kospi_yf = yf.download("^KS11", start=s_yf, end=e_yf, progress=False, auto_adjust=False)
        if len(kospi_yf) > 0:
            if isinstance(kospi_yf.columns, pd.MultiIndex):
                kospi_yf.columns = kospi_yf.columns.get_level_values(0)
            kospi_yf.index = pd.to_datetime(kospi_yf.index).strftime("%Y-%m-%d")
            macro["KOSPI"] = kospi_yf[["Close"]].rename(columns={"Close": "close"})

        kosdaq_yf = yf.download("^KQ11", start=s_yf, end=e_yf, progress=False, auto_adjust=False)
        if len(kosdaq_yf) > 0:
            if isinstance(kosdaq_yf.columns, pd.MultiIndex):
                kosdaq_yf.columns = kosdaq_yf.columns.get_level_values(0)
            kosdaq_yf.index = pd.to_datetime(kosdaq_yf.index).strftime("%Y-%m-%d")
            macro["KOSDAQ"] = kosdaq_yf[["Close"]].rename(columns={"Close": "close"})
        print(f"    KOSPI {len(macro.get('KOSPI', []))}일, KOSDAQ {len(macro.get('KOSDAQ', []))}일 ✓")
    except Exception as e:
        print(f"    [WARN] KOSPI/KOSDAQ: {e}")

    # 미국 지수 + VIX
    try:
        nasdaq = yf.download("^IXIC", start=s_yf, end=e_yf, progress=False, auto_adjust=False)
        if len(nasdaq) > 0:
            # MultiIndex 처리
            if isinstance(nasdaq.columns, pd.MultiIndex):
                nasdaq.columns = nasdaq.columns.get_level_values(0)
            nasdaq.index = pd.to_datetime(nasdaq.index).strftime("%Y-%m-%d")
            macro["NASDAQ"] = nasdaq[["Close"]].rename(columns={"Close": "close"})

        vix = yf.download("^VIX", start=s_yf, end=e_yf, progress=False, auto_adjust=False)
        if len(vix) > 0:
            if isinstance(vix.columns, pd.MultiIndex):
                vix.columns = vix.columns.get_level_values(0)
            vix.index = pd.to_datetime(vix.index).strftime("%Y-%m-%d")
            macro["VIX"] = vix[["Close"]].rename(columns={"Close": "close"})

        # USD/KRW
        krw = yf.download("KRW=X", start=s_yf, end=e_yf, progress=False, auto_adjust=False)
        if len(krw) > 0:
            if isinstance(krw.columns, pd.MultiIndex):
                krw.columns = krw.columns.get_level_values(0)
            krw.index = pd.to_datetime(krw.index).strftime("%Y-%m-%d")
            macro["USDKRW"] = krw[["Close"]].rename(columns={"Close": "close"})

        print(f"    NASDAQ {len(macro.get('NASDAQ', []))}일, VIX {len(macro.get('VIX', []))}일, USD/KRW {len(macro.get('USDKRW', []))}일 ✓")
    except Exception as e:
        print(f"    [WARN] yfinance: {e}")

    return macro


def compute_macro_signals(macro):
    """매크로 시계열에 시그널 컬럼 추가."""
    if "KOSPI" in macro:
        df = macro["KOSPI"]
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean()

        def regime(row):
            if pd.isna(row["ma60"]): return "neutral"
            if row["close"] > row["ma60"] and row["ma20"] > row["ma60"]:
                return "uptrend"
            if row["close"] < row["ma20"] and row["ma20"] < row["ma60"]:
                return "downtrend"
            return "neutral"
        df["regime"] = df.apply(regime, axis=1)

    if "KOSDAQ" in macro:
        df = macro["KOSDAQ"]
        df["ma20"] = df["close"].rolling(20).mean()
        df["ma60"] = df["close"].rolling(60).mean()
        def regime(row):
            if pd.isna(row["ma60"]): return "neutral"
            if row["close"] > row["ma60"] and row["ma20"] > row["ma60"]:
                return "uptrend"
            if row["close"] < row["ma20"] and row["ma20"] < row["ma60"]:
                return "downtrend"
            return "neutral"
        df["regime"] = df.apply(regime, axis=1)

    if "NASDAQ" in macro:
        df = macro["NASDAQ"]
        df["chg_1d"] = df["close"].pct_change() * 100
        df["chg_5d"] = df["close"].pct_change(5) * 100

    if "VIX" in macro:
        df = macro["VIX"]
        df["high_vix"] = df["close"] > 25  # 공포 지수 25 이상

    if "USDKRW" in macro:
        df = macro["USDKRW"]
        df["chg_5d"] = df["close"].pct_change(5) * 100
        df["surge"] = df["chg_5d"] > 1.5  # 5일 +1.5% 급등

    return macro


def compute_signal_score(df):
    """일별 매도 시그널 점수 (기존과 동일)."""
    df = df.copy()
    df["max_so_far"] = df["close"].cummax()
    df["from_max"] = (df["close"] / df["max_so_far"] - 1) * 100

    df["sig_score"] = 0.0
    df["sig_score"] += df["obv_diverg_bear"].fillna(0) * 2
    cmf_now_dist = (df["cmf"] <= -0.10) & (df["cmf"].shift(1) > -0.10)
    df["sig_score"] += cmf_now_dist.astype(int) * 1.5
    mfi_top = (df["mfi"].shift(1) >= 80) & (df["mfi"] < 75)
    df["sig_score"] += mfi_top.astype(int) * 1
    ma_break = (df["close"].shift(1) > df["close"].shift(1).rolling(20).mean().shift(1)) & \
               (df["close"] < df["close"].rolling(20).mean())
    df["sig_score"] += ma_break.astype(int) * 2
    ma20 = df["close"].rolling(20).mean()
    ma60 = df["close"].rolling(60).mean()
    regime_break = (ma20.shift(1) > ma60.shift(1)) & (ma20 < ma60)
    df["sig_score"] += regime_break.astype(int) * 2
    df["trailing_25"] = (df["from_max"].shift(1) > -25) & (df["from_max"] <= -25)
    df["sig_score"] += df["trailing_25"].astype(int) * 3
    df["sig_score"] += df["is_failed_breakout"].fillna(0) * 3
    df["sig_score"] += df["is_volume_climax"].fillna(0) * 2
    dist_alert = (df["distribution_count_4w"].fillna(0) >= 5)
    dist_alert_new = dist_alert & ~dist_alert.shift(1, fill_value=False)
    df["sig_score"] += dist_alert_new.astype(int) * 2

    df["sig_score"] = df["sig_score"].clip(0, 10)
    return df


def apply_macro_adjustment(df, macro, market):
    """매크로 보정 적용 — 새 컬럼 sig_score_macro."""
    df = df.copy()

    # market: KOSPI 또는 KOSDAQ
    market_df = macro.get(market)

    df["macro_adj"] = 1.0
    df["macro_add"] = 0.0

    for date in df.index:
        adj = 1.0
        add = 0.0

        # 시장 환경 (×0.7 ~ ×1.3)
        if market_df is not None and date in market_df.index:
            regime = market_df["regime"].loc[date]
            if regime == "downtrend":
                adj *= 1.3
            elif regime == "uptrend":
                adj *= 0.7

        # 미국 나스닥 (어제) 영향
        if "NASDAQ" in macro:
            ndf = macro["NASDAQ"]
            # 한국 시장에서 어제(전일)의 미국 나스닥 = 오늘 한국 시장 시작 전 데이터
            try:
                idx_pos = list(ndf.index).index(date) if date in ndf.index else None
                if idx_pos is not None and idx_pos > 0:
                    prev_chg = ndf["chg_1d"].iloc[idx_pos]
                    if pd.notna(prev_chg) and prev_chg < -2:
                        add += 1.0  # 미국 -2% 하락 다음날
            except Exception:
                pass

        # VIX (high_vix)
        if "VIX" in macro:
            vdf = macro["VIX"]
            if date in vdf.index and vdf["high_vix"].loc[date]:
                add += 1.5

        # USD/KRW 급등
        if "USDKRW" in macro:
            udf = macro["USDKRW"]
            if date in udf.index and udf.get("surge", pd.Series([False] * len(udf), index=udf.index)).loc[date]:
                add += 1.0

        df.at[date, "macro_adj"] = adj
        df.at[date, "macro_add"] = add

    df["sig_score_macro"] = (df["sig_score"] * df["macro_adj"] + df["macro_add"]).clip(0, 10)
    return df


def simulate_with_score(buys, df_signals, last_price, score_col="sig_score"):
    """주어진 점수 컬럼 기반 시뮬."""
    events = []
    for b in buys:
        events.append({"date": b["date"], "type": "buy", "data": b})

    idx_obj = df_signals.index
    df_idx = idx_obj.strftime("%Y-%m-%d").tolist() if hasattr(idx_obj, "strftime") else [str(d) for d in idx_obj]

    for i, idx in enumerate(df_idx):
        score = df_signals[score_col].iloc[i]
        if score >= 3:
            events.append({
                "date": idx, "type": "sell_candidate",
                "data": {"score": float(score), "price": float(df_signals["close"].iloc[i])}
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
            d = ev["date"]
            score = ev["data"]["score"]
            price = ev["data"]["price"]
            current_qty = sum(l["qty"] for l in holding)
            if current_qty <= 0: continue

            if last_sell_date:
                d_now = datetime.strptime(d, "%Y-%m-%d")
                d_last = datetime.strptime(last_sell_date, "%Y-%m-%d")
                if (d_now - d_last).days < 30:
                    continue

            if score >= 7: ratio = 0.5
            elif score >= 5: ratio = 1/3
            else: ratio = 0.25

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
            sold_revenue = sell_qty * price
            pnl = sold_revenue - sold_cost
            realized_pnl += pnl
            sim_sells.append({"date": d, "qty": sell_qty, "price": price, "pnl": pnl, "score": score})
            last_sell_date = d

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
    """사용자 실제."""
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


def analyze_stock(stock_name, code, txs, macro, market):
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
    start_dt = datetime.strptime(start, "%Y%m%d") - timedelta(days=90)
    start = start_dt.strftime("%Y%m%d")
    end = "20260424"

    try:
        pdf = krx.get_market_ohlcv_by_date(start, end, code)
    except Exception:
        return None
    if len(pdf) < 100:
        return None

    pdf.index = pdf.index.strftime("%Y-%m-%d")
    pdf_renamed = pdf.rename(columns={
        "시가":"open","고가":"high","저가":"low","종가":"close","거래량":"volume"
    }).copy()

    pdf_renamed = add_price_volume_signals(pdf_renamed)
    pdf_renamed = add_sudden_drop_signals(pdf_renamed)
    pdf_renamed = compute_signal_score(pdf_renamed)
    pdf_renamed = apply_macro_adjustment(pdf_renamed, macro, market)

    last_price = float(pdf["종가"].iloc[-1])
    actual = compute_actual_pnl(buys, sells, last_price)
    sim_plain = simulate_with_score(buys, pdf_renamed, last_price, "sig_score")
    sim_macro = simulate_with_score(buys, pdf_renamed, last_price, "sig_score_macro")

    return {
        "stock": stock_name, "code": code,
        "actual": actual,
        "sim_plain": sim_plain,
        "sim_macro": sim_macro,
        "diff_plain": sim_plain["total_pnl"] - actual["total_pnl"],
        "diff_macro": sim_macro["total_pnl"] - actual["total_pnl"],
        "improvement": sim_macro["total_pnl"] - sim_plain["total_pnl"],
    }


def main():
    print("="*80)
    print("  매크로 보정 백테스트 — 같은 시그널 + 매크로 환경 보정")
    print("="*80)

    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    # 매크로 데이터 (한 번만 로드)
    macro = load_macro_data("20140101", "20260424")
    macro = compute_macro_signals(macro)

    # 분석 대상 (이전과 동일)
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
    targets = [(s, smap[s]["code"], smap[s].get("market", "KOSPI"))
                for s, _, _, _ in losers + winners
                if smap.get(s, {}).get("code")]

    print(f"\n분석 대상: {len(targets)}종목")
    results = []
    for stock_name, code, market in targets:
        # 시장 매핑 (코스닥/KOSDAQ)
        market_key = "KOSDAQ" if "코스닥" in market else "KOSPI"
        print(f"  {stock_name} ({code}, {market_key})...")
        r = analyze_stock(stock_name, code, txs, macro, market_key)
        if r:
            results.append(r)
            print(f"    실제 {fmt(r['actual']['total_pnl'])} | "
                  f"Plain {fmt(r['sim_plain']['total_pnl'])} | "
                  f"Macro {fmt(r['sim_macro']['total_pnl'])} | "
                  f"Macro 개선 {fmt(r['improvement'])}")

    if not results:
        print("ERROR")
        return

    total_actual = sum(r["actual"]["total_pnl"] for r in results)
    total_plain = sum(r["sim_plain"]["total_pnl"] for r in results)
    total_macro = sum(r["sim_macro"]["total_pnl"] for r in results)
    improvement = total_macro - total_plain
    n_macro_better = sum(1 for r in results if r["improvement"] > 0)

    print()
    print("="*80)
    print(f"  종합:")
    print(f"    실제:        {fmt(total_actual)}")
    print(f"    Plain:       {fmt(total_plain)}")
    print(f"    Macro:       {fmt(total_macro)}")
    print(f"    Macro 개선:  {fmt(improvement)}")
    print(f"    Macro 우세 종목: {n_macro_better}/{len(results)}")
    print("="*80)

    # HTML
    html = build_html(results, total_actual, total_plain, total_macro, improvement, n_macro_better)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ {OUT}")


def build_html(results, total_actual, total_plain, total_macro, improvement, n_macro_better):
    rows = ""
    for r in results:
        imp_clr = "ret-down" if r["improvement"] > 0 else "ret-up" if r["improvement"] < 0 else ""
        rows += f"""<tr>
          <td><b>{r['stock']}</b><br><span style="color:#666;font-size:0.78em">{r['code']}</span></td>
          <td class="mono" style="text-align:right">{fmt(r['actual']['total_pnl'])}</td>
          <td class="mono" style="text-align:right">{fmt(r['sim_plain']['total_pnl'])}<br><span style="font-size:0.78em;color:#888">{r['sim_plain']['n_sells']}회</span></td>
          <td class="mono" style="text-align:right">{fmt(r['sim_macro']['total_pnl'])}<br><span style="font-size:0.78em;color:#888">{r['sim_macro']['n_sells']}회</span></td>
          <td class="mono {imp_clr}" style="text-align:right;font-weight:600">{fmt(r['improvement'])}</td>
        </tr>"""

    imp_clr = "#10b981" if improvement > 0 else "#ef4444"
    return f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8"><title>매크로 백테스트</title>
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
  <a href="backtest_macro.html" class="active">시그널+매크로</a>
</div>

<h1>🧪 매크로 보정 백테스트</h1>
<p class="subtitle">기존 시그널만 vs 시그널+매크로 보정 — 11종목 비교</p>

<div class="card">
  <div class="callout">
    <b>매크로 보정 룰:</b><br>
    KOSPI/KOSDAQ MA60 위 + MA20>MA60 (상승추세) → 점수 ×0.7 (보수적, HOLD 우세)<br>
    KOSPI/KOSDAQ MA20<MA60 (하락추세) → 점수 ×1.3 (적극적 매도)<br>
    + 미국 나스닥 어제 -2%↓ → 점수 +1.0<br>
    + VIX > 25 → 점수 +1.5<br>
    + USD/KRW 5일 +1.5%↑ → 점수 +1.0
  </div>
</div>

<div class="kpi-strip">
  <div class="kpi-mini">
    <div class="num mono">{fmt(total_actual)}</div>
    <div class="lbl">실제 매매</div>
  </div>
  <div class="kpi-mini">
    <div class="num mono">{fmt(total_plain)}</div>
    <div class="lbl">Plain 시그널</div>
  </div>
  <div class="kpi-mini">
    <div class="num mono">{fmt(total_macro)}</div>
    <div class="lbl">+ 매크로 보정</div>
  </div>
  <div class="kpi-mini" style="border:2px solid {imp_clr}">
    <div class="num mono" style="color:{imp_clr}">{fmt(improvement)}</div>
    <div class="lbl">매크로 개선</div>
  </div>
  <div class="kpi-mini">
    <div class="num">{n_macro_better}/{len(results)}</div>
    <div class="lbl">매크로 우세</div>
  </div>
</div>

<div class="card">
  <h2>종목별 결과</h2>
  <table>
    <tr><th>종목</th><th style="text-align:right">실제</th><th style="text-align:right">Plain</th><th style="text-align:right">+ Macro</th><th style="text-align:right">매크로 개선</th></tr>
    {rows}
  </table>
</div>

</div></body></html>"""


if __name__ == "__main__":
    main()
