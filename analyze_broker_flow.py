#!/usr/bin/env python3
"""거래원 수급 분석 — 추세 국면 + 다이버전스 + 가격 컨펌 백테스트.

Usage:
  python3 analyze_broker_flow.py
출력: broker_flow_analysis.html
"""
import os, sys, warnings, json, math
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from signals.broker_flow import (
    load_stock_flow, build_timeseries, detect_signals, check_price_confirmation,
    FOREIGN_RATIO_20D, FOREIGN_RATIO_5D,
    INST_RATIO_20D, INST_RATIO_5D,
    RETAIL_RATIO_5D, SMART_RATIO_20D,
    NEAR_HIGH_PCT, DIVERGENCE_PCT,
)

FLOW_DIR = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-srshin614@gmail.com"
    "/내 드라이브/01.Claude/01.주식/daily_flow"
)
STOCK_NAME = "RF머트리얼즈"
STOCK_CODE  = "327260"
OUT_HTML    = os.path.join(BASE_DIR, "broker_flow_analysis.html")

REGIME_LABEL = {0: "중립", 1: "상승추세", 2: "분배의심", 3: "하락추세"}
REGIME_COLOR = {0: "rgba(149,165,166,0.06)", 1: "rgba(46,204,113,0.06)",
                2: "rgba(243,156,18,0.10)",  3: "rgba(231,76,60,0.10)"}


def clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)): return None
    return v.item() if hasattr(v, "item") else v


def fetch_price(code, start, end):
    from pykrx import stock as krx
    df = krx.get_market_ohlcv_by_date(start, end, code)
    df.index = df.index.strftime("%Y-%m-%d")
    return df


def backtest(signals, df_with_price):
    """각 시그널의 +5/+10/+20일 수익률 + 가격 컨펌 여부."""
    idx_list = df_with_price.index.strftime("%Y-%m-%d").tolist()
    closes   = df_with_price["close"].tolist()
    out = []
    for s in signals:
        d = s["date"]
        if d not in idx_list:
            out.append({**s, "ret5": None, "ret10": None, "ret20": None,
                        "confirmed": False, "days_to_confirm": None})
            continue
        i  = idx_list.index(d)
        p0 = closes[i]
        def ret(off):
            j = i + off
            if j >= len(closes) or p0 == 0 or p0 is None: return None
            return round((closes[j] / p0 - 1) * 100, 2)

        df_idx = df_with_price.copy()
        df_idx.index = df_with_price.index  # already datetime
        confirm = check_price_confirmation(df_idx, d)
        out.append({**s, "ret5": ret(5), "ret10": ret(10), "ret20": ret(20),
                    "confirmed": confirm["confirmed"],
                    "days_to_confirm": confirm["days_to_confirm"],
                    "confirm_reason": confirm["reason"]})
    return out


def summary_by_grade(bt, key="grade"):
    from collections import defaultdict
    s = defaultdict(lambda: {"count": 0, "hit10": 0, "avg10": [], "avg20": [],
                             "confirmed": 0})
    for r in bt:
        g = r[key]
        s[g]["count"] += 1
        if r.get("confirmed"): s[g]["confirmed"] += 1
        if r["ret10"] is not None:
            s[g]["hit10"] += 1 if r["ret10"] < 0 else 0
            s[g]["avg10"].append(r["ret10"])
        if r["ret20"] is not None:
            s[g]["avg20"].append(r["ret20"])
    out = {}
    for g, v in s.items():
        cnt = v["count"]
        out[g] = {
            "count":      cnt,
            "confirmed":  v["confirmed"],
            "confirm_rate": round(v["confirmed"] / cnt * 100) if cnt else 0,
            "hit_rate_10d": round(v["hit10"] / cnt * 100) if cnt else 0,
            "avg_ret_10d": round(sum(v["avg10"]) / len(v["avg10"]), 2) if v["avg10"] else None,
            "avg_ret_20d": round(sum(v["avg20"]) / len(v["avg20"]), 2) if v["avg20"] else None,
        }
    return out


