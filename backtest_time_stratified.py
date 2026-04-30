#!/usr/bin/env python3
"""시기별 백테스트 — 시장 변천에 따른 시그널 성능 차이 분석.

질문: 시그널이 모든 시기에 똑같이 작동하는가? 아니면 시기에 따라 다른가?

방법:
  1. KOSPI 데이터로 시기별 regime 분류 (강세/횡보/약세)
  2. 같은 11종목을 연도/반기별로 split
  3. 각 시기에서 시그널 정확도 측정
  4. Regime별 시그널 효과 비교

출력: dashboard/backtest_time.html
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
import yfinance as yf

OUT = os.path.join(BASE_DIR, "dashboard", "backtest_time.html")


def fmt(v):
    if v is None: return "─"
    if abs(v) >= 1e8: return f"{v/1e8:+.2f}억"
    return f"{v/1e4:+,.0f}만"


def classify_market_regime():
    """KOSPI/KOSDAQ 시계열에서 시기별 regime 분류.

    regime:
      bull        : KOSPI 12개월 변화 > +15%
      bear        : KOSPI 12개월 변화 < -15%
      transition  : 그 외 (회복기/조정기)
      sideways    : -5% < 변화 < +5% 박스권
    """
    df = yf.download("^KS11", start="2014-01-01", end="2026-04-25", progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")
    df = df[["Close"]].rename(columns={"Close": "close"})
    df["chg_12m"] = df["close"].pct_change(252) * 100  # 12개월 변화
    df["chg_6m"]  = df["close"].pct_change(126) * 100

    def _regime(row):
        if pd.isna(row["chg_12m"]):
            return "warmup"
        if row["chg_12m"] >= 15:
            return "bull"
        if row["chg_12m"] <= -15:
            return "bear"
        if abs(row["chg_12m"]) <= 5 and abs(row["chg_6m"]) <= 5:
            return "sideways"
        return "transition"

    df["regime"] = df.apply(_regime, axis=1)
    return df


def get_year_half(date_str):
    """YYYY-MM-DD → 'YYYY-H1' or 'YYYY-H2'"""
    y = date_str[:4]
    m = int(date_str[5:7])
    return f"{y}-H{1 if m <= 6 else 2}"


def get_year(date_str):
    return date_str[:4]


def stratify_signals_and_actual(stock_name, code, txs, kospi_regime):
    """단일 종목: 시기별 시그널 발동 + 사용자 매매 + 가격 변화 추적."""
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

    # 시그널 발동일별 시기/regime/이후 결과
    sig_records = []
    pdf_dates = pdf.index.strftime("%Y-%m-%d").tolist() if hasattr(pdf.index, "strftime") else [str(d) for d in pdf.index]
    closes = pdf["close"].tolist()

    for i in range(20, len(pdf)):
        score = pdf["top_score"].iat[i]
        if score < 3:
            continue
        date_str = pdf_dates[i]

        # 30일 후 가격 변화
        j = min(i + 30, len(pdf) - 1)
        future_change = (closes[j] / closes[i] - 1) * 100

        # 60일 후
        k = min(i + 60, len(pdf) - 1)
        future_change_60 = (closes[k] / closes[i] - 1) * 100

        # 시장 regime
        regime = kospi_regime["regime"].get(date_str, "unknown")

        sig_records.append({
            "date": date_str,
            "year": int(date_str[:4]),
            "year_half": get_year_half(date_str),
            "score": float(score),
            "regime": regime,
            "close": closes[i],
            "ret_30d": future_change,
            "ret_60d": future_change_60,
            "stock": stock_name,
        })

    return {
        "stock": stock_name, "code": code,
        "n_buys": len(buys), "n_sells": len(sells),
        "first_buy": first_buy,
        "signals": sig_records,
    }


def aggregate_by_period(all_sigs, key_fn):
    """시기별 시그널 통계 집계."""
    agg = defaultdict(lambda: {"count": 0, "ret_30d": [], "ret_60d": [],
                                 "by_regime": defaultdict(int)})
    for s in all_sigs:
        k = key_fn(s["date"])
        agg[k]["count"] += 1
        agg[k]["ret_30d"].append(s["ret_30d"])
        agg[k]["ret_60d"].append(s["ret_60d"])
        agg[k]["by_regime"][s["regime"]] += 1

    out = {}
    for k, v in sorted(agg.items()):
        n = v["count"]
        avg_30 = sum(v["ret_30d"]) / len(v["ret_30d"]) if v["ret_30d"] else 0
        avg_60 = sum(v["ret_60d"]) / len(v["ret_60d"]) if v["ret_60d"] else 0
        # 적중률: 시그널 발동 후 30일 이내 -3% 이상 하락
        hit_30 = sum(1 for r in v["ret_30d"] if r < -3) / len(v["ret_30d"]) * 100 if v["ret_30d"] else 0
        # 큰 적중: 30일 후 -10% 이상
        big_hit = sum(1 for r in v["ret_30d"] if r < -10) / len(v["ret_30d"]) * 100 if v["ret_30d"] else 0
        out[k] = {
            "count": n,
            "avg_ret_30d": round(avg_30, 1),
            "avg_ret_60d": round(avg_60, 1),
            "hit_rate_30d": round(hit_30),
            "big_hit_30d": round(big_hit),
            "by_regime": dict(v["by_regime"]),
        }
    return out


def main():
    print("="*80)
    print("  시기별 백테스트 (Time-Stratified)")
    print("="*80)

    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    # 시장 regime
    print("\n[1] KOSPI regime 분류")
    kospi_regime = classify_market_regime()
    regime_counts = kospi_regime["regime"].value_counts().to_dict()
    print(f"    {regime_counts}")

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
        realized.append((s, pnl))

    losers = sorted([r for r in realized if r[1] < 0], key=lambda x: x[1])[:6]
    winners = sorted([r for r in realized if r[1] > 0], key=lambda x: -x[1])[:6]
    targets = [(s, smap[s]["code"]) for s, _ in losers + winners
                if smap.get(s, {}).get("code")]

    print(f"\n[2] 분석 대상: {len(targets)}종목")

    all_signals = []
    by_stock = {}
    for stock_name, code in targets:
        print(f"  {stock_name} ({code})...")
        r = stratify_signals_and_actual(stock_name, code, txs, kospi_regime)
        if r:
            all_signals.extend(r["signals"])
            by_stock[stock_name] = r

    print(f"\n[3] 총 시그널 발동: {len(all_signals)}건")

    # 시기별 집계
    by_year = aggregate_by_period(all_signals, get_year)
    by_half = aggregate_by_period(all_signals, get_year_half)
    by_regime = defaultdict(lambda: {"count": 0, "ret_30d": [], "ret_60d": []})
    for s in all_signals:
        by_regime[s["regime"]]["count"] += 1
        by_regime[s["regime"]]["ret_30d"].append(s["ret_30d"])
        by_regime[s["regime"]]["ret_60d"].append(s["ret_60d"])

    by_regime_summary = {}
    for r, v in by_regime.items():
        n = v["count"]
        avg_30 = sum(v["ret_30d"]) / len(v["ret_30d"]) if v["ret_30d"] else 0
        avg_60 = sum(v["ret_60d"]) / len(v["ret_60d"]) if v["ret_60d"] else 0
        hit_30 = sum(1 for x in v["ret_30d"] if x < -3) / len(v["ret_30d"]) * 100 if v["ret_30d"] else 0
        big_hit = sum(1 for x in v["ret_30d"] if x < -10) / len(v["ret_30d"]) * 100 if v["ret_30d"] else 0
        by_regime_summary[r] = {
            "count": n,
            "avg_ret_30d": round(avg_30, 1),
            "avg_ret_60d": round(avg_60, 1),
            "hit_rate_30d": round(hit_30),
            "big_hit_30d": round(big_hit),
        }

    print("\n[4] 시기별 시그널 정확도")
    print("\n  연도별:")
    for y, v in by_year.items():
        print(f"    {y}: {v['count']:>4}건 | 30일후 평균 {v['avg_ret_30d']:+5.1f}% | "
              f"-3%↓ 적중 {v['hit_rate_30d']:>3}% | -10%↓ 큰적중 {v['big_hit_30d']:>3}%")

    print("\n  Regime별:")
    for r, v in by_regime_summary.items():
        print(f"    {r:<12}: {v['count']:>4}건 | 30일후 평균 {v['avg_ret_30d']:+5.1f}% | "
              f"-3%↓ 적중 {v['hit_rate_30d']:>3}% | -10%↓ 큰적중 {v['big_hit_30d']:>3}%")

    # HTML
    html = build_html(by_year, by_half, by_regime_summary, by_stock, all_signals)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ {OUT}")


def build_html(by_year, by_half, by_regime, by_stock, all_signals):
    # 연도별 테이블
    year_rows = ""
    for y, v in by_year.items():
        avg30_clr = "ret-down" if v["avg_ret_30d"] < 0 else "ret-up"
        hit_clr = "ret-down" if v["hit_rate_30d"] >= 50 else "ret-up"
        year_rows += f"""<tr>
          <td>{y}</td>
          <td class="mono" style="text-align:right">{v['count']}</td>
          <td class="mono {avg30_clr}" style="text-align:right">{v['avg_ret_30d']:+.1f}%</td>
          <td class="mono" style="text-align:right">{v['avg_ret_60d']:+.1f}%</td>
          <td class="mono {hit_clr}" style="text-align:right">{v['hit_rate_30d']}%</td>
          <td class="mono" style="text-align:right">{v['big_hit_30d']}%</td>
        </tr>"""

    # Regime 테이블
    regime_rows = ""
    regime_label = {
        "bull": "🐂 강세장 (12M +15%↑)",
        "bear": "🐻 약세장 (12M -15%↓)",
        "sideways": "↔️ 박스권 (12M -5~+5%)",
        "transition": "🔄 회복/조정",
        "warmup": "데이터 부족",
        "unknown": "불명",
    }
    regime_order = ["bull", "bear", "sideways", "transition", "warmup", "unknown"]
    for r in regime_order:
        if r not in by_regime: continue
        v = by_regime[r]
        avg30_clr = "ret-down" if v["avg_ret_30d"] < 0 else "ret-up"
        hit_clr = "ret-down" if v["hit_rate_30d"] >= 50 else "ret-up"
        regime_rows += f"""<tr>
          <td>{regime_label.get(r, r)}</td>
          <td class="mono" style="text-align:right">{v['count']}</td>
          <td class="mono {avg30_clr}" style="text-align:right">{v['avg_ret_30d']:+.1f}%</td>
          <td class="mono" style="text-align:right">{v['avg_ret_60d']:+.1f}%</td>
          <td class="mono {hit_clr}" style="text-align:right;font-weight:600">{v['hit_rate_30d']}%</td>
          <td class="mono" style="text-align:right">{v['big_hit_30d']}%</td>
        </tr>"""

    # 반기 테이블 (최근 8개)
    half_items = list(by_half.items())[-12:]
    half_rows = ""
    for h, v in half_items:
        avg30_clr = "ret-down" if v["avg_ret_30d"] < 0 else "ret-up"
        hit_clr = "ret-down" if v["hit_rate_30d"] >= 50 else "ret-up"
        regime_str = " ".join(f"{k}:{vc}" for k, vc in v["by_regime"].items() if vc > 0)
        half_rows += f"""<tr>
          <td>{h}</td>
          <td class="mono" style="text-align:right">{v['count']}</td>
          <td class="mono {avg30_clr}" style="text-align:right">{v['avg_ret_30d']:+.1f}%</td>
          <td class="mono {hit_clr}" style="text-align:right">{v['hit_rate_30d']}%</td>
          <td style="font-size:0.78em">{regime_str}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>Time-Stratified Backtest</title>
