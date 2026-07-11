# Pipeline contracts

**Status:** Normative contract. Core invariants are implemented; remaining hardening is tracked separately.

See [Implementation status](./implementation-status.md) for the evidence-backed implementation matrix. Text below describes the intended contract unless an implementation note narrows current behavior.

These contracts make the Milestone 1 workflow reproducible, recoverable, and safe for creator-owned recordings and editorial decisions. They define behavior that every real or fake adapter must preserve; they do not require a distributed system or a general workflow engine.

## Invariants

- A Source Recording is immutable after import.
- Source Time is the only editorial coordinate.
- Reanalysis creates new revisions; it never rewrites reviewed proposals, decisions, or exports.
- SQLite is the single metadata writer and source of truth for lineage and stage state.
- Files become usable artifacts only after validation, required integrity checks, atomic placement, and database registration.
- A failed, cancelled, or interrupted attempt never replaces the last valid artifact generation.
- Every expensive model is replaceable behind a versioned adapter and owns the GPU only in a disposable child process.
- Human review is required before every export. Machine Risk flags add warnings; their absence is not a safety guarantee.

## Identity and lineage

The persisted model distinguishes these identities:

| Identity | Contract |
|---|---|
| Source Import attempt | One copy-and-probe attempt that may create a Source Recording |
| Source Recording | Immutable imported media plus selected streams, identified by a generated ID and SHA-256 |
| Media Timeline | Immutable selected-stream transform, Source Time origin/end, and alignment metadata committed with a Source Recording |
| Creator Profile revision | Immutable snapshot of the preferences used by an Analysis Run |
| Analysis Run | Immutable derivation from one Source Recording and Creator Profile revision through one Queue Snapshot |
| Stage Attempt | One execution attempt for one stage and input fingerprint |
| Artifact | Validated file output with owner attempt, kind, path, size, integrity metadata, and configuration fingerprint |
| Evidence Item | Immutable source-linked transcript or Observation item with producer generation, Source Time span, type, value/content hash, and locator |
| Candidate Moment | Immutable generator output with Source Time anchor or span, evidence IDs, generator version, and idempotency key |
| Candidate Cluster | A deduplicated group retaining links to every contributing Candidate Moment |
| Context Envelope | Immutable interval and exact evidence package supplied to evaluation |
| Evaluation Attempt | One evaluator/profile execution against one Context Envelope |
| Clip Proposal | Immutable evaluator result linked to its envelope, evidence, and evaluator attempt |
| Queue Snapshot | Immutable ranked proposal list and ordering configuration presented for review |
| Reference Moment revision | Immutable evaluation annotation with source identity, certainty, category/language slice, interval, Event anchor, suitability, and rationale |
| Editorial Decision revision | Append-only accept, maybe, reject, or withdrawn judgment for a proposal |
| Boundary Edit | Optional editor-corrected interval associated with a specific decision revision |
| Export | Validated rendered artifact linked to the exact decision revision and export configuration |

Friendly names are presentation metadata, never stable identifiers. A proposal has a many-to-many link to the Candidate Moments and Evidence Items it covers so deduplication and shared envelopes never erase provenance.

## Stage graph and lifecycle

Import, analysis, review, and export have separate lifecycles:

~~~text
Source Import attempt:
  probe/select -> copy -> timeline + proxy/audio validation
               -> Source Recording + Media Timeline + aligned derivatives

Analysis Run:
  ASR -> observations/index -> candidates -> clusters/envelopes
      -> evaluation -> ranking/queue -> terminal Queue Snapshot

Review commands:
  Queue Snapshot -> Editorial Decision revisions + Boundary Edits

Export command:
  current Editorial Decision revision == accept -> Export generation
~~~

This boundary is recorded in [ADR 0009](./adr/0009-end-analysis-at-the-queue-snapshot.md).

Milestone 1 runs at most one import or Analysis Run stage at a time. Acquisition is an atomic SQLite transaction with owner/heartbeat identity, so a second app instance cannot start conflicting work. Review can remain open indefinitely without keeping an Analysis Run active. An export is a separate bounded command and changing export settings creates a new Export generation, not a new Analysis Run.

A Stage Attempt has five persisted states:

- pending
- running
- succeeded
- failed
- cancelled

