ALTER TABLE clip_proposals ADD COLUMN reasons_against_selection_json TEXT NOT NULL DEFAULT '[]' CHECK (
    json_valid(reasons_against_selection_json)
);

ALTER TABLE evaluation_attempts ADD COLUMN runtime_metadata_json TEXT CHECK (
    runtime_metadata_json IS NULL OR json_valid(runtime_metadata_json)
);

CREATE TABLE embedding_generations (
    id TEXT PRIMARY KEY,
    analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    model_profile TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
    configuration_fingerprint TEXT NOT NULL CHECK (length(configuration_fingerprint) = 64),
    vector_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts(id),
    manifest_artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts(id),
    dimension INTEGER NOT NULL CHECK (dimension > 0),
    dtype TEXT NOT NULL,
    document_count INTEGER NOT NULL CHECK (document_count >= 0),
    query_count INTEGER NOT NULL CHECK (query_count >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (analysis_run_id, model_profile, input_fingerprint, configuration_fingerprint)
);

CREATE INDEX idx_embedding_generation_run ON embedding_generations(analysis_run_id);

CREATE TABLE recovery_items (
    id TEXT PRIMARY KEY,
    item_type TEXT NOT NULL CHECK (item_type IN ('unregistered_source_tree')),
    relative_path TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('pending', 'completed')),
    error_summary TEXT CHECK (length(error_summary) <= 2000),
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX idx_recovery_item_state ON recovery_items(state);
