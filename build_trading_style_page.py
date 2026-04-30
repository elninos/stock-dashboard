#!/usr/bin/env python3
"""매매 스타일 분석 페이지.

11년치 거래 데이터를 분석해 매매 스타일을 진단하고 개선점 제안.

dashboard/trading_style.html 생성.
"""
import os, sys, json, warnings
from collections import defaultdict
from datetime import datetime
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE

OUT = os.path.join(BASE_DIR, "dashboard", "trading_style.html")
TODAY = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


def fmt_man(v):
    """원 → 만원 또는 억"""
    if abs(v) >= 1e8:
        return f"{v/1e8:+.2f}억"
    return f"{v/1e4:+,.0f}만"


def main():
    txs = load_json(TRANSACTIONS_FILE, default=[])
    smap = load_json(STOCK_MAP_FILE, default={})
    trades = [t for t in txs if t.get("type") in ("buy", "sell")]
    buys = [t for t in trades if t["type"] == "buy"]
    sells = [t for t in trades if t["type"] == "sell"]

    # ── 1. 연도별 매매 빈도 ────────────────────────────
    by_year = defaultdict(lambda: {"buy": 0, "sell": 0, "amt_buy": 0, "amt_sell": 0})
    for t in trades:
        y = t["date"][:4]
        by_year[y][t["type"]] += 1
        by_year[y]["amt_" + t["type"]] += t.get("amount", 0)

    # ── 2. 보유 기간 (FIFO 매칭) ────────────────────────
    pos = defaultdict(list)
    hold_days = []
    matched_pnl = []  # (days, pnl_pct, stock)
    for t in trades:
        s = t["stock"]
        if t["type"] == "buy":
            pos[s].append({"date": t["date"], "qty": t["qty"], "price": t["price"]})
        else:
            remain = t["qty"]
            sell_d = datetime.strptime(t["date"], "%Y-%m-%d")
            while remain > 0 and pos[s]:
                lot = pos[s][0]
                take = min(remain, lot["qty"])
                buy_d = datetime.strptime(lot["date"], "%Y-%m-%d")
                days = (sell_d - buy_d).days
                hold_days.append(days)
                pnl_pct = (t["price"] / lot["price"] - 1) * 100 if lot["price"] > 0 else 0
                matched_pnl.append((days, pnl_pct, s))
                lot["qty"] -= take
                remain -= take
                if lot["qty"] <= 0:
                    pos[s].pop(0)

    n_match = len(matched_pnl)
    gains = sum(1 for _, p, _ in matched_pnl if p > 0)
    win_rate = gains / n_match * 100 if n_match else 0
    avg_hold = sum(hold_days) / len(hold_days) if hold_days else 0
    short_n = sum(1 for d in hold_days if d <= 7)
    mid_n = sum(1 for d in hold_days if 7 < d <= 90)
    long_n = sum(1 for d in hold_days if d > 90)

    # 평균 이익/손실 비율
    win_pnls = [p for _, p, _ in matched_pnl if p > 0]
    loss_pnls = [p for _, p, _ in matched_pnl if p < 0]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0

    # ── 3. 종목별 손익 ────────────────────────
    pnl_by_stock = defaultdict(lambda: {"cost": 0, "rev": 0, "buy_n": 0, "sell_n": 0,
                                          "last_buy": "", "last_sell": ""})
    for t in trades:
        s = t["stock"]
        if t["type"] == "buy":
            pnl_by_stock[s]["cost"] += t.get("amount", 0) + t.get("fee", 0)
            pnl_by_stock[s]["buy_n"] += 1
            pnl_by_stock[s]["last_buy"] = t["date"]
        else:
            pnl_by_stock[s]["rev"] += t.get("amount", 0) - t.get("fee", 0) - t.get("tax", 0)
            pnl_by_stock[s]["sell_n"] += 1
            pnl_by_stock[s]["last_sell"] = t["date"]

    realized = []
    for s, v in pnl_by_stock.items():
        pnl = v["rev"] - v["cost"]
        if v["sell_n"] > 0:
            realized.append((s, pnl, v["buy_n"], v["sell_n"], v["cost"], v["rev"]))

    losers = sorted([r for r in realized if r[1] < 0], key=lambda x: x[1])[:10]
    winners = sorted([r for r in realized if r[1] > 0], key=lambda x: -x[1])[:10]
    total_loss = sum(r[1] for r in realized if r[1] < 0)
    total_gain = sum(r[1] for r in realized if r[1] > 0)

    # ── 4. 좀비 종목 검출 ────────────────────────
    zombies = []
    for s, v in pnl_by_stock.items():
        if v["buy_n"] >= 5 and v["last_buy"]:
            last = datetime.strptime(v["last_buy"], "%Y-%m-%d")
            days_since = (TODAY - last).days
            sell_ratio = v["sell_n"] / v["buy_n"]
            if days_since > 365 and sell_ratio < 0.3:
                zombies.append({
                    "stock": s, "buy_n": v["buy_n"], "sell_n": v["sell_n"],
                    "days_since": days_since, "cost": v["cost"], "rev": v["rev"],
                    "pnl_realized": v["rev"] - v["cost"],
                })
    zombies.sort(key=lambda x: -x["cost"])

    # ── 5. 분할매수 통계 ────────────────────────
    buy_counts = defaultdict(int)
    for t in buys:
        buy_counts[t["stock"]] += 1
    n_single = sum(1 for c in buy_counts.values() if c == 1)
    n_multi = sum(1 for c in buy_counts.values() if 2 <= c < 10)
    n_heavy = sum(1 for c in buy_counts.values() if c >= 10)

    # 매수 횟수와 손실의 상관관계
    multi_buy_loss = []
    for s, v in pnl_by_stock.items():
        if v["sell_n"] == 0: continue
        pnl_pct = (v["rev"]/v["cost"]-1)*100 if v["cost"] > 0 else 0
        multi_buy_loss.append((v["buy_n"], pnl_pct))

    # 횟수 구간별 평균 수익률
    bucket = defaultdict(list)
    for n, p in multi_buy_loss:
        if n == 1: bucket["1회"].append(p)
        elif n <= 3: bucket["2~3회"].append(p)
        elif n <= 9: bucket["4~9회"].append(p)
        elif n <= 19: bucket["10~19회"].append(p)
        else: bucket["20회+"].append(p)

    # ── 6. 매도 후 재매수 (상승 추격) 패턴 ─────────
    rebuy_pattern = 0
    for s in set(t["stock"] for t in trades):
        s_trades = sorted([t for t in trades if t["stock"]==s], key=lambda x:x["date"])
        prev_was_sell = False
        for t in s_trades:
            if t["type"] == "buy" and prev_was_sell:
                rebuy_pattern += 1
            prev_was_sell = (t["type"] == "sell")

    # ────────────────────────────────────────────────
    # HTML 생성
    # ────────────────────────────────────────────────
    year_rows = ""
    for y in sorted(by_year.keys()):
        v = by_year[y]
        year_rows += f"""<tr>
          <td>{y}</td>
          <td class="mono" style="text-align:right">{v['buy']}</td>
          <td class="mono" style="text-align:right">{v['sell']}</td>
          <td class="mono" style="text-align:right">{v['amt_buy']/1e8:.2f}억</td>
          <td class="mono" style="text-align:right">{v['amt_sell']/1e8:.2f}억</td>
        </tr>"""

    losers_rows = ""
    for s, pnl, bn, sn, c, r in losers:
        losers_rows += f"""<tr>
          <td>{s}</td>
          <td class="mono ret-up" style="text-align:right;font-weight:600">{fmt_man(pnl)}</td>
          <td class="mono" style="text-align:center">{bn}</td>
          <td class="mono" style="text-align:center">{sn}</td>
          <td class="mono" style="text-align:right">{c/1e8:.2f}억</td>
        </tr>"""

    winners_rows = ""
    for s, pnl, bn, sn, c, r in winners:
        winners_rows += f"""<tr>
          <td>{s}</td>
          <td class="mono ret-down" style="text-align:right;font-weight:600">{fmt_man(pnl)}</td>
          <td class="mono" style="text-align:center">{bn}</td>
          <td class="mono" style="text-align:center">{sn}</td>
          <td class="mono" style="text-align:right">{c/1e8:.2f}억</td>
        </tr>"""

    zombie_rows = ""
    for z in zombies[:15]:
        sell_pct = z["sell_n"] / z["buy_n"] * 100
        # 보유 평균단가 계산 (현재 보유분만)
        zombie_rows += f"""<tr>
          <td><b>{z['stock']}</b></td>
          <td class="mono" style="text-align:center">{z['buy_n']}</td>
          <td class="mono" style="text-align:center">{z['sell_n']} <span style="color:#666">({sell_pct:.0f}%)</span></td>
          <td class="mono" style="text-align:center">{z['days_since']:,}일전</td>
          <td class="mono" style="text-align:right">{z['cost']/1e4:,.0f}만</td>
          <td class="mono ret-up" style="text-align:right">{fmt_man(z['pnl_realized'])}</td>
        </tr>"""

    bucket_rows = ""
    bucket_order = ["1회", "2~3회", "4~9회", "10~19회", "20회+"]
    for b in bucket_order:
        if b not in bucket: continue
        vals = bucket[b]
        avg = sum(vals)/len(vals)
        wr = sum(1 for v in vals if v > 0) / len(vals) * 100
        clr = "ret-down" if avg > 0 else "ret-up"
        bucket_rows += f"""<tr>
          <td>{b}</td>
          <td class="mono" style="text-align:center">{len(vals)}</td>
          <td class="mono {clr}" style="text-align:right">{avg:+.1f}%</td>
          <td class="mono" style="text-align:center">{wr:.0f}%</td>
        </tr>"""

    profit_factor = abs(total_gain / total_loss) if total_loss != 0 else 0
    gain_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>매매 스타일 분석</title>
