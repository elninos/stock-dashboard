#!/usr/bin/env python3
"""다종목 고점 거래원 정밀 분석 — KIS API 일괄.

각 종목별:
  1. 1년 내 절대 고점 자동 식별
  2. 고점 ±2개월 거래원 시계열 KIS API로 수집
  3. 매수→매도 전환 시그널 + 그룹별 분석
  4. 통합 HTML 페이지 (탭 구조)

분석 대상:
  삼천당제약, 대한광통신, 오스코텍, 코오롱티슈진, 보로노이,
  셀트리온, 이오테크닉스, 파크시스템스, 토모큐브
  + 제이스택 (코드 확인 후 추가)

출력: dashboard/multi_peaks.html
"""
import os, sys, warnings, json, time
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
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from pykrx import stock as krx
import pandas as pd

OUT = os.path.join(BASE_DIR, "dashboard", "multi_peaks.html")

TARGETS = [
    "삼천당제약",
    "대한광통신",
    "오스코텍",
    "코오롱티슈진",
    "보로노이",
    "셀트리온",
    "이오테크닉스",
    "파크시스템스",
    "토모큐브",
]


def classify_broker_group(name: str) -> str:
    if not name: return "small_inst"
    FOREIGN = ["JP모간","모간","골드만","메릴린치","UBS","CLSA","씨티","BNP",
                 "노무라","맥쿼리","다이와","외국계","홍콩상하이","도이치"]
    RETAIL = ["키움","토스","카카오","상상인"]
    LARGE = ["NH투자","KB증권","한국증권","한국투자","삼성증권",
              "한화","미래에셋","신한","하나"]
    for kw in FOREIGN:
        if kw in name: return "foreign"
    for kw in RETAIL:
        if kw in name: return "retail"
    for kw in LARGE:
        if kw in name: return "large_inst"
    return "small_inst"


def find_lead_buyers_with_reversal(df, peak_date: str, window: int = 5):
    dates = sorted(df["date"].unique())
    signals = []
    for i, date in enumerate(dates):
        if i < window * 2: continue
        prev_dates = dates[i - window * 2 + 1: i - window + 1]
        cur_dates = dates[i - window + 1: i + 1]

        prev_df = df[df["date"].isin(prev_dates)]
        prev_5d = prev_df.groupby(["broker_code", "broker_name"])["net"].sum()
        prev_top3 = prev_5d.nlargest(3)

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
            n_foreign_rev = sum(1 for r in reversed_b if r[3] == "foreign")
            if n_foreign_rev >= 1:
                score += 2

            try:
                peak_dt = datetime.strptime(peak_date, "%Y-%m-%d")
                sig_dt = datetime.strptime(date.replace("-", ""), "%Y%m%d") if "-" not in date else datetime.strptime(date, "%Y-%m-%d")
                days_to_peak = (peak_dt - sig_dt).days
            except Exception:
                days_to_peak = 0

            signals.append({
                "date": date, "score": score,
                "n_reversed": n_rev, "n_foreign_rev": n_foreign_rev,
                "reversed": reversed_b, "held": held_b,
                "days_to_peak": days_to_peak,
            })
    return signals


def aggregate_by_group(df, window: int = 5):
    df = df.copy()
    df["group"] = df["broker_name"].apply(classify_broker_group)
    by_group = df.groupby(["date", "group"])["net"].sum().reset_index()
    pivot = by_group.pivot(index="date", columns="group", values="net").fillna(0)
    for col in list(pivot.columns):
        pivot[f"{col}_5d"] = pivot[col].rolling(window).sum()
    return pivot


