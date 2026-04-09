"""NH투자증권 Excel parser (간략/상세 format)."""
import pandas as pd
import os
import glob

from .common import (safe_int, safe_float, read_xls_html, make_tx,
                     normalize_date, LendingFeeAllocator, CashBalanceTracker)

BROKER = "NH투자증권"

BUY_DETAILS = {"매수", "코스피매수", "KOSDAQ매수", "외화증권매수", "K-OTC매수", "KONEX매수"}
SELL_DETAILS = {"매도", "코스피매도", "KOSDAQ매도", "외화증권매도", "K-OTC매도", "KONEX매도"}
DIVIDEND_DETAILS = {"배당금", "외화배당금입금", "ETF분배금입금"}
TRANSFER_DETAILS = {"대체출고", "대체입고"}
IPO_TYPES = {"공모주입고", "유상주", "무상주", "대여무상권리주입고", "외화무상주", "실권입고", "배당주"}
TRANSFER_OUT_TYPES = {"액면병합출고", "주식매수청구출고"}
DANSU_TYPES = {"배당단수주대금", "무상단수주대금", "대여무상단수주대금입금", "감자단수주대금"}
DEPOSIT_TYPES = {"입금", "대체입금", "이체입금", "타행이체입금", "타행환입금",
                 "미약정대체입금", "외화대체입금", "외화이체입금"}
WITHDRAWAL_TYPES = {"출금", "대체출금", "이체출금", "타행이체출금", "은행이체출금",
                    "오픈뱅킹출금이체", "미약정대체출금", "외화대체출금"}
LOAN_OUT_PREFIXES = [
    "수익증권담보대출현금상환(",
    "현금상환(",
    "예탁증권담보대출취소(",
    "수익증권담보대출취소(",
]
LOAN_IN_PREFIXES = [
    "수익증권담보대출현금상환취소(",
    "현금상환취소(",
    "예탁증권담보대출(",
    "수익증권담보대출(",
]
LENDING_DETAILS = {"대여출고", "대여상환입고"}


def _normalize_nh_detail(filepath):
    """Read NH Excel (간략 or 상세 format) and return a normalized DataFrame."""
    if filepath.endswith(".xls"):
        try:
            df = pd.read_excel(filepath)
        except Exception:
            df = read_xls_html(filepath)
    else:
        df = pd.read_excel(filepath)
    if len(df) == 0:
        return pd.DataFrame()

    first_val = str(df.iloc[0].get("수량", ""))
    if first_val == "단가":
        rows_data = []
        raw = df.iloc[1:].values.tolist()
        cols = list(df.columns)
        # Pre-compute column indices
        ci = {c: cols.index(c) for c in ("실거래일자", "거래유형", "상세내용", "종목명",
                                          "수량", "거래금액", "잔고", "이율", "수수료", "거래일자")}
        i = 0
        while i < len(raw):
            r1 = raw[i]
            date = str(r1[ci["실거래일자"]]) if pd.notna(r1[ci["실거래일자"]]) else ""
            if date and date.startswith("20"):
                r2 = raw[i + 1] if i + 1 < len(raw) else [None] * len(cols)
                rows_data.append({
                    "실거래일자": date,
                    "거래유형": r1[ci["거래유형"]] if pd.notna(r1[ci["거래유형"]]) else "",
                    "상세내용": str(r1[ci["상세내용"]]) if pd.notna(r1[ci["상세내용"]]) else "",
                    "종목명": r1[ci["종목명"]] if pd.notna(r1[ci["종목명"]]) else None,
                    "수량": safe_int(r1[ci["수량"]]),
                    "단가": safe_float(r2[ci["수량"]]),
                    "거래금액": safe_float(r1[ci["거래금액"]]),
                    "정산금액": safe_float(r2[ci["거래금액"]]),
                    "잔고": safe_int(r1[ci["잔고"]]),
                    "예수금잔액": safe_float(r2[ci["잔고"]]),
                    "수수료": safe_float(r1[ci["수수료"]]),
                    "세금": safe_float(r2[ci["수수료"]]),
                    "이자": safe_float(r2[ci["이율"]]),
                    "거래일자": str(r1[ci["거래일자"]]) if pd.notna(r1[ci["거래일자"]]) else "",
                })
                i += 2
            else:
                i += 1
        return pd.DataFrame(rows_data)
    else:
        df["이자"] = 0
        df["예수금잔액"] = None
        if "정산금액" not in df.columns:
            df["정산금액"] = 0
        return df


_NOTE_DETAILS = {"액면병합출고", "주식매수청구출고", "액면병합입고", "감자출고", "감자입고"}