Allowed transitions are pending to running or cancelled, and running to succeeded, failed, or cancelled. A terminal attempt never changes state. Retry or resume creates a new attempt linked to the prior attempt and its valid checkpoint. Only a running attempt may own a worker process or GPU Lease. A stage may be shown as blocked when a dependency or non-retryable failure prevents a new attempt; blocked is derived stage status, not another attempt state.

Cancellation is a control request recorded separately from attempt state. A running attempt left behind by controller termination is reconciled to failed with an interruption error code; its work can then resume through a new attempt.

Every attempt records its input fingerprint, configuration fingerprint, attempt number, prior attempt/checkpoint when applicable, owner instance, worker process identity, start and end times, progress, committed outputs, retryability, and a bounded actionable error summary.

The orchestrator is in-process and is the only SQLite writer. CUDA and other failure-prone model runtimes execute in owned child process trees. The implemented worker protocol uses versioned request/result JSON files plus exit status, bounded controller polling, ASR checkpoints, stdout/stderr diagnostics, and process-tree termination. Streaming ready/progress/drained events remain a future refinement rather than a current claim.

At startup, the current orchestrator reconciles stale import/analysis leases, running Stage/Evaluation Attempts, interrupted exports, registered missing valuable artifacts, persisted unregistered source-tree recovery items, and the recovery quarantine. The fuller contract also calls for:

- attempts left in running;
- process identities, using creation identity as well as PID to avoid PID reuse;
- partial and attempt-owned files;
- registered artifacts whose files are missing or fail validation;
- unregistered completed files;
- a persisted GPU lease whose owner no longer exists.

The GPU mutex is currently OS-owned rather than persisted, and recovery does not yet discover every possible unregistered embedding/evaluator/export partial. Those remain explicit hardening work.

The first fake vertical slice exercises success, cancellation, and checkpoint resume. Before Milestone 1 completion, deterministic fault fixtures also cover malformed output, worker crash or hang, disk-full behavior, and an owned GPU process that fails to exit. This hardening follows the first real end-to-end recording rather than blocking it.

Retries are explicit and bounded by error code. Invalid input, missing or corrupt pinned assets, unsupported capabilities, validation failure, and unresolved process ownership are non-retryable until state changes. A transient worker or file-sharing failure may retry from its last valid checkpoint. No retry silently changes a model, quantization, context, threshold, or quality policy.

## Artifact commit and invalidation

An artifact attempt writes only to an attempt-owned partial path beside its final destination. It then:

1. flushes and closes the file;
2. validates format, duration or schema as appropriate;
3. records the required integrity metadata;
4. atomically renames it to an immutable generation path;
5. registers that generation and marks the attempt succeeded in one SQLite transaction.

There is no atomic transaction spanning NTFS and SQLite. Startup reconciliation is therefore part of the commit protocol, not optional cleanup. It may remove only files owned by incomplete attempts and unreferenced generations; it never removes a previous valid generation or user-owned source, decision, or export.

SHA-256 is mandatory for imported sources, installed asset files, analysis audio, embedding artifacts, evaluator raw output, exports, backups, and portable label packages. llama.cpp archives have catalog-supplied expected hashes. Hugging Face assets use exact committed revisions and post-download per-file hashes in a local manifest, which is reverified before use; this is not an upstream expected-hash claim.

SQLite runs with foreign-key enforcement, WAL mode, a bounded busy timeout, transactional numbered migrations, and startup integrity checks. A migration that can discard information requires a verified backup first.

Configuration fingerprints include the relevant source hash, selected streams and time transform, tool/runtime versions, model and tokenizer revisions, prompt and schema versions, Creator Profile revision, windowing, thresholds, merge policy, ranking policy, and export arguments. Invalidation follows the stage graph rather than recreating unrelated work: changing selected streams or the authoritative Media Timeline requires a new Source Import/Source Recording identity; a proxy encoding change may create new aligned derivatives only after timeline parity validation; ASR settings invalidate transcript consumers; retrieval/profile changes invalidate candidates onward; evaluator changes invalidate proposals onward; ranking changes create a new Queue Snapshot; export changes create only a new Export generation.

A new Analysis Run may reuse compatible immutable generations from an earlier run. It never mutates the Queue Snapshot currently being reviewed or any later Editorial Decision or Export.

## Source import and media validation

Milestone 1 first probes a regular local media path without copying it. If video/audio defaults are ambiguous, import fails before the copy with the available stream IDs and requires `--video-stream <id>` and `--audio-stream <id>`. The five-state attempt model therefore needs no indefinite awaiting-input state.

