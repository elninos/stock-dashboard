#!/usr/bin/env python3
"""Summarize collected briefing posts using Claude API.

Reads briefing.json, sends posts to Claude for:
1. Cross-channel comprehensive market summary (Korean)
2. Stock/ticker mention extraction with frequency counts
3. Key themes and actionable insights

Outputs briefing_summary.json for dashboard display.
"""
import json
import os
import sys
from datetime import date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BRIEFING_FILE = os.path.join(BASE_DIR, "briefing.json")
SUMMARY_FILE = os.path.join(BASE_DIR, "briefing_summary.json")

# Allow API key via env or .env file
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not API_KEY:
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("ANTHROPIC_API_KEY="):
                    API_KEY = line.strip().split("=", 1)[1].strip().strip('"').strip("'")


def build_posts_text(day_data: dict) -> str:
    """Build a combined text block from all sources for a given day."""
    parts = []
    for src in day_data.get("sources", []):
        channel = src["name"]
        category = src.get("category", "")
        header = f"\n{'='*60}\n채널: {channel} ({category})\n{'='*60}"
        parts.append(header)
        for post in src["posts"]:
            ts = f"[{post.get('date', '')} {post.get('time', '')}]"
            parts.append(f"\n{ts}\n{post['text'][:1500]}")
    return "\n".join(parts)


def summarize_with_claude(posts_text: str, target_date: str) -> dict:
    """Call Claude API to generate structured summary."""
    import anthropic

    client = anthropic.Anthropic(api_key=API_KEY)

    prompt = f"""다음은 {target_date} 기준으로 수집된 여러 투자 텔레그램 채널의 포스트들입니다.
이 내용들을 종합 분석하여 아래 JSON 형식으로 응답해주세요.

중요 규칙:
- 반드시 유효한 JSON만 출력하세요. 다른 텍스트 없이 JSON만 출력하세요.
- 모든 텍스트는 한국어로 작성하세요.
- 여러 채널에서 중복 언급되는 종목/테마를 특히 강조하세요.

JSON 형식:
{{
  "date": "{target_date}",
  "market_summary": "오늘의 시장 종합 요약 (3-5문장, 핵심 이슈와 분위기)",
  "themes": [
    {{
      "title": "테마명",
      "summary": "테마 설명 (2-3문장)",
      "sentiment": "positive/negative/neutral",
      "mentioned_in": ["채널명1", "채널명2"]
    }}
  ],
  "stocks": [
    {{
      "name": "종목명",
      "ticker": "종목코드 (알면)",
      "mention_count": 3,
      "channels": ["채널명1", "채널명2"],
      "context": "언급 맥락 요약 (1-2문장)",
      "sentiment": "positive/negative/neutral"
    }}
  ],
  "macro": {{
    "us": "미국 시장 관련 요약 (1-2문장, 없으면 빈문자열)",
    "kr": "한국 시장 관련 요약 (1-2문장, 없으면 빈문자열)",
    "global": "글로벌/기타 매크로 요약 (1-2문장, 없으면 빈문자열)"
  }},
  "key_numbers": [
    {{
      "label": "지표명",
      "value": "수치",
      "change": "변동 (예: +2.3%)",
      "source": "출처 채널"
    }}
  ]
}}

stocks 배열은 mention_count 내림차순으로 정렬하고, 최소 2개 채널에서 언급된 종목을 우선 배치하세요.
themes 배열은 중요도순으로 정렬하세요.

수집된 포스트:
{posts_text}"""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Try to extract JSON from response
    if raw.startswith("```"):
        # Remove markdown code block
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw)


def main():
    if not API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set.")
        print("Set via: export ANTHROPIC_API_KEY='sk-ant-...'")
        print("Or create .env file with: ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    target_date = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()

    # Load briefing data
    with open(BRIEFING_FILE, encoding="utf-8") as f:
        briefings = json.load(f)

    if target_date not in briefings:
        # Try latest available date
        available = sorted(briefings.keys(), reverse=True)
        if available:
            target_date = available[0]
            print(f"Requested date not found, using latest: {target_date}")
        else:
            print("No briefing data available.")
            sys.exit(1)

    day_data = briefings[target_date]
    total_posts = sum(len(s["posts"]) for s in day_data["sources"])
    print(f"Summarizing {total_posts} posts from {len(day_data['sources'])} channels ({target_date})...")

    posts_text = build_posts_text(day_data)
    print(f"Input text: {len(posts_text):,} chars")

    summary = summarize_with_claude(posts_text, target_date)

    # Load existing summaries
    if os.path.exists(SUMMARY_FILE):
        with open(SUMMARY_FILE, encoding="utf-8") as f:
            summaries = json.load(f)
    else:
        summaries = {}

    summaries[target_date] = summary

    # Keep last 30 days
    sorted_dates = sorted(summaries.keys(), reverse=True)[:30]
    summaries = {d: summaries[d] for d in sorted_dates}

    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(summaries, f, ensure_ascii=False, indent=2)

    # Print summary stats
    stocks = summary.get("stocks", [])
    themes = summary.get("themes", [])
    multi_channel = [s for s in stocks if len(s.get("channels", [])) >= 2]

    print(f"\n=== 요약 완료 ===")
    print(f"테마: {len(themes)}개")
    print(f"언급 종목: {len(stocks)}개 (복수채널 언급: {len(multi_channel)}개)")
    if multi_channel:
        print(f"\n📊 복수 채널 언급 종목:")
        for s in multi_channel:
            print(f"  {s['name']} — {s['mention_count']}회 ({', '.join(s['channels'])})")
            print(f"    → {s['context']}")

    print(f"\nSaved to {SUMMARY_FILE}")


if __name__ == "__main__":
    main()
