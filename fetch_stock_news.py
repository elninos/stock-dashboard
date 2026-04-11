#!/usr/bin/env python3
"""Fetch news for held stocks via Google News RSS.

Reads transactions.json → fetches Google News RSS per stock → saves raw articles to stock_news_raw.json.
Summarization is handled separately by Claude Code agent (summarize_stock_news.py).
"""
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import quote
from urllib.request import Request, urlopen

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRANSACTIONS_FILE = os.path.join(BASE_DIR, "transactions.json")
OUTPUT_FILE = os.path.join(BASE_DIR, "stock_news_raw.json")

MAX_ARTICLES = 8
NEWS_DAYS = 7


def get_held_stocks():
    with open(TRANSACTIONS_FILE, encoding="utf-8") as f:
        txs = json.load(f)
    holdings = defaultdict(int)
    for tx in txs:
        stock = tx.get("stock", "")
        if not stock:
            continue
        t = tx["type"]
        q = tx.get("qty", 0)
        if t == "buy":            holdings[stock] += q
        elif t == "sell":         holdings[stock] -= q
        elif t == "transfer_in":  holdings[stock] += q
        elif t == "transfer_out": holdings[stock] -= q
    return [s for s, q in sorted(holdings.items()) if q > 0]


def fetch_google_news(stock_name: str) -> list[dict]:
    url = f"https://news.google.com/rss/search?q={quote(stock_name)}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as resp:
            content = resp.read()
        root = ET.fromstring(content)
        cutoff = datetime.now() - timedelta(days=NEWS_DAYS)
        articles = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            pub_str = item.findtext("pubDate", "")
            desc = re.sub(r"<[^>]+>", "", item.findtext("description", "")).strip()
            source_el = item.find("source")
            source = source_el.text.strip() if source_el is not None and source_el.text else ""
            try:
                pub_dt = datetime.strptime(pub_str[:25], "%a, %d %b %Y %H:%M:%S")
                if pub_dt < cutoff:
                    continue
                date_str = pub_dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                date_str = pub_str[:10] if pub_str else ""
            if not title:
                continue
            articles.append({
                "title": title,
                "link": link,
                "date": date_str,
                "source": source,
                "snippet": desc[:400],
            })
            if len(articles) >= MAX_ARTICLES:
                break
        return articles
    except Exception as e:
        print(f"    error: {e}")
        return []


def main():
    stocks = get_held_stocks()
    print(f"보유 종목 {len(stocks)}개 뉴스 수집 시작...")

    result = {"fetched_at": datetime.now().isoformat(), "stocks": {}}
    for stock in stocks:
        print(f"  [{stock}] ...", end=" ", flush=True)
        articles = fetch_google_news(stock)
        result["stocks"][stock] = articles
        print(f"{len(articles)}건")
        time.sleep(0.3)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    has_news = sum(1 for arts in result["stocks"].values() if arts)
    print(f"\n완료: {has_news}/{len(stocks)}종목 뉴스 있음 → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
