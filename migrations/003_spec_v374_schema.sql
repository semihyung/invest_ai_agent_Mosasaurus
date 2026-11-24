-- backward_compatible: false
-- 명세 v3.7.4 9.2절 스키마 정합화: 제약을 바꾸는 테이블은 SQLite 표준 절차(새 테이블 생성 →
-- 복사 → 기존 삭제 → 이름 변경)로 재구성하고, 신규 테이블 3종·뷰·트리거·partial index 를 추가한다.
-- 실행 전 db.py 가 `PRAGMA foreign_keys = OFF` 를 설정하고, 커밋 전 foreign_key_check 로 검증한다.

CREATE TABLE universe_assets_new (
    ticker TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('RISK_ASSET', 'SAFE_PARKING')),
    asset_class TEXT NOT NULL DEFAULT 'EQUITY',
    region TEXT NOT NULL CHECK (region IN ('KR', 'US', 'GLOBAL')),
    underlying_index TEXT NOT NULL,
    underlying_index_family TEXT NOT NULL DEFAULT '',
    product_type TEXT NOT NULL CHECK (product_type = 'SPOT_ETF'),
    replication_method TEXT NOT NULL CHECK (replication_method IN ('PHYSICAL', 'SYNTHETIC')),
    leverage_multiplier REAL NOT NULL DEFAULT 1.0 CHECK (leverage_multiplier = 1.0),
    is_inverse INTEGER NOT NULL DEFAULT 0 CHECK (is_inverse = 0),
    is_futures_based INTEGER NOT NULL DEFAULT 0 CHECK (is_futures_based = 0),
    classification_verified INTEGER NOT NULL DEFAULT 0 CHECK (classification_verified IN (0, 1)),
    classification_source TEXT,
    verified_by TEXT,
    verified_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'REMOVED')),
    data_status TEXT NOT NULL CHECK (data_status IN ('VALID', 'STALE', 'DATA_INVALID')),
    blocked_until_next_rotation INTEGER NOT NULL DEFAULT 0 CHECK (blocked_until_next_rotation IN (0, 1)),
    blocked_since TEXT,
    added_at TEXT NOT NULL,
    removed_at TEXT,
    CHECK (category = 'SAFE_PARKING' OR asset_class = 'EQUITY'),
    CHECK (
        classification_verified = 0
        OR (classification_source IS NOT NULL AND verified_by IS NOT NULL AND verified_at IS NOT NULL)
    )
);

INSERT INTO universe_assets_new (
    ticker, name, category, region, underlying_index, underlying_index_family,
    product_type, replication_method, leverage_multiplier, is_inverse, is_futures_based,
    classification_verified, classification_source, verified_by, verified_at,
    status, data_status, blocked_until_next_rotation, added_at, removed_at
)
SELECT
    ticker, name, category, region, underlying_index, underlying_index,
    product_type, replication_method, leverage_multiplier, is_inverse, is_futures_based,
    classification_verified,
    CASE WHEN classification_verified = 1 THEN classification_source END,
    CASE WHEN classification_verified = 1 THEN verified_by END,
    CASE WHEN classification_verified = 1 THEN verified_at END,
    status, data_status, COALESCE(blocked_until_next_rotation, 0), added_at, removed_at
FROM universe_assets;

DROP TABLE universe_assets;
ALTER TABLE universe_assets_new RENAME TO universe_assets;

CREATE TABLE daily_candles_new (
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    raw_open REAL,
    raw_high REAL,
    raw_low REAL,
    raw_close REAL NOT NULL CHECK (raw_close > 0),
    adjusted_close REAL NOT NULL CHECK (adjusted_close > 0),
    volume INTEGER NOT NULL CHECK (volume >= 0),
    turnover_krw INTEGER,
    source TEXT NOT NULL,
    source_timestamp TEXT NOT NULL,
    PRIMARY KEY (ticker, trade_date),
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT
);

