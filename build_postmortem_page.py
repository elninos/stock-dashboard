#!/usr/bin/env python3
"""매매 사후 분석 (Postmortem) — 사용자 매수/매도 vs 시그널 비교.

각 보유 종목에 대해:
  1. 사용자 매수/매도 이력
  2. 가격 차트 + 매수(▲)/매도(▼) 마커
  3. OBV/CMF/MFI 시그널 발생 시점
  4. "이상적 익절 시점" 자동 식별
  5. 사용자 매도가 빨랐나 늦었나 평가

dashboard/postmortem.html
"""
import os, sys, warnings, json, math
from collections import defaultdict
from datetime import datetime, timedelta
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from signals.broker_flow import load_stock_flow, build_timeseries, detect_signals
from signals.price_volume import add_price_volume_signals

OUT = os.path.join(BASE_DIR, "dashboard", "postmortem.html")
FLOW_DIR = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-srshin614@gmail.com"
    "/내 드라이브/01.Claude/01.주식/daily_flow"
)
TODAY_STR = "20260424"
START_STR = "20240101"  # 2년치


def clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)): return None
    return v.item() if hasattr(v, "item") else v


def fetch_price(code, start, end):
    from pykrx import stock as krx
    try:
        df = krx.get_market_ohlcv_by_date(start, end, code)
        df.index = df.index.strftime("%Y-%m-%d")
        return df
    except Exception:
        return None


def evaluate_sell(sell_date, sell_price, price_series):
    """매도 후 5/30/60일 주가 변화 → 매도 평가.

    매도 후 가격이 더 올랐다면 = 너무 일찍 판 매도 (놓친 이익)
    매도 후 가격이 빠졌다면 = 잘 판 매도
    """
    idx_list = list(price_series.index)
    if sell_date not in idx_list:
        return None
    i = idx_list.index(sell_date)

    def get_after(off):
        j = i + off
        if j >= len(idx_list): return None
        return float(price_series.iloc[j])

    p5 = get_after(5)
    p30 = get_after(30)
    p60 = get_after(60)

    # 30일 후 가격 기준 평가
    eval_text = "─"
    eval_class = ""
    missed_pct = None
    if p30 is not None:
        chg = (p30 / sell_price - 1) * 100
        missed_pct = chg
        if chg > 10:
            eval_text = f"❌ 너무 일찍 판 매도 (+{chg:.1f}% 놓침)"
            eval_class = "ret-up"
        elif chg > 3:
            eval_text = f"△ 약간 일찍 판 매도 (+{chg:.1f}% 놓침)"
            eval_class = "ret-up"
        elif chg < -10:
            eval_text = f"✓ 잘 판 매도 ({chg:.1f}% 회피)"
            eval_class = "ret-down"
        elif chg < -3:
            eval_text = f"○ 적절한 매도 ({chg:.1f}% 회피)"
            eval_class = "ret-down"
        else:
            eval_text = f"= 비슷 ({chg:+.1f}%)"
    return {
        "p5": p5, "p30": p30, "p60": p60,
        "eval_text": eval_text, "eval_class": eval_class,
        "missed_pct": missed_pct,
    }


def find_ideal_exit(price_series, lookback_days=30):
    """이상적 익절 시점: 데이터 기간 내 가격 고점들 + 그 후 -15% 이탈 시점."""
    closes = price_series
    if len(closes) < lookback_days: return None
    # 절대 고점
    peak_idx = closes.idxmax()
    peak_price = float(closes.max())
    peak_pos = list(closes.index).index(peak_idx)

    # 고점 이후 -15% 이탈 첫 시점
    after_peak = closes.iloc[peak_pos:]
    threshold = peak_price * 0.85
    breach = after_peak[after_peak <= threshold]
    if len(breach) == 0:
        return {"peak_date": peak_idx, "peak_price": peak_price, "exit_date": None, "exit_price": None}
    exit_date = breach.index[0]
    exit_price = float(breach.iloc[0])
    return {
        "peak_date": peak_idx, "peak_price": peak_price,
        "exit_date": exit_date, "exit_price": exit_price,
    }


