# Implementation status

**Status:** Working first vertical slice; corpus-based promotion is still pending.

This page is the current truth table. [Milestone 1](./milestone-1.md) and [Pipeline contracts](./pipeline-contracts.md) remain the normative design and acceptance specification; a requirement there is not automatically an achieved result here.

## Capability matrix

| Capability | Implemented now | Verified now | Remaining gate |
|---|---|---|---|
| Repository-local environment and data | `.venv` is repository-local; llama.cpp, models, caches, media, SQLite, artifacts, logs, backups, and exports live below ignored `workdir/` | Setup, migration, integrity checks, and path-containment tests | Packaging beyond the current Windows developer workflow |
| Immutable media import | Copy-with-hash, stream selection, Source Time manifest, review proxy, 16 kHz mono analysis audio, compact import-time waveform cache, artifact registration | Ordinary and nonzero-PTS/multi-stream fixtures; proxy/audio/waveform/export alignment | Representative long, VFR, rotated, damaged, and unusual-codec corpus |
| Fake vertical slice | Deterministic offline ASR/evaluator path through the same run, queue, review, and export contracts | Default offline tests | Diagnostic only; it says nothing about model quality |
| ASR | Disposable faster-whisper CUDA worker, Turbo and large-v3 profiles, deterministic chunks, overlap stitching, fingerprinted checkpoints, cancellation, and explicit Silero VAD (500 ms minimum silence, 200 ms speech padding) | Whisper Turbo loaded on the target RTX 4090 and produced timestamped transcript data in a real run | Turbo versus large-v3 corpus bake-off; Parakeet adapter and Windows feasibility test |
| Transcript retrieval | Lexical windows plus Qwen3-Embedding-0.6B in a disposable CPU worker; versioned `.npy` artifact and lineage | Qwen embedding worker completed in the real run | Held-out recall/latency ablation; BGE-M3 is download-only, not an analysis adapter |
| Candidate construction | Independent deterministic transcript generators, block-local robust energy/change observations, transcript-derived 5-second speech activity/rate/pause-change evidence, fair merge, provenance, 15-minute section-balanced selection, five-minute envelope cap, evidence, and boundary anchors | Nonstationary-audio, speech-change, late-recording fairness, and envelope-cap tests plus real-run persistence inspection | Accepted-example, chat, richer-audio, and visual generators only after measured ablation |
| Local semantic evaluator | Managed loopback llama.cpp server, escaped untrusted envelope, exact prompt counting, `anchored-json-v7` strict schema, one repair, evidence/anchor/application validation, and private raw failed-output audit | Qwen3.6-35B-A3B no-MTP completed a real evaluation on pinned llama.cpp `b9956`; post-completion failures and network resets retain token/audit evidence | Fixed 100-envelope validity screen and four-model quality/runtime comparison |
| GPU lifecycle | Application-wide Windows named mutex, Job Object ownership, one model process at a time; ASR exits before llama.cpp starts; evaluator PID, startup/evaluation duration, effective context, and VRAM before/loaded/delta are persisted | Real run left no owned `llama-server` process and completed without simultaneous ASR/LLM residency | Representative cancellation/crash and external-VRAM-pressure testing; named-mutex owner, effective offload, peak VRAM, and post-unload llama VRAM are not persisted |
| MTP | Qwen embedded-head and Gemma separate-drafter launch paths behind explicit `--mtp` | Qwen 35B MTP loaded and returned strict JSON in a smoke request | Paired complete-batch latency, acceptance, quality, and stability measurements; MTP is not the default |
| Creator Profile | Versioned languages, desired/avoided content, editable 0-4 category priorities, and per-category duration ranges; priorities affect transparent ranking and durations affect evaluator validation | API validation, prompt, and ranking tests | Learn weights only after enough whole-recording decisions |
| Queue and review | Dynamic 10–30 target, category/section/temporal/content diversity, immutable Queue Snapshots, Request More with a pinned parent prefix, evidence/judgments/structure/concerns, Previous/Next navigation, Maybe filter, live boundary waveform, append-only decisions, and targeted outside-context successor reanalysis up to 240 seconds | API/ranking/lineage tests and local browser smoke against a real queue; hidden views pause playback and stale async UI results are fenced | Representative creator usability study and multi-session timer validation |
| Reference annotation | Create/freeze revisions with certainty, category, language slice, interval, Event, suitability, and rationale; References view uses source playback and hides proposal content | API/UI smoke | No hard access-control barrier against visiting Review first; sealed work still needs annotation discipline/exposure tracking |
| Export | Explicit confirmation, current-decision fencing, risk/stale-coverage confirmation, original-source H.264/AAC render, source metadata/chapter stripping, validation, hash, immutable generation | A real accepted proposal exported to a playable H.264/AAC MP4; timing and metadata stripping have fixture coverage | Representative duration/aspect/rotation/VFR corpus and disk-full fault fixture |
| Cancellation, retry, and recovery | Attempt ownership, live worker PID/progress, cancellation requests, error-classified retries capped at three, checkpoint/explicit lineage reuse, startup reconciliation, partial quarantine, and idempotent fenced decision/export commands | Offline cancellation/recovery/idempotency tests and real process cleanup | Broader crash matrix, disk-full fixture, orphan audit, log rotation, and long-run soak |
| Backup and restore | SQLite backup API, portable creator-label package, verification, pre-restore safety preservation, corrupt-current restore path, atomic snapshot replacement, migration, and integrity checks | Automated healthy-current and corrupt-current restore tests | Application-specific media retention/garbage-collection workflow; metadata backup does not contain media bytes |
| Local web boundary | Loopback binding, Host/Origin checks, per-session mutation token, CSP, opaque media IDs, bounded ranges, text-only rendering | Security/API tests and browser smoke with no console warnings/errors | Authentication and remote access are intentionally out of scope |
| Evaluation and promotion | Coherent-snapshot deterministic report schema v2 with complete metric-input fingerprint, frozen-reference enforcement, `event-max-cardinality-rank-overlap-v2` matching, parent+delta Request More discovery, queue-filtered decisions, recall@10/20/30, boundary/tIoU, slices, and active-review metrics; human-seeded reanalysis is excluded | Matching-cycle/tie, report invalidation, expanded-lineage, and end-to-end fake-run determinism tests | Cross-recording macro/experiment aggregation, ablation/bake-off runner, representative development corpus, frozen budgets, and sealed holdout; no quality gate is claimed yet |

