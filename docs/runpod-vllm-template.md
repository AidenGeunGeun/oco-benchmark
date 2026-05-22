# Runpod vLLM Latest Template — Operations Reference

This benchmark runs on Runpod's **vLLM Latest** template (Runpod template ID `pvcdqlwm9r`), which wraps the official `vllm/vllm-openai:latest` image. This document records the template's behavior, the Start CMD conventions, the storage layout, and the operational lessons we've learned the hard way across prior runs.

Authoritative source for template defaults: the README shown in the Runpod Console for this template. This file is the project's reference for *how we use it*.

---

## Template defaults

- **Image:** `vllm/vllm-openai:latest`
- **Default model:** `Qwen/Qwen3-8B` (we override via Start CMD)
- **Container entrypoint behavior:** the Start CMD you supply is passed as arguments to `vllm serve`. The leading `vllm serve` is implicit — do NOT include `vllm`, `serve`, or `bash -lc` in the Start CMD.
- **Auto-generated API key:** `sk-[pod-id]` — set by Runpod injecting `VLLM_API_KEY=sk-$RUNPOD_POD_ID` as an environment variable at container start, where `$RUNPOD_POD_ID` is substituted with the actual pod ID. The substitution is done by Runpod's env-var injection layer, not by vLLM.
- **Exposed proxy URL:** `https://[pod-id]-8000.proxy.runpod.net/v1`
- **Underlying requirement:** the image relies on NVIDIA CUDA Forward Compatibility, available only on Data Center GPUs (H100/H200/A100/L40/B200, etc.). Consumer RTX cards may have issues.

## Endpoints

- `/health` — no auth required, returns 200 once vLLM finishes startup
- `/v1/models` — auth required, lists loaded models
- `/v1/chat/completions` — auth required, OpenAI-compatible chat
- `/v1/completions` — auth required, OpenAI-compatible text completion
- `/v1/embeddings` — auth required, for embedding models
- `/v1/responses` — auth required, OpenAI Responses API
- `/` returns 404 — **this is normal**, not a problem

## Storage layout (critical)

The template gives you two separate disks. They behave very differently across pod stop/start cycles.

| Path | Backing | Survives stop/start? | Use for |
|---|---|---|---|
| `/workspace/` | Pod Volume (sized in pod config) | **Yes** | HF model cache, bench scripts, run artifacts, anything you want to keep |
| `/usr/`, `/etc/`, container root | Docker image layer | **No** — recreated from image | Nothing benchmark-related |
| `/tmp/` | Container disk | No | Truly ephemeral data |

**Implication 1:** Set `HF_HOME=/workspace/huggingface` so model weights cache to Pod Volume. After the first download, every restart loads from cache (~1-2 min) instead of cold (~5-15 min).

**Implication 2:** `apt-get install` lands in `/usr/bin/` on the image layer. Re-run after every stop/start. Tools we routinely need: `strace` (boundary proof), and anything diagnostic.

**Implication 3:** Pod Volume disappears only if you **delete** the pod, not on stop/start. Network Volumes (a separate Runpod feature, only available on Secure Cloud) are not used here — Community Cloud doesn't support them.

## Sizing the Pod Volume

Runpod template README guideline: **HF model total size rounded up to the nearest 5GB**.

| Model | HF total size | Recommended Pod Volume |
|---|---|---|
| Qwen3.6-27B-FP8 | ~50 GB | ≥55 GB; we use 200 GB for headroom |
| Qwen3.6-27B (BF16) | ~55 GB | ≥60 GB |
| Qwen3.6-27B-NVFP4 | ~16 GB | ≥20 GB |

200 GB gives plenty of room for the model cache, multiple bench result JSONs, OCO worktrees during benchmark runs, and a swap profile or two.

## Start CMD conventions

**Format:** plain CLI args, one line. Implicitly prefixed by `vllm serve`.

**Positional model is preferred** (per vLLM's own warning that `--model` will be removed in a future release):

```
Qwen/Qwen3.6-27B-FP8 --served-model-name selfhost-qwen --host 0.0.0.0 --port 8000 ...
```

The `--model Qwen/...` form also works on current versions but emits a deprecation warning.

**Do NOT include in Start CMD:**

- `vllm` or `serve` — template prepends them.
- `bash`, `bash -lc`, or any shell wrapper — Start CMD is execve'd directly, not shell-parsed.
- Environment variable references like `$RUNPOD_POD_ID` or `${VLLM_API_KEY}` — they will NOT be expanded; treated as literal strings. (See "Gotcha 1" below.)
- `--api-key sk-<pod-id>` placeholder text — verbatim paste means vLLM authenticates against the literal string `sk-<pod-id>`. (See "Gotcha 2".)

## Gotchas

### 1. Environment variable substitution is NOT applied to Start CMD

`$VAR` references in Start CMD are treated literally because `execve` doesn't shell-expand. Substitution only happens for env vars themselves (Runpod's injection layer) and for tools that explicitly do their own substitution.

