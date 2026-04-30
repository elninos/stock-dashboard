"""가격 feature — OHLCV에서 파생되는 모든 가격 지표.

인터페이스:
  get_features(code, start, end) → DataFrame indexed by date

생성 컬럼:
  close, volume, value (대금)
  ret_1d, ret_5d, ret_20d                  과거 수익률
  ma20, ma60                               이동평균
  vs_ma20, vs_ma60                         (close/ma - 1) %
  high_60d, dd_from_60d_high               60일 고가 대비
  near_60d_high                            (close / 60d_high), 1.0 = 신고가
  fwd_ret_5d, fwd_ret_20d                  미래 수익률 (라벨링용)
  fwd_max_dd_20d                           20일 내 최대 낙폭 (peak labeling)
"""
import pandas as pd
import sys, os
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
from core.db import query_df


def get_features(code: str, start: str = None, end: str = None) -> pd.DataFrame:
    where = ["code = ?"]
    params = [code]
    if start: where.append("date >= ?"); params.append(start)
    if end:   where.append("date <= ?"); params.append(end)
    sql = f"""
        SELECT date, open, high, low, close, volume
        FROM prices WHERE {' AND '.join(where)} ORDER BY date
    """
    df = query_df(sql, tuple(params))
    if df.empty: return df

    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    df["value"] = df["close"] * df["volume"]   # 거래대금 근사 (close × vol)

    # 과거 수익률
    df["ret_1d"]  = df["close"].pct_change(1)
    df["ret_5d"]  = df["close"].pct_change(5)
    df["ret_20d"] = df["close"].pct_change(20)

    # 이동평균
    df["ma20"]    = df["close"].rolling(20).mean()
    df["ma60"]    = df["close"].rolling(60).mean()
    df["vs_ma20"] = df["close"]/df["ma20"] - 1
    df["vs_ma60"] = df["close"]/df["ma60"] - 1

    # 60일 고가 대비
    df["high_60d"]      = df["high"].rolling(60).max()
    df["near_60d_high"] = df["close"] / df["high_60d"]
    df["dd_from_60d_high"] = df["close"]/df["high_60d"] - 1     # ≤ 0

    # 미래 (라벨링용 — peak detection)
    df["fwd_ret_5d"]  = df["close"].pct_change(5).shift(-5)
    df["fwd_ret_20d"] = df["close"].pct_change(20).shift(-20)
    fwd_low_20d = df["low"].rolling(20).min().shift(-20)
    df["fwd_max_dd_20d"] = fwd_low_20d / df["close"] - 1        # ≤ 0

    return df
