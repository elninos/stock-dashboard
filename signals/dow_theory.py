"""Dow Theory — Higher High / Lower High 패턴.

추세의 객관적 정의:
  상승 추세: HH (Higher High) + HL (Higher Low) 반복
  추세 약화: LH (Lower High) — 첫 경고
  하락 확정: LL (Lower Low) — 매도 시그널

Swing point 추출 방식:
  N일 lookback 내 최고/최저 = swing high/low
"""
import warnings
warnings.filterwarnings("ignore")


def add_dow_signals(df, lookback: int = 10):
    """Swing high/low + LH/LL 시그널 컬럼 추가.

    필요 컬럼: high, low, close
    추가 컬럼: swing_high, swing_low, last_high, last_low,
             dow_lh (Lower High 출현), dow_ll (Lower Low 확정)
    """
    import pandas as pd
    import numpy as np

    df = df.copy()
    win = lookback * 2 + 1

    # Swing point: 중심 ± lookback일 내 최대/최소
    df["swing_high"] = (df["high"] == df["high"].rolling(win, center=True).max()).astype(int)
    df["swing_low"]  = (df["low"]  == df["low"].rolling(win, center=True).min()).astype(int)

    # 직전 swing high/low 추적
    last_swing_high = None
    prev_swing_high = None
    last_swing_low = None
    prev_swing_low = None

    lh_signals = []  # Lower High 출현일
    ll_signals = []  # Lower Low 출현일
    last_highs = []
    last_lows = []

    for i in range(len(df)):
        row = df.iloc[i]
        # 새 swing high 등록
        if row["swing_high"] == 1:
            prev_swing_high = last_swing_high
            last_swing_high = row["high"]
        # 새 swing low 등록
        if row["swing_low"] == 1:
            prev_swing_low = last_swing_low
            last_swing_low = row["low"]

        # LH 검출: 새 swing high가 직전보다 낮음
        is_lh = 0
        if (row["swing_high"] == 1 and prev_swing_high is not None and
                last_swing_high < prev_swing_high * 0.98):  # 2% 이상 낮으면 LH
            is_lh = 1
        lh_signals.append(is_lh)

        # LL 검출: 새 swing low가 직전보다 낮음
        is_ll = 0
        if (row["swing_low"] == 1 and prev_swing_low is not None and
                last_swing_low < prev_swing_low * 0.98):
            is_ll = 1
        ll_signals.append(is_ll)

        last_highs.append(last_swing_high if last_swing_high else float("nan"))
        last_lows.append(last_swing_low if last_swing_low else float("nan"))

    df["dow_lh"] = lh_signals
    df["dow_ll"] = ll_signals
    df["last_swing_high"] = last_highs
    df["last_swing_low"] = last_lows

    return df
