"""종목별 시그널 캘리브레이션 — "이 종목엔 어떤 시그널이 어떤 시간축에서 통하나?"

흐름:
  1. 종목별로 과거 N일 동안 매 K일마다 시뮬레이션 시점 잡음
  2. 각 시점에서 narrative + statistical 둘 다 분석 → 등급 기록
  3. 각 시점 + window in [5, 20, 60] 일 후 실제 수익률/낙폭 측정
  4. 그리드 평가:
       (signal_source, window, threshold) 조합별 precision/recall/lift
  5. 종목별 best config = playbook

출력:
  - per-stock JSON (analysis/playbooks/{code}.json)
  - 종합 markdown 리포트
"""
import os, sys, json, warnings
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from collections import defaultdict
warnings.filterwarnings("ignore")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.db import query_df
from analysis import narrative, statistical
from analysis.synthesis import combine_verdicts


# 캘리브레이션 grid
TEST_LOOKBACK_DAYS    = 365
TEST_INTERVAL_DAYS    = 5
WINDOWS               = [5, 20, 60]                       # fwd 일수 후보
SIGNAL_SOURCES        = ["narrative", "statistical", "synthesis"]
SELL_GRADES_MAP = {
    "narrative":   {"강한 매도 신호", "매도 신호", "주의"},  # narrative verdict
    "statistical": {"매도주의", "관망"},                    # statistical grade
    "synthesis":   {"강한 매도주의", "매도주의"},
}

# 종목별 변동성 기반 threshold (window σ × multiplier)
THRESHOLD_SIGMAS = [1.0, 1.5, 2.0]


PLAYBOOK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playbooks")
os.makedirs(PLAYBOOK_DIR, exist_ok=True)


def _load_price(code: str) -> pd.DataFrame:
    df = query_df("SELECT date, close, low, high FROM prices WHERE code = ? ORDER BY date", (code,))
    if df.empty: return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df["ret_1d"] = df["close"].pct_change()
    return df


def collect_records(code: str, name: str = "") -> tuple:
    """과거 시점들에서 narrative+statistical 결과 + 가격 시계열 수집.

    반환: (records_df, price_df)
      records_df: [date, narrative_v, statistical_g, synthesis_g, close]
      price_df: 전체 OHLCV (fwd 라벨 계산용)
    """
    price = _load_price(code)
    if price.empty: return pd.DataFrame(), pd.DataFrame()

    today = price.index[-1]
    cutoff_start = today - timedelta(days=TEST_LOOKBACK_DAYS)
    test_dates = price.loc[cutoff_start:today - timedelta(days=max(WINDOWS)+5)].index
    test_dates = test_dates[::TEST_INTERVAL_DAYS]

    rows = []
    for i, d in enumerate(test_dates):
        d_str = str(d.date())
        narr = narrative.analyze(code, name=name, end_date=d_str)
        stat = statistical.analyze(code, name=name, end_date=d_str)
        narr_v = narr.get("verdict", "랠리 없음")
        stat_g = stat.get("grade",   "홀드")
        synth_g, _, _ = combine_verdicts(narr_v, stat_g)

        rows.append({
            "date":        d,
            "close":       int(price.loc[d, "close"]),
            "narrative":   narr_v,
            "statistical": stat_g,
            "synthesis":   synth_g,
        })
        if (i + 1) % 10 == 0:
            print(f"      {i+1}/{len(test_dates)}", end=" ", flush=True)
    if len(test_dates):
        print()
    return pd.DataFrame(rows), price


def add_forward_labels(records: pd.DataFrame, price: pd.DataFrame) -> pd.DataFrame:
    """records에 각 fwd window별 수익률/낙폭 추가."""
    if records.empty: return records
    out = records.copy().set_index("date")

    for w in WINDOWS:
        rets = []; dds = []
        for d in out.index:
            after = price.loc[price.index > d].head(w)
            if len(after) == 0:
                rets.append(np.nan); dds.append(np.nan); continue
            base = price.loc[d, "close"]
            rets.append(after["close"].iloc[-1] / base - 1)
            dds.append(after["low"].min() / base - 1)
        out[f"fwd_ret_{w}d"] = rets
        out[f"fwd_dd_{w}d"]  = dds

    # 종목 변동성 기반 threshold (window별)
    daily_sigma = price["ret_1d"].std()
    for w in WINDOWS:
        sigma_w = daily_sigma * (w ** 0.5)
        out.attrs = out.attrs or {}
        out[f"sigma_{w}d"] = sigma_w
    return out.reset_index()


