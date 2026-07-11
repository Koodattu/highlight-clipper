CREATE TABLE analysis_stage_reuses (
    analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    stage_name TEXT NOT NULL CHECK (stage_name IN ('asr', 'embeddings')),
    producer_analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    producer_stage_attempt_id TEXT NOT NULL REFERENCES stage_attempts(id),
    output_fingerprint TEXT NOT NULL CHECK (length(output_fingerprint) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY (analysis_run_id, stage_name),
    CHECK (analysis_run_id <> producer_analysis_run_id)
);

CREATE INDEX idx_analysis_stage_reuse_producer
ON analysis_stage_reuses(producer_analysis_run_id, stage_name);
