"""종합 점수화 및 등급 산출."""


GRADE_THRESHOLDS = [
    (10, "매도강추", "#dc2626"),
    (7,  "매도주의", "#ea580c"),
    (4,  "관망",     "#ca8a04"),
    (0,  "홀드",     "#16a34a"),
]


def get_grade(score: float, market_regime: str = "중립") -> tuple[str, str]:
    """점수 → (등급명, 색상코드). 시장 강세 시 임계값 +1 완화."""
    adjusted = score - (1 if market_regime == "강세" else 0)
    for threshold, label, color in GRADE_THRESHOLDS:
        if adjusted >= threshold:
            return label, color
    return "홀드", "#16a34a"


def compute_signal(
    name: str,
    code: str,
    trend: dict,
    investor: dict,
    broker: dict,
    market_regime: str = "중립",
) -> dict:
    """세 가지 sub_score 합산 → 종합 시그널."""
    t_score = trend.get("sub_score", 0)
    i_score = investor.get("sub_score", 0)
    b_score = broker.get("sub_score", 0)
    total   = t_score + i_score + b_score

    grade, color = get_grade(total, market_regime)

    all_reasons = (
        trend.get("reasons", [])
        + investor.get("reasons", [])
        + broker.get("reasons", [])
    )

    return {
        "stock":        name,
        "code":         code,
        "score":        total,
        "grade":        grade,
        "grade_color":  color,
        "trend":        {**trend,    "sub_score": t_score},
        "investor":     {**investor, "sub_score": i_score},
        "broker":       {**broker,   "sub_score": b_score},
        "reasons":      all_reasons,
    }
