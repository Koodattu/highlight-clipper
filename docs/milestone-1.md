# Milestone 1: Trustworthy local review queue

**Status:** Proposed pending final shared-understanding confirmation

Milestone 1 is a local, single-creator application that turns one long local recording into a short queue of evidence-backed Clip Proposals. The creator reviews, corrects boundaries, and labels those proposals, then explicitly confirms source-aspect exports rendered from the original Source Recording.

The milestone establishes a complete text/audio-first editorial loop and the durable learning data needed for later personalization. It does not claim to be the finished multimodal system or a learned Personalized Ranker. Those remain the product direction described in the [roadmap](./roadmap.md), and the first release preserves their extension seams.

Milestone 1 measures retrieval quality, boundary quality, runtime feasibility, and editorial usefulness on a pilot corpus. It does not claim broad generalization from a few recordings.

## Success criteria

- One regular local video file completes the workflow from atomic import through human-confirmed source-aspect export.
- Interrupted or cancelled analysis resumes from valid completed work without duplicating artifacts or losing creator-owned history.
- Reanalysis creates new immutable revisions; it never overwrites reviewed proposals, Editorial Decisions, Boundary Edits, or exports.
- The default automated test suite is deterministic, offline, and CPU-only.
- Across three manually annotated development recordings, leave-one-recording-out macro recall finds at least 80% of definite Reference Moments within the first 30 Clip Proposals, with no recording below 60%.
- Every language/category slice with at least five definite Reference Moments has recall@30 of at least 60%; thinner slices are reported as not yet validated rather than hidden in the aggregate.
- Across at least twelve matched development references, median absolute start and end correction is no more than five seconds and nearest-rank p90 correction is no more than fifteen seconds per edge.
- After configuration is frozen, a fourth sealed whole recording with at least ten definite Reference Moments must independently pass the 80% recall@30 and boundary gates. A failure remains a reported holdout failure; tuning on it converts it to development data and requires a new sealed recording.
- On a fixed set of at least 100 positive, routine-negative, rejection, and insufficient-context envelopes, evaluator dispositions are schema-valid after at most one repair at least 99% of the time; unknown Evidence Item/Boundary Anchor IDs and out-of-bounds proposals are never accepted.
- Excluding one-time model downloads, end-to-end analysis completes within one wall-clock hour per source hour on the target machine.
- Review takes no more than ten minutes per source hour and a median of no more than ten minutes per Accepted Clip, and every pilot recording containing definite Reference Moments produces at least one Accepted Clip.
- Real-model, GPU, and long-media checks are opt-in and do not make ordinary tests depend on downloaded models.
- Recall at 10, 20, and 30 proposals, recall after 10, 20, and 30 minutes of cumulative review, acceptance/maybe rates, category coverage, and accepted-without-boundary-edit rate are recorded.

The four-recording result remains a pilot. Claims of broader creator-level generalization require at least ten whole recordings and fifty definite Reference Moments evaluated with a frozen configuration.

The initial end-to-end performance timer starts when Source Import begins and stops when the Analysis Run commits its Queue Snapshot. It includes source copy, timeline scan, proxy/audio creation, ASR, indexing, cold model loads/unloads, allowed retries, and cleanup; it excludes one-time setup/model downloads, human review, and Export rendering. Stage timings and reanalysis-with-reused-artifacts timing are reported separately with the complete target-machine runtime manifest.

## Product boundary

### Included

- One local user and one versioned Creator Profile.
- A simple local Creator Profile editor for languages, category priorities, desired/avoided content, and preferred duration ranges; saving creates a new revision.
- Finnish and English, including code-switching within an utterance.
- Reaction, Comedy, Story, Opinion, and Explanation proposal categories.
- Immutable source import into the repository-local Work Directory.
- A local web Review Queue with playback, surrounding context, transcript and evidence, keyboard review actions, rejection reasons, boundary controls, and automatic progression.
- Long-running analysis status, progress, cancellation, and explicit retry controls.
- A CLI for setup, large-file import, serving the web app, and verified backup; the browser never uploads a multi-hour source file.
- A Reference Moment annotation mode that hides system output until an evaluation annotation revision is frozen.
- Immutable Queue Snapshots and append-only accept, maybe, reject, and undo/withdrawn Editorial Decision revisions.
- A required structured Rejection Reason for rejections, plus an optional note.
- Boundary Edits as first-class learning data.
- Explicit confirmation before every export and an additional warning for risk-flagged proposals.
- Source-aspect rendering from the original recording.
- Consistent database backup and portable creator-label export.

