BEGIN IMMEDIATE;

CREATE TABLE system_state (
    "key" TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE credentials_state (
    provider TEXT PRIMARY KEY CHECK (provider IN ('KIS', 'DART', 'OPENAI', 'TELEGRAM')),
    expires_at TEXT,
    last_refreshed_at TEXT,
    status TEXT CHECK (status IN ('VALID', 'EXPIRING_SOON', 'INVALID')),
    error_count INTEGER DEFAULT 0
);

CREATE TABLE universe_assets (
    ticker TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('RISK_ASSET', 'SAFE_PARKING')),
    region TEXT NOT NULL CHECK (region IN ('KR', 'US', 'GLOBAL')),
    underlying_index TEXT NOT NULL,
    product_type TEXT NOT NULL CHECK (product_type = 'SPOT_ETF'),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'REMOVED')),
    data_status TEXT NOT NULL CHECK (data_status IN ('VALID', 'DATA_INVALID')),
    blocked_until_next_rotation INTEGER DEFAULT 0,
    added_at TEXT NOT NULL,
    removed_at TEXT
);

CREATE TABLE universe_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT
);

CREATE TABLE daily_candles (
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    raw_open REAL,
    raw_high REAL,
    raw_low REAL,
    raw_close REAL,
    adjusted_close REAL NOT NULL,
    volume INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_timestamp TEXT NOT NULL,
    PRIMARY KEY (ticker, trade_date)
);

CREATE TABLE weekly_previews (
    preview_id TEXT PRIMARY KEY,
    base_candle_date TEXT NOT NULL,
    ranked_candidates_json TEXT NOT NULL,
    projected_slots_json TEXT NOT NULL,
    projected_trades_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('SENT', 'CONSUMED', 'OVERRIDDEN')),
    created_at TEXT NOT NULL
);

CREATE TABLE positions (
    ticker TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL,
    avg_buy_price REAL NOT NULL,
    market_value INTEGER NOT NULL,
    peak_price REAL NOT NULL,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE portfolio_slots (
    slot_id TEXT PRIMARY KEY CHECK (slot_id IN ('S1', 'S2', 'S3')),
    target_weight REAL NOT NULL,
    ticker TEXT,
    assigned_at TEXT NOT NULL,
    assignment_reason TEXT NOT NULL
);

CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    kis_order_no TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    requested_qty INTEGER NOT NULL,
    filled_qty INTEGER DEFAULT 0,
    remaining_qty INTEGER NOT NULL,
    order_type TEXT NOT NULL CHECK (order_type = 'LIMIT'),
    order_price INTEGER NOT NULL,
    status TEXT NOT NULL,
    slot_id TEXT,
    strategy_reason TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE order_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    payload_sanitized TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE trade_fills (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    kis_execution_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price INTEGER NOT NULL,
    fee INTEGER DEFAULT 0,
    tax INTEGER DEFAULT 0,
    executed_at TEXT NOT NULL,
    UNIQUE (order_id, kis_execution_id)
);

CREATE TABLE daily_portfolio (
    snapshot_date TEXT PRIMARY KEY,
    total_equity INTEGER NOT NULL,
    cash_balance INTEGER NOT NULL,
    parking_value INTEGER NOT NULL,
    risk_asset_value INTEGER NOT NULL,
    hwm_adjusted_equity REAL NOT NULL,
    twr_cumulative REAL NOT NULL,
    benchmark_values TEXT NOT NULL,
    data_quality_status TEXT NOT NULL
);

CREATE TABLE cash_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_date TEXT NOT NULL,
    amount INTEGER NOT NULL,
    flow_type TEXT NOT NULL CHECK (flow_type IN ('DEPOSIT', 'WITHDRAWAL', 'DISTRIBUTION')),
    source TEXT NOT NULL,
    memo TEXT
);

CREATE TABLE corporate_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    action_type TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    record_date TEXT,
    pay_date TEXT,
    cash_amount REAL,
    adjustment_factor REAL DEFAULT 1.0,
    source TEXT NOT NULL,
    verified INTEGER DEFAULT 0
);

CREATE TABLE system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARN', 'CRITICAL')),
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

PRAGMA user_version = 1;
COMMIT;
