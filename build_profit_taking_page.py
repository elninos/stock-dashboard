#!/usr/bin/env python3
"""익절 타이밍 분석 페이지.

평가이익 종목을 식별하고, 시그널 시스템 + 트레일링 스탑 + 분할 익절 룰로
"언제 얼마를 매도할지" 자동 판정.

dashboard/profit_taking.html
"""
import os, sys, warnings, json
from collections import defaultdict
from datetime import datetime, timedelta
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from signals.broker_flow import (
    load_stock_flow, build_timeseries, detect_signals, FOREIGN, RETAIL_HEAVY,
)
from signals.price_volume import add_price_volume_signals

OUT = os.path.join(BASE_DIR, "dashboard", "profit_taking.html")
FLOW_DIR = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-srshin614@gmail.com"
    "/내 드라이브/01.Claude/01.주식/daily_flow"
)
TODAY_STR = "20260424"
START_STR = "20250101"


def fmt_man(v):
    if abs(v) >= 1e8: return f"{v/1e8:+.2f}억"
    return f"{v/1e4:+,.0f}만"


def calculate_holdings(txs):
    """FIFO로 현재 보유 + 평단가 계산."""
    trades = [t for t in txs if t.get("type") in ("buy","sell")]
    pos = defaultdict(list)
    for t in trades:
        s = t["stock"]
        if t["type"] == "buy":
            pos[s].append({"qty": t["qty"], "price": t["price"], "date": t["date"]})
        else:
            remain = t["qty"]
            while remain > 0 and pos[s]:
                lot = pos[s][0]
                take = min(remain, lot["qty"])
                lot["qty"] -= take
                remain -= take
                if lot["qty"] <= 0:
                    pos[s].pop(0)

    holdings = []
    for s, lots in pos.items():
        qty = sum(l["qty"] for l in lots if l["qty"]>0)
        if qty <= 0: continue
        cost = sum(l["qty"]*l["price"] for l in lots if l["qty"]>0)
        first_buy = min(l["date"] for l in lots if l["qty"]>0)
        holdings.append({
            "stock": s, "qty": qty,
            "avg":   cost/qty,
            "cost":  cost,
            "first_buy": first_buy,
        })
    return holdings


