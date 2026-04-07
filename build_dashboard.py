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
prices_file = "/Users/r/Documents/Claude/stock-dashboard/prices.json"
current_prices = {}
if os.path.exists(prices_file):
    with open(prices_file, encoding="utf-8") as f:
        raw_prices = json.load(f)
        current_prices = {k: v["price"] for k, v in raw_prices.items()}

# Filter to KRW transactions: buy/sell/dividend with amount > 0, plus all transfers
txs = [tx for tx in all_txs if tx["currency"] == "KRW" and (tx["amount"] > 0 or tx["type"] in ("transfer_in", "transfer_out"))]

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

# ===== Compute per account, per stock =====
account_stock_data = defaultdict(lambda: defaultdict(list))
for tx in txs:
    if tx["type"] in ["buy", "sell", "dividend", "transfer_in", "transfer_out"]:
        account_stock_data[tx["account"]][tx["stock"]].append(tx)

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

    account_cashflows.sort(key=lambda x: x[0])
    # Add current market value of holdings for IRR
    irr_account_cf = list(account_cashflows)
    for stock, h in holdings.items():
        cp = current_prices.get(stock, h["avg_price"])
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
        cp = current_prices.get(stock, m["avg_buy_price"])
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
    cp = current_prices.get(stock, h["avg_price"])
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

months_sorted = sorted(monthly_data.keys())
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
    })


def fmt_num(n):
    if n is None:
        return "N/A"
    if abs(n) >= 1e8:
        return f"{n/1e8:.1f}억"
    if abs(n) >= 1e4:
        return f"{n/1e4:,.0f}만"
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
        cp = current_prices.get(s["name"], s["avg_price"] if s["current_qty"] > 0 else 0)
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
        cp = current_prices.get(stock, h["avg_price"])
        mv = h["qty"] * cp
        acct_total_mv += mv
    for stock, h in holdings.items():
        cp = current_prices.get(stock, h["avg_price"])
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
        "irr": round(data["irr"] * 100, 1) if data["irr"] else None,
        "holdings": data["holdings"],
        "num_trades": data["num_trades"],
        "treemap": account_treemap,
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
    cp = current_prices.get(s["name"], s["avg_price"] if s["current_qty"] > 0 else 0)
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
    h["qty"] * current_prices.get(stock, h["avg_price"])
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
    "holdings": overall_holdings,
    "num_stocks": len(stock_summaries),
    "num_accounts": len(account_summaries),
    "total_market_value": total_market_value,
    "total_unrealized": total_unrealized,
}

# Build treemap data for holdings
treemap_data = []
for stock, h in overall_holdings.items():
    cp = current_prices.get(stock, h["avg_price"])
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
  --bg: #0f1117;
  --bg2: #141620;
  --card: #1a1d29;
  --card-hover: #1f2233;
  --border: #2a2d3a;
  --border-light: #353849;
  --text: #e1e4eb;
  --text-dim: #8b8fa3;
  --text-muted: #5d6177;
  --accent: #6366f1;
  --accent-dim: rgba(99,102,241,0.15);
  --positive: #22c55e;
  --positive-dim: rgba(34,197,94,0.12);
  --negative: #ef4444;
  --negative-dim: rgba(239,68,68,0.12);
  --warn: #f59e0b;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans KR', sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; -webkit-font-smoothing: antialiased; }}
.container {{ max-width: 1440px; margin: 0 auto; padding: 20px; }}
h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 8px; letter-spacing: -0.02em; }}
.subtitle {{ color: var(--text-dim); margin-bottom: 24px; font-size: 0.88rem; }}
.tabs {{ display: flex; gap: 4px; margin-bottom: 24px; background: var(--card); border-radius: 12px; padding: 4px; border: 1px solid var(--border); width: fit-content; }}
.tab {{ padding: 10px 24px; border-radius: 8px; cursor: pointer; font-size: 0.9rem; font-weight: 600; color: var(--text-dim); transition: all 0.25s ease; border: none; background: none; }}
.tab:hover {{ color: var(--text); background: rgba(255,255,255,0.03); }}
.tab.active {{ background: var(--accent); color: white; box-shadow: 0 2px 8px rgba(99,102,241,0.3); }}
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
.kpi-row.secondary {{ grid-template-columns: repeat(4, 1fr); }}
.kpi {{ background: var(--card); border-radius: 12px; padding: 18px 20px; border: 1px solid var(--border); transition: border-color 0.2s, transform 0.2s; }}
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
.chart-container {{ position: relative; height: 350px; }}

