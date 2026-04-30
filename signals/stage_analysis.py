"""Stan Weinstein's Stage Analysis.

종목을 4단계로 분류:
  Stage 1 (Basing)     — 횡보, 매집 진행 중
  Stage 2 (Advancing)  — 상승 추세, HOLD
  Stage 3 (Topping)    — 정점 형성, 매도 시작
  Stage 4 (Declining)  — 하락 추세, 청산

기준: 30주 이동평균선 (≈ 150 거래일)
판정:
  종가 vs 30주 MA 거리 (distance)
  30주 MA 기울기 (20일 변화율)
"""
import warnings
warnings.filterwarnings("ignore")


def classify_stages(df, weeks: int = 30, slope_period: int = 20):
    """일별 Stage 분류 컬럼 추가.

    필요 컬럼: close
    추가 컬럼: ma30w, ma30w_slope, distance, stage
    """
    import pandas as pd
    import numpy as np

    df = df.copy()
    days = weeks * 5  # 30주 = 150 거래일
    df["ma30w"] = df["close"].rolling(days).mean()
    df["ma30w_slope"] = (df["ma30w"] / df["ma30w"].shift(slope_period) - 1) * 100
    df["distance"] = (df["close"] / df["ma30w"] - 1) * 100

    # Stage 분류 (완화된 임계값)
    def _stage(row, prev_stage):
        slope = row["ma30w_slope"]
        dist = row["distance"]
        if pd.isna(slope) or pd.isna(dist):
            return 0  # 데이터 부족

        # Stage 2: 상승추세 (가격이 MA 위 OR 기울기 양수)
        if slope > 0 and dist > 0:
            return 2
        if slope > 2:  # 강한 상승은 거리 무관
            return 2
        # Stage 4: 하락추세
        if slope < -1 and dist < -3:
            return 4
        # Stage 3: 토핑 (가격이 MA 근처 + 기울기 약화, 이전이 Stage 2)
        if dist <= 8 and slope < 0 and prev_stage == 2:
            return 3
        # Stage 1: 베이싱 (이전이 Stage 4 후)
        if -8 <= dist <= 3 and slope > -1 and prev_stage in (4, 1):
            return 1
        # 기본: 직전 stage 유지 (강세 중 잠시 횡보면 Stage 2 유지)
        if prev_stage == 2 and slope > -1 and dist > -3:
            return 2
        return 0  # 진짜 판정 불가만 0

    stages = []
    prev = 0
    for _, row in df.iterrows():
        s = _stage(row, prev)
        stages.append(s)
        if s != 0:
            prev = s
    df["stage"] = stages
    return df


def stage_label(stage: int) -> str:
    return {
        0: "중립",
        1: "Stage 1 — 베이싱 (관심)",
        2: "Stage 2 — 상승 추세 (HOLD)",
        3: "Stage 3 — 토핑 (매도 시작)",
        4: "Stage 4 — 하락 추세 (청산)",
    }.get(stage, "?")


def stage_action(stage: int) -> str:
    """Stage별 기본 행동 권고."""
    return {
        0: "데이터 부족",
        1: "관심 (Stage 2 진입 대기)",
        2: "HOLD (시그널 무시, Chandelier Exit만)",
        3: "매도 시그널 활성화",
        4: "전량 청산",
    }.get(stage, "?")
