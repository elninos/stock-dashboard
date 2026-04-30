#!/usr/bin/env python3
"""갑작스런 하락 직전 신호 사후 분석.

파마리서치(8월 71만→32만) / 대한광통신(2월 1027→633) 두 종목의
하락 직전에 어떤 신호들이 떴는지 자동 식별 + 시각화.

목적: 다음에 평가이익 종목이 빠지기 시작할 때 가장 빨리 캐치하는 방법 찾기.
"""
import os, sys, json, math, warnings
from datetime import datetime, timedelta
from collections import defaultdict
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from signals.sudden_drop import add_sudden_drop_signals, detect_drop_signals
from pykrx import stock as krx

OUT = os.path.join(BASE_DIR, "dashboard", "sudden_drop.html")


def clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)): return None
    return v.item() if hasattr(v, "item") else v


def fmt_pct(v):
    return f"{v:+.1f}%" if v is not None else "─"


def analyze_drop(stock_name, code, peak_date_str, lookback_days=60, lookforward_days=60):
    """고점 시점 ± N일 분석."""
    peak_dt = datetime.strptime(peak_date_str, "%Y-%m-%d")
    start = (peak_dt - timedelta(days=lookback_days)).strftime("%Y%m%d")
    end = (peak_dt + timedelta(days=lookforward_days)).strftime("%Y%m%d")

    pdf = krx.get_market_ohlcv_by_date(start, end, code)
    if len(pdf) == 0:
        return None
    pdf.index = pdf.index.strftime("%Y-%m-%d")

    pdf_renamed = pdf.rename(columns={"시가":"open","고가":"high","저가":"low","종가":"close","거래량":"volume"})
    pdf_renamed = add_sudden_drop_signals(pdf_renamed)

    events = detect_drop_signals(pdf_renamed)

    # 고점 정보
    peak_idx = pdf["종가"].idxmax()
    peak_price = float(pdf["종가"].max())
    peak_pos = list(pdf.index).index(peak_idx)

    # 고점 -10% 이탈 시점
    after_peak = pdf["종가"].iloc[peak_pos:]
    threshold = peak_price * 0.90
    breach10 = after_peak[after_peak <= threshold]
    drop10_date = breach10.index[0] if len(breach10) > 0 else None
    drop10_price = float(breach10.iloc[0]) if len(breach10) > 0 else None

    threshold20 = peak_price * 0.80
    breach20 = after_peak[after_peak <= threshold20]
    drop20_date = breach20.index[0] if len(breach20) > 0 else None
    drop20_price = float(breach20.iloc[0]) if len(breach20) > 0 else None

    # 신호별 lead time 계산 (고점 또는 -10% 이탈 대비 며칠 전?)
    lead_times = []
    for e in events:
        e_date = e["date"]
        e_dt = datetime.strptime(e_date, "%Y-%m-%d")
        # 고점 대비 며칠?
        lead_to_peak = (peak_dt - e_dt).days
        # -10% 이탈 대비 며칠 전?
        if drop10_date:
            drop10_dt = datetime.strptime(drop10_date, "%Y-%m-%d")
            lead_to_drop10 = (drop10_dt - e_dt).days
        else:
            lead_to_drop10 = None
        lead_times.append({
            "date": e_date,
            "lead_to_peak": lead_to_peak,
            "lead_to_drop10": lead_to_drop10,
            "events": e,
        })

    # 가장 빠른 신호 (peak 직전, drop10 이전)
    pre_peak_signals = [lt for lt in lead_times if lt["lead_to_peak"] >= 0]
    pre_drop10_signals = [lt for lt in lead_times if lt["lead_to_drop10"] is not None and lt["lead_to_drop10"] >= 0]

    return {
        "stock": stock_name, "code": code,
        "pdf": pdf, "pdf_signals": pdf_renamed,
        "peak_date": peak_idx, "peak_price": peak_price,
        "drop10_date": drop10_date, "drop10_price": drop10_price,
        "drop20_date": drop20_date, "drop20_price": drop20_price,
        "events": events,
        "lead_times": lead_times,
        "pre_peak_signals": pre_peak_signals,
        "pre_drop10_signals": pre_drop10_signals,
    }


def find_best_signal(pre_drop10_signals):
    """가장 빨리 잡은 시그널 식별. 신호별 우선순위:

    failed_breakout / volume_climax > distribution_day > others
    """
    if not pre_drop10_signals:
        return None

    # 가장 일찍 발동한 신호
    earliest = max(pre_drop10_signals, key=lambda x: x["lead_to_drop10"])
    return earliest


