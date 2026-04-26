"""공통 설정값 — 하드코딩 대신 여기서 관리."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── 환율 fallback ────────────────────────────────────────────────────────────
FX_FALLBACK = {
    "USD": 1400.0,
    "JPY": 9.5,    # 100엔 기준
    "CNY": 200.0,
    "HKD": 190.0,
}

# ── HTTP ─────────────────────────────────────────────────────────────────────
TIMEOUT_SHORT  = 5
TIMEOUT_MEDIUM = 10
TIMEOUT_LONG   = 15
RATE_LIMIT_SEC = 0.2   # API 호출 간 대기

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ── 뉴스 ─────────────────────────────────────────────────────────────────────
MAX_NEWS_ARTICLES  = 8
NEWS_LOOKBACK_DAYS = 7

# ── Claude API ───────────────────────────────────────────────────────────────
CLAUDE_MODEL      = "claude-sonnet-4-6"
BATCH_SIZE_NEWS   = 5

# ── 브리핑 기간 ──────────────────────────────────────────────────────────────
BRIEFING_PERIODS = {
    "daily":    1,
    "weekly":   7,
    "biweekly": 14,
    "monthly":  28,
}

# ── 파일 경로 ─────────────────────────────────────────────────────────────────
TRANSACTIONS_FILE        = os.path.join(BASE_DIR, "transactions.json")
PRICES_FILE              = os.path.join(BASE_DIR, "prices.json")
STOCK_MAP_FILE           = os.path.join(BASE_DIR, "stock_map.json")
BRIEFING_FILE            = os.path.join(BASE_DIR, "briefing.json")
BRIEFING_SUMMARY_FILE    = os.path.join(BASE_DIR, "briefing_summary.json")
STOCK_NEWS_RAW_FILE      = os.path.join(BASE_DIR, "stock_news_raw.json")
STOCK_NEWS_FILE          = os.path.join(BASE_DIR, "stock_news.json")
HIST_PORTFOLIO_FILE      = os.path.join(BASE_DIR, "historical_portfolio_values.json")
SOURCES_FILE             = os.path.join(BASE_DIR, "sources.json")
SELL_SIGNALS_FILE        = os.path.join(BASE_DIR, "sell_signals.json")

# ── DART OpenAPI ──────────────────────────────────────────────────────────────
DART_API_KEY             = "95a83c9efdb1e3ce13be539270823fa31aafdad5"
DART_CORP_CODE_JSON      = os.path.join(BASE_DIR, "dart_corp_codes.json")
DART_CORP_CODE_ZIP       = os.path.join(BASE_DIR, "dart_corp_codes.zip")
