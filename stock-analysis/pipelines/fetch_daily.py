"""
fetch_daily.py — KIS API 데이터 fetch 및 캐시 저장 스크립트

프로젝트 루트: stock-analysis/  (이 파일의 parent.parent)

사용법:
    python3 pipelines/fetch_daily.py --ticker 327260
    python3 pipelines/fetch_daily.py --all
    python3 pipelines/fetch_daily.py --ticker 327260 --force
    python3 pipelines/fetch_daily.py --ticker 327260 --type ohlcv
    python3 pipelines/fetch_daily.py --ticker 327260 --type supply
    python3 pipelines/fetch_daily.py --ticker 327260 --type short
    python3 pipelines/fetch_daily.py --ticker 327260 --type broker
    python3 pipelines/fetch_daily.py --ticker 327260 --type technical
    python3 pipelines/fetch_daily.py --ticker 327260 --type financial
    python3 pipelines/fetch_daily.py --ticker 327260 --type market
    python3 pipelines/fetch_daily.py --ticker 327260 --type info
    python3 pipelines/fetch_daily.py --ticker 327260 --type news
"""

import argparse
import json
import os
import sys
import csv
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ── 경로 설정 ──────────────────────────────────────────────────────────────
ROOT         = Path(__file__).resolve().parent.parent   # stock-analysis/
CACHE_DIR    = ROOT / "data" / "kis_cache"
PROFILES_DIR = ROOT / "profiles"
LOG_DIR      = ROOT / "pipelines" / "logs"
TOKEN_CACHE  = ROOT / "pipelines" / "token_cache.json"

CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── 로깅 설정 ──────────────────────────────────────────────────────────────
log_file = LOG_DIR / f"{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── 설정 로드 ──────────────────────────────────────────────────────────────
sys.path.insert(0, str(ROOT / "pipelines"))
try:
    import config as _cfg
    APP_KEY    = os.environ.get("KIS_APP_KEY",    getattr(_cfg, "KIS_APP_KEY",    ""))
    APP_SECRET = os.environ.get("KIS_APP_SECRET", getattr(_cfg, "KIS_APP_SECRET", ""))
    ACCOUNT_NO = os.environ.get("KIS_ACCOUNT_NO", getattr(_cfg, "KIS_ACCOUNT_NO", ""))
    IS_MOCK    = getattr(_cfg, "IS_MOCK", False)
except ImportError:
    APP_KEY    = os.environ.get("KIS_APP_KEY",    "")
    APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
    ACCOUNT_NO = os.environ.get("KIS_ACCOUNT_NO", "")
    IS_MOCK    = False

BASE_URL = (
    "https://openapivts.koreainvestment.com:29443"
    if IS_MOCK else
    "https://openapi.koreainvestment.com:9443"
)

# ── 지원 타입 ──────────────────────────────────────────────────────────────
ALL_TYPES = ["ohlcv", "technical", "supply", "short", "broker", "financial", "market", "info", "news"]

CACHE_TTL = {
    "ohlcv":      timedelta(days=1),
    "technical":  timedelta(days=1),
    "supply":     timedelta(days=1),
    "short":      timedelta(days=1),
    "broker":     timedelta(days=1),
    "financial":  timedelta(days=7),
    "market":     timedelta(days=1),
    "info":       timedelta(days=7),
    "news":       timedelta(days=1),
}

CACHE_FILE = {
    "ohlcv":     lambda t: CACHE_DIR / f"{t}_ohlcv.csv",
    "technical": lambda t: CACHE_DIR / f"{t}_technical.json",
    "supply":    lambda t: CACHE_DIR / f"{t}_supply.json",
    "short":     lambda t: CACHE_DIR / f"{t}_short.json",
    "broker":    lambda t: CACHE_DIR / f"{t}_broker.json",
    "financial": lambda t: CACHE_DIR / f"{t}_financial.json",
    "market":    lambda t: CACHE_DIR / f"{t}_market.json",
    "info":      lambda t: CACHE_DIR / f"{t}_info.json",
    "news":      lambda t: CACHE_DIR / f"{t}_news.json",
}


