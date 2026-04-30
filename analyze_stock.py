#!/usr/bin/env python3
"""종목별 정밀 분석 — 단일 진입점.

사용:
    python3 analyze_stock.py 010170
    python3 analyze_stock.py 010170 --html
    python3 analyze_stock.py 010170 --json out.json

출력:
    - 터미널 리포트 (기본)
    - HTML 리포트 (--html, dashboard/stock/{code}.html)
    - JSON 데이터 (--json)

분석 순서 (사용자 권장 framework):
    1. 매크로 (한국+미국)
    2. 테마/Value Chain/Peer
    3. DART 인사이더
    4. 수급 (투자자별)
    5. 창구 (거래원)
    6. 공매도/대차
    7. 차트/패턴
    8. 실적/뉴스/이벤트
    9. 유사 패턴 (TODO)
    10. 시그널 통합
    11. 포지션 사이징 + 전략 (단기/중기/장기)

사용자 선호도: 중장기 수익 극대화 > 단기 손실 회피
"""
import sys, os, json, warnings, argparse, time
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
from collections import defaultdict


# === Helpers ===

def fmt_pct(v, w=7):
    if v is None: return f"{'─':>{w}}"
    return f"{v:>+{w-1}.1f}%"


def fmt_amt(v):
    if v is None: return "─"
    if abs(v) >= 1e8: return f"{v/1e8:+,.1f}억"
    if abs(v) >= 1e4: return f"{v/1e4:+,.0f}만"
    return f"{v:+,.0f}"


def fmt_qty(v):
    if v is None: return "─"
    if abs(v) >= 1_000_000: return f"{v/1_000_000:+.2f}M"
    if abs(v) >= 10_000:    return f"{v/10_000:+.1f}만"
    return f"{v:+,}"


