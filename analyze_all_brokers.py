#!/usr/bin/env python3
"""다종목 거래원 수급 분석 통합 대시보드.

daily_flow/ 폴더의 모든 종목을 분석 후 하나의 HTML에 탭으로 표시.
- 종합 적중률 집계 (모든 종목 시그널 합산)
- 종목별 차트 + 시그널 + 백테스트
- 종목 간 비교

Usage:  python3 analyze_all_brokers.py
출력:    broker_flow_dashboard.html
"""
import os, sys, warnings, json, math, unicodedata
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from signals.broker_flow import (
    load_stock_flow, build_timeseries, detect_signals, check_price_confirmation,
    detect_foreign_consensus,
    NEAR_HIGH_PCT, DIVERGENCE_PCT,
    FOREIGN_RATIO_20D, INST_RATIO_20D,
    DIST_RETAIL_5D, DIST_LARGE_INST_5D,
    FOREIGN_BREADTH_5D, HHI_THRESHOLD,
)
from file_io import load_json
from config import STOCK_MAP_FILE

FLOW_DIR = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-srshin614@gmail.com"
    "/내 드라이브/01.Claude/01.주식/daily_flow"
)
OUT_HTML = os.path.join(BASE_DIR, "broker_flow_dashboard.html")
MIN_DAYS = 25  # MA20 + 여유분 필요

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


def backtest(signals, df):
    idx_list = df.index.strftime("%Y-%m-%d").tolist()
    closes   = df["close"].tolist()
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
            if j >= len(closes) or p0 in (0, None): return None
            return round((closes[j] / p0 - 1) * 100, 2)
        confirm = check_price_confirmation(df, d)
        out.append({**s, "ret5": ret(5), "ret10": ret(10), "ret20": ret(20),
                    "confirmed": confirm["confirmed"],
                    "days_to_confirm": confirm["days_to_confirm"],
                    "confirm_reason": confirm["reason"]})
    return out


def grade_summary(bt):
    from collections import defaultdict
    s = defaultdict(lambda: {"count":0,"hit10":0,"avg10":[],"avg20":[],"confirmed":0})
    for r in bt:
        g = r["grade"]; s[g]["count"] += 1
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
            "count": cnt, "confirmed": v["confirmed"],
            "confirm_rate": round(v["confirmed"]/cnt*100) if cnt else 0,
            "hit_rate_10d": round(v["hit10"]/cnt*100) if cnt else 0,
            "avg_ret_10d": round(sum(v["avg10"])/len(v["avg10"]),2) if v["avg10"] else None,
            "avg_ret_20d": round(sum(v["avg20"])/len(v["avg20"]),2) if v["avg20"] else None,
        }
    return out


def divergence_summary(bt):
    """다이버전스 마커가 붙은 시그널만 별도 집계."""
    div = [r for r in bt if r.get("divergence")]
    if not div:
        return {"count": 0, "hit_rate_10d": 0, "avg_ret_10d": None, "avg_ret_20d": None}
    cnt = len(div)
    hit10 = sum(1 for r in div if r["ret10"] is not None and r["ret10"] < 0)
    a10 = [r["ret10"] for r in div if r["ret10"] is not None]
    a20 = [r["ret20"] for r in div if r["ret20"] is not None]
    return {
        "count": cnt,
        "hit_rate_10d": round(hit10/cnt*100),
        "avg_ret_10d": round(sum(a10)/len(a10),2) if a10 else None,
        "avg_ret_20d": round(sum(a20)/len(a20),2) if a20 else None,
    }


