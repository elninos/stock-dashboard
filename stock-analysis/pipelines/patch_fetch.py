# 오류난 함수들만 교체
import sys
from pathlib import Path

src = Path("/sessions/nifty-determined-feynman/mnt/stock-analysis/pipelines/fetch_daily.py")
code = src.read_text(encoding="utf-8")

# ── 1. fetch_short: 404 → 올바른 경로로 교체 ──────────────────────────────
OLD_SHORT = '''def fetch_short(ticker: str, token: str):
    """공매도 잔고·비율"""
    end   = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

    data = kis_get(token,
        "/uapi/domestic-stock/v1/quotations/inquire-short-selling",
        "FHPST04010000",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
        },
    )

    recent = []
    prev_balance = None
    alert = False
    for r in reversed(data.get("output", [])[:20]):
        date_raw = r.get("stck_bsop_date", "")
        if not date_raw:
            continue
        balance = int(r.get("smtn_seln_qty", 0) or 0)
        if prev_balance and prev_balance > 0:
            change_pct = (balance - prev_balance) / prev_balance * 100
            if change_pct >= 20:
                alert = True
        prev_balance = balance

        recent.append({
            "date": f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}",
            "short_volume":        int(r.get("cntg_vol", 0) or 0),
            "short_ratio":         round(float(r.get("seln_qty_smtn_rt", 0) or 0), 1),
            "short_balance":       balance,
            "short_balance_ratio": round(float(r.get("smtn_seln_qty_rt", 0) or 0), 1),
            "margin_balance":      int(r.get("bln_seln_qty", 0) or 0),
        })

    result = {"ticker": ticker, "recent_20d": recent, "alert": alert}
    save_json(CACHE_FILE["short"](ticker), result)
    log.info(f"[short] {ticker} {len(recent)}일 저장, alert={alert}")'''

NEW_SHORT = '''def fetch_short(ticker: str, token: str):
    """공매도 잔고·비율 — KIS: 국내주식 공매도 일별조회"""
    end   = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")

    data = kis_get(token,
        "/uapi/domestic-stock/v1/quotations/inquire-daily-short-selling",
        "FHPST04010000",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_ORG_ADJ_PRC": "0",
        },
    )

    recent = []
    prev_balance = None
    alert = False
    rows = data.get("output", []) or []
    for r in reversed(rows[:20]):
        date_raw = r.get("stck_bsop_date", "")
        if not date_raw:
            continue
        balance = int(r.get("smtn_seln_qty", 0) or 0)
        if prev_balance and prev_balance > 0:
            change_pct = (balance - prev_balance) / prev_balance * 100
            if change_pct >= 20:
                alert = True
        prev_balance = balance
        recent.append({
            "date": f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}",
            "short_volume":        int(r.get("shnu_qty", 0) or 0),
            "short_ratio":         round(float(r.get("shnu_qty_smtn_rt", 0) or 0), 1),
            "short_balance":       balance,
            "short_balance_ratio": round(float(r.get("smtn_seln_qty_rt", 0) or 0), 1),
            "margin_balance":      int(r.get("bln_qty", 0) or 0),
        })

    result = {"ticker": ticker, "recent_20d": recent, "alert": alert}
    save_json(CACHE_FILE["short"](ticker), result)
    log.info(f"[short] {ticker} {len(recent)}일 저장, alert={alert}")'''

# ── 2. fetch_broker: 500 → tr_id 수정 + 파라미터 정리 ─────────────────────
OLD_BROKER = '''def fetch_broker(ticker: str, token: str):
    """거래원 — 기관·외국인 창구"""
    data = kis_get(token,
        "/uapi/domestic-stock/v1/quotations/inquire-member",
        "FHKST01010600",
        {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
    )

    output = data.get("output1", {}) or {}

    def broker_pair(buy_nm_key, buy_amt_key, sell_nm_key, sell_amt_key, r):
        name = r.get(buy_nm_key, "")
        amt  = int(r.get(buy_amt_key, 0) or 0)
        return {"broker": name, "amount": amt} if name else None

    top_buy, top_sell = [], []
    for i in range(1, 6):
        b = output.get(f"mmcm_nm{i}", "")
        bamt = int(output.get(f"seln_vol{i}", 0) or 0)
        s = output.get(f"mmcm_nm{i}", "")
        samt = int(output.get(f"shnu_vol{i}", 0) or 0)
        if b:
            top_buy.append({"broker": b, "amount": bamt})
        if s:
            top_sell.append({"broker": s, "amount": samt})

    result = {
        "ticker": ticker,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "top_buy":  top_buy[:5],
        "top_sell": top_sell[:5],
        "foreign_desk_concentration": False,
        "program_buy_ratio": 0.0,
    }
    save_json(CACHE_FILE["broker"](ticker), result)
    log.info(f"[broker] {ticker} 저장 완료")'''

