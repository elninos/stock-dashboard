"""단기 수급 분석 — 당일 실시간 + 1~5일 창구 추적 + 기술적 지표 + 종합 판단.

inquire-member (FHKST01010600): 당일 창구별 매수/매도 TOP 5 스냅샷
inquire-investor (FHKST01010900): 최근 5일 투자자별 순매수
member_daily DB: 최근 5거래일 창구별 일별 흐름
prices DB: RSI / MACD / BB / 이동평균 계산
"""
import os, sys
import numpy as np
import pandas as pd
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.db import query_df
from signals.kis_api import get_client, rate_limit


# ─────────────────────────────────────────────────────
#  API helpers
# ─────────────────────────────────────────────────────

def _snapshot(code: str) -> dict:
    client = get_client(); rate_limit()
    res = client.get(
        "/uapi/domestic-stock/v1/quotations/inquire-member",
        tr_id="FHKST01010600",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
    )
    if res.get("rt_cd") != "0": return {}
    out = res.get("output", [])
    return (out[0] if isinstance(out, list) else out) if out else {}


def _investor_recent(code: str, days: int = 5) -> list:
    client = get_client(); rate_limit()
    res = client.get(
        "/uapi/domestic-stock/v1/quotations/inquire-investor",
        tr_id="FHKST01010900",
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
    )
    if res.get("rt_cd") != "0": return []
    result = []
    for r in res.get("output", [])[:days + 1]:
        frgn = r.get("frgn_ntby_qty", ""); orgn = r.get("orgn_ntby_qty", ""); prsn = r.get("prsn_ntby_qty", "")
        result.append({
            "date":  r.get("stck_bsop_date", ""),
            "close": int(r.get("stck_clpr", 0) or 0),
            "frgn":  int(frgn) if frgn else None,
            "orgn":  int(orgn) if orgn else None,
            "prsn":  int(prsn) if prsn else None,
        })
    return result


# ─────────────────────────────────────────────────────
#  공매도 / 신용잔고
# ─────────────────────────────────────────────────────

def _short_data(code: str) -> dict:
    """공매도 잔고(DB) + 신용잔고(KIS API) 수집."""
    df = query_df("""
        SELECT date, close, short_vol, short_ratio, short_balance_qty, short_balance_pct
        FROM short_balance WHERE code=? ORDER BY date DESC LIMIT 22
    """, (code,))
    if df.empty:
        return {}

    latest = df.iloc[0]
    short_ratio_today = float(latest.get("short_ratio", 0) or 0)
    short_bal_now     = int(latest.get("short_balance_qty", 0) or 0)
    short_bal_pct     = float(latest.get("short_balance_pct", 0) or 0)
    avg_ratio_5d      = float(df.head(5)["short_ratio"].mean())

    idx5  = min(5,  len(df) - 1)
    idx20 = min(20, len(df) - 1)
    bal_5d  = int(df.iloc[idx5]["short_balance_qty"]  or 0)
    bal_20d = int(df.iloc[idx20]["short_balance_qty"] or 0)
    chg_5d  = (short_bal_now - bal_5d)  / bal_5d  * 100 if bal_5d  > 0 else 0.0
    chg_20d = (short_bal_now - bal_20d) / bal_20d * 100 if bal_20d > 0 else 0.0

    out = dict(
        df=df,
        short_ratio_today=short_ratio_today,
        short_bal_now=short_bal_now,
        short_bal_pct=short_bal_pct,
        avg_ratio_5d=avg_ratio_5d,
        chg_5d=chg_5d, chg_20d=chg_20d,
        credit_balance=None, credit_pct=None, credit_chg_5d=None,
    )

    # 신용잔고 KIS API (실패 시 조용히 스킵)
    try:
        from datetime import timedelta
        from signals.kis_credit_program import fetch_daily_credit_balance
        end   = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=40)).strftime("%Y%m%d")
        credit = fetch_daily_credit_balance(code, start, end)
        if credit:
            c0 = credit[0]
            cb_now = int(c0.get("credit_balance") or 0)
            out["credit_balance"] = cb_now
            out["credit_pct"]     = float(c0.get("credit_pct") or 0)
            if len(credit) >= 5:
                cb_5d = int(credit[min(5, len(credit)-1)].get("credit_balance") or 0)
                if cb_5d > 0:
                    out["credit_chg_5d"] = (cb_now - cb_5d) / cb_5d * 100
    except Exception:
        pass

    return out


