"""why_rally — 주가 상승 원인 + 현재 유효성 분석.

출력:
  1. 구간별 주도 창구 / 외국계 vs 국내
  2. 구간별 투자자 유형 (외국인/기관/개인) — investor_flow DB
  3. 구간별 공매도 추이 — short_balance DB
  4. 사모/기타법인 세분화 — KRX API (pykrx, 실패 시 graceful skip)
  5. 현재 원인 유효성 체크리스트 + 뉴스 검색 키워드

사용:
  python3 -m analysis.why_rally 010170
  python3 -m analysis.why_rally 010170 --name 대한광통신
"""
import os, sys, warnings
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.db import query_df

FOREIGN_BROKERS = {
    'JP모간', '메릴린치', 'UBS', '골드만삭스', '모간서울', '씨티그룹',
    '바클레이즈', '맥쿼리', '노무라', '다이와', 'BNP파리바', 'HSBC',
    '크레디트스위스', 'Deutsche', '모간스탠리', 'CLSA',
}

# KRX 9분류 투자자 매핑 (pykrx detail 컬럼 → 표시명)
KRX_INVESTOR_COLS = {
    '금융투자': '금융투자(증권사)',
    '보험':    '보험',
    '투신':    '투신(펀드)',
    '사모':    '사모펀드',
    '은행':    '은행',
    '기타금융': '기타금융',
    '연기금등': '연기금',
    '기타법인': '기타법인',
    '개인':    '개인',
    '외국인':  '외국인',
}


# ─────────────────────────────────────────────
#  가격 로드 / 구간 탐지
# ─────────────────────────────────────────────

def load_price(code: str) -> pd.DataFrame:
    df = query_df(
        "SELECT date, open, high, low, close, volume FROM prices WHERE code=? ORDER BY date",
        (code,)
    )
    df['date'] = pd.to_datetime(df['date'])
    return df


def find_phases(df: pd.DataFrame):
    """52주 저점→고점→현재 기준 구간 자동 탐지."""
    cutoff = df['date'].max() - timedelta(days=365)
    df1y = df[df['date'] >= cutoff].copy()

    low_idx   = df1y['close'].idxmin()
    high_idx  = df1y['close'].idxmax()
    low_date  = df1y.loc[low_idx,  'date']
    high_date = df1y.loc[high_idx, 'date']
    low_price  = int(df1y.loc[low_idx,  'close'])
    high_price = int(df1y.loc[high_idx, 'close'])
    cur_date   = df1y['date'].max()
    cur_price  = int(df1y['close'].iloc[-1])

    mid_date = low_date + (high_date - low_date) / 2
    phases = []

    if low_date > cutoff + timedelta(days=30):
        phases.append({'label': '저점 이전',
                       'start': cutoff.strftime('%Y-%m-%d'),
                       'end':   low_date.strftime('%Y-%m-%d')})

    if (high_date - low_date).days > 60:
        phases.append({'label': '1차 랠리',
                       'start': low_date.strftime('%Y-%m-%d'),
                       'end':   mid_date.strftime('%Y-%m-%d')})
        phases.append({'label': '2차 랠리 (폭등)',
                       'start': mid_date.strftime('%Y-%m-%d'),
                       'end':   high_date.strftime('%Y-%m-%d')})
    else:
        phases.append({'label': '랠리',
                       'start': low_date.strftime('%Y-%m-%d'),
                       'end':   high_date.strftime('%Y-%m-%d')})

    if (cur_date - high_date).days > 5:
        phases.append({'label': '고점 이후',
                       'start': high_date.strftime('%Y-%m-%d'),
                       'end':   cur_date.strftime('%Y-%m-%d')})

    return phases, low_date, low_price, high_date, high_price, cur_price


def big_move_days(df: pd.DataFrame, threshold: float = 0.08) -> list:
    df1y = df[df['date'] >= df['date'].max() - timedelta(days=365)].copy()
    df1y['chg'] = df1y['close'].pct_change()
    big = df1y[df1y['chg'].abs() >= threshold].copy()
    return [
        {'date': r['date'].strftime('%Y-%m-%d'),
         'close': int(r['close']),
         'chg': round(float(r['chg']) * 100, 1)}
        for _, r in big.iterrows()
    ]


