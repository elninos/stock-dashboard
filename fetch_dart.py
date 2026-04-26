#!/usr/bin/env python3
"""DART OpenAPI 공시 수집기.

보유 종목 + 주요사항보고서(시장 전반)를 수집해 briefing.json 포맷으로 반환.
Corp code ZIP은 로컬에 캐시 (weekly refresh).
"""

import io
import json
import os
import re
import time
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DART_API_KEY  = "95a83c9efdb1e3ce13be539270823fa31aafdad5"
DART_BASE     = "https://opendart.fss.or.kr/api"
CORP_CODE_ZIP = os.path.join(BASE_DIR, "dart_corp_codes.zip")
CORP_CODE_JSON= os.path.join(BASE_DIR, "dart_corp_codes.json")

# 수집할 공시 유형 코드 → 한글 레이블
PBLNTF_TYPES = {
    "B": "주요사항보고",   # CB, 유증, 자사주, 합병 등 주가 영향 큰 것
    "D": "지분공시",       # 5%룰, 임원 지분 변동
    "A": "정기공시",       # 분기/반기/사업보고서
}

# 종목명 → DART corp_code 수동 보정 (자동 매칭 실패 시 사용)
MANUAL_OVERRIDES = {
    "NAVER": "035420",    # NAVER 주식코드
    "SK하이닉스": "000660",
    "셀트리온": "068270",
    "보로노이": "310210",
    "파마리서치": "214450",
    "이오테크닉스": "039030",
    "자화전자": "033240",
    "코오롱티슈진": "950160",
    "파크시스템스": "140860",
    "필옵틱스": "161580",
    "리노공업": "058470",
    "오스코텍": "039200",
    "에이프로젠": "007460",
    "에이피알": "278470",
    "코아스템켐온": "166480",
    "콜마비앤에이치": "200130",
    "대한광통신": "010170",
    "두산": "000150",
    "우성아이비": "006340",
}


def _http_get(url: str, params: dict = None, timeout: int = 15):
    """Simple HTTP GET using urllib (no extra deps)."""
    import urllib.request
    import urllib.parse
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        print(f"  [DART HTTP ERROR] {e}")
        return None


def _load_corp_codes() -> dict:
    """Load name→stock_code + name→corp_code mappings, refreshing weekly."""
    # Refresh if cache older than 7 days
    need_refresh = True
    if os.path.exists(CORP_CODE_JSON):
        age = time.time() - os.path.getmtime(CORP_CODE_JSON)
        if age < 7 * 86400:
            need_refresh = False

    if not need_refresh:
        with open(CORP_CODE_JSON, encoding="utf-8") as f:
            return json.load(f)

    print("  [DART] Downloading corp code list...")
    data = _http_get(f"{DART_BASE}/corpCode.xml", {"crtfc_key": DART_API_KEY})
    if not data:
        # Fallback to existing cache if any
        if os.path.exists(CORP_CODE_JSON):
            with open(CORP_CODE_JSON, encoding="utf-8") as f:
                return json.load(f)
        return {}

    # Save ZIP
    with open(CORP_CODE_ZIP, "wb") as f:
        f.write(data)

    # Parse XML inside ZIP
    mapping = {}   # corp_name → {"corp_code": ..., "stock_code": ...}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml_name = [n for n in zf.namelist() if n.endswith(".xml")][0]
            xml_data = zf.read(xml_name)
        root = ET.fromstring(xml_data)
        for item in root.findall(".//list"):
            corp_code  = item.findtext("corp_code", "").strip()
            corp_name  = item.findtext("corp_name", "").strip()
            stock_code = item.findtext("stock_code", "").strip()
            if corp_name:
                mapping[corp_name] = {"corp_code": corp_code, "stock_code": stock_code}
    except Exception as e:
        print(f"  [DART] XML parse error: {e}")

    with open(CORP_CODE_JSON, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False)

    print(f"  [DART] Corp codes cached: {len(mapping)} companies")
    return mapping


def _resolve_corp_code(name: str, mapping: dict):
    """Try to find DART corp_code for a given stock name."""
    # 1. Manual override by name
    stock_code = MANUAL_OVERRIDES.get(name)
    if stock_code:
        # Find corp_code by stock_code
        for v in mapping.values():
            if v.get("stock_code") == stock_code:
                return v["corp_code"]

    # 2. Exact name match
    if name in mapping:
        return mapping[name]["corp_code"]

    # 3. Partial match (name contained in corp_name)
    for corp_name, v in mapping.items():
        if name in corp_name or corp_name in name:
            if v.get("stock_code"):  # only listed stocks
                return v["corp_code"]

    return None