def _print_short(sd: dict) -> None:
    """공매도 & 신용잔고 섹션 출력."""
    if not sd:
        print("\n▣ 공매도 & 신용잔고  —  데이터 없음")
        return

    print(f"\n▣ 공매도 & 신용잔고")

    # 공매도 요약
    bal_pct  = sd["short_bal_pct"]
    chg_5d   = sd["chg_5d"]
    chg_20d  = sd["chg_20d"]

    if bal_pct >= 10:   bal_lv = "🔴 매우 높음"
    elif bal_pct >= 5:  bal_lv = "⚠️ 높음"
    elif bal_pct >= 2:  bal_lv = "🟡 중간"
    else:               bal_lv = "✅ 낮음"

    print(f"\n  [공매도]")
    print(f"  당일 비중 {sd['short_ratio_today']:.2f}%  |  5일 평균 {sd['avg_ratio_5d']:.2f}%")
    print(f"  잔고 {sd['short_bal_now']:,}주  |  잔고율 {bal_pct:.2f}%  {bal_lv}")
    print(f"  5일 잔고 변화 {chg_5d:+.1f}%  |  20일 잔고 변화 {chg_20d:+.1f}%")

    if chg_5d >= 30:
        print(f"  → ⚠️ 공매도 잔고 5일 {chg_5d:+.0f}% 급증 — 공매도 세력 진입 가능성")
    elif chg_5d <= -20:
        print(f"  → 🟢 공매도 잔고 5일 {chg_5d:.0f}% 급감 — 숏커버링 발생, 단기 반등 촉매")
    elif chg_5d >= 10:
        print(f"  → 공매도 잔고 소폭 증가 추이 — 하방 베팅 점진적 확대")

    # 일별 테이블
    df = sd.get("df")
    if df is not None and not df.empty:
        print(f"\n  {'날짜':10s}  {'종가':>8s}  {'공매도비중':>8s}  {'잔고(주)':>12s}  {'잔고율%':>7s}")
        print(f"  {'-'*55}")
        for _, row in df.head(10).iterrows():
            print(f"  {row['date']:10s}  {int(row['close'] or 0):>8,}"
                  f"  {float(row['short_ratio'] or 0):>7.2f}%"
                  f"  {int(row['short_balance_qty'] or 0):>12,}"
                  f"  {float(row['short_balance_pct'] or 0):>6.2f}%")

    # 신용잔고
    print(f"\n  [신용잔고]")
    if sd.get("credit_balance") is not None:
        cb  = sd["credit_balance"]
        pct = sd.get("credit_pct") or 0
        print(f"  잔고 {cb:,}주  ({pct:.2f}%)")
        chg = sd.get("credit_chg_5d")
        if chg is not None:
            if chg >= 20:
                print(f"  → ⚠️ 5일 {chg:+.0f}% 급증 — 개인 레버리지 과열 (역지표)")
            elif chg <= -20:
                print(f"  → 5일 {chg:.0f}% 감소 — 신용 레버리지 해소 중")
            else:
                print(f"  → 5일 변화 {chg:+.1f}%")
    else:
        print(f"  실시간 조회 불가 — KIS API FHPST04760000")

    print(f"\n  ※ 대차거래 잔고: pykrx get_shorting_balance_by_date() 별도 조회 가능")


# ─────────────────────────────────────────────────────
#  Technical indicators (계산만, 프린트 없음)
# ─────────────────────────────────────────────────────

def _load_ohlcv(code: str, n: int = 120) -> pd.DataFrame:
    df = query_df(
        "SELECT date, open, high, low, close, volume FROM prices WHERE code=? ORDER BY date DESC LIMIT ?",
        (code, n),
    )
    return df[::-1].reset_index(drop=True) if not df.empty else df


def _rsi(close: pd.Series, period: int = 14) -> float:
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def _macd(close: pd.Series) -> tuple:
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    sig   = macd.ewm(span=9, adjust=False).mean()
    hist  = macd - sig
    return float(macd.iloc[-1]), float(sig.iloc[-1]), float(hist.iloc[-1])


def _bollinger(close: pd.Series, period: int = 20) -> tuple:
    mid   = close.rolling(period).mean()
    std   = close.rolling(period).std()
    upper = mid + 2 * std; lower = mid - 2 * std
    pct_b = (close - lower) / (upper - lower)
    return float(upper.iloc[-1]), float(mid.iloc[-1]), float(lower.iloc[-1]), float(pct_b.iloc[-1])


def _calc_technicals(df: pd.DataFrame) -> dict:
    """지표 계산 후 dict 반환 (프린트 없음)."""
    if df.empty or len(df) < 30:
        return {}
    close = df["close"]; volume = df["volume"]
    p = float(close.iloc[-1])

    ma5  = float(close.tail(5).mean())
    ma20 = float(close.tail(20).mean()) if len(close) >= 20 else float("nan")
    ma60 = float(close.tail(60).mean()) if len(close) >= 60 else float("nan")
    rsi  = _rsi(close, 14)
    macd_v, macd_sig, macd_hist = _macd(close)
    macd_hist_prev = _macd(close.iloc[:-1])[2] if len(close) > 27 else macd_hist
    bb_u, bb_m, bb_l, pct_b = _bollinger(close, 20)

    vol_today = float(volume.iloc[-1])
    vol_avg20 = float(volume.iloc[:-1].tail(20).mean())
    vr = vol_today / vol_avg20 if vol_avg20 > 0 else float("nan")

    high_52w = float(close.tail(250).max())
    low_52w  = float(close.tail(250).min())
    high_5d  = float(df["high"].tail(5).max())
    low_5d   = float(df["low"].tail(5).min())

    return dict(
        p=p, ma5=ma5, ma20=ma20, ma60=ma60,
        rsi=rsi, macd_v=macd_v, macd_sig=macd_sig, macd_hist=macd_hist, macd_hist_prev=macd_hist_prev,
        bb_u=bb_u, bb_m=bb_m, bb_l=bb_l, pct_b=pct_b,
        vol_today=int(vol_today), vol_avg20=int(vol_avg20), vr=vr,
        high_52w=high_52w, low_52w=low_52w,
        from_high=(p/high_52w-1)*100, from_low=(p/low_52w-1)*100,
        high_5d=high_5d, low_5d=low_5d,
    )


