"""가격-거래량 기반 매집/분배 시그널.

OBV (On-Balance Volume), Chaikin Money Flow, Money Flow Index.
모두 OHLCV로만 계산 가능 — 외부 API 필요 없음.

핵심 개념: 가격과 거래량의 다이버전스로 숨겨진 매집/분배 탐지.
  - 가격 횡보/하락 + OBV ↑ = 매집 중 (스마트머니 매수)
  - 가격 상승 + OBV ↓ = 분배 중 (스마트머니 이탈)
"""
import warnings
warnings.filterwarnings("ignore")


def compute_obv(df) -> "pd.Series":
    """On-Balance Volume.

    OBV[t] = OBV[t-1] + sign(close[t] - close[t-1]) × volume[t]
    누적 거래량 — 매집/분배 추세 시각화.
    """
    import numpy as np
    sign = np.sign(df["close"].diff().fillna(0))
    return (sign * df["volume"]).cumsum()


def compute_cmf(df, period: int = 20) -> "pd.Series":
    """Chaikin Money Flow.

    범위: -1 ~ +1
      +0.25 이상: 강한 매집
      -0.25 이하: 강한 분배
      0 근처: 중립

    Money Flow Multiplier = ((close - low) - (high - close)) / (high - low)
    Money Flow Volume = MFM × volume
    CMF = sum(MFV, period) / sum(volume, period)
    """
    high, low, close, vol = df["high"], df["low"], df["close"], df["volume"]
    rng = (high - low).replace(0, float("nan"))
    mfm = ((close - low) - (high - close)) / rng
    mfv = mfm * vol
    cmf = mfv.rolling(period).sum() / vol.rolling(period).sum()
    return cmf.round(3)


def compute_mfi(df, period: int = 14) -> "pd.Series":
    """Money Flow Index — RSI의 거래량 가중 버전.

    범위: 0 ~ 100
      80 이상: 과매수 (분배 가능)
      20 이하: 과매도 (매집 가능)
    """
    import numpy as np
    typical = (df["high"] + df["low"] + df["close"]) / 3
    rmf = typical * df["volume"]
    delta = typical.diff()
    pos_flow = rmf.where(delta > 0, 0).rolling(period).sum()
    neg_flow = rmf.where(delta < 0, 0).rolling(period).sum().abs()
    mfr = pos_flow / neg_flow.replace(0, float("nan"))
    mfi = 100 - (100 / (1 + mfr))
    return mfi.round(1)


def detect_obv_divergence(df, lookback: int = 20):
    """OBV 다이버전스 탐지.

    Bearish divergence (분배):
      - 가격이 lookback 기간 신고가 (또는 근처)
      - OBV는 같은 기간 신고가 도달 못 함
      - = "가격 ↑ but 거래량 누적은 ↓" → 약한 상승, 분배 의심

    Bullish divergence (매집):
      - 가격이 lookback 기간 신저가 (또는 근처)
      - OBV는 신저가 미달 (덜 빠짐)
      - = "가격 ↓ but 거래량 누적은 유지" → 약한 하락, 매집 중

    반환: (bearish_series, bullish_series) — 둘 다 bool Series
    """
    price_high  = df["close"].rolling(lookback).max()
    price_low   = df["close"].rolling(lookback).min()
    obv_high    = df["obv"].rolling(lookback).max()
    obv_low     = df["obv"].rolling(lookback).min()

    # 가격 신고가 근접 (95% 이상) but OBV는 신고가 미달 (95% 미만)
    bearish = (
        (df["close"] >= price_high * 0.97) &
        (df["obv"] < obv_high * 0.95)
    ).fillna(False)

    # 가격 신저가 근접 (5% 이내) but OBV는 신저가 위 (5% 위)
    bullish = (
        (df["close"] <= price_low * 1.03) &
        (df["obv"] > obv_low * 1.05)
    ).fillna(False)

    return bearish, bullish


def add_price_volume_signals(df) -> "pd.DataFrame":
    """기존 timeseries DataFrame에 OBV/CMF/MFI 컬럼 추가.

    필요 컬럼: close, high, low, volume
    추가 컬럼: obv, obv_ma20, cmf, mfi, obv_diverg_bear, obv_diverg_bull
    """
    if not all(c in df.columns for c in ("close", "volume")):
        return df

    # OHLC 없으면 close로 대체 (OBV는 close만 있으면 됨)
    if "high" not in df.columns:
        df["high"] = df["close"]
    if "low" not in df.columns:
        df["low"] = df["close"]

    df["obv"]      = compute_obv(df)
    df["obv_ma20"] = df["obv"].rolling(20).mean()
    df["cmf"]      = compute_cmf(df)
    df["mfi"]      = compute_mfi(df)

    # 다이버전스
    bear, bull = detect_obv_divergence(df, lookback=20)
    df["obv_diverg_bear"] = bear.astype(int)
    df["obv_diverg_bull"] = bull.astype(int)

    return df


# ────────────────────────────────────────────────
# 시그널 임계값
# ────────────────────────────────────────────────
CMF_DIST_THRESH       = -0.10   # CMF 누적 -0.10 이하 = 분배
CMF_ACCUM_THRESH      = +0.10   # CMF 누적 +0.10 이상 = 매집
MFI_OVERBOUGHT        = 80
MFI_OVERSOLD          = 20


def detect_pv_signals(df) -> dict:
    """가격-거래량 기반 시그널 종합.

    반환: {
      'obv_bear_dates': [...], 'obv_bull_dates': [...],
      'cmf_dist_dates': [...], 'cmf_accum_dates': [...],
      'mfi_overbought_dates': [...], 'mfi_oversold_dates': [...],
    }
    """
    if "obv" not in df.columns:
        df = add_price_volume_signals(df)

    obv_bear = df.index[df["obv_diverg_bear"] == 1].strftime("%Y-%m-%d").tolist()
    obv_bull = df.index[df["obv_diverg_bull"] == 1].strftime("%Y-%m-%d").tolist()
    cmf_dist  = df.index[df["cmf"] <= CMF_DIST_THRESH].strftime("%Y-%m-%d").tolist()
    cmf_accum = df.index[df["cmf"] >= CMF_ACCUM_THRESH].strftime("%Y-%m-%d").tolist()
    mfi_ob = df.index[df["mfi"] >= MFI_OVERBOUGHT].strftime("%Y-%m-%d").tolist()
    mfi_os = df.index[df["mfi"] <= MFI_OVERSOLD].strftime("%Y-%m-%d").tolist()

    return {
        "obv_bear_dates":   obv_bear,
        "obv_bull_dates":   obv_bull,
        "cmf_dist_dates":   cmf_dist,
        "cmf_accum_dates":  cmf_accum,
        "mfi_overbought_dates": mfi_ob,
        "mfi_oversold_dates":   mfi_os,
    }
