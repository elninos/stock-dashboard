#!/usr/bin/env python3
"""Build interactive HTML dashboard from parsed transactions."""
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta

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
    irr = calc_xirr(account_cashflows)

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
    irr = calc_xirr(m["cashflows"])
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
overall_irr = calc_xirr(all_cashflows)

# ===== Timeline data for chart =====
monthly_data = defaultdict(lambda: {"invested": 0, "returned": 0, "pnl": 0, "dividends": 0})
for tx in txs:
    month = tx["date"][:7]
    if tx["type"] == "buy":
        monthly_data[month]["invested"] += tx["amount"]
    elif tx["type"] == "sell":
        monthly_data[month]["returned"] += tx["amount"]
    elif tx["type"] == "dividend":
        monthly_data[month]["dividends"] += tx["amount"]

months_sorted = sorted(monthly_data.keys())
cum_invested = 0
cum_returned = 0
cum_dividends = 0
timeline = []
for m in months_sorted:
    d = monthly_data[m]
    cum_invested += d["invested"]
    cum_returned += d["returned"]
    cum_dividends += d["dividends"]
    timeline.append({
        "month": m,
        "cum_invested": cum_invested,
        "cum_returned": cum_returned,
        "cum_dividends": cum_dividends,
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
  --card: #1a1d29;
  --border: #2a2d3a;
  --text: #e1e4eb;
  --text-dim: #8b8fa3;
  --accent: #6366f1;
  --positive: #22c55e;
  --negative: #ef4444;
  --warn: #f59e0b;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }}
.container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 8px; }}
.subtitle {{ color: var(--text-dim); margin-bottom: 24px; font-size: 0.9rem; }}
.tabs {{ display: flex; gap: 4px; margin-bottom: 24px; background: var(--card); border-radius: 12px; padding: 4px; border: 1px solid var(--border); }}
.tab {{ padding: 10px 20px; border-radius: 8px; cursor: pointer; font-size: 0.9rem; font-weight: 500; color: var(--text-dim); transition: all 0.2s; border: none; background: none; }}
.tab:hover {{ color: var(--text); }}
.tab.active {{ background: var(--accent); color: white; }}
.tab-content {{ display: none; }}
.tab-content.active {{ display: block; }}

.kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }}
.kpi {{ background: var(--card); border-radius: 12px; padding: 20px; border: 1px solid var(--border); }}
.kpi-label {{ font-size: 0.8rem; color: var(--text-dim); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}
.kpi-value {{ font-size: 1.5rem; font-weight: 700; }}
.kpi-sub {{ font-size: 0.8rem; color: var(--text-dim); margin-top: 4px; }}
.positive {{ color: var(--positive); }}
.negative {{ color: var(--negative); }}

.card {{ background: var(--card); border-radius: 12px; padding: 20px; border: 1px solid var(--border); margin-bottom: 20px; }}
.card-title {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 16px; display: flex; align-items: center; gap: 8px; }}
.chart-container {{ position: relative; height: 350px; }}

table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
th {{ text-align: left; padding: 10px 12px; border-bottom: 2px solid var(--border); color: var(--text-dim); font-weight: 600; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.5px; cursor: pointer; user-select: none; white-space: nowrap; }}
th:hover {{ color: var(--text); }}
th.sort-asc::after {{ content: ' \\25B2'; font-size: 0.6rem; }}
th.sort-desc::after {{ content: ' \\25BC'; font-size: 0.6rem; }}
td {{ padding: 10px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }}
tr:hover td {{ background: rgba(99, 102, 241, 0.05); }}
.text-right {{ text-align: right; }}
.text-center {{ text-align: center; }}

.holdings-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 12px; }}
.holding-card {{ background: rgba(99,102,241,0.08); border-radius: 10px; padding: 14px; border: 1px solid rgba(99,102,241,0.15); }}
.holding-name {{ font-weight: 600; font-size: 0.9rem; margin-bottom: 6px; }}
.holding-detail {{ font-size: 0.8rem; color: var(--text-dim); }}

