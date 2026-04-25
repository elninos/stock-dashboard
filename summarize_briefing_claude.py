#!/usr/bin/env python3
"""Summarize briefing using Claude Code CLI (Max 구독 포함, 추가 API 비용 없음).

summarize_briefing.py의 Windows용 버전.
- Anthropic API 직접 호출 대신 'claude --print' CLI 서브프로세스 사용
- Claude Max 구독에 포함 → 추가 비용 없음
- 비효율 제거: weekly/biweekly/monthly는 하루 1회만 실행
"""
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from config import BRIEFING_FILE, BRIEFING_SUMMARY_FILE as SUMMARY_FILE, BRIEFING_PERIODS as PERIODS
from file_io import load_json, save_json, now_kst

TODAY = date.today().isoformat()


# ── Post collection (summarize_briefing.py와 동일) ────────────────────

def collect_posts_for_period(briefings: dict, anchor_date: str, days: int) -> list[dict]:
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


# ── Claude Code CLI 호출 ──────────────────────────────────────────────

def call_claude_cli(prompt: str, timeout: int = 300) -> str:
    """Claude Code CLI로 프롬프트를 전송하고 응답 텍스트 반환.

    claude --print 는 비대화형 모드로 실행 후 종료.
    프롬프트는 stdin으로 전달 (긴 프롬프트의 CLI arg 길이 제한 우회).
    """
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


def summarize_with_claude_cli(posts_text: str, period: str, anchor_date: str, days: int) -> dict:
    """Claude Code CLI로 기간별 요약 생성 → dict 반환."""
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

    raw = call_claude_cli(prompt)

    # JSON 코드블록 제거
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    return json.loads(raw)


# ── 스킵 로직: weekly/biweekly/monthly는 하루 1회만 ─────────────────

def should_run_period(existing: dict, period: str) -> bool:
    """이미 오늘 해당 기간을 요약했으면 False (daily 제외)."""
    if period == "daily":
        return True  # daily는 항상 재실행 (새 포스트 반영)

    period_data = existing.get(period, {})
    if not period_data:
        return True  # 아직 없음

    # 마지막 업데이트가 오늘 날짜면 스킵
    last_date = existing.get("updated_at", "")[:10]  # "2026-04-25"
    if last_date == TODAY:
        return False

    return True


# ── Main ─────────────────────────────────────────────────────────────

def main():
    anchor_date = sys.argv[1] if len(sys.argv) > 1 else TODAY
    force = "--force" in sys.argv  # 강제 재실행 (스킵 무시)

    briefings = load_json(BRIEFING_FILE)
    existing = load_json(SUMMARY_FILE, default={})

    available = sorted(briefings.keys(), reverse=True)
    if not available:
        print("브리핑 데이터 없음.")
        sys.exit(1)

    if anchor_date not in briefings:
        anchor_date = available[0]
        print(f"요청 날짜 없음, 최신 사용: {anchor_date}")

    # 기존 결과 유지하면서 업데이트
    result = dict(existing)
    result["updated_at"] = now_kst()
    ran = 0

    for period, days in PERIODS.items():
        if not force and not should_run_period(existing, period):
            print(f"[{period}] 오늘 이미 요약됨, 스킵. (--force 로 강제 실행)")
            continue

        posts = collect_posts_for_period(briefings, anchor_date, days)
        if not posts:
            print(f"[{period}] 포스트 없음, 스킵.")
            continue

        print(f"[{period}] {len(posts)}개 포스트 → Claude Code CLI 요약중...")
        posts_text = build_posts_text(posts)
        print(f"  입력: {len(posts_text):,}자")

        try:
            summary = summarize_with_claude_cli(posts_text, period, anchor_date, days)
            result[period] = summary
            themes = summary.get("themes", [])
            stocks = summary.get("stocks", [])
            multi = [s for s in stocks if len(s.get("channels", [])) >= 2]
            print(f"  → 테마 {len(themes)}개, 종목 {len(stocks)}개 (복수채널 {len(multi)}개)")
            ran += 1
        except Exception as e:
            print(f"  [ERROR] {e}")

    save_json(SUMMARY_FILE, result)
    print(f"\n{SUMMARY_FILE} 저장 완료 ({ran}개 기간 요약)")


if __name__ == "__main__":
    main()
