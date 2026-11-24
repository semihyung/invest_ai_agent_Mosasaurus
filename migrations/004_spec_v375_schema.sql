-- backward_compatible: false
-- 명세 v3.7.5 9.2절 스키마 정합화: 정정 체인 삭제(`orders.replaces_order_id`, 정정 인덱스,
-- `EMERGENCY_REPRICE`), 매도 의무의 체결 연결(`order_intents.obligation_id`), 현금흐름
-- `PROVISIONAL`/`DISMISSED`/`ADJUSTMENT`, 일일 스냅샷의 오염 제거 금액(`unclassified_excluded_amount`),
-- 종결 불변 트리거 정정, 의무 수량 불변 트리거를 반영한다.
-- 003 과 같은 SQLite 표준 절차(새 테이블 생성 → 복사 → 기존 삭제 → 이름 변경)를 쓰며,
-- 실행 전 db.py 가 `PRAGMA foreign_keys = OFF` 를 설정하고 커밋 전 foreign_key_check 로 검증한다.

-- 003 의 트리거는 테이블 재구성 중 이름 변경 검사에 걸리므로 먼저 지우고 마지막에 다시 만든다.
DROP TRIGGER trg_orders_match_intent;
DROP TRIGGER trg_terminal_immutable;

-- exit_obligations: orders 와의 순환 FK(`last_order_id`)를 제거한다.
CREATE TABLE exit_obligations_new (
    obligation_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    source_event_id TEXT NOT NULL UNIQUE,
    required_qty INTEGER NOT NULL CHECK (required_qty > 0),
    filled_qty INTEGER NOT NULL DEFAULT 0 CHECK (filled_qty >= 0 AND filled_qty <= required_qty),
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'RESOLVED', 'SUPERSEDED', 'VOIDED')),
    superseded_by TEXT,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_checked_at TEXT,
    resolved_at TEXT,
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT,
    FOREIGN KEY (superseded_by) REFERENCES exit_obligations(obligation_id) ON DELETE RESTRICT
);

INSERT INTO exit_obligations_new (
    obligation_id, ticker, source_event_id, required_qty, filled_qty, status,
    superseded_by, reason_code, created_at, last_checked_at, resolved_at
)
SELECT
    obligation_id, ticker, source_event_id, required_qty, filled_qty, status,
    superseded_by, reason_code, created_at, last_checked_at, resolved_at
FROM exit_obligations;

DROP TABLE exit_obligations;
ALTER TABLE exit_obligations_new RENAME TO exit_obligations;

-- order_intents: 가격 정책에서 EMERGENCY_REPRICE 삭제, obligation_id 추가.
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
    price_policy TEXT NOT NULL DEFAULT 'ASK1' CHECK (price_policy IN ('ASK1', 'BID1')),
    obligation_id TEXT,
    intent_payload_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('CREATED', 'IN_PROGRESS', 'COMPLETED', 'CLOSED', 'CONFLICT')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (intent_type <> 'EXIT_OBLIGATION_RETRY' OR obligation_id IS NOT NULL),
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT,
    FOREIGN KEY (slot_id) REFERENCES portfolio_slots(slot_id) ON DELETE SET NULL,
    FOREIGN KEY (obligation_id) REFERENCES exit_obligations(obligation_id) ON DELETE RESTRICT
);

-- 기존 데이터에 EXIT_OBLIGATION_RETRY 의도가 있으면 obligation_id 가 없어 CHECK 로 거부된다
-- (Phase 1 은 REAL 전 단계라 해당 행이 없어야 하며, 있으면 마이그레이션을 중단시켜 사람이 확인한다).
INSERT INTO order_intents_new (
    parent_intent_id, idempotency_key, cycle_id, intent_type, slot_id, ticker, side,
    requested_qty, price_policy, intent_payload_hash, status, created_at, updated_at
)
SELECT
    parent_intent_id, idempotency_key, cycle_id, intent_type, slot_id, ticker, side,
    requested_qty,
    CASE WHEN price_policy = 'BID1' THEN 'BID1' ELSE 'ASK1' END,
    intent_payload_hash, status, created_at, updated_at
FROM order_intents;

DROP TABLE order_intents;
ALTER TABLE order_intents_new RENAME TO order_intents;

