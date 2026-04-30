#!/usr/bin/env python3
"""매일 자동 수집 (cron 16:30 KST 실행).

오늘 데이터만 추가 (이미 있으면 REPLACE).
보유 종목 + 관심 종목 기준.
"""
import os, sys, time, warnings
from datetime import datetime
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from pipelines.backfill_kis import (
    get_holding_stocks, fetch_krx_ohlcv, backfill_short,
    backfill_investor, backfill_member_daily, backfill_indices
)
from signals.kis_member_daily import build_broker_mapping
from core.db import save_broker_names


def main():
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"=== 일일 수집 ({today}) ===")

    holdings = get_holding_stocks()
    print(f"보유 {len(holdings)}종목")

    # 거래원 매핑 갱신 (TOP 10 종목 스냅샷)
    mapping = build_broker_mapping([c for _, c in holdings[:10]])
    save_broker_names(mapping)

    # 가격 OHLCV — KRX Open API로 KOSPI+KOSDAQ 전종목 일괄 수집
    # (최근 7일치 평일 = 약 14 API 호출, 캐시 hit 시 즉시)
    n_prices = fetch_krx_ohlcv(days=7)
    print(f"가격 OHLCV (KRX Open API): {n_prices}행")

    # 종목별 데이터 수집 (KIS API로 보유 종목별 추가 신호 수집)
    t0 = time.time()
    for i, (name, code) in enumerate(holdings):
        try:
            # 공매도/투자자는 30일치 (cumulative 갱신)
            backfill_short(code, days=30)
            backfill_investor(code)
            # 거래원은 7일치 (휴일 보완)
            backfill_member_daily(code, mapping, days=7)
            print(f"  [{i+1}/{len(holdings)}] {name} ✓")
        except Exception as e:
            print(f"  [{i+1}/{len(holdings)}] {name} ✗ {e}")

    # 지수
    backfill_indices(days=7)

    elapsed = time.time() - t0
    print(f"\n완료 ({elapsed:.0f}초)")


if __name__ == "__main__":
    main()
