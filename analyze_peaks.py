#!/usr/bin/env python3
"""고점 분석 — 누가 올렸고 언제 빠지기 시작했는가.

파마리서치(이미 빠진 케이스)에서 분배 패턴을 학습하고,
대한광통신(불안한 케이스)에 같은 분석 적용하여 비교.

분석 항목:
  1. 가격 고점 위치 (peak date)
  2. 상승 구간 매수 주도 거래원 TOP 10
  3. 하락 구간 매도 주도 거래원 TOP 10
  4. 분배 주도자 — 상승에서 사고 하락에서 판 거래원 (스마트한 사람들)
  5. 스마트머니 이탈 시점 vs 가격 고점 시점의 시차 (조기경보 윈도우)

Usage: python3 analyze_peaks.py
출력:   peak_analysis.html
"""
import os, sys, warnings, json, math, unicodedata
from collections import defaultdict
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from signals.broker_flow import (
    load_stock_flow, build_timeseries, FOREIGN, RETAIL_HEAVY, _broker_group,
)
from file_io import load_json
from config import STOCK_MAP_FILE

FLOW_DIR = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-srshin614@gmail.com"
    "/내 드라이브/01.Claude/01.주식/daily_flow"
)
OUT_HTML = os.path.join(BASE_DIR, "peak_analysis.html")

TARGETS = ["파마리서치", "대한광통신"]


def clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)): return None
    return v.item() if hasattr(v, "item") else v


def fetch_price(code, start, end):
    from pykrx import stock as krx
    df = krx.get_market_ohlcv_by_date(start, end, code)
    df.index = df.index.strftime("%Y-%m-%d")
    return df


def find_peak(price_series):
    """최고 종가 날짜와 가격 반환."""
    idxmax = price_series.idxmax()
    return idxmax, float(price_series.loc[idxmax])


def broker_net_by_phase(flow_data, peak_date, min_date):
    """상승/하락 구간별 거래원 누적 순매수.

    상승: min_date부터 peak_date까지 (포함)
    하락: peak_date 다음날부터 끝까지
    반환: (pre_dict, post_dict) - {(broker, group): net_qty}
    """
    pre  = defaultdict(int)  # {(broker, group): net}
    post = defaultdict(int)
    for date, rows in flow_data.items():
        for r in rows:
            key = (r["broker"], r["group"])
            if min_date <= date <= peak_date:
                pre[key] += r["net"]
            elif date > peak_date:
                post[key] += r["net"]
    return pre, post


def top_n(d, n=10, ascending=False):
    """net 기준 상위 N. ascending=False면 매수 큰 순, True면 매도 큰 순."""
    items = list(d.items())
    items.sort(key=lambda x: x[1], reverse=not ascending)
    return items[:n]


def find_smart_exit_date(df):
    """스마트머니 20일 비율이 처음으로 음수 되는 날 (양→음 첫 전환).

    실제 분배 시작점을 의미.
    """
    sr = df["smart_net_ratio_20d"]
    for i in range(1, len(sr)):
        if sr.iloc[i-1] is not None and sr.iloc[i] is not None:
            try:
                if float(sr.iloc[i-1]) > 0 and float(sr.iloc[i]) < 0:
                    return df.index[i].strftime("%Y-%m-%d")
            except Exception:
                continue
    return None


def find_divergence_dates(df):
    """다이버전스 발생일 모두."""
    if "divergence" not in df.columns: return []
    return [df.index[i].strftime("%Y-%m-%d") for i in range(len(df)) if bool(df["divergence"].iloc[i])]