# ══════════════════════════════════════════════════════════════════════════════
# 캐시 유틸
# ══════════════════════════════════════════════════════════════════════════════
def is_cache_valid(path: Path, kind: str) -> bool:
    if not path.exists():
        return False
    if kind == "ohlcv":
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        return datetime.now() - mtime < CACHE_TTL[kind]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("stale"):
            return False
        updated = datetime.fromisoformat(data.get("last_updated", "2000-01-01"))
        return datetime.now() - updated < CACHE_TTL[kind]
    except Exception:
        return False


def save_json(path: Path, data: dict):
    data["last_updated"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_stale(path: Path, kind: str):
    """API 실패 시 기존 캐시에 stale 표기"""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["stale"] = True
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# KIS API 인증
# ══════════════════════════════════════════════════════════════════════════════
def get_access_token() -> str:
    if not APP_KEY or not APP_SECRET:
        raise EnvironmentError("KIS_APP_KEY / KIS_APP_SECRET 미설정. pipelines/config.py를 확인하세요.")

    # 토큰 캐시 확인
    if TOKEN_CACHE.exists():
        try:
            cached = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            expires = datetime.fromisoformat(cached.get("expires_at", "2000-01-01"))
            if datetime.now() < expires:
                return cached["access_token"]
        except Exception:
            pass

    url = f"{BASE_URL}/oauth2/tokenP"
    resp = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json={"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data["access_token"]
    expires_in = int(data.get("expires_in", 86400))

    TOKEN_CACHE.write_text(
        json.dumps({
            "access_token": token,
            "expires_at": (datetime.now() + timedelta(seconds=expires_in - 300)).isoformat(),
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    log.info("KIS 토큰 발급 완료")
    return token


def kis_headers(token: str, tr_id: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }


def kis_get(token: str, path: str, tr_id: str, params: dict, retries: int = 2) -> dict:
    """KIS GET — 5xx 시 짧은 백오프로 재시도 (간헐 rate-limit 대응)"""
    last_exc = None
    for attempt in range(retries + 1):
        resp = requests.get(
            f"{BASE_URL}{path}",
            headers=kis_headers(token, tr_id),
            params=params,
            timeout=15,
        )
        if resp.status_code >= 500 and attempt < retries:
            time.sleep(0.4 * (attempt + 1))
            last_exc = requests.HTTPError(f"{resp.status_code} on {path}")
            continue
        resp.raise_for_status()
        data = resp.json()
        rt_cd = data.get("rt_cd", "")
        if rt_cd != "0":
            raise ValueError(f"KIS API 오류 [{rt_cd}]: {data.get('msg1', '')}")
        return data
    raise last_exc  # type: ignore[misc]


# ══════════════════════════════════════════════════════════════════════════════
# 개별 fetch 함수
# ══════════════════════════════════════════════════════════════════════════════

def fetch_ohlcv(ticker: str, token: str):
    """일봉 OHLCV — 최근 120일"""
    end   = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=180)).strftime("%Y%m%d")

    data = kis_get(token,
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
        "FHKST03010100",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        },
    )

    rows = data.get("output2", [])
    path = CACHE_FILE["ohlcv"](ticker)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "open", "high", "low", "close", "volume"])
        for r in reversed(rows):
            if not r.get("stck_bsop_date"):
                continue
            dt = r["stck_bsop_date"]
            writer.writerow([
                f"{dt[:4]}-{dt[4:6]}-{dt[6:]}",
                int(r.get("stck_oprc", 0) or 0),
                int(r.get("stck_hgpr", 0) or 0),
                int(r.get("stck_lwpr", 0) or 0),
                int(r.get("stck_clpr", 0) or 0),
                int(r.get("acml_vol", 0) or 0),
            ])
    log.info(f"[ohlcv] {ticker} {len(rows)}행 저장")