def evaluate_config(records: pd.DataFrame, source: str, window: int, sigma_mult: float) -> dict:
    """단일 (source, window, threshold) 조합의 성능 평가."""
    sell_grades = SELL_GRADES_MAP[source]
    df = records.dropna(subset=[f"fwd_dd_{window}d"]).copy()
    if df.empty: return {}

    sigma_w = df[f"sigma_{window}d"].iloc[0] if f"sigma_{window}d" in df.columns else 0.05
    drop_threshold = -sigma_w * sigma_mult
    drop_threshold = min(drop_threshold, -0.05)   # 최소 -5% 보장

    df["actual_drop"]  = df[f"fwd_dd_{window}d"] <= drop_threshold
    df["called_sell"]  = df[source].isin(sell_grades)

    tp = ((df["called_sell"]) & (df["actual_drop"])).sum()
    fp = ((df["called_sell"]) & (~df["actual_drop"])).sum()
    fn = ((~df["called_sell"]) & (df["actual_drop"])).sum()
    tn = ((~df["called_sell"]) & (~df["actual_drop"])).sum()

    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall    = tp / (tp + fn) if (tp + fn) > 0 else None
    base_rate = df["actual_drop"].mean()                  # 무작위 기준선
    lift_vs_random = (precision - base_rate) if precision is not None else None

    # 전략 수익률 (매도 신호 시 회피)
    strategy_ret = df[~df["called_sell"]][f"fwd_ret_{window}d"].mean()
    hold_ret     = df[f"fwd_ret_{window}d"].mean()
    return_lift  = strategy_ret - hold_ret if (strategy_ret is not None and hold_ret is not None) else None

    # 신호 부호 자동 판정 (precision < base_rate면 inverted)
    polarity = "normal"
    if precision is not None and base_rate is not None:
        if precision < base_rate:
            polarity = "inverted"

    return {
        "source":          source,
        "window":          window,
        "sigma_mult":      sigma_mult,
        "threshold":       float(drop_threshold),
        "n_total":         int(len(df)),
        "n_drops":         int(df["actual_drop"].sum()),
        "n_signals":       int(df["called_sell"].sum()),
        "TP": int(tp), "FP": int(fp), "FN": int(fn), "TN": int(tn),
        "precision":       float(precision) if precision is not None else None,
        "recall":          float(recall) if recall is not None else None,
        "base_rate":       float(base_rate),
        "lift_vs_random":  float(lift_vs_random) if lift_vs_random is not None else None,
        "hold_ret":        float(hold_ret) if not pd.isna(hold_ret) else None,
        "strategy_ret":    float(strategy_ret) if not pd.isna(strategy_ret) else None,
        "return_lift":     float(return_lift) if return_lift is not None else None,
        "polarity":        polarity,
    }


def calibrate_stock(code: str, name: str) -> dict:
    """1종목 캘리브레이션 → playbook."""
    print(f"  [{name}] 시뮬 데이터 수집...")
    records, price = collect_records(code, name)
    if records.empty:
        return {"code": code, "name": name, "applicable": False, "reason": "데이터 부족"}

    records = add_forward_labels(records, price)

    # 그리드 평가
    grid_results = []
    for source in SIGNAL_SOURCES:
        for w in WINDOWS:
            for sigma in THRESHOLD_SIGMAS:
                r = evaluate_config(records, source, w, sigma)
                if r: grid_results.append(r)

    # 최선 config 선택 — return_lift 최대 (단, n_signals 최소 5건은 있어야)
    valid = [g for g in grid_results
             if g.get("return_lift") is not None and g.get("n_signals", 0) >= 5]

    if not valid:
        # 신호 자체가 적게 발동 → applicable=False
        best = None
        applicable = False
        reason = "신호 발동 빈도 부족"
    else:
        best = max(valid, key=lambda g: g["return_lift"])
        # 적용 가능성 — return_lift > 0 또는 precision > base_rate × 1.2
        applicable = (best["return_lift"] > 0.01) or \
                     (best.get("precision") is not None and best.get("base_rate") is not None and
                      best["precision"] > best["base_rate"] * 1.2)
        reason = (f"best lift {best['return_lift']*100:+.1f}%p, "
                  f"precision {best['precision']*100:.1f}% vs base {best['base_rate']*100:.1f}%")

    return {
        "code": code, "name": name,
        "applicable": applicable,
        "reason": reason,
        "best_config": best,
        "grid_results": grid_results,
        "as_of": str(records["date"].max().date()) if not records.empty else None,
        "n_simulations": len(records),
    }


