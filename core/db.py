"""SQLite 시계열 저장 헬퍼.

스키마:
  prices              (code, date, open, high, low, close, volume)
  member_daily        (code, date, broker_code, buy, sell, net, close)
  short_balance       (code, date, short_vol, short_balance, short_pct)
  investor_flow       (code, date, foreign_amt, inst_amt, retail_amt)
  index_ohlcv         (index_code, date, open, high, low, close, volume)
  signals_history     (code, date, score, action, reasons)
"""
import os, sqlite3, json
import pandas as pd

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "timeseries.db"
)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")  # 동시성 ↑
    return conn


def init_db():
    """모든 테이블 생성 (1회 호출)."""
    conn = get_conn()
    conn.executescript("""
    -- 가격 (OHLCV)
    CREATE TABLE IF NOT EXISTS prices (
        code TEXT NOT NULL,
        date TEXT NOT NULL,
        open INTEGER, high INTEGER, low INTEGER, close INTEGER,
        volume INTEGER,
        PRIMARY KEY (code, date)
    );
    CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

    -- 거래원 일별
    CREATE TABLE IF NOT EXISTS member_daily (
        code TEXT NOT NULL,
        date TEXT NOT NULL,
        broker_code TEXT NOT NULL,
        broker_name TEXT,
        buy INTEGER, sell INTEGER, net INTEGER,
        close INTEGER,
        PRIMARY KEY (code, date, broker_code)
    );
    CREATE INDEX IF NOT EXISTS idx_member_date ON member_daily(date);
    CREATE INDEX IF NOT EXISTS idx_member_broker ON member_daily(broker_code);

    -- 공매도 잔고
    CREATE TABLE IF NOT EXISTS short_balance (
        code TEXT NOT NULL,
        date TEXT NOT NULL,
        close INTEGER,
        short_vol INTEGER,
        short_ratio REAL,
        short_balance_qty INTEGER,
        short_balance_pct REAL,
        PRIMARY KEY (code, date)
    );
    CREATE INDEX IF NOT EXISTS idx_short_date ON short_balance(date);

    -- 투자자별 매매 (외인/기관/개인)
    CREATE TABLE IF NOT EXISTS investor_flow (
        code TEXT NOT NULL,
        date TEXT NOT NULL,
        close INTEGER,
        foreign_qty INTEGER, foreign_amt INTEGER,
        inst_qty INTEGER, inst_amt INTEGER,
        retail_qty INTEGER, retail_amt INTEGER,
        PRIMARY KEY (code, date)
    );
    CREATE INDEX IF NOT EXISTS idx_investor_date ON investor_flow(date);

    -- 지수 OHLCV (KOSPI/KOSDAQ/섹터)
    CREATE TABLE IF NOT EXISTS index_ohlcv (
        index_code TEXT NOT NULL,
        date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL,
        volume INTEGER,
        PRIMARY KEY (index_code, date)
    );

    -- 분봉 (장중 패턴)
    CREATE TABLE IF NOT EXISTS minutes (
        code TEXT NOT NULL,
        datetime TEXT NOT NULL,  -- YYYY-MM-DD HH:MM
        open INTEGER, high INTEGER, low INTEGER, close INTEGER,
        volume INTEGER,
        PRIMARY KEY (code, datetime)
    );
    CREATE INDEX IF NOT EXISTS idx_minutes_dt ON minutes(datetime);

    -- 호가/체결강도 (스냅샷, 매일 종가 직전)
    CREATE TABLE IF NOT EXISTS asking_price (
        code TEXT NOT NULL,
        datetime TEXT NOT NULL,
        bid_total INTEGER, ask_total INTEGER,
        bid_strength REAL,  -- 매수 체결 강도 %
        PRIMARY KEY (code, datetime)
    );

    -- 시그널 결과 (매일 스냅샷)
    CREATE TABLE IF NOT EXISTS signals_history (
        code TEXT NOT NULL,
        date TEXT NOT NULL,
        score REAL,
        action TEXT,
        reasons TEXT,  -- JSON
        PRIMARY KEY (code, date)
    );
    CREATE INDEX IF NOT EXISTS idx_signals_date ON signals_history(date);

    -- 거래원 코드↔이름 매핑
    CREATE TABLE IF NOT EXISTS broker_names (
        broker_code TEXT PRIMARY KEY,
        broker_name TEXT NOT NULL,
        updated_at TEXT
    );
    """)
    conn.commit()
    conn.close()


