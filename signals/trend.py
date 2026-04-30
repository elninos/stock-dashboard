"""추세 분석: MA(5/20/60), MACD(12,26,9), RSI(14) — pykrx 기반."""
import time
from datetime import datetime, timedelta


def _get_ohlcv(code: str, days: int = 120):
    from pykrx import stock as krx
    to = datetime.now()
    frm = to - timedelta(days=days)
    df = krx.get_market_ohlcv_by_date(frm.strftime("%Y%m%d"), to.strftime("%Y%m%d"), code)
    return df


def calc_ma(series, window):
    return series.rolling(window).mean()


def calc_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("inf"))
    return 100 - (100 / (1 + rs))


def analyze_trend(name: str, code: str) -> dict:
    """종목 추세 분석. 반환: sub_score(0~6), 세부 지표."""
    try:
        df = _get_ohlcv(code, days=120)
        if df is None or len(df) < 30:
            return {"error": "데이터 부족", "sub_score": 0}

        close = df["종가"].astype(float)

        ma5  = calc_ma(close, 5).iloc[-1]
        ma20 = calc_ma(close, 20).iloc[-1]
        ma60 = calc_ma(close, 60).iloc[-1] if len(close) >= 60 else None
        price = close.iloc[-1]

        _, _, hist = calc_macd(close)
        macd_hist_now  = hist.iloc[-1]
        macd_hist_prev = hist.iloc[-2]

        rsi = calc_rsi(close).iloc[-1]

        # ── 점수화 ──────────────────────────────────────────
        score = 0
        reasons = []

        # MA5 vs MA20
        if ma5 < ma20:
            gap_pct = (ma20 - ma5) / ma20 * 100
            if gap_pct > 3:
                score += 2
                reasons.append(f"MA5 데드크로스 ({gap_pct:.1f}%)")
            else:
                score += 1
                reasons.append("MA5 MA20 근접 하향")

        # MA20 vs MA60
        if ma60 is not None and ma20 < ma60:
            score += 1
            reasons.append("MA20 < MA60")

        # MACD 히스토그램 음전환 또는 감소
        if macd_hist_now < 0:
            score += 2
            reasons.append(f"MACD 히스토그램 음수 ({macd_hist_now:.0f})")
        elif macd_hist_now < macd_hist_prev:
            score += 1
            reasons.append("MACD 히스토그램 감소")

        # RSI
        if rsi > 75:
            score += 2
            reasons.append(f"RSI 과열 ({rsi:.1f})")
        elif rsi > 65:
            score += 1
            reasons.append(f"RSI 주의 ({rsi:.1f})")

        return {
            "price": int(price),
            "ma5": round(ma5),
            "ma20": round(ma20),
            "ma60": round(ma60) if ma60 else None,
            "macd_hist": round(macd_hist_now, 1),
            "rsi": round(rsi, 1),
            "sub_score": min(score, 6),
            "reasons": reasons,
        }

    except Exception as e:
        return {"error": str(e), "sub_score": 0, "reasons": []}