def diagnose_position(stock_name, code, qty, avg, cost, first_buy):
    """단일 종목의 현재 상태 + 익절 권고 자동 판정.

    수급 우선 원칙:
      - 거래원 수급이 견조하면 이익률 무시하고 HOLD
      - 수급 약화 시그널 발동 시에만 익절
      - 이익률은 "어떤 강도로 익절할지" 결정용
    """
    from pykrx import stock as krx
    try:
        pdf = krx.get_market_ohlcv_by_date(START_STR, TODAY_STR, code)
        if len(pdf) == 0:
            return None
    except Exception:
        return None

    pdf_renamed = pdf.rename(columns={"시가":"open","고가":"high","저가":"low","종가":"close","거래량":"volume"})
    pdf_renamed = add_price_volume_signals(pdf_renamed)

    cur = float(pdf["종가"].iloc[-1])
    first_buy_dt = first_buy.replace("-", "")
    pdf_since = pdf[pdf.index.strftime("%Y%m%d") >= first_buy_dt]
    high_since_buy = float(pdf_since["종가"].max()) if len(pdf_since) > 0 else cur
    high_60 = float(pdf["종가"].rolling(60).max().iloc[-1])
    high_ref = max(high_since_buy, high_60)
    pnl = (cur - avg) * qty
    pnl_pct = (cur/avg - 1) * 100
    value = cur * qty
    from_high60 = (cur/high_ref - 1) * 100 if high_ref > 0 else 0
    from_high_all = (cur/float(pdf["종가"].max()) - 1) * 100

    # ── 거래원 수급 시그널 (있는 종목만)
    sig_state = "─"
    sig_reasons = []
    smart20 = None; smart5 = None; smart5_prev = None
    fr5 = None; breadth5 = None
    has_flow = False

    flow = load_stock_flow(stock_name, FLOW_DIR)
    if flow and len(flow) >= 25:
        try:
            df = build_timeseries(flow, pdf["종가"])
            has_flow = True
            sigs = detect_signals(df)
            if sigs:
                last = sigs[-1]
                from datetime import datetime as dt
                today = dt.strptime(TODAY_STR, "%Y%m%d")
                last_dt = dt.strptime(last["date"], "%Y-%m-%d")
                if (today - last_dt).days <= 10:
                    sig_state = last.get("action", last.get("grade", "─"))
                    sig_reasons = last.get("reasons", [])[:3]
            # 마지막 행 수급 메트릭
            last_row = df.iloc[-1]
            smart20 = float(last_row.get("smart_net_ratio_20d", 0) or 0)
            smart5  = float(last_row.get("smart_net_ratio_5d",  0) or 0)
            if len(df) >= 2:
                smart5_prev = float(df.iloc[-6].get("smart_net_ratio_5d", 0) or 0) if len(df) >= 6 else smart5
            fr5      = float(last_row.get("foreign_ratio_5d",   0) or 0)
            breadth5 = int(last_row.get("foreign_breadth_5d",  0) or 0)
        except Exception:
            pass

    # OBV/MFI 시그널 (모든 종목 가능)
    last_pv = pdf_renamed.iloc[-1]
    obv_bear = bool(last_pv.get("obv_diverg_bear", 0))
    obv_bull = bool(last_pv.get("obv_diverg_bull", 0))
    mfi = last_pv.get("mfi")
    cmf = last_pv.get("cmf")

    # 익절 권고 자동 판정 (수급 우선)
    action, action_class, urgency, primary_reason = decide_profit_action_v2(
        pnl_pct=pnl_pct,
        from_high=from_high60,
        sig_state=sig_state,
        sig_reasons=sig_reasons,
        smart20=smart20, smart5=smart5,
        fr5=fr5, breadth5=breadth5,
        obv_bear=obv_bear,
        mfi=float(mfi) if mfi is not None else None,
        cmf=float(cmf) if cmf is not None else None,
        has_flow=has_flow,
    )

    # 시뮬레이션: -10% 빠지면 평가이익 얼마 줄어드는지
    sim_drop = qty * cur * 0.10  # 10% 하락 시 손실
    sim_remain = pnl - sim_drop  # 그래도 남는 평가이익

    return {
        "stock": stock_name, "code": code, "qty": qty,
        "avg": avg, "cost": cost, "first_buy": first_buy,
        "cur": cur, "value": value,
        "pnl": pnl, "pnl_pct": pnl_pct,
        "high_ref": high_ref, "high_since_buy": high_since_buy,
        "from_high60": from_high60,
        "from_high_all": from_high_all,
        "sig_state": sig_state,
        "sig_reasons": sig_reasons,
        "obv_bear": obv_bear, "obv_bull": obv_bull,
        "mfi": float(mfi) if mfi is not None else None,
        "cmf": float(cmf) if cmf is not None else None,
        "smart20": smart20, "smart5": smart5,
        "fr5": fr5, "breadth5": breadth5,
        "has_flow": has_flow,
        "action": action, "action_class": action_class,
        "urgency": urgency,
        "primary_reason": primary_reason,
        "sim_drop_10pct": sim_drop,
        "sim_remain_after_10pct": sim_remain,
    }


