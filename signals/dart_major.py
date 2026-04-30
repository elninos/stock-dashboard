"""DART 대량보유 공시 + 최대주주 현황 분석.

KRX 9분류(사모/기타법인) 직접 조회가 불가한 상황에서,
DART 공시를 통해 5% 이상 법인 주주(사모/기타법인) 동향을 파악한다.

제공 데이터:
  1. 대량보유 공시 타임라인 — 누가 언제 지분을 변경했나
  2. 최대주주 현황 — 연도말 기준 지분 구조
  3. 임원/주요주주 특정증권 변동 — 내부자 매매
"""
import os, json, time, zipfile, io, re, warnings
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

DART_KEY = os.getenv("DART_API_KEY", "95a83c9efdb1e3ce13be539270823fa31aafdad5")
DART_BASE = "https://opendart.fss.or.kr/api"

CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "dart_cache"
)
os.makedirs(CACHE_DIR, exist_ok=True)

# 법인 유형 분류 (보고자 이름 → 투자자 유형)
_PENSION_KEYWORDS  = ["국민연금", "공무원연금", "사학연금", "우정사업", "공제회", "공단"]
_FUND_KEYWORDS     = ["자산운용", "투자운용", "자산관리", "헤지펀드", "사모", "밸류", "파트너스",
                       "인베스트", "캐피탈", "어드바이저"]
_FOREIGN_KEYWORDS  = ["맥쿼리", "블랙록", "뱅가드", "피델리티", "ASSET", "CAPITAL", "FUND", "LP",
                       "골드만", "모간", "씨티", "UBS", "도이치", "노무라", "Credit"]
_CORP_KEYWORDS     = ["(주)", "주식회사", "인더스트리", "엔터", "홀딩스"]


def _investor_type(name: str) -> str:
    """보고자 이름으로 투자자 유형 추론."""
    for kw in _PENSION_KEYWORDS:
        if kw in name:
            return "연기금등"
    for kw in _FUND_KEYWORDS:
        if kw in name:
            return "사모"
    for kw in _FOREIGN_KEYWORDS:
        if kw in name:
            return "외국인"
    for kw in _CORP_KEYWORDS:
        if kw in name:
            return "기타법인"
    return "개인"


def _cache(name: str) -> str:
    return os.path.join(CACHE_DIR, name)


def _fresh(path: str, hours: float = 12) -> bool:
    return os.path.exists(path) and (time.time() - os.path.getmtime(path)) / 3600 < hours


