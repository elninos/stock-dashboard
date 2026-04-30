"""매크로 시장 분석 — 한국 + 미국.

핵심 지표:
  - 한국: KOSPI, KOSDAQ, USD/KRW
  - 미국: S&P500, NASDAQ, DXY, VIX, US10Y, WTI, Gold

Regime 분류 (각 지수별):
  - 강세장 (Bull):   60일 ↑ + 200일선 위 + 고점 -10% 이내
  - 상승장 (Up):     60일 ↑ + 200일선 근처
  - 횡보장 (Range):  60일 ±5% 이내
  - 하락장 (Down):   60일 ↓ + 200일선 아래
  - 약세장 (Bear):   고점 -20% 이상 + 200일선 아래

위험선호도:
  - VIX < 16: 저변동 (risk-on)
  - VIX 16~25: 정상
  - VIX > 25: 고변동 (risk-off)
"""
import warnings
warnings.filterwarnings("ignore")
from datetime import datetime, timedelta
import os, json
import FinanceDataReader as fdr

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "macro_cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def _cached_fetch(symbol: str, days: int = 400, ttl_min: int = 60):
    """FDR 데이터 캐싱."""
    cache_file = os.path.join(CACHE_DIR, f"{symbol.replace('/','_').replace('=','_')}.json")
    if os.path.exists(cache_file):
        age_min = (datetime.now().timestamp() - os.path.getmtime(cache_file)) / 60
        if age_min < ttl_min:
            try:
                with open(cache_file, "r") as f:
                    cached = json.load(f)
                import pandas as pd
                df = pd.DataFrame(cached["data"])
                df.index = pd.to_datetime(df.index if "index" not in df.columns else df.pop("index"))
                return df
            except Exception:
                pass

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    df = fdr.DataReader(symbol, start, end)
    if df is None or len(df) == 0:
        return None
    # save cache
    try:
        df_dict = df.reset_index().to_dict(orient="records")
        for r in df_dict:
            for k, v in r.items():
                if hasattr(v, "isoformat"):
                    r[k] = v.isoformat()
        with open(cache_file, "w") as f:
            json.dump({"data": df_dict, "saved_at": datetime.now().isoformat()}, f)
    except Exception:
        pass
    df.index = df.index if not hasattr(df.index[0], "tz_localize") else df.index
    return df


def fetch_yfinance(symbol: str, days: int = 400):
    """yfinance fallback."""
    try:
        import yfinance as yf
        df = yf.download(symbol, period=f"{days}d", progress=False)
        if df is None or len(df) == 0: return None
        # 멀티인덱스 컬럼 제거
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception:
        return None


def classify_regime(df, name=""):
    """단일 지수의 regime 분류."""
    if df is None or len(df) < 60:
        return {"name": name, "available": False}
    closes = df["Close"]
    cur = float(closes.iloc[-1])

    def chg(n):
        if len(closes) < n+1: return None
        return (cur / float(closes.iloc[-n-1]) - 1) * 100

    d1, d5, d20, d60, d120 = chg(1), chg(5), chg(20), chg(60), chg(120)
    d240 = chg(240) if len(closes) >= 241 else None

    # 200일 이동평균선
    ma200 = float(closes.tail(200).mean()) if len(closes) >= 200 else float(closes.mean())
    ma60 = float(closes.tail(60).mean())
    ma20 = float(closes.tail(20).mean())

    above_ma200 = cur > ma200
    above_ma60 = cur > ma60
    above_ma20 = cur > ma20

    # 52주 고점/저점
    peak = float(closes.tail(252).max())
    trough = float(closes.tail(252).min())
    from_peak = (cur / peak - 1) * 100
    from_trough = (cur / trough - 1) * 100

    # 변동성 (20일 표준편차)
    rets = closes.pct_change().tail(20)
    vol_20d = float(rets.std() * (252 ** 0.5)) * 100  # 연환산

    # Regime 판정
    if from_peak <= -20:
        regime = "약세장"
    elif d60 is not None and d60 >= 10 and above_ma200 and from_peak >= -10:
        regime = "강세장"
    elif d60 is not None and d60 >= 3 and above_ma200:
        regime = "상승장"
    elif d60 is not None and d60 <= -10 and not above_ma200:
        regime = "하락장"
    elif d60 is not None and abs(d60) <= 5:
        regime = "횡보장"
    else:
        regime = "혼조"

    return {
        "name": name,
        "available": True,
        "current": cur,
        "d1": d1, "d5": d5, "d20": d20, "d60": d60, "d120": d120, "d240": d240,
        "ma200": ma200, "ma60": ma60, "ma20": ma20,
        "above_ma200": above_ma200, "above_ma60": above_ma60, "above_ma20": above_ma20,
        "peak": peak, "trough": trough,
        "from_peak": from_peak, "from_trough": from_trough,
        "vol_20d": vol_20d,
        "regime": regime,
    }


def analyze_macro():
    """전체 매크로 분석."""
    results = {}

    # FDR 데이터
    fdr_sources = [
        ("KS11",     "코스피",     "korea"),
        ("KQ11",     "코스닥",     "korea"),
        ("US500",    "S&P500",   "us"),
        ("IXIC",     "나스닥",     "us"),
        ("DJI",      "다우",      "us"),
        ("VIX",      "VIX",      "risk"),
        ("USD/KRW",  "원달러",     "fx"),
        ("CL=F",     "WTI유가",    "commodity"),
        ("GC=F",     "금",        "commodity"),
    ]
    for sym, name, cat in fdr_sources:
        try:
            df = _cached_fetch(sym, days=400)
            r = classify_regime(df, name)
            r["category"] = cat
            r["symbol"] = sym
            results[name] = r
        except Exception as e:
            results[name] = {"name": name, "available": False, "error": str(e)[:60]}

    # yfinance: 미국 국채 + DXY
    yf_sources = [
        ("^TNX",     "미국10Y",     "rate"),
        ("^IRX",     "미국13W",     "rate"),
        ("DX-Y.NYB", "달러인덱스",   "fx"),
    ]
    for sym, name, cat in yf_sources:
        try:
            df = fetch_yfinance(sym, days=400)
            r = classify_regime(df, name)
            r["category"] = cat
            r["symbol"] = sym
            results[name] = r
        except Exception as e:
            results[name] = {"name": name, "available": False, "error": str(e)[:60]}

    # 종합 점수
    score = compute_overall_score(results)
    results["_overall"] = score

    return results


