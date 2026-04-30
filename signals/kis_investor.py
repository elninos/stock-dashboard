"""KIS API — 종목별 투자자 매매동향 (9분류).

API:
  FHPTJ04160001 — 종목별 투자자매매동향(일별) : 9분류 순매수량/금액
  HHPTJ04160200 — 외인기관 추정가집계         : 장중 5구간 추정치
  FHKST644400C0 — 외국계 순매수추이           : 장중 외국계 창구 누적

9분류:
  외국인 / 개인 / 기관계 / 증권 / 투신 / 사모 / 은행 / 보험 / 종금 / 연기금 / 기타 / 기타법인 / 기타단체
"""
from datetime import datetime, timedelta
from .kis_api import get_client, rate_limit, cached_call, smart_ttl


def fetch_investor_flow(stock_code: str, market: str = "J") -> list:
    """일별 투자자 매매동향 9분류 + 외국인 등록/비등록. 최근 30일.

    market: "J" — KOSPI/KOSDAQ/NXT 공통 (FID_COND_MRKT_DIV_CODE J만 유효)
    반환: list of dict — date, close, open, high, low, volume, turnover,
          frgn/frgn_reg/frgn_nreg/prsn/orgn/.../etc_corp/etc_orgt
          각각 _qty (주) 와 _amt (백만원)
          외국인 매수/매도: frgn_buy_vol, frgn_sell_vol, frgn_buy_amt, frgn_sell_amt
    """
    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily",
        tr_id="FHPTJ04160001",
        params={
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD":         stock_code,
            "FID_INPUT_DATE_1":       "",  # 빈값 = 최신 가용 날짜 자동
            "FID_ORG_ADJ_PRC":        "",
            "FID_ETC_CLS_CODE":       "1",
        },
    )
    if res.get("rt_cd") != "0":
        return []

    out = []
    for row in res.get("output2", []):
        try:
            out.append({
                "date":          row.get("stck_bsop_date"),
                "close":         int(row.get("stck_clpr", 0)),
                "open":          int(row.get("stck_oprc", 0)),
                "high":          int(row.get("stck_hgpr", 0)),
                "low":           int(row.get("stck_lwpr", 0)),
                "change":        int(row.get("prdy_vrss", 0)),
                "change_sign":   row.get("prdy_vrss_sign", ""),
                "volume":        int(row.get("acml_vol", 0)),
                "turnover":      int(row.get("acml_tr_pbmn", 0)),        # 거래대금 (원)
                # ── 순매수량 (주) ────────────────────────────────────
                "frgn_qty":      int(row.get("frgn_ntby_qty", 0)),
                "frgn_reg_qty":  int(row.get("frgn_reg_ntby_qty", 0)),  # 등록 외국인
                "frgn_nreg_qty": int(row.get("frgn_nreg_ntby_qty", 0)), # 비등록 (헤지)
                "prsn_qty":      int(row.get("prsn_ntby_qty", 0)),
                "orgn_qty":      int(row.get("orgn_ntby_qty", 0)),
                "scrt_qty":      int(row.get("scrt_ntby_qty", 0)),
                "ivtr_qty":      int(row.get("ivtr_ntby_qty", 0)),
                "pe_fund_qty":   int(row.get("pe_fund_ntby_vol", 0)),
                "bank_qty":      int(row.get("bank_ntby_qty", 0)),
                "insu_qty":      int(row.get("insu_ntby_qty", 0)),
                "mrbn_qty":      int(row.get("mrbn_ntby_qty", 0)),
                "fund_qty":      int(row.get("fund_ntby_qty", 0)),
                "etc_qty":       int(row.get("etc_ntby_qty", 0)),
                "etc_corp_qty":  int(row.get("etc_corp_ntby_vol", 0)),
                "etc_orgt_qty":  int(row.get("etc_orgt_ntby_vol", 0)),
                # ── 순매수금액 (백만원) ──────────────────────────────
                "frgn_amt":      int(row.get("frgn_ntby_tr_pbmn", 0)),
                "frgn_reg_amt":  int(row.get("frgn_reg_ntby_pbmn", 0)),  # 등록 외국인
                "frgn_nreg_amt": int(row.get("frgn_nreg_ntby_pbmn", 0)), # 비등록 (헤지)
                "prsn_amt":      int(row.get("prsn_ntby_tr_pbmn", 0)),
                "orgn_amt":      int(row.get("orgn_ntby_tr_pbmn", 0)),
                "scrt_amt":      int(row.get("scrt_ntby_tr_pbmn", 0)),
                "ivtr_amt":      int(row.get("ivtr_ntby_tr_pbmn", 0)),
                "pe_fund_amt":   int(row.get("pe_fund_ntby_tr_pbmn", 0)),
                "bank_amt":      int(row.get("bank_ntby_tr_pbmn", 0)),
                "insu_amt":      int(row.get("insu_ntby_tr_pbmn", 0)),
                "mrbn_amt":      int(row.get("mrbn_ntby_tr_pbmn", 0)),
                "fund_amt":      int(row.get("fund_ntby_tr_pbmn", 0)),
                "etc_amt":       int(row.get("etc_ntby_tr_pbmn", 0)),
                "etc_corp_amt":  int(row.get("etc_corp_ntby_tr_pbmn", 0)),
                "etc_orgt_amt":  int(row.get("etc_orgt_ntby_tr_pbmn", 0)),
                # ── 외국인 매수/매도 분리 (거래강도 분석용) ───────────
                "frgn_buy_vol":  int(row.get("frgn_shnu_vol", 0)),
                "frgn_sell_vol": int(row.get("frgn_seln_vol", 0)),
                "frgn_buy_amt":  int(row.get("frgn_shnu_tr_pbmn", 0)),
                "frgn_sell_amt": int(row.get("frgn_seln_tr_pbmn", 0)),
            })
        except Exception:
            pass
    return out