def _stock_txs(df, details, tx_type, account):
    """Generate transactions for simple stock in/out rows (감자, 액면병합, etc.)."""
    txs = []
    for _, row in df[df["상세내용"].isin(details)].iterrows():
        if pd.isna(row["종목명"]) or int(row["수량"]) == 0:
            continue
        detail = str(row["상세내용"])
        txs.append(make_tx(
            date=normalize_date(row["실거래일자"]),
            account=account, broker=BROKER, tx_type=tx_type,
            stock=str(row["종목명"]), qty=int(row["수량"]),
            price=safe_float(row["단가"]),
            note=detail if detail in _NOTE_DETAILS else None,
        ))
    return txs


def parse_nh_excel(folder_path, account_name):
    """Parse NH투자증권 Excel files (간략 or 상세 format)."""
    transactions = []
    for f in glob.glob(os.path.join(folder_path, "*.xlsx")) + glob.glob(os.path.join(folder_path, "*.xls")):
        df = _normalize_nh_detail(f)
        if df.empty:
            continue
        # NH files are reverse-chronological
        df = df.iloc[::-1].reset_index(drop=True)

        # Collect 신용대출매도 types dynamically (e.g. 코스피/KOSDAQ variants)
        credit_sell_details = {d for d in df["상세내용"].dropna().unique()
                               if d.startswith("신용대출매도")}
        all_sell = SELL_DETAILS | credit_sell_details

        # Buy/Sell
        for _, row in df[df["상세내용"].isin(BUY_DETAILS | all_sell)].iterrows():
            detail = str(row["상세내용"])
            transactions.append(make_tx(
                date=normalize_date(row["실거래일자"]),
                account=account_name, broker=BROKER,
                tx_type="buy" if detail in BUY_DETAILS else "sell",
                stock=str(row["종목명"]),
                qty=safe_int(row["수량"]), price=safe_float(row["단가"]),
                amount=safe_float(row["거래금액"]),
                fee=safe_float(row["수수료"]), tax=safe_float(row["세금"]),
            ))

        # Dividends
        for _, row in df[df["상세내용"].isin(DIVIDEND_DETAILS)].iterrows():
            transactions.append(make_tx(
                date=normalize_date(row["실거래일자"]),
                account=account_name, broker=BROKER, tx_type="dividend",
                stock=str(row["종목명"]) if pd.notna(row["종목명"]) else "Unknown",
                amount=safe_float(row["거래금액"]), tax=safe_float(row["세금"]),
            ))

        # Transfers (대체출고/대체입고)
        for _, row in df[df["상세내용"].isin(TRANSFER_DETAILS)].iterrows():
            if pd.isna(row["종목명"]) or safe_int(row["수량"]) == 0:
                continue
            transactions.append(make_tx(
                date=normalize_date(row["실거래일자"]),
                account=account_name, broker=BROKER,
                tx_type="transfer_out" if row["상세내용"] == "대체출고" else "transfer_in",
                stock=str(row["종목명"]), qty=safe_int(row["수량"]),
                price=safe_float(row["단가"]),
            ))

        # IPO/Rights/Bonus shares
        for _, row in df[df["상세내용"].isin(IPO_TYPES)].iterrows():
            if pd.isna(row["종목명"]) or safe_int(row["수량"]) == 0:
                continue
            p = safe_float(row["단가"])
            q = safe_int(row["수량"])
            transactions.append(make_tx(
                date=normalize_date(row["실거래일자"]),
                account=account_name, broker=BROKER, tx_type="buy",
                stock=str(row["종목명"]), qty=q, price=p, amount=q * p if p > 0 else 0,
            ))

        # 액면병합출고/주식매수청구출고
        transactions.extend(_stock_txs(df, TRANSFER_OUT_TYPES, "transfer_out", account_name))
        # 액면병합입고
        transactions.extend(_stock_txs(df, {"액면병합입고"}, "transfer_in", account_name))
        # 감자출고/감자입고
        transactions.extend(_stock_txs(df, {"감자출고"}, "transfer_out", account_name))
        transactions.extend(_stock_txs(df, {"감자입고"}, "transfer_in", account_name))

        # 주식매수청구대금입금 (수량은 주식매수청구출고에서 이미 차감됨)
        for _, row in df[df["상세내용"] == "주식매수청구대금입금"].iterrows():
            if pd.isna(row["종목명"]):
                continue
            transactions.append(make_tx(
                date=normalize_date(row["실거래일자"]),
                account=account_name, broker=BROKER, tx_type="sell",
                stock=str(row["종목명"]),
                qty=0,
                amount=safe_float(row["거래금액"]),
                tax=safe_float(row["세금"]),
                note="주식매수청구",
            ))

        # 청산출고
        transactions.extend(_stock_txs(df, {"청산출고"}, "transfer_out", account_name))

        # 분배금입금
        for _, row in df[df["상세내용"] == "분배금입금"].iterrows():
            if pd.isna(row["종목명"]):
                continue
            transactions.append(make_tx(
                date=normalize_date(row["실거래일자"]),
                account=account_name, broker=BROKER, tx_type="sell",
                stock=str(row["종목명"]),
                qty=safe_int(row["수량"]),
                amount=safe_float(row["정산금액"]),
                tax=safe_float(row["세금"]),
            ))

        # 대여배당금입금
        for _, row in df[df["상세내용"] == "대여배당금입금"].iterrows():
            amt = safe_float(row["거래금액"])
            if amt <= 0:
                continue
            transactions.append(make_tx(
                date=normalize_date(row["실거래일자"]),
                account=account_name, broker=BROKER, tx_type="dividend",
                stock=str(row["종목명"]) if pd.notna(row["종목명"]) else "Unknown",
                amount=amt,
            ))

        # Loan interest payments
        for _, row in df[df["상세내용"].str.contains("이자", na=False)].iterrows():
            amt = safe_float(row["거래금액"])
            if amt <= 0:
                continue
            transactions.append(make_tx(
                date=normalize_date(row["실거래일자"]),
                account=account_name, broker=BROKER, tx_type="loan_interest",
                stock=str(row["종목명"]) if pd.notna(row["종목명"]) else "",
                amount=amt,
            ))

        # Fractional share cash
        for _, row in df[df["상세내용"].isin(DANSU_TYPES)].iterrows():
            amt = safe_float(row["거래금액"])
            if amt <= 0:
                continue
            transactions.append(make_tx(
                date=normalize_date(row["실거래일자"]),
                account=account_name, broker=BROKER, tx_type="dividend",
                stock=str(row["종목명"]) if pd.notna(row["종목명"]) else "Unknown",
                amount=amt, note=str(row["상세내용"]),
            ))

        # Build lending fee allocator
        lending_events = []
        for _, row in df[df["상세내용"].isin(LENDING_DETAILS)].iterrows():
            stock = str(row["종목명"]) if pd.notna(row["종목명"]) else None
            if not stock:
                continue
            lending_events.append((
                normalize_date(row["실거래일자"]), stock,
                safe_int(row["수량"]) if row["상세내용"] == "대여출고" else -safe_int(row["수량"]),
            ))
        allocator = LendingFeeAllocator(lending_events)
        cash_tracker = CashBalanceTracker()

        # Cash flows + Loans + Lending fees (single pass)
        for _, row in df.iterrows():
            detail = str(row["상세내용"]) if pd.notna(row["상세내용"]) else ""
            amount = safe_float(row["거래금액"])
            settled = safe_float(row["정산금액"])
            interest = safe_float(row["이자"])
            curr_cash = safe_float(row["예수금잔액"]) if pd.notna(row.get("예수금잔액")) else None
            val = settled if settled > 0 else amount
            date = normalize_date(row["실거래일자"])

            if val <= 0 and not detail.startswith("신용대출매도"):
                cash_tracker.update(curr_cash)
                continue

            tx_type = None
            note = ""

            if detail.startswith("신용대출매도"):
                auto_repay = cash_tracker.calc_auto_repayment(curr_cash, settled, interest)
                if auto_repay > 0:
                    transactions.append(make_tx(
                        date=date, account=account_name, broker=BROKER,
                        tx_type="loan_out",
                        stock=str(row["종목명"]) if pd.notna(row["종목명"]) else "",
                        amount=auto_repay,
                        note=f"신용대출매도 자동상환 (이자 {int(interest)})",
                    ))
                cash_tracker.update(curr_cash)
                continue

            if detail in DEPOSIT_TYPES:
                tx_type = "deposit"
            elif detail in WITHDRAWAL_TYPES:
                tx_type = "withdrawal"
            elif any(detail.startswith(k) for k in LOAN_OUT_PREFIXES):
                tx_type = "loan_out"
                if detail.startswith("현금상환(") or detail.startswith("수익증권담보대출현금상환("):
                    if interest > 0:
                        val = val - interest
                        note = f"이자 {int(interest)} 제외"
            elif any(detail.startswith(k) for k in LOAN_IN_PREFIXES):
                tx_type = "loan_in"
                if detail.startswith("현금상환취소(") or detail.startswith("수익증권담보대출현금상환취소("):
                    if interest > 0:
                        val = val - interest
                        note = f"이자 {int(interest)} 제외"
            elif detail == "대여수수료입금":
                stock_name = str(row["종목명"]) if pd.notna(row["종목명"]) else None
                transactions.extend(allocator.allocate(date, stock_name, val, account_name, BROKER))
                cash_tracker.update(curr_cash)
                continue

            if tx_type is None:
                cash_tracker.update(curr_cash)
                continue

            transactions.append(make_tx(
                date=date, account=account_name, broker=BROKER,
                tx_type=tx_type, amount=val, note=note if note else None,
            ))
            cash_tracker.update(curr_cash)

    return transactions