INSERT INTO daily_candles_new (
    ticker, trade_date, raw_open, raw_high, raw_low, raw_close, adjusted_close,
    volume, source, source_timestamp
)
SELECT
    ticker, trade_date, raw_open, raw_high, raw_low, COALESCE(raw_close, adjusted_close),
    adjusted_close, volume, source, source_timestamp
FROM daily_candles;

DROP TABLE daily_candles;
ALTER TABLE daily_candles_new RENAME TO daily_candles;

CREATE TABLE weekly_previews_new (
    preview_id TEXT NOT NULL,
    preview_revision INTEGER NOT NULL DEFAULT 1 CHECK (preview_revision >= 1),
    base_candle_date TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'SCHEDULED' CHECK (source IN ('SCHEDULED', 'FALLBACK')),
    ranked_candidates_json TEXT NOT NULL,
    projected_slots_json TEXT NOT NULL,
    projected_trades_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('CREATED', 'SENT', 'DELIVERY_FAILED', 'CONSUMED', 'EXPIRED')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (preview_id, preview_revision)
);

INSERT INTO weekly_previews_new (
    preview_id, base_candle_date, ranked_candidates_json, projected_slots_json,
    projected_trades_json, status, created_at
)
SELECT
    preview_id, base_candle_date, ranked_candidates_json, projected_slots_json,
    projected_trades_json,
    CASE WHEN status = 'OVERRIDDEN' THEN 'EXPIRED' ELSE status END,
    created_at
FROM weekly_previews;

DROP TABLE weekly_previews;
ALTER TABLE weekly_previews_new RENAME TO weekly_previews;

-- 유니버스 밖 보유(수동 매수·티커 변경)도 기록하도록 positions 의 종목 FK 를 제거한다.
CREATE TABLE positions_new (
    ticker TEXT PRIMARY KEY,
    tracking_status TEXT NOT NULL DEFAULT 'MANAGED' CHECK (tracking_status IN ('MANAGED', 'UNMANAGED')),
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    avg_buy_price REAL NOT NULL,
    market_value INTEGER NOT NULL,
    peak_price_adj REAL,
    last_synced_at TEXT NOT NULL,
    CHECK (tracking_status = 'UNMANAGED' OR peak_price_adj IS NOT NULL)
);

INSERT INTO positions_new (
    ticker, quantity, avg_buy_price, market_value, peak_price_adj, last_synced_at
)
SELECT ticker, quantity, avg_buy_price, market_value, peak_price_adj, last_synced_at
FROM positions;

DROP TABLE positions;
ALTER TABLE positions_new RENAME TO positions;

CREATE TABLE pending_actions_new (
    id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL CHECK (action_type IN (
        'EMERGENCY_ON', 'EMERGENCY_OFF', 'RESUME', 'TRAINING_ON', 'TRAINING_OFF',
        'UNIVERSE_ADD', 'UNIVERSE_REMOVE', 'OVERRIDE_VETO', 'RECONCILE_ADOPT',
        'CLEAR_HALT', 'CLASSIFY_FLOW', 'CA_CONFIRM', 'APPROVE_REAL',
        'APPROVE_SMOKE', 'APPROVE_BACKTEST'
    )),
    args_hash TEXT NOT NULL DEFAULT '',
    code_hash TEXT NOT NULL UNIQUE,
    ticker TEXT,
    reason TEXT NOT NULL,
    approval_status TEXT NOT NULL CHECK (approval_status IN ('PENDING', 'APPROVED', 'EXPIRED', 'REJECTED')),
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    confirmed_at TEXT,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT
);

INSERT INTO pending_actions_new (
    id, action_type, code_hash, ticker, reason, approval_status, created_at, expires_at, confirmed_at
)
SELECT id, action_type, 'legacy:' || id, ticker, reason, approval_status, created_at, expires_at, confirmed_at
FROM pending_actions;

DROP TABLE pending_actions;
ALTER TABLE pending_actions_new RENAME TO pending_actions;

