#!/usr/bin/env python3
"""Fetch news for held stocks via Google News RSS.

Reads transactions.json → fetches Google News RSS per stock → saves raw articles to stock_news_raw.json.
Summarization is handled separately by Claude Code agent (summarize_stock_news.py).
"""
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import quote

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

from config import TRANSACTIONS_FILE, STOCK_NEWS_RAW_FILE as OUTPUT_FILE, MAX_NEWS_ARTICLES as MAX_ARTICLES, NEWS_LOOKBACK_DAYS as NEWS_DAYS
from file_io import load_json, save_json, now_kst
from http_client import http_get


def get_held_stocks():
    txs = load_json(TRANSACTIONS_FILE, default=[])
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
        content = http_get(url)
        if content is None:
            return []
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

    result = {"fetched_at": now_kst(), "stocks": {}}
    for stock in stocks:
        print(f"  [{stock}] ...", end=" ", flush=True)
        articles = fetch_google_news(stock)
        result["stocks"][stock] = articles
        print(f"{len(articles)}건")
        time.sleep(0.3)

    save_json(OUTPUT_FILE, result)

    has_news = sum(1 for arts in result["stocks"].values() if arts)
    print(f"\n완료: {has_news}/{len(stocks)}종목 뉴스 있음 → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