def _get(endpoint: str, params: dict, cache_key: str = "", hours: float = 12) -> dict:
    if cache_key:
        cp = _cache(cache_key)
        if _fresh(cp, hours):
            with open(cp, encoding="utf-8") as f:
                return json.load(f)
    params["crtfc_key"] = DART_KEY
    resp = requests.get(f"{DART_BASE}/{endpoint}", params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if cache_key and data.get("status") == "000":
        with open(_cache(cache_key), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    return data


# ──────────────────────────────────────────────
# corp_code 조회 (종목코드 → DART corp_code)
# ──────────────────────────────────────────────

_CORP_CODE_CACHE: dict[str, str] = {}


def get_corp_code(stock_code: str) -> "str | None":
    """6자리 종목코드 → DART corp_code (8자리 숫자)."""
    if stock_code in _CORP_CODE_CACHE:
        return _CORP_CODE_CACHE[stock_code]

    cp = _cache("corp_code_map.json")
    if _fresh(cp, hours=24 * 7):
        with open(cp, encoding="utf-8") as f:
            mapping = json.load(f)
        if stock_code in mapping:
            _CORP_CODE_CACHE[stock_code] = mapping[stock_code]
            return mapping[stock_code]

    # 전체 corpCode.xml 다운로드 (7일 캐시)
    xml_path = _cache("corpCode.xml.zip")
    if not _fresh(xml_path, hours=24 * 7):
        resp = requests.get(f"{DART_BASE}/corpCode.xml",
                            params={"crtfc_key": DART_KEY}, timeout=30)
        resp.raise_for_status()
        with open(xml_path, "wb") as f:
            f.write(resp.content)

    with open(xml_path, "rb") as f:
        z = zipfile.ZipFile(f)
        xml_data = z.read(z.namelist()[0])

    root = ET.fromstring(xml_data)
    mapping = {}
    for item in root.findall(".//list"):
        code = (item.findtext("stock_code") or "").strip()
        corp = (item.findtext("corp_code") or "").strip()
        if code and corp:
            mapping[code] = corp

    with open(cp, "w", encoding="utf-8") as f:
        json.dump(mapping, f)

    result = mapping.get(stock_code)
    if result:
        _CORP_CODE_CACHE[stock_code] = result
    return result


# ──────────────────────────────────────────────
# 대량보유 공시 타임라인
# ──────────────────────────────────────────────

def major_holder_timeline(
    stock_code: str,
    start: str = "",
    end: str = "",
) -> list[dict]:
    """대량보유 공시 + 임원/주요주주 특정증권 변동 타임라인.

    Args:
        stock_code: 6자리 종목코드
        start/end: YYYYMMDD, 기본 최근 1년

    Returns:
        list of {
            "date": str,
            "type": "대량보유"|"임원변동",
            "who": str,         # 보고자명
            "investor_type": str,
            "report": str,      # 공시 제목
            "rcept_no": str,
        }
    """
    corp_code = get_corp_code(stock_code)
    if not corp_code:
        return []

    if not start:
        start = datetime(datetime.today().year - 1, datetime.today().month,
                         datetime.today().day).strftime("%Y%m%d")
    if not end:
        end = datetime.today().strftime("%Y%m%d")

    ck = f"dart_list_{corp_code}_{start}_{end}.json"
    data = _get("list.json", {
        "corp_code": corp_code,
        "bgn_de":    start,
        "end_de":    end,
        "page_count": 100,
    }, cache_key=ck, hours=6)

    items = data.get("list", [])
    timeline = []
    for item in items:
        nm  = item.get("report_nm", "")
        flr = item.get("flr_nm", "")
        dt  = item.get("rcept_dt", "")
        rno = item.get("rcept_no", "")

        if "대량보유" in nm:
            kind = "대량보유"
        elif "임원" in nm and "특정증권" in nm:
            kind = "임원변동"
        else:
            continue

        # 기재정정은 원본과 유사하므로 중복 제거
        if "[기재정정]" in nm:
            continue

        timeline.append({
            "date":          f"{dt[:4]}-{dt[4:6]}-{dt[6:]}",
            "type":          kind,
            "who":           flr,
            "investor_type": _investor_type(flr),
            "report":        nm,
            "rcept_no":      rno,
        })

    timeline.sort(key=lambda x: x["date"])
    return timeline


# ──────────────────────────────────────────────
# 최대주주 현황 (연말 사업보고서 기준)
# ──────────────────────────────────────────────

def largest_shareholders(stock_code: str, year=None) -> list[dict]:
    """DART 최대주주 현황.

    Args:
        stock_code: 6자리 종목코드
        year: 사업보고서 기준 연도 (기본 전년도)

    Returns:
        list of {
            "name": str,
            "relation": str,       # 최대주주/특수관계자
            "investor_type": str,
            "begin_shares": int,
            "end_shares": int,
            "end_pct": float,
            "delta_pct": float,    # 기초→기말 지분율 변동
        }
    """
    corp_code = get_corp_code(stock_code)
    if not corp_code:
        return []

    if year is None:
        # 현재 월이 4월 이후면 전년도 사업보고서 사용
        today = datetime.today()
        year = today.year - 1 if today.month <= 3 else today.year - 1

    ck = f"dart_lgshr_{corp_code}_{year}.json"
    data = _get("hyslrSttus.json", {
        "corp_code":  corp_code,
        "bsns_year":  str(year),
        "reprt_code": "11011",  # 사업보고서
    }, cache_key=ck, hours=24)

    result = []
    for item in (data.get("list") or []):
        kind = item.get("stock_knd", "")
        if kind and kind != "보통주":
            continue  # 우선주 제외

        def _parse(s: str) -> float:
            try:
                return float(str(s or "0").replace(",", ""))
            except ValueError:
                return 0.0

        begin_pct = _parse(item.get("bsis_posesn_stock_qota_rt"))
        end_pct   = _parse(item.get("trmend_posesn_stock_qota_rt"))

        result.append({
            "name":          item.get("nm", ""),
            "relation":      item.get("relate", ""),
            "investor_type": _investor_type(item.get("nm", "")),
            "begin_shares":  int(_parse(item.get("bsis_posesn_stock_co"))),
            "end_shares":    int(_parse(item.get("trmend_posesn_stock_co"))),
            "begin_pct":     begin_pct,
            "end_pct":       end_pct,
            "delta_pct":     round(end_pct - begin_pct, 2),
        })

    return result


# ──────────────────────────────────────────────
# 종합 분석 출력
# ──────────────────────────────────────────────

def print_major_analysis(stock_code: str, name: str = "", start: str = "", end: str = "") -> dict:
    """대량보유/최대주주 분석 결과를 출력하고 dict 반환."""
    label = name or stock_code
    print(f"\n{'═'*50}")
    print(f"▶ 주요주주 / 법인 매집 현황 [{label}]")
    print(f"{'═'*50}")

    # 1) 최대주주 현황
    shareholders = largest_shareholders(stock_code)
    if shareholders:
        print("\n[최대주주 현황 (최근 사업보고서 기준)]")
        for s in shareholders:
            delta_str = (f"+{s['delta_pct']:.2f}%p" if s['delta_pct'] > 0
                         else f"{s['delta_pct']:.2f}%p" if s['delta_pct'] < 0
                         else "변동없음")
            print(f"  {s['name']:20s}  {s['investor_type']:8s}  "
                  f"{s['end_pct']:.2f}%  ({delta_str})")
    else:
        print("  최대주주 현황 없음")

    # 2) 대량보유 타임라인
    timeline = major_holder_timeline(stock_code, start=start, end=end)
    if timeline:
        print("\n[대량보유 공시 / 임원 변동 타임라인]")
        for t in timeline:
            marker = "🏢" if t["investor_type"] == "기타법인" else \
                     "🏦" if t["investor_type"] == "연기금등" else \
                     "🌍" if t["investor_type"] == "외국인" else "👤"
            print(f"  {t['date']}  {marker} {t['who']:20s}  {t['type']}  {t['report']}")
    else:
        print("  공시 없음 (해당 기간)")

    # 3) 요약 인사이트
    print()
    corps = [s for s in shareholders if s["investor_type"] == "기타법인"]
    pensions = [s for s in shareholders if s["investor_type"] == "연기금등"]
    recent_corps = [t for t in timeline if t["investor_type"] in ("기타법인", "사모")]
    recent_pension = [t for t in timeline if t["investor_type"] == "연기금등"]

    insights = []
    if corps:
        top = corps[0]
        insights.append(
            f"  기타법인 대주주: {top['name']} ({top['end_pct']:.1f}%, "
            f"{'증가' if top['delta_pct'] > 0 else '감소'} {abs(top['delta_pct']):.1f}%p)"
        )
    if pensions:
        top = pensions[0]
        insights.append(f"  연기금 보유: {top['name']} ({top['end_pct']:.1f}%)")
    if recent_corps:
        insights.append(f"  법인 대량보유 공시 {len(recent_corps)}건 (최근 1년)")
    if recent_pension:
        insights.append(f"  연기금 보유 공시 {len(recent_pension)}건 (최근 1년)")
    if not insights:
        insights.append("  5% 이상 법인/기관 보유자 없음")

    print("[인사이트]")
    for i in insights:
        print(i)

    return {
        "shareholders": shareholders,
        "timeline":     timeline,
        "corps":        corps,
        "pensions":     pensions,
    }


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "010170"
    name = sys.argv[2] if len(sys.argv) > 2 else ""
    print_major_analysis(code, name)
