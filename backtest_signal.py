#!/usr/bin/env python3
"""시그널 백테스트.

매주 시점마다 시그널 (가격/거래량/패턴/Peer/DART)을 계산하고,
'그 시그널대로 매매했다면' PnL을 buy-and-hold와 비교.

시그널만 사용 가능한 historical 데이터:
  - OHLCV (pykrx, 5년)
  - DART (전체 history)
  - 글로벌 Peer (yfinance, 5년)
  - 매크로 (FDR, 5년)

사용자 선호: 중장기 수익 극대화 → 손실 회피보다 누적 수익 극대화 평가
"""
import sys, os, warnings, argparse
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
from pykrx import stock as krx
import pandas as pd
import yfinance as yf


# === 시그널 계산 (시점별) ===

def compute_signal_at(df: pd.DataFrame, asof_idx: int, peer_dfs: dict = None,
                      dart_events: list = None) -> dict:
    """asof_idx 시점에서의 시그널 계산.

    df: stock OHLCV (전체 시계열, asof_idx 까지만 사용)
    peer_dfs: {symbol: close_series} — 글로벌 Peer
    dart_events: [{date, type, sub_type, change, score_delta}] — DART 이벤트
    """
    if asof_idx < 60:
        return {"available": False, "reason": "데이터 부족"}

    # asof 시점까지의 데이터만
    history = df.iloc[:asof_idx+1]
    asof_date = history.index[-1]
    cur = float(history["종가"].iloc[-1])

    score_sell = 0; score_buy = 0
    reasons_sell = []; reasons_buy = []

    # === 1) 패턴 (가격/거래량) ===
    last20 = history.tail(20)
    last5  = history.tail(5)
    prev20 = history.iloc[-25:-5]

    rv5 = last5["거래량"].mean()
    pv20 = prev20["거래량"].mean() if len(prev20) > 0 else 1
    vol_ratio = rv5/pv20 if pv20 > 0 else 0

    chg_5 = (cur/float(history["종가"].iloc[-6])-1)*100 if len(history) >= 6 else 0
    chg_20 = (cur/float(history["종가"].iloc[-21])-1)*100 if len(history) >= 21 else 0
    chg_60 = (cur/float(history["종가"].iloc[-61])-1)*100 if len(history) >= 61 else 0
    chg_120 = (cur/float(history["종가"].iloc[-121])-1)*100 if len(history) >= 121 else 0

    peak_60 = float(history["종가"].tail(60).max())
    from_peak = (cur/peak_60-1)*100

    # 강세 추세 보호 (사용자 중장기 선호 반영)
    # 60일 +100% 이상 강세 추세 → 단기 분배 시그널은 거의 무시
    strong_uptrend = chg_60 > 100 or chg_120 > 200
    moderate_uptrend = 30 < chg_60 <= 100

    # 분배 패턴 — 강한 추세에서는 더 엄격하게
    if strong_uptrend:
        # 강세 추세 → 천정 -3% 이내 + 거래량 둔화 + 5일 음봉일 때만
        if from_peak > -3 and vol_ratio < 1.0 and chg_5 < 0:
            score_sell += 3
            reasons_sell.append(f"천정 분배 (고점 {from_peak:+.1f}%, vol 둔화, 5일 음봉)")
    elif moderate_uptrend:
        # 중간 추세 → 고점 -8% 이내 + 거래량 둔화
        if from_peak > -8 and vol_ratio < 1.3:
            score_sell += 2
            reasons_sell.append(f"분배 가능성 (60일 +{chg_60:.0f}% 추세 후 천정 부근)")
    else:
        # 추세 없을 때만 일반 분배 패턴
        if from_peak > -10 and vol_ratio < 1.5 and chg_60 > 0:
            score_sell += 3
            reasons_sell.append(f"분배 (고점 {from_peak:+.1f}%, vol {vol_ratio:.2f}x)")

    # 펌프 패턴 (개미 폭등) — 추세 무관 매도 신호
    if chg_5 > 25 and vol_ratio > 3:
        score_sell += 2
        reasons_sell.append(f"펌프 (5일 +{chg_5:.0f}%, vol {vol_ratio:.1f}x)")

    # 모멘텀 매수
    if from_peak < -25 and chg_20 > 0 and vol_ratio > 1.3:
        score_buy += 4
        reasons_buy.append(f"바닥 반등 (고점 {from_peak:+.0f}%, 거래량 회복)")

    # 강세 추세 진행 중 = 매수 (추세 추종)
    if 10 < chg_60 < 80 and chg_20 > 5 and chg_5 > -5:
        score_buy += 3
        reasons_buy.append(f"상승 추세 진행 (D-60 {chg_60:+.0f}%, 추세 미성숙)")

    # 장대음봉 (천정 신호) — 강세 추세에서는 강한 의미
    last5_rows = history.tail(5)
    bear_count = 0
    for _, row in last5_rows.iterrows():
        body = abs(float(row["종가"]) - float(row["시가"]))/float(row["종가"]) * 100 if float(row["종가"])>0 else 0
        if float(row["등락률"]) <= -10 and body > 5:
            bear_count += 1
    if bear_count > 0:
        # 강세 추세 + 장대음봉 = 진짜 천정
        weight = 4 if strong_uptrend else 3
        score_sell += bear_count * weight
        reasons_sell.append(f"최근 5일 장대음봉 {bear_count}개 ({'강세 후 천정' if strong_uptrend else '하락 신호'})")

    # 변동성 폭증 (고점 신호)
    last20_range = (last20["고가"] - last20["저가"]) / last20["종가"] * 100
    cur_range = float(last20_range.iloc[-1])
    avg_range = float(last20_range.iloc[:-1].mean())
    if cur_range > avg_range * 2.5 and from_peak > -5 and strong_uptrend:
        score_sell += 2
        reasons_sell.append(f"강세 천정에서 변동성 폭증 ({cur_range:.1f}%)")

    # === 2) 글로벌 Peer 동조성 ===
    if peer_dfs:
        peer_changes_5d = []
        for sym, peer_close in peer_dfs.items():
            try:
                # asof 일자에 가장 가까운 peer 가격
                peer_at_or_before = peer_close[peer_close.index <= asof_date]
                if len(peer_at_or_before) >= 6:
                    p_cur = float(peer_at_or_before.iloc[-1])
                    p_prev = float(peer_at_or_before.iloc[-6])
                    peer_changes_5d.append((p_cur/p_prev-1)*100)
            except Exception:
                pass
        if peer_changes_5d:
            avg_peer_5d = sum(peer_changes_5d)/len(peer_changes_5d)
            gap = chg_5 - avg_peer_5d
            if gap < -8 and chg_5 < 0:
                score_sell += 2
                reasons_sell.append(f"한국 단독 약세 (5일 {chg_5:+.1f}% vs Peer {avg_peer_5d:+.1f}%, gap {gap:.1f}%p)")
            elif gap > 10 and avg_peer_5d > 0:
                score_buy += 1
                reasons_buy.append(f"Peer 동조 + 알파 (gap +{gap:.1f}%p)")

    # === 3) DART 이벤트 (asof 이전 30일 이내) ===
    if dart_events:
        recent_dart = [e for e in dart_events
                       if e["date"] <= asof_date.strftime("%Y-%m-%d")
                       and e["date"] >= (asof_date - timedelta(days=30)).strftime("%Y-%m-%d")]
        # 5%선 이탈은 강세 추세에서 특히 강한 신호 (가중치 ×1.5)
        for ev in recent_dart:
            sell_d = ev.get("sell_delta", 0)
            buy_d  = ev.get("buy_delta", 0)
            # 5%선 이탈 강조
            if ev.get("type") == "major_5pct_drop" and strong_uptrend:
                sell_d = int(sell_d * 1.5)
            score_sell += sell_d
            score_buy  += buy_d
            if ev.get("reason"):
                if sell_d > 0:
                    reasons_sell.append(ev["reason"])
                if buy_d > 0:
                    reasons_buy.append(ev["reason"])

    net = score_sell - score_buy
    return {
        "available": True,
        "asof": asof_date,
        "price": cur,
        "score_sell": score_sell,
        "score_buy": score_buy,
        "net": net,
        "reasons_sell": reasons_sell,
        "reasons_buy": reasons_buy,
        "chg_5": chg_5, "chg_20": chg_20, "chg_60": chg_60,
        "from_peak": from_peak,
        "vol_ratio": vol_ratio,
    }


