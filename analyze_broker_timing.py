#!/usr/bin/env python3
"""대한광통신 + 파마리서치 거래원 매도 타이밍 정밀 분석.

사용자 daily_flow 38일치 데이터로:
  1. 거래원별 일별 매수/매도 시계열 구성
  2. "상승 주도자" 자동 식별 (5일 누적 매수 TOP 3)
  3. 주도자의 매수 → 매도 전환 추적 (= 매도 시그널)
  4. 외국계 vs 개미 비중 변화
  5. 가격과의 시간 정렬 → 시그널 발동일 vs 가격 고점 비교

출력: dashboard/broker_timing.html
"""
import os, sys, warnings, math, json
from datetime import datetime, timedelta
from collections import defaultdict
import unicodedata
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from signals.broker_flow import (
    load_stock_flow, FOREIGN, RETAIL_HEAVY, _broker_group, LARGE_INST_BROKERS
)
from pykrx import stock as krx
import pandas as pd

OUT = os.path.join(BASE_DIR, "dashboard", "broker_timing.html")

FLOW_DIR = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-srshin614@gmail.com"
    "/내 드라이브/01.Claude/01.주식/daily_flow"
)


def fmt_qty(n):
    if n is None: return "─"
    if abs(n) >= 1e6: return f"{n/1e6:+.2f}M"
    if abs(n) >= 1e3: return f"{n/1e3:+.1f}k"
    return f"{n:+,}"


def build_broker_timeseries(flow_data: dict) -> dict:
    """거래원별 일별 시계열 구성.
    반환: {broker: {date: net}, ...}
    """
    series = defaultdict(dict)
    broker_groups = {}

    for date in sorted(flow_data.keys()):
        for r in flow_data[date]:
            broker = r["broker"]
            series[broker][date] = r["net"]
            broker_groups[broker] = r["group"]

    return {"series": dict(series), "groups": broker_groups}


def find_lead_buyers(broker_ts: dict, window_days: int = 5):
    """상승 주도자 자동 식별.

    각 시점에서 직전 5일 누적 매수가 가장 큰 거래원 TOP 3.
    """
    series = broker_ts["series"]
    all_dates = sorted({d for s in series.values() for d in s})

    lead_buyers_by_date = {}  # {date: [(broker, 5d_net), ...]}

    for i, date in enumerate(all_dates):
        if i < window_days - 1:
            continue
        window = all_dates[i - window_days + 1: i + 1]

        broker_5d = {}
        for broker, dates in series.items():
            net_5d = sum(dates.get(d, 0) for d in window)
            if net_5d != 0:
                broker_5d[broker] = net_5d

        # TOP 3 매수 (양수 큰 순)
        top_buyers = sorted([(b, n) for b, n in broker_5d.items() if n > 0],
                             key=lambda x: -x[1])[:3]
        lead_buyers_by_date[date] = top_buyers

    return lead_buyers_by_date


def detect_reversal_signals(broker_ts: dict, lead_buyers_by_date: dict, window_days: int = 5):
    """주도자의 매수 → 매도 전환 시그널 탐지.

    시그널:
      A. 직전 N일 매수 TOP인 거래원이 다음 N일에 매도 전환
      B. TOP 3 모두 매도 전환 (강한 시그널)
    """
    series = broker_ts["series"]
    all_dates = sorted({d for s in series.values() for d in s})

    signals = []

    for i, date in enumerate(all_dates):
        if i < window_days * 2:
            continue
        # 직전 window의 주도자
        prev_window = all_dates[i - window_days * 2 + 1: i - window_days + 1]
        cur_window = all_dates[i - window_days + 1: i + 1]

        # 직전 window의 매수 주도자
        prev_5d = {}
        for broker, dates in series.items():
            n = sum(dates.get(d, 0) for d in prev_window)
            if n > 0:
                prev_5d[broker] = n

        prev_top3 = sorted(prev_5d.items(), key=lambda x: -x[1])[:3]
        if not prev_top3:
            continue

        # 현재 window에서 그들의 행동
        reversed_brokers = []
        held_brokers = []
        for broker, prev_net in prev_top3:
            cur_net = sum(series[broker].get(d, 0) for d in cur_window)
            if cur_net < 0:
                reversed_brokers.append((broker, prev_net, cur_net))
            elif cur_net > 0:
                held_brokers.append((broker, prev_net, cur_net))

        if reversed_brokers:
            n_reversed = len(reversed_brokers)
            score = n_reversed * 2 + (3 if n_reversed >= 2 else 0)
            signals.append({
                "date": date,
                "score": score,
                "n_reversed": n_reversed,
                "reversed_brokers": reversed_brokers,
                "held_brokers": held_brokers,
                "prev_window": [prev_window[0], prev_window[-1]],
                "cur_window": [cur_window[0], cur_window[-1]],
            })

    return signals