def fetch_technical(ticker: str):
    """기술적 지표 — ohlcv CSV에서 직접 계산"""
    import statistics

    path_csv = CACHE_FILE["ohlcv"](ticker)
    if not path_csv.exists():
        raise FileNotFoundError(f"ohlcv 캐시 없음. 먼저 ohlcv fetch 필요: {path_csv}")

    rows = []
    with open(path_csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append({k: int(v) if k != "date" else v for k, v in r.items()})

    if len(rows) < 20:
        raise ValueError("ohlcv 데이터 부족 (최소 20일 필요)")

    closes  = [r["close"]  for r in rows]
    volumes = [r["volume"] for r in rows]

    def sma(data, n):
        return round(sum(data[-n:]) / n) if len(data) >= n else None

    # 이동평균
    ma5   = sma(closes, 5)
    ma20  = sma(closes, 20)
    ma60  = sma(closes, 60)
    ma120 = sma(closes, 120)

    # RSI(14)
    def calc_rsi(prices, period=14):
        if len(prices) < period + 1:
            return None
        gains, losses = [], []
        for i in range(1, len(prices)):
            d = prices[i] - prices[i-1]
            gains.append(max(d, 0))
            losses.append(max(-d, 0))
        ag = sum(gains[-period:]) / period
        al = sum(losses[-period:]) / period
        if al == 0:
            return 100.0
        rs = ag / al
        return round(100 - 100 / (1 + rs), 1)

    rsi14 = calc_rsi(closes)

    # MACD(12,26,9)
    def ema(prices, n):
        if len(prices) < n:
            return None
        k = 2 / (n + 1)
        e = sum(prices[:n]) / n
        for p in prices[n:]:
            e = p * k + e * (1 - k)
        return e

    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_val    = round(ema12 - ema26) if ema12 and ema26 else None
    macd_signal = None
    macd_hist   = None

    # 볼린저밴드(20일)
    if len(closes) >= 20:
        mid = ma20
        std = statistics.stdev(closes[-20:])
        bb_upper = round(mid + 2 * std)
        bb_lower = round(mid - 2 * std)
    else:
        bb_upper = bb_lower = None

    # 거래량 비율 (5일 평균 대비)
    vol_avg5    = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else None
    vol_ratio_5d = round(volumes[-1] / vol_avg5, 2) if vol_avg5 else None

    result = {
        "ticker": ticker,
        "date": rows[-1]["date"],
        "ma5": ma5, "ma20": ma20, "ma60": ma60, "ma120": ma120,
        "rsi14": rsi14,
        "macd": macd_val, "macd_signal": macd_signal, "macd_hist": macd_hist,
        "bb_upper": bb_upper, "bb_mid": ma20, "bb_lower": bb_lower,
        "vol_ratio_5d": vol_ratio_5d,
    }
    save_json(CACHE_FILE["technical"](ticker), result)
    log.info(f"[technical] {ticker} 계산 완료 (RSI={rsi14}, MA5={ma5})")


def fetch_supply(ticker: str, token: str):
    """수급 — 외인·기관·개인 순매수 (20일)"""
    data = kis_get(token,
        "/uapi/domestic-stock/v1/quotations/inquire-investor",
        "FHKST01010900",
        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
    )

    recent = []
    for r in (data.get("output") or [])[:20]:
        date_raw = r.get("stck_bsop_date", "")
        if not date_raw:
            continue
        recent.append({
            "date": f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}",
            "foreign_net":     int(r.get("frgn_ntby_qty", 0) or 0),
            "institution_net": int(r.get("orgn_ntby_qty", 0) or 0),
            "individual_net":  int(r.get("prsn_ntby_qty", 0) or 0),
        })

    foreign_cumul = sum(r["foreign_net"] for r in recent)
    inst_cumul    = sum(r["institution_net"] for r in recent)

    if foreign_cumul > 0 and inst_cumul > 0:
        trend = "외인+기관 동반 매수"
    elif foreign_cumul > 0:
        trend = "외인 매수 우위"
    elif inst_cumul > 0:
        trend = "기관 매수 우위"
    else:
        trend = "외인+기관 동반 매도"

    result = {
        "ticker": ticker,
        "recent_20d": recent,
        "summary": {
            "foreign_20d_cumul": foreign_cumul,
            "institution_20d_cumul": inst_cumul,
            "trend": trend,
        },
    }
    save_json(CACHE_FILE["supply"](ticker), result)
    log.info(f"[supply] {ticker} {len(recent)}일 저장, trend={trend}")


