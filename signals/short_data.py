"""공매도 데이터 로더 + 시그널 탐지.

daily_short/{종목명}/{종목명}_{YYYYMMDD}.csv 파일을 읽어
공매도 잔고/거래량/비중 시계열 + 급증 시그널 반환.

핵심 시그널:
  1. 공매도 잔고율 ≥ 5% → 공매도 압력 높음
  2. 공매도 잔고 5일 +30% 급증 → 분배 의심
  3. 공매도 거래 비중 ≥ 30% → 당일 매도 압력 강함
  4. 잔고/거래량 동시 급증 → 강력한 매도 시그널
"""
import os, glob, re, warnings
from collections import defaultdict
warnings.filterwarnings("ignore")


# 시그널 임계값
SHORT_BALANCE_RATIO_HIGH    = 5.0   # 잔고 비율 5% 이상
SHORT_BALANCE_INCREASE_5D   = 30.0  # 5일간 잔고 +30% 증가
SHORT_VOLUME_RATIO_HIGH     = 30.0  # 당일 공매도 비중 30% 이상


def _load_file(fpath: str):
    """CSV 파일 로드. NH HTS [1320] export 형식 가정.

    예상 컬럼 (실제 확인 필요):
      일자, 종가, 등락률, 거래량,
      공매도수량, 공매도비중(%), 공매도거래대금,
      공매도잔고수량, 공매도잔고비율(%), 공매도잔고금액
    """
    with open(fpath, "rb") as f:
        raw = f.read()
    bom = b"\xef\xbb\xbf"
    if raw.startswith(bom):
        text = raw.decode("utf-8-sig")
    else:
        text = raw.decode("cp949", errors="replace")

    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if len(lines) < 2:
        return None

    # 헤더 파싱
    header = [c.strip('"').strip() for c in lines[0].split(",")]
    # 컬럼 인덱스 매핑
    col_idx = {}
    for i, c in enumerate(header):
        c_norm = c.replace(" ", "").replace("(", "").replace(")", "").replace("%", "")
        if "일자" in c_norm or c_norm == "날짜":
            col_idx["date"] = i
        elif "종가" in c_norm:
            col_idx["close"] = i
        elif "공매도수량" in c_norm or ("공매도" in c_norm and "거래량" in c_norm):
            col_idx["short_vol"] = i
        elif "공매도비중" in c_norm or "공매도비율" in c_norm:
            if "잔고" not in c_norm:
                col_idx["short_vol_ratio"] = i
        elif "잔고수량" in c_norm or "공매도잔고수량" in c_norm:
            col_idx["short_balance"] = i
        elif "잔고비율" in c_norm or "잔고비중" in c_norm:
            col_idx["short_balance_ratio"] = i
        elif "잔고금액" in c_norm:
            col_idx["short_balance_amt"] = i
        elif "거래량" in c_norm:
            col_idx["volume"] = i

    rows = []
    for line in lines[1:]:
        parts = [p.strip('"').strip() for p in line.split(",")]
        if len(parts) < len(header):
            continue
        try:
            row = {}
            for k, idx in col_idx.items():
                v = parts[idx].replace(",", "").replace("+", "").strip()
                if v in ("", "-", "N/A"):
                    row[k] = None
                elif k == "date":
                    # "20260424" or "2026-04-24" or "2026/04/24"
                    v = v.replace("-", "").replace("/", "").replace(".", "")
                    row[k] = v[:8] if len(v) >= 8 else None
                elif k.endswith("_ratio"):
                    row[k] = float(v) if v else None
                else:
                    row[k] = int(float(v)) if v else None
            if row.get("date"):
                rows.append(row)
        except Exception:
            continue
    return rows or None


def load_stock_short(stock_name: str, short_dir: str) -> dict:
    """종목 폴더에서 모든 일자 데이터 통합. 가장 최근 파일이 누적 시계열."""
    folder = os.path.join(short_dir, stock_name)
    if not os.path.isdir(folder):
        return {}

    # 가장 최근 파일 사용 (각 파일은 누적 N일치 포함)
    files = sorted(glob.glob(os.path.join(folder, "*.csv")))
    if not files:
        return {}

    # 모든 파일 통합 (일자 중복 시 최신 파일 우선)
    merged = {}
    for fpath in files:
        rows = _load_file(fpath)
        if not rows: continue
        for r in rows:
            merged[r["date"]] = r

    return merged