/* === Treemap === */
.treemap-container {{ position: relative; width: 100%; height: 420px; border-radius: 8px; overflow: hidden; }}
.treemap-container.acct-treemap {{ height: 320px; }}
.treemap-cell {{ position: absolute; overflow: hidden; display: flex; flex-direction: column; align-items: center; justify-content: center; cursor: default; transition: filter 0.15s; border: 1px solid rgba(0,0,0,0.3); }}
.treemap-cell:hover {{ filter: brightness(1.15); z-index: 2; }}
.treemap-cell .name {{ font-weight: 700; font-size: 0.82rem; color: #fff; text-shadow: 0 1px 3px rgba(0,0,0,0.6); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 95%; text-align: center; }}
.treemap-cell .pct {{ font-weight: 600; font-size: 0.75rem; color: rgba(255,255,255,0.9); text-shadow: 0 1px 3px rgba(0,0,0,0.6); }}
.treemap-cell .val {{ font-size: 0.65rem; color: rgba(255,255,255,0.7); text-shadow: 0 1px 3px rgba(0,0,0,0.6); margin-top: 1px; }}
.treemap-cell.small .name {{ font-size: 0.7rem; }}
.treemap-cell.small .pct {{ font-size: 0.65rem; }}
.treemap-cell.small .val {{ display: none; }}
.treemap-cell.tiny .name {{ font-size: 0.6rem; }}
.treemap-cell.tiny .pct {{ display: none; }}
.treemap-cell.tiny .val {{ display: none; }}

/* === Tables === */
table {{ width: 100%; border-collapse: collapse; font-size: 0.84rem; }}
thead {{ position: sticky; top: 0; z-index: 5; }}
th {{ text-align: left; padding: 10px 12px; background: var(--card); border-bottom: 2px solid var(--border); color: var(--text-dim); font-weight: 600; font-size: 0.73rem; text-transform: uppercase; letter-spacing: 0.5px; cursor: pointer; user-select: none; white-space: nowrap; backdrop-filter: blur(8px); }}
th:hover {{ color: var(--text); }}
th.sort-asc::after {{ content: ' \\25B2'; font-size: 0.6rem; }}
th.sort-desc::after {{ content: ' \\25BC'; font-size: 0.6rem; }}
td {{ padding: 9px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; font-feature-settings: 'tnum'; font-variant-numeric: tabular-nums; }}
tr:nth-child(even) td {{ background: rgba(255,255,255,0.015); }}
tr:hover td {{ background: rgba(99, 102, 241, 0.06); }}
.text-right {{ text-align: right; }}
.text-center {{ text-align: center; }}
.mono {{ font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace; font-size: 0.82rem; }}

/* === Holdings grid === */
.holdings-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; }}
.holding-card {{ background: rgba(99,102,241,0.08); border-radius: 10px; padding: 14px; border: 1px solid rgba(99,102,241,0.15); transition: border-color 0.2s; }}
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
.search-box:focus {{ outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(99,102,241,0.1); }}
.toolbar {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }}
.filter-group {{ display: flex; gap: 4px; }}
.filter-btn {{ padding: 5px 14px; border-radius: 16px; cursor: pointer; font-size: 0.8rem; font-weight: 500; border: 1px solid var(--border); background: var(--card); color: var(--text-dim); transition: all 0.2s; }}
.filter-btn:hover {{ border-color: var(--accent); color: var(--text); }}
.filter-btn.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
.result-count {{ font-size: 0.8rem; color: var(--text-dim); margin-left: auto; font-feature-settings: 'tnum'; }}

/* === Tooltip === */
.tm-tooltip {{ position: fixed; pointer-events: none; background: rgba(15,17,23,0.95); border: 1px solid var(--border-light); border-radius: 8px; padding: 10px 14px; font-size: 0.82rem; color: var(--text); z-index: 1000; backdrop-filter: blur(8px); box-shadow: 0 4px 16px rgba(0,0,0,0.4); display: none; max-width: 280px; }}
.tm-tooltip .tt-name {{ font-weight: 700; margin-bottom: 4px; font-size: 0.9rem; }}
.tm-tooltip .tt-row {{ display: flex; justify-content: space-between; gap: 16px; font-feature-settings: 'tnum'; }}
.tm-tooltip .tt-label {{ color: var(--text-dim); }}

