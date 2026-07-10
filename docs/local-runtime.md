# Local runtime and model profiles

**Status:** Proposed runbook; exact revisions and benchmark winners are not yet selected.

All private, generated, downloaded, and machine-specific runtime data lives below the repository-local `workdir/`. The entire directory is ignored by Git with one anchored rule. Tracked source code, portable configuration, scripts, tests, and documentation stay outside it.

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
  artifacts/<source-id>/generations/ immutable reusable stage generations
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

Model download workers set `HF_HOME`, `TORCH_HOME`, and `UV_CACHE_DIR` to the corresponding directories above. Model libraries also receive an explicit download directory. This prevents an accidental second model cache under the Windows user profile.

The runtime manifest also records the Windows build, CPU model/core counts, installed and available RAM, pagefile policy/availability, storage volume/device and free space, GPU identity, display-driver version, CUDA/cuDNN/backend DLL names and hashes, FFmpeg/FFprobe versions, Python lock hash, process architecture, and system locale. These are diagnostic provenance rather than reasons to invalidate editorial history automatically.

## ASR profiles and model locations

The baseline adapter pins [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper), CTranslate2, and their Python dependencies in the project lock. It pre-downloads a reviewed repository revision into the Work Directory, then workers receive only the absolute local path and `local_files_only` behavior.