# ─────────────────────────────────────────────
#  1. 창구(broker) 구간 분석
# ─────────────────────────────────────────────

def broker_phase_analysis(code: str, phases: list) -> list:
    md = query_df("""
        SELECT date, broker_name, net, buy, sell
        FROM member_daily WHERE code=? AND date >= ?
        ORDER BY date
    """, (code, phases[0]['start']))

    results = []
    for ph in phases:
        seg = md[(md['date'] >= ph['start']) & (md['date'] <= ph['end'])].copy()
        if seg.empty:
            continue

        by_b     = seg.groupby('broker_name')['net'].sum()
        top5     = by_b.sort_values(ascending=False).head(5)
        bot3     = by_b.sort_values(ascending=True).head(3)
        is_for   = seg['broker_name'].isin(FOREIGN_BROKERS)
        f_net    = int(seg[is_for]['net'].sum())
        d_net    = int(seg[~is_for]['net'].sum())
        total    = abs(f_net) + abs(d_net)

        results.append({
            'label':        ph['label'],
            'start':        ph['start'],
            'end':          ph['end'],
            'top_buy':      [(n, int(v)) for n, v in top5.items()],
            'top_sell':     [(n, int(v)) for n, v in bot3.items()],
            'foreign_net':  f_net,
            'domestic_net': d_net,
            'foreign_pct':  abs(f_net) / total * 100 if total > 0 else 0,
            'foreign_role': '매수주도' if f_net > 0 else '매도주도',
        })
    return results


# ─────────────────────────────────────────────
#  2. 투자자 유형 구간 분석 (외국인/기관/개인)
# ─────────────────────────────────────────────

def investor_phase_analysis(code: str, phases: list) -> list:
    """investor_flow DB — 외국인/기관/개인 구간 순매수 합산."""
    iv = query_df("""
        SELECT date, foreign_qty, inst_qty, retail_qty
        FROM investor_flow WHERE code=? AND date >= ?
        ORDER BY date
    """, (code, phases[0]['start']))

    if iv.empty:
        return []

    results = []
    for ph in phases:
        seg = iv[(iv['date'] >= ph['start']) & (iv['date'] <= ph['end'])]
        if seg.empty:
            continue
        f = int(seg['foreign_qty'].sum())
        i = int(seg['inst_qty'].sum())
        r = int(seg['retail_qty'].sum())

        # 주도 판정
        dominant = max([(abs(f), '외국인', f), (abs(i), '기관', i), (abs(r), '개인', r)],
                       key=lambda x: x[0])

        results.append({
            'label':   ph['label'],
            'start':   ph['start'],
            'end':     ph['end'],
            'foreign': f,
            'inst':    i,
            'retail':  r,
            'dominant_name':  dominant[1],
            'dominant_net':   dominant[2],
        })
    return results


# ─────────────────────────────────────────────
#  3. 공매도 구간 분석
# ─────────────────────────────────────────────

def short_phase_analysis(code: str, phases: list) -> list:
    """short_balance DB — 구간별 공매도 잔고 변화."""
    sb = query_df("""
        SELECT date, short_vol, short_ratio, short_balance_qty, short_balance_pct
        FROM short_balance WHERE code=? AND date >= ?
        ORDER BY date
    """, (code, phases[0]['start']))

    if sb.empty:
        return []

    results = []
    for ph in phases:
        seg = sb[(sb['date'] >= ph['start']) & (sb['date'] <= ph['end'])]
        if seg.empty:
            continue

        bal_start = int(seg.iloc[0]['short_balance_qty'])
        bal_end   = int(seg.iloc[-1]['short_balance_qty'])
        pct_start = float(seg.iloc[0]['short_balance_pct'])
        pct_end   = float(seg.iloc[-1]['short_balance_pct'])
        chg_pct   = (bal_end - bal_start) / bal_start * 100 if bal_start > 0 else 0
        avg_ratio = float(seg['short_ratio'].mean())
        max_ratio = float(seg['short_ratio'].max())

        if chg_pct >= 30:    direction = '🔴 급증 (공매도 세력 진입)'
        elif chg_pct >= 10:  direction = '⚠️ 증가'
        elif chg_pct <= -20: direction = '🟢 급감 (숏커버링)'
        elif chg_pct <= -10: direction = '↘ 감소'
        else:                direction = '→ 횡보'

        results.append({
            'label':      ph['label'],
            'start':      ph['start'],
            'end':        ph['end'],
            'bal_start':  bal_start,
            'bal_end':    bal_end,
            'pct_end':    pct_end,
            'chg_pct':    chg_pct,
            'avg_ratio':  avg_ratio,
            'max_ratio':  max_ratio,
            'direction':  direction,
        })
    return results


