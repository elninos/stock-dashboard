"""KRX 9분류 투자자 데이터 수집 — Playwright 브라우저 세션 방식.

data.krx.co.kr은 JS 세션 쿠키가 필요해서 직접 API 호출이 안 됨.
Playwright로 메인 페이지를 방문해 JSESSIONID 등 쿠키를 획득한 뒤
requests로 API를 조회함.

9분류: 금융투자/보험/투신/사모/은행/기타금융/연기금등/기타법인/개인/외국인
"""
import os, json, time, logging
from datetime import datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

KRX_MAIN   = "https://data.krx.co.kr/contents/MDC/MDI/mdiBusi/MdiBusList.cmd"
KRX_API    = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
CACHE_DIR  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "krx_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# MDCSTAT02303 컬럼 → 한글 매핑
COL_MAP = {
    "TRDVAL1":  "금융투자",
    "TRDVAL2":  "보험",
    "TRDVAL3":  "투신",
    "TRDVAL4":  "사모",
    "TRDVAL5":  "은행",
    "TRDVAL6":  "기타금융",
    "TRDVAL7":  "연기금등",
    "TRDVAL8":  "기타법인",
    "TRDVAL9":  "개인",
    "TRDVAL10": "외국인",
}

INVESTOR_CATEGORIES = list(COL_MAP.values())


def _isin(code: str) -> str:
    """6자리 종목코드 → ISIN (보통주 기준 KR7XXXXXX004)."""
    return f"KR7{code}004"


def _cache_path(code: str, date: str) -> str:
    return os.path.join(CACHE_DIR, f"krx9_{code}_{date}.json")


def _cache_range_path(code: str, start: str, end: str) -> str:
    return os.path.join(CACHE_DIR, f"krx9_{code}_{start}_{end}.json")


def _is_fresh(path: str, ttl_hours: float = 6) -> bool:
    if not os.path.exists(path):
        return False
    age = (time.time() - os.path.getmtime(path)) / 3600
    return age < ttl_hours


# ─────────────────────────────────────────────
# 쿠키 획득 (Playwright)
# ─────────────────────────────────────────────