After stream selection, Source Import copies the media into:

~~~text
workdir/sources/<source-id>/original.<extension>
~~~

The copy uses a partial file and calculates SHA-256 while copying. Import then performs the authoritative first/last displayed-frame scan, creates and validates the aligned review proxy and analysis audio, and commits the Source Recording, immutable Media Timeline, and derivative generations together through the artifact recovery protocol. It never mutates an imported manifest later.

The application never moves, hard-links, edits, or deletes the caller's original file. A file already inside the source directory can be registered only after the same hash, stream-selection, timeline, and derivative-validation path.

Import rejects URLs, playlists, network protocols, directories, and paths that cannot be canonicalized. Subprocesses receive argument arrays, run without an interactive shell, and may write only beneath the Work Directory.

Implemented setup verifies database/media prerequisites and installed asset hashes, and Source Import enforces a conservative free-disk estimate for the source, proxy, analysis audio, waveform, temporary output, and reserve. The runtime manifest records RAM/pagefile/GPU/storage diagnostics. A unified policy that rejects every long stage based on RAM/pagefile headroom, future export volume, all selected models, and aggregate cache growth remains a fuller contract rather than current enforcement.

Probe persists:

- container duration and start timestamps;
- selected video and audio stream identifiers;
- each selected stream's time base, start, duration, codec, rotation and sample/display properties;
- audio/video start offset and duration disagreement;
- variable-frame-rate indication;
- the exact FFprobe build and arguments.

Milestone 1 requires one playable selected video stream and one selected audio stream. An unambiguous default stream is selected automatically; multiple plausible streams require an explicit choice. Missing, corrupt, truncated, encrypted, or unsupported media fails before analysis with an actionable reason.

The proxy, analysis audio, and exports are validated for expected streams, codecs, duration tolerance, first playable timestamp, seekability, and Source Time alignment. Golden media fixtures include ordinary MP4, nonzero or negative container timestamps, differing audio/video starts, rotation, and variable frame rate.

## Time and interval representation

The selected video stream establishes Source Time. Its first decodable displayed frame is exactly 0.0. A video or audio presentation timestamp is converted through its stream time base to the common container timeline, then the selected video's first-frame time is subtracted and the result is rounded once to integer microseconds.

The exclusive `source_end_us` is the end of the last decodable displayed video frame, finalized and validated during Source Import while producing the proxy. Source Time values are non-negative integer microseconds within [0, source_end_us]. Editorial intervals are half-open [start, end), with 0 <= start < end <= source_end_us.

Audio before the first video frame is excluded. If selected audio starts later or contains timestamp gaps, aligned analysis/proxy audio represents the gap as silence; audio after `source_end_us` is excluded. The signed stream offsets and original timestamps remain in the source manifest even though editorial Source Time is non-negative.

API and model boundaries may render decimal seconds, but conversion is centralized and round-trips to the stored microsecond. Values are clamped only by an explicitly documented media conversion; invalid or inverted editorial values otherwise fail validation.

Every valid Clip Proposal has a required Event point. Optional structure points have these meanings:

- Setup start: earliest included context that is necessary to understand the Event;
- Hook: earliest attention-earning line or action;
- Payoff: resolution, reaction, revelation, or takeaway produced by the Event;
- Exit: point after the Event/Payoff where the clip can end cleanly.

Setup start, Hook, Event, and Payoff satisfy proposal start <= point < proposal end; Exit may equal the exclusive proposal end. Setup start and Hook must not follow Event; Payoff must not precede Event; Exit must not precede Event or a present Payoff. Setup start and Hook have no required order, so a cold-open Hook followed by explanation remains valid. Points may coincide, and optional points are omitted rather than fabricated.

## ASR and canonical transcript

The ASR adapter processes bounded chunks with overlap and cancellation checkpoints. Chunk size, overlap, VAD behavior, language hints, model revision, runtime revision, and decoding options belong to its fingerprint.

The adapter returns raw and normalized text, stable segment and word IDs, half-open Source Time intervals, confidence or no-speech values when available, and detected language at the finest supported granularity. Stitching is deterministic, removes overlap duplicates, validates monotonic timestamps, and cannot mark the transcript complete until every chunk has a valid result.

