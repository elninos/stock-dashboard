#!/usr/bin/env python3
"""Windows 일일 브리핑 자동화 오케스트레이터.

Task Scheduler에서 run_daily.bat → 이 스크립트 호출.
실행 순서:
  1. git pull (최신 데이터 동기화, GitHub Actions가 올린 raw 데이터 받아옴)
  2. summarize_briefing_claude.py (Claude Code CLI 요약, Max 무료)
  3. summarize_stock_news_claude.py (Claude Code CLI 요약, Max 무료)
  4. git add + commit + push (→ GitHub Actions deploy.yml 트리거)

NOTE: fetch_briefing.py / fetch_stock_news.py는 GitHub Actions update-briefing.yml이
      2시간마다 이미 실행 중 → 여기서 중복 실행 불필요.
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
        # 1. Git pull (GitHub Actions가 올린 최신 briefing.json / stock_news_raw.json 받아오기)
        log("\n[1/4] git pull")
        run(["git", "pull", "--rebase", "origin", "main"])

        # 2. 브리핑 AI 요약 (Claude Code CLI — Max 구독, 추가 비용 없음)
        log("\n[2/4] summarize_briefing_claude.py")
        run(["python", os.path.join(REPO_DIR, "summarize_briefing_claude.py")])

        # 3. 뉴스 AI 요약 (Claude Code CLI — Max 구독)
        log("\n[3/4] summarize_stock_news_claude.py")
        run(["python", os.path.join(REPO_DIR, "summarize_stock_news_claude.py")], check=False)

        # 4. Git commit & push (요약 결과만 — raw 데이터는 GitHub Actions가 관리)
        log("\n[4/4] git commit & push")
        run(["git", "add",
             "briefing_summary.json", "stock_news.json"],
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
