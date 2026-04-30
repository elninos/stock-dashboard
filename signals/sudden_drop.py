"""갑작스런 하락 직전 신호 탐지.

OHLCV 기반 — 거래원 데이터 없이도 강력. 강세장 노이즈 적음.

핵심 신호 7가지:
  1. Distribution Day (음봉 + 평소 거래량의 1.5배+)
  2. Long Upper Wick (위꼬리 긴 봉 — 신고가 도달 후 매도)
  3. Failed Breakout (신고가 갱신 후 즉시 빠짐)
  4. Volume Climax (거래량 폭증 = 분배 종료)
  5. Gap Down (갭 하락)
  6. Wide Range Down Bar (변동성 음봉)
  7. Distribution Days Count (4주 누적 분배일)
"""
import warnings
warnings.filterwarnings("ignore")


def add_sudden_drop_signals(df) -> "pd.DataFrame":
    """OHLCV DataFrame에 모든 신호 컬럼 추가.

    필요 컬럼: open, high, low, close, volume
    """
    import pandas as pd

    df = df.copy()
    df["range"] = df["high"] - df["low"]
    df["body"]  = (df["close"] - df["open"]).abs()
    df["upper_wick"] = df["high"] - df[["open", "close"]].max(axis=1)
    df["lower_wick"] = df[["open", "close"]].min(axis=1) - df["low"]
    df["change"] = df["close"].pct_change() * 100
    df["volume_ma20"] = df["volume"].rolling(20).mean()
    df["range_ma20"]  = df["range"].rolling(20).mean()
    df["high20"] = df["close"].rolling(20).max()

    # 1) Distribution Day: 음봉 + 거래량 평소의 1.5배 이상
    df["is_distribution_day"] = (
        (df["change"] <= -0.5) &
        (df["volume"] >= df["volume_ma20"] * 1.5)
    ).astype(int)

    # 2) Long Upper Wick: 위꼬리가 전체 range의 50% 이상 + 거래량 큼
    df["upper_wick_ratio"] = (df["upper_wick"] / df["range"].replace(0, float("nan"))).fillna(0)
    df["is_long_upper_wick"] = (
        (df["upper_wick_ratio"] >= 0.50) &
        (df["volume"] >= df["volume_ma20"] * 1.2)
    ).astype(int)

    # 3) Failed Breakout: 어제 20일 신고가 갱신 + 오늘 -3% 이상 하락
    df["new_high_yday"] = (df["close"].shift(1) >= df["high20"].shift(1) * 0.99).astype(int)
    df["is_failed_breakout"] = (
        (df["new_high_yday"] == 1) &
        (df["change"] <= -3.0)
    ).astype(int)

    # 4) Volume Climax: 거래량이 평소의 3배 이상 + 음봉 또는 위꼬리 큰 봉
    df["is_volume_climax"] = (
        (df["volume"] >= df["volume_ma20"] * 3) &
        ((df["change"] < 0) | (df["upper_wick_ratio"] >= 0.4))
    ).astype(int)

    # 5) Gap Down: 시가가 전일 종가 -2% 이상
    df["gap_pct"] = (df["open"] / df["close"].shift(1) - 1) * 100
    df["is_gap_down"] = (df["gap_pct"] <= -2.0).astype(int)

    # 6) Wide Range Down Bar: 변동성이 평소의 1.8배 + 음봉
    df["range_ratio"] = df["range"] / df["range_ma20"].replace(0, float("nan"))
    df["is_wide_range_down"] = (
        (df["range_ratio"] >= 1.8) &
        (df["change"] < -2.0)
    ).astype(int)

    # 7) Distribution Days Count (William O'Neil 방식): 4주(20일) 누적
    df["distribution_count_4w"] = df["is_distribution_day"].rolling(20).sum()

    # 종합 시그널 점수 (0~10)
    df["sudden_drop_score"] = (
        df["is_distribution_day"] +
        df["is_long_upper_wick"] +
        df["is_failed_breakout"] * 2 +  # failed breakout은 가중
        df["is_volume_climax"] * 2 +    # volume climax 가중
        df["is_gap_down"] +
        df["is_wide_range_down"]
    )

    return df


def detect_drop_signals(df) -> list:
    """일별 신호 이벤트 리스트 반환."""
    if "sudden_drop_score" not in df.columns:
        df = add_sudden_drop_signals(df)

    events = []
    for i in range(20, len(df)):
        row = df.iloc[i]
        idx = df.index[i]
        date = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)

        triggers = []
        if row.get("is_distribution_day") == 1:
            triggers.append({
                "type": "distribution_day",
                "icon": "📉",
                "label": "분배일 (Distribution Day)",
                "detail": f"음봉 {row['change']:.1f}% + 거래량 {row['volume']/row['volume_ma20']:.1f}x",
                "weight": 1,
            })
        if row.get("is_long_upper_wick") == 1:
            triggers.append({
                "type": "long_upper_wick",
                "icon": "⬆️",
                "label": "위꼬리 긴 봉",
                "detail": f"위꼬리 {row['upper_wick_ratio']*100:.0f}% (신고가 도달 후 매도)",
                "weight": 1,
            })
        if row.get("is_failed_breakout") == 1:
            triggers.append({
                "type": "failed_breakout",
                "icon": "❌",
                "label": "Failed Breakout",
                "detail": f"전일 신고가 갱신 후 {row['change']:.1f}% 하락",
                "weight": 2,
            })
        if row.get("is_volume_climax") == 1:
            triggers.append({
                "type": "volume_climax",
                "icon": "💥",
                "label": "Volume Climax",
                "detail": f"거래량 평소의 {row['volume']/row['volume_ma20']:.1f}배 + 음봉/위꼬리",
                "weight": 2,
            })
        if row.get("is_gap_down") == 1:
            triggers.append({
                "type": "gap_down",
                "icon": "⬇️",
                "label": "갭 하락",
                "detail": f"시가 갭 {row['gap_pct']:.1f}%",
                "weight": 1,
            })
        if row.get("is_wide_range_down") == 1:
            triggers.append({
                "type": "wide_range_down",
                "icon": "🌊",
                "label": "변동성 음봉",
                "detail": f"range 평소의 {row['range_ratio']:.1f}배 + {row['change']:.1f}%",
                "weight": 1,
            })

        if triggers:
            events.append({
                "date": date,
                "close": float(row["close"]),
                "score": int(row["sudden_drop_score"]),
                "dist_count_4w": int(row.get("distribution_count_4w", 0) or 0),
                "triggers": triggers,
            })

    return events
