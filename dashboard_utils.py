"""Pure utility functions for build_dashboard.py.

No global state — all functions take explicit arguments.
"""
import json
import urllib.request
from datetime import datetime


# ===== Exchange rates =====

def fetch_fx_rate(pair: str, fallback: float, divisor: float = 1.0) -> float:
    """Fetch KRW rate for a currency pair from Naver Finance API."""
    try:
        url = f"https://api.stock.naver.com/marketindex/exchange/FX_{pair}KRW"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
            rate_str = data.get("exchangeInfo", data).get("closePrice", "0").replace(",", "")
            return float(rate_str) / divisor
    except Exception:
        return fallback


# ===== Portfolio calculations =====

def calc_stock_metrics(trades):
    """Calculate metrics for a list of buy/sell trades for one stock in one account."""
    buys = []  # [(qty, price, date)]
    total_invested = 0
    total_returned = 0
    total_fees = 0
    total_tax = 0
    total_dividends = 0
    realized_pnl = 0
    cashflows = []  # (date, amount) for IRR - negative=outflow, positive=inflow
    current_qty = 0
    current_cost = 0  # total cost basis of current holdings

    for tx in sorted(trades, key=lambda x: x["date"]):
        if tx["type"] == "buy":
            buys.append({"qty": tx["qty"], "price": tx["price"], "date": tx["date"]})
            cost = tx["amount"] + tx["fee"]
            total_invested += tx["amount"]
            total_fees += tx["fee"]
            current_qty += tx["qty"]
            current_cost += cost
            cashflows.append((tx["date"], -cost))
        elif tx["type"] == "sell":
            sell_qty = tx["qty"]
            sell_amount = tx["amount"] - tx["fee"] - tx["tax"]
            total_returned += tx["amount"]
            total_fees += tx["fee"]
            total_tax += tx["tax"]

            # FIFO cost basis
            cost_basis = 0
            remaining = sell_qty
            while remaining > 0 and buys:
                b = buys[0]
                take = min(remaining, b["qty"])
                cost_basis += take * b["price"]
                b["qty"] -= take
                remaining -= take
                if b["qty"] == 0:
                    buys.pop(0)

            realized_pnl += (tx["amount"] - cost_basis - tx["fee"] - tx["tax"])
            current_qty -= sell_qty
            if current_qty > 0 and total_invested > 0:
                current_cost = sum(b["qty"] * b["price"] for b in buys)
            else:
                current_cost = 0
            cashflows.append((tx["date"], sell_amount))
        elif tx["type"] == "transfer_out":
            # Stock moved OUT of this account — remove from FIFO like a sell but no cash
            out_qty = tx["qty"]
            remaining = out_qty
            while remaining > 0 and buys:
                b = buys[0]
                take = min(remaining, b["qty"])
                b["qty"] -= take
                remaining -= take
                if b["qty"] == 0:
                    buys.pop(0)
            current_qty -= out_qty
            if current_qty > 0:
                current_cost = sum(b["qty"] * b["price"] for b in buys)
            else:
                current_cost = 0
        elif tx["type"] == "transfer_in":
            # Stock moved IN — add to FIFO at the transfer price (or 0 if unknown)
            price = tx["price"] if tx["price"] > 0 else 0
            buys.append({"qty": tx["qty"], "price": price, "date": tx["date"]})
            current_qty += tx["qty"]
            current_cost += tx["qty"] * price
        elif tx["type"] == "dividend":
            total_dividends += tx["amount"]
            cashflows.append((tx["date"], tx["amount"]))

    # Clamp negative positions to 0 (missing historical buy data)
    if current_qty < 0:
        current_qty = 0
        current_cost = 0
        buys = []

    avg_buy_price = 0
    if current_qty > 0 and buys:
        total_buy_qty = sum(b["qty"] for b in buys)
        avg_buy_price = sum(b["qty"] * b["price"] for b in buys) / total_buy_qty if total_buy_qty > 0 else 0

    return {
        "current_qty": current_qty,
        "avg_buy_price": round(avg_buy_price),
        "current_cost": round(current_cost),
        "total_invested": round(total_invested),
        "total_returned": round(total_returned),
        "realized_pnl": round(realized_pnl),
        "total_dividends": round(total_dividends),
        "total_fees": round(total_fees),
        "total_tax": round(total_tax),
        "cashflows": cashflows,
    }


def calc_xirr(cashflows, guess=0.1):
    """Calculate XIRR from list of (date_str, amount) tuples."""
    if not cashflows or len(cashflows) < 2:
        return None
    has_neg = any(cf[1] < 0 for cf in cashflows)
    has_pos = any(cf[1] > 0 for cf in cashflows)
    if not has_neg or not has_pos:
        return None

    dates = [datetime.strptime(cf[0], "%Y-%m-%d") for cf in cashflows]
    amounts = [cf[1] for cf in cashflows]
    d0 = dates[0]
    days = [(d - d0).days / 365.25 for d in dates]

    def npv(rate):
        return sum(a / (1 + rate) ** t for a, t in zip(amounts, days))

    def dnpv(rate):
        return sum(-t * a / (1 + rate) ** (t + 1) for a, t in zip(amounts, days))

    rate = guess
    for _ in range(200):
        n = npv(rate)
        d = dnpv(rate)
        if abs(d) < 1e-12:
            break
        new_rate = rate - n / d
        if abs(new_rate - rate) < 1e-8:
            return new_rate
        rate = new_rate
        if rate < -0.99:
            rate = -0.99
        if rate > 10:
            return None
    return rate if -1 < rate < 10 else None


# ===== HTML formatting helpers =====

def fmt_num(n):
    if n is None:
        return "N/A"
    return f"{n:,.0f}"


def fmt_pct(n):
    if n is None:
        return "N/A"
    return f"{n*100:.1f}%" if isinstance(n, float) and abs(n) < 100 else f"{n:.1f}%"


def pnl_class(v):
    if v > 0:
        return "positive"
    if v < 0:
        return "negative"
    return ""
