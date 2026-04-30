"""고점 판별 전용 시그널 시스템 (Top Detection).

핵심 아이디어:
  추세 중간엔 시그널 무시. 신고가권에서만 발동.

이 시스템이 답하려는 질문:
  "지금이 고점이라는 증거가 얼마나 많은가?"

대상:
  파마리서치 71만원, 대한광통신 1,027원 같은 단기 고점을 사전에 잡는 것.

신호 종류 (고점 특화):
  1. 신고가권 진입 — 60일 신고가 90%↑ OR ATH 95%↑ (필수 조건)
  2. Bearish Divergence — 가격 신고가 + 수급 음수
  3. Smart Money Reversal — 외인+기관 양→음 전환
  4. Distribution Pattern — 개미 매수 + 대형기관 매도
  5. Failed Breakout — 신고가 갱신 후 -3% 이상 하락
  6. Volume Climax — 거래량 폭증 + 음봉/위꼬리
  7. Distribution Days — 4주 5+ 누적
  8. Dow Theory LH — 첫 Lower High
  9. OBV Divergence — 가격 신고가 but OBV 정체

발동 조건:
  ① 신고가권 (60일 90% OR ATH 95%) 충족 시에만 점수 계산
  ② 점수 ≥ 5 → 고점 시그널
  ③ 점수 ≥ 8 → 강한 고점 시그널
"""
import warnings
warnings.filterwarnings("ignore")


# 고점 정의
NEAR_HIGH_60D_THRESHOLD = 0.90    # 60일 신고가의 90% 이상
NEAR_ATH_THRESHOLD       = 0.95    # 절대 신고가의 95% 이상

# 시그널 가중치 (고점에 특화)
WEIGHT_BEARISH_DIVERG    = 4       # 가장 강한 시그널
WEIGHT_SMART_REVERSAL    = 3       # 양→음 전환
WEIGHT_SMART_ACCEL       = 2       # 5일 가속화
WEIGHT_DISTRIBUTION      = 3       # 개미↔대형기관
WEIGHT_FAILED_BREAKOUT   = 3
WEIGHT_VOLUME_CLIMAX     = 2
WEIGHT_DIST_DAYS         = 2       # 4주 5+
WEIGHT_DOW_LH            = 1.5
WEIGHT_OBV_DIVERG        = 2       # 단발 OBV 분배

# ── 강화 시그널 (지속/누적 패턴) ─────────────────
WEIGHT_OBV_DIVERG_CLUSTER = 5      # OBV 분배 5일 내 3회+ — 가장 강력
WEIGHT_OBV_DIVERG_HEAVY   = 7      # OBV 분배 10일 내 5회+
WEIGHT_CMF_PERSISTENT     = 4      # CMF -0.15↓ 7일 연속
WEIGHT_CMF_HEAVY          = 6      # CMF -0.20↓ 14일 연속
WEIGHT_MFI_PERSISTENT     = 3      # MFI 80+ 5일 연속
WEIGHT_MFI_HEAVY          = 4      # MFI 80+ 10일 연속


