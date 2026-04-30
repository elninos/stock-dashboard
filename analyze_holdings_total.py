#!/usr/bin/env python3
"""보유 종목 종합 진단 — KIS 수급 + 브리핑 + 뉴스 통합.

데이터:
  1. KIS API 거래원/투자자 매매 (수급)
  2. briefing_summary.json (텔레그램+블로그 종합)
  3. stock_news.json (뉴스)
  4. 가격/거래량 (패턴 분류)
  + DART (키 받으면 추가)

종합 점수:
  매도 점수 = 수급(40%) + 패턴(30%) + sentiment(20%) + 뉴스(10%)
  매수 점수 = 매수시그널(50%) + sentiment(30%) + 뉴스(20%)

  → 종합 행동 권고
"""
import sys, warnings, os
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
from collections import defaultdict
from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from signals.kis_investor import analyze_investor_signal
from signals.kis_broker import analyze_broker_signal
from pykrx import stock as krx

try:
    from signals.dart_insider import analyze_insider_signal
    DART_OK = True
except Exception:
    DART_OK = False

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard", "holdings_total.html")


def fmt(v):
    if v is None: return "─"
    if abs(v) >= 1e8: return f"{v/1e8:+.2f}억"
    return f"{v/1e4:+,.0f}만"


def classify_pattern(code):
    """패턴 분류 (분배/펌프/잔여)."""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")
    try:
        pdf = krx.get_market_ohlcv_by_date(start, end, code)
    except Exception:
        return None
    if len(pdf) < 30: return None
    pdf.index = pdf.index.strftime("%Y-%m-%d")

    rv = pdf["거래량"].tail(5).mean()
    pv = pdf["거래량"].iloc[-25:-5].mean()
    vol_ratio = rv/pv if pv > 0 else 0

    pdf["range_pct"] = (pdf["고가"] - pdf["저가"]) / pdf["종가"] * 100
    ra = pdf["range_pct"].tail(5).mean()
    pa = pdf["range_pct"].iloc[-25:-5].mean()
    atr_ratio = ra/pa if pa > 0 else 0

    peak = pdf["종가"].max()
    cur = pdf["종가"].iloc[-1]
    from_peak = (cur/peak-1)*100
    chg_30d = (cur/pdf["종가"].iloc[-31]-1)*100 if len(pdf) >= 31 else 0
    chg_5d = (cur/pdf["종가"].iloc[-6]-1)*100 if len(pdf) >= 6 else 0

    score = {"분배": 0, "펌프": 0, "잔여": 0}
    if from_peak > -15: score["분배"] += 3
    if vol_ratio < 2.5: score["분배"] += 2
    if atr_ratio < 1.5: score["분배"] += 1
    if vol_ratio >= 3: score["펌프"] += 4
    if atr_ratio >= 1.8: score["펌프"] += 3
    if chg_5d > 15: score["펌프"] += 2
    if from_peak < -25: score["잔여"] += 3
    if chg_30d < -10: score["잔여"] += 2

    best = max(score, key=score.get)
    return {
        "pattern": best,
        "vol_ratio": vol_ratio, "atr_ratio": atr_ratio,
        "from_peak": from_peak, "chg_30d": chg_30d, "chg_5d": chg_5d,
        "current": cur, "peak": peak,
    }


def get_briefing_signal(stock_name, briefing):
    """브리핑 sentiment 추출 (daily/weekly 종합)."""
    result = {"daily": None, "weekly": None, "monthly": None}

    for period in ["daily", "weekly", "monthly"]:
        period_data = briefing.get(period, {})
        if not isinstance(period_data, dict): continue
        stocks = period_data.get("stocks", [])
        for s in stocks:
            if s.get("name") == stock_name:
                result[period] = {
                    "mention": s.get("mention_count", 0),
                    "channels": len(s.get("channels", [])),
                    "sentiment": s.get("sentiment"),
                    "context": s.get("context", "")[:200],
                }
                break
    return result


def get_news_signal(stock_name, stock_news):
    """뉴스 시그널 추출."""
    stocks_data = stock_news.get("stocks", {})
    return stocks_data.get(stock_name)


