CREATE TABLE source_import_attempts (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
    input_path TEXT NOT NULL,
    requested_video_stream INTEGER,
    requested_audio_stream INTEGER,
    owner_instance TEXT,
    progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
    error_code TEXT,
    error_summary TEXT CHECK (length(error_summary) <= 2000),
    retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT
);

CREATE TABLE source_recordings (
    id TEXT PRIMARY KEY,
    import_attempt_id TEXT NOT NULL UNIQUE REFERENCES source_import_attempts(id),
    original_name TEXT NOT NULL,
    original_relpath TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    source_end_us INTEGER NOT NULL CHECK (source_end_us > 0),
    video_stream_index INTEGER NOT NULL,
    audio_stream_index INTEGER NOT NULL,
    media_manifest_json TEXT NOT NULL CHECK (json_valid(media_manifest_json)),
    created_at TEXT NOT NULL
);

CREATE TABLE creator_profile_revisions (
    id TEXT PRIMARY KEY,
    revision_number INTEGER NOT NULL UNIQUE CHECK (revision_number > 0),
    languages_json TEXT NOT NULL CHECK (json_valid(languages_json)),
    category_priorities_json TEXT NOT NULL CHECK (json_valid(category_priorities_json)),
    desired_content TEXT NOT NULL,
    avoided_content TEXT NOT NULL,
    preferred_durations_json TEXT NOT NULL CHECK (json_valid(preferred_durations_json)),
    created_at TEXT NOT NULL
);

CREATE TABLE analysis_runs (
    id TEXT PRIMARY KEY,
    source_recording_id TEXT NOT NULL REFERENCES source_recordings(id),
    creator_profile_revision_id TEXT NOT NULL REFERENCES creator_profile_revisions(id),
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
    input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
    configuration_fingerprint TEXT NOT NULL CHECK (length(configuration_fingerprint) = 64),
    configuration_json TEXT NOT NULL CHECK (json_valid(configuration_json)),
    requested_more_from_run_id TEXT REFERENCES analysis_runs(id),
    queue_snapshot_id TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    coverage_saturated INTEGER NOT NULL DEFAULT 0 CHECK (coverage_saturated IN (0, 1)),
    token_saturated INTEGER NOT NULL DEFAULT 0 CHECK (token_saturated IN (0, 1)),
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE stage_attempts (
    id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('source_import', 'analysis', 'export')),
    scope_id TEXT NOT NULL,
    stage_name TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
    input_fingerprint TEXT NOT NULL CHECK (length(input_fingerprint) = 64),
    configuration_fingerprint TEXT NOT NULL CHECK (length(configuration_fingerprint) = 64),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    prior_attempt_id TEXT REFERENCES stage_attempts(id),
    checkpoint_json TEXT CHECK (checkpoint_json IS NULL OR json_valid(checkpoint_json)),
    owner_instance TEXT,
    worker_pid INTEGER,
    progress REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
    retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
    error_code TEXT,
    error_summary TEXT CHECK (length(error_summary) <= 2000),
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    UNIQUE (scope_type, scope_id, stage_name, attempt_number)
);

CREATE TABLE cancellation_requests (
    attempt_id TEXT PRIMARY KEY REFERENCES stage_attempts(id),
    requested_at TEXT NOT NULL,
    requester TEXT NOT NULL
);

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    source_recording_id TEXT REFERENCES source_recordings(id),
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT CHECK (sha256 IS NULL OR length(sha256) = 64),
    integrity_json TEXT NOT NULL CHECK (json_valid(integrity_json)),
    configuration_fingerprint TEXT NOT NULL CHECK (length(configuration_fingerprint) = 64),
    regenerable INTEGER NOT NULL DEFAULT 0 CHECK (regenerable IN (0, 1)),
    removed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE transcript_segments (
    id TEXT PRIMARY KEY,
    analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    sequence_number INTEGER NOT NULL CHECK (sequence_number >= 0),
    start_us INTEGER NOT NULL CHECK (start_us >= 0),
    end_us INTEGER NOT NULL CHECK (end_us > start_us),
    raw_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    language TEXT,
    producer_fingerprint TEXT NOT NULL CHECK (length(producer_fingerprint) = 64),
    UNIQUE (analysis_run_id, sequence_number)
);