# ─────────────────────────────────────────────
#  2b. KIS 9분류 최근 수급 현황
# ─────────────────────────────────────────────

def kis_investor_current(code: str) -> dict:
    """KIS API 9분류 최근 5/20일 수급 현황 (캐싱 적용)."""
    try:
        from signals.kis_investor import analyze_investor_signal
        return analyze_investor_signal(code)
    except Exception as e:
        return {"available": False, "error": str(e)}


def market_macro_context() -> dict:
    """NAVER Finance 시장 전체 당일 수급 스냅샷."""
    try:
        from signals.naver_market import get_market_flow_snapshot
        return get_market_flow_snapshot()
    except Exception:
        return {"available": False}


# ─────────────────────────────────────────────
#  4. 사모/기타법인 — KRX 9분류 (pykrx)
# ─────────────────────────────────────────────

def _isin_from_code(code: str) -> str:
    """종목코드 → ISIN (KR7XXXXXXX)."""
    try:
        from pykrx import stock as krx
        isin_map = krx.get_market_ticker_list(market="KOSPI") + \
                   krx.get_market_ticker_list(market="KOSDAQ")
        # pykrx ticker list doesn't return ISIN directly; use shortcut
        # KR7 + code + check digit approximation — use known pattern
        return f"KR7{code}004"   # 대부분의 보통주 ISIN 패턴
    except Exception:
        return f"KR7{code}004"


def fetch_krx_investor_detail(code: str, start: str, end: str) -> pd.DataFrame:
    """KRX 9분류 투자자 순매수금액 (pykrx).

    반환: DataFrame with columns [금융투자, 보험, 투신, 사모, 은행, 기타금융, 연기금등, 기타법인, 개인, 외국인]
    index: date
    실패 시 None
    """
    try:
        from pykrx.website.krx.market import core as mcore
        fetcher = mcore.투자자별_거래실적_개별종목_일별추이_상세()
        isin = _isin_from_code(code)
        # trdVolVal=2 (거래대금), askBid=3 (순매수)
        df = fetcher.fetch(start.replace('-', ''), end.replace('-', ''),
                           isin, trdVolVal=2, askBid=3)
        if df is None or df.empty:
            return None

        # 컬럼명 매핑: TRDVAL1~TRDVAL10 → 투자자명
        col_map = {
            'TRDVAL1': '금융투자', 'TRDVAL2': '보험',   'TRDVAL3': '투신',
            'TRDVAL4': '사모',     'TRDVAL5': '은행',   'TRDVAL6': '기타금융',
            'TRDVAL7': '연기금등', 'TRDVAL8': '기타법인','TRDVAL9': '개인',
            'TRDVAL10': '외국인',
        }
        df = df.rename(columns=col_map)
        # TRD_DD → index
        if 'TRD_DD' in df.columns:
            df['date'] = pd.to_datetime(df['TRD_DD'], format='%Y/%m/%d', errors='coerce')
            df = df.set_index('date')
        # 숫자 변환 (쉼표 제거)
        for c in col_map.values():
            if c in df.columns:
                df[c] = pd.to_numeric(df[c].astype(str).str.replace(',', ''), errors='coerce')
        return df[[c for c in col_map.values() if c in df.columns]]
    except Exception:
        return None


