"""거래원 feature — 단일창구 집중도 + 주도자 식별 + 빅데이.

핵심 아이디어 (친구분 분석에서):
  - "어느 창구가 랠리를 만들었나"를 식별하면 그 창구 이탈이 = 매도 시그널
  - 단일창구 집중도가 높을수록 출구 유동성 리스크 ↑
  - 빅데이 (단일창구 평균+2σ 매수) 패턴 소실 = 모멘텀 고갈

생성 컬럼:
  top_broker_60d                  60일 누적 최대 매수 거래원 이름
  top_broker_share_60d            60일 누적 매수에서 그 창구 점유율 (%)
  top_broker_net_5d               그 창구의 5일 net (qty)
  top_broker_net_20d              20일 net
  top_broker_reversed             5일 net이 음수로 전환했나 (bool)
  buy_concentration_60d           60일 매수 거래원 HHI (집중도, 0~1)
  big_day_count_20d               빅데이(매수 평균+2σ 이상) 개수 — 최근 20일
  big_day_recency                 마지막 빅데이로부터 며칠 (NaN = 60일 내 없음)
"""
import pandas as pd, numpy as np
import sys, os
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
from core.db import query_df


def _load_member(code: str, start: str = None, end: str = None) -> pd.DataFrame:
    where = ["code = ?"]
    params = [code]
    if start: where.append("date >= ?"); params.append(start)
    if end:   where.append("date <= ?"); params.append(end)
    sql = f"""
        SELECT date, broker_code, broker_name, buy, sell, net
        FROM member_daily WHERE {' AND '.join(where)} ORDER BY date
    """
    df = query_df(sql, tuple(params))
    if df.empty: return df
    df["date"] = pd.to_datetime(df["date"])
    return df


def get_features(code: str, start: str = None, end: str = None) -> pd.DataFrame:
    raw = _load_member(code, start, end)
    if raw.empty:
        return pd.DataFrame()

    all_dates = pd.Index(sorted(raw["date"].unique()), name="date")
    out = pd.DataFrame(index=all_dates)

    # 일별 단일창구 최대 매수자 (그날 가장 큰 net buyer)
    daily_top = (
        raw.sort_values(["date", "net"], ascending=[True, False])
           .groupby("date").first()[["broker_name", "net"]]
           .rename(columns={"broker_name": "daily_top_broker", "net": "daily_top_net"})
    )
    out = out.join(daily_top)

    # 60일 누적 net by broker — 매일 윈도우로 다시 계산은 비용↑
    # v0: 점프 윈도우 (각 시점에서 직전 60거래일 누적)
    pivot_buy = raw.pivot_table(index="date", columns="broker_name",
                                 values="buy", aggfunc="sum", fill_value=0)
    pivot_net = raw.pivot_table(index="date", columns="broker_name",
                                 values="net", aggfunc="sum", fill_value=0)

    # 60일 롤링 매수 누적 (각 broker별)
    roll_buy_60 = pivot_buy.rolling(60, min_periods=20).sum()
    roll_net_60 = pivot_net.rolling(60, min_periods=20).sum()
    roll_net_5  = pivot_net.rolling(5,  min_periods=3).sum()
    roll_net_20 = pivot_net.rolling(20, min_periods=10).sum()

    # 시점별 top broker (60일 net 최대)
    top_60 = roll_net_60.idxmax(axis=1)
    out["top_broker_60d"] = top_60

    def _pick(matrix: pd.DataFrame, brokers: pd.Series) -> pd.Series:
        # 각 행에서 brokers[date]가 가리키는 컬럼 값 추출
        vals = []
        for d, b in brokers.items():
            if pd.isna(b) or b not in matrix.columns:
                vals.append(np.nan)
            else:
                vals.append(matrix.at[d, b])
        return pd.Series(vals, index=brokers.index)

    # top broker의 60일 매수 점유율
    total_buy_60 = roll_buy_60.sum(axis=1)
    top_buy_60   = _pick(roll_buy_60, top_60)
    out["top_broker_share_60d"] = (top_buy_60 / total_buy_60).fillna(0)

    # top broker의 5/20일 net flow
    top_net_5  = _pick(roll_net_5,  top_60)
    top_net_20 = _pick(roll_net_20, top_60)
    out["top_broker_net_5d"]  = top_net_5
    out["top_broker_net_20d"] = top_net_20
    out["top_broker_reversed"] = (top_net_5 < 0) & (top_net_20 > 0)

    # HHI — 매수 집중도 (시장점유율 제곱합)
    share_60 = roll_buy_60.div(total_buy_60.replace(0, np.nan), axis=0).fillna(0)
    out["buy_concentration_60d"] = (share_60 ** 2).sum(axis=1)

    # 빅데이 — 일별 max net이 60일 평균+2σ 이상인 날
    daily_max_net = pivot_net.max(axis=1)
    rolling_mean = daily_max_net.rolling(60, min_periods=20).mean()
    rolling_std  = daily_max_net.rolling(60, min_periods=20).std()
    big_day = daily_max_net > (rolling_mean + 2 * rolling_std)

    out["big_day_count_20d"] = big_day.rolling(20).sum()
    # last big day distance
    last_idx = big_day.index.to_series()
    last_big_day = last_idx.where(big_day).ffill()
    days_since = (last_idx - last_big_day).dt.days
    out["big_day_recency"] = days_since

    return out