def build_short_timeseries(short_data: dict):
    """공매도 시계열 + 5일 변화율 계산. 반환: pd.DataFrame"""
    import pandas as pd

    if not short_data:
        return None

    dates = sorted(short_data.keys())
    records = []
    for d in dates:
        r = short_data[d]
        # YYYYMMDD → datetime
        ts = pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:8]}")
        records.append({
            "date": ts,
            "close":               r.get("close"),
            "volume":              r.get("volume"),
            "short_vol":           r.get("short_vol"),
            "short_vol_ratio":     r.get("short_vol_ratio"),
            "short_balance":       r.get("short_balance"),
            "short_balance_ratio": r.get("short_balance_ratio"),
            "short_balance_amt":   r.get("short_balance_amt"),
        })

    df = pd.DataFrame(records).set_index("date").sort_index()

    # 잔고 변화율 (5일/20일)
    if "short_balance" in df.columns and df["short_balance"].notna().any():
        df["short_balance_5d_pct"]  = df["short_balance"].pct_change(5)  * 100
        df["short_balance_20d_pct"] = df["short_balance"].pct_change(20) * 100

    return df


def detect_short_signals(df) -> list:
    """공매도 기반 매도 시그널 탐지."""
    if df is None or df.empty:
        return []

    signals = []
    for i in range(len(df)):
        row = df.iloc[i]
        date_str = df.index[i].strftime("%Y-%m-%d")

        triggers = []
        score = 0

        bal_ratio = row.get("short_balance_ratio")
        bal_5d = row.get("short_balance_5d_pct")
        vol_ratio = row.get("short_vol_ratio")

        # 1) 잔고 비율 높음
        if bal_ratio is not None and bal_ratio >= SHORT_BALANCE_RATIO_HIGH:
            score += 1
            triggers.append({
                "type": "high_balance",
                "label": f"공매도 잔고율 높음 ({bal_ratio:.2f}%)",
                "weight": 1,
            })

        # 2) 잔고 5일 급증
        if bal_5d is not None and bal_5d >= SHORT_BALANCE_INCREASE_5D:
            score += 2
            triggers.append({
                "type": "balance_spike",
                "label": f"잔고 5일 +{bal_5d:.0f}% 급증",
                "weight": 2,
            })

        # 3) 당일 공매도 비중 높음
        if vol_ratio is not None and vol_ratio >= SHORT_VOLUME_RATIO_HIGH:
            score += 1
            triggers.append({
                "type": "high_short_volume",
                "label": f"당일 공매도 비중 {vol_ratio:.0f}%",
                "weight": 1,
            })

        # 4) 잔고 + 비중 동시 급증
        if (bal_5d is not None and bal_5d >= 20 and
            vol_ratio is not None and vol_ratio >= 25):
            score += 2
            triggers.append({
                "type": "double_spike",
                "label": f"잔고+거래 동시 급증 (5일 잔고 +{bal_5d:.0f}% / 비중 {vol_ratio:.0f}%)",
                "weight": 2,
            })

        if score > 0:
            signals.append({
                "date": date_str,
                "score": score,
                "balance_ratio": bal_ratio,
                "balance_5d_pct": bal_5d,
                "vol_ratio": vol_ratio,
                "triggers": triggers,
            })

    return signals


def analyze_short(stock_name: str, short_dir: str) -> dict:
    """단일 종목 공매도 분석 (대시보드용 요약)."""
    data = load_stock_short(stock_name, short_dir)
    if not data:
        return {"available": False}

    df = build_short_timeseries(data)
    if df is None or df.empty:
        return {"available": False}

    signals = detect_short_signals(df)
    last = df.iloc[-1]

    return {
        "available": True,
        "n_days": len(df),
        "last_date": df.index[-1].strftime("%Y-%m-%d"),
        "last_balance_ratio": float(last.get("short_balance_ratio") or 0),
        "last_balance_5d_pct": float(last.get("short_balance_5d_pct") or 0),
        "last_vol_ratio": float(last.get("short_vol_ratio") or 0),
        "n_signals": len(signals),
        "recent_signals": signals[-5:],
        "alert": (last.get("short_balance_5d_pct") or 0) >= SHORT_BALANCE_INCREASE_5D,
    }