Raw ASR output is retained for audit. Retrieval consumes a canonical transcript after Unicode NFC, case-folding, and punctuation/whitespace normalization. It preserves Finnish/English code-switching rather than forcing one recording-level language. Silence/music hallucination is a promotion measurement; there is no dedicated production hallucination-filter stage yet.

ASR promotion measures normalized WER and CER, word-timestamp error on a manually aligned subset, silence/music hallucination, full-hour throughput, peak VRAM, recovery, and downstream candidate/boundary quality. The versioned normalization uses Unicode NFC, case folding, punctuation/whitespace normalization, and preserves Finnish letters and code-switched terms. Per-language and code-switch floors are frozen before comparison so “best of the contenders” cannot promote an inadequate backend.

## Observations, retrieval, and candidate budgets

Transcript discovery begins with deterministic sentence-aligned windows targeting approximately 20, 45, 90, and 180 seconds with half-window strides. The exact utterance inclusion, edge handling, embedding instruction, model revision, vector dimension, normalization, and thresholds are versioned.

The first implementation includes a cheap lexical baseline alongside one multilingual embedding query family. Embeddings for one recording are stored as a versioned array artifact with integrity metadata and SQLite lineage; brute-force local search is sufficient for Milestone 1. The embedding path is dropped or deferred if it does not beat the lexical baseline end to end.

Every Candidate Moment includes a point or short span in Source Time, generator and version, generator-local confidence, immutable evidence IDs, category hint, and idempotency key. Confidence values from different generators are not assumed to be calibrated or directly comparable.

Each Context Envelope contains versioned Boundary Anchors derived from envelope edges, every contributing Candidate Moment anchor/span edge, timestamped Evidence Item peaks/edges, transcript word/sentence boundaries, and pauses inferred from transcript-segment gaps. The evaluator chooses proposal boundaries and structure points by anchor ID instead of inventing free numeric timestamps. This guarantees that an audio-only Reaction still has an Event anchor. Later persisted VAD, scene, or game adapters may add anchors through the same contract. A human Boundary Edit may use frame-level Source Time directly.

Initial workload guards are:

- target 15-30 raw Candidate Moments per source hour across generators, with a hard cap of 50 per hour;
- preserve generator, category-query, and recording-section coverage before taking additional results from a dominant generator;
- target 4-10 merged Context Envelopes per source hour, with a hard cap of 100 per Source Recording;
- use a soft unique-envelope-coverage ceiling at the greater of 15 minutes or 10% of source duration, with a hard cap at the greater of 30 minutes or 20% of source duration;
- hard-cap total rendered evaluator prompt input at 100,000 tokens per source hour and 1,000,000 tokens per Analysis Run;
- persist exact prompt tokens and whether a coverage/token guard saturated;
- retain all contributing evidence and provenance through temporal and semantic deduplication;
- treat a valid empty or low-candidate result as an outcome, not a failed stage.

Coverage is measured on the union of Source Time intervals so overlap is not double-counted, while the prompt-token cap counts every rendered prompt so repeated context cannot evade the workload bound. Neither coverage ceiling can exceed the Source Recording. If a hard guard would discard candidates, the selector allocates the remaining budget across generator/category/recording-section coverage and reports saturation. These are versioned operating budgets to prevent unbounded evaluation, not quality targets. They may change only through recorded evaluation.

## Semantic evaluation outcomes

The exact rendered prompt uses stable evidence IDs, escapes delimiter characters inside untrusted JSON, and structurally delimits system instructions from Creator Profile, transcript content, and observations. Transcript and profile text are untrusted data and are never intentionally treated as instructions; schema, anchor, and evidence validation remains necessary because prompt formatting cannot guarantee model behavior.

Each rubric judgment is an anchored integer from 0 through 4. The schema avoids decimal pseudo-probabilities, and the prompt defines what each level means for the selected judgment and category. Risk remains a set of typed flags, not a scalar quality score.

One successful Evaluation Attempt gives a Context Envelope exactly one profile-specific disposition:

- proposal set containing one to a configured maximum of three valid Clip Proposals;
- semantic rejection with reasons;
- insufficient context;
- input too large for the promoted profile;
- invalid for that Model Profile after bounded repair.

A proposal-set disposition links each proposal to the Candidate Moments and Evidence Items it covers and records one outcome for every other contributing Candidate Moment, such as duplicate-of-proposal, too weak, insufficient context, or omitted by the configured proposal cap. No Candidate Moment in a shared envelope disappears without an inspectable outcome.

