# Milestone 1: Local review queue

**Status:** Proposed pending shared-understanding confirmation

Milestone 1 is a local, single-creator application that turns one long local recording into a short queue of evidence-backed clip proposals. The creator reviews, trims, and labels those proposals, then exports accepted intervals in the source aspect ratio.

The milestone proves retrieval quality, boundary quality, and editorial usefulness. It also begins collecting the creator-owned decisions needed for later personalization.

## Success criteria

- One local video file can complete the workflow from ingestion through source-aspect clip export rendered from the original Source Recording.
- Interrupted analysis resumes from completed stages rather than restarting.
- The default automated test suite is deterministic, offline, and CPU-only.
- Across three manually annotated whole recordings, provisional leave-one-recording-out macro recall finds at least 80% of definite Reference Highlights within the first 30 Clip Proposals.
- For every deterministically matched Reference Highlight, the median absolute correction is no more than five seconds for both proposed starts and ends.
- Real-model, GPU, and long-media checks are opt-in and do not make ordinary tests depend on downloaded models.

## Product boundary

### Included

- One local user and one Creator Profile.
- Finnish and English, including code-switching within an utterance.
- Reaction, Comedy, Story, Opinion, and Explanation proposal categories.
- A local web Review Queue with playback, transcript and evidence, keyboard review actions, rejection reasons, boundary controls, and automatic progression.
- Accept, maybe, and reject Editorial Decisions.
- A required structured Rejection Reason for rejections, plus an optional note.
- Boundary Edits as first-class learning data.
- Explicit confirmation before exporting a risk-flagged proposal.
- Source-aspect rendering from the original recording.

### Deferred

- Twitch, YouTube, or other URL ingestion.
- Twitch chat, stream events, markers, retention data, and historical clip imports.
- Visual sampling, OCR, VLMs, face analysis, and game-specific adapters.
- Pitch analysis, openSMILE, laughter models, and general audio-event tagging.
- A learned Personalized Ranker.
- Vertical reframing, captions, titles, descriptions, and multiple rendered variants.
- Uploading, posting, or any other publication integration.
- Multiple creators, accounts, authentication, network access, and cloud inference.
- PostgreSQL, Parquet, DuckDB, distributed workers, and a separate frontend build system.

## Workflow

```text
local Source Recording
        |
        v
probe and source-time manifest
        |
        +--> browser-compatible review proxy
        +--> 16 kHz mono analysis audio
        |
        v
timestamped transcript + lightweight audio Observations
        |
        v
independent high-recall Candidate Moment generators
        |
        v
merge and deduplicate
        |
        v
two-to-five-minute Context Envelopes
        |
        v
structured local semantic evaluation and boundary selection
        |
        v
transparent baseline ordering + diversity selection
        |
        v
Review Queue (10-30 proposals by default)
        |
        +--> Editorial Decisions and Boundary Edits
        +--> source-aspect Accepted Clip export from original
```

Each stage is resumable and idempotent. Job completion and failures are persisted in SQLite. Processing uses an in-process worker; there is no distributed queue.

Resumability has explicit semantics:

- A source fingerprint detects a changed or replaced input file.
- Artifact writes are atomic; partial files never count as completed output.
- Re-running a completed stage does not create duplicate records or artifacts.
- Failed-stage cleanup removes only incomplete artifacts owned by that attempt.
- Changes to source fingerprints, model revisions, prompts, schemas, or relevant settings invalidate that stage and its downstream dependents.

## Time and media contracts

- The Source Recording is referenced as an immutable local file.
- Every internal editorial timestamp uses Source Time: decimal seconds from the playable beginning of the original recording.
- Container timestamps, sample indices, model-relative positions, and proxy offsets are converted at system boundaries.
- Preprocessing creates a lower-resolution H.264/AAC proxy for browser review and 16 kHz mono audio for analysis.
- Derivatives remain aligned to Source Time.
- Accepted Clips are rendered from the original recording, never from the review proxy.

