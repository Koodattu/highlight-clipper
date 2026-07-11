CREATE TABLE boundary_reanalysis_targets (
    analysis_run_id TEXT PRIMARY KEY REFERENCES analysis_runs(id),
    parent_queue_snapshot_id TEXT NOT NULL REFERENCES queue_snapshots(id),
    superseded_proposal_id TEXT NOT NULL REFERENCES clip_proposals(id),
    source_editorial_decision_id TEXT NOT NULL REFERENCES editorial_decisions(id),
    boundary_edit_id TEXT NOT NULL REFERENCES boundary_edits(id),
    requested_start_us INTEGER NOT NULL CHECK (requested_start_us >= 0),
    requested_end_us INTEGER NOT NULL CHECK (requested_end_us > requested_start_us),
    created_at TEXT NOT NULL,
    UNIQUE (parent_queue_snapshot_id, boundary_edit_id)
);

CREATE INDEX idx_boundary_reanalysis_superseded
ON boundary_reanalysis_targets(superseded_proposal_id, created_at);
