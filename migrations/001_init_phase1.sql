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
    replication_method TEXT NOT NULL CHECK (replication_method IN ('PHYSICAL', 'SYNTHETIC')),
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
    reason TEXT,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT
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
    PRIMARY KEY (ticker, trade_date),
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT
);

CREATE TABLE weekly_previews (
    preview_id TEXT PRIMARY KEY,
    base_candle_date TEXT NOT NULL,
    ranked_candidates_json TEXT NOT NULL,
    projected_slots_json TEXT NOT NULL,
    projected_trades_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('CREATED', 'SENT', 'DELIVERY_FAILED', 'CONSUMED', 'OVERRIDDEN', 'EXPIRED')),
    created_at TEXT NOT NULL
);

CREATE TABLE positions (
    ticker TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL,
    avg_buy_price REAL NOT NULL,
    market_value INTEGER NOT NULL,
    peak_price_adj REAL NOT NULL,
    last_synced_at TEXT NOT NULL,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT
);

CREATE TABLE portfolio_slots (
    slot_id TEXT PRIMARY KEY CHECK (slot_id IN ('S1', 'S2', 'S3')),
    target_weight REAL NOT NULL,
    ticker TEXT,
    assigned_at TEXT NOT NULL,
    assignment_reason TEXT NOT NULL,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE SET NULL
);

CREATE TABLE order_intents (
    parent_intent_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    cycle_id TEXT NOT NULL,
    intent_type TEXT NOT NULL CHECK (intent_type IN ('STRATEGY_REBALANCE', 'RISK_REDUCTION', 'PARKING_ALLOCATION', 'SMOKE_TEST')),
    slot_id TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    requested_qty INTEGER NOT NULL CHECK (requested_qty > 0),
    intent_payload_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('CREATED', 'IN_PROGRESS', 'COMPLETED', 'CLOSED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT,
    FOREIGN KEY (slot_id) REFERENCES portfolio_slots(slot_id) ON DELETE SET NULL
);

CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    parent_intent_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE,
    kis_order_no TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    requested_qty INTEGER NOT NULL CHECK (requested_qty > 0),
    filled_qty INTEGER DEFAULT 0 CHECK (filled_qty >= 0),
    remaining_qty INTEGER NOT NULL CHECK (remaining_qty >= 0),
    order_type TEXT NOT NULL CHECK (order_type = 'LIMIT'),
    order_price INTEGER NOT NULL CHECK (order_price > 0),
    status TEXT NOT NULL,
    broker_status_code TEXT,
    last_broker_checked_at TEXT,
    request_payload_hash TEXT,
    response_payload_sanitized TEXT,
    slot_id TEXT,
    strategy_reason TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (filled_qty + remaining_qty <= requested_qty),
    FOREIGN KEY (parent_intent_id) REFERENCES order_intents(parent_intent_id) ON DELETE RESTRICT,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT,
    FOREIGN KEY (slot_id) REFERENCES portfolio_slots(slot_id) ON DELETE SET NULL
);

CREATE TABLE order_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    payload_sanitized TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(order_id) ON DELETE RESTRICT
);

CREATE TABLE trade_fills (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    kis_execution_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty INTEGER NOT NULL CHECK (qty > 0),
    price INTEGER NOT NULL CHECK (price > 0),
    fee INTEGER DEFAULT 0,
    tax INTEGER DEFAULT 0,
    executed_at TEXT NOT NULL,
    UNIQUE (order_id, kis_execution_id),
    FOREIGN KEY (order_id) REFERENCES orders(order_id) ON DELETE RESTRICT,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT
);

