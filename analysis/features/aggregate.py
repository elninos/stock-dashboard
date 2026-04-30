"""모든 feature 서비스 호출 → 단일 DataFrame.

Per-stock 파이프라인의 입력. 각 모듈은 독립 호출 가능.
누락 데이터(예: 거래원 1년 한계)는 그냥 NaN으로 둠 — 분석 단계에서 처리.
"""
import pandas as pd
from . import price, investor, broker, short


def get_all_features(code: str, start: str = None, end: str = None) -> pd.DataFrame:
    p = price.get_features(code, start, end)
    if p.empty:
        return pd.DataFrame()

    i = investor.get_features(code, start, end)
    b = broker.get_features(code, start, end)
    s = short.get_features(code, start, end)

    # price를 인덱스 기준으로 left join
    df = p
    for other, prefix in [(i, "inv"), (b, "brk"), (s, "sht")]:
        if other is None or other.empty:
            continue
        df = df.join(other.add_prefix(f"{prefix}_") if False else other, how="left")
    return df