def write_playbook(result: dict):
    fname = os.path.join(PLAYBOOK_DIR, f"{result['code']}.json")
    with open(fname, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    return fname


def render_summary(results: list) -> str:
    md = ["# 종목별 캘리브레이션 결과\n"]
    md.append(f"실행: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    md.append(f"\n## 종목별 best config\n")
    md.append("| 종목 | 적용 | source | window | threshold | precision | base_rate | return lift | polarity |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        if r.get("best_config"):
            b = r["best_config"]
            prec = f"{b['precision']*100:.1f}%" if b.get("precision") is not None else "-"
            base = f"{b['base_rate']*100:.1f}%"
            lift = f"{b['return_lift']*100:+.2f}%" if b.get("return_lift") is not None else "-"
            md.append(f"| {r['name']} | {'✅' if r['applicable'] else '❌'} | "
                      f"{b['source']} | {b['window']}일 | {b['threshold']*100:.1f}% | "
                      f"{prec} | {base} | {lift} | {b['polarity']} |")
        else:
            md.append(f"| {r['name']} | ❌ | - | - | - | - | - | - | - |")

    md.append(f"\n## 종목별 상세 — Top 3 config\n")
    for r in results:
        md.append(f"\n### {r['name']} ({r['code']}) — {'✅ 적용' if r.get('applicable') else '❌ 적용 불가'}")
        md.append(f"- 사유: {r.get('reason','')}")
        md.append(f"- 시뮬 횟수: {r.get('n_simulations',0)}\n")
        valid = [g for g in r.get("grid_results", []) if g.get("return_lift") is not None]
        if valid:
            top3 = sorted(valid, key=lambda g: g["return_lift"], reverse=True)[:3]
            md.append("| rank | source | window | sigma | threshold | precision | recall | n_signals | return_lift |")
            md.append("|---|---|---|---|---|---|---|---|---|")
            for i, g in enumerate(top3, 1):
                prec = f"{g['precision']*100:.1f}%" if g.get("precision") is not None else "-"
                rec  = f"{g['recall']*100:.1f}%"    if g.get("recall")    is not None else "-"
                lift = f"{g['return_lift']*100:+.2f}%" if g.get("return_lift") is not None else "-"
                md.append(f"| {i} | {g['source']} | {g['window']}일 | {g['sigma_mult']} | "
                          f"{g['threshold']*100:.1f}% | {prec} | {rec} | {g['n_signals']} | {lift} |")
    return "\n".join(md)


def main(targets: list):
    print(f"{'='*60}\n  종목별 캘리브레이션 — {len(targets)}종목\n{'='*60}\n")

    results = []
    for name, code in targets:
        result = calibrate_stock(code, name)
        write_playbook(result)
        results.append(result)
        print(f"    → {result['name']}: {'적용' if result['applicable'] else '미적용'} — {result['reason']}\n")

    # markdown 종합
    out = os.path.join(PLAYBOOK_DIR, f"calibration_{datetime.now().strftime('%Y%m%d_%H%M')}.md")
    with open(out, "w") as f:
        f.write(render_summary(results))
    print(f"종합 리포트: {out}")
    return results


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
        targets = [
            ("대한광통신", "010170"),
            ("삼천당제약", "000250"),
            ("제이스로보틱스", "090470"),
            ("코오롱티슈진", "950160"),
        ]
    main(targets)
