#!/usr/bin/env python3
"""Peak Warning + Trend Break 통합 백테스트.

매주 금요일 시점에:
  1. peak_warning 점수 계산 (외인/기관 + DART + 가격 패턴)
  2. trend_break 점수 계산 (이동평균 + 다우이론 + DART)
  3. 룰:
     - peak ≥ 14: 1/2 매도 (가장 강한 천정 신호)
     - peak ≥ 12 (천정 부근): 1/3 매도
     - peak ≥ 8 (천정 -7% 이내): 1/4 추가 매도
     - trend ≥ 14 (3주 연속): 잔량 전량 매도 (대세 하락)
     - trend ≤ 5 + 60MA 위 + 2주 연속: 재매수
"""
import sys, os, warnings, argparse
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
from pykrx import stock as krx
import pandas as pd

from signals.peak_warning import diagnose_peak
from signals.trend_break import diagnose_trend_break
from signals.naver_flow import fetch_naver_flow
from backtest_signal import fetch_dart_events


def fmt_won(v):
    if v is None: return "─"
    av = abs(v); s = "+" if v >= 0 else "-"
    if av >= 1_0000_0000: return f"{s}{av/1_0000_0000:.2f}억"
    if av >= 1_0000:      return f"{s}{av/1_0000:,.0f}만"
    return f"{s}{av:,.0f}원"


def fmt_won_pos(v):
    if v is None: return "─"
    if v >= 1_0000_0000: return f"{v/1_0000_0000:.2f}억"
    if v >= 1_0000:      return f"{v/1_0000:,.0f}만"
    return f"{v:,.0f}원"


