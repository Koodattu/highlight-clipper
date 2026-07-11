# Local runtime and model profiles

**Status:** Implemented operating runbook; exact assets are pinned, while benchmark winners remain unselected.

The committed source of truth is [`model-catalog.json`](../src/highlight_clipper/assets/model-catalog.json). The working baseline and the still-unmeasured promotion gates are summarized in [Implementation status](./implementation-status.md).

All private, generated, downloaded, and machine-specific application data lives below the repository-local `workdir/`. The entire directory is ignored by Git with one anchored rule. The repository-local `.venv/` is the one runtime exception outside `workdir/`; it is also ignored. Tracked source code, portable configuration, tests, and documentation stay outside both.

## Runtime layout

```text
workdir/
  runtime/
    llama.cpp/<pinned-build>/     llama-server.exe and matching DLLs
  models/
    llm/<profile>/                target GGUF and optional MTP drafter
    asr/                          faster-whisper or Parakeet weights
    embeddings/                   transcript embedding model
  sources/<source-id>/            immutable imported recordings
  state/highlight-clipper.sqlite3  SQLite database and WAL/SHM sidecars
  artifacts/
    asr/<run-id>/                  registered ASR output
    asr-checkpoints/<fingerprint>.json reusable completed ASR checkpoints
    embeddings/<run-id>/<generation-fingerprint>/ vectors and manifest
    evaluator/<run-id>/            private evaluator responses
    evaluation-reports/<run-id>/   deterministic metric reports
  exports/<source-id>/             rendered Export generations from the original
  backups/                         consistent database and portable label backups
  cache/
    huggingface/
    torch/
    uv/
  logs/
  tmp/                            incomplete, disposable stage output
```

The application resolves every relative path from the repository root, not from the caller's current directory. It gives Source Recordings, Analysis Runs, and attempts short generated identifiers rather than reproducing long video titles in paths.

`sources/`, `state/`, `artifacts/`, `exports/`, and `backups/` are valuable user data even though Git ignores them. `cache/`, `logs/`, and `tmp/` are disposable. In particular, `git clean -xfd` can delete the entire ignored work directory and must not be used as a cleanup command.

Workers write attempt-owned temporary output beside its destination with a `.partial` suffix. A file becomes a usable artifact only after validation, required integrity metadata, atomic placement, and SQLite registration under the recovery protocol in [Pipeline contracts](./pipeline-contracts.md). Originals under `sources/` are never modified. SQLite backups use SQLite's backup mechanism rather than copying only the main file while WAL mode is active.

Setup sets `HF_HOME`, `TORCH_HOME`, and `UV_CACHE_DIR` for model downloads to the corresponding directories above. Model libraries also receive an explicit download directory. This prevents an accidental second model cache under the Windows user profile.

The runtime manifest records OS/Python identity, CPU/logical cores, RAM and pagefile snapshots when available, Work Directory capacity, NVIDIA GPU/driver output, `nvcc` output when available, discovered CUDA/cuDNN DLL hashes, installed asset-manifest identities, locale, FFmpeg/FFprobe executable hashes, and—unless media checking is explicitly skipped—the media preflight with FFmpeg/FFprobe version strings. Import and Export manifests also record their media-tool versions. The runtime manifest does not currently record the Python lock hash, storage-device identity, or explicit process architecture. These diagnostics do not invalidate editorial history automatically.

## Setup commands

Create the repository-local environment and redirect uv's install cache before dependency resolution:

```powershell
$env:UV_CACHE_DIR = (Join-Path $PWD "workdir\cache\uv")
uv sync --extra dev --extra asr --extra embeddings --extra models
```

Then use the repository-local executable:

```powershell
.\.venv\Scripts\highlight-clipper.exe setup --baseline
.\.venv\Scripts\highlight-clipper.exe setup --list-models
.\.venv\Scripts\highlight-clipper.exe setup --model whisper-large-v3
.\.venv\Scripts\highlight-clipper.exe setup --all-evaluators
```

Plain `setup` initializes/verifies local state and media prerequisites but downloads no model or llama.cpp asset. `--baseline` installs llama.cpp `b9956`, Whisper Turbo, Qwen3-Embedding-0.6B, and Qwen3.6-35B-A3B. `--all-evaluators` installs llama.cpp and the four LLM profiles; combine it with `--baseline` when the ASR and embedding baseline is also needed.