Cancellation, worker failure, and timeout are terminal states of an Evaluation Attempt, not semantic dispositions of the Context Envelope. The envelope remains pending for a later attempt unless the creator explicitly skips it.

Invalid outputs receive at most one schema-constrained repair within the same Evaluation Attempt using validation errors that do not add new evidence. A second invalid result records invalid-for-profile and remains visible in run status; it never silently disappears from the queue budget or prevent a later attempt with another promoted profile.

Evidence references and Boundary Anchors are validated by ID against the exact input package. Application validation checks resolved intervals, required Event, partial marker ordering, enum values, anchored judgments, evidence identity, and source bounds. “Invented timestamp” means an evidence citation or boundary that does not resolve from an allowed input ID; model-selected proposal times are otherwise legitimate choices among Boundary Anchors.

The exact raw response, request metadata, and validation errors are retained as private attempt artifacts for audit. Only validated fields may create a Clip Proposal.

Machine Risk flags alone cannot produce semantic rejection or ranking ineligibility. They remain visible evidence for the human editor.

Milestone 1 begins with a 32K integration cap, records the real prompt-size distribution, and may promote a smaller cap when every representative envelope and output reserve fits. Larger context profiles remain diagnostics until separately promoted. The current adapter marks an oversized envelope `input_too_large`; a future policy may explicitly re-envelope it. Input is never silently truncated and the server is not repeatedly relaunched per candidate.

## Proposal duration and boundaries

Category duration ranges are soft editorial defaults:

| Category | Preferred duration |
|---|---:|
| Reaction | 15-60 seconds |
| Comedy | 20-90 seconds |
| Story | 45-180 seconds |
| Opinion | 30-180 seconds |
| Explanation | 60-240 seconds |

Every machine Clip Proposal has a hard maximum of 240 seconds. A proposal outside its category's preferred range requires a structured duration-exception reason. Evaluator boundaries resolve from Boundary Anchors, preferring sentence/word edges and pauses; human Boundary Edits may make frame-level corrections.

## Ranking and Queue Snapshots

Only valid proposals enter ranking. The fixed baseline formula, eligibility rules, normalization, diversity parameters, category/section coverage, temporal suppression, semantic similarity threshold, and deterministic tie-breakers are versioned and persisted with each Queue Snapshot.

Machine Risk flags are not a negative quality score and never silently filter a proposal. Generator confidence and evaluator judgments remain separately inspectable.

A Queue Snapshot never reorders or gains members while it is being reviewed. Requesting more proposals starts a new Analysis Run with an explicitly expanded retrieval/evaluation budget, reuses compatible ASR/embedding generations, evaluates only the candidate delta, pins the exact parent proposal/rank/score prefix, and creates a new Queue Snapshot. Existing proposal identities and decisions remain available for comparison. Evaluation discovery for that child uses the parent-plus-delta candidate union.

## Review and export

Editorial decisions are append-only revisions with idempotency keys and optimistic concurrency. Repeated keyboard events or browser retries cannot create contradictory current decisions. Undo appends an explicit withdrawn revision rather than deleting history.

A maybe decision is unresolved rather than a weak positive: it is not exportable and is excluded from binary ranker training until the creator resolves it or an explicitly versioned learning policy treats it separately.

A rejection requires one structured Rejection Reason, including unusable proposed boundaries when the editor does not want to repair them. A Boundary Edit is optional for accept, maybe, or reject, so the system can learn both accepted corrections and boundary failures without biasing the selection label.

Boundary controls may extend outside the evaluated Context Envelope. The requested interval is preserved immediately, marked outside-evaluated-context, and shown with stale evidence/Risk coverage. For an edited interval no longer than the 240-second machine-proposal maximum, the editor may request evaluation through a new Analysis Run that reuses compatible artifacts and creates a successor Clip Proposal/Queue Snapshot linked by supersedes rather than revising the original. Longer human edits remain valid review data but cannot seed machine successor evaluation. Exporting without that optional re-evaluation requires an additional stale-coverage confirmation.

