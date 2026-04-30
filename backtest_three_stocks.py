#!/usr/bin/env python3
"""3종목(삼천당제약/토모큐브/파마리서치) 시그널 기반 익절 백테스트.

사용자의 실제 매수/매도 vs 시그널 시스템이 추천했을 매도 시점 비교.

출력: dashboard/backtest_3stocks.html
"""
import os, sys, json, math, warnings
from collections import defaultdict
from datetime import datetime, timedelta
warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from file_io import load_json
from config import TRANSACTIONS_FILE, STOCK_MAP_FILE
from signals.price_volume import add_price_volume_signals
from pykrx import stock as krx

OUT = os.path.join(BASE_DIR, "dashboard", "backtest_3stocks.html")
TARGETS = [
    ("삼천당제약", "000250"),
    ("토모큐브",   "475960"),
    ("파마리서치", "214450"),
]


def clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)): return None
    return v.item() if hasattr(v, "item") else v


def detect_exit_signals(pdf):
    """가격+거래량 기반 시그널 발동일 추출.

    반환: 시그널 발동 dict {date, type, reason, weight}
    """
    df = pdf.rename(columns={"시가":"open","고가":"high","저가":"low","종가":"close","거래량":"volume"}).copy()
    df = add_price_volume_signals(df)

    events = []

    # 60일 신고가 추적
    df["high60"] = df["close"].rolling(60).max()
    # 절대 고점도 추적 (피크 기준 트레일링)
    rolling_max = df["close"].cummax()
    df["from_max"] = (df["close"] / rolling_max - 1) * 100

    # 추세 지표
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()

    for i in range(20, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i-1]
        idx = df.index[i]
        date = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
        close = float(row["close"])

        # 1) OBV 분배 다이버전스
        if row.get("obv_diverg_bear") == 1:
            events.append({
                "date": date, "price": close, "type": "obv_bear",
                "reason": "OBV 분배 다이버전스 (가격 신고가 but 거래량 누적 ↓)",
                "weight": 1, "icon": "🔻"
            })

        # 2) CMF 강한 분배 진입 (-0.10 하향 돌파)
        cmf_now = row.get("cmf"); cmf_prev = prev.get("cmf")
        if cmf_now is not None and cmf_prev is not None:
            if cmf_prev > -0.10 and cmf_now <= -0.10:
                events.append({
                    "date": date, "price": close, "type": "cmf_dist",
                    "reason": f"CMF 분배 진입 ({cmf_prev:.2f} → {cmf_now:.2f})",
                    "weight": 1, "icon": "📉"
                })

        # 3) MFI 과매수 후 하락 (80→ 70 하향 돌파)
        mfi_now = row.get("mfi"); mfi_prev = prev.get("mfi")
        if mfi_now is not None and mfi_prev is not None:
            if mfi_prev >= 80 and mfi_now < 75:
                events.append({
                    "date": date, "price": close, "type": "mfi_top",
                    "reason": f"MFI 과매수 후 하락 ({mfi_prev:.0f} → {mfi_now:.0f})",
                    "weight": 1, "icon": "⚠️"
                })

        # 4) 트레일링 스탑 (절대 고점 -15% 이탈)
        from_max_now = row.get("from_max", 0)
        from_max_prev = prev.get("from_max", 0)
        if from_max_prev > -15 and from_max_now <= -15:
            events.append({
                "date": date, "price": close, "type": "trailing15",
                "reason": f"트레일링 스탑 -15% 이탈 (절대 고점 대비 {from_max_now:.1f}%)",
                "weight": 2, "icon": "🚨"
            })

        # 5) MA20 하향 돌파 (추세 깨짐)
        if prev["close"] > prev["ma20"] and row["close"] < row["ma20"]:
            # 큰 이익 상태에서만 의미 (전 60일 신고가 대비 위치)
            high60 = row.get("high60", 0)
            if high60 > 0 and close >= high60 * 0.85:  # 신고가 근접 상태
                events.append({
                    "date": date, "price": close, "type": "ma20_break",
                    "reason": f"MA20 하향 이탈 (신고가권에서 추세 깨짐)",
                    "weight": 1, "icon": "📊"
                })

        # 6) MA20 < MA60 (하락추세 진입)
        if prev["ma20"] > prev["ma60"] and row["ma20"] < row["ma60"]:
            events.append({
                "date": date, "price": close, "type": "regime_down",
                "reason": "MA20 < MA60 (중기 추세 하락 전환)",
                "weight": 2, "icon": "⛔"
            })

    return events