def _fetch_disclosures(corp_code, bgn_de: str, pblntf_ty: str,
                       corp_cls: str = "") -> list[dict]:
    """Fetch disclosure list from DART API."""
    params = {
        "crtfc_key": DART_API_KEY,
        "bgn_de": bgn_de,
        "pblntf_ty": pblntf_ty,
        "page_count": 20,
    }
    if corp_code:
        params["corp_code"] = corp_code
    if corp_cls:
        params["corp_cls"] = corp_cls

    data = _http_get(f"{DART_BASE}/list.json", params)
    if not data:
        return []

    try:
        result = json.loads(data)
    except Exception:
        return []

    if result.get("status") != "000":
        return []

    return result.get("list", [])


def fetch_dart_posts(held_stocks, lookback_days: int = 7) -> list[dict]:
    """
    Fetch DART disclosures and return as list of post dicts compatible
    with briefing.json format.

    held_stocks: list of stock names currently held.
    """
    mapping = _load_corp_codes()
    bgn_de = (date.today() - timedelta(days=lookback_days)).strftime("%Y%m%d")
    posts = []

    # ── 1. 보유 종목별 공시 (전 유형) ──────────────────────────────────
    resolved = {}  # stock_name → corp_code
    for name in held_stocks:
        cc = _resolve_corp_code(name, mapping)
        if cc:
            resolved[name] = cc

    cc_to_name = {v: k for k, v in resolved.items()}

    if resolved:
        print(f"  [DART] Fetching disclosures for {len(resolved)} held stocks...")
        seen_rcept = set()
        for pty, pty_label in PBLNTF_TYPES.items():
            for sname, cc in resolved.items():
                items = _fetch_disclosures(cc, bgn_de, pty)
                time.sleep(0.15)
                for item in items:
                    rcept_no = item.get("rcept_no", "")
                    if rcept_no in seen_rcept:
                        continue
                    seen_rcept.add(rcept_no)
                    posts.append(_item_to_post(item, pty_label, held_tag=sname))

    # ── 2. 시장 전반 주요사항보고 (KOSPI+KOSDAQ, 보유 외 종목 포함) ──────
    print("  [DART] Fetching market-wide 주요사항보고...")
    market_seen = {p["_rcept_no"] for p in posts if "_rcept_no" in p}
    for cls in ["Y", "K"]:
        items = _fetch_disclosures(None, bgn_de, "B", corp_cls=cls)
        time.sleep(0.15)
        for item in items[:30]:  # top 30 per market
            rcept_no = item.get("rcept_no", "")
            if rcept_no in market_seen:
                continue
            market_seen.add(rcept_no)
            posts.append(_item_to_post(item, "주요사항보고"))

    # Sort by date desc
    posts.sort(key=lambda x: (x["date"], x["time"]), reverse=True)
    return posts


def _item_to_post(item: dict, pblntf_label: str, held_tag: str = "") -> dict:
    """Convert a DART disclosure item to briefing post format."""
    rcept_dt = item.get("rcept_dt", "")  # YYYYMMDD
    if len(rcept_dt) == 8:
        post_date = f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:]}"
    else:
        post_date = date.today().isoformat()

    corp_name    = item.get("corp_name", "")
    report_nm    = item.get("report_nm", "")
    rcept_no     = item.get("rcept_no", "")
    flr_nm       = item.get("flr_nm", "")   # 제출인 (공시자)
    rm           = item.get("rm", "")        # 비고 (유/코 등)

    dart_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}" if rcept_no else ""

    tag = f"[보유] " if held_tag else ""
    text = f"{tag}[{corp_name}] {report_nm}"
    if flr_nm and flr_nm != corp_name:
        text += f" (제출: {flr_nm})"
    text += f"\n공시유형: {pblntf_label}"
    if rm:
        text += f"  ·  {rm}"

    return {
        "date": post_date,
        "time": "00:00",
        "text": text,
        "links": [dart_url] if dart_url else [],
        "post_url": dart_url,
        "_rcept_no": rcept_no,       # internal dedup key (stripped before saving)
        "_corp_name": corp_name,
        "_held": bool(held_tag),
    }


if __name__ == "__main__":
    # Quick test
    test_stocks = ["SK하이닉스", "셀트리온", "파마리서치", "보로노이"]
    posts = fetch_dart_posts(test_stocks, lookback_days=7)
    print(f"\n총 {len(posts)}건 공시 수집")
    for p in posts[:10]:
        held = "★" if p.get("_held") else " "
        print(f"  {held} [{p['date']}] {p['text'][:80]}")