.account-selector {{ display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }}
.account-btn {{ padding: 6px 16px; border-radius: 20px; cursor: pointer; font-size: 0.85rem; border: 1px solid var(--border); background: var(--card); color: var(--text-dim); transition: all 0.2s; }}
.account-btn:hover {{ border-color: var(--accent); color: var(--text); }}
.account-btn.active {{ background: var(--accent); color: white; border-color: var(--accent); }}

.search-box {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 8px 14px; color: var(--text); font-size: 0.9rem; width: 250px; }}
.search-box::placeholder {{ color: var(--text-dim); }}
.search-box:focus {{ outline: none; border-color: var(--accent); }}

.toolbar {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }}
.filter-group {{ display: flex; gap: 4px; }}
.filter-btn {{ padding: 5px 14px; border-radius: 16px; cursor: pointer; font-size: 0.8rem; border: 1px solid var(--border); background: var(--card); color: var(--text-dim); transition: all 0.2s; }}
.filter-btn:hover {{ border-color: var(--accent); color: var(--text); }}
.filter-btn.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
.result-count {{ font-size: 0.8rem; color: var(--text-dim); margin-left: auto; }}

@media (max-width: 768px) {{
  .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }}
  .tabs {{ overflow-x: auto; }}
  table {{ font-size: 0.75rem; }}
  td, th {{ padding: 6px 8px; }}
}}
</style>
</head>
<body>
<div class="container">
<h1>주식 통합 대시보드</h1>
<p class="subtitle">NH투자증권 {len([a for a in account_summaries if a.startswith('NH')])}개 계좌 + 토스증권 1개 계좌 | {min(tx['date'] for tx in txs)} ~ {max(tx['date'] for tx in txs)} | 총 {len(txs):,}건</p>

<div class="tabs">
  <button class="tab active" onclick="switchTab('overall')">전체 종합</button>
  <button class="tab" onclick="switchTab('accounts')">계좌별</button>
  <button class="tab" onclick="switchTab('stocks')">종목별</button>
  <button class="tab" onclick="switchTab('timeline')">추이</button>
</div>

<!-- ===== OVERALL TAB ===== -->
<div id="tab-overall" class="tab-content active">
  <div class="kpi-grid">
    <div class="kpi">
      <div class="kpi-label">총 매수금액</div>
      <div class="kpi-value">{fmt_num(overall_invested)}</div>
      <div class="kpi-sub">{len(stock_summaries)}종목 거래</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">총 매도금액</div>
      <div class="kpi-value">{fmt_num(overall_returned)}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">실현 손익</div>
      <div class="kpi-value {pnl_class(overall_realized_pnl)}">{fmt_num(overall_realized_pnl)}</div>
      <div class="kpi-sub">수수료 {fmt_num(overall_fees)} + 세금 {fmt_num(overall_tax)}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">배당금</div>
      <div class="kpi-value positive">{fmt_num(overall_dividends)}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">순손익 (실현+배당)</div>
      <div class="kpi-value {pnl_class(overall_net_pnl)}">{fmt_num(overall_net_pnl)}</div>
      <div class="kpi-sub">ROI {overall_roi:.1f}%</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">IRR (연환산)</div>
      <div class="kpi-value {pnl_class(overall_irr or 0)}">{fmt_pct(overall_irr/100) if overall_irr else 'N/A'}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">보유 평가금액</div>
      <div class="kpi-value">{fmt_num(total_market_value)}</div>
      <div class="kpi-sub">{len(overall_holdings)}종목 보유중</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">평가손익 (미실현)</div>
      <div class="kpi-value {pnl_class(total_unrealized)}">{fmt_num(total_unrealized)}</div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">현재 보유 종목</div>
    <div class="holdings-grid">
"""

for stock, h in sorted(overall_holdings.items(), key=lambda x: x[1]["qty"] * current_prices.get(x[0], x[1]["avg_price"]), reverse=True):
    cp = current_prices.get(stock, h["avg_price"])
    mv = h["qty"] * cp
    pnl = mv - h["cost"]
    pnl_pct = (pnl / h["cost"] * 100) if h["cost"] > 0 else 0
    weight = (mv / total_market_value * 100) if total_market_value > 0 else 0
    html += f"""      <div class="holding-card">
        <div class="holding-name">{stock} <span style="font-size:0.75rem;color:var(--text-dim)">{weight:.1f}%</span></div>
        <div class="holding-detail">{h['qty']:,}주 x {cp:,}원</div>
        <div class="holding-detail">평가 {fmt_num(mv)} <span class="{pnl_class(pnl)}">{pnl_pct:+.1f}%</span></div>
      </div>
