#!/usr/bin/env python3
"""Parse NH Excel, 나무 Excel, and Toss PDF files into unified JSON for dashboard."""
import json
import os

from parsers import parse_nh_excel, parse_namu_excel, parse_toss_pdfs
from parsers.common import NH_DIR, NAMU_DIR, OUTPUT


def main():
    all_transactions = []

    # Parse NH accounts
    if os.path.isdir(NH_DIR):
        for folder in sorted(os.listdir(NH_DIR)):
            folder_path = os.path.join(NH_DIR, folder)
            if os.path.isdir(folder_path) and (
                folder.startswith("NH") or folder.startswith("01.") or folder.startswith("02.")
            ):
                print(f"Parsing {folder}...")
                txs = parse_nh_excel(folder_path, folder)
                all_transactions.extend(txs)
                print(f"  Found {len(txs)} transactions")

    # Parse 나무증권 accounts
    if os.path.isdir(NAMU_DIR):
        for folder in sorted(os.listdir(NAMU_DIR)):
            folder_path = os.path.join(NAMU_DIR, folder)
            if os.path.isdir(folder_path):
                print(f"Parsing {folder}...")
                txs = parse_namu_excel(folder_path, folder)
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
