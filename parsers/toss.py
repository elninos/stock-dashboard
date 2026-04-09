"""토스증권 PDF parser."""
import fitz
import re
import os
import glob

from .common import TOSS_DIR, make_tx

BROKER = "토스증권"
ACCOUNT = "토스"

TRADE_TYPES = {
    "구매": "buy", "판매": "sell", "배당금입금": "dividend",
    "1주이벤트입고": "buy", "해외주식이벤트입고": "buy",
    "출석체크이벤트입고": "buy", "외화증권배당금입금": "dividend",
}


def parse_toss_pdfs():
    """Parse all Toss PDF files with proper KRW/USD section handling."""
    all_transactions = []

    for pdf_file in sorted(glob.glob(os.path.join(TOSS_DIR, "거래내역서_*.pdf"))):
        doc = fitz.open(pdf_file)
        in_usd_section = False

        for page in doc:
            text = page.get_text()
            lines = text.strip().split("\n")

            clean_lines = []
            section_markers = []
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
                section_markers.append("USD" if in_usd_section else "KRW")
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

                stock_name = re.sub(r"\([A-Z][A-Z0-9]+\)$", "", stock_line).strip()
                stock_name = stock_name.replace("\xa0", " ")
                foreign_code = re.search(r"\(([A-Z]{2}[A-Z0-9]+)\)", stock_line)
                has_foreign_code = bool(foreign_code and not foreign_code.group(1).startswith("A"))

                if i < len(clean_lines) and re.match(r"^\([A-Z]{2}[A-Z0-9]+\)$", clean_lines[i].strip()):
                    code_line = clean_lines[i].strip()
                    if not code_line.startswith("(A"):
                        has_foreign_code = True
                    i += 1

                raw_nums = []
                while i < len(clean_lines) and not re.match(r"^\d{4}\.\d{2}\.\d{2}$", clean_lines[i]):
                    val = clean_lines[i].strip()
                    i += 1
                    if val.startswith("($") or val.startswith("($ "):
                        continue
                    parts = val.split()
                    for part in parts:
                        try:
                            raw_nums.append(float(part.replace(",", "")))
                        except ValueError:
                            continue

                if is_usd or has_foreign_code:
                    if len(raw_nums) < 3:
                        continue
                    qty_raw = raw_nums[1]
                    amount = raw_nums[2] if len(raw_nums) > 2 else 0
                    price = raw_nums[3] if len(raw_nums) > 3 else 0
                    fee = raw_nums[4] if len(raw_nums) > 4 else 0
                    tax = raw_nums[5] if len(raw_nums) > 5 else 0

                    if qty_raw < 1 and tx_type == "buy":
                        continue
                    qty = int(qty_raw) if qty_raw >= 1 else 0

                    if tx_type == "dividend":
                        amount = raw_nums[2] if len(raw_nums) > 2 else 0
                        qty = 0
                        price = 0

                    if qty == 0 and tx_type in ["buy", "sell"]:
                        continue

                    all_transactions.append(make_tx(
                        date_str, ACCOUNT, BROKER, tx_type,
                        stock=stock_name, qty=qty, price=price,
                        amount=amount, fee=fee, tax=tax, currency="USD",
                    ))
                else:
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

                    all_transactions.append(make_tx(
                        date_str, ACCOUNT, BROKER, tx_type,
                        stock=stock_name, qty=qty, price=price,
                        amount=amount, fee=fee, tax=tax, currency="KRW",
                    ))

        doc.close()

    return all_transactions
