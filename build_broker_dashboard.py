#!/usr/bin/env python3
"""거래원 수급 통합 대시보드 빌더.

출력 구조:
  dashboard/
    index.html              ← 전체 대시보드 (overview)
    stocks/{code}.html      ← 종목별 상세 페이지
    assets/style.css        ← 공통 스타일

Usage: python3 build_broker_dashboard.py
"""
import os, sys, warnings, json, math, unicodedata
from collections import defaultdict
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from signals.broker_flow import (
    load_stock_flow, build_timeseries, detect_signals, detect_foreign_consensus,
    check_price_confirmation,
    NEAR_HIGH_PCT, DIVERGENCE_PCT,
    FOREIGN_RATIO_20D, INST_RATIO_20D,
    DIST_RETAIL_5D, DIST_LARGE_INST_5D,
    FOREIGN_BREADTH_5D, HHI_THRESHOLD, TOP_SHARE_5D,
)
from signals.price_volume import add_price_volume_signals, CMF_DIST_THRESH, CMF_ACCUM_THRESH
from signals.krx_extra import (
    fetch_shorting_balance, fetch_foreign_ownership, fetch_inst_detail_flow,
    analyze_short_pressure, analyze_foreign_ownership, analyze_inst_detail,
)
from signals.short_data import analyze_short as analyze_short_hts

SHORT_DIR_LOCAL = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-srshin614@gmail.com"
    "/내 드라이브/01.Claude/01.주식/daily_short"
)
from file_io import load_json
from config import STOCK_MAP_FILE

FLOW_DIR = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-srshin614@gmail.com"
    "/내 드라이브/01.Claude/01.주식/daily_flow"
)
DASHBOARD_DIR = os.path.join(BASE_DIR, "dashboard")
STOCKS_DIR    = os.path.join(DASHBOARD_DIR, "stocks")
MIN_DAYS = 25

REGIME_LABEL = {0: "중립", 1: "상승추세", 2: "분배의심", 3: "하락추세"}
REGIME_COLOR_BG = {0: "rgba(149,165,166,0.04)", 1: "rgba(46,204,113,0.06)",
                    2: "rgba(243,156,18,0.10)", 3: "rgba(231,76,60,0.10)"}


def clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)): return None
    return v.item() if hasattr(v, "item") else v


def fmt_qty(n):
    if n is None: return "─"
    if abs(n) >= 1e6: return f"{n/1e6:+.2f}M"
    if abs(n) >= 1e3: return f"{n/1e3:+.1f}k"
    return f"{n:+,}"


def fetch_price(code, start, end):
    from pykrx import stock as krx
    df = krx.get_market_ohlcv_by_date(start, end, code)
    df.index = df.index.strftime("%Y-%m-%d")
    return df


def action_class(action):
    if not action: return "action-hold"
    if "전량 매도" in action or "강한" in action: return "action-strong"
    if "매도" in action: return "action-sell"
    if "익절" in action: return "action-partial"
    if "추가 매수" in action: return "action-buy-strong"
    if "매수" in action or "관심" in action: return "action-buy"
    return "action-hold"


def grade_class(grade):
    return {
        "매도강추": "grade-strong-sell",
        "매도주의": "grade-sell-watch",
        "관망":     "grade-watch",
        "매수강추": "grade-strong-buy",
        "매수주의": "grade-buy",
    }.get(grade, "grade-watch")


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
                    "days_to_confirm": confirm["days_to_confirm"]})
    return out


def grade_summary(bt):
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


def broker_phase_analysis(flow_data, peak_date):
    """상승/하락 구간별 거래원 누적 매매. 분배 주도자도 추출."""
    pre  = defaultdict(lambda: [0, ""])
    post = defaultdict(lambda: [0, ""])
    for d, rows in flow_data.items():
        for r in rows:
            tgt = pre if d <= peak_date else post
            tgt[r["broker"]][0] += r["net"]
            tgt[r["broker"]][1] = r["group"]

    top_buyers_pre  = sorted(pre.items(),  key=lambda x: -x[1][0])[:10]
    top_sellers_post = sorted(post.items(), key=lambda x: x[1][0])[:10]

    # 분배 주도자: 상승에서 산 사람 + 하락에서 판 사람
    distributors = []
    for b, (post_n, post_g) in post.items():
        if post_n < 0 and pre.get(b, [0])[0] > 0:
            distributors.append({
                "broker": b, "group": post_g,
                "pre_buy":  pre[b][0],
                "post_sell": post_n,
                "round":    pre[b][0] + post_n,
            })
    distributors.sort(key=lambda x: x["post_sell"])
    return top_buyers_pre, top_sellers_post, distributors[:8]


def analyze_one(name, code):
    flow = load_stock_flow(name, FLOW_DIR)
    if not flow or len(flow) < MIN_DAYS:
        return None

    dates = sorted(flow.keys())
    try:
        price_df = fetch_price(code, dates[0], dates[-1])
    except Exception as e:
        print(f"  [ERR] {name} 가격: {e}")
        return None
    if len(price_df) < MIN_DAYS:
        return None

    df = build_timeseries(flow, price_df["종가"])

    # 가격-거래량 시그널: OBV/CMF/MFI (전체 가격 데이터로 계산 후 매핑)
    pv_df = price_df.rename(columns={"시가":"open","고가":"high","저가":"low","종가":"close","거래량":"volume"}).copy()
    pv_df = add_price_volume_signals(pv_df)
    # 매핑: df 인덱스에 맞춰 OBV/CMF/MFI 추가
    for col in ("obv", "obv_ma20", "cmf", "mfi", "obv_diverg_bear", "obv_diverg_bull"):
        if col in pv_df.columns:
            df[col] = [pv_df[col].get(d) for d in df.index.strftime("%Y-%m-%d")]

    sigs = detect_signals(df)
    buy_sigs = detect_foreign_consensus(df)
    bt = backtest(sigs, df)

    # KRX 추가 데이터 (선택적 — 실패해도 진행)
    extra = {"short": None, "foreign": None, "inst_detail": None, "short_hts": None}
    try:
        sd = fetch_shorting_balance(code, dates[0], dates[-1])
        extra["short"] = analyze_short_pressure(sd)
    except Exception as e:
        extra["short"] = {"available": False, "error": str(e)[:60]}
    try:
        fd = fetch_foreign_ownership(code, dates[0], dates[-1])
        extra["foreign"] = analyze_foreign_ownership(fd)
    except Exception as e:
        extra["foreign"] = {"available": False, "error": str(e)[:60]}
    try:
        id_ = fetch_inst_detail_flow(code, dates[0], dates[-1])
        extra["inst_detail"] = analyze_inst_detail(id_)
    except Exception as e:
        extra["inst_detail"] = {"available": False, "error": str(e)[:60]}
    # HTS 직접 추출 공매도 (daily_short/) — KRX API 차단되어도 작동
    try:
        extra["short_hts"] = analyze_short_hts(name, SHORT_DIR_LOCAL)
    except Exception as e:
        extra["short_hts"] = {"available": False, "error": str(e)[:60]}

    # 금액 환산
    for g in ("foreign","inst","retail","smart_net"):
        df[f"{g}_amt"] = (df[g] * df["close"] / 1e8).round(2)
    for g in ("foreign","inst","smart_net"):
        df[f"{g}_amt_20d"] = df[f"{g}_amt"].rolling(20).sum().round(2)

    # 고점/저점
    peak_date = price_df["종가"].idxmax()
    peak_price = float(price_df["종가"].max())
    last_price = float(price_df["종가"].iloc[-1])

    # 거래원 분석 (전반/후반 또는 고점 기준)
    top_buy_pre, top_sell_post, distributors = broker_phase_analysis(flow, peak_date)

    return {
        "name": name, "code": code,
        "df": df, "flow": flow,
        "signals": sigs, "buy_signals": buy_sigs,
        "bt": bt,
        "by_grade": grade_summary(bt),
        "n_days": len(flow),
        "peak_date":  peak_date, "peak_price": peak_price,
        "last_price": last_price, "last_date": dates[-1],
        "drop_from_peak": (last_price/peak_price - 1) * 100,
        "top_buyers_pre":  top_buy_pre,
        "top_sellers_post": top_sell_post,
        "distributors": distributors,
        "extra": extra,
    }