def krx_phase_analysis(code: str, phases: list) -> list:
    """KRX 9분류 구간 합산."""
    start = phases[0]['start']
    end   = phases[-1]['end']
    df    = fetch_krx_investor_detail(code, start, end)
    if df is None:
        return []

    results = []
    for ph in phases:
        seg = df[(df.index >= ph['start']) & (df.index <= ph['end'])]
        if seg.empty:
            continue
        row = {'label': ph['label'], 'start': ph['start'], 'end': ph['end']}
        for col in ['금융투자', '보험', '투신', '사모', '은행', '기타금융', '연기금등', '기타법인', '개인', '외국인']:
            if col in seg.columns:
                row[col] = int(seg[col].sum())
        results.append(row)
    return results


# ─────────────────────────────────────────────
#  5. 유효성 체크
# ─────────────────────────────────────────────

def validity_check(code: str, phases: list, broker_results: list,
                   short_results: list, kis_sig: dict = None) -> list:
    checks = []

    # 랠리 주도 창구 현재 상태
    rally_ph = next((r for r in broker_results if '폭등' in r['label'] or '랠리' in r['label']), None)
    if rally_ph and rally_ph['top_buy']:
        top_b = rally_ph['top_buy'][0][0]
        recent = query_df("""
            SELECT SUM(net) as n FROM member_daily
            WHERE code=? AND broker_name=? AND date >= date('now','-7 days')
        """, (code, top_b))
        net7 = int(recent.iloc[0]['n'] or 0) if not recent.empty else 0
        checks.append({
            'item':   f'랠리 주도창구 [{top_b}] 최근 7일',
            'status': '✅ 매수 유지' if net7 > 0 else '🔴 청산/이탈',
            'detail': f'{net7:+,}주',
        })

    # 고점 이후 외국계
    post = next((r for r in broker_results if '고점 이후' in r['label']), None)
    if post:
        fn = post['foreign_net']
        checks.append({
            'item':   '외국계 고점 이후 포지션',
            'status': '✅ 추가 매수' if fn > 500000 else ('🔴 청산 중' if fn < -500000 else '⚠️ 중립'),
            'detail': f'{fn:+,}주',
        })

    # 공매도 추이
    cur_short = next((r for r in short_results if '고점 이후' in r['label']), None)
    if not cur_short:
        cur_short = short_results[-1] if short_results else None
    if cur_short:
        checks.append({
            'item':   f'공매도 잔고율 ({cur_short["end"]} 기준)',
            'status': ('🔴 높음' if cur_short['pct_end'] >= 5
                       else '⚠️ 중간' if cur_short['pct_end'] >= 2
                       else '✅ 낮음'),
            'detail': f'{cur_short["pct_end"]:.2f}%  (구간내 {cur_short["chg_pct"]:+.1f}%  {cur_short["direction"]})',
        })

    # KIS 9분류 시그널
    if kis_sig and kis_sig.get("available"):
        frgn_5d  = kis_sig.get("frgn_5d", 0)
        smart_5d = kis_sig.get("smart_5d", 0)
        hidden_5d = kis_sig.get("hidden_5d", 0)

        if frgn_5d > 10:
            checks.append({
                "item": "외국인 최근 5일",
                "status": "✅ 순매수",
                "detail": f"+{frgn_5d:.1f}억",
            })
        elif frgn_5d < -10:
            checks.append({
                "item": "외국인 최근 5일",
                "status": "🔴 순매도",
                "detail": f"{frgn_5d:.1f}억",
            })

        if hidden_5d >= 5:
            checks.append({
                "item": "사모+기타법인 5일 (숨은 수급)",
                "status": "✅ 순매수 중",
                "detail": f"+{hidden_5d:.1f}억  (사모 {kis_sig.get('pe_fund_5d',0):.1f} / 기타법인 {kis_sig.get('etc_corp_5d',0):.1f})",
            })
        elif hidden_5d <= -5:
            checks.append({
                "item": "사모+기타법인 5일 (숨은 수급)",
                "status": "🔴 순매도",
                "detail": f"{hidden_5d:.1f}억",
            })

        if smart_5d >= 20:
            checks.append({
                "item": "스마트머니 5일 (외인+기관)",
                "status": "✅ 동반 매수",
                "detail": f"+{smart_5d:.1f}억",
            })
        elif smart_5d <= -20:
            checks.append({
                "item": "스마트머니 5일 (외인+기관)",
                "status": "🔴 동반 매도",
                "detail": f"{smart_5d:.1f}억",
            })

    return checks


