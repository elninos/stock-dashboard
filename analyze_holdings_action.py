#!/usr/bin/env python3
"""보유 종목 행동 권고 — 매수/HOLD/매도 종합 진단.

분석 대상: 보유 종목 (KIS API 거래원 시계열 + 가격 + 사용자 평가이익)

매도 시그널:
  - 매수 주도자 → 매도 전환 (이미 분석)
  - 가격 트레일링 스탑

매수 시그널 (NEW):
  - 외국계 컨센서스 (3+ 외국계 동반 매수)
  - 매수 주도자 5일 연속 동일 (강한 매수 의지)
  - 신규 외국계 진입 (이전 20일 거래 X → 5일 매수)
  - 매수 우세 + 가격 조정 (저점 매수 기회)

행동 권고:
  📈 추가매수: 매수 시그널 강함 + 평가이익 적당 (조정 중)
  🟢 HOLD: 매수 지속 + 매도 시그널 약함 + 신고가 근처
  🟡 부분 익절: 매도 시그널 발동 + 큰 평가이익
  🚨 적극 매도: 강한 매도 시그널 + 외국계 분배

출력: dashboard/holdings_action.html
"""
import os, sys, warnings, json, time
from datetime import datetime, timedelta
from collections import defaultdict
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from signals.kis_member_daily import (
    build_broker_mapping, fetch_all_brokers_daily,
    aggregate_to_dataframe,
)
from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from pykrx import stock as krx
import pandas as pd

OUT = os.path.join(BASE_DIR, "dashboard", "holdings_action.html")

FOREIGN_KW = ["JP모간","모간","골드만","메릴린치","UBS","CLSA","씨티","BNP",
                "노무라","맥쿼리","다이와","외국계","홍콩상하이","도이치"]
RETAIL_KW = ["키움","토스","카카오","상상인"]
LARGE_KW = ["NH투자","KB증권","한국증권","한국투자","삼성증권","한화","미래에셋","신한","하나"]


def classify(name: str) -> str:
    if not name: return "small"
    for kw in FOREIGN_KW:
        if kw in name: return "foreign"
    for kw in RETAIL_KW:
        if kw in name: return "retail"
    for kw in LARGE_KW:
        if kw in name: return "large"
    return "small"


def detect_buy_signals(df, peak_date):
    """매수 시그널 탐지 (최근 20일)."""
    dates = sorted(df["date"].unique())
    if len(dates) < 10:
        return []

    recent_dates = dates[-20:]  # 최근 20일
    cur_dates = dates[-5:]      # 직전 5일

    cur_df = df[df["date"].isin(cur_dates)]

    # 1. 외국계 컨센서스 (5일간 매수한 외국계 거래원 수)
    foreign_buyers = set()
    for (code, name), net in cur_df.groupby(["broker_code","broker_name"])["net"].sum().items():
        if classify(name) == "foreign" and net > 0:
            foreign_buyers.add(name)

    # 2. 매수 주도자 일관성 (5일 TOP 3 동일)
    daily_top3 = []
    for d in cur_dates:
        d_df = df[df["date"] == d]
        top3 = set(d_df.nlargest(3, "net")[["broker_code"]].values.flatten().tolist())
        if len(top3) > 0:
            daily_top3.append(top3)
    consistent_count = 0
    if daily_top3:
        common = set.intersection(*daily_top3) if len(daily_top3) > 1 else set()
        consistent_count = len(common)

    # 3. 신규 외국계 진입
    if len(dates) >= 25:
        old_dates = dates[-25:-5]  # 직전 20일
        old_df = df[df["date"].isin(old_dates)]
        new_foreign = []
        for (code, name), net in cur_df.groupby(["broker_code","broker_name"])["net"].sum().items():
            if classify(name) != "foreign" or net <= 0: continue
            old_net = old_df[old_df["broker_code"] == code]["net"].sum()
            old_abs = old_df[old_df["broker_code"] == code]["buy"].sum() + \
                       old_df[old_df["broker_code"] == code]["sell"].sum()
            if old_abs < 1000:  # 거의 거래 안 함
                new_foreign.append(name)

    # 4. 외국계 vs 개미 (외국계가 사고 개미가 팔면 = 좋은 매수 신호)
    foreign_5d = sum(net for (c,n), net in cur_df.groupby(["broker_code","broker_name"])["net"].sum().items()
                       if classify(n) == "foreign")
    retail_5d = sum(net for (c,n), net in cur_df.groupby(["broker_code","broker_name"])["net"].sum().items()
                      if classify(n) == "retail")

    signals = []

    if len(foreign_buyers) >= 3 and foreign_5d > 0:
        signals.append({
            "type": "foreign_consensus",
            "score": 4 + min(len(foreign_buyers) - 3, 3),
            "label": f"외국계 컨센서스 ({len(foreign_buyers)}명 매수, {foreign_5d:+,}주)",
        })

    if consistent_count >= 2:
        signals.append({
            "type": "consistent_buyers",
            "score": 3,
            "label": f"매수 주도자 5일 연속 동일 ({consistent_count}명)",
        })

    if len(dates) >= 25 and new_foreign:
        signals.append({
            "type": "new_foreign",
            "score": 5,
            "label": f"신규 외국계 진입 ({', '.join(new_foreign[:3])})",
        })

    if foreign_5d > 0 and retail_5d < 0:
        signals.append({
            "type": "smart_buy_dumb_sell",
            "score": 4,
            "label": f"외국계 매수 + 개미 매도 (역분배: {foreign_5d:+,} ↔ {retail_5d:+,})",
        })

    return signals


