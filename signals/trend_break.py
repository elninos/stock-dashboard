"""대세 하락 시그널 진단.

천정 예측이 아닌 '추세 깨짐' 사후 진단.

체크리스트 (각 점수):
  [이동평균선]
   1. 종가 < 60일 MA  ............. 1점 (단기 이탈)
   2. 종가 < 120일 MA  ............ 2점 (중기 이탈) ★ 핵심
   3. 종가 < 240일 MA  ............ 3점 (장기 이탈) ★ Stage 4
   4. 60일 MA < 120일 MA  ......... 1점 (단/중기 데드정렬)
   5. 120일 MA < 240일 MA  ........ 2점 (중/장기 데드정렬)
   6. 60일 MA 기울기 음 ........... 1점 (모멘텀 상실)

  [다우 이론 / 추세 구조]
   7. Lower Low (60일 swing low 이탈) ........ 3점
   8. Lower High (60일 swing high 갱신 실패) . 2점

  [거래량]
   9. 분배 (10일 중 하락일 평균거래량 > 상승일 평균거래량)  2점

  [상대 강도]
  10. vs Peer 60일 알파 < -20%  .. 2점
  11. vs 코스피 60일 알파 < -10% . 1점

  [펀더멘털 (DART)]
  12. 5%주주 5%선 이탈 (90일내) .. 3점
  13. 임원 대량 매도 (60일내, 100만주+) .. 2점
  14. 자사주 처분 결정 (90일내) .. 2점

  총점 max ≈ 27

  진단:
   ≥ 14: 명확한 대세 하락 (매도 권고)
   8~13: 추세 약화 (관찰 + 손절선 셋업)
   4~7:  정상 조정
   0~3:  강세 유지
"""
import warnings
warnings.filterwarnings("ignore")
import pandas as pd


