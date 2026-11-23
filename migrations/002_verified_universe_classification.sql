ALTER TABLE universe_assets
    ADD COLUMN leverage_multiplier REAL NOT NULL DEFAULT 1.0
    CHECK (leverage_multiplier = 1.0);

ALTER TABLE universe_assets
    ADD COLUMN is_inverse INTEGER NOT NULL DEFAULT 0
    CHECK (is_inverse = 0);

ALTER TABLE universe_assets
    ADD COLUMN is_futures_based INTEGER NOT NULL DEFAULT 0
    CHECK (is_futures_based = 0);

ALTER TABLE universe_assets
    ADD COLUMN classification_verified INTEGER NOT NULL DEFAULT 0
    CHECK (classification_verified IN (0, 1));

ALTER TABLE universe_assets
    ADD COLUMN classification_source TEXT NOT NULL DEFAULT 'UNVERIFIED';

ALTER TABLE universe_assets
    ADD COLUMN verified_by TEXT NOT NULL DEFAULT 'UNVERIFIED';

ALTER TABLE universe_assets
    ADD COLUMN verified_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00Z';
