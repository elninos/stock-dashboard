"""사건 추적 분석 — "이번 랠리 누가 만들었나?" 부터 시작.

흐름 (친구분 분석 자동화):
  1. rally 자동 식별  최근 120일 내 저점 → 고점 (또는 현재)
  2. 그 구간 주역 추출  거래원 Top N + 투자자별 누적
  3. 주역 현재 상태 추적  최근 5/20일 강도 vs 랠리 평균
  4. 판정  주역 약화/이탈 → 매도 신호

이건 "이 종목 = 이 주역들" 캐릭터를 그리는 게 본질.
통계는 statistical.py가 담당, 여기는 정성 분석.
"""
import pandas as pd
import numpy as np
from datetime import timedelta
from .features import aggregate, broker as brk_feat
import sys, os
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from core.db import query_df


# 랠리 식별 임계값
RALLY_LOOKBACK_DAYS   = 120          # 얼마나 거슬러 올라가서 저점 찾을지
RALLY_MIN_GAIN        = 0.20         # 랠리로 인정할 최소 상승률
TOP_BROKER_N          = 5            # 주역으로 뽑을 거래원 수


# ──────────────────────────────────────────
# 1) 랠리 자동 식별
# ──────────────────────────────────────────

def find_rally(df_price: pd.DataFrame) -> dict:
    """현재 또는 최근 랠리 식별.

    정의:
      rally_start = lookback 기간 내 최저 종가 시점
      rally_end   = rally_start 이후 최고 종가 시점 (= 현재 또는 직전 고점)
      magnitude   = (rally_end_price - rally_start_price) / rally_start_price

    랠리 < MIN_GAIN이면 None 반환 (= 상승 사건 없음).
    """
    if df_price.empty: return None
    recent = df_price.tail(RALLY_LOOKBACK_DAYS).copy()

    start_idx = recent["close"].idxmin()
    after_start = recent.loc[start_idx:]
    if len(after_start) < 5: return None

    end_idx = after_start["close"].idxmax()
    start_price = recent.loc[start_idx, "close"]
    end_price   = after_start.loc[end_idx, "close"]
    magnitude   = (end_price / start_price) - 1
    if magnitude < RALLY_MIN_GAIN:
        return None

    # 랠리 종료 후 며칠 지났나 (현재 vs 고점)
    last_idx = recent.index[-1]
    days_since_peak = (last_idx - end_idx).days

    return {
        "start_date":      str(start_idx.date()),
        "end_date":        str(end_idx.date()),
        "current_date":    str(last_idx.date()),
        "start_price":     int(start_price),
        "peak_price":      int(end_price),
        "current_price":   int(recent.loc[last_idx, "close"]),
        "magnitude":       float(magnitude),
        "days_since_peak": int(days_since_peak),
        "is_active":       days_since_peak <= 5,   # 5일 내 고점 = 진행 중
    }


# ──────────────────────────────────────────
# 2) 주역 추출 (rally 기간 내)
# ──────────────────────────────────────────

def find_brokers(code: str, start: str, end: str, top_n: int = TOP_BROKER_N) -> list:
    """rally 구간 내 거래원별 누적 net 매수 → Top N."""
    sql = """
        SELECT broker_name,
               SUM(buy)  AS total_buy,
               SUM(sell) AS total_sell,
               SUM(net)  AS total_net,
               COUNT(DISTINCT date) AS active_days
        FROM member_daily
        WHERE code = ? AND date BETWEEN ? AND ?
        GROUP BY broker_name
        ORDER BY total_net DESC
    """
    df = query_df(sql, (code, start, end))
    if df.empty: return []

    total_buy_all = df["total_buy"].sum() or 1
    df["share_of_total_buy"] = df["total_buy"] / total_buy_all

    return df.head(top_n).to_dict(orient="records")


