"""KRX 추가 데이터: 공매도, 외국인 보유율, 기관 세부 분류.

pykrx로 가져옴. KRX API가 일시적으로 응답하지 않을 수 있으므로
retry + local cache로 안정화.

캐시 위치: data/krx_cache/{stock_code}_{type}.json
"""
import os, json, time, warnings
from datetime import datetime
warnings.filterwarnings("ignore")

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "krx_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

CACHE_TTL_DAYS = 1   # 1일 이상 지난 캐시는 재요청


def _cache_path(code: str, kind: str) -> str:
    return os.path.join(CACHE_DIR, f"{code}_{kind}.json")


def _is_cache_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    age = (time.time() - os.path.getmtime(path)) / 86400
    return age < CACHE_TTL_DAYS


def _save_cache(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _load_cache(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _retry_fetch(fn, retries: int = 3, delay: float = 1.5):
    """KRX API retry. 실패 시 None 반환."""
    last_err = None
    for i in range(retries):
        try:
            return fn()
        except Exception as e:
            last_err = e
            time.sleep(delay * (i + 1))
    return None  # type: ignore


def fetch_shorting_balance(code: str, start: str, end: str) -> dict:
    """공매도 잔고 시계열. {date: {balance, ratio}} 형태."""
    cache = _cache_path(code, f"short_{start}_{end}")
    if _is_cache_fresh(cache):
        return _load_cache(cache)

    from pykrx import stock as krx
    df = _retry_fetch(lambda: krx.get_shorting_balance_by_date(start, end, code))
    if df is None or len(df) == 0:
        # 빈 결과는 캐시하지 않음 — 다음 실행 시 재시도
        return {}

    out = {}
    for d, row in df.iterrows():
        date_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        out[date_str] = {col: int(v) if hasattr(v, "item") else v for col, v in row.items()}
    _save_cache(cache, out)
    return out


def fetch_foreign_ownership(code: str, start: str, end: str) -> dict:
    """외국인 보유율 시계열. {date: {보유수량, 보유비중, ...}}."""
    cache = _cache_path(code, f"foreign_{start}_{end}")
    if _is_cache_fresh(cache):
        return _load_cache(cache)

    from pykrx import stock as krx
    df = _retry_fetch(lambda: krx.get_exhaustion_rates_of_foreign_investment_by_date(start, end, code))
    if df is None or len(df) == 0:
        # 빈 결과는 캐시하지 않음 — 다음 실행 시 재시도
        return {}

    out = {}
    for d, row in df.iterrows():
        date_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        out[date_str] = {col: float(v) if hasattr(v, "item") else v for col, v in row.items()}
    _save_cache(cache, out)
    return out


def fetch_inst_detail_flow(code: str, start: str, end: str) -> dict:
    """기관 세부 분류별 거래대금 시계열.

    detail=True 시 컬럼:
      금융투자 / 보험 / 투신 / 사모 / 은행 / 기타금융 / 연기금등 / 기타법인 /
      개인 / 외국인 / 기타외국인 / 전체
    """
    cache = _cache_path(code, f"instdtl_{start}_{end}")
    if _is_cache_fresh(cache):
        return _load_cache(cache)

    from pykrx import stock as krx
    df = _retry_fetch(lambda: krx.get_market_trading_value_by_date(start, end, code, detail=True))
    if df is None or len(df) == 0:
        # 빈 결과는 캐시하지 않음 — 다음 실행 시 재시도
        return {}

    out = {}
    for d, row in df.iterrows():
        date_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        out[date_str] = {col: int(v) if hasattr(v, "item") else v for col, v in row.items()}
    _save_cache(cache, out)
    return out


# ────────────────────────────────────────────────
# 분석 함수
# ────────────────────────────────────────────────

def analyze_short_pressure(short_data: dict, lookback: int = 5) -> dict:
    """공매도 잔고 추이 분석.

    기준:
      - 잔고비율 ≥ 5% → 공매도 압력 높음
      - 5일 잔고 증가율 ≥ 30% → 급증 (분배 의심)
    """
    if not short_data:
        return {"available": False}

    dates = sorted(short_data.keys())
    if len(dates) < lookback:
        return {"available": False}

    # 잔고 컬럼 추정
    sample = short_data[dates[-1]]
    bal_col = next((c for c in sample if "잔고" in c and "수량" in c), None)
    rate_col = next((c for c in sample if "비율" in c or "비중" in c), None)

    if not bal_col:
        return {"available": False, "error": f"컬럼 미확인: {list(sample.keys())}"}

    last = short_data[dates[-1]]
    prev = short_data[dates[-min(lookback, len(dates))]]

    last_bal = last.get(bal_col, 0)
    prev_bal = prev.get(bal_col, 0)
    chg_pct = ((last_bal / prev_bal - 1) * 100) if prev_bal > 0 else 0

    return {
        "available": True,
        "last_balance": last_bal,
        "last_ratio":   last.get(rate_col, 0) if rate_col else None,
        "change_5d_pct": round(chg_pct, 1),
        "alert": chg_pct >= 30,  # 5일간 30% 이상 급증
    }


def analyze_foreign_ownership(foreign_data: dict, lookback: int = 20) -> dict:
    """외국인 보유율 변화 분석.

    기준:
      - 보유율 20일 변화 +0.5%p 이상 → 매집 중
      - 보유율 20일 변화 -0.5%p 이하 → 이탈 중
    """
    if not foreign_data:
        return {"available": False}

    dates = sorted(foreign_data.keys())
    if len(dates) < lookback:
        return {"available": False}

    sample = foreign_data[dates[-1]]
    rate_col = next((c for c in sample if "비중" in c or "비율" in c), None)
    qty_col = next((c for c in sample if "보유수량" in c), None)

    if not rate_col:
        return {"available": False, "error": f"컬럼 미확인: {list(sample.keys())}"}

    last_rate = foreign_data[dates[-1]].get(rate_col, 0)
    prev_rate = foreign_data[dates[-min(lookback, len(dates))]].get(rate_col, 0)
    delta = last_rate - prev_rate

    return {
        "available": True,
        "last_rate":  round(last_rate, 2),
        "delta_20d":  round(delta, 2),
        "trend":     "매집" if delta >= 0.5 else "이탈" if delta <= -0.5 else "중립",
    }


def analyze_inst_detail(inst_data: dict, lookback: int = 5) -> dict:
    """기관 세부 분류 매수 패턴.

    각 그룹별 5일 누적 순매수금액(억 원) 반환.
      - 연기금등: 장기 매집
      - 사모: 중기 매집
      - 투신: 단기 매매
    """
    if not inst_data:
        return {"available": False}

    dates = sorted(inst_data.keys())
    if len(dates) < lookback:
        return {"available": False}

    target_groups = ["금융투자", "보험", "투신", "사모", "은행", "기타금융",
                      "연기금등", "기타법인", "개인", "외국인"]

    result = {}
    for g in target_groups:
        # 5일 누적 (단위는 detail=True 결과에 따라 다름 - 보통 매수-매도)
        total = 0
        for d in dates[-lookback:]:
            v = inst_data[d].get(g, 0)
            if isinstance(v, (int, float)):
                total += v
        result[g] = round(total / 1e8, 1) if total != 0 else 0.0  # 억 원

    return {
        "available": True,
        "flows_5d_amt": result,  # 억 원
        # 핵심 그룹 강조
        "smart_money_5d": round(
            (result.get("연기금등", 0) + result.get("사모", 0) + result.get("외국인", 0)), 1
        ),
    }
