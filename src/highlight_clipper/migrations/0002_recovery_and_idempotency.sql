ALTER TABLE source_import_attempts ADD COLUMN planned_source_recording_id TEXT;
ALTER TABLE editorial_decisions ADD COLUMN request_fingerprint TEXT CHECK (
    request_fingerprint IS NULL OR length(request_fingerprint) = 64
);
ALTER TABLE exports ADD COLUMN request_fingerprint TEXT CHECK (
    request_fingerprint IS NULL OR length(request_fingerprint) = 64
);

CREATE TABLE export_requests (
    idempotency_key TEXT PRIMARY KEY,
    request_fingerprint TEXT NOT NULL CHECK (length(request_fingerprint) = 64),
    export_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'failed')),
    output_relpath TEXT NOT NULL UNIQUE,
    owner_instance TEXT NOT NULL,
    attempt_number INTEGER NOT NULL DEFAULT 1 CHECK (attempt_number > 0),
    error_summary TEXT CHECK (length(error_summary) <= 2000),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_export_request_state ON export_requests(state);

