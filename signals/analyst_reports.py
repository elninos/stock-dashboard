"""네이버 금융 — 종목별 애널리스트 리포트 스크래핑.

URL: https://finance.naver.com/research/company_list.naver?searchType=itemCode&itemCode={code}&page={N}

활용:
  - 리포트 발행 빈도 timeline (커버리지 변화)
  - 새 증권사 커버리지 추가 시점 (관심 증가 시그널)
  - 목표가 변화 (있으면)
  - 발행 증권사 다양성
"""
import os, json, time, re
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
import pandas as pd

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "research_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def fetch_research_page(code: str, page: int = 1) -> list:
    url = (f"https://finance.naver.com/research/company_list.naver"
           f"?searchType=itemCode&itemCode={code}&page={page}")
    r = requests.get(url, headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table", class_="type_1")
    if not table: return []

    out = []
    for row in table.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cells) < 5: continue
        # [종목명, 제목, 증권사, 목표가(빈칸), 날짜, 조회수]
        # 또는 PDF 링크 있는 row 등 다양
        title_link = row.find("a", href=re.compile(r"company_read"))
        if not title_link: continue
        if len(cells) >= 5:
            stock = cells[0]
            title = cells[1]
            broker = cells[2]
            target_price = cells[3] if len(cells) > 3 else ""
            date_str = cells[4] if len(cells) > 4 else cells[3]
            views = cells[5] if len(cells) > 5 else ""
            # 날짜 파싱 (26.04.17 → 2026-04-17)
            m = re.match(r"(\d{2})\.(\d{2})\.(\d{2})", date_str)
            if m:
                yr = int(m.group(1))
                yr = 2000 + yr if yr < 50 else 1900 + yr
                date_iso = f"{yr}-{m.group(2)}-{m.group(3)}"
            else:
                date_iso = date_str
            out.append({
                "stock": stock, "title": title, "broker": broker,
                "target_price": target_price, "date": date_iso,
                "views": views,
            })
    return out


def fetch_research(code: str, max_pages: int = 5, use_cache: bool = True,
                    cache_ttl_min: int = 720) -> pd.DataFrame:
    cache_file = os.path.join(CACHE_DIR, f"{code}_research.json")
    if use_cache and os.path.exists(cache_file):
        age_min = (datetime.now().timestamp() - os.path.getmtime(cache_file)) / 60
        if age_min < cache_ttl_min:
            try:
                with open(cache_file, "r") as f:
                    cached = json.load(f)
                df = pd.DataFrame(cached["data"])
                if len(df) > 0:
                    df["date"] = pd.to_datetime(df["date"], errors="coerce")
                return df.dropna(subset=["date"]).sort_values("date")
            except Exception: pass

    all_rows = []
    for p in range(1, max_pages + 1):
        rows = fetch_research_page(code, p)
        if not rows: break
        all_rows.extend(rows)
        time.sleep(0.4)

    try:
        with open(cache_file, "w") as f:
            json.dump({"saved_at": datetime.now().isoformat(), "data": all_rows}, f, ensure_ascii=False)
    except Exception: pass

    df = pd.DataFrame(all_rows)
    if len(df) == 0: return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.dropna(subset=["date"]).sort_values("date")


def analyze_coverage(code: str) -> dict:
    df = fetch_research(code, max_pages=10)
    if len(df) == 0:
        return {"available": False}

    df_recent = df[df["date"] >= datetime.now() - timedelta(days=730)]
    by_broker = df_recent["broker"].value_counts()
    n_brokers = len(by_broker)

    # 분기별 발행 수
    df_recent["quarter"] = df_recent["date"].dt.to_period("Q")
    by_q = df_recent.groupby("quarter").size()

    # 새 증권사 첫 등장 (60일 이내)
    cutoff = datetime.now() - timedelta(days=60)
    recent = df[df["date"] >= cutoff]
    older = df[df["date"] < cutoff]
    older_brokers = set(older["broker"].unique())
    new_brokers = set(recent["broker"].unique()) - older_brokers

    return {
        "available": True,
        "total_reports": len(df),
        "n_brokers": n_brokers,
        "brokers": by_broker.to_dict(),
        "by_quarter": {str(k): int(v) for k, v in by_q.items()},
        "new_brokers_60d": list(new_brokers),
        "data": df,
    }


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "214450"
    r = analyze_coverage(code)
    if not r["available"]:
        print("데이터 없음"); exit()
    print(f"\n종목 {code} — 애널리스트 커버리지")
    print(f"  총 리포트: {r['total_reports']}건")
    print(f"  커버 증권사: {r['n_brokers']}개")
    print(f"  최근 60일 새 증권사: {r['new_brokers_60d']}")
    print(f"\n  분기별 발행:")
    for q, n in r["by_quarter"].items():
        bar = "█" * n
        print(f"    {q}: {n:>3}건 {bar}")
    print(f"\n  증권사별 (TOP 10):")
    for b, n in list(r["brokers"].items())[:10]:
        print(f"    {b:<14} {n:>3}건")
