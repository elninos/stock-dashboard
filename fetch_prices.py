#!/usr/bin/env python3
"""Fetch current stock prices from Naver Finance and update prices.json."""
import json
import os
import time
import urllib.request
import urllib.error

PRICES_FILE = os.path.join(os.path.dirname(__file__), "prices.json")
TRANSACTIONS_FILE = os.path.join(os.path.dirname(__file__), "transactions.json")

# ===== Stock code mappings =====
# Korean stocks: name -> KRX code (6 digits)
KR_CODES = {
    "AJ네트웍스": "095570",
    "BGF에코머티리얼즈": "082270",
    "CJ ENM": "035760",
    "GST": "083450",
    "HLB제넥스": "025000",
    "HPSP": "403870",
    "KH 미래물산": "025880",
    "KODEX 코스닥150레버리지": "233740",
    "KT": "030200",
    "LB세미콘": "061970",
    "LS머트리얼즈": "417200",
    "NAVER": "035420",
    "NHN": "181710",
    "NHN KCP": "060250",
    "PS일렉트로닉스": "017510",
    "RF머트리얼즈": "327260",
    "SK렌터카": "068400",
    "SK바이오사이언스": "302440",
    "SK바이오팜": "326030",
    "SK하이닉스": "000660",
    "SM C&C": "048550",
    "TIME K바이오액티브": "463050",
    "TIME 차이나AI테크액티브": "463060",
    "경남제약": "053950",
    "넥스틴": "348210",
    "노바텍": "285490",
    "농우바이오": "054050",
    "대한광통신": "010170",
    "두산": "000150",
    "두산에너빌리티": "034020",
    "디와이": "013570",
    "레드로버": "060300",
    "레드캡투어": "038390",
    "리가켐바이오": "141080",
    "메디톡스": "086900",
    "미래나노텍": "095500",
    "비덴트": "121800",
    "삼성SDI": "006400",
    "삼성생명": "032830",
    "삼성전자": "005930",
    "삼성엔씨켐": "462010",
    "삼양엔씨켐": "462010",
    "삼천당제약": "000250",
    "셀트리온": "068270",
    "스튜디오드래곤": "253450",
    "시노펙스": "025320",
    "시프트업": "462870",
    "실리콘투": "257720",
    "쏘닉스": "060230",
    "씨에스윈드": "112610",
    "씨티씨바이오": "060590",
    "아리바이오": "253590",
    "아미코젠": "092040",
    "아스트": "067390",
    "아이센스": "099190",
    "아이언디바이스": "264660",
    "아티스트스튜디오": "308080",
    "에스디바이오센서": "137310",
    "에스아이리소스": "065420",
    "에이비프로바이오": "195990",
    "에이프로": "262260",
    "에이프로젠": "007460",
    "에이티넘인베스트": "021080",
    "에이피알": "278470",
    "에코글로우": "371950",
    "에코볼트": "425560",
    "엔에이치SL스팩": "475930",
    "엔씨소프트": "036570",
    "오르비텍": "046120",
    "오스코텍": "039200",
    "와이엠티": "251370",
    "우성아이비": "046820",
    "위닉스": "044340",
    "위더스제약": "330350",
    "유아이디": "069330",
    "이오테크닉스": "039030",
    "제일기획": "030000",
    "지누스": "013890",
    "진매트릭스": "109820",
    "차AI헬스케어": "404990",
    "차바이오텍": "085660",
    "카카오": "035720",
    "코아스템켐온": "166480",
    "코오롱티슈진": "950160",
    "콘텐트리중앙": "036420",
    "콜마비앤에이치": "200130",
    "크래프톤": "259960",
    "태경비케이": "014580",
    "토모큐브": "340570",
    "티에프이": "062970",
    "파마리서치": "214450",
    "파크시스템스": "140860",
    "퓨쳐켐": "220100",
    "피엔에이치테크": "290470",
    "필옵틱스": "161580",
    "하림": "136480",
    "하이록코리아": "013030",
    "하이브": "352820",
    "한양증권": "001750",
    "한화솔루션": "009830",
    "후성": "093370",
}

