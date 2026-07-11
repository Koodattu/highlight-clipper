ALTER TABLE transcript_segments ADD COLUMN avg_log_probability REAL;
ALTER TABLE transcript_segments ADD COLUMN no_speech_probability REAL;

CREATE TABLE transcript_words (
    id TEXT PRIMARY KEY,
    transcript_segment_id TEXT NOT NULL REFERENCES transcript_segments(id),
    sequence_number INTEGER NOT NULL CHECK (sequence_number >= 0),
    start_us INTEGER NOT NULL CHECK (start_us >= 0),
    end_us INTEGER NOT NULL CHECK (end_us > start_us),
    word TEXT NOT NULL,
    probability REAL,
    UNIQUE (transcript_segment_id, sequence_number)
);

CREATE INDEX idx_transcript_word_time ON transcript_words(start_us, end_us);

