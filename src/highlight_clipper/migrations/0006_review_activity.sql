CREATE TABLE review_activity_events (
    id TEXT PRIMARY KEY,
    queue_snapshot_id TEXT NOT NULL REFERENCES queue_snapshots(id),
    clip_proposal_id TEXT NOT NULL REFERENCES clip_proposals(id),
    session_id TEXT NOT NULL CHECK (length(session_id) BETWEEN 8 AND 128),
    sequence_number INTEGER NOT NULL CHECK (sequence_number >= 0),
    active_milliseconds INTEGER NOT NULL CHECK (active_milliseconds BETWEEN 1 AND 15000),
    activity_kind TEXT NOT NULL CHECK (activity_kind IN ('playback', 'interaction')),
    created_at TEXT NOT NULL,
    UNIQUE (session_id, sequence_number)
);

CREATE INDEX idx_review_activity_queue_time ON review_activity_events(queue_snapshot_id, created_at, id);
