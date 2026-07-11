ALTER TABLE reference_moment_revisions ADD COLUMN language_slice TEXT NOT NULL DEFAULT 'unknown' CHECK (
    language_slice IN ('fi', 'en', 'code_switched', 'language_neutral', 'unknown')
);

CREATE INDEX idx_reference_source_frozen ON reference_moment_revisions(
    source_recording_id,
    frozen,
    annotation_set_id,
    revision_number
);
