#!/usr/bin/env python3
"""파마리서치 8월 고점 (2025-08-26, 711,000원) 거래원 정밀 분석.

KIS API로 모든 거래원 일별 매매 시계열 수집 후:
  1. 매수 주도자 식별 (5일 누적 매수 TOP)
  2. 매도 전환 시점 추적
  3. 외국계 vs 개미 비중 변화
  4. 시그널 발동일 vs 가격 고점 매핑

출력: dashboard/pharma_aug_peak.html
"""
import os, sys, warnings, json
from datetime import datetime, timedelta
from collections import defaultdict
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from signals.kis_member_daily import (
    build_broker_mapping, fetch_all_brokers_daily,
    aggregate_to_dataframe, load_broker_names,
)
from file_io import load_json
from config import TRANSACTIONS_FILE
from pykrx import stock as krx
import pandas as pd

OUT = os.path.join(BASE_DIR, "dashboard", "pharma_aug_peak.html")

STOCK_CODE = "214450"
STOCK_NAME = "파마리서치"
ANALYSIS_START = "20250715"  # 고점 6주 전
ANALYSIS_END   = "20250930"   # 고점 5주 후


# 거래원 그룹 분류 (코드 기반은 정확하지 않으니 이름 기반)
def classify_broker_group(name: str) -> str:
    FOREIGN_KEYWORDS = ["JP모간", "모간스탠리", "골드만", "메릴린치", "UBS", "CLSA",
                          "씨티그룹", "BNP파리바", "노무라", "맥쿼리", "다이와", "외국계",
                          "홍콩상하이", "도이치"]
    RETAIL_KEYWORDS = ["키움", "토스", "카카오페이", "상상인"]
    LARGE_INST = ["NH투자", "KB증권", "한국증권", "한국투자", "삼성증권",
                   "한화", "미래에셋", "신한", "하나"]

    for kw in FOREIGN_KEYWORDS:
        if kw in name: return "foreign"
    for kw in RETAIL_KEYWORDS:
        if kw in name: return "retail"
    for kw in LARGE_INST:
        if kw in name: return "large_inst"
    return "small_inst"


def find_lead_buyers_with_reversal(df, peak_date: str, window: int = 5):
    """매수 주도자 + 매도 전환 패턴 찾기."""
    dates = sorted(df["date"].unique())
    signals = []

    for i, date in enumerate(dates):
        if i < window * 2:
            continue
        prev_dates = dates[i - window * 2 + 1: i - window + 1]
        cur_dates = dates[i - window + 1: i + 1]

        # 직전 N일 매수 TOP 3
        prev_df = df[df["date"].isin(prev_dates)]
        prev_5d = prev_df.groupby(["broker_code", "broker_name"])["net"].sum()
        prev_top3 = prev_5d.nlargest(3)

        # 현재 N일 그들의 행동
        cur_df = df[df["date"].isin(cur_dates)]
        reversed_b = []
        held_b = []
        for (code, name), prev_net in prev_top3.items():
            if prev_net <= 0: continue
            cur_net = cur_df[cur_df["broker_code"] == code]["net"].sum()
            if cur_net < 0:
                reversed_b.append((name, prev_net, cur_net, classify_broker_group(name)))
            else:
                held_b.append((name, prev_net, cur_net, classify_broker_group(name)))

        if reversed_b:
            n_rev = len(reversed_b)
            score = n_rev * 2 + (3 if n_rev >= 2 else 0)
            # 외국계 전환 가중치
            n_foreign_rev = sum(1 for r in reversed_b if r[3] == "foreign")
            if n_foreign_rev >= 1:
                score += 2

            peak_dt = datetime.strptime(peak_date, "%Y-%m-%d")
            sig_dt = datetime.strptime(date, "%Y-%m-%d")
            days_to_peak = (peak_dt - sig_dt).days

            signals.append({
                "date": date,
                "score": score,
                "n_reversed": n_rev,
                "n_foreign_rev": n_foreign_rev,
                "reversed": reversed_b,
                "held": held_b,
                "days_to_peak": days_to_peak,
            })
    return signals


def aggregate_by_group(df, window: int = 5):
    """그룹별 N일 누적 시계열."""
    df = df.copy()
    df["group"] = df["broker_name"].apply(classify_broker_group)

    by_group = df.groupby(["date", "group"])["net"].sum().reset_index()
    pivot = by_group.pivot(index="date", columns="group", values="net").fillna(0)

    for col in pivot.columns:
        pivot[f"{col}_5d"] = pivot[col].rolling(window).sum()

    return pivot