def backtest(stock_code: str, lookback_years: float = 2.0,
             starting_qty: int = 100,
             peak_strong: int = 14, peak_mid: int = 12, peak_warn: int = 8,
             trend_sell: int = 16, trend_buy: int = 7,
             trend_confirm_weeks: int = 3,
             peer_symbol: str = "LITE",
             no_rebuy: bool = False,
             verbose: bool = False):
    end = datetime.now()
    start = end - timedelta(days=int(365 * (lookback_years + 1.5)))

    df = krx.get_market_ohlcv_by_date(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), stock_code)
    if len(df) < 240:
        return {"error": "데이터 부족"}
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    # 외인/기관 매매
    try:
        flow = fetch_naver_flow(stock_code, max_pages=10)
    except Exception:
        flow = None

    # DART
    dart = fetch_dart_events(stock_code, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

    # KOSPI
    try:
        import FinanceDataReader as fdr
        kospi = fdr.DataReader("KS11", start.strftime("%Y-%m-%d"))["Close"]
    except Exception:
        kospi = None

    # Peer
    try:
        import yfinance as yf
        peer_df = yf.download(peer_symbol, start=start.strftime("%Y-%m-%d"),
                                end=end.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
        if hasattr(peer_df.columns, "levels"):
            peer_df.columns = peer_df.columns.get_level_values(0)
        peer_close = peer_df["Close"]
    except Exception:
        peer_close = None

    backtest_start_idx = max(240, len(df) - int(252 * lookback_years))
    initial_price = float(df["종가"].iloc[backtest_start_idx])

    qty = starting_qty
    cash = 0
    actions = []
    trend_streak = 0
    buy_streak = 0

    weekly_indices = [i for i in range(backtest_start_idx, len(df))
                       if df.index[i].weekday() == 4]
    if df.index[-1].weekday() != 4:
        weekly_indices.append(len(df) - 1)

    for idx in weekly_indices:
        asof = df.index[idx]
        history = df.iloc[:idx+1]

        try:
            peak_r = diagnose_peak(history, asof_date=asof, dart_events=dart, flow_df=flow)
            trend_r = diagnose_trend_break(history, peer_close=peer_close,
                                            market_close=kospi, dart_events=dart, asof_date=asof)
        except Exception:
            continue
        if not (peak_r.get("available") and trend_r.get("available")): continue

        price = peak_r["price"]
        ps = peak_r["score"]
        ts = trend_r["score"]
        from_peak = peak_r["from_peak"]

        # 연속 카운트
        if ts >= trend_sell:
            trend_streak += 1
        else:
            trend_streak = 0
        if ts <= trend_buy:
            buy_streak += 1
        else:
            buy_streak = 0

        action = "HOLD"

        # 직전 매도 가격 추적 (재매수 헛매수 방지)
        last_sell_price = None
        for a in reversed(actions):
            if "SELL" in a["action"] or "PEAK" in a["action"]:
                last_sell_price = a["price"]; break

        # 가격 vs MA 위치 — 강세 추세 보호 핵심
        ma60 = trend_r.get("ma60")
        ma120 = trend_r.get("ma120")
        ma240 = trend_r.get("ma240")
        above_120ma = ma120 and price > ma120
        above_240ma = ma240 and price > ma240
        # 강세 추세 = 240MA 위 + 120MA 위
        strong_uptrend = above_120ma and above_240ma
        # 약세 = 120MA 아래
        weak_trend = ma120 and price < ma120

        # ① 대세 하락 확정 (3주 연속 + 240MA 아래) → 전량 매도
        # 핵심: 240MA 위에서는 발동 안 함 (강세 추세 보호)
        if qty > 0 and trend_streak >= trend_confirm_weeks and not above_240ma:
            cash += qty * price
            action = f"TREND SELL ALL ({qty:,}주)"
            qty = 0
            trend_streak = 0
        # ② 천정 강한 + 추세 약화 동반 → 1/2 매도
        elif qty > 0 and ps >= peak_strong and ts >= 6 and from_peak > -10:
            sell_qty = qty // 2
            if sell_qty > 0:
                cash += sell_qty * price
                qty -= sell_qty
                action = f"PEAK 1/2 ({sell_qty:,}주)"
        # ③ 천정 중간 + 추세 약화 약간 + 천정 -5% 이내 + 강세 아닐 때 → 1/3
        elif qty > 0 and ps >= peak_mid and ts >= 4 and from_peak > -5 and not strong_uptrend:
            sell_qty = qty // 3
            if sell_qty > 0:
                cash += sell_qty * price
                qty -= sell_qty
                action = f"PEAK 1/3 ({sell_qty:,}주)"
        # ④ 약한 경보는 더 엄격: peak ≥ 8 + trend ≥ 6 + 천정 -3% 이내 + 보유 시작의 80%+ 만 트리거
        elif qty > 0 and ps >= peak_warn and ts >= 6 and from_peak > -3 \
             and qty >= starting_qty * 0.8:
            sell_qty = qty // 4
            if sell_qty > 0:
                cash += sell_qty * price
                qty -= sell_qty
                action = f"PEAK 1/4 ({sell_qty:,}주)"

        # ⑤ 재매수: 추세 회복 시 즉시 (60MA 위 + peak/trend 모두 낮음 + 강세 추세 시작)
        # 강세주가 일시 약세 후 회복 시 따라가는 것이 핵심
        if not no_rebuy and qty < starting_qty and cash > 0:
            ma60 = trend_r.get("ma60")
            ma120 = trend_r.get("ma120")
            # 추세 회복 조건:
            # ① 60MA 위 + 120MA 위 + peak ≤ 5 + trend ≤ 5 + 60MA가 우상향 (60일 전보다 큼)
            recover_strong = (ma60 and ma120 and price > ma60 > ma120
                              and ps <= 5 and ts <= 5)
            # ② 또는 60MA 위 + peak ≤ 7 + trend ≤ 7 + 1주 연속
            recover_mid = (ma60 and price > ma60 and ps <= 7 and ts <= 7
                           and buy_streak >= 1)
            if recover_strong or (recover_mid and qty < starting_qty * 0.5):
                target_qty = int((cash // price // 10) * 10)
                if target_qty > 0:
                    cash -= target_qty * price
                    qty += target_qty
                    action = f"BUY ({target_qty:,}주)"

        actions.append({
            "date": asof, "price": price,
            "peak_score": ps, "trend_score": ts, "from_peak": from_peak,
            "action": action, "qty": qty, "cash": cash,
            "value": qty * price + cash,
        })

        if verbose and action != "HOLD":
            print(f"  {asof.strftime('%Y-%m-%d')}  {price:>10,.0f}  P{ps:>3}/T{ts:>3} ({from_peak:+.1f}%)  → {action}")

    final_price = float(df["종가"].iloc[-1])
    final_value = qty * final_price + cash
    bh_value = starting_qty * final_price

    transactions = [a for a in actions if a["action"] != "HOLD"]

    return {
        "stock_code": stock_code,
        "start_date": df.index[backtest_start_idx].strftime("%Y-%m-%d"),
        "end_date": df.index[-1].strftime("%Y-%m-%d"),
        "initial_price": initial_price,
        "final_price": final_price,
        "starting_qty": starting_qty,
        "final_qty": qty, "final_cash": cash,
        "final_value": final_value, "buyhold_value": bh_value,
        "alpha": final_value - bh_value,
        "alpha_pct": (final_value/bh_value - 1) * 100 if bh_value > 0 else 0,
        "actions": actions, "transactions": transactions,
    }


def print_report(r):
    if "error" in r:
        print(f"❌ {r['error']}")
        return
    print(f"\n{'═'*100}")
    print(f"  📊 {r['stock_code']} ({r['start_date']} ~ {r['end_date']})")
    print(f"{'═'*100}")
    print(f"  💰 가격: {fmt_won_pos(r['initial_price'])} → {fmt_won_pos(r['final_price'])} ({(r['final_price']/r['initial_price']-1)*100:+.1f}%)")
    print(f"  📦 시작: {r['starting_qty']:,}주")
    print(f"  [A] Buy & Hold: {fmt_won_pos(r['buyhold_value']):>14}")
    print(f"  [B] 시그널:     {fmt_won_pos(r['final_value']):>14}  (보유 {r['final_qty']:,}주 + 현금 {fmt_won_pos(r['final_cash'])})")
    print(f"  ⚖️  알파:       {fmt_won(r['alpha']):>14}  ({r['alpha_pct']:+.1f}%)")

    if r["transactions"]:
        print(f"\n  📋 거래 ({len(r['transactions'])}건)")
        print(f"     {'일자':<12}{'가격':>11}{'액션':<26}{'점수':<14}{'고점%':>7}")
        print(f"     {'─'*80}")
        for t in r["transactions"]:
            score = f"P{t['peak_score']}/T{t['trend_score']}"
            fp = f"{t['from_peak']:+.1f}%"
            print(f"     {t['date'].strftime('%Y-%m-%d'):<12}{t['price']:>11,.0f}{t['action']:<26}{score:<14}{fp:>7}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("code")
    p.add_argument("--years", type=float, default=2.0)
    p.add_argument("--qty", type=int, default=100)
    p.add_argument("--peer", default="LITE")
    p.add_argument("-v", action="store_true", help="verbose")
    args = p.parse_args()

    r = backtest(args.code, args.years, args.qty, peer_symbol=args.peer, verbose=args.v)
    print_report(r)


if __name__ == "__main__":
    main()
