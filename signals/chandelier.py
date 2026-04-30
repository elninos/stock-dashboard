"""Chandelier Exit — ATR 기반 트레일링 스탑.

Charles Le Beau 발명, Chuck LeBeau 명명.
ATR(Average True Range) × 배수를 사용해 종목별 변동성을 반영한 매도선.

공식:
  Long Exit = max(High, period=22) - ATR(period=22) × multiplier(=3)

장점:
  - 변동성 큰 종목 → 매도선 멀리 (덜 휘둘림)
  - 변동성 작은 종목 → 매도선 가까이 (빨리 컷)
  - 단순 % 기반 트레일링보다 종목별 적응적
"""
import warnings
warnings.filterwarnings("ignore")


def compute_atr(df, period: int = 22):
    """Average True Range.

    필요 컬럼: high, low, close
    """
    import pandas as pd
    high, low, close = df["high"], df["low"], df["close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def add_chandelier_exit(df, period: int = 22, multiplier: float = 3.0):
    """일별 Chandelier Exit 컬럼 추가.

    필요 컬럼: high, low, close
    추가 컬럼: atr, chandelier_exit, ce_breach
    """
    df = df.copy()
    df["atr"] = compute_atr(df, period)
    df["chandelier_exit"] = df["high"].rolling(period).max() - df["atr"] * multiplier
    df["ce_breach"] = (
        (df["close"].shift(1) > df["chandelier_exit"].shift(1)) &
        (df["close"] <= df["chandelier_exit"])
    ).fillna(False).astype(int)
    return df