def main():
    print("="*80)
    print(f"  파마리서치 8월 고점 거래원 정밀 분석")
    print("="*80)

    # 1. 거래원 매핑 자동 구축
    print("\n[1] 거래원 코드↔이름 매핑 구축")
    sample_stocks = ["005930", "000660", "035420", "214450", "207940",
                       "005380", "068270", "035720", "010170", "327260"]
    mapping = build_broker_mapping(sample_stocks)
    print(f"    {len(mapping)}개 거래원 매핑 완료")
    for k, v in list(mapping.items())[:10]:
        print(f"      {k}: {v}")

    # 2. 모든 거래원 × 파마리서치 8월 데이터 수집
    print(f"\n[2] 파마리서치 ({ANALYSIS_START} ~ {ANALYSIS_END}) 모든 거래원 수집")
    print("    예상 시간: 약 25초 (99 거래원 호출)")

    results = fetch_all_brokers_daily(
        STOCK_CODE, ANALYSIS_START, ANALYSIS_END,
        min_vol=100, show_progress=True,
    )
    print(f"    활성 거래원: {len(results)}개")

    # 3. DataFrame 변환
    df = aggregate_to_dataframe(results, mapping)
    if df is None or df.empty:
        print("ERROR: 데이터 없음")
        return

    print(f"    총 데이터: {len(df)}행")
    print(f"    기간: {df['date'].min()} ~ {df['date'].max()}")

    # 4. 가격 데이터
    pdf = krx.get_market_ohlcv_by_date(ANALYSIS_START, ANALYSIS_END, STOCK_CODE)
    pdf.index = pdf.index.strftime("%Y-%m-%d")

    # 정렬을 위해 date 형식 통일
    df["date"] = df["date"].apply(lambda d: f"{d[:4]}-{d[4:6]}-{d[6:8]}")

    peak_date = pdf["종가"].idxmax()
    peak_price = float(pdf["종가"].max())
    print(f"\n[3] 고점: {peak_date} @ {peak_price:,.0f}원")

    # 5. 매수→매도 전환 시그널
    print(f"\n[4] 매수→매도 전환 시그널 분석")
    signals = find_lead_buyers_with_reversal(df, peak_date, window=5)
    print(f"    시그널 {len(signals)}건 발견")

    # 가장 강한 시그널 (점수 ≥7)
    strong = [s for s in signals if s["score"] >= 7]
    print(f"    강한 시그널 (점수≥7): {len(strong)}건")
    for s in strong[:10]:
        rev_names = ", ".join(r[0] for r in s["reversed"])
        marker = "★" if 0 <= s["days_to_peak"] <= 14 else " "
        print(f"      {marker} {s['date']} 점수{s['score']} ({s['days_to_peak']:+d}일) 외인전환:{s['n_foreign_rev']} — {rev_names}")

    # 6. 그룹별 5일 누적
    group_ts = aggregate_by_group(df, window=5)
    print(f"\n[5] 그룹별 5일 누적 (8월 고점 시기):")
    if peak_date in group_ts.index:
        peak_idx = list(group_ts.index).index(peak_date)
        sample_idx = max(0, peak_idx - 5)
        print(f"    고점 5일 전 ({group_ts.index[sample_idx]}):")
        for col in ["foreign_5d", "large_inst_5d", "retail_5d", "small_inst_5d"]:
            if col in group_ts.columns:
                v = group_ts[col].iloc[sample_idx]
                print(f"      {col}: {int(v):+,}")

    # 7. HTML 생성
    print(f"\n[6] HTML 생성")
    html = build_html(df, signals, group_ts, pdf, peak_date, peak_price)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"    ✓ {OUT}")