def confirmed_only_summary(bt):
    """가격 컨펌된 시그널만 따로 적중률."""
    confirmed = [r for r in bt if r.get("confirmed")]
    if not confirmed:
        return {"count": 0, "hit_rate_10d": 0, "avg_ret_10d": None, "avg_ret_20d": None}
    cnt = len(confirmed)
    hit10 = sum(1 for r in confirmed if r["ret10"] is not None and r["ret10"] < 0)
    avg10 = [r["ret10"] for r in confirmed if r["ret10"] is not None]
    avg20 = [r["ret20"] for r in confirmed if r["ret20"] is not None]
    return {
        "count": cnt,
        "hit_rate_10d": round(hit10 / cnt * 100),
        "avg_ret_10d": round(sum(avg10) / len(avg10), 2) if avg10 else None,
        "avg_ret_20d": round(sum(avg20) / len(avg20), 2) if avg20 else None,
    }


def main():
    import pandas as pd

    print(f"[1] 수급 데이터 로드: {STOCK_NAME}")
    flow = load_stock_flow(STOCK_NAME, FLOW_DIR)
    if not flow:
        print("ERROR: daily_flow 데이터 없음"); sys.exit(1)

    # 가격 데이터 먼저 로드 (build_timeseries에 전달)
    dates_sorted = sorted(flow.keys())
    print(f"[2] 가격 데이터 로드: {dates_sorted[0]} ~ {dates_sorted[-1]}")
    price_df = fetch_price(STOCK_CODE, dates_sorted[0], dates_sorted[-1])
    print(f"    {len(price_df)}거래일")
    price_series = price_df["종가"]
    price_series.index = price_df.index  # YYYY-MM-DD str

    df = build_timeseries(flow, price_series)
    signals = detect_signals(df)
    print(f"[3] 시그널 탐지: {len(flow)}일치, 시그널 {len(signals)}건")

    # 백테스트
    bt = backtest(signals, df)
    by_grade        = summary_by_grade(bt, "grade")
    confirmed_stats = confirmed_only_summary(bt)

    print(f"[4] 백테스트 결과:")
    for g in ["매도강추", "매도주의", "관망"]:
        if g not in by_grade: continue
        v = by_grade[g]
        avg10 = f"{v['avg_ret_10d']:+.1f}%" if v["avg_ret_10d"] is not None else "─"
        avg20 = f"{v['avg_ret_20d']:+.1f}%" if v["avg_ret_20d"] is not None else "─"
        print(f"    [{g}] {v['count']}건 (컨펌 {v['confirmed']}건/{v['confirm_rate']}%)  "
              f"10일후하락:{v['hit_rate_10d']}%  평균수익률 10일:{avg10} 20일:{avg20}")
    if confirmed_stats["count"] > 0:
        avg10 = f"{confirmed_stats['avg_ret_10d']:+.1f}%" if confirmed_stats["avg_ret_10d"] is not None else "─"
        avg20 = f"{confirmed_stats['avg_ret_20d']:+.1f}%" if confirmed_stats["avg_ret_20d"] is not None else "─"
        print(f"    [컨펌만] {confirmed_stats['count']}건  10일후하락:{confirmed_stats['hit_rate_10d']}%  "
              f"평균수익률 10일:{avg10} 20일:{avg20}")

    # 금액 환산
    for g in ("foreign", "inst", "retail", "smart_net"):
        df[f"{g}_amt"] = (df[g] * df["close"] / 1e8).round(2)
    for g in ("foreign", "inst", "smart_net"):
        df[f"{g}_amt_20d"] = df[f"{g}_amt"].rolling(20).sum().round(2)

    # ── 차트 데이터
    df_idx = df.index.strftime("%Y-%m-%d").tolist()
    def S(c): return [clean(v) for v in df[c]]

    sig_x, sig_y, sig_hover, sig_color, sig_symbol, sig_size = [], [], [], [], [], []
    for s in bt:
        d = s["date"]
        if d not in df_idx: continue
        sig_x.append(d)
        sig_y.append(clean(df.loc[df.index[df_idx.index(d)], "close"]))
        # 색상/심볼: 컨펌 + 등급에 따라
        confirmed = s.get("confirmed")
        if s["grade"] == "매도강추":
            color = "#e74c3c"; sz = 16 if confirmed else 12
        elif s["grade"] == "매도주의":
            color = "#f39c12"; sz = 14 if confirmed else 11
        else:
            color = "#95a5a6"; sz = 11 if confirmed else 9
        symbol = "star" if s.get("divergence") else ("triangle-down" if confirmed else "circle-open")
        sig_color.append(color); sig_size.append(sz); sig_symbol.append(symbol)

        r5  = f"{s['ret5']:+.1f}%"  if s["ret5"]  is not None else "집계중"
        r10 = f"{s['ret10']:+.1f}%" if s["ret10"] is not None else "집계중"
        r20 = f"{s['ret20']:+.1f}%" if s["ret20"] is not None else "집계중"
        regime = REGIME_LABEL.get(s["regime"], "?")
        cf = s.get("confirm_reason") or "(미컨펌)"
        sig_hover.append(
            f"<b>{d}  {s['grade']} (점수:{s['score']})</b><br>"
            f"국면: {regime}  ·  신고가권: {s.get('near_high', 0)}%<br>"
            + ("<b style='color:#ff6b6b'>★ 베어리시 다이버전스</b><br>" if s.get("divergence") else "")
            + "──────────────<br>"
            + "<br>".join(s["reasons"])
            + f"<br>──────────────<br>가격컨펌: {cf}<br>"
            + f"+5일: {r5}  +10일: {r10}  +20일: {r20}"
        )

    # 추세 국면 배경 띠
    regime_shapes = []
    if "regime" in df.columns:
        # 연속된 같은 regime 구간을 모음
        prev_r, start = None, None
        rlist = df["regime"].tolist()
        for i, r in enumerate(rlist):
            r = int(r) if not (isinstance(r, float) and math.isnan(r)) else 0
            if r != prev_r:
                if prev_r is not None and prev_r != 0 and start is not None:
                    regime_shapes.append({
                        "type": "rect", "xref": "x", "yref": "paper",
                        "x0": df_idx[start], "x1": df_idx[i],
                        "y0": 0, "y1": 1,
                        "fillcolor": REGIME_COLOR[prev_r], "line": {"width": 0},
                        "layer": "below",
                    })
                start = i; prev_r = r
        # 마지막 구간
        if prev_r is not None and prev_r != 0 and start is not None:
            regime_shapes.append({
                "type": "rect", "xref": "x", "yref": "paper",
                "x0": df_idx[start], "x1": df_idx[-1],
                "y0": 0, "y1": 1,
                "fillcolor": REGIME_COLOR[prev_r], "line": {"width": 0},
                "layer": "below",
            })

    data = {
        "dates": df_idx,
        "close": S("close"), "ma20": S("ma20"), "ma60": S("ma60"),
        "high60": S("high60"),
        "fr20": S("foreign_ratio_20d"), "ir20": S("inst_ratio_20d"),
        "rr5":  S("retail_ratio_5d"),   "sr20": S("smart_net_ratio_20d"),
        "f_amt": S("foreign_amt"), "i_amt": S("inst_amt"), "r_amt": S("retail_amt"),
        "f_amt20": S("foreign_amt_20d"), "i_amt20": S("inst_amt_20d"),
        "sm_amt20": S("smart_net_amt_20d"),
        "near_high": S("near_high"),
        "regime_shapes": regime_shapes,
        "sig_x": sig_x, "sig_y": [clean(v) for v in sig_y],
        "sig_hover": sig_hover, "sig_color": sig_color,
        "sig_symbol": sig_symbol, "sig_size": sig_size,
    }
    thresholds = {
        "fr20": FOREIGN_RATIO_20D, "ir20": INST_RATIO_20D,
        "near_high": NEAR_HIGH_PCT, "divergence": DIVERGENCE_PCT,
    }

    # 시그널 테이블
    sig_rows = ""
    for s in reversed(bt[-80:]):
        clr = "#e74c3c" if s["score"] >= 5 else "#f39c12" if s["score"] >= 3 else "#95a5a6"
        regime = REGIME_LABEL.get(s["regime"], "?")
        regime_clr = {0:"#95a5a6",1:"#2ecc71",2:"#f39c12",3:"#e74c3c"}.get(s["regime"], "#888")
        def fmt(r):
            if r is None: return "─", ""
            return f"{r:+.1f}%", f"color:{'#2ecc71' if r<0 else '#e74c3c'}"
        r5,c5 = fmt(s["ret5"]); r10,c10 = fmt(s["ret10"]); r20,c20 = fmt(s["ret20"])
        confirm_badge = ""
        if s.get("confirmed"):
            confirm_badge = f'<span style="background:#e74c3c;color:#fff;padding:1px 6px;border-radius:3px;font-size:.75em">컨펌 {s["days_to_confirm"]}일</span>'
        diverg_badge = '<span style="color:#ff6b6b">★</span> ' if s.get("divergence") else ""

        sig_rows += f"""<tr>
          <td>{s['date']}</td>
          <td style="color:{clr};font-weight:bold">{diverg_badge}{s['grade']}</td>
          <td style="color:{clr};text-align:center">{s['score']}</td>
          <td style="color:{regime_clr};text-align:center;font-size:.85em">{regime}</td>
          <td style="text-align:center">{s.get('near_high', 0)}%</td>
          <td style="text-align:center">{confirm_badge}</td>
          <td style="font-size:.78em;color:#aaa">{'  /  '.join(s['reasons'][:2])}{'…' if len(s['reasons'])>2 else ''}</td>
          <td style="text-align:center;{c5}">{r5}</td>
          <td style="text-align:center;{c10}">{r10}</td>
          <td style="text-align:center;{c20}">{r20}</td>
        </tr>"""

    # 백테스트 카드
    bt_cards = ""
    for g in ["매도강추", "매도주의", "관망"]:
        if g not in by_grade: continue
        v = by_grade[g]
        cclr = "#e74c3c" if g == "매도강추" else "#f39c12" if g == "매도주의" else "#95a5a6"
        avg10 = f"{v['avg_ret_10d']:+.1f}%" if v["avg_ret_10d"] is not None else "─"
        avg20 = f"{v['avg_ret_20d']:+.1f}%" if v["avg_ret_20d"] is not None else "─"
        hit_c = "#2ecc71" if v["hit_rate_10d"] >= 60 else "#f39c12" if v["hit_rate_10d"] >= 40 else "#e74c3c"
        bt_cards += f"""<div class="bt-card">
          <div class="bt-grade" style="color:{cclr}">{g}</div>
          <div class="bt-count">{v['count']}건 · 컨펌 {v['confirmed']}건 ({v['confirm_rate']}%)</div>
          <div class="bt-stat">10일 후 하락률<br><b style="font-size:1.3em;color:{hit_c}">{v['hit_rate_10d']}%</b></div>
          <div class="bt-stat" style="margin-top:8px">평균수익률<br>10일 <b>{avg10}</b> · 20일 <b>{avg20}</b></div>
        </div>"""

    # 컨펌-only 카드
    if confirmed_stats["count"] > 0:
        avg10 = f"{confirmed_stats['avg_ret_10d']:+.1f}%" if confirmed_stats["avg_ret_10d"] is not None else "─"
        avg20 = f"{confirmed_stats['avg_ret_20d']:+.1f}%" if confirmed_stats["avg_ret_20d"] is not None else "─"
        hit_c = "#2ecc71" if confirmed_stats["hit_rate_10d"] >= 60 else "#f39c12" if confirmed_stats["hit_rate_10d"] >= 40 else "#e74c3c"
        bt_cards += f"""<div class="bt-card" style="border:2px solid #4fc3f7">
          <div class="bt-grade" style="color:#4fc3f7">⭐ 가격 컨펌만</div>
          <div class="bt-count">{confirmed_stats['count']}건</div>
          <div class="bt-stat">10일 후 하락률<br><b style="font-size:1.3em;color:{hit_c}">{confirmed_stats['hit_rate_10d']}%</b></div>
          <div class="bt-stat" style="margin-top:8px">평균수익률<br>10일 <b>{avg10}</b> · 20일 <b>{avg20}</b></div>
        </div>"""

    print("[5] HTML 생성")
    html = build_html(STOCK_NAME, STOCK_CODE, data, thresholds, sig_rows, bt_cards)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"    저장: {OUT_HTML}")