# ──────────────────────────────────────────
# 공통 헬퍼: append + 중복 제거
# ──────────────────────────────────────────

def upsert(table: str, df, key_cols: list):
    """DataFrame 일괄 INSERT (중복은 REPLACE).

    PK가 충돌하면 덮어쓰기.
    """
    if df is None or df.empty:
        return 0
    conn = get_conn()
    placeholders = ",".join("?" * len(df.columns))
    cols_str = ",".join(df.columns)
    sql = f"INSERT OR REPLACE INTO {table} ({cols_str}) VALUES ({placeholders})"
    rows = [tuple(r) for r in df.itertuples(index=False, name=None)]
    conn.executemany(sql, rows)
    conn.commit()
    n = conn.total_changes
    conn.close()
    return n


def query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    """SQL → DataFrame."""
    conn = get_conn()
    df = pd.read_sql(sql, conn, params=params)
    conn.close()
    return df


# ──────────────────────────────────────────
# 편의 함수
# ──────────────────────────────────────────

def append_prices(code: str, df):
    """가격 시계열 저장."""
    if df is None or df.empty: return 0
    df = df.copy()
    df["code"] = code
    cols = ["code", "date", "open", "high", "low", "close", "volume"]
    df = df[[c for c in cols if c in df.columns]]
    return upsert("prices", df, ["code", "date"])


def append_member_daily(code: str, df):
    """거래원 일별 매매 저장."""
    if df is None or df.empty: return 0
    df = df.copy()
    df["code"] = code
    cols = ["code", "date", "broker_code", "broker_name", "buy", "sell", "net", "close"]
    df = df[[c for c in cols if c in df.columns]]
    return upsert("member_daily", df, ["code", "date", "broker_code"])


def append_short(code: str, rows: list):
    """공매도 잔고."""
    if not rows: return 0
    df = pd.DataFrame(rows)
    df["code"] = code
    cols = ["code", "date", "close", "short_vol", "short_ratio",
             "short_balance_qty", "short_balance_pct"]
    df = df[[c for c in cols if c in df.columns]]
    return upsert("short_balance", df, ["code", "date"])


def append_investor(code: str, rows: list):
    """투자자별 매매."""
    if not rows: return 0
    df = pd.DataFrame(rows)
    df["code"] = code
    df = df.rename(columns={
        "personal_qty": "retail_qty", "personal_amt": "retail_amt"
    })
    cols = ["code", "date", "close",
             "foreign_qty", "foreign_amt", "inst_qty", "inst_amt",
             "retail_qty", "retail_amt"]
    df = df[[c for c in cols if c in df.columns]]
    return upsert("investor_flow", df, ["code", "date"])


def save_broker_names(mapping: dict):
    """거래원 매핑 저장."""
    if not mapping: return
    from datetime import datetime
    now = datetime.now().isoformat()
    rows = [(code, name, now) for code, name in mapping.items()]
    conn = get_conn()
    conn.executemany(
        "INSERT OR REPLACE INTO broker_names VALUES (?, ?, ?)", rows
    )
    conn.commit()
    conn.close()


def load_broker_names_from_db() -> dict:
    """DB에서 거래원 매핑 로드."""
    conn = get_conn()
    cur = conn.execute("SELECT broker_code, broker_name FROM broker_names")
    mapping = dict(cur.fetchall())
    conn.close()
    return mapping


# 초기 실행 시 자동 init
init_db()