def compute_total_score(kis_inv, kis_broker, pattern, briefing, news, dart=None):
    """종합 매도/매수 점수."""
    sell = 0; buy = 0
    sell_reasons = []; buy_reasons = []

    # === 1. KIS 수급 (40%) ===
    if kis_inv and kis_inv.get("available"):
        sm5 = kis_inv["smart_5d"]; sm20 = kis_inv["smart_20d"]
        r5 = kis_inv["retail_5d"]
        if sm20 < -200: sell += 4; sell_reasons.append(f"스마트 20일 {sm20:+.0f}억")
        elif sm20 < -50: sell += 2; sell_reasons.append(f"스마트 20일 {sm20:+.0f}억")
        if sm5 < -100: sell += 3; sell_reasons.append(f"스마트 5일 {sm5:+.0f}억")
        if r5 > 100 and sm5 < -50:
            sell += 4; sell_reasons.append(f"분배 (개미 +{r5:.0f}억 vs 스마트 {sm5:+.0f}억)")
        if sm20 > 100: buy += 4; buy_reasons.append(f"스마트 20일 +{sm20:.0f}억 매수")
        elif sm5 > 50: buy += 2; buy_reasons.append(f"스마트 5일 +{sm5:.0f}억")

    # === 2. 거래원 (KIS broker triggers) ===
    if kis_broker and kis_broker.get("available"):
        for t in (kis_broker.get("triggers") or []):
            if t["type"] == "sell":
                sell += 2; sell_reasons.append(t["label"][:60])
            elif t["type"] == "buy":
                buy += 2; buy_reasons.append(t["label"][:60])

    # === 3. 패턴 (30%) ===
    if pattern:
        if pattern["pattern"] == "분배":
            sell += 3; sell_reasons.append(f"패턴: 분배 (vol {pattern['vol_ratio']:.1f}x)")
        elif pattern["pattern"] == "펌프":
            sell += 1; sell_reasons.append(f"패턴: 개미펌프 (vol {pattern['vol_ratio']:.1f}x — 단기 위험)")
            buy += 1
        elif pattern["pattern"] == "잔여":
            buy += 1; buy_reasons.append(f"패턴: 약세장 진입 (-{abs(pattern['from_peak']):.0f}% 빠짐)")

    # === 4. Briefing sentiment (20%) ===
    if briefing.get("daily") or briefing.get("weekly"):
        # 가장 최신 + 멘션 많은 것
        for period in ["daily", "weekly", "monthly"]:
            data = briefing.get(period)
            if not data: continue
            mention = data["mention"]
            sentiment = data["sentiment"]
            channels = data["channels"]

            if sentiment == "positive":
                weight = min(mention // 3, 4)  # 최대 4점
                if channels >= 2: weight += 1
                buy += weight
                buy_reasons.append(f"브리핑 {period}: {sentiment} ({mention}회, {channels}채널)")
            elif sentiment == "negative":
                weight = min(mention // 3, 4)
                if channels >= 2: weight += 1
                sell += weight
                sell_reasons.append(f"브리핑 {period}: {sentiment} ({mention}회, {channels}채널)")
            break

    # === 5. DART 인사이더 (가중 큼: 직접 증거) ===
    if dart and dart.get("available"):
        ds = dart["score"]
        if dart["n_ins_sells"] > 0:
            sell += min(dart["n_ins_sells"] * 2, 6)
            sell_reasons.append(f"DART 임원매도 {dart['n_ins_sells']}건 ({dart['ins_sell_qty']:,}주)")
        if dart["n_ins_buys"] > 0 and dart["ins_buy_qty"] > dart["ins_sell_qty"] * 1.5:
            buy += min(dart["n_ins_buys"] * 2, 6)
            buy_reasons.append(f"DART 임원매수 {dart['n_ins_buys']}건 ({dart['ins_buy_qty']:,}주, 매도의 {dart['ins_buy_qty']/max(dart['ins_sell_qty'],1):.1f}배)")
        if dart["n_major_dec"] > 0:
            sell += min(dart["n_major_dec"] * 2, 6)
            sell_reasons.append(f"DART 5%주주 감소 {dart['n_major_dec']}건")
        if dart["n_major_inc"] > 0:
            buy += min(dart["n_major_inc"], 4)
            buy_reasons.append(f"DART 5%주주 증가 {dart['n_major_inc']}건")
        if dart["n_ts_buys"] > 0:
            buy += 4
            buy_reasons.append(f"DART 자사주 취득 {dart['n_ts_buys']}건")
        if dart["n_ts_sells"] > 0:
            sell += 3
            sell_reasons.append(f"DART 자사주 처분 {dart['n_ts_sells']}건")

    # === 6. 뉴스 (10%) ===
    if news and isinstance(news, dict):
        if "summary" in news:
            summary = str(news.get("summary", ""))
            if any(kw in summary for kw in ["호재", "급등", "급증", "어닝", "서프라이즈", "상향"]):
                buy += 2; buy_reasons.append("뉴스 호재")
            if any(kw in summary for kw in ["악재", "급락", "감소", "하락", "쇼크", "하향"]):
                sell += 2; sell_reasons.append("뉴스 악재")

    return {
        "sell_score": sell,
        "buy_score": buy,
        "sell_reasons": sell_reasons,
        "buy_reasons": buy_reasons,
    }


def decide_action(scores, pnl_pct, pattern):
    """행동 권고."""
    s = scores["sell_score"]
    b = scores["buy_score"]
    net = s - b

    # 패턴별 차별화
    pat = pattern["pattern"] if pattern else None

    # 펌프 패턴 — 트레일링 스탑 우선
    if pat == "펌프":
        return ("⚠️ 펌프 종목 — 트레일링 스탑 -10%", "펌프 패턴: 단기 변동성 큼", "warn")

    # 잔여 패턴 — HOLD 또는 추가 매수
    if pat == "잔여" and b >= 3:
        return ("📈 저점 반등 후보", "이미 빠진 후 매수 시그널", "good")

    # 분배 + 강한 매도 + 큰 이익
    if net >= 8 and pnl_pct >= 50:
        return ("🚨 1/3 매도", "분배 강함 + 큰 이익", "danger")
    if net >= 5 and pnl_pct >= 100:
        return ("🚨 1/4 익절", "큰 이익 + 매도 시그널", "danger")
    if net >= 3 and pnl_pct >= 50:
        return ("🟡 부분 익절 검토", "매도 우세", "warn")

    # 손실 + 매도 시그널 — 손절
    if pnl_pct <= -10 and s >= 5:
        return ("⛔ 손절 검토", "손실 확대 + 매도 시그널", "danger")

    # 매수 우세
    if b > s + 3:
        return ("📈 추가 매수 후보", "매수 우세", "good")

    # 균형
    if abs(net) <= 2:
        return ("⚪ 관찰", "시그널 혼재 (수급 vs 펀더멘털)", "neutral")

    if net > 0:
        return ("🟡 모니터링", "약한 매도 우세", "warn")
    return ("🟢 HOLD", "특이 시그널 없음", "good")


def main():
    print("="*100)
    print("  보유 종목 종합 진단 — KIS + 브리핑 + 뉴스")
    print("="*100)

    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})
    briefing = load_json("briefing_summary.json", default={})
    news = load_json("stock_news.json", default={})

    qty = defaultdict(int)
    for t in txs:
        if t.get("type") == "buy": qty[t["stock"]] += t.get("qty", 0)
        elif t.get("type") == "sell": qty[t["stock"]] -= t.get("qty", 0)

    holdings = []
    EXCLUDE = ["KODEX","TIME","TIGER"]
    for s, q in qty.items():
        if q > 0:
            info = smap.get(s, {})
            if info.get("nation") == "KOR" and info.get("code") and not any(kw in s for kw in EXCLUDE):
                # FIFO 평단
                lots = []
                for t in sorted([x for x in txs if x["stock"]==s and x["type"] in ("buy","sell")], key=lambda x: x["date"]):
                    if t["type"] == "buy":
                        lots.append({"qty": t["qty"], "price": t["price"]})
                    else:
                        rem = t["qty"]
                        while rem > 0 and lots:
                            lot = lots[0]
                            take = min(rem, lot["qty"])
                            lot["qty"] -= take
                            rem -= take
                            if lot["qty"] <= 0: lots.pop(0)
                cq = sum(l["qty"] for l in lots)
                cc = sum(l["qty"]*l["price"] for l in lots)
                avg = cc/cq if cq > 0 else 0
                holdings.append((s, info["code"], cq, avg))

    print(f"\n분석 대상: {len(holdings)}종목\n")

    results = []
    for stock_name, code, qty_h, avg in holdings:
        try:
            kis_inv = analyze_investor_signal(code)
            kis_broker = analyze_broker_signal(code)
            pat = classify_pattern(code)
            brief = get_briefing_signal(stock_name, briefing)
            new = get_news_signal(stock_name, news)
            dart = None
            if DART_OK:
                try:
                    dart = analyze_insider_signal(code, lookback_days=180)
                except Exception:
                    dart = None

            cur = pat["current"] if pat else avg
            pnl_pct = (cur/avg-1)*100 if avg > 0 else 0
            pnl_amt = qty_h*(cur-avg) if avg > 0 else 0

            scores = compute_total_score(kis_inv, kis_broker, pat, brief, new, dart)
            action, reason, flag = decide_action(scores, pnl_pct, pat)

            results.append({
                "stock": stock_name, "code": code,
                "qty": qty_h, "avg": avg, "cur": cur,
                "pnl_pct": pnl_pct, "pnl_amt": pnl_amt,
                "pattern": pat, "brief": brief, "news": new, "dart": dart,
                "scores": scores,
                "action": action, "reason": reason, "flag": flag,
            })

            print(f"  {stock_name:<14} 평가{pnl_pct:>+5.0f}% / 매도{scores['sell_score']:>2} 매수{scores['buy_score']:>2} / "
                  f"패턴 {pat['pattern'] if pat else '─':<4} → {action}")
        except Exception as e:
            print(f"  [ERR] {stock_name}: {e}")

    # HTML
    by_flag = defaultdict(list)
    for r in results:
        by_flag[r["flag"]].append(r)

    html = build_html(results, by_flag)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ {OUT}")