def build_html(name, code, data, thr, sig_rows, bt_cards):
    d = json.dumps(data, ensure_ascii=False)
    t = json.dumps(thr)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>{name} 거래원 수급 분석</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:'Segoe UI',sans-serif; background:#0f1117; color:#dde; padding:20px; font-size:14px; }}
h1 {{ font-size:1.5em; color:#4fc3f7; margin-bottom:4px; }}
.subtitle {{ color:#667; font-size:.85em; margin-bottom:20px; }}
.section-title {{ font-size:1.05em; color:#4fc3f7; margin-bottom:8px; font-weight:600; }}
.desc {{ color:#889; font-size:.82em; line-height:1.7; margin-bottom:10px; }}
.card {{ background:#1a1d26; border-radius:10px; padding:18px; margin-bottom:16px; }}
.grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px; }}
.regime-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-bottom:14px; }}
.r-item {{ background:#20232e; border-radius:8px; padding:12px; font-size:.82em; }}
.r-item .label {{ font-weight:600; margin-bottom:4px; }}
.r-item .body {{ color:#99a; line-height:1.55; font-size:.9em; }}
.bt-row {{ display:flex; gap:12px; margin-bottom:16px; flex-wrap:wrap; }}
.bt-card {{ background:#1a1d26; border-radius:10px; padding:16px; flex:1; min-width:200px; text-align:center; }}
.bt-grade {{ font-size:1.05em; font-weight:700; margin-bottom:6px; }}
.bt-count {{ color:#888; font-size:.78em; margin-bottom:10px; }}
.bt-stat {{ font-size:.85em; color:#bbb; line-height:1.7; }}
.bt-stat b {{ color:#eee; }}
table {{ width:100%; border-collapse:collapse; font-size:.78em; }}
th {{ background:#20232e; padding:8px 6px; text-align:left; color:#889; font-weight:500; white-space:nowrap; }}
td {{ padding:7px 6px; border-bottom:1px solid #1e2130; vertical-align:top; }}
tr:hover td {{ background:#1e2130; }}
.callout {{ background:#1a2332; border-left:3px solid #4fc3f7; padding:12px 14px; border-radius:6px;
            margin-bottom:14px; color:#bcd; font-size:.86em; line-height:1.7; }}
</style>
</head>
<body>
<h1>{name} ({code}) — 거래원 수급 분석</h1>
<p class="subtitle">추세 국면 분류 · 베어리시 다이버전스 · 가격 컨펌 백테스트</p>

<!-- 핵심 컨셉 -->
<div class="card">
  <div class="section-title">분석 프레임워크</div>
  <div class="callout">
    <b>두 가지 함정을 동시에 피하는 게 목표:</b><br>
    ① 오를 종목을 너무 일찍 팔지 않는다 (상승추세 중 차익실현은 무시)<br>
    ② 고점에서 빠지는 종목을 끝까지 들지 않는다 (분배 의심 + 가격 컨펌으로 진짜 매도 잡기)
  </div>
  <div class="regime-grid">
    <div class="r-item" style="border-left:3px solid #2ecc71">
      <div class="label" style="color:#2ecc71">① 상승추세</div>
      <div class="body">종가 > MA60, MA20 > MA60<br>→ <b>들고 있어라.</b> 외국계가 팔아도 단순 차익실현일 가능성. 시그널 가중치 낮춤.</div>
    </div>
    <div class="r-item" style="border-left:3px solid #f39c12">
      <div class="label" style="color:#f39c12">② 분배 의심 ⚠️</div>
      <div class="body">신고가권(≥{int(NEAR_HIGH_PCT*100)}%) + 스마트머니 음전환<br>→ <b>위험 구간.</b> 큰손이 몰래 빠져나오는 중. 시그널 가중치 강화.</div>
    </div>
    <div class="r-item" style="border-left:3px solid #e74c3c">
      <div class="label" style="color:#e74c3c">③ 하락추세</div>
      <div class="body">종가 < MA20 < MA60<br>→ <b>이미 늦었지만</b> 청산 시그널. 손절 컨펌으로 사용.</div>
    </div>
    <div class="r-item" style="border-left:3px solid #95a5a6">
      <div class="label" style="color:#95a5a6">④ 중립/횡보</div>
      <div class="body">위 조건 어디에도 안 들어감.<br>→ 표준 임계값 적용.</div>
    </div>
  </div>
  <div class="callout" style="border-left-color:#ff6b6b">
    <b>★ 다이버전스 (가장 강력한 시그널, 점수+3):</b><br>
    가격은 신고가권({int(DIVERGENCE_PCT*100)}%↑)인데 외국계+기관 20일이 음수, 또한 5일이 20일보다 더 음수(가속화).<br>
    = "주가는 버티고 있지만 큰손이 빠르게 빠져나오는 중" — 진짜 고점 90%를 잡는다고 알려진 패턴.
  </div>
  <div class="callout" style="border-left-color:#4fc3f7">
    <b>⭐ 가격 컨펌:</b><br>
    수급 시그널이 떠도 즉시 매도하지 않고, 그 후 10일 이내 종가가 <b>MA20 또는 직전 20일 저점을 이탈</b>할 때만 "컨펌"으로 분류.<br>
    이게 "오를 종목 너무 빨리 안 팔기"의 핵심 — 백테스트에서 컨펌만 모아 별도로 적중률 계산.
  </div>
</div>

<!-- 주가 + 시그널 + 국면 띠 -->
<div class="card">
  <div class="section-title">주가 + MA + 시그널 + 추세 국면</div>
  <p class="desc">
    배경 색상 = 추세 국면 (초록=상승, 주황=분배의심, 빨강=하락).<br>
    마커: ★ 다이버전스 · ▼ 가격 컨펌됨 · ○ 수급 시그널만 (미컨펌). 마우스 올리면 상세.
  </p>
  <div id="price_chart" style="height:420px"></div>
</div>

<!-- 비율 차트 -->
<div class="card">
  <div class="section-title">20일 수급 비율 (%) + 신고가 거리</div>
  <p class="desc">
    스마트머니(노란선)가 0선 아래로 내려가는 동시에 신고가 거리(파란선)가 90~100% 구간에 있으면 다이버전스 형성 중.
  </p>
  <div id="ratio_chart" style="height:340px"></div>
</div>

<!-- 일별 금액 -->
<div class="grid2">
  <div class="card">
    <div class="section-title">일별 그룹 순매수 (억 원)</div>
    <p class="desc">수량 × 종가 = 실제 거래 금액.</p>
    <div id="group_bar" style="height:280px"></div>
  </div>
  <div class="card">
    <div class="section-title">20일 롤링 스마트머니 (억 원)</div>
    <p class="desc">0선 하향 돌파 = 큰손 한 달 누적 매도 전환.</p>
    <div id="smart_chart" style="height:280px"></div>
  </div>
</div>

<!-- 백테스트 -->
<div class="card">
  <div class="section-title">백테스트 — 시그널 후 실제 주가 변화</div>
  <p class="desc">
    각 등급별 적중률 + 가격 컨펌만 따로 모은 결과.<br>
    <b>컨펌-only 카드의 적중률이 진짜 보고 싶은 값</b> — 수급 시그널이 떴고 가격까지 깨진 경우만.
  </p>
  <div class="bt-row">{bt_cards}</div>
</div>

<!-- 시그널 목록 -->
<div class="card">
  <div class="section-title">시그널 상세 목록 (최근 80건)</div>
  <table>
    <tr>
      <th>날짜</th><th>등급</th><th>점수</th><th>국면</th>
      <th style="text-align:center">신고가<br>거리</th>
      <th style="text-align:center">컨펌</th>
      <th>주요 이유</th>
      <th style="text-align:center">+5일</th>
      <th style="text-align:center">+10일</th>
      <th style="text-align:center">+20일</th>
    </tr>
    {sig_rows}
  </table>
</div>

<script>
const D = {d};
const THR = {t};

const BASE = {{
  paper_bgcolor:'#1a1d26', plot_bgcolor:'#1a1d26',
  font:{{color:'#ccc',size:12}},
  xaxis:{{gridcolor:'#252836',zeroline:false}},
  yaxis:{{gridcolor:'#252836'}},
  legend:{{orientation:'h',y:-0.15}},
  margin:{{t:10,b:55,l:65,r:10}},
  hovermode:'closest',
}};

// ① 주가 + 시그널 + 국면 배경
Plotly.newPlot('price_chart',[
  {{x:D.dates,y:D.close,type:'scatter',mode:'lines',name:'종가',line:{{color:'#4fc3f7',width:2}}}},
  {{x:D.dates,y:D.ma20, type:'scatter',mode:'lines',name:'MA20',line:{{color:'rgba(241,196,15,0.7)',width:1.2}}}},
  {{x:D.dates,y:D.ma60, type:'scatter',mode:'lines',name:'MA60',line:{{color:'rgba(149,165,166,0.8)',width:1.2,dash:'dot'}}}},
  {{x:D.sig_x,y:D.sig_y,type:'scatter',mode:'markers',name:'시그널',
    marker:{{color:D.sig_color,size:D.sig_size,symbol:D.sig_symbol,line:{{color:'#fff',width:1}}}},
    hovertext:D.sig_hover,hoverinfo:'text',hoverlabel:{{bgcolor:'#1a1d26',bordercolor:'#555'}}}},
],{{...BASE,
  yaxis:{{...BASE.yaxis,title:'원'}},
  shapes:D.regime_shapes,
  margin:{{t:10,b:55,l:70,r:10}},
}},{{responsive:true}});

// ② 비율 차트
const n = D.dates.length;
const thrLines = [
  {{type:'line',x0:D.dates[0],x1:D.dates[n-1],y0:THR.fr20,y1:THR.fr20,
    line:{{color:'rgba(46,204,113,0.4)',width:1,dash:'dot'}}}},
  {{type:'line',x0:D.dates[0],x1:D.dates[n-1],y0:THR.ir20,y1:THR.ir20,
    line:{{color:'rgba(52,152,219,0.4)',width:1,dash:'dot'}}}},
  {{type:'line',x0:D.dates[0],x1:D.dates[n-1],y0:0,y1:0,line:{{color:'#555',width:1}}}},
];
Plotly.newPlot('ratio_chart',[
  {{x:D.dates,y:D.fr20,type:'scatter',mode:'lines',name:'외국계 20일%',line:{{color:'#2ecc71',width:1.5}},yaxis:'y'}},
  {{x:D.dates,y:D.ir20,type:'scatter',mode:'lines',name:'기관 20일%',  line:{{color:'#3498db',width:1.5}},yaxis:'y'}},
  {{x:D.dates,y:D.sr20,type:'scatter',mode:'lines',name:'스마트머니 20일%',
    line:{{color:'#f1c40f',width:2.5}},fill:'tozeroy',fillcolor:'rgba(241,196,15,0.07)',yaxis:'y'}},
  {{x:D.dates,y:D.near_high.map(v=>v?v*100:null),type:'scatter',mode:'lines',
    name:'신고가 거리(%)',line:{{color:'#9b59b6',width:1,dash:'dot'}},yaxis:'y2'}},
],{{...BASE,
  yaxis:{{...BASE.yaxis,title:'수급 비율 (%)',zeroline:false}},
  yaxis2:{{title:'신고가 거리 (%)',overlaying:'y',side:'right',range:[0,105],
           gridcolor:'transparent'}},
  shapes:thrLines,
}},{{responsive:true}});

// ③ 일별 금액
Plotly.newPlot('group_bar',[
  {{x:D.dates,y:D.f_amt,type:'bar',name:'외국계',marker:{{color:'#2ecc71',opacity:0.85}}}},
  {{x:D.dates,y:D.i_amt,type:'bar',name:'기관',  marker:{{color:'#3498db',opacity:0.85}}}},
  {{x:D.dates,y:D.r_amt,type:'bar',name:'개인',  marker:{{color:'#e74c3c',opacity:0.85}}}},
],{{...BASE,barmode:'relative',
  yaxis:{{...BASE.yaxis,title:'억 원',zeroline:true,zerolinecolor:'#555'}},
}},{{responsive:true}});

// ④ 스마트머니 롤링
Plotly.newPlot('smart_chart',[
  {{x:D.dates,y:D.f_amt20, type:'scatter',mode:'lines',name:'외국계 20일',line:{{color:'#2ecc71',width:1.5}}}},
  {{x:D.dates,y:D.i_amt20, type:'scatter',mode:'lines',name:'기관 20일',  line:{{color:'#3498db',width:1.5}}}},
  {{x:D.dates,y:D.sm_amt20,type:'scatter',mode:'lines',name:'스마트머니',
    line:{{color:'#f1c40f',width:2.5}},fill:'tozeroy',fillcolor:'rgba(241,196,15,0.07)'}},
],{{...BASE,
  yaxis:{{...BASE.yaxis,title:'억 원',zeroline:true,zerolinecolor:'#888',zerolinewidth:2}},
}},{{responsive:true}});
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