### Deferred

- Twitch, YouTube, or other URL ingestion.
- Twitch chat, stream events, markers, retention data, and historical clip imports.
- Visual sampling, OCR, VLMs, face analysis, and game-specific adapters.
- Pitch analysis, openSMILE, laughter models, and general audio-event tagging.
- A learned Personalized Ranker.
- Vertical reframing, captions, titles, descriptions, and multiple rendered variants.
- Uploading, posting, or any other publication integration.
- Multiple creators, accounts, remote access, cloud inference, and distributed workers.
- PostgreSQL, Parquet, DuckDB, a vector database, and a separate frontend build system.

Deferral means “add through the documented extension seam after measured improvement,” not “the architecture does not support this.” See the [roadmap](./roadmap.md).

## Workflow

~~~text
runtime/media/database preflight
        |
        v
atomic local Source Import + SHA-256 + immutable Media Timeline
        |
        +--> browser-compatible review proxy
        +--> 16 kHz mono analysis audio
        |
        v
chunked timestamped transcript + lightweight audio Observations
        |
        v
multi-scale transcript index + independent high-recall generators
        |
        v
budgeted merge and deduplication with complete provenance
        |
        v
two-to-five-minute Context Envelopes
        |
        v
structured local semantic evaluation and boundary selection
        |
        v
versioned baseline ordering + diversity selection
        |
        v
immutable Review Queue snapshot (10-30 proposals by default)
        |
        v
Analysis Run finishes
        |
        +--> append-only review commands: Editorial Decisions and Boundary Edits
        +--> independent human-confirmed export commands from original
        +--> portable creator-label dataset for evaluation and later learning
~~~

The detailed identity, state, recovery, security, and artifact semantics are normative in [Pipeline contracts](./pipeline-contracts.md).

## Orchestration and lineage

Milestone 1 has at most one active Source Import or Analysis Run stage at a time. Source Import selects streams, commits the authoritative Media Timeline, proxy, and analysis audio. An Analysis Run begins with that complete Source Recording and finishes when it creates one Queue Snapshot; review may remain open indefinitely, and export is an independent command. The orchestrator and queue are in-process, while ASR, GPU embeddings when selected, and semantic evaluation run in owned disposable child process trees.

Stages are resumable and idempotent through immutable generations and versioned input/configuration fingerprints. Invalidation follows the stage graph: a changed ASR configuration does not recreate media; a ranking change creates a new Queue Snapshot; an export-profile change creates only a new Export. A new Analysis Run may reuse compatible generations and never mutates a Queue Snapshot already under review.

SQLite is the only metadata writer. Workers report structured progress and results; they do not write application tables directly. Startup reconciles interrupted attempts, owned process trees, the GPU lease, partial files, and filesystem/database disagreement before accepting new work.

See [ADR 0007](./adr/0007-preserve-editorial-history-across-reanalysis.md) and [ADR 0009](./adr/0009-end-analysis-at-the-queue-snapshot.md).

## Time and media contracts

- Source Import probes first; ambiguous defaults require explicit video/audio stream IDs before copying.
- Import copies a regular local media file into `workdir/sources/<source-id>/` using a partial file, SHA-256 validation, authoritative first/last-frame scan, aligned proxy/audio validation, and atomic artifact registration. It never moves or modifies the caller's file.
- The imported Source Recording is immutable and canonical.
- The first decodable displayed frame of the selected video stream establishes Source Time 0.0; the exclusive source end is the end of its last displayed frame.
- SQLite persists non-negative Source Time as integer microseconds and every editorial interval is half-open within [0, source end).
- Selected stream identities, original time bases, container and stream starts, audio/video offset, duration disagreement, rotation, and variable-frame-rate indication are persisted.
- Audio before video zero or after source end is excluded; later audio starts and timestamp gaps become aligned silence.
- Container timestamps, sample indices, model-relative positions, proxy offsets, and export seeks are converted at system boundaries.
- Preprocessing creates a lower-resolution H.264/AAC proxy for browser review and 16 kHz mono audio for analysis.
- Derivatives are validated for expected streams, playable duration, seekability, and Source Time alignment before becoming usable artifacts.
- Exports are re-encoded from the original recording for frame-accurate boundaries, never from the review proxy.

