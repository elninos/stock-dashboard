"""Shared utilities for stock transaction parsers."""
import pandas as pd

BASE_DRIVE = "/Users/r/Library/CloudStorage/GoogleDrive-srshin614@gmail.com/내 드라이브"
BACKDATA = f"{BASE_DRIVE}/03.Finance/Backdata"
NH_DIR = f"{BACKDATA}/01.NH증권"
NAMU_DIR = f"{BACKDATA}/02.나무증권"
TOSS_DIR = f"{BACKDATA}/04.토스"
OUTPUT = "/Users/r/Documents/Claude/stock-dashboard/transactions.json"


def safe_int(val):
    if isinstance(val, (int, float)):
        return int(val)
    try:
        return int(float(str(val)))
    except (ValueError, TypeError):
        return 0


def safe_float(val):
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def normalize_date(val):
    """Convert '2026.04.09' → '2026-04-09'."""
    return str(val).replace(".", "-") if pd.notna(val) else ""


def effective_amount(settled, amount):
    """Return settled amount if positive, otherwise raw amount."""
    return settled if settled > 0 else amount


def make_tx(date, account, broker, tx_type, stock="", qty=0, price=0,
            amount=0, fee=0, tax=0, currency="KRW", note=None):
    """Create a standardized transaction dict."""
    tx = {
        "date": date, "account": account, "broker": broker,
        "type": tx_type, "stock": stock, "qty": qty,
        "price": price, "amount": amount, "fee": fee,
        "tax": tax, "currency": currency,
    }
    if note:
        tx["note"] = note
    return tx


def read_xls_html(filepath):
    """Read .xls files that are actually HTML format (NH증권 exports).

    NH HTS exports HTML disguised as .xls with a 2-row header (상세 format).
    Reading with header=0 keeps the second header row as the first data row,
    matching the structure of genuine .xlsx files so the parser can detect
    상세 format via df.iloc[0]['수량'] == '단가'.
    """
    dfs = pd.read_html(filepath, encoding="euc-kr", header=0)
    return dfs[0]


class LendingFeeAllocator:
    """Allocate lending fees to stocks based on active lending positions."""

    def __init__(self, lending_events):
        """lending_events: list of (date, stock, delta) tuples.
        Delta convention: positive for 출고 (lent out), negative for 상환입고 (returned).
        """
        self.events = sorted(lending_events, key=lambda x: x[0])

    def allocate(self, fee_date, stock_name, amount, account, broker, tax=0):
        """Returns list of lending_fee transaction dicts."""
        if stock_name:
            return [make_tx(fee_date, account, broker, "lending_fee",
                            stock=stock_name, amount=amount, tax=tax)]
        positions = {}
        for evt_date, evt_stock, delta in self.events:
            if evt_date <= fee_date:
                positions[evt_stock] = positions.get(evt_stock, 0) + delta
        active = {k: v for k, v in positions.items() if v > 0}
        total_lent = sum(active.values())
        if total_lent > 0:
            return [make_tx(fee_date, account, broker, "lending_fee",
                            stock=stk, amount=round(amount * q / total_lent))
                    for stk, q in active.items()]
        return [make_tx(fee_date, account, broker, "lending_fee", amount=amount)]


class CashBalanceTracker:
    """Track 예수금잔액 for 신용대출매도 auto-repayment via reverse-engineering.
    Formula: auto_repay = 정산금액 - (curr_cash - prev_cash) - 이자
    """

    def __init__(self):
        self.prev = None

    def update(self, cash_bal):
        if cash_bal is not None:
            self.prev = cash_bal

    def calc_auto_repayment(self, curr_cash_bal, settled, interest):
        """Returns auto-repayment principal amount, or 0 if not calculable."""
        if self.prev is None or curr_cash_bal is None or settled <= 0:
            return 0
        cash_increase = curr_cash_bal - self.prev
        auto_repay = settled - cash_increase - interest
        return auto_repay if auto_repay > 0 else 0