-- orders: replaces_order_id 삭제.
CREATE TABLE orders_new (
    order_id TEXT PRIMARY KEY,
    parent_intent_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE,
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
    FOREIGN KEY (ticker) REFERENCES universe_assets(ticker) ON DELETE RESTRICT,
    FOREIGN KEY (slot_id) REFERENCES portfolio_slots(slot_id) ON DELETE SET NULL
);

INSERT INTO orders_new (
    order_id, parent_intent_id, attempt_id, kis_order_no, kis_org_no, ticker, side,
    requested_qty, filled_qty, remaining_qty, order_type, order_price, status,
    broker_status_code, last_broker_checked_at, request_payload_hash,
    response_payload_sanitized, slot_id, strategy_reason, created_at, submitted_at, updated_at
)
SELECT
    order_id, parent_intent_id, attempt_id, kis_order_no, kis_org_no, ticker, side,
    requested_qty, filled_qty, remaining_qty, order_type, order_price, status,
    broker_status_code, last_broker_checked_at, request_payload_hash,
    response_payload_sanitized, slot_id, strategy_reason, created_at, submitted_at, updated_at
FROM orders;

DROP TABLE orders;
ALTER TABLE orders_new RENAME TO orders;

-- daily_portfolio: 오염 제거 금액과 재계산 표시 추가.
CREATE TABLE daily_portfolio_new (
    snapshot_date TEXT PRIMARY KEY,
    performance_epoch INTEGER NOT NULL DEFAULT 1,
    total_equity INTEGER NOT NULL,
    cash_balance INTEGER NOT NULL,
    parking_value INTEGER NOT NULL,
    risk_asset_value INTEGER NOT NULL,
    unmanaged_value INTEGER NOT NULL DEFAULT 0,
    distribution_receivable INTEGER NOT NULL DEFAULT 0,
    unclassified_excluded_amount INTEGER NOT NULL DEFAULT 0,
    restated INTEGER NOT NULL DEFAULT 0 CHECK (restated IN (0, 1)),
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
    snapshot_date, performance_epoch, total_equity, cash_balance, parking_value,
    risk_asset_value, unmanaged_value, distribution_receivable, units_outstanding,
    flow_adjusted_unit_nav, hwm_flow_adjusted, hwm_rebased, hwm_frozen, drawdown_stage,
    twr_cumulative, twr_quality_status, benchmark_values, data_quality_status
)
SELECT
    snapshot_date, performance_epoch, total_equity, cash_balance, parking_value,
    risk_asset_value, unmanaged_value, distribution_receivable, units_outstanding,
    flow_adjusted_unit_nav, hwm_flow_adjusted, hwm_rebased, hwm_frozen, drawdown_stage,
    twr_cumulative, twr_quality_status, benchmark_values, data_quality_status
FROM daily_portfolio;

DROP TABLE daily_portfolio;
ALTER TABLE daily_portfolio_new RENAME TO daily_portfolio;

-- cash_flows: ADJUSTMENT 유형, PROVISIONAL/DISMISSED 상태, 재관측 시각 추가.
CREATE TABLE cash_flows_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_transaction_id TEXT NOT NULL UNIQUE,
    detection_method TEXT NOT NULL DEFAULT 'BROKER_TX' CHECK (detection_method IN ('CASH_RECON', 'BROKER_TX')),
    flow_date TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount <> 0),
    flow_type TEXT NOT NULL CHECK (flow_type IN (
        'DEPOSIT', 'WITHDRAWAL', 'DISTRIBUTION', 'ADJUSTMENT', 'UNCLASSIFIED'
    )),
    classification_status TEXT NOT NULL CHECK (classification_status IN (
        'PROVISIONAL', 'CONFIRMED', 'UNCLASSIFIED_CASH_FLOW', 'DISMISSED'
    )),
    consumed_by_init INTEGER NOT NULL DEFAULT 0 CHECK (consumed_by_init IN (0, 1)),
    unit_price_applied REAL,
    units_delta REAL,
    applied_snapshot_date TEXT,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    first_observed_at TEXT NOT NULL,
    reconfirmed_at TEXT,
    confirmed_at TEXT,
    confirmed_via_action_id TEXT,
    memo TEXT,
    CHECK (
        (flow_type IN ('DEPOSIT', 'DISTRIBUTION') AND amount > 0)
        OR (flow_type = 'WITHDRAWAL' AND amount < 0)
        OR flow_type IN ('ADJUSTMENT', 'UNCLASSIFIED')
    ),
    -- PROVISIONAL·UNCLASSIFIED_CASH_FLOW 는 미분류 행에만, CONFIRMED 는 미분류가 아닌 행에만 허용한다.
    CHECK (classification_status NOT IN ('PROVISIONAL', 'UNCLASSIFIED_CASH_FLOW') OR flow_type = 'UNCLASSIFIED'),
    CHECK (classification_status <> 'CONFIRMED' OR flow_type <> 'UNCLASSIFIED'),
    FOREIGN KEY (confirmed_via_action_id) REFERENCES pending_actions(id) ON DELETE RESTRICT
);

