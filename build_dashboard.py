#!/usr/bin/env python3
"""Build interactive HTML dashboard from parsed transactions."""
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, date

with open("/Users/r/Documents/Claude/stock-dashboard/transactions.json", encoding="utf-8") as f:
    all_txs = json.load(f)

# Load current prices
import os

# Load briefing data
briefing_file = "/Users/r/Documents/Claude/stock-dashboard/briefing.json"
briefing_data = {}
if os.path.exists(briefing_file):
    with open(briefing_file, encoding="utf-8") as f:
        briefing_data = json.load(f)

# Load briefing summary (period-based AI summaries)
briefing_summary_file = "/Users/r/Documents/Claude/stock-dashboard/briefing_summary.json"
briefing_summary = {}
if os.path.exists(briefing_summary_file):
    with open(briefing_summary_file, encoding="utf-8") as f:
        briefing_summary = json.load(f)
prices_file = "/Users/r/Documents/Claude/stock-dashboard/prices.json"
current_prices = {}  # stock name -> price (in original currency)
prices_updated_at = None
if os.path.exists(prices_file):
    with open(prices_file, encoding="utf-8") as f:
        raw_prices = json.load(f)
        prices_updated_at = raw_prices.get("_updated_at")
        for k, v in raw_prices.items():
            if k.startswith("_"):
                continue
            current_prices[k] = v["price"]

# NOTE: txs and cash_txs are created AFTER USD normalization below
cash_flow_types = {"deposit", "withdrawal", "loan_in", "loan_out", "lending_fee"}

# ===== Exchange rate for USD holdings valuation =====
def fetch_usd_krw():
    """Fetch current USD/KRW exchange rate."""
    import urllib.request
    try:
        url = "https://api.stock.naver.com/marketindex/exchange/FX_USDKRW"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
            rate_str = data.get("exchangeInfo", data).get("closePrice", "0").replace(",", "")
            return float(rate_str)
    except Exception:
        return 1400.0  # fallback

usd_krw = fetch_usd_krw()

def fetch_jpy_krw():
    """Fetch current JPY/KRW exchange rate (100 JPY → KRW)."""
    import urllib.request
    try:
        url = "https://api.stock.naver.com/marketindex/exchange/FX_JPYKRW"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
            rate_str = data.get("exchangeInfo", data).get("closePrice", "0").replace(",", "")
            # Naver returns rate per 100 JPY
            return float(rate_str) / 100.0
    except Exception:
        return 9.5  # fallback ~9.5 KRW per 1 JPY

jpy_krw = fetch_jpy_krw()

def fetch_cny_krw():
    """Fetch current CNY/KRW exchange rate."""
    import urllib.request
    try:
        url = "https://api.stock.naver.com/marketindex/exchange/FX_CNYKRW"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
            rate_str = data.get("exchangeInfo", data).get("closePrice", "0").replace(",", "")
            return float(rate_str)
    except Exception:
        return 200.0  # fallback

def fetch_hkd_krw():
    """Fetch current HKD/KRW exchange rate."""
    import urllib.request
    try:
        url = "https://api.stock.naver.com/marketindex/exchange/FX_HKDKRW"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
            rate_str = data.get("exchangeInfo", data).get("closePrice", "0").replace(",", "")
            return float(rate_str)
    except Exception:
        return 190.0  # fallback

cny_krw = fetch_cny_krw()
hkd_krw = fetch_hkd_krw()
print(f"USD/KRW: {usd_krw:,.2f}, JPY/KRW: {jpy_krw:,.4f}, CNY/KRW: {cny_krw:,.2f}, HKD/KRW: {hkd_krw:,.2f}")

# Build currency mapping from prices.json nation info
# nation → currency: KOR→KRW, USA→USD, JPN→JPY, CHN→CNY, HKG→HKD
NATION_CURRENCY = {"KOR": "KRW", "USA": "USD", "JPN": "JPY", "CHN": "CNY", "HKG": "HKD"}
FX_RATES = {"KRW": 1, "USD": usd_krw, "JPY": jpy_krw, "CNY": cny_krw, "HKD": hkd_krw}

stock_currency_map = {}  # stock -> "KRW" | "USD" | "JPY" | "CNY" | "HKD"
stock_nation_map = {}    # stock -> "KOR" | "USA" | "JPN" | "CHN" | "HKG"
if os.path.exists(prices_file):
    with open(prices_file, encoding="utf-8") as f:
        raw_p = json.load(f)
        for k, v in raw_p.items():
            if k.startswith("_") or not isinstance(v, dict):
                continue
            nation = v.get("nation", "KOR")
            stock_currency_map[k] = NATION_CURRENCY.get(nation, "USD")
            stock_nation_map[k] = nation


def get_krw_price(stock, fallback=0):
    """Get current price in KRW (converts foreign prices using exchange rate)."""
    price = current_prices.get(stock, fallback)
    cur = stock_currency_map.get(stock, "KRW")
    rate = FX_RATES.get(cur, 1)
    return price * rate

# ===== Per-Stock Per-Account calculations =====
# Track positions using FIFO
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
    # Need at least one negative and one positive
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


today_str = date.today().strftime("%Y-%m-%d")

# ===== Normalize foreign currency transactions to KRW =====
# 토스 USD: amounts already in KRW (trade-time rate from PDF)
# 나무증권 USD/JPY: amounts in original currency, need conversion
# NH투자증권: amounts in original currency but marked as KRW — detect by stock_currency_map
for tx in all_txs:
    if tx["broker"] == "나무증권":
        if tx["currency"] == "USD":
            tx["amount"] = round(tx["amount"] * usd_krw)
            tx["price"] = round(tx["price"] * usd_krw)
            tx["fee"] = round(tx["fee"] * usd_krw)
            tx["tax"] = round(tx["tax"] * usd_krw)
            tx["currency"] = "KRW(USD)"
        elif tx["currency"] == "JPY":
            tx["amount"] = round(tx["amount"] * jpy_krw)
            tx["price"] = round(tx["price"] * jpy_krw)
            tx["fee"] = round(tx["fee"] * jpy_krw)
            tx["tax"] = round(tx["tax"] * jpy_krw)
            tx["currency"] = "KRW(JPY)"
    elif tx["broker"] == "NH투자증권" and tx["currency"] == "KRW":
        # NH 간략 format has no currency info — detect foreign stocks via stock_currency_map
        stock_cur = stock_currency_map.get(tx.get("stock", ""), "KRW")
        if stock_cur != "KRW" and tx["type"] in ("buy", "sell", "dividend", "transfer_in", "transfer_out"):
            rate = FX_RATES.get(stock_cur, 1)
            tx["amount"] = round(tx["amount"] * rate)
            tx["price"] = round(tx["price"] * rate)
            tx["fee"] = round(tx["fee"] * rate)
            tx["tax"] = round(tx["tax"] * rate)
            tx["currency"] = f"KRW({stock_cur})"

# Build filtered lists AFTER USD normalization
cash_txs = [tx for tx in all_txs if tx["type"] in cash_flow_types]

# Re-filter after normalization
txs = [tx for tx in all_txs if tx["amount"] > 0 or tx["type"] in ("transfer_in", "transfer_out")]

# ===== Compute per account, per stock =====
# All amounts are already converted to KRW — no sub-account splitting needed
account_stock_data = defaultdict(lambda: defaultdict(list))
for tx in txs:
    if tx["type"] in ["buy", "sell", "dividend", "transfer_in", "transfer_out"]:
        acc = tx["account"]
        account_stock_data[acc][tx["stock"]].append(tx)

# Per-account summary
account_summaries = {}
for account in sorted(account_stock_data.keys()):
    stocks_data = {}
    account_total_invested = 0
    account_total_returned = 0
    account_realized_pnl = 0
    account_dividends = 0
    account_fees = 0
    account_tax = 0
    account_cashflows = []
    holdings = {}

    for stock, trades in sorted(account_stock_data[account].items()):
        m = calc_stock_metrics(trades)
        stocks_data[stock] = m
        account_total_invested += m["total_invested"]
        account_total_returned += m["total_returned"]
        account_realized_pnl += m["realized_pnl"]
        account_dividends += m["total_dividends"]
        account_fees += m["total_fees"]
        account_tax += m["total_tax"]
        account_cashflows.extend(m["cashflows"])
        if m["current_qty"] > 0:
            holdings[stock] = {
                "qty": m["current_qty"],
                "avg_price": m["avg_buy_price"],
                "cost": m["current_cost"],
            }

    # Calculate loan interest for this account
    account_loan_interest = sum(
        tx["amount"] for tx in txs
        if tx["type"] == "loan_interest" and tx["account"] == account
    )
    # Calculate cash flow metrics for this account
    acct_deposits = sum(tx["amount"] for tx in cash_txs if tx["type"] == "deposit" and tx["account"] == account)
    acct_withdrawals = sum(tx["amount"] for tx in cash_txs if tx["type"] == "withdrawal" and tx["account"] == account)
    acct_loan_in = sum(tx["amount"] for tx in cash_txs if tx["type"] == "loan_in" and tx["account"] == account)
    acct_loan_out = sum(tx["amount"] for tx in cash_txs if tx["type"] == "loan_out" and tx["account"] == account)
    acct_lending_fee = sum(tx["amount"] for tx in cash_txs if tx["type"] == "lending_fee" and tx["account"] == account)
    acct_net_deposit = acct_deposits - acct_withdrawals
    acct_loan_balance = acct_loan_in - acct_loan_out

    account_cashflows.sort(key=lambda x: x[0])
    # Add current market value of holdings for IRR
    irr_account_cf = list(account_cashflows)
    for stock, h in holdings.items():
        cp = get_krw_price(stock, 0)
        irr_account_cf.append((today_str, h["qty"] * cp))
    irr = calc_xirr(irr_account_cf)

    account_summaries[account] = {
        "stocks": stocks_data,
        "total_invested": account_total_invested,
        "total_returned": account_total_returned,
        "realized_pnl": account_realized_pnl,
        "dividends": account_dividends,
        "fees": account_fees,
        "tax": account_tax,
        "loan_interest": account_loan_interest,
        "net_deposit": acct_net_deposit,
        "loan_balance": acct_loan_balance,
        "lending_fee": acct_lending_fee,
        "total_deposits": acct_deposits,
        "total_withdrawals": acct_withdrawals,
        "irr": irr,
        "holdings": holdings,
        "num_trades": sum(len(t) for t in account_stock_data[account].values()),
    }

# Per-stock aggregate (all accounts)
stock_all_data = defaultdict(list)
for tx in txs:
    if tx["type"] in ["buy", "sell", "dividend", "transfer_in", "transfer_out"]:
        stock_all_data[tx["stock"]].append(tx)

stock_summaries = {}
for stock, trades in sorted(stock_all_data.items()):
    m = calc_stock_metrics(trades)
    # Add current market value as virtual cashflow for IRR calculation
    irr_cashflows = list(m["cashflows"])
    if m["current_qty"] > 0:
        cp = get_krw_price(stock, m["avg_buy_price"])
        irr_cashflows.append((today_str, m["current_qty"] * cp))
    irr = calc_xirr(irr_cashflows)
    net_pnl = m["realized_pnl"] + m["total_dividends"]
    roi = (net_pnl / m["total_invested"] * 100) if m["total_invested"] > 0 else 0
    stock_summaries[stock] = {
        **m,
        "irr": irr,
        "net_pnl": net_pnl,
        "roi": roi,
    }

# Overall summary
all_cashflows = []
overall_invested = 0
overall_returned = 0
overall_realized_pnl = 0
overall_dividends = 0
overall_fees = 0
overall_tax = 0
overall_holdings = {}
overall_loan_interest = sum(
    tx["amount"] for tx in txs if tx["type"] == "loan_interest"
)
overall_deposits = sum(tx["amount"] for tx in cash_txs if tx["type"] == "deposit")
overall_withdrawals = sum(tx["amount"] for tx in cash_txs if tx["type"] == "withdrawal")
overall_net_deposit = overall_deposits - overall_withdrawals
overall_loan_in = sum(tx["amount"] for tx in cash_txs if tx["type"] == "loan_in")
overall_loan_out = sum(tx["amount"] for tx in cash_txs if tx["type"] == "loan_out")
overall_loan_balance = overall_loan_in - overall_loan_out
overall_lending_fee = sum(tx["amount"] for tx in cash_txs if tx["type"] == "lending_fee")

for stock, m in stock_summaries.items():
    overall_invested += m["total_invested"]
    overall_returned += m["total_returned"]
    overall_realized_pnl += m["realized_pnl"]
    overall_dividends += m["total_dividends"]
    overall_fees += m["total_fees"]
    overall_tax += m["total_tax"]
    all_cashflows.extend(m["cashflows"])
    if m["current_qty"] > 0:
        overall_holdings[stock] = {
            "qty": m["current_qty"],
            "avg_price": m["avg_buy_price"],
            "cost": m["current_cost"],
        }

all_cashflows.sort(key=lambda x: x[0])
# Add current market value of all holdings for IRR
irr_all_cf = list(all_cashflows)
for stock, h in overall_holdings.items():
    cp = get_krw_price(stock, 0)
    irr_all_cf.append((today_str, h["qty"] * cp))
overall_irr = calc_xirr(irr_all_cf)

# ===== Timeline data for chart =====
monthly_data = defaultdict(lambda: {"invested": 0, "returned": 0, "pnl": 0, "dividends": 0, "buy_count": 0, "sell_count": 0})
for tx in txs:
    month = tx["date"][:7]
    if tx["type"] == "buy":
        monthly_data[month]["invested"] += tx["amount"]
        monthly_data[month]["buy_count"] += 1
    elif tx["type"] == "sell":
        monthly_data[month]["returned"] += tx["amount"]
        monthly_data[month]["sell_count"] += 1
    elif tx["type"] == "dividend":
        monthly_data[month]["dividends"] += tx["amount"]

# Approximate monthly realized pnl as returned - invested for that month
for m in monthly_data:
    monthly_data[m]["pnl"] = monthly_data[m]["returned"] - monthly_data[m]["invested"]

# ===== Monthly portfolio holdings tracking (for total asset value timeline) =====
# Track holdings (qty per stock) over time using FIFO, compute month-end values
# Sort all stock transactions by date
stock_txs_sorted = sorted(
    [tx for tx in txs if tx["type"] in ("buy", "sell", "transfer_in", "transfer_out")],
    key=lambda x: x["date"]
)

# Build month-end holdings snapshots
monthly_holdings = {}  # month -> {stock: qty}
current_holdings = defaultdict(int)  # stock -> qty
current_cost_basis = defaultdict(float)  # stock -> total cost
stock_buys_fifo = defaultdict(list)  # stock -> [(qty, price)]

for tx in stock_txs_sorted:
    month = tx["date"][:7]
    stock = tx["stock"]
    if tx["type"] == "buy":
        current_holdings[stock] += tx["qty"]
        current_cost_basis[stock] += tx["amount"]
        stock_buys_fifo[stock].append({"qty": tx["qty"], "price": tx["price"]})
    elif tx["type"] == "sell":
        current_holdings[stock] -= tx["qty"]
        # FIFO cost removal
        remaining = tx["qty"]
        while remaining > 0 and stock_buys_fifo[stock]:
            b = stock_buys_fifo[stock][0]
            take = min(remaining, b["qty"])
            current_cost_basis[stock] -= take * b["price"]
            b["qty"] -= take
            remaining -= take
            if b["qty"] == 0:
                stock_buys_fifo[stock].pop(0)
        if current_holdings[stock] <= 0:
            current_holdings[stock] = 0
            current_cost_basis[stock] = 0
    elif tx["type"] == "transfer_in":
        current_holdings[stock] += tx["qty"]
        current_cost_basis[stock] += tx["qty"] * tx["price"]
        stock_buys_fifo[stock].append({"qty": tx["qty"], "price": tx["price"]})
    elif tx["type"] == "transfer_out":
        current_holdings[stock] -= tx["qty"]
        remaining = tx["qty"]
        while remaining > 0 and stock_buys_fifo[stock]:
            b = stock_buys_fifo[stock][0]
            take = min(remaining, b["qty"])
            current_cost_basis[stock] -= take * b["price"]
            b["qty"] -= take
            remaining -= take
            if b["qty"] == 0:
                stock_buys_fifo[stock].pop(0)
        if current_holdings[stock] <= 0:
            current_holdings[stock] = 0
            current_cost_basis[stock] = 0
    # Snapshot at each month
    monthly_holdings[month] = {
        "holdings": {s: q for s, q in current_holdings.items() if q > 0},
        "cost_basis": {s: c for s, c in current_cost_basis.items() if current_holdings[s] > 0},
    }