def analyze_stock(stock_name, code, txs):
    """단일 종목 사후 분석."""
    s_trades = [t for t in txs if t["stock"] == stock_name and t["type"] in ("buy","sell")]
    if len(s_trades) < 2:
        return None

    s_trades.sort(key=lambda x: x["date"])
    first_buy = s_trades[0]["date"]
    start = first_buy.replace("-", "")
    if start < START_STR:
        start = START_STR

    pdf = fetch_price(code, start, TODAY_STR)
    if pdf is None or len(pdf) < 30:
        return None

    pdf_renamed = pdf.rename(columns={"시가":"open","고가":"high","저가":"low","종가":"close","거래량":"volume"})
    pdf_renamed = add_price_volume_signals(pdf_renamed)

    # 매수/매도 분리
    buys = [t for t in s_trades if t["type"]=="buy"]
    sells = [t for t in s_trades if t["type"]=="sell"]

    # 매도 평가
    sells_eval = []
    for sell in sells:
        ev = evaluate_sell(sell["date"], sell["price"], pdf["종가"])
        sells_eval.append({
            "date": sell["date"],
            "price": sell["price"],
            "qty": sell["qty"],
            "eval": ev,
        })

    # 이상적 익절 시점
    ideal = find_ideal_exit(pdf["종가"])

    # FIFO 평단가 (매수 합계)
    cost = sum(b["amount"] for b in buys) - sum(s["amount"] for s in sells)
    qty = sum(b["qty"] for b in buys) - sum(s["qty"] for s in sells)

    # OBV 분배 다이버전스 발생일
    obv_bear_dates = [pdf.index[i] for i in range(len(pdf_renamed))
                       if pdf_renamed["obv_diverg_bear"].iloc[i] == 1]

    # 통계
    n_too_early = sum(1 for s in sells_eval if s["eval"] and s["eval"]["missed_pct"] and s["eval"]["missed_pct"] > 10)
    n_well_timed = sum(1 for s in sells_eval if s["eval"] and s["eval"]["missed_pct"] and s["eval"]["missed_pct"] < -3)
    n_total_eval = sum(1 for s in sells_eval if s["eval"] and s["eval"]["missed_pct"] is not None)

    return {
        "stock": stock_name, "code": code,
        "buys": buys, "sells": sells, "sells_eval": sells_eval,
        "ideal": ideal,
        "first_buy": first_buy,
        "obv_bear_dates": obv_bear_dates,
        "pdf": pdf,
        "n_too_early": n_too_early,
        "n_well_timed": n_well_timed,
        "n_total_eval": n_total_eval,
    }