CREATE TABLE order_intents_new (
    parent_intent_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    cycle_id TEXT NOT NULL,
    intent_type TEXT NOT NULL CHECK (intent_type IN (
        'STRATEGY_REBALANCE', 'RISK_REDUCTION', 'PARKING_ALLOCATION',
        'EXIT_OBLIGATION_RETRY', 'SMOKE_TEST'
    )),
    slot_id TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    requested_qty INTEGER NOT NULL CHECK (requested_qty > 0),
    price_policy TEXT NOT NULL DEFAULT 'ASK1' CHECK (price_policy IN ('ASK1', 'BID1', 'EMERGENCY_REPRICE')),
    intent_payload_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('CREATED', 'IN_PROGRESS', 'COMPLETED', 'CLOSED', 'CONFLICT')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT,
    FOREIGN KEY (slot_id) REFERENCES portfolio_slots(slot_id) ON DELETE SET NULL
);

INSERT INTO order_intents_new (
    parent_intent_id, idempotency_key, cycle_id, intent_type, slot_id, ticker, side,
    requested_qty, intent_payload_hash, status, created_at, updated_at
)
SELECT
    parent_intent_id, idempotency_key, cycle_id, intent_type, slot_id, ticker, side,
    requested_qty, intent_payload_hash, status, created_at, updated_at
FROM order_intents;

DROP TABLE order_intents;
ALTER TABLE order_intents_new RENAME TO order_intents;

CREATE TABLE orders_new (
    order_id TEXT PRIMARY KEY,
    parent_intent_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE,
    replaces_order_id TEXT,
    kis_order_no TEXT,
    kis_org_no TEXT,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    requested_qty INTEGER NOT NULL CHECK (requested_qty > 0),
    filled_qty INTEGER NOT NULL DEFAULT 0 CHECK (filled_qty >= 0),
    remaining_qty INTEGER NOT NULL CHECK (remaining_qty >= 0),
    order_type TEXT NOT NULL CHECK (order_type = 'LIMIT'),
    order_price INTEGER NOT NULL CHECK (order_price > 0),
    status TEXT NOT NULL CHECK (status IN (
        'CREATED', 'SUBMITTING', 'PENDING_CONFIRMATION', 'ACCEPTED', 'PARTIALLY_FILLED',
        'CANCEL_PENDING', 'FILLED', 'CANCELED', 'REJECTED', 'CONFIRMED_NOT_ACCEPTED', 'EXPIRED'
    )),
    broker_status_code TEXT,
    last_broker_checked_at TEXT,
    request_payload_hash TEXT,
    response_payload_sanitized TEXT,
    slot_id TEXT,
    strategy_reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    submitted_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK (filled_qty + remaining_qty <= requested_qty),
    FOREIGN KEY (parent_intent_id) REFERENCES order_intents(parent_intent_id) ON DELETE RESTRICT,
    FOREIGN KEY (replaces_order_id) REFERENCES orders(order_id) ON DELETE RESTRICT,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT,
    FOREIGN KEY (slot_id) REFERENCES portfolio_slots(slot_id) ON DELETE SET NULL
);

INSERT INTO orders_new (
    order_id, parent_intent_id, attempt_id, kis_order_no, ticker, side, requested_qty,
    filled_qty, remaining_qty, order_type, order_price, status, broker_status_code,
    last_broker_checked_at, request_payload_hash, response_payload_sanitized, slot_id,
    strategy_reason, created_at, submitted_at, updated_at
)
SELECT
    order_id, parent_intent_id, attempt_id, kis_order_no, ticker, side, requested_qty,
    COALESCE(filled_qty, 0), remaining_qty, order_type, order_price, status, broker_status_code,
    last_broker_checked_at, request_payload_hash, response_payload_sanitized, slot_id,
    strategy_reason, submitted_at, submitted_at, updated_at
FROM orders;

DROP TABLE orders;
ALTER TABLE orders_new RENAME TO orders;

