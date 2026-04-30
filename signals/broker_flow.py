"""거래원 수급 분석 — 추세 국면 + 다이버전스 + 가격 컨펌.

핵심 아이디어:
  단순히 "외국계가 팔았다" → 너무 자주 발동 (상승장 차익실현 포함)
  대신 "신고가 근처인데 외국계+기관이 빠지기 시작" = 진짜 분배(distribution)

시그널 발동 단계:
  1. 추세 국면 분류 (uptrend / topping / downtrend / neutral)
  2. 베어리시 다이버전스 탐지 (가격 ↑ but 수급 ↓)
  3. 가격 컨펌 (MA20 또는 직전 저점 이탈 시 진짜 매도)
"""
import glob, os, re
from collections import defaultdict
import warnings
warnings.filterwarnings("ignore")

# ── 거래원 그룹 분류
FOREIGN = {
    "JP모간증권", "모간스탠리증권", "골드만삭스증권", "메릴린치증권",
    "UBS증권", "CLSA코리아증권", "씨티그룹글로벌", "BNP파리바증권",
    "노무라증권", "맥쿼리증권", "다이와증권SCMK", "외국계합", "외국계",
    "홍콩상하이증권", "CS증권", "도이치증권",
}
RETAIL_HEAVY = {
    "키움증권", "토스증권", "카카오페이증권", "상상인증권",
}

# ── 대형 증권사 (분배 시 주체로 자주 출현, 개인 비중 높은 창구)
# 대한광통신 케이스에서 NH/KB/한화가 동반 매도 → 개미가 받음 = 분배 패턴
LARGE_INST_BROKERS = {
    "NH투자증권", "KB증권", "한국투자증권", "삼성증권",
    "한화증권", "미래에셋증권", "신한투자증권", "하나금융투자",
}

# ── 거래량 대비 비율 임계값 (%)
FOREIGN_RATIO_20D = -3.0
FOREIGN_RATIO_5D  = -2.0
INST_RATIO_20D    = -5.0
INST_RATIO_5D     = -3.0
RETAIL_RATIO_5D   = 8.0
SMART_RATIO_20D   = -3.0   # 다이버전스 판정 기준

# ── 추세 국면 임계값
NEAR_HIGH_PCT     = 0.90   # 60일 신고가의 90% 이상이면 신고가권
DIVERGENCE_PCT    = 0.92   # 다이버전스 신고가권 (조금 더 엄격)
CONFIRM_DAYS      = 10     # 시그널 후 N일 이내 가격 컨펌

# ── 신규 시그널 임계값 (실제 데이터 기준 캘리브레이션)
# 대한광통신 케이스 분석 결과 적용 (2025-01~02 주요 분배일 5건 캡처)
DIST_RETAIL_5D       = 2.5    # 개미 5일 매수 +2.5% 이상
DIST_LARGE_INST_5D   = -2.5   # 대형 기관 5일 매도 -2.5% 이하
FOREIGN_BREADTH_5D   = 8      # 외국계 매수자수-매도자수 5일 누적 8 이상
HHI_THRESHOLD        = 1500   # 거래원 집중도 (단일 거래원 30%+ 점유 수준)
TOP_SHARE_5D         = 35.0   # 단일 거래원 5일 평균 점유율 35% 이상


def _broker_group(name: str) -> str:
    if name in FOREIGN:  return "foreign"
    if name in RETAIL_HEAVY: return "retail"
    return "inst"


def _load_file(fpath: str):
    with open(fpath, "rb") as f:
        raw = f.read()
    bom = b"\xef\xbb\xbf"
    if raw.startswith(bom):
        text = raw.decode("utf-8-sig")
        if "NO_DATA" in text:
            return None
    else:
        text = raw.decode("cp949", errors="replace")

    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    rows = []
    for line in lines[1:]:
        parts = [p.strip('"').strip() for p in line.split('","')]
        if len(parts) < 6:
            continue
        try:
            broker = re.sub(r"\s+", "", parts[1])
            rows.append({
                "broker": broker,
                "group":  _broker_group(broker),
                "sell":   int(parts[2].replace(",", "")),
                "buy":    int(parts[3].replace(",", "")),
                "net":    int(parts[4].replace(",", "").replace("+", "")),
                "total":  int(parts[5].replace(",", "")),
            })
        except Exception:
            pass
    return rows or None