def detect_recent_sell_signals(df, peak_date):
    """최근 20일 매도 시그널 (이전 multi_peaks와 동일 로직)."""
    dates = sorted(df["date"].unique())
    if len(dates) < 10: return []

    cur_dates = dates[-5:]
    prev_dates = dates[-10:-5]

    prev_df = df[df["date"].isin(prev_dates)]
    cur_df = df[df["date"].isin(cur_dates)]

    prev_5d = prev_df.groupby(["broker_code","broker_name"])["net"].sum()
    prev_top3 = prev_5d.nlargest(3)

    reversed_b = []
    for (code, name), prev_net in prev_top3.items():
        if prev_net <= 0: continue
        cur_net = cur_df[cur_df["broker_code"] == code]["net"].sum()
        if cur_net < 0:
            reversed_b.append((name, prev_net, cur_net, classify(name)))

    signals = []
    if reversed_b:
        n_rev = len(reversed_b)
        n_foreign = sum(1 for r in reversed_b if r[3] == "foreign")
        score = n_rev * 2 + (3 if n_rev >= 2 else 0) + (2 if n_foreign >= 1 else 0)
        signals.append({
            "type": "reversal",
            "score": score,
            "label": f"매수→매도 전환 ({n_rev}명, 외인:{n_foreign}) — {', '.join(r[0] for r in reversed_b)}",
            "details": reversed_b,
        })

    # 분배 패턴
    foreign_5d = sum(net for (c,n), net in cur_df.groupby(["broker_code","broker_name"])["net"].sum().items() if classify(n)=="foreign")
    large_5d = sum(net for (c,n), net in cur_df.groupby(["broker_code","broker_name"])["net"].sum().items() if classify(n)=="large")
    retail_5d = sum(net for (c,n), net in cur_df.groupby(["broker_code","broker_name"])["net"].sum().items() if classify(n)=="retail")

    if retail_5d > 0 and (foreign_5d + large_5d) < 0:
        signals.append({
            "type": "distribution",
            "score": 5,
            "label": f"분배 패턴 (개미 +{retail_5d:,} / 외인+대형 {foreign_5d+large_5d:+,})",
        })

    return signals


def get_user_position(stock_name, txs, last_price):
    """사용자 보유 평단가 + 평가이익."""
    s_trades = [t for t in txs if t["stock"] == stock_name and t["type"] in ("buy","sell")]
    holding = []
    for t in sorted(s_trades, key=lambda x: x["date"]):
        if t["type"] == "buy":
            holding.append({"qty": t["qty"], "price": t["price"]})
        else:
            remain = t["qty"]
            while remain > 0 and holding:
                lot = holding[0]
                take = min(remain, lot["qty"])
                lot["qty"] -= take
                remain -= take
                if lot["qty"] <= 0:
                    holding.pop(0)
    qty = sum(l["qty"] for l in holding)
    if qty <= 0: return None
    cost = sum(l["qty"] * l["price"] for l in holding)
    avg = cost / qty
    pnl_pct = (last_price / avg - 1) * 100
    return {
        "qty": qty, "avg": avg, "cost": cost,
        "value": qty * last_price,
        "pnl": qty * last_price - cost,
        "pnl_pct": pnl_pct,
    }


