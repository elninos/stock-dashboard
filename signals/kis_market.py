"""KIS API — 증시 거시 데이터.

APIs:
  FHKST649100C0 — 국내 증시자금 종합    (고객예탁금, 신용융자잔고, 펀드 등)
  FHPTJ04040000 — 시장별 투자자매매동향  (KOSPI/KOSDAQ 9분류 일별)
  FHPST04820000 — 공매도 상위 종목 랭킹
"""
from datetime import datetime, timedelta
from .kis_api import get_client, rate_limit, cached_call, smart_ttl


def fetch_market_funds(start: str = None, end: str = None) -> list:
    """증시자금 종합 일별 추이.

    start/end: YYYYMMDD (None이면 최근 60일)
    반환: [{date, deposit, deposit_chg, credit_loan, mktcap, turnover_ratio,
            uncollected_amt, futures_deposit, equity_fund, mixed_fund, bond_fund}]
    단위: 백만원 (deposit, credit_loan 등)
    """
    if not end:
        end = datetime.now().strftime("%Y%m%d")
    if not start:
        start = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")

    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/quotations/mktfunds",
        tr_id="FHKST649100C0",
        params={"FID_INPUT_DATE_1": start},
    )
    if res.get("rt_cd") != "0":
        return []

    out = []
    for row in res.get("output", []):
        try:
            out.append({
                "date":            row.get("bsop_date"),
                "deposit":         int(row.get("cust_dpmn_amt", 0)),           # 고객예탁금
                "deposit_chg":     int(row.get("cust_dpmn_amt_prdy_vrss", 0)), # 전일대비
                "credit_loan":     int(row.get("crdt_loan_rmnd", 0)),          # 신용융자잔고
                "mktcap":          int(row.get("hts_avls", 0)),                # 시가총액
                "turnover_ratio":  float(row.get("amt_tnrt", 0)),              # 금액회전율 (%)
                "uncollected_amt": int(row.get("uncl_amt", 0)),                # 미수금
                "futures_deposit": int(row.get("futs_tfam_amt", 0)),           # 선물예수금
                "equity_fund":     int(row.get("sttp_amt", 0)),                # 주식형펀드 설정액
                "mixed_fund":      int(row.get("mxtp_amt", 0)),                # 혼합형펀드
                "bond_fund":       int(row.get("bntp_amt", 0)),                # 채권형펀드
            })
        except Exception:
            pass
    return out


