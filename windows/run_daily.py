#!/usr/bin/env python3
"""Windows 일일 브리핑 자동화 오케스트레이터.

Task Scheduler에서 run_daily.bat → 이 스크립트 호출.
실행 순서:
  1. git pull (최신 데이터 동기화)
  2. fetch_briefing.py (텔레그램/블로그 수집)
  3. summarize_briefing_claude.py (Claude Code CLI 요약, Max 무료)
  4. fetch_stock_news.py (뉴스 수집)
  5. summarize_stock_news_claude.py (Claude Code CLI 요약, Max 무료)
  6. git add + commit + push (→ GitHub Actions 빌드/배포 트리거)
"""
import os
import subprocess
import sys
from datetime import datetime

# 저장소 루트 (이 파일의 부모 디렉터리)
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(REPO_DIR, "windows", "run_daily.log")


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd: list[str], cwd: str = REPO_DIR, check: bool = True) -> subprocess.CompletedProcess:
    log(f"$ {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.stdout.strip():
        log(result.stdout.strip()[:500])
    if result.returncode != 0:
        if result.stderr.strip():
            log(f"[STDERR] {result.stderr.strip()[:500]}")
        if check:
            raise RuntimeError(f"Command failed (exit {result.returncode}): {' '.join(cmd)}")
    return result


def main():
    log("=" * 60)
    log("Windows 일일 브리핑 시작")
    log("=" * 60)

    try:
        # 1. Git pull (원격 최신 상태 동기화)
        log("\n[1/6] git pull")
        run(["git", "pull", "--rebase", "origin", "main"])

        # 2. 브리핑 수집 (텔레그램/블로그 — AI 없음)
        log("\n[2/6] fetch_briefing.py")
        run(["python", os.path.join(REPO_DIR, "fetch_briefing.py")])

        # 3. 브리핑 AI 요약 (Claude Code CLI — Max 구독, 추가 비용 없음)
        log("\n[3/6] summarize_briefing_claude.py")
        run(["python", os.path.join(REPO_DIR, "summarize_briefing_claude.py")])

        # 4. 뉴스 수집 (AI 없음)
        log("\n[4/6] fetch_stock_news.py")
        run(["python", os.path.join(REPO_DIR, "fetch_stock_news.py")], check=False)

        # 5. 뉴스 AI 요약 (Claude Code CLI — Max 구독)
        log("\n[5/6] summarize_stock_news_claude.py")
        run(["python", os.path.join(REPO_DIR, "summarize_stock_news_claude.py")], check=False)

        # 6. Git commit & push
        log("\n[6/6] git commit & push")
        run(["git", "add",
             "briefing.json", "briefing_summary.json",
             "stock_news_raw.json", "stock_news.json"],
            check=False)

        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        commit_result = run(
            ["git", "commit", "-m", f"Windows auto briefing {ts} KST"],
            check=False
        )

        if "nothing to commit" in commit_result.stdout or commit_result.returncode == 1:
            log("변경사항 없음 — push 스킵")
        else:
            run(["git", "push", "origin", "main"])
            log("Push 완료 → GitHub Actions 배포 트리거됨")

    except Exception as e:
        log(f"[FATAL ERROR] {e}")
        sys.exit(1)

    log("\n✓ 완료")


if __name__ == "__main__":
    main()