def decide_profit_action_v2(pnl_pct, from_high, sig_state, sig_reasons,
                              smart20, smart5, fr5, breadth5,
                              obv_bear, mfi, cmf, has_flow):
    """수급 우선 익절 판정.

    원칙: "큰손이 아직 매집 중이면 이익률 무관하게 HOLD"
          "큰손이 빠지기 시작하면 익절"

    수급 시그널이 우선이고, 이익률은 익절 강도(1/4 vs 1/2 vs 전량) 결정에만 사용.
    """
    # ─── 손실 영역 ───
    if pnl_pct < 10:
        if pnl_pct <= -15:
            return ("⛔ 손절 검토 (큰 손실)", "action-sell", 4, "손실 -15% 이상")
        if pnl_pct < 0:
            return ("─ 손절/홀드 결정", "action-hold", 0, "손실 진행 중")
        return ("─ 익절 분석 대상 아님", "action-hold", 0, "이익 부족")

    # ─── 분배 시그널 우선 (수급 시스템 결과) ───
    if sig_state and ("전량 매도" in sig_state or "다이버전스" in sig_state or "분배" in sig_state):
        # 분배 시그널 + 큰 이익 = 강한 익절
        if pnl_pct >= 100:
            return (f"🚨 1/2 익절 ({sig_state.split('(')[0].strip()})", "action-strong", 5,
                    "분배 시그널 + 큰 이익")
        elif pnl_pct >= 50:
            return (f"🔻 1/3 익절 ({sig_state.split('(')[0].strip()})", "action-sell", 4,
                    "분배 시그널 + 중이익")
        else:
            return (f"⚠️ 1/4 익절 ({sig_state.split('(')[0].strip()})", "action-partial", 3,
                    "분배 시그널")

    # ─── 스마트머니 이탈 시작 ───
    if has_flow and smart20 is not None:
        # 스마트머니 20일 + 5일 모두 음수 = 본격 이탈
        if smart20 < -3 and smart5 is not None and smart5 < -3:
            if pnl_pct >= 50:
                return ("🔻 1/3 익절 (스마트머니 본격 이탈)", "action-sell", 4,
                        f"외인+기관 20일 {smart20:+.1f}% / 5일 {smart5:+.1f}%")
            else:
                return ("⚠️ 1/4 익절 (스마트머니 이탈)", "action-partial", 2,
                        f"외인+기관 20일 {smart20:+.1f}%")
        # 스마트머니 5일이 막 음전환
        if smart5 is not None and smart5 < -1 and smart20 < 0:
            if pnl_pct >= 100:
                return ("⚠️ 1/4 익절 (수급 약화 시작)", "action-partial", 2,
                        f"외인+기관 5일 {smart5:+.1f}% 음전환")

    # ─── OBV 분배 다이버전스 (수급 데이터 없을 때 보조) ───
    if obv_bear and pnl_pct >= 50:
        return ("🔻 1/3 익절 (OBV 분배 다이버전스)", "action-sell", 3,
                "가격 신고가 but 거래량 누적 ↓")

    # ─── 트레일링 스탑 (큰 이익 종목 보호) ───
    if pnl_pct >= 50 and from_high <= -15:
        return ("🚨 1/2 익절 (트레일링 -15%)", "action-strong", 4,
                f"보유 신고가 대비 {from_high:.1f}% 이탈")

    # ─── MFI 극단 과매수 (큰 이익 + 모멘텀 정점) ───
    if mfi is not None and mfi >= 80 and pnl_pct >= 100:
        return ("⚠️ 1/4 익절 (MFI 과매수 정점)", "action-partial", 2,
                f"MFI {mfi:.0f} (모멘텀 정점)")

    # ─── HOLD 우선 — 수급 견조 ───
    # 외국계 컨센서스 (강한 매수)
    if has_flow and breadth5 is not None and breadth5 >= 5 and fr5 is not None and fr5 > 0:
        return ("🟢 HOLD (외국계 컨센서스 매수)", "action-hold", 0,
                f"외인 5일 매수자 +{breadth5} / 비율 {fr5:+.1f}%")

    # 스마트머니 양수 (큰손 매집 중)
    if has_flow and smart20 is not None and smart20 > 0:
        return ("🟢 HOLD (스마트머니 매집 중)", "action-hold", 0,
                f"외인+기관 20일 {smart20:+.1f}%")

    # 신고가권 + 큰 이익
    if pnl_pct >= 50 and from_high >= -5:
        return ("🟢 HOLD (신고가권 큰 이익)", "action-hold", 0,
                f"신고가 대비 {from_high:+.1f}%")

    # OBV 매집 다이버전스 (반등 가능)
    if cmf is not None and cmf >= 0.10:
        return ("🟢 HOLD (CMF 매집 신호)", "action-hold", 0,
                f"CMF {cmf:+.2f} (매집 진행)")

    # 큰 이익 + 추세 약화 (수급 데이터 없을 때만)
    if not has_flow and pnl_pct >= 100 and from_high <= -10:
        return ("⚠️ 1/4 익절 (큰 이익 + 추세 약화)", "action-partial", 2,
                f"신고가 대비 {from_high:.1f}%")

    # 기본 HOLD (이익 진행 중, 시그널 없음)
    return ("🟢 HOLD (이익 진행 중)", "action-hold", 0, "특이 시그널 없음")