INSERT INTO cash_flows_new (
    id, broker_transaction_id, detection_method, flow_date, occurred_at, amount, flow_type,
    classification_status, consumed_by_init, unit_price_applied, units_delta,
    applied_snapshot_date, source, observed_at, first_observed_at, confirmed_at,
    confirmed_via_action_id, memo
)
SELECT
    id, broker_transaction_id, detection_method, flow_date, occurred_at, amount, flow_type,
    classification_status, consumed_by_init, unit_price_applied, units_delta,
    applied_snapshot_date, source, observed_at, observed_at, confirmed_at,
    confirmed_via_action_id, memo
FROM cash_flows;

DROP TABLE cash_flows;
ALTER TABLE cash_flows_new RENAME TO cash_flows;

-- 인덱스 (재구성으로 사라진 것 포함) ---------------------------------------------
CREATE INDEX idx_orders_status_updated ON orders(status, updated_at);
CREATE INDEX idx_orders_parent_intent_submitted ON orders(parent_intent_id, submitted_at);
CREATE INDEX idx_orders_ticker_submitted ON orders(ticker, submitted_at);
CREATE INDEX idx_exit_obligations_status_ticker ON exit_obligations(status, ticker);
CREATE UNIQUE INDEX idx_exit_obligations_one_open_per_ticker ON exit_obligations(ticker) WHERE status = 'OPEN';
-- 같은 의도의 attempt 는 동시에 1건만 활성일 수 있다(정정 체인 삭제로 인덱스 1개로 통일).
CREATE UNIQUE INDEX ux_orders_one_active_attempt ON orders(parent_intent_id)
    WHERE status IN (
        'CREATED', 'SUBMITTING', 'PENDING_CONFIRMATION', 'ACCEPTED', 'PARTIALLY_FILLED', 'CANCEL_PENDING'
    );
CREATE INDEX idx_order_intents_ticker ON order_intents(ticker, status);
CREATE INDEX idx_order_intents_obligation ON order_intents(obligation_id);

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

-- 종결 주문: status 불변, 체결 수량은 늘어나는 방향(LATE_FILL_CORRECTION)만 허용하고, 그때 잔량은 0 으로 고정한다.
-- 잔량을 늘리는 변경도 거부한다(체결·취소 후 남은 수량을 되살릴 수 없다).
CREATE TRIGGER trg_terminal_immutable
BEFORE UPDATE ON orders
WHEN OLD.status IN ('FILLED', 'CANCELED', 'REJECTED', 'CONFIRMED_NOT_ACCEPTED', 'EXPIRED')
BEGIN
    SELECT RAISE(ABORT, 'terminal order is immutable')
    WHERE NEW.status <> OLD.status
       OR NEW.filled_qty < OLD.filled_qty
       OR NEW.remaining_qty > OLD.remaining_qty
       OR (NEW.filled_qty > OLD.filled_qty AND NEW.remaining_qty <> 0);
END;

-- 매도 의무의 필요 수량은 생성 후 바꿀 수 없다. 수량이 달라지면 SUPERSEDED + 새 OPEN 으로 대체한다.
CREATE TRIGGER trg_obligation_required_immutable
BEFORE UPDATE ON exit_obligations
WHEN NEW.required_qty <> OLD.required_qty
BEGIN
    SELECT RAISE(ABORT, 'exit obligation required_qty is immutable');
END;