| Profile | Model repository | Role |
|---|---|---|
| `whisper-turbo` | [`mobiuslabsgmbh/faster-whisper-large-v3-turbo`](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo) | Lowest-integration-risk first baseline; this is the repository currently mapped by faster-whisper's `turbo` alias |
| `whisper-large-v3` | [`Systran/faster-whisper-large-v3`](https://huggingface.co/Systran/faster-whisper-large-v3) | Accuracy contender using the same adapter/runtime |
| `parakeet-v3` | [`nvidia/parakeet-tdt-0.6b-v3`](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) | Optional contender only after native Windows/RTX 4090 NeMo smoke-test success |

Each ASR Profile pins the model repository/revision and file hashes, adapter/runtime versions, compute type, batching, VAD, chunk/overlap/stitching, language hints, timestamp policy, and normalization version. Turbo and large-v3 use the same worker contract so their comparison changes the profile rather than the pipeline.

ASR setup places weights under `workdir/models/asr/<profile>/` and caches under `workdir/cache/huggingface/`. The worker starts with network access disabled, runs bounded chunks/checkpoints, exits after transcription, and releases the GPU Lease through process-tree exit. Parakeet does not become a required dependency unless it wins the fixed Windows bake-off.

## Embedding profile

The first multilingual embedding integration uses [`Qwen/Qwen3-Embedding-0.6B`](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) in a disposable CPU worker, with a pinned revision, hashes, tokenizer, query instruction, pooling, normalization, output dimension, dtype, and windowing policy. CPU-first avoids another GPU load/unload phase; its full-recording throughput remains a measured gate rather than an assumption.

The required lexical retrieval baseline runs without this model. If the embedding path does not improve held-out end-to-end recall enough to justify indexing time and storage, it is removed from the promoted Milestone 1 profile. [`BAAI/bge-m3`](https://huggingface.co/BAAI/bge-m3) is an optional challenger only after the first complete baseline, not another permanent adapter.

Embedding setup stores weights under `workdir/models/embeddings/<profile>/`, uses the Work Directory caches, and passes an absolute local path with network access disabled. A future GPU profile uses the same owned-worker and GPU-Lease lifecycle.

## Installing llama.cpp

Do not put an unversioned `llama-server.exe` loose in the repository root. Use a pinned upstream [llama.cpp release](https://github.com/ggml-org/llama.cpp/releases):

1. Choose a pinned build that loads the no-MTP Qwen3.6-35B-A3B baseline and exposes the documented common server flags. MTP support is not required for the first vertical slice.
2. Download the Windows x64 CUDA 12 binary archive and its matching CUDA runtime archive from the same release.
3. Extract both into `workdir/runtime/llama.cpp/<release-tag>/`, keeping the EXE and its matching backend/runtime DLLs together.
4. Record the release tag, source commit, asset names, hashes, and output of `llama-server.exe --version` in the local runtime manifest.
5. Install a newer build into a new versioned directory. Never overwrite DLLs belonging to a running build or mix files from different releases.

Setup verifies the server version, required common flags, CUDA loading, structured-output support, and clean process-tree shutdown before accepting the baseline Runtime Bundle. A later MTP experiment bundle must pin a commit containing both the [Qwen MTP change](https://github.com/ggml-org/llama.cpp/pull/22673) and the [Gemma 4 MTP change](https://github.com/ggml-org/llama.cpp/pull/23398), then smoke-test Qwen's embedded and Gemma's separate-drafter paths. Date alone is not proof of ancestry or compatibility.

A source build remains an escape hatch when a required fix has not reached a release. For an RTX 4090, it must enable CUDA and target compute capability 8.9 as described by the upstream [Windows/CUDA build documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md). A pinned prebuilt CUDA release is the recommended Milestone 1 path because it is simpler to reproduce.

## How the application runs llama.cpp

Milestone 1 uses a directly managed `llama-server` child process, not an always-running server and not router mode:

1. Select one committed Model Profile and its promoted context cap.
2. Acquire the application-wide GPU Lease through an atomic Windows named mutex and persist its owner identity for diagnostics.
3. Launch the pinned `llama-server.exe` in a kill-on-close Windows Job Object using an argument array, with its runtime directory as the child working directory.
4. Wait for the loopback health endpoint and verify the loaded model identity.
5. Evaluate the compatible pending Context Envelopes for that Analysis Run through the OpenAI-compatible API, using a JSON schema on every request.
6. Stop the server, close the Job Object on timeout or cancellation, wait for the owned process tree to exit, and record whether GPU memory returned near its measured baseline.
7. Release the GPU lease before ASR, embeddings, or another evaluator can start.

The named mutex prevents two local application instances from owning the GPU concurrently. The owned process tree exiting is the lease-release invariant. Total WDDM GPU use is a noisy secondary diagnostic because unrelated desktop applications can allocate memory: an unexpected delta produces a warning and a fresh headroom check before the next model, but blocks only when an owned process remains or the next allocation actually cannot fit.

The launcher allocates an available loopback port rather than assuming port 8080 is free, retries a bounded bind race, and never binds the evaluator to `0.0.0.0`. It uses a random API key when supported by the pinned build, drains bounded stdout/stderr, and verifies both health and loaded model identity before sending private transcript content.

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
--gpu-layers auto
--fit on
--flash-attn on
--cache-type-k q8_0
--cache-type-v q8_0
--no-mmproj
--offline
```

These names follow the current [llama-server argument reference](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md). The application checks `--help` during setup so a changed or missing flag fails before processing a long recording.

Sampling, reasoning, seed policy, and the response schema belong to the request or Model Profile; they are not silently inherited from server defaults. The user's demonstrated Qwen profile is recorded explicitly as temperature 0.6, top-p 0.95, top-k 20, and min-p 0.00. Each architecture may use a pinned publisher-appropriate sampling profile, but every elimination-screen candidate runs the same three fixed seed values and finalists receive additional repeated runs. `preserve_thinking` is useful for carrying reasoning through a multi-turn conversation, but evaluator requests are independent, so reasoning is controlled explicitly with a finite budget.

The server output cap is derived from the selected profile and is the ceiling for reasoning plus the final structured answer. The initial comparison uses:

- non-thinking: reasoning disabled and a 2,048-token total output cap;
- bounded thinking: a 4,096-token reasoning budget and a 6,144-token total output cap, leaving at least a 2,048-token final-answer reserve if the full reasoning budget is used.

The reserve is not an independent final-JSON cap: if reasoning ends early, the final answer can consume more of the 6,144 total. Actual reasoning/final token use and truncation are persisted. These are starting experiment profiles, not guarantees; the smaller valid budget wins when quality is unchanged.

### Context policy

`--ctx-size` is the server slot's combined prompt-and-generation capacity. With `--parallel 1`, one request can use the configured tier. `--n-predict` limits newly generated tokens; it is not the input context size.

The integration profile starts at 32,768 tokens. After server health succeeds and before generation, the application calls the pinned server's `/apply-template` and `/tokenize` endpoints to count the exact rendered request, then reserves the profile's total output cap. Smoke tests compare this count with the server's reported prompt usage. Any mismatch fails the attempt rather than risking silent context shift or truncation.

Compatible envelopes are processed as one batch. An envelope that does not fit is explicitly re-enveloped or recorded as input-too-large; it is never silently truncated. The application does not relaunch the server at a new tier for individual candidates. Observed prompt sizes may justify promoting a smaller cap. Contexts above 32K are separate diagnostics at 64K, 128K, or 256K and are promoted only if representative prompts require them and the complete profile remains within the agreed VRAM and latency budgets. Prompts are grouped by tier for any such experiment.

The effective tier, rendered-prompt hash, prompt tokens, reasoning tokens, final-answer tokens, KV types, GPU offload, and wall time are persisted with each evaluator attempt.

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

Every download is pinned by Hugging Face repository revision and verified by SHA-256 in the runtime manifest. A changed file at the same friendly name is treated as a different Model Profile.

### Downloading into the Work Directory

The setup command will create the directories and set cache environment variables before invoking Hugging Face. The equivalent PowerShell shape is:

```powershell
$env:HF_HOME = (Resolve-Path '.\workdir\cache\huggingface').Path
$env:TORCH_HOME = (Resolve-Path '.\workdir\cache\torch').Path

hf download unsloth/Qwen3.6-35B-A3B-MTP-GGUF `
  Qwen3.6-35B-A3B-UD-IQ4_NL.gguf `
  --revision <pinned-commit> `
  --local-dir .\workdir\models\llm\qwen36-35b-a3b

hf download unsloth/Qwen3.6-27B-MTP-GGUF `
  Qwen3.6-27B-UD-Q4_K_XL.gguf `
  --revision <pinned-commit> `
  --local-dir .\workdir\models\llm\qwen36-27b

hf download unsloth/gemma-4-31B-it-qat-GGUF `
  gemma-4-31B-it-qat-UD-Q4_K_XL.gguf `
  mtp-gemma-4-31B-it.gguf `
  --revision <pinned-commit> `
  --local-dir .\workdir\models\llm\gemma4-31b

hf download unsloth/gemma-4-26B-A4B-it-qat-GGUF `
  gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf `
  mtp-gemma-4-26B-A4B-it.gguf `
  --revision <pinned-commit> `
  --local-dir .\workdir\models\llm\gemma4-26b-a4b
```

`<pinned-commit>` is deliberately not `main`: setup resolves and records a reviewed repository commit before downloading. Normal setup downloads only the selected integration baseline. The full four-profile bake-off is an explicit optional setup action.

The four targets and two Gemma drafters total roughly 69 GB, before Hugging Face cache overhead, ASR models, recordings, proxies, and exports. Full-bake-off setup calculates source/model/cache/temp/export headroom and refuses to start without the configured free-space reserve; it does not require every user to keep four production models.

## MTP launch differences

Multi-Token Prediction is speculative decoding: a drafter proposes several future tokens and the target model verifies them. It can improve decoding throughput, but it does not make prompt ingestion faster and can regress short responses or low-acceptance workloads. Because evaluator output is intentionally short and prompts dominate part of the workload, MTP is an optimization experiment after a valid no-MTP baseline, never a correctness dependency.

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

The production launcher converts every path to an absolute path and passes arguments without PowerShell string reconstruction. These snippets are documentation, not the source of truth; committed typed Model Profiles will be.

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

Initial fit discovery may use `--gpu-layers auto --fit on`, but it does not eliminate a model. Before the four-profile screen, each candidate starts from a controlled free-VRAM baseline, records the fit margin, and freezes the successful effective GPU offload or explicit equivalent settings. The same controlled rule applies to finalist comparisons so contemporaneous WDDM use cannot silently confound selection.

Editorial quality and boundaries must meet the Milestone 1 held-out gates. Full-stage elapsed time, source-hours processed per wall hour, model load/unload, peak RAM/pagefile, disk amplification, evaluator seconds per envelope, and review-time budgets are frozen before profile promotion and the sealed evaluation.

The selected default is the fastest complete deployable profile that meets every quality and reliability gate, not the model with the highest benchmark rank or decode tokens per second. Comparisons are between exact Model Profiles, so differences in quantization are explicit rather than mistaken for architecture-only conclusions. Other profiles remain opt-in diagnostics rather than permanent production dependencies.