See [ADR 0004](./adr/0004-use-source-relative-seconds.md), [ADR 0005](./adr/0005-separate-source-from-analysis-media.md), [ADR 0008](./adr/0008-import-source-recordings-by-copy.md), and the [pipeline contracts](./pipeline-contracts.md).

## Discovery

Transcript and audio discovery remain independent so conspicuous audio cannot bury quiet semantic moments. Each generator emits Candidate Moments with generator-local confidence and immutable evidence; merging retains every provenance link.

### Transcript and ASR

- Start the vertical slice with `faster-whisper` Whisper Turbo through a replaceable adapter because it has the lowest Windows integration risk.
- Run a fixed-corpus bake-off against Whisper large-v3. Parakeet v3 joins only after a native-Windows/RTX 4090 smoke test passes; its Linux-preferred NeMo path does not block Milestone 1.
- Promote one production ASR backend rather than carrying several permanent paths.
- Compare version-normalized WER and CER, word-timestamp error on a manually aligned subset, silence/music hallucination, full-hour throughput, peak RAM/VRAM, recovery, and downstream retrieval/boundary quality, with minimum Finnish/English/code-switch floors frozen before promotion.
- Preserve raw output plus a versioned canonical transcript with stable word/segment identities and half-open Source Time intervals.
- Process long audio in deterministic overlapping chunks with resumable checkpoints and duplicate-free stitching.

### Transcript retrieval

- Build deterministic sentence-aligned windows targeting approximately 20, 45, 90, and 180 seconds with half-window strides.
- Include a cheap lexical baseline and one replaceable multilingual embedding query family.
- Compare embedding candidates on downstream recall, throughput, instruction sensitivity, memory, and index size rather than leaderboard rank alone.
- Start embeddings on CPU unless measurement shows that indexing time harms the product budget; a GPU-backed adapter uses the same disposable-worker and GPU-lease contract.
- Begin with category-description lexical/embedding retrieval, topic novelty, and cheap audio peaks. Add quotable-assertion, question/answer, disagreement, prediction/outcome, story-progression, or accepted-example generators only through recorded ablation.
- Similarity to accepted examples uses only decisions that predate and belong outside the held-out Source Recording.

### Audio discovery

- Measure energy and peak changes.
- Use Silero speech activity, pauses, and transcript-derived speech rate.
- Normalize observations against rolling local median and median absolute deviation rather than fixed global thresholds.
- Preserve raw measurements as Observations rather than claiming to infer emotion.

### Candidate workload

Initial versioned guards target 15-30 raw Candidate Moments per source hour, hard-cap the combined raw set at 50 per hour, retain category/generator/recording-section coverage, and target 4-10 merged Context Envelopes per hour with a hard cap of 100 per recording.

Unique evaluator coverage uses a soft ceiling at the greater of 15 minutes or 10% of source duration and a hard cap at the greater of 30 minutes or 20%, never exceeding the Source Recording. Rendered evaluator input is additionally hard-capped at 100,000 prompt tokens per source hour and 1,000,000 per Analysis Run. Actual tokens and saturation are persisted so envelope count or overlap cannot hide an impractically large workload.

These bounds protect runtime without making confidence values globally comparable. A valid empty or low-candidate result remains a successful analysis outcome.

See [ADR 0001](./adr/0001-generate-candidates-independently.md) and [Pipeline contracts](./pipeline-contracts.md).

## Semantic evaluation

Candidate Moment retrieval and clip-boundary selection are separate stages. Each Candidate Cluster receives a roughly two-to-five-minute Context Envelope. A successful evaluation returns either an explicit no-proposal disposition or one to a configured maximum of three distinct Clip Proposals. Every contributing Candidate Moment is linked to a proposal or receives an inspectable omission reason, so two nearby real moments do not suppress or silently erase one another.

The local evaluator runs through a pinned llama.cpp Windows/CUDA Runtime Bundle. The first useful real pipeline uses one baseline profile with MTP disabled. Only after a fixed annotated set exists are Qwen3.6-35B-A3B, Qwen3.6-27B, Gemma 4 31B, and Gemma 4 26B-A4B screened under a common profile; reasoning, context, quantization, and MTP tuning is limited to the best one or two deployable profiles.