def find_investors(code: str, start: str, end: str) -> dict:
    """rally 구간 내 투자자별 누적."""
    sql = """
        SELECT SUM(foreign_qty) AS f_qty, SUM(foreign_amt) AS f_amt,
               SUM(inst_qty)    AS i_qty, SUM(inst_amt)    AS i_amt,
               SUM(retail_qty)  AS r_qty, SUM(retail_amt)  AS r_amt
        FROM investor_flow
        WHERE code = ? AND date BETWEEN ? AND ?
    """
    df = query_df(sql, (code, start, end))
    if df.empty or df.iloc[0].isna().all(): return {}
    r = df.iloc[0]
    return {
        "foreign": {"qty": int(r["f_qty"] or 0), "amt": int(r["f_amt"] or 0)},
        "inst":    {"qty": int(r["i_qty"] or 0), "amt": int(r["i_amt"] or 0)},
        "retail":  {"qty": int(r["r_qty"] or 0), "amt": int(r["r_amt"] or 0)},
    }


def find_big_days(code: str, start: str, end: str) -> list:
    """rally 구간 내 빅데이 — 단일 거래원 net이 그 기간 평균+2σ 이상인 날."""
    sql = """
        SELECT date, broker_name, MAX(net) AS max_net
        FROM (
            SELECT date, broker_name, net FROM member_daily
            WHERE code = ? AND date BETWEEN ? AND ?
        )
        GROUP BY date
    """
    df = query_df(sql, (code, start, end))
    if df.empty: return []

    mean = df["max_net"].mean(); std = df["max_net"].std()
    threshold = mean + 2 * std if std and not np.isnan(std) else mean * 1.5

    big = df[df["max_net"] >= threshold].copy()
    big = big.sort_values("date").to_dict(orient="records")

    # 클러스터링 — 인접 빅데이는 그룹화
    clusters = []
    current = []
    for d in big:
        if not current:
            current = [d]; continue
        prev_date = pd.to_datetime(current[-1]["date"])
        cur_date  = pd.to_datetime(d["date"])
        if (cur_date - prev_date).days <= 5:
            current.append(d)
        else:
            clusters.append(current); current = [d]
    if current: clusters.append(current)

    return [{
        "start": c[0]["date"], "end": c[-1]["date"], "n_days": len(c),
        "broker": c[0]["broker_name"], "total_net": int(sum(d["max_net"] for d in c)),
    } for c in clusters]


# ──────────────────────────────────────────
# 3) 주역 추적 (현재 상태)
# ──────────────────────────────────────────

def track_broker(code: str, broker_name: str, end_date: str,
                 rally_start: str, rally_end: str,
                 rally_total_net: int, lookback_days: int = 20) -> dict:
    """단일 주역 거래원의 최근 활동 — 랠리 평균 강도와 비교.

    핵심 로직:
      rally 기간 동안 daily_avg = rally_total_net / 활성일수
      현재 5일 강도 vs 5 × daily_avg → 비율로 "약화" 판정
        ratio < 30%  → 약화
        ratio < 0    → 매도 전환 (이탈)
    """
    end = pd.to_datetime(end_date)
    start = (end - timedelta(days=lookback_days * 2)).strftime("%Y-%m-%d")
    sql = """
        SELECT date, buy, sell, net FROM member_daily
        WHERE code = ? AND broker_name = ? AND date >= ? AND date <= ?
        ORDER BY date
    """
    df = query_df(sql, (code, broker_name, start, str(end.date())))
    if df.empty: return {"available": False}

    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    last_5  = int(df.tail(5)["net"].sum())
    last_20 = int(df.tail(20)["net"].sum())

    # rally 평균 일별 net (활성일 기준)
    sql_active = """
        SELECT COUNT(DISTINCT date) FROM member_daily
        WHERE code = ? AND broker_name = ? AND date BETWEEN ? AND ?
    """
    cnt = query_df(sql_active, (code, broker_name, rally_start, rally_end)).iloc[0, 0] or 1
    rally_daily_avg = rally_total_net / cnt
    expected_5d = rally_daily_avg * 5

    # 강도 비율 (현재 5일 / 랠리 평균 5일)
    if expected_5d > 0:
        strength_ratio = last_5 / expected_5d
    else:
        strength_ratio = None

    # 판정
    if last_5 < 0 and rally_total_net > 0:
        status = "이탈"; reversed_to_sell = True; weakening = False
    elif strength_ratio is not None and strength_ratio < 0.30:
        status = "약화"; reversed_to_sell = False; weakening = True
    elif strength_ratio is not None and strength_ratio < 0.70:
        status = "둔화"; reversed_to_sell = False; weakening = True
    elif last_5 > 0:
        status = "강함"; reversed_to_sell = False; weakening = False
    else:
        status = "혼조"; reversed_to_sell = False; weakening = False

    return {
        "available":          True,
        "broker":             broker_name,
        "last_5d_net":        last_5,
        "last_20d_net":       last_20,
        "rally_daily_avg":    float(rally_daily_avg),
        "expected_5d":        float(expected_5d),
        "strength_ratio":     float(strength_ratio) if strength_ratio is not None else None,
        "reversed_to_sell":   reversed_to_sell,
        "weakening":          weakening,
        "status":             status,
    }