CREATE TABLE trade_fills_new (
    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    kis_execution_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty INTEGER NOT NULL CHECK (qty > 0),
    cumulative_qty INTEGER NOT NULL CHECK (cumulative_qty > 0),
    price REAL NOT NULL CHECK (price > 0),
    price_source TEXT NOT NULL DEFAULT 'REST_DELTA' CHECK (price_source IN ('REST_DELTA', 'WS_NOTICE')),
    fee INTEGER NOT NULL DEFAULT 0,
    tax INTEGER NOT NULL DEFAULT 0,
    executed_at TEXT NOT NULL,
    UNIQUE (order_id, kis_execution_id),
    UNIQUE (order_id, cumulative_qty),
    FOREIGN KEY (order_id) REFERENCES orders(order_id) ON DELETE RESTRICT,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT
);

INSERT INTO trade_fills_new (
    fill_id, order_id, kis_execution_id, ticker, side, qty, cumulative_qty, price,
    fee, tax, executed_at
)
SELECT
    fill_id, order_id, kis_execution_id, ticker, side, qty,
    (SELECT SUM(f2.qty) FROM trade_fills f2 WHERE f2.order_id = trade_fills.order_id AND f2.fill_id <= trade_fills.fill_id),
    price, COALESCE(fee, 0), COALESCE(tax, 0), executed_at
FROM trade_fills;

DROP TABLE trade_fills;
ALTER TABLE trade_fills_new RENAME TO trade_fills;

CREATE TABLE exit_obligations_new (
    obligation_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    source_event_id TEXT NOT NULL UNIQUE,
    required_qty INTEGER NOT NULL CHECK (required_qty > 0),
    filled_qty INTEGER NOT NULL DEFAULT 0 CHECK (filled_qty >= 0 AND filled_qty <= required_qty),
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'RESOLVED', 'SUPERSEDED', 'VOIDED')),
    superseded_by TEXT,
    reason_code TEXT NOT NULL,
    last_order_id TEXT,
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    resolved_at TEXT,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT,
    FOREIGN KEY (superseded_by) REFERENCES exit_obligations(obligation_id) ON DELETE RESTRICT,
    FOREIGN KEY (last_order_id) REFERENCES orders(order_id) ON DELETE SET NULL
);

INSERT INTO exit_obligations_new (
    obligation_id, ticker, source_event_id, required_qty, filled_qty, status,
    reason_code, last_order_id, created_at, last_checked_at, resolved_at
)
SELECT
    obligation_id, ticker, source_event_id, required_qty, filled_qty, status,
    reason_code, last_order_id, created_at, last_checked_at, resolved_at
FROM exit_obligations;

DROP TABLE exit_obligations;
ALTER TABLE exit_obligations_new RENAME TO exit_obligations;

CREATE TABLE daily_portfolio_new (
    snapshot_date TEXT PRIMARY KEY,
    performance_epoch INTEGER NOT NULL DEFAULT 1,
    total_equity INTEGER NOT NULL,
    cash_balance INTEGER NOT NULL,
    parking_value INTEGER NOT NULL,
    risk_asset_value INTEGER NOT NULL,
    unmanaged_value INTEGER NOT NULL DEFAULT 0,
    distribution_receivable INTEGER NOT NULL DEFAULT 0,
    units_outstanding REAL NOT NULL DEFAULT 0 CHECK (units_outstanding >= 0),
    flow_adjusted_unit_nav REAL,
    hwm_flow_adjusted REAL NOT NULL,
    hwm_rebased INTEGER NOT NULL DEFAULT 0 CHECK (hwm_rebased IN (0, 1)),
    hwm_frozen INTEGER NOT NULL DEFAULT 0 CHECK (hwm_frozen IN (0, 1)),
    drawdown_stage TEXT NOT NULL DEFAULT 'NORMAL' CHECK (drawdown_stage IN ('NORMAL', 'CAUTION', 'WARNING', 'CRITICAL')),
    twr_cumulative REAL NOT NULL,
    twr_quality_status TEXT NOT NULL CHECK (twr_quality_status IN ('VALID', 'DATA_INVALID')),
    benchmark_values TEXT NOT NULL,
    data_quality_status TEXT NOT NULL
);

