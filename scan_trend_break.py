#!/usr/bin/env python3
"""보유 종목 전체 대세 하락 시그널 스캔."""
import sys, os, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
from pykrx import stock as krx
import pandas as pd

from signals.trend_break import diagnose_trend_break, stage_label_from_ma
from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE


def scan(codes_names: list):
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")

    # 코스피 (시장 비교)
    try:
        import FinanceDataReader as fdr
        kospi = fdr.DataReader("KS11", (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d"))["Close"]
    except Exception:
        kospi = None

    # 글로벌 광통신 Peer (선택)
    try:
        import yfinance as yf
        peer_df = yf.download("LITE", period="400d", progress=False, auto_adjust=True)
        if hasattr(peer_df.columns, "levels"):
            peer_df.columns = peer_df.columns.get_level_values(0)
        lite = peer_df["Close"]
    except Exception:
        lite = None

    results = []
    for code, name, *extra in codes_names:
        try:
            df = krx.get_market_ohlcv_by_date(start, end, code)
            if len(df) < 60:
                results.append({"code": code, "name": name, "available": False})
                continue
            df.index = pd.to_datetime(df.index)
            r = diagnose_trend_break(df, peer_close=lite, market_close=kospi)
            r["code"] = code
            r["name"] = name
            r["stage"] = stage_label_from_ma(r["price"], r["ma60"], r["ma120"], r["ma240"])
            results.append(r)
        except Exception as e:
            results.append({"code": code, "name": name, "available": False, "error": str(e)[:60]})

    # 점수 내림차순 정렬
    results.sort(key=lambda x: -x.get("score", 0))

    # 출력
    print("\n" + "═"*120)
    print(f"  📊 대세 하락 시그널 스캔 — {len(codes_names)}종목")
    print("═"*120)
    print(f"  {'#':<3}{'종목':<14}{'코드':<8}{'점수':>4} {'진단':<26}{'Stage':<22}{'현재가':>10}{'60MA':>10}{'120MA':>10}{'240MA':>10}")
    print(f"  {'─'*120}")

    for i, r in enumerate(results, 1):
        if not r.get("available"):
            print(f"  {i:<3}{r['name']:<14}{r['code']:<8}  ─   ✗ 데이터 없음")
            continue
        score = r["score"]
        diag = r["diagnosis"][:24]
        stage = r["stage"][:20]
        ma60_str = f"{r['ma60']:,.0f}" if r['ma60'] else "─"
        ma120_str = f"{r['ma120']:,.0f}" if r['ma120'] else "─"
        ma240_str = f"{r['ma240']:,.0f}" if r['ma240'] else "─"
        print(f"  {i:<3}{r['name']:<14}{r['code']:<8}{score:>4} {diag:<26}{stage:<22}{r['price']:>10,.0f}{ma60_str:>10}{ma120_str:>10}{ma240_str:>10}")

    # 카테고리별 그룹
    print("\n" + "═"*120)
    print("  카테고리별 분류")
    print("═"*120)
    by_action = {}
    for r in results:
        if not r.get("available"): continue
        by_action.setdefault(r["action"], []).append(r)

    for act, label in [("SELL", "🚨 매도 권고 (점수 ≥14)"), ("WATCH", "⚠️ 관찰 (점수 8~13)"),
                        ("HOLD", "🟢 보유 (점수 0~7)")]:
        items = by_action.get(act, [])
        if not items: continue
        print(f"\n  {label} — {len(items)}종목")
        for r in items:
            tg = r["triggers"][:3]
            tg_str = " / ".join(t[:50] for t in tg)
            print(f"    • {r['name']:<14} 점수 {r['score']:>2}  {r['stage'][:18]:<20} {tg_str[:80]}")

    return results


def main():
    # 보유 종목 자동 추출
    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})

    from collections import defaultdict
    qty = defaultdict(int)
    for t in txs:
        if t.get("type") == "buy": qty[t["stock"]] += t.get("qty", 0)
        elif t.get("type") == "sell": qty[t["stock"]] -= t.get("qty", 0)

    holdings = []
    EXCLUDE = ["KODEX", "TIME", "TIGER", "PLUS"]
    for s, q in qty.items():
        if q <= 0: continue
        info = smap.get(s, {})
        if info.get("nation") != "KOR": continue
        if not info.get("code"): continue
        if any(kw in s for kw in EXCLUDE): continue
        holdings.append((info["code"], s))

    print(f"보유 종목 {len(holdings)}개 스캔...")
    scan(holdings)


if __name__ == "__main__":
    main()
