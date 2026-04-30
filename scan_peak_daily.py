#!/usr/bin/env python3
"""보유 종목 천정 경보 + 대세 하락 일일 스캔.

매일 실행:
  1. 보유 종목 리스트 자동 추출
  2. 각 종목 peak 시그널 점수 계산 (외인/기관 일별 매매 + DART + 패턴)
  3. trend break 점수 (대세 하락)
  4. 점수 ≥ 임계값 종목 알림
"""
import sys, os, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
from collections import defaultdict
from pykrx import stock as krx
import pandas as pd

from signals.peak_warning import diagnose_peak
from signals.trend_break import diagnose_trend_break
from signals.naver_flow import fetch_naver_flow
from backtest_signal import fetch_dart_events
from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE


def fmt_won(v):
    if abs(v) >= 1_0000_0000: return f"{v/1_0000_0000:+.2f}억"
    if abs(v) >= 1_0000: return f"{v/1_0000:+,.0f}만"
    return f"{v:+,.0f}"


def get_holdings():
    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})
    qty = defaultdict(int)
    avg_cost = defaultdict(lambda: {"qty": 0, "cost": 0})

    for t in sorted(txs, key=lambda x: x.get("date","")):
        if t.get("type") == "buy":
            qty[t["stock"]] += t.get("qty", 0)
        elif t.get("type") == "sell":
            qty[t["stock"]] -= t.get("qty", 0)

    # FIFO 평단
    for s in qty:
        if qty[s] <= 0: continue
        lots = []
        for t in sorted([x for x in txs if x.get("stock")==s and x.get("type") in ("buy","sell")],
                        key=lambda x: x.get("date","")):
            if t["type"] == "buy":
                lots.append({"qty": t["qty"], "price": t["price"]})
            else:
                rem = t["qty"]
                while rem > 0 and lots:
                    lot = lots[0]
                    take = min(rem, lot["qty"])
                    lot["qty"] -= take
                    rem -= take
                    if lot["qty"] <= 0: lots.pop(0)
        cq = sum(l["qty"] for l in lots)
        cc = sum(l["qty"]*l["price"] for l in lots)
        avg_cost[s] = {"qty": cq, "cost": cc/cq if cq > 0 else 0}

    holdings = []
    EXCLUDE = ["KODEX", "TIME", "TIGER", "PLUS"]
    for s, q in qty.items():
        if q <= 0: continue
        info = smap.get(s, {})
        if info.get("nation") != "KOR" or not info.get("code"): continue
        if any(kw in s for kw in EXCLUDE): continue
        holdings.append({
            "name": s,
            "code": info["code"],
            "qty": avg_cost[s]["qty"],
            "avg_price": avg_cost[s]["cost"],
        })
    return holdings


def scan():
    holdings = get_holdings()
    end = datetime.now().strftime("%Y%m%d")
    start_long = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")

    print(f"\n{'═'*120}")
    print(f"  🎯 천정 경보 + 대세 하락 일일 스캔 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  대상: {len(holdings)}종목 (외인/기관 일별 매매 + DART + 패턴)")
    print(f"{'═'*120}\n")

    results = []
    for i, h in enumerate(holdings, 1):
        name = h["name"]; code = h["code"]
        print(f"  [{i}/{len(holdings)}] {name} ({code}) 분석 중...", end="\r")
        try:
            # 가격
            df = krx.get_market_ohlcv_by_date(start_long, end, code)
            if len(df) < 60: continue
            df.index = pd.to_datetime(df.index)

            # 외인/기관 매매
            try:
                flow = fetch_naver_flow(code, max_pages=5)
            except Exception:
                flow = None

            # DART
            try:
                dart = fetch_dart_events(code, "2024-06-01", end)
            except Exception:
                dart = []

            # Peak 진단
            peak_r = diagnose_peak(df, dart_events=dart, flow_df=flow)
            # Trend break 진단
            trend_r = diagnose_trend_break(df, dart_events=dart)

            cur_price = float(df["종가"].iloc[-1])
            pnl_pct = (cur_price/h["avg_price"]-1)*100 if h["avg_price"] > 0 else 0
            pnl_amt = h["qty"] * (cur_price - h["avg_price"])

            results.append({
                "name": name, "code": code,
                "qty": h["qty"], "avg": h["avg_price"], "cur": cur_price,
                "pnl_pct": pnl_pct, "pnl_amt": pnl_amt,
                "peak_score": peak_r.get("score", 0) if peak_r.get("available") else 0,
                "peak_action": peak_r.get("action", "?"),
                "peak_level": peak_r.get("level", "?"),
                "peak_triggers": peak_r.get("triggers", [])[:3],
                "from_peak": peak_r.get("from_peak", 0),
                "trend_score": trend_r.get("score", 0) if trend_r.get("available") else 0,
                "trend_action": trend_r.get("action", "?"),
                "trend_diagnosis": trend_r.get("diagnosis", "?"),
            })
        except Exception as e:
            results.append({"name": name, "code": code, "error": str(e)[:60]})

    print("                                                                                ", end="\r")

    # 정렬: 점수 높은 순 (peak + trend)
    results.sort(key=lambda x: -(x.get("peak_score", 0) + x.get("trend_score", 0)))

    # 카테고리별
    sell_strong = []
    sell_warning = []
    trend_break = []
    monitor = []
    hold = []

    for r in results:
        if "error" in r: continue
        ps = r["peak_score"]; ts = r["trend_score"]
        # 우선순위: 천정 강한 경보 → trend break → 천정 중간 → 모니터 → 보유
        if ps >= 12:
            sell_strong.append(r)
        elif ts >= 14:
            trend_break.append(r)
        elif ps >= 8:
            sell_warning.append(r)
        elif ps >= 4 or ts >= 8:
            monitor.append(r)
        else:
            hold.append(r)

    # 출력
    def print_group(label, items, color_emoji=""):
        if not items: return
        print(f"\n  {color_emoji} {label} — {len(items)}종목")
        print(f"  {'─'*118}")
        print(f"  {'종목':<14}{'코드':<8}{'평단':>9}{'현재':>9}{'PnL%':>8}{'PnL':>10}{'고점%':>7}{'천정':>5}{'대세':>5}  {'주요 시그널':<40}")
        print(f"  {'─'*118}")
        for r in items:
            triggers = " / ".join(t[:25] for t in r["peak_triggers"][:2])
            print(f"  {r['name'][:12]:<14}{r['code']:<8}{r['avg']:>9,.0f}{r['cur']:>9,.0f}{r['pnl_pct']:>+7.1f}%{fmt_won(r['pnl_amt']):>10}{r['from_peak']:>+6.1f}%{r['peak_score']:>5}{r['trend_score']:>5}  {triggers[:40]}")

    print_group("🚨 천정 강한 경보 (점수 ≥12) — 즉시 1/3 매도 검토", sell_strong)
    print_group("🚨 대세 하락 진입 (trend ≥14) — 잔량 매도 검토", trend_break)
    print_group("⚠️ 천정 중간 경보 (점수 8~11) — 관찰 + 손절선 셋업", sell_warning)
    print_group("🟡 모니터 (천정 4~7 또는 대세 8~13)", monitor)
    print_group("🟢 정상 (보유 지속)", hold)

    return results


if __name__ == "__main__":
    scan()
