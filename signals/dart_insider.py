"""DART OpenAPI — 임원/주요주주 거래, 5% 보유 변동, 자사주.

전용 엔드포인트로 증감수량/방향까지 파싱:
  - elestock     임원·주요주주 특정증권 소유보고 (isu_dcrs_qy = 증감수량, 양수=매수)
  - majorstock   주식등의 대량보유 (5%) (stkqy_irds = 증감수량)
  - tsstkAcqDecsn 자사주 취득 결정
  - tsstkDpDecsn  자사주 처분 결정

가장 강한 시그널:
  - 임원 매수 (isu_dcrs_qy > 0) = "내부자가 산다"
  - 임원 매도 (isu_dcrs_qy < 0) = "내부자가 판다"
  - 5% 보유 감소 (stkqy_irds < 0) = 큰손 이탈
  - 자사주 취득 결정 = 매수 신호
  - 자사주 처분 결정 = 매도 신호
"""
import os
import re
import requests
from datetime import datetime, timedelta

ENV_FILE = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-srshin614@gmail.com"
    "/내 드라이브/01.Claude/01.주식/.env.dart"
)

BASE = "https://opendart.fss.or.kr/api"


def _load_key():
    if not os.path.exists(ENV_FILE):
        return None
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE)
    return os.environ.get("DART_API_KEY")


_dart = None
_corp_cache = {}


def get_reader():
    """OpenDartReader (corp_code 매핑용)."""
    global _dart
    if _dart is not None:
        return _dart
    key = _load_key()
    if not key:
        raise RuntimeError(
            f"DART API 키 없음. {ENV_FILE} 에 DART_API_KEY=xxx 설정 필요."
        )
    import OpenDartReader
    _dart = OpenDartReader(key)
    return _dart


def get_corp_code(stock_code: str):
    """종목코드 → DART 고유번호."""
    if stock_code in _corp_cache:
        return _corp_cache[stock_code]
    dart = get_reader()
    try:
        code = dart.find_corp_code(stock_code)
    except Exception:
        code = None
    _corp_cache[stock_code] = code
    return code


def _to_int(v) -> int:
    if v is None:
        return 0
    s = str(v).replace(",", "").strip()
    if s in ("", "-"):
        return 0
    try:
        return int(re.sub(r"[^\-0-9]", "", s) or "0")
    except Exception:
        return 0


def _api(endpoint: str, **params) -> list:
    """DART API 직접 호출."""
    key = _load_key()
    if not key:
        return []
    params["crtfc_key"] = key
    try:
        r = requests.get(f"{BASE}/{endpoint}.json", params=params, timeout=10)
        data = r.json()
    except Exception:
        return []
    if data.get("status") not in ("000",):
        return []
    return data.get("list", [])


def fetch_insider_trades(stock_code: str, start: str, end: str = None) -> list:
    """임원·주요주주 특정증권 소유보고 (방향성 포함).

    반환: [{date, name, relation, change_qty, after_qty, reason, rcept_no, url}]
    change_qty 양수=매수, 음수=매도
    """
    corp = get_corp_code(stock_code)
    if not corp:
        return []
    rows = _api("elestock", corp_code=corp)
    if end is None:
        end = datetime.now().strftime("%Y%m%d")

    out = []
    for r in rows:
        rcept = r.get("rcept_no", "")
        date = rcept[:8] if rcept else ""
        if not (start <= date <= end):
            continue
        # 증감수량 = sp_stock_lmp_irds_cnt (양수=늘림/매수, 음수=줄임/매도)
        qty = _to_int(r.get("sp_stock_lmp_irds_cnt"))
        out.append({
            "date":       date,
            "name":       r.get("repror"),
            "relation":   r.get("isu_exctv_rgist_at"),  # 등기/미등기
            "position":   r.get("isu_exctv_ofcps"),     # 직위
            "main":       r.get("isu_main_shrholdr"),   # 주요주주 여부
            "change_qty": qty,                          # 증감수량 (+/-)
            "rate_irds":  r.get("sp_stock_lmp_irds_rate"),  # 비율 증감
            "after_qty":  _to_int(r.get("sp_stock_lmp_cnt")),
            "after_rate": r.get("sp_stock_lmp_rate"),
            "rcept_no":   rcept,
            "url":        f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
        })
    return out


def fetch_major_holder_changes(stock_code: str, start: str, end: str = None) -> list:
    """5% 대량보유 변동 (방향성 포함)."""
    corp = get_corp_code(stock_code)
    if not corp:
        return []
    rows = _api("majorstock", corp_code=corp)
    if end is None:
        end = datetime.now().strftime("%Y%m%d")

    out = []
    for r in rows:
        rcept = r.get("rcept_no", "")
        date = rcept[:8] if rcept else ""
        if not (start <= date <= end):
            continue
        out.append({
            "date":       date,
            "holder":     r.get("repror"),
            "stkqy":      _to_int(r.get("stkqy")),         # 보유주식수
            "stkqy_irds": _to_int(r.get("stkqy_irds")),    # 증감수량 (+늘림/-줄임)
            "stkrt":      r.get("stkrt"),                  # 보유비율
            "stkrt_irds": r.get("stkrt_irds"),             # 비율 증감
            "ctr_stkqy":  _to_int(r.get("ctr_stkqy")),
            "rcept_no":   rcept,
            "url":        f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
        })
    return out