def analyze_retail_vs_smart(broker_ts: dict, window_days: int = 5):
    """개미 매수 vs 큰손 매도 패턴 추적."""
    series = broker_ts["series"]
    groups = broker_ts["groups"]
    all_dates = sorted({d for s in series.values() for d in s})

    out = []
    for i, date in enumerate(all_dates):
        if i < window_days - 1:
            continue
        window = all_dates[i - window_days + 1: i + 1]

        retail_net = 0
        large_inst_net = 0
        foreign_net = 0
        for broker, dates in series.items():
            net = sum(dates.get(d, 0) for d in window)
            if broker in RETAIL_HEAVY:
                retail_net += net
            if broker in LARGE_INST_BROKERS:
                large_inst_net += net
            if groups.get(broker) == "foreign":
                foreign_net += net

        out.append({
            "date": date,
            "retail_5d": retail_net,
            "large_inst_5d": large_inst_net,
            "foreign_5d": foreign_net,
        })

    return out


def get_user_trades(stock_name, txs, start_date, end_date):
    """사용자 매매 이력 (분석 기간 내)."""
    trades = sorted(
        [t for t in txs if t["stock"] == stock_name and t["type"] in ("buy", "sell")
         and start_date <= t["date"] <= end_date],
        key=lambda x: x["date"]
    )
    buys = [t for t in trades if t["type"] == "buy"]
    sells = [t for t in trades if t["type"] == "sell"]
    return buys, sells


def analyze_one_stock(stock_name, code, txs):
    """단일 종목 종합 분석."""
    flow = load_stock_flow(stock_name, FLOW_DIR)
    if not flow:
        print(f"  [SKIP] {stock_name} — daily_flow 없음")
        return None

    dates = sorted(flow.keys())
    start, end = dates[0], dates[-1]
    print(f"  데이터 기간: {start} ~ {end} ({len(dates)}일)")

    # 가격 데이터
    pdf = krx.get_market_ohlcv_by_date(start, end, code)
    pdf.index = pdf.index.strftime("%Y-%m-%d")

    # 거래원 시계열
    bts = build_broker_timeseries(flow)

    # 상승 주도자 시계열
    lead_buyers = find_lead_buyers(bts, window_days=5)

    # 매수→매도 전환 시그널
    reversal_signals = detect_reversal_signals(bts, lead_buyers, window_days=5)

    # 개미 vs 큰손
    retail_smart = analyze_retail_vs_smart(bts, window_days=5)

    # 고점 정보
    peak_date = pdf["종가"].idxmax()
    peak_price = float(pdf["종가"].max())

    # 사용자 매매
    user_buys, user_sells = get_user_trades(stock_name, txs, start, end)

    # 시그널 발동 vs 가격 고점 매핑
    for s in reversal_signals:
        sig_dt = datetime.strptime(s["date"][:8] if len(s["date"]) == 8 else s["date"], "%Y%m%d" if len(s["date"]) == 8 else "%Y-%m-%d")
        peak_dt = datetime.strptime(peak_date, "%Y-%m-%d")
        days_to_peak = (peak_dt - sig_dt).days
        s["days_to_peak"] = days_to_peak
        s["close"] = float(pdf["종가"].get(s["date"], 0))

    return {
        "stock": stock_name, "code": code,
        "dates": dates,
        "pdf": pdf,
        "broker_ts": bts,
        "lead_buyers": lead_buyers,
        "reversal_signals": reversal_signals,
        "retail_smart": retail_smart,
        "peak_date": peak_date, "peak_price": peak_price,
        "user_buys": user_buys, "user_sells": user_sells,
    }


