"""천정 경보 시그널.

목표: 가격이 peak 부근일 때 (peak ±5%) 매도 시그널 발생.

핵심 패턴 (3종목 분석 공통):
  A. 신고가 + 큰 위꼬리 (intraday 신고가 후 종가 후퇴)
  B. 거래량 폭증 + 가격 정체 (분배의 첫 신호)
  C. 신고가 직후 음봉
  D. 장대음봉 (-7%+, 몸통 큼) — 확정 천정
  E. DART 5%선 이탈, 임원 대량 매도

각 시그널 점수 합산 → 임계값 넘으면 경보.
"""
import warnings
warnings.filterwarnings("ignore")
import pandas as pd


def diagnose_peak(df: pd.DataFrame, asof_date=None, dart_events: list = None,
                  flow_df=None) -> dict:
    """천정 경보 진단.

    df: OHLCV (시가/고가/저가/종가/거래량/등락률 칼럼 사용)
    asof_date: 분석 기준일 (None이면 최신)
    dart_events: [{date, type, reason, ...}] 형식
    flow_df: 네이버 외인/기관 일별 매매 (optional, naver_flow.fetch_naver_flow 결과)
    """
    # 칼럼 정규화
    if "종가" in df.columns and "close" not in df.columns:
        df = df.rename(columns={
            "시가": "open", "고가": "high", "저가": "low",
            "종가": "close", "거래량": "volume", "등락률": "chg_pct"
        })
    if asof_date:
        df = df[df.index <= asof_date]
    if len(df) < 30:
        return {"available": False, "error": "데이터 부족"}

    cur = float(df["close"].iloc[-1])
    asof = df.index[-1]

    # 60일 high
    last60 = df.tail(60)
    peak_60 = float(last60["high"].max())
    peak_60_close = float(last60["close"].max())
    from_peak = (cur/peak_60_close-1)*100  # 종가 기준 고점 거리

    score = 0
    triggers = []

    # === A. 위꼬리 캔들 (peak day signature) — 임계값 낮춤 5% → 3% ===
    last5 = df.tail(5)
    for i, (date, row) in enumerate(last5.iterrows()):
        h, l, o, c = float(row["high"]), float(row["low"]), float(row["open"]), float(row["close"])
        if c <= 0: continue
        upper_wick = (h - max(o, c)) / c * 100
        # 신고가 시도 (60일 고점 +/- 2% 이내) + 위꼬리 ≥ 3%
        if h >= peak_60 * 0.98 and upper_wick >= 3:
            days_ago = (len(last5) - 1 - i)
            # 위꼬리 크기 비례 점수
            wick_pts = 5 if upper_wick >= 7 else (4 if upper_wick >= 5 else 3)
            score += max(wick_pts - days_ago, 2)
            triggers.append(f"신고가 위꼬리 캔들 ({date.strftime('%m-%d')}, 위꼬리 {upper_wick:.1f}%, {days_ago}일 전)")

    # === B. 분배 (거래량 급증 + 가격 정체/하락) — 임계값 낮춤 2x → 1.5x ===
    last10 = df.tail(10)
    avg_vol_20 = df["volume"].iloc[-30:-10].mean() if len(df) >= 30 else df["volume"].mean()
    distribution_days = 0
    big_distribution = 0
    for date, row in last10.iterrows():
        v = float(row["volume"])
        chg = float(row.get("chg_pct", 0))
        if v > avg_vol_20 * 1.5 and chg < 1:
            distribution_days += 1
        if v > avg_vol_20 * 2.5 and chg < 0:
            big_distribution += 1
    if big_distribution >= 1:
        score += 3
        triggers.append(f"⭐ 큰 분배 캔들 {big_distribution}일 (거래량 2.5x+ + 음봉)")
    if distribution_days >= 2:
        score += 2
        triggers.append(f"분배 캔들 {distribution_days}일 (거래량 1.5x+ + 정체)")
    elif distribution_days >= 1:
        score += 1
        triggers.append(f"분배 캔들 1일 (관찰)")

    # === A2. 연속 음봉 (peak 직후 약화) ===
    # 최근 5일 중 음봉 카운트 (천정 부근일 때만)
    near_peak_now = from_peak >= -7
    if near_peak_now:
        bear_streak = 0
        for date, row in last5.iterrows():
            if float(row.get("chg_pct", 0)) < 0:
                bear_streak += 1
        if bear_streak >= 3:
            score += 3
            triggers.append(f"천정 부근 연속 음봉 {bear_streak}일")
        elif bear_streak >= 2:
            score += 2
            triggers.append(f"천정 부근 음봉 {bear_streak}일")

    # === A3. 거래량 감소 (매수세 소진) ===
    if len(df) >= 20 and near_peak_now:
        vol5 = df["volume"].tail(5).mean()
        vol_prev10 = df["volume"].iloc[-15:-5].mean()
        if vol_prev10 > 0:
            vol_ratio_5_to_10 = vol5 / vol_prev10
            if vol_ratio_5_to_10 < 0.5:
                score += 3
                triggers.append(f"천정 부근 거래량 급감 (최근 5일 평균이 직전 10일의 {vol_ratio_5_to_10*100:.0f}% — 매수세 소진)")
            elif vol_ratio_5_to_10 < 0.7:
                score += 1
                triggers.append(f"거래량 둔화 ({vol_ratio_5_to_10*100:.0f}%)")

    # === A4. 5일선 이탈 (단기 데드크로스) ===
    if len(df) >= 10 and near_peak_now:
        ma5 = df["close"].rolling(5).mean()
        ma5_v = float(ma5.iloc[-1])
        ma5_3d = float(ma5.iloc[-4]) if len(ma5) >= 4 else ma5_v
        if cur < ma5_v and ma5_v < ma5_3d:
            score += 2
            triggers.append(f"천정 부근 5일선 하향 이탈 (5MA {ma5_v:,.0f} 음전환)")

    # === A5. 횡보 분배 (Wyckoff) — 천정 부근에서 5일+ 횡보 ===
    # peak 부근 (-5% 이내)에서 5일 이상 가격이 좁은 범위 (±3% 이내) 횡보 + 거래량 둔화
    if len(df) >= 15 and near_peak_now:
        last10 = df.tail(10)
        # 최근 10일 중 peak 95% 이상에서 머문 일수
        days_near_peak = (last10["high"] >= peak_60 * 0.95).sum()
        if days_near_peak >= 5:
            # 가격 변동성 좁음 (10일 max-min < 7% of peak)
            price_range = (last10["high"].max() - last10["low"].min()) / peak_60 * 100
            if price_range < 10:
                # 거래량 둔화 동반
                vol_ratio_check = last10["volume"].mean() / df["volume"].iloc[-30:-10].mean() if len(df) >= 30 else 1
                if vol_ratio_check < 0.9:
                    score += 4
                    triggers.append(f"⭐ 횡보 분배 ({days_near_peak}일간 peak 부근 횡보, 거래량 {vol_ratio_check*100:.0f}%)")
                else:
                    score += 2
                    triggers.append(f"천정 부근 횡보 ({days_near_peak}일, range {price_range:.1f}%)")

    # === A6. 신고가 갱신 실패 (Lower High 시그널) ===
    # 최근 5일 중 60일 신고가 (-2%) 시도했지만 실패
    if len(df) >= 60 and near_peak_now:
        last5_high = df["high"].tail(5).max()
        # 그 직전 60일 고점 (5일 제외)
        prev_high_60 = df["high"].iloc[-65:-5].max() if len(df) >= 65 else df["high"].max()
        if last5_high < prev_high_60 * 0.99 and from_peak >= -7:
            # 천정 부근 머물지만 신고가 못 깨고 있음
            score += 2
            triggers.append(f"신고가 갱신 실패 (직전 60일 고점 {prev_high_60:,.0f} vs 최근 5일 고점 {last5_high:,.0f})")

    # === A7. 모멘텀 둔화 (RSI 대용) — 5일 평균 음전환 ===
    # 최근 5일 평균 등락률이 전 5일 평균보다 낮음 (모멘텀 둔화)
    if len(df) >= 15 and near_peak_now:
        chg_recent5 = df["chg_pct"].tail(5).mean() if "chg_pct" in df.columns else 0
        chg_prev5 = df["chg_pct"].iloc[-10:-5].mean() if "chg_pct" in df.columns else 0
        if chg_prev5 > 1 and chg_recent5 < 0.3:
            score += 2
            triggers.append(f"모멘텀 둔화 (전 5일 평균 +{chg_prev5:.1f}% → 최근 5일 +{chg_recent5:.1f}%)")

    # === C. 장대음봉 (확정 천정) ===
    bear_5d = []
    for date, row in last5.iterrows():
        c = float(row["close"]); o = float(row["open"])
        chg = float(row.get("chg_pct", 0))
        body_pct = abs(c - o) / c * 100 if c > 0 else 0
        if chg <= -7 and body_pct > 5:
            bear_5d.append({"date": date, "chg": chg, "body": body_pct})
    if bear_5d:
        # 확정 천정 — 매우 강한 시그널
        for b in bear_5d:
            score += 6 if b["chg"] <= -15 else 4
        latest = bear_5d[-1]
        triggers.append(f"⭐ 장대음봉 ({latest['date'].strftime('%m-%d')}, {latest['chg']:+.1f}%, 몸통 {latest['body']:.0f}%)")

    # === D. 신고가 직후 음봉 ===
    # 최근 5일 내 60일 신고가 갱신 + 그 후 음봉 발생
    if len(df) >= 10:
        for i in range(max(0, len(df)-7), len(df)-1):
            row = df.iloc[i]
            # 그날까지의 60일 고점
            window_60 = df.iloc[max(0, i-59):i+1]
            day_high = float(row["high"])
            window_high = float(window_60["high"].iloc[:-1].max()) if len(window_60) > 1 else 0
            if day_high > window_high:  # 신고가 갱신
                # 다음날부터 5일까지 음봉 검사
                for j in range(i+1, min(i+6, len(df))):
                    next_row = df.iloc[j]
                    next_chg = float(next_row.get("chg_pct", 0))
                    if next_chg <= -3:
                        days_ago = len(df) - 1 - j
                        score += 3
                        triggers.append(f"신고가 후 음봉 ({df.index[j].strftime('%m-%d')}, {next_chg:+.1f}%)")
                        break
                break  # 첫 신고가 케이스만 카운트

    # === E. 단기 추세 깨짐 (20일선 데드크로스) ===
    if len(df) >= 20:
        ma20 = df["close"].rolling(20).mean()
        ma20_v = float(ma20.iloc[-1])
        if cur < ma20_v and from_peak > -10:
            # 천정 부근에서 20일선 이탈
            score += 2
            triggers.append(f"천정 부근 20일선 이탈 (현재 {cur:,.0f} < 20MA {ma20_v:,.0f})")

    # === F. ATR 폭증 (변동성 spike) ===
    if len(df) >= 30:
        atr5 = ((df["high"] - df["low"]) / df["close"] * 100).tail(5).mean()
        atr20 = ((df["high"] - df["low"]) / df["close"] * 100).iloc[-25:-5].mean()
        if atr20 > 0 and atr5 > atr20 * 2 and from_peak > -15:
            score += 2
            triggers.append(f"변동성 폭증 (5일 ATR {atr5:.1f}% vs 20일 평균 {atr20:.1f}%)")

    # === H. 외인/기관 분배 패턴 (네이버 일별 매매) ===
    near_peak = from_peak >= -7  # 다른 곳에서 정의되어 있지만 안전하게 재정의
    if flow_df is not None and len(flow_df) >= 5:
        flow_focus = flow_df[flow_df.index <= asof].tail(5)
        if len(flow_focus) >= 5:
            inst_5d = int(flow_focus["inst_net"].sum())
            fr_5d = int(flow_focus["foreign_net"].sum())
            # 기관 매도 + 외인 매수 = 분배 패턴 (천정 부근일 때만)
            if near_peak and inst_5d <= -30000 and fr_5d >= 20000:
                score += 5
                triggers.append(f"⭐ 분배 패턴 (5일 기관 {inst_5d:+,}주 vs 외인 {fr_5d:+,}주, 천정 부근)")
            elif near_peak and inst_5d <= -20000 and fr_5d >= 10000:
                score += 3
                triggers.append(f"분배 패턴 (5일 기관 {inst_5d:+,}주 vs 외인 {fr_5d:+,}주)")

            # 외인 매도 전환 (이전 10일 + 였다가 최근 5일 -)
            if len(flow_df[flow_df.index <= asof]) >= 15:
                prev10 = flow_df[flow_df.index <= asof].iloc[-15:-5]["foreign_net"].sum()
                if prev10 >= 30000 and fr_5d <= -10000:
                    score += 4
                    triggers.append(f"⭐ 외인 매도 전환 (직전 10일 +{prev10:,} → 최근 5일 {fr_5d:+,})")

            # 외국인 보유율 감소
            if len(flow_focus) >= 5:
                fp_now = float(flow_focus["foreign_pct"].iloc[-1])
                fp_5d_ago = float(flow_focus["foreign_pct"].iloc[0])
                fp_chg = fp_now - fp_5d_ago
                if near_peak and fp_chg <= -0.3:
                    score += 3
                    triggers.append(f"외국인 보유율 5일 {fp_chg:+.2f}%p 감소 ({fp_5d_ago:.2f}% → {fp_now:.2f}%)")

            # 기관 단독 대량 매도
            if near_peak and inst_5d <= -50000:
                score += 4
                triggers.append(f"⭐ 기관 5일 대량 매도 ({inst_5d:+,}주)")

    # === G. DART 시그널 ===
    if dart_events:
        cutoff_60 = (asof - pd.Timedelta(days=60)).strftime("%Y-%m-%d")
        cutoff_90 = (asof - pd.Timedelta(days=90)).strftime("%Y-%m-%d")
        asof_str = asof.strftime("%Y-%m-%d")

        # 5%주주 이탈
        five_pct = [e for e in dart_events
                    if e.get("type") == "major_5pct_drop"
                    and cutoff_90 <= e["date"] <= asof_str]
        if five_pct:
            score += 4
            triggers.append(f"⭐ 5%주주 이탈 (90일내, {five_pct[-1]['date']})")

        # 임원 대량 매도
        import re
        big_sells = []
        for e in dart_events:
            if e.get("type") == "insider_sell" and cutoff_60 <= e["date"] <= asof_str:
                m = re.search(r'-([\d,]+)주', e.get("reason", ""))
                if m and int(m.group(1).replace(",", "")) >= 500_000:
                    big_sells.append(e)
        if big_sells:
            score += 3
            triggers.append(f"⭐ 임원 대량 매도 (60일내 {len(big_sells)}건)")

        # 자사주 처분
        ts_sells = [e for e in dart_events
                    if e.get("type") == "treasury_sell"
                    and cutoff_90 <= e["date"] <= asof_str]
        if ts_sells:
            score += 3
            triggers.append(f"자사주 처분 결정 (90일내)")

    # === 진단 ===
    near_peak = from_peak >= -7  # 고점 -7% 이내

    if not near_peak:
        # 이미 많이 빠진 후엔 의미 약함
        if score >= 6:
            level = "⚠️ 후행 천정 시그널 (이미 많이 빠짐)"
            action = "WATCH"
        else:
            level = "🟢 정상"
            action = "HOLD"
    else:
        if score >= 12:
            level = "🚨 강한 천정 경보 — 1차 매도 권고"
            action = "SELL_PARTIAL"
        elif score >= 8:
            level = "⚠️ 중간 천정 경보 — 관찰 + 손절선 셋업"
            action = "WATCH"
        elif score >= 4:
            level = "🟡 약한 신호"
            action = "MONITOR"
        else:
            level = "🟢 정상 (천정 신호 없음)"
            action = "HOLD"

    return {
        "available": True,
        "asof": asof,
        "price": cur,
        "peak_60_close": peak_60_close,
        "peak_60_high": peak_60,
        "from_peak": from_peak,
        "near_peak": near_peak,
        "score": score,
        "level": level,
        "action": action,
        "triggers": triggers,
    }


if __name__ == "__main__":
    import sys
    from pykrx import stock as krx
    from datetime import datetime, timedelta

    code = sys.argv[1] if len(sys.argv) > 1 else "010170"
    asof = sys.argv[2] if len(sys.argv) > 2 else None  # YYYY-MM-DD

    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=200)).strftime("%Y%m%d")
    df = krx.get_market_ohlcv_by_date(start, end, code)
    df.index = pd.to_datetime(df.index)

    asof_date = pd.to_datetime(asof) if asof else None
    r = diagnose_peak(df, asof_date=asof_date)
    print(f"\n천정 경보 진단 — {code} (기준일 {r['asof'].strftime('%Y-%m-%d')})")
    print(f"  현재 {r['price']:,.0f}원 / 60일 고점 {r['peak_60_close']:,.0f}원 (장중 {r['peak_60_high']:,.0f}) / 고점대비 {r['from_peak']:+.1f}%")
    print(f"  점수: {r['score']} → {r['level']}")
    print(f"  → 권고: {r['action']}")
    print(f"\n  트리거 ({len(r['triggers'])}개):")
    for t in r["triggers"]:
        print(f"    • {t}")
