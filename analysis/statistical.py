"""Per-stock analyzer — 과거 고점 패턴 추출 + 현재 상태 평가 + 리포트.

방법론:
  1. Feature 시계열 로드 (analysis.features.aggregate)
  2. 과거 "위험 고점" 식별 — 향후 N일 내 ≥X% 낙폭 + 60일 고가권
  3. 각 peak 시점 feature snapshot 수집
  4. 현재 상태와 비교 — 어떤 지표가 peak 평균/중앙값을 넘었나
  5. 리포트 (markdown) 생성

이건 단일 종목 단위 — 다종목 일괄은 cli.py에서 병렬 호출.
"""
import os, json
import pandas as pd
import numpy as np
from datetime import datetime
from .features import aggregate


# 위험 고점 정의 — 종목별 변동성 보정
PEAK_DD_SIGMA         = 1.5     # 종목 자체 일별 수익률 σ × 1.5 ≤ 20일 내 최대낙폭 → peak
PEAK_DD_FALLBACK      = -0.10   # σ가 너무 작으면 최소 -10% 보장
PEAK_NEAR_HIGH_PCT    = 0.85    # 60일 고가의 85% 이상
PEAK_MIN_RALLY        = 0.20    # 직전 20일 +20% 이상

# 비교용 feature 리스트 (현재 vs 과거 peaks)
RISK_FEATURES = [
    "near_60d_high", "ret_20d",
    "foreign_amt_20d", "inst_amt_20d", "retail_amt_20d",
    "smart_amt_5d", "distribution_score",
    "top_broker_share_60d", "top_broker_net_5d", "buy_concentration_60d",
    "big_day_count_20d", "big_day_recency",
    "short_balance_pct", "short_balance_chg_20d",
]


def find_peaks(df: pd.DataFrame) -> pd.DataFrame:
    """위험 고점 식별 — 종목별 변동성 보정 임계값.

    종목 자체 일별 수익률 σ × √20 × PEAK_DD_SIGMA → 20일 기대 낙폭 임계값
    (예: 일별 σ=3%면 20일 기대 변동 ≈ 13.4%, 1.5σ면 -20%)
    """
    daily_sigma = df["ret_1d"].std()
    sigma_20d   = daily_sigma * (20 ** 0.5)
    threshold   = -sigma_20d * PEAK_DD_SIGMA
    threshold   = min(threshold, PEAK_DD_FALLBACK)   # 최소 -10% 보장

    cond = (
        (df["fwd_max_dd_20d"] <= threshold) &
        (df["near_60d_high"] >= PEAK_NEAR_HIGH_PCT) &
        (df["ret_20d"] >= PEAK_MIN_RALLY)
    )
    peaks = df[cond].copy()
    if peaks.empty: return peaks

    peaks["gap"] = peaks.index.to_series().diff().dt.days.fillna(999)
    peaks = peaks[peaks["gap"] >= 5]
    peaks.attrs["dd_threshold"] = float(threshold)
    peaks.attrs["daily_sigma"]  = float(daily_sigma)
    return peaks


def feature_summary_at_peaks(peaks: pd.DataFrame) -> pd.DataFrame:
    """각 peak 시점의 risk feature 통계 (median, q25, q75)."""
    cols = [c for c in RISK_FEATURES if c in peaks.columns]
    if not cols or peaks.empty:
        return pd.DataFrame()
    summary = peaks[cols].agg(["median", lambda x: x.quantile(0.25), lambda x: x.quantile(0.75), "count"]).T
    summary.columns = ["median", "q25", "q75", "n"]
    return summary