# ──────────────────────────────────────────────────────────
# OVERVIEW PAGE
# ──────────────────────────────────────────────────────────
def render_overview(results, all_bt):
    overall = grade_summary(all_bt)

    # 알림: 현재 ON인 종목들
    danger_stocks = []  # 분배 패턴 또는 트레일링 스탑 또는 다이버전스
    warn_stocks = []    # 관찰 필요
    good_stocks = []    # 외인 컨센서스
    for r in results:
        last = r["df"].iloc[-1]
        rr5  = last.get("retail_ratio_5d", 0) or 0
        li5  = last.get("large_inst_ratio_5d", 0) or 0
        breadth = last.get("foreign_breadth_5d", 0) or 0
        fr5  = last.get("foreign_ratio_5d", 0) or 0
        from_high = last.get("from_high", 0) or 0
        regime = int(last.get("regime", 0)) if "regime" in r["df"].columns else 0
        diverg_recent = bool(last.get("divergence", False)) if "divergence" in r["df"].columns else False

        if (rr5 >= DIST_RETAIL_5D and li5 <= DIST_LARGE_INST_5D) or from_high <= -10 or diverg_recent:
            reasons = []
            if (rr5 >= DIST_RETAIL_5D and li5 <= DIST_LARGE_INST_5D):
                reasons.append(f"분배(개미+{rr5:.1f}%/대형{li5:+.1f}%)")
            if from_high <= -10:
                reasons.append(f"고점 {from_high:.1f}%")
            if diverg_recent:
                reasons.append("다이버전스")
            danger_stocks.append((r, " · ".join(reasons)))
        elif regime == 2 or regime == 3:
            warn_stocks.append((r, REGIME_LABEL[regime]))
        elif breadth >= FOREIGN_BREADTH_5D and fr5 > 1.0:
            good_stocks.append((r, f"외인 컨센서스 (breadth +{int(breadth)})"))

    # 알림 카드 HTML
    alert_html = ""
    if danger_stocks:
        items = "<br>".join(f"<b>{r['name']}</b> — {reason}" for r, reason in danger_stocks)
        alert_html += f"""<div class="alert danger">
          <div class="alert-title">🚨 위험 알림 ({len(danger_stocks)}종목)</div>
          <div class="alert-value" style="font-size:1.05em;line-height:1.5">{items}</div>
        </div>"""
    if warn_stocks:
        items = "<br>".join(f"<b>{r['name']}</b> — {reason}" for r, reason in warn_stocks)
        alert_html += f"""<div class="alert warn">
          <div class="alert-title">⚠️ 주의 ({len(warn_stocks)}종목)</div>
          <div class="alert-value" style="font-size:1.05em;line-height:1.5">{items}</div>
        </div>"""
    if good_stocks:
        items = "<br>".join(f"<b>{r['name']}</b> — {reason}" for r, reason in good_stocks)
        alert_html += f"""<div class="alert good">
          <div class="alert-title">📈 매수 신호 ({len(good_stocks)}종목)</div>
          <div class="alert-value" style="font-size:1.05em;line-height:1.5">{items}</div>
        </div>"""
    if not alert_html:
        alert_html = '<div class="alert"><div class="alert-title">현재 알림</div><div class="alert-detail">긴급 시그널 없음</div></div>'

    # 종합 백테스트 카드
    bt_cards = ""
    for g in ["매도강추", "매도주의", "관망"]:
        if g not in overall: continue
        v = overall[g]
        avg10 = f"{v['avg_ret_10d']:+.1f}%" if v["avg_ret_10d"] is not None else "─"
        avg20 = f"{v['avg_ret_20d']:+.1f}%" if v["avg_ret_20d"] is not None else "─"
        hit_c = "ret-down" if v["hit_rate_10d"] >= 60 else ("grade-sell-watch" if v["hit_rate_10d"] >= 40 else "ret-up")
        gclass = grade_class(g)
        bt_cards += f"""<div class="kpi">
          <div class="kpi-label {gclass}" style="font-size:0.95em;font-weight:600">{g}</div>
          <div class="kpi-sub">{v['count']}건</div>
          <div class="kpi-value {hit_c}">{v['hit_rate_10d']}%</div>
          <div class="kpi-sub">10일 후 하락률</div>
          <div class="kpi-sub" style="margin-top:6px">평균 10일 <b style="color:#ddd">{avg10}</b> · 20일 <b style="color:#ddd">{avg20}</b></div>
        </div>"""

    # 비교 테이블
    cmp_rows = ""
    for r in results:
        last = r["df"].iloc[-1]
        rgm = int(last.get("regime", 0)) if "regime" in r["df"].columns else 0
        rgm_label = REGIME_LABEL.get(rgm, "?")
        sm20 = last.get("smart_net_ratio_20d", 0) or 0
        nh   = (last.get("near_high", 0) or 0) * 100
        from_high = last.get("from_high", 0) or 0
        rr5  = last.get("retail_ratio_5d", 0) or 0
        li5  = last.get("large_inst_ratio_5d", 0) or 0
        breadth = last.get("foreign_breadth_5d", 0) or 0
        fr5  = last.get("foreign_ratio_5d", 0) or 0
        hhi5 = last.get("hhi_5d", 0) or 0

        # 현재 활성 플래그
        flags = []
        if rr5 >= DIST_RETAIL_5D and li5 <= DIST_LARGE_INST_5D:
            flags.append("🚨")
        if breadth >= FOREIGN_BREADTH_5D and fr5 > 1.0:
            flags.append("📈")
        if hhi5 >= HHI_THRESHOLD:
            flags.append("⚠️")
        if from_high <= -10:
            flags.append("🔻")

        # 최근 행동 권고
        recent_action = "─"
        if r["signals"]:
            last_sig = r["signals"][-1]
            recent_action = last_sig.get("action", "─")
        elif r["buy_signals"]:
            last_buy = r["buy_signals"][-1]
            recent_action = last_buy.get("action", "─")

        # 통계
        n_dist = sum(1 for s in r["signals"] if s.get("dist_pattern"))
        n_diverg = sum(1 for s in r["signals"] if s.get("divergence"))
        n_buy = len(r["buy_signals"])

        cmp_rows += f"""<tr class="clickable" onclick="location.href='stocks/{r['code']}.html'">
          <td><b>{r['name']}</b><br><span style="color:#666;font-size:0.82em">{r['code']}</span></td>
          <td class="mono" style="text-align:center">{r['n_days']}</td>
          <td style="text-align:center" class="regime-{rgm}"><b>{rgm_label}</b></td>
          <td class="mono" style="text-align:center">{nh:.0f}%</td>
          <td class="mono" style="text-align:center" class="{'ret-up' if from_high<-5 else 'mono'}">{from_high:+.1f}%</td>
          <td class="mono" style="text-align:center;color:{'#ef4444' if sm20<0 else '#10b981'}">{sm20:+.1f}%</td>
          <td class="mono" style="text-align:center;color:{'#ef4444' if li5<0 else '#10b981'}">{li5:+.1f}%</td>
          <td style="text-align:center;font-size:1.1em" class="flag-row">{''.join(flags) or '─'}</td>
          <td style="text-align:center" class="mono">🚨 {n_dist}</td>
          <td style="text-align:center" class="mono">★ {n_diverg}</td>
          <td style="text-align:center" class="mono grp-foreign">📈 {n_buy}</td>
          <td><span class="action-badge {action_class(recent_action)}">{recent_action}</span></td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>거래원 수급 통합 대시보드</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<div class="container">

<div class="nav">
  <a href="index.html" class="active">📊 전체 대시보드</a>
  <a href="profit_taking.html">💰 익절 타이밍</a>
  <a href="status.html">📋 현재 상황</a>
  <a href="trading_style.html">🎯 매매 스타일</a>
  <span style="color:#444;margin-left:auto">|</span>
  <span style="color:#888;font-size:0.85em">{len(results)}종목 분석 · daily_flow 기반</span>
</div>

<h1>거래원 수급 통합 대시보드</h1>
<p class="subtitle">추세 국면 · 베어리시 다이버전스 · 분배 패턴 · 외국계 컨센서스 · 가격 컨펌 백테스트</p>

<!-- 알림 -->
<div class="alert-row">{alert_html}</div>

<!-- 종합 백테스트 -->
<div class="card">
  <h2>종합 백테스트 ({len(results)}종목 합산)</h2>
  <p class="desc">각 등급의 시그널이 발생한 후 실제 주가가 하락한 비율과 평균 수익률.<br>
    적중률 60% 이상 = 유효 / 40~60% = 보통 / 40% 미만 = 임계값 재조정 필요.</p>
  <div class="grid4">{bt_cards}</div>
</div>

<!-- 비교 테이블 -->
<div class="card">
  <h2>종목별 현재 상태</h2>
  <p class="desc">행 클릭 시 상세 페이지로 이동.<br>
    🚨 분배 패턴 · 📈 외국계 컨센서스 · ⚠️ 거래원 집중도 · 🔻 트레일링 스탑(고점 -10%↓)</p>
  <table>
    <tr>
      <th>종목</th>
      <th style="text-align:center">데이터<br>일수</th>
      <th style="text-align:center">현재 국면</th>
      <th style="text-align:center">신고가<br>거리</th>
      <th style="text-align:center">고점<br>이격</th>
      <th style="text-align:center">스마트<br>20일%</th>
      <th style="text-align:center">대형기관<br>5일%</th>
      <th style="text-align:center">현재<br>플래그</th>
      <th style="text-align:center">분배<br>건수</th>
      <th style="text-align:center">다이버<br>전스</th>
      <th style="text-align:center">매수<br>컨센</th>
      <th>최근 권고</th>
    </tr>
    {cmp_rows}
  </table>
</div>

<!-- 시그널 임계값 안내 -->
<div class="card">
  <h2>시그널 임계값 (현재 적용 중)</h2>
  <div class="grid2">
    <div>
      <h3>매도 시그널</h3>
      <ul class="reason-list" style="line-height:1.9">
        <li>외국계 20일 비율 ≤ <b>{FOREIGN_RATIO_20D}%</b> → +1~2점</li>
        <li>기관 20일 비율 ≤ <b>{INST_RATIO_20D}%</b> → +1~2점</li>
        <li>스마트머니 20일 비율 양→음 전환 → +2점</li>
        <li>다이버전스 (신고가 {int(DIVERGENCE_PCT*100)}%↑ + 스마트머니 음수) → +3점</li>
        <li>분배 패턴 (개미 +{DIST_RETAIL_5D}% / 대형기관 {DIST_LARGE_INST_5D}%) → +3~5점 (지속성 가중)</li>
        <li>거래량 다이버전스 (가격 +5% / 거래량 비율 &lt; 0.8) → +2점</li>
        <li>거래원 집중도 (HHI ≥ {HHI_THRESHOLD} 또는 단일 점유율 ≥ {TOP_SHARE_5D}%) → +1점</li>
        <li>트레일링 스탑 (60일 신고가 -10%↓) → +1점</li>
        <li>3개+ 카테고리 동시 발동 시너지 → +2~3점</li>
      </ul>
    </div>
    <div>
      <h3>매수 시그널 (외국계 컨센서스)</h3>
      <ul class="reason-list" style="line-height:1.9">
        <li>5일 누적 외국계 매수자-매도자 ≥ <b>{FOREIGN_BREADTH_5D}</b></li>
        <li>외국계 5일 비율 &gt; +1.0%</li>
        <li>점수 기본 +2, 상승추세 +1, 하락추세(반등) +2</li>
      </ul>
      <h3 style="margin-top:14px">행동 권고 4단계</h3>
      <ul class="reason-list" style="line-height:1.9">
        <li><span class="action-badge action-strong">전량 매도</span> 다이버전스+분배 동시 또는 트레일링 스탑</li>
        <li><span class="action-badge action-sell">매도</span> 다이버전스 / 분배 / 하락추세 단독</li>
        <li><span class="action-badge action-partial">부분 익절</span> 신고가권 + 약한 시그널</li>
        <li><span class="action-badge action-buy">신규 매수</span> 외국계 컨센서스</li>
        <li><span class="action-badge action-hold">HOLD</span> 추세 견조 + 시그널 없음</li>
      </ul>
    </div>
  </div>
</div>

</div>
</body>
</html>"""