def analyze_single_stock(stock_name, code, mapping, txs):
    """단일 종목 고점 분석."""
    print(f"\n[{stock_name} ({code})]")

    # 1. 1년 내 가격 데이터로 고점 찾기
    today = "20260424"
    year_ago = (datetime.strptime(today, "%Y%m%d") - timedelta(days=365)).strftime("%Y%m%d")

    try:
        pdf_full = krx.get_market_ohlcv_by_date(year_ago, today, code)
    except Exception as e:
        print(f"  [ERR] 가격 데이터: {e}")
        return None
    if len(pdf_full) < 30:
        print(f"  [SKIP] 데이터 부족")
        return None

    pdf_full.index = pdf_full.index.strftime("%Y-%m-%d")
    peak_date = pdf_full["종가"].idxmax()
    peak_price = float(pdf_full["종가"].max())
    print(f"  1년 내 고점: {peak_date} @ {peak_price:,.0f}원")

    # 2. 고점 ±2개월 거래원 데이터 수집
    peak_dt = datetime.strptime(peak_date, "%Y-%m-%d")
    start = (peak_dt - timedelta(days=60)).strftime("%Y%m%d")
    end = (peak_dt + timedelta(days=45)).strftime("%Y%m%d")

    print(f"  거래원 시계열 수집: {start} ~ {end}")
    t0 = time.time()
    results = fetch_all_brokers_daily(code, start, end, min_vol=100)
    elapsed = time.time() - t0
    print(f"  활성 거래원 {len(results)}개 ({elapsed:.0f}초)")

    if not results:
        return None

    df = aggregate_to_dataframe(results, mapping)
    if df is None or df.empty:
        return None

    # 날짜 형식 통일
    df["date"] = df["date"].apply(lambda d: f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d)

    # 가격 데이터 (분석 기간만)
    pdf = pdf_full[(pdf_full.index >= df["date"].min()) & (pdf_full.index <= df["date"].max())]

    # 3. 시그널 분석
    signals = find_lead_buyers_with_reversal(df, peak_date, window=5)
    strong = [s for s in signals if s["score"] >= 7]
    pre_peak_strong = [s for s in strong if 0 <= s["days_to_peak"] <= 21]

    print(f"  강한 시그널 {len(strong)}건, 사전(고점 21일 내) {len(pre_peak_strong)}건")
    for s in pre_peak_strong[:5]:
        rev = ", ".join(r[0] for r in s["reversed"])
        print(f"    {s['date']} 점수{s['score']} ({s['days_to_peak']:+d}일) — {rev}")

    # 4. 그룹별
    group_ts = aggregate_by_group(df, window=5)

    # 5. 사용자 매매
    start_iso = start[:4] + "-" + start[4:6] + "-" + start[6:8]
    end_iso = end[:4] + "-" + end[4:6] + "-" + end[6:8]
    user_trades = [t for t in txs if t["stock"] == stock_name and t["type"] in ("buy","sell")
                    and start_iso <= t["date"] <= end_iso]

    return {
        "stock": stock_name, "code": code,
        "df": df, "pdf": pdf,
        "peak_date": peak_date, "peak_price": peak_price,
        "signals": signals, "strong": strong, "pre_peak_strong": pre_peak_strong,
        "group_ts": group_ts,
        "user_buys": [t for t in user_trades if t["type"] == "buy"],
        "user_sells": [t for t in user_trades if t["type"] == "sell"],
        "n_brokers": len(results),
        "period": (start_iso, end_iso),
    }