def fetch_short(ticker: str, token: str):
    """공매도 일별추이 — KIS 국내주식-134 (daily-short-sale)"""
    end   = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=45)).strftime("%Y%m%d")

    data = kis_get(token,
        "/uapi/domestic-stock/v1/quotations/daily-short-sale",
        "FHPST04830000",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
        },
    )

    rows = data.get("output2", []) or []
    recent = []
    prev_short = None
    alert = False
    for r in rows[:20]:
        date_raw = r.get("stck_bsop_date", "")
        if not date_raw:
            continue
        short_vol = int(float(r.get("ssts_cntg_qty", 0) or 0))
        if prev_short and prev_short > 0:
            if (short_vol - prev_short) / prev_short * 100 >= 20:
                alert = True
        prev_short = short_vol
        recent.append({
            "date":                f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}",
            "short_volume":        short_vol,
            "short_ratio":         round(float(r.get("ssts_vol_rlim", 0) or 0), 2),
            "short_balance":       int(float(r.get("acml_ssts_cntg_qty", 0) or 0)),
            "short_balance_ratio": round(float(r.get("acml_ssts_cntg_qty_rlim", 0) or 0), 2),
            "margin_balance":      0,
        })
    recent.reverse()  # 최신이 마지막

    result = {"ticker": ticker, "recent_20d": recent, "alert": alert}
    save_json(CACHE_FILE["short"](ticker), result)
    log.info(f"[short] {ticker} {len(recent)}일 저장, alert={alert}")


def fetch_broker(ticker: str, token: str):
    """거래원 — 기관·외국인 창구"""
    data = kis_get(token,
        "/uapi/domestic-stock/v1/quotations/inquire-member",
        "FHKST01010600",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": datetime.now().strftime("%Y%m%d"),
        },
    )

    # output 또는 output1 둘 다 시도
    output = data.get("output1") or data.get("output") or {}
    if isinstance(output, list):
        output = output[0] if output else {}

    # KIS 필드: seln_*=매도, shnu_*=매수, total_*_qty=수량, *_rlim=점유율(%)
    top_buy, top_sell = [], []
    for i in range(1, 6):
        buy_nm  = output.get(f"shnu_mbcr_name{i}", "")
        buy_amt = int(output.get(f"total_shnu_qty{i}", 0) or 0)
        sel_nm  = output.get(f"seln_mbcr_name{i}", "")
        sel_amt = int(output.get(f"total_seln_qty{i}", 0) or 0)
        if buy_nm:
            top_buy.append({"broker": buy_nm, "amount": buy_amt})
        if sel_nm:
            top_sell.append({"broker": sel_nm, "amount": sel_amt})

    result = {
        "ticker": ticker,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "top_buy":  top_buy[:5],
        "top_sell": top_sell[:5],
        "foreign_desk_concentration": False,
        "program_buy_ratio": 0.0,
    }
    save_json(CACHE_FILE["broker"](ticker), result)
    log.info(f"[broker] {ticker} 저장 완료")