NEW_BROKER = '''def fetch_broker(ticker: str, token: str):
    """거래원 — 기관·외국인 창구"""
    data = kis_get(token,
        "/uapi/domestic-stock/v1/quotations/inquire-member",
        "FHKST01010600",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": datetime.now().strftime("%Y%m%d"),
        },
    )

    # output 또는 output1 둘 다 시도
    output = data.get("output1") or data.get("output") or {}
    if isinstance(output, list):
        output = output[0] if output else {}

    top_buy, top_sell = [], []
    for i in range(1, 6):
        buy_nm  = output.get(f"seln_mbcr_name{i}", "") or output.get(f"mmcm_nm{i}", "")
        buy_amt = int(output.get(f"seln_vol{i}", 0) or 0)
        sel_nm  = output.get(f"shnu_mbcr_name{i}", "") or output.get(f"mmcm_nm{i}", "")
        sel_amt = int(output.get(f"shnu_vol{i}", 0) or 0)
        if buy_nm:
            top_buy.append({"broker": buy_nm, "amount": buy_amt})
        if sel_nm:
            top_sell.append({"broker": sel_nm, "amount": sel_amt})

    result = {
        "ticker": ticker,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "top_buy":  top_buy[:5],
        "top_sell": top_sell[:5],
        "foreign_desk_concentration": False,
        "program_buy_ratio": 0.0,
    }
    save_json(CACHE_FILE["broker"](ticker), result)
    log.info(f"[broker] {ticker} 저장 완료")'''

# ── 3. fetch_financial: 500 → 파라미터 대소문자 통일 ─────────────────────
OLD_FIN = '''    inc = kis_get(token,
        "/uapi/domestic-stock/v1/finance/income-statement",
        "FHKST66430300",
        {"FID_DIV_CLS_CODE": "1", "fid_cond_mrkt_div_code": "J", "fid_input_iscd": ticker},
    )'''

NEW_FIN = '''    inc = kis_get(token,
        "/uapi/domestic-stock/v1/finance/income-statement",
        "FHKST66430300",
        {"FID_DIV_CLS_CODE": "1", "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
    )'''

# ── 4. fetch_market: 500 → KOSPI 지수용 tr_id + market div 수정 ──────────
OLD_MARKET_GET = '''    def get_closes(iscd):
        d = kis_get(token,
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKST03010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": iscd,
             "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": end,
             "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"},
        )
        prices = [int(r.get("stck_clpr", 0) or 0) for r in reversed(d.get("output2", [])) if r.get("stck_clpr")]
        return prices

    stock_prices  = get_closes(ticker)
    kospi_prices  = get_closes("0001")'''

NEW_MARKET_GET = '''    def get_stock_closes(iscd):
        d = kis_get(token,
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKST03010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": iscd,
             "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": end,
             "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"},
        )
        prices = [int(r.get("stck_clpr", 0) or 0) for r in reversed(d.get("output2", [])) if r.get("stck_clpr")]
        return prices

    def get_index_closes(iscd):
        d = kis_get(token,
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKUP03010100",
            {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": iscd,
             "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": end,
             "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"},
        )
        prices = [int(float(r.get("bstp_nmix_prpr", 0) or 0)) for r in reversed(d.get("output2", [])) if r.get("bstp_nmix_prpr")]
        return prices

    stock_prices  = get_stock_closes(ticker)
    kospi_prices  = get_index_closes("0001")'''

# ── 5. fetch_news: 500 → 파라미터 정리 ───────────────────────────────────
OLD_NEWS = '''    data = kis_get(token,
        "/uapi/domestic-stock/v1/quotations/news-title",
        "YNAS9001R",
        {"FID_NEWS_OFER_ENTP_CODE": "", "FID_COND_MRKT_DIV_CODE": "J",
         "FID_INPUT_ISCD": ticker, "FID_INPUT_DATE_1": "", "FID_INPUT_HOUR_1": ""},
    )'''

NEW_NEWS = '''    data = kis_get(token,
        "/uapi/domestic-stock/v1/quotations/news-title",
        "YNAS9001R",
        {
            "FID_NEWS_OFER_ENTP_CODE": "0",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": datetime.now().strftime("%Y%m%d"),
            "FID_INPUT_HOUR_1": "000000",
        },
    )'''

replacements = [
    (OLD_SHORT, NEW_SHORT, "short"),
    (OLD_BROKER, NEW_BROKER, "broker"),
    (OLD_FIN, NEW_FIN, "financial params"),
    (OLD_MARKET_GET, NEW_MARKET_GET, "market index"),
    (OLD_NEWS, NEW_NEWS, "news params"),
]

for old, new, label in replacements:
    if old in code:
        code = code.replace(old, new)
        print(f"✅ {label} 수정")
    else:
        print(f"❌ {label} - 매칭 실패")

src.write_text(code, encoding="utf-8")
print("\n저장 완료")
