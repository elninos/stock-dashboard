"""KRX Open API 7개 엔드포인트 동작 검증."""
import sys, json, time
from signals import krx_open_api as krx

DATE = sys.argv[1] if len(sys.argv) > 1 else "20260428"

TESTS = [
    ("유가증권 일별매매정보",  krx.get_kospi_daily,     "stk_bydd_trd"),
    ("코스닥 일별매매정보",    krx.get_kosdaq_daily,    "ksq_bydd_trd"),
    ("KOSPI 시리즈 지수",     krx.get_kospi_index,     "kospi_dd_trd"),
    ("KOSDAQ 시리즈 지수",    krx.get_kosdaq_index,    "ksdaq_dd_trd"),
    ("KRX 섹터지수",          krx.get_krx_index,       "krx_dd_trd"),
    ("ETF 일별매매정보",       krx.get_etf_daily,       "etf_bydd_trd"),
    ("유가증권 종목기본정보",  krx.get_kospi_base_info, "stk_isu_base_info"),
    ("코스닥 종목기본정보",    krx.get_kosdaq_base_info,"ksq_isu_base_info"),
]

print(f"=== KRX Open API 동작 검증 ({DATE}) ===\n")

results = []
for label, fn, endpoint in TESTS:
    print(f"▶ {label} ({endpoint})")
    t0 = time.time()
    try:
        rows = fn(DATE)
        elapsed = time.time() - t0
        if rows:
            cols = list(rows[0].keys())
            print(f"  ✅ {len(rows):>5}행, {elapsed:.2f}s")
            print(f"     컬럼({len(cols)}): {cols[:6]}{'...' if len(cols)>6 else ''}")
            print(f"     샘플: {json.dumps(rows[0], ensure_ascii=False)[:200]}")
            results.append((label, "OK", len(rows), elapsed))
        else:
            print(f"  ⚠ 0행 반환 ({elapsed:.2f}s)")
            results.append((label, "EMPTY", 0, elapsed))
    except Exception as e:
        print(f"  ❌ 예외: {e}")
        results.append((label, f"ERROR: {e}", 0, 0))
    print()

print("\n=== 요약 ===")
for label, status, n, t in results:
    mark = "✅" if status == "OK" else ("⚠" if status == "EMPTY" else "❌")
    print(f"  {mark} {label:<25} {status:<10} rows={n:<6} {t:.2f}s")