def analyze_one(stock_name, stock_code):
    flow = load_stock_flow(stock_name, FLOW_DIR)
    if not flow:
        print(f"[SKIP] {stock_name} — 데이터 없음")
        return None

    dates = sorted(flow.keys())
    print(f"  데이터: {dates[0]} ~ {dates[-1]} ({len(flow)}일)")

    price_df = fetch_price(stock_code, dates[0], dates[-1])
    price_series = price_df["종가"]

    peak_date, peak_price = find_peak(price_series)
    min_date = dates[0]
    last_date = dates[-1]
    last_price = float(price_series.iloc[-1])

    print(f"  고점: {peak_date} @ {peak_price:,.0f}원")
    print(f"  현재: {last_date} @ {last_price:,.0f}원 ({(last_price/peak_price-1)*100:+.1f}%)")

    # 상승/하락 구간 거래원 분석
    pre, post = broker_net_by_phase(flow, peak_date, min_date)
    top_buyers_pre  = top_n(pre,  n=10, ascending=False)
    top_sellers_post = top_n(post, n=10, ascending=True)
    top_sellers_pre = top_n(pre,  n=10, ascending=True)  # 상승 중 판 사람 (역추세)

    # 분배 주도자: 상승에서 산 사람 + 하락에서 판 사람의 교집합
    distributors = []
    pre_buyers_set = {k for k, v in pre.items() if v > 0}
    for k, post_net in post.items():
        if post_net < 0 and k in pre_buyers_set:
            pre_net = pre[k]
            distributors.append({
                "broker": k[0], "group": k[1],
                "pre_buy": pre_net, "post_sell": post_net,
                "round_trip": pre_net + post_net,  # 음수면 누적 매도 우세
            })
    distributors.sort(key=lambda x: x["post_sell"])  # 가장 많이 판 순
    distributors = distributors[:10]

    # 추세 분석용
    df = build_timeseries(flow, price_series)
    smart_exit = find_smart_exit_date(df)
    divergences = find_divergence_dates(df)

    # 조기 경보 윈도우: 스마트머니 이탈 → 가격 고점까지 며칠
    warning_days = None
    if smart_exit and smart_exit < peak_date:
        from datetime import datetime
        d_exit = datetime.strptime(smart_exit, "%Y-%m-%d")
        d_peak = datetime.strptime(peak_date, "%Y-%m-%d")
        warning_days = (d_peak - d_exit).days

    # 금액 환산
    for g in ("foreign","inst","retail","smart_net"):
        df[f"{g}_amt"] = (df[g] * df["close"] / 1e8).round(2)
    for g in ("foreign","inst","smart_net"):
        df[f"{g}_amt_20d"] = df[f"{g}_amt"].rolling(20).sum().round(2)

    print(f"  스마트머니 첫 음전환: {smart_exit}")
    print(f"  다이버전스 발생: {len(divergences)}건")
    if warning_days is not None:
        print(f"  ★ 조기경보 윈도우: 스마트머니 이탈 후 {warning_days}일 만에 가격 고점")
    print(f"  분배 주도자 TOP 5: {[d['broker'] for d in distributors[:5]]}")

    return {
        "name": stock_name, "code": stock_code,
        "df": df, "flow": flow,
        "peak_date": peak_date, "peak_price": peak_price,
        "last_date": last_date, "last_price": last_price,
        "min_date": min_date,
        "drop_from_peak": (last_price / peak_price - 1) * 100,
        "top_buyers_pre":   top_buyers_pre,
        "top_sellers_post": top_sellers_post,
        "top_sellers_pre":  top_sellers_pre,
        "distributors": distributors,
        "smart_exit": smart_exit,
        "warning_days": warning_days,
        "divergences": divergences,
    }


def chart_data(r):
    df = r["df"]
    df_idx = df.index.strftime("%Y-%m-%d").tolist()
    def S(c): return [clean(v) for v in df[c]] if c in df.columns else [None]*len(df)

    # 마커: 고점 / 스마트머니 이탈 / 다이버전스 시작점
    markers = []
    if r["peak_date"] in df_idx:
        i = df_idx.index(r["peak_date"])
        markers.append({"date": r["peak_date"], "price": clean(df["close"].iloc[i]),
                        "label": f"가격 고점<br>{r['peak_price']:,.0f}원",
                        "color": "#e74c3c", "symbol": "triangle-down", "size": 18})
    if r["smart_exit"] and r["smart_exit"] in df_idx:
        i = df_idx.index(r["smart_exit"])
        markers.append({"date": r["smart_exit"], "price": clean(df["close"].iloc[i]),
                        "label": f"스마트머니 첫 음전환<br>{r['smart_exit']}",
                        "color": "#f39c12", "symbol": "diamond", "size": 14})
    for d in r["divergences"][:5]:  # 처음 5개만 표시
        if d in df_idx:
            i = df_idx.index(d)
            markers.append({"date": d, "price": clean(df["close"].iloc[i]),
                            "label": f"★ 다이버전스<br>{d}",
                            "color": "#ff6b6b", "symbol": "star", "size": 12})

    return {
        "dates": df_idx,
        "close": S("close"), "ma20": S("ma20"), "ma60": S("ma60"),
        "fr20": S("foreign_ratio_20d"), "ir20": S("inst_ratio_20d"),
        "sr20": S("smart_net_ratio_20d"),
        "near_high": S("near_high"),
        "markers": markers,
        "peak_date": r["peak_date"],
        "smart_exit": r["smart_exit"],
    }


def fmt_qty(n):
    if abs(n) >= 1e6: return f"{n/1e6:+,.2f}M"
    if abs(n) >= 1e3: return f"{n/1e3:+,.1f}k"
    return f"{n:+,}"