**Symptom:** vLLM logs all requests returning 401 Unauthorized even though `VLLM_API_KEY` is correctly set.

**Fix:** either hardcode the literal value in `--api-key`, OR — cleaner — omit `--api-key` entirely from Start CMD. vLLM falls back to the `VLLM_API_KEY` env var, which IS expanded by Runpod's injection.

### 2. Placeholder text in Start CMD authenticates against the literal placeholder

If a doc or chat suggests `--api-key sk-<pod-id>` and you paste it verbatim, vLLM accepts requests only with `Authorization: Bearer sk-<pod-id>` (angle brackets included). Always verify with a curl against `/v1/models` immediately after boot.

### 3. `/` returns 404 — that's not a server problem

vLLM doesn't mount a route at `/`. Verify liveness with `/health` (200, no auth) and `/v1/models` (200 with valid auth).

### 4. Stopping the pod resets apt installs

Anything installed via `apt-get install` lives on the container root filesystem and is wiped on stop/start. Keep an install script handy in `/workspace/setup-tools.sh` and run it after each restart.

### 5. Restart cost is mostly model load, not container creation

Cold first boot: 5-15 min (HF download + load + warmup). Restart with cached weights on `/workspace`: 1-2 min. Adding MTP adds another 5-13 min for drafter load + CUDA graph capture + DeepGEMM warmup — this is *not* a hang, it's normal.

### 6. The Runpod proxy is a Cloudflare-fronted HTTP layer

External calls go through Cloudflare. Two consequences:

- **Bot detection:** Python's default `urllib` User-Agent gets 403 with `error code: 1010`. Send `User-Agent: curl/8.7.1` and `Accept: */*` headers from external clients. Direct `curl` works because the default UA passes.
- **524 origin response timeout:** Cloudflare returns 524 after ~120 seconds if vLLM hasn't sent any bytes. Affects long single-stream responses or any non-streaming call that takes >120s. Mitigation: run heavy benchmark traffic from *inside* the pod against `http://localhost:8000/v1` — eliminates the proxy from the hot path.

### 7. Bot-detection symptom looks like `403 error code: 1010`

This is Cloudflare, not Runpod or vLLM. Fix is client-side User-Agent.

## MTP (speculative decoding) recipe

The vLLM speculative-decoding config for Qwen3.6-27B is added via a single flag at the end of Start CMD:

```
--speculative-config '{"method":"qwen3_next_mtp","num_speculative_tokens":N}'
```

Notes:

- Method `qwen3_next_mtp` has been renamed to `mtp` in newer vLLM versions. The old name still works with a deprecation warning. Either is fine.
- Qwen's official model card recipe uses `num_speculative_tokens: 2`. We've measured `2` and `4` historically; `4` was faster on H200 FP8 direct API, `2` was the safer default on B200.
- MTP requires `--enable-chunked-prefill` to remain on (Qwen3.6/Mamba cache mode `align` requires it). Our baseline Start CMD already has this.
- First bench after MTP boot is JIT-warmup-poisoned; always run twice and use the second result.

## Restart workflow

Standard procedure for swapping serving profiles or recovering from a bad Start CMD:

1. Edit Start CMD in Runpod Console → Edit Pod.
2. **Stop** pod (not Delete — that destroys the Pod Volume).
3. **Start** pod.
4. Wait for `Application startup complete` in logs.
5. Verify with `curl http://localhost:8000/v1/models` inside the pod, then external proxy from your dev machine if needed.
6. Reinstall any apt tools you depend on (`apt-get update && apt-get install -y strace`).
7. Re-run any in-pod scripts you need (they live on `/workspace`, no re-deployment needed).

## What's been packaged-by-Runpod, what's vLLM

The README is explicit: this template is packaged by Runpod; they do not control the underlying vLLM image's content or behavior. When something behaves unexpectedly:

- **Container lifecycle, env injection, Start CMD parsing, proxy URL** → Runpod template behavior.
- **CLI flags, attention backends, MTP, parser names, OpenAI API shape** → upstream vLLM behavior. Refer to the vLLM docs.

## References

- vLLM OpenAI-compatible server: https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html
- vLLM Qwen3.6 recipe: https://recipes.vllm.ai/Qwen/Qwen3.6-27B
- Runpod template README: in-console on the pod template page