def render_section(r, idx):
    pdf = r["pdf"]
    df_idx = pdf.index.tolist()
    closes = [float(v) for v in pdf["종가"]]

    sig_x, sig_y, sig_size, sig_hover = [], [], [], []
    for s in r["signals"]:
        if s["score"] < 5: continue
        if s["date"] not in df_idx: continue
        idx_pos = df_idx.index(s["date"])
        sig_x.append(s["date"])
        sig_y.append(closes[idx_pos])
        sig_size.append(8 + s["score"])
        rev_str = "<br>".join(
            f"  {r2[0]} ({r2[3]}): {r2[1]:+,} → {r2[2]:+,}"
            for r2 in s["reversed"]
        )
        sig_hover.append(
            f"<b>{s['date']} 매도전환 (점수 {s['score']})</b><br>"
            f"고점까지 {s['days_to_peak']:+d}일<br>"
            f"━━━━━━━<br>"
            f"<b style='color:#ef4444'>매도 전환 ({s['n_reversed']}명, 외인:{s['n_foreign_rev']}):</b><br>"
            f"{rev_str}"
        )

    gts = r["group_ts"]
    gts_dates = list(gts.index)
    foreign_5d = list(gts.get("foreign_5d", []))
    large_5d = list(gts.get("large_inst_5d", []))
    retail_5d = list(gts.get("retail_5d", []))
    small_5d = list(gts.get("small_inst_5d", []))

    cd = json.dumps({
        "dates": df_idx, "close": closes,
        "sig_x": sig_x, "sig_y": sig_y, "sig_size": sig_size, "sig_hover": sig_hover,
        "gts_dates": gts_dates, "foreign_5d": foreign_5d, "large_5d": large_5d,
        "retail_5d": retail_5d, "small_5d": small_5d,
        "peak_date": r["peak_date"], "peak_price": r["peak_price"],
        "buy_x": [b["date"] for b in r["user_buys"] if b["date"] in df_idx],
        "buy_y": [float(b["price"]) for b in r["user_buys"] if b["date"] in df_idx],
        "sell_x": [s["date"] for s in r["user_sells"] if s["date"] in df_idx],
        "sell_y": [float(s["price"]) for s in r["user_sells"] if s["date"] in df_idx],
    }, ensure_ascii=False)

    # 강한 시그널 테이블
    sig_rows = ""
    for s in sorted([s for s in r["signals"] if s["score"] >= 5], key=lambda x: x["date"]):
        clr = "#ef4444" if s["score"] >= 9 else "#f59e0b" if s["score"] >= 7 else "#888"
        days_clr = "#10b981" if 0 <= s["days_to_peak"] <= 14 else "#f59e0b" if s["days_to_peak"] >= 0 else "#888"
        rev = ", ".join(f"{r2[0]}({r2[3][:3]})" for r2 in s["reversed"])
        sig_rows += f"""<tr>
          <td class="mono">{s['date']}</td>
          <td class="mono" style="text-align:center;color:{clr};font-weight:600">{s['score']}</td>
          <td class="mono" style="text-align:center;color:{days_clr}">{s['days_to_peak']:+d}일</td>
          <td>{rev}</td>
        </tr>"""

    n_pre = len(r["pre_peak_strong"])
    earliest = max(r["pre_peak_strong"], key=lambda x: x["days_to_peak"]) if r["pre_peak_strong"] else None
    earliest_str = ""
    if earliest:
        earliest_str = f"<b style='color:#10b981'>{earliest['days_to_peak']}일 전</b> 점수 {earliest['score']}"
    else:
        earliest_str = "<span style='color:#888'>없음</span>"

    return f"""<div class="card" style="margin-bottom:20px">
      <h2 style="color:#4fc3f7">{r['stock']} <span style="color:#666;font-size:0.65em">({r['code']})</span></h2>
      <div class="grid3" style="margin-bottom:14px">
        <div class="kpi"><div class="kpi-label">고점</div><div class="kpi-value mono">{r['peak_price']:,.0f}원</div><div class="kpi-sub">{r['peak_date']}</div></div>
        <div class="kpi"><div class="kpi-label">활성 거래원</div><div class="kpi-value">{r['n_brokers']}개</div><div class="kpi-sub">분석 기간 {r['period'][0]} ~ {r['period'][1]}</div></div>
        <div class="kpi"><div class="kpi-label">사전 시그널</div><div class="kpi-value">{n_pre}건</div><div class="kpi-sub">가장 일찍: {earliest_str}</div></div>
      </div>

      <div id="chart_price_{idx}" style="height:340px"></div>
      <div id="chart_groups_{idx}" style="height:280px;margin-top:14px"></div>

      <h3 style="color:#aaa;margin-top:18px">매도 전환 시그널 (점수 ≥5)</h3>
      <table class="table-compact">
        <tr><th>날짜</th><th style="text-align:center">점수</th><th style="text-align:center">고점까지</th><th>매도 전환 거래원</th></tr>
        {sig_rows}
      </table>

      <script>
      (function() {{
        const D = {cd};
        const BASE = {{
          paper_bgcolor:'#14171f', plot_bgcolor:'#14171f',
          font:{{color:'#bbb',size:11}},
          xaxis:{{gridcolor:'#1f2230'}}, yaxis:{{gridcolor:'#1f2230'}},
          legend:{{orientation:'h',y:-0.13}},
          margin:{{t:10,b:55,l:75,r:10}},
        }};

        Plotly.newPlot('chart_price_{idx}', [
          {{x:D.dates,y:D.close,type:'scatter',mode:'lines',name:'종가',line:{{color:'#4fc3f7',width:2}}}},
          {{x:[D.peak_date],y:[D.peak_price],type:'scatter',mode:'markers+text',name:'💎 고점',
            text:[`💎 ${{D.peak_price.toLocaleString()}}`],textposition:'top center',
            textfont:{{size:11,color:'#fbbf24'}}, marker:{{color:'#fbbf24',size:14,symbol:'star'}}}},
          {{x:D.buy_x,y:D.buy_y,type:'scatter',mode:'markers',name:'내 매수',
            marker:{{color:'rgba(16,185,129,0.7)',size:8,symbol:'triangle-up'}}}},
          {{x:D.sell_x,y:D.sell_y,type:'scatter',mode:'markers',name:'내 매도',
            marker:{{color:'rgba(239,68,68,0.85)',size:9,symbol:'triangle-down'}}}},
          {{x:D.sig_x,y:D.sig_y,type:'scatter',mode:'markers',name:'⚡ 매도전환',
            marker:{{color:'#a78bfa',size:D.sig_size,symbol:'diamond',line:{{color:'#fff',width:1.5}}}},
            hovertext:D.sig_hover,hoverinfo:'text',hoverlabel:{{bgcolor:'#14171f'}}}},
        ], {{...BASE,yaxis:{{...BASE.yaxis,title:'원'}}}}, {{responsive:true}});

        Plotly.newPlot('chart_groups_{idx}', [
          {{x:D.gts_dates,y:D.foreign_5d,type:'scatter',mode:'lines',name:'🌐 외국계',line:{{color:'#10b981',width:2}}}},
          {{x:D.gts_dates,y:D.large_5d,type:'scatter',mode:'lines',name:'🏛️ 대형기관',line:{{color:'#3498db',width:2}}}},
          {{x:D.gts_dates,y:D.retail_5d,type:'scatter',mode:'lines',name:'👥 개미',line:{{color:'#ef4444',width:2}}}},
          {{x:D.gts_dates,y:D.small_5d,type:'scatter',mode:'lines',name:'🏢 중소형',line:{{color:'rgba(167,139,250,0.7)',width:1.2,dash:'dot'}}}},
        ], {{...BASE,yaxis:{{...BASE.yaxis,title:'5일 누적 (주)',zeroline:true,zerolinecolor:'#555'}}}}, {{responsive:true}});
      }})();
      </script>
    </div>"""