def render_section(r, idx):
    pdf = r["pdf"]
    df_idx = pdf.index.tolist()
    closes = [float(v) for v in pdf["종가"]]

    # 사용자 매수/매도 마커
    buy_x = [b["date"] for b in r["user_buys"] if b["date"] in df_idx]
    buy_y = [float(b["price"]) for b in r["user_buys"] if b["date"] in df_idx]
    sell_x = [s["date"] for s in r["user_sells"] if s["date"] in df_idx]
    sell_y = [float(s["price"]) for s in r["user_sells"] if s["date"] in df_idx]

    # 시그널 마커
    sig_x = [s["date"] for s in r["reversal_signals"] if s["date"] in df_idx]
    sig_y = [s["close"] for s in r["reversal_signals"] if s["date"] in df_idx]
    sig_size = [10 + s["score"] for s in r["reversal_signals"] if s["date"] in df_idx]
    sig_hover = []
    for s in r["reversal_signals"]:
        if s["date"] not in df_idx: continue
        rev_str = "<br>".join(f"  {b[0]}: {fmt_qty(b[1])} → {fmt_qty(b[2])}"
                                for b in s["reversed_brokers"])
        held_str = "<br>".join(f"  {b[0]}: {fmt_qty(b[2])} (유지)"
                                 for b in s["held_brokers"][:3])
        sig_hover.append(
            f"<b>{s['date']} 매수→매도 전환 시그널 (점수 {s['score']})</b><br>"
            f"가격: {s['close']:,.0f}원<br>"
            f"고점({r['peak_date']})까지 {s['days_to_peak']}일<br>"
            f"━━━━━━━━━━━━<br>"
            f"<b style='color:#ef4444'>매도 전환 ({s['n_reversed']}명):</b><br>"
            f"{rev_str}<br>"
            f"<b style='color:#10b981'>매수 유지 ({len(s['held_brokers'])}명):</b><br>"
            f"{held_str}"
        )

    # 개미 vs 큰손 시계열
    rs_dates = [r["date"] for r in r["retail_smart"]]
    retail_5d = [r["retail_5d"] for r in r["retail_smart"]]
    large_inst_5d = [r["large_inst_5d"] for r in r["retail_smart"]]
    foreign_5d = [r["foreign_5d"] for r in r["retail_smart"]]

    cd = json.dumps({
        "dates": df_idx, "close": closes,
        "buy_x": buy_x, "buy_y": buy_y,
        "sell_x": sell_x, "sell_y": sell_y,
        "sig_x": sig_x, "sig_y": sig_y, "sig_size": sig_size, "sig_hover": sig_hover,
        "rs_dates": rs_dates, "retail_5d": retail_5d,
        "large_inst_5d": large_inst_5d, "foreign_5d": foreign_5d,
        "peak_date": r["peak_date"], "peak_price": r["peak_price"],
    }, ensure_ascii=False)

    # 시그널 테이블 (전환 정보)
    sig_rows = ""
    for s in r["reversal_signals"]:
        score_clr = "#ef4444" if s["score"] >= 5 else "#f59e0b" if s["score"] >= 3 else "#888"
        days_clr = "#10b981" if s["days_to_peak"] >= 7 else "#f59e0b" if s["days_to_peak"] >= 0 else "#888"
        rev_str = ", ".join(b[0] for b in s["reversed_brokers"])
        sig_rows += f"""<tr>
          <td class="mono">{s['date']}</td>
          <td class="mono" style="text-align:right">{s['close']:,.0f}</td>
          <td class="mono" style="text-align:center;color:{score_clr};font-weight:600">{s['score']}</td>
          <td class="mono" style="text-align:center;color:{days_clr}">{s['days_to_peak']:+}일</td>
          <td>{s['n_reversed']}명: {rev_str}</td>
        </tr>"""

    return f"""<div class="card" style="margin-bottom:20px">
      <h2 style="color:#4fc3f7">{r['stock']} <span style="color:#666;font-size:0.65em">({r['code']})</span></h2>
      <p class="desc">데이터 기간: {r['dates'][0]} ~ {r['dates'][-1]} ({len(r['dates'])}일)</p>
      <p class="desc">고점: <b>{r['peak_price']:,.0f}원</b> ({r['peak_date']}) · 매수→매도 전환 시그널 <b>{len(r['reversal_signals'])}건</b></p>

      <div id="chart_price_{idx}" style="height:380px"></div>

      <h3 style="color:#aaa;margin-top:18px">개미 vs 큰손 5일 누적 순매수</h3>
      <p class="desc">개미가 사고 큰손이 팔면 분배 진행 중. 외국계가 매도로 전환하면 강한 신호.</p>
      <div id="chart_flow_{idx}" style="height:280px"></div>

      <h3 style="color:#aaa;margin-top:18px">매수→매도 전환 시그널 타임라인</h3>
      <p class="desc">직전 5일 매수 주도 거래원 중 N명이 다음 5일에 매도로 전환.</p>
      <table class="table-compact">
        <tr>
          <th>날짜</th>
          <th style="text-align:right">가격</th>
          <th style="text-align:center">점수</th>
          <th style="text-align:center">고점까지</th>
          <th>매도 전환 거래원</th>
        </tr>
        {sig_rows}
      </table>

      <script>
      (function() {{
        const D = {cd};
        // 가격 차트
        Plotly.newPlot('chart_price_{idx}', [
          {{x:D.dates,y:D.close,type:'scatter',mode:'lines',name:'종가',line:{{color:'#4fc3f7',width:2}}}},
          {{x:[D.peak_date],y:[D.peak_price],type:'scatter',mode:'markers+text',name:'고점',
            text:['💎 고점'],textposition:'top center',textfont:{{size:11,color:'#fbbf24'}},
            marker:{{color:'#fbbf24',size:14,symbol:'star'}}}},
          {{x:D.buy_x,y:D.buy_y,type:'scatter',mode:'markers',name:'내 매수',
            marker:{{color:'rgba(16,185,129,0.7)',size:8,symbol:'triangle-up'}}}},
          {{x:D.sell_x,y:D.sell_y,type:'scatter',mode:'markers',name:'내 매도',
            marker:{{color:'rgba(239,68,68,0.85)',size:9,symbol:'triangle-down'}}}},
          {{x:D.sig_x,y:D.sig_y,type:'scatter',mode:'markers',name:'⚡ 매수→매도 전환',
            marker:{{color:'#a78bfa',size:D.sig_size,symbol:'diamond',line:{{color:'#fff',width:1.5}}}},
            hovertext:D.sig_hover,hoverinfo:'text',hoverlabel:{{bgcolor:'#14171f'}}}},
        ], {{
          paper_bgcolor:'#14171f', plot_bgcolor:'#14171f',
          font:{{color:'#bbb',size:11}},
          xaxis:{{gridcolor:'#1f2230'}}, yaxis:{{gridcolor:'#1f2230', title:'원'}},
          legend:{{orientation:'h',y:-0.13}},
          margin:{{t:10,b:55,l:75,r:10}},
        }}, {{responsive:true}});

        // 개미 vs 큰손
        Plotly.newPlot('chart_flow_{idx}', [
          {{x:D.rs_dates,y:D.retail_5d,type:'scatter',mode:'lines',name:'개미 5일',
            line:{{color:'#ef4444',width:1.8}}}},
          {{x:D.rs_dates,y:D.large_inst_5d,type:'scatter',mode:'lines',name:'대형기관 5일',
            line:{{color:'#3498db',width:1.8}}}},
          {{x:D.rs_dates,y:D.foreign_5d,type:'scatter',mode:'lines',name:'외국계 5일',
            line:{{color:'#10b981',width:1.8}}}},
        ], {{
          paper_bgcolor:'#14171f', plot_bgcolor:'#14171f',
          font:{{color:'#bbb',size:11}},
          xaxis:{{gridcolor:'#1f2230'}}, yaxis:{{gridcolor:'#1f2230', title:'순매수 (주)', zeroline:true, zerolinecolor:'#555'}},
          legend:{{orientation:'h',y:-0.18}},
          margin:{{t:10,b:55,l:65,r:10}},
        }}, {{responsive:true}});
      }})();
      </script>
    </div>"""