def render_stock_section(r, idx):
    pdf = r["pdf"]
    df_idx = pdf.index.tolist()
    closes = [clean(v) for v in pdf["종가"]]

    # 매수 마커
    buy_x = [b["date"] for b in r["buys"] if b["date"] in df_idx]
    buy_y = [float(b["price"]) for b in r["buys"] if b["date"] in df_idx]
    buy_hover = [f"매수 {b['date']}<br>{b['qty']:,}주 @ {b['price']:,.0f}원" for b in r["buys"] if b["date"] in df_idx]

    # 매도 마커
    sell_x = []; sell_y = []; sell_color = []; sell_hover = []
    for se in r["sells_eval"]:
        if se["date"] not in df_idx: continue
        sell_x.append(se["date"])
        sell_y.append(float(se["price"]))
        eval_data = se["eval"]
        if eval_data and eval_data["missed_pct"] is not None:
            mp = eval_data["missed_pct"]
            if mp > 10:
                sell_color.append("#ef4444")  # 너무 일찍
            elif mp > 3:
                sell_color.append("#f59e0b")
            elif mp < -3:
                sell_color.append("#10b981")  # 잘 판
            else:
                sell_color.append("#9ca3af")
        else:
            sell_color.append("#9ca3af")
        et = eval_data["eval_text"] if eval_data else "─"
        sell_hover.append(f"매도 {se['date']}<br>{se['qty']:,}주 @ {se['price']:,.0f}원<br>━━━<br>{et}")

    # 이상적 익절 시점
    ideal_marker_x = []; ideal_marker_y = []; ideal_hover = []
    if r["ideal"]:
        ip = r["ideal"]
        if ip["peak_date"] in df_idx:
            ideal_marker_x.append(ip["peak_date"])
            ideal_marker_y.append(ip["peak_price"])
            ideal_hover.append(f"💎 절대 고점<br>{ip['peak_date']}<br>{ip['peak_price']:,.0f}원")
        if ip["exit_date"] and ip["exit_date"] in df_idx:
            ideal_marker_x.append(ip["exit_date"])
            ideal_marker_y.append(ip["exit_price"])
            ideal_hover.append(f"🎯 이상적 익절 시점<br>{ip['exit_date']}<br>{ip['exit_price']:,.0f}원<br>(고점 -15%)")

    # OBV bear 마커
    obv_x = [d for d in r["obv_bear_dates"] if d in df_idx][:20]
    obv_y = [float(pdf["종가"].loc[d]) for d in obv_x]

    cd = json.dumps({
        "dates": df_idx, "close": closes,
        "buy_x": buy_x, "buy_y": buy_y, "buy_hover": buy_hover,
        "sell_x": sell_x, "sell_y": sell_y, "sell_color": sell_color, "sell_hover": sell_hover,
        "ideal_x": ideal_marker_x, "ideal_y": ideal_marker_y, "ideal_hover": ideal_hover,
        "obv_x": obv_x, "obv_y": obv_y,
    }, ensure_ascii=False)

    # 매도 평가 테이블
    eval_rows = ""
    for se in r["sells_eval"]:
        ev = se["eval"]
        if not ev:
            ev_text = "─"; ev_class = ""
        else:
            ev_text = ev["eval_text"]; ev_class = ev["eval_class"]
        p30 = f"{ev['p30']:,.0f}" if ev and ev["p30"] else "─"
        eval_rows += f"""<tr>
          <td class="mono">{se['date']}</td>
          <td class="mono" style="text-align:right">{se['qty']:,}</td>
          <td class="mono" style="text-align:right">{se['price']:,.0f}</td>
          <td class="mono" style="text-align:right">{p30}</td>
          <td class="{ev_class}" style="font-size:0.85em">{ev_text}</td>
        </tr>"""

    n_eval = r["n_total_eval"]
    too_early_pct = r["n_too_early"] / n_eval * 100 if n_eval else 0
    well_pct = r["n_well_timed"] / n_eval * 100 if n_eval else 0

    summary = f"""<div class="grid3" style="margin-bottom:14px">
      <div class="kpi"><div class="kpi-label">매수 횟수</div>
        <div class="kpi-value">{len(r['buys'])}회</div></div>
      <div class="kpi"><div class="kpi-label">매도 횟수</div>
        <div class="kpi-value">{len(r['sells'])}회</div></div>
      <div class="kpi"><div class="kpi-label">너무 일찍 판 매도</div>
        <div class="kpi-value" style="color:#ef4444">{r['n_too_early']}건<br><span style="font-size:0.6em">({too_early_pct:.0f}%)</span></div></div>
    </div>"""

    return f"""<div class="card" style="margin-bottom:18px">
      <h2 style="color:#4fc3f7">{r['stock']} <span style="color:#666;font-size:0.7em">({r['code']})</span></h2>
      {summary}
      <div id="chart_pm_{idx}" style="height:380px"></div>
      <h3 style="color:#aaa;margin-top:14px;margin-bottom:8px">매도 이력 평가</h3>
      <table class="table-compact">
        <tr>
          <th>매도일</th>
          <th style="text-align:right">수량</th>
          <th style="text-align:right">매도가</th>
          <th style="text-align:right">30일 후</th>
          <th>평가</th>
        </tr>
        {eval_rows}
      </table>
      <script>
      (function() {{
        const D = {cd};
        const BASE = {{
          paper_bgcolor:'#14171f', plot_bgcolor:'#14171f',
          font:{{color:'#bbb',size:11}},
          xaxis:{{gridcolor:'#1f2230'}}, yaxis:{{gridcolor:'#1f2230', title:'원'}},
          legend:{{orientation:'h',y:-0.18}},
          margin:{{t:10,b:55,l:65,r:10}},
        }};
        Plotly.newPlot('chart_pm_{idx}', [
          {{x:D.dates,y:D.close,type:'scatter',mode:'lines',name:'종가',line:{{color:'#4fc3f7',width:1.8}}}},
          {{x:D.buy_x,y:D.buy_y,type:'scatter',mode:'markers',name:'내 매수',
            marker:{{color:'#10b981',size:9,symbol:'triangle-up',line:{{color:'#fff',width:1}}}},
            hovertext:D.buy_hover,hoverinfo:'text'}},
          {{x:D.sell_x,y:D.sell_y,type:'scatter',mode:'markers',name:'내 매도',
            marker:{{color:D.sell_color,size:11,symbol:'triangle-down',line:{{color:'#fff',width:1.5}}}},
            hovertext:D.sell_hover,hoverinfo:'text'}},
          {{x:D.ideal_x,y:D.ideal_y,type:'scatter',mode:'markers+text',name:'이상적 익절',
            text: D.ideal_hover.map(h=>h.split('<br>')[0]),
            textposition:'top center',textfont:{{size:10,color:'#a78bfa'}},
            marker:{{color:'#a78bfa',size:14,symbol:'star',line:{{color:'#fff',width:1}}}},
            hovertext:D.ideal_hover,hoverinfo:'text'}},
          {{x:D.obv_x,y:D.obv_y,type:'scatter',mode:'markers',name:'OBV 분배',
            marker:{{color:'rgba(239,68,68,0.4)',size:6,symbol:'circle',line:{{width:0}}}},
            hovertext:D.obv_x.map(d=>'OBV 분배 다이버전스<br>'+d),hoverinfo:'text',visible:'legendonly'}},
        ], BASE, {{responsive:true}});
      }})();
      </script>
    </div>"""


