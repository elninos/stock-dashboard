#!/usr/bin/env python3
"""보유 종목 + 제이스텍 거래원 정밀 분석 (multi_peaks 확장)."""
import os, sys, warnings
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# multi_peaks의 TARGETS만 변경
import analyze_multi_peaks as mp

# 현재 보유 + 제이스텍 (이미 분석한 9개 제외)
mp.TARGETS = [
    "콜마비앤에이치",
    "에이프로젠",
    "코아스템켐온",
    "필옵틱스",
    "NAVER",
    "에이피알",
    "두산",
    "리노공업",
    "SK하이닉스",
    "RF머트리얼즈",
    "파마리서치",  # 1년 내 다시 분석 (이전엔 8월만 했음)
    "제이스텍",  # 사용자 요청
]
# 14종목 - 일부는 KODEX (ETF), TIME (액티브펀드) 제외 — 거래원 분석 의미 없음
# 또 RF머트리얼즈, 두산, 리노공업, 에이피알은 신고가 갱신 중일 가능성

mp.OUT = os.path.join(BASE_DIR, "dashboard", "holdings_peaks.html")

if __name__ == "__main__":
    mp.main()