def track_investors(code: str, end_date: str, lookback_days: int = 20) -> dict:
    """투자자별 최근 활동."""
    end = pd.to_datetime(end_date)
    start_5  = (end - timedelta(days=10)).strftime("%Y-%m-%d")
    start_20 = (end - timedelta(days=30)).strftime("%Y-%m-%d")

    def sum_for(start, end):
        sql = """
            SELECT SUM(foreign_qty) f_qty, SUM(foreign_amt) f_amt,
                   SUM(inst_qty)    i_qty, SUM(inst_amt)    i_amt,
                   SUM(retail_qty)  r_qty, SUM(retail_amt)  r_amt
            FROM investor_flow WHERE code = ? AND date BETWEEN ? AND ?
        """
        df = query_df(sql, (code, start, end))
        return df.iloc[0].fillna(0).to_dict() if not df.empty else {}

    s5  = sum_for(start_5,  str(end.date()))
    s20 = sum_for(start_20, str(end.date()))

    return {
        "5d":  {"foreign_amt": int(s5.get("f_amt") or 0), "inst_amt": int(s5.get("i_amt") or 0),
                "retail_amt": int(s5.get("r_amt") or 0)},
        "20d": {"foreign_amt": int(s20.get("f_amt") or 0), "inst_amt": int(s20.get("i_amt") or 0),
                "retail_amt": int(s20.get("r_amt") or 0)},
    }


# ──────────────────────────────────────────
# 4) 종합 분석
# ──────────────────────────────────────────

