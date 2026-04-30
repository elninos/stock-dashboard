#!/usr/bin/env python3
"""매도 시그널 분석 — 매일 장마감 후 실행.

Usage:
  python3 analyze_signals.py              # 전체 보유 종목 분석
  python3 analyze_signals.py --stock RF머트리얼즈  # 단일 종목 테스트
"""
import argparse
import os
import sys
import time
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from file_io import load_json, save_json, now_kst
from signals.trend import analyze_trend
from signals.investor_flow import analyze_investor_flow
from signals.market_regime import analyze_market
from signals.broker_flow_parser import analyze_broker_flow
from signals.scoring import compute_signal

SELL_SIGNALS_FILE = os.path.join(BASE_DIR, "sell_signals.json")
BROKER_FLOW_DIR   = os.path.join(BASE_DIR, "broker_flow")


def get_kor_holdings() -> list[tuple[str, str]]:
    """transactions.json에서 현재 보유 KOR 종목 (이름, 코드) 반환."""
    txs      = load_json(TRANSACTIONS_FILE, default=[])
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


def run(target_stocks=None):
    print("=== 매도 시그널 분석 시작 ===\n")

    # 시장환경 먼저
    print("▶ 시장환경 분석 (KOSPI/KOSDAQ)...")
    market = analyze_market()
    regime = market.get("overall", "중립")
    kospi  = market.get("kospi", {})
    kosdaq = market.get("kosdaq", {})
    kospi_lvl = kospi.get('level', 0)
    kosdaq_lvl = kosdaq.get('level', 0)
    print(f"  KOSPI {kospi_lvl:,}  MA20 {kospi.get('ma20_gap_pct', 0):+.1f}%  → {kospi.get('regime', '-')}")
    print(f"  KOSDAQ {kosdaq_lvl:,}  MA20 {kosdaq.get('ma20_gap_pct', 0):+.1f}%  → {kosdaq.get('regime', '-')}")
    print(f"  종합 시장환경: {regime}\n")

    stocks = target_stocks or get_kor_holdings()
    print(f"▶ 보유 KOR 종목 {len(stocks)}개 분석\n")

    signals = []

    for i, (name, code) in enumerate(stocks, 1):
        print(f"[{i}/{len(stocks)}] {name} ({code})")

        # 추세
        trend = analyze_trend(name, code)
        print(f"  추세: score={trend.get('sub_score', 0)}  RSI={trend.get('rsi', '-')}  "
              f"MACD_hist={trend.get('macd_hist', '-')}")
        time.sleep(0.5)

        # 투자자별 수급
        investor = analyze_investor_flow(name, code)
        print(f"  수급: score={investor.get('sub_score', 0)}  "
              f"외국인20d={investor.get('foreign_20d', 0)/1e8:+.1f}억  "
              f"기관20d={investor.get('inst_20d', 0)/1e8:+.1f}억")
        time.sleep(0.5)

        # 창구별 (파일 있으면)
        broker = analyze_broker_flow(name, broker_flow_dir=BROKER_FLOW_DIR)
        if broker.get("available"):
            print(f"  창구: score={broker.get('sub_score', 0)}  "
                  f"최근7일={broker.get('recent_7d_net', 0):+,}")
        else:
            print(f"  창구: 데이터 없음 (broker_flow/ 폴더에 파일 추가 시 활성화)")

        # 종합
        signal = compute_signal(name, code, trend, investor, broker, regime)
        grade  = signal["grade"]
        score  = signal["score"]
        print(f"  ▶ 종합 {score}점 → [{grade}]")
        if signal["reasons"]:
            for r in signal["reasons"][:3]:
                print(f"     • {r}")
        print()

        signals.append(signal)
        time.sleep(0.3)

    # 점수 내림차순 정렬
    signals.sort(key=lambda x: x["score"], reverse=True)

    output = {
        "generated_at": now_kst(),
        "market": market,
        "signals": signals,
    }
    save_json(SELL_SIGNALS_FILE, output)

    print(f"=== 완료: {len(signals)}개 종목 → {SELL_SIGNALS_FILE} ===\n")

    # 요약 출력
    print("▶ 등급별 요약:")
    from collections import Counter
    grade_count = Counter(s["grade"] for s in signals)
    for grade in ["매도강추", "매도주의", "관망", "홀드"]:
        cnt = grade_count.get(grade, 0)
        if cnt:
            names = [s["stock"] for s in signals if s["grade"] == grade]
            print(f"  {grade}: {cnt}개 — {', '.join(names)}")


def main():
    parser = argparse.ArgumentParser(description="매도 시그널 분석")
    parser.add_argument("--stock", nargs="+", help="특정 종목만 분석 (종목명)")
    args = parser.parse_args()

    if args.stock:
        stock_map = load_json(STOCK_MAP_FILE, default={})
        targets = []
        for name in args.stock:
            info = stock_map.get(name)
            if info and info.get("code"):
                targets.append((name, info["code"]))
            else:
                print(f"[WARN] stock_map에 없음: {name}")
        if targets:
            run(targets)
    else:
        run()


if __name__ == "__main__":
    main()