@media (max-width: 768px) {{
  .kpi-row.primary, .kpi-row.secondary {{ grid-template-columns: repeat(2, 1fr); }}
  .tabs {{ overflow-x: auto; width: 100%; }}
  table {{ font-size: 0.75rem; }}
  td, th {{ padding: 6px 8px; }}
  .treemap-container {{ height: 300px; }}
  .treemap-container.acct-treemap {{ height: 240px; }}
  .search-box {{ width: 180px; }}
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
<h1>주식 통합 대시보드</h1>
<p class="subtitle">NH투자증권 {len([a for a in account_summaries if a.startswith('NH')])}개 계좌 + 토스증권 1개 계좌 | {min(tx['date'] for tx in txs)} ~ {max(tx['date'] for tx in txs)} | 총 {len(txs):,}건</p>

<div class="tabs">
  <button class="tab active" onclick="switchTab('dashboard')">대시보드</button>
  <button class="tab" onclick="switchTab('portfolio')">포트폴리오</button>
  <button class="tab" onclick="switchTab('analysis')">분석</button>
</div>

<!-- ===== DASHBOARD TAB ===== -->
<div id="tab-dashboard" class="tab-content active">
  <!-- Primary KPIs -->
  <div class="kpi-row primary">
    <div class="kpi">
      <div class="kpi-label">보유 평가금액</div>
      <div class="kpi-value">{fmt_num(total_market_value)}</div>
      <div class="kpi-sub">{len(overall_holdings)}종목 보유중</div>
    </div>
    <div class="kpi {"border-positive" if total_unrealized >= 0 else "border-negative"}">
      <div class="kpi-label">평가손익 (미실현)</div>
      <div class="kpi-value {pnl_class(total_unrealized)}">{fmt_num(total_unrealized)}</div>
      <div class="kpi-sub {pnl_class(total_unrealized)}">{(total_unrealized / sum(h['cost'] for h in overall_holdings.values()) * 100) if sum(h['cost'] for h in overall_holdings.values()) > 0 else 0:+.1f}%</div>
    </div>
    <div class="kpi {"border-positive" if overall_realized_pnl >= 0 else "border-negative"}">
      <div class="kpi-label">실현 손익</div>
      <div class="kpi-value {pnl_class(overall_realized_pnl)}">{fmt_num(overall_realized_pnl)}</div>
      <div class="kpi-sub">수수료 {fmt_num(overall_fees)} + 세금 {fmt_num(overall_tax)}</div>
    </div>
    <div class="kpi {"border-positive" if overall_net_pnl >= 0 else "border-negative"}">
      <div class="kpi-label">총손익 (실현+배당)</div>
      <div class="kpi-value {pnl_class(overall_net_pnl)}">{fmt_num(overall_net_pnl)}</div>
      <div class="kpi-sub {pnl_class(overall_net_pnl)}">ROI {overall_roi:+.1f}%</div>
    </div>
  </div>
  <!-- Secondary KPIs -->
  <div class="kpi-row secondary" style="margin-bottom:24px;">
    <div class="kpi">
      <div class="kpi-label">총 매수금액</div>
      <div class="kpi-value compact">{fmt_num(overall_invested)}</div>
      <div class="kpi-sub">{len(stock_summaries)}종목 거래</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">총 매도금액</div>
      <div class="kpi-value compact">{fmt_num(overall_returned)}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">배당금</div>
      <div class="kpi-value compact positive">{fmt_num(overall_dividends)}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">IRR (연환산)</div>
      <div class="kpi-value compact {pnl_class(overall_irr or 0)}">{fmt_pct(overall_irr) if overall_irr else 'N/A'}</div>
    </div>
  </div>

  <!-- Treemap -->
  <div class="card">
    <div class="card-title">포트폴리오 구성 (평가금액 기준)</div>
    <div class="treemap-container" id="treemapContainer"></div>
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
  <div class="card">
    <div class="card-title">월별 투자/회수 추이</div>
    <div class="chart-container"><canvas id="timelineChart"></canvas></div>
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
</div>

<div class="tm-tooltip" id="acctTmTooltip"></div>

<script>
const ACCOUNTS = """ + json.dumps(js_account_data, ensure_ascii=False) + """;
const STOCKS = """ + json.dumps(js_stock_data, ensure_ascii=False) + """;
const OVERALL = """ + json.dumps(js_overall, ensure_ascii=False) + """;
const TIMELINE = """ + json.dumps(timeline, ensure_ascii=False) + """;
const TREEMAP_DATA = """ + json.dumps(treemap_data, ensure_ascii=False) + """;

function fmt(n) {
  if (n == null) return 'N/A';
  if (Math.abs(n) >= 1e8) return (n/1e8).toFixed(1) + '억';
  if (Math.abs(n) >= 1e4) return (n/1e4).toLocaleString('ko-KR', {maximumFractionDigits:0}) + '만';
  return n.toLocaleString('ko-KR');
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
      data-name="${r.name}" data-ret="${r.return_pct}" data-mv="${r.market_value}" data-qty="${r.qty}" data-cp="${r.current_price}" data-cost="${r.cost}" data-upnl="${r.unrealized_pnl}" data-weight="${r.weight || 0}">
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

function renderTreemap() {
  renderTreemapInContainer('treemapContainer', TREEMAP_DATA, 'tmTooltip');
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

// ===== Tab switching =====
function switchTab(name) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'analysis') initAnalysis();
  if (name === 'portfolio') { renderStockTable(); initAccounts(); }
  if (name === 'dashboard') { setTimeout(renderTreemap, 50); }
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
    </div>
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
        x: { ticks: { callback: v => fmt(v), color: '#8b8fa3' }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#e1e4eb', font: { size: 11 } }, grid: { display: false } }
      }
    }
  });

  new Chart(document.getElementById('topWinnersChart'), chartOpts(winners, '#22c55e'));
  new Chart(document.getElementById('topLosersChart'), chartOpts(losers, '#ef4444'));
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
      plugins: { legend: { labels: { color: '#e1e4eb' } }, tooltip: { callbacks: { label: ctx => ctx.dataset.label + ': ' + fmt(ctx.raw) } } },
      scales: {
        x: { ticks: { color: '#8b8fa3', maxRotation: 45 }, grid: { display: false } },
        y: { ticks: { callback: v => fmt(v), color: '#8b8fa3' }, grid: { color: '#2a2d3a' } }
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
          borderColor: '#ef4444',
          backgroundColor: 'rgba(239,68,68,0.08)',
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2,
        },
        {
          label: '누적 매도',
          data: TIMELINE.map(t => t.cum_returned),
          borderColor: '#22c55e',
          backgroundColor: 'rgba(34,197,94,0.08)',
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2,
        },
        {
          label: '누적 배당',
          data: TIMELINE.map(t => t.cum_dividends),
          borderColor: '#6366f1',
          backgroundColor: 'rgba(99,102,241,0.08)',
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2,
        },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#e1e4eb' } }, tooltip: { callbacks: { label: ctx => ctx.dataset.label + ': ' + fmt(ctx.raw) } } },
      scales: {
        x: { ticks: { color: '#8b8fa3', maxRotation: 45 }, grid: { display: false } },
        y: { ticks: { callback: v => fmt(v), color: '#8b8fa3' }, grid: { color: '#2a2d3a' } }
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
        borderColor: '#6366f1',
        backgroundColor: 'rgba(99,102,241,0.1)',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#e1e4eb' } }, tooltip: { callbacks: { label: ctx => fmt(ctx.raw) + '원' } } },
      scales: {
        x: { ticks: { color: '#8b8fa3', maxRotation: 45 }, grid: { display: false } },
        y: { ticks: { callback: v => fmt(v), color: '#8b8fa3' }, grid: { color: '#2a2d3a' } }
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
        backgroundColor: 'rgba(99,102,241,0.6)',
        borderColor: '#6366f1',
        borderWidth: 1,
        borderRadius: 4,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => fmt(ctx.raw) + '원' } } },
      scales: {
        x: { ticks: { color: '#8b8fa3', maxRotation: 45 }, grid: { display: false } },
        y: { ticks: { callback: v => fmt(v), color: '#8b8fa3' }, grid: { color: '#2a2d3a' } }
      }
    }
  });
}

// Init
initOverallCharts();
renderStockTable();
renderTreemap();
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