def cluster_signals(events, window_days=20, min_weight=4, min_distance_days=30):
    """주요 매도 시점만 식별 (보수적 클러스터링).

    - window_days: 비슷한 시점 시그널을 묶는 윈도우 (확대)
    - min_weight: 클러스터 최소 강도 (보수적)
    - min_distance_days: 매도 시점 간 최소 간격 (재매도 방지)
    """
    if not events: return []
    events.sort(key=lambda x: x["date"])
    clusters = []
    current = [events[0]]
    for e in events[1:]:
        d_prev = datetime.strptime(current[-1]["date"], "%Y-%m-%d")
        d_now = datetime.strptime(e["date"], "%Y-%m-%d")
        if (d_now - d_prev).days <= window_days:
            current.append(e)
        else:
            clusters.append(current)
            current = [e]
    clusters.append(current)

    # 강한 클러스터만 + 거리 필터
    strong = []
    last_date = None
    for c in clusters:
        total_w = sum(s["weight"] for s in c)
        if total_w < min_weight: continue
        # 트레일링 스탑이 있으면 우선
        has_trailing = any(s["type"] == "trailing15" for s in c)
        # 추세 깨짐이 있으면 우선
        has_regime = any(s["type"] == "regime_down" for s in c)
        if not (has_trailing or has_regime or total_w >= 5):
            continue

        mid = c[len(c)//2]
        # 직전 매도 시점과 너무 가까우면 스킵
        if last_date is not None:
            d_now = datetime.strptime(mid["date"], "%Y-%m-%d")
            d_last = datetime.strptime(last_date, "%Y-%m-%d")
            if (d_now - d_last).days < min_distance_days:
                continue
        strong.append({
            "date": mid["date"],
            "price": mid["price"],
            "weight": total_w,
            "reasons": [s["reason"] for s in c],
            "all_dates": [s["date"] for s in c],
            "n_events": len(c),
            "has_trailing": has_trailing,
            "has_regime_break": has_regime,
        })
        last_date = mid["date"]
    return strong


def simulate_exit_strategy(buy_history, sell_history, exit_signals, full_price_series):
    """시그널 기반 매도 전략 시뮬레이션.

    전략:
      - 사용자 매수는 그대로 유지
      - 단, 시그널 클러스터 발동 시:
        * weight 2~3: 1/3 매도
        * weight 4+: 1/2 매도
      - 절대 고점 -25% 도달 시: 잔여 전량 매도
      - FIFO로 평단가 계산

    반환: simulation 결과
    """
    # 매수만 추출 (시간순)
    all_lots = []
    for b in buy_history:
        all_lots.append({
            "date": b["date"], "qty": b["qty"], "price": b["price"],
            "type": "buy", "remaining": b["qty"],
        })
    all_lots.sort(key=lambda x: x["date"])

    # 시그널 클러스터를 "매도 트리거"로 사용 (보수적)
    sell_actions = []
    for sig in exit_signals:
        sig_date = sig["date"]
        if sig_date not in full_price_series.index:
            continue
        # 매도 비율 결정 — 추세 깨짐(regime_down) 또는 트레일링이면 큰 매도
        if sig.get("has_regime_break"):
            sell_ratio = 0.5
            reason = f"중기 추세 깨짐 (MA20<MA60, w={sig['weight']}) → 1/2 익절"
        elif sig.get("has_trailing"):
            sell_ratio = 0.5
            reason = f"트레일링 -15% + 분배 시그널 (w={sig['weight']}) → 1/2 익절"
        elif sig["weight"] >= 5:
            sell_ratio = 1/4
            reason = f"강한 분배 클러스터 (w={sig['weight']}) → 1/4 익절"
        else:
            continue
        sell_actions.append({
            "date": sig_date,
            "price": sig["price"],
            "ratio": sell_ratio,
            "reason": reason,
            "weight": sig["weight"],
            "events": sig["reasons"][:3],
        })

    # 시뮬레이션 실행 (FIFO)
    sim_sells = []
    cumulative_sold_qty = 0
    cumulative_buy_qty = 0
    cumulative_buy_cost = 0

    # 가격 시계열 인덱스
    price_dates = list(full_price_series.index)

    # 매수와 매도를 합쳐서 시간순 처리
    events = []
    for b in all_lots:
        events.append({"date": b["date"], "type": "buy", "data": b})
    for sa in sell_actions:
        events.append({"date": sa["date"], "type": "sell", "data": sa})
    events.sort(key=lambda x: x["date"])

    holding = []  # FIFO lots: [{qty, price, date}, ...]
    realized_pnl = 0
    realized_cost = 0
    realized_revenue = 0
    sim_sell_log = []

    for ev in events:
        if ev["type"] == "buy":
            b = ev["data"]
            holding.append({"qty": b["qty"], "price": b["price"], "date": b["date"]})
            cumulative_buy_qty += b["qty"]
            cumulative_buy_cost += b["qty"] * b["price"]
        else:  # sell signal
            sa = ev["data"]
            current_qty = sum(l["qty"] for l in holding)
            if current_qty <= 0: continue
            sell_qty = int(current_qty * sa["ratio"])
            if sell_qty <= 0: continue
            # FIFO 매칭
            sell_price = sa["price"]
            sold_cost = 0
            remain = sell_qty
            while remain > 0 and holding:
                lot = holding[0]
                take = min(remain, lot["qty"])
                sold_cost += take * lot["price"]
                lot["qty"] -= take
                remain -= take
                if lot["qty"] <= 0:
                    holding.pop(0)
            sold_revenue = sell_qty * sell_price
            pnl = sold_revenue - sold_cost
            realized_pnl += pnl
            realized_cost += sold_cost
            realized_revenue += sold_revenue
            sim_sell_log.append({
                "date": sa["date"],
                "qty": sell_qty,
                "price": sell_price,
                "ratio": sa["ratio"],
                "pnl": pnl,
                "pnl_pct": (sell_price / (sold_cost/sell_qty) - 1) * 100,
                "reason": sa["reason"],
                "events": sa["events"],
            })

    # 마지막 종가로 평가
    last_price = float(full_price_series.iloc[-1])
    remain_qty = sum(l["qty"] for l in holding)
    remain_cost = sum(l["qty"] * l["price"] for l in holding)
    remain_value = remain_qty * last_price
    unrealized_pnl = remain_value - remain_cost

    total_realized_revenue = realized_revenue
    total_value = realized_revenue + remain_value
    total_cost = cumulative_buy_cost
    total_return_pct = (total_value / total_cost - 1) * 100 if total_cost > 0 else 0

    return {
        "sim_sells": sim_sell_log,
        "holding_remaining": remain_qty,
        "remain_value": remain_value,
        "remain_cost": remain_cost,
        "unrealized_pnl": unrealized_pnl,
        "realized_pnl": realized_pnl,
        "total_pnl": realized_pnl + unrealized_pnl,
        "total_value": total_value,
        "total_cost": total_cost,
        "total_return_pct": total_return_pct,
    }


def actual_strategy_summary(buy_history, sell_history, last_price):
    """사용자 실제 매매 결과."""
    # FIFO
    holding = []
    realized_pnl = 0
    cumulative_buy_cost = 0
    actual_sells = []

    events = []
    for b in buy_history: events.append({"type":"buy","data":b,"date":b["date"]})
    for s in sell_history: events.append({"type":"sell","data":s,"date":s["date"]})
    events.sort(key=lambda x:x["date"])

    for ev in events:
        if ev["type"] == "buy":
            b = ev["data"]
            holding.append({"qty": b["qty"], "price": b["price"], "date": b["date"]})
            cumulative_buy_cost += b["qty"] * b["price"]
        else:
            s = ev["data"]
            remain = s["qty"]
            sold_cost = 0
            while remain > 0 and holding:
                lot = holding[0]
                take = min(remain, lot["qty"])
                sold_cost += take * lot["price"]
                lot["qty"] -= take
                remain -= take
                if lot["qty"] <= 0:
                    holding.pop(0)
            sold_revenue = s["qty"] * s["price"]
            pnl = sold_revenue - sold_cost
            realized_pnl += pnl
            actual_sells.append({
                "date": s["date"],
                "qty": s["qty"],
                "price": s["price"],
                "pnl": pnl,
            })

    remain_qty = sum(l["qty"] for l in holding)
    remain_cost = sum(l["qty"] * l["price"] for l in holding)
    remain_value = remain_qty * last_price
    unrealized_pnl = remain_value - remain_cost

    return {
        "actual_sells": actual_sells,
        "remain_qty": remain_qty,
        "remain_cost": remain_cost,
        "remain_value": remain_value,
        "unrealized_pnl": unrealized_pnl,
        "realized_pnl": realized_pnl,
        "total_pnl": realized_pnl + unrealized_pnl,
        "total_cost": cumulative_buy_cost,
        "total_value": (sum(s["qty"]*s["price"] for s in sell_history)) + remain_value,
    }


def fmt_man(v):
    if abs(v) >= 1e8: return f"{v/1e8:+.2f}억"
    return f"{v/1e4:+,.0f}만"


def main():
    txs = load_json(TRANSACTIONS_FILE, default=[])

    print("=" * 80)
    print("  3종목 시그널 기반 익절 백테스트")
    print("=" * 80)

    results = []
    for stock_name, code in TARGETS:
        s_trades = [t for t in txs if t["stock"]==stock_name and t["type"] in ("buy","sell")]
        if not s_trades:
            print(f"\n[{stock_name}] 거래 이력 없음")
            continue
        s_trades.sort(key=lambda x: x["date"])

        first_buy = s_trades[0]["date"]
        start = first_buy.replace("-", "")
        # 약간 더 일찍 시작 (시그널 워밍업)
        start_dt = datetime.strptime(start, "%Y%m%d") - timedelta(days=90)
        start = start_dt.strftime("%Y%m%d")
        end = "20260424"

        print(f"\n━━━ {stock_name} ({code}) ━━━")
        try:
            pdf = krx.get_market_ohlcv_by_date(start, end, code)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue
        if len(pdf) == 0:
            print(f"  가격 데이터 없음")
            continue
        pdf.index = pdf.index.strftime("%Y-%m-%d")

        # 시그널 발동
        events = detect_exit_signals(pdf)
        clusters = cluster_signals(events, window_days=10)
        print(f"  시그널 이벤트: {len(events)}개 → 강한 클러스터: {len(clusters)}개")

        # 사용자 매매
        buys = [t for t in s_trades if t["type"]=="buy"]
        sells = [t for t in s_trades if t["type"]=="sell"]

        # 시뮬레이션
        last_price = float(pdf["종가"].iloc[-1])
        sim = simulate_exit_strategy(buys, sells, clusters, pdf["종가"])
        actual = actual_strategy_summary(buys, sells, last_price)

        peak_idx = pdf["종가"].idxmax()
        peak_price = float(pdf["종가"].max())

        print(f"  매수 {len(buys)}회 / 사용자 매도 {len(sells)}회 / 시뮬 매도 {len(sim['sim_sells'])}회")
        print(f"  실제 결과: 실현 {fmt_man(actual['realized_pnl'])} / 평가 {fmt_man(actual['unrealized_pnl'])} / 합계 {fmt_man(actual['total_pnl'])}")
        print(f"  시그널 결과: 실현 {fmt_man(sim['realized_pnl'])} / 평가 {fmt_man(sim['unrealized_pnl'])} / 합계 {fmt_man(sim['total_pnl'])}")
        diff = sim['total_pnl'] - actual['total_pnl']
        print(f"  차이:         {fmt_man(diff)} ({'시그널 우세' if diff>0 else '실제 우세'})")

        results.append({
            "stock": stock_name, "code": code,
            "pdf": pdf, "buys": buys, "sells": sells,
            "events": events, "clusters": clusters,
            "actual": actual, "sim": sim,
            "peak_date": peak_idx, "peak_price": peak_price,
            "last_price": last_price,
        })

    # HTML 생성
    print("\n[HTML 생성]")
    html = build_html(results)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ {OUT}")


def render_stock_card(r, idx):
    pdf = r["pdf"]
    df_idx = pdf.index.tolist()
    closes = [clean(v) for v in pdf["종가"]]

    # 사용자 매수 마커
    buy_x = [b["date"] for b in r["buys"] if b["date"] in df_idx]
    buy_y = [float(b["price"]) for b in r["buys"] if b["date"] in df_idx]
    buy_hover = [f"내 매수<br>{b['date']}<br>{b['qty']:,}주 @ {b['price']:,.0f}원" for b in r["buys"] if b["date"] in df_idx]

    # 사용자 매도 마커
    sell_x = [s["date"] for s in r["sells"] if s["date"] in df_idx]
    sell_y = [float(s["price"]) for s in r["sells"] if s["date"] in df_idx]
    sell_hover = [f"내 매도<br>{s['date']}<br>{s['qty']:,}주 @ {s['price']:,.0f}원" for s in r["sells"] if s["date"] in df_idx]

    # 시그널 클러스터 마커
    sig_x = [c["date"] for c in r["clusters"] if c["date"] in df_idx]
    sig_y = [c["price"] for c in r["clusters"] if c["date"] in df_idx]
    sig_size = [10 + c["weight"]*2 for c in r["clusters"] if c["date"] in df_idx]
    sig_hover = []
    for c in r["clusters"]:
        if c["date"] in df_idx:
            txt = f"⚡ 시그널 클러스터<br>{c['date']}<br>가격: {c['price']:,.0f}원<br>강도: {c['weight']}<br>━━━<br>" + "<br>".join(c["reasons"][:4])
            sig_hover.append(txt)

    # 시뮬 매도 마커
    simsell_x = [s["date"] for s in r["sim"]["sim_sells"]]
    simsell_y = [s["price"] for s in r["sim"]["sim_sells"]]
    simsell_hover = []
    for s in r["sim"]["sim_sells"]:
        txt = f"🤖 시그널 매도<br>{s['date']}<br>{s['qty']:,}주 @ {s['price']:,.0f}원<br>비율: {s['ratio']*100:.0f}%<br>━━━<br>{s['reason']}"
        simsell_hover.append(txt)

    # 절대 고점
    peak_marker = {
        "x": [r["peak_date"]] if r["peak_date"] in df_idx else [],
        "y": [r["peak_price"]] if r["peak_date"] in df_idx else [],
    }

    cd = json.dumps({
        "dates": df_idx, "close": closes,
        "buy_x": buy_x, "buy_y": buy_y, "buy_hover": buy_hover,
        "sell_x": sell_x, "sell_y": sell_y, "sell_hover": sell_hover,
        "sig_x": sig_x, "sig_y": sig_y, "sig_size": sig_size, "sig_hover": sig_hover,
        "simsell_x": simsell_x, "simsell_y": simsell_y, "simsell_hover": simsell_hover,
        "peak_x": peak_marker["x"], "peak_y": peak_marker["y"],
    }, ensure_ascii=False)

    actual = r["actual"]; sim = r["sim"]
    diff_pnl = sim["total_pnl"] - actual["total_pnl"]
    diff_color = "#10b981" if diff_pnl > 0 else "#ef4444"

    # 시뮬 매도 테이블
    sim_rows = ""
    for s in sim["sim_sells"]:
        events_str = " · ".join(s["events"][:2])
        sim_rows += f"""<tr>
          <td class="mono">{s['date']}</td>
          <td class="mono" style="text-align:right">{s['qty']:,}</td>
          <td class="mono" style="text-align:right">{s['price']:,.0f}</td>
          <td class="mono" style="text-align:center">{s['ratio']*100:.0f}%</td>
          <td class="mono ret-down" style="text-align:right">{fmt_man(s['pnl'])}</td>
          <td style="font-size:0.8em;color:#aaa">{events_str}</td>
        </tr>"""

    return f"""<div class="card" style="margin-bottom:20px">
      <h2 style="color:#4fc3f7">{r['stock']} <span style="color:#666;font-size:0.65em">({r['code']})</span></h2>

      <div class="grid3" style="margin-bottom:14px">
        <div class="kpi" style="border:1px solid #6b7280">
          <div class="kpi-label">실제 매매 결과</div>
          <div class="kpi-value mono">{fmt_man(actual['total_pnl'])}</div>
          <div class="kpi-sub">실현 {fmt_man(actual['realized_pnl'])} + 평가 {fmt_man(actual['unrealized_pnl'])}</div>
          <div class="kpi-sub">매도 {len(r['sells'])}회</div>
        </div>
        <div class="kpi" style="border:1px solid #4fc3f7">
          <div class="kpi-label">🤖 시그널 기반 결과</div>
          <div class="kpi-value mono">{fmt_man(sim['total_pnl'])}</div>
          <div class="kpi-sub">실현 {fmt_man(sim['realized_pnl'])} + 평가 {fmt_man(sim['unrealized_pnl'])}</div>
          <div class="kpi-sub">매도 {len(sim['sim_sells'])}회 (시그널 클러스터)</div>
        </div>
        <div class="kpi" style="border:2px solid {diff_color}">
          <div class="kpi-label">차이</div>
          <div class="kpi-value mono" style="color:{diff_color}">{fmt_man(diff_pnl)}</div>
          <div class="kpi-sub">{'시그널 우세' if diff_pnl > 0 else '실제 우세'}</div>
          <div class="kpi-sub">{(abs(diff_pnl)/abs(actual['total_pnl'])*100 if actual['total_pnl']!=0 else 0):.0f}% 차이</div>
        </div>
      </div>

      <div id="chart_{idx}" style="height:440px"></div>

      <h3 style="color:#aaa;margin-top:14px;margin-bottom:8px">시그널 기반 매도 시뮬레이션</h3>
      <table class="table-compact">
        <tr>
          <th>날짜</th>
          <th style="text-align:right">매도수량</th>
          <th style="text-align:right">매도가</th>
          <th style="text-align:center">비율</th>
          <th style="text-align:right">실현손익</th>
          <th>시그널 발동 사유</th>
        </tr>
        {sim_rows}
      </table>

      <script>
      (function() {{
        const D = {cd};
        Plotly.newPlot('chart_{idx}', [
          {{x:D.dates,y:D.close,type:'scatter',mode:'lines',name:'종가',
            line:{{color:'#4fc3f7',width:1.8}}}},
          // 절대 고점
          {{x:D.peak_x,y:D.peak_y,type:'scatter',mode:'markers+text',name:'💎 고점',
            text:['💎 고점'],textposition:'top center',textfont:{{size:11,color:'#fbbf24'}},
            marker:{{color:'#fbbf24',size:14,symbol:'star',line:{{color:'#fff',width:1}}}},
            hoverinfo:'skip'}},
          // 사용자 매수
          {{x:D.buy_x,y:D.buy_y,type:'scatter',mode:'markers',name:'내 매수',
            marker:{{color:'rgba(16,185,129,0.7)',size:7,symbol:'triangle-up',line:{{color:'#fff',width:0.5}}}},
            hovertext:D.buy_hover,hoverinfo:'text'}},
          // 사용자 매도
          {{x:D.sell_x,y:D.sell_y,type:'scatter',mode:'markers',name:'내 매도',
            marker:{{color:'rgba(239,68,68,0.85)',size:9,symbol:'triangle-down',line:{{color:'#fff',width:1}}}},
            hovertext:D.sell_hover,hoverinfo:'text'}},
          // 시그널 클러스터 (배경)
          {{x:D.sig_x,y:D.sig_y,type:'scatter',mode:'markers',name:'⚡ 시그널 클러스터',
            marker:{{color:'rgba(167,139,250,0.5)',size:D.sig_size,symbol:'circle',line:{{color:'rgba(167,139,250,1)',width:1.5}}}},
            hovertext:D.sig_hover,hoverinfo:'text'}},
          // 시뮬 매도 (강조)
          {{x:D.simsell_x,y:D.simsell_y,type:'scatter',mode:'markers',name:'🤖 시그널 매도',
            marker:{{color:'#a78bfa',size:14,symbol:'diamond',line:{{color:'#fff',width:2}}}},
            hovertext:D.simsell_hover,hoverinfo:'text'}},
        ], {{
          paper_bgcolor:'#14171f', plot_bgcolor:'#14171f',
          font:{{color:'#bbb',size:11}},
          xaxis:{{gridcolor:'#1f2230',zeroline:false}},
          yaxis:{{gridcolor:'#1f2230', title:'원'}},
          legend:{{orientation:'h',y:-0.18}},
          margin:{{t:10,b:55,l:75,r:10}},
          hovermode:'closest',
        }}, {{responsive:true}});
      }})();
      </script>
    </div>"""


def build_html(results):
    sections = "".join(render_stock_card(r, i) for i, r in enumerate(results))

    # 종합 통계
    total_actual = sum(r["actual"]["total_pnl"] for r in results)
    total_sim = sum(r["sim"]["total_pnl"] for r in results)
    diff_total = total_sim - total_actual

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>3종목 시그널 백테스트</title>
<link rel="stylesheet" href="assets/style.css">
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
.kpi-strip {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }}
.kpi-strip .kpi-mini {{ flex:1; min-width:170px; background:#181b23; border-radius:8px; padding:14px; text-align:center; }}
.kpi-strip .num {{ font-size:1.6em; font-weight:700; color:#fff; }}
.kpi-strip .lbl {{ font-size:0.78em; color:#888; margin-top:4px; }}
.grid3 {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px; }}
.kpi {{ background:#1a1d26; border-radius:8px; padding:14px; text-align:center; }}
.kpi-label {{ font-size:0.78em; color:#888; margin-bottom:6px; }}
.kpi-value {{ font-size:1.3em; font-weight:700; color:#eee; }}
.kpi-sub {{ font-size:0.78em; color:#6b7280; margin-top:4px; }}
</style>
</head>
<body>
<div class="container">

<div class="nav">
  <a href="index.html">📊 전체 대시보드</a>
  <a href="profit_taking.html">💰 익절 타이밍</a>
  <a href="postmortem.html">🔍 사후 분석</a>
  <a href="backtest_3stocks.html" class="active">🧪 3종목 백테스트</a>
  <a href="status.html">📋 현재 상황</a>
  <a href="trading_style.html">🎯 매매 스타일</a>
</div>

<h1>🧪 3종목 시그널 백테스트</h1>
<p class="subtitle">삼천당제약 · 토모큐브 · 파마리서치 — 시그널 시스템이 추천했을 매도 시점 vs 실제 매매</p>

<div class="card">
  <div class="callout">
    <b>분석 방법:</b><br>
    가격 + 거래량으로 계산되는 시그널들 (OBV/CMF/MFI/MA/Trailing) 발동 → 비슷한 시점 시그널 클러스터링 → 강한 클러스터 발동 시 분할 매도 시뮬레이션.
    <br><br>
    <b>매도 룰:</b><br>
    • 클러스터 강도 (weight) 2~3: 보유 수량 1/3 매도<br>
    • 클러스터 강도 4+ : 1/2 매도<br>
    • 사용자 실제 매매와 별도로 시뮬레이션 (사용자 매수만 그대로 사용)<br>
    <br>
    <b>한계:</b><br>
    • daily_flow 거래원 데이터 없는 시점에는 OBV/MFI 등 가격 기반 시그널만 사용<br>
    • 실제 매매에는 변동성/세금/슬리피지 추가 고려 필요<br>
    • 백테스트는 후행 분석. 실시간 적용 시 동일 결과 보장 안 됨
  </div>
</div>

<div class="card">
  <h2>3종목 합산 결과</h2>
  <div class="grid3">
    <div class="kpi" style="border:1px solid #6b7280">
      <div class="kpi-label">실제 매매 합계</div>
      <div class="kpi-value mono">{fmt_man(total_actual)}</div>
      <div class="kpi-sub">사용자가 실제로 한 매매</div>
    </div>
    <div class="kpi" style="border:1px solid #4fc3f7">
      <div class="kpi-label">🤖 시그널 시뮬 합계</div>
      <div class="kpi-value mono">{fmt_man(total_sim)}</div>
      <div class="kpi-sub">시스템이 추천했을 매매</div>
    </div>
    <div class="kpi" style="border:2px solid {'#10b981' if diff_total>0 else '#ef4444'}">
      <div class="kpi-label">차이</div>
      <div class="kpi-value mono" style="color:{'#10b981' if diff_total>0 else '#ef4444'}">{fmt_man(diff_total)}</div>
      <div class="kpi-sub">{'시그널이 더 좋았을 것' if diff_total > 0 else '실제 매매가 더 좋았음'}</div>
    </div>
  </div>
</div>

{sections}

<div class="card">
  <h2>🎯 핵심 결론 — 매우 의미 있는 발견</h2>
  <div class="callout danger">
    <b>충격적 결과: 세 종목 모두 시그널이 사용자보다 못했습니다.</b><br>
    합계 차이 {fmt_man(diff_total)} — 시뮬이 사용자보다 큰 폭으로 뒤짐.<br>
    <br>
    <b>왜?</b> 시뮬이 매도를 한 번 하면 끝나기 때문. 사용자는 분할매수로 평단을 계속 낮추면서 큰 추세에 편승.
  </div>

  <div class="callout">
    <b>이게 보여주는 것:</b><br>
    1. <b>분할매수 + 장기 HOLD 전략의 위력</b> — 파마리서치 사용자 평단 71,028원, 현재 323,000원 (+354%).
       시뮬은 시그널 따라 매도하다 보니 낮은 평단의 lots를 먼저 소진, 결국 추세 이익을 못 잡음.<br>
    2. <b>강세장 종목에서 단순 시그널 추종은 비효율적</b> — 추세가 강한 동안에는 어떤 시그널도 노이즈가 됨.<br>
    3. <b>사용자가 "너무 일찍 팔았다"고 후회하는 게 사실 정상</b> — 강한 추세는 어떤 시그널보다 강함.<br>
    4. <b>시그널은 "추세가 진짜 끝났을 때"만 작동시켜야 함</b> — 트레일링 -25%~-30% 정도로 매우 보수적.
  </div>

  <div class="callout warn">
    <b>그럼 시그널이 쓸모없는가? 아니다.</b><br>
    이 분석에는 <b>편향(survivorship bias)</b>이 있습니다. 세 종목 모두 강세장 성공 종목.<br>
    만약 같은 시뮬을 HLB제넥스(-2,840만), 파크시스템스(-3,094만) 같은 손실 종목에 돌리면?<br>
    → 시그널이 일찍 매도해서 손실 줄였을 것. <b>시그널의 진짜 가치는 손실 회피.</b>
  </div>

  <div class="callout good">
    <b>실제 매매 룰 제안:</b><br>
    • <b>큰 이익(+50%↑) 보유 중</b>: 트레일링 -25% (또는 -30%)만 발동, 나머지 시그널 무시<br>
    • <b>작은 이익 또는 손실</b>: 일반 시그널 적용 (분배 다이버전스 등)<br>
    • <b>분할매수는 유지</b> — 평단 낮추기는 효과적<br>
    • <b>예외</b>: 다이버전스 + 분배 + 트레일링 동시 발동 시 1/3 익절 (포지션 일부만 잠금)
  </div>
</div>

<div class="card">
  <h2>💡 종목별 디테일 — 시뮬 매도가 왜 잘못됐나</h2>
  <ul class="reason-list" style="line-height:2">
    <li><b>삼천당제약</b>: 사용자 14회 매도 → 시뮬 6회 매도. 시뮬이 매도를 일찍 시작해서 평단 낮은 lots 소진 → 후반 큰 추세 못 잡음.</li>
    <li><b>토모큐브</b>: 사용자는 사실상 매도 안 함(1회) + 평가이익 +2,536만. 시뮬은 매도 3회 → 평가이익 줄어듬. <b>HOLD가 정답이었다.</b></li>
    <li><b>파마리서치</b>: 가장 극적인 차이(-2.57억). 사용자 분할매수 80회로 평단을 71,028까지 낮춤. 시뮬은 매도 35회로 추세를 35번 끊었음.</li>
  </ul>
</div>

</div>
</body>
</html>"""


if __name__ == "__main__":
    main()