def analyze_one(stock_name, stock_code):
    """단일 종목 분석. 반환: {df, signals, bt, grade_summary, ...} or None"""
    flow = load_stock_flow(stock_name, FLOW_DIR)
    if not flow or len(flow) < MIN_DAYS:
        print(f"  [SKIP] {stock_name} — {len(flow)}일치 (최소 {MIN_DAYS}일 필요)")
        return None

    dates = sorted(flow.keys())
    try:
        price_df = fetch_price(stock_code, dates[0], dates[-1])
    except Exception as e:
        print(f"  [ERR] {stock_name} 가격 로드 실패: {e}")
        return None
    if len(price_df) < MIN_DAYS:
        print(f"  [SKIP] {stock_name} — 가격 {len(price_df)}일")
        return None

    price_series = price_df["종가"]
    df = build_timeseries(flow, price_series)
    signals = detect_signals(df)
    buy_signals = detect_foreign_consensus(df)
    bt = backtest(signals, df)

    # 분배 패턴 발생 일자 따로 추출
    dist_dates = [s["date"] for s in signals
                  if any("분배 패턴" in r for r in s.get("reasons", []))]

    # 금액 환산
    for g in ("foreign","inst","retail","smart_net"):
        df[f"{g}_amt"] = (df[g] * df["close"] / 1e8).round(2)
    for g in ("foreign","inst","smart_net"):
        df[f"{g}_amt_20d"] = df[f"{g}_amt"].rolling(20).sum().round(2)

    return {
        "name": stock_name, "code": stock_code,
        "df": df, "signals": signals, "bt": bt,
        "buy_signals": buy_signals,
        "dist_dates":  dist_dates,
        "by_grade": grade_summary(bt),
        "diverg":   divergence_summary(bt),
        "n_days": len(flow),
    }


def stock_chart_data(result):
    """종목별 차트용 JSON dict 생성."""
    df = result["df"]; bt = result["bt"]
    df_idx = df.index.strftime("%Y-%m-%d").tolist()
    def S(c): return [clean(v) for v in df[c]] if c in df.columns else [None]*len(df)

    sig_x, sig_y, sig_hover, sig_color, sig_symbol, sig_size = [], [], [], [], [], []
    for s in bt:
        d = s["date"]
        if d not in df_idx: continue
        sig_x.append(d)
        sig_y.append(clean(df["close"].iloc[df_idx.index(d)]))
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
            f"<b>{d} {s['grade']} (점수:{s['score']})</b><br>"
            f"국면: {regime} · 신고가권: {s.get('near_high', 0)}%<br>"
            + ("<b style='color:#ff6b6b'>★ 다이버전스</b><br>" if s.get("divergence") else "")
            + "─────────────<br>"
            + "<br>".join(s["reasons"])
            + f"<br>─────────────<br>가격컨펌: {cf}<br>"
            + f"+5일: {r5}  +10일: {r10}  +20일: {r20}"
        )

    # 추세 국면 배경
    regime_shapes = []
    if "regime" in df.columns:
        rlist = df["regime"].tolist()
        prev_r, start = None, None
        for i, r in enumerate(rlist):
            r_int = int(r) if not (isinstance(r, float) and math.isnan(r)) else 0
            if r_int != prev_r:
                if prev_r and prev_r != 0 and start is not None:
                    regime_shapes.append({
                        "type":"rect","xref":"x","yref":"paper",
                        "x0":df_idx[start],"x1":df_idx[i],"y0":0,"y1":1,
                        "fillcolor":REGIME_COLOR[prev_r],"line":{"width":0},"layer":"below"})
                start = i; prev_r = r_int
        if prev_r and prev_r != 0 and start is not None:
            regime_shapes.append({
                "type":"rect","xref":"x","yref":"paper",
                "x0":df_idx[start],"x1":df_idx[-1],"y0":0,"y1":1,
                "fillcolor":REGIME_COLOR[prev_r],"line":{"width":0},"layer":"below"})

    return {
        "dates": df_idx,
        "close": S("close"), "ma20": S("ma20"), "ma60": S("ma60"),
        "fr20": S("foreign_ratio_20d"), "ir20": S("inst_ratio_20d"),
        "rr5":  S("retail_ratio_5d"),   "sr20": S("smart_net_ratio_20d"),
        "near_high": S("near_high"),
        "f_amt": S("foreign_amt"), "i_amt": S("inst_amt"), "r_amt": S("retail_amt"),
        "sm_amt20": S("smart_net_amt_20d"),
        "regime_shapes": regime_shapes,
        "sig_x": sig_x, "sig_y": [clean(v) for v in sig_y],
        "sig_hover": sig_hover, "sig_color": sig_color,
        "sig_symbol": sig_symbol, "sig_size": sig_size,
    }


