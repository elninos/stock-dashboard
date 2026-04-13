#!/usr/bin/env python3
"""Summarize stock news using Claude API.

Reads stock_news_raw.json → calls Claude per batch of stocks → saves stock_news.json.
Run after fetch_stock_news.py.
"""
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_FILE = os.path.join(BASE_DIR, "stock_news_raw.json")
OUTPUT_FILE = os.path.join(BASE_DIR, "stock_news.json")

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not API_KEY:
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("ANTHROPIC_API_KEY="):
                    API_KEY = line.strip().split("=", 1)[1].strip().strip('"').strip("'")

BATCH_SIZE = 5


def summarize_batch(batch: list[tuple[str, list[dict]]]) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=API_KEY)

    sections = []
    for stock, articles in batch:
        if not articles:
            continue
        lines = [f"\n## {stock} ({len(articles)}건)"]
        for a in articles:
            lines.append(f"[{a['date']}] [{a['source']}] {a['title']}")
            if a.get("snippet"):
                lines.append(f"  {a['snippet'][:200]}")
        sections.append("\n".join(lines))

    if not sections:
        return {}

    stocks_in_batch = [s for s, arts in batch if arts]

    prompt = f"""다음은 주식 보유 종목들의 최근 7일 뉴스입니다.
각 종목별로 아래 JSON 형식으로 분석해주세요.

규칙:
- 반드시 유효한 JSON만 출력 (다른 텍스트 없이)
- 모든 텍스트는 한국어
- 기사가 없는 종목은 결과에서 제외
- summary: 핵심 내용 2-3문장 (투자 관점 중심)
- sentiment: "positive" / "negative" / "neutral"
- sentiment_reason: 감성 판단 근거 한 줄
- keywords: 핵심 키워드 최대 4개 배열
- notable: 특히 주목할 기사 제목 (없으면 빈문자열)

JSON 형식:
{{
  "종목명": {{
    "summary": "...",
    "sentiment": "positive|negative|neutral",
    "sentiment_reason": "...",
    "keywords": ["키워드1", "키워드2"],
    "notable": "주목할 기사 제목 또는 빈문자열"
  }}
}}

뉴스 데이터:
{"".join(sections)}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
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
        print("WARNING: ANTHROPIC_API_KEY not set. Skipping news summarization.")
        sys.exit(0)

    with open(RAW_FILE, encoding="utf-8") as f:
        raw = json.load(f)

    all_stocks = raw.get("stocks", {})
    pairs = [(s, arts) for s, arts in all_stocks.items() if arts]
    no_news = [s for s, arts in all_stocks.items() if not arts]

    print(f"뉴스 있는 종목 {len(pairs)}개 요약 시작...")
    summaries = {}

    for i in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[i:i + BATCH_SIZE]
        names = [s for s, _ in batch]
        print(f"  배치 {i//BATCH_SIZE+1}: {', '.join(names)}")
        try:
            result = summarize_batch(batch)
            summaries.update(result)
            print(f"    → {len(result)}개 요약 완료")
        except Exception as e:
            print(f"    [ERROR] {e}")

    # Build output
    output = {
        "updated_at": datetime.now().isoformat(),
        "fetched_at": raw.get("fetched_at", ""),
        "stocks": {}
    }
    for stock, articles in all_stocks.items():
        s = summaries.get(stock, {})
        output["stocks"][stock] = {
            "articles": articles,
            "article_count": len(articles),
            "has_news": len(articles) > 0,
            "summary": s.get("summary", ""),
            "sentiment": s.get("sentiment", "neutral"),
            "sentiment_reason": s.get("sentiment_reason", ""),
            "keywords": s.get("keywords", []),
            "notable": s.get("notable", ""),
        }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n완료: {len(summaries)}개 요약 → {OUTPUT_FILE}")
    if no_news:
        print(f"뉴스 없음: {', '.join(no_news)}")


if __name__ == "__main__":
    main()