INSERT INTO daily_portfolio_new (
    snapshot_date, total_equity, cash_balance, parking_value, risk_asset_value,
    flow_adjusted_unit_nav, hwm_flow_adjusted, twr_cumulative, twr_quality_status,
    benchmark_values, data_quality_status
)
SELECT
    snapshot_date, total_equity, cash_balance, parking_value, risk_asset_value,
    flow_adjusted_unit_nav, hwm_flow_adjusted, twr_cumulative, twr_quality_status,
    benchmark_values, data_quality_status
FROM daily_portfolio;

DROP TABLE daily_portfolio;
ALTER TABLE daily_portfolio_new RENAME TO daily_portfolio;

CREATE TABLE cash_flows_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_transaction_id TEXT NOT NULL UNIQUE,
    detection_method TEXT NOT NULL DEFAULT 'BROKER_TX' CHECK (detection_method IN ('CASH_RECON', 'BROKER_TX')),
    flow_date TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount <> 0),
    flow_type TEXT NOT NULL CHECK (flow_type IN ('DEPOSIT', 'WITHDRAWAL', 'DISTRIBUTION', 'UNCLASSIFIED')),
    classification_status TEXT NOT NULL CHECK (classification_status IN ('CONFIRMED', 'UNCLASSIFIED_CASH_FLOW')),
    consumed_by_init INTEGER NOT NULL DEFAULT 0 CHECK (consumed_by_init IN (0, 1)),
    unit_price_applied REAL,
    units_delta REAL,
    applied_snapshot_date TEXT,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    confirmed_at TEXT,
    confirmed_via_action_id TEXT,
    memo TEXT,
    CHECK (
        (flow_type IN ('DEPOSIT', 'DISTRIBUTION') AND amount > 0)
        OR (flow_type = 'WITHDRAWAL' AND amount < 0)
        OR flow_type = 'UNCLASSIFIED'
    ),
    CHECK (
        (flow_type = 'UNCLASSIFIED') = (classification_status = 'UNCLASSIFIED_CASH_FLOW')
    ),
    FOREIGN KEY (confirmed_via_action_id) REFERENCES pending_actions(id) ON DELETE RESTRICT
);

INSERT INTO cash_flows_new (
    id, broker_transaction_id, flow_date, occurred_at, amount, flow_type,
    classification_status, source, observed_at, confirmed_at, memo
)
SELECT
    id, broker_transaction_id, flow_date, occurred_at, amount, flow_type,
    classification_status, source, observed_at, confirmed_at, memo
FROM cash_flows;

DROP TABLE cash_flows;
ALTER TABLE cash_flows_new RENAME TO cash_flows;

CREATE TABLE corporate_actions_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_event_id TEXT NOT NULL UNIQUE,
    ticker TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (action_type IN ('CASH_DISTRIBUTION', 'SPLIT', 'MERGE', 'OTHER')),
    ex_date TEXT NOT NULL,
    record_date TEXT,
    pay_date TEXT,
    cash_amount REAL,
    detected_factor REAL,
    adjustment_factor REAL,
    verified INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    verification_source TEXT CHECK (verification_source IN ('ALLOWLIST_SCHEDULE', 'OPERATOR_CONFIRMED')),
    verified_at TEXT,
    source TEXT NOT NULL,
    CHECK (verified = 0 OR (verification_source IS NOT NULL AND adjustment_factor IS NOT NULL)),
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT
);

INSERT INTO corporate_actions_new (
    id, provider_event_id, ticker, action_type, ex_date, record_date, pay_date,
    cash_amount, adjustment_factor, source, verified
)
SELECT
    id, provider_event_id, ticker,
    CASE WHEN action_type IN ('CASH_DISTRIBUTION', 'SPLIT', 'MERGE') THEN action_type ELSE 'OTHER' END,
    ex_date, record_date, pay_date, cash_amount, adjustment_factor, source, 0
FROM corporate_actions;

DROP TABLE corporate_actions;
ALTER TABLE corporate_actions_new RENAME TO corporate_actions;

