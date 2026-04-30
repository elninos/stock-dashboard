"""투자자 feature — 외국인/기관/개인 매매 시계열.

생성 컬럼 (qty 기준 — Naver는 qty만 제공, amt = qty × close 근사):
  foreign_qty, inst_qty, retail_qty                 일별 순매수
  smart_qty (= foreign + inst)                      스마트머니
  *_amt_5d, *_amt_20d                               5/20일 누적 (대금, 억원)
  retail_amt_5d                                     (역지표)
  distribution_score                                분배 패턴 (smart 음수 + retail 양수)
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
        SELECT date, foreign_qty, foreign_amt, inst_qty, inst_amt, retail_qty, retail_amt
        FROM investor_flow WHERE {' AND '.join(where)} ORDER BY date
    """
    df = query_df(sql, tuple(params))
    if df.empty: return df

    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    df["smart_qty"] = df["foreign_qty"] + df["inst_qty"]
    df["smart_amt"] = df["foreign_amt"] + df["inst_amt"]

    # 5/20일 누적 (원 → 억으로 변환)
    for win in (5, 20):
        df[f"foreign_amt_{win}d"] = df["foreign_amt"].rolling(win).sum() / 1e8
        df[f"inst_amt_{win}d"]    = df["inst_amt"].rolling(win).sum() / 1e8
        df[f"retail_amt_{win}d"]  = df["retail_amt"].rolling(win).sum() / 1e8
        df[f"smart_amt_{win}d"]   = df["smart_amt"].rolling(win).sum() / 1e8

    # 분배 패턴 점수 — smart money 매도 + retail 매수 (정규화 안 함, 그냥 곱)
    df["distribution_score"] = (-df["smart_amt_5d"]).clip(lower=0) * (df["retail_amt_5d"]).clip(lower=0)

    return df