CREATE TABLE daily_portfolio (
    snapshot_date TEXT PRIMARY KEY,
    total_equity INTEGER NOT NULL,
    cash_balance INTEGER NOT NULL,
    parking_value INTEGER NOT NULL,
    risk_asset_value INTEGER NOT NULL,
    flow_adjusted_unit_nav REAL NOT NULL,
    hwm_flow_adjusted REAL NOT NULL,
    twr_cumulative REAL NOT NULL,
    twr_quality_status TEXT NOT NULL CHECK (twr_quality_status IN ('VALID', 'DATA_INVALID')),
    benchmark_values TEXT NOT NULL,
    data_quality_status TEXT NOT NULL
);

CREATE TABLE cash_flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_transaction_id TEXT NOT NULL UNIQUE,
    flow_date TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    amount INTEGER NOT NULL,
    flow_type TEXT NOT NULL CHECK (flow_type IN ('DEPOSIT', 'WITHDRAWAL', 'DISTRIBUTION', 'UNCLASSIFIED')),
    classification_status TEXT NOT NULL CHECK (classification_status IN ('CONFIRMED', 'UNCLASSIFIED_CASH_FLOW')),
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    confirmed_at TEXT,
    memo TEXT
);

CREATE TABLE corporate_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_event_id TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    action_type TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    record_date TEXT,
    pay_date TEXT,
    cash_amount REAL,
    adjustment_factor REAL DEFAULT 1.0,
    source TEXT NOT NULL,
    verified INTEGER DEFAULT 0,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT
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

CREATE TABLE schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    checksum TEXT NOT NULL
);

CREATE TABLE deployment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    backup_id TEXT NOT NULL,
    migration_status TEXT NOT NULL,
    healthcheck_status TEXT NOT NULL,
    rollback_triggered INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    diff_sanitized TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE exit_obligations (
    obligation_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    source_event_id TEXT NOT NULL UNIQUE,
    required_qty INTEGER NOT NULL CHECK (required_qty > 0),
    filled_qty INTEGER NOT NULL DEFAULT 0 CHECK (filled_qty >= 0 AND filled_qty <= required_qty),
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'RESOLVED')),
    reason_code TEXT NOT NULL,
    last_order_id TEXT,
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    resolved_at TEXT,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT,
    FOREIGN KEY (last_order_id) REFERENCES orders(order_id) ON DELETE SET NULL
);

CREATE TABLE llm_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    model_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    risk_score INTEGER NOT NULL,
    decision TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_refs TEXT NOT NULL,
    summary_lines TEXT NOT NULL,
    usage_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT
);

CREATE TABLE risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    ticker TEXT,
    portfolio_dd REAL,
    source TEXT NOT NULL,
    decision TEXT NOT NULL,
    resolved_at TEXT,
    details_sanitized TEXT NOT NULL,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT
);

CREATE TABLE pending_actions (
    id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    ticker TEXT,
    reason TEXT NOT NULL,
    approval_status TEXT NOT NULL CHECK (approval_status IN ('PENDING', 'APPROVED', 'EXPIRED', 'REJECTED')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    confirmed_at TEXT,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT
);

CREATE INDEX idx_orders_status_updated ON orders(status, updated_at);
CREATE INDEX idx_orders_parent_intent_submitted ON orders(parent_intent_id, submitted_at);
CREATE INDEX idx_orders_ticker_submitted ON orders(ticker, submitted_at);
CREATE INDEX idx_order_events_order_created ON order_events(order_id, created_at);
CREATE INDEX idx_trade_fills_executed ON trade_fills(executed_at);
CREATE INDEX idx_daily_candles_ticker_date ON daily_candles(ticker, trade_date);
CREATE INDEX idx_system_events_severity_created ON system_events(severity, created_at);
CREATE INDEX idx_exit_obligations_status_ticker ON exit_obligations(status, ticker);
CREATE UNIQUE INDEX idx_exit_obligations_one_open_per_ticker ON exit_obligations(ticker) WHERE status = 'OPEN';

INSERT INTO schema_migrations(version, applied_at, checksum)
VALUES ('001_init_phase1', '2026-11-22T00:00:00Z', 'pending');

PRAGMA user_version = 1;
COMMIT;