# Compute month-end total asset value:
# - Historical months: use cost basis (no historical prices available)
# - Last month: use current market prices
months_sorted = sorted(monthly_data.keys())
current_month = months_sorted[-1] if months_sorted else ""

month_end_values = {}
for m in months_sorted:
    snap = monthly_holdings.get(m)
    if not snap:
        month_end_values[m] = 0
        continue
    if m == current_month:
        # Use market prices for current month
        total = 0
        for stock, qty in snap["holdings"].items():
            total += qty * get_krw_price(stock, snap["cost_basis"].get(stock, 0) / max(qty, 1))
        month_end_values[m] = round(total)
    else:
        # Use cost basis for historical months
        month_end_values[m] = round(sum(snap["cost_basis"].values()))

cum_invested = 0
cum_returned = 0
cum_dividends = 0
cum_pnl = 0
timeline = []
for m in months_sorted:
    d = monthly_data[m]
    cum_invested += d["invested"]
    cum_returned += d["returned"]
    cum_dividends += d["dividends"]
    cum_pnl += d["pnl"]
    holdings_value = month_end_values.get(m, 0)
    # Total asset = 보유주식 평가액 + 누적 회수 + 누적 배당 (= 내가 가진 총 가치)
    total_asset = holdings_value + cum_returned + cum_dividends
    timeline.append({
        "month": m,
        "invested": d["invested"],
        "returned": d["returned"],
        "dividends": d["dividends"],
        "realized_pnl": d["pnl"],
        "buy_count": d["buy_count"],
        "sell_count": d["sell_count"],
        "cum_invested": cum_invested,
        "cum_returned": cum_returned,
        "cum_dividends": cum_dividends,
        "cum_pnl": cum_pnl,
        "net_cashflow": cum_returned + cum_dividends - cum_invested,
        "holdings_value": holdings_value,
        "total_asset": total_asset,
    })


# === Win rate ===
win_count = sum(1 for m in stock_summaries.values() if m.get("net_pnl", 0) > 0)
loss_count = sum(1 for m in stock_summaries.values() if m.get("net_pnl", 0) < 0)
total_traded_count = win_count + loss_count
win_rate = (win_count / total_traded_count * 100) if total_traded_count > 0 else 0


def fmt_num(n):
    if n is None:
        return "N/A"
    return f"{n:,.0f}"


def fmt_pct(n):
    if n is None:
        return "N/A"
    return f"{n*100:.1f}%" if isinstance(n, float) and abs(n) < 100 else f"{n:.1f}%"


def pnl_class(v):
    if v > 0: return "positive"
    if v < 0: return "negative"
    return ""


# ===== Build HTML =====
# Prepare data for JS
js_account_data = {}
for acc, data in account_summaries.items():
    stocks_list = []
    for stock, m in data["stocks"].items():
        irr = calc_xirr(m["cashflows"])
        net_pnl = m["realized_pnl"] + m["total_dividends"]
        roi = (net_pnl / m["total_invested"] * 100) if m["total_invested"] > 0 else 0
        stocks_list.append({
            "name": stock,
            "invested": m["total_invested"],
            "returned": m["total_returned"],
            "realized_pnl": m["realized_pnl"],
            "dividends": m["total_dividends"],
            "net_pnl": net_pnl,
            "roi": round(roi, 1),
            "irr": round(irr * 100, 1) if irr else None,
            "current_qty": m["current_qty"],
            "avg_price": m["avg_buy_price"],
            "cost": m["current_cost"],
            "fees": m["total_fees"],
            "tax": m["total_tax"],
        })
    # Add current price / market value to each stock
    for s in stocks_list:
        cp = get_krw_price(s["name"], 0)
        s["current_price"] = cp
        s["market_value"] = s["current_qty"] * cp if s["current_qty"] > 0 else 0
        s["unrealized_pnl"] = s["market_value"] - s["cost"] if s["current_qty"] > 0 else 0

    total_mv = sum(s["market_value"] for s in stocks_list)
    for s in stocks_list:
        s["weight"] = round(s["market_value"] / total_mv * 100, 1) if total_mv > 0 and s["market_value"] > 0 else 0

    stocks_list.sort(key=lambda x: abs(x["net_pnl"]), reverse=True)

    # Build treemap data for this account's holdings
    account_treemap = []
    holdings = data["holdings"]
    acct_total_mv = 0
    for stock, h in holdings.items():
        cp = get_krw_price(stock, 0)
        mv = h["qty"] * cp
        acct_total_mv += mv
    for stock, h in holdings.items():
        cp = get_krw_price(stock, 0)
        mv = h["qty"] * cp
        cost = h["cost"]
        ret = ((mv - cost) / cost * 100) if cost > 0 else 0
        weight = (mv / acct_total_mv * 100) if acct_total_mv > 0 else 0
        account_treemap.append({
            "name": stock,
            "market_value": mv,
            "return_pct": round(ret, 1),
            "qty": h["qty"],
            "avg_price": h["avg_price"],
            "current_price": cp,
            "cost": cost,
            "unrealized_pnl": mv - cost,
            "weight": round(weight, 1),
            "nation": stock_nation_map.get(stock, "KOR"),
        })
    account_treemap.sort(key=lambda x: x["market_value"], reverse=True)

    js_account_data[acc] = {
        "stocks": stocks_list,
        "total_invested": data["total_invested"],
        "total_returned": data["total_returned"],
        "realized_pnl": data["realized_pnl"],
        "dividends": data["dividends"],
        "fees": data["fees"],
        "tax": data["tax"],
        "loan_interest": data["loan_interest"],
        "net_deposit": data["net_deposit"],
        "loan_balance": data["loan_balance"],
        "lending_fee": data["lending_fee"],
        "total_deposits": data["total_deposits"],
        "total_withdrawals": data["total_withdrawals"],
        "irr": round(data["irr"] * 100, 1) if data["irr"] else None,
        "holdings": data["holdings"],
        "num_trades": data["num_trades"],
        "treemap": account_treemap,
        "total_market_value": round(sum(item["market_value"] for item in account_treemap)),
        "total_cost": round(sum(item["cost"] for item in account_treemap)),
        "num_holdings": len(data["holdings"]),
    }

js_stock_data = []
for stock, m in stock_summaries.items():
    js_stock_data.append({
        "name": stock,
        "invested": m["total_invested"],
        "returned": m["total_returned"],
        "realized_pnl": m["realized_pnl"],
        "dividends": m["total_dividends"],
        "net_pnl": m["net_pnl"],
        "roi": round(m["roi"], 1),
        "irr": round(m["irr"] * 100, 1) if m["irr"] else None,
        "current_qty": m["current_qty"],
        "avg_price": m["avg_buy_price"],
        "cost": m["current_cost"],
        "fees": m["total_fees"],
        "tax": m["total_tax"],
    })
# Add current price / market value
for s in js_stock_data:
    cp = get_krw_price(s["name"], 0)
    s["current_price"] = cp
    s["market_value"] = s["current_qty"] * cp if s["current_qty"] > 0 else 0
    s["unrealized_pnl"] = s["market_value"] - s["cost"] if s["current_qty"] > 0 else 0

total_mv_all = sum(s["market_value"] for s in js_stock_data)
for s in js_stock_data:
    s["weight"] = round(s["market_value"] / total_mv_all * 100, 1) if total_mv_all > 0 and s["market_value"] > 0 else 0

js_stock_data.sort(key=lambda x: abs(x["net_pnl"]), reverse=True)

overall_net_pnl = overall_realized_pnl + overall_dividends
overall_roi = (overall_net_pnl / overall_invested * 100) if overall_invested > 0 else 0

total_market_value = sum(
    h["qty"] * get_krw_price(stock, 0)
    for stock, h in overall_holdings.items()
)
total_unrealized = total_market_value - sum(h["cost"] for h in overall_holdings.values())

js_overall = {
    "total_invested": overall_invested,
    "total_returned": overall_returned,
    "realized_pnl": overall_realized_pnl,
    "dividends": overall_dividends,
    "net_pnl": overall_net_pnl,
    "roi": round(overall_roi, 1),
    "irr": round(overall_irr * 100, 1) if overall_irr else None,
    "fees": overall_fees,
    "tax": overall_tax,
    "loan_interest": overall_loan_interest,
    "net_deposit": overall_net_deposit,
    "loan_balance": overall_loan_balance,
    "lending_fee": overall_lending_fee,
    "total_deposits": overall_deposits,
    "total_withdrawals": overall_withdrawals,
    "holdings": overall_holdings,
    "num_stocks": len(stock_summaries),
    "num_accounts": len(account_summaries),
    "total_market_value": total_market_value,
    "total_unrealized": total_unrealized,
}

# Build treemap data for holdings
treemap_data = []
for stock, h in overall_holdings.items():
    cp = get_krw_price(stock, 0)
    mv = h["qty"] * cp
    cost = h["cost"]
    unrealized_ret = ((mv - cost) / cost * 100) if cost > 0 else 0
    weight = (mv / total_market_value * 100) if total_market_value > 0 else 0
    treemap_data.append({
        "name": stock,
        "market_value": mv,
        "weight": round(weight, 1),
        "return_pct": round(unrealized_ret, 1),
        "qty": h["qty"],
        "avg_price": h["avg_price"],
        "current_price": cp,
        "cost": cost,
        "unrealized_pnl": mv - cost,
        "nation": stock_nation_map.get(stock, "KOR"),
    })
treemap_data.sort(key=lambda x: x["market_value"], reverse=True)