"""

html += """    </div>
  </div>

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

<!-- ===== ACCOUNTS TAB ===== -->
<div id="tab-accounts" class="tab-content">
  <div class="account-selector" id="accountSelector"></div>
  <div id="accountDetail"></div>
</div>

<!-- ===== STOCKS TAB ===== -->
<div id="tab-stocks" class="tab-content">
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

<!-- ===== TIMELINE TAB ===== -->
<div id="tab-timeline" class="tab-content">
  <div class="card">
    <div class="card-title">월별 투자/회수 추이</div>
    <div class="chart-container"><canvas id="timelineChart"></canvas></div>
  </div>
  <div class="card">
    <div class="card-title">누적 순현금흐름</div>
    <div class="chart-container"><canvas id="cashflowChart"></canvas></div>
  </div>
</div>
</div>

<script>
const ACCOUNTS = """ + json.dumps(js_account_data, ensure_ascii=False) + """;
const STOCKS = """ + json.dumps(js_stock_data, ensure_ascii=False) + """;
const OVERALL = """ + json.dumps(js_overall, ensure_ascii=False) + """;
const TIMELINE = """ + json.dumps(timeline, ensure_ascii=False) + """;

function fmt(n) {
  if (n == null) return 'N/A';
  if (Math.abs(n) >= 1e8) return (n/1e8).toFixed(1) + '억';
  if (Math.abs(n) >= 1e4) return (n/1e4).toLocaleString('ko-KR', {maximumFractionDigits:0}) + '만';
  return n.toLocaleString('ko-KR');
}
function pnlCls(v) { return v > 0 ? 'positive' : v < 0 ? 'negative' : ''; }

// Tab switching
function switchTab(name) {
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'timeline') initTimeline();
  if (name === 'stocks') renderStockTable();
  if (name === 'accounts') initAccounts();
}

// ===== STOCK TABLE =====
let stockSortCol = 'net_pnl';
let stockSortDir = 'desc';
let stockFilter = 'all';
let stockData = [...STOCKS];

