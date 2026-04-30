"""NH HTS [1503] 거래원 기간별분석 CSV/Excel 파서.

파일명 규칙: {종목명}_{거래원명}_{YYYYMMDD}_{YYYYMMDD}.csv (또는 .xlsx)
컬럼: 일자, 종가, 전일비방향, 전일비, (%), 매도량, 매수량, 순매수량, 누적순매수량, 거래합, 총거래량
마지막 행: 누계 합산 → skip
"""
import os
import glob
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd


BROKER_FLOW_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "broker_flow")

COLS = ["date", "close", "dir", "change", "change_pct",
        "sell_vol", "buy_vol", "net_vol", "cumul_net", "broker_total", "market_total"]


def _parse_file(filepath: str):
    """단일 파일 파싱. 실패 시 None 반환."""
    fp = Path(filepath)
    ext = fp.suffix.lower()

    try:
        if ext == ".csv":
            df = pd.read_csv(
                filepath, encoding="euc-kr", header=0,
                skipfooter=1, engine="python", names=COLS,
            )
        elif ext in (".xlsx", ".xls"):
            df = pd.read_excel(
                filepath, header=0, skipfooter=1, names=COLS,
                engine="openpyxl" if ext == ".xlsx" else "xlrd",
            )
        else:
            return None

        # 누계 행 제거 (date 컬럼이 숫자 아닌 경우)
        df = df[df["date"].astype(str).str.match(r"\d{2}/\d{2}/\d{2}")]

        # 날짜 파싱 YY/MM/DD → YYYY-MM-DD
        df["date"] = pd.to_datetime(df["date"], format="%y/%m/%d").dt.strftime("%Y-%m-%d")

        # 숫자 컬럼 정제 (쉼표 제거)
        num_cols = ["close", "change", "sell_vol", "buy_vol", "net_vol",
                    "cumul_net", "broker_total", "market_total"]
        for col in num_cols:
            df[col] = (
                df[col].astype(str)
                .str.replace(",", "", regex=False)
                .str.strip()
            )
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

        df["change_pct"] = (
            pd.to_numeric(
                df["change_pct"].astype(str).str.replace(",", "", regex=False),
                errors="coerce",
            ).fillna(0)
        )

        # 파일명에서 메타데이터 추출
        parts = fp.stem.split("_")
        if len(parts) >= 4:
            df["stock"]  = parts[0]
            df["broker"] = "_".join(parts[1:-2])
        else:
            df["stock"]  = parts[0] if parts else ""
            df["broker"] = ""

        return df.sort_values("date").reset_index(drop=True)

    except Exception as e:
        print(f"  [WARN] 파싱 실패 {fp.name}: {e}")
        return None


def load_broker_flows(stock_name: str, broker_flow_dir: str = BROKER_FLOW_DIR) -> pd.DataFrame:
    """특정 종목의 모든 거래원 파일을 합쳐서 반환.

    Returns:
        DataFrame with columns: date, broker, net_vol, buy_vol, sell_vol, cumul_net
        빈 DataFrame if no files found.
    """
    pattern = os.path.join(broker_flow_dir, f"{stock_name}_*.*")
    files = glob.glob(pattern)

    if not files:
        return pd.DataFrame()

    dfs = []
    for f in files:
        df = _parse_file(f)
        if df is not None and not df.empty:
            dfs.append(df[["date", "broker", "net_vol", "buy_vol", "sell_vol", "cumul_net", "market_total"]])

    if not dfs:
        return pd.DataFrame()

    return pd.concat(dfs, ignore_index=True).sort_values(["broker", "date"])


def analyze_broker_flow(stock_name: str, lookback_days: int = 60,
                        broker_flow_dir: str = BROKER_FLOW_DIR) -> dict:
    """거래원별 매매 패턴 분석. sub_score(0~4) 반환."""
    df = load_broker_flows(stock_name, broker_flow_dir)

    if df.empty:
        return {"available": False, "sub_score": 0, "reasons": []}

    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    df = df[df["date"] >= cutoff]

    if df.empty:
        return {"available": False, "sub_score": 0, "reasons": []}

    score = 0
    reasons = []
    top_buyers = []
    top_sellers = []

    brokers = df["broker"].unique()
    broker_stats = []

    for broker in brokers:
        b = df[df["broker"] == broker]
        total_net   = int(b["net_vol"].sum())
        buy_days    = int((b["net_vol"] > 0).sum())
        sell_days   = int((b["net_vol"] < 0).sum())
        total_days  = len(b)
        # 일방향성: buy_days / total_days (active days)
        active_days = buy_days + sell_days
        one_sided   = buy_days / active_days if active_days > 0 else 0

        broker_stats.append({
            "broker": broker,
            "net_vol": total_net,
            "buy_days": buy_days,
            "sell_days": sell_days,
            "one_sided_buy_ratio": round(one_sided, 2),
        })

    broker_stats.sort(key=lambda x: x["net_vol"], reverse=True)

    # 상위 매수 창구
    top_buyers = [b for b in broker_stats if b["net_vol"] > 0][:3]
    top_sellers = [b for b in broker_stats if b["net_vol"] < 0][:3]

    # 일방향 매수 창구 (90% 이상 매수일)
    one_sided_buyers = [
        b for b in broker_stats
        if b["one_sided_buy_ratio"] >= 0.9 and b["buy_days"] >= 10
    ]

    # 매도 전환 감지 (최근 5일 매도 우위)
    recent_cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    recent = df[df["date"] >= recent_cutoff]
    recent_net = int(recent.groupby("broker")["net_vol"].sum().sum())

    if recent_net < 0:
        score += 2
        reasons.append(f"최근 7일 전체 창구 순매도 ({recent_net:+,})")
    elif top_sellers and abs(top_sellers[0]["net_vol"]) > abs(top_buyers[0]["net_vol"] if top_buyers else 1):
        score += 1
        reasons.append(f"매도 창구 우위: {top_sellers[0]['broker']}")

    if one_sided_buyers:
        names = ", ".join(b["broker"] for b in one_sided_buyers[:2])
        score = max(0, score - 1)  # 일방향 매수 존재 시 완화
        reasons.append(f"일방향 매수 창구: {names}")

    return {
        "available": True,
        "top_buyers": top_buyers[:3],
        "top_sellers": top_sellers[:3],
        "one_sided_buyers": [b["broker"] for b in one_sided_buyers],
        "recent_7d_net": recent_net,
        "sub_score": min(score, 4),
        "reasons": reasons,
    }
