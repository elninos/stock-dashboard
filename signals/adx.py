"""ADX (Average Directional Index) — 추세 강도 측정.

Welles Wilder Jr. 1978년 발명.
방향과 무관하게 추세의 "강도"만 측정 (0~100).

기준:
  ADX < 20:   추세 없음 (횡보) → 시그널 노이즈, 신뢰도 낮음
  ADX 20~25:  약한 추세 시작
  ADX ≥ 25:   강한 추세 → 시그널 신뢰
  ADX ≥ 50:   매우 강한 추세
"""
import warnings
warnings.filterwarnings("ignore")


def add_adx(df, period: int = 14):
    """ADX + +DI/-DI 컬럼 추가.

    필요 컬럼: high, low, close
    추가 컬럼: plus_di, minus_di, adx, trend_strong
    """
    import pandas as pd
    import numpy as np

    df = df.copy()
    high, low, close = df["high"], df["low"], df["close"]

    # +DM, -DM
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0),
        index=df.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0),
        index=df.index
    )

    # True Range
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder's smoothing (EMA with alpha = 1/period)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di_smooth = plus_dm.ewm(alpha=1/period, adjust=False).mean()
    minus_di_smooth = minus_dm.ewm(alpha=1/period, adjust=False).mean()

    df["plus_di"] = 100 * plus_di_smooth / atr
    df["minus_di"] = 100 * minus_di_smooth / atr

    dx = 100 * (df["plus_di"] - df["minus_di"]).abs() / (df["plus_di"] + df["minus_di"])
    df["adx"] = dx.ewm(alpha=1/period, adjust=False).mean()
    df["trend_strong"] = (df["adx"] >= 25).astype(int)

    return df