# ──────────────────────────────────────────────────────────
# EXTRA DATA CARD (KRX 공매도/외국인보유/기관세부)
# ──────────────────────────────────────────────────────────
def render_extra_card(extra: dict) -> str:
    short = extra.get("short") or {}
    foreign = extra.get("foreign") or {}
    inst = extra.get("inst_detail") or {}
    short_hts = extra.get("short_hts") or {}

    # 어떤 데이터든 사용 가능한지
    any_avail = any(d.get("available") for d in (short, foreign, inst, short_hts) if isinstance(d, dict))
    if not any_avail:
        return f"""<div class="card">
          <h2>KRX 추가 데이터 (공매도 · 외국인 보유 · 기관 세부)</h2>
          <div class="callout warn">
            ⚠️ KRX API가 현재 응답하지 않습니다 (일시적 차단 가능).<br>
            다시 시도하려면: <code>rm -rf data/krx_cache && python3 build_broker_dashboard.py</code><br>
            또는 KRX 사이트 접속 가능한지 확인 후 재실행.
          </div>
        </div>"""

    cards = []

    # ── HTS 직접 추출 공매도 (가장 신뢰도 높음 — KRX API 우회)
    if short_hts.get("available"):
        bal_color = "#ef4444" if short_hts.get("alert") else "#fbbf24"
        bal_5d = short_hts.get("last_balance_5d_pct", 0)
        bal_ratio = short_hts.get("last_balance_ratio", 0)
        cards.append(f"""<div class="kpi" style="border:2px solid {bal_color}">
          <div class="kpi-label">🔻 공매도 잔고 (HTS 직접)</div>
          <div class="kpi-value mono" style="color:{bal_color}">{bal_ratio:.2f}%</div>
          <div class="kpi-sub">5일 변화 <b>{bal_5d:+.1f}%</b></div>
          <div class="kpi-sub" style="margin-top:4px">{'🚨 5일 +30% 급증' if short_hts.get('alert') else '안정'}</div>
          <div class="kpi-sub" style="margin-top:4px;color:#6b7280">시그널 {short_hts.get('n_signals', 0)}건 / {short_hts.get('n_days')}일치</div>
        </div>""")

    # 공매도 (KRX API)
    if short.get("available"):
        bal_color = "#ef4444" if short.get("alert") else "#fbbf24"
        chg = short.get("change_5d_pct", 0)
        ratio = short.get("last_ratio")
        ratio_str = f"{ratio:.2f}%" if ratio else "─"
        cards.append(f"""<div class="kpi" style="border:1px solid {bal_color}">
          <div class="kpi-label">🔻 공매도 잔고</div>
          <div class="kpi-value mono" style="color:{bal_color}">{ratio_str}</div>
          <div class="kpi-sub">잔고 5일 변화: <b>{chg:+.1f}%</b></div>
          <div class="kpi-sub" style="margin-top:4px">{'🚨 급증 (분배 의심)' if short.get('alert') else '정상'}</div>
        </div>""")
    else:
        cards.append(f"""<div class="kpi">
          <div class="kpi-label">공매도 잔고</div>
          <div class="kpi-value" style="color:#666;font-size:0.95em">데이터 없음</div>
          <div class="kpi-sub">{short.get('error', '')[:40]}</div>
        </div>""")

    # 외국인 보유율
    if foreign.get("available"):
        trend = foreign.get("trend", "중립")
        trend_color = {"매집":"#10b981","이탈":"#ef4444","중립":"#9ca3af"}.get(trend, "#9ca3af")
        delta = foreign.get("delta_20d", 0)
        cards.append(f"""<div class="kpi" style="border:1px solid {trend_color}">
          <div class="kpi-label">👥 외국인 보유율</div>
          <div class="kpi-value mono" style="color:{trend_color}">{foreign.get('last_rate', 0):.2f}%</div>
          <div class="kpi-sub">20일 변화: <b style="color:{trend_color}">{delta:+.2f}%p</b></div>
          <div class="kpi-sub" style="margin-top:4px;color:{trend_color};font-weight:600">{trend}</div>
        </div>""")
    else:
        cards.append(f"""<div class="kpi">
          <div class="kpi-label">외국인 보유율</div>
          <div class="kpi-value" style="color:#666;font-size:0.95em">데이터 없음</div>
          <div class="kpi-sub">{foreign.get('error', '')[:40]}</div>
        </div>""")

    # 기관 세부
    if inst.get("available"):
        flows = inst.get("flows_5d_amt", {})
        smart = inst.get("smart_money_5d", 0)
        smart_color = "#10b981" if smart > 0 else "#ef4444"
        # 핵심 그룹 강조
        rows = ""
        order = ["연기금등", "사모", "외국인", "투신", "보험", "금융투자", "은행", "개인"]
        for g in order:
            if g not in flows: continue
            v = flows[g]
            c = "#10b981" if v > 0 else "#ef4444" if v < 0 else "#888"
            tag = ""
            if g in ("연기금등", "사모"):
                tag = '<span class="label-tag" style="background:#1e40af;color:#fff">스마트</span> '
            rows += f'<tr><td>{tag}{g}</td><td class="mono" style="text-align:right;color:{c}">{v:+,.1f}억</td></tr>'

        cards.append(f"""<div class="kpi" style="border:1px solid {smart_color}; flex:1.5">
          <div class="kpi-label">🏛️ 기관 세부 5일 누적 (억 원)</div>
          <div class="kpi-value mono" style="color:{smart_color};font-size:1.1em">스마트머니 {smart:+,.1f}억</div>
          <div class="kpi-sub">연기금+사모+외국인 합계</div>
          <table class="table-compact" style="margin-top:8px;font-size:0.78em">{rows}</table>
        </div>""")
    else:
        cards.append(f"""<div class="kpi" style="flex:1.5">
          <div class="kpi-label">기관 세부 분류</div>
          <div class="kpi-value" style="color:#666;font-size:0.95em">데이터 없음</div>
          <div class="kpi-sub">{inst.get('error', '')[:40]}</div>
        </div>""")

    return f"""<div class="card">
      <h2>KRX 추가 데이터 — 매집/이탈 직접 시그널</h2>
      <p class="desc">
        🔻 공매도 잔고 급증 = 큰손이 하락 베팅 ·
        👥 외국인 보유율 변화 = 진짜 매집/이탈 (보유율은 거래원과 다름) ·
        🏛️ 연기금+사모+외국인 = 스마트머니 직접 추적
      </p>
      <div style="display:flex;gap:12px;flex-wrap:wrap">{''.join(cards)}</div>
    </div>"""