function setStockFilter(filter, btn) {
  stockFilter = filter;
  document.querySelectorAll('#tab-stocks .filter-btn').forEach(b => b.classList.remove('active'));
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
  const search = document.getElementById('stockSearch').value.toLowerCase();
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
      <td class="text-right">${fmt(s.invested)}</td>
      <td class="text-right">${fmt(s.returned)}</td>
      <td class="text-right ${pnlCls(s.realized_pnl)}">${fmt(s.realized_pnl)}</td>
      <td class="text-right">${s.dividends > 0 ? fmt(s.dividends) : '-'}</td>
      <td class="text-right ${pnlCls(s.net_pnl)}"><strong>${fmt(s.net_pnl)}</strong></td>
      <td class="text-right ${pnlCls(s.roi)}">${s.roi.toFixed(1)}%</td>
      <td class="text-right ${pnlCls(s.irr)}">${s.irr != null ? s.irr.toFixed(1) + '%' : '-'}</td>
      <td class="text-right">${s.current_qty > 0 ? s.current_qty.toLocaleString() : '-'}</td>
      <td class="text-right">${s.current_qty > 0 ? fmt(s.current_price) : '-'}</td>
      <td class="text-right">${s.market_value > 0 ? '<strong>' + fmt(s.market_value) + '</strong>' : '-'}</td>
      <td class="text-right ${pnlCls(s.unrealized_pnl)}">${s.current_qty > 0 ? fmt(s.unrealized_pnl) : '-'}</td>
      <td class="text-right">${s.weight > 0 ? s.weight.toFixed(1) + '%' : '-'}</td>
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
function initAccounts() {
  const sel = document.getElementById('accountSelector');
  if (sel.children.length > 0) return;
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
    <td class="text-right">${fmt(s.invested)}</td>
    <td class="text-right">${fmt(s.returned)}</td>
    <td class="text-right ${pnlCls(s.realized_pnl)}">${fmt(s.realized_pnl)}</td>
    <td class="text-right">${s.dividends > 0 ? fmt(s.dividends) : '-'}</td>
    <td class="text-right ${pnlCls(s.net_pnl)}"><strong>${fmt(s.net_pnl)}</strong></td>
    <td class="text-right ${pnlCls(s.roi)}">${s.roi.toFixed(1)}%</td>
    <td class="text-right ${pnlCls(s.irr)}">${s.irr != null ? s.irr.toFixed(1) + '%' : '-'}</td>
    <td class="text-right">${s.current_qty > 0 ? s.current_qty.toLocaleString() + '주' : '-'}</td>
    <td class="text-right">${s.current_qty > 0 ? fmt(s.current_price) : '-'}</td>
    <td class="text-right">${s.market_value > 0 ? '<strong>' + fmt(s.market_value) + '</strong>' : '-'}</td>
    <td class="text-right ${pnlCls(s.unrealized_pnl)}">${s.current_qty > 0 ? fmt(s.unrealized_pnl) : '-'}</td>
    <td class="text-right">${s.weight > 0 ? s.weight.toFixed(1) + '%' : '-'}</td>
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

  detail.innerHTML = `
    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-label">매수금액</div><div class="kpi-value">${fmt(data.total_invested)}</div><div class="kpi-sub">${data.num_trades}건 거래</div></div>
      <div class="kpi"><div class="kpi-label">매도금액</div><div class="kpi-value">${fmt(data.total_returned)}</div></div>
      <div class="kpi"><div class="kpi-label">실현손익</div><div class="kpi-value ${pnlCls(data.realized_pnl)}">${fmt(data.realized_pnl)}</div></div>
      <div class="kpi"><div class="kpi-label">배당</div><div class="kpi-value">${fmt(data.dividends)}</div></div>
      <div class="kpi"><div class="kpi-label">순손익</div><div class="kpi-value ${pnlCls(netPnl)}">${fmt(netPnl)}</div><div class="kpi-sub">ROI ${roi}%</div></div>
      <div class="kpi"><div class="kpi-label">IRR</div><div class="kpi-value ${pnlCls(data.irr)}">${data.irr != null ? data.irr.toFixed(1) + '%' : 'N/A'}</div></div>
    </div>
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

// ===== TIMELINE =====
let timelineInited = false;
function initTimeline() {
  if (timelineInited) return;
  timelineInited = true;

  new Chart(document.getElementById('timelineChart'), {
    type: 'bar',
    data: {
      labels: TIMELINE.map(t => t.month),
      datasets: [
        { label: '매수', data: TIMELINE.map(t => -t.cum_invested + (TIMELINE[TIMELINE.indexOf(t)-1]?.cum_invested || 0) === 0 ? 0 : t.cum_invested - (TIMELINE[TIMELINE.indexOf(t)-1]?.cum_invested || 0)),
          backgroundColor: '#ef444488', borderColor: '#ef4444', borderWidth: 1 },
        { label: '매도', data: TIMELINE.map((t,i) => t.cum_returned - (TIMELINE[i-1]?.cum_returned || 0)),
          backgroundColor: '#22c55e88', borderColor: '#22c55e', borderWidth: 1 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { tooltip: { callbacks: { label: ctx => ctx.dataset.label + ': ' + fmt(ctx.raw) } } },
      scales: {
        x: { ticks: { color: '#8b8fa3', maxRotation: 45 }, grid: { display: false } },
        y: { ticks: { callback: v => fmt(v), color: '#8b8fa3' }, grid: { color: '#2a2d3a' } }
      }
    }
  });

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
      plugins: { tooltip: { callbacks: { label: ctx => fmt(ctx.raw) + '원' } } },
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
</script>
</body>
</html>"""

with open("/Users/r/Documents/Claude/stock-dashboard/index.html", "w", encoding="utf-8") as f:
    f.write(html)
print("Dashboard saved to index.html")
print(f"Overall: invested={fmt_num(overall_invested)}, returned={fmt_num(overall_returned)}, pnl={fmt_num(overall_net_pnl)}, IRR={fmt_pct(overall_irr/100) if overall_irr else 'N/A'}")
