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
  sources/<source-id>/            immutable original recordings
  state/highlight-clipper.sqlite3  SQLite database and WAL/SHM sidecars
  artifacts/<recording-id>/       proxy, audio, transcript, observations, proposals
  exports/<recording-id>/         accepted clips rendered from the original source
  cache/
    huggingface/
    torch/
    uv/
  logs/
  tmp/                            incomplete, disposable stage output
```

The application resolves every relative path from the repository root, not from the caller's current directory. It gives recordings and jobs short generated identifiers rather than reproducing long video titles in paths.

`sources/`, `state/`, editorial labels within `artifacts/`, and `exports/` are valuable user data even though Git ignores them. `cache/`, `logs/`, and `tmp/` are disposable. In particular, `git clean -xfd` can delete the entire ignored work directory and must not be used as a cleanup command.

Workers write temporary output beside its destination with a `.partial` suffix and atomically rename it only after validation. Originals under `sources/` are never modified. SQLite backups use SQLite's backup mechanism rather than copying only the main file while WAL mode is active.

Model download workers set `HF_HOME`, `TORCH_HOME`, and `UV_CACHE_DIR` to the corresponding directories above. Model libraries also receive an explicit download directory. This prevents an accidental second model cache under the Windows user profile.

## Installing llama.cpp

Do not put an unversioned `llama-server.exe` loose in the repository root. Use a pinned upstream [llama.cpp release](https://github.com/ggml-org/llama.cpp/releases):

1. Choose a pinned build whose commit contains both the [Qwen MTP change](https://github.com/ggml-org/llama.cpp/pull/22673) and the [Gemma 4 MTP change](https://github.com/ggml-org/llama.cpp/pull/23398). Date alone is not treated as proof of compatibility.
2. Download the Windows x64 CUDA 12 binary archive and its matching CUDA runtime archive from the same release.
3. Extract both into `workdir/runtime/llama.cpp/<release-tag>/`, keeping the EXE and its matching backend/runtime DLLs together.
4. Record the release tag, source commit, asset names, hashes, and output of `llama-server.exe --version` in the local runtime manifest.
5. Install a newer build into a new versioned directory. Never overwrite DLLs belonging to a running build or mix files from different releases.

Setup smoke-tests both Qwen's embedded MTP path and Gemma's separate-drafter path before accepting the Runtime Bundle.

A source build remains an escape hatch when a required fix has not reached a release. For an RTX 4090, it must enable CUDA and target compute capability 8.9 as described by the upstream [Windows/CUDA build documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md). A pinned prebuilt CUDA release is the recommended Milestone 1 path because it is simpler to reproduce.

## How the application runs llama.cpp

Milestone 1 uses a directly managed `llama-server` child process, not an always-running server and not router mode:

1. Select one committed Model Profile and one context tier.
2. Acquire the application-wide GPU lease.
3. Launch the pinned `llama-server.exe` in a Windows Job Object using an argument array, with its runtime directory as the child working directory.
4. Wait for the loopback health endpoint and verify the loaded model identity.
5. Evaluate all pending Context Envelopes for that analysis job through the OpenAI-compatible API, using a JSON schema on every request.
6. Stop the server, close the Job Object on timeout or cancellation, wait for the owned process tree to exit, and verify that GPU memory returned near its recorded baseline.
7. Release the GPU lease before ASR, embeddings, or another evaluator can start.

The launcher allocates an available loopback port rather than assuming port 8080 is free. It never binds the evaluator to `0.0.0.0`.

The common server arguments are:

```text
--host 127.0.0.1
--port <allocated-port>
--no-ui
--model <absolute-target-gguf>
--ctx-size <32768|65536|131072|262144>
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

Sampling, reasoning, and the response schema belong to the request or Model Profile; they are not silently inherited from arbitrary server defaults. The user's working Qwen sampling values are retained as a bake-off profile. `preserve_thinking` is useful for carrying reasoning through a multi-turn conversation, but the evaluator uses independent requests, so thinking itself is controlled explicitly with llama.cpp's reasoning options and a finite budget.

The server output cap is derived from the selected profile and is the ceiling for reasoning plus the final structured answer. The initial candidate is 8,192 tokens, but it is persisted and validated as part of the same budget used by context preflight rather than hard-coded independently.

### Adaptive context

`--ctx-size` is the server slot's combined prompt-and-generation capacity. With `--parallel 1`, one request can use the configured tier. `--n-predict` limits newly generated tokens; it is not the input context size.

The default tier is 32,768 tokens. Before evaluation, the application counts the fully rendered prompt and reserves the profile's maximum reasoning and final-answer budget. If the total does not fit, it restarts the server at 65,536, 131,072, or 262,144 tokens. It never silently truncates a transcript. The selected tier and actual prompt/output token counts are persisted with the result.

## Evaluator candidates

The first bake-off uses four text-only profiles. Vision projectors are deliberately not downloaded for Milestone 1.

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

`<pinned-commit>` is deliberately not `main`: setup resolves and records a reviewed repository commit before downloading. The four targets and two Gemma drafters total roughly 69 GB, before Hugging Face cache overhead, ASR models, recordings, proxies, and exports. Setup must preflight substantially more than 69 GB of free space before offering the full four-model bake-off.

## MTP launch differences

Multi-Token Prediction is speculative decoding: a drafter proposes several future tokens and the target model verifies them. It can improve decoding throughput, but it does not make prompt ingestion faster and can regress short responses or low-acceptance workloads. The project therefore measures complete candidate-batch wall time and output validity with MTP disabled and enabled.

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

## Manual smoke-test suffixes

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

Each model is tested on the same Finnish, English, and code-switched Context Envelopes. For every model, quant, context tier, reasoning mode, and MTP setting, record:

- schema-valid proposal rate and application-validation failures;
- agreement with Reference Highlights and boundary corrections;
- complete wall time, prompt-processing speed, decode speed, and MTP acceptance;
- model load/unload time and peak VRAM;
- unsupported-language behavior, repetition, hallucinated evidence, and truncated output.

The selected default is the fastest profile that meets the quality gate, not simply the profile with the highest decode tokens per second. Other profiles remain opt-in diagnostics rather than four permanent production dependencies.