The integration profile starts with a 32K context cap and records the actual prompt-size distribution; a smaller cap is promoted when it fits every representative envelope and output reserve. Larger tiers are diagnostic until they separately meet quality, latency, and VRAM gates. Input is never silently truncated: oversized input is re-enveloped or recorded as input-too-large.

Every request uses a Pydantic-derived JSON schema, stable Evidence Item and Boundary Anchor IDs, and structurally delimited untrusted transcript/profile content. Runtime build, model revision and hashes, tokenizer/template, prompt/schema version, rendered input, sampling, reasoning/output budgets, context use, and effective offload are persisted.

Every retained Clip Proposal contains:

- category and concise summary;
- proposed start/end resolved from Boundary Anchors;
- a required Event point plus optional Setup-start, Hook, Payoff, and Exit points with partial ordering;
- separate judgments for Salience, Standalone Coherence, Hook Strength, Payoff Strength, Creator Fit, Short-form Suitability, and Context Sufficiency;
- Risk flags and reasons against selection;
- stable timestamped evidence references.

Each successful Evaluation Attempt persists proposals, semantic rejection, insufficient context, input-too-large, or invalid-for-profile after one repair. Cancellation, timeout, and runtime failure belong to the attempt and leave the Context Envelope pending for a later attempt; failures never silently reduce the Review Queue.

Judgments use anchored integer levels from 0 through 4 rather than false-precision decimal probabilities. The prompt and schema define each level per judgment. Raw evaluator responses are retained as private attempt artifacts for audit, while only application-validated fields can become a Clip Proposal.

Machine Risk flags alone cannot reject or hide a proposal. The evaluator does not inspect the complete recording, invent unavailable audio or visual evidence, or produce the final opaque ranking score.

Preferred durations are Reaction 15-60 seconds, Comedy 20-90, Story 45-180, Opinion 30-180, and Explanation 60-240. The hard machine-proposal maximum is 240 seconds; an out-of-range proposal requires a structured duration-exception reason.

See [ADR 0002](./adr/0002-separate-retrieval-from-boundary-selection.md), [ADR 0003](./adr/0003-use-a-bounded-semantic-evaluator.md), [ADR 0006](./adr/0006-serialize-gpu-models-in-worker-processes.md), and the [local runtime runbook](./local-runtime.md).

## Ordering and Review Queue

Milestone 1 uses a transparent, versioned fixed baseline over separately inspectable proposal judgments. The complete formula, eligibility rules, normalization, category/section coverage, semantic diversity, temporal suppression, and deterministic tie-breakers are persisted with each Queue Snapshot.

The baseline does not silently mix generator-local confidences into a global score and does not treat Risk as low quality. Diversity selection suppresses temporal and semantic duplicates while preserving category and recording-section coverage.

The default Review Queue size is approximately three proposals per source hour, targeting at least 10 when enough valid proposals exist and capped at 30. Requesting more starts a new Analysis Run with expanded versioned budgets and creates a new Queue Snapshot while preserving earlier ranks and decisions.

A Personalized Ranker is not trained until at least 200 Editorial Decisions exist across eight Source Recordings. It replaces the fixed baseline only after improving a frozen held-out whole-recording evaluation without unacceptable category collapse or instability.

## Review, safety, and local web boundary

The FastAPI application serves plain local HTML, CSS, and JavaScript. It binds to `127.0.0.1`, disables CORS, validates Host and Origin, and uses a random per-server-session token for mutating requests. Media is served only by opaque database identity with containment checks.

The review screen provides:

- immediate playback of the proposed interval and nearby context;
- a Source Time-aligned waveform with the proposal and editable boundaries;
- transcript and stable timestamped evidence;
- category, proposal structure, judgments, and Risk flags;
- keyboard-driven accept, maybe, reject, and undo actions;
- a structured Rejection Reason and optional note;
- boundary controls that preserve frame-level Source Time even outside the evaluated Context Envelope;
- a stale-evidence warning and optional new Analysis Run that reuses artifacts and creates a successor Proposal/Queue Snapshot linked by supersedes;
- automatic progression to the next proposal.