<link rel="stylesheet" href="assets/style.css">
</head><body>
<div class="container">
<div class="nav">
  <a href="index.html">📊 전체</a>
  <a href="backtest_top.html">고점 판별</a>
  <a href="backtest_time.html" class="active">🕒 시기별</a>
</div>

<h1>🕒 시기별 시그널 성능 백테스트</h1>
<p class="subtitle">시장 변천에 따른 시그널 정확도 차이 — 한 가지 룰이 모든 시기에 통할까?</p>

<div class="card">
  <div class="callout">
    <b>분석 방법:</b><br>
    1. 11종목의 모든 시그널 발동일 추출 (Top Detection 점수 ≥3)<br>
    2. 각 시그널의 30일/60일 후 가격 변화 추적<br>
    3. 시기별 (연도/반기/regime) 평균 수익률 + 적중률<br>
    <br>
    <b>적중률 정의:</b> 시그널 발동 후 30일 내 -3% 이상 하락 = 적중<br>
    <b>큰 적중:</b> -10% 이상 하락 (실질적 매도 가치)<br>
    <br>
    <b>KOSPI Regime 분류:</b><br>
    🐂 강세장: 12개월 +15% 이상<br>
    🐻 약세장: 12개월 -15% 이하<br>
    ↔️ 박스권: 12개월 ±5% 이내<br>
    🔄 전환: 그 외 (회복/조정)
  </div>
