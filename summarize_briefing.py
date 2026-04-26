#!/usr/bin/env python3
"""Summarize collected briefing posts using Claude API.

Reads briefing.json, sends posts to Claude for multi-period summaries:
- daily:    today's posts
- weekly:   last 7 days
- biweekly: last 14 days
- monthly:  last 28 days

Outputs briefing_summary.json with {daily, weekly, biweekly, monthly} keys.
"""
import json
import os
import sys
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

from config import BRIEFING_FILE, BRIEFING_SUMMARY_FILE as SUMMARY_FILE, BRIEFING_PERIODS as PERIODS, CLAUDE_MODEL
from file_io import load_api_key, load_json, save_json, now_kst

API_KEY = load_api_key(base_dir=BASE_DIR)


def collect_posts_for_period(briefings: dict, anchor_date: str, days: int) -> list[dict]:
    """Collect posts from all dates within [anchor - days + 1, anchor]."""
    anchor = date.fromisoformat(anchor_date)
    cutoff = anchor - timedelta(days=days - 1)
    result = []
    for date_str, day_data in briefings.items():
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            continue
        if cutoff <= d <= anchor:
            for src in day_data.get("sources", []):
                for post in src.get("posts", []):
                    result.append({
                        "date": post.get("date", date_str),
                        "time": post.get("time", ""),
                        "channel": src["name"],
                        "category": src.get("category", ""),
                        "text": post["text"],
                    })
    return result


def build_posts_text(posts: list[dict], max_chars_per_post: int = 1200) -> str:
    """Build combined text block, grouped by channel."""
    from collections import defaultdict
    by_channel = defaultdict(list)
    for p in posts:
        by_channel[(p["channel"], p["category"])].append(p)

    parts = []
    for (channel, category), channel_posts in by_channel.items():
        parts.append(f"\n{'='*60}\n채널: {channel} ({category})\n{'='*60}")
        for p in channel_posts:
            ts = f"[{p['date']} {p['time']}]"
            parts.append(f"\n{ts}\n{p['text'][:max_chars_per_post]}")
    return "\n".join(parts)


def summarize_with_claude(posts_text: str, period: str, anchor_date: str, days: int) -> dict:
    """Call Claude API to generate structured summary for a period."""
    import anthropic

    client = anthropic.Anthropic(api_key=API_KEY)

    if period == "daily":
        period_desc = f"{anchor_date} 당일"
        summary_instruction = "오늘의 시장 종합 요약 (3-5문장, 핵심 이슈와 분위기)"
    elif period == "weekly":
        period_desc = f"최근 7일 ({anchor_date} 기준)"
        summary_instruction = "최근 1주일 시장 흐름 종합 요약 (3-5문장, 주요 변화와 흐름)"
    elif period == "biweekly":
        period_desc = f"최근 14일 ({anchor_date} 기준)"
        summary_instruction = "최근 2주 시장 흐름 종합 요약 (3-5문장, 중기 트렌드)"
    else:
        period_desc = f"최근 28일 ({anchor_date} 기준)"
        summary_instruction = "최근 4주 시장 흐름 종합 요약 (3-5문장, 큰 그림과 방향성)"

    multi_day_fields = ""
    if period != "daily":
        multi_day_fields = """
      "days_mentioned": 3,"""

    prompt = f"""다음은 {period_desc} 기간에 수집된 여러 투자 채널(텔레그램/블로그)의 포스트들입니다.
이 내용들을 종합 분석하여 아래 JSON 형식으로 응답해주세요.

중요 규칙:
- 반드시 유효한 JSON만 출력하세요. 다른 텍스트 없이 JSON만 출력하세요.
- 모든 텍스트는 한국어로 작성하세요.
- 여러 채널에서 중복 언급되는 종목/테마를 특히 강조하세요.
- 실제 포스트 내용에 근거해서만 작성하세요. 내용이 없으면 빈 배열/빈 문자열로 두세요.

JSON 형식:
{{
  "date": "{anchor_date}",
  "period": "{period_desc}",
  "market_summary": "{summary_instruction}",
  "themes": [
    {{
      "title": "테마명",
      "summary": "테마 설명 (2-3문장)",
      "sentiment": "positive/negative/neutral",
      "related_stocks": ["종목명1", "종목명2"],
      "mentioned_in": ["채널명1", "채널명2"]{multi_day_fields}
    }}
  ],
  "stocks": [
    {{
      "name": "종목명",
      "ticker": "종목코드 (모르면 빈문자열)",
      "mention_count": 3,
      "channels": ["채널명1", "채널명2"],
      "context": "언급 맥락 요약 (1-2문장)",
      "sentiment": "positive/negative/neutral"{multi_day_fields}
    }}
  ],
  "macro": {{
    "us": "미국 시장 관련 요약 (없으면 빈문자열)",
    "kr": "한국 시장 관련 요약 (없으면 빈문자열)",
    "global": "글로벌/기타 매크로 요약 (없으면 빈문자열)"
  }},
  "key_numbers": [
    {{
      "label": "지표명",
      "value": "수치",
      "change": "변동 (예: +2.3%, 없으면 빈문자열)",
      "source": "출처 채널"
    }}
  ]
}}

stocks는 mention_count 내림차순 정렬, 복수 채널 언급 종목 우선.
themes는 중요도순 정렬.
{"key_numbers는 daily에만 의미있으므로 다른 기간은 빈 배열로." if period != "daily" else ""}

수집된 포스트:
{posts_text}"""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw)


def main():
    if not API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        sys.exit(1)

    anchor_date = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()

    briefings = load_json(BRIEFING_FILE)

    available = sorted(briefings.keys(), reverse=True)
    if not available:
        print("No briefing data available.")
        sys.exit(1)

    if anchor_date not in briefings:
        anchor_date = available[0]
        print(f"Requested date not found, using latest: {anchor_date}")

    result = {"updated_at": now_kst()}

    for period, days in PERIODS.items():
        posts = collect_posts_for_period(briefings, anchor_date, days)
        if not posts:
            print(f"[{period}] No posts found, skipping.")
            continue

        print(f"[{period}] {len(posts)} posts from {days} days → calling Claude...")
        posts_text = build_posts_text(posts)
        print(f"  Input: {len(posts_text):,} chars")

        try:
            summary = summarize_with_claude(posts_text, period, anchor_date, days)
            result[period] = summary
            themes = summary.get("themes", [])
            stocks = summary.get("stocks", [])
            multi = [s for s in stocks if len(s.get("channels", [])) >= 2]
            print(f"  → 테마 {len(themes)}개, 종목 {len(stocks)}개 (복수채널 {len(multi)}개)")
        except Exception as e:
            print(f"  [ERROR] {e}")

    save_json(SUMMARY_FILE, result)
    print(f"\nSaved to {SUMMARY_FILE}")


if __name__ == "__main__":
    main()