An Export can start only when the latest overall Editorial Decision revision is accept; an older historical accept is not exportable after maybe, reject, or withdrawn. The client supplies the exact decision revision it observed, and the server rejects a stale tab before reserving work. Unsaved visible boundary edits disable Export and reanalysis until a new decision revision records them. Every export requires explicit human confirmation; Risk-flagged and stale-evidence intervals receive additional warnings. Confirmation snapshots the exact current accept revision, accepted interval, source hash, FFmpeg build, and arguments. Existing Exports remain immutable history if a later decision changes.

Exports use re-encoding for frame-accurate boundaries, write to an attempt-owned partial file, validate streams and playable duration, hash and atomically place the output, then register a new immutable Export. A changed decision creates a new export generation and never silently overwrites the old one.

The default source-aspect Export is MP4 with H.264/libx264 at CRF 18 and preset medium, yuv420p, AAC at 48 kHz and 192 kbit/s, and fast-start metadata. It strips source metadata and chapters, preserves source display aspect, normalizes declared rotation, and minimally pads odd dimensions when the codec requires even dimensions. The capability preflight must verify these encoders/options; the export profile is versioned and replaceable rather than silently falling back.

## Creator operating flow

Milestone 1 uses a small CLI for filesystem-sized operations and the local web application for interactive work:

~~~text
.\.venv\Scripts\highlight-clipper.exe setup --baseline
.\.venv\Scripts\highlight-clipper.exe import <absolute-local-media-path> [--video-stream <id> --audio-stream <id>]
.\.venv\Scripts\highlight-clipper.exe analyze <source-recording-id>
.\.venv\Scripts\highlight-clipper.exe analyze <source-recording-id> --request-more-from <analysis-run-id>
.\.venv\Scripts\highlight-clipper.exe serve
.\.venv\Scripts\highlight-clipper.exe evaluate <succeeded-analysis-run-id>
.\.venv\Scripts\highlight-clipper.exe backup [--destination <path>]
.\.venv\Scripts\highlight-clipper.exe backup --restore <backup-directory>
~~~

Import copies directly from the supplied path into the Work Directory; a browser upload is not used for multi-hour media. In the web application the creator edits/version-controls the Creator Profile, selects an imported Source Recording, starts/resumes/cancels/retries analysis, sees stage progress and actionable failures, annotates Reference Moments, reviews Queue Snapshots, and confirms exports.

Reference annotation mode provides source playback, category/certainty/language slice, ideal start/end, required Event anchor, and rationale while hiding proposal content in that workspace. This is workflow blinding, not access control: the creator can still navigate to Review first, so sealed-corpus discipline or future exposure tracking is required.

## Local web boundary

The web application binds only to 127.0.0.1, disables CORS, validates Host and Origin, and requires a random per-server-session token for mutating requests. Only integrity-verified `review_proxy` and `export` artifacts are served by opaque database ID with containment checks and bounded byte ranges; originals, analysis audio, evaluator responses, and other private artifacts are rejected.

Transcript, Creator Profile, evidence, and model-generated content are rendered as text rather than trusted HTML. SQL is parameterized. Successful worker request/result files containing transcript text are deleted immediately. Log size rotation is not yet implemented, so logs remain private Work Directory diagnostics and should be included in the retention hardening pass.

## Backup and recovery

The application provides consistent backup creation, portable creator-label export, verification, and restore. Restore is a stopped-app operation: it verifies the selected source, refuses a recorded active operation, creates a consistent pre-restore backup when the current database is healthy (or preserves corrupt/unmigrated database/WAL/SHM files best-effort), atomically installs the snapshot, applies migrations, and runs integrity checks. Application-specific retention/cleanup remains:

- a consistent local SQLite snapshot using SQLite's backup API, written beneath the Work Directory by default and optionally copied and verified at a user-selected external destination;
- a portable versioned label package containing Source Recording hash/time manifest, Creator Profile and Reference Moment revisions, relevant Analysis Run/model/config identity, immutable Clip Proposal contents and evidence links, Queue Snapshot ranks, Editorial Decision revisions, Boundary Edits, and artifact hashes;
- a verified restore path with pre-restore safety preservation;
- app-specific cleanup that distinguishes valuable data from disposable caches and partial outputs.

Recordings and rendered media are not duplicated by the metadata backup. Their hashes and expected relative paths make missing files detectable. Cleanup may garbage-collect regenerable derived bytes no longer referenced by an active/reviewed generation after a configured retention period, while preserving lineage, editorial history, exports, and the ability to report what was removed. The application never recommends Git cleanup commands for Work Directory maintenance.