def fetch_investor_estimate(stock_code: str, market: str = "J") -> list:
    """외인기관 추정가집계 (장중 5구간, 증권사 제출 추정치).

    반환: [{time_slot, frgn_est_qty, frgn_est_amt, orgn_est_qty, orgn_est_amt}]
    """
    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/quotations/investor-trend-estimate",
        tr_id="HHPTJ04160200",
        params={
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD":         stock_code,
        },
    )
    if res.get("rt_cd") != "0":
        return []

    out = []
    for row in res.get("output", []) or res.get("output1", []):
        try:
            out.append({
                "time_slot":    row.get("stck_cntg_hour") or row.get("trdt_whol_tm_no", ""),
                "frgn_est_qty": int(row.get("frgn_ntby_qty", 0)),
                "frgn_est_amt": int(row.get("frgn_ntby_tr_pbmn", 0)),
                "orgn_est_qty": int(row.get("orgn_ntby_qty", 0)),
                "orgn_est_amt": int(row.get("orgn_ntby_tr_pbmn", 0)),
            })
        except Exception:
            pass
    return out


def fetch_foreign_flow(stock_code: str, market: str = "J") -> list:
    """외국계 창구 순매수추이 (장중 누적 시계열).

    반환: [{time, cum_buy_qty, cum_buy_amt, cum_sell_qty, cum_sell_amt, net_qty, net_amt}]
    """
    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/quotations/frgnmem-pchs-trend",
        tr_id="FHKST644400C0",
        params={
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD":         stock_code,
        },
    )
    if res.get("rt_cd") != "0":
        return []

    out = []
    for row in res.get("output", []) or res.get("output1", []):
        try:
            out.append({
                "time":         row.get("stck_cntg_hour") or row.get("bsop_hour", ""),
                "cum_buy_qty":  int(row.get("seln_cntg_qty", 0)),
                "cum_buy_amt":  int(row.get("seln_tr_pbmn", 0)),
                "cum_sell_qty": int(row.get("shnu_cntg_qty", 0)),
                "cum_sell_amt": int(row.get("shnu_tr_pbmn", 0)),
                "net_qty":      int(row.get("ntby_cntg_qty", 0)),
                "net_amt":      int(row.get("ntby_tr_pbmn", 0)),
            })
        except Exception:
            pass
    return out