def decide_action(buy_score, sell_score, pnl_pct, near_high_pct):
    """행동 권고 결정.

    buy_score: 매수 시그널 합산
    sell_score: 매도 시그널 합산
    pnl_pct: 평가이익 %
    near_high_pct: 신고가 대비 위치 (1.0 = 신고가)
    """
    net_score = buy_score - sell_score

    # 강한 매도 시그널 우선
    if sell_score >= 9:
        if pnl_pct >= 100:
            return ("🚨 적극 매도 (1/2)", "강한 분배 + 큰 이익", "danger")
        elif pnl_pct >= 50:
            return ("🚨 적극 매도 (1/3)", "강한 분배 + 중간 이익", "danger")
        else:
            return ("🔻 매도 검토 (1/4)", "분배 시그널", "warn")

    if sell_score >= 7:
        if pnl_pct >= 50 and near_high_pct >= 0.95:
            return ("🟡 부분 익절 (1/4)", "신고가 + 매도 시그널", "warn")
        elif pnl_pct >= 30:
            return ("🟡 모니터링 강화", "약한 매도 시그널", "warn")

    # 매수 시그널 우선
    if buy_score >= 7 and net_score >= 5:
        if pnl_pct < 30:
            return ("📈 추가 매수 검토", "강한 매수 + 평가이익 적당", "good")
        elif near_high_pct < 0.92:
            return ("📈 추가 매수 (조정)", "신고가 -8%↓ + 매수 진행", "good")
        else:
            return ("🟢 HOLD (매수 우세)", "매수 시그널 강함", "good")

    if buy_score >= 5 and net_score >= 3:
        return ("🟢 HOLD", "매수 우세", "good")

    if buy_score >= 3 and sell_score < 3:
        return ("🟢 HOLD", "조용한 매수", "neutral")

    if sell_score >= 3 and pnl_pct >= 50:
        return ("🟡 모니터링", "약한 매도 시그널", "warn")

    return ("⚪ 관찰", "특이 시그널 없음", "neutral")


def analyze(stock_name, code, mapping, txs):
    print(f"  {stock_name} ({code})...")
    today = "20260424"
    start_dt = datetime.strptime(today, "%Y%m%d") - timedelta(days=60)
    start = start_dt.strftime("%Y%m%d")

    # 가격
    pdf = krx.get_market_ohlcv_by_date(start, today, code)
    if len(pdf) < 30: return None
    pdf.index = pdf.index.strftime("%Y-%m-%d")
    last_price = float(pdf["종가"].iloc[-1])
    high = float(pdf["종가"].max())
    near_high_pct = last_price / high

    # 거래원
    results = fetch_all_brokers_daily(code, start, today, min_vol=100)
    if not results: return None
    df = aggregate_to_dataframe(results, mapping)
    if df is None or df.empty: return None
    df["date"] = df["date"].apply(lambda d: f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(str(d))==8 else d)

    # 시그널
    buy_sigs = detect_buy_signals(df, pdf["종가"].idxmax())
    sell_sigs = detect_recent_sell_signals(df, pdf["종가"].idxmax())

    buy_score = sum(s["score"] for s in buy_sigs)
    sell_score = sum(s["score"] for s in sell_sigs)

    # 사용자 포지션
    pos = get_user_position(stock_name, txs, last_price)
    pnl_pct = pos["pnl_pct"] if pos else 0

    # 행동 결정
    action, reason, flag = decide_action(buy_score, sell_score, pnl_pct, near_high_pct)

    return {
        "stock": stock_name, "code": code,
        "last_price": last_price, "high_60d": high,
        "near_high_pct": near_high_pct,
        "position": pos,
        "buy_signals": buy_sigs, "sell_signals": sell_sigs,
        "buy_score": buy_score, "sell_score": sell_score,
        "net_score": buy_score - sell_score,
        "action": action, "reason": reason, "flag": flag,
    }


def main():
    print("="*80)
    print("  보유 종목 행동 권고 — 매수/HOLD/매도 종합 진단")
    print("="*80)

    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    # 보유 종목 추출
    qty = defaultdict(int)
    for t in txs:
        if t.get("type") == "buy":
            qty[t["stock"]] += t.get("qty", 0)
        elif t.get("type") == "sell":
            qty[t["stock"]] -= t.get("qty", 0)

    holdings = []
    for s, q in qty.items():
        if q > 0:
            info = smap.get(s, {})
            if info.get("nation") == "KOR" and info.get("code"):
                holdings.append((s, info["code"]))

    # ETF/펀드 제외
    EXCLUDE = ["KODEX", "TIME", "TIGER", "ARIRANG"]
    targets = [(s, c) for s, c in holdings if not any(kw in s for kw in EXCLUDE)]
    print(f"\n분석 대상: {len(targets)}종목 (ETF/펀드 제외)\n")

    # 거래원 매핑 (이전 분석에서 만든 것 재사용 + 갱신)
    print("[거래원 매핑]")
    sample = [c for _, c in targets[:10]]
    mapping = build_broker_mapping(sample)
    print(f"  {len(mapping)}개\n")

    print("[종목별 시그널 분석]")
    results = []
    for stock_name, code in targets:
        try:
            r = analyze(stock_name, code, mapping, txs)
            if r:
                results.append(r)
                pos = r["position"]
                if pos:
                    print(f"    매수{r['buy_score']} 매도{r['sell_score']} 평가{pos['pnl_pct']:+.0f}% → {r['action']}")
        except Exception as e:
            print(f"  [ERR] {stock_name}: {e}")

    # 행동별 그룹
    by_action = defaultdict(list)
    for r in results:
        if "추가 매수" in r["action"] or "신규 매수" in r["action"]:
            by_action["📈 매수"].append(r)
        elif "HOLD" in r["action"]:
            by_action["🟢 HOLD"].append(r)
        elif "익절" in r["action"] or "모니터링" in r["action"]:
            by_action["🟡 주의"].append(r)
        elif "매도" in r["action"]:
            by_action["🚨 매도"].append(r)
        else:
            by_action["⚪ 관찰"].append(r)

    print("\n" + "="*80)
    print("  종합 결과")
    print("="*80)
    for k, items in by_action.items():
        print(f"\n{k} ({len(items)}종목):")
        for r in sorted(items, key=lambda x: -(x['position']['pnl_pct'] if x['position'] else 0)):
            pos = r["position"] or {}
            print(f"  {r['stock']:<14} 평가{pos.get('pnl_pct',0):+6.0f}%  매수{r['buy_score']:>2}/매도{r['sell_score']:>2} → {r['action']}")
            print(f"    근거: {r['reason']}")

    # HTML
    html = build_html(results, by_action)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ {OUT}")