def main():
    print("="*80)
    print("  다종목 고점 거래원 정밀 분석")
    print("="*80)

    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    # 코드 매핑
    target_pairs = []
    for name in TARGETS:
        info = smap.get(name, {})
        if info.get("code"):
            target_pairs.append((name, info["code"]))
        else:
            print(f"  [SKIP] {name} — 코드 없음")

    # 거래원 매핑 갱신
    print("\n[거래원 매핑 갱신]")
    sample_stocks = ["005930", "000660", "035420", "214450", "207940",
                       "005380", "068270", "035720", "010170", "327260"] + \
                       [c for _, c in target_pairs]
    mapping = build_broker_mapping(sample_stocks)
    print(f"  {len(mapping)}개 거래원 매핑")

    results = []
    for stock_name, code in target_pairs:
        try:
            r = analyze_single_stock(stock_name, code, mapping, txs)
            if r:
                results.append(r)
        except Exception as e:
            print(f"  [ERR] {stock_name}: {e}")

    if not results:
        print("결과 없음")
        return

    print(f"\n총 {len(results)}종목 분석 완료")

    sections = "".join(render_section(r, i) for i, r in enumerate(results))
    html = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>다종목 고점 거래원 분석</title>
<link rel="stylesheet" href="assets/style.css">
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
.grid3 {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; }}
.kpi {{ background:#1a1d26; border-radius:8px; padding:12px; text-align:center; }}
.kpi-label {{ font-size:0.78em; color:#888; margin-bottom:4px; }}
.kpi-value {{ font-size:1.2em; font-weight:700; color:#eee; }}
.kpi-sub {{ font-size:0.78em; color:#888; margin-top:4px; }}
</style></head><body>
<div class="container">

<div class="nav">
  <a href="index.html">📊 전체</a>
  <a href="pharma_aug_peak.html">파마 8월</a>
  <a href="multi_peaks.html" class="active">🎯 다종목 고점</a>
</div>

<h1>🎯 다종목 고점 거래원 정밀 분석</h1>
<p class="subtitle">{len(results)}종목 — KIS API로 모든 거래원 시계열 수집 + 매도 전환 시그널</p>

<div class="card">
  <div class="callout">
    <b>분석 방법:</b><br>
    각 종목의 1년 내 절대 고점 자동 식별 → 고점 ±2개월 거래원 시계열 수집<br>
    → 매수 주도자(직전 5일 TOP 3)가 다음 5일에 매도 전환 시그널 탐지<br>
    → 외국계 / 대형기관 / 개미 그룹별 5일 누적 시각화<br>
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