def load_stock_flow(stock_name: str, flow_dir: str) -> dict:
    folder = os.path.join(flow_dir, stock_name)
    if not os.path.isdir(folder):
        return {}
    result = {}
    for fpath in sorted(glob.glob(os.path.join(folder, "*.csv"))):
        m = re.search(r"_(\d{8})\.csv$", fpath)
        if not m:
            continue
        rows = _load_file(fpath)
        if rows:
            result[m.group(1)] = rows
    return result


def build_timeseries(flow_data: dict, price_series=None):
    """일자별 수급 + (옵션)가격 기반 추세 지표.

    price_series: pd.Series (index=YYYY-MM-DD str, value=종가) — 있으면 추세 분석 추가
    """
    import pandas as pd

    dates = sorted(flow_data.keys())
    records = []
    top_broker_per_day = []   # (date, top_broker_name, share)
    for d in dates:
        rows = flow_data[d]
        grp = defaultdict(int)
        large_inst_net = 0
        f_buy_cnt = 0   # 오늘 매수한 외국계 거래원 수
        f_sell_cnt = 0  # 오늘 매도한 외국계 거래원 수
        broker_abs_nets = []  # HHI 계산용

        for r in rows:
            grp[r["group"]] += r["net"]
            if r["broker"] in LARGE_INST_BROKERS:
                large_inst_net += r["net"]
            if r["group"] == "foreign":
                if r["net"] > 0: f_buy_cnt += 1
                elif r["net"] < 0: f_sell_cnt += 1
            broker_abs_nets.append((r["broker"], abs(r["net"])))

        # HHI: 거래원별 |순매수| 점유율 제곱합
        total_abs = sum(v for _, v in broker_abs_nets)
        if total_abs > 0:
            hhi = sum((v/total_abs)**2 for _, v in broker_abs_nets) * 10000
            # 최상위 거래원
            top_b, top_v = max(broker_abs_nets, key=lambda x: x[1])
            top_share = top_v / total_abs * 100
        else:
            hhi = 0
            top_b, top_share = "", 0

        top_broker_per_day.append({"date": d, "top_broker": top_b, "top_share": round(top_share, 1)})

        records.append({
            "date":      pd.Timestamp(d),
            "foreign":   grp["foreign"],
            "inst":      grp["inst"],
            "retail":    grp["retail"],
            "large_inst": large_inst_net,
            "f_buy_cnt":  f_buy_cnt,
            "f_sell_cnt": f_sell_cnt,
            "f_breadth":  f_buy_cnt - f_sell_cnt,  # 외국계 매수자 - 매도자 (양수=매수 우세)
            "hhi":        round(hhi, 1),
            "top_broker": top_b,
            "top_share":  round(top_share, 1),
            "total_vol":  sum(r["total"] for r in rows),
            "broker_cnt": len(rows),
        })
    df = pd.DataFrame(records).set_index("date")

    # ── 그룹별 롤링 수량 합
    for g in ("foreign", "inst", "retail", "large_inst"):
        for w in (5, 10, 20):
            df[f"{g}_{w}d"] = df[g].rolling(w).sum()

    # 스마트머니 (외국계+기관)
    df["smart_net"]     = df["foreign"] + df["inst"]
    df["smart_net_5d"]  = df["smart_net"].rolling(5).sum()
    df["smart_net_20d"] = df["smart_net"].rolling(20).sum()

    # 거래량 대비 비율 (%)
    vol5  = df["total_vol"].rolling(5).sum().replace(0, float("nan"))
    vol20 = df["total_vol"].rolling(20).sum().replace(0, float("nan"))
    for g in ("foreign", "inst", "retail", "smart_net", "large_inst"):
        df[f"{g}_ratio_5d"]  = (df[f"{g}_5d"]  / vol5  * 100).round(2)
        df[f"{g}_ratio_20d"] = (df[f"{g}_20d"] / vol20 * 100).round(2)

    # 외국계 컨센서스 (매수 시그널)
    df["foreign_breadth_5d"] = df["f_breadth"].rolling(5).sum()
    df["hhi_5d"] = df["hhi"].rolling(5).mean().round(0)
    df["top_share_5d"] = df["top_share"].rolling(5).mean().round(1)

    # ── 분배 패턴 일별 플래그 + 지속성 (10일 중 발생 일수)
    df["dist_pattern"] = (
        (df["retail_ratio_5d"] >= DIST_RETAIL_5D) &
        (df["large_inst_ratio_5d"] <= DIST_LARGE_INST_5D)
    ).fillna(False).astype(int)
    df["dist_persistence_10d"] = df["dist_pattern"].rolling(10).sum()

    # ── 가격 기반 지표 (있을 때만)
    if price_series is not None:
        idx_str = df.index.strftime("%Y-%m-%d")
        df["close"] = [price_series.get(d) for d in idx_str]
        df["close"] = df["close"].ffill()

        df["ma20"]   = df["close"].rolling(20).mean()
        df["ma60"]   = df["close"].rolling(60).mean()
        df["high60"] = df["close"].rolling(60).max()
        df["low20"]  = df["close"].rolling(20).min()
        df["near_high"] = df["close"] / df["high60"]   # 1.0 = 60일 신고가 도달

        # 추세 강도 (MA20 기울기 — 5일간 변화율 %)
        df["ma20_slope_5d"] = (df["ma20"].pct_change(5) * 100).round(2)

        # 거래량 다이버전스: 가격 ↑ but 거래량 ↓ (탑 형성 약신호)
        # 종가 5일 변화율 vs 거래량 5일 평균/직전20일 평균
        price_chg_5d = df["close"].pct_change(5) * 100
        vol_now = df["total_vol"].rolling(5).mean()
        vol_prev = df["total_vol"].rolling(20).mean().shift(5)
        vol_ratio = (vol_now / vol_prev).replace([float("inf"), -float("inf")], None)
        df["price_chg_5d"] = price_chg_5d.round(2)
        df["vol_ratio"]    = vol_ratio.round(3)
        df["vol_diverg"]   = ((price_chg_5d > 5) & (vol_ratio < 0.8)).fillna(False).astype(int)

        # 트레일링 스탑: 60일 신고가 -10% 이상 하락
        df["from_high"] = ((df["close"] / df["high60"] - 1) * 100).round(2)
        df["trailing_stop"] = (df["from_high"] <= -10).fillna(False).astype(int)

        # 추세 국면 분류
        df["regime"]       = df.apply(_classify_regime, axis=1)
        df["divergence"]   = _detect_divergence(df)

    return df


