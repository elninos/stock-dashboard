#!/usr/bin/env python3
"""다종목 일괄 백테스트.

알파 = 시그널 트레이딩 가치 - 단순 보유(Buy & Hold) 가치
       양수면 시그널이 더 잘 함, 음수면 그냥 보유가 더 나았음.
"""
import sys, os, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_trend_break import backtest


def fmt_won(v):
    """원화 단위 (억원/만원/원)."""
    if v is None: return "─"
    av = abs(v); s = "+" if v >= 0 else "-"
    if av >= 1_0000_0000:  # 1억 이상
        return f"{s}{av/1_0000_0000:>5.2f}억"
    if av >= 1_0000:        # 1만 이상
        return f"{s}{av/1_0000:>6,.0f}만"
    return f"{s}{av:>7,.0f}원"


def fmt_won_pos(v):
    """원화 (음수 부호 안 붙임)."""
    if v is None: return "─"
    if v >= 1_0000_0000: return f"{v/1_0000_0000:>5.2f}억"
    if v >= 1_0000:      return f"{v/1_0000:>6,.0f}만"
    return f"{v:>7,.0f}원"


CASES = [
    # (code, name, years, peer, label)
    ("214450", "파마리서치",  2.0, "LLY",  "✅ 매도 권고 (점수 15)"),
    ("200130", "콜마비앤에이치", 2.0, "EL",   "✅ 매도 권고 (점수 15)"),
    ("007460", "에이프로젠",   2.0, "LLY",  "✅ 매도 권고 (점수 15)"),
    ("035420", "NAVER",     2.0, "GOOGL","⚠️ 관찰 (점수 13)"),
    ("010170", "대한광통신",   2.0, "LITE", "🟢 강세 유지 (점수 2)"),
    ("000150", "두산",       2.0, "GE",   "🟢 강세 유지 (점수 2)"),
    ("000660", "SK하이닉스",   2.0, "NVDA", "🟢 강세 유지 (점수 2)"),
]

print(f"\n{'═'*120}")
print(f"  📊 대세 하락 시그널 — 다종목 백테스트 (2년)")
print(f"  💡 알파 = 시그널 트레이딩 가치 - 단순 보유 가치  (양수 = 시그널 더 좋음, 음수 = 그냥 보유가 더 좋음)")
print(f"{'═'*120}\n")
print(f"  {'종목':<14}{'코드':<8}{'현재 진단':<22}{'단순 보유':>10}{'시그널':>10}{'알파':>14}{'알파%':>9}{'거래':>4}{'정확':<6}")
print(f"  {'─'*120}")

results = []
for code, name, years, peer, label in CASES:
    try:
        r = backtest(code, years, 100, peer_symbol=peer)
        if "error" in r:
            print(f"  {name:<14}{code:<8}{label:<22}  ERROR: {r['error']}")
            continue
        n_tx = len(r["transactions"])
        # SELL 후 60일 가격 하락이면 ✅
        eval_str = "─"
        sells = [t for t in r["transactions"] if "SELL" in t["action"]]
        if sells:
            for s in sells:
                idx = next((i for i,a in enumerate(r["actions"]) if a["date"]==s["date"]), -1)
                if idx < 0: continue
                p_idx = min(idx + 8, len(r["actions"])-1)
                if p_idx > idx:
                    chg60 = (r["actions"][p_idx]["price"]/s["price"]-1)*100
                    eval_str = "✅" if chg60 < -5 else ("⚠️" if abs(chg60) <= 5 else "❌")
                    break

        alpha_pct = r["alpha_pct"]
        marker = "🟢" if alpha_pct > 0 else ("🔴" if alpha_pct < -1 else "⚪")
        print(f"  {name:<14}{code:<8}{label[:20]:<22}{fmt_won_pos(r['buyhold_value']):>10}{fmt_won_pos(r['final_value']):>10}{fmt_won(r['alpha']):>14}{marker}{alpha_pct:>+7.1f}%{n_tx:>4}{eval_str:<6}")
        results.append({"name": name, "code": code, "result": r})
    except Exception as e:
        print(f"  {name:<14}{code:<8} ❌ {str(e)[:60]}")

# 요약
print(f"\n  {'═'*120}")
print(f"  요약")
print(f"  {'─'*60}")
total_bh = sum(r["result"]["buyhold_value"] for r in results)
total_sig = sum(r["result"]["final_value"] for r in results)
total_alpha = total_sig - total_bh
print(f"  총 단순 보유 가치:  {fmt_won_pos(total_bh):>14}")
print(f"  총 시그널 가치:    {fmt_won_pos(total_sig):>14}")
print(f"  ⚖️  총 알파:        {fmt_won(total_alpha):>14}  ({(total_sig/total_bh-1)*100:+.1f}%)")

sell_recommended = [r for r in results if "매도" in next((c[4] for c in CASES if c[0]==r["code"]), "")]
hold_recommended = [r for r in results if "강세" in next((c[4] for c in CASES if c[0]==r["code"]), "") or "관찰" in next((c[4] for c in CASES if c[0]==r["code"]), "")]

if sell_recommended:
    sr_bh = sum(r["result"]["buyhold_value"] for r in sell_recommended)
    sr_sig = sum(r["result"]["final_value"] for r in sell_recommended)
    print(f"\n  🚨 매도 권고 종목 ({len(sell_recommended)}개) — 시그널이 진짜 추세 깨짐 잡았는지")
    print(f"     단순 보유:   {fmt_won_pos(sr_bh):>14}")
    print(f"     시그널:      {fmt_won_pos(sr_sig):>14}")
    print(f"     알파:        {fmt_won(sr_sig-sr_bh):>14}  ({(sr_sig/sr_bh-1)*100:+.1f}%)")

if hold_recommended:
    hr_bh = sum(r["result"]["buyhold_value"] for r in hold_recommended)
    hr_sig = sum(r["result"]["final_value"] for r in hold_recommended)
    print(f"\n  🟢 강세 유지 종목 ({len(hold_recommended)}개) — 시그널이 헛매도 안 했는지")
    print(f"     단순 보유:   {fmt_won_pos(hr_bh):>14}")
    print(f"     시그널:      {fmt_won_pos(hr_sig):>14}")
    print(f"     알파:        {fmt_won(hr_sig-hr_bh):>14}  ({(hr_sig/hr_bh-1)*100:+.1f}%)")