def main():
    print("[1] 거래 데이터 로드")
    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    # 분석 대상 종목: 매도 5회 이상 + KOR
    by_stock = defaultdict(int)
    for t in txs:
        if t.get("type") == "sell":
            by_stock[t["stock"]] += 1

    target_stocks = [s for s, n in by_stock.items() if n >= 5]
    target_stocks = [s for s in target_stocks if smap.get(s, {}).get("nation") == "KOR"]
    # 매도 횟수 많은 순
    target_stocks.sort(key=lambda x: -by_stock[x])
    target_stocks = target_stocks[:20]
    print(f"  분석 대상: {len(target_stocks)}종목 (매도 5회+ KOR)")

    print("[2] 종목별 사후 분석")
    results = []
    for s in target_stocks:
        code = smap[s].get("code")
        if not code: continue
        r = analyze_stock(s, code, txs)
        if r:
            results.append(r)
            print(f"  ✓ {s:<14} 매수 {len(r['buys'])} / 매도 {len(r['sells'])} / 너무일찍 {r['n_too_early']}건")

    # 종합 통계
    total_too_early = sum(r["n_too_early"] for r in results)
    total_well = sum(r["n_well_timed"] for r in results)
    total_eval = sum(r["n_total_eval"] for r in results)

    sections_html = "".join(render_stock_section(r, i) for i, r in enumerate(results))

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>매매 사후 분석</title>
<link rel="stylesheet" href="assets/style.css">
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
.kpi-strip {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }}
.kpi-strip .kpi-mini {{ flex:1; min-width:170px; background:#181b23; border-radius:8px; padding:14px; text-align:center; }}
.kpi-strip .num {{ font-size:1.7em; font-weight:700; color:#fff; }}
.kpi-strip .lbl {{ font-size:0.78em; color:#888; margin-top:4px; }}
.grid3 {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; }}
.kpi {{ background:#1a1d26; border-radius:8px; padding:12px; text-align:center; }}
.kpi-label {{ font-size:0.78em; color:#888; margin-bottom:4px; }}
.kpi-value {{ font-size:1.3em; font-weight:700; color:#eee; }}
</style>
</head>
<body>
<div class="container">

<div class="nav">
  <a href="index.html">📊 전체 대시보드</a>
  <a href="profit_taking.html">💰 익절 타이밍</a>
  <a href="postmortem.html" class="active">🔍 매매 사후 분석</a>
  <a href="status.html">📋 현재 상황</a>
  <a href="trading_style.html">🎯 매매 스타일</a>
</div>

<h1>🔍 매매 사후 분석 (Postmortem)</h1>
<p class="subtitle">내 실제 매수/매도 vs 시그널 비교 · "너무 일찍 팔았는가, 잘 팔았는가" 자동 평가</p>

<div class="kpi-strip">
  <div class="kpi-mini">
    <div class="num">{len(results)}</div>
    <div class="lbl">분석 종목</div>
  </div>
  <div class="kpi-mini">
    <div class="num" style="color:#ef4444">{total_too_early}</div>
    <div class="lbl">너무 일찍 판 매도</div>
  </div>
  <div class="kpi-mini">
    <div class="num" style="color:#10b981">{total_well}</div>
    <div class="lbl">잘 판 매도</div>
  </div>
  <div class="kpi-mini">
    <div class="num">{total_too_early/total_eval*100:.0f}%</div>
    <div class="lbl">조기 매도 비율</div>
  </div>
</div>

<div class="card">
  <h2>분석 방법</h2>
  <div class="callout">
    <b>매도 평가 기준:</b><br>
    매도 후 30일 후 주가를 보고 판단:<br>
    • <b style="color:#ef4444">❌ 너무 일찍 판 매도</b>: 매도 후 30일에 +10% 이상 더 올랐음<br>
    • <b style="color:#f59e0b">△ 약간 일찍</b>: +3~10% 더 올랐음<br>
    • <b style="color:#9ca3af">= 비슷</b>: ±3%<br>
    • <b style="color:#10b981">○ 적절한 매도</b>: -3~10% 빠짐 (회피)<br>
    • <b style="color:#10b981">✓ 잘 판 매도</b>: -10% 이상 빠짐 (큰 회피)<br>
    <br>
    <b>차트 마커:</b><br>
    🟢 ▲ 내 매수 · 🔴 ▼ 너무 일찍 판 매도 · 🟢 ▼ 잘 판 매도 · 💎 절대 고점 · 🎯 이상적 익절 시점 (고점 -15%)
  </div>
  <p class="desc">
    "이상적 익절 시점"은 절대 고점에서 -15% 빠진 시점. 트레일링 스탑 룰 기준.<br>
    실제 매도 시점이 이상적 시점보다 빨랐는지/느렸는지 비교해서 매매 패턴 진단.
  </p>
</div>

{sections_html}

</div>
</body>
</html>"""

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[3] HTML 저장: {OUT}")


if __name__ == "__main__":
    main()