def compute_overall_score(macro: dict) -> dict:
    """매크로 종합 점수.
    risk_on 점수 양수=위험선호, 음수=위험회피
    """
    score = 0
    reasons = []

    # 한국
    for k in ["코스피", "코스닥"]:
        m = macro.get(k, {})
        if not m.get("available"): continue
        if m["regime"] in ("강세장", "상승장"):
            score += 2; reasons.append(f"{k} {m['regime']} (60일 {m['d60']:+.1f}%)")
        elif m["regime"] in ("하락장", "약세장"):
            score -= 2; reasons.append(f"{k} {m['regime']} (60일 {m['d60']:+.1f}%)")

    # 미국
    for k in ["S&P500", "나스닥"]:
        m = macro.get(k, {})
        if not m.get("available"): continue
        if m["regime"] in ("강세장", "상승장"):
            score += 2; reasons.append(f"{k} {m['regime']} (60일 {m['d60']:+.1f}%)")
        elif m["regime"] in ("하락장", "약세장"):
            score -= 2; reasons.append(f"{k} {m['regime']} (60일 {m['d60']:+.1f}%)")

    # VIX
    vix = macro.get("VIX", {})
    if vix.get("available"):
        v = vix["current"]
        if v < 16:
            score += 1; reasons.append(f"VIX {v:.1f} (저변동, risk-on)")
        elif v > 25:
            score -= 2; reasons.append(f"VIX {v:.1f} (고변동, risk-off)")
        elif v > 20:
            score -= 1; reasons.append(f"VIX {v:.1f} (변동성 높음)")

    # 원달러
    krw = macro.get("원달러", {})
    if krw.get("available"):
        # 원화 약세 (USD/KRW 상승) = 외국인에게 매도 압력
        if krw["d20"] is not None and krw["d20"] > 2:
            score -= 1
            reasons.append(f"원달러 20일 {krw['d20']:+.1f}% (원화 약세 = 외국인 매도 압력)")
        elif krw["d20"] is not None and krw["d20"] < -2:
            score += 1
            reasons.append(f"원달러 20일 {krw['d20']:+.1f}% (원화 강세 = 외국인 매수 우호)")

    # 미국 10Y 금리
    us10 = macro.get("미국10Y", {})
    if us10.get("available"):
        # 금리 급등은 주식에 악재 (특히 성장주)
        if us10["d20"] is not None and us10["d20"] > 5:
            score -= 1
            reasons.append(f"미국10Y 20일 {us10['d20']:+.1f}% 급등 (성장주 악재)")
        elif us10["d20"] is not None and us10["d20"] < -5:
            score += 1
            reasons.append(f"미국10Y 20일 {us10['d20']:+.1f}% 급락 (성장주 우호)")

    # 위험선호도 라벨
    if score >= 5:
        label = "🟢 강한 risk-on (적극 보유/매수)"
    elif score >= 2:
        label = "🟢 risk-on"
    elif score <= -5:
        label = "🔴 강한 risk-off (방어/현금)"
    elif score <= -2:
        label = "🔴 risk-off"
    else:
        label = "⚪ 중립"

    return {"score": score, "label": label, "reasons": reasons}


def print_report():
    """매크로 리포트 출력."""
    m = analyze_macro()

    def fmt_pct(v):
        if v is None: return "─"
        return f"{v:+.1f}%"

    print("="*120)
    print(f"  매크로 시장 진단 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("="*120)

    # 카테고리별 출력
    cats = [
        ("KOREA", ["코스피", "코스닥"]),
        ("US", ["S&P500", "나스닥", "다우"]),
        ("RISK", ["VIX"]),
        ("FX", ["원달러", "달러인덱스"]),
        ("RATE", ["미국10Y", "미국13W"]),
        ("COMMODITY", ["WTI유가", "금"]),
    ]

    for cat_name, names in cats:
        print(f"\n[{cat_name}]")
        print(f"  {'지수':<12} {'현재':>10} {'D-1':>7} {'D-5':>7} {'D-20':>8} {'D-60':>8} {'D-120':>8} {'고점대비':>9} {'200일선':>7} {'변동성':>7} {'Regime':>8}")
        print(f"  {'-'*120}")
        for name in names:
            r = m.get(name, {})
            if not r.get("available"):
                print(f"  {name:<12} ✗")
                continue
            ma_pos = "위" if r["above_ma200"] else "아래"
            print(f"  {name:<12} {r['current']:>10,.2f} {fmt_pct(r['d1']):>7} {fmt_pct(r['d5']):>7} {fmt_pct(r['d20']):>8} {fmt_pct(r['d60']):>8} {fmt_pct(r['d120']):>8} {fmt_pct(r['from_peak']):>9} {ma_pos:>7} {r['vol_20d']:>6.1f}% {r['regime']:>8}")

    # 종합
    o = m["_overall"]
    print("\n" + "="*120)
    print(f"  종합 매크로 평가: {o['label']} (점수 {o['score']:+d})")
    print("="*120)
    for r in o["reasons"]:
        print(f"  • {r}")

    return m


if __name__ == "__main__":
    print_report()
