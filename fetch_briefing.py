#!/usr/bin/env python3
"""Fetch market briefing from telegram channels and blogs listed in sources.json."""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, date
from html.parser import HTMLParser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.path.join(BASE_DIR, "sources.json")
BRIEFING_FILE = os.path.join(BASE_DIR, "briefing.json")

TODAY = date.today().isoformat()


# ── Telegram regex parser ─────────────────────────────────────────────

def _strip_html(html_str: str) -> str:
    """Remove HTML tags, convert <br> to newlines."""
    text = re.sub(r"<br\s*/?>", "\n", html_str)
    text = re.sub(r"<[^>]+>", "", text)
    from html import unescape
    return unescape(text).strip()


def _extract_links(html_str: str) -> list[str]:
    """Extract href values from anchor tags."""
    return re.findall(r'href="(https?://[^"]+)"', html_str)


def fetch_telegram_posts(url: str, channel_id: str = "") -> list[dict]:
    """Fetch posts from a telegram channel preview page using regex."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [ERROR] Failed to fetch {url}: {e}")
        return []

    results = []

    # Split by message widget boundaries
    # Each post has data-post="channel/number"
    post_blocks = re.split(r'data-post="', html)[1:]  # skip first chunk (before any post)

    for block in post_blocks:
        # Extract post ID
        post_id_match = re.match(r'([^"]+)"', block)
        if not post_id_match:
            continue
        post_id = post_id_match.group(1)  # e.g. "bumgore/54106"

        # Extract datetime
        dt_match = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        post_date = ""
        post_time = ""
        if dt_match:
            try:
                dt = datetime.fromisoformat(dt_match.group(1).replace("Z", "+00:00"))
                # Convert to KST (UTC+9)
                from datetime import timezone, timedelta
                kst = timezone(timedelta(hours=9))
                dt_kst = dt.astimezone(kst)
                post_date = dt_kst.strftime("%Y-%m-%d")
                post_time = dt_kst.strftime("%H:%M")
            except Exception:
                pass

        # Extract message text
        text_match = re.search(
            r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            block, re.DOTALL
        )
        if not text_match:
            continue

        raw_html = text_match.group(1)
        text = _strip_html(raw_html)
        links = _extract_links(raw_html)

        # Also extract link preview URLs
        preview_links = re.findall(
            r'class="tgme_widget_message_link_preview"[^>]*href="(https?://[^"]+)"',
            block
        )
        links = list(dict.fromkeys(links + preview_links))  # dedupe preserving order

        if len(text) < 10:
            continue

        # Build telegram post URL
        post_url = f"https://t.me/{post_id}" if post_id else ""

        results.append({
            "date": post_date,
            "time": post_time,
            "text": text[:2000],
            "links": links,
            "post_url": post_url,
        })

    return results


def fetch_naver_rss(rss_url: str) -> list[dict]:
    """Fetch posts from a Naver blog RSS feed."""
    req = urllib.request.Request(rss_url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [ERROR] Failed to fetch RSS {rss_url}: {e}")
        return []

    from email.utils import parsedate_to_datetime
    results = []

    # Extract each <item>
    items = re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)
    for item in items:
        # Title
        title_match = re.search(r"<title><!\[CDATA\[(.*?)\]\]></title>", item, re.DOTALL)
        title = title_match.group(1).strip() if title_match else ""

        # Link
        link_match = re.search(r"<link><!\[CDATA\[(.*?)\]\]></link>", item, re.DOTALL)
        link = link_match.group(1).strip() if link_match else ""
        # Clean tracking params for display
        link_clean = re.sub(r"\?fromRss=.*", "", link)

        # Description (truncated content)
        desc_match = re.search(r"<description><!\[CDATA\[(.*?)\]\]></description>", item, re.DOTALL)
        desc_raw = desc_match.group(1) if desc_match else ""
        # Strip img tags and other HTML
        desc = re.sub(r"<img[^>]+/>", "", desc_raw)
        desc = _strip_html(desc).strip()

        # Tags
        tag_match = re.search(r"<tag><!\[CDATA\[(.*?)\]\]></tag>", item, re.DOTALL)
        tags = tag_match.group(1).strip() if tag_match else ""

        # Date
        date_match = re.search(r"<pubDate>(.*?)</pubDate>", item, re.DOTALL)
        post_date = ""
        post_time = ""
        if date_match:
            try:
                from datetime import timezone, timedelta
                dt = parsedate_to_datetime(date_match.group(1).strip())
                kst = timezone(timedelta(hours=9))
                dt_kst = dt.astimezone(kst)
                post_date = dt_kst.strftime("%Y-%m-%d")
                post_time = dt_kst.strftime("%H:%M")
            except Exception:
                pass

        if not title or not desc:
            continue

        # Compose text: title + content
        full_text = f"[{title}]\n\n{desc}"
        if tags:
            full_text += f"\n\n#태그: {tags}"

        results.append({
            "date": post_date,
            "time": post_time,
            "text": full_text[:3000],
            "links": [link_clean] if link_clean else [],
            "post_url": link_clean,
        })

    return results


def fetch_blog_posts(url: str) -> list[dict]:
    """Fetch posts from a blog via RSS (Naver blog supported)."""
    # Detect Naver blog and use RSS
    naver_match = re.search(r"blog\.naver\.com/([^/?#]+)", url)
    if naver_match:
        blog_id = naver_match.group(1)
        rss_url = f"https://rss.blog.naver.com/{blog_id}"
        return fetch_naver_rss(rss_url)
    return []


# ── Main ──────────────────────────────────────────────────────────────

def main():
    target_date = sys.argv[1] if len(sys.argv) > 1 else TODAY

    with open(SOURCES_FILE, encoding="utf-8") as f:
        sources = json.load(f)

    # Load existing briefings
    if os.path.exists(BRIEFING_FILE):
        with open(BRIEFING_FILE, encoding="utf-8") as f:
            briefings = json.load(f)
    else:
        briefings = {}

    day_data = {"fetched_at": datetime.now().isoformat(), "sources": []}

    # Telegram channels
    for src in sources.get("telegram", []):
        if not src.get("enabled", True):
            continue
        print(f"Fetching telegram: {src['name']} ({src['id']})")
        posts = fetch_telegram_posts(src["url"], src["id"])
        print(f"  → {len(posts)} posts found")

        day_data["sources"].append({
            "type": "telegram",
            "name": src["name"],
            "id": src["id"],
            "category": src.get("category", ""),
            "channel_url": f"https://t.me/{src['id']}",
            "posts": posts,
        })

    # Blogs
    for src in sources.get("blog", []):
        if not src.get("enabled", True):
            continue
        print(f"Fetching blog: {src['name']}")
        posts = fetch_blog_posts(src["url"])
        print(f"  → {len(posts)} posts found")

        day_data["sources"].append({
            "type": "blog",
            "name": src["name"],
            "url": src.get("url", ""),
            "category": src.get("category", ""),
            "posts": posts,
        })

    briefings[target_date] = day_data

    # Keep last 30 days only
    sorted_dates = sorted(briefings.keys(), reverse=True)[:30]
    briefings = {d: briefings[d] for d in sorted_dates}

    with open(BRIEFING_FILE, "w", encoding="utf-8") as f:
        json.dump(briefings, f, ensure_ascii=False, indent=2)

    total_posts = sum(len(s["posts"]) for s in day_data["sources"])
    print(f"\nSaved {total_posts} posts from {len(day_data['sources'])} sources to briefing.json ({target_date})")


if __name__ == "__main__":
    main()