def build_html(results, by_flag):
    flag_labels = {
        "danger": "🚨 즉시 검토",
        "warn":   "🟡 주의",
        "good":   "🟢 HOLD / 매수 후보",
        "neutral":"⚪ 관찰",
    }

    sections = ""
    for flag in ["danger", "warn", "good", "neutral"]:
        items = by_flag.get(flag, [])
        if not items: continue
        rows = ""
        for r in sorted(items, key=lambda x: -x["scores"]["sell_score"] if flag in ("danger","warn") else -x["scores"]["buy_score"]):
            pat_label = r["pattern"]["pattern"] if r["pattern"] else "?"
            sell_reasons = "<br>".join(f"  • {x[:60]}" for x in r["scores"]["sell_reasons"][:3])
            buy_reasons = "<br>".join(f"  • {x[:60]}" for x in r["scores"]["buy_reasons"][:3])

            # 브리핑 컨텍스트
            brief_ctx = ""
            for p in ["daily","weekly","monthly"]:
                if r["brief"].get(p) and r["brief"][p].get("context"):
                    brief_ctx = f"<div style='font-size:0.78em;color:#aaa;margin-top:6px;border-left:2px solid #4fc3f7;padding-left:8px'>📰 {p}: {r['brief'][p]['context']}</div>"
                    break

            # DART 인사이더
            dart_ctx = ""
            d = r.get("dart")
            if d and d.get("available") and any([d["n_ins_buys"], d["n_ins_sells"], d["n_major_inc"], d["n_major_dec"], d["n_ts_buys"], d["n_ts_sells"]]):
                parts = []
                if d["n_ins_sells"]: parts.append(f"임매도 {d['n_ins_sells']}건/{d['ins_sell_qty']:,}주")
                if d["n_ins_buys"]:  parts.append(f"임매수 {d['n_ins_buys']}건/{d['ins_buy_qty']:,}주")
                if d["n_major_dec"]: parts.append(f"5%↓ {d['n_major_dec']}건")
                if d["n_major_inc"]: parts.append(f"5%↑ {d['n_major_inc']}건")
                if d["n_ts_buys"]:   parts.append(f"자사매입 {d['n_ts_buys']}건")
                if d["n_ts_sells"]:  parts.append(f"자사처분 {d['n_ts_sells']}건")
                clr = "#ef4444" if d["score"] >= 5 else ("#10b981" if d["score"] <= -3 else "#f59e0b")
                dart_ctx = f"<div style='font-size:0.78em;color:{clr};margin-top:6px;border-left:2px solid {clr};padding-left:8px'>🏛️ DART 180일: {' / '.join(parts)} → 점수 {d['score']:+}</div>"

            pnl_clr = "ret-down" if r["pnl_pct"] > 0 else "ret-up"
            flag_clr = {"danger":"#ef4444","warn":"#f59e0b","good":"#10b981","neutral":"#9ca3af"}[r["flag"]]

            rows += f"""<tr>
              <td><b>{r['stock']}</b><br><span style="color:#666;font-size:0.78em">{r['code']}</span></td>
              <td class="mono {pnl_clr}" style="text-align:right">{r['pnl_pct']:+.1f}%<br>
                <span style="font-size:0.78em;color:#888">{(r['pnl_amt']/1e4):+,.0f}만</span></td>
              <td style="text-align:center">{pat_label}</td>
              <td class="mono" style="text-align:center">📈{r['scores']['buy_score']}<br>📉{r['scores']['sell_score']}</td>
              <td>
                <div style="font-weight:600;color:{flag_clr};margin-bottom:4px">{r['action']}</div>
                <div style="color:#aaa;font-size:0.85em">{r['reason']}</div>
                {f'<div style="color:#10b981;font-size:0.78em;margin-top:6px">{buy_reasons}</div>' if buy_reasons else ''}
                {f'<div style="color:#ef4444;font-size:0.78em;margin-top:4px">{sell_reasons}</div>' if sell_reasons else ''}
                {brief_ctx}
                {dart_ctx}
              </td>
            </tr>"""

        sections += f"""<div class="card" style="margin-bottom:16px">
          <h2>{flag_labels[flag]} ({len(items)}종목)</h2>
          <table>
            <tr>
              <th>종목</th>
              <th style="text-align:right">평가이익</th>
              <th style="text-align:center">패턴</th>
              <th style="text-align:center">점수</th>
              <th>권고 + 시그널 + 브리핑</th>
            </tr>
            {rows}
          </table>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>보유 종목 종합 진단</title>
<link rel="stylesheet" href="assets/style.css">
</head><body>
<div class="container">

<div class="nav">
  <a href="index.html">📊 전체</a>
  <a href="holdings_action.html">행동 권고</a>
  <a href="holdings_total.html" class="active">🎯 종합 진단</a>
</div>

<h1>🎯 보유 종목 종합 진단 (KIS + 브리핑 + 뉴스)</h1>
<p class="subtitle">수급 (40%) + 패턴 (30%) + sentiment (20%) + 뉴스 (10%) — 멀티소스 통합</p>

<div class="card">
  <div class="callout">
    <b>통합 데이터 소스:</b><br>
    • <b>KIS API</b>: 거래원/투자자별 매매 (실시간 수급)<br>
    • <b>패턴 분류</b>: 거래량/변동성/추세 → 분배/펌프/잔여 자동 구분<br>
    • <b>브리핑</b> (briefing_summary.json): 텔레그램 9채널 + 블로그 5개 sentiment<br>
    • <b>뉴스</b> (stock_news.json): 종목별 호재/악재<br>
    • <b>DART 공시</b>: 임원 매수/매도, 5%주주 변동, 자사주 취득/처분 (180일)<br>
    <br>
    <b>특징:</b><br>
    • 수급 vs 펀더멘털 충돌 시 (예: SK하이닉스) 둘 다 표시 — "관찰" 등급<br>
    • 패턴이 "펌프"면 트레일링 스탑 우선<br>
    • "잔여" 패턴 + 매수 시그널 → 저점 매수 후보
  </div>
</div>

{sections}

</div></body></html>"""


if __name__ == "__main__":
    main()