html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>주식 통합 대시보드</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {{
  --bg: #eef1f7;
  --bg2: #f8f9fc;
  --card: #ffffff;
  --card-hover: #fafbff;
  --border: #dde3ee;
  --border-light: #c4cedd;
  --text: #0f172a;
  --text-dim: #4a5568;
  --text-muted: #8898aa;
  --accent: #1a56db;
  --accent-dim: rgba(26,86,219,0.08);
  --positive: #0a7c59;
  --positive-dim: rgba(10,124,89,0.07);
  --negative: #c81e1e;
  --negative-dim: rgba(200,30,30,0.07);
  --warn: #b45309;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans KR', sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; -webkit-font-smoothing: antialiased; color: var(--text); }}
.container {{ max-width: 1440px; margin: 0 auto; padding: 20px; }}
h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 8px; letter-spacing: -0.02em; }}
.subtitle {{ color: var(--text-dim); margin-bottom: 24px; font-size: 0.88rem; }}
.tabs {{ display: flex; gap: 4px; margin-bottom: 24px; background: var(--card); border-radius: 12px; padding: 4px; border: 1px solid var(--border); width: fit-content; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
.tab {{ padding: 10px 24px; border-radius: 8px; cursor: pointer; font-size: 0.9rem; font-weight: 600; color: var(--text-dim); transition: all 0.25s ease; border: none; background: none; }}
.tab:hover {{ color: var(--text); background: rgba(0,0,0,0.04); }}
.tab.active {{ background: var(--accent); color: white; box-shadow: 0 2px 8px rgba(26,86,219,0.25); }}
.tab-content {{ display: none; animation: fadeIn 0.3s ease; }}
.tab-content.active {{ display: block; }}
@keyframes fadeIn {{ from {{ opacity: 0; transform: translateY(4px); }} to {{ opacity: 1; transform: translateY(0); }} }}

/* === Sub-tabs === */
.sub-tabs {{ display: flex; gap: 4px; margin-bottom: 16px; }}
.sub-tab {{ padding: 6px 18px; border-radius: 6px; cursor: pointer; font-size: 0.85rem; font-weight: 600; color: var(--text-dim); border: 1px solid var(--border); background: var(--card); transition: all 0.2s; }}
.sub-tab:hover {{ color: var(--text); border-color: var(--accent); }}
.sub-tab.active {{ background: var(--accent-dim); color: var(--accent); border-color: var(--accent); }}
.subtab-content {{ display: none; }}
.subtab-content.active {{ display: block; }}

/* === KPI Cards === */
.kpi-row {{ display: grid; gap: 14px; margin-bottom: 14px; }}
.kpi-row.primary {{ grid-template-columns: repeat(4, 1fr); }}
.kpi-row.secondary {{ grid-template-columns: repeat(5, 1fr); }}
.kpi {{ background: var(--card); border-radius: 12px; padding: 18px 20px; border: 1px solid var(--border); transition: border-color 0.2s, transform 0.2s; box-shadow: 0 1px 3px rgba(0,0,0,0.07); }}
.kpi:hover {{ border-color: var(--border-light); transform: translateY(-1px); }}
.kpi.border-positive {{ border-image: linear-gradient(135deg, var(--positive-dim), transparent 60%) 1; }}
.kpi.border-negative {{ border-image: linear-gradient(135deg, var(--negative-dim), transparent 60%) 1; }}
.kpi-label {{ font-size: 0.75rem; color: var(--text-dim); margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.6px; font-weight: 600; }}
.kpi-value {{ font-size: 1.7rem; font-weight: 700; font-feature-settings: 'tnum'; letter-spacing: -0.02em; }}
.kpi-value.compact {{ font-size: 1.3rem; }}
.kpi-sub {{ font-size: 0.78rem; color: var(--text-dim); margin-top: 4px; font-feature-settings: 'tnum'; }}
.positive {{ color: var(--positive); }}
.negative {{ color: var(--negative); }}

/* === Cards === */
.card {{ background: var(--card); border-radius: 12px; padding: 20px; border: 1px solid var(--border); margin-bottom: 20px; transition: border-color 0.2s; }}
.card:hover {{ border-color: var(--border-light); }}
.card-title {{ font-size: 1.05rem; font-weight: 600; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }}
.chart-container {{ position: relative; height: 220px; overflow: hidden; background: rgba(26,86,219,0.03); border-radius: 8px; }}

/* === Treemap === */
.treemap-container {{ position: relative; width: 100%; height: 420px; border-radius: 8px; overflow: hidden; }}
.treemap-container.acct-treemap {{ height: 320px; }}
.treemap-cell {{ position: absolute; overflow: hidden; display: flex; flex-direction: column; align-items: center; justify-content: center; cursor: default; transition: filter 0.15s; border: 1px solid rgba(0,0,0,0.3); }}
.treemap-cell:hover {{ filter: brightness(1.15); z-index: 2; }}
.treemap-cell .name {{ font-weight: 700; font-size: 0.82rem; color: #fff; text-shadow: 0 1px 3px rgba(0,0,0,0.6); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 95%; text-align: center; }}
.treemap-cell .pct {{ font-weight: 600; font-size: 0.75rem; color: rgba(255,255,255,0.9); text-shadow: 0 1px 3px rgba(0,0,0,0.6); opacity: 0; transition: opacity 0.2s; }}
.treemap-cell .val {{ font-size: 0.65rem; color: rgba(255,255,255,0.7); text-shadow: 0 1px 3px rgba(0,0,0,0.6); margin-top: 1px; opacity: 0; transition: opacity 0.2s; }}
.treemap-cell:hover .pct, .treemap-cell:hover .val {{ opacity: 1; }}
.treemap-cell.small .name {{ font-size: 0.7rem; }}
.treemap-cell.small .pct {{ font-size: 0.65rem; }}
.treemap-cell.small .val {{ display: none; }}
.treemap-cell.tiny .name {{ font-size: 0.6rem; }}
.treemap-cell.tiny .pct {{ display: none; }}
.treemap-cell.tiny .val {{ display: none; }}
.treemap-filters {{ display: flex; gap: 4px; margin-bottom: 8px; }}
.treemap-filters .tf-btn {{ padding: 4px 14px; border-radius: 6px; font-size: 0.78rem; font-weight: 600; color: var(--text-dim); border: 1px solid var(--border); background: var(--card); cursor: pointer; transition: all 0.2s; }}
.treemap-filters .tf-btn:hover {{ color: var(--text); border-color: var(--accent); }}
.treemap-filters .tf-btn.active {{ background: var(--accent-dim); color: var(--accent); border-color: var(--accent); }}
.treemap-cell.dimmed {{ opacity: 0.15; }}

/* === Tables === */
table {{ width: 100%; border-collapse: collapse; font-size: 0.84rem; }}
thead {{ position: sticky; top: 0; z-index: 5; }}
th {{ text-align: left; padding: 10px 12px; background: var(--card); border-bottom: 2px solid var(--border); color: var(--text-dim); font-weight: 600; font-size: 0.73rem; text-transform: uppercase; letter-spacing: 0.5px; cursor: pointer; user-select: none; white-space: nowrap; backdrop-filter: blur(8px); }}
th:hover {{ color: var(--text); }}
th.sort-asc::after {{ content: ' \\25B2'; font-size: 0.6rem; }}
th.sort-desc::after {{ content: ' \\25BC'; font-size: 0.6rem; }}
td {{ padding: 9px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; font-feature-settings: 'tnum'; font-variant-numeric: tabular-nums; }}
tr:nth-child(even) td {{ background: rgba(26,86,219,0.025); }}
tr:hover td {{ background: rgba(26,86,219,0.05); }}
.text-right {{ text-align: right; }}
.text-center {{ text-align: center; }}
.mono {{ font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace; font-size: 0.82rem; }}

/* === Holdings grid === */
.holdings-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; }}
.holding-card {{ background: rgba(26,86,219,0.06); border-radius: 10px; padding: 14px; border: 1px solid rgba(26,86,219,0.15); transition: border-color 0.2s; }}
.holding-card:hover {{ border-color: rgba(99,102,241,0.3); }}
.holding-name {{ font-weight: 600; font-size: 0.9rem; margin-bottom: 6px; }}
.holding-detail {{ font-size: 0.8rem; color: var(--text-dim); font-feature-settings: 'tnum'; }}

/* === Account selector === */
.account-selector {{ display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }}
.account-btn {{ padding: 6px 16px; border-radius: 20px; cursor: pointer; font-size: 0.85rem; font-weight: 500; border: 1px solid var(--border); background: var(--card); color: var(--text-dim); transition: all 0.2s; }}
.account-btn:hover {{ border-color: var(--accent); color: var(--text); }}
.account-btn.active {{ background: var(--accent); color: white; border-color: var(--accent); box-shadow: 0 2px 8px rgba(99,102,241,0.25); }}

/* === Toolbar === */
.search-box {{ background: var(--bg2); border: 1px solid var(--border); border-radius: 8px; padding: 8px 14px; color: var(--text); font-size: 0.9rem; width: 260px; transition: border-color 0.2s; }}
.search-box::placeholder {{ color: var(--text-muted); }}
.search-box:focus {{ outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(26,86,219,0.12); }}
.toolbar {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }}
.filter-group {{ display: flex; gap: 4px; }}
.filter-btn {{ padding: 5px 14px; border-radius: 16px; cursor: pointer; font-size: 0.8rem; font-weight: 500; border: 1px solid var(--border); background: var(--card); color: var(--text-dim); transition: all 0.2s; }}
.filter-btn:hover {{ border-color: var(--accent); color: var(--text); }}
.filter-btn.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
.result-count {{ font-size: 0.8rem; color: var(--text-dim); margin-left: auto; font-feature-settings: 'tnum'; }}

/* === Tooltip === */
.tm-tooltip {{ position: fixed; pointer-events: none; background: #ffffff; border: 1px solid var(--border-light); border-radius: 8px; padding: 10px 14px; font-size: 0.82rem; color: var(--text); z-index: 1000; backdrop-filter: blur(8px); box-shadow: 0 8px 24px rgba(0,0,0,0.12), 0 2px 8px rgba(0,0,0,0.08); display: none; max-width: 280px; }}
.tm-tooltip .tt-name {{ font-weight: 700; margin-bottom: 4px; font-size: 0.9rem; }}
.tm-tooltip .tt-row {{ display: flex; justify-content: space-between; gap: 16px; font-feature-settings: 'tnum'; }}
.tm-tooltip .tt-label {{ color: var(--text-dim); }}

/* === Detail toggle === */
.detail-section {{ }}
.detail-toggle {{ cursor: pointer; color: var(--text-dim); font-size: 0.85rem; font-weight: 600; padding: 10px 0; list-style: none; display: flex; align-items: center; gap: 6px; transition: color 0.2s; }}
.detail-toggle:hover {{ color: var(--text); }}
.detail-toggle::before {{ content: '▸'; font-size: 0.75rem; transition: transform 0.2s; }}
details[open] .detail-toggle::before {{ transform: rotate(90deg); }}
.detail-toggle::-webkit-details-marker {{ display: none; }}
.detail-content {{ animation: fadeIn 0.3s ease; }}

/* === Account Summary Cards === */
.acct-summary-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 10px;
  margin-bottom: 20px;
}}
.acct-card {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 14px 10px;
  transition: border-color 0.2s, transform 0.15s, box-shadow 0.2s;
  cursor: pointer;
  position: relative;
  overflow: hidden;
}}
.acct-card:hover {{
  border-color: var(--accent);
  transform: translateY(-2px);
  box-shadow: 0 6px 20px rgba(0,0,0,0.12);
}}
.acct-card.has-loan {{
  border-color: rgba(200,30,30,0.3);
}}
.acct-card.has-loan:hover {{
  border-color: var(--negative);
}}
/* 브로커별 컬러 구분 - 상단 accent 바 */
.acct-card::before {{
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 3px;
  border-radius: 10px 10px 0 0;
}}
.acct-card.broker-nh::before    {{ background: #3b82f6; }}
.acct-card.broker-namu::before  {{ background: #8b5cf6; }}
.acct-card.broker-toss::before  {{ background: #06b6d4; }}
.acct-card.has-loan::before     {{ background: linear-gradient(90deg, #ef4444, #f97316); }}

/* 계좌 헤더 */
.acct-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}}
.acct-broker-tag {{
  font-size: 0.65rem;
  font-weight: 700;
  letter-spacing: 0.8px;
  text-transform: uppercase;
  padding: 2px 7px;
  border-radius: 4px;
}}
.acct-card.broker-nh    .acct-broker-tag {{ background: rgba(59,130,246,0.12); color: #1d4ed8; }}
.acct-card.broker-namu  .acct-broker-tag {{ background: rgba(139,92,246,0.12); color: #7c3aed; }}
.acct-card.broker-toss  .acct-broker-tag {{ background: rgba(6,182,212,0.12);  color: #0369a1; }}
.acct-card.has-loan     .acct-broker-tag {{ background: rgba(200,30,30,0.10);  color: #b91c1c; }}
.acct-acct-id {{
  font-size: 0.72rem;
  color: var(--text-muted);
  font-weight: 500;
}}

/* 평가금액 */
.acct-mv {{
  font-size: 1.4rem;
  font-weight: 800;
  letter-spacing: -0.03em;
  line-height: 1.1;
  margin-bottom: 2px;
}}
.acct-mv.empty {{
  font-size: 0.9rem;
  font-weight: 500;
  color: var(--text-muted);
  margin-bottom: 8px;
}}

/* 평가손익 행 */
.acct-pnl-row {{
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
}}
.acct-upnl {{
  font-size: 0.82rem;
  font-weight: 600;
}}
.acct-upnl-pct {{
  font-size: 0.75rem;
  font-weight: 500;
  opacity: 0.85;
}}
.acct-upnl-bar {{
  flex: 1;
  height: 3px;
  border-radius: 2px;
  background: var(--border);
  overflow: hidden;
  margin-left: 4px;
}}
.acct-upnl-bar-fill {{
  height: 100%;
  border-radius: 2px;
  min-width: 2px;
}}

/* 하단 지표 2열 */
.acct-stats {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 4px;
  border-top: 1px solid var(--border);
  padding-top: 8px;
}}
.acct-stat {{
  display: flex;
  flex-direction: column;
  gap: 1px;
}}
.acct-stat-label {{
  font-size: 0.62rem;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.4px;
  font-weight: 600;
}}
.acct-stat-val {{
  font-size: 0.82rem;
  font-weight: 700;
  color: var(--text);
  font-feature-settings: 'tnum';
}}
/* === Dashboard Hero Header === */
.dashboard-header {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 20px 24px; margin-bottom: 20px;
  background: linear-gradient(135deg, #ffffff 0%, #f5f7ff 100%);
  border-radius: 16px; border: 1px solid var(--border);
  position: relative; overflow: hidden;
  box-shadow: 0 2px 8px rgba(0,0,0,0.07);
}}
.dashboard-header::before {{
  content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 4px;
  background: linear-gradient(to bottom, #3b82f6, #1a56db, #1e40af); border-radius: 4px 0 0 4px;
}}
.header-brand {{ font-size: 1.05rem; font-weight: 700; letter-spacing: -0.02em; display: flex; align-items: center; gap: 8px; }}
.header-brand-icon {{ font-size: 1.3rem; }}
.header-meta-text {{ font-size: 0.73rem; color: var(--text-dim); margin-top: 4px; line-height: 1.5; }}
.header-center {{ text-align: center; cursor: pointer; user-select: none; }}
.header-pv-label {{ font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; font-weight: 600; margin-bottom: 2px; }}
.header-pv-value {{ font-size: 2.5rem; font-weight: 800; letter-spacing: -0.04em; line-height: 1.05; }}
.header-pv-sub {{ font-size: 0.84rem; font-weight: 600; margin-top: 3px; }}
.pv-blur {{ filter: blur(9px); transition: filter 0.2s ease; user-select: none; }}
.pv-blur:hover {{ filter: blur(6px); }}
.header-right {{ display: flex; align-items: center; gap: 20px; }}
.header-stat {{ text-align: center; }}
.header-stat-label {{ font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600; margin-bottom: 3px; }}
.header-stat-value {{ font-size: 1.15rem; font-weight: 700; }}
.header-divider {{ width: 1px; height: 36px; background: var(--border); }}
.header-refresh-btn {{
  padding: 8px 16px; border: 1px solid var(--border-light); border-radius: 8px;
  background: rgba(26,86,219,0.06); color: var(--text-dim); cursor: pointer;
  font-size: 0.8rem; font-weight: 600; transition: all 0.2s; white-space: nowrap;
}}
.header-refresh-btn:hover {{ color: var(--accent); border-color: var(--accent); background: var(--accent-dim); }}
.header-refresh-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}

/* === Card improvements === */
.card {{ box-shadow: 0 1px 4px rgba(0,0,0,0.07), 0 4px 12px rgba(0,0,0,0.04); }}
.card:hover {{ box-shadow: 0 4px 16px rgba(0,0,0,0.10); border-color: var(--border-light); }}
.card-title {{ border-left: 3px solid var(--accent); padding-left: 10px; }}

/* === KPI improvement: border highlight on positive/negative === */
.kpi.border-positive {{ box-shadow: 0 1px 3px rgba(0,0,0,0.07), 0 0 0 1px rgba(10,124,89,0.2); }}
.kpi.border-negative {{ box-shadow: 0 1px 3px rgba(0,0,0,0.07), 0 0 0 1px rgba(200,30,30,0.2); }}

/* === Chart fix: prevent canvas overflow === */
.chart-container canvas {{ max-height: 350px; }}

/* === Info bar (below tabs) === */
.info-bar {{
  display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
  padding: 8px 14px; background: var(--card); border-radius: 8px;
  border: 1px solid var(--border); margin-bottom: 20px; font-size: 0.78rem; color: var(--text-dim);
  box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}}
.info-bar-item {{ display: flex; align-items: center; gap: 6px; }}
.info-bar-dot {{ width: 6px; height: 6px; border-radius: 50%; background: var(--accent); flex-shrink: 0; }}

@media (max-width: 900px) {{
  #treemapNationGrid {{ grid-template-columns: 1fr !important; }}
  .dashboard-header {{ flex-wrap: wrap; gap: 16px; }}
}}
@media (max-width: 768px) {{
  .kpi-row.primary {{ grid-template-columns: repeat(2, 1fr); }}
  .kpi-row.secondary {{ grid-template-columns: repeat(3, 1fr); }}
  .tabs {{ overflow-x: auto; width: 100%; }}
  table {{ font-size: 0.75rem; }}
  td, th {{ padding: 6px 8px; }}
  .treemap-container {{ height: 300px; }}
  .treemap-container.acct-treemap {{ height: 240px; }}
  .search-box {{ width: 180px; }}
  .header-center {{ display: none; }}
}}
@media (max-width: 480px) {{
  .kpi-row.primary, .kpi-row.secondary {{ grid-template-columns: 1fr 1fr; }}
  .kpi-value {{ font-size: 1.3rem; }}
  .kpi-value.compact {{ font-size: 1.1rem; }}
}}
</style>
</head>
<body>
<div class="container">
<div class="dashboard-header">
  <div>
    <div class="header-brand"><span class="header-brand-icon">📊</span> 주식 포트폴리오</div>
    <div class="header-meta-text">
      NH · 나무 · 토스 &nbsp;·&nbsp; {len(account_summaries)}개 계좌<br>
      {min(tx['date'] for tx in txs)[:7]} ~ {max(tx['date'] for tx in txs)[:7]} &nbsp;·&nbsp; {len(txs):,}건
    </div>
  </div>
  <div class="header-center" onclick="toggleAmounts()" title="클릭하여 금액 표시/숨김">
    <div class="header-pv-label">총 평가자산 &nbsp;<span id="amountEye" style="opacity:0.45; font-size:0.7rem; letter-spacing:0; text-transform:none; font-weight:400;">👁</span></div>
    <div class="header-pv-value pv-blur" id="pvMain">{fmt_num(total_market_value)}</div>
    <div class="header-pv-sub {pnl_class(total_unrealized)} pv-blur" id="pvSub">{fmt_num(total_unrealized)} ({(total_unrealized / max(sum(h['cost'] for h in overall_holdings.values()), 1) * 100):+.1f}%)</div>
  </div>
  <div class="header-right">
    <div class="header-stat">
      <div class="header-stat-label">실현손익</div>
      <div class="header-stat-value {pnl_class(overall_net_pnl)} pv-blur" id="pvPnl">{fmt_num(overall_net_pnl)}</div>
    </div>
    <div class="header-divider"></div>
    <div class="header-stat">
      <div class="header-stat-label">IRR</div>
      <div class="header-stat-value {pnl_class(overall_irr or 0)}">{fmt_pct(overall_irr) if overall_irr else 'N/A'}</div>
    </div>
    <div class="header-divider"></div>
    <div class="header-stat">
      <div class="header-stat-label">환율</div>
      <div class="header-stat-value" style="font-size:0.85rem;">USD {usd_krw:,.0f}</div>
    </div>
    <div class="header-divider"></div>
    <button id="refresh-btn" class="header-refresh-btn" onclick="refreshPrices()" title="네이버에서 현재가 새로고침">⟳ 새로고침</button>
  </div>
</div>

<div class="info-bar">
  <div class="info-bar-item"><span class="info-bar-dot"></span><span>가격 업데이트: <strong><span id="prices-updated-at">{prices_updated_at or '알 수 없음'}</span></strong></span></div>
  <div class="info-bar-item"><span class="info-bar-dot" style="background:var(--text-muted)"></span><span>JPY {jpy_krw:,.2f}원</span></div>
  <div class="info-bar-item"><span class="info-bar-dot" style="background:var(--text-muted)"></span><span>승률 {win_rate:.0f}% ({win_count}/{total_traded_count}종목)</span></div>
</div>

<div class="tabs">
  <button class="tab active" onclick="switchTab('dashboard')">대시보드</button>
  <button class="tab" onclick="switchTab('portfolio')">포트폴리오</button>
  <button class="tab" onclick="switchTab('analysis')">분석</button>
  <button class="tab" onclick="switchTab('briefing')">시장 브리핑</button>
</div>

<!-- ===== DASHBOARD TAB ===== -->
<div id="tab-dashboard" class="tab-content active">
  <!-- Hero KPIs: 핵심 4개만 크게 -->
  <div class="kpi-row primary">
    <div class="kpi">
      <div class="kpi-label">보유 평가금액</div>
      <div class="kpi-value">{fmt_num(total_market_value)}</div>
      <div class="kpi-sub">{len(overall_holdings)}종목 보유 · 원금 {fmt_num(sum(h['cost'] for h in overall_holdings.values()))}</div>
    </div>
    <div class="kpi {"border-positive" if total_unrealized >= 0 else "border-negative"}">
      <div class="kpi-label">평가손익</div>
      <div class="kpi-value {pnl_class(total_unrealized)}">{fmt_num(total_unrealized)}</div>
      <div class="kpi-sub {pnl_class(total_unrealized)}">{(total_unrealized / sum(h['cost'] for h in overall_holdings.values()) * 100) if sum(h['cost'] for h in overall_holdings.values()) > 0 else 0:+.1f}%</div>
    </div>
    <div class="kpi {"border-positive" if overall_net_pnl >= 0 else "border-negative"}">
      <div class="kpi-label">실현손익 + 배당</div>
      <div class="kpi-value {pnl_class(overall_net_pnl)}">{fmt_num(overall_net_pnl)}</div>
      <div class="kpi-sub">실현 {fmt_num(overall_realized_pnl)} + 배당 {fmt_num(overall_dividends)}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">IRR (연환산)</div>
      <div class="kpi-value {pnl_class(overall_irr or 0)}">{fmt_pct(overall_irr) if overall_irr else 'N/A'}</div>
      <div class="kpi-sub">ROI {overall_roi:+.1f}% · {len(stock_summaries)}종목 거래</div>
    </div>
  </div>

  <!-- 계좌별 현황 -->
  <div class="acct-summary-grid" id="acctSummaryGrid"></div>

  <!-- 상세 지표: 접기/펼치기 -->
  <details class="detail-section" style="margin-bottom: 24px;">
    <summary class="detail-toggle">상세 지표 보기</summary>
    <div class="detail-content">
      <div class="kpi-row secondary" style="margin-top:14px;">
        <div class="kpi">
          <div class="kpi-label">총 매수</div>
          <div class="kpi-value compact">{fmt_num(overall_invested)}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">총 매도</div>
          <div class="kpi-value compact">{fmt_num(overall_returned)}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">수수료 + 세금</div>
          <div class="kpi-value compact negative">{fmt_num(overall_fees + overall_tax)}</div>
          <div class="kpi-sub">수수료 {fmt_num(overall_fees)} · 세금 {fmt_num(overall_tax)}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">순입금</div>
          <div class="kpi-value compact">{fmt_num(overall_net_deposit)}</div>
          <div class="kpi-sub">입금 {fmt_num(overall_deposits)} / 출금 {fmt_num(overall_withdrawals)}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">대출잔액</div>
          <div class="kpi-value compact {"negative" if overall_loan_balance > 0 else ""}">{fmt_num(overall_loan_balance)}</div>
          <div class="kpi-sub">레버리지 {total_market_value / max(total_market_value - overall_loan_balance, 1):.2f}x</div>
        </div>
      </div>
      <div class="kpi-row secondary" style="grid-template-columns: repeat(6, 1fr);">
        <div class="kpi border-negative">
          <div class="kpi-label">대출이자</div>
          <div class="kpi-value compact negative">{fmt_num(overall_loan_interest)}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">대여수수료</div>
          <div class="kpi-value compact positive">{fmt_num(overall_lending_fee)}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">순금융비용</div>
          <div class="kpi-value compact negative">{fmt_num(overall_loan_interest - overall_lending_fee)}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">실질 순수익</div>
          <div class="kpi-value compact {pnl_class(overall_net_pnl - overall_loan_interest + overall_lending_fee)}">{fmt_num(overall_net_pnl - overall_loan_interest + overall_lending_fee)}</div>
          <div class="kpi-sub">손익 - 이자 + 대여수수료</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">자기자본 수익률</div>
          <div class="kpi-value compact {pnl_class(overall_net_pnl - overall_loan_interest)}">{((overall_net_pnl - overall_loan_interest) / max(overall_deposits, 1) * 100):+.1f}%</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">승률 (Win Rate)</div>
          <div class="kpi-value compact">{win_rate:.0f}%</div>
          <div class="kpi-sub">수익 {win_count} / 손실 {loss_count}종목</div>
        </div>
      </div>
    </div>
  </details>

  <!-- Asset chart on dashboard -->
  <div class="card">
    <div class="card-title">총 평가자산 추이</div>
    <div class="chart-container"><canvas id="dashAssetChart"></canvas></div>
  </div>

  <!-- Treemap + Nation Donut side by side -->
  <div style="display:grid;grid-template-columns:1fr 300px;gap:16px;margin-bottom:20px;align-items:start;">
    <div class="card" style="margin-bottom:0;">
      <div class="card-title">포트폴리오 구성 (평가금액 기준)</div>
      <div class="treemap-filters" id="treemapFilters">
        <button class="tf-btn active" onclick="filterTreemap('all')">전체</button>
        <button class="tf-btn" onclick="filterTreemap('KOR')">한국</button>
        <button class="tf-btn" onclick="filterTreemap('USA')">미국</button>
        <button class="tf-btn" onclick="filterTreemap('JPN')">일본</button>
        <button class="tf-btn" onclick="filterTreemap('other')">기타</button>
      </div>
      <div class="treemap-container" id="treemapContainer"></div>
    </div>
    <div class="card" style="margin-bottom:0;">
      <div class="card-title">국가별 배분</div>
      <div style="position:relative;height:340px;"><canvas id="nationDonutChart"></canvas></div>
      <div id="nationDonutStats" style="margin-top:8px;font-size:0.78rem;color:var(--text-dim);"></div>
    </div>
  </div>
  <div class="tm-tooltip" id="tmTooltip"></div>

  <div class="card">
    <div class="card-title">손익 TOP 종목</div>
    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
      <div>
        <h3 style="color: var(--positive); font-size: 0.9rem; margin-bottom: 8px;">수익 TOP 10</h3>
        <div class="chart-container" style="height: 250px;"><canvas id="topWinnersChart"></canvas></div>
      </div>
      <div>
        <h3 style="color: var(--negative); font-size: 0.9rem; margin-bottom: 8px;">손실 TOP 10</h3>
        <div class="chart-container" style="height: 250px;"><canvas id="topLosersChart"></canvas></div>
      </div>
    </div>
  </div>
</div>

<!-- ===== PORTFOLIO TAB ===== -->
<div id="tab-portfolio" class="tab-content">
  <div class="sub-tabs">
    <button class="sub-tab active" onclick="switchSubTab('stocks')">종목별</button>
    <button class="sub-tab" onclick="switchSubTab('byAccount')">계좌별</button>
  </div>

  <!-- Sub-tab: Stocks -->
  <div id="subtab-stocks" class="subtab-content active">
    <div class="toolbar">
      <input type="text" class="search-box" id="stockSearch" placeholder="종목명 검색..." oninput="filterStockTable()">
      <div class="filter-group">
        <button class="filter-btn active" onclick="setStockFilter('all', this)">전체</button>
        <button class="filter-btn" onclick="setStockFilter('profit', this)">수익</button>
        <button class="filter-btn" onclick="setStockFilter('loss', this)">손실</button>
        <button class="filter-btn" onclick="setStockFilter('holding', this)">보유중</button>
        <button class="filter-btn" onclick="setStockFilter('closed', this)">청산</button>
      </div>
      <span class="result-count" id="stockResultCount"></span>
    </div>
    <div class="card">
      <div class="card-title">종목별 실적 (전체 계좌 합산)</div>
      <div style="overflow-x: auto;">
        <table id="stockTable">
          <thead>
            <tr>
              <th data-col="name" data-type="string">종목명</th>
              <th data-col="invested" data-type="number" class="text-right">매수금액</th>
              <th data-col="returned" data-type="number" class="text-right">매도금액</th>
              <th data-col="realized_pnl" data-type="number" class="text-right">실현손익</th>
              <th data-col="dividends" data-type="number" class="text-right">배당</th>
              <th data-col="net_pnl" data-type="number" class="text-right">순손익</th>
              <th data-col="roi" data-type="number" class="text-right">ROI</th>
              <th data-col="irr" data-type="number" class="text-right">IRR</th>
              <th data-col="current_qty" data-type="number" class="text-right">보유수량</th>
              <th data-col="current_price" data-type="number" class="text-right">현재가</th>
              <th data-col="market_value" data-type="number" class="text-right">평가금액</th>
              <th data-col="unrealized_pnl" data-type="number" class="text-right">평가손익</th>
              <th data-col="weight" data-type="number" class="text-right">비중</th>
            </tr>
          </thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Sub-tab: By Account -->
  <div id="subtab-byAccount" class="subtab-content">
    <div class="account-selector" id="accountSelector"></div>
    <div id="accountDetail"></div>
  </div>
</div>

<!-- ===== ANALYSIS TAB ===== -->
<div id="tab-analysis" class="tab-content">

<!-- Period analysis section -->
<div class="card" style="margin-bottom:20px;">
  <div class="card-title">기간별 분석</div>

  <!-- Period preset buttons -->
  <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:16px;">
    <div class="filter-group" id="periodPresets">
      <button class="filter-btn active" onclick="setPeriodPreset('1w',this)">이번 주</button>
      <button class="filter-btn" onclick="setPeriodPreset('1m',this)">이번 달</button>
      <button class="filter-btn" onclick="setPeriodPreset('3m',this)">3개월</button>
      <button class="filter-btn" onclick="setPeriodPreset('ytd',this)">올해</button>
      <button class="filter-btn" onclick="setPeriodPreset('1y',this)">1년</button>
      <button class="filter-btn" onclick="setPeriodPreset('all',this)">전체</button>
    </div>
    <div style="display:flex; align-items:center; gap:6px; margin-left:8px;">
      <input type="date" id="periodStart" onchange="renderPeriodAnalysis()"
        style="border:1px solid var(--border); border-radius:6px; padding:5px 10px; font-size:0.83rem; background:var(--card); color:var(--text);">
      <span style="color:var(--text-muted);">~</span>
      <input type="date" id="periodEnd" onchange="renderPeriodAnalysis()"
        style="border:1px solid var(--border); border-radius:6px; padding:5px 10px; font-size:0.83rem; background:var(--card); color:var(--text);">
    </div>
  </div>

  <!-- Summary KPIs -->
  <div id="periodKpis" style="display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin-bottom:20px;"></div>

  <!-- Portfolio diff + trade list (two columns) -->
  <div style="display:grid; grid-template-columns:280px 1fr; gap:20px; align-items:start;">
    <div>
      <div style="font-size:0.75rem; font-weight:700; color:var(--text-muted); letter-spacing:.06em; text-transform:uppercase; margin-bottom:8px;">포트폴리오 변동</div>
      <div id="periodPortfolioDiff"></div>
    </div>
    <div>
      <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
        <div style="font-size:0.75rem; font-weight:700; color:var(--text-muted); letter-spacing:.06em; text-transform:uppercase;">거래 내역</div>
        <div class="filter-group" id="tradeViewToggle">
          <button class="filter-btn active" onclick="setTradeView('all',this)">전체</button>
          <button class="filter-btn" onclick="setTradeView('stock',this)">종목별</button>
        </div>
      </div>
      <div id="periodTrades" style="max-height:420px; overflow-y:auto;"></div>
    </div>
  </div>
</div>

  <div class="card">
    <div class="card-title">월별 투자/회수 추이</div>
    <div class="chart-container"><canvas id="timelineChart"></canvas></div>
  </div>
  <div class="card">
    <div class="card-title">총 평가자산 vs 누적 투자</div>
    <div class="chart-container"><canvas id="totalAssetChart"></canvas></div>
  </div>
  <div class="card">
    <div class="card-title">누적 투자 vs 회수 vs 배당</div>
    <div class="chart-container"><canvas id="cumCompareChart"></canvas></div>
  </div>
  <div class="card">
    <div class="card-title">누적 순현금흐름</div>
    <div class="chart-container"><canvas id="cashflowChart"></canvas></div>
  </div>
  <div class="card">
    <div class="card-title">월별 배당금</div>
    <div class="chart-container"><canvas id="dividendChart"></canvas></div>
  </div>
</div>

<!-- ===== BRIEFING TAB ===== -->
<div id="tab-briefing" class="tab-content">
  <!-- Period selector -->
  <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:20px; flex-wrap:wrap; gap:12px;">
    <div class="filter-group" id="briefingPeriodFilter"></div>
    <span id="briefingUpdatedAt" style="color:var(--text-dim); font-size:0.8rem;"></span>
  </div>

  <!-- Summary section -->
  <div id="briefingSummarySection"></div>

  <!-- Raw posts toggle -->
  <div style="margin-top:24px;">
    <button onclick="toggleRawPosts()" id="rawPostsToggleBtn"
      style="background:none; border:1px solid var(--border); color:var(--text-dim); border-radius:8px; padding:7px 16px; font-size:0.82rem; cursor:pointer;">
      ▶ 채널별 원문 보기
    </button>
    <div id="rawPostsSection" style="display:none; margin-top:16px;">
      <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; flex-wrap:wrap; gap:10px;">
        <div style="display:flex; align-items:center; gap:12px;">
          <select id="briefingDateSelect" onchange="renderRawPosts()" style="background:var(--card); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:7px 12px; font-size:0.85rem;"></select>
          <span id="briefingSourceCount" style="color:var(--text-dim); font-size:0.82rem;"></span>
        </div>
        <div class="filter-group" id="briefingSourceFilter"></div>
      </div>
      <div id="briefingContent"></div>
    </div>
  </div>
</div>
</div>

<div class="tm-tooltip" id="acctTmTooltip"></div>

<script>
const ACCOUNTS = """ + json.dumps(js_account_data, ensure_ascii=False) + """;
const STOCKS = """ + json.dumps(js_stock_data, ensure_ascii=False) + """;
const OVERALL = """ + json.dumps(js_overall, ensure_ascii=False) + """;
const TIMELINE = """ + json.dumps(timeline, ensure_ascii=False) + """;
const TREEMAP_DATA = """ + json.dumps(treemap_data, ensure_ascii=False) + """;
const BRIEFING = """ + json.dumps(briefing_data, ensure_ascii=False) + """;
const BRIEFING_SUMMARY = """ + json.dumps(briefing_summary, ensure_ascii=False) + """;
const TXS = """ + json.dumps([
    {"d": tx["date"], "t": tx["type"], "s": tx.get("stock",""), "a": round(tx.get("amount",0)), "q": round(tx.get("qty",0),4), "acc": tx.get("account","")}
    for tx in all_txs
    if tx["type"] in ("buy","sell","dividend","fee","tax","lending_fee")
], ensure_ascii=False) + """;
const STOCK_CODES = """ + json.dumps({name: v["code"] for name, v in raw_prices.items() if not name.startswith("_") and "code" in v}, ensure_ascii=False) + """;
const STOCK_NATIONS = """ + json.dumps({name: v.get("nation","KOR") for name, v in raw_prices.items() if not name.startswith("_") and "code" in v}, ensure_ascii=False) + """;

// Light theme Chart.js defaults
Chart.defaults.color = '#4a5568';
Chart.defaults.borderColor = '#dde3ee';
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans KR', sans-serif";
Chart.defaults.font.size = 11;
Chart.defaults.plugins.legend.labels.color = '#0f172a';
Chart.defaults.plugins.tooltip.backgroundColor = '#ffffff';
Chart.defaults.plugins.tooltip.titleColor = '#0f172a';
Chart.defaults.plugins.tooltip.bodyColor = '#4a5568';
Chart.defaults.plugins.tooltip.borderColor = '#dde3ee';
Chart.defaults.plugins.tooltip.borderWidth = 1;
Chart.defaults.plugins.tooltip.boxShadow = '0 4px 12px rgba(0,0,0,0.1)';

async function refreshPrices() {
  const btn = document.getElementById('refresh-btn');
  btn.textContent = '⟳ 조회 중...';
  btn.disabled = true;
  const now = new Date();
  let updated = 0, failed = 0;
  const newPrices = {};

  for (const [name, code] of Object.entries(STOCK_CODES)) {
    try {
      const url = `https://m.stock.naver.com/api/stock/${code}/basic`;
      const r = await fetch(url);
      if (!r.ok) { failed++; continue; }
      const d = await r.json();
      const priceStr = (d.closePrice || '0').replace(/,/g, '');
      const price = parseFloat(priceStr);
      if (!price) { failed++; continue; }
      newPrices[name] = price;
      updated++;
    } catch(e) { failed++; }
  }

  // Update STOCKS (포트폴리오 탭 데이터)
  for (const s of STOCKS) {
    if (newPrices[s.name] != null) {
      s.current_price = newPrices[s.name];
      s.current_value = s.shares * newPrices[s.name];
      s.pnl = s.current_value - s.cost;
    }
  }
  // Update per-account stocks
  for (const acct of Object.values(ACCOUNTS)) {
    for (const s of (acct.stocks || [])) {
      if (newPrices[s.name] != null) {
        s.current_price = newPrices[s.name];
        s.current_value = s.shares * newPrices[s.name];
        s.pnl = s.current_value - s.cost;
      }
    }
  }

  const ts = now.toLocaleString('ko-KR', {timeZone:'Asia/Seoul'}) + ' (실시간)';
  document.getElementById('prices-updated-at').textContent = ts;
  btn.textContent = `⟳ 새로고침 (${updated}건)`;
  btn.disabled = false;

  // Re-render current view
  const activeTab = document.querySelector('.tab-content.active')?.id;
  if (activeTab === 'tab-portfolio') renderPortfolio();
  if (activeTab === 'tab-dashboard') renderDashboard();
}

function fmt(n) {
  if (n == null) return 'N/A';
  return Math.round(n).toLocaleString('ko-KR');
}
function pnlCls(v) { return v > 0 ? 'positive' : v < 0 ? 'negative' : ''; }

// ===== TREEMAP =====
function getTreemapColor(retPct) {
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const t = clamp(retPct / 30, -1, 1);
  if (t >= 0) {
    const r = Math.round(30 + (1 - t) * 20);
    const g = Math.round(80 + t * 120);
    const b = Math.round(30 + (1 - t) * 20);
    return `rgb(${r},${g},${b})`;
  } else {
    const at = -t;
    const r = Math.round(80 + at * 140);
    const g = Math.round(30 + (1 - at) * 30);
    const b = Math.round(30 + (1 - at) * 20);
    return `rgb(${r},${g},${b})`;
  }
}

function squarify(items, x, y, w, h) {
  if (items.length === 0) return [];
  const totalVal = items.reduce((s, it) => s + it.market_value, 0);
  if (totalVal <= 0) return [];
  const rects = [];

  function doLayout(items, x, y, w, h) {
    if (items.length === 0) return;
    if (items.length === 1) {
      rects.push({ ...items[0], x, y, w, h });
      return;
    }
    const total = items.reduce((s, it) => s + it.market_value, 0);
    const isHoriz = w >= h;
    let rowItems = [items[0]];
    let rowSum = items[0].market_value;

    function worstAspect(row, rowSum) {
      const side = isHoriz ? h : w;
      const rowFrac = rowSum / total;
      const rowLen = isHoriz ? w * rowFrac : h * rowFrac;
      if (rowLen <= 0) return Infinity;
      let worst = 0;
      row.forEach(it => {
        const frac = it.market_value / rowSum;
        const cellLen = side * frac;
        const aspect = Math.max(rowLen / cellLen, cellLen / rowLen);
        worst = Math.max(worst, aspect);
      });
      return worst;
    }

    for (let i = 1; i < items.length; i++) {
      const curWorst = worstAspect(rowItems, rowSum);
      const newRow = [...rowItems, items[i]];
      const newSum = rowSum + items[i].market_value;
      const newWorst = worstAspect(newRow, newSum);
      if (newWorst <= curWorst) {
        rowItems = newRow;
        rowSum = newSum;
      } else {
        break;
      }
    }

    const rowFrac = rowSum / total;
    const remaining = items.slice(rowItems.length);

    let offset = 0;
    if (isHoriz) {
      const rowW = w * rowFrac;
      rowItems.forEach(it => {
        const cellFrac = it.market_value / rowSum;
        const cellH = h * cellFrac;
        rects.push({ ...it, x: x, y: y + offset, w: rowW, h: cellH });
        offset += cellH;
      });
      doLayout(remaining, x + rowW, y, w - rowW, h);
    } else {
      const rowH = h * rowFrac;
      rowItems.forEach(it => {
        const cellFrac = it.market_value / rowSum;
        const cellW = w * cellFrac;
        rects.push({ ...it, x: x + offset, y: y, w: cellW, h: rowH });
        offset += cellW;
      });
      doLayout(remaining, x, y + rowH, w, h - rowH);
    }
  }

  doLayout(items, x, y, w, h);
  return rects;
}

function renderTreemapInContainer(containerId, data, tooltipId) {
  const container = document.getElementById(containerId);
  if (!container || !data || data.length === 0) return;
  const W = container.clientWidth;
  const H = container.clientHeight;
  if (W <= 0 || H <= 0) return;
  const rects = squarify(data, 0, 0, W, H);
  const tooltip = document.getElementById(tooltipId);

  container.innerHTML = rects.map(r => {
    const sizeClass = (r.w < 60 || r.h < 40) ? 'tiny' : (r.w < 100 || r.h < 55) ? 'small' : '';
    return `<div class="treemap-cell ${sizeClass}" style="left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px;background:${getTreemapColor(r.return_pct)}"
      data-name="${r.name}" data-ret="${r.return_pct}" data-mv="${r.market_value}" data-qty="${r.qty}" data-cp="${r.current_price}" data-cost="${r.cost}" data-upnl="${r.unrealized_pnl}" data-weight="${r.weight || 0}" data-nation="${r.nation || 'KOR'}">
      <span class="name">${r.name}</span>
      <span class="pct">${r.return_pct >= 0 ? '+' : ''}${r.return_pct.toFixed(1)}%</span>
      <span class="val">${fmt(r.market_value)}</span>
    </div>`;
  }).join('');

  container.querySelectorAll('.treemap-cell').forEach(cell => {
    cell.addEventListener('mouseenter', e => {
      const d = cell.dataset;
      tooltip.innerHTML = `<div class="tt-name">${d.name}</div>
        <div class="tt-row"><span class="tt-label">평가금액</span><span>${fmt(+d.mv)}</span></div>
        <div class="tt-row"><span class="tt-label">수량</span><span>${(+d.qty).toLocaleString()}주</span></div>
        <div class="tt-row"><span class="tt-label">현재가</span><span>${fmt(+d.cp)}</span></div>
        <div class="tt-row"><span class="tt-label">원가</span><span>${fmt(+d.cost)}</span></div>
        <div class="tt-row"><span class="tt-label">평가손익</span><span class="${pnlCls(+d.upnl)}">${fmt(+d.upnl)} (${(+d.ret) >= 0 ? '+' : ''}${d.ret}%)</span></div>
        <div class="tt-row"><span class="tt-label">비중</span><span>${d.weight}%</span></div>`;
      tooltip.style.display = 'block';
    });
    cell.addEventListener('mousemove', e => {
      tooltip.style.left = (e.clientX + 12) + 'px';
      tooltip.style.top = (e.clientY + 12) + 'px';
    });
    cell.addEventListener('mouseleave', () => {
      tooltip.style.display = 'none';
    });
  });
}

let currentTreemapFilter = 'all';
function renderTreemap() {
  renderTreemapInContainer('treemapContainer', TREEMAP_DATA, 'tmTooltip');
  filterTreemap(currentTreemapFilter, true);
}
function filterTreemap(nation, skipRender) {
  currentTreemapFilter = nation;
  document.querySelectorAll('#treemapFilters .tf-btn').forEach(b => b.classList.remove('active'));
  event && event.target && event.target.classList.add('active');
  if (!event || !event.target) {
    document.querySelectorAll('#treemapFilters .tf-btn').forEach(b => {
      if ((nation === 'all' && b.textContent === '전체') ||
          (nation === 'KOR' && b.textContent === '한국') ||
          (nation === 'USA' && b.textContent === '미국') ||
          (nation === 'JPN' && b.textContent === '일본') ||
          (nation === 'other' && b.textContent === '기타'))
        b.classList.add('active');
    });
  }
  const otherNations = new Set(['CHN', 'HKG']);
  document.querySelectorAll('#treemapContainer .treemap-cell').forEach(cell => {
    const n = cell.dataset.nation;
    if (nation === 'all') { cell.classList.remove('dimmed'); }
    else if (nation === 'other') { cell.classList.toggle('dimmed', !otherNations.has(n)); }
    else { cell.classList.toggle('dimmed', n !== nation); }
  });
}

// ===== Sub-tab switching =====
function switchSubTab(name) {
  document.querySelectorAll('.sub-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.subtab-content').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('subtab-' + name).classList.add('active');
  if (name === 'stocks') renderStockTable();
  if (name === 'byAccount') {
    initAccounts();
    // Always re-render account treemap when subtab becomes visible
    renderAcctTreemap();
  }
}

// ===== Amount visibility toggle =====
let amountsHidden = true;
function toggleAmounts() {
  amountsHidden = !amountsHidden;
  document.querySelectorAll('.pv-blur').forEach(el => {
    el.style.filter = amountsHidden ? '' : 'none';
    el.style.userSelect = amountsHidden ? '' : 'text';
  });
  const eye = document.getElementById('amountEye');
  if (eye) eye.textContent = amountsHidden ? '👁' : '🙈';
}

// ===== Tab switching =====
function switchTab(name) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'analysis') initAnalysis();
  if (name === 'portfolio') { renderStockTable(); initAccounts(); }
  if (name === 'dashboard') { setTimeout(renderTreemap, 50); }
  if (name === 'analysis') initPeriodAnalysis();
  if (name === 'briefing') initBriefing();
}

// ===== STOCK TABLE =====
let stockSortCol = 'net_pnl';
let stockSortDir = 'desc';
let stockFilter = 'all';
let stockData = [...STOCKS];

function setStockFilter(filter, btn) {
  stockFilter = filter;
  document.querySelectorAll('#subtab-stocks .filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderStockTable();
}

function applyStockFilter(data) {
  switch (stockFilter) {
    case 'profit': return data.filter(s => s.net_pnl > 0);
    case 'loss': return data.filter(s => s.net_pnl < 0);
    case 'holding': return data.filter(s => s.current_qty > 0);
    case 'closed': return data.filter(s => s.current_qty === 0);
    default: return data;
  }
}

function renderStockTable() {
  const tbody = document.querySelector('#stockTable tbody');
  if (!tbody) return;
  const searchEl = document.getElementById('stockSearch');
  const search = searchEl ? searchEl.value.toLowerCase() : '';
  let data = stockData.filter(s => s.name.toLowerCase().includes(search));
  data = applyStockFilter(data);
  data.sort((a, b) => {
    let va = a[stockSortCol], vb = b[stockSortCol];
    if (va == null) va = -Infinity; if (vb == null) vb = -Infinity;
    if (typeof va === 'string') return stockSortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
    return stockSortDir === 'asc' ? (va > vb ? 1 : -1) : (va < vb ? 1 : -1);
  });
  const countEl = document.getElementById('stockResultCount');
  if (countEl) countEl.textContent = data.length + '/' + stockData.length + '종목';
  tbody.innerHTML = data.map(s => `
    <tr>
      <td><strong>${s.name}</strong></td>
      <td class="text-right mono">${fmt(s.invested)}</td>
      <td class="text-right mono">${fmt(s.returned)}</td>
      <td class="text-right mono ${pnlCls(s.realized_pnl)}">${fmt(s.realized_pnl)}</td>
      <td class="text-right mono">${s.dividends > 0 ? fmt(s.dividends) : '-'}</td>
      <td class="text-right mono ${pnlCls(s.net_pnl)}"><strong>${fmt(s.net_pnl)}</strong></td>
      <td class="text-right mono ${pnlCls(s.roi)}">${s.roi.toFixed(1)}%</td>
      <td class="text-right mono ${pnlCls(s.irr)}">${s.irr != null ? s.irr.toFixed(1) + '%' : '-'}</td>
      <td class="text-right mono">${s.current_qty > 0 ? s.current_qty.toLocaleString() : '-'}</td>
      <td class="text-right mono">${s.current_qty > 0 ? fmt(s.current_price) : '-'}</td>
      <td class="text-right mono">${s.market_value > 0 ? '<strong>' + fmt(s.market_value) + '</strong>' : '-'}</td>
      <td class="text-right mono ${pnlCls(s.unrealized_pnl)}">${s.current_qty > 0 ? fmt(s.unrealized_pnl) : '-'}</td>
      <td class="text-right mono">${s.weight > 0 ? s.weight.toFixed(1) + '%' : '-'}</td>
    </tr>
  `).join('');

  document.querySelectorAll('#stockTable th').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.col === stockSortCol) th.classList.add('sort-' + stockSortDir);
  });
}

document.querySelectorAll('#stockTable th').forEach(th => {
  th.addEventListener('click', () => {
    const col = th.dataset.col;
    if (stockSortCol === col) stockSortDir = stockSortDir === 'asc' ? 'desc' : 'asc';
    else { stockSortCol = col; stockSortDir = 'desc'; }
    renderStockTable();
  });
});

function filterStockTable() { renderStockTable(); }

// ===== ACCOUNTS =====
let currentAccount = null;
let accountsInited = false;

function renderAcctTreemap() {
  if (!currentAccount) return;
  const data = ACCOUNTS[currentAccount].treemap;
  if (!data || data.length === 0) return;
  setTimeout(function() {
    var el = document.getElementById('acctTreemapContainer');
    if (el && el.offsetWidth > 0) {
      renderTreemapInContainer('acctTreemapContainer', data, 'acctTmTooltip');
    }
  }, 200);
}
function initAccounts() {
  const sel = document.getElementById('accountSelector');
  if (!sel) return;
  if (sel.children.length > 0) return;
  accountsInited = true;
  Object.keys(ACCOUNTS).forEach((acc, i) => {
    const btn = document.createElement('button');
    btn.className = 'account-btn' + (i === 0 ? ' active' : '');
    btn.textContent = acc + ' (' + ACCOUNTS[acc].stocks.length + '종목)';
    btn.onclick = () => {
      sel.querySelectorAll('.account-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      renderAccount(acc);
    };
    sel.appendChild(btn);
  });
  renderAccount(Object.keys(ACCOUNTS)[0]);
}

let acctSortCol = 'net_pnl';
let acctSortDir = 'desc';
let acctFilter = 'all';
let acctSearch = '';

function setAcctFilter(filter, btn) {
  acctFilter = filter;
  document.querySelectorAll('#acctFilterGroup .filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderAccountTable();
}

function renderAccountTable() {
  if (!currentAccount) return;
  const data = ACCOUNTS[currentAccount];
  const search = acctSearch.toLowerCase();

  let stocks = [...data.stocks];
  if (search) stocks = stocks.filter(s => s.name.toLowerCase().includes(search));
  switch (acctFilter) {
    case 'profit': stocks = stocks.filter(s => s.net_pnl > 0); break;
    case 'loss': stocks = stocks.filter(s => s.net_pnl < 0); break;
    case 'holding': stocks = stocks.filter(s => s.current_qty > 0); break;
    case 'closed': stocks = stocks.filter(s => s.current_qty === 0); break;
  }
  stocks.sort((a, b) => {
    let va = a[acctSortCol], vb = b[acctSortCol];
    if (va == null) va = -Infinity; if (vb == null) vb = -Infinity;
    if (typeof va === 'string') return acctSortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va);
    return acctSortDir === 'asc' ? (va > vb ? 1 : -1) : (va < vb ? 1 : -1);
  });

  const countEl = document.getElementById('acctResultCount');
  if (countEl) countEl.textContent = stocks.length + '/' + data.stocks.length + '종목';

  const tbody = document.querySelector('#acctStockTable tbody');
  if (!tbody) return;
  tbody.innerHTML = stocks.map(s => `<tr>
    <td><strong>${s.name}</strong></td>
    <td class="text-right mono">${fmt(s.invested)}</td>
    <td class="text-right mono">${fmt(s.returned)}</td>
    <td class="text-right mono ${pnlCls(s.realized_pnl)}">${fmt(s.realized_pnl)}</td>
    <td class="text-right mono">${s.dividends > 0 ? fmt(s.dividends) : '-'}</td>
    <td class="text-right mono ${pnlCls(s.net_pnl)}"><strong>${fmt(s.net_pnl)}</strong></td>
    <td class="text-right mono ${pnlCls(s.roi)}">${s.roi.toFixed(1)}%</td>
    <td class="text-right mono ${pnlCls(s.irr)}">${s.irr != null ? s.irr.toFixed(1) + '%' : '-'}</td>
    <td class="text-right mono">${s.current_qty > 0 ? s.current_qty.toLocaleString() + '주' : '-'}</td>
    <td class="text-right mono">${s.current_qty > 0 ? fmt(s.current_price) : '-'}</td>
    <td class="text-right mono">${s.market_value > 0 ? '<strong>' + fmt(s.market_value) + '</strong>' : '-'}</td>
    <td class="text-right mono ${pnlCls(s.unrealized_pnl)}">${s.current_qty > 0 ? fmt(s.unrealized_pnl) : '-'}</td>
    <td class="text-right mono">${s.weight > 0 ? s.weight.toFixed(1) + '%' : '-'}</td>
  </tr>`).join('');

  document.querySelectorAll('#acctStockTable th').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.col === acctSortCol) th.classList.add('sort-' + acctSortDir);
  });
}

function bindAcctTableSort() {
  document.querySelectorAll('#acctStockTable th[data-col]').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      if (acctSortCol === col) acctSortDir = acctSortDir === 'asc' ? 'desc' : 'asc';
      else { acctSortCol = col; acctSortDir = 'desc'; }
      renderAccountTable();
    });
  });
}

function renderAccount(acc) {
  currentAccount = acc;
  acctSearch = '';
  acctFilter = 'all';
  const data = ACCOUNTS[acc];
  const detail = document.getElementById('accountDetail');
  if (!detail) return;
  const netPnl = data.realized_pnl + data.dividends;
  const roi = data.total_invested > 0 ? (netPnl / data.total_invested * 100).toFixed(1) : 0;

  let holdingsHtml = '';
  if (Object.keys(data.holdings).length > 0) {
    holdingsHtml = '<div class="card"><div class="card-title">보유 종목</div><div class="holdings-grid">';
    Object.entries(data.holdings).sort((a,b) => b[1].cost - a[1].cost).forEach(([stock, h]) => {
      holdingsHtml += `<div class="holding-card"><div class="holding-name">${stock}</div><div class="holding-detail">${h.qty.toLocaleString()}주 x ${h.avg_price.toLocaleString()}원</div><div class="holding-detail">원가 ${fmt(h.cost)}</div></div>`;
    });
    holdingsHtml += '</div></div>';
  }

  let treemapHtml = '';
  if (data.treemap && data.treemap.length > 0) {
    treemapHtml = '<div class="card"><div class="card-title">포트폴리오 구성 (평가금액 기준)</div><div class="treemap-container acct-treemap" id="acctTreemapContainer"></div></div>';
  }

  detail.innerHTML = `
    <div class="kpi-row primary">
      <div class="kpi"><div class="kpi-label">매수금액</div><div class="kpi-value">${fmt(data.total_invested)}</div><div class="kpi-sub">${data.num_trades}건 거래</div></div>
      <div class="kpi"><div class="kpi-label">매도금액</div><div class="kpi-value">${fmt(data.total_returned)}</div></div>
      <div class="kpi"><div class="kpi-label">실현손익</div><div class="kpi-value ${pnlCls(data.realized_pnl)}">${fmt(data.realized_pnl)}</div></div>
      <div class="kpi"><div class="kpi-label">배당</div><div class="kpi-value">${fmt(data.dividends)}</div></div>
    </div>
    <div class="kpi-row secondary" style="margin-bottom:20px;">
      <div class="kpi"><div class="kpi-label">순손익</div><div class="kpi-value compact ${pnlCls(netPnl)}">${fmt(netPnl)}</div><div class="kpi-sub">ROI ${roi}%</div></div>
      <div class="kpi"><div class="kpi-label">IRR</div><div class="kpi-value compact ${pnlCls(data.irr)}">${data.irr != null ? data.irr.toFixed(1) + '%' : 'N/A'}</div></div>
      ${data.loan_interest > 0 ? `<div class="kpi"><div class="kpi-label">대출이자</div><div class="kpi-value compact negative">${fmt(data.loan_interest)}</div><div class="kpi-sub">대여수수료 +${fmt(data.lending_fee || 0)}</div></div>` : ''}
    </div>
    ${data.total_deposits > 0 ? `<div class="kpi-row secondary" style="margin-bottom:20px;">
      <div class="kpi"><div class="kpi-label">순입금액</div><div class="kpi-value compact">${fmt(data.net_deposit)}</div><div class="kpi-sub">입금 ${fmt(data.total_deposits)} / 출금 ${fmt(data.total_withdrawals)}</div></div>
      <div class="kpi"><div class="kpi-label">대출잔액</div><div class="kpi-value compact ${data.loan_balance > 0 ? 'negative' : ''}">${fmt(data.loan_balance)}</div></div>
      ${data.loan_interest > 0 ? `<div class="kpi"><div class="kpi-label">실질 순수익</div><div class="kpi-value compact ${pnlCls(netPnl - data.loan_interest + (data.lending_fee || 0))}">${fmt(netPnl - data.loan_interest + (data.lending_fee || 0))}</div><div class="kpi-sub">손익 - 이자 + 대여수수료</div></div>` : ''}
    </div>` : ''}
    ${treemapHtml}
    ${holdingsHtml}
    <div class="card">
      <div class="card-title">종목별 실적</div>
      <div class="toolbar">
        <input type="text" class="search-box" id="acctSearch" placeholder="종목명 검색..." oninput="acctSearch=this.value; renderAccountTable()">
        <div class="filter-group" id="acctFilterGroup">
          <button class="filter-btn active" onclick="setAcctFilter('all', this)">전체</button>
          <button class="filter-btn" onclick="setAcctFilter('profit', this)">수익</button>
          <button class="filter-btn" onclick="setAcctFilter('loss', this)">손실</button>
          <button class="filter-btn" onclick="setAcctFilter('holding', this)">보유중</button>
          <button class="filter-btn" onclick="setAcctFilter('closed', this)">청산</button>
        </div>
        <span class="result-count" id="acctResultCount"></span>
      </div>
      <div style="overflow-x: auto;">
        <table id="acctStockTable">
          <thead><tr>
            <th data-col="name">종목명</th><th data-col="invested" class="text-right">매수금액</th><th data-col="returned" class="text-right">매도금액</th>
            <th data-col="realized_pnl" class="text-right">실현손익</th><th data-col="dividends" class="text-right">배당</th><th data-col="net_pnl" class="text-right sort-desc">순손익</th>
            <th data-col="roi" class="text-right">ROI</th><th data-col="irr" class="text-right">IRR</th><th data-col="current_qty" class="text-right">보유</th>
            <th data-col="current_price" class="text-right">현재가</th><th data-col="market_value" class="text-right">평가금액</th><th data-col="unrealized_pnl" class="text-right">평가손익</th><th data-col="weight" class="text-right">비중</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
  `;
  bindAcctTableSort();
  renderAccountTable();

  // Render account treemap after DOM is ready
  renderAcctTreemap();
}

// ===== TOP WINNERS/LOSERS CHARTS =====
// Dashboard asset chart (always visible on dashboard tab)
new Chart(document.getElementById('dashAssetChart'), {
  type: 'line',
  data: {
    labels: TIMELINE.map(t => t.month),
    datasets: [
      {
        label: '총 평가자산',
        data: TIMELINE.map(t => t.total_asset),
        borderColor: '#1a56db',
        backgroundColor: 'rgba(26,86,219,0.07)',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2.5,
      },
      {
        label: '누적 투자금',
        data: TIMELINE.map(t => t.cum_invested),
        borderColor: '#94a3b8',
        backgroundColor: 'rgba(239,68,68,0.03)',
        fill: false,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2,
        borderDash: [5, 3],
      },
    ]
  },
  options: {
    responsive: true, maintainAspectRatio: false,
    plugins: {
      legend: { labels: { color: '#0f172a' } },
      tooltip: {
        callbacks: {
          label: ctx => ctx.dataset.label + ': ' + fmt(ctx.raw),
          afterBody: function(items) {
            const idx = items[0].dataIndex;
            const t = TIMELINE[idx];
            const pnl = t.total_asset - t.cum_invested;
            const pnlPct = t.cum_invested > 0 ? (pnl / t.cum_invested * 100).toFixed(1) : '0.0';
            return '손익: ' + fmt(pnl) + ' (' + pnlPct + '%)';
          }
        }
      }
    },
    scales: {
      x: { ticks: { color: '#4a5568', maxRotation: 45 }, grid: { display: false } },
      y: { ticks: { callback: v => fmt(v), color: '#4a5568' }, grid: { color: 'rgba(0,0,0,0.06)' } }
    }
  }
});

function initOverallCharts() {
  const winners = STOCKS.filter(s => s.net_pnl > 0).sort((a,b) => b.net_pnl - a.net_pnl).slice(0, 10);
  const losers = STOCKS.filter(s => s.net_pnl < 0).sort((a,b) => a.net_pnl - b.net_pnl).slice(0, 10);

  const chartOpts = (data, color) => ({
    type: 'bar',
    data: {
      labels: data.map(s => s.name),
      datasets: [{
        data: data.map(s => s.net_pnl),
        backgroundColor: color + '88',
        borderColor: color,
        borderWidth: 1,
        borderRadius: 4,
      }]
    },
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: ctx => fmt(ctx.raw) + '원' } } },
      scales: {
        x: { ticks: { callback: v => fmt(v), color: '#4a5568' }, grid: { color: 'rgba(0,0,0,0.06)' } },
        y: { ticks: { color: '#0f172a', font: { size: 11 } }, grid: { display: false } }
      }
    }
  });

  new Chart(document.getElementById('topWinnersChart'), chartOpts(winners, '#0a7c59'));
  new Chart(document.getElementById('topLosersChart'), chartOpts(losers, '#c81e1e'));

  // Nation donut chart
  const nationLabel = { 'KOR': '한국', 'USA': '미국', 'JPN': '일본', 'CHN': '중국', 'HKG': '홍콩' };
  const nationColorMap = { '한국': '#6366f1', '미국': '#f59e0b', '일본': '#22c55e', '중국': '#ef4444', '홍콩': '#ec4899', '기타': '#8b8fa3' };
  const nationMv = {};
  for (const item of TREEMAP_DATA) {
    const label = nationLabel[item.nation] || '기타';
    nationMv[label] = (nationMv[label] || 0) + item.market_value;
  }
  const nationEntries = Object.entries(nationMv).sort((a,b) => b[1]-a[1]);
  const totalMv = nationEntries.reduce((s,e) => s+e[1], 0);
  // Stats below donut
  const statsEl = document.getElementById('nationDonutStats');
  if (statsEl) {
    statsEl.innerHTML = nationEntries.map(([name, mv]) =>
      `<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--border);">
        <span><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${nationColorMap[name]||'#8b8fa3'};margin-right:6px;"></span>${name}</span>
        <span style="font-feature-settings:'tnum'">${fmt(mv)} <span style="color:var(--text-muted)">(${(mv/totalMv*100).toFixed(1)}%)</span></span>
      </div>`
    ).join('');
  }
  new Chart(document.getElementById('nationDonutChart'), {
    type: 'doughnut',
    data: {
      labels: nationEntries.map(e => e[0]),
      datasets: [{
        data: nationEntries.map(e => e[1]),
        backgroundColor: nationEntries.map(e => nationColorMap[e[0]] || '#8b8fa3'),
        borderWidth: 3,
        borderColor: '#ffffff',
        hoverOffset: 8,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      cutout: '62%',
      plugins: {
        legend: { position: 'bottom', labels: { color: '#0f172a', font: { size: 11 }, padding: 10, boxWidth: 12 } },
        tooltip: {
          callbacks: {
            label: ctx => {
              const total = ctx.dataset.data.reduce((a,b) => a+b, 0);
              const pct = (ctx.raw / total * 100).toFixed(1);
              return ' ' + ctx.label + ': ' + fmt(ctx.raw) + ' (' + pct + '%)';
            }
          }
        }
      }
    }
  });
}

// ===== ANALYSIS (TIMELINE) =====
let analysisInited = false;
function initAnalysis() {
  if (analysisInited) return;
  analysisInited = true;

  // Chart 1: Monthly buy/sell bars
  new Chart(document.getElementById('timelineChart'), {
    type: 'bar',
    data: {
      labels: TIMELINE.map(t => t.month),
      datasets: [
        { label: '매수', data: TIMELINE.map(t => t.invested),
          backgroundColor: '#ef444488', borderColor: '#ef4444', borderWidth: 1, borderRadius: 3 },
        { label: '매도', data: TIMELINE.map(t => t.returned),
          backgroundColor: '#22c55e88', borderColor: '#22c55e', borderWidth: 1, borderRadius: 3 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#0f172a' } }, tooltip: { callbacks: { label: ctx => ctx.dataset.label + ': ' + fmt(ctx.raw) } } },
      scales: {
        x: { ticks: { color: '#4a5568', maxRotation: 45 }, grid: { display: false } },
        y: { ticks: { callback: v => fmt(v), color: '#4a5568' }, grid: { color: 'rgba(0,0,0,0.06)' } }
      }
    }
  });

  // Chart: Total Asset vs Cumulative Invested
  new Chart(document.getElementById('totalAssetChart'), {
    type: 'line',
    data: {
      labels: TIMELINE.map(t => t.month),
      datasets: [
        {
          label: '총 평가자산',
          data: TIMELINE.map(t => t.total_asset),
          borderColor: '#1a56db',
          backgroundColor: 'rgba(26,86,219,0.07)',
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2.5,
        },
        {
          label: '누적 투자금',
          data: TIMELINE.map(t => t.cum_invested),
          borderColor: '#94a3b8',
          backgroundColor: 'rgba(148,163,184,0.05)',
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2,
          borderDash: [5, 3],
        },
        {
          label: '보유주식 평가액',
          data: TIMELINE.map(t => t.holdings_value),
          borderColor: '#0a7c59',
          backgroundColor: 'rgba(10,124,89,0.05)',
          fill: false,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 1.5,
          borderDash: [3, 3],
        },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#0f172a' } },
        tooltip: {
          callbacks: {
            label: ctx => ctx.dataset.label + ': ' + fmt(ctx.raw),
            afterBody: function(items) {
              const idx = items[0].dataIndex;
              const t = TIMELINE[idx];
              const pnl = t.total_asset - t.cum_invested;
              const pnlPct = t.cum_invested > 0 ? (pnl / t.cum_invested * 100).toFixed(1) : '0.0';
              return '손익: ' + fmt(pnl) + ' (' + pnlPct + '%)';
            }
          }
        }
      },
      scales: {
        x: { ticks: { color: '#4a5568', maxRotation: 45 }, grid: { display: false } },
        y: { ticks: { callback: v => fmt(v), color: '#4a5568' }, grid: { color: 'rgba(0,0,0,0.06)' } }
      }
    }
  });

  // Chart 2: Cumulative invested vs returned vs dividends
  new Chart(document.getElementById('cumCompareChart'), {
    type: 'line',
    data: {
      labels: TIMELINE.map(t => t.month),
      datasets: [
        {
          label: '누적 매수',
          data: TIMELINE.map(t => t.cum_invested),
          borderColor: '#c81e1e',
          backgroundColor: 'rgba(200,30,30,0.08)',
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2,
        },
        {
          label: '누적 매도',
          data: TIMELINE.map(t => t.cum_returned),
          borderColor: '#0a7c59',
          backgroundColor: 'rgba(10,124,89,0.08)',
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2,
        },
        {
          label: '누적 배당',
          data: TIMELINE.map(t => t.cum_dividends),
          borderColor: '#1a56db',
          backgroundColor: 'rgba(26,86,219,0.08)',
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2,
        },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#0f172a' } }, tooltip: { callbacks: { label: ctx => ctx.dataset.label + ': ' + fmt(ctx.raw) } } },
      scales: {
        x: { ticks: { color: '#4a5568', maxRotation: 45 }, grid: { display: false } },
        y: { ticks: { callback: v => fmt(v), color: '#4a5568' }, grid: { color: 'rgba(0,0,0,0.06)' } }
      }
    }
  });

  // Chart 3: Cumulative net cashflow
  new Chart(document.getElementById('cashflowChart'), {
    type: 'line',
    data: {
      labels: TIMELINE.map(t => t.month),
      datasets: [{
        label: '누적 순현금흐름',
        data: TIMELINE.map(t => t.net_cashflow),
        borderColor: '#1a56db',
        backgroundColor: 'rgba(26,86,219,0.1)',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#0f172a' } }, tooltip: { callbacks: { label: ctx => fmt(ctx.raw) + '원' } } },
      scales: {
        x: { ticks: { color: '#4a5568', maxRotation: 45 }, grid: { display: false } },
        y: { ticks: { callback: v => fmt(v), color: '#4a5568' }, grid: { color: 'rgba(0,0,0,0.06)' } }
      }
    }
  });

  // Chart 4: Monthly dividends (only months with dividends > 0)
  const divMonths = TIMELINE.filter(t => t.dividends > 0);
  new Chart(document.getElementById('dividendChart'), {
    type: 'bar',
    data: {
      labels: divMonths.map(t => t.month),
      datasets: [{
        label: '배당금',
        data: divMonths.map(t => t.dividends),
        backgroundColor: 'rgba(26,86,219,0.6)',
        borderColor: '#1a56db',
        borderWidth: 1,
        borderRadius: 4,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => fmt(ctx.raw) + '원' } } },
      scales: {
        x: { ticks: { color: '#4a5568', maxRotation: 45 }, grid: { display: false } },
        y: { ticks: { callback: v => fmt(v), color: '#4a5568' }, grid: { color: 'rgba(0,0,0,0.06)' } }
      }
    }
  });
}

// ===== PERIOD ANALYSIS =====
let periodAnalysisInited = false;
let tradeViewMode = 'all';

function setTradeView(mode, btn) {
  tradeViewMode = mode;
  document.querySelectorAll('#tradeViewToggle .filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderPeriodAnalysis();
}

function toggleStockTrades(idx) {
  const el = document.getElementById('stockTrades_' + idx);
  const arrow = document.getElementById('stockArrow_' + idx);
  if (!el) return;
  const open = el.style.display !== 'none';
  el.style.display = open ? 'none' : 'block';
  if (arrow) arrow.textContent = open ? '▾' : '▴';
}
function initPeriodAnalysis() {
  if (periodAnalysisInited) return;
  periodAnalysisInited = true;
  // Default: 이번 달
  const btn = document.querySelector('#periodPresets .filter-btn:nth-child(2)');
  if (btn) setPeriodPreset('1m', btn);
}

function setPeriodPreset(preset, btn) {
  document.querySelectorAll('#periodPresets .filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const today = new Date();
  const fmt = d => d.toISOString().slice(0,10);
  let start;
  if (preset === '1w') {
    start = new Date(today); start.setDate(today.getDate() - 7);
  } else if (preset === '1m') {
    start = new Date(today); start.setMonth(today.getMonth() - 1);
  } else if (preset === '3m') {
    start = new Date(today); start.setMonth(today.getMonth() - 3);
  } else if (preset === 'ytd') {
    start = new Date(today.getFullYear(), 0, 1);
  } else if (preset === '1y') {
    start = new Date(today); start.setFullYear(today.getFullYear() - 1);
  } else {
    start = new Date('2000-01-01');
  }
  document.getElementById('periodStart').value = fmt(start);
  document.getElementById('periodEnd').value = fmt(today);
  renderPeriodAnalysis();
}

function renderPeriodAnalysis() {
  const startVal = document.getElementById('periodStart').value;
  const endVal   = document.getElementById('periodEnd').value;
  if (!startVal || !endVal) return;

  // Filter transactions in range
  const inRange = TXS.filter(tx => tx.d >= startVal && tx.d <= endVal);

  // Compute holdings at start and end from ALL transactions
  function holdingsAt(date) {
    const h = {};
    for (const tx of TXS) {
      if (tx.d > date) break;
      if (!tx.s) continue;
      if (!h[tx.s]) h[tx.s] = 0;
      if (tx.t === 'buy')  h[tx.s] += tx.q;
      if (tx.t === 'sell') h[tx.s] -= tx.q;
    }
    return h;
  }

  // Summary stats
  let buys=0, sells=0, divs=0, fees=0;
  const typeMap = {'buy':'매수','sell':'매도','dividend':'배당','fee':'수수료','tax':'세금','lending_fee':'대주료'};
  for (const tx of inRange) {
    if (tx.t === 'buy')          buys += tx.a;
    else if (tx.t === 'sell')    sells += tx.a;
    else if (tx.t === 'dividend') divs += tx.a;
    else if (tx.t === 'fee' || tx.t === 'tax' || tx.t === 'lending_fee') fees += tx.a;
  }
  const netCash = sells + divs - buys - fees;

  // KPIs
  const fmt = v => (v < 0 ? '-' : '') + Math.abs(v).toLocaleString('ko-KR') + '원';
  const kpiDefs = [
    { label:'매수', value: buys, cls:'negative' },
    { label:'매도', value: sells, cls:'positive' },
    { label:'배당', value: divs, cls:'positive' },
    { label:'순현금흐름', value: netCash, cls: netCash>=0?'positive':'negative' },
    { label:'거래건수', value: inRange.length, raw:true },
  ];
  document.getElementById('periodKpis').innerHTML = kpiDefs.map(k => `
    <div style="background:var(--bg2); border:1px solid var(--border); border-radius:10px; padding:14px 16px;">
      <div style="font-size:0.72rem; color:var(--text-muted); font-weight:600; text-transform:uppercase; letter-spacing:.05em; margin-bottom:6px;">${k.label}</div>
      <div style="font-size:1.15rem; font-weight:700;" class="${k.raw?'':k.cls}">${k.raw ? k.value.toLocaleString('ko-KR') : fmt(k.value)}</div>
    </div>`).join('');

  // Portfolio diff: holdings before start vs at end
  // Get day before start
  const startDateAdj = new Date(startVal);
  startDateAdj.setDate(startDateAdj.getDate() - 1);
  const startAdj = startDateAdj.toISOString().slice(0,10);
  const hStart = holdingsAt(startAdj);
  const hEnd   = holdingsAt(endVal);

  const allStocks = new Set([...Object.keys(hStart), ...Object.keys(hEnd)]);
  const added=[], removed=[], changed=[];
  for (const s of allStocks) {
    const qs = hStart[s] || 0;
    const qe = hEnd[s]   || 0;
    if (qs <= 0 && qe > 0)      added.push({s, qs, qe});
    else if (qs > 0 && qe <= 0) removed.push({s, qs, qe});
    else if (Math.abs(qe - qs) > 0.001) changed.push({s, qs, qe, d: qe - qs});
  }

  let diffHtml = '';
  if (added.length) {
    diffHtml += `<div style="font-size:0.75rem; font-weight:600; color:var(--positive); margin-bottom:4px;">신규 편입 +${added.length}</div>`;
    diffHtml += added.map(x => `<div style="font-size:0.82rem; padding:3px 0; border-bottom:1px solid var(--border); display:flex; justify-content:space-between;"><span>${x.s}</span><span style="color:var(--positive)">${x.qe.toLocaleString('ko-KR')}주</span></div>`).join('');
  }
  if (removed.length) {
    diffHtml += `<div style="font-size:0.75rem; font-weight:600; color:var(--negative); margin-top:8px; margin-bottom:4px;">청산 -${removed.length}</div>`;
    diffHtml += removed.map(x => `<div style="font-size:0.82rem; padding:3px 0; border-bottom:1px solid var(--border); display:flex; justify-content:space-between;"><span>${x.s}</span><span style="color:var(--negative)">${x.qs.toLocaleString('ko-KR')}주→0</span></div>`).join('');
  }
  if (changed.length) {
    diffHtml += `<div style="font-size:0.75rem; font-weight:600; color:var(--text-dim); margin-top:8px; margin-bottom:4px;">수량 변화 ${changed.length}종목</div>`;
    diffHtml += changed.map(x => `<div style="font-size:0.82rem; padding:3px 0; border-bottom:1px solid var(--border); display:flex; justify-content:space-between;"><span>${x.s}</span><span style="${x.d>0?'color:var(--positive)':'color:var(--negative)'}">${x.d>0?'+':''}${x.d.toLocaleString('ko-KR')}주</span></div>`).join('');
  }
  if (!diffHtml) diffHtml = '<div style="font-size:0.82rem; color:var(--text-muted);">변동 없음</div>';
  document.getElementById('periodPortfolioDiff').innerHTML = diffHtml;

  // Trade list
  const sorted = [...inRange].sort((a,b) => b.d.localeCompare(a.d));
  const typeColor = {'buy':'var(--negative)','sell':'var(--positive)','dividend':'#1a56db','fee':'var(--text-muted)','tax':'var(--text-muted)','lending_fee':'var(--text-muted)'};
  let tradeHtml = '';
  if (sorted.length === 0) {
    tradeHtml = '<div style="font-size:0.82rem; color:var(--text-muted);">해당 기간 거래 없음</div>';
  } else if (tradeViewMode === 'stock') {
    // ── 종목별 grouped view ──
    // Build stock map ordered by total traded amount desc
    const stockOrder = [];
    const stockMap = {};
    for (const tx of sorted) {
      const key = tx.s || '(기타)';
      if (!stockMap[key]) { stockMap[key] = { buys:0, sells:0, divs:0, fees:0, count:0, trades:[] }; stockOrder.push(key); }
      stockMap[key].trades.push(tx);
      stockMap[key].count++;
      if (tx.t === 'buy')      stockMap[key].buys  += tx.a;
      else if (tx.t === 'sell')     stockMap[key].sells += tx.a;
      else if (tx.t === 'dividend') stockMap[key].divs  += tx.a;
      else                          stockMap[key].fees  += tx.a;
    }
    // Sort by total volume (buys+sells) desc
    stockOrder.sort((a, b) => (stockMap[b].buys + stockMap[b].sells) - (stockMap[a].buys + stockMap[a].sells));

    tradeHtml = stockOrder.map((stock, idx) => {
      const d = stockMap[stock];
      const net = d.sells + d.divs - d.buys - d.fees;
      const chips = [
        d.buys  ? `<span style="color:var(--negative); font-size:0.78rem;">매수 ${fmt(d.buys)}</span>`  : '',
        d.sells ? `<span style="color:var(--positive); font-size:0.78rem;">매도 ${fmt(d.sells)}</span>` : '',
        d.divs  ? `<span style="color:#1a56db;        font-size:0.78rem;">배당 ${fmt(d.divs)}</span>`  : '',
        d.fees  ? `<span style="color:var(--text-muted); font-size:0.78rem;">비용 ${fmt(d.fees)}</span>` : '',
      ].filter(Boolean).join('');
      const netColor = net >= 0 ? 'var(--positive)' : 'var(--negative)';
      const rows = d.trades.map(tx => `
        <tr style="border-top:1px solid var(--border);">
          <td style="padding:6px 14px; color:var(--text-dim); white-space:nowrap; font-size:0.8rem;">${tx.d}</td>
          <td style="padding:6px 10px;"><span style="color:${typeColor[tx.t]||'var(--text)'}; font-weight:600; font-size:0.76rem;">${typeMap[tx.t]||tx.t}</span></td>
          <td style="padding:6px 10px; text-align:right; font-feature-settings:'tnum'; font-size:0.8rem;">${tx.a ? tx.a.toLocaleString('ko-KR') : '─'}</td>
          <td style="padding:6px 10px; text-align:right; font-feature-settings:'tnum'; font-size:0.8rem; color:var(--text-dim);">${tx.q ? tx.q.toLocaleString('ko-KR')+'주' : '─'}</td>
          <td style="padding:6px 14px; font-size:0.75rem; color:var(--text-muted);">${tx.acc || '─'}</td>
        </tr>`).join('');
      return `
      <div style="border:1px solid var(--border); border-radius:8px; margin-bottom:6px; overflow:hidden;">
        <div onclick="toggleStockTrades(${idx})" style="display:flex; align-items:center; gap:10px; padding:10px 14px; cursor:pointer; background:var(--bg2); user-select:none;">
          <span style="font-weight:700; font-size:0.88rem; min-width:80px;">${stock}</span>
          <span style="display:flex; gap:10px; flex:1; flex-wrap:wrap;">${chips}</span>
          <span style="font-size:0.8rem; font-weight:600; color:${netColor}; white-space:nowrap;">${net>=0?'+':''}${fmt(net)}</span>
          <span style="font-size:0.72rem; color:var(--text-muted); white-space:nowrap;">${d.count}건</span>
          <span id="stockArrow_${idx}" style="color:var(--text-muted); font-size:0.8rem;">▾</span>
        </div>
        <div id="stockTrades_${idx}" style="display:none;">
          <table style="width:100%; border-collapse:collapse;"><tbody>${rows}</tbody></table>
        </div>
      </div>`;
    }).join('');
  } else {
    // ── 전체 flat view ──
    tradeHtml = `<table style="width:100%; border-collapse:collapse; font-size:0.82rem;">
      <thead><tr style="border-bottom:2px solid var(--border);">
        <th style="text-align:left; padding:6px 10px; color:var(--text-muted); font-size:0.72rem; text-transform:uppercase; font-weight:600;">날짜</th>
        <th style="text-align:left; padding:6px 10px; color:var(--text-muted); font-size:0.72rem; text-transform:uppercase; font-weight:600;">종목</th>
        <th style="text-align:left; padding:6px 10px; color:var(--text-muted); font-size:0.72rem; text-transform:uppercase; font-weight:600;">유형</th>
        <th style="text-align:right; padding:6px 10px; color:var(--text-muted); font-size:0.72rem; text-transform:uppercase; font-weight:600;">금액</th>
        <th style="text-align:right; padding:6px 10px; color:var(--text-muted); font-size:0.72rem; text-transform:uppercase; font-weight:600;">수량</th>
        <th style="text-align:left; padding:6px 10px; color:var(--text-muted); font-size:0.72rem; text-transform:uppercase; font-weight:600;">계좌</th>
      </tr></thead><tbody>`;
    tradeHtml += sorted.map(tx => `
      <tr style="border-bottom:1px solid var(--border);">
        <td style="padding:7px 10px; color:var(--text-dim);">${tx.d}</td>
        <td style="padding:7px 10px; font-weight:600;">${tx.s || '─'}</td>
        <td style="padding:7px 10px;"><span style="color:${typeColor[tx.t]||'var(--text)'}; font-weight:600; font-size:0.78rem;">${typeMap[tx.t]||tx.t}</span></td>
        <td style="padding:7px 10px; text-align:right; font-feature-settings:'tnum';">${tx.a ? tx.a.toLocaleString('ko-KR') : '─'}</td>
        <td style="padding:7px 10px; text-align:right; font-feature-settings:'tnum';">${tx.q ? tx.q.toLocaleString('ko-KR') : '─'}</td>
        <td style="padding:7px 10px; font-size:0.77rem; color:var(--text-muted);">${tx.acc || '─'}</td>
      </tr>`).join('');
    tradeHtml += '</tbody></table>';
  }
  document.getElementById('periodTrades').innerHTML = tradeHtml;
}

// ===== BRIEFING TAB =====
const BRIEFING_PERIODS = [
  { key: 'daily',    label: '오늘' },
  { key: 'weekly',   label: '1주' },
  { key: 'biweekly', label: '2주' },
  { key: 'monthly',  label: '1달' },
];
let briefingActivePeriod = 'daily';
let rawPostsVisible = false;
let briefingRawInited = false;
let briefingActiveFilter = 'all';

function initBriefing() {
  // Build period selector
  const pf = document.getElementById('briefingPeriodFilter');
  pf.innerHTML = BRIEFING_PERIODS.map(p =>
    `<button class="filter-btn ${p.key === briefingActivePeriod ? 'active' : ''}"
       onclick="setBriefingPeriod('${p.key}', this)">${p.label}</button>`
  ).join('');

  // Updated-at
  if (BRIEFING_SUMMARY.updated_at) {
    const dt = new Date(BRIEFING_SUMMARY.updated_at);
    document.getElementById('briefingUpdatedAt').textContent =
      '업데이트 ' + dt.toLocaleDateString('ko-KR', {month:'numeric',day:'numeric'}) + ' ' +
      dt.toLocaleTimeString('ko-KR', {hour:'2-digit',minute:'2-digit'});
  }

  renderBriefingSummary();
}

function setBriefingPeriod(key, btn) {
  briefingActivePeriod = key;
  document.querySelectorAll('#briefingPeriodFilter .filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderBriefingSummary();
}

function sentimentBadge(s) {
  const map = { positive: ['#0a7c59','rgba(10,124,89,0.08)','▲ 긍정'], negative: ['#c81e1e','rgba(200,30,30,0.08)','▼ 부정'], neutral: ['#4a5568','rgba(74,85,104,0.08)','— 중립'] };
  const [color, bg, label] = map[s] || map.neutral;
  return `<span style="background:${bg}; color:${color}; border:1px solid ${color}40; border-radius:4px; padding:2px 8px; font-size:0.72rem; font-weight:600;">${label}</span>`;
}

function renderBriefingSummary() {
  const data = BRIEFING_SUMMARY[briefingActivePeriod];
  const el = document.getElementById('briefingSummarySection');
  if (!data) {
    el.innerHTML = '<p style="color:var(--text-dim)">해당 기간 요약 데이터가 없습니다.</p>';
    return;
  }

  const sentimentColor = { positive:'#0a7c59', negative:'#c81e1e', neutral:'#6b7280' };
  const sentimentLabel = { positive:'▲ 긍정', negative:'▼ 부정', neutral:'─ 중립' };
  const sentimentBg   = { positive:'rgba(10,124,89,0.08)', negative:'rgba(200,30,30,0.08)', neutral:'rgba(107,114,128,0.08)' };

  let html = '';

  // ── 시장 개요 ──────────────────────────────────────────────
  if (data.market_summary) {
    html += `
    <div class="card" style="margin-bottom:20px; border-left:3px solid var(--accent); padding:20px 24px;">
      <div style="display:flex; align-items:center; gap:10px; margin-bottom:10px;">
        <span style="font-size:0.95rem; font-weight:700; color:var(--text);">시장 개요</span>
        <span style="font-size:0.78rem; color:var(--text-muted); background:var(--bg); border:1px solid var(--border); border-radius:20px; padding:2px 10px;">${data.period || ''}</span>
      </div>
      <p style="font-size:0.9rem; line-height:1.8; color:var(--text); margin:0;">${data.market_summary}</p>
    </div>`;
  }

  // ── 핵심 테마 ──────────────────────────────────────────────
  const themes = data.themes || [];
  if (themes.length > 0) {
    html += `
    <div style="display:flex; align-items:center; gap:8px; margin-bottom:12px;">
      <span style="font-size:0.75rem; font-weight:700; color:var(--text-muted); letter-spacing:.08em; text-transform:uppercase;">핵심 테마</span>
      <span style="font-size:0.75rem; font-weight:600; color:var(--text-muted);">· ${themes.length}개</span>
    </div>
    <div style="display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-bottom:24px;">`;

    themes.forEach(t => {
      const sc = sentimentColor[t.sentiment] || sentimentColor.neutral;
      const sl = sentimentLabel[t.sentiment] || sentimentLabel.neutral;
      const sb = sentimentBg[t.sentiment]   || sentimentBg.neutral;
      const channels = (t.mentioned_in || []).map(c =>
        `<span style="display:inline-flex; align-items:center; background:rgba(26,86,219,0.07); color:#1a56db; border-radius:4px; padding:2px 8px; font-size:0.71rem; font-weight:500;">${c}</span>`
      ).join('');

      html += `
      <div class="card" style="margin-bottom:0; padding:18px 20px; display:flex; flex-direction:column; gap:0;">
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;">
          <span style="font-size:0.72rem; font-weight:700; letter-spacing:.04em; color:${sc}; background:${sb}; border-radius:4px; padding:3px 9px;">${sl}</span>
        </div>
        <div style="font-size:0.92rem; font-weight:700; line-height:1.4; color:var(--text); margin-bottom:10px;">${t.title}</div>
        <p style="font-size:0.83rem; line-height:1.7; color:var(--text-dim); margin:0 0 14px; flex:1;">${t.summary}</p>
        <div style="display:flex; flex-wrap:wrap; gap:4px; border-top:1px solid var(--border); padding-top:10px;">${channels}</div>
      </div>`;
    });
    html += `</div>`;
  }

  // ── 주목 종목 ──────────────────────────────────────────────
  const stocks = data.stocks || [];
  if (stocks.length > 0) {
    const stockItems = stocks.map(s => {
      const pct = s.price_change_pct;
      const pctHtml = pct != null
        ? `<span style="font-size:0.75rem; font-weight:600; color:${pct>=0?'#0a7c59':'#c81e1e'}">${pct>=0?'+':''}${pct.toFixed(1)}%</span>`
        : '';
      const chs = (s.channels || []).join(' · ');
      return `
      <div style="display:flex; flex-direction:column; padding:12px 16px; background:var(--bg2); border:1px solid var(--border); border-radius:10px; min-width:150px; gap:4px;">
        <div style="display:flex; align-items:center; justify-content:space-between; gap:8px;">
          <span style="font-size:0.9rem; font-weight:700; color:var(--text);">${s.name}</span>
          ${pctHtml}
        </div>
        <div style="font-size:0.75rem; color:var(--text-muted);">언급 <b style="color:var(--accent);">${s.mention_count}</b>회</div>
        <div style="font-size:0.71rem; color:var(--text-muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${chs}">${chs}</div>
      </div>`;
    }).join('');

    html += `
    <div style="display:flex; align-items:center; gap:8px; margin-bottom:12px;">
      <span style="font-size:0.75rem; font-weight:700; color:var(--text-muted); letter-spacing:.08em; text-transform:uppercase;">주목 종목</span>
    </div>
    <div style="display:flex; flex-wrap:wrap; gap:10px; margin-bottom:8px;">${stockItems}</div>`;
  }

  el.innerHTML = html;
}

function toggleRawPosts() {
  rawPostsVisible = !rawPostsVisible;
  const sec = document.getElementById('rawPostsSection');
  const btn = document.getElementById('rawPostsToggleBtn');
  sec.style.display = rawPostsVisible ? 'block' : 'none';
  btn.textContent = (rawPostsVisible ? '▼' : '▶') + ' 채널별 원문 보기';
  if (rawPostsVisible && !briefingRawInited) {
    const sel = document.getElementById('briefingDateSelect');
    const dates = Object.keys(BRIEFING).sort().reverse();
    sel.innerHTML = dates.map(d => `<option value="${d}">${d}</option>`).join('');
    briefingRawInited = true;
    renderRawPosts();
  }
}

function setBriefingFilter(name, btn) {
  briefingActiveFilter = name;
  document.querySelectorAll('#briefingSourceFilter .filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderRawPosts();
}

function renderRawPosts() {
  const sel = document.getElementById('briefingDateSelect');
  const dateKey = sel.value;
  const day = BRIEFING[dateKey];
  if (!day) {
    document.getElementById('briefingContent').innerHTML = '<p style="color:var(--text-dim)">브리핑 데이터가 없습니다.</p>';
    return;
  }

  const sources = day.sources || [];

  // Build source filter buttons
  const filterDiv = document.getElementById('briefingSourceFilter');
  const allActive = briefingActiveFilter === 'all' ? 'active' : '';
  let filterHtml = `<button class="filter-btn ${allActive}" onclick="setBriefingFilter('all', this)">전체</button>`;
  sources.forEach(s => {
    const active = briefingActiveFilter === (s.id || s.name) ? 'active' : '';
    filterHtml += `<button class="filter-btn ${active}" onclick="setBriefingFilter('${s.id || s.name}', this)">${s.name}</button>`;
  });
  filterDiv.innerHTML = filterHtml;

  const totalPosts = sources.reduce((sum, s) => sum + (s.posts || []).length, 0);
  document.getElementById('briefingSourceCount').textContent = `${sources.length}개 채널 · ${totalPosts}개 포스트`;

  const filtered = briefingActiveFilter === 'all' ? sources : sources.filter(s => (s.id || s.name) === briefingActiveFilter);

  let html = '';
  filtered.forEach(src => {
    const posts = src.posts || [];
    if (posts.length === 0) return;
    const channelUrl = src.channel_url || src.url || '#';
    html += `<div class="card">`;
    html += `<div class="card-title"><a href="${channelUrl}" target="_blank" rel="noopener" style="color:var(--accent); text-decoration:none;">${src.name}</a>`;
    html += `<span style="font-size:0.75rem; color:var(--text-muted); font-weight:400; margin-left:8px;">${src.category || ''} · ${posts.length}개</span></div>`;
    posts.forEach(p => {
      const time = p.time ? `<span style="color:var(--text-muted); font-size:0.75rem; min-width:40px;">${p.time}</span>` : '';
      const text = (p.text || '').replace(/\\n/g, '<br>');
      let linkHtml = '';
      if (p.post_url) linkHtml += `<a href="${p.post_url}" target="_blank" rel="noopener" style="color:var(--accent); font-size:0.75rem; text-decoration:none; margin-right:8px;">원문</a>`;
      (p.links || []).forEach(lnk => {
        if (lnk === p.post_url) return;
        if (lnk.includes('t.me/' + (src.id || '---'))) return;
        const domain = lnk.replace(/https?:\\/\\/([^/]+).*/, '$1').replace('www.', '');
        linkHtml += `<a href="${lnk}" target="_blank" rel="noopener" style="color:var(--text-dim); font-size:0.75rem; text-decoration:none; margin-right:8px;">${domain}</a>`;
      });
      html += `<div style="display:flex; gap:10px; padding:10px 0; border-bottom:1px solid var(--border); align-items:flex-start;">`;
      html += time;
      html += `<div style="flex:1; min-width:0;"><div style="font-size:0.85rem; line-height:1.6; word-break:break-word;">${text}</div>`;
      if (linkHtml) html += `<div style="margin-top:6px;">${linkHtml}</div>`;
      html += `</div></div>`;
    });
    html += `</div>`;
  });

  document.getElementById('briefingContent').innerHTML = html || '<p style="color:var(--text-dim)">해당 날짜에 포스트가 없습니다.</p>';
}

// ===== ACCOUNT SUMMARY (Dashboard) =====
function renderAccountSummary() {
  const grid = document.getElementById('acctSummaryGrid');
  if (!grid) return;

  // 전체 평가금액 (비중 bar 계산용)
  const totalMv = Object.values(ACCOUNTS).reduce((s, a) => s + (a.total_market_value || 0), 0);

  grid.innerHTML = Object.entries(ACCOUNTS).map(([name, acct]) => {
    const mv = acct.total_market_value || 0;
    const cost = acct.total_cost || 0;
    const upnl = mv - cost;
    const upnlPct = cost > 0 ? (upnl / cost * 100) : 0;
    const netPnl = acct.realized_pnl + acct.dividends;
    const hasLoan = acct.loan_balance > 0;
    const upnlCls = pnlCls(upnl);
    const mvWeight = totalMv > 0 ? (mv / totalMv * 100) : 0;

    // 계좌명 정리: "01.NH01"→"NH01", "나무01"→broker=나무/id=01
    const cleanName = name.replace(/^\d+\./, '');
    let brokerLabel, acctId, brokerClass;
    if (cleanName.startsWith('NH') || cleanName.startsWith('nh')) {
      brokerLabel = 'NH'; acctId = cleanName.slice(2); brokerClass = 'broker-nh';
    } else if (cleanName.startsWith('\uB098\uBB34')) {  // 나무
      brokerLabel = '\uB098\uBB34'; acctId = cleanName.slice(2); brokerClass = 'broker-namu';
    } else if (cleanName.startsWith('\uD1A0\uC2A4')) {  // 토스
      brokerLabel = '\uD1A0\uC2A4'; acctId = ''; brokerClass = 'broker-toss';
    } else {
      brokerLabel = cleanName; acctId = ''; brokerClass = 'broker-namu';
    }

    // 손익 bar 너비 (최대 100%, upnl % 기준 ±30% → 0~100%)
    const barPct = Math.min(Math.abs(upnlPct) / 30 * 100, 100);
    const barColor = upnl >= 0 ? 'var(--positive)' : 'var(--negative)';

    const loanBadge = hasLoan
      ? `<span style="font-size:0.62rem;font-weight:600;color:var(--negative)">대출 ${fmt(acct.loan_balance)}</span>`
      : `<span class="acct-acct-id">${acct.num_holdings}종목</span>`;

    const mvHtml = mv > 0
      ? `<div class="acct-mv">${fmt(mv)}</div>
         <div class="acct-pnl-row">
           <span class="acct-upnl ${upnlCls}">${upnl >= 0 ? '+' : ''}${fmt(upnl)}</span>
           <span class="acct-upnl-pct ${upnlCls}">${upnlPct >= 0 ? '+' : ''}${upnlPct.toFixed(1)}%</span>
           <div class="acct-upnl-bar"><div class="acct-upnl-bar-fill" style="width:${barPct}%;background:${barColor};"></div></div>
         </div>`
      : `<div class="acct-mv empty">미보유</div>
         <div class="acct-pnl-row"></div>`;

    return `<div class="acct-card ${brokerClass}${hasLoan ? ' has-loan' : ''}" onclick="switchToPortfolioAccount('${name}')">
      <div class="acct-header">
        <span class="acct-broker-tag">${brokerLabel}${acctId ? ' ' + acctId : ''}</span>
        ${loanBadge}
      </div>
      ${mvHtml}
      <div class="acct-stats">
        <div class="acct-stat">
          <span class="acct-stat-label">실현손익</span>
          <span class="acct-stat-val ${pnlCls(netPnl)}">${netPnl >= 0 ? '+' : ''}${fmt(netPnl)}</span>
        </div>
        <div class="acct-stat" style="text-align:right">
          <span class="acct-stat-label">IRR</span>
          <span class="acct-stat-val ${pnlCls(acct.irr)}">${acct.irr != null ? (acct.irr >= 0 ? '+' : '') + acct.irr.toFixed(1) + '%' : 'N/A'}</span>
        </div>
        <div class="acct-stat">
          <span class="acct-stat-label">비중</span>
          <span class="acct-stat-val">${mvWeight.toFixed(1)}%</span>
        </div>
        <div class="acct-stat" style="text-align:right">
          <span class="acct-stat-label">거래</span>
          <span class="acct-stat-val">${acct.num_trades}건</span>
        </div>
      </div>
    </div>`;
  }).join('');
}

function switchToPortfolioAccount(accName) {
  // 포트폴리오 탭 → 계좌별 → 해당 계좌로 이동
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-portfolio').classList.add('active');
  document.querySelector('.tab[onclick*="portfolio"]').classList.add('active');
  // 서브탭 → 계좌별
  document.querySelectorAll('.sub-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.subtab-content').forEach(t => t.classList.remove('active'));
  document.querySelector('.sub-tab[onclick*="byAccount"]').classList.add('active');
  document.getElementById('subtab-byAccount').classList.add('active');
  initAccounts();
  // 해당 계좌 선택
  const btns = document.querySelectorAll('#accountSelector .account-btn');
  btns.forEach(btn => {
    btn.classList.remove('active');
    if (btn.textContent.startsWith(accName + ' ')) {
      btn.classList.add('active');
      renderAccount(accName);
    }
  });
  renderAcctTreemap();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// Init
initOverallCharts();
renderStockTable();
renderTreemap();
renderAccountSummary();
// Re-render treemaps on resize
window.addEventListener('resize', () => {
  clearTimeout(window._tmResize);
  window._tmResize = setTimeout(() => {
    renderTreemap();
    if (currentAccount && document.getElementById('acctTreemapContainer')) {
      renderTreemapInContainer('acctTreemapContainer', ACCOUNTS[currentAccount].treemap, 'acctTmTooltip');
    }
  }, 200);
});
</script>
</body>
</html>"""

with open("/Users/r/Documents/Claude/stock-dashboard/index.html", "w", encoding="utf-8") as f:
    f.write(html)
print("Dashboard saved to index.html")
print(f"Overall: invested={fmt_num(overall_invested)}, returned={fmt_num(overall_returned)}, pnl={fmt_num(overall_net_pnl)}, IRR={fmt_pct(overall_irr) if overall_irr else 'N/A'}")