See [ADR 0004](./adr/0004-use-source-relative-seconds.md) and [ADR 0005](./adr/0005-separate-source-from-analysis-media.md).

## Discovery

Transcript and audio discovery remain independent so conspicuous audio cannot bury quiet semantic moments. Each generator emits Candidate Moments with its own confidence and evidence; merging preserves that provenance.

### Transcript discovery

- Start with `faster-whisper` using Whisper Turbo through a replaceable adapter.
- Run a time-boxed bake-off against Whisper large-v3 and Parakeet v3 on representative Finnish, English, and code-switched material; promote one production ASR backend rather than carrying several permanent paths.
- Preserve word and segment timestamps in Source Time.
- Embed multi-scale transcript windows through a replaceable local adapter; its exact model and CPU/GPU placement remain to be selected.
- Generate semantic candidates from similarity to category descriptions and from topic novelty.

### Audio discovery

- Measure energy and peak changes.
- Use Silero speech activity, pauses, and transcript-derived speech rate.
- Normalize observations against rolling local median and median absolute deviation rather than fixed global thresholds.

See [ADR 0001](./adr/0001-generate-candidates-independently.md).

## Semantic evaluation

Candidate Moment retrieval and clip-boundary selection are separate stages. A Candidate Moment receives a roughly two-to-five-minute Context Envelope; the evaluator proposes variable boundaries rather than a fixed clip duration.

The local evaluator runs through a pinned llama.cpp Windows/CUDA Runtime Bundle. The initial bake-off covers Qwen3.6-35B-A3B, Qwen3.6-27B, Gemma 4 31B, and Gemma 4 26B-A4B at suitable four-bit GGUF quantizations. One direct `llama-server` child owns the GPU while a candidate batch is evaluated, then exits before another GPU stage starts. The four candidates are benchmark profiles, not four simultaneously loaded production models.

The evaluator uses a 32K context tier by default and relaunches at 64K, 128K, or 256K only when preflight token counting requires it. Input is never silently truncated. Reasoning, sampling, MTP, and output budgets are explicit Model Profile settings. Every request uses a Pydantic-derived JSON schema and cites timestamped evidence available in its input. Runtime build, repository revision, exact GGUF hashes, prompt/schema version, and effective parameters are persisted with analysis metadata.

Every retained Clip Proposal contains:

- Category and concise summary.
- Proposed start and end in Source Time.
- Setup, Hook, Event, Payoff, and Exit timestamps; markers may coincide.
- Separate judgments for Salience, Standalone Coherence, Hook, Payoff, Creator Fit, and Short-form Suitability.
- Risk flags and reasons against selection.
- Timestamped supporting evidence.

The evaluator does not inspect the complete recording, invent unavailable audio or visual evidence, or produce the final opaque ranking score.

Application-level validation supplements JSON Schema. It rejects inverted or out-of-bounds intervals, markers outside the proposal or Context Envelope, invalid marker ordering, non-finite judgments, and evidence references not present in the evaluator input.

See [ADR 0002](./adr/0002-separate-retrieval-from-boundary-selection.md), [ADR 0003](./adr/0003-use-a-bounded-semantic-evaluator.md), [ADR 0006](./adr/0006-serialize-gpu-models-in-worker-processes.md), and the [local runtime runbook](./local-runtime.md).

## Ordering and review budget

Milestone 1 uses a transparent fixed baseline over proposal judgments. Diversity selection suppresses temporal and semantic duplicates and preserves category and recording-section coverage.

The default Review Queue size is approximately three proposals per source hour, with a minimum of 10 and a cap of 30. The creator can request more proposals.

A Personalized Ranker is not trained until at least 200 Editorial Decisions exist across eight Source Recordings. It replaces the fixed baseline only after improving ranking on held-out whole recordings.

## Review and safety

The FastAPI application serves plain local HTML, CSS, and JavaScript. It binds to `127.0.0.1` only and has no accounts or authentication.

The review screen provides:

- Immediate playback of the proposed interval and nearby context.
- Transcript and timestamped evidence.
- Category, proposal structure, judgments, and Risk flags.
- Keyboard-driven accept, maybe, and reject actions.
- A structured Rejection Reason and optional note.
- Start and end controls that persist Boundary Edits.
- Automatic progression to the next proposal.
- Explicit confirmation before exporting a risk-flagged proposal.

Risk flags never silently suppress a proposal; they remain visible for human judgment.

## Persistence and artifacts

- SQLite is the source of truth for recordings, stage state, transcripts, observations, candidates, proposals, decisions, and exports.
- Every downloaded runtime/model, Source Recording, database, proxy, audio file, preview, rendered clip, model cache, log, and temporary artifact remains beneath the repository-local, Git-ignored `workdir/`.
- Model outputs are cached by input fingerprint, model identity, prompt/schema version, and relevant parameters.
- The single anchored `/workdir/` ignore rule is the Git boundary; broad extension ignores do not hide intentional tiny test fixtures elsewhere in the repository.

## Evaluation

The initial evaluation set contains three representative whole recordings:

1. Gameplay-heavy.
2. Speech-heavy.
3. Mixed content with Finnish/English code-switching.

Each recording must contain at least five definite Reference Highlights. Each Reference Highlight records definite or possible status, category, ideal start and end, and a short rationale.

The initial result is provisional because three recordings are too few to establish generalization. Evaluation uses three leave-one-recording-out folds: tune thresholds or prompts on two whole recordings and evaluate the third, then macro-average the per-recording metrics. Adjacent material from the same event never crosses a fold boundary.

A top-K Clip Proposal recalls a Reference Highlight when one of that proposal's underlying Candidate Moments falls within the reference's ideal interval. Matching is deterministic and one-to-one, preferring the higher-ranked unmatched proposal when several qualify. If a recording returns fewer than 30 proposals, recall uses all returned proposals. Boundary metrics use the same matched pairs even when their proposed boundaries are poor, so boundary failures cannot disappear from measurement.

Primary metric:

- Macro-averaged definite Reference Highlight recall within the first 30 proposals, target at least 80%.

Secondary metrics:

- Absolute start and end corrections, target median no more than five seconds per edge.
- Temporal intersection-over-union for every matched pair.
- Acceptance rate and accepted-without-trimming rate.
- Category recall and queue diversity.
- Review time per Accepted Clip.

## Implementation slices

0. Create the ignored Work Directory and pinned llama.cpp Runtime Bundle; smoke-test FFmpeg Source Time alignment and browser playback, ASR GPU startup/unload, and schema-constrained llama.cpp output with verified VRAM release.
1. Define domain types, Source Time invariants, SQLite persistence, stage orchestration, fake adapters, and deterministic fixtures.
2. Complete the end-to-end fake workflow, including the local review UI and deterministic export fixture.
3. Integrate ffprobe/FFmpeg probing, proxy/audio creation, playback, and source-aspect export from the original.
4. Integrate faster-whisper and lightweight audio observations.
5. Integrate transcript embeddings, independent candidate generation, merge, and deduplication.
6. Integrate managed llama.cpp semantic evaluation, adaptive context, MTP benchmarking, validated Clip Proposals, baseline ordering, and diversity selection.
7. Run opt-in real-model checks, annotate the three-recording evaluation set, and tune against the agreed quality gates.

## Known environment risks

- Installed FFmpeg/ffprobe is version 4.2.1. The implementation targets its available command set and requires a startup capability check; an upgrade is required only if that check fails.
- `python` is version 3.11.9, but the Windows `py` launcher resolves to Python 2.7. Project commands must use `uv run` or an explicit environment interpreter.
- faster-whisper GPU execution needs compatible CUDA 12 and cuDNN 9 libraries; the working combination has not yet been verified.
- llama.cpp is not installed in the Work Directory and none of the four evaluator profiles has been downloaded or benchmarked yet. The chosen build must include both Qwen3.6 and Gemma 4 MTP support.
- No representative Source Recordings or Reference Highlights exist in the repository, so retrieval-quality gates cannot be verified until they are provided and annotated.