def main():
    print("="*80)
    print("  대한광통신 + 파마리서치 거래원 매도 타이밍 정밀 분석")
    print("="*80)

    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    targets = [("대한광통신", "010170"), ("파마리서치", "214450")]

    results = []
    for stock_name, code in targets:
        print(f"\n[{stock_name} ({code})]")
        r = analyze_one_stock(stock_name, code, txs)
        if r:
            results.append(r)
            print(f"  매수→매도 전환 시그널: {len(r['reversal_signals'])}건")
            print(f"  고점: {r['peak_date']} @ {r['peak_price']:,.0f}원")
            for s in r["reversal_signals"]:
                rev = ", ".join(b[0] for b in s["reversed_brokers"])
                print(f"    {s['date']} 점수{s['score']} ({s['days_to_peak']:+d}일) — {rev}")

    if not results:
        print("결과 없음")
        return

    sections = "".join(render_section(r, i) for i, r in enumerate(results))
    html = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>거래원 매도 타이밍 분석</title>
<link rel="stylesheet" href="assets/style.css">
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
</head><body>
<div class="container">

<div class="nav">
  <a href="index.html">📊 전체</a>
  <a href="broker_timing.html" class="active">🎯 거래원 매도 타이밍</a>
</div>

<h1>🎯 거래원 매도 타이밍 정밀 분석</h1>
<p class="subtitle">매수 주도자 → 매도 전환 추적 + 개미 vs 큰손 비교</p>

<div class="card">
  <div class="callout">
    <b>핵심 시그널: 매수 주도자 → 매도 전환</b><br>
    직전 5일 매수 TOP 3 거래원이 다음 5일에 매도로 전환했는가?<br>
    <br>
    <b>점수 산정:</b><br>
    1명 전환: +2점 (약한 경고)<br>
    2명 전환: +7점 (강한 경고)<br>
    3명 전환: +9점 (매우 강한 경고, 추세 끝났음)<br>
    <br>
    <b>해석:</b> 가격을 끌어올린 사람들이 빠지기 시작 = 추세 종료 임박
  </div>
</div>

{sections}

</div></body></html>"""

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ {OUT}")


if __name__ == "__main__":
    main()