def add_top_detection(df, smart_money_col=None):
    """고점 판별 컬럼 추가.

    필요 컬럼: close, high, volume, ATR, MA, OBV, MFI, CMF, dow_lh, ...
    smart_money_col: 외인+기관 비율 컬럼명 (없으면 OBV/거래량으로 대체)

    추가 컬럼:
      ath              : 절대 신고가
      high60           : 60일 신고가
      near_high60      : 60일 신고가 90%↑ (bool)
      near_ath         : ATH 95%↑ (bool)
      in_top_zone      : 고점권 진입 (둘 중 하나라도)
      top_score        : 종합 점수 (고점권에서만 의미)
      top_grade        : '경고' / '주의' / '강한경고'
    """
    import pandas as pd
    import numpy as np

    df = df.copy()

    # 1. 신고가 위치
    df["ath"] = df["close"].cummax()
    df["high60"] = df["close"].rolling(60).max()
    df["near_high60_pct"] = df["close"] / df["high60"]
    df["near_ath_pct"] = df["close"] / df["ath"]
    df["near_high60"] = (df["near_high60_pct"] >= NEAR_HIGH_60D_THRESHOLD)
    df["near_ath"] = (df["near_ath_pct"] >= NEAR_ATH_THRESHOLD)
    df["in_top_zone"] = df["near_high60"] | df["near_ath"]

    # ── 강화 시그널: 누적/지속 패턴 ─────────────
    # OBV 분배 5/10일 내 누적 카운트
    if "obv_diverg_bear" in df.columns:
        df["obv_bear_5d"] = df["obv_diverg_bear"].rolling(5).sum()
        df["obv_bear_10d"] = df["obv_diverg_bear"].rolling(10).sum()

    # CMF 지속: 음수 영역 연속 일수
    if "cmf" in df.columns:
        cmf_below_15 = (df["cmf"] <= -0.15).astype(int)
        cmf_below_20 = (df["cmf"] <= -0.20).astype(int)
        # 연속 카운트
        def streak(s):
            count = 0
            result = []
            for v in s:
                if v == 1:
                    count += 1
                else:
                    count = 0
                result.append(count)
            return pd.Series(result, index=s.index)
        df["cmf_neg15_streak"] = streak(cmf_below_15)
        df["cmf_neg20_streak"] = streak(cmf_below_20)

    # MFI 지속: 80+ 연속 일수
    if "mfi" in df.columns:
        mfi_high = (df["mfi"] >= 80).astype(int)
        df["mfi_high_streak"] = streak(mfi_high) if "cmf" in df.columns else (mfi_high * 0)

    # 2. 고점 점수 계산 (in_top_zone일 때만)
    score = pd.Series(0.0, index=df.index)
    reasons_list = [[] for _ in range(len(df))]

    # ── (1) Bearish Divergence — 가격 신고가 + smart money 음수
    if smart_money_col and smart_money_col in df.columns:
        sm = df[smart_money_col]
        bear_div = df["near_high60"] & (sm < 0)
        for i, b in enumerate(bear_div):
            if b:
                score.iat[i] += WEIGHT_BEARISH_DIVERG
                reasons_list[i].append(f"베어리시 다이버전스 (스마트머니 {sm.iat[i]:+.1f}%)")

    # OBV 다이버전스 (단발 + 누적 강화)
    if "obv_diverg_bear" in df.columns:
        for i in range(len(df)):
            if not df["in_top_zone"].iat[i]:
                continue
            # 단발 OBV
            if df["obv_diverg_bear"].iat[i] == 1:
                score.iat[i] += WEIGHT_OBV_DIVERG
                reasons_list[i].append("OBV 분배 다이버전스")
            # 누적 5일 내 3회+
            if "obv_bear_5d" in df.columns and df["obv_bear_5d"].iat[i] >= 3:
                score.iat[i] += WEIGHT_OBV_DIVERG_CLUSTER
                reasons_list[i].append(f"⚡ OBV 분배 클러스터 (5일내 {int(df['obv_bear_5d'].iat[i])}회)")
            # 누적 10일 내 5회+ (매우 강한 신호)
            if "obv_bear_10d" in df.columns and df["obv_bear_10d"].iat[i] >= 5:
                score.iat[i] += WEIGHT_OBV_DIVERG_HEAVY
                reasons_list[i].append(f"🔥 OBV 분배 폭주 (10일내 {int(df['obv_bear_10d'].iat[i])}회)")

    # CMF 지속 (자금 강한 유출)
    if "cmf_neg15_streak" in df.columns:
        for i in range(len(df)):
            if not df["in_top_zone"].iat[i]:
                continue
            streak15 = df["cmf_neg15_streak"].iat[i]
            streak20 = df["cmf_neg20_streak"].iat[i]
            # 가장 강한 것 하나만 점수 추가
            if streak20 >= 14:
                score.iat[i] += WEIGHT_CMF_HEAVY
                reasons_list[i].append(f"🔥 CMF 강한 분배 ({int(streak20)}일 연속 -0.20↓)")
            elif streak15 >= 7:
                score.iat[i] += WEIGHT_CMF_PERSISTENT
                reasons_list[i].append(f"⚡ CMF 지속 분배 ({int(streak15)}일 연속 -0.15↓)")

    # MFI 지속 (과매수 함정)
    if "mfi_high_streak" in df.columns:
        for i in range(len(df)):
            if not df["in_top_zone"].iat[i]:
                continue
            streak = df["mfi_high_streak"].iat[i]
            if streak >= 10:
                score.iat[i] += WEIGHT_MFI_HEAVY
                reasons_list[i].append(f"🔥 MFI 극단 과열 ({int(streak)}일 연속 80+)")
            elif streak >= 5:
                score.iat[i] += WEIGHT_MFI_PERSISTENT
                reasons_list[i].append(f"⚡ MFI 지속 과매수 ({int(streak)}일 연속 80+)")

    # ── (2) Smart Money Reversal — 양→음 전환
    if smart_money_col and smart_money_col in df.columns:
        sm = df[smart_money_col]
        for i in range(1, len(df)):
            if df["in_top_zone"].iat[i] and sm.iat[i-1] > 0 and sm.iat[i] < 0:
                score.iat[i] += WEIGHT_SMART_REVERSAL
                reasons_list[i].append(f"스마트머니 양→음 전환 ({sm.iat[i-1]:+.1f}→{sm.iat[i]:+.1f}%)")

    # ── (3) Distribution Pattern (개미 매수 + 대형기관 매도)
    if "retail_ratio_5d" in df.columns and "large_inst_ratio_5d" in df.columns:
        rr = df["retail_ratio_5d"]
        li = df["large_inst_ratio_5d"]
        for i in range(len(df)):
            if df["in_top_zone"].iat[i] and rr.iat[i] >= 5 and li.iat[i] <= -3:
                score.iat[i] += WEIGHT_DISTRIBUTION
                reasons_list[i].append(f"분배 패턴 (개미 +{rr.iat[i]:.1f}% ↔ 대형기관 {li.iat[i]:+.1f}%)")

    # ── (4) Failed Breakout
    if "is_failed_breakout" in df.columns:
        for i in range(len(df)):
            if df["in_top_zone"].iat[i] and df["is_failed_breakout"].iat[i] == 1:
                score.iat[i] += WEIGHT_FAILED_BREAKOUT
                reasons_list[i].append("Failed Breakout")

    # ── (5) Volume Climax
    if "is_volume_climax" in df.columns:
        for i in range(len(df)):
            if df["in_top_zone"].iat[i] and df["is_volume_climax"].iat[i] == 1:
                score.iat[i] += WEIGHT_VOLUME_CLIMAX
                reasons_list[i].append("Volume Climax")

    # ── (6) Distribution Days 누적
    if "distribution_count_4w" in df.columns:
        for i in range(len(df)):
            if df["in_top_zone"].iat[i] and df["distribution_count_4w"].iat[i] >= 5:
                score.iat[i] += WEIGHT_DIST_DAYS
                reasons_list[i].append(f"분배일 4주 {int(df['distribution_count_4w'].iat[i])}건")

    # ── (7) Dow Theory LH
    if "dow_lh" in df.columns:
        for i in range(len(df)):
            if df["in_top_zone"].iat[i] and df["dow_lh"].iat[i] == 1:
                score.iat[i] += WEIGHT_DOW_LH
                reasons_list[i].append("Dow Theory LH (Lower High)")

    # ── (8) MFI 과매수 후 하락
    if "mfi" in df.columns:
        mfi = df["mfi"]
        for i in range(1, len(df)):
            if (df["in_top_zone"].iat[i] and
                mfi.iat[i-1] >= 80 and mfi.iat[i] < 75):
                score.iat[i] += 1
                reasons_list[i].append(f"MFI {mfi.iat[i-1]:.0f}→{mfi.iat[i]:.0f}")

    # ── (9) CMF 분배 진입
    if "cmf" in df.columns:
        cmf = df["cmf"]
        for i in range(1, len(df)):
            if (df["in_top_zone"].iat[i] and
                cmf.iat[i-1] > -0.10 and cmf.iat[i] <= -0.10):
                score.iat[i] += 1
                reasons_list[i].append(f"CMF 분배 진입 ({cmf.iat[i]:+.2f})")

    df["top_score"] = score.clip(0, 15)
    df["top_reasons"] = reasons_list

    # 등급
    grades = []
    for s in df["top_score"]:
        if s >= 8: grades.append("강한경고")
        elif s >= 5: grades.append("경고")
        elif s >= 3: grades.append("주의")
        else: grades.append("")
    df["top_grade"] = grades

    return df


def find_top_signals(df, min_score: float = 3) -> list:
    """고점 시그널 발동일 추출."""
    if "top_score" not in df.columns:
        return []

    out = []
    idx_obj = df.index
    is_idx_dt = hasattr(idx_obj, "strftime")

    for i in range(len(df)):
        s = df["top_score"].iat[i]
        if s >= min_score:
            idx_val = idx_obj[i]
            date_str = idx_val.strftime("%Y-%m-%d") if is_idx_dt else str(idx_val)
            out.append({
                "date": date_str,
                "close": float(df["close"].iat[i]),
                "score": float(s),
                "grade": df["top_grade"].iat[i],
                "near_high60_pct": float(df["near_high60_pct"].iat[i]),
                "near_ath_pct": float(df["near_ath_pct"].iat[i]),
                "reasons": df["top_reasons"].iat[i],
            })
    return out
