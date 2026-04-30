"""네이버 종목 뉴스 historical 스크래핑.

API: https://api.stock.naver.com/news/stock/{code}?pageSize={N}&page={P}

응답 구조:
  [{total, items: [{id, officeName, datetime, title, body, mobileNewsUrl}]}]

페이지당 최대 50개 클러스터. 페이지 100+까지 (수년치).
"""
import os, json, time
from datetime import datetime, timedelta
import requests
import pandas as pd

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "news_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def fetch_news_page(code: str, page: int = 1, page_size: int = 50) -> list:
    url = f"https://api.stock.naver.com/news/stock/{code}?pageSize={page_size}&page={page}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    if r.status_code != 200: return []
    data = r.json()
    out = []
    for cluster in data:
        total = cluster.get("total", 1)
        for item in cluster.get("items", []):
            dt = item.get("datetime", "")
            try:
                date = datetime.strptime(dt[:8], "%Y%m%d")
            except: continue
            out.append({
                "date": date,
                "datetime_str": dt,
                "title": item.get("title", ""),
                "body": item.get("body", "")[:200],
                "office": item.get("officeName", ""),
                "url": item.get("mobileNewsUrl", ""),
                "cluster_size": total,
            })
    return out


def fetch_news(code: str, max_pages: int = 100, start_date=None,
                use_cache: bool = True, cache_ttl_min: int = 720) -> pd.DataFrame:
    """N페이지까지 뉴스 수집. start_date 이전이면 멈춤."""
    cache_file = os.path.join(CACHE_DIR, f"{code}_news.json")
    if use_cache and os.path.exists(cache_file):
        age_min = (datetime.now().timestamp() - os.path.getmtime(cache_file)) / 60
        if age_min < cache_ttl_min:
            try:
                with open(cache_file, "r") as f:
                    cached = json.load(f)
                df = pd.DataFrame(cached["data"])
                if len(df) > 0:
                    df["date"] = pd.to_datetime(df["date"])
                return df.sort_values("date")
            except: pass

    if start_date is None:
        start_date = datetime.now() - timedelta(days=730)
    elif isinstance(start_date, str):
        start_date = datetime.strptime(start_date, "%Y-%m-%d")

    all_rows = []
    for p in range(1, max_pages + 1):
        rows = fetch_news_page(code, p)
        if not rows: break
        # 가장 오래된 날짜
        oldest = min(r["date"] for r in rows)
        all_rows.extend(rows)
        if oldest < start_date:
            break
        time.sleep(0.2)

    # 중복 제거
    seen = set()
    unique = []
    for r in all_rows:
        key = (r["datetime_str"], r["title"])
        if key in seen: continue
        seen.add(key)
        unique.append(r)

    # 캐시
    try:
        cache_data = [{**r, "date": r["date"].isoformat()} for r in unique]
        with open(cache_file, "w") as f:
            json.dump({"saved_at": datetime.now().isoformat(), "data": cache_data}, f, ensure_ascii=False)
    except: pass

    df = pd.DataFrame(unique)
    if len(df) == 0: return df
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date")


def analyze_news_timeline(code: str, peak_date: str = None) -> dict:
    df = fetch_news(code, max_pages=100, use_cache=False)
    if len(df) == 0:
        return {"available": False}

    return {
        "available": True,
        "total_news": len(df),
        "n_offices": df["office"].nunique(),
        "earliest": df["date"].min(),
        "latest": df["date"].max(),
        "data": df,
    }


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "214450"
    print(f"종목 {code} 뉴스 수집 중...")
    df = fetch_news(code, max_pages=100, use_cache=False,
                     start_date=datetime.now() - timedelta(days=730))
    print(f"총 {len(df)}건 수집 ({df['date'].min().strftime('%Y-%m-%d')} ~ {df['date'].max().strftime('%Y-%m-%d')})")
    print(f"\n월별 빈도:")
    monthly = df.groupby(df["date"].dt.to_period("M")).size()
    for m, n in monthly.items():
        bar = "█" * min(n // 5, 60)
        print(f"  {m}: {n:>4}건 {bar}")