def _classify_regime(row) -> int:
    """0=neutral, 1=uptrend, 2=topping(분배 의심), 3=downtrend"""
    import math
    close, ma20, ma60 = row.get("close"), row.get("ma20"), row.get("ma60")
    near_high = row.get("near_high")
    sm20 = row.get("smart_net_ratio_20d")
    if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in (close, ma20, ma60)):
        return 0

    # 하락추세
    if close < ma20 and ma20 < ma60:
        return 3
    # 상승추세
    if close > ma60 and ma20 > ma60:
        # 신고가권인데 스마트머니 음전환 = 분배 의심
        if near_high is not None and near_high >= NEAR_HIGH_PCT and sm20 is not None and sm20 < 0:
            return 2
        return 1
    return 0  # 중립/횡보


def _detect_divergence(df) -> "pd.Series":
    """베어리시 다이버전스: 신고가권에서 스마트머니 음수 + 5일 가속화."""
    nh = df["near_high"] >= DIVERGENCE_PCT
    sm_neg = df["smart_net_ratio_20d"] < 0
    # 가속화: 직전 5일 비율이 더 음수
    accel = df["smart_net_ratio_5d"] < df["smart_net_ratio_20d"]
    return (nh & sm_neg & accel).fillna(False)


def detect_signals(df) -> list:
    """국면 인식 시그널 + 다이버전스 부스트."""
    signals = []
    has_price = "regime" in df.columns

    for i in range(20, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        date = df.index[i].strftime("%Y-%m-%d")

        regime  = int(row["regime"]) if has_price else 0
        diverg  = bool(row.get("divergence", False)) if has_price else False

        fr20 = row.get("foreign_ratio_20d", 0) or 0
        fr5  = row.get("foreign_ratio_5d",  0) or 0
        ir20 = row.get("inst_ratio_20d",    0) or 0
        ir5  = row.get("inst_ratio_5d",     0) or 0
        rr5  = row.get("retail_ratio_5d",   0) or 0
        sr20 = row.get("smart_net_ratio_20d", 0) or 0
        sr20_prev = prev.get("smart_net_ratio_20d", 0) or 0
        sr5  = row.get("smart_net_ratio_5d", 0) or 0

        reasons = []
        score   = 0

        # ── (1) 강한 다이버전스 — 신고가권 + 스마트머니 빠짐 (가속화)
        # 이게 진짜 분배 시그널의 핵심
        if diverg:
            score += 3
            reasons.append(
                f"📉 베어리시 다이버전스: 신고가권({row.get('near_high', 0)*100:.0f}%) + "
                f"스마트머니 20일 {sr20:+.1f}% (5일 {sr5:+.1f}% 가속화)"
            )

        # ── (2) 분배 의심 국면(regime=2)
        if regime == 2:
            score += 1
            reasons.append("⚠️ 분배 의심 국면 (신고가권 + 스마트머니 음전환)")

        # ── (3) 하락추세(regime=3) — 늦었지만 청산 신호
        if regime == 3:
            score += 1
            reasons.append("⛔ 하락추세 진입 (종가 < MA20 < MA60)")

        # ── (4) 외국계 비율
        if fr20 <= FOREIGN_RATIO_20D:
            # 상승추세(regime=1)에선 단순 차익실현일 가능성 → 점수 ↓
            add = 1 if regime == 1 else 2
            score += add
            reasons.append(
                f"외국계 20일 순매도 {fr20:+.1f}% "
                f"(기준 {FOREIGN_RATIO_20D}%, regime={regime} 가중치 +{add})"
            )
        elif fr5 <= FOREIGN_RATIO_5D and regime != 1:
            score += 1
            reasons.append(f"외국계 5일 순매도 {fr5:+.1f}% (기준 {FOREIGN_RATIO_5D}%)")

        # ── (5) 기관 비율
        if ir20 <= INST_RATIO_20D:
            add = 1 if regime == 1 else 2
            score += add
            reasons.append(
                f"기관 20일 순매도 {ir20:+.1f}% "
                f"(기준 {INST_RATIO_20D}%, 가중치 +{add})"
            )
        elif ir5 <= INST_RATIO_5D and regime != 1:
            score += 1
            reasons.append(f"기관 5일 순매도 {ir5:+.1f}% (기준 {INST_RATIO_5D}%)")

        # ── (6) 스마트머니 양→음 전환 (regime 무관)
        if sr20_prev > 0 and sr20 < 0:
            score += 2
            reasons.append(
                f"스마트머니 20일 비율 양→음 전환 ({sr20_prev:+.1f}% → {sr20:+.1f}%)"
            )

        # ── (7) 개인 역지표
        if rr5 >= RETAIL_RATIO_5D:
            score += 1
            reasons.append(f"개인 5일 순매수 {rr5:+.1f}% — 역지표 (고점 주의)")

        # ── (8) 분배 패턴: 개미 매수 + 대형기관 매도 동시 발생
        # 대한광통신에서 발견된 가장 위험한 패턴
        li5 = row.get("large_inst_ratio_5d", 0) or 0
        dist_today = (rr5 >= DIST_RETAIL_5D and li5 <= DIST_LARGE_INST_5D)
        if dist_today:
            # 지속성 가중치: 최근 10일 중 N일 발생
            persist = int(row.get("dist_persistence_10d", 1) or 1)
            if persist >= 5:
                add = 5
                pmsg = f"강력 분배 ({persist}/10일 발동)"
            elif persist >= 3:
                add = 4
                pmsg = f"진성 분배 ({persist}/10일 발동)"
            else:
                add = 3
                pmsg = f"분배 패턴 ({persist}/10일 발동)"
            score += add
            reasons.append(
                f"🚨 {pmsg}: 개미 5일 +{rr5:.1f}% ↔ "
                f"대형기관(NH/KB/한투/삼성 등) 5일 {li5:.1f}%"
            )

        # ── (9) 거래원 집중도 경보 (HHI 또는 단일 거래원 점유율)
        hhi5 = row.get("hhi_5d", 0) or 0
        top_share_5d = row.get("top_share_5d", 0) or 0
        top_broker = row.get("top_broker", "?")
        if hhi5 >= HHI_THRESHOLD or top_share_5d >= TOP_SHARE_5D:
            score += 1
            reasons.append(
                f"⚠️ 거래원 집중도: HHI {hhi5:.0f} / "
                f"5일 평균 최상위 점유율 {top_share_5d:.0f}% ({top_broker})"
            )

        # ── (10) 거래량 다이버전스 (가격 ↑ but 거래량 ↓)
        vol_div = bool(row.get("vol_diverg", False))
        if vol_div and has_price:
            score += 2
            pc = row.get("price_chg_5d", 0) or 0
            vr = row.get("vol_ratio", 0) or 0
            reasons.append(
                f"📊 거래량 다이버전스: 5일 가격 +{pc:.1f}% but 거래량 비율 {vr:.2f} "
                f"(거래량 동반 안 됨 → 약한 상승)"
            )

        # ── (11) 트레일링 스탑 (60일 신고가 -10% 이탈)
        ts = bool(row.get("trailing_stop", False))
        from_high = row.get("from_high", 0) or 0
        if ts and has_price:
            score += 1
            reasons.append(
                f"🔻 트레일링 스탑: 60일 신고가 대비 {from_high:.1f}% 하락"
            )

        # ── 시그널 시너지: 3개 이상 카테고리 동시 발동 시 보너스
        active_categories = sum([
            bool(diverg), bool(dist_today), bool(vol_div),
            bool(fr20 <= FOREIGN_RATIO_20D),
            bool(ir20 <= INST_RATIO_20D),
            bool(sr20_prev > 0 and sr20 < 0),
            bool(hhi5 >= HHI_THRESHOLD or top_share_5d >= TOP_SHARE_5D),
            bool(rr5 >= RETAIL_RATIO_5D),
        ])
        synergy = 0
        if active_categories >= 4:
            synergy = 3
            reasons.append(f"⚡ 시너지: {active_categories}개 카테고리 동시 발동 (+{synergy})")
        elif active_categories >= 3:
            synergy = 2
            reasons.append(f"⚡ 시너지: {active_categories}개 카테고리 동시 발동 (+{synergy})")
        score += synergy

        # ── 상승추세에서 약한 시그널은 무시 (조기 매도 방지)
        if regime == 1 and score <= 1 and not diverg:
            continue

        if score == 0:
            continue

        # ── 행동 권고 4단계 분류
        action = _classify_action(score, regime, diverg, dist_today, ts, from_high, row)

        signals.append({
            "date":   date,
            "score":  score,
            "grade":  "매도강추" if score >= 5 else "매도주의" if score >= 3 else "관망",
            "action": action,
            "regime": regime,
            "divergence": diverg,
            "vol_diverg": vol_div,
            "dist_pattern": dist_today,
            "dist_persistence": int(row.get("dist_persistence_10d", 0) or 0),
            "synergy": synergy,
            "active_categories": active_categories,
            "reasons": reasons,
            "fr20": round(fr20, 1), "ir20": round(ir20, 1),
            "sr20": round(sr20, 1), "rr5":  round(rr5, 1),
            "li5":  round(li5, 1), "hhi5": round(hhi5, 0),
            "top_broker": top_broker,
            "top_share":  round(top_share_5d, 1),
            "near_high": round((row.get("near_high") or 0) * 100, 1),
        })

    return signals


def _classify_action(score, regime, diverg, dist_today, trailing_stop, from_high, row) -> str:
    """행동 권고 4단계 분류.

    HOLD       — 약한 시그널, 추세 견조
    부분 익절   — 신고가권 + 약한 매도 시그널
    전량 매도   — 다이버전스/분배/트레일링 스탑 + 가격 깨짐
    신규 매수   — (외국계 컨센서스에서 별도 처리)
    """
    near_high = (row.get("near_high") or 0) * 100

    # 트레일링 스탑 또는 다이버전스+분배 동시 = 전량 매도
    if trailing_stop:
        return "전량 매도 (트레일링 스탑)"
    if diverg and dist_today:
        return "전량 매도 (다이버전스+분배)"
    if score >= 7:
        return "전량 매도 (강한 시그널)"

    # 다이버전스 단독 = 매도
    if diverg:
        return "매도 (다이버전스)"

    # 분배 패턴 단독 = 매도
    if dist_today:
        return "매도 (분배 패턴)"

    # 신고가권 + 약한 시그널 = 부분 익절
    if near_high >= 90 and score >= 3:
        return "부분 익절 (신고가권)"

    # 하락추세 진입 = 매도
    if regime == 3:
        return "매도 (하락 추세)"

    # 그 외 = HOLD
    return "HOLD (감시)"


def detect_foreign_consensus(df) -> list:
    """외국계 컨센서스 매수 시그널 (매수 신호).

    조건:
      - 5일간 외국계 매수자 수가 매도자 수보다 일관되게 많음 (breadth ≥ 8)
      - 외국계 5일 비율이 양수 (실제 순매수)
    반환: 시그널 목록 [{date, breadth_5d, fr5, score, ...}]
    """
    out = []
    if "foreign_breadth_5d" not in df.columns:
        return out

    for i in range(5, len(df)):
        row = df.iloc[i]
        breadth5 = row.get("foreign_breadth_5d", 0) or 0
        fr5      = row.get("foreign_ratio_5d", 0) or 0
        regime   = int(row.get("regime", 0)) if "regime" in df.columns else 0

        if breadth5 >= FOREIGN_BREADTH_5D and fr5 > 1.0:
            score = 2
            # 신고가권에서 매수 컨센서스 = 더 강한 신호
            if regime == 1:
                score += 1
            # 하락추세에서 매수 컨센서스 = 반등 신호 (저점 매수)
            if regime == 3:
                score += 2

            # 행동 권고
            if regime == 3:
                action = "신규 매수 (저점 + 외인 컨센서스)"
            elif regime == 1 and score >= 4:
                action = "추가 매수 (상승추세 + 강한 컨센서스)"
            elif score >= 4:
                action = "신규 매수 (강한 컨센서스)"
            else:
                action = "관심 (외인 컨센서스)"

            out.append({
                "date":   df.index[i].strftime("%Y-%m-%d"),
                "type":   "buy",
                "score":  score,
                "grade":  "매수강추" if score >= 4 else "매수주의",
                "action": action,
                "breadth_5d": int(breadth5),
                "fr5":    round(fr5, 1),
                "regime": regime,
                "reasons": [
                    f"📈 외국계 컨센서스 매수: 5일 누적 매수자-매도자 +{int(breadth5)}",
                    f"외국계 5일 순매수 {fr5:+.1f}%",
                ],
            })
    return out


def check_price_confirmation(df, signal_date: str, days: int = CONFIRM_DAYS) -> dict:
    """시그널 발생 후 N일 내 가격 컨펌 여부.

    컨펌 = 종가가 MA20 하향 돌파 OR 직전 20일 저점 이탈
    반환: {"confirmed": bool, "days_to_confirm": int|None, "reason": str|None}
    """
    if signal_date not in df.index.strftime("%Y-%m-%d").tolist():
        return {"confirmed": False, "days_to_confirm": None, "reason": None}

    idx_list = df.index.strftime("%Y-%m-%d").tolist()
    i = idx_list.index(signal_date)

    sig_low20 = df["low20"].iloc[i] if "low20" in df.columns else None

    for j in range(1, min(days + 1, len(df) - i)):
        row = df.iloc[i + j]
        c   = row.get("close")
        m20 = row.get("ma20")
        if c is None or m20 is None:
            continue
        if c < m20:
            return {"confirmed": True, "days_to_confirm": j, "reason": f"종가 MA20 하향 이탈 ({j}일 후)"}
        if sig_low20 is not None and c < sig_low20:
            return {"confirmed": True, "days_to_confirm": j, "reason": f"직전 20일 저점 이탈 ({j}일 후)"}

    return {"confirmed": False, "days_to_confirm": None, "reason": None}


def analyze_broker_flow(stock_name: str, stock_code: str, flow_dir: str,
                        price_series=None) -> dict:
    """외부 호출용 메인 함수."""
    flow = load_stock_flow(stock_name, flow_dir)
    if not flow:
        return {"error": "daily_flow 데이터 없음", "sub_score": 0}

    df = build_timeseries(flow, price_series)
    if df.empty or len(df) < 5:
        return {"error": "데이터 부족", "sub_score": 0}

    last = df.iloc[-1]
    signals = detect_signals(df)
    latest = signals[-1] if signals and signals[-1]["date"] == df.index[-1].strftime("%Y-%m-%d") else None

    return {
        "regime":              int(last.get("regime", 0)) if "regime" in df.columns else None,
        "divergence":          bool(last.get("divergence", False)) if "divergence" in df.columns else False,
        "near_high_pct":       round((last.get("near_high") or 0) * 100, 1) if "near_high" in df.columns else None,
        "foreign_ratio_5d":    float(last.get("foreign_ratio_5d",    0) or 0),
        "foreign_ratio_20d":   float(last.get("foreign_ratio_20d",   0) or 0),
        "inst_ratio_5d":       float(last.get("inst_ratio_5d",       0) or 0),
        "inst_ratio_20d":      float(last.get("inst_ratio_20d",      0) or 0),
        "smart_net_ratio_20d": float(last.get("smart_net_ratio_20d", 0) or 0),
        "retail_ratio_5d":     float(last.get("retail_ratio_5d",     0) or 0),
        "sub_score":           min(latest["score"] if latest else 0, 5),
        "reasons":             latest["reasons"] if latest else [],
        "signal_count":        len(signals),
        "recent_signals":      signals[-5:],
    }