CREATE TABLE evidence_items (
    id TEXT PRIMARY KEY,
    source_recording_id TEXT NOT NULL REFERENCES source_recordings(id),
    analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    producer_generation TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    start_us INTEGER NOT NULL CHECK (start_us >= 0),
    end_us INTEGER NOT NULL CHECK (end_us > start_us),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    locator_json TEXT NOT NULL CHECK (json_valid(locator_json)),
    UNIQUE (analysis_run_id, producer_generation, evidence_type, content_hash, start_us, end_us)
);

CREATE TABLE observations (
    id TEXT PRIMARY KEY,
    evidence_item_id TEXT NOT NULL UNIQUE REFERENCES evidence_items(id),
    observation_type TEXT NOT NULL,
    numeric_value REAL,
    metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json))
);

CREATE TABLE candidate_moments (
    id TEXT PRIMARY KEY,
    analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    generator_name TEXT NOT NULL,
    generator_version TEXT NOT NULL,
    anchor_us INTEGER NOT NULL CHECK (anchor_us >= 0),
    start_us INTEGER,
    end_us INTEGER,
    local_confidence REAL NOT NULL,
    category_hint TEXT CHECK (category_hint IS NULL OR category_hint IN ('reaction', 'comedy', 'story', 'opinion', 'explanation')),
    idempotency_key TEXT NOT NULL,
    CHECK ((start_us IS NULL AND end_us IS NULL) OR (start_us >= 0 AND end_us > start_us)),
    UNIQUE (analysis_run_id, generator_name, generator_version, idempotency_key)
);

CREATE TABLE candidate_evidence (
    candidate_moment_id TEXT NOT NULL REFERENCES candidate_moments(id),
    evidence_item_id TEXT NOT NULL REFERENCES evidence_items(id),
    PRIMARY KEY (candidate_moment_id, evidence_item_id)
);

CREATE TABLE candidate_clusters (
    id TEXT PRIMARY KEY,
    analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    start_us INTEGER NOT NULL CHECK (start_us >= 0),
    end_us INTEGER NOT NULL CHECK (end_us > start_us),
    clustering_version TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    UNIQUE (analysis_run_id, idempotency_key)
);

CREATE TABLE cluster_members (
    candidate_cluster_id TEXT NOT NULL REFERENCES candidate_clusters(id),
    candidate_moment_id TEXT NOT NULL REFERENCES candidate_moments(id),
    PRIMARY KEY (candidate_cluster_id, candidate_moment_id)
);

CREATE TABLE context_envelopes (
    id TEXT PRIMARY KEY,
    candidate_cluster_id TEXT NOT NULL UNIQUE REFERENCES candidate_clusters(id),
    analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    start_us INTEGER NOT NULL CHECK (start_us >= 0),
    end_us INTEGER NOT NULL CHECK (end_us > start_us),
    package_fingerprint TEXT NOT NULL CHECK (length(package_fingerprint) = 64),
    disposition TEXT CHECK (disposition IS NULL OR disposition IN ('proposal_set', 'semantic_rejection', 'insufficient_context', 'input_too_large', 'invalid_for_profile'))
);

CREATE TABLE boundary_anchors (
    id TEXT PRIMARY KEY,
    context_envelope_id TEXT NOT NULL REFERENCES context_envelopes(id),
    source_time_us INTEGER NOT NULL CHECK (source_time_us >= 0),
    anchor_type TEXT NOT NULL,
    evidence_item_id TEXT REFERENCES evidence_items(id),
    UNIQUE (context_envelope_id, source_time_us, anchor_type, evidence_item_id)
);

CREATE TABLE evaluation_attempts (
    id TEXT PRIMARY KEY,
    context_envelope_id TEXT NOT NULL REFERENCES context_envelopes(id),
    model_profile TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    prompt_hash TEXT CHECK (prompt_hash IS NULL OR length(prompt_hash) = 64),
    prompt_tokens INTEGER CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
    reasoning_tokens INTEGER CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
    final_tokens INTEGER CHECK (final_tokens IS NULL OR final_tokens >= 0),
    raw_response_relpath TEXT,
    validation_errors_json TEXT CHECK (validation_errors_json IS NULL OR json_valid(validation_errors_json)),
    disposition TEXT CHECK (disposition IS NULL OR disposition IN ('proposal_set', 'semantic_rejection', 'insufficient_context', 'input_too_large', 'invalid_for_profile')),
    started_at TEXT,
    ended_at TEXT,
    UNIQUE (context_envelope_id, model_profile, attempt_number)
);