def _get_krx_cookies() -> dict:
    """Playwright로 KRX 메인 페이지 방문 → 세션 쿠키 획득."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()
        # 메인 페이지 방문 — JS가 JSESSIONID 등 세션 쿠키 세팅
        page.goto(KRX_MAIN, wait_until="networkidle", timeout=30_000)
        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        browser.close()

    logger.debug("KRX 쿠키 획득: %s", list(cookies.keys()))
    return cookies


# ─────────────────────────────────────────────
# 단일 날짜 조회
# ─────────────────────────────────────────────

def fetch_one_day(code: str, date: str, cookies: Optional[dict] = None) -> Optional[dict]:
    """하루치 9분류 순매수금액 조회.

    Args:
        code:  종목코드 6자리 (e.g. "010170")
        date:  YYYYMMDD (e.g. "20250101")
        cookies: 재사용할 KRX 세션 쿠키 (None이면 새로 획득)

    Returns:
        {"date": "2025-01-01", "금융투자": 0, "보험": 0, ..., "외국인": 0}
        또는 None (오류/휴장일)
    """
    cache = _cache_path(code, date)
    if _is_fresh(cache):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)

    if cookies is None:
        cookies = _get_krx_cookies()

    headers = {
        "Referer":      "https://data.krx.co.kr/",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent":   ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/120.0.0.0 Safari/537.36"),
        "X-Requested-With": "XMLHttpRequest",
    }

    payload = {
        "bld":    "MDCSTAT02303",
        "locale": "ko_KR",
        "isuCd":  _isin(code),
        "trdDd":  date,
        "share":  "1",     # 거래대금 기준
        "money":  "1",     # 억 원 단위
        "csvxls_isNo": "false",
    }

    try:
        resp = requests.post(
            KRX_API,
            data=payload,
            headers=headers,
            cookies=cookies,
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        logger.warning("KRX API 오류 (%s %s): %s", code, date, e)
        return None

    # 응답 구조: {"output": [{...}], "CURRENT_DATETIME": "..."}
    rows = body.get("output") or body.get("OutBlock_1") or []
    if not rows:
        # 휴장일이거나 데이터 없음
        return None

    row = rows[0]

    # LOGOUT 체크
    if "LOGOUT" in str(row) or body.get("STATUS") == "LOGOUT":
        logger.warning("KRX 세션 만료. 쿠키 재획득 필요.")
        return None

    out = {"date": f"{date[:4]}-{date[4:6]}-{date[6:]}"}
    for col, name in COL_MAP.items():
        raw = row.get(col, "0")
        # 쉼표 제거 후 정수 변환 (음수 가능)
        try:
            out[name] = int(str(raw).replace(",", "").replace("+", "") or "0")
        except ValueError:
            out[name] = 0

    with open(cache, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    return out


# ─────────────────────────────────────────────
# 날짜 범위 조회
# ─────────────────────────────────────────────

def fetch_range(
    code: str,
    start: str,
    end: str,
    delay: float = 0.3,
) -> list[dict]:
    """start~end 기간 9분류 데이터 조회.

    Args:
        code:  6자리 종목코드
        start: YYYYMMDD
        end:   YYYYMMDD
        delay: 요청 사이 딜레이(초)

    Returns:
        list of {"date", "금융투자", ..., "외국인"}  (휴장일 제외)
    """
    cache = _cache_range_path(code, start, end)
    if _is_fresh(cache, ttl_hours=12):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)

    # 날짜 목록 생성
    s = datetime.strptime(start, "%Y%m%d")
    e = datetime.strptime(end, "%Y%m%d")
    dates = []
    cur = s
    while cur <= e:
        if cur.weekday() < 5:  # 평일만
            dates.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)

    if not dates:
        return []

    # 쿠키 한 번만 획득
    logger.info("KRX 쿠키 획득 중...")
    try:
        cookies = _get_krx_cookies()
    except Exception as e:
        logger.error("Playwright 오류: %s", e)
        return []

    results = []
    failed = 0
    for d in dates:
        row = fetch_one_day(code, d, cookies=cookies)
        if row:
            results.append(row)
        else:
            failed += 1
            # 연속 5일 실패 = 세션 만료로 간주, 쿠키 재획득
            if failed >= 5:
                logger.info("세션 만료 감지, 쿠키 재획득...")
                try:
                    cookies = _get_krx_cookies()
                    failed = 0
                except Exception as ex:
                    logger.error("쿠키 재획득 실패: %s", ex)
                    break
        time.sleep(delay)

    if results:
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False)

    return results


# ─────────────────────────────────────────────
# 분석 헬퍼
# ─────────────────────────────────────────────

def summarize_by_phase(rows: list[dict], start_date: str, end_date: str) -> dict:
    """기간 내 누적 순매수 (억 원) + 지배 주체 식별.

    Returns:
        {
            "금융투자": -120.5,
            "사모": 45.2,
            ...
            "dominant": "외국인",   # 가장 많이 순매수한 주체
            "top3": [("외국인", 320.1), ("연기금등", 80.0), ("사모", 45.2)]
        }
    """
    totals = {cat: 0 for cat in INVESTOR_CATEGORIES}

    for row in rows:
        if not (start_date <= row["date"].replace("-", "") <= end_date):
            continue
        for cat in INVESTOR_CATEGORIES:
            totals[cat] += row.get(cat, 0)

    # 억 원 단위 (KRX 단위 = 억 원)
    sorted_cats = sorted(totals.items(), key=lambda x: x[1], reverse=True)
    dominant = sorted_cats[0][0] if sorted_cats else None
    top3 = [(k, round(v / 1e8, 1)) if abs(v) > 1e8 else (k, round(v, 1))
            for k, v in sorted_cats[:3]]

    result = {cat: round(v / 1e8, 1) if abs(v) > 1e8 else round(v, 1)
              for cat, v in totals.items()}
    result["dominant"]  = dominant
    result["top3"]      = top3
    return result


# ─────────────────────────────────────────────
# CLI 테스트
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    code  = sys.argv[1] if len(sys.argv) > 1 else "010170"
    start = sys.argv[2] if len(sys.argv) > 2 else "20250101"
    end   = sys.argv[3] if len(sys.argv) > 3 else datetime.today().strftime("%Y%m%d")

    print(f"\n[{code}] {start} ~ {end} KRX 9분류 투자자 조회")
    rows = fetch_range(code, start, end)
    print(f"  → {len(rows)}일 수집")

    if rows:
        summary = summarize_by_phase(rows, start, end)
        print("\n[누적 순매수, 억 원]")
        for cat in INVESTOR_CATEGORIES:
            val = summary[cat]
            bar = "▲" if val > 0 else "▽"
            print(f"  {cat:8s}  {bar} {val:+.1f}")
        print(f"\n  지배 주체: {summary['dominant']}")
        print(f"  Top3: {summary['top3']}")
