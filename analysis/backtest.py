"""백테스트 — 우리 매도 시그널이 진짜 작동하나 검증.

방법:
  1. 과거 1년 거래일 (오늘 기준) 중 매 5거래일마다 시뮬레이션 시점 잡음
  2. 그 시점에 synthesis.analyze 돌림 (end_date=그 날짜)
  3. 그 시점의 verdict 기록
  4. 그 시점 + 20거래일 후 실제 수익률/낙폭 측정
  5. 집계:
     - 정확도(precision): "매도주의" 발동 → 실제 -10% 이상 빠진 비율
     - 재현율(recall):    실제 -10% 이상 빠진 케이스 → 우리가 잡은 비율
     - 리드타임:          신호 → 실제 하락까지 며칠

추가 — 비교 baseline:
  - "항상 홀드" 전략 vs "매도주의 발동 시 매도" 전략의 수익률 차이
"""
import os, sys, json, warnings
import pandas as pd
from datetime import datetime, timedelta
warnings.filterwarnings("ignore")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.db import query_df
from analysis import synthesis


# 백테스트 설정
TEST_LOOKBACK_DAYS    = 365     # 과거 며칠치 검증
TEST_INTERVAL_DAYS    = 5       # 며칠마다 시뮬레이션 시점 (5거래일 = 1주일)
FORWARD_WINDOW_DAYS   = 20      # 신호 후 며칠까지 추적
DROP_THRESHOLD        = -0.10   # 실제 "큰 하락" 정의: 20일 내 -10%


# 등급별 점수 (분석 시 매도 신호로 간주할 임계값)
SELL_GRADES = {"강한 매도주의", "매도주의"}


def _load_price_series(code: str) -> pd.DataFrame:
    df = query_df("SELECT date, close, low FROM prices WHERE code = ? ORDER BY date", (code,))
    if df.empty: return df
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date").sort_index()


def _forward_label(price: pd.DataFrame, anchor: pd.Timestamp,
                   window: int = FORWARD_WINDOW_DAYS) -> dict:
    """anchor 시점 이후 window 거래일 수익률 + 최대 낙폭."""
    after = price.loc[price.index > anchor].head(window)
    if len(after) == 0:
        return {"available": False}
    base = price.loc[anchor, "close"]
    end_close = after["close"].iloc[-1]
    min_low   = after["low"].min()
    return {
        "available":    True,
        "ret":          float(end_close / base - 1),
        "max_dd":       float(min_low / base - 1),
        "n_days":       len(after),
    }


def backtest_one_stock(code: str, name: str = "") -> list:
    """1종목 시뮬레이션 → list of records.

    각 레코드:
      {date, verdict_synth, verdict_narr, grade_stat, fwd_ret, fwd_dd, n_days, current_close}
    """
    price = _load_price_series(code)
    if price.empty: return []

    today = price.index[-1]
    cutoff_start = today - timedelta(days=TEST_LOOKBACK_DAYS)
    test_dates = price.loc[cutoff_start:today - timedelta(days=FORWARD_WINDOW_DAYS+5)].index
    test_dates = test_dates[::TEST_INTERVAL_DAYS]

    from analysis import narrative, statistical
    from analysis.synthesis import combine_verdicts

    records = []
    for i, d in enumerate(test_dates):
        d_str = str(d.date())
        narr = narrative.analyze(code, name=name, end_date=d_str)
        stat = statistical.analyze(code, name=name, end_date=d_str)
        narr_v = narr.get("verdict", "랠리 없음")
        stat_g = stat.get("grade", "홀드")
        grade, conf, _ = combine_verdicts(narr_v, stat_g)

        fwd = _forward_label(price, d)
        if not fwd.get("available"): continue

        records.append({
            "date":          d_str,
            "close":         int(price.loc[d, "close"]),
            "verdict":       grade,
            "narrative":     narr_v,
            "statistical":   stat_g,
            "confidence":    conf,
            "fwd_ret":       fwd["ret"],
            "fwd_dd":        fwd["max_dd"],
            "n_days":        fwd["n_days"],
        })

        if (i + 1) % 10 == 0:
            print(f"    {i+1}/{len(test_dates)}", end=" ", flush=True)
    print()
    return records


def confusion_matrix(records: list, drop_threshold: float = DROP_THRESHOLD) -> dict:
    """매도 신호 정확도 vs 실제 하락."""
    if not records:
        return {}

    df = pd.DataFrame(records)
    df["actual_drop"] = df["fwd_dd"] <= drop_threshold
    df["called_sell"] = df["verdict"].isin(SELL_GRADES)

    tp = ((df["called_sell"]) & (df["actual_drop"])).sum()
    fp = ((df["called_sell"]) & (~df["actual_drop"])).sum()
    fn = ((~df["called_sell"]) & (df["actual_drop"])).sum()
    tn = ((~df["called_sell"]) & (~df["actual_drop"])).sum()

    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall    = tp / (tp + fn) if (tp + fn) > 0 else None

    # 등급별 평균 미래 수익/낙폭
    by_grade = df.groupby("verdict").agg(
        n=("date", "count"),
        avg_fwd_ret=("fwd_ret", "mean"),
        avg_fwd_dd=("fwd_dd", "mean"),
        median_fwd_dd=("fwd_dd", "median"),
        drop_rate=("actual_drop", "mean"),
    ).round(4)

    return {
        "n_total":   len(df),
        "n_drops":   int(df["actual_drop"].sum()),
        "n_signals": int(df["called_sell"].sum()),
        "TP": int(tp), "FP": int(fp), "FN": int(fn), "TN": int(tn),
        "precision": precision, "recall": recall,
        "by_grade":  by_grade.to_dict(orient="index"),
    }