def fetch_dart_events(stock_code: str, start_date: str, end_date: str) -> list:
    """DART 이벤트 수집 (시점별 사용 위해 일자 정렬)."""
    try:
        from signals.dart_insider import (
            fetch_insider_trades, fetch_major_holder_changes, fetch_treasury_stock
        )
        # YYYYMMDD 포맷
        s = start_date.replace("-","")
        e = end_date.replace("-","")
        events = []

        # 임원 거래
        for ins in fetch_insider_trades(stock_code, s, e):
            d = ins["date"]
            d_iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            qty = ins.get("change_qty", 0)
            if qty < 0:
                events.append({
                    "date": d_iso, "type": "insider_sell",
                    "sell_delta": min(abs(qty)//1_000_000 + 1, 4),
                    "buy_delta": 0,
                    "reason": f"임원 매도 ({qty:+,}주)",
                })
            elif qty > 0:
                events.append({
                    "date": d_iso, "type": "insider_buy",
                    "sell_delta": 0,
                    "buy_delta": min(qty//1_000_000 + 1, 4),
                    "reason": f"임원 매수 ({qty:+,}주)",
                })

        # 5%주주 변동
        for mj in fetch_major_holder_changes(stock_code, s, e):
            d = mj["date"]
            d_iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            irds = mj.get("stkqy_irds", 0)
            try: rate = float(mj.get("stkrt", "0"))
            except: rate = 0
            holder = mj.get("holder", "")
            # 5%선 통과 (특히 국민연금 5%선 이탈은 강한 매도)
            if irds < 0 and rate < 5 and rate > 0:
                events.append({
                    "date": d_iso, "type": "major_5pct_drop",
                    "sell_delta": 5,
                    "buy_delta": 0,
                    "reason": f"5%주주 {holder[:10]} 5%선 이탈 ({rate:.2f}%)",
                })
            elif irds < 0:
                events.append({
                    "date": d_iso, "type": "major_decrease",
                    "sell_delta": 2,
                    "buy_delta": 0,
                    "reason": f"5%주주 {holder[:10]} 감소",
                })
            elif irds > 0:
                events.append({
                    "date": d_iso, "type": "major_increase",
                    "sell_delta": 0,
                    "buy_delta": 2,
                    "reason": f"5%주주 {holder[:10]} 증가",
                })

        # 자사주
        for ts in fetch_treasury_stock(stock_code, s, e):
            d = ts.get("date", "")
            if not d: continue
            d_iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d)==8 else d
            if ts["type"] == "buy":
                events.append({
                    "date": d_iso, "type": "treasury_buy",
                    "sell_delta": 0, "buy_delta": 4,
                    "reason": f"자사주 취득결정 ({ts.get('qty',0):,}주)",
                })
            else:
                events.append({
                    "date": d_iso, "type": "treasury_sell",
                    "sell_delta": 3, "buy_delta": 0,
                    "reason": f"자사주 처분결정",
                })

        events.sort(key=lambda x: x["date"])
        return events
    except Exception as e:
        print(f"  ! DART fetch error: {e}")
        return []


def fetch_peer_data(symbols: list, start: str, end: str) -> dict:
    """글로벌 Peer 일별 close."""
    out = {}
    for sym in symbols:
        try:
            df = yf.download(sym, start=start, end=end, progress=False, auto_adjust=True)
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
            if len(df) > 30:
                out[sym] = df["Close"]
        except Exception:
            pass
    return out


# === 백테스트 시뮬레이션 ===

def backtest(stock_code: str, lookback_years: float = 1.0,
             entry_buy_threshold: int = 4,
             entry_sell_threshold: int = 6,
             trim_threshold: int = 8,
             starting_qty: int = 1000) -> dict:
    """백테스트 실행.

    starting_qty: 처음부터 보유 (현재 사용자 시나리오 = 코어 포지션)
    매주 금요일 시점 시그널 확인.
    """
    end_date = datetime.now()
    start_date = end_date - timedelta(days=int(365 * (lookback_years + 0.5)))

    # 1. 가격 데이터
    df = krx.get_market_ohlcv_by_date(start_date.strftime("%Y%m%d"),
                                       end_date.strftime("%Y%m%d"), stock_code)
    if len(df) < 100:
        return {"error": "가격 데이터 부족"}
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    # 2. DART 이벤트
    print(f"  DART 이벤트 수집...")
    dart_events = fetch_dart_events(stock_code, start_date.strftime("%Y-%m-%d"),
                                     end_date.strftime("%Y-%m-%d"))
    print(f"    {len(dart_events)}건 수집")

    # 3. Peer 데이터 (광통신 글로벌 Tier 1/2)
    print(f"  Peer 데이터 수집...")
    peer_syms = ["LITE", "COHR", "MRVL", "CRDO", "CIEN", "AAOI"]
    peer_dfs = fetch_peer_data(peer_syms, start_date.strftime("%Y-%m-%d"),
                                end_date.strftime("%Y-%m-%d"))
    print(f"    {len(peer_dfs)}/{len(peer_syms)} Peer 가져옴")

    # 4. 시뮬레이션 — 매주 금요일에 결정
    actions = []
    backtest_start_idx = max(60, len(df) - int(252 * lookback_years))
    print(f"  시뮬레이션 시작: {df.index[backtest_start_idx].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}")

    qty = starting_qty
    cash = 0
    initial_price = float(df["종가"].iloc[backtest_start_idx])
    avg_buy_price = initial_price  # 처음부터 보유 가정

    weekly_indices = []
    for idx in range(backtest_start_idx, len(df)):
        if df.index[idx].weekday() == 4:  # 금요일
            weekly_indices.append(idx)
    if df.index[-1].weekday() != 4:
        weekly_indices.append(len(df)-1)

    for idx in weekly_indices:
        sig = compute_signal_at(df, idx, peer_dfs, dart_events)
        if not sig["available"]: continue

        date = sig["asof"]
        price = sig["price"]
        net = sig["net"]
        s_buy = sig["score_buy"]
        s_sell = sig["score_sell"]

        action = "HOLD"
        action_qty = 0

        # 보유 중인 경우
        if qty > 0:
            if net >= trim_threshold and qty >= 4:
                # 강한 매도 — 1/3 매도
                action_qty = qty // 3
                cash += action_qty * price
                qty -= action_qty
                action = f"SELL 1/3 ({action_qty:,}주)"
            elif net >= entry_sell_threshold and qty >= 4:
                # 약한 매도 — 1/4 매도
                action_qty = qty // 4
                cash += action_qty * price
                qty -= action_qty
                action = f"SELL 1/4 ({action_qty:,}주)"

        # 현금 보유 중인 경우 + 매수 시그널
        if cash > price * 100 and s_buy >= entry_buy_threshold and net <= -2:
            # 현금의 절반으로 매수
            buy_qty = int((cash / 2) // price // 10) * 10
            if buy_qty > 0:
                cash -= buy_qty * price
                # 평단 갱신
                if qty > 0:
                    avg_buy_price = (avg_buy_price * qty + price * buy_qty) / (qty + buy_qty)
                else:
                    avg_buy_price = price
                qty += buy_qty
                action = f"BUY ({buy_qty:,}주)"

        actions.append({
            "date": date, "price": price,
            "score_sell": s_sell, "score_buy": s_buy, "net": net,
            "action": action,
            "qty": qty, "cash": cash,
            "portfolio_value": qty * price + cash,
            "reasons_sell": sig["reasons_sell"][:3],
            "reasons_buy": sig["reasons_buy"][:3],
        })

    # 5. 결과 정리
    final_price = float(df["종가"].iloc[-1])
    final_value = qty * final_price + cash
    bh_value = starting_qty * final_price

    # 행동만 추출
    transactions = [a for a in actions if a["action"] != "HOLD"]

    return {
        "stock_code": stock_code,
        "lookback_years": lookback_years,
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
        "alpha_pct": (final_value/bh_value-1)*100,
        "actions": actions,
        "transactions": transactions,
        "n_dart_events": len(dart_events),
        "dart_events": dart_events,
    }


# === 결과 출력 ===

def print_backtest_report(r):
    print("\n" + "═" * 110)
    print(f"  📊 백테스트 결과 — {r['stock_code']} ({r['start_date']} ~ {r['end_date']})")
    print("═" * 110)

    print(f"\n  💰 가격: {r['initial_price']:,.0f}원 → {r['final_price']:,.0f}원 ({(r['final_price']/r['initial_price']-1)*100:+.1f}%)")
    print(f"  📦 시작 보유: {r['starting_qty']:,}주")
    print(f"\n  [전략 A] Buy & Hold (그냥 보유)")
    print(f"     최종 가치: {r['buyhold_value']:>14,.0f}원")
    print(f"\n  [전략 B] 시그널 기반 트레이딩")
    print(f"     최종 보유: {r['final_qty']:>14,}주")
    print(f"     최종 현금: {r['final_cash']:>14,.0f}원")
    print(f"     포트 가치: {r['final_value']:>14,.0f}원")
    print(f"\n  ⚖️  알파: {r['alpha']:+,.0f}원 ({r['alpha_pct']:+.1f}%)")
    if r['alpha'] > 0:
        print(f"     → ✅ 시그널 트레이딩이 Buy & Hold보다 우수")
    else:
        print(f"     → ❌ Buy & Hold이 더 우수 ({-r['alpha']:,.0f}원 차이)")

    print(f"\n  📋 거래 내역 ({len(r['transactions'])}건)")
    print(f"     {'일자':<12}{'가격':>10}{'액션':<22}{'점수':<14}{'잔량':>10}{'현금':>14}")
    print(f"     {'─'*90}")
    for t in r["transactions"]:
        score_str = f"매도{t['score_sell']}/매수{t['score_buy']}"
        print(f"     {t['date'].strftime('%Y-%m-%d'):<12}{t['price']:>10,.0f}{t['action']:<22}{score_str:<14}{t['qty']:>10,}{t['cash']:>14,.0f}")

    # 매수/매도 시점에서 그 후 30일 가격 변화
    print(f"\n  🎯 시그널 정확도 (각 액션 후 30일 가격 변화)")
    print(f"     {'일자':<12}{'액션':<22}{'시점가격':>10}{'30일후':>10}{'변화':>9}{'평가':<10}")
    print(f"     {'─'*80}")
    for t in r["transactions"]:
        # 30일 후 가격 찾기
        actions = r["actions"]
        idx = next((i for i, a in enumerate(actions) if a["date"] == t["date"]), -1)
        if idx >= 0:
            future_idx = min(idx + 4, len(actions) - 1)  # 4주 후 = 약 30일
            future_price = actions[future_idx]["price"]
            chg = (future_price/t["price"]-1)*100
            # SELL이면 가격 하락이 좋은 시그널, BUY는 상승이 좋음
            if "SELL" in t["action"]:
                judge = "✅ 정확" if chg < -3 else ("⚠️ 무방" if abs(chg) <= 3 else "❌ 잘못")
            elif "BUY" in t["action"]:
                judge = "✅ 정확" if chg > 3 else ("⚠️ 무방" if abs(chg) <= 3 else "❌ 잘못")
            else:
                judge = "─"
            print(f"     {t['date'].strftime('%Y-%m-%d'):<12}{t['action']:<22}{t['price']:>10,.0f}{future_price:>10,.0f}{chg:>+8.1f}% {judge:<10}")

    # DART 이벤트 시점 표시
    print(f"\n  🏛️ DART 이벤트 ({r['n_dart_events']}건)")
    for ev in r["dart_events"][-15:]:
        print(f"     {ev['date']}  [{ev['type']:<18}] {ev.get('reason','')}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("code", help="종목코드")
    p.add_argument("--years", type=float, default=1.0, help="백테스트 기간 (년)")
    p.add_argument("--qty", type=int, default=1000, help="초기 보유 수량")
    p.add_argument("--sell", type=int, default=6, help="매도 임계값")
    p.add_argument("--trim", type=int, default=8, help="강한 매도 임계값")
    p.add_argument("--buy", type=int, default=4, help="매수 임계값")
    args = p.parse_args()

    r = backtest(args.code, args.years, args.buy, args.sell, args.trim, args.qty)
    if "error" in r:
        print(f"❌ {r['error']}")
        return
    print_backtest_report(r)


if __name__ == "__main__":
    main()
