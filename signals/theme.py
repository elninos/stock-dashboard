"""산업/테마/Value Chain 분석 모듈.

기능:
  1. 종목 → 테마 매핑 (어떤 테마 그룹에 속하는지)
  2. Value Chain 단계 분류 (Tier 1 / 2 / 3)
  3. 글로벌 Peer 동조성 (correlation, beta, lag)
  4. 테마 강도 점수 (peer 평균 대비 종목 alpha)

Value Chain 정의:
  - Tier 1 (핵심 기술): 테마의 본질적 기술/원자재 보유
  - Tier 2 (모듈/장비): Tier 1을 활용한 시스템/장비
  - Tier 3 (부품/소재): Tier 2에 부품 공급

예) 광통신:
  Tier 1: Lumentum (광원 레이저), Coherent (광원)
  Tier 2: Marvell, Credo (광 ASIC/모듈), Cisco/Ciena (장비)
  Tier 3 (한국): 대한광통신/광무 (광섬유), 오이솔루션 (광트랜시버), 빛과전자 (광반도체)
"""
import warnings
warnings.filterwarnings("ignore")
import yfinance as yf
from datetime import datetime, timedelta
from pykrx import stock as krx
import pandas as pd


# === Value Chain 매핑 (확장 가능) ===
THEMES = {
    "광통신/AI데이터센터": {
        "description": "AI 데이터센터 광통신 — GPU 간 광 인터커넥트, CPO, 광트랜시버",
        "tier1_global": [  # 핵심 기술 (광원/레이저)
            ("LITE", "Lumentum"),
            ("COHR", "Coherent"),
        ],
        "tier2_global": [  # 모듈/ASIC
            ("MRVL", "Marvell"),
            ("CRDO", "Credo"),
            ("CIEN", "Ciena"),
            ("CSCO", "Cisco"),
            ("AAOI", "Applied Optoelectronics"),
        ],
        "tier3_korea": [  # 한국 — 부품/광케이블
            ("010170", "대한광통신",   "광섬유/광케이블"),
            ("069540", "빛과전자",     "광반도체/레이저"),
            ("138080", "오이솔루션",   "광트랜시버"),
            ("046970", "우리로",       "광부품"),
            ("056360", "코위버",       "광전송장비"),
            ("230240", "에치에프알",   "광액세스 장비"),
            ("100590", "머큐리",       "광통신 시스템"),
            ("029480", "광무",         "광부품/광커넥터"),
            ("007660", "이수페타시스", "AI PCB (인접)"),
        ],
        "etfs_global": ["XSD", "SOXX", "SMH", "IGN"],
    },
    "AI반도체/GPU": {
        "description": "AI 가속기, HBM, GPU",
        "tier1_global": [("NVDA", "NVIDIA"), ("AMD", "AMD")],
        "tier2_global": [("AVGO", "Broadcom"), ("ASML", "ASML")],
        "tier3_korea": [
            ("000660", "SK하이닉스",   "HBM"),
            ("005930", "삼성전자",     "메모리"),
            ("058470", "리노공업",     "테스트 소켓"),
            ("039030", "이오테크닉스", "반도체 장비"),
            ("140860", "파크시스템스", "원자현미경"),
        ],
        "etfs_global": ["SOXX", "SMH", "XSD"],
    },
    "바이오/제약": {
        "description": "바이오 의약품, CDMO, 신약개발",
        "tier1_global": [("LLY", "Eli Lilly"), ("NVO", "Novo Nordisk")],
        "tier2_global": [],
        "tier3_korea": [
            ("214450", "파마리서치",   "리쥬란/필러"),
            ("068270", "셀트리온",     "바이오시밀러"),
            ("310210", "보로노이",     "신약개발"),
            ("950170", "코오롱티슈진", "세포치료"),
            ("039200", "오스코텍",     "신약"),
        ],
        "etfs_global": ["IBB", "XBI"],
    },
    "콘텐츠/소비": {
        "description": "포털, 콘텐츠, 화장품",
        "tier1_global": [("META", "Meta"), ("GOOGL", "Google")],
        "tier2_global": [],
        "tier3_korea": [
            ("035420", "NAVER",        "포털/AI"),
            ("278470", "에이피알",     "화장품"),
            ("444180", "콜마비앤에이치", "건강기능식품"),
        ],
        "etfs_global": ["XLY", "XLC"],
    },
    "제조/산업재": {
        "description": "방산, 발전, 산업기계",
        "tier1_global": [("GE", "GE Aerospace"), ("HON", "Honeywell")],
        "tier2_global": [],
        "tier3_korea": [
            ("000150", "두산",         "지주/방산/원전"),
            ("058470", "리노공업",     "정밀부품"),
        ],
        "etfs_global": ["XLI"],
    },
}