def _print_technicals(t: dict) -> list:
    """기술적 지표 섹션 출력 + 신호 리스트 반환."""
    if not t:
        print("\n  가격 데이터 부족 — 지표 계산 불가")
        return []

    p = t['p']

    print(f"\n▣ 기술적 지표 & 코멘트")

    # 가격 & MA
    print(f"\n  [가격 & 이동평균]")
    print(f"  현재가(전일종가) {p:,.0f}원  |  5일 고가 {t['high_5d']:,.0f}  저가 {t['low_5d']:,.0f}")
    for ma_val, label in [(t['ma5'], "MA5 "), (t['ma20'], "MA20"), (t['ma60'], "MA60")]:
        if np.isnan(ma_val): continue
        pct = (p / ma_val - 1) * 100
        flag = "✅" if p >= ma_val else "⚠️"
        print(f"  {flag} {label} {ma_val:,.0f}  ({'위' if p>=ma_val else '아래'}  {pct:+.1f}%)")
    if not any(np.isnan(x) for x in [t['ma5'], t['ma20'], t['ma60']]):
        if t['ma5'] > t['ma20'] > t['ma60']:   print("  → 단기/중기/장기 정배열 — 상승 추세")
        elif t['ma5'] < t['ma20'] < t['ma60']: print("  → 역배열 — 하락 추세")
        elif p < t['ma20']:                     print("  → MA20 하회 — 단기 추세 이탈")

    # 거래량
    print(f"\n  [거래량]")
    vr = t['vr']
    if not np.isnan(vr):
        bar = '█' * min(15, max(1, int(vr * 5)))
        if vr >= 2.0:   vr_c = "🔥 거래량 폭발 — 강한 방향성"
        elif vr >= 1.5: vr_c = "📈 평균 이상 — 수급 관심 증가"
        elif vr >= 0.8: vr_c = "→ 평균 수준"
        else:           vr_c = "📉 거래량 감소 — 모멘텀 약화"
        print(f"  전일 {t['vol_today']:,}주  |  20일 평균 {t['vol_avg20']:,}주  |  비율 {vr:.2f}x  {bar}")
        print(f"  → {vr_c}")

    # RSI
    rsi = t['rsi']
    print(f"\n  [RSI 14]")
    if not np.isnan(rsi):
        bar = '█' * int(rsi / 10)
        if rsi >= 75:   rsi_c = "🔴 극단적 과매수 — 단기 조정 가능성"
        elif rsi >= 65: rsi_c = "⚠️ 과매수 구간 — 모멘텀 둔화 주시"
        elif rsi <= 25: rsi_c = "🟢 극단적 과매도 — 반등 가능성"
        elif rsi <= 35: rsi_c = "🟡 과매도 구간 — 저점 매수 고려"
        else:           rsi_c = "→ 중립 구간"
        print(f"  RSI {rsi:.1f}  [{bar:<10s}]  {rsi_c}")

    # MACD
    mh = t['macd_hist']; mhp = t['macd_hist_prev']
    print(f"\n  [MACD (12,26,9)]")
    if not any(np.isnan(x) for x in [t['macd_v'], t['macd_sig'], mh]):
        if mh > 0 and mhp <= 0:   macd_c = "🟢 골든크로스 — 상승 전환"
        elif mh < 0 and mhp >= 0: macd_c = "🔴 데드크로스 — 하락 전환"
        elif mh > 0:
            trend = "확대" if mh > mhp else "축소"
            macd_c = f"{'📈' if trend=='확대' else '📉'} 히스토그램 {trend} — {'모멘텀 강화' if trend=='확대' else '모멘텀 둔화'}"
        else:
            trend = "확대" if mh < mhp else "축소"
            macd_c = f"{'📉' if trend=='확대' else '📈'} 하락 히스토그램 {trend}"
        print(f"  MACD {t['macd_v']:+.2f}  시그널 {t['macd_sig']:+.2f}  히스토그램 {mh:+.2f}")
        print(f"  → {macd_c}")

    # 볼린저 밴드
    pct_b = t['pct_b']
    print(f"\n  [볼린저 밴드 (20,2σ)]")
    if not any(np.isnan(x) for x in [t['bb_u'], t['bb_m'], t['bb_l'], pct_b]):
        width_pct = (t['bb_u'] - t['bb_l']) / t['bb_m'] * 100
        if pct_b >= 1.0:   bb_c = "🔴 상단 돌파 — 과열, 되돌림 주의"
        elif pct_b >= 0.8: bb_c = "⚠️ 상단 근접 — 단기 저항"
        elif pct_b <= 0.0: bb_c = "🟢 하단 이탈 — 과매도, 반등 주시"
        elif pct_b <= 0.2: bb_c = "🟡 하단 근접 — 지지 구간"
        else:              bb_c = "→ 밴드 중간 구간"
        print(f"  상단 {t['bb_u']:,.0f}  중간 {t['bb_m']:,.0f}  하단 {t['bb_l']:,.0f}  (%B {pct_b:.2f})")
        print(f"  밴드폭 {width_pct:.1f}%  → {bb_c}")

    # 고저 위치
    print(f"\n  [고저 위치]")
    print(f"  52주 고가 {t['high_52w']:,.0f} ({t['from_high']:+.1f}%)  |  52주 저가 {t['low_52w']:,.0f} ({t['from_low']:+.1f}%)")
    if t['from_high'] >= -5:    print("  → 52주 신고가 근처 — 추가 상승 여력 or 차익실현 구간")
    elif t['from_high'] <= -30: print("  → 고점 대비 -30% 이상 — 낙폭 과대 구간")

    # 기술 신호 수집 (종합용)
    tech_signals = []
    if not np.isnan(rsi):
        if rsi >= 70:   tech_signals.append(("경고", f"RSI {rsi:.0f} 과매수"))
        elif rsi <= 30: tech_signals.append(("긍정", f"RSI {rsi:.0f} 과매도"))
    if not np.isnan(mh) and not np.isnan(mhp):
        if mh > 0 and mhp <= 0:   tech_signals.append(("긍정", "MACD 골든크로스"))
        elif mh < 0 and mhp >= 0: tech_signals.append(("경고", "MACD 데드크로스"))
        elif mh > 0 and mh > mhp: tech_signals.append(("긍정", "MACD 히스토그램 확대"))
        elif mh < 0 and mh < mhp: tech_signals.append(("경고", "MACD 하락 확대"))
    if not np.isnan(pct_b):
        if pct_b >= 0.85:  tech_signals.append(("경고", "BB 상단"))
        elif pct_b <= 0.15: tech_signals.append(("긍정", "BB 하단 지지"))
    if not np.isnan(t['ma20']):
        if p > t['ma20']:  tech_signals.append(("긍정", "MA20 위"))
        else:              tech_signals.append(("경고", "MA20 하회"))
    if not np.isnan(t['ma5']) and not np.isnan(t['ma20']) and not np.isnan(t['ma60']):
        if t['ma5'] > t['ma20'] > t['ma60']:   tech_signals.append(("긍정", "MA 정배열"))
        elif t['ma5'] < t['ma20'] < t['ma60']: tech_signals.append(("경고", "MA 역배열"))
    if not np.isnan(vr):
        if vr >= 2.0:   tech_signals.append(("참고", f"거래량 폭발 {vr:.1f}x"))
        elif vr >= 1.5: tech_signals.append(("참고", f"거래량 증가 {vr:.1f}x"))
        elif vr < 0.5:  tech_signals.append(("경고", f"거래량 급감 {vr:.1f}x"))

    return tech_signals