def main():
    smap = load_json(STOCK_MAP_FILE, default={})
    if not os.path.isdir(FLOW_DIR):
        print(f"ERROR: {FLOW_DIR} 없음"); sys.exit(1)

    # macOS 파일시스템은 NFD, JSON은 NFC → 비교 시 정규화 필수
    stocks = sorted([
        unicodedata.normalize("NFC", d)
        for d in os.listdir(FLOW_DIR)
        if os.path.isdir(os.path.join(FLOW_DIR, d))
    ])
    print(f"[1] 종목 {len(stocks)}개 발견: {stocks}")

    results = []
    for name in stocks:
        info = smap.get(name, {})
        code = info.get("code")
        if not code:
            print(f"  [SKIP] {name} — stock_map.json에 코드 없음")
            continue
        print(f"[2] 분석: {name} ({code})")
        r = analyze_one(name, code)
        if r:
            results.append(r)

    if not results:
        print("ERROR: 분석 가능한 종목 없음"); sys.exit(1)

    # ── 종합 집계
    print(f"\n[3] 종합 집계 ({len(results)}종목):")
    all_bt = []
    for r in results:
        all_bt.extend(r["bt"])
    overall = grade_summary(all_bt)
    overall_div = divergence_summary(all_bt)

    for g in ["매도강추", "매도주의", "관망"]:
        if g not in overall: continue
        v = overall[g]
        avg10 = f"{v['avg_ret_10d']:+.1f}%" if v["avg_ret_10d"] is not None else "─"
        avg20 = f"{v['avg_ret_20d']:+.1f}%" if v["avg_ret_20d"] is not None else "─"
        print(f"  [{g}] {v['count']}건  10일후하락:{v['hit_rate_10d']}%  "
              f"평균수익률 10일:{avg10}  20일:{avg20}")
    if overall_div["count"] > 0:
        avg10 = f"{overall_div['avg_ret_10d']:+.1f}%" if overall_div["avg_ret_10d"] is not None else "─"
        avg20 = f"{overall_div['avg_ret_20d']:+.1f}%" if overall_div["avg_ret_20d"] is not None else "─"
        print(f"  [★ 다이버전스] {overall_div['count']}건  10일후하락:{overall_div['hit_rate_10d']}%  "
              f"평균수익률 10일:{avg10}  20일:{avg20}")

    # ── 종목별 데이터 준비
    stock_data = {}
    for r in results:
        stock_data[r["name"]] = {
            "code": r["code"],
            "n_days": r["n_days"],
            "n_signals": len(r["signals"]),
            "by_grade": r["by_grade"],
            "diverg":   r["diverg"],
            "chart":    stock_chart_data(r),
            "signals":  [
                {k: v for k, v in s.items() if k != "reasons_full"}
                for s in r["bt"][-50:]  # 최근 50건만
            ],
        }

    # ── HTML 생성
    print("[4] HTML 생성")
    html = build_html(stock_data, overall, overall_div, results)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"    저장: {OUT_HTML}")


def build_overall_card(grade, v, label_color):
    if v is None or v.get("count", 0) == 0:
        return ""
    avg10 = f"{v['avg_ret_10d']:+.1f}%" if v["avg_ret_10d"] is not None else "─"
    avg20 = f"{v['avg_ret_20d']:+.1f}%" if v["avg_ret_20d"] is not None else "─"
    hit_c = "#2ecc71" if v["hit_rate_10d"] >= 60 else "#f39c12" if v["hit_rate_10d"] >= 40 else "#e74c3c"
    confirm_info = f"컨펌 {v['confirmed']}건 ({v['confirm_rate']}%)" if "confirmed" in v else "&nbsp;"
    return f"""<div class="bt-card">
      <div class="bt-grade" style="color:{label_color}">{grade}</div>
      <div class="bt-count">{v['count']}건 · {confirm_info}</div>
      <div class="bt-stat">10일 후 하락률<br><b style="font-size:1.4em;color:{hit_c}">{v['hit_rate_10d']}%</b></div>
      <div class="bt-stat" style="margin-top:8px">평균수익률<br>10일 <b>{avg10}</b> · 20일 <b>{avg20}</b></div>
    </div>"""