Decisions are append-only and idempotent. A Boundary Edit may accompany accept, maybe, or reject; unusable proposed boundaries remain a valid Rejection Reason when the editor does not want to repair them. Every export requires human confirmation; a Risk flag or interval outside evaluated context adds a stronger warning. Unflagged never means safety-scanned.

A maybe decision is unresolved: it remains available in a separate review filter, is not exportable, and is not silently treated as either a positive or negative training label. Undo appends withdrawn. A new Export is allowed only when the latest overall decision revision itself is accept; historical Exports remain immutable after later changes.

All transcript, profile, evidence, and evaluator content is rendered as untrusted text. Logs are bounded and omit complete transcripts by default.

Filesystem-sized operations use the small documented CLI: setup, import by absolute local path, serve, and verified backup. The web application edits the Creator Profile, starts and monitors analysis, annotates Reference Moments, reviews proposals, and confirms exports; it never browser-uploads a multi-hour recording.

## Persistence, artifacts, and backup

- SQLite is the source of truth for identity, lineage, stage state, metadata, decisions, and artifact registrations.
- SQLite enables foreign-key enforcement, WAL mode, a bounded busy timeout, transactional schema migrations, startup integrity checks, and backup before any migration that can discard information.
- Large embeddings are versioned checked artifact arrays referenced by SQLite; a vector database is unnecessary for one recording.
- Every runtime/model, Source Recording, database, proxy, audio file, transcript, observation artifact, preview, export, cache, log, and partial file remains beneath the repository-local Git-ignored `workdir/`.
- Model outputs are cached by exact evidence/input fingerprint, Model Profile, prompt/schema version, and relevant parameters.
- The single anchored `/workdir/` ignore rule is the Git boundary; broad extension ignores do not hide intentional test fixtures elsewhere.
- A backup command uses SQLite's backup API and exports a versioned portable label package with source/time manifest, Creator Profile and Reference Moment revisions, Analysis Run/model identity, proposals/evidence, Queue Snapshot ranks, Editorial Decisions, Boundary Edits, and artifact hashes.
- Backups default to a consistent local snapshot under the Work Directory and may be copied and verified to a user-selected external destination.
- App-specific cleanup never uses Git, distinguishes valuable creator data from disposable cache/incomplete attempts, and may garbage-collect unreferenced regenerable bytes while preserving lineage and editorial history.

## Evaluation

### Corpus and annotation

The development pilot contains three representative whole recordings:

1. gameplay-heavy;
2. speech-heavy;
3. mixed content with Finnish/English code-switching.

Each has at least five definite Reference Moments and may have possible ones. Across the development set, Finnish, English, code-switching, and all five proposal categories are deliberately sampled; a category/language claim is withheld until its slice has at least five definite references. A Reference Moment is annotated before viewing system output and records a revision, certainty, category, ideal half-open interval, Event anchor, short-form suitability, and rationale. The complete recording is reviewed so unlabeled output can be interpreted honestly.

A fourth representative recording with at least ten definite Reference Moments, including Finnish, English, and code-switched material, is kept sealed while thresholds, prompts, Model Profiles, runtime budgets, and product gates are selected. Its references are revealed and scored once after configuration freeze. If it fails, the failure remains the holdout result; using it for diagnosis or tuning turns it into development data and requires a new sealed recording. Evaluation-set revisions and every tuning experiment are recorded.

No Source Recording contributes adjacent windows, Reference Moments, Editorial Decisions, or accepted-example features to a fold in which that recording is held out. The evaluator screen also includes routine negative envelopes and insufficient-context cases rather than only Reference Moments.

### Matching

Discovery recall and Review Queue recall are separate:

- A Candidate Moment discovers a Reference Moment when its anchor lies inside the reference interval.
- A Clip Proposal covers a Reference Moment when its required Event point lies inside the reference interval.

Matching is deterministic and one-to-one through maximum-cardinality bipartite matching. Among equally large matchings, it minimizes total proposal rank, then maximizes total temporal overlap, then uses reference start and stable IDs as tie-breakers. The matching-policy version is persisted.

Boundary quality remains separate from Event retrieval. Boundary metrics use the same matched pairs even when proposed boundaries are poor, so concise valid clips are not penalized as retrieval failures and bad boundaries cannot disappear from measurement.

When a system returns fewer than K valid proposals, recall@K uses all returned proposals and reports the shortfall rather than padding or hiding it.