def fetch_financial(ticker: str, token: str):
    """재무·실적 — 손익계산서 + 밸류에이션"""
    # 손익계산서 (TR_ID FHKST66430200 = 손익계산서, 300 = 재무비율)
    inc = kis_get(token,
        "/uapi/domestic-stock/v1/finance/income-statement",
        "FHKST66430200",
        {"FID_DIV_CLS_CODE": "1", "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
    )

    def to_int(v):
        try:
            return int(float(v)) if v not in ("", None) else 0
        except (ValueError, TypeError):
            return 0

    def to_float(v):
        try:
            return float(v) if v not in ("", None) else 0.0
        except (ValueError, TypeError):
            return 0.0

    quarters = []
    for r in (inc.get("output") or [])[:4]:
        period = r.get("stac_yymm", "")
        if not period:
            continue
        quarters.append({
            "period": period,
            "revenue":    to_int(r.get("sale_account", 0)),
            "op_profit":  to_int(r.get("bsop_prti", 0)),
            "net_profit": to_int(r.get("thtr_ntin", 0)),
            "yoy_rev":    round(to_float(r.get("sale_account_yoy", 0)), 1),
            "qoq_rev":    round(to_float(r.get("sale_account_qoq", 0)), 1),
        })

    # 밸류에이션 (주식 기본 시세)
    val_data = kis_get(token,
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "FHKST01010100",
        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
    )
    o = val_data.get("output", {}) or {}

    result = {
        "ticker": ticker,
        "quarters": quarters,
        "valuation": {
            "per":            round(float(o.get("per", 0) or 0), 1),
            "pbr":            round(float(o.get("pbr", 0) or 0), 1),
            "roe":            round(float(o.get("roe", 0) or 0), 1),
            "sector_per_avg": 0.0,
        },
        "next_earnings_date": "",
        "earnings_surprise_history": [],
    }
    save_json(CACHE_FILE["financial"](ticker), result)
    log.info(f"[financial] {ticker} {len(quarters)}분기 저장")


def fetch_market(ticker: str, token: str):
    """시장환경 — KOSPI 대비 상대강도"""
    # 종목 최근 30일 OHLCV
    end   = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=45)).strftime("%Y%m%d")

    def get_stock_closes(iscd):
        d = kis_get(token,
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKST03010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": iscd,
             "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": end,
             "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"},
        )
        prices = [int(r.get("stck_clpr", 0) or 0) for r in reversed(d.get("output2", [])) if r.get("stck_clpr")]
        return prices

    def get_index_closes(iscd):
        try:
            d = kis_get(token,
                "/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice",
                "FHKUP03500100",
                {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": iscd,
                 "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": end,
                 "FID_PERIOD_DIV_CODE": "D"},
            )
            prices = [int(float(r.get("bstp_nmix_prpr", 0) or 0))
                      for r in reversed(d.get("output2", []) or [])
                      if r.get("bstp_nmix_prpr")]
            return prices
        except Exception as e:
            log.warning(f"KOSPI 지수 조회 실패: {e}")
            return []

    stock_prices  = get_stock_closes(ticker)
    kospi_prices  = get_index_closes("0001")

    def ret_1m(prices):
        if len(prices) < 2:
            return 0.0
        return round((prices[-1] - prices[-22]) / prices[-22] * 100, 1) if len(prices) >= 22 else round((prices[-1] - prices[0]) / prices[0] * 100, 1)

    rs_kospi = round(ret_1m(stock_prices) - ret_1m(kospi_prices), 1)

    # 당일 KOSPI 등락
    price_data = kis_get(token,
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "FHKST01010100",
        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
    )
    o = price_data.get("output", {}) or {}

    result = {
        "ticker": ticker,
        "rs_vs_kospi_1m":    rs_kospi,
        "rs_vs_sector_1m":   0.0,
        "sector_index_trend": "확인필요",
        "peer_performance":   "확인필요",
        "macro": {
            "kospi":   round(float(o.get("bstp_nmix_prdy_ctrt", 0) or 0), 2),
            "usd_krw": 0,
            "us_10y":  0.0,
        },
    }
    save_json(CACHE_FILE["market"](ticker), result)
    log.info(f"[market] {ticker} RS={rs_kospi}")


def fetch_info(ticker: str, token: str):
    """종목 기본정보"""
    # 기존 캐시가 있으면 이름만 보완 (500 오류 방어)
    cached_name = ""
    cache_path = CACHE_FILE["info"](ticker)
    if cache_path.exists():
        try:
            cached_name = json.loads(cache_path.read_text(encoding="utf-8")).get("name", "")
        except Exception:
            pass

    # KIS 시장코드 → 표시명 매핑
    MKT_MAP = {"STK": "KOSPI", "KSQ": "KOSDAQ", "KNX": "KONEX"}

    try:
        data = kis_get(token,
            "/uapi/domestic-stock/v1/quotations/search-stock-info",
            "CTPF1002R",
            {"PRDT_TYPE_CD": "300", "PDNO": ticker},
        )
        o = data.get("output", {}) or {}
        mket_raw = o.get("mket_id_cd", "")
        result = {
            "ticker": ticker,
            "name":               o.get("prdt_abrv_name", "") or cached_name,
            "market":             MKT_MAP.get(mket_raw, mket_raw),
            "sector":             o.get("std_idst_clsf_cd_name", ""),
            "shares_outstanding": int(o.get("lstg_stqt", 0) or 0),
        }
    except Exception as e:
        log.warning(f"[info] search-stock-info 실패({e}), inquire-price fallback")
        price_data = kis_get(token,
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        o2 = price_data.get("output", {}) or {}
        result = {
            "ticker": ticker,
            "name":               o2.get("hts_kor_isnm", "") or cached_name,
            "market":             o2.get("rprs_mrkt_kor_name", ""),
            "sector":             o2.get("bstp_kor_isnm", ""),
            "shares_outstanding": int(o2.get("lstn_stcn", 0) or 0),
        }
    save_json(cache_path, result)
    log.info(f"[info] {ticker} {result.get('name')} 저장")


def fetch_news(ticker: str, token: str):
    """종합 시황/공시(제목) — KIS 국내주식-141 (news-title, FHKST01011800)

    파라미터 8개 모두 필수. 종목코드 외에는 공백 문자열로 전달.
    """
    articles = []
    try:
        data = kis_get(token,
            "/uapi/domestic-stock/v1/quotations/news-title",
            "FHKST01011800",
            {
                "FID_NEWS_OFER_ENTP_CODE": "",
                "FID_COND_MRKT_CLS_CODE":  "",
                "FID_INPUT_ISCD":          ticker,
                "FID_TITL_CNTT":           "",
                "FID_INPUT_DATE_1":        "",
                "FID_INPUT_HOUR_1":        "",
                "FID_RANK_SORT_CLS_CODE":  "",
                "FID_INPUT_SRNO":          "",
            },
        )
        for r in (data.get("output") or [])[:20]:
            date_raw = r.get("data_dt", "")
            dt = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}" if len(date_raw) >= 8 else date_raw
            articles.append({
                "date":    dt,
                "time":    r.get("data_tm", ""),
                "title":   r.get("hts_pbnt_titl_cntt", ""),
                "source":  r.get("dorg", ""),
                "summary": "",
            })
    except Exception as e:
        log.warning(f"[news] KIS news-title 실패({e}) — 빈 배열 저장")

    result = {"ticker": ticker, "articles": articles}
    save_json(CACHE_FILE["news"](ticker), result)
    log.info(f"[news] {ticker} {len(articles)}건 저장")