def decide_profit_action(pnl_pct, from_high60, sig_state, obv_bear, mfi, cmf):
    """익절 행동 자동 판정.

    1. 평가이익 +20% 미만은 익절 분석 대상 아님 (별도 손절/관망)
    2. 큰 이익(+50%↑)에서만 트레일링 스탑 적용 (-15% 이탈 시)
    3. 시그널 우선순위: 분배 → 다이버전스 → 트레일링 → MFI → HOLD
    """
    # 익절 대상 아님 (이익 부족)
    if pnl_pct < 20:
        if pnl_pct <= -15:
            return ("⛔ 손절 검토 (큰 손실)", "action-sell", 4)
        if pnl_pct < 0:
            return ("─ 손절/홀드 결정 필요", "action-hold", 0)
        return ("─ 익절 분석 대상 아님 (이익 부족)", "action-hold", 0)

    # 분배 시그널 (시그널 시스템) — 가장 강한 매도 신호
    if sig_state and ("전량 매도" in sig_state or "다이버전스" in sig_state or "분배" in sig_state):
        return (f"🚨 1/2~전량 익절 ({sig_state.split('(')[0].strip()})", "action-strong", 5)

    # 큰 이익 + 트레일링 스탑 (-15%)
    if pnl_pct >= 50 and from_high60 <= -15:
        return ("🚨 전량 익절 (트레일링 -15%)", "action-strong", 5)

    # 매우 큰 이익 + 추세 약화 (-10% ~ -15%)
    if pnl_pct >= 100 and from_high60 <= -10:
        return ("🔻 1/2 익절 (큰 이익 + 추세 약화)", "action-strong", 4)

    # OBV 분배 다이버전스 (큰 이익에서만)
    if obv_bear and pnl_pct >= 50:
        return ("🔻 1/3 익절 (OBV 분배 다이버전스)", "action-sell", 3)

    # MFI 과매수 + 큰 이익
    if mfi is not None and mfi >= 80 and pnl_pct >= 50:
        return ("⚠️ 1/3 익절 (MFI 과매수)", "action-partial", 2)

    # 큰 이익 + 약한 추세 약화
    if pnl_pct >= 100 and from_high60 <= -5:
        return ("⚠️ 1/4 익절 (큰 이익 + 약한 약세)", "action-partial", 2)

    # 매수 시그널 유지
    if sig_state and ("매수" in sig_state or "관심" in sig_state):
        return ("🟢 HOLD (외국계 컨센서스)", "action-hold", 0)

    # 신고가권 + 큰 이익 → HOLD
    if pnl_pct >= 50 and from_high60 >= -5:
        return ("🟢 HOLD (신고가권 큰 이익)", "action-hold", 0)

    # +50% 도달 → 첫 분할 익절 후보
    if pnl_pct >= 50:
        return ("🟡 1/4 익절 후보 (+50%)", "action-partial", 1)

    # +20~50%
    return ("🟢 HOLD (이익 진행 중)", "action-hold", 0)


