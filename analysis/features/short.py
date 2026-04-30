"""공매도 feature — 잔고 추세 + 가격 컨텍스트.

생성 컬럼:
  short_balance_qty               잔고 주식수
  short_balance_pct               잔고 비율 (%)
  short_balance_chg_5d            5일 변화율 (%)
  short_balance_chg_20d           20일 변화율
  short_ratio                     일별 공매도 거래 비중 (%)
  short_ratio_5d_avg              최근 5일 평균
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
        SELECT date, short_vol, short_ratio, short_balance_qty, short_balance_pct
        FROM short_balance WHERE {' AND '.join(where)} ORDER BY date
    """
    df = query_df(sql, tuple(params))
    if df.empty: return df

    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    df["short_balance_chg_5d"]  = df["short_balance_qty"].pct_change(5)
    df["short_balance_chg_20d"] = df["short_balance_qty"].pct_change(20)
    df["short_ratio_5d_avg"]    = df["short_ratio"].rolling(5).mean()

    return df