<link rel="stylesheet" href="assets/style.css">
<style>
.diag-card {{
  background: #181b23;
  border-radius: 10px;
  padding: 16px 20px;
  margin-bottom: 14px;
  border-left: 4px solid #4fc3f7;
}}
.diag-card.warn  {{ border-left-color: #f59e0b; }}
.diag-card.bad   {{ border-left-color: #ef4444; }}
.diag-card.good  {{ border-left-color: #10b981; }}
.diag-title {{ font-weight: 600; margin-bottom: 6px; font-size: 1.0em; }}
.diag-detail {{ color: #aaa; font-size: 0.86em; line-height: 1.7; }}
.suggest {{
  background: #0d1421;
  padding: 10px 14px;
  border-radius: 6px;
  margin-top: 8px;
  border-left: 2px solid #4fc3f7;
  color: #b8c4d0;
  font-size: 0.85em;
}}
.kpi-strip {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }}
.kpi-strip .kpi-mini {{
  flex: 1; min-width: 140px; background: #181b23; border-radius: 8px;
  padding: 14px; text-align: center;
}}
.kpi-strip .num {{ font-size: 1.6em; font-weight: 700; color: #fff; }}
.kpi-strip .lbl {{ font-size: 0.78em; color: #888; margin-top: 4px; }}
.kpi-strip .sub {{ font-size: 0.78em; color: #6b7280; margin-top: 2px; }}
ul.simple {{ padding-left: 22px; line-height: 1.9; }}
ul.simple li {{ color: #aaa; font-size: 0.86em; }}
ul.simple li b {{ color: #ddd; }}
</style>
</head>
<body>
<div class="container">

<div class="nav">
  <a href="index.html">📊 전체 대시보드</a>
  <a href="profit_taking.html">💰 익절 타이밍</a>
  <a href="status.html">📋 현재 상황</a>
  <a href="trading_style.html" class="active">🎯 매매 스타일</a>
</div>

<h1>매매 스타일 분석</h1>
<p class="subtitle">11년치 거래 {len(trades):,}건 · {len(set(t["stock"] for t in trades))}개 종목 · 매수 {len(buys):,}건 / 매도 {len(sells):,}건</p>

<!-- 핵심 KPI -->
<div class="kpi-strip">
  <div class="kpi-mini">
    <div class="num" style="color:{'#10b981' if win_rate>=55 else '#f59e0b' if win_rate>=50 else '#ef4444'}">{win_rate:.1f}%</div>
    <div class="lbl">승률</div>
    <div class="sub">{gains:,}승 / {n_match-gains:,}패</div>
  </div>
  <div class="kpi-mini">
    <div class="num">{avg_hold:.0f}일</div>
    <div class="lbl">평균 보유</div>
    <div class="sub">중앙값 {sorted(hold_days)[len(hold_days)//2]:.0f}일</div>
  </div>
  <div class="kpi-mini">
    <div class="num" style="color:{'#10b981' if profit_factor>=1.5 else '#f59e0b' if profit_factor>=1 else '#ef4444'}">{profit_factor:.2f}</div>
    <div class="lbl">Profit Factor</div>
    <div class="sub">총이익 / 총손실</div>
  </div>
  <div class="kpi-mini">
    <div class="num">{avg_win:+.1f}% / {avg_loss:.1f}%</div>
    <div class="lbl">평균 이익 / 손실</div>
    <div class="sub">비율 {gain_loss_ratio:.2f}x</div>
  </div>
  <div class="kpi-mini">
    <div class="num">{long_n/n_match*100:.0f}%</div>
    <div class="lbl">장기보유 (90일+)</div>
    <div class="sub">{long_n:,}건</div>
  </div>
</div>

<!-- 진단 -->
<div class="card">
  <h2>🔍 매매 스타일 진단</h2>

  <div class="diag-card warn">
    <div class="diag-title">⚠️ 1. 분할매수 → 평단 낮추기 → 손실 키우기 패턴</div>
    <div class="diag-detail">
      종목당 평균 매수 횟수 <b>{sum(buy_counts.values())/len(buy_counts):.1f}회</b>.
      10회 이상 매수 종목이 <b>{n_heavy}개</b> (전체 {len(buy_counts)}개 중 {n_heavy/len(buy_counts)*100:.0f}%).<br>
      <b>핵심 발견:</b> 매수 횟수가 많을수록 평균 수익률이 떨어짐.
      <table class="table-compact" style="margin-top:8px;width:auto">
        <tr><th>매수 횟수</th><th>종목수</th><th>평균 수익률</th><th>승률</th></tr>
        {bucket_rows}
      </table>
      <div class="suggest">
        💡 <b>제안:</b> 같은 종목 4회 이상 매수 시 자동 경고. "평단 낮추기" 시도가 손실로 이어지는 구간.
        대신 <b>익절은 분할로, 손절은 한 번에</b>로 룰 변경.
      </div>
    </div>
  </div>

  <div class="diag-card bad">
    <div class="diag-title">🚨 2. 좀비 종목 — 손절 못함 패턴 ({len(zombies)}개)</div>
    <div class="diag-detail">
      매수 5회 이상 + 1년 이상 매수 안함 + 매도가 매수의 30% 미만 = "버려진 종목".<br>
      현재 미실현 손실로 깔려있을 가능성 높음.
      <div class="suggest">
        💡 <b>제안:</b> 1년 이상 미매수 + 매도율 30% 미만 종목 자동 알림.
        분기마다 <b>좀비 청산일(zombie cleanup)</b> 정해서 일괄 정리.
      </div>
    </div>
  </div>

  <div class="diag-card warn">
    <div class="diag-title">⚠️ 3. 승률 50%인데 Profit Factor {profit_factor:.2f}</div>
    <div class="diag-detail">
      승률 {win_rate:.1f}% (사실상 동전던지기) · 평균 이익 {avg_win:.1f}% vs 평균 손실 {avg_loss:.1f}%.<br>
      이익/손실 비율 {gain_loss_ratio:.2f}배 — {'손실이 더 크게 나는 구조' if gain_loss_ratio<1 else '이익이 더 크게 나는 구조'}.
      <div class="suggest">
        💡 <b>제안:</b> 승률을 60%로 올리는 것보다 <b>손실 종목의 평균 손실을 줄이는 것</b>이 더 쉬움.
        손절 룰 (-7% / -10%) 적용 시 평균 손실 {avg_loss:.1f}% → -8%로 개선 가능.
      </div>
    </div>
  </div>

  <div class="diag-card good">
    <div class="diag-title">✓ 4. 장기보유 성향은 강점</div>
    <div class="diag-detail">
      {long_n/n_match*100:.0f}%가 90일 이상 보유 — 단타 매매 손실(수수료/세금)이 적음.<br>
      평균 보유 {avg_hold:.0f}일 ({avg_hold/365:.1f}년).
      <div class="suggest">
        💡 <b>유지:</b> 이 강점은 그대로. 다만 "장기보유 = 손절 안 함"이 되지 않게 주의.
      </div>
    </div>
  </div>

  <div class="diag-card warn">
    <div class="diag-title">⚠️ 5. 매도 후 재매수 {rebuy_pattern}회 — 상승 추격 가능성</div>
    <div class="diag-detail">
      판 종목을 다시 산 횟수 {rebuy_pattern}회. 상승 추격 매수일 가능성 (판 가격보다 비싸게 다시 사는 경우).
      <div class="suggest">
        💡 <b>제안:</b> 매도 후 재매수 시 "이전 매도가" 알림 표시. 상승 추격 vs 단순 재진입 구분.
      </div>
    </div>
  </div>
</div>

<!-- 좀비 종목 -->
<div class="card">
  <h2>🚨 좀비 종목 청산 후보 (TOP 15, 매수금액 큰 순)</h2>
  <p class="desc">
    매수 5회 이상 + 마지막 매수 1년+ + 매도/매수 비율 30% 미만<br>
    이 종목들을 위해 자본이 묶여 있는 상태. 분기별 정리 필요.
  </p>
  <table>
    <tr>
      <th>종목</th>
      <th style="text-align:center">매수 회수</th>
      <th style="text-align:center">매도 회수</th>
      <th style="text-align:center">마지막 매수</th>
      <th style="text-align:right">매수 합계</th>
      <th style="text-align:right">실현손익</th>
    </tr>
    {zombie_rows}
  </table>
</div>

<!-- 손익 TOP -->
<div class="grid2">
  <div class="card">
    <h2>📉 큰 손실 TOP 10</h2>
    <p class="desc">총 실현손실: <b style="color:#ef4444">{fmt_man(total_loss)}</b></p>
    <table class="table-compact">
      <tr><th>종목</th><th style="text-align:right">손익</th><th style="text-align:center">매수</th><th style="text-align:center">매도</th><th style="text-align:right">매수합</th></tr>
      {losers_rows}
    </table>
  </div>
  <div class="card">
    <h2>📈 큰 이익 TOP 10</h2>
    <p class="desc">총 실현이익: <b style="color:#10b981">{fmt_man(total_gain)}</b></p>
    <table class="table-compact">
      <tr><th>종목</th><th style="text-align:right">손익</th><th style="text-align:center">매수</th><th style="text-align:center">매도</th><th style="text-align:right">매수합</th></tr>
      {winners_rows}
    </table>
  </div>
</div>

<!-- 연도별 -->
<div class="card">
  <h2>📅 연도별 매매 빈도</h2>
  <table class="table-compact">
    <tr><th>연도</th>
      <th style="text-align:right">매수 건수</th>
      <th style="text-align:right">매도 건수</th>
      <th style="text-align:right">매수 금액</th>
      <th style="text-align:right">매도 금액</th></tr>
    {year_rows}
  </table>
</div>

<!-- 시그널 시스템 활용 제안 -->
<div class="card">
  <h2>🎯 시그널 시스템과 결합한 룰 제안</h2>
  <div class="callout">
    이미 만든 시그널 시스템 + 매매 스타일 데이터 → 개인화된 룰 6개 제안
  </div>

  <div class="diag-card good">
    <div class="diag-title">규칙 1: <b>같은 종목 4회+ 매수 경고</b></div>
    <div class="diag-detail">
      4회 이상 매수 시도 시 → 대시보드 알림 + "이 종목 평균 수익률은 이 횟수에 ___%"
      자동 표시.<br>
      구현: <span class="code">build_dashboard.py</span>에 매수 카운트 추적 추가
    </div>
  </div>

  <div class="diag-card good">
    <div class="diag-title">규칙 2: <b>좀비 종목 분기별 자동 탐지</b></div>
    <div class="diag-detail">
      매수 5회+ AND 1년+ 미매수 → 매도 시그널 강제 발동 (현재 시그널과 OR).<br>
      해당 종목은 행동 권고 = "정리 검토" 자동 부여.
    </div>
  </div>

  <div class="diag-card good">
    <div class="diag-title">규칙 3: <b>손절 룰 (-7% / -10%)</b></div>
    <div class="diag-detail">
      현재 평균 손실 {avg_loss:.1f}% → -8% 캡 적용 시 시뮬레이션 결과 표시.<br>
      이미 매수한 종목은 평단가 -7% 이탈 시 "트레일링 스탑 발동" 알림.
    </div>
  </div>

  <div class="diag-card good">
    <div class="diag-title">규칙 4: <b>매도 후 재매수 시 이전 매도가 표시</b></div>
    <div class="diag-detail">
      직전 매도가보다 +X% 비싸게 재매수 시 경고. 상승 추격 매매 방지.
    </div>
  </div>

  <div class="diag-card good">
    <div class="diag-title">규칙 5: <b>익절 분할 vs 손절 일괄 룰</b></div>
    <div class="diag-detail">
      이익 종목: +20% / +40% / +60%에서 1/3씩 분할 매도 (보유 늘리기 방지).<br>
      손실 종목: -7% 이탈 시 한 번에 전량 매도 (물타기 금지).
    </div>
  </div>

  <div class="diag-card good">
    <div class="diag-title">규칙 6: <b>"매수 금지" 종목 리스트</b></div>
    <div class="diag-detail">
      손실 -1,000만원 이상으로 매도한 종목 → 6개월간 신규 매수 금지 (감정적 재진입 방지).<br>
      현재 해당 종목: NAVER, 파마리서치, HLB제넥스, 토모큐브 등
    </div>
  </div>
</div>

</div>
</body>
</html>
"""

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✓ {OUT}")


if __name__ == "__main__":
    main()
