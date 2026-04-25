#!/usr/bin/env python3
"""Summarize stock news using Claude Code CLI (Max 구독 포함, 추가 API 비용 없음).

summarize_stock_news.py의 Windows용 버전.
- Anthropic API 직접 호출 대신 'claude --print' CLI 서브프로세스 사용
"""
import json
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from config import STOCK_NEWS_RAW_FILE as RAW_FILE, STOCK_NEWS_FILE as OUTPUT_FILE, BATCH_SIZE_NEWS as BATCH_SIZE
from file_io import load_json, save_json, now_kst


def call_claude_cli(prompt: str, timeout: int = 180) -> str:
    """Claude Code CLI로 프롬프트 전송, 응답 텍스트 반환."""
    result = subprocess.run(
        ["claude", "--print", "--dangerously-skip-permissions"],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr_preview = result.stderr[:500] if result.stderr else "(no stderr)"
        raise RuntimeError(f"claude CLI 오류 (exit {result.returncode}): {stderr_preview}")
    return result.stdout.strip()


def summarize_batch(batch: list[tuple[str, list[dict]]]) -> dict:
    """배치(종목 목록)를 Claude Code CLI로 요약 → dict 반환."""
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

    raw = call_claude_cli(prompt)

    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw)


def main():
    raw = load_json(RAW_FILE)

    all_stocks = raw.get("stocks", {})
    pairs = [(s, arts) for s, arts in all_stocks.items() if arts]
    no_news = [s for s, arts in all_stocks.items() if not arts]

    print(f"뉴스 있는 종목 {len(pairs)}개 요약 시작 (Claude Code CLI)...")
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

    output = {
        "updated_at": now_kst(),
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

    save_json(OUTPUT_FILE, output)
    print(f"\n완료: {len(summaries)}개 요약 → {OUTPUT_FILE}")
    if no_news:
        print(f"뉴스 없음: {', '.join(no_news)}")


if __name__ == "__main__":
    main()