def diagnose_trend_break(df: pd.DataFrame, peer_close: pd.Series = None,
                          market_close: pd.Series = None,
                          dart_events: list = None,
                          asof_date=None) -> dict:
    """대세 하락 시그널 종합 진단.

    df: stock OHLCV (close 칼럼 필수, '종가' or 'close')
    peer_close: 글로벌 Peer 평균 close 시계열 (선택)
    market_close: 시장 인덱스 close 시계열 (선택, 코스피 등)
    dart_events: [{date, type, reason, ...}] 형식
    asof_date: 분석 기준일 (None이면 마지막 행)
    """
    if "종가" in df.columns and "close" not in df.columns:
        df = df.rename(columns={"종가": "close", "거래량": "volume", "등락률": "chg_pct"})

    if asof_date:
        df = df[df.index <= asof_date]

    if len(df) < 60:
        return {"available": False, "error": "데이터 부족 (<60일)"}

    cur = float(df["close"].iloc[-1])
    asof = df.index[-1]

    # === 이동평균선 ===
    ma60 = df["close"].rolling(60).mean()
    ma120 = df["close"].rolling(120).mean() if len(df) >= 120 else None
    ma240 = df["close"].rolling(240).mean() if len(df) >= 240 else None

    ma60_v = float(ma60.iloc[-1])
    ma120_v = float(ma120.iloc[-1]) if ma120 is not None and not pd.isna(ma120.iloc[-1]) else None
    ma240_v = float(ma240.iloc[-1]) if ma240 is not None and not pd.isna(ma240.iloc[-1]) else None

    score = 0
    triggers = []
    misses = []

    # 1. 종가 < 60일 MA
    if cur < ma60_v:
        score += 1
        triggers.append(f"종가 < 60일MA ({cur:,.0f} < {ma60_v:,.0f})")
    else:
        misses.append(f"종가 ≥ 60일MA")

    # 2. 종가 < 120일 MA
    if ma120_v is not None:
        if cur < ma120_v:
            score += 2
            triggers.append(f"⭐ 종가 < 120일MA ({cur:,.0f} < {ma120_v:,.0f})")
        else:
            misses.append(f"종가 ≥ 120일MA ({cur:,.0f} > {ma120_v:,.0f})")

    # 3. 종가 < 240일 MA
    if ma240_v is not None:
        if cur < ma240_v:
            score += 3
            triggers.append(f"⭐ 종가 < 240일MA = Stage 4 ({cur:,.0f} < {ma240_v:,.0f})")
        else:
            misses.append(f"종가 ≥ 240일MA ({cur:,.0f} > {ma240_v:,.0f})")

    # 4. 60일 < 120일 (단/중기 데드정렬)
    if ma120_v is not None and ma60_v < ma120_v:
        score += 1
        triggers.append("60일MA < 120일MA (단/중기 데드정렬)")
    elif ma120_v is not None:
        misses.append("60일MA ≥ 120일MA")

    # 5. 120일 < 240일 (중/장기 데드정렬)
    if ma120_v is not None and ma240_v is not None and ma120_v < ma240_v:
        score += 2
        triggers.append("⭐ 120일MA < 240일MA (장기 데드정렬)")
    elif ma240_v is not None:
        misses.append("120일MA ≥ 240일MA")

    # 6. 60일 MA 기울기 음 (모멘텀 상실)
    if len(ma60.dropna()) >= 21:
        ma60_20d_ago = float(ma60.iloc[-21])
        if not pd.isna(ma60_20d_ago) and ma60_v < ma60_20d_ago:
            score += 1
            slope = (ma60_v / ma60_20d_ago - 1) * 100
            triggers.append(f"60일MA 기울기 음 ({slope:+.1f}%)")
        else:
            misses.append("60일MA 기울기 양")

    # === 다우 이론 ===

    # 7. Lower Low: 직전 60일 저점 이탈
    last60 = df.tail(60)
    last60_low = float(last60["close"].iloc[:-5].min())  # 최근 5일 제외
    last5_low = float(last60["close"].tail(5).min())
    if last5_low < last60_low:
        score += 3
        triggers.append(f"⭐ Lower Low — 60일 저점 {last60_low:,.0f} 이탈 (최근 5일 저점 {last5_low:,.0f})")
    else:
        misses.append(f"60일 저점 미이탈 (저점 {last60_low:,.0f})")

    # 8. Lower High: 60일 고점 미갱신
    last60_high = float(last60["close"].iloc[:-5].max())
    last5_high = float(last60["close"].tail(5).max())
    if last5_high < last60_high * 0.95:  # 5% 이상 미달
        score += 2
        gap = (last5_high/last60_high-1)*100
        triggers.append(f"Lower High — 60일 고점 {last60_high:,.0f} 갱신 실패 ({gap:.1f}%)")
    elif last5_high >= last60_high:
        misses.append(f"60일 신고점 갱신")

    # === 거래량 분배 ===
    last10 = df.tail(10)
    if "volume" in last10.columns:
        up_days = last10[last10["close"].diff() > 0]
        down_days = last10[last10["close"].diff() < 0]
        if len(up_days) > 0 and len(down_days) > 0:
            avg_up_vol = up_days["volume"].mean()
            avg_dn_vol = down_days["volume"].mean()
            if avg_dn_vol > avg_up_vol * 1.2:
                score += 2
                triggers.append(f"분배 (하락일 거래량 {avg_dn_vol:,.0f} > 상승일 {avg_up_vol:,.0f})")
            else:
                misses.append("거래량 분배 패턴 없음")

    # === 상대 강도 ===
    cur_60d_ago = float(df["close"].iloc[-61]) if len(df) >= 61 else cur
    target_60d = (cur/cur_60d_ago - 1) * 100

    if peer_close is not None and len(peer_close) >= 60:
        peer_at_or_before = peer_close[peer_close.index <= asof]
        if len(peer_at_or_before) >= 61:
            p_cur = float(peer_at_or_before.iloc[-1])
            p_60d_ago = float(peer_at_or_before.iloc[-61])
            peer_60d = (p_cur/p_60d_ago - 1) * 100
            alpha = target_60d - peer_60d
            if alpha < -20:
                score += 2
                triggers.append(f"⭐ vs Peer 알파 {alpha:+.1f}%p (60일 종목 {target_60d:+.0f}% / Peer {peer_60d:+.0f}%)")
            else:
                misses.append(f"vs Peer 알파 {alpha:+.1f}%p")

    if market_close is not None and len(market_close) >= 60:
        m_at = market_close[market_close.index <= asof]
        if len(m_at) >= 61:
            m_cur = float(m_at.iloc[-1])
            m_60d = float(m_at.iloc[-61])
            mkt_60d = (m_cur/m_60d - 1) * 100
            mkt_alpha = target_60d - mkt_60d
            if mkt_alpha < -10:
                score += 1
                triggers.append(f"vs 시장 알파 {mkt_alpha:+.1f}%p")
            else:
                misses.append(f"vs 시장 알파 {mkt_alpha:+.1f}%p")

    # === 펀더멘털 (DART) ===
    if dart_events:
        cutoff_90 = (asof - pd.Timedelta(days=90)).strftime("%Y-%m-%d")
        cutoff_60 = (asof - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
        asof_str = asof.strftime("%Y-%m-%d")

        # 12. 5%주주 5%선 이탈
        five_pct_drops = [e for e in dart_events
                          if e.get("type") == "major_5pct_drop"
                          and cutoff_90 <= e["date"] <= asof_str]
        if five_pct_drops:
            score += 3
            triggers.append(f"⭐ 5%주주 이탈 (90일내 {len(five_pct_drops)}건): {five_pct_drops[-1].get('reason','')}")

        # 13. 임원 대량 매도 (100만주+)
        big_insider_sells = []
        for e in dart_events:
            if e.get("type") == "insider_sell" and cutoff_60 <= e["date"] <= asof_str:
                # reason에서 주식수 추출
                r = e.get("reason", "")
                import re
                m = re.search(r'-([\d,]+)주', r)
                if m:
                    qty = int(m.group(1).replace(",", ""))
                    if qty >= 1_000_000:
                        big_insider_sells.append(e)
        if big_insider_sells:
            score += 2
            triggers.append(f"⭐ 임원 대량 매도 (60일내 {len(big_insider_sells)}건)")

        # 14. 자사주 처분
        ts_sells = [e for e in dart_events
                    if e.get("type") == "treasury_sell"
                    and cutoff_90 <= e["date"] <= asof_str]
        if ts_sells:
            score += 2
            triggers.append(f"자사주 처분 결정 (90일내 {len(ts_sells)}건)")

    # === 진단 ===
    if score >= 14:
        diagnosis = "🚨 명확한 대세 하락 (매도 권고)"
        action = "SELL"
    elif score >= 8:
        diagnosis = "⚠️ 추세 약화 (관찰 + 손절선 셋업)"
        action = "WATCH"
    elif score >= 4:
        diagnosis = "🟡 정상 조정 범위"
        action = "HOLD"
    else:
        diagnosis = "🟢 강세 유지"
        action = "HOLD"

    return {
        "available": True,
        "asof": asof,
        "price": cur,
        "score": score,
        "diagnosis": diagnosis,
        "action": action,
        "triggers": triggers,
        "misses": misses[:5],
        "ma60": ma60_v,
        "ma120": ma120_v,
        "ma240": ma240_v,
    }


def stage_label_from_ma(price, ma60, ma120, ma240):
    """MA 정렬에 따른 stage."""
    if not all([ma60, ma120, ma240]): return "?"
    # Stage 2: 가격 > 모든 MA + 정상 정렬 (단기 > 중기 > 장기)
    if price > ma60 > ma120 > ma240:
        return "Stage 2 (강세 추세)"
    if price > ma60 and price > ma120:
        return "Stage 2 (상승 추세)"
    # Stage 4: 가격 < 모든 MA + 데드 정렬
    if price < ma60 < ma120 < ma240:
        return "Stage 4 (대세 하락)"
    if price < ma60 and price < ma120 and ma60 < ma120:
        return "Stage 4 (하락 추세)"
    # Stage 3: 분배 (천정 부근, MA 가까이 횡보)
    if price > ma120 and ma60 < ma120:
        return "Stage 3 (분배)"
    # Stage 1: 베이싱
    return "Stage 1 (베이싱) 또는 전환"


if __name__ == "__main__":
    import sys
    from pykrx import stock as krx
    from datetime import datetime, timedelta

    code = sys.argv[1] if len(sys.argv) > 1 else "010170"
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")
    df = krx.get_market_ohlcv_by_date(start, end, code)
    df.index = pd.to_datetime(df.index)

    r = diagnose_trend_break(df)
    print(f"\n대세 하락 진단 — {code}")
    print(f"  점수 {r['score']} → {r['diagnosis']}")
    print(f"  현재 {r['price']:,.0f}원 / 60MA {r['ma60']:,.0f} / 120MA {r['ma120'] or 'N/A'} / 240MA {r['ma240'] or 'N/A'}")
    print(f"\n  ✓ 트리거 ({len(r['triggers'])}개):")
    for t in r["triggers"]:
        print(f"    {t}")
    print(f"\n  ✗ 미충족 ({len(r['misses'])}개):")
    for m in r["misses"]:
        print(f"    {m}")