# ─────────────────────────────────────────────
#  메인 리포트
# ─────────────────────────────────────────────

def print_report(code: str, name: str = ""):
    label = name or code
    W = 62
    print(f"\n{'='*W}")
    print(f"  {label} ({code}) — 상승 원인 & 유효성 분석")
    print(f"  기준: {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*W}")

    df = load_price(code)
    if df.empty:
        print("  가격 데이터 없음")
        return

    phases, low_d, low_p, high_d, high_p, cur_p = find_phases(df)
    mag       = (high_p / low_p - 1) * 100
    from_high = (cur_p / high_p - 1) * 100

    print(f"\n  52주 저점: {low_d.strftime('%Y-%m-%d')}  {low_p:,}원")
    print(f"  52주 고점: {high_d.strftime('%Y-%m-%d')}  {high_p:,}원  (저점比 {mag:+.0f}%)")
    print(f"  현재:      {df['date'].max().strftime('%Y-%m-%d')}  {cur_p:,}원  (고점比 {from_high:+.1f}%)")

    # ── 매크로 컨텍스트 ────────────────────────────
    macro = market_macro_context()
    if macro.get("available"):
        d = macro.get("date", "")
        f100 = macro.get("frgn_100m", 0)
        o100 = macro.get("orgn_100m", 0)
        p100 = macro.get("prsn_100m", 0)
        f_s = ("+" if f100 >= 0 else "") + f"{f100:.0f}억"
        o_s = ("+" if o100 >= 0 else "") + f"{o100:.0f}억"
        p_s = ("+" if p100 >= 0 else "") + f"{p100:.0f}억"
        print(f"\n  [시장 수급 {d}]  외국인 {f_s}  기관 {o_s}  개인 {p_s}  (NAVER, 당일)")
        for tr in macro.get("triggers", []):
            print(f"  ▷ {tr}")

    # ── 급등락일 (뉴스 기준점) ─────────────────────
    moves = big_move_days(df, 0.08)
    if moves:
        print(f"\n▣ 주요 급등락일 (±8% 이상)")
        print(f"  {'날짜':12s}  {'종가':>8s}  {'등락':>7s}")
        print(f"  {'-'*32}")
        for m in moves:
            arr = '▲' if m['chg'] > 0 else '▼'
            print(f"  {m['date']:12s}  {m['close']:>8,}  {arr}{abs(m['chg']):>5.1f}%")

    # ── 1. 창구별 구간 분석 ────────────────────────
    broker_results = broker_phase_analysis(code, phases)
    print(f"\n▣ 구간별 주도 창구")
    for r in broker_results:
        p_seg = df[(df['date'] >= r['start']) & (df['date'] <= r['end'])]
        if p_seg.empty: continue
        p_s = int(p_seg.iloc[0]['close']); p_e = int(p_seg.iloc[-1]['close'])
        pchg = (p_e / p_s - 1) * 100
        print(f"\n  [{r['label']}]  {r['start']}~{r['end']}  {p_s:,}→{p_e:,}원  {pchg:+.0f}%")
        print(f"  외국계 {r['foreign_net']:+,}주 ({r['foreign_role']}, {r['foreign_pct']:.0f}%)  "
              f"|  국내 {r['domestic_net']:+,}주")
        print(f"  매수 주도: " + "  /  ".join(f"{n}({v:+,})" for n, v in r['top_buy'][:3]))
        print(f"  매도 주도: " + "  /  ".join(f"{n}({v:+,})" for n, v in r['top_sell'][:3]))

    # ── 2. 투자자 유형 (외국인/기관/개인) ─────────
    inv_results = investor_phase_analysis(code, phases)
    if inv_results:
        print(f"\n▣ 구간별 투자자 유형 (외국인 / 기관 / 개인)")
        print(f"  {'구간':14s}  {'외국인':>12s}  {'기관':>12s}  {'개인':>12s}  주도")
        print(f"  {'-'*60}")
        for r in inv_results:
            dom = r['dominant_name']
            print(f"  {r['label']:14s}  {r['foreign']:>+12,}  {r['inst']:>+12,}  "
                  f"{r['retail']:>+12,}  [{dom}]")

    # ── 2b. KIS 9분류 현재 수급 ───────────────────
    kis_sig = kis_investor_current(code)
    if kis_sig.get("available"):
        ld = kis_sig.get("last_date", "")
        print(f"\n▣ 현재 수급 — KIS 9분류  (기준일: {ld}, 단위: 억원)")
        print(f"  {'구분':10s}  {'5일':>8s}  {'20일':>8s}")
        print(f"  {'-'*30}")

        def _fmt(v):
            return ("+" if v >= 0 else "") + f"{v:.1f}"

        rows_9 = [
            ("외국인",   kis_sig.get("frgn_5d", 0),     kis_sig.get("frgn_20d", 0)),
            ("기관계",   kis_sig.get("orgn_5d", 0),     kis_sig.get("orgn_20d", 0)),
            ("개인",     kis_sig.get("prsn_5d", 0),     0),
            ("사모",     kis_sig.get("pe_fund_5d", 0),  kis_sig.get("pe_fund_20d", 0)),
            ("기타법인", kis_sig.get("etc_corp_5d", 0), kis_sig.get("etc_corp_20d", 0)),
            ("숨은수급", kis_sig.get("hidden_5d", 0),   kis_sig.get("hidden_20d", 0)),
        ]
        for lbl, v5, v20 in rows_9:
            marker = "  "
            if lbl == "숨은수급":
                print(f"  {'-'*30}")
                marker = "▶ "
            s20 = _fmt(v20) if v20 != 0 else "—"
            print(f"  {marker}{lbl:8s}  {_fmt(v5):>8s}  {s20:>8s}")

        buy_sigs  = kis_sig.get("buy_signals",  [])
        sell_sigs = kis_sig.get("sell_signals", [])
        if buy_sigs or sell_sigs:
            print()
        for s in buy_sigs:
            print(f"  ✅ {s}")
        for s in sell_sigs:
            print(f"  🔴 {s}")
    elif "error" in kis_sig:
        print(f"\n  KIS 9분류 조회 실패: {kis_sig['error']}")

    # ── 3. 공매도 구간 추이 ────────────────────────
    short_results = short_phase_analysis(code, phases)
    if short_results:
        print(f"\n▣ 구간별 공매도 잔고 추이")
        print(f"  {'구간':14s}  {'잔고율(시작→끝)':>16s}  {'구간변화':>8s}  {'평균비중':>8s}  상태")
        print(f"  {'-'*72}")
        for r in short_results:
            print(f"  {r['label']:14s}  "
                  f"{r['pct_end']:>7.2f}%  "
                  f"{r['chg_pct']:>+8.1f}%  "
                  f"{r['avg_ratio']:>7.2f}%  "
                  f"{r['direction']}")

    # ── 4. 사모/기타법인 — DART 대량보유 공시 ──────
    print(f"\n▣ 법인/기관 주요주주 현황 (DART 공시 기반)")
    dart_ctx = {}
    try:
        from signals.dart_major import (
            largest_shareholders, major_holder_timeline, _investor_type
        )
        start_str = phases[0]['start'].replace('-', '')
        end_str   = phases[-1]['end'].replace('-', '')

        shareholders = largest_shareholders(code)
        timeline     = major_holder_timeline(code,
                                              start=start_str,
                                              end=end_str)

        if shareholders:
            print(f"\n  [최대주주 현황]  {'이름':20s}  {'유형':8s}  {'보유율':>8s}  {'변동':>8s}")
            print(f"  {'-'*56}")
            for s in shareholders:
                delta_s = (f"+{s['delta_pct']:.2f}%p" if s['delta_pct'] > 0
                           else f"{s['delta_pct']:.2f}%p" if s['delta_pct'] < 0
                           else "변동없음")
                rel = f"[{s['relation']}]" if s['relation'] else ""
                print(f"  {s['name']:20s}  {s['investor_type']:8s}  "
                      f"{s['end_pct']:>7.2f}%  {delta_s:>10s}  {rel}")

        if timeline:
            print(f"\n  [대량보유/임원 변동 타임라인]")
            for t in timeline:
                marker = {'기타법인': '🏢', '연기금등': '🏦',
                          '외국인':   '🌍', '사모':    '💰'}.get(t['investor_type'], '👤')
                print(f"  {t['date']}  {marker} {t['who']:20s}  {t['type']:6s}  {t['report']}")
        else:
            print("  5% 이상 대량보유 공시 없음 (해당 기간)")

        dart_ctx = {"shareholders": shareholders, "timeline": timeline}
    except Exception as e:
        print(f"  DART 조회 실패: {e}")

    # ── 5. 유효성 체크 ────────────────────────────
    checks = validity_check(code, phases, broker_results, short_results, kis_sig)

    # DART 기반 추가 체크
    if dart_ctx:
        _tl = dart_ctx.get("timeline", [])
        _sh = dart_ctx.get("shareholders", [])
        # 최근 30일 대량보유 변동
        cutoff_30d = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        recent_bulk = [t for t in _tl if t["date"] >= cutoff_30d and t["type"] == "대량보유"]
        if recent_bulk:
            names = ", ".join(set(t["who"] for t in recent_bulk))
            checks.append({
                "item":   "최근 30일 대량보유 공시",
                "status": "⚠️ 변동 있음",
                "detail": f"{names} ({len(recent_bulk)}건)",
            })
        # 법인 대주주 순증 여부
        corps = [s for s in _sh if s["investor_type"] == "기타법인"]
        if corps:
            top_c = corps[0]
            checks.append({
                "item":   f"기타법인 대주주 [{top_c['name']}]",
                "status": "✅ 지분 유지" if top_c["delta_pct"] >= -0.5
                          else "🔴 지분 감소",
                "detail": f"{top_c['end_pct']:.1f}% (기말 기준)",
            })

    print(f"\n▣ 현재 원인 유효성 체크")
    for c in checks:
        print(f"  {c['status']}  {c['item']}: {c['detail']}")

    # ── 뉴스 검색 컨텍스트 ─────────────────────────
    print(f"\n▣ 뉴스 검색 컨텍스트")
    print(f"  키워드: \"{label}\" + \"뉴스\" + \"2025 2026\"")
    print(f"  주요 날짜 (±8% 급등락일):")
    for m in moves[:12]:
        arr = '▲' if m['chg'] > 0 else '▼'
        print(f"    {m['date']} ({arr}{abs(m['chg']):.1f}%) — 전후 뉴스 확인")

    print(f"\n{'='*W}\n")

    return {
        'code': code, 'name': label,
        'phases': broker_results,
        'investor': inv_results,
        'short': short_results,
        'dart': dart_ctx,
        'kis_9class': kis_sig,
        'macro': macro,
        'validity': checks,
        'big_moves': moves,
    }


if __name__ == "__main__":
    import argparse
    from file_io import load_json
    from config import STOCK_MAP_FILE

    p = argparse.ArgumentParser()
    p.add_argument("codes", nargs="*")
    p.add_argument("--names", nargs="*")
    args = p.parse_args()

    smap = load_json(STOCK_MAP_FILE, default={})
    code_to_name = {info["code"]: nm for nm, info in smap.items() if "code" in info}
    name_to_code = {nm: info["code"] for nm, info in smap.items() if "code" in info}

    seen = set()
    targets = []

    def _add(nm, cd):
        if cd not in seen:
            seen.add(cd)
            targets.append((nm, cd))

    for c in args.codes or []:
        _add(code_to_name.get(c, c), c)
    for n in (args.names or []):
        if n in name_to_code:
            _add(n, name_to_code[n])

    if not targets:
        targets = [("대한광통신", "010170")]

    for nm, cd in targets:
        print_report(cd, nm)
