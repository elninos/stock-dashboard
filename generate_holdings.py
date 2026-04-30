#!/usr/bin/env python3
"""보유 KOR 종목 목록을 Google Drive에 holdings.txt로 생성.

Windows AutoHotkey(hts_dump.ahk)가 이 파일을 읽어 NH HTS [1503]을 자동화한다.
장 마감 전(예: 15:30)에 실행하거나 analyze_signals.py와 함께 묶어서 실행.

Usage:
  python3 generate_holdings.py
"""
import os
import sys
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from file_io import load_json

# Google Drive 출력 경로 — 본인 환경에 맞게 수정
GDRIVE_BASE = os.path.expanduser(
    "~/Library/CloudStorage/GoogleDrive-srshin614@gmail.com"
    "/내 드라이브/01.Claude/01.주식"
)
HOLDINGS_TXT = os.path.join(GDRIVE_BASE, "holdings.txt")


def get_kor_holdings() -> list[tuple[str, str]]:
    txs = load_json(TRANSACTIONS_FILE, default=[])
    stock_map = load_json(STOCK_MAP_FILE, default={})

    qty = defaultdict(int)
    for tx in txs:
        if tx["type"] == "buy":
            qty[tx["stock"]] += tx["qty"]
        elif tx["type"] == "sell":
            qty[tx["stock"]] -= tx["qty"]

    holdings = []
    for name, q in qty.items():
        if q <= 0:
            continue
        info = stock_map.get(name, {})
        if info.get("nation") != "KOR" or not info.get("code"):
            continue
        holdings.append((name, info["code"]))

    return sorted(holdings)


def main():
    holdings = get_kor_holdings()
    if not holdings:
        print("[WARN] 보유 KOR 종목 없음")
        return

    os.makedirs(os.path.dirname(HOLDINGS_TXT), exist_ok=True)

    lines = [f"# 보유 KOR 종목 — {len(holdings)}개"]
    for name, code in holdings:
        lines.append(f"{name},{code}")

    with open(HOLDINGS_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"holdings.txt 저장 완료: {HOLDINGS_TXT}")
    for name, code in holdings:
        print(f"  {name} ({code})")


if __name__ == "__main__":
    main()