def compare_current_to_peaks(now: pd.Series, peaks: pd.DataFrame) -> list:
    """현재 시점 feature가 과거 peak 분포 어디에 있는지 — 트리거된 항목만 반환."""
    triggers = []
    cols = [c for c in RISK_FEATURES if c in peaks.columns and c in now.index]
    for c in cols:
        cur = now[c]
        if pd.isna(cur): continue
        peak_vals = peaks[c].dropna()
        if peak_vals.empty: continue

        # 방향성 정의 (어느 쪽으로 가야 peak스러운가)
        # higher = peak일 때 더 높음 (예: short_balance_chg, distribution_score, top_broker_share)
        # lower  = peak일 때 더 낮음 (예: smart_amt_5d, top_broker_net_5d)
        direction = "higher" if c in (
            "near_60d_high","ret_20d","retail_amt_20d","distribution_score",
            "top_broker_share_60d","buy_concentration_60d",
            "short_balance_pct","short_balance_chg_20d",
            "big_day_recency",
        ) else "lower"

        med = peak_vals.median()
        q25 = peak_vals.quantile(0.25); q75 = peak_vals.quantile(0.75)

        if direction == "higher" and cur >= med:
            severity = "강함" if cur >= q75 else "보통"
            triggers.append({"feature": c, "current": float(cur),
                             "peak_median": float(med), "peak_q75": float(q75),
                             "direction": direction, "severity": severity})
        elif direction == "lower" and cur <= med:
            severity = "강함" if cur <= q25 else "보통"
            triggers.append({"feature": c, "current": float(cur),
                             "peak_median": float(med), "peak_q25": float(q25),
                             "direction": direction, "severity": severity})
    return triggers


def overall_verdict(triggers: list, n_peaks: int) -> tuple:
    """트리거 개수 + 강도로 등급."""
    if n_peaks == 0:
        return "데이터부족", "과거 위험 고점 사례 부족 — 통계 신뢰도 낮음"
    strong = sum(1 for t in triggers if t["severity"] == "강함")
    total  = len(triggers)
    if strong >= 4:    return "매도주의", f"강한 매도 신호 {strong}개 (총 {total}개 트리거)"
    if strong >= 2:    return "관망",     f"매도 신호 {strong}개 강함 / {total}개 트리거"
    if total >= 3:     return "주의",     f"매도 신호 {total}개 보통 강도"
    return "홀드", f"매도 신호 미약 ({total}개)"


def analyze(code: str, name: str = "", start: str = "2020-01-01",
            end_date: str = None) -> dict:
    """단일 종목 분석. end_date 지정 시 그 시점까지의 데이터로만 분석 (백테스트용)."""
    df = aggregate.get_all_features(code, start=start, end=end_date)
    if df.empty or len(df) < 60:
        return {"code": code, "name": name, "error": "데이터 부족"}

    # peaks: feature 시계열 끝 1년 정도는 fwd_dd가 NaN이므로 제외됨
    peaks = find_peaks(df)
    summary = feature_summary_at_peaks(peaks)

    # 현재 = 가장 최근 가용 행 (전부 NaN 아닌)
    now = df.iloc[-1]
    triggers = compare_current_to_peaks(now, peaks) if not peaks.empty else []
    grade, reason = overall_verdict(triggers, len(peaks))

    # rally driver — 최근 60일 가장 큰 매수 broker (=top_broker_60d)
    driver = now.get("top_broker_60d") if "top_broker_60d" in now.index else None
    driver_share = now.get("top_broker_share_60d", np.nan)
    driver_5d_net = now.get("top_broker_net_5d", np.nan)
    driver_reversed = bool(now.get("top_broker_reversed", False))

    return {
        "code": code,
        "name": name,
        "as_of": str(df.index[-1].date()),
        "current_price": int(now["close"]) if not pd.isna(now["close"]) else None,
        "ret_20d": float(now.get("ret_20d", 0)),
        "near_60d_high": float(now.get("near_60d_high", 0)),
        "n_peaks": len(peaks),
        "peak_dates": [str(d.date()) for d in peaks.index],
        "rally_driver": {
            "broker": driver if isinstance(driver, str) else None,
            "share_60d": float(driver_share) if not pd.isna(driver_share) else None,
            "net_5d_qty": float(driver_5d_net) if not pd.isna(driver_5d_net) else None,
            "reversed_to_sell": driver_reversed,
        },
        "triggers": triggers,
        "grade": grade,
        "reason": reason,
        "peak_feature_summary": summary.round(4).to_dict() if not summary.empty else {},
    }