With ten sealed references and the 80% recall gate, as few as eight pairs may be matched; nearest-rank p90 then equals the worst matched edge error. That conservative behavior is intentional and is reported with the matched count.

### Metrics and ablations

Primary metrics:

- macro definite Reference Moment recall at 10, 20, and 30 proposals;
- per-recording definite recall and macro recall;
- start and end absolute correction reported separately, with median and nearest-rank p90 across matched references;
- temporal intersection-over-union for every matched pair.

Product metrics:

- acceptance and maybe rates;
- accepted-without-boundary-edit rate;
- category recall and queue diversity;
- review minutes per source hour and per Accepted Clip;
- definite Reference Moment recall after 10, 20, and 30 minutes of cumulative review;
- useful clips per Source Recording.

Active review time runs while proposal/context media is playing or the creator interacts with review controls and excludes idle gaps longer than 60 seconds. The timer-policy version is persisted with review-budget metrics.

Required ablations:

1. deterministic random timestamps;
2. loudness/VAD only;
3. lexical transcript retrieval;
4. transcript embeddings without semantic evaluation;
5. transcript plus semantic evaluation;
6. audio plus transcript;
7. each later chat, richer-audio, visual, or personalized addition.

A more expensive component is promoted only when held-out end-to-end benefit justifies its latency, storage, and maintenance cost.

## Implementation slices

0. Define the minimal domain types, integer Source Time conversion, SQLite migrations/lineage, adapter seams, and deterministic media/model fixtures.
1. Complete a thin fake happy-path workflow plus cancellation and checkpoint resume: Creator Profile, Queue Snapshot, review decisions, Boundary Edits, and deterministic export.
2. Integrate atomic Source Import, explicit stream selection, FFprobe/FFmpeg timeline scan, proxy/audio creation, playback, Source Time alignment fixtures, and source-aspect export.
3. Integrate chunked faster-whisper Turbo, lightweight audio Observations, lexical retrieval, one embedding path, candidate budgets, merge, and Context Envelopes.
4. Integrate the managed no-MTP Qwen baseline, strict evidence/anchor/schema validation, ordering/diversity, and run the first useful real recording through review and export.
5. Harden the proven path with malformed-output, crash/hang, disk-full, orphan reconciliation, GPU-owner failure, backup/restore, and local-web security checks.
6. Annotate the pilot corpus, freeze runtime/product budgets, run ASR and evaluator screening, promote one configuration, and evaluate the sealed recording.
7. Tune bounded reasoning, larger context only if measured prompts require it, and MTP only for the best one or two profiles; retain only measured improvements.

## Decisions intentionally left empirical

- Winning ASR backend and decoding/chunk settings.
- Winning embedding model, dimension, instruction, and CPU/GPU placement.
- Winning evaluator architecture, quantization, prompt, reasoning mode, sampling, and output budget.
- Whether any context beyond 32K is useful and deployable on the 24 GB GPU.
- Whether MTP improves complete candidate-batch latency enough to retain.
- Exact generator thresholds, merge policy, ordering weights, and diversity parameters.
- Stage-level latency, model load/unload, RAM/pagefile, disk amplification, and evaluator-throughput thresholds; freeze them after the first representative baseline and before finalist promotion.

The architecture defines how these decisions are compared and promoted; it does not pretend they are facts before measurement.

## Known environment risks

- Installed FFmpeg/FFprobe is version 4.2.1. Startup must verify the required decoders, encoders, filters, timestamp behavior, and browser-compatible output; upgrade only if the capability check fails.
- `python` is version 3.11.9, but the Windows `py` launcher resolves to Python 2.7. Project commands use `uv run` or the explicit environment interpreter.
- faster-whisper GPU execution needs a verified CUDA 12 and cuDNN 9 combination.
- llama.cpp is not installed in the Work Directory and none of the evaluator profiles has been downloaded or benchmarked. The baseline Runtime Bundle needs only the no-MTP Qwen path; a later experiment bundle must add and smoke-test both Qwen3.6 and Gemma 4 MTP support.
- No representative Source Recordings or Reference Moments exist in the repository, so retrieval, boundary, latency, and editorial-productivity gates cannot yet be measured.
- Valuable ignored data under the Work Directory is outside Git protection and requires the documented backup and app-specific cleanup path.
