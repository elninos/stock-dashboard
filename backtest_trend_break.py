#!/usr/bin/env python3
"""대세 하락 시그널 기반 백테스트.

룰:
  - 점수 ≥ 14 → 전량 매도 (cash 보유)
  - 점수 ≤ 5 + 가격 > 60MA → 전량 재매수
  - 그 외는 유지
"""
import sys, os, warnings, argparse
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
from pykrx import stock as krx
import pandas as pd
import yfinance as yf

from signals.trend_break import diagnose_trend_break
from backtest_signal import fetch_dart_events


def backtest(stock_code: str, lookback_years: float = 2.0,
             starting_qty: int = 1000, sell_threshold: int = 14,
             buy_threshold: int = 5, peer_symbol: str = "LITE",
             confirm_weeks: int = 3):
    """
    confirm_weeks: 점수가 sell_threshold 이상으로 N주 연속이어야 매도 (헛매도 방지)
    """
    end = datetime.now()
    start = end - timedelta(days=int(365 * (lookback_years + 1)))  # 240MA 위해 +1년

    df = krx.get_market_ohlcv_by_date(start.strftime("%Y%m%d"),
                                       end.strftime("%Y%m%d"), stock_code)
    if len(df) < 240:
        return {"error": "데이터 부족"}
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    # 코스피
    try:
        import FinanceDataReader as fdr
        kospi = fdr.DataReader("KS11", start.strftime("%Y-%m-%d"))["Close"]
    except Exception:
        kospi = None

    # Peer
    try:
        peer_df = yf.download(peer_symbol, start=start.strftime("%Y-%m-%d"),
                               end=end.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
        if hasattr(peer_df.columns, "levels"):
            peer_df.columns = peer_df.columns.get_level_values(0)
        peer_close = peer_df["Close"]
    except Exception:
        peer_close = None

    # DART
    print(f"  DART 이벤트 수집...")
    dart_events = fetch_dart_events(stock_code, start.strftime("%Y-%m-%d"),
                                     end.strftime("%Y-%m-%d"))

    # 시뮬레이션 시작 인덱스 (240일 이후)
    backtest_start_idx = max(240, len(df) - int(252 * lookback_years))

    initial_price = float(df["close" if "close" in df.columns else "종가"].iloc[backtest_start_idx])

    qty = starting_qty
    cash = 0
    avg_buy = initial_price
    actions = []
    state = "HOLD"  # HOLD | CASH

    # 매주 금요일
    weekly_indices = [i for i in range(backtest_start_idx, len(df))
                       if df.index[i].weekday() == 4]
    if df.index[-1].weekday() != 4:
        weekly_indices.append(len(df) - 1)

    print(f"  시뮬레이션: {df.index[backtest_start_idx].strftime('%Y-%m-%d')} ~ {df.index[-1].strftime('%Y-%m-%d')}  (confirm {confirm_weeks}주)")

    high_score_streak = 0  # 연속 매도 시그널 주
    low_score_streak = 0   # 연속 매수 시그널 주

    for idx in weekly_indices:
        asof_date = df.index[idx]
        history = df.iloc[:idx+1]

        try:
            r = diagnose_trend_break(history, peer_close=peer_close,
                                      market_close=kospi, dart_events=dart_events,
                                      asof_date=asof_date)
        except Exception as e:
            continue
        if not r.get("available"): continue

        score = r["score"]
        price = r["price"]

        # 연속 시그널 카운트
        if score >= sell_threshold:
            high_score_streak += 1
        else:
            high_score_streak = 0

        if score <= buy_threshold:
            low_score_streak += 1
        else:
            low_score_streak = 0

        action = "HOLD"

        if state == "HOLD" and qty > 0:
            # N주 연속 매도 시그널일 때만 실제 매도
            if high_score_streak >= confirm_weeks:
                cash = qty * price
                action = f"SELL ALL ({qty:,}주)"
                qty = 0
                state = "CASH"
                high_score_streak = 0

        if state == "CASH" and cash > 0:
            ma60 = r.get("ma60")
            # 2주 연속 + 60MA 위 + 점수 낮음
            if low_score_streak >= 2 and ma60 and price > ma60:
                buy_qty = int(cash // price // 10) * 10
                if buy_qty > 0:
                    cash -= buy_qty * price
                    qty += buy_qty
                    avg_buy = price
                    action = f"BUY ({buy_qty:,}주)"
                    state = "HOLD"
                    low_score_streak = 0

        actions.append({
            "date": asof_date, "price": price,
            "score": score, "diagnosis": r["diagnosis"],
            "action": action,
            "qty": qty, "cash": cash,
            "portfolio_value": qty * price + cash,
            "triggers": r["triggers"][:3],
        })

    final_price = float(df["close" if "close" in df.columns else "종가"].iloc[-1])
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
        "final_qty": qty,
        "final_cash": cash,
        "final_value": final_value,
        "buyhold_value": bh_value,
        "alpha": final_value - bh_value,
        "alpha_pct": (final_value/bh_value - 1) * 100 if bh_value > 0 else 0,
        "actions": actions,
        "transactions": transactions,
    }


def print_report(r):
    if "error" in r:
        print(f"❌ {r['error']}")
        return
    print(f"\n{'═'*100}")
    print(f"  📊 백테스트 — {r['stock_code']} ({r['start_date']} ~ {r['end_date']})")
    print(f"{'═'*100}")
    print(f"  💰 가격: {r['initial_price']:,.0f}원 → {r['final_price']:,.0f}원 ({(r['final_price']/r['initial_price']-1)*100:+.1f}%)")
    print(f"  📦 시작: {r['starting_qty']:,}주")
    print()
    print(f"  [A] Buy & Hold: {r['buyhold_value']:>14,.0f}원")
    print(f"  [B] 시그널:     {r['final_value']:>14,.0f}원  (보유 {r['final_qty']:,}주 + 현금 {r['final_cash']:,.0f})")
    print(f"  ⚖️  알파:        {r['alpha']:+,.0f}원 ({r['alpha_pct']:+.1f}%)")

    print(f"\n  📋 거래 ({len(r['transactions'])}건)")
    print(f"     {'일자':<12}{'가격':>12}{'액션':<22}{'점수':>4}  {'트리거 (요약)':<60}")
    print(f"     {'─'*110}")
    for t in r["transactions"]:
        trig = " | ".join(x[:25] for x in t["triggers"][:2]) if t["triggers"] else ""
        print(f"     {t['date'].strftime('%Y-%m-%d'):<12}{t['price']:>12,.0f}{t['action']:<22}{t['score']:>4}  {trig[:60]}")

    # 시그널 정확도 (각 SELL 후 30/60/90일 가격)
    print(f"\n  🎯 시그널 정확도 (SELL 후 가격 추이)")
    print(f"     {'일자':<12}{'액션':<22}{'시점가':>10}{'30일후':>10}{'60일후':>10}{'90일후':>10}{'평가':<8}")
    print(f"     {'─'*90}")
    for t in r["transactions"]:
        if "SELL" not in t["action"]: continue
        idx = next((i for i, a in enumerate(r["actions"]) if a["date"] == t["date"]), -1)
        if idx < 0: continue
        actions = r["actions"]
        p30_idx = min(idx + 4, len(actions)-1)
        p60_idx = min(idx + 8, len(actions)-1)
        p90_idx = min(idx + 12, len(actions)-1)
        p30 = actions[p30_idx]["price"]
        p60 = actions[p60_idx]["price"]
        p90 = actions[p90_idx]["price"]
        c30 = (p30/t["price"]-1)*100
        c60 = (p60/t["price"]-1)*100
        c90 = (p90/t["price"]-1)*100
        # SELL 후 가격 하락 = 정확
        avg_chg = (c30 + c60 + c90) / 3
        judge = "✅ 정확" if avg_chg < -3 else ("⚠️ 무방" if avg_chg < 3 else "❌ 잘못")
        p30_str = f"{p30:,.0f} ({c30:+.0f}%)"
        p60_str = f"{p60:,.0f} ({c60:+.0f}%)"
        p90_str = f"{p90:,.0f} ({c90:+.0f}%)"
        print(f"     {t['date'].strftime('%Y-%m-%d'):<12}{t['action'][:20]:<22}{t['price']:>10,.0f}{p30_str:>15}{p60_str:>15}{p90_str:>15} {judge}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("code")
    p.add_argument("--years", type=float, default=2.0)
    p.add_argument("--qty", type=int, default=1000)
    p.add_argument("--peer", default="LITE", help="비교 Peer 심볼")
    args = p.parse_args()

    r = backtest(args.code, args.years, args.qty, peer_symbol=args.peer)
    print_report(r)


if __name__ == "__main__":
    main()