def fetch_treasury_stock(stock_code: str, start: str, end: str = None) -> list:
    """자사주 취득/처분 결정 공시."""
    corp = get_corp_code(stock_code)
    if not corp:
        return []
    if end is None:
        end = datetime.now().strftime("%Y%m%d")

    out = []
    # 직접 결정 (취득)
    for r in _api("tsstkAcqDecsn", corp_code=corp,
                  bgn_de=start, end_de=end):
        out.append({
            "date":     r.get("rcept_no", "")[:8],
            "type":     "buy",
            "qty":      _to_int(r.get("aqpln_stk_co")),    # 취득예정수량
            "amount":   _to_int(r.get("aqpln_prc_tot")),   # 취득예정금액
            "method":   r.get("aqpln_mth"),                # 취득방법
            "purpose":  r.get("aqpln_pp"),                 # 목적
            "rcept_no": r.get("rcept_no"),
            "url":      f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={r.get('rcept_no')}",
        })
    # 처분 결정
    for r in _api("tsstkDpDecsn", corp_code=corp,
                  bgn_de=start, end_de=end):
        out.append({
            "date":     r.get("rcept_no", "")[:8],
            "type":     "sell",
            "qty":      _to_int(r.get("dppln_stk_co")),
            "amount":   _to_int(r.get("dppln_prc_tot")),
            "method":   r.get("dppln_mth"),
            "purpose":  r.get("dppln_pp"),
            "rcept_no": r.get("rcept_no"),
            "url":      f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={r.get('rcept_no')}",
        })
    return out


def analyze_insider_signal(stock_code: str, lookback_days: int = 180) -> dict:
    """방향성 있는 인사이더 시그널.

    score 양수=매도 신호, 음수=매수 신호
    """
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")

    try:
        insiders = fetch_insider_trades(stock_code, start, end)
        major = fetch_major_holder_changes(stock_code, start, end)
        treasury = fetch_treasury_stock(stock_code, start, end)
    except Exception as e:
        return {"available": False, "error": str(e)[:80]}

    score = 0
    signals = []

    # 임원 거래: 매수 vs 매도 분리
    ins_buys  = [i for i in insiders if i["change_qty"] > 0]
    ins_sells = [i for i in insiders if i["change_qty"] < 0]
    ins_buy_qty  = sum(i["change_qty"] for i in ins_buys)
    ins_sell_qty = -sum(i["change_qty"] for i in ins_sells)

    if ins_sells:
        signals.append(f"임원 매도 {len(ins_sells)}건 ({ins_sell_qty:,}주)")
        score += min(len(ins_sells) * 2, 10)
    if ins_buys:
        signals.append(f"임원 매수 {len(ins_buys)}건 ({ins_buy_qty:,}주)")
        score -= min(len(ins_buys) * 2, 10)

    # 5% 대량보유: 늘림(+) vs 줄임(-)
    maj_inc = [m for m in major if m["stkqy_irds"] > 0]
    maj_dec = [m for m in major if m["stkqy_irds"] < 0]
    if maj_dec:
        signals.append(f"5%주주 감소 {len(maj_dec)}건")
        score += min(len(maj_dec) * 3, 9)
    if maj_inc:
        signals.append(f"5%주주 증가 {len(maj_inc)}건")
        score -= min(len(maj_inc) * 2, 6)

    # 자사주
    ts_buys  = [t for t in treasury if t["type"] == "buy"]
    ts_sells = [t for t in treasury if t["type"] == "sell"]
    if ts_buys:
        signals.append(f"자사주 취득결정 {len(ts_buys)}건")
        score -= 4
    if ts_sells:
        signals.append(f"자사주 처분결정 {len(ts_sells)}건")
        score += 4

    return {
        "available":   True,
        "n_insiders":  len(insiders),
        "n_ins_buys":  len(ins_buys),
        "n_ins_sells": len(ins_sells),
        "ins_buy_qty":  ins_buy_qty,
        "ins_sell_qty": ins_sell_qty,
        "n_major_inc": len(maj_inc),
        "n_major_dec": len(maj_dec),
        "n_ts_buys":   len(ts_buys),
        "n_ts_sells":  len(ts_sells),
        "score":       score,  # 양수=매도, 음수=매수
        "signals":     signals,
        "insiders":    insiders,
        "major":       major,
        "treasury":    treasury,
    }
