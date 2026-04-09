"""나무증권 Excel parser (상세 format with 2-row pairs)."""
import pandas as pd
import os
import glob

from .common import (make_tx, safe_float, normalize_date, effective_amount,
                     LendingFeeAllocator, CashBalanceTracker)

BROKER = "나무증권"

BUY_TYPES = ["외화증권매수", "KOSDAQ매수", "코스피매수", "K-OTC매수", "KONEX매수"]
SELL_TYPES = ["외화증권매도", "KOSDAQ매도", "코스피매도", "K-OTC매도", "KONEX매도"]
DIVIDEND_TYPES = ["외화배당금입금", "배당금", "ETF분배금입금", "대여외화배당금입금"]
TRANSFER_IN_TYPES = [
    "대체입고", "타사대체입고", "공모주입고", "감자입고",
    "외화주식액면분할입고", "외화주식액면병합입고", "외화종목코드변경입고", "배당주",
]
TRANSFER_OUT_TYPES = [
    "대체출고", "감자출고",
    "외화주식액면분할출고", "외화주식액면병합출고", "외화종목코드변경출고",
]
DEPOSIT_TYPES = ["이체입금", "대체입금", "외화대체입금", "외화이체입금", "미약정대체입금"]
WITHDRAWAL_TYPES = ["이체출금", "대체출금", "외화대체출금", "은행이체출금", "오픈뱅킹출금이체"]
LOAN_IN_TYPES = ["예탁증권담보대출(주식담보대출)"]
LOAN_OUT_TYPES = ["현금상환(주식담보대출)", "예탁증권담보대출취소(주식담보대출)"]
LENDING_DETAILS = ("대여출고", "대여상환입고", "해외대여출고", "해외대여상환입고")
SKIP_TYPES = [
    "조건부매수", "환매도",
    "외화매수",
    "공모청약출금", "공모청약출금취소", "공모주환불금", "공모추가불입",
    "공모주청약수수료출금",
    "예탁금이용료", "외화예탁금이용료",
    "DR수수료", "외화제세금환급", "외화증권배당세금", "세금환급",
    "감자단수주대금", "배당단수주대금",
    "해외권리환전",
]


def _detect_currency(detail, isin_code):
    """Determine currency from transaction detail and ISIN code."""
    is_foreign = (
        detail.startswith("외화") or detail.startswith("해외")
        or detail == "대여외화배당금입금"
    )
    if not is_foreign:
        if isinstance(isin_code, str) and len(isin_code) > 2:
            if isin_code.isdigit() or (len(isin_code) == 6 and isin_code.isalnum()):
                return "KRW"
            if isin_code.startswith("KR"):
                return "KRW"
            if isin_code.startswith("JP"):
                return "JPY"
            return "USD"
        return "KRW"
    if isinstance(isin_code, str) and isin_code.startswith("JP"):
        return "JPY"
    return "USD"


def _safe_val(cell, exclude_labels=()):
    """Parse a cell value as float, returning 0 if NaN or a label string."""
    if cell is None or pd.isna(cell):
        return 0
    s = str(cell)
    if s in exclude_labels or s == "":
        return 0
    return safe_float(s)


def _parse_row_pairs(df):
    """Parse 2-row pairs from 나무증권 상세 format into list of dicts.
    Returns list in file order (reverse-chronological).
    """
    rows = []
    for i in range(1, len(df) - 1, 2):
        main = df.iloc[i]
        sub = df.iloc[i + 1] if i + 1 < len(df) else None

        if pd.isna(main["상세내용"]):
            continue

        detail = str(main["상세내용"])
        date_str = str(main["실거래일자"]).replace(".", "-") if pd.notna(main["실거래일자"]) else ""
        if not date_str:
            continue

        stock = str(main["종목명"]) if pd.notna(main["종목명"]) else ""
        qty = int(_safe_val(main["수량"], ("단가",)))
        amount = _safe_val(main["거래금액"], ("정산금액",))
        fee = _safe_val(main["수수료"], ("세금",))

        isin_code = str(sub["종목명"]) if sub is not None and pd.notna(sub["종목명"]) else ""
        price = _safe_val(sub["수량"], ("단가",)) if sub is not None else 0
        settled = _safe_val(sub["거래금액"], ("정산금액",)) if sub is not None else 0
        tax = _safe_val(sub["수수료"], ("세금",)) if sub is not None else 0
        interest = _safe_val(sub["이율"], ("이자",)) if sub is not None else 0
        cash_bal_raw = sub["잔고"] if sub is not None else None
        cash_bal = None
        if cash_bal_raw is not None and not pd.isna(cash_bal_raw) and str(cash_bal_raw) not in ("잔고금액", ""):
            try:
                cash_bal = float(str(cash_bal_raw).replace(",", ""))
            except ValueError:
                pass

        rows.append({
            "detail": detail, "date": date_str, "stock": stock,
            "qty": qty, "amount": amount, "fee": fee,
            "isin_code": isin_code, "price": price, "settled": settled,
            "tax": tax, "interest": interest, "cash_bal": cash_bal,
        })
    return rows