CREATE TABLE deployment_events_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    deploy_status TEXT NOT NULL DEFAULT 'DEPLOYED' CHECK (deploy_status IN ('QUEUED', 'DEPLOYED', 'ROLLED_BACK', 'SKIPPED')),
    backup_id TEXT,
    migration_status TEXT NOT NULL,
    healthcheck_status TEXT NOT NULL,
    rollback_triggered INTEGER NOT NULL DEFAULT 0 CHECK (rollback_triggered IN (0, 1)),
    created_at TEXT NOT NULL
);

INSERT INTO deployment_events_new (
    id, version, commit_sha, backup_id, migration_status, healthcheck_status,
    rollback_triggered, created_at
)
SELECT
    id, version, commit_sha, backup_id, migration_status, healthcheck_status,
    COALESCE(rollback_triggered, 0), created_at
FROM deployment_events;

DROP TABLE deployment_events;
ALTER TABLE deployment_events_new RENAME TO deployment_events;

CREATE TABLE llm_logs_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL UNIQUE,
    original_rcept_no TEXT,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    model_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    risk_score INTEGER NOT NULL CHECK (risk_score BETWEEN 0 AND 100),
    decision TEXT NOT NULL CHECK (decision IN (
        'RISK_VETO', 'NO_MATERIAL_RISK', 'REVIEW_REQUIRED', 'ANALYSIS_UNAVAILABLE', 'DISABLED'
    )),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    evidence_refs TEXT NOT NULL,
    summary_lines TEXT NOT NULL,
    usage_tokens INTEGER NOT NULL,
    reserved_cost_usd REAL NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT
);

INSERT INTO llm_logs_new (
    id, dedup_key, created_at, ticker, trigger_type, model_id, schema_version,
    risk_score, decision, confidence, evidence_refs, summary_lines, usage_tokens, cost_usd
)
SELECT
    id, dedup_key, created_at, ticker, trigger_type, model_id, schema_version,
    risk_score, decision, confidence, evidence_refs, summary_lines, usage_tokens, cost_usd
FROM llm_logs;

DROP TABLE llm_logs;
ALTER TABLE llm_logs_new RENAME TO llm_logs;

CREATE TABLE schema_migrations_new (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    checksum TEXT NOT NULL,
    backward_compatible INTEGER NOT NULL DEFAULT 0 CHECK (backward_compatible IN (0, 1))
);

INSERT INTO schema_migrations_new (version, applied_at, checksum)
SELECT version, applied_at, checksum FROM schema_migrations;

DROP TABLE schema_migrations;
ALTER TABLE schema_migrations_new RENAME TO schema_migrations;

-- 신규 테이블 3종 -----------------------------------------------------------
CREATE TABLE safety_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_type TEXT NOT NULL CHECK (flag_type IN ('HALT', 'BUY_BLOCK', 'TICKER_BLOCK')),
    code TEXT NOT NULL CHECK (code IN (
        'ACCOUNT_MISMATCH', 'PENDING_CONFIRMATION', 'DB_INTEGRITY', 'CALENDAR_MISMATCH',
        'CREDENTIALS_UNAVAILABLE', 'UPS_LOW', 'CLOCK_UNSYNCED', 'BOOT_RECOVERY_FAILED',
        'DEPLOY_ROLLBACK', 'CONFIG_INVALID',
        'UNCLASSIFIED_CASH_FLOW', 'ACCOUNTING_INVALID', 'ACCOUNT_DATA_STALE',
        'CORPORATE_ACTION_UNVERIFIED', 'QUOTE_INVALID', 'PRICE_SERIES_INVALID', 'TRADING_HALTED'
    )),
    ticker TEXT,
    auto_clearable INTEGER NOT NULL CHECK (auto_clearable IN (0, 1)),
    detail_sanitized TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    cleared_at TEXT,
    cleared_by TEXT,
    CHECK ((flag_type = 'TICKER_BLOCK') = (ticker IS NOT NULL))
);