def fetch_market_investor(
    market: str = "J",
    start: str = None,
    end: str = None,
) -> list:
    """시장별 투자자 9분류 일별 매매동향. 최대 ~300일치 반환.

    market: "J"=KOSPI, "Q"=KOSDAQ
    FID_COND_MRKT_DIV_CODE는 항상 "U", FID_INPUT_ISCD_1은 KSP/KSQ 필수.
    반환: [{date, index_price, frgn_qty/amt, frgn_reg_amt, frgn_nreg_amt,
            prsn_qty/amt, orgn_qty/amt, pe_fund_qty/amt, etc_corp_qty/amt, ...}]
    """
    if not end:
        end = datetime.now().strftime("%Y%m%d")
    if not start:
        start = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")

    iscd      = "0001" if market == "J" else "1001"
    iscd_name = "KSP"  if market == "J" else "KSQ"

    client = get_client()
    rate_limit()

    # DATE_1 = as-of 날짜 (가장 최신), 역방향으로 ~300영업일 반환.
    # output[0]이 DATE_1 기준 최신, output[-1]이 가장 오래된 날짜.
    res = client.get(
        "/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market",
        tr_id="FHPTJ04040000",
        params={
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD":         iscd,
            "FID_INPUT_DATE_1":       end,    # as-of (최신) — output[0]이 이 날짜
            "FID_INPUT_ISCD_1":       iscd_name,  # KSP/KSQ 필수 — 빈값 시 9분류 전부 0
            "FID_INPUT_DATE_2":       start,  # 하한 날짜
            "FID_INPUT_ISCD_2":       iscd,
        },
    )
    if res.get("rt_cd") != "0":
        return []

    out = []
    for row in res.get("output", []):
        try:
            out.append({
                "date":           row.get("stck_bsop_date"),
                "index_price":    float(row.get("bstp_nmix_prpr", 0) or 0),
                "index_open":     float(row.get("bstp_nmix_oprc", 0) or 0),
                "index_high":     float(row.get("bstp_nmix_hgpr", 0) or 0),
                "index_low":      float(row.get("bstp_nmix_lwpr", 0) or 0),
                # ── 순매수량 (주) ───────────────────────────────────────
                "frgn_qty":       int(row.get("frgn_ntby_qty", 0)),
                "frgn_reg_qty":   int(row.get("frgn_reg_ntby_qty", 0)),   # 등록 외국인
                "frgn_nreg_qty":  int(row.get("frgn_nreg_ntby_qty", 0)),  # 비등록 (헤지펀드)
                "prsn_qty":       int(row.get("prsn_ntby_qty", 0)),
                "orgn_qty":       int(row.get("orgn_ntby_qty", 0)),
                "scrt_qty":       int(row.get("scrt_ntby_qty", 0)),
                "ivtr_qty":       int(row.get("ivtr_ntby_qty", 0)),
                "pe_fund_qty":    int(row.get("pe_fund_ntby_vol", 0)),
                "bank_qty":       int(row.get("bank_ntby_qty", 0)),
                "insu_qty":       int(row.get("insu_ntby_qty", 0)),
                "mrbn_qty":       int(row.get("mrbn_ntby_qty", 0)),
                "fund_qty":       int(row.get("fund_ntby_qty", 0)),
                "etc_qty":        int(row.get("etc_ntby_qty", 0)),
                "etc_corp_qty":   int(row.get("etc_corp_ntby_vol", 0)),
                "etc_orgt_qty":   int(row.get("etc_orgt_ntby_vol", 0)),
                # ── 순매수금액 (백만원) ─────────────────────────────────
                "frgn_amt":       int(row.get("frgn_ntby_tr_pbmn", 0)),
                "frgn_reg_amt":   int(row.get("frgn_reg_ntby_pbmn", 0)),  # 등록 외국인
                "frgn_nreg_amt":  int(row.get("frgn_nreg_ntby_pbmn", 0)), # 비등록 (헤지펀드)
                "prsn_amt":       int(row.get("prsn_ntby_tr_pbmn", 0)),
                "orgn_amt":       int(row.get("orgn_ntby_tr_pbmn", 0)),
                "scrt_amt":       int(row.get("scrt_ntby_tr_pbmn", 0)),
                "ivtr_amt":       int(row.get("ivtr_ntby_tr_pbmn", 0)),
                "pe_fund_amt":    int(row.get("pe_fund_ntby_tr_pbmn", 0)),
                "bank_amt":       int(row.get("bank_ntby_tr_pbmn", 0)),
                "insu_amt":       int(row.get("insu_ntby_tr_pbmn", 0)),
                "mrbn_amt":       int(row.get("mrbn_ntby_tr_pbmn", 0)),
                "fund_amt":       int(row.get("fund_ntby_tr_pbmn", 0)),
                "etc_amt":        int(row.get("etc_ntby_tr_pbmn", 0)),
                "etc_corp_amt":   int(row.get("etc_corp_ntby_tr_pbmn", 0)),
                "etc_orgt_amt":   int(row.get("etc_orgt_ntby_tr_pbmn", 0)),
            })
        except Exception:
            pass
    return out


