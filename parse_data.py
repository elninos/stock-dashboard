#!/usr/bin/env python3
"""Parse NH Excel and Toss PDF files into unified JSON for dashboard."""
import pandas as pd
import fitz
import json
import re
import os
import glob
from datetime import datetime

BASE_DRIVE = "/Users/r/Library/CloudStorage/GoogleDrive-srshin614@gmail.com/내 드라이브"
NH_DIR = f"{BASE_DRIVE}/새 폴더/아카이브"
TOSS_DIR = f"{BASE_DRIVE}/03.Finance/토스"
OUTPUT = "/Users/r/Documents/Claude/stock-dashboard/transactions.json"


def parse_nh_excel(folder_path, account_name):
    """Parse NH투자증권 Excel files."""
    transactions = []
    for f in glob.glob(os.path.join(folder_path, "*.xlsx")):
        df = pd.read_excel(f)
        trades = df[df["거래유형"].isin(["매수", "매도"])].copy()
        for _, row in trades.iterrows():
            tx_type = "buy" if row["거래유형"] == "매수" else "sell"
            transactions.append({
                "date": str(row["실거래일자"]).replace(".", "-"),
                "account": account_name,
                "broker": "NH투자증권",
                "type": tx_type,
                "stock": str(row["종목명"]),
                "qty": int(row["수량"]),
                "price": float(row["단가"]),
                "amount": float(row["거래금액"]),
                "fee": float(row["수수료"]),
                "tax": float(row["세금"]),
                "currency": "KRW",
            })
        # Capture dividends
        divs = df[df["상세내용"] == "배당금"].copy()
        for _, row in divs.iterrows():
            transactions.append({
                "date": str(row["실거래일자"]).replace(".", "-"),
                "account": account_name,
                "broker": "NH투자증권",
                "type": "dividend",
                "stock": str(row["종목명"]) if pd.notna(row["종목명"]) else "Unknown",
                "qty": 0,
                "price": 0,
                "amount": float(row["거래금액"]),
                "fee": 0,
                "tax": float(row["세금"]),
                "currency": "KRW",
            })
        # Capture 대체출고/대체입고 (inter-account transfers)
        transfers = df[df["상세내용"].isin(["대체출고", "대체입고"])].copy()
        for _, row in transfers.iterrows():
            if pd.isna(row["종목명"]) or int(row["수량"]) == 0:
                continue
            tx_type = "transfer_out" if row["상세내용"] == "대체출고" else "transfer_in"
            transactions.append({
                "date": str(row["실거래일자"]).replace(".", "-"),
                "account": account_name,
                "broker": "NH투자증권",
                "type": tx_type,
                "stock": str(row["종목명"]),
                "qty": int(row["수량"]),
                "price": float(row["단가"]) if pd.notna(row["단가"]) else 0,
                "amount": 0,
                "fee": 0,
                "tax": 0,
                "currency": "KRW",
            })
        # Capture 공모주입고/유상주/무상주/대여무상권리주 입고
        ipo_types = ["공모주입고", "유상주", "무상주", "대여무상권리주입고", "외화무상주"]
        ipo = df[df["상세내용"].isin(ipo_types)].copy()
        for _, row in ipo.iterrows():
            if pd.isna(row["종목명"]) or int(row["수량"]) == 0:
                continue
            transactions.append({
                "date": str(row["실거래일자"]).replace(".", "-"),
                "account": account_name,
                "broker": "NH투자증권",
                "type": "buy",
                "stock": str(row["종목명"]),
                "qty": int(row["수량"]),
                "price": float(row["단가"]) if pd.notna(row["단가"]) and float(row["단가"]) > 0 else 0,
                "amount": int(row["수량"]) * float(row["단가"]) if pd.notna(row["단가"]) and float(row["단가"]) > 0 else 0,
                "fee": 0,
                "tax": 0,
                "currency": "KRW",
            })
        # Capture 유상청약출금 (subscription payment - actual cost basis)
        subs = df[df["상세내용"] == "유상청약출금"].copy()
        for _, row in subs.iterrows():
            if pd.isna(row["종목명"]) or int(row["수량"]) == 0:
                continue
            # This is the cash outflow for rights issue, not a stock buy
            # The stock comes in later as 유상주 입고
            # Skip to avoid double-counting (유상주 입고 already captured above)
    return transactions


