CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    event_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (source, event_key)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    job_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'completed', 'retry_wait', 'needs_review')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_type, source_key)
);

CREATE INDEX IF NOT EXISTS jobs_due_idx ON jobs (status, available_at, id);
CREATE INDEX IF NOT EXISTS jobs_resource_lease_idx ON jobs (resource_key, status, lease_until);

CREATE TABLE IF NOT EXISTS order_links (
    meli_order_id TEXT PRIMARY KEY,
    shopify_order_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_links (
    review_key TEXT PRIMARY KEY,
    shopify_draft_order_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_logs (
    id INTEGER PRIMARY KEY,
    job_id INTEGER,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_key TEXT PRIMARY KEY,
    checkpoint_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