# ──────────────────────────────────────────────────────────
# DETAIL PAGE
# ──────────────────────────────────────────────────────────
def render_detail(r):
    df = r["df"]
    df_idx = df.index.strftime("%Y-%m-%d").tolist()
    def S(c): return [clean(v) for v in df[c]] if c in df.columns else [None]*len(df)

    # 차트 시그널 마커
    sig_x, sig_y, sig_hover, sig_color, sig_symbol, sig_size = [], [], [], [], [], []
    for s in r["bt"]:
        d = s["date"]
        if d not in df_idx: continue
        sig_x.append(d)
        sig_y.append(clean(df["close"].iloc[df_idx.index(d)]))
        confirmed = s.get("confirmed")
        if s["grade"] == "매도강추":
            color = "#ef4444"; sz = 16 if confirmed else 12
        elif s["grade"] == "매도주의":
            color = "#f59e0b"; sz = 14 if confirmed else 11
        else:
            color = "#9ca3af"; sz = 11 if confirmed else 9
        symbol = "star" if s.get("divergence") else ("triangle-down" if confirmed else "circle-open")
        sig_color.append(color); sig_size.append(sz); sig_symbol.append(symbol)
        r5  = f"{s['ret5']:+.1f}%"  if s["ret5"]  is not None else "집계중"
        r10 = f"{s['ret10']:+.1f}%" if s["ret10"] is not None else "집계중"
        r20 = f"{s['ret20']:+.1f}%" if s["ret20"] is not None else "집계중"
        sig_hover.append(
            f"<b>{d} {s['grade']} (점수:{s['score']})</b><br>"
            f"행동: {s.get('action', '─')}<br>"
            f"국면: {REGIME_LABEL.get(s.get('regime', 0), '?')} · 신고가권: {s.get('near_high', 0)}%<br>"
            + ("<b style='color:#fb7185'>★ 다이버전스</b><br>" if s.get("divergence") else "")
            + ("<b style='color:#fbbf24'>🚨 분배</b><br>" if s.get("dist_pattern") else "")
            + "─────────<br>"
            + "<br>".join(s["reasons"][:4])
            + f"<br>─────────<br>+5일: {r5}  +10일: {r10}  +20일: {r20}"
        )

    # 매수 시그널 마커 (다른 색)
    for s in r["buy_signals"]:
        d = s["date"]
        if d not in df_idx: continue
        sig_x.append(d)
        sig_y.append(clean(df["close"].iloc[df_idx.index(d)]))
        sig_color.append("#10b981"); sig_size.append(12)
        sig_symbol.append("triangle-up")
        sig_hover.append(
            f"<b>{d} {s['grade']} (점수:{s['score']})</b><br>"
            f"행동: {s.get('action', '─')}<br>"
            + "<br>".join(s["reasons"])
        )

    # 추세 국면 배경
    regime_shapes = []
    if "regime" in df.columns:
        rlist = df["regime"].tolist()
        prev_r, start = None, None
        for i, rv in enumerate(rlist):
            r_int = int(rv) if not (isinstance(rv, float) and math.isnan(rv)) else 0
            if r_int != prev_r:
                if prev_r and prev_r != 0 and start is not None:
                    regime_shapes.append({
                        "type":"rect","xref":"x","yref":"paper",
                        "x0":df_idx[start],"x1":df_idx[i],"y0":0,"y1":1,
                        "fillcolor":REGIME_COLOR_BG[prev_r],"line":{"width":0},"layer":"below"})
                start = i; prev_r = r_int
        if prev_r and prev_r != 0 and start is not None:
            regime_shapes.append({
                "type":"rect","xref":"x","yref":"paper",
                "x0":df_idx[start],"x1":df_idx[-1],"y0":0,"y1":1,
                "fillcolor":REGIME_COLOR_BG[prev_r],"line":{"width":0},"layer":"below"})

    chart_data = {
        "dates": df_idx,
        "close": S("close"), "ma20": S("ma20"), "ma60": S("ma60"),
        "fr20": S("foreign_ratio_20d"), "ir20": S("inst_ratio_20d"),
        "rr5":  S("retail_ratio_5d"),   "sr20": S("smart_net_ratio_20d"),
        "li5":  S("large_inst_ratio_5d"),
        "near_high": S("near_high"),
        "f_amt": S("foreign_amt"), "i_amt": S("inst_amt"), "r_amt": S("retail_amt"),
        "sm_amt20": S("smart_net_amt_20d"),
        "obv": S("obv"), "obv_ma20": S("obv_ma20"),
        "cmf": S("cmf"), "mfi": S("mfi"),
        "obv_bear_dates": [df_idx[i] for i, v in enumerate(S("obv_diverg_bear")) if v],
        "obv_bull_dates": [df_idx[i] for i, v in enumerate(S("obv_diverg_bull")) if v],
        "regime_shapes": regime_shapes,
        "sig_x": sig_x, "sig_y": [clean(v) for v in sig_y],
        "sig_hover": sig_hover, "sig_color": sig_color,
        "sig_symbol": sig_symbol, "sig_size": sig_size,
    }
    cd_json = json.dumps(chart_data, ensure_ascii=False)

    # 현재 상태 KPI
    last = df.iloc[-1]
    last_rgm = int(last.get("regime", 0)) if "regime" in df.columns else 0
    last_action = "HOLD"
    if r["signals"]:
        last_action = r["signals"][-1].get("action", "HOLD")
    elif r["buy_signals"]:
        last_action = r["buy_signals"][-1].get("action", "HOLD")

    drop_color = "ret-up" if r["drop_from_peak"] < -5 else "mono"
    sm20 = last.get("smart_net_ratio_20d", 0) or 0
    li5  = last.get("large_inst_ratio_5d", 0) or 0
    fr5  = last.get("foreign_ratio_5d", 0) or 0
    breadth = last.get("foreign_breadth_5d", 0) or 0

    kpi_html = f"""<div class="grid4">
      <div class="kpi">
        <div class="kpi-label">현재 가격</div>
        <div class="kpi-value mono">{r['last_price']:,.0f}원</div>
        <div class="kpi-sub {drop_color}">고점({r['peak_price']:,.0f}원) 대비 {r['drop_from_peak']:+.1f}%</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">현재 국면</div>
        <div class="kpi-value regime-{last_rgm}">{REGIME_LABEL[last_rgm]}</div>
        <div class="kpi-sub">신고가 거리 {(last.get('near_high', 0) or 0)*100:.0f}%</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">스마트머니 20일</div>
        <div class="kpi-value mono" style="color:{'#ef4444' if sm20<0 else '#10b981'}">{sm20:+.1f}%</div>
        <div class="kpi-sub">대형기관 5일 {li5:+.1f}%</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">권고 행동</div>
        <div class="kpi-value"><span class="action-badge {action_class(last_action)}">{last_action}</span></div>
        <div class="kpi-sub">외인 breadth {int(breadth)}, fr5 {fr5:+.1f}%</div>
      </div>
    </div>"""

    # 시그널 테이블
    sig_rows_html = ""
    for s in reversed(r["bt"][-50:]):
        gc = grade_class(s["grade"])
        ac = action_class(s.get("action", ""))
        rgm_label = REGIME_LABEL.get(s.get("regime", 0), "?")
        def fmt(rt):
            if rt is None: return "─", ""
            return f"{rt:+.1f}%", "ret-down" if rt < 0 else "ret-up"
        r5,c5  = fmt(s["ret5"])
        r10,c10 = fmt(s["ret10"])
        r20,c20 = fmt(s["ret20"])
        confirmed_b = ""
        if s.get("confirmed"):
            confirmed_b = f'<span class="label-tag" style="background:#7f1d1d;color:#fff">컨펌 {s["days_to_confirm"]}일</span>'
        diverg_b = '<span style="color:#fb7185">★</span> ' if s.get("divergence") else ""
        dist_b = '<span style="color:#fbbf24">🚨</span> ' if s.get("dist_pattern") else ""
        synergy_b = ""
        if s.get("synergy", 0) > 0:
            synergy_b = f'<span class="label-tag" style="background:#1e40af;color:#fff">⚡{s["active_categories"]}</span> '

        sig_rows_html += f"""<tr>
          <td class="mono">{s['date']}</td>
          <td><span class="{gc}">{diverg_b}{dist_b}{s['grade']}</span></td>
          <td class="mono" style="text-align:center"><span class="{gc}">{s['score']}</span></td>
          <td style="text-align:center" class="regime-{s.get('regime', 0)}">{rgm_label}</td>
          <td><span class="action-badge {ac}">{s.get('action', '─')}</span></td>
          <td style="text-align:center">{synergy_b}{confirmed_b}</td>
          <td class="mono ret-down" style="text-align:right">{s.get('rr5', 0):+.1f}%</td>
          <td class="mono ret-up" style="text-align:right">{s.get('li5', 0):+.1f}%</td>
          <td style="text-align:center" class="mono {c5}">{r5}</td>
          <td style="text-align:center" class="mono {c10}">{r10}</td>
          <td style="text-align:center" class="mono {c20}">{r20}</td>
        </tr>"""

    # 거래원 매수/매도 TOP
    def render_brokers(items, color):
        rows = ""
        for i, (b, (n, g)) in enumerate(items, 1):
            gclass = {"foreign":"grp-foreign","inst":"grp-inst","retail":"grp-retail"}.get(g, "")
            glabel = {"foreign":"외국계","inst":"기관","retail":"개인"}.get(g, "?")
            ncolor = "ret-up" if n > 0 else "ret-down"
            rows += f"""<tr>
              <td class="mono" style="color:#666;text-align:center;width:36px">{i}</td>
              <td>{b}</td>
              <td class="{gclass}" style="font-size:0.82em">{glabel}</td>
              <td class="mono" style="text-align:right;color:{color}">{fmt_qty(n)}</td>
            </tr>"""
        return f'<table class="table-compact">{rows}</table>'

    # 분배 주도자
    dist_rows = ""
    for i, d in enumerate(r["distributors"], 1):
        gclass = {"foreign":"grp-foreign","inst":"grp-inst","retail":"grp-retail"}.get(d["group"], "")
        glabel = {"foreign":"외국계","inst":"기관","retail":"개인"}.get(d["group"], "?")
        dist_rows += f"""<tr>
          <td class="mono" style="color:#666;text-align:center">{i}</td>
          <td>{d['broker']}</td>
          <td class="{gclass}" style="font-size:0.82em">{glabel}</td>
          <td class="mono ret-up" style="text-align:right">{fmt_qty(d['pre_buy'])}</td>
          <td class="mono ret-down" style="text-align:right;font-weight:600">{fmt_qty(d['post_sell'])}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>{r['name']} ({r['code']}) — 거래원 수급</title>
<link rel="stylesheet" href="../assets/style.css">
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
</head>
<body>
<div class="container">

<div class="nav">
  <a href="../index.html">← 전체 대시보드</a>
  <a href="../profit_taking.html">💰 익절 타이밍</a>
  <a href="../status.html">📋 현재 상황</a>
  <a href="../trading_style.html">🎯 매매 스타일</a>
  <span style="color:#444;margin-left:auto">|</span>
  <span style="color:#888">{r['name']} ({r['code']}) · 데이터 {r['n_days']}일</span>
</div>

<h1>{r['name']} <span style="color:#666;font-size:0.65em">({r['code']})</span></h1>

{kpi_html}

<!-- 메인 차트 -->
<div class="card">
  <h2>주가 + MA + 시그널</h2>
  <p class="desc">배경: 추세 국면 (초록=상승 / 주황=분배의심 / 빨강=하락) · 마커: ★다이버전스 ▼매도시그널 ▲매수시그널</p>
  <div id="chart_main" style="height:440px"></div>
</div>

<!-- 비율 차트 -->
<div class="card">
  <h2>20일 수급 비율 (%)</h2>
  <p class="desc">스마트머니(노란선)가 0 아래로 내려가면서 신고가 거리(보라선)가 90~100% = 다이버전스 형성</p>
  <div id="chart_ratio" style="height:320px"></div>
</div>

<!-- 그룹별 일별 금액 -->
<div class="grid2">
  <div class="card">
    <h2>일별 그룹 순매수 (억 원)</h2>
    <div id="chart_amt" style="height:280px"></div>
  </div>
  <div class="card">
    <h2>20일 롤링 스마트머니 (억 원)</h2>
    <div id="chart_smart" style="height:280px"></div>
  </div>
</div>

<!-- 거래원 분석 -->
<div class="grid2">
  <div class="card">
    <h2>📈 매수 주도 TOP 10</h2>
    <p class="desc">데이터 기간 시작 ~ 가격 고점({r['peak_date']})까지 누적 순매수 1위~10위</p>
    {render_brokers(r['top_buyers_pre'], '#10b981')}
  </div>
  <div class="card">
    <h2>📉 매도 주도 TOP 10</h2>
    <p class="desc">가격 고점({r['peak_date']}) ~ 현재까지 누적 순매도 1위~10위</p>
    {render_brokers(r['top_sellers_post'], '#ef4444')}
  </div>
</div>

<!-- KRX 추가 데이터 -->
{render_extra_card(r['extra'])}

<!-- OBV / CMF / MFI 차트 -->
<div class="card">
  <h2>OBV · CMF · MFI — 매집/분배 다이버전스 탐지</h2>
  <p class="desc">
    <b>OBV(누적 거래량)</b>: 가격은 신고가지만 OBV는 정체 = 분배 다이버전스(약한 상승) ·
    <b>CMF</b>: -0.10 이하 분배, +0.10 이상 매집 ·
    <b>MFI</b>: 80↑ 과매수(분배 가능), 20↓ 과매도(매집 가능)
  </p>
  <div id="chart_obv" style="height:300px"></div>
  <div class="grid2" style="margin-top:14px">
    <div id="chart_cmf" style="height:240px"></div>
    <div id="chart_mfi" style="height:240px"></div>
  </div>
</div>

<!-- 분배 주도자 -->
{f'''<div class="card">
  <h2>★ 분배 주도자 — 상승에서 사고 하락에서 판 거래원</h2>
  <p class="desc">가장 영리한 매매. 외국계·대형 기관에 많을수록 "큰손이 분배했다"는 뚜렷한 증거.</p>
  <table class="table-compact">
    <tr><th style="width:36px">#</th><th>거래원</th><th>그룹</th>
      <th style="text-align:right">상승중<br>매수</th>
      <th style="text-align:right">하락중<br>매도</th></tr>
    {dist_rows}
  </table>
</div>''' if r['distributors'] else ''}

<!-- 시그널 시계열 -->
<div class="card">
  <h2>시그널 타임라인 (최근 50건)</h2>
  <p class="desc">★ 다이버전스 · 🚨 분배 패턴 · ⚡ 시너지(N개 카테고리 동시) · 컨펌 = 가격이 MA20/저점 깸</p>
  <table class="table-compact">
    <tr>
      <th>날짜</th><th>등급</th><th style="text-align:center">점수</th>
      <th>국면</th><th>행동 권고</th><th style="text-align:center">표식</th>
      <th style="text-align:right">개미5d</th>
      <th style="text-align:right">대형기관5d</th>
      <th style="text-align:center">+5일</th>
      <th style="text-align:center">+10일</th>
      <th style="text-align:center">+20일</th>
    </tr>
    {sig_rows_html}
  </table>
</div>

</div>

<script>
const D = {cd_json};
const BASE = {{
  paper_bgcolor:'#14171f', plot_bgcolor:'#14171f',
  font:{{color:'#bbb',size:11}},
  xaxis:{{gridcolor:'#1f2230',zeroline:false}},
  yaxis:{{gridcolor:'#1f2230'}},
  legend:{{orientation:'h',y:-0.18}},
  margin:{{t:10,b:55,l:65,r:55}},
  hovermode:'closest',
}};

// 메인 차트
Plotly.newPlot('chart_main', [
  {{x:D.dates,y:D.close,type:'scatter',mode:'lines',name:'종가',line:{{color:'#4fc3f7',width:2.2}}}},
  {{x:D.dates,y:D.ma20,type:'scatter',mode:'lines',name:'MA20',line:{{color:'rgba(241,196,15,0.7)',width:1.2}}}},
  {{x:D.dates,y:D.ma60,type:'scatter',mode:'lines',name:'MA60',line:{{color:'rgba(149,165,166,0.7)',width:1.2,dash:'dot'}}}},
  {{x:D.sig_x,y:D.sig_y,type:'scatter',mode:'markers',name:'시그널',
    marker:{{color:D.sig_color,size:D.sig_size,symbol:D.sig_symbol,line:{{color:'#fff',width:1}}}},
    hovertext:D.sig_hover,hoverinfo:'text',hoverlabel:{{bgcolor:'#14171f',bordercolor:'#444'}}}}
], {{...BASE, yaxis:{{...BASE.yaxis,title:'원'}}, shapes:D.regime_shapes, margin:{{t:10,b:55,l:75,r:10}}}}, {{responsive:true}});

// 비율 차트
const n = D.dates.length;
const thrLines = [
  {{type:'line',x0:D.dates[0],x1:D.dates[n-1],y0:{FOREIGN_RATIO_20D},y1:{FOREIGN_RATIO_20D},
    line:{{color:'rgba(46,204,113,0.4)',width:1,dash:'dot'}}}},
  {{type:'line',x0:D.dates[0],x1:D.dates[n-1],y0:{INST_RATIO_20D},y1:{INST_RATIO_20D},
    line:{{color:'rgba(52,152,219,0.4)',width:1,dash:'dot'}}}},
  {{type:'line',x0:D.dates[0],x1:D.dates[n-1],y0:0,y1:0,line:{{color:'#555',width:1}}}},
];
Plotly.newPlot('chart_ratio', [
  {{x:D.dates,y:D.fr20,type:'scatter',mode:'lines',name:'외국계 20일%',line:{{color:'#10b981',width:1.5}}}},
  {{x:D.dates,y:D.ir20,type:'scatter',mode:'lines',name:'기관 20일%',  line:{{color:'#3498db',width:1.5}}}},
  {{x:D.dates,y:D.li5, type:'scatter',mode:'lines',name:'대형기관 5일%',line:{{color:'#f97316',width:1.2,dash:'dash'}}}},
  {{x:D.dates,y:D.sr20,type:'scatter',mode:'lines',name:'스마트머니 20일%',
    line:{{color:'#fbbf24',width:2.2}},fill:'tozeroy',fillcolor:'rgba(251,191,36,0.07)'}},
  {{x:D.dates,y:D.near_high.map(v=>v?v*100:null),type:'scatter',mode:'lines',
    name:'신고가 거리(%)',line:{{color:'#a78bfa',width:1,dash:'dot'}},yaxis:'y2'}},
], {{...BASE,
  yaxis:{{...BASE.yaxis,title:'비율 (%)'}},
  yaxis2:{{title:'신고가(%)',overlaying:'y',side:'right',range:[0,105],gridcolor:'transparent'}},
  shapes:thrLines,
}}, {{responsive:true}});

// 일별 금액
Plotly.newPlot('chart_amt', [
  {{x:D.dates,y:D.f_amt,type:'bar',name:'외국계',marker:{{color:'#10b981',opacity:0.85}}}},
  {{x:D.dates,y:D.i_amt,type:'bar',name:'기관',  marker:{{color:'#3498db',opacity:0.85}}}},
  {{x:D.dates,y:D.r_amt,type:'bar',name:'개인',  marker:{{color:'#ef4444',opacity:0.85}}}},
], {{...BASE,barmode:'relative',
  yaxis:{{...BASE.yaxis,title:'억 원',zeroline:true,zerolinecolor:'#555'}},
}}, {{responsive:true}});

// 스마트머니 롤링
Plotly.newPlot('chart_smart', [
  {{x:D.dates,y:D.sm_amt20,type:'scatter',mode:'lines',name:'스마트머니 20일',
    line:{{color:'#fbbf24',width:2.5}},fill:'tozeroy',fillcolor:'rgba(251,191,36,0.07)'}},
], {{...BASE,
  yaxis:{{...BASE.yaxis,title:'억 원',zeroline:true,zerolinecolor:'#888',zerolinewidth:2}},
}}, {{responsive:true}});

// OBV (누적 거래량) — 가격과 함께 표시 (이중 y축)
Plotly.newPlot('chart_obv', [
  {{x:D.dates,y:D.close,type:'scatter',mode:'lines',name:'종가',
    line:{{color:'#4fc3f7',width:1.5}},yaxis:'y'}},
  {{x:D.dates,y:D.obv,type:'scatter',mode:'lines',name:'OBV (누적)',
    line:{{color:'#a78bfa',width:1.8}},yaxis:'y2'}},
  {{x:D.dates,y:D.obv_ma20,type:'scatter',mode:'lines',name:'OBV MA20',
    line:{{color:'rgba(167,139,250,0.4)',width:1,dash:'dot'}},yaxis:'y2'}},
  // 다이버전스 마커
  {{x:D.obv_bear_dates,y:D.obv_bear_dates.map(d=>D.close[D.dates.indexOf(d)]),
    type:'scatter',mode:'markers',name:'분배 다이버전스',
    marker:{{color:'#ef4444',size:10,symbol:'triangle-down'}},yaxis:'y'}},
  {{x:D.obv_bull_dates,y:D.obv_bull_dates.map(d=>D.close[D.dates.indexOf(d)]),
    type:'scatter',mode:'markers',name:'매집 다이버전스',
    marker:{{color:'#10b981',size:10,symbol:'triangle-up'}},yaxis:'y'}},
], {{...BASE,
  yaxis:{{...BASE.yaxis,title:'가격(원)'}},
  yaxis2:{{title:'OBV',overlaying:'y',side:'right',gridcolor:'transparent'}},
}}, {{responsive:true}});

// CMF
Plotly.newPlot('chart_cmf', [
  {{x:D.dates,y:D.cmf,type:'scatter',mode:'lines',name:'CMF (20일)',
    line:{{color:'#fbbf24',width:1.8}},fill:'tozeroy',fillcolor:'rgba(251,191,36,0.05)'}},
], {{...BASE,
  yaxis:{{...BASE.yaxis,title:'CMF',range:[-0.4,0.4],zeroline:true,zerolinecolor:'#555'}},
  shapes:[
    {{type:'line',x0:D.dates[0],x1:D.dates[D.dates.length-1],y0:{CMF_DIST_THRESH},y1:{CMF_DIST_THRESH},
      line:{{color:'rgba(239,68,68,0.4)',width:1,dash:'dot'}}}},
    {{type:'line',x0:D.dates[0],x1:D.dates[D.dates.length-1],y0:{CMF_ACCUM_THRESH},y1:{CMF_ACCUM_THRESH},
      line:{{color:'rgba(16,185,129,0.4)',width:1,dash:'dot'}}}},
  ],
  annotations:[
    {{x:D.dates[10],y:{CMF_ACCUM_THRESH},text:'매집',showarrow:false,yshift:8,font:{{color:'rgba(16,185,129,0.6)',size:10}}}},
    {{x:D.dates[10],y:{CMF_DIST_THRESH},text:'분배',showarrow:false,yshift:-8,font:{{color:'rgba(239,68,68,0.6)',size:10}}}},
  ],
}}, {{responsive:true}});

// MFI
Plotly.newPlot('chart_mfi', [
  {{x:D.dates,y:D.mfi,type:'scatter',mode:'lines',name:'MFI (14일)',
    line:{{color:'#a78bfa',width:1.8}}}},
], {{...BASE,
  yaxis:{{...BASE.yaxis,title:'MFI',range:[0,100]}},
  shapes:[
    {{type:'line',x0:D.dates[0],x1:D.dates[D.dates.length-1],y0:80,y1:80,
      line:{{color:'rgba(239,68,68,0.4)',width:1,dash:'dot'}}}},
    {{type:'line',x0:D.dates[0],x1:D.dates[D.dates.length-1],y0:20,y1:20,
      line:{{color:'rgba(16,185,129,0.4)',width:1,dash:'dot'}}}},
  ],
  annotations:[
    {{x:D.dates[10],y:80,text:'과매수',showarrow:false,yshift:8,font:{{color:'rgba(239,68,68,0.6)',size:10}}}},
    {{x:D.dates[10],y:20,text:'과매도',showarrow:false,yshift:-8,font:{{color:'rgba(16,185,129,0.6)',size:10}}}},
  ],
}}, {{responsive:true}});
</script>
</body>
</html>"""