def fetch_short_top(market: str = "J", period_days: int = 5) -> list:
    """공매도 상위 종목 랭킹 (최대 30건).

    market: "J"=KOSPI, "Q"=KOSDAQ, "A"=전체
    period_days: 1·2·3·4·5(1주)·14(2주)·21(3주)
    반환: [{code, name, close, short_vol, short_vol_ratio, short_amt, short_amt_ratio,
            date_from, date_to, avg_price}]
    """
    # FID_INPUT_CNT_1 코드 매핑 (D 기준)
    _cnt_map = {1: "0", 2: "1", 3: "2", 4: "3", 5: "4", 7: "4", 14: "9", 21: "14"}
    cnt = _cnt_map.get(period_days, "4")

    iscd_map = {"J": "0001", "Q": "1001", "A": "0000"}
    iscd = iscd_map.get(market, "0001")

    client = get_client()
    rate_limit()

    res = client.get(
        "/uapi/domestic-stock/v1/ranking/short-sale",
        tr_id="FHPST04820000",
        params={
            "FID_COND_MRKT_DIV_CODE": "J",     # 항상 J (주식)
            "FID_COND_SCR_DIV_CODE":  "20482",  # 고정값 — 20601은 틀린 값
            "FID_INPUT_ISCD":         iscd,
            "FID_PERIOD_DIV_CODE":    "D",
            "FID_INPUT_CNT_1":        cnt,
            "FID_TRGT_EXLS_CLS_CODE": "",
            "FID_TRGT_CLS_CODE":      "",
            "FID_APLY_RANG_PRC_1":    "",
            "FID_APLY_RANG_PRC_2":    "",
            "FID_APLY_RANG_VOL":      "",
        },
    )
    if res.get("rt_cd") != "0":
        return []

    out = []
    for row in res.get("output", []):
        try:
            out.append({
                "code":            row.get("mksc_shrn_iscd"),
                "name":            row.get("hts_kor_isnm"),
                "close":           int(row.get("stck_prpr", 0)),
                "short_vol":       int(row.get("ssts_cntg_qty", 0)),
                "short_vol_ratio": float(row.get("ssts_vol_rlim", 0) or 0),
                "short_amt":       int(row.get("ssts_tr_pbmn", 0)),
                "short_amt_ratio": float(row.get("ssts_tr_pbmn_rlim", 0) or 0),
                "date_from":       row.get("stnd_date1"),
                "date_to":         row.get("stnd_date2"),
                "avg_price":       int(row.get("avrg_prc", 0)),
            })
        except Exception:
            pass
    return out


def get_market_snapshot(market: str = "J") -> dict:
    """증시 거시 현황 요약 (캐싱 적용). 대시보드 상단 용."""
    today    = datetime.now().strftime("%Y%m%d")
    start_60 = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")

    funds = cached_call(
        "mktfunds", "all", smart_ttl("mktfunds"),
        lambda: fetch_market_funds(start_60, today),
    )
    investor = cached_call(
        "mktinv", market, smart_ttl("mktinv"),
        lambda: fetch_market_investor(market, start_60, today),
    )

    last_fund = funds[-1] if funds else {}
    last_inv  = investor[0] if investor else {}   # output[0]이 최신 (DATE_1 기준 역방향)

    return {
        "available":      bool(last_fund),
        "last_date":      last_fund.get("date"),
        "deposit":        last_fund.get("deposit", 0),
        "deposit_chg":    last_fund.get("deposit_chg", 0),
        "credit_loan":    last_fund.get("credit_loan", 0),
        "frgn_amt":       last_inv.get("frgn_amt", 0),
        "frgn_reg_amt":   last_inv.get("frgn_reg_amt", 0),   # 등록 외국인
        "frgn_nreg_amt":  last_inv.get("frgn_nreg_amt", 0),  # 비등록 (헤지펀드)
        "orgn_amt":       last_inv.get("orgn_amt", 0),
        "prsn_amt":       last_inv.get("prsn_amt", 0),
        "pe_fund_amt":    last_inv.get("pe_fund_amt", 0),
        "etc_corp_amt":   last_inv.get("etc_corp_amt", 0),
        "inv_date":       last_inv.get("date"),
        "funds_data":     funds,
        "investor_data":  investor,
    }