def render_broker_table(items, label, color, top_label, group_label="그룹"):
    """거래원 순위 테이블 HTML 생성."""
    rows = ""
    for i, ((broker, group), net) in enumerate(items, 1):
        clr = "#2ecc71" if net > 0 else "#e74c3c"
        gclr = {"foreign":"#2ecc71","inst":"#3498db","retail":"#e74c3c"}.get(group, "#95a5a6")
        glabel = {"foreign":"외국계","inst":"기관","retail":"개인"}.get(group, "?")
        rows += f"""<tr>
          <td style="color:#666;text-align:center">{i}</td>
          <td>{broker}</td>
          <td style="color:{gclr};text-align:center;font-size:.85em">{glabel}</td>
          <td style="color:{clr};text-align:right;font-family:monospace;font-weight:600">{fmt_qty(net)}</td>
        </tr>"""
    return f"""<div class="broker-card">
      <div class="broker-card-title" style="color:{color}">{top_label}</div>
      <table class="broker-tbl">
        <tr><th style="width:30px">#</th><th>거래원</th><th>{group_label}</th><th style="text-align:right">순매수</th></tr>
        {rows}
      </table>
    </div>"""


def render_distributors_table(distributors):
    """분배 주도자 (상승에서 사서 하락에서 판 거래원)."""
    rows = ""
    for i, d in enumerate(distributors, 1):
        gclr = {"foreign":"#2ecc71","inst":"#3498db","retail":"#e74c3c"}.get(d["group"], "#95a5a6")
        glabel = {"foreign":"외국계","inst":"기관","retail":"개인"}.get(d["group"], "?")
        rows += f"""<tr>
          <td style="color:#666;text-align:center">{i}</td>
          <td>{d['broker']}</td>
          <td style="color:{gclr};text-align:center;font-size:.85em">{glabel}</td>
          <td style="color:#2ecc71;text-align:right;font-family:monospace">{fmt_qty(d['pre_buy'])}</td>
          <td style="color:#e74c3c;text-align:right;font-family:monospace;font-weight:600">{fmt_qty(d['post_sell'])}</td>
          <td style="color:{'#e74c3c' if d['round_trip']<0 else '#2ecc71'};text-align:right;font-family:monospace">{fmt_qty(d['round_trip'])}</td>
        </tr>"""
    return f"""<table class="broker-tbl">
      <tr><th style="width:30px">#</th><th>거래원</th><th>그룹</th>
        <th style="text-align:right">상승중<br>매수</th>
        <th style="text-align:right">하락중<br>매도</th>
        <th style="text-align:right">최종<br>순포지션</th></tr>
      {rows}
    </table>"""