def analyze(code: str, name: str = "", end_date: str = None) -> dict:
    """단일 종목 사건 추적 분석.

    end_date: 분석 기준일 (없으면 가장 최근). 백테스트에서 과거 시점 시뮬레이션 시 사용.
    """
    df = aggregate.get_all_features(code, start="2020-01-01",
                                     end=end_date)
    if df.empty or len(df) < 30:
        return {"code": code, "name": name, "error": "데이터 부족"}

    # rally 식별 — 가격만 보면 충분
    rally = find_rally(df[["close"]])
    if rally is None:
        return {
            "code": code, "name": name,
            "as_of": str(df.index[-1].date()),
            "rally": None, "verdict": "랠리 없음",
            "summary": "최근 120일 내 +20% 이상 상승 사건 없음",
        }

    # 주역 추출 (rally 시작일부터 현재 또는 고점까지)
    end_for_drivers = rally["end_date"]
    brokers = find_brokers(code, rally["start_date"], end_for_drivers, top_n=TOP_BROKER_N)
    investors = find_investors(code, rally["start_date"], end_for_drivers)
    big_day_clusters = find_big_days(code, rally["start_date"], end_for_drivers)

    # 주역 추적 (현재 시점 기준)
    as_of = str(df.index[-1].date())
    broker_tracking = []
    weakening_count = 0; reversal_count = 0
    for b in brokers[:3]:    # Top 3만 추적
        if b["total_net"] <= 0: continue
        track = track_broker(code, b["broker_name"], as_of,
                              rally_start=rally["start_date"], rally_end=rally["end_date"],
                              rally_total_net=int(b["total_net"]), lookback_days=20)
        track["rally_total_net"] = int(b["total_net"])
        track["rally_share"]     = float(b["share_of_total_buy"])
        broker_tracking.append(track)
        if track.get("reversed_to_sell"): reversal_count += 1
        elif track.get("weakening"):       weakening_count += 1

    investor_tracking = track_investors(code, as_of)

    # 빅데이 부재 판정 (모멘텀 고갈) — verdict에 반영
    last_big_day = big_day_clusters[-1]["end"] if big_day_clusters else None
    days_since_big = None
    if last_big_day:
        days_since_big = (df.index[-1] - pd.to_datetime(last_big_day)).days
    big_day_extinct = (days_since_big is not None and days_since_big > 30)

    # 종합 판정 — 다층
    n_drivers = len([t for t in broker_tracking if t.get("available")])
    if n_drivers == 0:
        verdict = "추적불가"; summary = "주역 거래원 추적 데이터 부족"
    else:
        # 점수 — 매도 전환 2점, 약화 1점, 빅데이 고갈 1점
        score = reversal_count * 2 + weakening_count + (1 if big_day_extinct else 0)

        if score >= 4:
            verdict = "강한 매도 신호"
        elif score >= 3:
            verdict = "매도 신호"
        elif score >= 2:
            verdict = "주의"
        elif score >= 1:
            verdict = "약한 주의"
        else:
            verdict = "유지"

        # 요약 문구
        parts = []
        if reversal_count: parts.append(f"주역 {reversal_count}명 매도 전환")
        if weakening_count: parts.append(f"주역 {weakening_count}명 약화")
        if big_day_extinct: parts.append(f"빅데이 {days_since_big}일 부재 (모멘텀 고갈)")
        if not parts: parts.append("주역 매수 강도 유지")
        summary = " + ".join(parts)

    return {
        "code":              code,
        "name":              name,
        "as_of":             as_of,
        "rally":             rally,
        "drivers": {
            "brokers":   brokers,
            "investors": investors,
            "big_day_clusters": big_day_clusters,
        },
        "tracking": {
            "brokers":   broker_tracking,
            "investors": investor_tracking,
            "big_day": {
                "last":         last_big_day,
                "days_since":   days_since_big,
                "is_extinct":   (days_since_big is not None and days_since_big > 30),
            },
        },
        "verdict": verdict,
        "summary": summary,
        "weakening_count": weakening_count,
        "reversal_count":  reversal_count,
    }


# ──────────────────────────────────────────
# 5) 리포트 렌더링
# ──────────────────────────────────────────