def parse_toss_pdfs():
    """Parse all Toss PDF files with proper KRW/USD section handling."""
    all_transactions = []
    account_name = "토스"

    TRADE_TYPES = {"구매": "buy", "판매": "sell", "배당금입금": "dividend",
                   "1주이벤트입고": "buy", "해외주식이벤트입고": "buy",
                   "출석체크이벤트입고": "buy", "외화증권배당금입금": "dividend"}

    for pdf_file in sorted(glob.glob(os.path.join(TOSS_DIR, "거래내역서_*.pdf"))):
        doc = fitz.open(pdf_file)
        in_usd_section = False

        for page in doc:
            text = page.get_text()
            lines = text.strip().split("\n")

            # Track section transitions
            clean_lines = []
            section_markers = []  # (line_index, 'KRW'|'USD')
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                if "원화 거래내역" in line:
                    in_usd_section = False
                    continue
                if "달러 거래내역" in line:
                    in_usd_section = True
                    continue
                if re.match(r"^\d+ / \d+$", line):
                    continue
                if line in ["거래일자", "거래구분", "종목명(종목코드)", "환율", "거래수량", "거래대금",
                             "단가", "수수료", "거래세", "제세금", "변제/연체합", "잔고", "잔액"]:
                    continue
                if any(x in line for x in ["발급", "조회 기간", "요청 고객", "수량단위", "금액단위"]):
                    continue
                if line in ["성명", "계좌 번호", "전체", "원화+달러"]:
                    continue
                if re.match(r"^\d{8}-", line):
                    continue
                # Track which section this line belongs to
                section_markers.append(("USD" if in_usd_section else "KRW"))
                clean_lines.append(line)

            i = 0
            while i < len(clean_lines):
                line = clean_lines[i]
                is_usd = section_markers[i] == "USD"

                date_match = re.match(r"^(\d{4}\.\d{2}\.\d{2})$", line)
                if not date_match:
                    i += 1
                    continue

                date_str = date_match.group(1).replace(".", "-")
                i += 1
                if i >= len(clean_lines):
                    break

                tx_type_str = clean_lines[i].strip()
                i += 1

                if tx_type_str not in TRADE_TYPES:
                    while i < len(clean_lines) and not re.match(r"^\d{4}\.\d{2}\.\d{2}$", clean_lines[i]):
                        i += 1
                    continue

                tx_type = TRADE_TYPES[tx_type_str]

                if i >= len(clean_lines):
                    break
                stock_line = clean_lines[i].strip()
                i += 1

                # Stock name: may have code like (A239890) or (US75734B1008) or (CA09173B1076) or (MHY...)
                stock_name = re.sub(r"\([A-Z][A-Z0-9]+\)$", "", stock_line).strip()
                # Foreign stock: any code NOT starting with A (Korean codes start with A)
                foreign_code = re.search(r"\(([A-Z]{2}[A-Z0-9]+)\)", stock_line)
                has_foreign_code = bool(foreign_code and not foreign_code.group(1).startswith("A"))

                # If next line is also a stock code like (US...) or (CA...) or (MH...)
                if i < len(clean_lines) and re.match(r"^\([A-Z]{2}[A-Z0-9]+\)$", clean_lines[i].strip()):
                    code_line = clean_lines[i].strip()
                    if not code_line.startswith("(A"):
                        has_foreign_code = True
                    i += 1

                # Collect all remaining numeric values for this row
                raw_nums = []
                while i < len(clean_lines) and not re.match(r"^\d{4}\.\d{2}\.\d{2}$", clean_lines[i]):
                    val = clean_lines[i].strip()
                    i += 1
                    # Skip dollar amounts
                    if val.startswith("($") or val.startswith("($ "):
                        continue
                    try:
                        num_val = float(val.replace(",", ""))
                        raw_nums.append(num_val)
                    except ValueError:
                        continue

                if is_usd or has_foreign_code:
                    # USD format: exchange_rate, qty, amount_krw, price_krw, fee_krw, tax_krw, loan_krw, balance_qty, cash_krw
                    if len(raw_nums) < 3:
                        continue
                    exchange_rate = raw_nums[0]
                    qty_raw = raw_nums[1]
                    amount = raw_nums[2] if len(raw_nums) > 2 else 0
                    price = raw_nums[3] if len(raw_nums) > 3 else 0
                    fee = raw_nums[4] if len(raw_nums) > 4 else 0
                    tax = raw_nums[5] if len(raw_nums) > 5 else 0

                    # Fractional shares: qty < 1 means fractional event stock
                    if qty_raw < 1 and tx_type == "buy":
                        continue  # Skip fractional event stocks (negligible value)
                    qty = int(qty_raw) if qty_raw >= 1 else 0

                    if tx_type == "dividend":
                        amount = raw_nums[2] if len(raw_nums) > 2 else 0
                        qty = 0
                        price = 0

                    if qty == 0 and tx_type in ["buy", "sell"]:
                        continue

                    all_transactions.append({
                        "date": date_str, "account": account_name, "broker": "토스증권",
                        "type": tx_type, "stock": stock_name, "qty": qty,
                        "price": price, "amount": amount, "fee": fee, "tax": tax,
                        "currency": "USD",
                    })
                else:
                    # KRW format: qty, amount, price, fee, tax, tax2, loan, balance_qty, cash
                    qty = int(raw_nums[0]) if len(raw_nums) > 0 else 0
                    amount = raw_nums[1] if len(raw_nums) > 1 else 0
                    price = raw_nums[2] if len(raw_nums) > 2 else 0
                    fee = raw_nums[3] if len(raw_nums) > 3 else 0
                    tax = raw_nums[4] if len(raw_nums) > 4 else 0

                    if tx_type == "dividend":
                        amount = raw_nums[1] if len(raw_nums) > 1 else 0
                        qty = 0
                        price = 0

                    if qty == 0 and tx_type in ["buy", "sell"] and tx_type_str != "1주이벤트입고":
                        continue
                    if amount <= 0 and tx_type != "dividend":
                        continue

                    all_transactions.append({
                        "date": date_str, "account": account_name, "broker": "토스증권",
                        "type": tx_type, "stock": stock_name, "qty": qty,
                        "price": price, "amount": amount, "fee": fee, "tax": tax,
                        "currency": "KRW",
                    })

        doc.close()

    return all_transactions


def main():
    all_transactions = []

    # Parse NH accounts
    if os.path.isdir(NH_DIR):
        for folder in sorted(os.listdir(NH_DIR)):
            folder_path = os.path.join(NH_DIR, folder)
            if os.path.isdir(folder_path) and folder.startswith("NH"):
                print(f"Parsing {folder}...")
                txs = parse_nh_excel(folder_path, folder)
                all_transactions.extend(txs)
                print(f"  Found {len(txs)} transactions")

    # Parse Toss
    print("Parsing Toss PDFs...")
    toss_txs = parse_toss_pdfs()
    all_transactions.extend(toss_txs)
    print(f"  Found {len(toss_txs)} transactions")

    # Sort by date
    all_transactions.sort(key=lambda x: x["date"])

    # Summary
    accounts = set(tx["account"] for tx in all_transactions)
    stocks = set(tx["stock"] for tx in all_transactions if tx["type"] in ["buy", "sell"])
    print(f"\nTotal: {len(all_transactions)} transactions")
    print(f"Accounts: {sorted(accounts)}")
    print(f"Stocks: {len(stocks)} unique")

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(all_transactions, f, ensure_ascii=False, indent=2)
    print(f"\nSaved to {OUTPUT}")


if __name__ == "__main__":
    main()