def render_report(result: dict) -> str:
    """분석 결과 → markdown 리포트."""
    md = []
    md.append(f"# {result.get('name','')} ({result['code']}) 분석 리포트")
    md.append(f"\n생성: {datetime.now().strftime('%Y-%m-%d %H:%M')} | 기준일: {result.get('as_of','')}")
    if "error" in result:
        md.append(f"\n**에러**: {result['error']}")
        return "\n".join(md)

    md.append(f"\n## 요약")
    price = result.get("current_price")
    md.append(f"- 현재가: {price:,}원" if price else "- 현재가: -")
    md.append(f"- 60일 고가 대비: {result['near_60d_high']*100:.1f}%")
    md.append(f"- 최근 20일 수익률: {result['ret_20d']*100:+.1f}%")
    md.append(f"- **판정: {result['grade']}** — {result['reason']}")

    rd = result.get("rally_driver", {})
    if rd.get("broker"):
        md.append(f"\n## 랠리 주도자 (최근 60일)")
        md.append(f"- 단일창구: **{rd['broker']}** (매수 점유율 {rd['share_60d']*100:.1f}%)")
        net5 = rd.get("net_5d_qty")
        if net5 is not None:
            sign = "매수" if net5 >= 0 else "매도"
            md.append(f"- 그 창구 5일 net: {net5:+,.0f}주 ({sign} 강도)")
        if rd.get("reversed_to_sell"):
            md.append(f"- ⚠️ **주도자 매도 전환** — 5일 net 음수, 20일 net 양수")

    md.append(f"\n## 과거 위험 고점 ({result['n_peaks']}건)")
    md.append("기준: 60일 고가 85%+, 20일 +20% 상승 후, 향후 20일 내 ≥15% 낙폭")
    if result["peak_dates"]:
        md.append("\n발생일: " + ", ".join(result["peak_dates"]))
    else:
        md.append("\n과거 사례 없음 — 통계 신뢰도 낮음")

    md.append(f"\n## 현재 상태 트리거 ({len(result['triggers'])}개)")
    if not result["triggers"]:
        md.append("발동 없음.")
    else:
        md.append("| feature | 현재 | peak 중앙값 | 강도 |")
        md.append("|---|---|---|---|")
        for t in result["triggers"]:
            cur = t["current"]
            med = t["peak_median"]
            cur_s = f"{cur:.4g}"
            med_s = f"{med:.4g}"
            md.append(f"| {t['feature']} | {cur_s} | {med_s} | {t['severity']} |")

    summary = result.get("peak_feature_summary", {})
    if summary:
        md.append(f"\n## 과거 peak 시점 feature 통계")
        feats = list(summary.get("median", {}).keys())
        if feats:
            md.append("| feature | median | q25 | q75 | n |")
            md.append("|---|---|---|---|---|")
            for f in feats:
                m = summary["median"].get(f, np.nan)
                q1 = summary["q25"].get(f, np.nan)
                q3 = summary["q75"].get(f, np.nan)
                n = int(summary["n"].get(f, 0))
                md.append(f"| {f} | {m:.4g} | {q1:.4g} | {q3:.4g} | {n} |")

    return "\n".join(md)


def write_report(result: dict, out_dir: str = None) -> str:
    """리포트를 파일로 저장. 경로 반환."""
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(out_dir, exist_ok=True)
    fname = f"{result.get('name','x')}_{result['code']}_{result.get('as_of','')}.md"
    fpath = os.path.join(out_dir, fname)
    with open(fpath, "w") as f:
        f.write(render_report(result))
    # JSON 결과도 함께
    jpath = fpath.replace(".md", ".json")
    with open(jpath, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    return fpath
