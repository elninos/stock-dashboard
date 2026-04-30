"""네이버 금융 — 외국인/기관 일별 매매 스크래핑.

KIS API 30일 한계 + pykrx 깨짐 우회.
페이지당 약 20영업일 → 10페이지 = 200일 (~1년치) 가능.

URL: https://finance.naver.com/item/frgn.naver?code={code}&page={N}

반환 컬럼:
  date, close, change_pct, volume,
  inst_net (기관 순매매량 — 주식수),
  foreign_net (외국인 순매매량 — 주식수),
  foreign_holding (외국인 보유주수),
  foreign_pct (외국인 보유율)
"""
import os, json, time, re
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
import pandas as pd

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "naver_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def _parse_int(s: str) -> int:
    if not s: return 0
    s = s.replace(",", "").strip()
    # 부호 처리 (+/-)
    if s.startswith("+"): s = s[1:]
    try: return int(s)
    except: return 0


def _parse_pct(s: str) -> float:
    if not s: return 0.0
    s = s.replace("%", "").replace(",", "").strip()
    if s.startswith("+"): s = s[1:]
    try: return float(s)
    except: return 0.0


def fetch_naver_flow_page(code: str, page: int = 1) -> list:
    """단일 페이지 스크래핑."""
    url = f"https://finance.naver.com/item/frgn.naver?code={code}&page={page}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    tables = soup.find_all("table")
    # Naver 페이지 구조 변경 대응 — "날짜 ... 외국인" 헤더가 있는 테이블 자동 탐색
    target = None
    for t in tables:
        head = t.get_text(" ", strip=True)[:60]
        if "날짜" in head and "외국인" in head:
            target = t; break
    if target is None:
        return []
    rows = target.find_all("tr")
    out = []
    for row in rows:
        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cells) < 9: continue
        date_str = cells[0]
        if not re.match(r"^\d{4}\.\d{2}\.\d{2}$", date_str): continue
        # 컬럼:
        # 날짜 | 종가 | 전일비 | 등락률 | 거래량 | 기관 순매매 | 외국인 순매매 | 외국인 보유주수 | 외국인 보유율
        out.append({
            "date":            date_str.replace(".", "-"),
            "close":           _parse_int(cells[1]),
            "change_pct":      _parse_pct(cells[3]),
            "volume":          _parse_int(cells[4]),
            "inst_net":        _parse_int(cells[5]),       # 기관 순매매량 (주식수)
            "foreign_net":     _parse_int(cells[6]),       # 외국인 순매매량 (주식수)
            "foreign_holding": _parse_int(cells[7]),       # 외국인 보유주수
            "foreign_pct":     _parse_pct(cells[8]),       # 외국인 보유율
        })
    return out


def fetch_naver_flow(code: str, max_pages: int = 10, use_cache: bool = True,
                      cache_ttl_min: int = 60) -> pd.DataFrame:
    """일별 매매 N페이지 합쳐서 DataFrame 반환."""
    cache_file = os.path.join(CACHE_DIR, f"{code}_flow.json")
    if use_cache and os.path.exists(cache_file):
        age_min = (datetime.now().timestamp() - os.path.getmtime(cache_file)) / 60
        if age_min < cache_ttl_min:
            try:
                with open(cache_file, "r") as f:
                    cached = json.load(f)
                df = pd.DataFrame(cached["data"])
                if len(df) > 0:
                    df["date"] = pd.to_datetime(df["date"])
                    return df.set_index("date").sort_index()
            except Exception:
                pass

    all_rows = []
    for p in range(1, max_pages + 1):
        rows = fetch_naver_flow_page(code, p)
        if not rows:
            break
        all_rows.extend(rows)
        time.sleep(0.3)  # rate limit

    # 캐시 저장
    try:
        with open(cache_file, "w") as f:
            json.dump({"saved_at": datetime.now().isoformat(), "data": all_rows}, f)
    except Exception:
        pass

    df = pd.DataFrame(all_rows)
    if len(df) == 0:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset=["date"]).sort_values("date").set_index("date")
    return df


def analyze_flow(code: str, lookback_days: int = 60) -> dict:
    """기간 누적 + 시그널 추출."""
    df = fetch_naver_flow(code, max_pages=8)
    if len(df) == 0:
        return {"available": False, "error": "데이터 없음"}

    df = df.tail(lookback_days)
    if len(df) < 5:
        return {"available": False, "error": "데이터 부족"}

    # 누적 (주식수 → 백만원 환산은 불가, 주식수 그대로)
    last5 = df.tail(5); last10 = df.tail(10); last20 = df.tail(20)

    # 외국인 보유율 변화
    foreign_pct_5d = df["foreign_pct"].iloc[-1] - df["foreign_pct"].iloc[-6] if len(df) >= 6 else 0
    foreign_pct_20d = df["foreign_pct"].iloc[-1] - df["foreign_pct"].iloc[-21] if len(df) >= 21 else 0

    # 거래량 가중 매매가치 추정
    avg_close = df["close"].mean()
    inst_amt_5d = last5["inst_net"].sum() * avg_close / 1e8  # 억원
    inst_amt_20d = last20["inst_net"].sum() * avg_close / 1e8
    foreign_amt_5d = last5["foreign_net"].sum() * avg_close / 1e8
    foreign_amt_20d = last20["foreign_net"].sum() * avg_close / 1e8

    return {
        "available": True,
        "n_days": len(df),
        "last_date": df.index[-1].strftime("%Y-%m-%d"),
        "last_close": int(df["close"].iloc[-1]),
        "foreign_pct_now": float(df["foreign_pct"].iloc[-1]),
        "foreign_pct_5d_chg": float(foreign_pct_5d),
        "foreign_pct_20d_chg": float(foreign_pct_20d),
        "inst_net_5d": int(last5["inst_net"].sum()),
        "inst_net_10d": int(last10["inst_net"].sum()),
        "inst_net_20d": int(last20["inst_net"].sum()),
        "foreign_net_5d": int(last5["foreign_net"].sum()),
        "foreign_net_10d": int(last10["foreign_net"].sum()),
        "foreign_net_20d": int(last20["foreign_net"].sum()),
        "inst_amt_5d_estim": float(inst_amt_5d),
        "inst_amt_20d_estim": float(inst_amt_20d),
        "foreign_amt_5d_estim": float(foreign_amt_5d),
        "foreign_amt_20d_estim": float(foreign_amt_20d),
        "data": df.reset_index().to_dict(orient="records"),
    }


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "214450"
    print(f"네이버 금융 외인/기관 일별 매매 — {code}")
    df = fetch_naver_flow(code, max_pages=10, use_cache=False)
    print(f"\n수집 {len(df)}일 ({df.index.min().strftime('%Y-%m-%d')} ~ {df.index.max().strftime('%Y-%m-%d')})")
    print(df.tail(10))