def analyze_investor_signal(stock_code: str, market: str = "J") -> dict:  # noqa: market unused, always J
    """투자자별 매매 분석 9분류 — 시그널 추출 (캐싱 적용)."""
    data = cached_call(
        "investor", stock_code, smart_ttl("investor"),
        lambda: fetch_investor_flow(stock_code, market),
    )
    if not data:
        return {"available": False, "error": "데이터 없음"}

    data.sort(key=lambda x: x["date"])
    last = data[-1]

    def sum_amt(field, n):
        if len(data) < n:
            return 0
        return sum(d[field] for d in data[-n:]) / 100  # 백만원 → 억

    frgn_5d      = sum_amt("frgn_amt", 5)
    frgn_20d     = sum_amt("frgn_amt", 20)
    frgn_reg_5d  = sum_amt("frgn_reg_amt", 5)   # 등록 외국인 (장기/기관성)
    frgn_nreg_5d = sum_amt("frgn_nreg_amt", 5)  # 비등록 (헤지펀드성)
    orgn_5d  = sum_amt("orgn_amt", 5)
    orgn_20d = sum_amt("orgn_amt", 20)
    prsn_5d  = sum_amt("prsn_amt", 5)

    # 사모 + 기타법인 (숨은 수급)
    pe_5d      = sum_amt("pe_fund_amt", 5)
    pe_20d     = sum_amt("pe_fund_amt", 20)
    corp_5d    = sum_amt("etc_corp_amt", 5)
    corp_20d   = sum_amt("etc_corp_amt", 20)
    hidden_5d  = pe_5d + corp_5d
    hidden_20d = pe_20d + corp_20d

    smart_5d  = frgn_5d + orgn_5d
    smart_20d = frgn_20d + orgn_20d

    triggers_buy  = []
    triggers_sell = []

    # ── 외국인 ────────────────────────────────────────
    if frgn_20d >= 30:
        triggers_buy.append(f"외국인 20일 +{frgn_20d:.1f}억 순매수")
    if frgn_5d >= 10 and frgn_20d > 0:
        triggers_buy.append(f"외국인 5일 +{frgn_5d:.1f}억 (단기 가속)")
    if frgn_20d <= -30:
        triggers_sell.append(f"외국인 20일 {frgn_20d:.1f}억 순매도")
    if frgn_5d <= -10:
        triggers_sell.append(f"외국인 5일 {frgn_5d:.1f}억 순매도")

    # ── 기관 ─────────────────────────────────────────
    if orgn_20d >= 30:
        triggers_buy.append(f"기관 20일 +{orgn_20d:.1f}억 순매수")
    if orgn_20d <= -30:
        triggers_sell.append(f"기관 20일 {orgn_20d:.1f}억 순매도")

    # ── 스마트머니 합산 ───────────────────────────────
    if smart_5d >= 20 and smart_20d > 0:
        triggers_buy.append(f"스마트머니 5일 +{smart_5d:.1f}억 (외인+기관 동반)")
    if smart_5d < -20 and prsn_5d > 20:
        triggers_sell.append(f"분배 패턴: 스마트머니 {smart_5d:.1f}억 ↔ 개인 +{prsn_5d:.1f}억")

    # ── 사모·기타법인 (숨은 수급) ─────────────────────
    if hidden_20d >= 20:
        triggers_buy.append(f"사모+기타법인 20일 +{hidden_20d:.1f}억 (숨은 매집)")
    if pe_5d >= 10:
        triggers_buy.append(f"사모 5일 +{pe_5d:.1f}억 순매수")
    if corp_5d >= 10:
        triggers_buy.append(f"기타법인 5일 +{corp_5d:.1f}억 순매수")

    return {
        "available":     True,
        "n_days":        len(data),
        "last_date":     last["date"],
        # 외국인 (합산 + 등록/비등록 분리)
        "frgn_5d":       frgn_5d,
        "frgn_20d":      frgn_20d,
        "frgn_reg_5d":   frgn_reg_5d,   # 등록: 장기투자자·펀드
        "frgn_nreg_5d":  frgn_nreg_5d,  # 비등록: 헤지펀드·단기
        # 기관/개인
        "orgn_5d":       orgn_5d,
        "orgn_20d":      orgn_20d,
        "prsn_5d":       prsn_5d,
        "smart_5d":      smart_5d,
        "smart_20d":     smart_20d,
        # 숨은 수급
        "pe_fund_5d":    pe_5d,
        "pe_fund_20d":   pe_20d,
        "etc_corp_5d":   corp_5d,
        "etc_corp_20d":  corp_20d,
        "hidden_5d":     hidden_5d,
        "hidden_20d":    hidden_20d,
        # 시그널
        "buy_signals":   triggers_buy,
        "sell_signals":  triggers_sell,
        "data":          data,
    }