def render_section(r):
    # 마커 카드
    drop_color = "#e74c3c" if r["drop_from_peak"] < -5 else "#f39c12" if r["drop_from_peak"] < 0 else "#2ecc71"
    warn = ""
    if r["warning_days"] is not None:
        warn = f"""<div class="warn-card">
          <div style="font-size:.85em;color:#888;margin-bottom:4px">조기경보 윈도우</div>
          <div style="font-size:1.6em;color:#f1c40f;font-weight:700">{r['warning_days']}일</div>
          <div style="font-size:.78em;color:#888;margin-top:4px">스마트머니 이탈({r['smart_exit']}) → 가격 고점({r['peak_date']})</div>
        </div>"""
    elif r["smart_exit"] is not None:
        warn = f"""<div class="warn-card" style="border-color:#e74c3c">
          <div style="font-size:.85em;color:#888;margin-bottom:4px">⚠️ 스마트머니가 늦게 빠짐</div>
          <div style="font-size:1.1em;color:#e74c3c;font-weight:700">고점 이후 음전환</div>
          <div style="font-size:.78em;color:#888;margin-top:4px">고점 {r['peak_date']} / 스마트머니 음전환 {r['smart_exit']}</div>
        </div>"""

    return f"""<div class="card">
      <h2 style="color:#4fc3f7">{r['name']} ({r['code']})</h2>
      <div class="info-row">
        <div class="info-card">
          <div class="info-label">가격 고점</div>
          <div class="info-value">{r['peak_price']:,.0f}원</div>
          <div class="info-sub">{r['peak_date']}</div>
        </div>
        <div class="info-card">
          <div class="info-label">현재 가격</div>
          <div class="info-value">{r['last_price']:,.0f}원</div>
          <div class="info-sub" style="color:{drop_color}">{r['drop_from_peak']:+.1f}% (고점 대비)</div>
        </div>
        <div class="info-card">
          <div class="info-label">다이버전스 발생</div>
          <div class="info-value">{len(r['divergences'])}건</div>
          <div class="info-sub">★ 마커로 차트 표시</div>
        </div>
        {warn}
      </div>
      <div id="chart_{r['code']}" style="height:400px"></div>

      <h3 style="color:#4fc3f7;margin-top:24px;margin-bottom:10px">상승 구간 ({r['min_date']} → {r['peak_date']}) 주도자</h3>
      <p class="desc">이 종목을 끌어올린 거래원 TOP 10. 외국계가 많으면 펀더멘털 매수, 개인이 많으면 테마 매수.</p>
      <div class="broker-grid">
        {render_broker_table(r['top_buyers_pre'], "buy", "#2ecc71", f"📈 상승 견인 (매수 TOP 10)")}
        {render_broker_table(r['top_sellers_pre'], "sell-pre", "#888", f"⚠️ 상승 중 매도 (역추세 매도자)")}
      </div>

      <h3 style="color:#4fc3f7;margin-top:24px;margin-bottom:10px">하락 구간 ({r['peak_date']} → {r['last_date']}) 주도자</h3>
      <p class="desc">고점 이후 매도 주도 거래원 TOP 10. 이들이 하락의 주범이자 가장 빠르게 빠진 사람들.</p>
      <div class="broker-grid">
        {render_broker_table(r['top_sellers_post'], "sell", "#e74c3c", f"📉 하락 주도 (매도 TOP 10)")}
      </div>

      <h3 style="color:#4fc3f7;margin-top:24px;margin-bottom:10px">★ 분배 주도자 — 상승에서 사고 하락에서 판 거래원</h3>
      <p class="desc">
        가장 영리한 매매 — 저점에서 매집했다가 고점에서 차익 실현.<br>
        이 거래원들이 외국계·기관에 많을수록 "큰손이 분배(distribution)했다"는 뚜렷한 증거입니다.
      </p>
      {render_distributors_table(r['distributors'])}
    </div>"""


def main():
    smap = load_json(STOCK_MAP_FILE, default={})

    results = []
    for name in TARGETS:
        info = smap.get(name, {})
        code = info.get("code")
        if not code:
            print(f"[SKIP] {name} — 코드 없음")
            continue
        print(f"\n=== {name} ({code}) ===")
        r = analyze_one(name, code)
        if r:
            results.append(r)

    if not results:
        print("ERROR: 분석 가능한 종목 없음")
        sys.exit(1)

    print(f"\n[HTML] 생성 중...")
    sections_html = "\n".join(render_section(r) for r in results)
    charts_data = {r["code"]: chart_data(r) for r in results}

    html = build_html(sections_html, charts_data, results)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  저장: {OUT_HTML}")