def find_theme(stock_code: str):
    """종목코드로 어느 테마/Tier에 속하는지 찾기."""
    for theme_name, theme in THEMES.items():
        for code, name, *_ in theme.get("tier3_korea", []):
            if code == stock_code:
                return {
                    "theme": theme_name,
                    "tier": 3,
                    "description": theme["description"],
                    "stock_name": name,
                    "biz": _[0] if _ else "",
                    "theme_data": theme,
                }
    return None


def fetch_yf_close(symbol: str, days: int = 100) -> pd.Series:
    """yfinance close 가격."""
    try:
        df = yf.download(symbol, period=f"{days}d", progress=False, auto_adjust=True)
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        return df["Close"]
    except Exception:
        return None


def fetch_krx_close(code: str, days: int = 100) -> pd.Series:
    """KRX close 가격 (pykrx)."""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days+30)).strftime("%Y%m%d")
    try:
        df = krx.get_market_ohlcv_by_date(start, end, code)
        if df is None or len(df) == 0: return None
        return df["종가"].rename(code)
    except Exception:
        return None


def returns_summary(close: pd.Series) -> dict:
    """가격 시계열로부터 수익률 요약."""
    if close is None or len(close) < 30: return None
    cur = float(close.iloc[-1])
    def chg(n):
        if len(close) < n+1: return None
        return (cur / float(close.iloc[-n-1]) - 1) * 100
    return {
        "current": cur,
        "d1": chg(1), "d5": chg(5), "d20": chg(20), "d60": chg(60),
        "peak": float(close.max()),
        "from_peak": (cur/float(close.max())-1)*100,
    }


def correlation(s1: pd.Series, s2: pd.Series, n: int = 60) -> dict:
    """두 시계열의 상관계수/베타/lag.
    s1 = 우리 종목, s2 = peer
    """
    if s1 is None or s2 is None: return None
    # 두 시리즈를 일자 정렬
    df = pd.concat([s1.rename("a"), s2.rename("b")], axis=1).dropna()
    if len(df) < 20: return None
    df = df.tail(n)
    a_ret = df["a"].pct_change().dropna()
    b_ret = df["b"].pct_change().dropna()
    common = a_ret.index.intersection(b_ret.index)
    a_ret = a_ret.loc[common]
    b_ret = b_ret.loc[common]
    if len(a_ret) < 10: return None

    corr = a_ret.corr(b_ret)
    # 베타: cov(a,b) / var(b)
    var_b = b_ret.var()
    beta = a_ret.cov(b_ret) / var_b if var_b > 0 else None

    # Lag 분석: peer 가격 변동이 며칠 후 우리 종목에 반영되는가
    # 1~5일 lag 시도, 가장 높은 corr 찾기
    best_lag = 0
    best_lag_corr = corr
    for lag in range(-3, 4):  # peer가 -3 ~ +3일 시프트
        if lag == 0: continue
        try:
            shifted = b_ret.shift(lag).dropna()
            common2 = a_ret.index.intersection(shifted.index)
            if len(common2) < 10: continue
            c = a_ret.loc[common2].corr(shifted.loc[common2])
            if abs(c) > abs(best_lag_corr):
                best_lag_corr = c
                best_lag = lag
        except Exception:
            pass

    return {
        "corr": corr,
        "beta": beta,
        "best_lag": best_lag,
        "best_lag_corr": best_lag_corr,
        "samples": len(a_ret),
    }


