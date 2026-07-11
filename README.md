# Highlight Clipper

Highlight Clipper is a local-first, evidence-backed highlight retrieval, review, and export application. The first release runs a complete text/audio pipeline on one creator's recordings: immutable import, GPU ASR, GPU transcript embeddings, fully GPU-offloaded local llama.cpp evaluation, a human review queue, append-only decisions, and verified exports from the original media.

The current baseline is deliberately useful before adding chat, visual models, or a learned ranker. Those later signals plug into stable evidence, candidate, evaluator, ranking, and renderer contracts instead of replacing creator-owned history.

See [implementation status](docs/implementation-status.md) for the exact boundary between implemented, hardware-verified, and still awaiting corpus evidence.

## First local run

The real-model path currently targets Windows, an NVIDIA GPU, PowerShell, `uv`, and FFmpeg/FFprobe on `PATH`. Python is installed into the repository-local `.venv`. Models, llama.cpp, caches, media, SQLite, logs, artifacts, backups, and exports stay below the Git-ignored repository-local `workdir/`.

```powershell
$env:UV_CACHE_DIR = (Join-Path $PWD "workdir\cache\uv")
uv sync --extra dev --extra asr --extra embeddings --extra models
.\.venv\Scripts\highlight-clipper.exe setup --baseline
```

`setup --baseline` installs the pinned llama.cpp bundle plus Whisper Turbo, Qwen3-Embedding-0.6B, and Qwen3.6-35B-A3B. It verifies media capabilities and hashes every installed asset. Plain or baseline setup also backfills compact waveform caches for recordings imported by an older checkout. Allow roughly 50 GB of free space for models, download cache, and safe temporary headroom.

Import one recording, keeping the returned `source_recording_id`:

```powershell
.\.venv\Scripts\highlight-clipper.exe import C:\absolute\path\to\recording.mp4
```

Start the loopback-only workspace and open `http://127.0.0.1:8765`:

```powershell
.\.venv\Scripts\highlight-clipper.exe serve
```

Use **Sources → Run local analysis** for the default real pipeline. The equivalent CLI command is:

```powershell
.\.venv\Scripts\highlight-clipper.exe analyze <source_recording_id>
```

For a recording with a known Finnish language track, pass the language code to faster-whisper explicitly:

```powershell
.\.venv\Scripts\highlight-clipper.exe analyze <source_recording_id> --asr-language fi
```

Only one GPU model is resident at a time: the ASR worker loads, checkpoints and exits; the CUDA embedding worker loads, writes its vectors and exits; then the fully GPU-offloaded managed llama.cpp server loads, evaluates and exits. A Windows named mutex and Job Object serialize all three model stages and clean up their process trees on cancellation or failure.

The default queue target adapts to source length (`min(30, max(10, ceil(3 × source hours)))`) and applies category, 15-minute-section, temporal-overlap, and content-similarity diversity. Review exposes evidence, structure, reasons against selection, live edited-boundary waveforms, explicit Previous/Next navigation, and a Maybe-only filter. An edit outside evaluated context can launch a successor analysis for an interval up to 240 seconds without mutating the old proposal, decision, or queue.

For a fast pipeline/control-plane check without model assets:

```powershell
.\.venv\Scripts\highlight-clipper.exe analyze <source_recording_id> --fake
```

The fake path is diagnostic only and is never selected implicitly.

To expand a completed default-budget run while preserving its exact queue prefix and decisions:

```powershell
.\.venv\Scripts\highlight-clipper.exe analyze <source_recording_id> `
  --request-more-from <analysis_run_id>
```

The CLI inherits the parent run's model, context, MTP, and profile settings. The child reuses compatible ASR and embedding generations, evaluates only the candidate delta, and writes a new immutable Queue Snapshot.

## Measuring a run

Create and freeze Reference Moments in the **References** workspace before consulting the system queue. The annotation view plays source media while hiding proposal content. It records category, language slice, ideal interval, Event, certainty, suitability, and rationale.

Score one completed Analysis Run with deterministic one-to-one matching:

```powershell
.\.venv\Scripts\highlight-clipper.exe evaluate <analysis_run_id>
```

The command writes and registers a deterministic hashed report below `workdir/artifacts/evaluation-reports/`. It reports discovery recall, queue recall@10/20/30, boundary error/tIoU, category/language slices, decisions, and active review time. Cross-recording model/ASR bake-off aggregation and the representative creator corpus are still required before declaring any profile the quality winner.

## Model and runtime experiments

The committed catalog pins exact Hugging Face revisions, filenames, execution profiles, and llama.cpp release archives. Do not place a loose `llama-server.exe` in the repository root.

```powershell
.\.venv\Scripts\highlight-clipper.exe setup --list-models
.\.venv\Scripts\highlight-clipper.exe setup --model whisper-large-v3
.\.venv\Scripts\highlight-clipper.exe setup --model gemma4-26b-a4b
.\.venv\Scripts\highlight-clipper.exe setup --all-evaluators
```

Select an evaluator or override the default 32K context cap per Analysis Run:

```powershell
.\.venv\Scripts\highlight-clipper.exe analyze <source_recording_id> `
  --evaluator gemma4-26b-a4b --context-size 32768
```

`--mtp` enables that profile's experimental Multi-Token Prediction path. Qwen uses its embedded MTP head; Gemma uses the separately pinned drafter GGUF. MTP is not the default until paired complete-workload measurements show a material win without a quality or stability regression.

See [local runtime and model profiles](docs/local-runtime.md) for exact model locations, Qwen/Gemma parameters, context policy, MTP launch differences, and the four-model promotion plan.

## Data safety and backup

- Originals below `workdir/sources/` are immutable copies and are never edited in place.
- Analysis generations, queue snapshots, decisions, boundary edits, and exports are append-only or immutable.
- The web app binds to loopback, validates Host/Origin and a per-session mutation token, and serves media only through opaque database IDs.
- `workdir/` is ignored by Git but contains valuable data. Never use `git clean -xfd` as an application cleanup command.

Create and verify a consistent SQLite plus portable-label backup:

```powershell
.\.venv\Scripts\highlight-clipper.exe backup
.\.venv\Scripts\highlight-clipper.exe backup --verify <backup-directory>
.\.venv\Scripts\highlight-clipper.exe backup --restore <backup-directory>
```

Restore verifies the selected backup, preserves the current database in a safety directory, installs and migrates the snapshot, and runs integrity checks. Metadata backups intentionally do not duplicate recordings or rendered media, so they cannot recover missing media bytes by themselves. Stop the local app before restoring.

## Verification

The ordinary suite is deterministic, offline, and CPU-only:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
```

Real-model, GPU, and representative-long-media checks remain explicit because they require local assets and substantially more time.

## Design documentation

- [Implementation status](docs/implementation-status.md)
- [Milestone 1: trustworthy local review queue](docs/milestone-1.md)
- [Pipeline contracts](docs/pipeline-contracts.md)
- [Local runtime and model profiles](docs/local-runtime.md)
- [Product roadmap](docs/roadmap.md)