def render_report(r: dict) -> str:
    md = []
    md.append(f"# {r.get('name','')} ({r['code']}) — 사건 추적 분석")
    md.append(f"기준일: {r.get('as_of','')}")

    if "error" in r:
        md.append(f"\n**에러**: {r['error']}")
        return "\n".join(md)

    rally = r.get("rally")
    if not rally:
        md.append(f"\n## 결론: {r.get('verdict','')}")
        md.append(r.get("summary",""))
        return "\n".join(md)

    md.append(f"\n## 1. 이번 랠리")
    md.append(f"- 기간: {rally['start_date']} ~ {rally['end_date']}")
    md.append(f"- 가격: {rally['start_price']:,}원 → {rally['peak_price']:,}원 ({rally['magnitude']*100:+.0f}%)")
    md.append(f"- 현재가: {rally['current_price']:,}원 (고점 후 {rally['days_since_peak']}일)")
    md.append(f"- 진행: {'진행중' if rally['is_active'] else '조정/하락 국면'}")

    md.append(f"\n## 2. 누가 만든 랠리인가")
    md.append(f"\n### 거래원 Top {TOP_BROKER_N} (rally 기간 net 매수)")
    md.append("| 순위 | 거래원 | net (주) | 매수점유 | 매매일 |")
    md.append("|---|---|---|---|---|")
    for i, b in enumerate(r["drivers"]["brokers"], 1):
        md.append(f"| {i} | {b['broker_name']} | {b['total_net']:+,} | {b['share_of_total_buy']*100:.1f}% | {b['active_days']} |")

    inv = r["drivers"]["investors"]
    if inv:
        md.append(f"\n### 투자자별 (rally 기간 누적)")
        md.append("| 주체 | 순매수 (주) | 순매수 (억) |")
        md.append("|---|---|---|")
        for k, label in [("foreign","외국인"),("inst","기관"),("retail","개인")]:
            v = inv.get(k, {})
            qty = v.get("qty",0); amt = v.get("amt",0) / 1e8
            md.append(f"| {label} | {qty:+,} | {amt:+.1f} |")

    bd = r["drivers"]["big_day_clusters"]
    md.append(f"\n### 빅데이 클러스터 ({len(bd)}개)")
    if bd:
        for c in bd:
            md.append(f"- {c['start']} ~ {c['end']} ({c['n_days']}일) — {c['broker']} +{c['total_net']:,}주")
    else:
        md.append("없음")

    md.append(f"\n## 3. 주역 현재 상태")
    md.append("| 거래원 | rally net | 5일 net | 5일/예상 | 상태 |")
    md.append("|---|---|---|---|---|")
    for t in r["tracking"]["brokers"]:
        icon = {"이탈":"🔴","약화":"🟠","둔화":"🟡","강함":"🟢","혼조":"⚪"}.get(t["status"],"")
        sr = t.get("strength_ratio")
        sr_s = f"{sr*100:.0f}%" if sr is not None else "-"
        md.append(f"| {t['broker']} | {t['rally_total_net']:+,} | {t['last_5d_net']:+,} | {sr_s} | {icon} {t['status']} |")

    inv5 = r["tracking"]["investors"]["5d"]
    inv20 = r["tracking"]["investors"]["20d"]
    md.append(f"\n### 투자자 최근 흐름 (억원)")
    md.append("| 주체 | 5일 | 20일 |")
    md.append("|---|---|---|")
    for k, label in [("foreign_amt","외국인"),("inst_amt","기관"),("retail_amt","개인")]:
        v5 = inv5.get(k,0)/1e8; v20 = inv20.get(k,0)/1e8
        md.append(f"| {label} | {v5:+.1f} | {v20:+.1f} |")

    bigday = r["tracking"]["big_day"]
    md.append(f"\n### 빅데이 동향")
    if bigday["last"]:
        ext = "❌ 모멘텀 고갈" if bigday["is_extinct"] else ("⚠️ 둔화 가능" if bigday["days_since"]>15 else "✅ 활성")
        md.append(f"- 마지막 빅데이: {bigday['last']} ({bigday['days_since']}일 전) — {ext}")
    else:
        md.append("- 빅데이 없음 (rally 자체에 단일창구 집중 부재)")

    md.append(f"\n## 4. 판정")
    md.append(f"### **{r['verdict']}** — {r['summary']}")
    md.append(f"\n근거:")
    md.append(f"- 매도 전환 주역: {r['reversal_count']}명")
    md.append(f"- 약화 주역: {r['weakening_count']}명")
    if bigday.get("is_extinct"):
        md.append(f"- 빅데이 {bigday['days_since']}일째 부재")

    return "\n".join(md)