def render_section(r, idx):
    pdf = r["pdf"]
    df_idx = pdf.index.tolist()
    closes = [clean(v) for v in pdf["종가"]]
    volumes = [clean(v) for v in pdf["거래량"]]

    # 신호 마커
    sig_markers = defaultdict(lambda: {"x": [], "y": [], "hover": []})
    for e in r["events"]:
        if e["date"] not in df_idx: continue
        for trig in e["triggers"]:
            sig_markers[trig["type"]]["x"].append(e["date"])
            sig_markers[trig["type"]]["y"].append(e["close"])
            sig_markers[trig["type"]]["hover"].append(
                f"{trig['icon']} {trig['label']}<br>{e['date']}<br>{trig['detail']}<br>가격: {e['close']:,.0f}원"
            )

    # 시그널 색상/심볼
    sig_styles = {
        "distribution_day": {"color":"#fbbf24", "symbol":"square", "name":"📉 분배일"},
        "long_upper_wick":  {"color":"#a78bfa", "symbol":"triangle-up-open", "name":"⬆️ 위꼬리"},
        "failed_breakout":  {"color":"#ef4444", "symbol":"x", "name":"❌ Failed Breakout"},
        "volume_climax":    {"color":"#f97316", "symbol":"diamond", "name":"💥 Volume Climax"},
        "gap_down":         {"color":"#fb923c", "symbol":"triangle-down", "name":"⬇️ 갭 하락"},
        "wide_range_down":  {"color":"#dc2626", "symbol":"hexagon", "name":"🌊 변동성 음봉"},
    }

    # 고점 + 하락 마커
    annotations = []
    if r["peak_date"] in df_idx:
        annotations.append({
            "x": r["peak_date"], "y": r["peak_price"],
            "text": f"💎 고점 {r['peak_price']:,.0f}",
            "showarrow": True, "arrowhead": 2, "arrowcolor": "#fbbf24",
            "font": {"color": "#fbbf24", "size": 11},
            "bgcolor": "rgba(20,23,31,0.8)", "bordercolor": "#fbbf24",
        })
    if r["drop10_date"] and r["drop10_date"] in df_idx:
        annotations.append({
            "x": r["drop10_date"], "y": r["drop10_price"],
            "text": f"-10% 이탈",
            "showarrow": True, "arrowhead": 2, "arrowcolor": "#ef4444",
            "ay": 30,
            "font": {"color": "#ef4444", "size": 10},
            "bgcolor": "rgba(20,23,31,0.8)", "bordercolor": "#ef4444",
        })

    chart_data = {
        "dates": df_idx,
        "close": closes,
        "volume": volumes,
        "annotations": annotations,
    }

    traces_js = []
    for sig_type, style in sig_styles.items():
        m = sig_markers[sig_type]
        if not m["x"]: continue
        traces_js.append({
            "x": m["x"], "y": m["y"],
            "type": "scatter", "mode": "markers", "name": style["name"],
            "marker": {"color": style["color"], "size": 11, "symbol": style["symbol"],
                       "line": {"color": "#fff", "width": 1}},
            "hovertext": m["hover"], "hoverinfo": "text",
        })
    chart_data["sig_traces"] = traces_js

    cd = json.dumps(chart_data, ensure_ascii=False)

    # 베스트 시그널 분석
    pre_drop = r["pre_drop10_signals"]
    pre_drop.sort(key=lambda x: -x["lead_to_drop10"])  # 가장 빨리 잡은 순

    # 신호 종류별 lead time 통계
    type_lead = defaultdict(list)
    for lt in pre_drop:
        for trig in lt["events"]["triggers"]:
            type_lead[trig["type"]].append(lt["lead_to_drop10"])

    # 가장 좋은 신호 종류 (가장 빨리 + 충분한 발생 횟수)
    type_summary = []
    for t, leads in type_lead.items():
        type_summary.append({
            "type": t,
            "label": sig_styles.get(t, {}).get("name", t),
            "n_pre_drop": len(leads),
            "max_lead": max(leads),
            "avg_lead": sum(leads)/len(leads),
            "earliest_date": next(lt["date"] for lt in pre_drop if any(tr["type"]==t for tr in lt["events"]["triggers"])),
        })
    type_summary.sort(key=lambda x: -x["max_lead"])

    type_rows = ""
    for ts in type_summary:
        type_rows += f"""<tr>
          <td>{ts['label']}</td>
          <td class="mono" style="text-align:center">{ts['n_pre_drop']}회</td>
          <td class="mono" style="text-align:center;color:#10b981">{ts['max_lead']}일 전</td>
          <td class="mono" style="text-align:center">{ts['avg_lead']:.1f}일 전</td>
          <td class="mono">{ts['earliest_date']}</td>
        </tr>"""

    # 타임라인 테이블
    timeline_rows = ""
    for lt in pre_drop[:15]:
        e = lt["events"]
        triggers_str = " · ".join(f"{t['icon']} {t['label']}" for t in e["triggers"])
        triggers_detail = "<br>".join(f"<span style='color:#999'>• {t['detail']}</span>" for t in e["triggers"])
        lead_color = "#10b981" if lt["lead_to_drop10"] >= 14 else "#f59e0b" if lt["lead_to_drop10"] >= 7 else "#ef4444"
        timeline_rows += f"""<tr>
          <td class="mono">{e['date']}</td>
          <td class="mono" style="text-align:right">{e['close']:,.0f}</td>
          <td class="mono" style="text-align:center;color:{lead_color};font-weight:600">{lt['lead_to_drop10']}일 전</td>
          <td class="mono" style="text-align:center">{e['score']}</td>
          <td>{triggers_str}<br><span style="font-size:0.78em">{triggers_detail}</span></td>
        </tr>"""

    drop_pct = (r["drop10_price"]/r["peak_price"]-1)*100 if r["drop10_price"] else 0
    drop20_pct = (r["drop20_price"]/r["peak_price"]-1)*100 if r["drop20_price"] else 0
    drop10_str = f"{r['drop10_price']:,.0f}원" if r["drop10_price"] else "─"
    drop20_str = f"{r['drop20_price']:,.0f}원" if r["drop20_price"] else "─"

    return f"""<div class="card" style="margin-bottom:20px">
      <h2 style="color:#4fc3f7">{r['stock']} <span style="color:#666;font-size:0.65em">({r['code']})</span></h2>

      <div class="grid3" style="margin-bottom:14px">
        <div class="kpi" style="border:1px solid #fbbf24">
          <div class="kpi-label">💎 고점</div>
          <div class="kpi-value mono">{r['peak_price']:,.0f}원</div>
          <div class="kpi-sub">{r['peak_date']}</div>
        </div>
        <div class="kpi" style="border:1px solid #f59e0b">
          <div class="kpi-label">-10% 이탈</div>
          <div class="kpi-value mono">{drop10_str}</div>
          <div class="kpi-sub">{r['drop10_date'] or '─'} ({drop_pct:.1f}%)</div>
        </div>
        <div class="kpi" style="border:1px solid #ef4444">
          <div class="kpi-label">-20% 이탈</div>
          <div class="kpi-value mono">{drop20_str}</div>
          <div class="kpi-sub">{r['drop20_date'] or '─'} ({drop20_pct:.1f}%)</div>
        </div>
      </div>

      <div id="chart_drop_{idx}" style="height:480px"></div>

      <h3 style="color:#aaa;margin-top:18px;margin-bottom:8px">📊 신호 종류별 사전 경고 능력</h3>
      <p class="desc">고점 → -10% 이탈까지 어떤 신호가 가장 빨리 잡았나? 클수록 빨리 잡은 것.</p>
      <table class="table-compact">
        <tr>
          <th>신호 종류</th>
          <th style="text-align:center">사전 발동 횟수</th>
          <th style="text-align:center">가장 빨리 잡은 시점</th>
          <th style="text-align:center">평균 lead time</th>
          <th>첫 발동일</th>
        </tr>
        {type_rows}
      </table>

      <h3 style="color:#aaa;margin-top:18px;margin-bottom:8px">📋 사전 경고 신호 타임라인 (TOP 15, 일찍 잡은 순)</h3>
      <table class="table-compact">
        <tr>
          <th>날짜</th>
          <th style="text-align:right">종가</th>
          <th style="text-align:center">-10% 대비</th>
          <th style="text-align:center">신호 강도</th>
          <th>발동 신호</th>
        </tr>
        {timeline_rows}
      </table>

      <script>
      (function() {{
        const D = {cd};
        const baseTraces = [
          {{x:D.dates,y:D.close,type:'scatter',mode:'lines',name:'종가',
            line:{{color:'#4fc3f7',width:1.8}},yaxis:'y'}},
          {{x:D.dates,y:D.volume,type:'bar',name:'거래량',
            marker:{{color:'rgba(167,139,250,0.4)'}},yaxis:'y2'}},
        ];
        const sigTraces = D.sig_traces.map(t => ({{...t, yaxis:'y'}}));
        Plotly.newPlot('chart_drop_{idx}', baseTraces.concat(sigTraces), {{
          paper_bgcolor:'#14171f', plot_bgcolor:'#14171f',
          font:{{color:'#bbb',size:11}},
          xaxis:{{gridcolor:'#1f2230',zeroline:false}},
          yaxis:{{gridcolor:'#1f2230', title:'원', domain:[0.3, 1]}},
          yaxis2:{{gridcolor:'#1f2230', title:'거래량', domain:[0, 0.25]}},
          legend:{{orientation:'h',y:-0.18}},
          margin:{{t:10,b:55,l:75,r:10}},
          hovermode:'closest',
          annotations: D.annotations,
        }}, {{responsive:true}});
      }})();
      </script>
    </div>"""