def parse_namu_excel(folder_path, account_name):
    """Parse 나무증권 Excel files (상세 format with 2-row pairs)."""
    transactions = []
    for f in glob.glob(os.path.join(folder_path, "*.xlsx")):
        df = pd.read_excel(f)
        if len(df) < 2:
            continue

        # Parse row pairs and reverse to chronological order
        parsed = _parse_row_pairs(df)
        parsed.reverse()

        # Build lending events for fee allocation
        lending_events = [
            (r["date"], r["stock"], int(r["qty"]) if "출고" in r["detail"] else -int(r["qty"]))
            for r in parsed if r["detail"] in LENDING_DETAILS and r["stock"]
        ]
        allocator = LendingFeeAllocator(lending_events)
        cash_tracker = CashBalanceTracker()

        for r in parsed:
            detail = r["detail"]
            date_str = r["date"]
            stock = r["stock"]
            qty = r["qty"]
            amount = r["amount"]
            fee = r["fee"]
            price = r["price"]
            settled = r["settled"]
            tax = r["tax"]
            interest = r["interest"]
            cash_bal = r["cash_bal"]
            currency = _detect_currency(detail, r["isin_code"])

            if detail in SKIP_TYPES:
                cash_tracker.update(cash_bal)
                continue

            if detail in BUY_TYPES:
                if qty == 0:
                    continue
                transactions.append(make_tx(
                    date_str, account_name, BROKER, "buy",
                    stock=stock, qty=qty, price=price,
                    amount=amount, fee=fee, tax=tax, currency=currency,
                ))
            elif detail in SELL_TYPES:
                if qty == 0:
                    continue
                transactions.append(make_tx(
                    date_str, account_name, BROKER, "sell",
                    stock=stock, qty=qty, price=price,
                    amount=amount, fee=fee, tax=tax, currency=currency,
                ))
            elif detail in DIVIDEND_TYPES:
                amt = effective_amount(settled, amount)
                if amt <= 0:
                    continue
                transactions.append(make_tx(
                    date_str, account_name, BROKER, "dividend",
                    stock=stock, amount=amt, tax=tax, currency=currency,
                ))
            elif detail in TRANSFER_IN_TYPES:
                if not stock or qty == 0:
                    continue
                transactions.append(make_tx(
                    date_str, account_name, BROKER, "transfer_in",
                    stock=stock, qty=qty, price=price, currency=currency,
                ))
            elif detail in TRANSFER_OUT_TYPES:
                if not stock or qty == 0:
                    continue
                transactions.append(make_tx(
                    date_str, account_name, BROKER, "transfer_out",
                    stock=stock, qty=qty, price=price, currency=currency,
                ))
            elif detail in DEPOSIT_TYPES:
                val = effective_amount(settled, amount)
                if val <= 0:
                    continue
                transactions.append(make_tx(
                    date_str, account_name, BROKER, "deposit",
                    amount=val, currency=currency,
                ))
            elif detail in WITHDRAWAL_TYPES:
                val = effective_amount(settled, amount)
                if val <= 0:
                    continue
                transactions.append(make_tx(
                    date_str, account_name, BROKER, "withdrawal",
                    amount=val, currency=currency,
                ))
            elif detail in LOAN_IN_TYPES:
                val = effective_amount(settled, amount)
                if val <= 0:
                    continue
                transactions.append(make_tx(
                    date_str, account_name, BROKER, "loan_in", amount=val,
                ))
            elif detail in LOAN_OUT_TYPES:
                val = effective_amount(settled, amount)
                if val <= 0:
                    cash_tracker.update(cash_bal)
                    continue
                if detail == "현금상환(주식담보대출)" and interest > 0:
                    val = val - interest
                transactions.append(make_tx(
                    date_str, account_name, BROKER, "loan_out", amount=val,
                ))
            elif detail == "대여수수료입금":
                val = effective_amount(settled, amount)
                if val <= 0:
                    continue
                transactions.extend(allocator.allocate(
                    date_str, stock if stock else None, val,
                    account_name, BROKER, tax=tax,
                ))
            elif detail in LENDING_DETAILS:
                pass  # tracked for position timeline only
            elif detail.startswith("신용대출매도") and "주식담보대출" in detail:
                auto_repay = cash_tracker.calc_auto_repayment(cash_bal, settled, interest)
                if auto_repay > 0:
                    transactions.append(make_tx(
                        date_str, account_name, BROKER, "loan_out",
                        stock=stock, amount=auto_repay,
                        note=f"신용대출매도 자동상환 (이자 {int(interest)})",
                    ))

            cash_tracker.update(cash_bal)

    return transactions