CREATE TABLE clip_proposals (
    id TEXT PRIMARY KEY,
    analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(id),
    context_envelope_id TEXT NOT NULL REFERENCES context_envelopes(id),
    evaluation_attempt_id TEXT NOT NULL REFERENCES evaluation_attempts(id),
    category TEXT NOT NULL CHECK (category IN ('reaction', 'comedy', 'story', 'opinion', 'explanation')),
    summary TEXT NOT NULL,
    start_us INTEGER NOT NULL CHECK (start_us >= 0),
    end_us INTEGER NOT NULL CHECK (end_us > start_us),
    event_us INTEGER NOT NULL,
    setup_start_us INTEGER,
    hook_us INTEGER,
    payoff_us INTEGER,
    exit_us INTEGER,
    judgments_json TEXT NOT NULL CHECK (json_valid(judgments_json)),
    duration_exception_reason TEXT,
    supersedes_proposal_id TEXT REFERENCES clip_proposals(id),
    created_at TEXT NOT NULL,
    CHECK (event_us >= start_us AND event_us < end_us),
    CHECK (setup_start_us IS NULL OR (setup_start_us >= start_us AND setup_start_us <= event_us)),
    CHECK (hook_us IS NULL OR (hook_us >= start_us AND hook_us <= event_us)),
    CHECK (payoff_us IS NULL OR (payoff_us >= event_us AND payoff_us < end_us)),
    CHECK (exit_us IS NULL OR (exit_us >= event_us AND exit_us <= end_us)),
    CHECK (end_us - start_us <= 240000000)
);

CREATE TABLE proposal_evidence (
    clip_proposal_id TEXT NOT NULL REFERENCES clip_proposals(id),
    evidence_item_id TEXT NOT NULL REFERENCES evidence_items(id),
    PRIMARY KEY (clip_proposal_id, evidence_item_id)
);

CREATE TABLE proposal_candidates (
    clip_proposal_id TEXT NOT NULL REFERENCES clip_proposals(id),
    candidate_moment_id TEXT NOT NULL REFERENCES candidate_moments(id),
    PRIMARY KEY (clip_proposal_id, candidate_moment_id)
);

CREATE TABLE proposal_risks (
    clip_proposal_id TEXT NOT NULL REFERENCES clip_proposals(id),
    risk_kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (clip_proposal_id, risk_kind, reason)
);

CREATE TABLE candidate_outcomes (
    evaluation_attempt_id TEXT NOT NULL REFERENCES evaluation_attempts(id),
    candidate_moment_id TEXT NOT NULL REFERENCES candidate_moments(id),
    outcome TEXT NOT NULL,
    clip_proposal_id TEXT REFERENCES clip_proposals(id),
    reason TEXT,
    PRIMARY KEY (evaluation_attempt_id, candidate_moment_id)
);

CREATE TABLE queue_snapshots (
    id TEXT PRIMARY KEY,
    analysis_run_id TEXT NOT NULL UNIQUE REFERENCES analysis_runs(id),
    ranking_version TEXT NOT NULL,
    ranking_configuration_json TEXT NOT NULL CHECK (json_valid(ranking_configuration_json)),
    created_at TEXT NOT NULL
);

CREATE TABLE queue_entries (
    queue_snapshot_id TEXT NOT NULL REFERENCES queue_snapshots(id),
    clip_proposal_id TEXT NOT NULL REFERENCES clip_proposals(id),
    rank INTEGER NOT NULL CHECK (rank > 0),
    baseline_score REAL NOT NULL,
    diversity_json TEXT NOT NULL CHECK (json_valid(diversity_json)),
    PRIMARY KEY (queue_snapshot_id, clip_proposal_id),
    UNIQUE (queue_snapshot_id, rank)
);