CREATE TABLE backtest_approvals (
    approval_id TEXT PRIMARY KEY,
    backtest_run_id TEXT NOT NULL,
    code_commit_sha TEXT NOT NULL,
    strategy_params_hash TEXT NOT NULL,
    data_range_start TEXT NOT NULL,
    data_range_end TEXT NOT NULL,
    report_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('APPROVED', 'REVOKED')),
    approved_at TEXT NOT NULL,
    approved_via_action_id TEXT NOT NULL,
    FOREIGN KEY (approved_via_action_id) REFERENCES pending_actions(id) ON DELETE RESTRICT
);

CREATE TABLE operator_approvals (
    approval_id TEXT PRIMARY KEY,
    approval_type TEXT NOT NULL CHECK (approval_type IN ('REAL_ACTIVATION', 'SMOKE_SESSION')),
    host_file_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CONSUMED', 'EXPIRED', 'REVOKED')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    approved_via_action_id TEXT NOT NULL,
    FOREIGN KEY (approved_via_action_id) REFERENCES pending_actions(id) ON DELETE RESTRICT
);

CREATE VIEW halt_reasons AS
    SELECT * FROM safety_flags WHERE flag_type = 'HALT' AND cleared_at IS NULL;

-- 인덱스 (재구성으로 사라진 것 포함) ---------------------------------------------
CREATE INDEX idx_orders_status_updated ON orders(status, updated_at);
CREATE INDEX idx_orders_parent_intent_submitted ON orders(parent_intent_id, submitted_at);
CREATE INDEX idx_orders_ticker_submitted ON orders(ticker, submitted_at);
CREATE INDEX idx_trade_fills_executed ON trade_fills(executed_at);
CREATE INDEX idx_exit_obligations_status_ticker ON exit_obligations(status, ticker);
CREATE UNIQUE INDEX idx_exit_obligations_one_open_per_ticker ON exit_obligations(ticker) WHERE status = 'OPEN';
CREATE UNIQUE INDEX ux_orders_one_active_attempt ON orders(parent_intent_id)
    WHERE replaces_order_id IS NULL AND status IN (
        'CREATED', 'SUBMITTING', 'PENDING_CONFIRMATION', 'ACCEPTED', 'PARTIALLY_FILLED', 'CANCEL_PENDING'
    );
CREATE UNIQUE INDEX ux_orders_one_active_replacement ON orders(parent_intent_id)
    WHERE replaces_order_id IS NOT NULL AND status IN (
        'CREATED', 'SUBMITTING', 'PENDING_CONFIRMATION', 'ACCEPTED', 'PARTIALLY_FILLED', 'CANCEL_PENDING'
    );
CREATE UNIQUE INDEX ux_safety_flag_active ON safety_flags(flag_type, code, IFNULL(ticker, ''))
    WHERE cleared_at IS NULL;
CREATE UNIQUE INDEX ux_slot_ticker ON portfolio_slots(ticker) WHERE ticker IS NOT NULL;
CREATE INDEX idx_order_intents_ticker ON order_intents(ticker, status);
CREATE INDEX idx_safety_flags_active ON safety_flags(flag_type, cleared_at);

-- 트리거 --------------------------------------------------------------------
CREATE TRIGGER trg_orders_match_intent
BEFORE INSERT ON orders
BEGIN
    SELECT RAISE(ABORT, 'order ticker and side must match its intent')
    WHERE NOT EXISTS (
        SELECT 1 FROM order_intents AS i
        WHERE i.parent_intent_id = NEW.parent_intent_id
          AND i.ticker = NEW.ticker
          AND i.side = NEW.side
    );
END;

-- 종결 상태의 status 는 바꿀 수 없고, 체결 수량은 늘어나는(LATE_FILL_CORRECTION) 방향만 허용한다.
CREATE TRIGGER trg_terminal_immutable
BEFORE UPDATE ON orders
WHEN OLD.status IN ('FILLED', 'CANCELED', 'REJECTED', 'CONFIRMED_NOT_ACCEPTED', 'EXPIRED')
BEGIN
    SELECT RAISE(ABORT, 'terminal order status is immutable')
    WHERE NEW.status <> OLD.status OR NEW.filled_qty < OLD.filled_qty;
END;