def analyze_theme(stock_code: str) -> dict:
    """종목의 테마 / Value Chain / 글로벌 Peer 동조성 종합."""
    info = find_theme(stock_code)
    if not info:
        return {"available": False, "error": "테마 매핑 없음"}

    theme_data = info["theme_data"]

    # 우리 종목 가격
    target_close = fetch_krx_close(stock_code, 120)
    target_ret = returns_summary(target_close)

    # Tier 1, 2 글로벌 peers
    peers_data = []
    for tier_key, label in [("tier1_global", "Tier1"), ("tier2_global", "Tier2")]:
        for sym, name in theme_data.get(tier_key, []):
            close = fetch_yf_close(sym, 120)
            ret = returns_summary(close)
            corr = correlation(target_close, close, 60)
            peers_data.append({
                "tier": label, "symbol": sym, "name": name,
                "ret": ret, "corr": corr,
            })

    # Tier 3 한국 동종 종목
    korea_peers = []
    for entry in theme_data.get("tier3_korea", []):
        code = entry[0]; name = entry[1]; biz = entry[2] if len(entry) > 2 else ""
        if code == stock_code: continue
        close = fetch_krx_close(code, 120)
        ret = returns_summary(close)
        corr = correlation(target_close, close, 60)
        korea_peers.append({
            "code": code, "name": name, "biz": biz,
            "ret": ret, "corr": corr,
        })

    # 테마 ETF
    etfs_data = []
    for sym in theme_data.get("etfs_global", []):
        close = fetch_yf_close(sym, 120)
        ret = returns_summary(close)
        corr = correlation(target_close, close, 60)
        etfs_data.append({"symbol": sym, "ret": ret, "corr": corr})

    # 테마 강도 점수
    # 1) 글로벌 peers 평균 60일 수익률
    glob_60d = [p["ret"]["d60"] for p in peers_data if p["ret"] and p["ret"]["d60"] is not None]
    kor_60d  = [p["ret"]["d60"] for p in korea_peers if p["ret"] and p["ret"]["d60"] is not None]
    target_60d = target_ret["d60"] if target_ret else None

    glob_avg_60d = sum(glob_60d)/len(glob_60d) if glob_60d else None
    kor_avg_60d  = sum(kor_60d)/len(kor_60d) if kor_60d else None

    # 2) 알파 vs peer 평균
    alpha_vs_kor = (target_60d - kor_avg_60d) if (target_60d is not None and kor_avg_60d is not None) else None
    alpha_vs_global = (target_60d - glob_avg_60d) if (target_60d is not None and glob_avg_60d is not None) else None

    # 3) 글로벌 peers 5일 평균 (단기 모멘텀)
    glob_5d = [p["ret"]["d5"] for p in peers_data if p["ret"] and p["ret"]["d5"] is not None]
    kor_5d  = [p["ret"]["d5"] for p in korea_peers if p["ret"] and p["ret"]["d5"] is not None]
    glob_avg_5d = sum(glob_5d)/len(glob_5d) if glob_5d else None
    kor_avg_5d = sum(kor_5d)/len(kor_5d) if kor_5d else None

    # 테마 강도 라벨
    if glob_avg_5d is not None and glob_avg_5d > 5:
        theme_momentum = "🔥 글로벌 Peer 강한 단기 상승"
    elif glob_avg_5d is not None and glob_avg_5d < -5:
        theme_momentum = "🧊 글로벌 Peer 단기 하락"
    elif glob_avg_60d is not None and glob_avg_60d > 30:
        theme_momentum = "🟢 글로벌 Peer 60일 강한 상승"
    elif glob_avg_60d is not None and glob_avg_60d < 0:
        theme_momentum = "🔴 글로벌 Peer 60일 하락"
    else:
        theme_momentum = "⚪ 글로벌 Peer 정상"

    # 한국/글로벌 괴리
    decoupling = None
    if kor_avg_5d is not None and glob_avg_5d is not None:
        gap = kor_avg_5d - glob_avg_5d
        if gap < -8:
            decoupling = f"⚠️ 한국 5일 {kor_avg_5d:+.1f}% vs 글로벌 {glob_avg_5d:+.1f}% — 한국만 약세 ({gap:+.1f}%p 괴리)"
        elif gap > 8:
            decoupling = f"🟢 한국 5일 {kor_avg_5d:+.1f}% vs 글로벌 {glob_avg_5d:+.1f}% — 한국 상회"

    return {
        "available": True,
        "theme": info["theme"],
        "tier": info["tier"],
        "description": info["description"],
        "stock_name": info["stock_name"],
        "biz": info["biz"],
        "target_ret": target_ret,
        "global_peers": peers_data,
        "korea_peers": korea_peers,
        "etfs": etfs_data,
        "glob_avg_60d": glob_avg_60d,
        "kor_avg_60d": kor_avg_60d,
        "glob_avg_5d": glob_avg_5d,
        "kor_avg_5d": kor_avg_5d,
        "alpha_vs_kor": alpha_vs_kor,
        "alpha_vs_global": alpha_vs_global,
        "theme_momentum": theme_momentum,
        "decoupling": decoupling,
    }