def build_html(results, by_action):
    sections = ""
    for group_label, items in by_action.items():
        if not items: continue
        rows = ""
        for r in sorted(items, key=lambda x: -(x['position']['pnl_pct'] if x['position'] else -999)):
            pos = r["position"] or {}
            buy_sig_html = "<br>".join(f"  • {s['label']} (+{s['score']})" for s in r["buy_signals"])
            sell_sig_html = "<br>".join(f"  • {s['label']} (+{s['score']})" for s in r["sell_signals"])
            pnl_clr = "ret-down" if pos.get("pnl_pct",0) > 0 else "ret-up"
            flag_clr = {"good":"#10b981","warn":"#f59e0b","danger":"#ef4444","neutral":"#9ca3af"}.get(r["flag"], "#9ca3af")
            rows += f"""<tr>
              <td><b>{r['stock']}</b><br><span style="color:#666;font-size:0.78em">{r['code']}</span></td>
              <td class="mono" style="text-align:right">{r['last_price']:,.0f}<br>
                <span style="font-size:0.78em;color:#888">신고가 {r['near_high_pct']*100:.0f}%</span></td>
              <td class="mono {pnl_clr}" style="text-align:right">{pos.get('pnl_pct',0):+.1f}%<br>
                <span style="font-size:0.78em;color:#888">{(pos.get('pnl',0)/1e4):+,.0f}만</span></td>
              <td class="mono" style="text-align:center">📈{r['buy_score']}<br>📉{r['sell_score']}</td>
              <td>
                <div style="font-weight:600;color:{flag_clr};margin-bottom:4px">{r['action']}</div>
                <div style="color:#aaa;font-size:0.85em">{r['reason']}</div>
                <div style="color:#10b981;font-size:0.78em;margin-top:6px">{buy_sig_html}</div>
                <div style="color:#ef4444;font-size:0.78em;margin-top:4px">{sell_sig_html}</div>
              </td>
            </tr>"""

        sections += f"""<div class="card" style="margin-bottom:18px">
          <h2>{group_label} ({len(items)}종목)</h2>
          <table>
            <tr>
              <th>종목</th>
              <th style="text-align:right">현재가</th>
              <th style="text-align:right">평가이익</th>
              <th style="text-align:center">점수</th>
              <th>행동 권고 + 시그널</th>
            </tr>
            {rows}
          </table>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>보유 종목 행동 권고</title>
<link rel="stylesheet" href="assets/style.css">
</head><body>
<div class="container">
<div class="nav">
  <a href="index.html">📊 전체</a>
  <a href="holdings_peaks.html">고점 분석</a>
  <a href="holdings_action.html" class="active">🎯 행동 권고</a>
</div>

<h1>🎯 보유 종목 행동 권고 (매수/HOLD/매도)</h1>
<p class="subtitle">KIS 거래원 시계열 + 평가이익 + 신고가 거리 종합 — 종목별 차별화</p>

<div class="card">
  <div class="callout">
    <b>매수 시그널:</b> 외국계 컨센서스 / 매수 주도자 일관성 / 신규 외국계 진입 / 외인↔개미 역분배<br>
    <b>매도 시그널:</b> 매수→매도 전환 / 분배 패턴<br>
    <br>
    <b>행동 결정 룰:</b><br>
    📈 매수: 매수 점수 ≥7 + 순점수 ≥5 + 평가이익 부족<br>
    🟢 HOLD: 매수 우세 + 신고가 근처<br>
    🟡 주의: 약한 매도 + 큰 평가이익<br>
    🚨 매도: 강한 매도 (점수 ≥9) + 큰 평가이익
  </div>
</div>

{sections}

</div></body></html>"""


if __name__ == "__main__":
    main()