## Target-machine integration proof

The final policies have two deliberately separate pieces of target-machine evidence; they must not be combined into a fictional cold timing:

- `analysis_6232af8ef89d4d44a86c2bb3c950b6d4` created the new VAD-keyed Whisper Turbo checkpoint on the 24 GB RTX 4090; its cold ASR stage took about 26 seconds. The run then exercised bounded retry observability through a network reset and a server-start exit, but its final pre-v7 evaluator result was `invalid_for_profile` and its queue contained zero proposals.
- `analysis_04858cb0a0f84deeaa5a413756a8d1f3` is the current `anchored-json-v7` / `text-audio-speech-embedding-section-balanced-v3` / `fixed-diverse-profile-v3` smoke. It completed in 87.7 seconds, recorded 20,953 prompt tokens, used Qwen3.6-35B-A3B at an effective 32,768-token context with MTP off, required one bounded application/schema repair, and produced two proposals in `queue_4526f43627344ca784f69f7e458a13bc`. Its ASR worker reused the compatible completed checkpoint, so this is not a cold end-to-end timing. Evaluator telemetry recorded 7.7 seconds of server startup, 62.8 seconds of evaluation, 1,103 MiB VRAM before load, 19,174 MiB loaded, and an 18,071 MiB load delta.

An earlier v6/v2 run also completed a human accept and verified source render to H.264/AAC MP4 with source metadata and chapters stripped. That remains export-plumbing evidence, but it predates the final prompt/retrieval/ranking policies. Browser review of the current real queue completed without console warnings or errors, and the managed server exited with no `llama-server` process left behind.

Together these runs prove current schema, lifecycle, retry observability, telemetry, queue, and export integration. They do not establish cold representative throughput, highlight recall, Finnish/English accuracy, creator usefulness, boundary quality, or a model winner.

The Qwen 35B embedded-MTP path also completed a load/strict-JSON smoke. Its cold model load dominated that tiny request, so the result is not a reason to enable MTP. The 27B Qwen and both Gemma assets have committed catalog/launcher support but have not been downloaded and screened on the project workload.

## What is deliberately not in the first baseline

Chat, visual/OCR/VLM processing, richer audio classifiers, a learned personalized ranker, vertical reframing, captions, publication integrations, multiple users, remote access, and distributed infrastructure remain deferred. Their extension seams are present; speculative implementations are not.

## Promotion sequence

The shortest honest path to a quality-promoted release is:

1. annotate three representative development recordings without consulting system proposals;
2. freeze the implemented matching/report policy, add cross-recording aggregation, and freeze runtime/product budgets and ablation definitions;
3. compare Whisper Turbo/large-v3 and the four exact evaluator profiles on the frozen material;
4. choose the simplest profile that clears every quality, reliability, fit, and latency gate;
5. test bounded thinking, larger context, and MTP only on finalists; and
6. run one sealed whole-recording evaluation once after configuration freeze.

Until that sequence is complete, `whisper-turbo` + `qwen3-embedding-0.6b` + `qwen36-35b-a3b` at 32K/no-MTP is the integration baseline, not a declared quality winner.
