#!/usr/bin/env python3
"""KIS API 일괄 백필 — 보유 + 관심 종목 1년치 데이터를 SQLite에 누적.

수집 데이터:
  1. 가격 OHLCV (pykrx)
  2. 거래원 일별 매매 (KIS, 모든 거래원)
  3. 공매도 잔고 (KIS, 90~120일치)
  4. 투자자별 매매 (KIS, 30일치)
  5. 지수 OHLCV (KOSPI/KOSDAQ + 섹터)

총 호출:
  종목 N개 × ~50 거래원 + 종목별 (공매도/투자자/가격) × 3 + 지수 ~30개
  ≈ N × 53 + 30 호출
  N=30 종목이면 약 1,620 호출 ≈ 6분 (rate limit 고려)
"""
import os, sys, time, warnings
from datetime import datetime, timedelta
from collections import defaultdict
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from core.db import (
    init_db, get_conn,
    append_prices, append_member_daily, append_short, append_investor,
    save_broker_names, load_broker_names_from_db, upsert,
)
from signals.kis_member_daily import build_broker_mapping, fetch_all_brokers_daily, aggregate_to_dataframe
from signals.kis_short import fetch_daily_short
from signals.kis_investor import fetch_investor_flow
from signals.kis_index import fetch_index_ohlcv, INDEX_CODES
from signals.krx_open_api import get_kospi_daily, get_kosdaq_daily
import pandas as pd


def _krx_rows_to_records(rows, code_filter=None):
    """KRX Open API 응답 rows → prices 테이블 레코드 list.

    code_filter가 주어지면 해당 종목만 필터.
    """
    out = []
    for r in rows:
        code = r.get("ISU_CD")
        if not code:
            continue
        if code_filter and code != code_filter:
            continue
        bas = r.get("BAS_DD", "")
        if len(bas) == 8:
            date = f"{bas[:4]}-{bas[4:6]}-{bas[6:8]}"
        else:
            date = bas

        def _to_int(v):
            try:
                return int(str(v).replace(",", "")) if v not in (None, "") else 0
            except (ValueError, AttributeError):
                return 0

        out.append({
            "code":   code,
            "date":   date,
            "open":   _to_int(r.get("TDD_OPNPRC")),
            "high":   _to_int(r.get("TDD_HGPRC")),
            "low":    _to_int(r.get("TDD_LWPRC")),
            "close":  _to_int(r.get("TDD_CLSPRC")),
            "volume": _to_int(r.get("ACC_TRDVOL")),
        })
    return out


def fetch_krx_ohlcv(days: int = 1) -> int:
    """KOSPI + KOSDAQ 전종목 OHLCV 일괄 수집 → prices 테이블.

    days=1 이면 오늘만 (2 API 호출). 주말/휴장이면 직전 거래일까지 자동 거슬러 올라감.
    days>1 이면 최근 N일 평일 모두 수집 (캐시 hit 시 빠름).

    Returns:
        prices 테이블에 누적된 행 수.
    """
    end_dt = datetime.now()
    total = 0
    days_added = 0
    cur = end_dt
    # 최근 평일부터 거꾸로 days일치 수집
    while days_added < days:
        if cur.weekday() < 5:
            d = cur.strftime("%Y%m%d")
            rows = get_kospi_daily(d) + get_kosdaq_daily(d)
            recs = _krx_rows_to_records(rows)
            if recs:
                df = pd.DataFrame(recs)
                total += upsert("prices", df, ["code", "date"])
                days_added += 1
            elif days == 1:
                # 오늘 데이터 없음 (휴장/장중) → 직전 평일 시도
                pass
        cur -= timedelta(days=1)
        # 안전장치: 최대 14일까지 거슬러 올라감
        if (end_dt - cur).days > 14 + days:
            break
    return total


def get_holding_stocks():
    """현재 보유 KOR 종목 자동 추출."""
    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    qty = defaultdict(int)
    for t in txs:
        if t.get("type") == "buy":
            qty[t["stock"]] += t.get("qty", 0)
        elif t.get("type") == "sell":
            qty[t["stock"]] -= t.get("qty", 0)

    holdings = []
    for s, q in qty.items():
        if q > 0:
            info = smap.get(s, {})
            if info.get("nation") == "KOR" and info.get("code"):
                holdings.append((s, info["code"]))
    return holdings


def backfill_prices(stock_code: str, days: int = 1825):
    """가격 OHLCV (KRX Open API) — 일자별 per-day 호출 (캐시).

    KRX Open API는 일자별 전종목 응답이라 첫 종목이 fetch하면 나머지는 캐시 hit.
    """
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days)
    records = []
    cur = start_dt
    while cur <= end_dt:
        if cur.weekday() < 5:
            d = cur.strftime("%Y%m%d")
            rows = get_kospi_daily(d) + get_kosdaq_daily(d)
            records.extend(_krx_rows_to_records(rows, code_filter=stock_code))
        cur += timedelta(days=1)
    if not records:
        return 0
    df = pd.DataFrame(records)
    return append_prices(stock_code, df.drop(columns=["code"]))