## ASR profiles and model locations

The baseline adapter pins [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper), CTranslate2, and their Python dependencies in the project lock. It pre-downloads a reviewed repository revision into the Work Directory, then workers receive only the absolute local path and `local_files_only` behavior.

| Profile | Model repository | Role |
|---|---|---|
| `whisper-turbo` | [`mobiuslabsgmbh/faster-whisper-large-v3-turbo`](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo) | Lowest-integration-risk first baseline; this is the repository currently mapped by faster-whisper's `turbo` alias |
| `whisper-large-v3` | [`Systran/faster-whisper-large-v3`](https://huggingface.co/Systran/faster-whisper-large-v3) | Accuracy contender using the same adapter/runtime |
| `parakeet-v3` | [`nvidia/parakeet-tdt-0.6b-v3`](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) | Optional contender only after native Windows/RTX 4090 NeMo smoke-test success |

Each ASR Profile pins the model repository/revision; setup writes installed-file hashes to the local asset manifest. Adapter/runtime dependencies are locked, while compute type, explicit Silero VAD parameters, chunk/overlap/stitching, language hints, timestamp policy, and normalization version are code/configuration fields included in execution fingerprints. Turbo and large-v3 use the same worker contract so their comparison changes the profile rather than the pipeline.

ASR setup places weights under `workdir/models/asr/<profile>/` and caches under `workdir/cache/huggingface/`. Analysis passes an absolute installed path to the worker, so it does not perform a model download. The worker runs bounded chunks/checkpoints and exits after transcription, releasing owned CUDA allocations through process-tree exit. Parakeet is currently a download-only diagnostic entry and has no analysis adapter; it does not become a dependency unless a later Windows bake-off justifies implementing it.

## Embedding profile

The first multilingual embedding integration uses [`Qwen/Qwen3-Embedding-0.6B`](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) in a disposable CUDA worker, with a pinned revision, hashes, tokenizer, query instruction, pooling, normalization, output dimension, dtype, and windowing policy. The `embeddings` dependency extra is Windows-only and selects the official CUDA 13.0 PyTorch wheel. Model execution and vector normalization use CUDA with bfloat16 model compute and SDPA; the completed float32 vectors are copied to host memory only for validation and atomic `.npy` persistence. Missing CUDA support or CPU tensor fallback fails the worker.

The required lexical retrieval baseline runs without this model. If the embedding path does not improve held-out end-to-end recall enough to justify indexing time and storage, it is removed from the promoted Milestone 1 profile. [`BAAI/bge-m3`](https://huggingface.co/BAAI/bge-m3) is an optional challenger only after the first complete baseline, not another permanent adapter.

Embedding setup stores the current profile under `workdir/models/embeddings/qwen3-0.6b/`, uses the Work Directory caches, and passes an absolute local path with network access disabled. The worker acquires the same GPU Lease as ASR and llama.cpp; process-tree exit is the unload boundary.

## Installing llama.cpp

Do not put an unversioned `llama-server.exe` loose in the repository root. `setup --baseline`, `setup --llama-cpp`, or `setup --all-evaluators` installs the two pinned upstream [llama.cpp release](https://github.com/ggml-org/llama.cpp/releases) archives:

1. Read the committed `b9956` archive URLs and expected SHA-256 values from the catalog.
2. Download the Windows x64 CUDA 12 binary archive and matching CUDA runtime archive from that release.
3. Extract both into `workdir/runtime/llama.cpp/<release-tag>/`, keeping the EXE and its matching backend/runtime DLLs together.
4. Record the release tag, archive metadata and expected hashes, server-relative path, and complete extracted-file manifest. The target-machine runtime manifest records the resulting asset-manifest identity.
5. Install a newer build into a new versioned directory. Never overwrite DLLs belonging to a running build or mix files from different releases.

Setup verifies the catalog-supplied archive hashes, safe extraction, complete local file manifest, and required `--help` flags. CUDA loading, structured output, model identity, and owned process-tree shutdown are exercised when a real evaluator runs; setup alone does not claim those runtime checks. The pinned build exposes both Qwen embedded-head and Gemma separate-drafter MTP flags; Qwen has received a strict-JSON launch smoke, while Gemma has not yet received a project-workload smoke.

A source build is a developer diagnostic when a required fix has not reached a release. The application does not currently offer a custom-runtime override: using one requires an explicit catalog/source change and a new verified manifest. For an RTX 4090, such a build should enable CUDA and target compute capability 8.9 as described by the upstream [Windows/CUDA build documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md).

## How the application runs llama.cpp

### End-to-end residency lifecycle

The real Analysis Run is ordered to protect a 24 GB card:

1. The faster-whisper worker acquires the global GPU mutex, loads its local model lazily, transcribes/checkpoints bounded chunks, and exits.
2. Qwen transcript embeddings acquire the same mutex, run in a separate CUDA worker, persist their vectors, and exit.
3. Only after embeddings have exited does the managed llama.cpp server acquire the same mutex, fully offload one selected evaluator, process all pending envelopes for the run, and close in `finally` on success, cancellation, or failure.
4. Process-tree exit is the unload boundary. Whisper, Qwen embeddings, and llama.cpp are never intentionally resident together, and the server is not kept alive between Analysis Runs.

This is the implemented VRAM policy. Future visual or learned models must use the same ownership contract rather than creating an independent resident service.

Milestone 1 uses a directly managed `llama-server` child process, not an always-running server and not router mode:

1. Select one committed Model Profile and explicit context cap.
2. Acquire the application-wide GPU lease through an OS-owned Windows named mutex.
3. Launch the pinned `llama-server.exe` in a kill-on-close Windows Job Object using an argument array, with its runtime directory as the child working directory.
4. Wait for the loopback health endpoint and verify the loaded model identity.
5. Evaluate the compatible pending Context Envelopes for that Analysis Run through the OpenAI-compatible API, using a JSON schema on every request.
6. Stop the server, close the Job Object on timeout or cancellation, and wait for the owned process tree to exit.
7. Release the GPU lease before another GPU worker can start.

The named mutex prevents two local application instances from intentionally owning the GPU concurrently. The owned process tree exiting is the unload boundary. The mutex owner is not persisted in SQLite, and the current llama.cpp adapter does not enforce a before/after WDDM-memory warning policy. The launcher requires full target and drafter offload with fitting disabled, so external desktop allocations can cause an explicit startup failure instead of silent CPU-layer fallback.

The launcher allocates an available loopback port rather than assuming port 8080 is free and never binds the evaluator to `0.0.0.0`. It uses a random per-launch API key and verifies health, requested effective context, and loaded model identity before sending private transcript content. stdout/stderr are written below `workdir/logs/llama.cpp/`; log rotation and a retry for the small port-selection/bind race remain hardening work.

The common server arguments are:

```text
--host 127.0.0.1
--port <allocated-port>
--api-key <random-per-launch-token>
--no-ui
--model <absolute-target-gguf>
--ctx-size <promoted-context-cap>
--n-predict <profile-output-cap>
--parallel 1
--n-gpu-layers all
--fit off
--flash-attn on
--cache-type-k q8_0
--cache-type-v q8_0
--no-mmproj
--offline
```

These names follow the current [llama-server argument reference](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md). The application checks `--help` during setup so a changed or missing flag fails before processing a long recording.

Sampling, thinking, seed policy, and response schema are explicit Model Profile/request fields rather than server defaults. The implemented Qwen profiles use temperature 0.7, top-p 0.8, top-k 20, min-p 0, presence penalty 1.5, fixed seed 3407, and `enable_thinking: false`. The Gemma profiles use temperature 1.0, top-p 0.95, top-k 64, min-p 0, no presence penalty, fixed seed 3407, and no chat-template kwargs. Every current profile has a 2,048-token output cap and requests `reasoning_format: auto` so the server separates reasoning when present.

The user's earlier 0.6/0.95/20/0 Qwen command remains a useful manual observation, but the committed non-thinking settings follow the current model-profile guidance and are the reproducible baseline. Multi-seed screening and a bounded-thinking 4,096-reasoning/6,144-total contender remain experiment designs; they are not selectable execution modes yet.

llama.cpp `b9956` requires the OpenAI-style nested structured-output shape used by the adapter:

```json
{
  "response_format": {
    "type": "json_schema",
    "json_schema": {"name": "highlight_evaluation", "strict": true, "schema": {}}
  }
}
```

The simpler direct-schema form failed against the pinned server during integration. This compatibility detail is covered by the real evaluator smoke and must be rechecked before changing the runtime bundle.

### Context policy

`--ctx-size` is the server slot's combined prompt-and-generation capacity. With `--parallel 1`, one request can use the configured tier. `--n-predict` limits newly generated tokens; it is not the input context size.

The promoted integration default is 32,768 tokens. `--context-size` allows an explicit value from 8,192 through 262,144; the application does not automatically choose or relaunch tiers. After server health succeeds and before generation, it calls `/apply-template` and `/tokenize` to count the exact rendered request, reserves 2,048 output tokens, and requires the server-reported prompt usage to match. A mismatch fails the attempt rather than risking silent context shift or truncation.

Compatible envelopes are processed during one managed server lifetime. An envelope that does not fit is recorded as `input_too_large`; it is never silently truncated. Automatic re-enveloping is not implemented. Contexts above 32K are manual diagnostics at 64K, 128K, or 256K and may reduce GPU offload or fail the 24 GB fit constraint; promotion requires representative prompts and complete quality/latency/VRAM measurements.

The selected and server-reported effective context, rendered-prompt hash, prompt/final/reasoning token counts, MTP flag, prompt/schema version, runtime/model manifest hashes, worker PID, server-startup time, evaluation time, and VRAM before/loaded/delta are persisted. Stage wall time is derivable from attempt start/end timestamps. KV types are fixed in the launcher; effective GPU-layer offload, prompt/decode timing split, peak VRAM, and post-unload llama VRAM are not yet persisted.

## Evaluator candidates

The initial real integration baseline is the user's already demonstrated Qwen3.6-35B-A3B quantization with MTP disabled. This reduces integration uncertainty; it is not declared the quality winner. The fixed-corpus screen then compares four text-only deployable profiles. Vision projectors are deliberately not downloaded for Milestone 1.

| Profile | Architecture | Initial target on a 24 GB RTX 4090 | MTP representation |
|---|---|---|---|
| `qwen36-35b-a3b` | MoE, about 35B total and 3B active | [`unsloth/Qwen3.6-35B-A3B-MTP-GGUF`](https://huggingface.co/unsloth/Qwen3.6-35B-A3B-MTP-GGUF), `Qwen3.6-35B-A3B-UD-IQ4_NL.gguf`, about 18.5 GB | Embedded in the target GGUF |
| `qwen36-27b` | Dense, 27B active | [`unsloth/Qwen3.6-27B-MTP-GGUF`](https://huggingface.co/unsloth/Qwen3.6-27B-MTP-GGUF), `Qwen3.6-27B-UD-Q4_K_XL.gguf`, about 17.9 GB | Embedded in the target GGUF |
| `gemma4-31b` | Dense, about 31B active | [`unsloth/gemma-4-31B-it-qat-GGUF`](https://huggingface.co/unsloth/gemma-4-31B-it-qat-GGUF), `gemma-4-31B-it-qat-UD-Q4_K_XL.gguf`, about 17.3 GB | Separate `mtp-gemma-4-31B-it.gguf`, about 280 MB |
| `gemma4-26b-a4b` | MoE, about 25B total and 3.8B active | [`unsloth/gemma-4-26B-A4B-it-qat-GGUF`](https://huggingface.co/unsloth/gemma-4-26B-A4B-it-qat-GGUF), `gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf`, about 14.2 GB | Separate `mtp-gemma-4-26B-A4B-it.gguf`, about 252 MB |

The Gemma profiles use their QAT repositories because the target and small QAT drafter are designed for four-bit operation. The Qwen profiles use MTP-aware GGUF repositories. Unsloth Dynamic (`UD`) is a mixed-tensor GGUF quantization recipe; it still runs in stock llama.cpp and does not require an Unsloth runtime.

The dense 27B and 31B models execute substantially more active parameters per generated token than the A3B/A4B MoE models. It is therefore plausible for Qwen3.6-35B-A3B to decode faster than Qwen3.6-27B even though its file represents more total parameters.

Every Hugging Face profile uses a committed exact repository revision and expected filename set. After download, setup hashes every local file into `asset-manifest.json`; later use rehashes and requires the exact catalog identity and file set. Hugging Face file hashes are therefore local post-download integrity records, not catalog-supplied upstream checksums. The llama.cpp archives are different: their expected SHA-256 values are committed before download.

### Downloading into the Work Directory

Use the application setup command so the committed catalog, destination, cache policy, and local asset manifest stay in agreement:

```powershell
.\.venv\Scripts\highlight-clipper.exe setup --model qwen36-35b-a3b
.\.venv\Scripts\highlight-clipper.exe setup --model qwen36-27b
.\.venv\Scripts\highlight-clipper.exe setup --model gemma4-31b
.\.venv\Scripts\highlight-clipper.exe setup --model gemma4-26b-a4b
```

The exact non-`main` revisions are already committed; setup consumes rather than resolves them. `setup --baseline` installs the integration baseline. `setup --all-evaluators` installs all four LLM profiles and llama.cpp but does not implicitly install ASR/embedding assets.

The four targets and two Gemma drafters total roughly 69 GB before Hugging Face cache overhead, ASR models, recordings, proxies, and exports. Setup performs a conservative free-space check per requested profile (`2 × estimated download + 5 GB`); it does not calculate one aggregate full-bake-off capacity plan. Check the volume before requesting all evaluators.

### Current profile readiness

| Profile | Current evidence |
|---|---|
| Whisper Turbo | Installed and completed a real CUDA transcription |
| Whisper large-v3 | Executable through the same adapter; not promoted or compared |
| Qwen3-Embedding-0.6B | Installed; the prior CPU path completed a real run, while the required CUDA path is implemented but not yet hardware-smoked |
| BGE-M3 | Download-only diagnostic catalog entry |
| Parakeet v3 | Download-only diagnostic catalog entry; no NeMo adapter |
| Qwen3.6-35B-A3B | Installed; no-MTP real pipeline completed; embedded-MTP strict-JSON smoke completed |
| Qwen3.6-27B | Catalog and managed-launch support; not downloaded/screened in this implementation pass |
| Gemma 4 31B | Catalog and separate-drafter launch support; not downloaded/screened |
| Gemma 4 26B-A4B | Catalog and separate-drafter launch support; not downloaded/screened |

## MTP launch differences

Multi-Token Prediction is speculative decoding: a drafter proposes several future tokens and the target model verifies them. It can improve decoding throughput, but it does not make prompt ingestion faster and can regress short responses or low-acceptance workloads. Because evaluator output is intentionally short and prompts dominate part of the workload, MTP is an optimization experiment after a valid no-MTP baseline, never a correctness dependency.

The Qwen 35B MTP smoke on the target machine loaded successfully and produced strict JSON. The cold launch was about 41.5 seconds while the tiny request itself was about 0.28 seconds. That proves compatibility only: it does not compare complete candidate-batch time or acceptance against no-MTP, and it is not evidence to enable MTP by default.

The initial experiment plan proposes a 15% median complete-batch improvement as the materiality threshold and freezes that threshold before measurements begin. A complete batch includes child launch, model/drafter load, health wait, all paired requests, shutdown, owned-process exit, and lease release. Compare at least five paired cold-start repetitions, report median and p90, and define quality equivalence margins before the run. MTP is retained only when the frozen materiality, quality, and stability rules pass.

### Qwen3.6

The Qwen MTP head is inside the target GGUF. Do not pass a separate draft model:

```text
<common arguments>
--spec-type draft-mtp
--spec-draft-n-max 2
```

The first tuning sweep compares MTP off with `--spec-draft-n-max 1` and `2`. Older examples using `--spec-type mtp` or `--draft` are obsolete; current llama.cpp uses `draft-mtp` and `--spec-draft-n-max`.

### Gemma 4

Gemma's target and MTP drafter are separate GGUF files. The application downloads both and supplies the absolute drafter path rather than relying on repository conventions or runtime discovery:

```text
<common arguments>
--spec-type draft-mtp
--spec-draft-model <absolute-mtp-gemma-4-...gguf>
--spec-draft-ngl all
--spec-draft-n-max 4
```

The first sweep compares MTP off and draft maxima `1`, `2`, and `4`. The dense 31B model is more likely to benefit because its target decode is expensive. The 26B-A4B target is already a fast MoE, so drafter overhead may erase the gain; MTP remains available but is not considered successful unless it improves the project's own workload.

## Manual no-MTP baseline

Starting from the common arguments, the first integration command needs only the target model:

```text
--model <repo>\workdir\models\llm\qwen36-35b-a3b\Qwen3.6-35B-A3B-UD-IQ4_NL.gguf
```

No speculative-decoding flags or Gemma drafter are required.

## MTP smoke-test suffixes

Starting from the common arguments, replace the model placeholder and append exactly one of these profile suffixes. `<repo>` means the absolute repository path; the managed child does not resolve paths relative to the caller's shell.

Qwen3.6-35B-A3B:

```text
--model <repo>\workdir\models\llm\qwen36-35b-a3b\Qwen3.6-35B-A3B-UD-IQ4_NL.gguf
--spec-type draft-mtp --spec-draft-n-max 2
```

Qwen3.6-27B:

```text
--model <repo>\workdir\models\llm\qwen36-27b\Qwen3.6-27B-UD-Q4_K_XL.gguf
--spec-type draft-mtp --spec-draft-n-max 2
```

Gemma 4 31B:

```text
--model <repo>\workdir\models\llm\gemma4-31b\gemma-4-31B-it-qat-UD-Q4_K_XL.gguf
--spec-type draft-mtp
--spec-draft-model <repo>\workdir\models\llm\gemma4-31b\mtp-gemma-4-31B-it.gguf
--spec-draft-ngl all --spec-draft-n-max 4
```

Gemma 4 26B-A4B:

```text
--model <repo>\workdir\models\llm\gemma4-26b-a4b\gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf
--spec-type draft-mtp
--spec-draft-model <repo>\workdir\models\llm\gemma4-26b-a4b\mtp-gemma-4-26B-A4B-it.gguf
--spec-draft-ngl all --spec-draft-n-max 4
```

The production launcher converts every path to an absolute path and passes arguments without PowerShell string reconstruction. These snippets are documentation, not the source of truth; the committed catalog and typed Model Profiles are.

## Bake-off and promotion

The experiment order prevents a combinatorial search:

1. Complete fake and one real no-MTP end-to-end workflow.
2. Freeze a Finnish, English, and code-switched Context Envelope set.
3. Screen all four deployable profiles with the same prompts/schema, 32K cap, non-thinking mode, MTP off, controlled hardware headroom, and three-seed set; sampling itself remains an explicit model-appropriate profile field.
4. Retain the best one or two profiles that meet the quality and operational gates.
5. Compare bounded thinking with non-thinking only on those finalists.
6. Test larger context only for measured oversized representative prompts.
7. Tune MTP last and retain it only under the material-gain rule above.

For every deployable profile and experiment setting, record:

- first-pass and after-one-repair schema-valid rates;
- evidence-identity, timestamp, interval, and application-validation failures;
- agreement with Reference Moments and boundary corrections;
- complete wall time, prompt-processing speed, decode speed, and MTP acceptance;
- model load/unload time, effective offload, peak VRAM, process-tree exit, and VRAM delta;
- per-language and per-category behavior, repetition, hallucinated evidence, and truncated output;
- retry, cancellation, crash-recovery, and full-batch completion rates.

The fixed screen contains at least 100 Context Envelopes and includes definite/possible references, routine negative material, semantic rejections, and insufficient-context cases. It contains at least 15 Finnish, 15 English, and 15 code-switched envelopes plus at least 10 relevant examples for each proposal category; slices may overlap. First-pass and after-one-repair validity are reported as explicit numerators/denominators overall and per slice. The hard validator accepts zero unknown evidence/Boundary Anchor IDs or invalid intervals; after at most one repair, a valid profile-specific disposition must reach 99% overall and at least 95% in every sufficiently sampled language/category slice.

Manual fit discovery may use `--n-gpu-layers auto --fit on` only as a diagnostic. Managed Analysis Runs require `--n-gpu-layers all --fit off`; insufficient VRAM fails startup instead of moving model layers to CPU. Before the four-profile screen, each candidate starts from a controlled free-VRAM baseline and records its full-offload fit margin. The same controlled rule applies to finalist comparisons so contemporaneous WDDM use cannot silently confound selection.

Editorial quality and boundaries must meet the Milestone 1 held-out gates. Full-stage elapsed time, source-hours processed per wall hour, model load/unload, peak RAM/pagefile, disk amplification, evaluator seconds per envelope, and review-time budgets are frozen before profile promotion and the sealed evaluation.

The selected default is the fastest complete deployable profile that meets every quality and reliability gate, not the model with the highest benchmark rank or decode tokens per second. Comparisons are between exact Model Profiles, so differences in quantization are explicit rather than mistaken for architecture-only conclusions. Other profiles remain opt-in diagnostics rather than permanent production dependencies.