def main():
    print("[1] 거래 데이터 로드")
    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    print("[2] 보유 종목 계산")
    holdings = calculate_holdings(txs)
    kor_holdings = []
    for h in holdings:
        info = smap.get(h["stock"], {})
        if info.get("nation") == "KOR" and info.get("code"):
            h["code"] = info["code"]
            kor_holdings.append(h)
    print(f"    KOR 보유 종목: {len(kor_holdings)}개")

    print("[3] 종목별 진단 (현재가 + 시그널)")
    results = []
    for h in kor_holdings:
        r = diagnose_position(h["stock"], h["code"], h["qty"], h["avg"], h["cost"], h["first_buy"])
        if r:
            results.append(r)
            print(f"    ✓ {h['stock']:<14} {r['pnl_pct']:+.1f}% {r['action'][:25]}")

    # 정렬: 긴급도 → 평가이익 순
    results.sort(key=lambda x: (-x["urgency"], -x["pnl"]))

    # 합계
    total_cost = sum(r["cost"] for r in results)
    total_value = sum(r["value"] for r in results)
    total_pnl = total_value - total_cost
    pnl_pct_total = (total_value / total_cost - 1) * 100 if total_cost > 0 else 0

    gainers = [r for r in results if r["pnl"] > 0]
    losers = [r for r in results if r["pnl"] <= 0]
    big_gain_50 = [r for r in gainers if r["pnl_pct"] >= 50]
    big_gain_100 = [r for r in gainers if r["pnl_pct"] >= 100]

    # 긴급 익절 후보 카운트
    urgent_5 = [r for r in results if r["urgency"] == 5]
    urgent_4 = [r for r in results if r["urgency"] == 4]
    urgent_3 = [r for r in results if r["urgency"] == 3]
    hold_0 = [r for r in results if r["urgency"] == 0]

    print(f"\n[4] 합계: 평가이익 {fmt_man(total_pnl)} ({pnl_pct_total:+.1f}%)")
    print(f"    긴급 익절: {len(urgent_5)+len(urgent_4)}개 / 1/3 익절: {len(urgent_3)}개 / HOLD: {len(hold_0)}개")

    # ───────────────── HTML ─────────────────
    print("[5] HTML 생성")

    # 종목별 카드/행
    rows_html = ""
    for r in results:
        u = r["urgency"]
        urgency_color = {5:"#ef4444",4:"#ef4444",3:"#f59e0b",2:"#fbbf24",1:"#a78bfa",0:"#10b981"}.get(u, "#6b7280")
        urgency_bg = {5:"#1a0d0d",4:"#1a0d0d",3:"#1a1410",2:"#1a1410",1:"#0d0d1a",0:"#0d1a14"}.get(u, "#181b23")
        pnl_color = "ret-down" if r["pnl"] > 0 else "ret-up"
        days_held = (datetime.strptime(TODAY_STR, "%Y%m%d") - datetime.strptime(r["first_buy"], "%Y-%m-%d")).days

        # MFI/CMF 배지
        badges = []
        if r["obv_bear"]:
            badges.append('<span class="label-tag" style="background:#7f1d1d;color:#fff">OBV 분배</span>')
        if r["mfi"] and r["mfi"] >= 80:
            badges.append(f'<span class="label-tag" style="background:#7c2d12;color:#fff">MFI {r["mfi"]:.0f}</span>')
        elif r["mfi"] and r["mfi"] <= 20:
            badges.append(f'<span class="label-tag" style="background:#064e3b;color:#fff">MFI {r["mfi"]:.0f}</span>')
        if r["cmf"] and r["cmf"] <= -0.10:
            badges.append('<span class="label-tag" style="background:#7f1d1d;color:#fff">CMF 분배</span>')

        sig_reasons_html = ""
        if r["sig_reasons"]:
            sig_reasons_html = "<br>".join(r["sig_reasons"][:2])
            sig_reasons_html = f'<div style="color:#888;font-size:0.78em;margin-top:4px">{sig_reasons_html}</div>'

        rows_html += f"""<tr style="background:{urgency_bg}">
          <td>
            <a href="stocks/{r['code']}.html" style="color:#4fc3f7;text-decoration:none"><b>{r['stock']}</b></a><br>
            <span style="color:#666;font-size:0.78em">{r['code']} · {days_held}일째</span>
          </td>
          <td class="mono" style="text-align:right">{r['qty']:,}</td>
          <td class="mono" style="text-align:right">{r['avg']:,.0f}</td>
          <td class="mono" style="text-align:right">{r['cur']:,.0f}</td>
          <td class="mono" style="text-align:right">{r['value']/1e8:.2f}억</td>
          <td class="mono {pnl_color}" style="text-align:right;font-weight:600">{fmt_man(r['pnl'])}<br><span style="font-size:0.85em">{r['pnl_pct']:+.1f}%</span></td>
          <td class="mono" style="text-align:center;color:{('#ef4444' if r['from_high60']<-5 else '#10b981')}">
            {r['from_high60']:+.1f}%
          </td>
          <td>{' '.join(badges) or '<span style="color:#666">─</span>'}</td>
          <td>
            <span class="action-badge {r['action_class']}" style="font-size:0.78em">{r['action']}</span>
            {sig_reasons_html}
          </td>
          <td class="mono ret-up" style="text-align:right;font-size:0.85em">{fmt_man(r['sim_drop_10pct'])}</td>
        </tr>"""

    # 전체 평가이익이 -10% 빠지면
    sim_total_drop = total_value * 0.10

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>익절 타이밍 분석</title>
<link rel="stylesheet" href="assets/style.css">
<style>
.kpi-strip {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }}
.kpi-strip .kpi-mini {{
  flex: 1; min-width: 150px; background: #181b23; border-radius: 8px;
  padding: 14px; text-align: center;
}}
.kpi-strip .num {{ font-size: 1.6em; font-weight: 700; color: #fff; }}
.kpi-strip .lbl {{ font-size: 0.78em; color: #888; margin-top: 4px; }}
.kpi-strip .sub {{ font-size: 0.78em; color: #6b7280; margin-top: 2px; }}
.rule-card {{
  background: #181b23; border-radius: 10px;
  padding: 14px 18px; margin-bottom: 10px;
  border-left: 4px solid #4fc3f7;
}}
.rule-card .rule-num {{ font-size: 0.85em; color: #4fc3f7; font-weight: 600; margin-bottom: 4px; }}
.rule-card .rule-title {{ font-weight: 600; margin-bottom: 4px; font-size: 0.95em; }}
.rule-card .rule-detail {{ color: #aaa; font-size: 0.84em; line-height: 1.7; }}
.urgent-banner {{
  padding: 14px 18px;
  border-radius: 10px;
  margin-bottom: 14px;
  font-size: 0.92em;
}}
.urgent-banner.danger {{ background: linear-gradient(135deg, #1a0d0d 0%, #14171f 100%); border:2px solid #ef4444; }}
.urgent-banner.warn {{ background: linear-gradient(135deg, #1a1410 0%, #14171f 100%); border:2px solid #f59e0b; }}
.urgent-banner.good {{ background: linear-gradient(135deg, #0d1a14 0%, #14171f 100%); border:2px solid #10b981; }}
table.holdings td {{ padding: 10px 8px; }}
</style>
</head>
<body>
<div class="container">

<div class="nav">
  <a href="index.html">📊 전체 대시보드</a>
  <a href="status.html">📋 현재 상황</a>
  <a href="trading_style.html">🎯 매매 스타일</a>
  <a href="profit_taking.html" class="active">💰 익절 타이밍</a>
</div>

<h1>💰 익절 타이밍 분석</h1>
<p class="subtitle">"평가이익 ≠ 실현이익" — 안 팔면 0원 · 시그널 시스템 + 트레일링 룰로 자동 권고</p>

<!-- 핵심 KPI -->
<div class="kpi-strip">
  <div class="kpi-mini">
    <div class="num" style="color:#10b981">{fmt_man(total_pnl)}</div>
    <div class="lbl">현재 평가이익</div>
    <div class="sub">{pnl_pct_total:+.1f}% (총 평가 {total_value/1e8:.1f}억)</div>
  </div>
  <div class="kpi-mini">
    <div class="num">{len(big_gain_100)}</div>
    <div class="lbl">+100%↑ 종목</div>
    <div class="sub">+50%↑: {len(big_gain_50)}개</div>
  </div>
  <div class="kpi-mini">
    <div class="num" style="color:{'#ef4444' if len(urgent_5)+len(urgent_4)>0 else '#10b981'}">{len(urgent_5)+len(urgent_4)}</div>
    <div class="lbl">🚨 긴급 익절</div>
    <div class="sub">전량 또는 1/2 매도 권고</div>
  </div>
  <div class="kpi-mini">
    <div class="num" style="color:#f59e0b">{len(urgent_3)+len([r for r in results if r['urgency']==2])}</div>
    <div class="lbl">⚠️ 부분 익절</div>
    <div class="sub">1/3~1/4 매도 권고</div>
  </div>
  <div class="kpi-mini">
    <div class="num" style="color:#10b981">{len(hold_0)}</div>
    <div class="lbl">🟢 HOLD</div>
    <div class="sub">시그널 없음, 추세 견조</div>
  </div>
  <div class="kpi-mini">
    <div class="num ret-up">{fmt_man(sim_total_drop)}</div>
    <div class="lbl">전체 -10% 시</div>
    <div class="sub">평가이익 {fmt_man(total_pnl - sim_total_drop)} 잔여</div>
  </div>
</div>

<!-- 긴급 알림 -->
{f'<div class="urgent-banner danger"><b>🚨 즉시 익절 검토 ({len(urgent_5)+len(urgent_4)}종목):</b> ' + ', '.join(r['stock'] for r in urgent_5+urgent_4) + '</div>' if urgent_5 or urgent_4 else ''}
{f'<div class="urgent-banner warn"><b>⚠️ 부분 익절 후보 ({len(urgent_3)}종목):</b> ' + ', '.join(r['stock'] for r in urgent_3) + '</div>' if urgent_3 else ''}

<!-- 익절 룰 -->
<div class="card">
  <h2>🎯 익절 룰 (Profit-Taking Rules)</h2>
  <div class="callout">
    <b>"평가이익이 많다"는 함정.</b> 매도하지 않으면 시장 변동에 그대로 노출됩니다.<br>
    <b>이익을 단계적으로 실현하면서 일부는 추세에 태우는 것</b>이 익절 시스템의 목표.
  </div>

  <div class="rule-card" style="border-left-color:#a78bfa">
    <div class="rule-num">RULE 1 · 도달 기반 분할 익절</div>
    <div class="rule-title">💰 +50% / +100% / +200% 도달 시 1/4씩 익절</div>
    <div class="rule-detail">
      이익이 클수록 변동성도 큼 → 단계별로 잠금. 마지막 1/4은 추세에 태움.<br>
      <b>실행:</b> 한 번에 다 팔지 말고 4번에 나눠서. "원금 + α 회수" 다음에 추세 보유.
    </div>
  </div>

  <div class="rule-card" style="border-left-color:#fbbf24">
    <div class="rule-num">RULE 2 · 분배 시그널 강제 익절</div>
    <div class="rule-title">🚨 다이버전스 / 분배 패턴 / 거래량 다이버전스 발동 시 1/3 익절</div>
    <div class="rule-detail">
      가격은 신고가지만 큰손이 빠지는 중 = 곧 빠질 가능성 큼. 즉시 부분 익절.<br>
      <b>지표:</b> OBV 분배 다이버전스, MFI ≥ 80, CMF ≤ -0.10, 외국계+기관 20일 음전환
    </div>
  </div>

  <div class="rule-card" style="border-left-color:#ef4444">
    <div class="rule-num">RULE 3 · 트레일링 스탑 (이익 잠금)</div>
    <div class="rule-title">🔻 60일 신고가 -10% 이탈 시 즉시 1/2~전량 익절</div>
    <div class="rule-detail">
      추세 깨짐 컨펌. 평가이익이 크다면 주가가 -10% 빠질 때 절대 금액 손실이 큼.<br>
      <b>예시:</b> +200% 종목이 -10% 빠지면 평가이익이 +170%로 줄어듬 (체감 큼)
    </div>
  </div>

  <div class="rule-card" style="border-left-color:#10b981">
    <div class="rule-num">RULE 4 · HOLD 조건</div>
    <div class="rule-title">🟢 외국계 컨센서스 매수 + 신고가권 유지 + 분배 시그널 없음</div>
    <div class="rule-detail">
      "절대 안 판다"가 아니라 "지금은 안 판다". 추세가 살아있는 동안만 보유.<br>
      매일 시그널 재평가하고, RULE 2/3 발동 즉시 분할 익절로 전환.
    </div>
  </div>
</div>

<!-- 종목별 익절 권고 테이블 -->
<div class="card">
  <h2>📋 종목별 익절 권고 (긴급도 순)</h2>
  <p class="desc">
    🚨 즉시 익절 → 🔻 1/3 익절 → ⚠️ 1/4 익절 → 🟡 첫 익절 후보 → 🟢 HOLD<br>
    종목명 클릭 시 상세 페이지(daily_flow 데이터 있는 종목)
  </p>
  <table class="holdings">
    <tr>
      <th>종목</th>
      <th style="text-align:right">수량</th>
      <th style="text-align:right">평단</th>
      <th style="text-align:right">현재가</th>
      <th style="text-align:right">평가금액</th>
      <th style="text-align:right">평가이익</th>
      <th style="text-align:center">60일고점<br>이격</th>
      <th>분배 신호</th>
      <th>권고 행동</th>
      <th style="text-align:right">-10% 시<br>손실</th>
    </tr>
    {rows_html}
  </table>
</div>

<!-- 시뮬레이션 -->
<div class="card">
  <h2>📊 익절 시나리오 비교</h2>
  <p class="desc">현재 평가이익 {fmt_man(total_pnl)}을 어떻게 잠그느냐에 따른 결과 시뮬레이션</p>
  <div class="grid3">
    <div class="kpi" style="border:1px solid #ef4444">
      <div class="kpi-label" style="color:#ef4444">시나리오 A: 안 팔고 -20% 시장 하락</div>
      <div class="kpi-value mono">{fmt_man(total_pnl - total_value*0.20)}</div>
      <div class="kpi-sub">평가이익 {(total_pnl - total_value*0.20)/total_pnl*100:.0f}% 잔존</div>
    </div>
    <div class="kpi" style="border:1px solid #f59e0b">
      <div class="kpi-label" style="color:#f59e0b">시나리오 B: 1/3 익절 후 -20% 하락</div>
      <div class="kpi-value mono">{fmt_man(total_pnl/3 + (total_pnl - total_value*0.20)*2/3)}</div>
      <div class="kpi-sub">분할 익절로 손실 완충</div>
    </div>
    <div class="kpi" style="border:1px solid #10b981">
      <div class="kpi-label" style="color:#10b981">시나리오 C: 시그널 발동 종목만 익절</div>
      <div class="kpi-value mono">+α</div>
      <div class="kpi-sub">현재 긴급/부분 익절 {len(urgent_5)+len(urgent_4)+len(urgent_3)}종목 매도</div>
    </div>
  </div>
</div>

<!-- 매매 스타일 변화 제안 -->
<div class="card">
  <h2>🔄 매매 스타일 진단 — 손절 → 익절 중심으로</h2>
  <div class="callout warn">
    <b>새로운 진단:</b> 사용자의 진짜 약점은 "손절 못함"이 아니라 <b>"익절 못함"</b>.<br>
    평가이익 +4.88억 / 실현이익은 그보다 훨씬 적을 가능성. 안 팔면 시장 변동에 그대로 노출.
  </div>
  <ul class="reason-list" style="line-height:2">
    <li><b>이전 진단의 오류:</b> 분할매수가 손실의 원인이라고 봤지만, 실제로는 분할매수 → 평가이익 큰 종목 유지 = 좋은 결과</li>
    <li><b>진짜 약점:</b> 큰 평가이익 종목(+100%↑) 발생해도 실현 안 함 → 시장 변동 시 평가이익 증발 위험</li>
    <li><b>해결책:</b> 익절 시그널 자동화 + 단계적 분할 익절 (RULE 1~4 적용)</li>
    <li><b>지금 당장 할 일:</b> 위 테이블의 🚨/🔻 종목 검토 → 1/3 또는 1/2 매도 검토</li>
  </ul>
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