def backfill_member_daily(stock_code: str, broker_mapping: dict, days: int = 365):
    """거래원 일별 매매 — KIS API 한계 12개월."""
    end = datetime.now().strftime("%Y%m%d")
    # KIS 한계 12개월 — 무리하게 넘기면 빈 응답
    days = min(days, 365)
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    results = fetch_all_brokers_daily(stock_code, start, end, min_vol=100)
    if not results: return 0
    df = aggregate_to_dataframe(results, broker_mapping)
    if df is None or df.empty: return 0
    # date 형식 통일
    df["date"] = df["date"].apply(lambda d: f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(str(d))==8 else d)
    return append_member_daily(stock_code, df)


def backfill_short(stock_code: str, days: int = 1825):
    """공매도 잔고 — KIS API 5년+ 가능. default 5년.

    KIS API는 한 번 호출에 N일치만 받을 수 있을 수도 있어 페이징.
    """
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days)
    all_rows = []
    # 1년씩 페이징 (한 번에 큰 범위 안 받힐 수도)
    cursor = start_dt
    while cursor < end_dt:
        next_cursor = min(cursor + timedelta(days=365), end_dt)
        s = cursor.strftime("%Y%m%d")
        e = next_cursor.strftime("%Y%m%d")
        rows = fetch_daily_short(stock_code, s, e)
        if rows:
            all_rows.extend(rows)
        cursor = next_cursor + timedelta(days=1)

    rows = all_rows
    if not rows: return 0
    # date 형식 통일
    for r in rows:
        d = str(r.get("date", ""))
        if len(d) == 8:
            r["date"] = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return append_short(stock_code, rows)


def backfill_investor(stock_code: str):
    """투자자별 매매 (KIS는 약 30일)."""
    rows = fetch_investor_flow(stock_code)
    if not rows: return 0
    for r in rows:
        d = str(r.get("date", ""))
        if len(d) == 8:
            r["date"] = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return append_investor(stock_code, rows)


def backfill_indices(days: int = 1825):
    """주요 지수 OHLCV — 5년."""
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    total = 0
    for code, name in INDEX_CODES.items():
        rows = fetch_index_ohlcv(code, start, end)
        if not rows: continue
        df = pd.DataFrame(rows)
        df["index_code"] = code
        df = df[["index_code","date","open","high","low","close","volume"]]
        # date 형식 통일
        df["date"] = df["date"].apply(lambda d: f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(str(d))==8 else d)
        n = upsert("index_ohlcv", df, ["index_code","date"])
        total += n
        print(f"    [{code}] {name}: {n}행")
    return total


def main(only_holdings=False, max_stocks=None):
    print("="*80)
    print("  KIS 일괄 백필 — SQLite 누적")
    print("="*80)

    init_db()

    # 1. 거래원 매핑 (DB + 파일)
    print("\n[1] 거래원 매핑 갱신")
    sample_stocks = ["005930", "000660", "035420", "214450", "207940", "068270", "035720"]
    holdings = get_holding_stocks()
    sample_stocks += [c for _, c in holdings[:10]]
    mapping = build_broker_mapping(sample_stocks)
    save_broker_names(mapping)
    print(f"    {len(mapping)}개 거래원 매핑 (DB 저장)")

    # 2. 분석 대상
    targets = holdings
    if max_stocks:
        targets = targets[:max_stocks]
    print(f"\n[2] 분석 대상: {len(targets)}종목")

    # 3. 종목별 백필
    t_total = time.time()
    for i, (name, code) in enumerate(targets):
        print(f"\n  [{i+1}/{len(targets)}] {name} ({code})")
        t0 = time.time()
        try:
            n_p = backfill_prices(code)
            print(f"    가격: {n_p}행")
            n_s = backfill_short(code)
            print(f"    공매도: {n_s}행")
            n_i = backfill_investor(code)
            print(f"    투자자: {n_i}행")
            n_m = backfill_member_daily(code, mapping)
            print(f"    거래원: {n_m}행 ({time.time()-t0:.0f}초)")
        except Exception as e:
            print(f"    [ERR] {e}")

    # 4. 지수 백필
    print(f"\n[3] 지수 백필")
    n_idx = backfill_indices()
    print(f"  총 {n_idx}행")

    elapsed = time.time() - t_total
    print(f"\n총 소요: {elapsed/60:.1f}분")

    # 5. 통계
    print("\n[4] DB 통계")
    conn = get_conn()
    for table in ["prices", "member_daily", "short_balance", "investor_flow", "index_ohlcv"]:
        cnt = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {cnt:,}행")
    conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=None, help="최대 종목 수")
    args = parser.parse_args()
    main(max_stocks=args.max)