def safe(fn, *args, **kwargs):
    """모듈 실행 안전 래퍼."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        return {"available": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}


# === Section 1: 기본 정보 ===

def get_basic_info(code: str):
    """종목 기본 정보 + 보유 정보."""
    from file_io import load_json
    from config import TRANSACTIONS_FILE, STOCK_MAP_FILE

    smap = load_json(STOCK_MAP_FILE, default={})
    txs = load_json(TRANSACTIONS_FILE, default=[])

    # 종목명 찾기
    name = None
    for s, info in smap.items():
        if info.get("code") == code:
            name = s; break

    # 보유 수량/평단
    qty = 0
    lots = []
    if name:
        for t in sorted([x for x in txs if x.get("stock") == name and x.get("type") in ("buy","sell")],
                        key=lambda x: x["date"]):
            if t["type"] == "buy":
                lots.append({"qty": t["qty"], "price": t["price"]})
                qty += t["qty"]
            else:
                rem = t["qty"]
                qty -= t["qty"]
                while rem > 0 and lots:
                    lot = lots[0]
                    take = min(rem, lot["qty"])
                    lot["qty"] -= take
                    rem -= take
                    if lot["qty"] <= 0: lots.pop(0)

    cq = sum(l["qty"] for l in lots)
    cc = sum(l["qty"]*l["price"] for l in lots)
    avg = cc/cq if cq > 0 else 0

    return {
        "code": code,
        "name": name or "?",
        "qty": cq,
        "avg_price": avg,
        "is_holding": cq > 0,
    }


# === Section 2: 매크로 ===

def get_macro():
    from signals.macro import analyze_macro
    return safe(analyze_macro)


# === Section 3: 테마 ===

def get_theme(code: str):
    from signals.theme import analyze_theme
    return safe(analyze_theme, code)


# === Section 4: DART ===

def get_dart(code: str, lookback: int = 180):
    from signals.dart_insider import analyze_insider_signal
    return safe(analyze_insider_signal, code, lookback_days=lookback)


# === Section 5: 수급 ===

def get_investor(code: str):
    from signals.kis_investor import analyze_investor_signal
    return safe(analyze_investor_signal, code)


# === Section 6: 창구 ===

def get_broker_window(code: str, days: int = 60):
    """60일 거래원 누적 분석."""
    from signals.kis_member_daily import (
        fetch_all_brokers_daily, build_broker_mapping, load_broker_names
    )

    try:
        # 매핑 보강 (내가 분석할 종목 + 메이저 종목들)
        build_broker_mapping([code, "035420", "000660", "214450"], force_rebuild=False)
        names = load_broker_names()

        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days+30)).strftime("%Y%m%d")
        results = fetch_all_brokers_daily(code, start, end, min_vol=10000)
        if not results:
            return {"available": False, "error": "거래원 데이터 없음"}

        # 누적 + 5/10/20일
        rows = []
        for c, data in results.items():
            data_sorted = sorted(data, key=lambda r: r["date"])
            total_net = sum(r["net"] for r in data_sorted)
            net5  = sum(r["net"] for r in data_sorted[-5:])
            net10 = sum(r["net"] for r in data_sorted[-10:])
            net20 = sum(r["net"] for r in data_sorted[-20:])
            rows.append({
                "code": c, "name": names.get(c, f"unknown_{c}"),
                "total_net": total_net, "net5": net5, "net10": net10, "net20": net20,
                "data": data_sorted,
            })

        # 그룹 분류
        FOREIGN_KW = ["외국계", "모간", "씨엘에", "JP모간", "메릴린치", "노무라", "BNP",
                      "맥쿼리", "골드만", "UBS", "다이와", "씨에스", "도이치"]
        RETAIL_KW = ["키움", "토스", "카카오", "상상인", "이베스트"]
        LARGE_KW  = ["NH", "한국투자", "삼성", "한화", "미래에셋", "신한", "하나", "KB", "대신"]

        def classify(name):
            for k in FOREIGN_KW:
                if k in name: return "외국계"
            for k in RETAIL_KW:
                if k in name: return "개미창구"
            for k in LARGE_KW:
                if k in name: return "대형국내"
            return "기타"

        groups = defaultdict(lambda: {"total_net":0,"net5":0,"net10":0,"net20":0,"n":0})
        for r in rows:
            r["group"] = classify(r["name"])
            g = groups[r["group"]]
            g["total_net"] += r["total_net"]
            g["net5"]  += r["net5"]
            g["net10"] += r["net10"]
            g["net20"] += r["net20"]
            g["n"] += 1

        # TOP 매수/매도
        rows.sort(key=lambda r: r["total_net"])
        top_sells = rows[:10]
        top_buys  = list(reversed(rows[-10:]))

        return {
            "available": True,
            "lookback_days": days,
            "n_brokers": len(rows),
            "groups": dict(groups),
            "top_buys": top_buys,
            "top_sells": top_sells,
        }
    except Exception as e:
        return {"available": False, "error": str(e)[:80]}


# === Section 7: 공매도 ===

def get_short(code: str):
    from signals.kis_short import analyze_short_signal
    return safe(analyze_short_signal, code)


# === Section 8: 차트/패턴 ===

def get_pattern(code: str, lookback: int = 90):
    from pykrx import stock as krx
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=lookback+30)).strftime("%Y%m%d")
    try:
        df = krx.get_market_ohlcv_by_date(start, end, code)
        if len(df) < 30: return {"available": False, "error": "데이터 부족"}
    except Exception as e:
        return {"available": False, "error": str(e)[:80]}

    df.index = df.index.strftime("%Y-%m-%d")
    cur = float(df["종가"].iloc[-1])
    peak = float(df["종가"].max())
    from_peak = (cur/peak-1)*100

    # 거래량
    rv = df["거래량"].tail(5).mean()
    pv = df["거래량"].iloc[-25:-5].mean()
    vol_ratio = rv/pv if pv > 0 else 0

    # 변동성
    df["range_pct"] = (df["고가"] - df["저가"])/df["종가"] * 100
    ra = df["range_pct"].tail(5).mean()
    pa = df["range_pct"].iloc[-25:-5].mean()
    atr_ratio = ra/pa if pa > 0 else 0

    # 모멘텀
    chg_5  = (cur/float(df["종가"].iloc[-6])-1)*100  if len(df) >= 6 else None
    chg_20 = (cur/float(df["종가"].iloc[-21])-1)*100 if len(df) >= 21 else None
    chg_60 = (cur/float(df["종가"].iloc[-61])-1)*100 if len(df) >= 61 else None

    # 패턴 분류
    score = {"분배": 0, "펌프": 0, "잔여": 0, "정상": 0}
    if from_peak > -15: score["분배"] += 3
    if vol_ratio < 2.5: score["분배"] += 2
    if atr_ratio < 1.5: score["분배"] += 1
    if vol_ratio >= 3:  score["펌프"] += 4
    if atr_ratio >= 1.8: score["펌프"] += 3
    if chg_5 and chg_5 > 15: score["펌프"] += 2
    if from_peak < -25: score["잔여"] += 3
    if chg_20 and chg_20 < -10: score["잔여"] += 2
    if from_peak > -10 and abs(chg_20 or 0) < 10: score["정상"] += 2

    pattern = max(score, key=score.get)

    # 매물대
    last60 = df.tail(60)
    all_vol = last60["거래량"].sum()
    bins = []
    for thr in [25000, 22000, 20000, 18000, 16000, 14000, 12000, 10000, 8000, 6000, 4000, 2000]:
        in_range = last60[(last60["종가"] >= thr-1000) & (last60["종가"] < thr+1000)]
        pct = in_range["거래량"].sum()/all_vol*100 if all_vol > 0 else 0
        if pct > 0:
            bins.append({"price": thr, "pct": pct})

    # 장대음봉 (최근 30일)
    bear_days = []
    last30 = df.tail(30)
    for date, row in last30.iterrows():
        body = abs(float(row["종가"]) - float(row["시가"])) / float(row["종가"]) * 100 if float(row["종가"]) > 0 else 0
        if float(row["등락률"]) <= -10 and body > 5:
            bear_days.append({"date": date, "chg": float(row["등락률"]), "body": body})

    return {
        "available": True,
        "current": cur,
        "peak": peak,
        "from_peak": from_peak,
        "chg_5": chg_5, "chg_20": chg_20, "chg_60": chg_60,
        "vol_ratio": vol_ratio,
        "atr_ratio": atr_ratio,
        "pattern": pattern,
        "pattern_scores": score,
        "supply_levels": bins[:8],
        "bear_days": bear_days,
    }


# === Section 9: 뉴스/브리핑 ===

def get_news_briefing(code: str, name: str):
    from file_io import load_json

    briefing = load_json("briefing_summary.json", default={})
    news = load_json("stock_news.json", default={})

    # 브리핑
    brief = {"daily": None, "weekly": None, "monthly": None}
    for period in ["daily", "weekly", "monthly"]:
        pdata = briefing.get(period, {})
        if isinstance(pdata, dict):
            for s in pdata.get("stocks", []):
                if s.get("name") == name:
                    brief[period] = {
                        "mention": s.get("mention_count", 0),
                        "channels": len(s.get("channels", [])),
                        "sentiment": s.get("sentiment"),
                        "context": (s.get("context") or "")[:300],
                    }
                    break

    # 종목 뉴스
    n = news.get("stocks", {}).get(name)

    return {
        "available": (any(brief.values()) or n is not None),
        "briefing": brief,
        "news": n,
    }


# === Section 10: 시그널 통합 ===

def compute_integrated(sections, user_pref="midlong"):
    """종합 점수 + 신뢰도.

    가중치 (사용자 선호도 = 중장기):
        DART 인사이더 (중장기 신호) ............ 25%
        매크로 + 테마 (방향성) ................ 25%
        수급 (단기 분배 → 비중 축소) ........... 15%
        창구 (단기 분배) ..................... 10%
        패턴 (분배/펌프/잔여) .................. 10%
        뉴스/브리핑 (sentiment) ................ 10%
        공매도 (헤지·정보) ................... 5%
    """
    sell = 0; buy = 0
    sell_reasons = []; buy_reasons = []

    # 1) DART (25%)
    d = sections.get("dart") or {}
    if d.get("available"):
        if d["n_ins_sells"] > 0:
            w = min(d["n_ins_sells"] * 2, 8)
            sell += w
            sell_reasons.append(f"DART 임원 매도 {d['n_ins_sells']}건 ({d['ins_sell_qty']:,}주)")
        if d["n_ins_buys"] > 0 and d["ins_buy_qty"] > d["ins_sell_qty"] * 1.5:
            w = min(d["n_ins_buys"] * 2, 8)
            buy += w
            buy_reasons.append(f"DART 임원 매수 우세 ({d['ins_buy_qty']:,}주)")
        if d["n_major_dec"] > 0:
            w = min(d["n_major_dec"] * 2, 6)
            sell += w
            sell_reasons.append(f"DART 5%주주 감소 {d['n_major_dec']}건")
        if d["n_major_inc"] > 0:
            buy += min(d["n_major_inc"], 4)
            buy_reasons.append(f"DART 5%주주 증가 {d['n_major_inc']}건")
        if d["n_ts_buys"] > 0:
            buy += 4
            buy_reasons.append(f"자사주 취득 {d['n_ts_buys']}건")
        if d["n_ts_sells"] > 0:
            sell += 3
            sell_reasons.append(f"자사주 처분 {d['n_ts_sells']}건")

    # 2) 매크로 + 테마 (25%)
    m = sections.get("macro") or {}
    if m.get("_overall"):
        os_ = m["_overall"]["score"]
        if os_ >= 2:
            buy += 2; buy_reasons.append(f"매크로 risk-on (점수 {os_:+d})")
        elif os_ <= -2:
            sell += 2; sell_reasons.append(f"매크로 risk-off (점수 {os_:+d})")

    th = sections.get("theme") or {}
    if th.get("available"):
        # 테마 자체 강세 → 보유 정당화
        if th.get("glob_avg_60d") and th["glob_avg_60d"] > 30:
            buy += 3
            buy_reasons.append(f"테마 글로벌 60일 +{th['glob_avg_60d']:.0f}% (펀더멘털 강세)")
        # 한국만 약세 (decoupling)
        if th.get("decoupling") and "한국만 약세" in th["decoupling"]:
            sell += 2
            sell_reasons.append(f"한국 Peer만 약세 (5일 vs 글로벌 -{abs(th.get('kor_avg_5d',0)-th.get('glob_avg_5d',0)):.1f}%p)")
        # 알파 너무 큼 (오버슈팅)
        if th.get("alpha_vs_global") and th["alpha_vs_global"] > 200:
            sell += 3
            sell_reasons.append(f"vs 글로벌 알파 +{th['alpha_vs_global']:.0f}%p (오버슈팅)")

    # 3) 수급 (15%)
    inv = sections.get("investor") or {}
    if inv.get("available"):
        sm5 = inv.get("smart_5d", 0); sm20 = inv.get("smart_20d", 0)
        r5  = inv.get("retail_5d", 0)
        if sm20 < -200:
            sell += 3; sell_reasons.append(f"스마트머니 20일 {sm20:+.0f}억 매도")
        elif sm20 < -50:
            sell += 1; sell_reasons.append(f"스마트머니 20일 {sm20:+.0f}억")
        if sm5 < -100:
            sell += 2; sell_reasons.append(f"스마트머니 5일 {sm5:+.0f}억 매도")
        if r5 > 100 and sm5 < -50:
            sell += 3; sell_reasons.append(f"분배 패턴 (개미 +{r5:.0f}억 vs 스마트 {sm5:+.0f}억)")
        if sm20 > 100:
            buy += 3; buy_reasons.append(f"스마트머니 20일 +{sm20:.0f}억 매수")

    # 4) 창구 (10%)
    bk = sections.get("broker") or {}
    if bk.get("available"):
        groups = bk.get("groups", {})
        # 외국계 5일/10일 매도 전환
        fg = groups.get("외국계", {})
        if fg.get("net5", 0) < -1_000_000:
            sell += 2
            sell_reasons.append(f"외국계 창구 5일 {fg['net5']:,}주 매도")
        if fg.get("net5", 0) > 1_000_000:
            buy += 2
            buy_reasons.append(f"외국계 창구 5일 +{fg['net5']:,}주 매수")
        # 개미창구 폭발적 매수 = 분배 표적
        retail = groups.get("개미창구", {})
        if retail.get("net5", 0) > 500_000 and fg.get("net5", 0) < -300_000:
            sell += 2
            sell_reasons.append(f"개미창구 매수 + 외국계 매도 = 분배")

    # 5) 패턴 (10%)
    pat = sections.get("pattern") or {}
    if pat.get("available"):
        if pat["pattern"] == "분배":
            sell += 2; sell_reasons.append(f"패턴: 분배 (vol {pat['vol_ratio']:.1f}x)")
        elif pat["pattern"] == "펌프":
            sell += 1; sell_reasons.append(f"패턴: 개미펌프 (단기 위험)")
            buy += 1
        elif pat["pattern"] == "잔여":
            buy += 1; buy_reasons.append(f"패턴: 약세 후반 (저점 가능)")

    # 6) 뉴스/브리핑 (10%)
    nb = sections.get("news_briefing") or {}
    if nb.get("available"):
        for period in ["daily", "weekly", "monthly"]:
            b = nb["briefing"].get(period)
            if b and b.get("sentiment"):
                weight = min(b.get("mention", 1) // 3, 3)
                if b.get("channels", 0) >= 2: weight += 1
                if b["sentiment"] == "positive":
                    buy += weight; buy_reasons.append(f"브리핑 {period} positive ({b.get('mention')}회)")
                elif b["sentiment"] == "negative":
                    sell += weight; sell_reasons.append(f"브리핑 {period} negative ({b.get('mention')}회)")
                break

    # 7) 공매도 (5%)
    sh = sections.get("short") or {}
    if sh.get("available") and sh.get("score", 0) >= 2:
        sell += 2; sell_reasons.append(f"공매도: {sh['triggers'][0] if sh.get('triggers') else ''}")

    return {
        "sell_score": sell,
        "buy_score": buy,
        "net": sell - buy,
        "sell_reasons": sell_reasons,
        "buy_reasons": buy_reasons,
    }


# === Section 11: 포지션 사이징 + 전략 ===

def recommend_strategy(sections, basic, integrated):
    """단기/중기/장기 전략.

    사용자 선호도: 중장기 수익 극대화.
    → 단기 매도 시그널은 보조 지표, 중장기는 펀더멘털·테마·DART가 핵심
    """
    qty = basic.get("qty", 0)
    avg = basic.get("avg_price", 0)
    pat = sections.get("pattern", {}) or {}
    cur = pat.get("current", avg)
    pnl_pct = (cur/avg-1)*100 if avg > 0 else 0

    sell = integrated["sell_score"]
    buy  = integrated["buy_score"]
    net  = integrated["net"]

    # 테마 펀더멘털 강함 여부
    th = sections.get("theme", {}) or {}
    theme_strong = th.get("available") and th.get("glob_avg_60d", 0) > 30
    decoupling = th.get("decoupling") if th.get("available") else None
    alpha_overshoot = th.get("alpha_vs_global", 0) > 200 if th.get("available") else False

    # DART 시그널 (중장기 핵심)
    d = sections.get("dart", {}) or {}
    insider_strong_sell = d.get("available") and (d.get("n_ins_sells", 0) >= 2 or d.get("n_major_dec", 0) >= 2)
    insider_strong_buy  = d.get("available") and d.get("score", 0) <= -3

    # 단기 전략 (1주~1개월)
    short_term = []
    if pat.get("pattern") == "분배" and net >= 5:
        short_term.append(f"분배 진행 — 반등 시 부분 매도")
    if pat.get("pattern") == "펌프":
        short_term.append("펌프 패턴 — 트레일링 스탑 -10% 권고")
    sm5 = (sections.get("investor") or {}).get("smart_5d", 0)
    if sm5 < -200:
        short_term.append(f"스마트머니 5일 {sm5:+.0f}억 매도 — 추세 반전 전 신중")
    if not short_term:
        short_term.append("단기 특이점 없음")

    # 중기 전략 (1~3개월) — 사용자 선호 영역
    mid_term = []
    if insider_strong_sell:
        mid_term.append("⚠️ 인사이더 매도 강함 — 펀더멘털 약화 신호. 비중 축소 검토")
    if alpha_overshoot:
        mid_term.append(f"오버슈팅 (vs 글로벌 +{th.get('alpha_vs_global', 0):.0f}%p) — 평균회귀 가능, 부분 차익실현")
    if decoupling and "한국만 약세" in (decoupling or ""):
        mid_term.append("한국 Peer 단독 약세 — 외국인·기관 관심 식는 중. 비중 축소")
    if theme_strong and not insider_strong_sell and not alpha_overshoot:
        mid_term.append("테마 펀더멘털 강세 + 인사이더 안정 → 코어 보유")
    if not mid_term:
        mid_term.append("중기 시그널 명확하지 않음 — 추적 모니터링")

    # 장기 전략 (6개월~) — 펀더멘털·테마 본질
    long_term = []
    if theme_strong:
        long_term.append("테마 다년 트렌드 ✓ 코어 보유 가치")
    else:
        long_term.append("테마 모멘텀 약함 → 코어 보유 매력 낮음")
    if insider_strong_buy:
        long_term.append("내부자 매수 우세 — 장기 매수 후보")
    if insider_strong_sell:
        long_term.append("내부자 매도 진행 — 장기 보유 재고")

    # 포지션 사이징 권고 (사용자 중장기 선호 반영)
    sizing = []
    if not basic.get("is_holding"):
        # 미보유 (관심 종목)
        if buy > sell + 3 and theme_strong:
            sizing.append("🟢 매수 후보 — 펀더멘털·시그널 우호")
        elif net >= 5:
            sizing.append("⛔ 매수 보류 — 분배 진행 중")
        else:
            sizing.append("⚪ 관망")
    else:
        # 보유 중 — 사용자 선호 (중장기 수익 극대화) 반영
        if pnl_pct >= 200 and net >= 8 and (insider_strong_sell or alpha_overshoot):
            sizing.append(f"🚨 1/3 익절 — 이익 +{pnl_pct:.0f}% 큰 폭 + 강한 매도 시그널 + 펀더멘털 약화")
            sizing.append(f"   잔량 {qty*2//3:,}주 코어 보유 (테마 자체는 살아있음)")
        elif pnl_pct >= 100 and net >= 5 and (insider_strong_sell or alpha_overshoot):
            sizing.append(f"🟡 1/4 익절 — 이익 +{pnl_pct:.0f}% + 매도 시그널")
            sizing.append(f"   잔량 {qty*3//4:,}주 보유")
        elif pnl_pct >= 50 and net >= 5 and not theme_strong:
            sizing.append(f"🟡 1/4 익절 — 테마 약화")
        elif pnl_pct <= -15 and insider_strong_sell:
            sizing.append(f"⛔ 손절 검토 — 손실 {pnl_pct:.0f}% + 인사이더 매도")
        elif pnl_pct <= -15 and not theme_strong:
            sizing.append(f"⛔ 손절 검토 — 테마 약화 + 손실 {pnl_pct:.0f}%")
        elif buy > sell + 3 and theme_strong:
            sizing.append(f"📈 추가 매수 후보 — 매수 우세 + 테마 강세")
        else:
            sizing.append(f"🟢 HOLD — 코어 보유 (이익 {pnl_pct:+.0f}%, net {net:+d})")

    return {
        "pnl_pct": pnl_pct,
        "short_term": short_term,
        "mid_term": mid_term,
        "long_term": long_term,
        "sizing": sizing,
        "key_signals": {
            "theme_strong": theme_strong,
            "decoupling": decoupling,
            "alpha_overshoot": alpha_overshoot,
            "insider_strong_sell": insider_strong_sell,
            "insider_strong_buy": insider_strong_buy,
        },
    }


# === Master orchestrator ===

def deep_analyze(code: str, verbose: bool = True) -> dict:
    sections = {}
    t0 = time.time()

    if verbose:
        print(f"\n{'='*100}")
        print(f"  종목 정밀 분석 시작 — {code}")
        print(f"{'='*100}\n")

    steps = [
        ("basic",         "기본 정보",          lambda: get_basic_info(code)),
        ("macro",         "매크로",            lambda: get_macro()),
        ("theme",         "테마/Peer",          lambda: get_theme(code)),
        ("dart",          "DART 인사이더",      lambda: get_dart(code)),
        ("investor",      "수급(투자자)",       lambda: get_investor(code)),
        ("broker",        "창구(거래원)",       lambda: get_broker_window(code)),
        ("short",         "공매도",             lambda: get_short(code)),
        ("pattern",       "차트/패턴",          lambda: get_pattern(code)),
    ]

    for key, label, fn in steps:
        ts = time.time()
        sections[key] = fn() or {"available": False, "error": "no result"}
        if verbose:
            avail = sections[key].get("available", True) if isinstance(sections[key], dict) else True
            mark = "✓" if avail else "✗"
            print(f"  {mark} {label:<14} {time.time()-ts:>5.1f}s")

    # 뉴스/브리핑 (basic에서 name 필요)
    name = sections["basic"].get("name", "?") if sections["basic"] else "?"
    sections["news_briefing"] = get_news_briefing(code, name)
    if verbose:
        print(f"  ✓ 뉴스/브리핑")

    # 통합
    sections["integrated"] = compute_integrated(sections)
    sections["strategy"] = recommend_strategy(sections, sections["basic"], sections["integrated"])

    if verbose:
        print(f"\n  분석 완료 — 총 {time.time()-t0:.1f}초\n")

    return sections


# === 터미널 리포트 (DETAILED) ===

def _hr(char="─", width=110):
    return char * width

def _section_header(num, title):
    print(f"\n{'═'*110}")
    print(f"  [{num}] {title}")
    print(f"{'═'*110}")


def print_report(sections):
    basic = sections["basic"]
    pat = sections.get("pattern", {}) or {}
    cur_price = pat.get("current", basic.get("avg_price", 0))
    pnl_pct = (cur_price/basic["avg_price"]-1)*100 if basic.get("avg_price", 0) > 0 else 0
    pnl_amt = basic["qty"] * (cur_price - basic["avg_price"]) if basic.get("is_holding") else 0

    print(f"\n{'█'*110}")
    print(f"  🔬 {basic['name']} ({basic['code']}) — 정밀 분석")
    if basic["is_holding"]:
        print(f"  보유 {basic['qty']:,}주 / 평단 {basic['avg_price']:,.0f}원 / 현재 {cur_price:,.0f}원 / PnL {pnl_pct:+.1f}% ({pnl_amt/1e4:+,.0f}만원)")
    else:
        print(f"  (미보유 — 관심 종목)")
    print(f"  분석 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'█'*110}")

    # ═════════════════════════════════════════════════════════════════════════
    # [1] 매크로
    # ═════════════════════════════════════════════════════════════════════════
    m = sections["macro"]
    if m.get("_overall"):
        _section_header(1, "매크로 시장 진단 (한국+미국)")
        o = m["_overall"]
        print(f"  📊 종합 평가: {o['label']} (점수 {o['score']:+d})")
        print()

        cats = [
            ("🇰🇷 KOREA",  ["코스피", "코스닥"]),
            ("🇺🇸 US",     ["S&P500", "나스닥", "다우"]),
            ("⚡ RISK",    ["VIX"]),
            ("💱 FX",     ["원달러", "달러인덱스"]),
            ("📈 RATE",   ["미국10Y", "미국13W"]),
            ("🛢 RAWMAT", ["WTI유가", "금"]),
        ]
        print(f"  {'카테고리':<12}{'지수':<14}{'현재':>11} {'D-1':>7} {'D-5':>7} {'D-20':>7} {'D-60':>8} {'D-120':>8} {'고점대비':>9} {'200일선':>7} {'Regime':>8}")
        print(f"  {_hr()}")
        for cat_label, names in cats:
            for i, name in enumerate(names):
                r = m.get(name, {})
                if not r.get("available"):
                    continue
                ma = "위" if r.get("above_ma200") else "아래"
                cat_show = cat_label if i == 0 else ""
                print(f"  {cat_show:<12}{name:<14}{r['current']:>11,.2f} {fmt_pct(r['d1']):>7} {fmt_pct(r['d5']):>7} {fmt_pct(r['d20']):>7} {fmt_pct(r['d60']):>8} {fmt_pct(r['d120']):>8} {fmt_pct(r['from_peak']):>9} {ma:>7} {r['regime']:>8}")
        print()
        print(f"  💡 시그널 사유:")
        for r in o["reasons"]:
            print(f"     • {r}")

    # ═════════════════════════════════════════════════════════════════════════
    # [2] 테마/Value Chain/Peer
    # ═════════════════════════════════════════════════════════════════════════
    th = sections["theme"]
    if th.get("available"):
        _section_header(2, f"테마 / Value Chain / 글로벌 Peer — {th['theme']} (Tier {th['tier']})")
        print(f"  📌 {th['description']}")
        t = th["target_ret"]
        print()
        print(f"  ⭐ 우리 종목 ({th['stock_name']}, {th.get('biz','')})")
        print(f"     현재 {t['current']:>10,.0f}  D-1 {fmt_pct(t['d1']):>7}  D-5 {fmt_pct(t['d5']):>7}  D-20 {fmt_pct(t['d20']):>7}  D-60 {fmt_pct(t['d60']):>8}  고점 {fmt_pct(t['from_peak']):>8}")

        # 글로벌 Peer
        print(f"\n  🌐 글로벌 Peer (Tier 1/2 — 펀더멘털 직접 수혜)")
        print(f"     {'Tier':<6}{'심볼':<8}{'이름':<26}{'D-5':>7} {'D-20':>7} {'D-60':>7} {'corr':>6} {'beta':>6} {'lag':>4}")
        print(f"     {_hr('-', 80)}")
        for p in th.get("global_peers", []):
            r = p["ret"]; cr = p["corr"]
            if not r: continue
            c = f"{cr['corr']:.2f}" if cr and cr.get("corr") is not None else "─"
            b = f"{cr['beta']:.2f}" if cr and cr.get("beta") is not None else "─"
            lag = f"{cr['best_lag']:+d}d" if cr else "─"
            print(f"     {p['tier']:<6}{p['symbol']:<8}{p['name']:<26}{fmt_pct(r['d5']):>7} {fmt_pct(r['d20']):>7} {fmt_pct(r['d60']):>7} {c:>6} {b:>6} {lag:>4}")

        # 한국 Peer
        print(f"\n  🇰🇷 한국 동종 (Tier 3 — 테마 베타)")
        print(f"     {'코드':<8}{'이름':<14}{'사업':<24}{'D-5':>7} {'D-20':>7} {'D-60':>7} {'corr':>6} {'beta':>6}")
        print(f"     {_hr('-', 90)}")
        for p in th.get("korea_peers", []):
            r = p["ret"]; cr = p["corr"]
            if not r: continue
            c = f"{cr['corr']:.2f}" if cr and cr.get("corr") is not None else "─"
            b = f"{cr['beta']:.2f}" if cr and cr.get("beta") is not None else "─"
            print(f"     {p['code']:<8}{p['name']:<14}{p['biz']:<24}{fmt_pct(r['d5']):>7} {fmt_pct(r['d20']):>7} {fmt_pct(r['d60']):>7} {c:>6} {b:>6}")

        # ETF
        if th.get("etfs"):
            print(f"\n  📦 관련 ETF (글로벌 자금 흐름 proxy)")
            print(f"     {'심볼':<10}{'D-5':>7} {'D-20':>7} {'D-60':>7} {'corr':>6}")
            print(f"     {_hr('-', 45)}")
            for e in th["etfs"]:
                r = e["ret"]; cr = e["corr"]
                if not r: continue
                c = f"{cr['corr']:.2f}" if cr and cr.get("corr") is not None else "─"
                print(f"     {e['symbol']:<10}{fmt_pct(r['d5']):>7} {fmt_pct(r['d20']):>7} {fmt_pct(r['d60']):>7} {c:>6}")

        # 종합
        print(f"\n  💡 테마 강도 진단")
        print(f"     글로벌 평균: 60일 {fmt_pct(th.get('glob_avg_60d')):>8}  5일 {fmt_pct(th.get('glob_avg_5d')):>7}")
        print(f"     한국 평균:   60일 {fmt_pct(th.get('kor_avg_60d')):>8}  5일 {fmt_pct(th.get('kor_avg_5d')):>7}")
        print(f"     알파 vs 글로벌:  {fmt_pct(th.get('alpha_vs_global')):>8}")
        print(f"     알파 vs 한국:    {fmt_pct(th.get('alpha_vs_kor')):>8}")
        print(f"     → {th.get('theme_momentum')}")
        if th.get("decoupling"):
            print(f"     → {th['decoupling']}")

    # ═════════════════════════════════════════════════════════════════════════
    # [3] DART 인사이더
    # ═════════════════════════════════════════════════════════════════════════
    d = sections["dart"]
    if d.get("available"):
        _section_header(3, "DART 인사이더 / 대주주 / 자사주 (180일)")
        clr = "🔴" if d['score'] >= 5 else ("🟢" if d['score'] <= -3 else "🟡")
        print(f"  {clr} DART 종합 점수: {d['score']:+d} (양수=매도, 음수=매수)")
        print(f"     임원 거래: 매수 {d['n_ins_buys']}건 ({d['ins_buy_qty']:,}주) / 매도 {d['n_ins_sells']}건 ({d['ins_sell_qty']:,}주)")
        print(f"     5%주주:   증가 {d['n_major_inc']}건 / 감소 {d['n_major_dec']}건")
        print(f"     자사주:   취득결정 {d['n_ts_buys']}건 / 처분결정 {d['n_ts_sells']}건")

        # 임원 거래 상세
        if d.get("insiders"):
            print(f"\n  📋 임원 거래 상세 (최신순)")
            print(f"     {'일자':<10}{'이름':<14}{'직위':<14}{'증감수':>13}{'보유 후':>13}{'비율증감':>9}{'보유율':>8}")
            print(f"     {_hr('-', 90)}")
            for ins in sorted(d["insiders"], key=lambda x: x["date"], reverse=True)[:10]:
                name = (ins.get("name") or "")[:12]
                pos = (ins.get("position") or "")[:12]
                rate_irds = ins.get("rate_irds") or "─"
                rate = ins.get("after_rate") or "─"
                qty_str = f"{ins['change_qty']:+,}"
                after_str = f"{ins.get('after_qty',0):,}"
                print(f"     {ins['date']:<10}{name:<14}{pos:<14}{qty_str:>13}{after_str:>13}{rate_irds:>9}{rate:>8}")

        # 5%주주 변동 상세
        if d.get("major"):
            print(f"\n  📋 5%주주 변동 상세 (최신순)")
            print(f"     {'일자':<10}{'보유자':<22}{'보유주식수':>13}{'증감':>13}{'비율':>8}{'비고':<30}")
            print(f"     {_hr('-', 100)}")
            for mj in sorted(d["major"], key=lambda x: x["date"], reverse=True)[:10]:
                holder = (mj.get("holder") or "")[:20]
                stkqy = f"{mj['stkqy']:,}"
                irds = f"{mj['stkqy_irds']:+,}"
                rate = str(mj.get("stkrt", ""))[:6]
                # 5%선 통과 분석
                note = ""
                try:
                    rate_f = float(rate) if rate and rate != "─" else 0
                    if rate_f < 5 and rate_f > 0:
                        note = "⚠️ 5%선 아래 = 추가 매도 공시 의무 없음"
                    elif rate_f >= 10:
                        note = "🔵 10%+ 대주주"
                except: pass
                print(f"     {mj['date']:<10}{holder:<22}{stkqy:>13}{irds:>13}{rate:>8}  {note}")

        # 자사주 결정
        if d.get("treasury"):
            print(f"\n  📋 자사주 취득/처분 결정")
            for ts in d["treasury"][:5]:
                kind = "🟢 취득" if ts["type"] == "buy" else "🔴 처분"
                print(f"     {ts['date']} {kind} {ts.get('qty',0):,}주 / {ts.get('amount',0):,}원 — {ts.get('purpose','')}")

    # ═════════════════════════════════════════════════════════════════════════
    # [4] 수급 (KIS 30일 일별)
    # ═════════════════════════════════════════════════════════════════════════
    inv = sections["investor"]
    if inv.get("available"):
        _section_header(4, f"수급 (KIS 투자자별 {inv['n_days']}일)")
        print(f"  📊 누적 흐름 (백만원→억 환산)")
        print(f"     {'구분':<12}{'5일':>14}{'20일':>14}{'추세':<10}")
        print(f"     {_hr('-', 60)}")
        for label, k5, k20 in [
            ("외국인", inv['foreign_5d'], inv['foreign_20d']),
            ("기관",   inv['inst_5d'],    inv['inst_20d']),
            ("개인",   inv['retail_5d'],  None),
            ("스마트머니", inv['smart_5d'], inv['smart_20d']),
        ]:
            k20_str = f"{k20:+,.0f}억" if k20 is not None else "─"
            arrow = ""
            if k20 is not None:
                if k5 < -100 and k20 > 100: arrow = "▼ 매도 전환"
                elif k5 > 100 and k20 < -100: arrow = "▲ 매수 전환"
                elif k5 < 0 and k20 < 0: arrow = "▼▼ 매도 지속"
                elif k5 > 0 and k20 > 0: arrow = "▲▲ 매수 지속"
            print(f"     {label:<12}{k5:>+12.0f}억 {k20_str:>13}  {arrow:<10}")

        # 일별 상세 (최근 15일)
        data = inv.get("data", [])
        if data:
            print(f"\n  📅 일별 매매 (최근 15일, 백만원→억)")
            print(f"     {'일자':<12}{'종가':>9}{'등락':>7}{'거래량':>12}{'외국인':>10}{'기관':>10}{'개인':>10}{'스마트':>10}")
            print(f"     {_hr('-', 90)}")
            for f in sorted(data, key=lambda x: x["date"], reverse=True)[:15]:
                d_ = f.get("date", "")
                d_iso = f"{d_[:4]}-{d_[4:6]}-{d_[6:8]}" if len(d_) == 8 else d_
                close = f.get("close", 0)
                chg = f.get("change", 0)
                chg_pct = (chg/(close-chg)*100) if close-chg > 0 else 0
                fr = f.get("foreign_amt", 0)/100
                ins = f.get("inst_amt", 0)/100
                ret = f.get("personal_amt", 0)/100
                smart = fr + ins
                # KIS는 거래량 정보 없으니 0 표시
                print(f"     {d_iso:<12}{close:>9,}{chg_pct:>+6.1f}% {' '*5:>11} {fr:>+8.0f}억 {ins:>+8.0f}억 {ret:>+8.0f}억 {smart:>+8.0f}억")

    # ═════════════════════════════════════════════════════════════════════════
    # [5] 창구 (60일 거래원 분석)
    # ═════════════════════════════════════════════════════════════════════════
    bk = sections["broker"]
    if bk.get("available"):
        _section_header(5, f"창구 / 거래원 분석 ({bk['lookback_days']}일, 활성 거래원 {bk['n_brokers']}개)")
        print(f"  👥 그룹별 누적 매매 (주식 단위)")
        print(f"     {'그룹':<10}{'창구수':>5}{'60일 누적':>14}{'5일':>14}{'10일':>14}{'20일':>14}")
        print(f"     {_hr('-', 80)}")
        for g in ["외국계", "대형국내", "개미창구", "기타"]:
            gd = bk["groups"].get(g, {})
            if gd.get("n", 0) == 0: continue
            print(f"     {g:<10}{gd['n']:>5}{fmt_qty(gd['total_net']):>14}{fmt_qty(gd['net5']):>14}{fmt_qty(gd['net10']):>14}{fmt_qty(gd['net20']):>14}")

        # 매수 TOP 7
        print(f"\n  📈 매수 TOP 7 (60일 누적)")
        print(f"     {'순':<3}{'거래원':<22}{'그룹':<10}{'60일':>13}{'5일':>13}{'10일':>13}{'20일':>13}")
        print(f"     {_hr('-', 90)}")
        for i, r in enumerate(bk["top_buys"][:7], 1):
            print(f"     {i:<3}{r['name'][:20]:<22}{r.get('group','?'):<10}{fmt_qty(r['total_net']):>13}{fmt_qty(r['net5']):>13}{fmt_qty(r['net10']):>13}{fmt_qty(r['net20']):>13}")

        # 매도 TOP 7
        print(f"\n  📉 매도 TOP 7 (60일 누적)")
        print(f"     {'순':<3}{'거래원':<22}{'그룹':<10}{'60일':>13}{'5일':>13}{'10일':>13}{'20일':>13}")
        print(f"     {_hr('-', 90)}")
        for i, r in enumerate(bk["top_sells"][:7], 1):
            print(f"     {i:<3}{r['name'][:20]:<22}{r.get('group','?'):<10}{fmt_qty(r['total_net']):>13}{fmt_qty(r['net5']):>13}{fmt_qty(r['net10']):>13}{fmt_qty(r['net20']):>13}")

        # 분배 시점 일별 매트릭스 — 매수 TOP 5 + 매도 TOP 5의 최근 12일 매매
        print(f"\n  🔥 분배/매집 시점 매트릭스 (최근 12거래일)")
        # 핵심 거래원 10개
        key_brokers = bk["top_buys"][:5] + bk["top_sells"][:5]
        # 일자별
        all_dates = set()
        for r in key_brokers:
            for row in r.get("data", []):
                all_dates.add(row["date"])
        if all_dates:
            recent_dates = sorted(all_dates)[-12:]
            # 헤더: 거래원 이름 (단축)
            short_names = [r["name"][:6] for r in key_brokers]
            print(f"     {'일자':<10}" + " ".join(f"{n:>9}" for n in short_names))
            print(f"     {_hr('-', 100)}")
            for date in recent_dates:
                row = f"     {date[:4]}-{date[4:6]}-{date[6:]}"
                cells = []
                for r in key_brokers:
                    found = next((x for x in r.get("data", []) if x["date"] == date), None)
                    if found:
                        cells.append(f"{found['net']:>+9,}")
                    else:
                        cells.append(f"{'─':>9}")
                print(f"  {row} " + " ".join(cells))

    # ═════════════════════════════════════════════════════════════════════════
    # [6] 공매도
    # ═════════════════════════════════════════════════════════════════════════
    sh = sections["short"]
    if sh.get("available"):
        _section_header(6, "공매도 / 대차 잔고")
        print(f"  📊 잔고율 {sh.get('last_balance_pct',0):.2f}% / 당일 공매도 비중 {sh.get('last_short_ratio',0):.1f}%")
        if sh.get("balance_5d_pct") is not None:
            print(f"     잔고 5일 변화: {sh['balance_5d_pct']:+.1f}%")
        if sh.get("triggers"):
            print(f"  💡 시그널:")
            for t in sh["triggers"][:5]:
                print(f"     • {t}")
        # 일별 추이
        sh_data = sh.get("data", [])
        if sh_data:
            print(f"\n  📅 일별 공매도 (최근 10일)")
            print(f"     {'일자':<12}{'잔고주수':>14}{'잔고율':>8}{'당일공매도':>14}{'당일비중':>9}")
            print(f"     {_hr('-', 60)}")
            for s in sorted(sh_data, key=lambda x: x.get("date",""), reverse=True)[:10]:
                d_ = s.get("date", "")
                d_iso = f"{d_[:4]}-{d_[4:6]}-{d_[6:8]}" if len(d_) == 8 else d_
                bal = s.get("short_balance_qty", 0) or 0
                bal_pct = s.get("short_balance_pct", 0) or 0
                today = s.get("short_vol", 0) or 0
                today_pct = s.get("short_ratio", 0) or 0
                print(f"     {d_iso:<12}{bal:>14,}{bal_pct:>7.2f}% {today:>13,} {today_pct:>7.1f}%")

    # ═════════════════════════════════════════════════════════════════════════
    # [7] 차트/패턴
    # ═════════════════════════════════════════════════════════════════════════
    if pat.get("available"):
        _section_header(7, "차트 / 패턴 / 매물대")
        print(f"  💹 현재 {pat['current']:>10,.0f}원  /  60일 고점 {pat['peak']:>10,.0f}원  ({fmt_pct(pat['from_peak'])})")
        print(f"     모멘텀:  D-5 {fmt_pct(pat['chg_5'])}  /  D-20 {fmt_pct(pat['chg_20'])}  /  D-60 {fmt_pct(pat['chg_60'])}")
        print(f"     거래량:  최근 5일 / 직전 20일 = {pat['vol_ratio']:.2f}x")
        print(f"     변동성:  최근 5일 / 직전 20일 = {pat['atr_ratio']:.2f}x")
        print(f"  🎯 패턴 분류: {pat['pattern']}")
        ps = pat.get("pattern_scores", {})
        if ps:
            print(f"     패턴 점수: 분배 {ps.get('분배',0)} / 펌프 {ps.get('펌프',0)} / 잔여 {ps.get('잔여',0)} / 정상 {ps.get('정상',0)}")

        # 매물대
        if pat.get("supply_levels"):
            print(f"\n  📊 매물대 분포 (60일)")
            for sl in pat["supply_levels"]:
                bar = "█" * max(1, int(sl["pct"]))
                marker = " ← 현재" if abs(pat['current'] - sl['price']) < 1000 else ""
                print(f"     {sl['price']-1000:>6,}~{sl['price']+1000:>6,}원: {sl['pct']:>5.1f}% {bar}{marker}")

        # 장대음봉
        if pat.get("bear_days"):
            print(f"\n  🚨 최근 장대음봉 (천정 신호)")
            for b in pat["bear_days"][:5]:
                print(f"     {b['date']}: 등락 {b['chg']:+.1f}% / 몸통 {b['body']:.1f}%")

    # ═════════════════════════════════════════════════════════════════════════
    # [8] 뉴스/브리핑
    # ═════════════════════════════════════════════════════════════════════════
    nb = sections["news_briefing"]
    if nb.get("available"):
        _section_header(8, "뉴스 / 브리핑 (텔레그램 + 블로그 + 뉴스)")

        # 브리핑 (daily/weekly/monthly 모두)
        for period in ["daily", "weekly", "biweekly", "monthly"]:
            b = nb["briefing"].get(period)
            if b:
                clr = "🟢" if b.get("sentiment") == "positive" else ("🔴" if b.get("sentiment") == "negative" else "⚪")
                print(f"  {clr} 브리핑 [{period}]: mention {b['mention']}회 / {b['channels']}채널 / sentiment={b['sentiment']}")
                if b.get("context"):
                    ctx = b["context"]
                    # 80자씩 wrap
                    for i in range(0, min(len(ctx), 400), 90):
                        print(f"     {ctx[i:i+90]}")
                print()

        # 종목 뉴스
        n = nb.get("news")
        if n and isinstance(n, dict):
            sentiment = n.get("sentiment", "─")
            sentiment_clr = "🟢" if sentiment == "positive" else ("🔴" if sentiment == "negative" else "⚪")
            print(f"  {sentiment_clr} 뉴스 종합 (sentiment={sentiment}, 기사 {n.get('article_count',0)}건)")
            if n.get("summary"):
                summary = n["summary"]
                for i in range(0, min(len(summary), 600), 90):
                    print(f"     {summary[i:i+90]}")
            if n.get("keywords"):
                print(f"     키워드: {' / '.join(n['keywords'])}")
            if n.get("articles"):
                print(f"\n  📰 기사 목록 (최근 {min(len(n['articles']),8)}건)")
                for i, a in enumerate(n["articles"][:8], 1):
                    title = a.get("title", "")[:90]
                    print(f"     {i}. {title}")

    # ═════════════════════════════════════════════════════════════════════════
    # [9] 통합 점수
    # ═════════════════════════════════════════════════════════════════════════
    ig = sections["integrated"]
    _section_header(9, "통합 점수 (멀티소스)")
    print(f"  🔴 매도 점수: {ig['sell_score']:>3}     🟢 매수 점수: {ig['buy_score']:>3}     ⚖️  net = {ig['net']:+d}")
    print(f"\n  🔴 매도 사유 ({len(ig['sell_reasons'])}개):")
    for i, r in enumerate(ig["sell_reasons"], 1):
        print(f"     {i:>2}. {r}")
    print(f"\n  🟢 매수 사유 ({len(ig['buy_reasons'])}개):")
    for i, r in enumerate(ig["buy_reasons"], 1):
        print(f"     {i:>2}. {r}")

    # ═════════════════════════════════════════════════════════════════════════
    # [10] 전략 + 포지션 권고
    # ═════════════════════════════════════════════════════════════════════════
    st = sections["strategy"]
    _section_header(10, "전략 / 포지션 권고  (사용자 선호: 중장기 수익 극대화)")
    print(f"  📅 단기 전략 (1주 ~ 1개월):")
    for s in st["short_term"]:
        print(f"     • {s}")
    print(f"\n  🗓️  중기 전략 (1 ~ 3개월) — 핵심 영역:")
    for s in st["mid_term"]:
        print(f"     • {s}")
    print(f"\n  📆 장기 전략 (6개월+):")
    for s in st["long_term"]:
        print(f"     • {s}")
    print(f"\n  💼 포지션 권고:")
    for s in st["sizing"]:
        print(f"     {s}")

    # 액션 시나리오 (가격대별)
    if pat.get("available") and basic.get("is_holding"):
        print(f"\n  🎯 가격대별 액션 시나리오")
        cur = pat["current"]
        peak = pat["peak"]
        from_peak = pat["from_peak"]
        # 매물대 위/아래
        sup_levels = pat.get("supply_levels", [])
        upper = [s for s in sup_levels if s["price"] > cur]
        lower = [s for s in sup_levels if s["price"] < cur]
        if upper:
            top_upper = sorted(upper, key=lambda x: -x["pct"])[:2]
            for sl in top_upper:
                pct_to = (sl["price"]/cur - 1) * 100
                print(f"     ↑ {sl['price']:,}원 도달 시 ({pct_to:+.0f}%): 추가 매도 검토 (매물대 {sl['pct']:.1f}%)")
        if peak > cur:
            pct_to_peak = (peak/cur - 1) * 100
            print(f"     ↑ 60일 고점 {peak:,.0f}원 ({pct_to_peak:+.0f}%): 잔량 매도 트리거")
        if lower:
            top_lower = sorted(lower, key=lambda x: -x["pct"])[:2]
            for sl in top_lower:
                pct_to = (sl["price"]/cur - 1) * 100
                print(f"     ↓ {sl['price']:,}원 ({pct_to:+.0f}%): 매수세 시험점 / 손절 검토")
    print()


# === HTML 리포트 ===

def write_html(sections, out_path: str):
    basic = sections["basic"]
    ig = sections["integrated"]
    st = sections["strategy"]

    def section_block(title, html):
        return f'<div class="card"><h2>{title}</h2>{html}</div>'

    # 매크로
    m = sections["macro"]
    macro_html = "<p>데이터 없음</p>"
    if m.get("_overall"):
        o = m["_overall"]
        rows = "".join(f"<li>{r}</li>" for r in o["reasons"][:5])
        macro_html = f"<p><b>{o['label']}</b> (점수 {o['score']:+d})</p><ul>{rows}</ul>"

    # 테마
    th = sections["theme"]
    theme_html = "<p>데이터 없음</p>"
    if th.get("available"):
        peer_rows = ""
        for p in th.get("global_peers", []):
            r = p["ret"]
            if r:
                peer_rows += f"<tr><td>{p['tier']}</td><td>{p['symbol']}</td><td>{p['name']}</td><td class='mono'>{fmt_pct(r['d5'])}</td><td class='mono'>{fmt_pct(r['d20'])}</td><td class='mono'>{fmt_pct(r['d60'])}</td></tr>"
        kor_rows = ""
        for p in th.get("korea_peers", []):
            r = p["ret"]; cr = p["corr"]
            if r:
                c = f"{cr['corr']:.2f}" if cr and cr.get("corr") is not None else "─"
                kor_rows += f"<tr><td>{p['code']}</td><td>{p['name']}</td><td>{p['biz']}</td><td class='mono'>{fmt_pct(r['d5'])}</td><td class='mono'>{fmt_pct(r['d60'])}</td><td class='mono'>{c}</td></tr>"
        decop = f"<p style='color:#ef4444'>⚠️ {th['decoupling']}</p>" if th.get("decoupling") else ""
        theme_html = f"""
        <p><b>{th['theme']}</b> · Tier {th['tier']} · {th.get('description','')}</p>
        <p>글로벌 평균 60일 {fmt_pct(th.get('glob_avg_60d'))} | 5일 {fmt_pct(th.get('glob_avg_5d'))}</p>
        <p>한국 평균 60일 {fmt_pct(th.get('kor_avg_60d'))} | 5일 {fmt_pct(th.get('kor_avg_5d'))}</p>
        <p>알파 vs 글로벌: <b>{fmt_pct(th.get('alpha_vs_global'))}</b></p>
        <p>{th.get('theme_momentum','')}</p>
        {decop}
        <h3>글로벌 Peer</h3>
        <table><tr><th>Tier</th><th>심볼</th><th>이름</th><th>D-5</th><th>D-20</th><th>D-60</th></tr>{peer_rows}</table>
        <h3>한국 Peer</h3>
        <table><tr><th>코드</th><th>이름</th><th>사업</th><th>D-5</th><th>D-60</th><th>corr</th></tr>{kor_rows}</table>
        """

    # DART
    d = sections["dart"]
    dart_html = "<p>데이터 없음</p>"
    if d.get("available"):
        ins_rows = ""
        for ins in (d.get("insiders") or [])[:8]:
            ins_rows += f"<tr><td>{ins['date']}</td><td>{ins.get('name','')}</td><td>{ins.get('position','')}</td><td class='mono'>{ins['change_qty']:+,}</td><td class='mono'>{ins.get('after_qty',0):,}</td></tr>"
        maj_rows = ""
        for mj in (d.get("major") or [])[:8]:
            maj_rows += f"<tr><td>{mj['date']}</td><td>{mj.get('holder','')}</td><td class='mono'>{mj['stkqy']:,}</td><td class='mono'>{mj['stkqy_irds']:+,}</td><td>{mj.get('stkrt','')}</td></tr>"
        clr = "#ef4444" if d['score'] >= 5 else ("#10b981" if d['score'] <= -3 else "#f59e0b")
        dart_html = f"""
        <p>DART 점수: <b style="color:{clr}">{d['score']:+d}</b> (양수=매도, 음수=매수)</p>
        <p>임원: 매수 {d['n_ins_buys']}건 ({d['ins_buy_qty']:,}주) / 매도 {d['n_ins_sells']}건 ({d['ins_sell_qty']:,}주)</p>
        <p>5%주주: 증가 {d['n_major_inc']}건 / 감소 {d['n_major_dec']}건</p>
        <p>자사주: 취득 {d['n_ts_buys']}건 / 처분 {d['n_ts_sells']}건</p>
        <h3>임원 거래</h3>
        <table><tr><th>일자</th><th>이름</th><th>직위</th><th>증감</th><th>보유 후</th></tr>{ins_rows or '<tr><td colspan=5>없음</td></tr>'}</table>
        <h3>5%주주</h3>
        <table><tr><th>일자</th><th>보유자</th><th>보유수</th><th>증감</th><th>비율</th></tr>{maj_rows or '<tr><td colspan=5>없음</td></tr>'}</table>
        """

    # 수급
    inv = sections["investor"]
    inv_html = "<p>데이터 없음</p>"
    if inv.get("available"):
        inv_html = f"""
        <table>
        <tr><th>구분</th><th>5일</th><th>20일</th></tr>
        <tr><td>외국인</td><td class='mono'>{inv['foreign_5d']:+.0f}억</td><td class='mono'>{inv['foreign_20d']:+.0f}억</td></tr>
        <tr><td>기관</td><td class='mono'>{inv['inst_5d']:+.0f}억</td><td class='mono'>{inv['inst_20d']:+.0f}억</td></tr>
        <tr><td>개인</td><td class='mono'>{inv['retail_5d']:+.0f}억</td><td>─</td></tr>
        <tr><td><b>스마트머니</b></td><td class='mono'><b>{inv['smart_5d']:+.0f}억</b></td><td class='mono'><b>{inv['smart_20d']:+.0f}억</b></td></tr>
        </table>
        """

    # 창구
    bk = sections["broker"]
    bk_html = "<p>데이터 없음</p>"
    if bk.get("available"):
        gr_rows = ""
        for g in ["외국계","대형국내","개미창구","기타"]:
            gd = bk["groups"].get(g, {})
            if gd.get("n", 0) == 0: continue
            gr_rows += f"<tr><td><b>{g}</b></td><td class='mono'>{fmt_qty(gd['total_net'])}</td><td class='mono'>{fmt_qty(gd['net5'])}</td><td class='mono'>{fmt_qty(gd['net10'])}</td><td class='mono'>{fmt_qty(gd['net20'])}</td><td>{gd['n']}</td></tr>"
        buy_rows = "".join(f"<tr><td>{r['name']}</td><td>{r['group']}</td><td class='mono'>{fmt_qty(r['total_net'])}</td><td class='mono'>{fmt_qty(r['net5'])}</td><td class='mono'>{fmt_qty(r['net20'])}</td></tr>" for r in bk["top_buys"][:7])
        sell_rows = "".join(f"<tr><td>{r['name']}</td><td>{r['group']}</td><td class='mono'>{fmt_qty(r['total_net'])}</td><td class='mono'>{fmt_qty(r['net5'])}</td><td class='mono'>{fmt_qty(r['net20'])}</td></tr>" for r in bk["top_sells"][:7])
        bk_html = f"""
        <p>{bk['lookback_days']}일 누적, 활성 거래원 {bk['n_brokers']}개</p>
        <h3>창구 그룹별</h3>
        <table><tr><th>그룹</th><th>누적</th><th>5일</th><th>10일</th><th>20일</th><th>창구</th></tr>{gr_rows}</table>
        <h3>매수 TOP 7</h3>
        <table><tr><th>거래원</th><th>그룹</th><th>누적</th><th>5일</th><th>20일</th></tr>{buy_rows}</table>
        <h3>매도 TOP 7</h3>
        <table><tr><th>거래원</th><th>그룹</th><th>누적</th><th>5일</th><th>20일</th></tr>{sell_rows}</table>
        """

    # 패턴
    pat = sections["pattern"]
    pat_html = "<p>데이터 없음</p>"
    if pat.get("available"):
        bear_html = "".join(f"<li>장대음봉 {b['date']} ({b['chg']:+.1f}%)</li>" for b in (pat.get("bear_days") or [])[:3])
        sup_html = "".join(f"<li>{b['price']-1000:,}~{b['price']+1000:,}원: {b['pct']:.1f}%</li>" for b in (pat.get("supply_levels") or [])[:6])
        pat_html = f"""
        <p>현재 {pat['current']:,.0f}원 / 고점 {pat['peak']:,.0f}원 ({fmt_pct(pat['from_peak'])})</p>
        <p>5일/20일/60일 모멘텀: {fmt_pct(pat['chg_5'])} / {fmt_pct(pat['chg_20'])} / {fmt_pct(pat['chg_60'])}</p>
        <p>거래량 {pat['vol_ratio']:.2f}x / 변동성 {pat['atr_ratio']:.2f}x</p>
        <p>→ 패턴: <b>{pat['pattern']}</b></p>
        {f"<h3>매물대 (60일)</h3><ul>{sup_html}</ul>" if sup_html else ""}
        {f"<h3>최근 장대음봉</h3><ul>{bear_html}</ul>" if bear_html else ""}
        """

    # 뉴스/브리핑
    nb = sections["news_briefing"]
    nb_html = "<p>데이터 없음</p>"
    if nb.get("available"):
        nb_html = ""
        for period in ["daily","weekly","monthly"]:
            b = nb["briefing"].get(period)
            if b:
                clr = "#10b981" if b.get('sentiment') == 'positive' else ("#ef4444" if b.get('sentiment') == 'negative' else "#9ca3af")
                nb_html += f"<p>[{period}] mention {b['mention']}, {b['channels']}채널, <b style='color:{clr}'>{b['sentiment']}</b><br><small>{b.get('context','')}</small></p>"
        if nb.get("news") and nb["news"].get("summary"):
            nb_html += f"<p>[뉴스] {nb['news']['summary']}</p>"

    # 통합/전략
    sell_reasons_html = "".join(f"<li>{r}</li>" for r in ig["sell_reasons"][:7])
    buy_reasons_html  = "".join(f"<li>{r}</li>" for r in ig["buy_reasons"][:7])
    short_html = "".join(f"<li>{s}</li>" for s in st["short_term"])
    mid_html   = "".join(f"<li>{s}</li>" for s in st["mid_term"])
    long_html  = "".join(f"<li>{s}</li>" for s in st["long_term"])
    sizing_html = "".join(f"<li>{s}</li>" for s in st["sizing"])

    integ_html = f"""
    <div style="display:flex;gap:20px;flex-wrap:wrap">
      <div style="flex:1;min-width:280px"><h3 style="color:#ef4444">매도 점수: {ig['sell_score']}</h3><ul>{sell_reasons_html}</ul></div>
      <div style="flex:1;min-width:280px"><h3 style="color:#10b981">매수 점수: {ig['buy_score']}</h3><ul>{buy_reasons_html}</ul></div>
    </div>
    <p style="font-size:1.2em">net = <b>{ig['net']:+d}</b></p>
    """

    strategy_html = f"""
    <div style="display:flex;gap:20px;flex-wrap:wrap">
      <div style="flex:1;min-width:280px;border-left:3px solid #f59e0b;padding-left:14px">
        <h3>📅 단기 (1주~1개월)</h3><ul>{short_html}</ul>
      </div>
      <div style="flex:1;min-width:280px;border-left:3px solid #4fc3f7;padding-left:14px">
        <h3>🗓️ 중기 (1~3개월)</h3><ul>{mid_html}</ul>
      </div>
      <div style="flex:1;min-width:280px;border-left:3px solid #10b981;padding-left:14px">
        <h3>📆 장기 (6개월+)</h3><ul>{long_html}</ul>
      </div>
    </div>
    <h3 style="margin-top:20px">💼 포지션 권고</h3><ul>{sizing_html}</ul>
    """

    pnl_str = f"평단 {basic['avg_price']:,.0f} / 보유 {basic['qty']:,}주 / PnL {st['pnl_pct']:+.1f}%" if basic["is_holding"] else "(미보유 — 관심 종목)"

    html = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>{basic['name']} ({basic['code']}) — 정밀 분석</title>
<link rel="stylesheet" href="../assets/style.css">
<style>
  .card {{ background: #1a1a1a; border-radius: 8px; padding: 20px; margin-bottom: 20px; }}
  .card h2 {{ margin-top: 0; color: #4fc3f7; border-bottom: 1px solid #333; padding-bottom: 8px; }}
  .card h3 {{ color: #ddd; }}
  table {{ width: 100%; border-collapse: collapse; margin: 10px 0; }}
  th, td {{ padding: 6px 10px; border-bottom: 1px solid #333; text-align: left; }}
  th {{ background: #222; }}
  .mono {{ font-family: monospace; text-align: right; }}
  ul {{ padding-left: 20px; }}
  li {{ margin: 4px 0; }}
</style>
</head><body>
<div class="container">

<h1>🔬 {basic['name']} ({basic['code']}) — 정밀 분석</h1>
<p class="subtitle">{pnl_str} · 분석 시각 {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

{section_block("[1] 매크로", macro_html)}
{section_block("[2] 테마/Value Chain/Peer", theme_html)}
{section_block("[3] DART 인사이더 (180일)", dart_html)}
{section_block("[4] 수급 (KIS 30일)", inv_html)}
{section_block("[5] 창구 (60일)", bk_html)}
{section_block("[6] 차트/패턴", pat_html)}
{section_block("[7] 뉴스/브리핑", nb_html)}
{section_block("[8] 통합 점수", integ_html)}
{section_block("[9] 전략 (단기/중기/장기) + 포지션 권고", strategy_html)}

</div></body></html>
"""

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


# === 메인 ===

def main():
    p = argparse.ArgumentParser()
    p.add_argument("code", help="종목코드 (6자리)")
    p.add_argument("--html", action="store_true", help="HTML 리포트도 생성")
    p.add_argument("--json", help="JSON 결과 저장 경로")
    p.add_argument("-q", "--quiet", action="store_true", help="진행 메시지 끄기")
    args = p.parse_args()

    sections = deep_analyze(args.code, verbose=not args.quiet)

    print_report(sections)

    if args.html:
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "dashboard", "stock", f"{args.code}.html")
        write_html(sections, out)
        print(f"\n✓ HTML: {out}")

    if args.json:
        # 데이터프레임 등 직렬화 안 되는 것 정리
        def clean(o):
            if hasattr(o, "to_dict"): return o.to_dict()
            if hasattr(o, "isoformat"): return o.isoformat()
            return o
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(sections, f, ensure_ascii=False, indent=2, default=clean)
        print(f"✓ JSON: {args.json}")


if __name__ == "__main__":
    main()
