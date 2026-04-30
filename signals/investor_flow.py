"""투자자별 수급 분석: 외국인/기관/개인 순매수 — pykrx 기반."""
from datetime import datetime, timedelta


def _get_investor_flow(code: str, days: int = 30):
    from pykrx import stock as krx
    to = datetime.now()
    frm = to - timedelta(days=days)
    df = krx.get_market_trading_value_by_date(
        frm.strftime("%Y%m%d"), to.strftime("%Y%m%d"), code
    )
    return df


def analyze_investor_flow(name: str, code: str) -> dict:
    """외국인/기관/개인 5일·20일 순매수 분석. sub_score(0~4) 반환."""
    try:
        df = _get_investor_flow(code, days=40)
        if df is None or df.empty or len(df) < 5:
            return {"error": "KRX 투자자 데이터 없음 (서버 일시 불응)", "sub_score": 0}

        # 컬럼명: 기관합계, 외국인합계, 개인 (pykrx 버전별 다를 수 있음)
        col_map = {}
        for col in df.columns:
            if "외국인" in col:
                col_map["foreign"] = col
            elif "기관" in col:
                col_map["inst"] = col
            elif "개인" in col:
                col_map["retail"] = col

        if not col_map.get("foreign") or not col_map.get("inst"):
            return {"error": f"컬럼 미확인: {list(df.columns)}", "sub_score": 0}

        foreign = df[col_map["foreign"]]
        inst    = df[col_map["inst"]]

        f5  = int(foreign.iloc[-5:].sum())
        f20 = int(foreign.iloc[-20:].sum()) if len(foreign) >= 20 else int(foreign.sum())
        i5  = int(inst.iloc[-5:].sum())
        i20 = int(inst.iloc[-20:].sum()) if len(inst) >= 20 else int(inst.sum())

        score = 0
        reasons = []

        # 외국인 20일
        if f20 < 0:
            score += 2
            reasons.append(f"외국인 20일 순매도 ({f20/1e8:.1f}억)")
        elif f5 < 0:
            score += 1
            reasons.append(f"외국인 5일 순매도 ({f5/1e8:.1f}억)")

        # 기관 20일
        if i20 < 0:
            score += 2
            reasons.append(f"기관 20일 순매도 ({i20/1e8:.1f}억)")
        elif i5 < 0:
            score += 1
            reasons.append(f"기관 5일 순매도 ({i5/1e8:.1f}억)")

        return {
            "foreign_5d": f5,
            "foreign_20d": f20,
            "inst_5d": i5,
            "inst_20d": i20,
            "sub_score": min(score, 4),
            "reasons": reasons,
        }

    except Exception as e:
        return {"error": str(e), "sub_score": 0, "reasons": []}
