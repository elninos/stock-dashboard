#!/usr/bin/env python3
"""Peak Warning + Trend Break 통합 — 다종목 일괄 백테스트."""
import sys, os, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_peak import backtest, fmt_won, fmt_won_pos


# 다양한 케이스: 강세주 / 천정 후 빠진 종목 / 손실 종목
CASES = [
    # (code, name, peer, label)
    ("214450", "파마리서치",     "LLY",    "🚨 천정 후 -54%"),
    ("000250", "삼천당제약",     "LLY",    "🚨 천정 후 -62%"),
    ("200130", "콜마비앤에이치",  "EL",     "🔴 Stage 4"),
    ("007460", "에이프로젠",      "LLY",    "🔴 Stage 4 (작은 종목)"),
    ("035420", "NAVER",        "GOOGL",  "🟡 박스권"),
    ("010170", "대한광통신",     "LITE",   "🟢 강세 (+2940%)"),
    ("000150", "두산",          "GE",     "🟢 강세 (+189%)"),
    ("000660", "SK하이닉스",     "NVDA",   "🟢 강세 (+36%)"),
    ("058470", "리노공업",       "AMAT",   "🟢 강세 (+118%)"),
    ("039030", "이오테크닉스",   "AMAT",   "🟢 강세 (+137%)"),
    ("310210", "보로노이",       "LLY",    "🟢 강세 (+30%)"),
    ("278470", "에이피알",       "EL",     "🟢 강세 (+114%)"),
]

print(f"\n{'═'*130}")
print(f"  📊 천정 시그널 + 대세 하락 통합 백테스트 (2년)")
print(f"  💡 알파 = 시그널 매매 - 단순 보유  (양수 = 시그널 더 좋음)")
print(f"{'═'*130}\n")
print(f"  {'종목':<14}{'코드':<8}{'특성':<22}{'단순 보유':>12}{'시그널':>12}{'알파':>14}{'알파%':>9}{'거래':>4}")
print(f"  {'─'*130}")

results = []
total_bh = 0; total_sig = 0
strong_results = []
weak_results = []

import sys
NO_REBUY = "--no-rebuy" in sys.argv

if NO_REBUY:
    print("  ⚠️ 모드: 매도만 (재매수 없음) — 사용자 실제 시나리오\n")

for code, name, peer, label in CASES:
    try:
        r = backtest(code, lookback_years=2.0, starting_qty=100, peer_symbol=peer, no_rebuy=NO_REBUY)
        if "error" in r:
            print(f"  {name:<14}{code:<8}{label:<22}  ERROR: {r['error']}")
            continue

        marker = "🟢" if r['alpha_pct'] > 0.5 else ("🔴" if r['alpha_pct'] < -3 else "⚪")
        print(f"  {name:<14}{code:<8}{label:<22}{fmt_won_pos(r['buyhold_value']):>12}{fmt_won_pos(r['final_value']):>12}{fmt_won(r['alpha']):>14}{marker}{r['alpha_pct']:>+7.1f}%{len(r['transactions']):>4}")

        total_bh += r["buyhold_value"]
        total_sig += r["final_value"]

        if "🚨" in label or "🔴" in label:
            strong_results.append({"name": name, "result": r, "label": label})
        else:
            weak_results.append({"name": name, "result": r, "label": label})

        results.append({"name": name, "code": code, "label": label, "result": r})
    except Exception as e:
        print(f"  {name:<14}{code:<8} ❌ {str(e)[:60]}")

# 요약
print(f"\n  {'═'*130}")
print(f"  ⚖️  종합")
print(f"  {'─'*60}")
total_alpha = total_sig - total_bh
total_alpha_pct = (total_sig/total_bh - 1) * 100 if total_bh > 0 else 0
print(f"  단순 보유 합계:    {fmt_won_pos(total_bh):>14}")
print(f"  시그널 합계:      {fmt_won_pos(total_sig):>14}")
print(f"  총 알파:         {fmt_won(total_alpha):>14}  ({total_alpha_pct:+.1f}%)")

if strong_results:
    bh = sum(r["result"]["buyhold_value"] for r in strong_results)
    sig = sum(r["result"]["final_value"] for r in strong_results)
    print(f"\n  🚨 천정 후 빠진 종목 ({len(strong_results)}개) — 시그널이 매도 잘 했는지")
    print(f"     단순 보유: {fmt_won_pos(bh):>12}  /  시그널: {fmt_won_pos(sig):>12}")
    print(f"     알파:     {fmt_won(sig-bh):>12}  ({(sig/bh-1)*100:+.1f}%)")

if weak_results:
    bh = sum(r["result"]["buyhold_value"] for r in weak_results)
    sig = sum(r["result"]["final_value"] for r in weak_results)
    print(f"\n  🟢 강세 유지 종목 ({len(weak_results)}개) — 시그널이 헛매도 안 했는지")
    print(f"     단순 보유: {fmt_won_pos(bh):>12}  /  시그널: {fmt_won_pos(sig):>12}")
    print(f"     알파:     {fmt_won(sig-bh):>12}  ({(sig/bh-1)*100:+.1f}%)")

# 종목별 거래 횟수 통계
print(f"\n  📊 거래 빈도")
for r in results:
    res = r["result"]
    n = len(res["transactions"])
    print(f"     {r['name']:<14} 거래 {n:>2}건  (알파 {res['alpha_pct']:+.1f}%)")