def print_report(stock_code: str):
    """테마 분석 리포트."""
    r = analyze_theme(stock_code)
    if not r["available"]:
        print(f"  {stock_code}: {r.get('error')}")
        return r

    print("="*120)
    print(f"  테마/Value Chain 분석 — {r['stock_name']} ({stock_code}) | {r['biz']}")
    print(f"  → 테마: {r['theme']} | Tier {r['tier']}")
    print(f"  → {r['description']}")
    print("="*120)

    def fmt(v):
        return "─" if v is None else f"{v:+.1f}%"

    # 우리 종목
    t = r["target_ret"]
    if t:
        print(f"\n[우리 종목]")
        print(f"  {r['stock_name']:<14} 현재 {t['current']:>10,.0f} | D-1 {fmt(t['d1'])} | D-5 {fmt(t['d5'])} | D-20 {fmt(t['d20'])} | D-60 {fmt(t['d60'])} | 고점 {fmt(t['from_peak'])}")

    # 글로벌 Tier 1, 2
    print(f"\n[글로벌 Peer (Tier 1/2)]")
    print(f"  {'Tier':<5} {'심볼':<6} {'이름':<24} {'D-5':>7} {'D-20':>7} {'D-60':>7} {'corr':>6} {'beta':>6} {'lag':>4}")
    print(f"  {'-'*80}")
    for p in r["global_peers"]:
        ret = p["ret"]; cr = p["corr"]
        d5  = fmt(ret["d5"])  if ret else "─"
        d20 = fmt(ret["d20"]) if ret else "─"
        d60 = fmt(ret["d60"]) if ret else "─"
        c   = f"{cr['corr']:.2f}" if cr and cr.get("corr") is not None else "─"
        b   = f"{cr['beta']:.2f}" if cr and cr.get("beta") is not None else "─"
        lag = f"{cr['best_lag']:+d}" if cr else "─"
        print(f"  {p['tier']:<5} {p['symbol']:<6} {p['name']:<24} {d5:>7} {d20:>7} {d60:>7} {c:>6} {b:>6} {lag:>4}")

    # 한국 Tier 3
    print(f"\n[한국 동종 종목 (Tier 3)]")
    print(f"  {'코드':<7} {'이름':<14} {'사업':<22} {'D-5':>7} {'D-20':>7} {'D-60':>7} {'corr':>6} {'beta':>6}")
    print(f"  {'-'*90}")
    for p in r["korea_peers"]:
        ret = p["ret"]; cr = p["corr"]
        d5  = fmt(ret["d5"])  if ret else "─"
        d20 = fmt(ret["d20"]) if ret else "─"
        d60 = fmt(ret["d60"]) if ret else "─"
        c   = f"{cr['corr']:.2f}" if cr and cr.get("corr") is not None else "─"
        b   = f"{cr['beta']:.2f}" if cr and cr.get("beta") is not None else "─"
        print(f"  {p['code']:<7} {p['name']:<14} {p['biz']:<22} {d5:>7} {d20:>7} {d60:>7} {c:>6} {b:>6}")

    # ETF
    print(f"\n[관련 ETF]")
    print(f"  {'심볼':<8} {'D-5':>7} {'D-20':>7} {'D-60':>7} {'corr':>6}")
    print(f"  {'-'*45}")
    for e in r["etfs"]:
        ret = e["ret"]; cr = e["corr"]
        d5  = fmt(ret["d5"])  if ret else "─"
        d20 = fmt(ret["d20"]) if ret else "─"
        d60 = fmt(ret["d60"]) if ret else "─"
        c   = f"{cr['corr']:.2f}" if cr and cr.get("corr") is not None else "─"
        print(f"  {e['symbol']:<8} {d5:>7} {d20:>7} {d60:>7} {c:>6}")

    # 종합
    print(f"\n[테마 강도 진단]")
    print(f"  글로벌 Peer 평균: 60일 {fmt(r['glob_avg_60d'])} / 5일 {fmt(r['glob_avg_5d'])}")
    print(f"  한국 Peer 평균:   60일 {fmt(r['kor_avg_60d'])} / 5일 {fmt(r['kor_avg_5d'])}")
    print(f"  알파 vs 글로벌:   {fmt(r['alpha_vs_global'])}")
    print(f"  알파 vs 한국:     {fmt(r['alpha_vs_kor'])}")
    print(f"  → {r['theme_momentum']}")
    if r["decoupling"]:
        print(f"  → {r['decoupling']}")

    return r


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "010170"
    print_report(code)
