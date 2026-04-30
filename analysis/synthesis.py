"""사건 추적 + 사후 통계 교차 검증.

흐름:
  1. narrative.analyze(code)    → 주역 추적 결과
  2. statistical.analyze(code)  → 과거 peak 패턴 매칭 결과
  3. 두 결과 교차 → 종합 verdict
       둘 다 매도  → 강한 매도주의 (high confidence)
       narrative만  → 주역 약화 — 새 패턴일 수 있음
       statistical만 → 패턴은 나오는데 주역 모호 — 추가 분석 필요
       둘 다 홀드   → 홀드

리포트는 사건 추적 메인, 통계는 감사 형태로 뒤에 붙임.
"""
from . import narrative, statistical


# 등급 매핑 — narrative verdict → 점수
NARRATIVE_SCORE = {
    "강한 매도 신호": 4,
    "매도 신호":     3,
    "주의":          2,
    "약한 주의":     1,
    "유지":          0,
    "추적불가":     -1,
    "랠리 없음":     0,
}

# 등급 매핑 — statistical grade → 점수
STATISTICAL_SCORE = {
    "매도주의":   3,
    "관망":      2,
    "주의":      1,
    "홀드":      0,
    "데이터부족": -1,
}


def combine_verdicts(narr_v: str, stat_g: str) -> tuple:
    """두 verdict 결합 → (종합 등급, 신뢰도, 근거)"""
    n = NARRATIVE_SCORE.get(narr_v, 0)
    s = STATISTICAL_SCORE.get(stat_g, 0)

    # 둘 다 데이터 부족
    if n < 0 and s < 0:
        return "데이터부족", "낮음", "사건 추적/통계 모두 데이터 부족"

    # 점수 합산 (max 7)
    total = max(n, 0) + max(s, 0)

    # 일치도
    if n >= 3 and s >= 2:    confidence = "높음"
    elif n >= 2 and s >= 1:  confidence = "중간"
    elif n == 0 and s == 0:  confidence = "높음"
    else:                    confidence = "낮음"   # 한쪽만 신호

    if total >= 6:    grade = "강한 매도주의"
    elif total >= 4:  grade = "매도주의"
    elif total >= 2:  grade = "관망"
    elif total >= 1:  grade = "약한 주의"
    else:             grade = "홀드"

    # 근거 메시지
    parts = [f"사건 추적: {narr_v}", f"통계 패턴: {stat_g}"]
    if n >= 2 and s == 0:
        parts.append("⚠️ 통계는 안 잡힌 신호 — 새 패턴 가능성")
    if s >= 2 and n == 0:
        parts.append("⚠️ 사건 추적은 안 잡힌 신호 — 주역 정의 검토 필요")
    return grade, confidence, " | ".join(parts)


def analyze(code: str, name: str = "") -> dict:
    """단일 종목 종합 분석."""
    narr = narrative.analyze(code, name=name)
    stat = statistical.analyze(code, name=name)

    narr_v = narr.get("verdict", "랠리 없음")
    stat_g = stat.get("grade",   "홀드")

    grade, confidence, reason = combine_verdicts(narr_v, stat_g)

    return {
        "code":         code,
        "name":         name,
        "as_of":        narr.get("as_of") or stat.get("as_of"),
        "narrative":    narr,
        "statistical":  stat,
        "combined": {
            "grade":      grade,
            "confidence": confidence,
            "reason":     reason,
        },
    }


def render_report(r: dict) -> str:
    """종합 리포트 — 사건 추적 메인 + 통계 감사."""
    md = []

    name = r.get("name","")
    md.append(f"# {name} ({r['code']}) — 종합 분석")
    md.append(f"기준일: {r.get('as_of','')}")

    c = r["combined"]
    md.append(f"\n## 종합 판정")
    md.append(f"### **{c['grade']}**  (신뢰도 {c['confidence']})")
    md.append(f"\n{c['reason']}")

    # 1. 사건 추적
    md.append("\n---\n")
    md.append(narrative.render_report(r["narrative"]).split("\n", 2)[2])  # 헤더 제거

    # 2. 통계 감사
    md.append("\n---\n")
    md.append("## 📊 통계 감사 (참고)")
    stat = r["statistical"]
    md.append(f"\n사후 통계 등급: **{stat.get('grade','')}** — {stat.get('reason','')}")
    if stat.get("triggers"):
        md.append(f"\n과거 위험 고점 {stat.get('n_peaks',0)}건 기준 트리거 {len(stat['triggers'])}개:")
        for t in stat["triggers"][:5]:
            md.append(f"- `{t['feature']}`: 현재 {t['current']:.4g} (peak 중앙값 {t['peak_median']:.4g}) — {t['severity']}")

    return "\n".join(md)