CREATE TABLE editorial_decisions (
    id TEXT PRIMARY KEY,
    clip_proposal_id TEXT NOT NULL REFERENCES clip_proposals(id),
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    decision TEXT NOT NULL CHECK (decision IN ('accept', 'maybe', 'reject', 'withdrawn')),
    rejection_reason TEXT,
    note TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL UNIQUE,
    expected_prior_revision INTEGER NOT NULL CHECK (expected_prior_revision >= 0),
    created_at TEXT NOT NULL,
    CHECK ((decision = 'reject' AND rejection_reason IS NOT NULL) OR (decision <> 'reject' AND rejection_reason IS NULL)),
    UNIQUE (clip_proposal_id, revision_number)
);

CREATE TABLE boundary_edits (
    id TEXT PRIMARY KEY,
    editorial_decision_id TEXT NOT NULL UNIQUE REFERENCES editorial_decisions(id),
    start_us INTEGER NOT NULL CHECK (start_us >= 0),
    end_us INTEGER NOT NULL CHECK (end_us > start_us),
    outside_evaluated_context INTEGER NOT NULL CHECK (outside_evaluated_context IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE exports (
    id TEXT PRIMARY KEY,
    source_recording_id TEXT NOT NULL REFERENCES source_recordings(id),
    clip_proposal_id TEXT NOT NULL REFERENCES clip_proposals(id),
    editorial_decision_id TEXT NOT NULL REFERENCES editorial_decisions(id),
    artifact_id TEXT NOT NULL UNIQUE REFERENCES artifacts(id),
    start_us INTEGER NOT NULL CHECK (start_us >= 0),
    end_us INTEGER NOT NULL CHECK (end_us > start_us),
    export_profile_json TEXT NOT NULL CHECK (json_valid(export_profile_json)),
    confirmation_json TEXT NOT NULL CHECK (json_valid(confirmation_json)),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE reference_moment_revisions (
    id TEXT PRIMARY KEY,
    source_recording_id TEXT NOT NULL REFERENCES source_recordings(id),
    annotation_set_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    certainty TEXT NOT NULL CHECK (certainty IN ('definite', 'possible')),
    category TEXT NOT NULL CHECK (category IN ('reaction', 'comedy', 'story', 'opinion', 'explanation')),
    start_us INTEGER NOT NULL CHECK (start_us >= 0),
    end_us INTEGER NOT NULL CHECK (end_us > start_us),
    event_us INTEGER NOT NULL,
    short_form_suitability INTEGER NOT NULL CHECK (short_form_suitability BETWEEN 0 AND 4),
    rationale TEXT NOT NULL,
    frozen INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0, 1)),
    created_at TEXT NOT NULL,
    CHECK (event_us >= start_us AND event_us < end_us),
    UNIQUE (annotation_set_id, revision_number)
);

CREATE TABLE active_operation_lease (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    operation_type TEXT NOT NULL CHECK (operation_type IN ('source_import', 'analysis')),
    operation_id TEXT NOT NULL,
    owner_instance TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE INDEX idx_stage_attempt_scope ON stage_attempts(scope_type, scope_id, stage_name);
CREATE INDEX idx_artifact_source_kind ON artifacts(source_recording_id, kind);
CREATE INDEX idx_transcript_run_time ON transcript_segments(analysis_run_id, start_us);
CREATE INDEX idx_evidence_run_time ON evidence_items(analysis_run_id, start_us);
CREATE INDEX idx_candidate_run_time ON candidate_moments(analysis_run_id, anchor_us);
CREATE INDEX idx_proposal_run_time ON clip_proposals(analysis_run_id, start_us);
CREATE INDEX idx_decision_proposal_revision ON editorial_decisions(clip_proposal_id, revision_number DESC);

CREATE TRIGGER stage_attempt_state_transition
BEFORE UPDATE OF state ON stage_attempts
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state = 'pending' AND NEW.state IN ('running', 'cancelled')) OR
    (OLD.state = 'running' AND NEW.state IN ('succeeded', 'failed', 'cancelled'))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid stage attempt state transition');
END;

CREATE TRIGGER import_attempt_state_transition
BEFORE UPDATE OF state ON source_import_attempts
WHEN NEW.state <> OLD.state AND NOT (
    (OLD.state = 'pending' AND NEW.state IN ('running', 'cancelled')) OR
    (OLD.state = 'running' AND NEW.state IN ('succeeded', 'failed', 'cancelled'))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid source import attempt state transition');
END;