# Foreign stocks: name -> {ticker, exchange}
# These are traded on US/HK exchanges, need different API
FOREIGN_STOCKS = {
    "알리바바 그룹 홀딩스 ADR": {"ticker": "BABA", "exchange": "US"},
    "뉴스케일파워": {"ticker": "SMR", "exchange": "US"},
    "레딧": {"ticker": "RDDT", "exchange": "US"},
    "로켓 랩": {"ticker": "RKLB", "exchange": "US"},
    "리얼티 인컴": {"ticker": "O", "exchange": "US"},
    "비트코인 전략 2배 ETF": {"ticker": "BITU", "exchange": "US"},
    "비트팜스": {"ticker": "BITF", "exchange": "US"},
    "셀시어스 홀딩스": {"ticker": "CELH", "exchange": "US"},
    "슈왑 미국 배당주 ETF": {"ticker": "SCHD", "exchange": "US"},
    "캐스터 마리타임": {"ticker": "CTRM", "exchange": "US"},
    "팔란티어": {"ticker": "PLTR", "exchange": "US"},
    "프로셰어즈 QQQ 3배 ETF": {"ticker": "TQQQ", "exchange": "US"},
    "하이맥스 테크놀로지스": {"ticker": "HIMX", "exchange": "US"},
    "글로벌엑스 중국 클린 에너지 ETF(USD)": {"ticker": "2809.HK", "exchange": "HK"},
    "글로벌엑스 중국 클린 에너지 ETF(HKD)": {"ticker": "2809.HK", "exchange": "HK"},
    "융기실리콘자재": {"ticker": "601012.SS", "exchange": "CN"},
    "통위": {"ticker": "600438.SS", "exchange": "CN"},
}


def fetch_naver_price(code):
    """Fetch current price from Naver Finance mobile API."""
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            price = int(data.get("closePrice", "0").replace(",", ""))
            return price if price > 0 else None
    except Exception as e:
        return None


def fetch_us_price_yahoo(ticker):
    """Fetch US stock price from Yahoo Finance API."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
            return round(price, 2)
    except Exception as e:
        return None


def main():
    # Load existing prices
    prices = {}
    if os.path.exists(PRICES_FILE):
        with open(PRICES_FILE, encoding="utf-8") as f:
            prices = json.load(f)

    # Load transactions to find all stocks
    with open(TRANSACTIONS_FILE, encoding="utf-8") as f:
        txs = json.load(f)
    all_stocks = set(tx["stock"] for tx in txs)

    updated = 0
    failed = []

    # Fetch Korean stock prices
    print("=== Korean stocks (Naver Finance) ===")
    for name in sorted(all_stocks):
        if name in KR_CODES:
            code = KR_CODES[name]
            price = fetch_naver_price(code)
            if price:
                prices[name] = {"code": code, "price": price}
                print(f"  OK {name}: {price:,}")
                updated += 1
            else:
                failed.append(name)
                print(f"  FAIL {name} ({code})")
            time.sleep(0.2)  # Rate limiting

    # Fetch foreign stock prices
    print("\n=== Foreign stocks (Yahoo Finance) ===")
    for name in sorted(all_stocks):
        if name in FOREIGN_STOCKS:
            info = FOREIGN_STOCKS[name]
            ticker = info["ticker"]
            price = fetch_us_price_yahoo(ticker)
            if price:
                prices[name] = {"code": ticker, "price": price}
                print(f"  OK {name} ({ticker}): {price}")
                updated += 1
            else:
                failed.append(name)
                print(f"  FAIL {name} ({ticker})")
            time.sleep(0.3)

    # Save
    with open(PRICES_FILE, "w", encoding="utf-8") as f:
        json.dump(prices, f, ensure_ascii=False, indent=2)

    print(f"\n=== Summary ===")
    print(f"Updated: {updated}")
    print(f"Failed: {len(failed)}")
    if failed:
        print(f"  {', '.join(failed)}")
    print(f"Total in prices.json: {len(prices)}")
    print(f"Saved to {PRICES_FILE}")


if __name__ == "__main__":
    main()
