#!/usr/bin/env python3
"""특정 종목 리스트에 대한 일괄 인제스션.

기존 pipelines/backfill_kis.py 함수 재사용 + Naver 투자자 백필 추가
(KIS investor는 30일 한계, Naver는 1.5년+ 가능).

사용:
  python -m analysis.ingest 010170 000250 090470 950160 --years 2
  python -m analysis.ingest --names 대한광통신 삼천당제약 제이스로보틱스 코오롱티슈진
"""
import os, sys, time, argparse, warnings
from datetime import datetime
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from file_io import load_json
from config import STOCK_MAP_FILE
from core.db import (
    init_db, get_conn, append_investor, save_broker_names,
)
from pipelines.backfill_kis import (
    backfill_prices, backfill_short, backfill_investor, backfill_member_daily,
)
from signals.kis_member_daily import build_broker_mapping
from signals.naver_flow import fetch_naver_flow


def backfill_investor_naver(code: str, max_pages: int = 80) -> int:
    """Naver 일별 매매 1.5년+ → investor_flow 테이블.

    Naver는 qty만 제공 — amt = qty × close 근사.
    개인 = -(외인 + 기관) (시장 청산 항등식 근사, 기타법인 비중 무시).
    """
    df = fetch_naver_flow(code, max_pages=max_pages, use_cache=False)
    if df is None or len(df) == 0:
        return 0

    rows = []
    for date, r in df.iterrows():
        close = int(r["close"])
        f_qty = int(r["foreign_net"])
        i_qty = int(r["inst_net"])
        retail_qty = -(f_qty + i_qty)
        rows.append({
            "date":         date.strftime("%Y-%m-%d"),
            "close":        close,
            "foreign_qty":  f_qty,
            "foreign_amt":  f_qty * close,
            "inst_qty":     i_qty,
            "inst_amt":     i_qty * close,
            "retail_qty":   retail_qty,
            "retail_amt":   retail_qty * close,
        })
    return append_investor(code, rows)


def resolve_targets(args) -> list:
    """CLI 인자 → [(name, code), ...] 리스트."""
    smap = load_json(STOCK_MAP_FILE, default={})
    code_to_name = {info["code"]: name for name, info in smap.items() if "code" in info}

    targets = []
    if args.names:
        for n in args.names:
            info = smap.get(n)
            if not info or "code" not in info:
                print(f"  [경고] {n} stock_map 미등록 — 스킵")
                continue
            targets.append((n, info["code"]))
    if args.codes:
        for c in args.codes:
            name = code_to_name.get(c, c)
            targets.append((name, c))
    return targets


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("codes", nargs="*", help="종목코드 (e.g. 010170 000250)")
    parser.add_argument("--names", nargs="*", help="종목명 (stock_map에서 코드 조회)")
    parser.add_argument("--years", type=int, default=2, help="가격/공매도 백필 연수 (default 2)")
    parser.add_argument("--skip-member", action="store_true", help="거래원 데이터 스킵 (느림)")
    args = parser.parse_args()

    targets = resolve_targets(args)
    if not targets:
        print("대상 종목 없음. --help 참고.")
        return

    days = args.years * 365
    print(f"{'='*60}")
    print(f"  Ingest — {len(targets)}종목 × {args.years}년")
    print(f"{'='*60}")
    for name, code in targets:
        print(f"  - {name} ({code})")
    print()

    init_db()

    # 거래원 매핑 (4종목 모두 스캔)
    if not args.skip_member:
        print("[1] 거래원 매핑 갱신")
        codes_only = [c for _, c in targets]
        mapping = build_broker_mapping(codes_only)
        save_broker_names(mapping)
        print(f"    {len(mapping)}개 거래원 매핑\n")
    else:
        mapping = {}

    # 종목별 백필
    t0 = time.time()
    for i, (name, code) in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {name} ({code})")
        ts = time.time()
        try:
            n_p = backfill_prices(code, days=days)
            print(f"    가격(pykrx): {n_p}행")
        except Exception as e:
            print(f"    [ERR 가격] {e}")

        try:
            n_s = backfill_short(code, days=days)
            print(f"    공매도(KIS): {n_s}행")
        except Exception as e:
            print(f"    [ERR 공매도] {e}")

        try:
            # Naver primary (~6.5y, qty × close 근사). KIS 보강은 단위 불일치(백만원)로 v0에서는 미사용.
            n_in = backfill_investor_naver(code, max_pages=80)
            print(f"    투자자(Naver): {n_in}행")
        except Exception as e:
            print(f"    [ERR 투자자] {e}")

        if not args.skip_member:
            try:
                n_m = backfill_member_daily(code, mapping, days=min(days, 365))
                print(f"    거래원(KIS): {n_m}행")
            except Exception as e:
                print(f"    [ERR 거래원] {e}")

        print(f"    경과: {time.time()-ts:.0f}초\n")

    # 통계
    print("[2] DB 통계")
    conn = get_conn()
    codes = [c for _, c in targets]
    placeholders = ",".join("?" * len(codes))
    for table in ["prices", "investor_flow", "short_balance", "member_daily"]:
        q = f"SELECT code, COUNT(*) AS n, MIN(date) AS s, MAX(date) AS e FROM {table} WHERE code IN ({placeholders}) GROUP BY code"
        for row in conn.execute(q, codes).fetchall():
            print(f"  {table:15} {row[0]}: {row[1]:>5}행  {row[2]} ~ {row[3]}")
    conn.close()
    print(f"\n총 소요: {(time.time()-t0)/60:.1f}분")


if __name__ == "__main__":
    main()