</div>

<div class="card">
  <h2>🌊 KOSPI Regime별 시그널 정확도</h2>
  <p class="desc">시그널이 어떤 시장 환경에서 가장 잘 작동했나?</p>
  <table>
    <tr>
      <th>Regime</th>
      <th style="text-align:right">시그널 수</th>
      <th style="text-align:right">30일 평균</th>
      <th style="text-align:right">60일 평균</th>
      <th style="text-align:right">-3%↓ 적중률</th>
      <th style="text-align:right">-10%↓ 큰적중</th>
    </tr>
    {regime_rows}
  </table>
</div>

<div class="card">
  <h2>📅 연도별 시그널 정확도</h2>
  <p class="desc">매년 시그널이 어떻게 작동했나? 차이가 크다면 시기별 적응 룰 필요.</p>
  <table>
    <tr>
      <th>연도</th>
      <th style="text-align:right">시그널 수</th>
      <th style="text-align:right">30일 평균</th>
      <th style="text-align:right">60일 평균</th>
      <th style="text-align:right">적중률</th>
      <th style="text-align:right">큰적중률</th>
    </tr>
    {year_rows}
  </table>
</div>

<div class="card">
  <h2>📆 반기별 (최근 12개)</h2>
  <table>
    <tr>
      <th>시기</th>
      <th style="text-align:right">시그널 수</th>
      <th style="text-align:right">30일 평균</th>
      <th style="text-align:right">적중률</th>
      <th>Regime 분포</th>
    </tr>
    {half_rows}
  </table>
</div>

<div class="card">
  <h2>해석</h2>
  <div class="callout">
    <b>이 분석이 보여주는 것:</b><br>
    • Regime별 시그널 정확도 차이가 크면 → <b>시장 환경별 적응 룰 필요</b><br>
    • 강세장에서 적중률 낮음 + 약세장에서 적중률 높음 → 우리 가설 입증<br>
    • 연도별 변화 큼 → 한 가지 임계값으로 부족<br>
    <br>
    <b>활용:</b><br>
    1. 약세장 regime일 때 → 시그널 임계값 낮추기 (3 → 2)<br>
    2. 강세장 regime일 때 → 시그널 임계값 올리기 (3 → 5)<br>
    3. 시기별 자동 적응으로 false signal 감소
  </div>
</div>

</div></body></html>"""


if __name__ == "__main__":
    main()