def baseline_comparison(records: list) -> dict:
    """'홀드' vs '매도주의 시 매도(20일 후 재진입)' 수익률 비교 — 거친 추정."""
    if not records: return {}
    df = pd.DataFrame(records)

    hold_avg = df["fwd_ret"].mean()                   # 항상 홀드 평균
    sell_signals = df[df["verdict"].isin(SELL_GRADES)]
    nonsell = df[~df["verdict"].isin(SELL_GRADES)]

    # "매도주의 회피" 전략 — 매도주의 시점은 진입 안 함
    avg_when_holding = nonsell["fwd_ret"].mean() if len(nonsell) else None
    avg_avoided      = sell_signals["fwd_ret"].mean() if len(sell_signals) else None

    return {
        "hold_avg_fwd_ret":       float(hold_avg),
        "avoided_avg_fwd_ret":    float(avg_avoided) if avg_avoided is not None else None,
        "non_sell_avg_fwd_ret":   float(avg_when_holding) if avg_when_holding is not None else None,
        "n_sell_signals":         int(len(sell_signals)),
        "n_non_sell":             int(len(nonsell)),
    }


def main(codes_names: list):
    print(f"{'='*60}\n  백테스트 — {len(codes_names)}종목 × {TEST_LOOKBACK_DAYS}일\n{'='*60}\n")

    all_records = {}
    all_metrics = {}
    for name, code in codes_names:
        print(f"[{name} ({code})]")
        rec = backtest_one_stock(code, name=name)
        if not rec:
            print("    데이터 부족")
            continue
        all_records[code] = rec
        cm = confusion_matrix(rec)
        bl = baseline_comparison(rec)
        all_metrics[code] = {"cm": cm, "baseline": bl, "name": name}

        print(f"    시뮬레이션 {cm['n_total']}회, 실제 큰 하락 {cm['n_drops']}회, "
              f"매도 신호 {cm['n_signals']}회")
        if cm.get("precision") is not None:
            print(f"    precision: {cm['precision']*100:.1f}%, recall: {cm['recall']*100:.1f}%")
        print(f"    홀드 평균 fwd_ret: {bl['hold_avg_fwd_ret']*100:+.1f}%, "
              f"매도주의 회피 평균: {bl['avoided_avg_fwd_ret']*100 if bl['avoided_avg_fwd_ret'] else 0:+.1f}%")
        print()

    # 종합 보고서
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"backtest_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    with open(out, "w") as f:
        json.dump({"records": all_records, "metrics": all_metrics}, f,
                  ensure_ascii=False, indent=2, default=str)
    print(f"결과 저장: {out}")

    # markdown 요약
    md_out = out.replace(".json", ".md")
    with open(md_out, "w") as f:
        f.write(render_summary(all_metrics))
    print(f"요약: {md_out}")


def render_summary(metrics: dict) -> str:
    md = ["# 백테스트 결과 요약\n"]
    md.append(f"실행: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    md.append(f"기간: 과거 {TEST_LOOKBACK_DAYS}일, {TEST_INTERVAL_DAYS}일 간격, 신호 후 {FORWARD_WINDOW_DAYS}일 추적\n")
    md.append(f"\"큰 하락\" 정의: {FORWARD_WINDOW_DAYS}일 내 {DROP_THRESHOLD*100:.0f}% 이상\n")

    md.append(f"\n## 종목별 결과\n")
    md.append("| 종목 | 시뮬 | 실제 하락 | 매도 신호 | precision | recall | 홀드 fwd | 회피 fwd |")
    md.append("|---|---|---|---|---|---|---|---|")
    for code, m in metrics.items():
        cm = m["cm"]; bl = m["baseline"]
        prec = f"{cm['precision']*100:.1f}%" if cm.get("precision") is not None else "-"
        rec  = f"{cm['recall']*100:.1f}%"    if cm.get("recall")    is not None else "-"
        avoid = f"{bl['avoided_avg_fwd_ret']*100:+.1f}%" if bl.get("avoided_avg_fwd_ret") is not None else "-"
        md.append(f"| {m['name']} | {cm['n_total']} | {cm['n_drops']} | {cm['n_signals']} "
                  f"| {prec} | {rec} | {bl['hold_avg_fwd_ret']*100:+.1f}% | {avoid} |")

    md.append(f"\n## 종목별 등급 분포 (등급 → fwd 수익률 평균)\n")
    for code, m in metrics.items():
        md.append(f"\n### {m['name']} ({code})")
        md.append("| 등급 | n | avg fwd_ret | avg fwd_dd | 실제 하락률 |")
        md.append("|---|---|---|---|---|")
        for grade, row in m["cm"].get("by_grade", {}).items():
            md.append(f"| {grade} | {row['n']} | {row['avg_fwd_ret']*100:+.1f}% "
                      f"| {row['avg_fwd_dd']*100:+.1f}% | {row['drop_rate']*100:.0f}% |")

    return "\n".join(md)


if __name__ == "__main__":
    import argparse
    from file_io import load_json
    from config import STOCK_MAP_FILE

    p = argparse.ArgumentParser()
    p.add_argument("codes", nargs="*")
    p.add_argument("--names", nargs="*")
    args = p.parse_args()

    smap = load_json(STOCK_MAP_FILE, default={})
    code_to_name = {info["code"]: name for name, info in smap.items() if "code" in info}
    targets = []
    for c in args.codes or []: targets.append((code_to_name.get(c, c), c))
    for n in args.names or []:
        info = smap.get(n)
        if info and "code" in info: targets.append((n, info["code"]))

    if not targets:
        # default — 4 v0 stocks
        targets = [
            ("대한광통신", "010170"),
            ("삼천당제약", "000250"),
            ("제이스로보틱스", "090470"),
            ("코오롱티슈진", "950160"),
        ]
    main(targets)