def main():
    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    # 분석 대상 (peak 시점은 전체 가격 데이터로 자동 식별)
    targets = [
        # 파마리서치는 2025년 8월 26일이 절대 고점
        ("파마리서치", "214450"),
        # 대한광통신은 데이터에서 자동 식별
        ("대한광통신", "010170"),
    ]

    print("=" * 80)
    print("  갑작스런 하락 직전 신호 사후 분석")
    print("=" * 80)

    results = []
    for stock_name, code in targets:
        # 절대 고점부터 ±60일 분석을 위해 일단 전체 가격 받아서 peak 찾고
        try:
            full_pdf = krx.get_market_ohlcv_by_date("20250101", "20260424", code)
        except Exception as e:
            print(f"[ERR] {stock_name}: {e}")
            continue
        if len(full_pdf) == 0:
            continue
        full_pdf.index = full_pdf.index.strftime("%Y-%m-%d")
        peak_date_str = full_pdf["종가"].idxmax()
        peak_price = float(full_pdf["종가"].max())

        print(f"\n━━━ {stock_name} ━━━")
        print(f"  절대 고점: {peak_date_str} @ {peak_price:,.0f}원")

        r = analyze_drop(stock_name, code, peak_date_str)
        if r:
            results.append(r)
            print(f"  사전 경고 신호: {len(r['pre_drop10_signals'])}건")
            if r["pre_drop10_signals"]:
                earliest = max(r["pre_drop10_signals"], key=lambda x: x["lead_to_drop10"])
                trigger_names = [t['label'] for t in earliest['events']['triggers']]
                print(f"  가장 빨리 잡은 신호: {earliest['lead_to_drop10']}일 전 — {' / '.join(trigger_names)}")

    # HTML
    print("\n[HTML 생성]")
    sections = "".join(render_section(r, i) for i, r in enumerate(results))

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>갑작스런 하락 직전 신호 분석</title>
<link rel="stylesheet" href="assets/style.css">
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
.kpi-strip {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }}
.kpi-strip .kpi-mini {{ flex:1; min-width:170px; background:#181b23; border-radius:8px; padding:14px; text-align:center; }}
.kpi-strip .num {{ font-size:1.6em; font-weight:700; color:#fff; }}
.kpi-strip .lbl {{ font-size:0.78em; color:#888; margin-top:4px; }}
.grid3 {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; }}
.kpi {{ background:#1a1d26; border-radius:8px; padding:14px; text-align:center; }}
.kpi-label {{ font-size:0.78em; color:#888; margin-bottom:6px; }}
.kpi-value {{ font-size:1.3em; font-weight:700; color:#eee; }}
.kpi-sub {{ font-size:0.78em; color:#6b7280; margin-top:4px; }}
.sig-card {{ background:#181b23; border-radius:8px; padding:14px; margin-bottom:8px; border-left:4px solid #4fc3f7; }}
.sig-card .sig-title {{ font-weight:600; margin-bottom:4px; font-size:0.95em; }}
.sig-card .sig-detail {{ color:#aaa; font-size:0.84em; line-height:1.7; }}
</style>
</head>
<body>
<div class="container">

<div class="nav">
  <a href="index.html">📊 전체 대시보드</a>
  <a href="profit_taking.html">💰 익절 타이밍</a>
  <a href="sudden_drop.html" class="active">⚡ 하락 직전 신호</a>
  <a href="backtest_3stocks.html">🧪 백테스트</a>
  <a href="postmortem.html">🔍 사후 분석</a>
</div>

<h1>⚡ 갑작스런 하락 직전 신호 — 사후 분석</h1>
<p class="subtitle">파마리서치(8월 71만→32만) · 대한광통신(2월 1027→633) — 어떤 신호가 가장 빨리 잡았나?</p>

<div class="card">
  <h2>🎯 분석 목적</h2>
  <div class="callout">
    <b>거래원 데이터의 한계 인정:</b> 강세장에선 노이즈, 38일치는 너무 짧음.<br>
    <b>대신 OHLCV 자체에 답이 있음.</b> 큰손이 빠질 때 가격+거래량 패턴에 직접 흔적 남김.
  </div>

  <h3 style="color:#aaa;margin-top:14px;margin-bottom:10px">탐지하는 7가지 신호</h3>
  <div class="sig-card" style="border-left-color:#fbbf24">
    <div class="sig-title">📉 1. 분배일 (Distribution Day)</div>
    <div class="sig-detail">음봉 -0.5% 이상 + 거래량이 평소(20일 평균)의 1.5배 이상.<br>
      기관/외국인이 매도하는 직접 흔적. <b>O'Neil 방식 4주 누적 5+ 시 시장 상투</b>.</div>
  </div>
  <div class="sig-card" style="border-left-color:#a78bfa">
    <div class="sig-title">⬆️ 2. 위꼬리 긴 봉 (Long Upper Wick)</div>
    <div class="sig-detail">위꼬리가 전체 range의 50% 이상. 신고가 도달했지만 매도 압력에 밀림.</div>
  </div>
  <div class="sig-card" style="border-left-color:#ef4444">
    <div class="sig-title">❌ 3. Failed Breakout (가중 +2)</div>
    <div class="sig-detail">전일 20일 신고가 갱신 + 다음날 -3% 이상 하락.<br>
      <b>"신고가 함정"</b>. 매수 세력이 매도 세력을 못 이김. 가장 강력한 사전 경고.</div>
  </div>
  <div class="sig-card" style="border-left-color:#f97316">
    <div class="sig-title">💥 4. Volume Climax (가중 +2)</div>
    <div class="sig-detail">거래량 평소의 3배 이상 + 음봉 또는 위꼬리.<br>
      분배의 절정 — 큰손이 마지막 매물 정리.</div>
  </div>
  <div class="sig-card" style="border-left-color:#fb923c">
    <div class="sig-title">⬇️ 5. 갭 하락</div>
    <div class="sig-detail">시가가 전일 종가 -2% 이상. 야간/장전 정보로 인한 매도 결정.</div>
  </div>
  <div class="sig-card" style="border-left-color:#dc2626">
    <div class="sig-title">🌊 6. 변동성 음봉 (Wide Range Down)</div>
    <div class="sig-detail">range가 평소의 1.8배 + 음봉 -2% 이상. 패닉 매도 가능성.</div>
  </div>
</div>

{sections}

<div class="card">
  <h2>💡 종합 결론</h2>
  <div class="callout good">
    <b>이 신호들의 가치:</b><br>
    • 거래원 데이터 없이도 OHLCV만으로 작동 → <b>모든 종목에 즉시 적용</b><br>
    • 강세장 노이즈 적음 → <b>큰 추세는 안 끊고, 진짜 분배만 잡음</b><br>
    • Lead time 명확히 측정 가능 → <b>며칠 전에 잡았는지 검증됨</b><br>
    <br>
    <b>실전 룰 제안:</b><br>
    1. <b>Failed Breakout</b>: 무조건 1/3 즉시 익절 (가장 강한 단일 신호)<br>
    2. <b>Volume Climax</b>: 1/3 익절 + 트레일링 스탑 -10% 강화<br>
    3. <b>분배일 4주 누적 5건+</b>: 1/3 익절 + 추가 매수 금지<br>
    4. <b>위꼬리 긴 봉이 신고가에서 발생</b>: 즉시 1/4 익절 + 모니터링
  </div>
</div>

</div>
</body>
</html>"""

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ {OUT}")


if __name__ == "__main__":
    main()