def build_html(stock_data, overall, overall_div, results):
    # ── 종합 백테스트 카드들
    overall_cards = (
        build_overall_card("매도강추", overall.get("매도강추"), "#e74c3c") +
        build_overall_card("매도주의", overall.get("매도주의"), "#f39c12") +
        build_overall_card("관망",     overall.get("관망"),     "#95a5a6")
    )
    if overall_div["count"] > 0:
        avg10 = f"{overall_div['avg_ret_10d']:+.1f}%" if overall_div["avg_ret_10d"] is not None else "─"
        avg20 = f"{overall_div['avg_ret_20d']:+.1f}%" if overall_div["avg_ret_20d"] is not None else "─"
        hit_c = "#2ecc71" if overall_div["hit_rate_10d"] >= 60 else "#f39c12" if overall_div["hit_rate_10d"] >= 40 else "#e74c3c"
        overall_cards += f"""<div class="bt-card" style="border:2px solid #ff6b6b">
          <div class="bt-grade" style="color:#ff6b6b">★ 다이버전스</div>
          <div class="bt-count">{overall_div['count']}건</div>
          <div class="bt-stat">10일 후 하락률<br><b style="font-size:1.4em;color:{hit_c}">{overall_div['hit_rate_10d']}%</b></div>
          <div class="bt-stat" style="margin-top:8px">평균수익률<br>10일 <b>{avg10}</b> · 20일 <b>{avg20}</b></div>
        </div>"""

    # ── 종목 비교 테이블
    cmp_rows = ""
    for r in results:
        bg = r["by_grade"]
        nbuy = bg.get("매도강추", {}).get("count", 0)
        ncau = bg.get("매도주의", {}).get("count", 0)
        nwat = bg.get("관망", {}).get("count", 0)
        ndiv = r["diverg"]["count"]
        # 평균 적중률 (매도강추+매도주의)
        sell_signals = (bg.get("매도강추",{}).get("count",0) +
                        bg.get("매도주의",{}).get("count",0))
        sell_hits = (bg.get("매도강추",{}).get("hit_rate_10d",0) * bg.get("매도강추",{}).get("count",0) +
                     bg.get("매도주의",{}).get("hit_rate_10d",0) * bg.get("매도주의",{}).get("count",0))
        avg_hit = round(sell_hits / sell_signals) if sell_signals > 0 else 0
        hit_c = "#2ecc71" if avg_hit >= 60 else "#f39c12" if avg_hit >= 40 else "#95a5a6"

        # 현재 국면 (마지막 행)
        last_row = r["df"].iloc[-1]
        last_regime = int(last_row.get("regime", 0)) if "regime" in r["df"].columns else 0
        rgm_label = REGIME_LABEL.get(last_regime, "?")
        rgm_color = {0:"#95a5a6",1:"#2ecc71",2:"#f39c12",3:"#e74c3c"}.get(last_regime, "#888")
        last_sm = last_row.get("smart_net_ratio_20d", 0) or 0
        last_nh = (last_row.get("near_high", 0) or 0) * 100
        last_li = last_row.get("large_inst_ratio_5d", 0) or 0
        last_rr5 = last_row.get("retail_ratio_5d", 0) or 0
        last_hhi = last_row.get("hhi_5d", 0) or 0
        last_breadth = last_row.get("foreign_breadth_5d", 0) or 0

        # 분배 패턴 + 외국계 컨센서스 표시
        dist_now = "🚨" if (last_rr5 >= DIST_RETAIL_5D and last_li <= DIST_LARGE_INST_5D) else ""
        buy_now  = "📈" if (last_breadth >= FOREIGN_BREADTH_5D and (last_row.get("foreign_ratio_5d", 0) or 0) > 1.0) else ""
        hhi_now  = "⚠️" if last_hhi >= HHI_THRESHOLD else ""
        n_dist = len(r["dist_dates"])
        n_buy  = len(r["buy_signals"])

        cmp_rows += f"""<tr onclick="showTab('{r['name']}')" style="cursor:pointer">
          <td><b>{r['name']}</b><br><span style="color:#666;font-size:.85em">{r['code']}</span></td>
          <td style="text-align:center">{r['n_days']}일</td>
          <td style="text-align:center;color:{rgm_color};font-weight:bold">{rgm_label}</td>
          <td style="text-align:center">{last_nh:.0f}%</td>
          <td style="text-align:center;color:{'#e74c3c' if last_sm<0 else '#2ecc71'}">{last_sm:+.1f}%</td>
          <td style="text-align:center;color:{'#e74c3c' if last_li<0 else '#2ecc71'};font-size:.9em">{last_li:+.1f}%</td>
          <td style="text-align:center;font-size:1.1em">{dist_now} {buy_now} {hhi_now}</td>
          <td style="text-align:center;color:#e74c3c">{nbuy + ncau}</td>
          <td style="text-align:center;color:#ff6b6b">★ {ndiv}</td>
          <td style="text-align:center;color:#ffa07a">🚨 {n_dist}</td>
          <td style="text-align:center;color:#2ecc71">📈 {n_buy}</td>
          <td style="text-align:center;color:{hit_c};font-weight:bold">{avg_hit}%</td>
        </tr>"""

    # ── 종목별 탭 패널
    tab_panels = ""
    tab_buttons = ""
    for i, r in enumerate(results):
        name = r["name"]
        active = "active" if i == 0 else ""
        tab_buttons += f'<button class="tab-btn {active}" onclick="showTab(\'{name}\')" data-tab="{name}">{name}</button>'

        bg = r["by_grade"]
        cards = (
            build_overall_card("매도강추", bg.get("매도강추"), "#e74c3c") +
            build_overall_card("매도주의", bg.get("매도주의"), "#f39c12") +
            build_overall_card("관망",     bg.get("관망"),     "#95a5a6")
        )
        if r["diverg"]["count"] > 0:
            v = r["diverg"]
            avg10 = f"{v['avg_ret_10d']:+.1f}%" if v["avg_ret_10d"] is not None else "─"
            avg20 = f"{v['avg_ret_20d']:+.1f}%" if v["avg_ret_20d"] is not None else "─"
            hit_c = "#2ecc71" if v["hit_rate_10d"] >= 60 else "#f39c12" if v["hit_rate_10d"] >= 40 else "#e74c3c"
            cards += f"""<div class="bt-card" style="border:2px solid #ff6b6b">
              <div class="bt-grade" style="color:#ff6b6b">★ 다이버전스</div>
              <div class="bt-count">{v['count']}건</div>
              <div class="bt-stat">10일 후 하락률<br><b style="font-size:1.4em;color:{hit_c}">{v['hit_rate_10d']}%</b></div>
              <div class="bt-stat" style="margin-top:8px">평균수익률<br>10일 <b>{avg10}</b> · 20일 <b>{avg20}</b></div>
            </div>"""

        # 시그널 테이블
        sig_rows = ""
        for s in reversed(r["bt"][-40:]):
            clr = "#e74c3c" if s["score"] >= 5 else "#f39c12" if s["score"] >= 3 else "#95a5a6"
            regime = REGIME_LABEL.get(s["regime"], "?")
            regime_clr = {0:"#95a5a6",1:"#2ecc71",2:"#f39c12",3:"#e74c3c"}.get(s["regime"], "#888")
            def fmt(rt):
                if rt is None: return "─", ""
                return f"{rt:+.1f}%", f"color:{'#2ecc71' if rt<0 else '#e74c3c'}"
            r5,c5 = fmt(s["ret5"]); r10,c10 = fmt(s["ret10"]); r20,c20 = fmt(s["ret20"])
            cb = ""
            if s.get("confirmed"):
                cb = f'<span style="background:#e74c3c;color:#fff;padding:1px 6px;border-radius:3px;font-size:.72em">컨펌 {s["days_to_confirm"]}일</span>'
            db = '<span style="color:#ff6b6b">★</span> ' if s.get("divergence") else ""
            sig_rows += f"""<tr>
              <td>{s['date']}</td>
              <td style="color:{clr};font-weight:bold">{db}{s['grade']}</td>
              <td style="color:{clr};text-align:center">{s['score']}</td>
              <td style="color:{regime_clr};text-align:center;font-size:.85em">{regime}</td>
              <td style="text-align:center">{cb}</td>
              <td style="text-align:center;{c5}">{r5}</td>
              <td style="text-align:center;{c10}">{r10}</td>
              <td style="text-align:center;{c20}">{r20}</td>
            </tr>"""

        tab_panels += f"""<div class="tab-panel {active}" data-panel="{name}">
          <h2 style="color:#4fc3f7;margin-bottom:6px">{name} ({r['code']})</h2>
          <div style="color:#888;font-size:.85em;margin-bottom:14px">
            데이터 {r['n_days']}일치 · 시그널 {len(r['signals'])}건 · 다이버전스 {r['diverg']['count']}건
          </div>
          <div class="bt-row">{cards}</div>
          <div class="card">
            <div class="section-title">주가 + MA + 시그널 + 추세 국면</div>
            <div id="chart_price_{i}" style="height:380px"></div>
          </div>
          <div class="card">
            <div class="section-title">20일 수급 비율 + 신고가 거리</div>
            <div id="chart_ratio_{i}" style="height:300px"></div>
          </div>
          <div class="card">
            <div class="section-title">시그널 목록 (최근 40건)</div>
            <table>
              <tr><th>날짜</th><th>등급</th><th>점수</th><th>국면</th><th>컨펌</th>
                <th style="text-align:center">+5일</th><th style="text-align:center">+10일</th><th style="text-align:center">+20일</th></tr>
              {sig_rows}
            </table>
          </div>
        </div>"""

    # JSON 데이터 (차트용)
    chart_json = json.dumps({
        name: data["chart"] for name, data in stock_data.items()
    }, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>거래원 수급 통합 대시보드</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:'Segoe UI',sans-serif; background:#0f1117; color:#dde; padding:20px; font-size:14px; }}
h1 {{ font-size:1.5em; color:#4fc3f7; margin-bottom:4px; }}
h2 {{ font-size:1.25em; }}
.subtitle {{ color:#667; font-size:.85em; margin-bottom:20px; }}
.section-title {{ font-size:1.05em; color:#4fc3f7; margin-bottom:8px; font-weight:600; }}
.desc {{ color:#889; font-size:.82em; line-height:1.7; margin-bottom:10px; }}
.card {{ background:#1a1d26; border-radius:10px; padding:18px; margin-bottom:16px; }}
.bt-row {{ display:flex; gap:12px; margin-bottom:16px; flex-wrap:wrap; }}
.bt-card {{ background:#1a1d26; border-radius:10px; padding:14px; flex:1; min-width:180px; text-align:center; }}
.bt-grade {{ font-size:1.05em; font-weight:700; margin-bottom:6px; }}
.bt-count {{ color:#888; font-size:.78em; margin-bottom:10px; }}
.bt-stat {{ font-size:.85em; color:#bbb; line-height:1.7; }}
.bt-stat b {{ color:#eee; }}
table {{ width:100%; border-collapse:collapse; font-size:.82em; }}
th {{ background:#20232e; padding:8px 6px; text-align:left; color:#889; font-weight:500; white-space:nowrap; }}
td {{ padding:7px 6px; border-bottom:1px solid #1e2130; vertical-align:top; }}
tr:hover td {{ background:#1e2130; }}
.callout {{ background:#1a2332; border-left:3px solid #4fc3f7; padding:12px 14px; border-radius:6px;
            margin-bottom:14px; color:#bcd; font-size:.86em; line-height:1.7; }}
.tabs {{ display:flex; gap:6px; flex-wrap:wrap; margin-bottom:16px; padding-bottom:10px; border-bottom:1px solid #2a2d3e; }}
.tab-btn {{ background:#1a1d26; border:1px solid #2a2d3e; color:#aaa; padding:8px 14px;
           border-radius:6px; cursor:pointer; font-size:.9em; transition:all 0.2s; }}
.tab-btn:hover {{ background:#252836; color:#eee; }}
.tab-btn.active {{ background:#4fc3f7; color:#0f1117; border-color:#4fc3f7; font-weight:600; }}
.tab-panel {{ display:none; }}
.tab-panel.active {{ display:block; }}
</style>
</head>
<body>

<h1>거래원 수급 통합 대시보드</h1>
<p class="subtitle">추세 국면 + 다이버전스 + 가격 컨펌 — {len(results)}종목 분석</p>

<!-- 종합 백테스트 -->
<div class="card">
  <div class="section-title">종합 백테스트 — 모든 종목 시그널 합산</div>
  <p class="desc">
    여러 종목 데이터를 합치면 통계적 유의성이 올라갑니다.
    <b>★ 다이버전스 카드</b>의 적중률이 핵심 지표 — 이 값이 60% 넘으면 시그널 로직이 유효한 것.
  </p>
  <div class="bt-row">{overall_cards}</div>
</div>

<!-- 종목 비교 테이블 -->
<div class="card">
  <div class="section-title">종목별 현재 상태 + 시그널 통계</div>
  <p class="desc">
    행 클릭 시 해당 종목 상세 탭으로 이동.<br>
    <b>현재 국면</b>이 분배의심/하락추세인 종목, <b>스마트머니 20일</b>이 음수인 종목, <b>다이버전스(★)</b>가 있는 종목에 주목.
  </p>
  <table>
    <tr>
      <th>종목</th>
      <th style="text-align:center">데이터<br>일수</th>
      <th style="text-align:center">현재<br>국면</th>
      <th style="text-align:center">신고가<br>거리</th>
      <th style="text-align:center">스마트<br>머니20일</th>
      <th style="text-align:center">대형기관<br>5일%</th>
      <th style="text-align:center">현재<br>플래그</th>
      <th style="text-align:center">매도<br>시그널</th>
      <th style="text-align:center">다이버<br>전스</th>
      <th style="text-align:center">분배<br>패턴</th>
      <th style="text-align:center">외인<br>컨센서스</th>
      <th style="text-align:center">매도<br>적중률</th>
    </tr>
    {cmp_rows}
  </table>
  <div style="margin-top:8px;font-size:.78em;color:#888;line-height:1.6">
    🚨 = 분배 패턴 발생 중 (개미 매수 + 대형기관 매도) ·
    📈 = 외국계 컨센서스 매수 (3+ 외국계 동반) ·
    ⚠️ = 거래원 집중도 위험 (HHI ≥ {HHI_THRESHOLD})
  </div>
</div>

<!-- 종목별 상세 -->
<div class="tabs">
  {tab_buttons}
</div>

{tab_panels}

<script>
const CHARTS = {chart_json};
const BASE = {{
  paper_bgcolor:'#1a1d26', plot_bgcolor:'#1a1d26',
  font:{{color:'#ccc',size:11}},
  xaxis:{{gridcolor:'#252836',zeroline:false}},
  yaxis:{{gridcolor:'#252836'}},
  legend:{{orientation:'h',y:-0.18}},
  margin:{{t:10,b:55,l:65,r:10}},
  hovermode:'closest',
}};

const stockNames = Object.keys(CHARTS);

function renderChartsFor(stockName, idx) {{
  const D = CHARTS[stockName];
  // 주가 + 시그널
  Plotly.newPlot(`chart_price_${{idx}}`, [
    {{x:D.dates,y:D.close,type:'scatter',mode:'lines',name:'종가',line:{{color:'#4fc3f7',width:2}}}},
    {{x:D.dates,y:D.ma20,type:'scatter',mode:'lines',name:'MA20',line:{{color:'rgba(241,196,15,0.7)',width:1.2}}}},
    {{x:D.dates,y:D.ma60,type:'scatter',mode:'lines',name:'MA60',line:{{color:'rgba(149,165,166,0.8)',width:1.2,dash:'dot'}}}},
    {{x:D.sig_x,y:D.sig_y,type:'scatter',mode:'markers',name:'시그널',
      marker:{{color:D.sig_color,size:D.sig_size,symbol:D.sig_symbol,line:{{color:'#fff',width:1}}}},
      hovertext:D.sig_hover,hoverinfo:'text',hoverlabel:{{bgcolor:'#1a1d26'}}}},
  ], {{...BASE, yaxis:{{...BASE.yaxis,title:'원'}}, shapes:D.regime_shapes}}, {{responsive:true}});

  // 비율 차트
  const n = D.dates.length;
  const thrLines = [
    {{type:'line',x0:D.dates[0],x1:D.dates[n-1],y0:{FOREIGN_RATIO_20D},y1:{FOREIGN_RATIO_20D},
      line:{{color:'rgba(46,204,113,0.4)',width:1,dash:'dot'}}}},
    {{type:'line',x0:D.dates[0],x1:D.dates[n-1],y0:{INST_RATIO_20D},y1:{INST_RATIO_20D},
      line:{{color:'rgba(52,152,219,0.4)',width:1,dash:'dot'}}}},
    {{type:'line',x0:D.dates[0],x1:D.dates[n-1],y0:0,y1:0,line:{{color:'#555',width:1}}}},
  ];
  Plotly.newPlot(`chart_ratio_${{idx}}`, [
    {{x:D.dates,y:D.fr20,type:'scatter',mode:'lines',name:'외국계 20일%',line:{{color:'#2ecc71',width:1.5}}}},
    {{x:D.dates,y:D.ir20,type:'scatter',mode:'lines',name:'기관 20일%',  line:{{color:'#3498db',width:1.5}}}},
    {{x:D.dates,y:D.sr20,type:'scatter',mode:'lines',name:'스마트머니 20일%',
      line:{{color:'#f1c40f',width:2.5}},fill:'tozeroy',fillcolor:'rgba(241,196,15,0.07)'}},
    {{x:D.dates,y:D.near_high.map(v=>v?v*100:null),type:'scatter',mode:'lines',
      name:'신고가 거리(%)',line:{{color:'#9b59b6',width:1,dash:'dot'}},yaxis:'y2'}},
  ], {{...BASE,
    yaxis:{{...BASE.yaxis,title:'수급 비율 (%)'}},
    yaxis2:{{title:'신고가 거리(%)',overlaying:'y',side:'right',range:[0,105],gridcolor:'transparent'}},
    shapes:thrLines,
  }}, {{responsive:true}});
}}

let renderedStocks = new Set();
function showTab(name) {{
  document.querySelectorAll('.tab-btn').forEach(b => {{
    b.classList.toggle('active', b.dataset.tab === name);
  }});
  document.querySelectorAll('.tab-panel').forEach(p => {{
    p.classList.toggle('active', p.dataset.panel === name);
  }});
  // 차트 lazy render
  if (!renderedStocks.has(name)) {{
    const idx = stockNames.indexOf(name);
    if (idx >= 0) {{
      renderChartsFor(name, idx);
      renderedStocks.add(name);
    }}
  }}
  window.scrollTo({{top: document.querySelector('.tabs').offsetTop - 20, behavior:'smooth'}});
}}

// 첫 탭 자동 렌더
window.addEventListener('DOMContentLoaded', () => {{
  if (stockNames.length > 0) {{
    renderChartsFor(stockNames[0], 0);
    renderedStocks.add(stockNames[0]);
  }}
}});
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