def main():
    smap = load_json(STOCK_MAP_FILE, default={})
    if not os.path.isdir(FLOW_DIR):
        print(f"ERROR: {FLOW_DIR} 없음"); sys.exit(1)

    os.makedirs(STOCKS_DIR, exist_ok=True)

    stocks = sorted([
        unicodedata.normalize("NFC", d)
        for d in os.listdir(FLOW_DIR)
        if os.path.isdir(os.path.join(FLOW_DIR, d))
    ])
    print(f"[1] 종목 {len(stocks)}개 발견")

    results = []
    for name in stocks:
        info = smap.get(name, {})
        code = info.get("code")
        if not code:
            print(f"  [SKIP] {name} — 코드 없음")
            continue
        print(f"[2] 분석: {name} ({code})")
        r = analyze_one(name, code)
        if r:
            results.append(r)

    if not results:
        print("ERROR: 분석 가능한 종목 없음"); sys.exit(1)

    # 종합 백테스트 데이터
    all_bt = []
    for r in results:
        all_bt.extend(r["bt"])

    print(f"[3] 종합 백테스트:")
    overall = grade_summary(all_bt)
    for g in ["매도강추", "매도주의", "관망"]:
        if g not in overall: continue
        v = overall[g]
        avg10 = f"{v['avg_ret_10d']:+.1f}%" if v["avg_ret_10d"] is not None else "─"
        print(f"  [{g}] {v['count']}건  10일후하락:{v['hit_rate_10d']}%  평균10일:{avg10}")

    # 페이지 생성
    print(f"[4] HTML 생성:")
    overview_html = render_overview(results, all_bt)
    overview_path = os.path.join(DASHBOARD_DIR, "index.html")
    with open(overview_path, "w", encoding="utf-8") as f:
        f.write(overview_html)
    print(f"  ✓ {overview_path}")

    for r in results:
        detail_html = render_detail(r)
        path = os.path.join(STOCKS_DIR, f"{r['code']}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(detail_html)
        print(f"  ✓ {path}  ({r['name']})")

    print(f"\n[완료] dashboard/index.html 열어서 확인하세요.")


if __name__ == "__main__":
    main()