# ─────────────────────────────────────────────────────
#  종합 판단 블록 — 서술형
# ─────────────────────────────────────────────────────

def _print_verdict(supply_ctx: dict, tech: dict, name: str) -> None:
    """수급 + 기술 지표를 종합해 서술형 해석 출력.

    supply_ctx keys:
      top_broker, top_net_90d, net3,          # 주도창구
      frgn_5d, orgn_5d,                        # 투자자 유형
      glob_net_today, net_today_top5,          # 당일
      inv_rows,                                # 최근 5일 투자자 리스트
      short,                                   # _short_data() 반환 dict
    tech: _calc_technicals() 반환 dict
    """
    W = 57
    print(f"\n{'━'*W}")
    print(f"  ★ 종합 해석 — {name}")
    print(f"{'━'*W}")

    # ── 현재 국면 한 줄 ─────────────────────────────
    pos = warn = 0
    t = tech or {}

    rsi     = t.get("rsi", float("nan"))
    pct_b   = t.get("pct_b", float("nan"))
    macd_h  = t.get("macd_hist", float("nan"))
    macd_hp = t.get("macd_hist_prev", float("nan"))
    ma5     = t.get("ma5", float("nan"))
    ma20    = t.get("ma20", float("nan"))
    ma60    = t.get("ma60", float("nan"))
    p       = t.get("p", float("nan"))
    vr      = t.get("vr", float("nan"))

    top_b   = supply_ctx.get("top_broker", "-")
    net3    = supply_ctx.get("net3", 0)
    frgn_5d = supply_ctx.get("frgn_5d", 0)
    orgn_5d = supply_ctx.get("orgn_5d", 0)
    glob_net= supply_ctx.get("glob_net_today", 0)

    # 점수 계산 (서술용)
    if not np.isnan(rsi):
        if rsi >= 70: warn += 2
        elif rsi <= 30: pos += 2
    if not np.isnan(pct_b):
        if pct_b >= 0.85: warn += 1
        elif pct_b <= 0.15: pos += 1
    if not np.isnan(macd_h) and not np.isnan(macd_hp):
        if macd_h > 0 and macd_h > macd_hp: pos += 1
        elif macd_h < 0 and macd_h < macd_hp: warn += 2
        elif macd_h > 0 and macd_hp > 0 and macd_h < macd_hp: warn += 1
    if not np.isnan(ma20) and not np.isnan(p):
        if p > ma20: pos += 1
        else: warn += 1
    if not np.isnan(ma5) and not np.isnan(ma20) and not np.isnan(ma60):
        if ma5 > ma20 > ma60: pos += 1
        elif ma5 < ma20 < ma60: warn += 2

    if net3 > 1000:   pos += 2
    elif net3 < -1000: warn += 2
    if frgn_5d > 0:   pos += 1
    elif frgn_5d < 0: warn += 1
    if orgn_5d > 0:   pos += 1
    elif orgn_5d < 0: warn += 1
    if glob_net > 0:  pos += 1
    elif glob_net < 0: warn += 1

    # 공매도 scoring
    sd          = supply_ctx.get("short", {})
    short_pct   = sd.get("short_bal_pct", 0) or 0
    short_chg5  = sd.get("chg_5d", 0) or 0
    if short_pct >= 10:   warn += 2
    elif short_pct >= 5:  warn += 1
    if short_chg5 >= 30:  warn += 2
    elif short_chg5 >= 15: warn += 1
    elif short_chg5 <= -20: pos += 1   # 숏커버링 반등 촉매

    score = pos - warn
    if score >= 4:    phase = "📗 강한 매수 우호"
    elif score >= 2:  phase = "🟡 매수 우호 (주의 병존)"
    elif score >= 0:  phase = "⬜ 중립 — 방향성 불명확"
    elif score >= -3: phase = "🟠 관망 우위 (경고 우세)"
    else:             phase = "📕 매도/관망 — 복합 이상 신호"

    print(f"\n  {phase}  (긍정 {pos}pt / 경고 {warn}pt)")

    # ── 수급 해석 ────────────────────────────────────
    print(f"\n  ▶ 수급 해석")

    # 주도창구
    top_net_90 = supply_ctx.get("top_net_90d", 0)
    inv_rows   = supply_ctx.get("inv_rows", [])
    if top_b != "-":
        print(f"  · 주도창구 [{top_b}]: 90일 누적 {top_net_90:+,.0f}주")
        if net3 > 2000:
            print(f"    최근 3일 {net3:+,.0f}주로 매수 지속 — 세력 건재")
        elif net3 > 0:
            print(f"    최근 3일 {net3:+,.0f}주로 소폭 매수 유지 — 추이 주시 필요")
        elif net3 > -2000:
            print(f"    최근 3일 {net3:+,.0f}주, 관망 전환 — 90일 포지션 출구 탐색 가능성")
        else:
            print(f"    최근 3일 {net3:+,.0f}주 대규모 매도 전환 ⚠️")
            print(f"    90일간 누적한 포지션을 청산 중. 다음 상승 주도창구 미확인 시 단기 리스크.")

    # 외국인/기관
    if frgn_5d != 0 or orgn_5d != 0:
        print(f"  · 투자자 흐름 (확정 기간 합산):")
        if frgn_5d > 0:
            print(f"    외국인 {frgn_5d:+,.0f}주 순매수 — 외부 자금 유입 긍정적")
        elif frgn_5d < 0:
            print(f"    외국인 {frgn_5d:+,.0f}주 순매도 — 외국인 이탈 중")
        if orgn_5d > 0:
            print(f"    기관 {orgn_5d:+,.0f}주 순매수 — 기관 지지 양호")
        elif orgn_5d < 0:
            print(f"    기관 {orgn_5d:+,.0f}주 순매도 — 기관도 이탈 중")

        # 외국인 일관성 체크
        if inv_rows:
            confirmed = [r for r in inv_rows if r.get("frgn") is not None]
            if len(confirmed) >= 3:
                frgn_vals = [r["frgn"] for r in confirmed]
                pos_days  = sum(1 for v in frgn_vals if v > 0)
                neg_days  = sum(1 for v in frgn_vals if v < 0)
                if pos_days >= len(frgn_vals) - 1:
                    print(f"    ({pos_days}/{len(frgn_vals)}일 순매수 — 외국인 일관성 높음)")
                elif neg_days >= len(frgn_vals) - 1:
                    print(f"    ({neg_days}/{len(frgn_vals)}일 순매도 — 외국인 일관성 있게 이탈)")
                else:
                    print(f"    (매수 {pos_days}일 / 매도 {neg_days}일 — 외국인 방향성 혼재)")

    # 당일
    net_t5 = supply_ctx.get("net_today_top5", 0)
    if glob_net != 0 or net_t5 != 0:
        print(f"  · 당일 현황:")
        if net_t5 > 0:
            print(f"    TOP5 창구 합산 매수우위 {net_t5:+,.0f}주 — 오늘 매수 우세")
        elif net_t5 < 0:
            print(f"    TOP5 창구 합산 매도우위 {net_t5:+,.0f}주 — 오늘 매도 우세")
        if glob_net < 0:
            print(f"    외국계 당일 순매도 {glob_net:+,.0f}주 — 외국 자금 오늘 빠지는 중")
        elif glob_net > 0:
            print(f"    외국계 당일 순매수 {glob_net:+,.0f}주 — 외국 자금 오늘 유입")

    # ── 공매도 / 신용잔고 해석 ───────────────────────
    if sd:
        print(f"\n  ▶ 공매도 해석")
        bal_pct = sd.get("short_bal_pct", 0) or 0
        chg5    = sd.get("chg_5d", 0) or 0
        chg20   = sd.get("chg_20d", 0) or 0
        ratio_d = sd.get("short_ratio_today", 0) or 0

        if bal_pct < 2 and abs(chg5) < 10:
            print(f"  · 공매도 잔고율 {bal_pct:.2f}% — 공매도 압력 미미. 수급 왜곡 요인 없음.")
        elif bal_pct >= 10:
            print(f"  · 공매도 잔고율 {bal_pct:.1f}% — 강한 공매도 압력. 주가 회복 시 숏커버링 랠리 가능하나,")
            print(f"    현 추세 하락 중에는 매도 압력 지속.")
        elif bal_pct >= 5:
            if chg5 >= 15:
                print(f"  · 공매도 잔고율 {bal_pct:.1f}% + 5일 {chg5:+.0f}% 증가 — 하방 베팅 강화 중.")
                print(f"    공매도 세력이 확신을 갖고 포지션 확대. 단기 추가 하락 압력 존재.")
            else:
                print(f"  · 공매도 잔고율 {bal_pct:.1f}% — 일정 수준 공매도 압력. 수급 회복 시 숏커버링 기대 가능.")
        else:
            if chg5 >= 20:
                print(f"  · 공매도 잔고 5일 {chg5:+.0f}% 증가 — 잔고율은 낮으나 공매도 세력 점진적 진입.")
            elif chg5 <= -20:
                print(f"  · 공매도 잔고 5일 {chg5:.0f}% 감소 (숏커버링) — 매도 압력 해소 → 단기 반등 촉매.")
            else:
                print(f"  · 공매도 잔고율 {bal_pct:.1f}% (5일 {chg5:+.1f}%) — 공매도 압력 크지 않음.")

        cb = sd.get("credit_balance")
        cc = sd.get("credit_chg_5d")
        if cb is not None and cc is not None:
            if cc >= 20:
                print(f"  · 신용잔고 5일 {cc:+.0f}% 급증 — 개인 레버리지 과열. 강제 청산 리스크 주의 (역지표).")
            elif cc <= -20:
                print(f"  · 신용잔고 5일 {cc:.0f}% 감소 — 레버리지 해소. 급락 가능성 낮아짐.")

    # ── 기술적 해석 ──────────────────────────────────
    print(f"\n  ▶ 기술적 해석")

    if not t:
        print("  · 가격 데이터 부족")
    else:
        from_high = t.get("from_high", float("nan"))
        from_low  = t.get("from_low",  float("nan"))

        # 추세
        if not any(np.isnan(x) for x in [ma5, ma20, ma60]):
            if ma5 > ma20 > ma60:
                print(f"  · 추세: MA 정배열 유지 (MA5 {ma5:,.0f} > MA20 {ma20:,.0f} > MA60 {ma60:,.0f})")
                print(f"    중장기 상승 추세 구조 intact. 조정 시 MA20이 1차 지지선.")
            elif ma5 < ma20 < ma60:
                print(f"  · 추세: MA 역배열 (MA5 {ma5:,.0f} < MA20 {ma20:,.0f} < MA60 {ma60:,.0f})")
                print(f"    하락 추세 구조. MA20 회복 여부가 반전 조건.")
            elif not np.isnan(p) and p < ma20:
                print(f"  · 추세: MA20({ma20:,.0f}) 하회 — 단기 추세 이탈. MA20 재돌파 여부 주목.")
            else:
                print(f"  · 추세: MA20({ma20:,.0f}) 위, MA 배열 혼조.")

        # 고저 위치
        if not np.isnan(from_high):
            if from_high >= -5:
                print(f"  · 위치: 52주 신고가 근처 ({from_high:+.1f}%) — 차익실현 욕구 vs 추가 상승 기대 공존.")
            elif from_high <= -40:
                print(f"  · 위치: 52주 고가 대비 {from_high:.0f}% — 낙폭 과대 구간, 반등 가능성.")
            else:
                print(f"  · 위치: 52주 고가 대비 {from_high:.0f}%, 저가 대비 {from_low:+.0f}%.")

        # 과매수/과매도 (RSI + BB 교차 해석)
        if not np.isnan(rsi) and not np.isnan(pct_b):
            if rsi >= 70 and pct_b >= 0.8:
                print(f"  · 과열: RSI {rsi:.0f} + BB %B {pct_b:.2f} 동시 과열 ⚠️")
                print(f"    두 지표가 동시에 극단값일 때 단기(5~10일) 조정 확률 통계적으로 높아짐.")
                print(f"    볼린저 중간선 MA20({ma20:,.0f})이 조정 시 1차 목표가.")
            elif rsi >= 70:
                print(f"  · 과열: RSI {rsi:.0f} 과매수 구간. 단기 모멘텀 둔화 가능.")
            elif rsi <= 30 and pct_b <= 0.2:
                print(f"  · 과매도: RSI {rsi:.0f} + BB 하단 동시 — 강한 반등 신호.")
            elif rsi <= 30:
                print(f"  · 과매도: RSI {rsi:.0f} — 기술적 반등 구간.")
            else:
                print(f"  · RSI {rsi:.0f} (중립), BB %B {pct_b:.2f} — 기술적 여유 있음.")

        # MACD 모멘텀
        if not any(np.isnan(x) for x in [macd_h, macd_hp]):
            if macd_h > 0 and macd_hp <= 0:
                print(f"  · 모멘텀: MACD 골든크로스 발생 — 추세 전환 초입.")
            elif macd_h < 0 and macd_hp >= 0:
                print(f"  · 모멘텀: MACD 데드크로스 발생 — 하락 전환 신호.")
            elif macd_h > 0:
                trend = "확대" if macd_h > macd_hp else "축소"
                if trend == "확대":
                    print(f"  · 모멘텀: MACD 히스토그램 확대 중 ({macd_hp:+.1f}→{macd_h:+.1f}) — 상승 탄력 강화.")
                else:
                    print(f"  · 모멘텀: MACD 히스토그램 축소 중 ({macd_hp:+.1f}→{macd_h:+.1f}) — 상승 모멘텀 둔화. 방향 전환 전조 주시.")
            else:
                trend = "확대" if macd_h < macd_hp else "축소"
                if trend == "확대":
                    print(f"  · 모멘텀: MACD 하락 히스토그램 확대 — 하락 압력 강화.")
                else:
                    print(f"  · 모멘텀: MACD 하락 히스토그램 축소 ({macd_hp:+.1f}→{macd_h:+.1f}) — 낙폭 둔화, 바닥 근처 가능성.")

        # 거래량
        if not np.isnan(vr):
            vol_today = t.get("vol_today", 0)
            vol_avg20 = t.get("vol_avg20", 0)
            if vr >= 2.0:
                print(f"  · 거래량: 전일 {vol_today:,}주 (평균比 {vr:.1f}x 폭발) — 강한 방향성 신호. 상승이면 진입, 하락이면 투매 신호.")
            elif vr >= 1.3:
                print(f"  · 거래량: 전일 {vol_today:,}주 (평균比 {vr:.1f}x) — 평균 이상 거래 동반, 수급 관심 증가.")
            elif vr < 0.6:
                print(f"  · 거래량: 전일 {vol_today:,}주 (평균比 {vr:.1f}x 감소) — 거래량 급감, 모멘텀 약화 또는 관망 심화.")
            else:
                print(f"  · 거래량: 전일 {vol_today:,}주 (평균比 {vr:.1f}x) — 보통 수준.")

    # ── 단기 시나리오 ────────────────────────────────
    print(f"\n  ▶ 단기 시나리오")

    if score >= 3:
        print(f"  · 강세 지속 시나리오가 우세. 눌림 시 추가 매수 고려 가능.")
        if not np.isnan(ma20):
            print(f"    지지선 MA20 {ma20:,.0f}원 — 이 수준 이상 유지 시 추세 유효.")
    elif score >= 1:
        print(f"  · 상승 추세 유지 중이나 단기 리스크 요인 병존.")
        if not np.isnan(ma20):
            print(f"    MA20({ma20:,.0f}원) 지지 확인 시 안정. 이탈 시 추가 하락 주의.")
    elif score >= -2:
        # 중립~경고 우세
        has_leader_exit = net3 < -1000
        has_tech_overheat = (not np.isnan(rsi) and rsi >= 70) or (not np.isnan(pct_b) and pct_b >= 0.85)
        if has_leader_exit and has_tech_overheat:
            print(f"  · 주도창구 이탈 + 기술적 과열이 겹친 구간 — 신규 진입 비추.")
            if not np.isnan(ma20):
                print(f"    MA20({ma20:,.0f}원) 수준까지 눌린 뒤 새 주도창구 등장 확인 후 진입이 안전.")
        elif has_leader_exit:
            print(f"  · 주도창구 이탈이 핵심 리스크. 대체 매수세 확인 전 추가 진입 자제.")
        else:
            print(f"  · 경고 신호 우세. 기존 보유 시 손절선 점검, 신규 진입 대기.")
    else:
        print(f"  · 수급 이탈 + 기술 약화 신호 복합 — 리스크 축소 우선.")
        if not np.isnan(ma60):
            print(f"    MA60({ma60:,.0f}원) 이탈 여부가 추세 전환 판단 기준.")

    print(f"{'━'*W}\n")