def build_html(df, signals, group_ts, pdf, peak_date, peak_price):
    df_idx = pdf.index.tolist()
    closes = [float(v) for v in pdf["종가"]]

    # 시그널 마커
    sig_x, sig_y, sig_size, sig_hover = [], [], [], []
    for s in signals:
        if s["score"] < 5: continue
        if s["date"] not in df_idx: continue
        idx_pos = df_idx.index(s["date"])
        sig_x.append(s["date"])
        sig_y.append(closes[idx_pos])
        sig_size.append(8 + s["score"])
        rev_str = "<br>".join(
            f"  {r[0]} ({r[3]}): {r[1]:+,} → {r[2]:+,}"
            for r in s["reversed"]
        )
        held_str = "<br>".join(
            f"  {r[0]}: {r[2]:+,} (유지)"
            for r in s["held"][:3]
        )
        sig_hover.append(
            f"<b>{s['date']} 매수→매도 전환 (점수 {s['score']})</b><br>"
            f"고점까지 {s['days_to_peak']:+d}일<br>"
            f"━━━━━━━━━━━━<br>"
            f"<b style='color:#ef4444'>매도 전환 ({s['n_reversed']}명, 외인:{s['n_foreign_rev']}):</b><br>"
            f"{rev_str}<br>"
            f"<b style='color:#10b981'>유지 ({len(s['held'])}명):</b><br>"
            f"{held_str}"
        )

    # 그룹별 시계열
    gts_dates = list(group_ts.index)
    foreign_5d = list(group_ts.get("foreign_5d", []))
    large_5d = list(group_ts.get("large_inst_5d", []))
    retail_5d = list(group_ts.get("retail_5d", []))
    small_5d = list(group_ts.get("small_inst_5d", []))

    # 사용자 매매
    txs = load_json(TRANSACTIONS_FILE, default=[])
    user_buys = [t for t in txs if t["stock"] == STOCK_NAME and t["type"] == "buy"
                  and ANALYSIS_START.replace("-", "") <= t["date"].replace("-", "") <= ANALYSIS_END.replace("-", "")]
    user_sells = [t for t in txs if t["stock"] == STOCK_NAME and t["type"] == "sell"
                   and ANALYSIS_START.replace("-", "") <= t["date"].replace("-", "") <= ANALYSIS_END.replace("-", "")]

    cd = json.dumps({
        "dates": df_idx, "close": closes,
        "sig_x": sig_x, "sig_y": sig_y, "sig_size": sig_size, "sig_hover": sig_hover,
        "gts_dates": gts_dates,
        "foreign_5d": foreign_5d, "large_5d": large_5d,
        "retail_5d": retail_5d, "small_5d": small_5d,
        "peak_date": peak_date, "peak_price": peak_price,
        "buy_x": [b["date"] for b in user_buys if b["date"] in df_idx],
        "buy_y": [float(b["price"]) for b in user_buys if b["date"] in df_idx],
        "sell_x": [s["date"] for s in user_sells if s["date"] in df_idx],
        "sell_y": [float(s["price"]) for s in user_sells if s["date"] in df_idx],
    }, ensure_ascii=False)

    # 시그널 테이블 (강한 것만)
    sig_rows = ""
    for s in sorted([s for s in signals if s["score"] >= 5],
                      key=lambda x: x["date"]):
        clr = "#ef4444" if s["score"] >= 9 else "#f59e0b" if s["score"] >= 7 else "#888"
        days_clr = "#10b981" if 0 <= s["days_to_peak"] <= 14 else "#f59e0b" if s["days_to_peak"] >= 0 else "#888"
        rev = ", ".join(f"{r[0]}({r[3][:3]})" for r in s["reversed"])
        sig_rows += f"""<tr>
          <td class="mono">{s['date']}</td>
          <td class="mono" style="text-align:center;color:{clr};font-weight:600">{s['score']}</td>
          <td class="mono" style="text-align:center;color:{days_clr}">{s['days_to_peak']:+d}일</td>
          <td>{rev}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>파마리서치 8월 고점 거래원 분석</title>
<link rel="stylesheet" href="assets/style.css">
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
</head><body>
<div class="container">
<div class="nav">
  <a href="index.html">📊 전체</a>
  <a href="broker_timing.html">1차 매수기 거래원</a>
  <a href="pharma_aug_peak.html" class="active">🎯 파마 8월 고점</a>
</div>

<h1>🎯 파마리서치 2025-08 고점 거래원 정밀 분석</h1>
<p class="subtitle">KIS API로 모든 거래원 × 일별 매매 시계열 수집 — daily_flow에 없던 8월 데이터 확보</p>

<div class="card">
  <div class="callout">
    <b>분석 방법:</b><br>
    1. KIS API <code>FHPST04540000</code>로 99개 거래원 코드 × {STOCK_NAME} 일별 매매 일괄 수집<br>
    2. 거래원 코드 ↔ 이름 매핑 자동 구축 (스냅샷 API 활용)<br>
    3. 5일 매수 주도자 → 다음 5일 매도 전환 시그널 탐지<br>
    4. 외국계 / 대형기관 / 개미 / 기타 그룹별 시계열<br>
    <br>
    <b>고점:</b> {peak_date} @ <b>{peak_price:,.0f}원</b>
  </div>
</div>

<div class="card">
  <h2>가격 + 사용자 매매 + 매도 전환 시그널</h2>
  <div id="chart_price" style="height:420px"></div>
</div>

<div class="card">
  <h2>그룹별 5일 누적 순매수</h2>
  <p class="desc">외국계가 매도 전환 → 대형기관 따라옴 → 마지막에 개미가 받는 패턴 추적</p>
  <div id="chart_groups" style="height:340px"></div>
</div>

<div class="card">
  <h2>매도 전환 시그널 시계열 (점수 ≥5)</h2>
  <table>
    <tr>
      <th>날짜</th>
      <th style="text-align:center">점수</th>
      <th style="text-align:center">고점까지</th>
      <th>매도 전환 거래원</th>
    </tr>
    {sig_rows}
  </table>
</div>

<script>
const D = {cd};
const BASE = {{
  paper_bgcolor:'#14171f', plot_bgcolor:'#14171f',
  font:{{color:'#bbb',size:11}},
  xaxis:{{gridcolor:'#1f2230'}}, yaxis:{{gridcolor:'#1f2230'}},
  legend:{{orientation:'h',y:-0.13}},
  margin:{{t:10,b:55,l:75,r:10}},
}};

Plotly.newPlot('chart_price', [
  {{x:D.dates,y:D.close,type:'scatter',mode:'lines',name:'종가',line:{{color:'#4fc3f7',width:2}}}},
  {{x:[D.peak_date],y:[D.peak_price],type:'scatter',mode:'markers+text',name:'💎 고점',
    text:[`💎 ${{D.peak_price.toLocaleString()}}원`],textposition:'top center',
    textfont:{{size:11,color:'#fbbf24'}},
    marker:{{color:'#fbbf24',size:14,symbol:'star'}}}},
  {{x:D.buy_x,y:D.buy_y,type:'scatter',mode:'markers',name:'내 매수',
    marker:{{color:'rgba(16,185,129,0.7)',size:8,symbol:'triangle-up'}}}},
  {{x:D.sell_x,y:D.sell_y,type:'scatter',mode:'markers',name:'내 매도',
    marker:{{color:'rgba(239,68,68,0.85)',size:9,symbol:'triangle-down'}}}},
  {{x:D.sig_x,y:D.sig_y,type:'scatter',mode:'markers',name:'⚡ 매수→매도 전환',
    marker:{{color:'#a78bfa',size:D.sig_size,symbol:'diamond',line:{{color:'#fff',width:1.5}}}},
    hovertext:D.sig_hover,hoverinfo:'text',hoverlabel:{{bgcolor:'#14171f'}}}},
], {{...BASE,yaxis:{{...BASE.yaxis,title:'원'}}}}, {{responsive:true}});

Plotly.newPlot('chart_groups', [
  {{x:D.gts_dates,y:D.foreign_5d,type:'scatter',mode:'lines',name:'🌐 외국계 5일',
    line:{{color:'#10b981',width:2}}}},
  {{x:D.gts_dates,y:D.large_5d,type:'scatter',mode:'lines',name:'🏛️ 대형기관 5일',
    line:{{color:'#3498db',width:2}}}},
  {{x:D.gts_dates,y:D.retail_5d,type:'scatter',mode:'lines',name:'👥 개미 5일',
    line:{{color:'#ef4444',width:2}}}},
  {{x:D.gts_dates,y:D.small_5d,type:'scatter',mode:'lines',name:'🏢 중소형 5일',
    line:{{color:'rgba(167,139,250,0.7)',width:1.2,dash:'dot'}}}},
], {{...BASE,
  yaxis:{{...BASE.yaxis,title:'5일 누적 순매수 (주)',zeroline:true,zerolinecolor:'#555'}},
}}, {{responsive:true}});
</script>

</div></body></html>"""


if __name__ == "__main__":
    main()