def build_html(sections_html, charts_data, results):
    cd = json.dumps(charts_data, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>고점 분석 — 누가 올렸고 언제 빠졌나</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:'Segoe UI',sans-serif; background:#0f1117; color:#dde; padding:20px; font-size:14px; }}
h1 {{ font-size:1.6em; color:#4fc3f7; margin-bottom:6px; }}
h2 {{ font-size:1.3em; margin-bottom:14px; }}
h3 {{ font-size:1.05em; }}
.subtitle {{ color:#667; font-size:.85em; margin-bottom:20px; }}
.card {{ background:#1a1d26; border-radius:12px; padding:22px; margin-bottom:20px; }}
.callout {{ background:#1a2332; border-left:3px solid #4fc3f7; padding:12px 16px; border-radius:6px;
            margin-bottom:18px; color:#bcd; font-size:.9em; line-height:1.7; }}
.desc {{ color:#889; font-size:.83em; line-height:1.7; margin-bottom:12px; }}
.info-row {{ display:flex; gap:12px; margin-bottom:14px; flex-wrap:wrap; }}
.info-card {{ background:#20232e; border-radius:10px; padding:14px 18px; flex:1; min-width:180px; }}
.info-card .info-label {{ font-size:.78em; color:#888; margin-bottom:6px; }}
.info-card .info-value {{ font-size:1.4em; font-weight:700; color:#eee; }}
.info-card .info-sub {{ font-size:.78em; color:#888; margin-top:4px; }}
.warn-card {{ background:#231a1a; border:2px solid #f1c40f; border-radius:10px; padding:14px 18px;
              flex:1; min-width:200px; }}
.broker-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:10px; }}
.broker-card {{ background:#20232e; border-radius:10px; padding:14px; }}
.broker-card-title {{ font-size:.95em; font-weight:600; margin-bottom:10px; }}
.broker-tbl {{ width:100%; border-collapse:collapse; font-size:.82em; }}
.broker-tbl th {{ background:transparent; padding:6px 8px; text-align:left; color:#777;
                font-weight:500; border-bottom:1px solid #2a2d3e; font-size:.85em; }}
.broker-tbl td {{ padding:6px 8px; border-bottom:1px solid #1e2130; }}
.broker-tbl tr:hover td {{ background:#252836; }}
@media (max-width: 900px) {{ .broker-grid {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>

<h1>고점 분석 — 누가 올렸고 언제 빠지기 시작했나</h1>
<p class="subtitle">파마리서치(이미 빠짐) → 패턴 학습 / 대한광통신(불안) → 같은 시그널 적용</p>

<div class="callout">
  <b>분석 프레임워크:</b><br>
  ① 가격 고점 = 데이터 기간 내 최고 종가<br>
  ② 상승 구간(시작→고점) 매수 TOP 10 = 이 종목을 끌어올린 사람들<br>
  ③ 하락 구간(고점→현재) 매도 TOP 10 = 가장 빨리 빠진 사람들<br>
  ④ <b>분배 주도자 = ② ∩ ③</b>: 상승에서 사고 하락에서 판 = 가장 영리한 매매<br>
  ⑤ <b>조기경보 윈도우</b> = 스마트머니가 처음 음전환된 날과 가격 고점의 차이.
  이게 클수록 "큰손이 먼저 빠진 후 가격이 따라 빠졌다"는 의미.
</div>

{sections_html}

<script>
const CHARTS = {cd};
const BASE = {{
  paper_bgcolor:'#1a1d26', plot_bgcolor:'#1a1d26',
  font:{{color:'#ccc',size:11}},
  xaxis:{{gridcolor:'#252836',zeroline:false}},
  yaxis:{{gridcolor:'#252836'}},
  legend:{{orientation:'h',y:-0.13}},
  margin:{{t:10,b:55,l:65,r:55}},
  hovermode:'x unified',
}};

Object.entries(CHARTS).forEach(([code, D]) => {{
  const traces = [
    {{x:D.dates,y:D.close,type:'scatter',mode:'lines',name:'종가',line:{{color:'#4fc3f7',width:2}}}},
    {{x:D.dates,y:D.ma20,type:'scatter',mode:'lines',name:'MA20',line:{{color:'rgba(241,196,15,0.7)',width:1.2}}}},
    {{x:D.dates,y:D.ma60,type:'scatter',mode:'lines',name:'MA60',line:{{color:'rgba(149,165,166,0.7)',width:1.2,dash:'dot'}}}},
    {{x:D.dates,y:D.sr20,type:'scatter',mode:'lines',name:'스마트머니 20일%',
      line:{{color:'#f1c40f',width:2}},yaxis:'y2'}},
  ];
  // 마커
  D.markers.forEach(m => {{
    traces.push({{
      x:[m.date], y:[m.price], type:'scatter', mode:'markers',
      name: m.label.replace('<br>',' '), showlegend: false,
      marker:{{color:m.color, size:m.size, symbol:m.symbol, line:{{color:'#fff',width:1.5}}}},
      hovertext:m.label, hoverinfo:'text',
    }});
  }});
  // 0선 (스마트머니)
  const shapes = [
    {{type:'line', x0:D.dates[0], x1:D.dates[D.dates.length-1], y0:0, y1:0,
      yref:'y2', line:{{color:'rgba(241,196,15,0.4)',width:1,dash:'dot'}}}}
  ];
  // 고점 수직선
  if (D.peak_date) {{
    shapes.push({{type:'line', x0:D.peak_date, x1:D.peak_date, y0:0, y1:1,
                  yref:'paper', line:{{color:'rgba(231,76,60,0.4)',width:1.5,dash:'dash'}}}});
  }}
  if (D.smart_exit) {{
    shapes.push({{type:'line', x0:D.smart_exit, x1:D.smart_exit, y0:0, y1:1,
                  yref:'paper', line:{{color:'rgba(243,156,18,0.4)',width:1.5,dash:'dash'}}}});
  }}

  Plotly.newPlot(`chart_${{code}}`, traces, {{...BASE,
    yaxis:{{...BASE.yaxis,title:'가격 (원)'}},
    yaxis2:{{title:'스마트머니 20일 (%)',overlaying:'y',side:'right',
            zeroline:true, zerolinecolor:'#888', gridcolor:'transparent'}},
    shapes:shapes,
  }}, {{responsive:true}});
}});
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