# ─────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────

def analyze(code: str, name: str = "") -> None:
    label = name or code
    print(f"\n{'='*55}")
    print(f"  {label} ({code})  단기 수급  — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*55}")

    # 종합용 수급 컨텍스트
    supply_ctx = {"top_broker": "-", "top_net_90d": 0, "net3": 0,
                  "frgn_5d": 0, "orgn_5d": 0,
                  "glob_net_today": 0, "net_today_top5": 0, "inv_rows": []}

    # ── 1. 당일 창구 스냅샷 ──────────────────────────────
    snap = _snapshot(code)
    if snap:
        total_vol  = int(snap.get("acml_vol", 0) or 0)
        print(f"\n▣ 당일 창구 TOP 5  (총거래량 {total_vol:,}주)")
        print(f"  {'매수창구':15s} {'매수량':>9s} {'비중':>6s}   {'매도창구':15s} {'매도량':>9s} {'비중':>6s}")
        print(f"  {'-'*52}")
        total_buy_today = total_sell_today = 0
        for i in range(1, 6):
            b_name = snap.get(f"shnu_mbcr_name{i}", "-") or "-"
            b_qty  = int(snap.get(f"total_shnu_qty{i}", 0) or 0)
            b_pct  = snap.get(f"shnu_mbcr_rlim{i}", "0")
            s_name = snap.get(f"seln_mbcr_name{i}", "-") or "-"
            s_qty  = int(snap.get(f"total_seln_qty{i}", 0) or 0)
            s_pct  = snap.get(f"seln_mbcr_rlim{i}", "0")
            total_buy_today  += b_qty
            total_sell_today += s_qty
            print(f"  {b_name:15s} {b_qty:>9,}  {b_pct:>5}%   {s_name:15s} {s_qty:>9,}  {s_pct:>5}%")

        glob_buy  = int(snap.get("glob_total_shnu_qty", 0) or 0)
        glob_sell = int(snap.get("glob_total_seln_qty", 0) or 0)
        glob_net  = glob_buy - glob_sell
        if glob_buy + glob_sell > 0:
            print(f"\n  외국계 합산(당일): 매수 {glob_buy:,}  매도 {glob_sell:,}  순 {glob_net:+,}")
        top_buyer  = snap.get("shnu_mbcr_name1", "-") or "-"
        top_seller = snap.get("seln_mbcr_name1", "-") or "-"
        net_today  = total_buy_today - total_sell_today
        print(f"\n  당일 최대 매수: {top_buyer}  |  당일 최대 매도: {top_seller}")
        supply_ctx["glob_net_today"]  = glob_net
        supply_ctx["net_today_top5"]  = net_today
    else:
        print("\n  당일 스냅샷 조회 실패")

    # ── 2. 최근 5일 투자자별 순매수 ─────────────────────
    inv = _investor_recent(code, 5)
    print(f"\n▣ 투자자별 순매수 (최근 5거래일)")
    print(f"  {'날짜':10s}  {'종가':>8s}  {'외국인':>9s}  {'기관':>9s}  {'개인':>9s}")
    print(f"  {'-'*52}")
    frgn_5d = orgn_5d = 0
    for r in inv:
        d = r['date']
        d_fmt = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d
        frgn  = f"{r['frgn']:>+9,}" if r['frgn'] is not None else f"{'(미확정)':>9s}"
        orgn  = f"{r['orgn']:>+9,}" if r['orgn'] is not None else f"{'':>9s}"
        prsn  = f"{r['prsn']:>+9,}" if r['prsn'] is not None else f"{'':>9s}"
        print(f"  {d_fmt:10s}  {r['close']:>8,}  {frgn}  {orgn}  {prsn}")
        if r['frgn'] is not None: frgn_5d += r['frgn']
        if r['orgn'] is not None: orgn_5d += r['orgn']
    supply_ctx["frgn_5d"]  = frgn_5d
    supply_ctx["orgn_5d"]  = orgn_5d
    supply_ctx["inv_rows"] = inv

    # ── 3. DB 창구별 일별 흐름 ───────────────────────────
    md5 = query_df("""
        SELECT date, broker_name, net, buy, sell
        FROM member_daily WHERE code=? AND date >= date('now','-8 days')
        ORDER BY date DESC, net DESC
    """, (code,))

    if not md5.empty:
        print(f"\n▣ 창구별 일별 순매수 (DB 최근 5거래일)")
        for date, grp in md5.groupby("date", sort=False):
            total_buy  = grp["buy"].sum(); total_sell = grp["sell"].sum()
            print(f"\n  [{date}]  총매수 {total_buy:,}주  총매도 {total_sell:,}주")
            print(f"  {'창구':15s}  {'순매수':>9s}  {'매수':>9s}  {'매도':>9s}")
            for _, row in grp.head(5).iterrows():
                print(f"  {row['broker_name']:15s}  {row['net']:>+9,.0f}  {row['buy']:>9,.0f}  {row['sell']:>9,.0f}  ▲")
            neg = grp[grp['net'] < 0].sort_values('net').head(3)
            if len(neg):
                print(f"  --- 순매도 ---")
                for _, row in neg.iterrows():
                    print(f"  {row['broker_name']:15s}  {row['net']:>+9,.0f}  {row['buy']:>9,.0f}  {row['sell']:>9,.0f}  ▼")
    else:
        print("\n  DB 창구 데이터 없음")

    # ── 4. 주도창구 추적 ─────────────────────────────────
    md90_top = query_df("""
        SELECT broker_name, SUM(net) as net_90d
        FROM member_daily WHERE code=? AND date>=date('now','-95 days')
        GROUP BY broker_name ORDER BY net_90d DESC LIMIT 1
    """, (code,))

    if not md90_top.empty:
        top_b   = md90_top.iloc[0]['broker_name']
        top_n   = md90_top.iloc[0]['net_90d']
        recent3 = query_df("""
            SELECT date, net FROM member_daily
            WHERE code=? AND broker_name=? AND date>=date('now','-5 days')
            ORDER BY date
        """, (code, top_b))
        net3 = recent3['net'].sum() if len(recent3) else 0

        if net3 > 1000:    st = '✅ 매수 지속'
        elif net3 > 0:     st = '🟡 소폭 매수'
        elif net3 > -1000: st = '⚠️ 관망'
        else:              st = '🔴 매도 전환'

        supply_ctx["top_broker"]  = top_b
        supply_ctx["top_net_90d"] = top_n
        supply_ctx["net3"]        = net3

        print(f"\n▣ 주도창구 추적  [{top_b}]")
        print(f"  90일 누적: {top_n:+,.0f}주   최근 3거래일: {net3:+,.0f}주  {st}")
        if len(recent3):
            mx = recent3['net'].abs().max() or 1
            for _, row in recent3.iterrows():
                bar = ('▲' if row['net'] > 0 else '▼') * min(10, max(1, int(abs(row['net']) / (mx / 10))))
                print(f"    {row['date']}  {row['net']:>+8,.0f}주  {bar}")

    # ── 5. 공매도 & 신용잔고 ────────────────────────────
    sd = _short_data(code)
    _print_short(sd)
    supply_ctx["short"] = sd

    # ── 6. 기술적 지표 ───────────────────────────────────
    df_price = _load_ohlcv(code, 120)
    t = _calc_technicals(df_price)
    _print_technicals(t)

    # ── 7. 수급 + 공매도 + 기술 종합 판단 ───────────────
    _print_verdict(supply_ctx, t, label)


if __name__ == "__main__":
    import argparse
    from file_io import load_json
    from config import STOCK_MAP_FILE

    p = argparse.ArgumentParser()
    p.add_argument("codes", nargs="*")
    p.add_argument("--names", nargs="*")
    args = p.parse_args()

    smap = load_json(STOCK_MAP_FILE, default={})
    code_to_name = {info["code"]: nm for nm, info in smap.items() if "code" in info}
    targets = []
    for c in args.codes or []:
        targets.append((code_to_name.get(c, c), c))
    for n in args.names or []:
        info = smap.get(n, {})
        if "code" in info:
            targets.append((n, info["code"]))

    if not targets:
        targets = [("제이스로보틱스", "090470")]

    for nm, cd in targets:
        analyze(cd, nm)