# ══════════════════════════════════════════════════════════════════════════════
# fetch_one 디스패처
# ══════════════════════════════════════════════════════════════════════════════
def fetch_one(ticker: str, kind: str, force: bool = False):
    path = CACHE_FILE[kind](ticker)
    if not force and is_cache_valid(path, kind):
        log.info(f"  [CACHE HIT] {ticker} {kind}")
        return

    log.info(f"  [FETCH] {ticker} {kind} ...")
    token = get_access_token()

    try:
        if kind == "ohlcv":
            fetch_ohlcv(ticker, token)
        elif kind == "technical":
            # technical은 ohlcv 먼저 필요
            if not is_cache_valid(CACHE_FILE["ohlcv"](ticker), "ohlcv"):
                fetch_ohlcv(ticker, token)
            fetch_technical(ticker)
        elif kind == "supply":
            fetch_supply(ticker, token)
        elif kind == "short":
            fetch_short(ticker, token)
        elif kind == "broker":
            fetch_broker(ticker, token)
        elif kind == "financial":
            fetch_financial(ticker, token)
        elif kind == "market":
            fetch_market(ticker, token)
        elif kind == "info":
            fetch_info(ticker, token)
        elif kind == "news":
            fetch_news(ticker, token)
    except Exception as e:
        log.error(f"  [ERROR] {ticker} {kind}: {e}")
        save_stale(path, kind)
        raise


# ══════════════════════════════════════════════════════════════════════════════
# 유틸
# ══════════════════════════════════════════════════════════════════════════════
def get_all_tickers() -> list:
    tickers = [p.stem for p in PROFILES_DIR.glob("*.md")]
    if not tickers:
        log.warning("profiles/ 에 종목 파일이 없습니다.")
    return tickers


# ══════════════════════════════════════════════════════════════════════════════
# 메인
# ══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="KIS API daily fetch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"지원 타입: {', '.join(ALL_TYPES)}"
    )
    parser.add_argument("--ticker", help="종목 코드 (예: 327260)")
    parser.add_argument("--all",    action="store_true", help="profiles/ 기준 전체 종목")
    parser.add_argument("--force",  action="store_true", help="캐시 무시 강제 갱신")
    parser.add_argument("--type",   choices=ALL_TYPES, default=None)
    args = parser.parse_args()

    if args.all:
        tickers = get_all_tickers()
    elif args.ticker:
        tickers = [args.ticker]
    else:
        parser.print_help()
        sys.exit(1)

    types_to_fetch = [args.type] if args.type else ALL_TYPES

    for ticker in tickers:
        print(f"\n▶ {ticker}  (fetch: {', '.join(types_to_fetch)})")
        for kind in types_to_fetch:
            try:
                fetch_one(ticker, kind, force=args.force)
                print(f"  ✅ {kind}")
            except Exception as e:
                print(f"  ❌ {kind}: {e}")
            time.sleep(0.15)  # KIS rate-limit 완화 (5~10 req/s)

    print("\n✅ fetch_daily.py 완료")


if __name__ == "__main__":
    main()
