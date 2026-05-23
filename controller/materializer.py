"""Materialize the isolated benchmark OCO config snapshot.

The materializer reads the user's production OCO config and produces a
self-contained snapshot directory that OCO can load directly. The snapshot
contains exactly two top-level files plus a prompts subdirectory:

  - opencode.jsonc        the OCO-readable config (must validate against
                          OCO's config schema)
  - strip-diff-manifest.json
                          reviewer-facing record of what was stripped and
                          what benchmark-specific overrides were applied;
                          NOT loaded by OCO
  - prompts/<agent>.txt   kept-agent prompt files (referenced by
                          {file:prompts/<agent>.txt} in opencode.jsonc)

Benchmark-internal annotations (sampling policy, OCO version, self-host
flags) live in the manifest, not in opencode.jsonc, because OCO's config
validator rejects unknown root keys.

No redaction is performed. The snapshot stays on the operator's machine and
on the inference pod; both already have the same secrets. Publication-time
sanitization, if ever needed, is a separate concern handled at writeup time
on the rsync'd snapshot.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from controller.atomic import atomic_write_json, atomic_write_text
from controller.constants import QWEN_OUTPUT_TOKEN_LIMIT
from controller.seed import SEED_MODULUS


# Debug A/B knob: when set to a truthy value, the materializer emits a
# snapshot with provider.selfhost.options.experimentalNonStreamingToolCalls
# = false, forcing OCO to use streaming for tool-call generation. Used to
# isolate OCO subagent-persistence behavior between streaming and
# non-streaming paths during smoke testing. Not intended for paid runs.
FORCE_STREAMING_ENV_VAR = "OCO_BENCHMARK_FORCE_STREAMING_TOOL_CALLS"


def _force_streaming_tool_calls() -> bool:
    raw = os.environ.get(FORCE_STREAMING_ENV_VAR, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


KEEP_SUBAGENTS = {"orchestrator", "investigator", "auditor"}
STRIP_SUBAGENTS = {"general", "explore", "test_runner", "web-search", "docs"}
PRIMARY_AGENT_CANDIDATES = ("pm", "build", "plan")
COMPACTION_AGENT = "compaction"
STRIP_TOOLS = {
    "webfetch",
    "compress",
    "compress_map",
    "playwright",
    "browser",
    "mcp",
}
CONFIG_FILENAMES = ("opencode.jsonc", "opencode.json", "oco.jsonc", "oco.json")
FILE_REFERENCE_PATTERN = re.compile(r"^\{file:([^{}]+)\}$")
PROMPT_FILE_SUFFIXES = {".md", ".txt"}
QWEN_PROMPT_OVERLAY_DIR = Path(__file__).resolve().parents[1] / "prompts.qwen"
QWEN_PROMPT_OVERLAY_TARGETS = {
    "prompts/pm.txt": "pm.txt",
    "prompts/orchestrator.txt": "orchestrator.txt",
}


class MaterializerError(RuntimeError):
    """Raised when production config cannot be safely materialized."""


@dataclass(frozen=True)
class MaterializerOptions:
    production_config_dir: Path
    output_dir: Path
    oco_version: str | None = None
    model_name: str = "selfhost-qwen"
    endpoint_url: str | None = None
    api_key: str | None = None
    context_window: int = 200000
    output_token_limit: int = QWEN_OUTPUT_TOKEN_LIMIT
    sampling_seed: int | None = None
    primary_agent: str | None = None


@dataclass(frozen=True)
class MaterializerResult:
    snapshot_dir: Path
    config_path: Path
    manifest_path: Path
    primary_agent: str
    kept_agents: tuple[str, ...]
    removed_agents: tuple[str, ...]


@dataclass(frozen=True)
class _PromptReference:
    agent_name: str
    source: Path
    relative: Path


def load_jsonc(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        cleaned = _strip_jsonc(raw)
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise MaterializerError(f"invalid JSON/JSONC in {path.name}: {exc}") from exc
    except OSError as exc:
        raise MaterializerError(f"could not read {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MaterializerError(f"{path.name} must contain a JSON object")
    return parsed


def _strip_jsonc(text: str) -> str:
    result: list[str] = []
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == "/" and nxt == "/":
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and nxt == "*":
            index += 2
            while index + 1 < len(text) and not (
                text[index] == "*" and text[index + 1] == "/"
            ):
                index += 1
            index += 2
            continue
        result.append(char)
        index += 1
    without_comments = "".join(result)
    return re.sub(r",\s*([}\]])", r"\1", without_comments)


def _find_config_file(config_dir: Path) -> Path:
    for name in CONFIG_FILENAMES:
        candidate = config_dir / name
        if candidate.exists():
            return candidate
    raise MaterializerError(
        "no OCO config file found; expected one of " + ", ".join(CONFIG_FILENAMES)
    )


def _agent_map(config: dict[str, Any]) -> dict[str, Any]:
    agents = config.get("agent")
    if not isinstance(agents, dict):
        raise MaterializerError(
            "production config is missing required agent definitions"
        )
    return agents


def _choose_primary_agent(agents: dict[str, Any], requested: str | None) -> str:
    if requested:
        if requested not in agents:
            raise MaterializerError(f"requested primary agent {requested!r} is missing")
        return requested
    for name in PRIMARY_AGENT_CANDIDATES:
        if name in agents:
            return name
    raise MaterializerError(
        "production config is missing a primary agent; expected pm, build, or plan"
    )


def _validate_required_agents(agents: dict[str, Any], primary: str) -> None:
    required = set(KEEP_SUBAGENTS) | {COMPACTION_AGENT, primary}
    missing = sorted(name for name in required if name not in agents)
    if missing:
        raise MaterializerError(
            "production config is missing required agents: " + ", ".join(missing)
        )


def _is_stripped_tool(name: str) -> bool:
    lower = name.lower()
    return (
        lower in STRIP_TOOLS
        or lower.startswith("mcp")
        or lower.startswith("playwright")
    )


def _sanitize_agent(agent: Any) -> tuple[Any, list[str]]:
    """Return (sanitized_agent, removed_tool_keys).

    OCO production agents do not declare a top-level `tools` field; OCO
    derives tool availability from permissions and provider defaults. If a
    production fixture happens to declare `tools`, we drop it because OCO's
    schema expects a record shape and benchmark-fabricated arrays were the
    cause of the first integration-test failure. Permission entries for
    stripped tools (webfetch/compress/etc.) are filtered out and recorded
    in the returned list.
    """
    if not isinstance(agent, dict):
        return agent, []
    sanitized = json.loads(json.dumps(agent))
    removed: list[str] = []
    sanitized.pop("tools", None)
    for key in ("permission", "permissions"):
        permissions = sanitized.get(key)
        if isinstance(permissions, dict):
            sanitized[key] = _sanitize_permissions(permissions, removed)
    return sanitized, sorted(set(removed))


def _sanitize_permissions(value: dict[str, Any], removed: list[str]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, child in value.items():
        if _is_stripped_tool(str(key)):
            removed.append(str(key))
            continue
        if isinstance(child, dict):
            cleaned[key] = _sanitize_permissions(child, removed)
        elif isinstance(child, list):
            kept_items = []
            for item in child:
                item_name = str(item)
                if _is_stripped_tool(item_name):
                    removed.append(item_name)
                else:
                    kept_items.append(item)
            cleaned[key] = kept_items
        else:
            cleaned[key] = child
    return cleaned


def _prompt_references(
    agents: dict[str, Any], production_dir: Path, *, require_existing: bool
) -> list[_PromptReference]:
    references: list[_PromptReference] = []
    for agent_name, agent in agents.items():
        if not isinstance(agent, dict):
            continue
        reference = _prompt_reference(
            agent_name,
            agent.get("prompt"),
            production_dir,
            require_existing=require_existing,
        )
        if reference is not None:
            references.append(reference)
    unique = {(item.agent_name, item.relative.as_posix()): item for item in references}
    return sorted(unique.values(), key=lambda item: item.relative.as_posix())


def _prompt_reference(
    agent_name: str,
    value: Any,
    production_dir: Path,
    *,
    require_existing: bool,
) -> _PromptReference | None:
    if not isinstance(value, str) or "\n" in value:
        return None
    prompt = value.strip()
    file_match = FILE_REFERENCE_PATTERN.fullmatch(prompt)
    is_file_reference = file_match is not None
    reference_text = file_match.group(1).strip() if file_match else prompt
    if not reference_text:
        if is_file_reference:
            raise MaterializerError(
                f"agent {agent_name!r} has an empty prompt file reference"
            )
        return None
    reference_path = Path(reference_text)
    path_like = _looks_like_prompt_path(reference_path)
    if not is_file_reference and not path_like:
        source = production_dir / reference_path
        if not source.exists():
            return None
    relative = _validate_snapshot_prompt_path(
        agent_name, reference_text, reference_path
    )
    source = production_dir / relative
    exists = source.exists()
    if exists and not source.is_file():
        raise MaterializerError(
            f"agent {agent_name!r} prompt file reference {reference_text!r} is not a file"
        )
    if require_existing and not exists:
        raise MaterializerError(
            f"agent {agent_name!r} prompt file reference {reference_text!r} does not exist at {source}"
        )
    return _PromptReference(agent_name=agent_name, source=source, relative=relative)


def _looks_like_prompt_path(path: Path) -> bool:
    return (
        path.is_absolute()
        or (len(path.parts) > 0 and path.parts[0] == "prompts")
        or path.suffix.lower() in PROMPT_FILE_SUFFIXES
    )


def _validate_snapshot_prompt_path(
    agent_name: str, reference_text: str, reference_path: Path
) -> Path:
    if reference_path.is_absolute() or any(
        part == ".." for part in reference_path.parts
    ):
        raise MaterializerError(
            f"agent {agent_name!r} prompt file reference {reference_text!r} must be relative to the production config directory"
        )
    if not reference_path.parts:
        raise MaterializerError(
            f"agent {agent_name!r} has an empty prompt file reference"
        )
    return reference_path


def _is_qwen_model(model_name: str) -> bool:
    return "qwen" in model_name.lower()


def _qwen_prompt_overlay(reference: _PromptReference) -> Path | None:
    overlay_name = QWEN_PROMPT_OVERLAY_TARGETS.get(reference.relative.as_posix())
    if overlay_name is None:
        return None
    return QWEN_PROMPT_OVERLAY_DIR / overlay_name


def _copy_prompt_files(
    references: list[_PromptReference], snapshot_dir: Path, *, use_qwen_overlay: bool
) -> tuple[list[str], list[dict[str, str]]]:
    copied: list[str] = []
    overlays: list[dict[str, str]] = []
    for reference in references:
        source = reference.source
        if use_qwen_overlay:
            overlay_source = _qwen_prompt_overlay(reference)
            if overlay_source is not None:
                if not overlay_source.is_file():
                    raise MaterializerError(
                        f"Qwen prompt overlay for {reference.relative.as_posix()} is missing at {overlay_source}"
                    )
                source = overlay_source
                overlays.append(
                    {
                        "agent": reference.agent_name,
                        "target": reference.relative.as_posix(),
                        "source": str(overlay_source),
                    }
                )
        target = snapshot_dir / reference.relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(reference.relative.as_posix())
    return copied, overlays


SELFHOST_PROVIDER_ID = "selfhost"
STRIPPED_TOOL_DENY_KEYS = (
    "webfetch",
    "compress",
    "compress_map",
    "browser",
    "mcp",
)

# Headless-safe permission overrides FORCED on every kept agent.
#
# OCO's permission system has exactly three actions: "allow", "deny", "ask".
# In `oco run --format json` the "ask" action publishes `permission.asked`
# and waits indefinitely because there is no interactive responder; this
# manifested in smoke testing as a 900s subprocess timeout the first time a
# subagent's hallucinated path triggered the built-in external_directory
# ask rule. None of OCO's permission paths have a built-in timeout, so the
# only safe shape for a headless benchmark is to ensure no rule resolves to
# "ask" for tools the agent will exercise.
#
# Every override here is forced (overwrites any production value) because
# the benchmark runs in a sandboxed worktree on a throwaway pod where
# OpSec does not apply — only deterministic non-hang behavior matters.
# Production design choices like auditor.permission.edit = "deny" or
# investigator.permission.bash = "deny" exist to scope interactive
# behavior; they are not safety constraints. Inside an isolated benchmark
# attempt we trade production fidelity for aggressive determinism.
#
# Mapping (rules use the named permission keys at
# packages/opencode/src/config/config.ts:704):
#
# - read: allow            built-in default sets .env reads to "ask"; we
#                          override because real task repos may carry .env
# - edit: allow            covers both the write and edit tools (write
#                          delegates to permission key "edit" at
#                          packages/opencode/src/tool/write.ts:35)
# - bash: allow            free shell inside the worktree
# - external_directory:    fires BEFORE the write/edit/bash check whenever
#   deny                   a resolved path lives outside the OCO instance
#                          directory; deny converts a hallucinated outside
#                          path into a fast tool-call error the model can
#                          recover from instead of a hang
# - doom_loop: deny        OCO's loop-protection ask path
# - question: deny         user-question tool would otherwise hang on
#                          input
HEADLESS_SAFE_PERMISSION_OVERRIDES: dict[str, str] = {
    "read": "allow",
    "edit": "allow",
    "bash": "allow",
    "external_directory": "deny",
    "doom_loop": "deny",
    "question": "deny",
}


def _build_request_options(seed: int | None) -> dict[str, Any]:
    """Sampling fields written under provider.selfhost.models.<m>.options.

    OCO merges model options into request options at session/llm.ts:130 and
    forwards them as providerOptions.<provider_id> to the OpenAI-compatible
    SDK. This block intentionally excludes:

    - temperature and top_p, which OCO reads only from agent.<name> for its
      top-level AI SDK request settings (session/llm.ts:151 and :154);
    - max_tokens, because OCO computes maxOutputTokens from
      model.limit.output capped by OUTPUT_TOKEN_MAX (transform.ts:904), not
      from options.max_tokens.
    """
    options: dict[str, Any] = {
        "parallelToolCalls": False,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
        "extra_body": {
            "top_k": 20,
            "chat_template_kwargs": {
                "enable_thinking": True,
                "preserve_thinking": True,
            },
        },
    }
    if seed is not None:
        options["seed"] = seed
    return options


def _build_agent_sampling_overrides() -> dict[str, Any]:
    """Sampling fields written on each kept agent.

    OCO reads agent.temperature and agent.top_p (consumed as topP at
    runtime) into the top-level AI SDK request settings; putting them in
    model.options is insufficient.
    """
    return {"temperature": 1.0, "top_p": 0.95}


def _configure_selfhost_provider(
    config: dict[str, Any],
    *,
    model_name: str,
    endpoint_url: str | None,
    api_key: str | None,
    context_window: int,
    output_token_limit: int,
    seed: int | None,
) -> None:
    """Install the benchmark model under the selfhost provider with all
    placements OCO's runtime actually reads.

    Source-grounded placements:

    - baseURL, apiKey, and experimentalNonStreamingToolCalls go at PROVIDER
      level under provider.selfhost.options. OCO's SDK init reads from
      provider.options (provider/provider.ts:1028) and the non-streaming
      flag is checked at session/llm.ts:134.
    - parallelToolCalls and the rest of the request-shaping sampling fields
      go inside provider.selfhost.models.<model>.options. OCO merges
      model.options into request options at session/llm.ts:130.
    - Model entry MUST carry id, name, capability flags, and limit. OCO
      computes maxOutputTokens from limit.output (transform.ts:904) and
      checks model capability flags before applying sampling fields.
    """
    providers = config.setdefault("provider", {})
    if not isinstance(providers, dict):
        providers = {}
        config["provider"] = providers

    selfhost = providers.setdefault(SELFHOST_PROVIDER_ID, {})
    if not isinstance(selfhost, dict):
        selfhost = {}
        providers[SELFHOST_PROVIDER_ID] = selfhost
    selfhost.setdefault("name", "Self-hosted benchmark target")

    provider_options = selfhost.setdefault("options", {})
    if not isinstance(provider_options, dict):
        provider_options = {}
        selfhost["options"] = provider_options
    if endpoint_url:
        provider_options["baseURL"] = endpoint_url
    if api_key:
        provider_options["apiKey"] = api_key
    provider_options[
        "experimentalNonStreamingToolCalls"
    ] = not _force_streaming_tool_calls()

    models = selfhost.setdefault("models", {})
    if not isinstance(models, dict):
        models = {}
        selfhost["models"] = models
    target = models.setdefault(model_name, {})
    if not isinstance(target, dict):
        target = {}
        models[model_name] = target
    target["id"] = model_name
    target.setdefault("name", model_name)
    target["reasoning"] = True
    target["temperature"] = True
    target["tool_call"] = True
    target["limit"] = {
        "context": context_window,
        "output": output_token_limit,
    }
    target_options = target.setdefault("options", {})
    if not isinstance(target_options, dict):
        target_options = {}
        target["options"] = target_options
    target_options.update(_build_request_options(seed))


def _apply_agent_overrides(kept_agents: dict[str, Any]) -> None:
    """Stamp per-agent sampling overrides, headless-safe permissions, and
    deny rules for stripped tools.

    Sampling overrides put temperature/top_p where OCO actually reads them
    (agent level).

    Headless-safe permission overrides (HEADLESS_SAFE_PERMISSION_OVERRIDES)
    are FORCED on every kept agent. They overwrite any existing production
    value because deterministic non-interactive behavior is non-negotiable
    for a headless benchmark — a single rule resolving to "ask" hangs the
    subprocess until the outer timeout. See HEADLESS_SAFE_PERMISSION_OVERRIDES
    for the per-tool reasoning.

    Stripped-tool deny rules ensure built-in OCO tools (webfetch, compress,
    compress_map, browser, mcp) are not available to kept agents, since
    dropping the legacy agent.tools field by itself does not disable
    built-ins. These use setdefault — if the production config already
    denies a stripped tool, the existing rule wins; if it is silent, the
    materializer fills in deny.
    """
    overrides = _build_agent_sampling_overrides()
    for agent in kept_agents.values():
        if not isinstance(agent, dict):
            continue
        agent.update(overrides)
        permission = agent.setdefault("permission", {})
        if not isinstance(permission, dict):
            continue
        for tool, action in HEADLESS_SAFE_PERMISSION_OVERRIDES.items():
            permission[tool] = action
        for tool in STRIPPED_TOOL_DENY_KEYS:
            permission.setdefault(tool, "deny")


def _apply_compaction_policy(config: dict[str, Any], *, model_name: str) -> None:
    compaction = config.setdefault("compaction", {})
    if not isinstance(compaction, dict):
        compaction = {}
        config["compaction"] = compaction
    compaction.update({"auto": True, "prune": True, "reserved": 20000})
    agents = _agent_map(config)
    compaction_agent = agents.get(COMPACTION_AGENT)
    if isinstance(compaction_agent, dict):
        compaction_agent["model"] = f"{SELFHOST_PROVIDER_ID}/{model_name}"


def _benchmark_overrides_record(
    *,
    model_name: str,
    endpoint_url: str | None,
    context_window: int,
    output_token_limit: int,
    seed: int | None,
    oco_version: str | None,
) -> dict[str, Any]:
    """Reproducibility annotation; lives in the manifest, NOT in opencode.jsonc.

    Captures the full benchmark policy for reviewer inspection regardless of
    where each field physically lives in the materialized config.
    """
    sampling: dict[str, Any] = {
        "temperature": 1.0,
        "top_p": 0.95,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
        "extra_body": {
            "top_k": 20,
            "chat_template_kwargs": {
                "enable_thinking": True,
                "preserve_thinking": True,
            },
        },
        "seed_range": f"0..{SEED_MODULUS - 1}",
        "seed_derivation": "SHA-256(task_id), first 64 bits modulo 2^31-1",
    }
    if seed is not None:
        sampling["seed"] = seed
    return {
        "oco_version": oco_version,
        "model_name": model_name,
        "endpoint_url": endpoint_url,
        "context_window": context_window,
        "output_token_limit": output_token_limit,
        "sampling": sampling,
        "self_hosted_non_streaming_tool_calls": True,
        "self_hosted_parallel_tool_calls": False,
        "headless_safe_permission_overrides": dict(HEADLESS_SAFE_PERMISSION_OVERRIDES),
    }


def materialize_config(options: MaterializerOptions) -> MaterializerResult:
    production_dir = options.production_config_dir.expanduser().resolve()
    output_dir = options.output_dir
    error_path = output_dir.parent / "oco-config-materialization-error.json"
    tmp_dir = output_dir.parent / f".{output_dir.name}.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    try:
        config_path = _find_config_file(production_dir)
        config = load_jsonc(config_path)
        agents = _agent_map(config)
        primary = _choose_primary_agent(agents, options.primary_agent)
        _validate_required_agents(agents, primary)

        kept_agent_names = {primary, *KEEP_SUBAGENTS, COMPACTION_AGENT}
        removed_agents = sorted(name for name in agents if name not in kept_agent_names)
        removed_agent_prompts = _prompt_references(
            {name: agents[name] for name in removed_agents},
            production_dir,
            require_existing=False,
        )
        kept_agents: dict[str, Any] = {}
        permission_removed_by_agent: dict[str, list[str]] = {}
        agent_model_address = f"{SELFHOST_PROVIDER_ID}/{options.model_name}"
        for name in sorted(kept_agent_names):
            sanitized, removed_perm_keys = _sanitize_agent(agents[name])
            if isinstance(sanitized, dict):
                sanitized["model"] = agent_model_address
            kept_agents[name] = sanitized
            permission_removed_by_agent[name] = removed_perm_keys
        config["agent"] = {
            **kept_agents,
            **{
                name: {"disable": True}
                for name in sorted(STRIP_SUBAGENTS)
                if name not in kept_agent_names
            },
        }

        stripped_plugin_count = (
            len(config.get("plugin", []) or [])
            if isinstance(config.get("plugin", []), list)
            else 0
        )
        stripped_mcp_keys = (
            sorted((config.get("mcp") or {}).keys())
            if isinstance(config.get("mcp"), dict)
            else []
        )
        stripped_skills = []
        for key in ("skill", "skills"):
            value = config.get(key)
            if isinstance(value, dict):
                stripped_skills.extend(str(item) for item in value.keys())
            elif isinstance(value, list):
                stripped_skills.extend(str(item) for item in value)
        config["plugin"] = []
        config["mcp"] = {}
        config["skills"] = {}
        config.pop("skill", None)

        _apply_compaction_policy(config, model_name=options.model_name)
        _configure_selfhost_provider(
            config,
            model_name=options.model_name,
            endpoint_url=options.endpoint_url,
            api_key=options.api_key,
            context_window=options.context_window,
            output_token_limit=options.output_token_limit,
            seed=options.sampling_seed,
        )
        _apply_agent_overrides(kept_agents)

        benchmark_overrides = _benchmark_overrides_record(
            model_name=options.model_name,
            endpoint_url=options.endpoint_url,
            context_window=options.context_window,
            output_token_limit=options.output_token_limit,
            seed=options.sampling_seed,
            oco_version=options.oco_version,
        )

        tmp_dir.mkdir(parents=True)
        prompt_sources = _prompt_references(
            _agent_map(config), production_dir, require_existing=True
        )
        use_qwen_overlay = _is_qwen_model(options.model_name)
        copied_prompts, prompt_overlays = _copy_prompt_files(
            prompt_sources, tmp_dir, use_qwen_overlay=use_qwen_overlay
        )
        copied_prompt_set = set(copied_prompts)
        materialized_config_path = tmp_dir / "opencode.jsonc"
        atomic_write_text(
            materialized_config_path,
            json.dumps(config, indent=2, sort_keys=True) + "\n",
        )
        manifest = {
            "source_config": str(config_path),
            "primary_agent": primary,
            "kept_agents": sorted(kept_agent_names),
            "removed_agents": removed_agents,
            "explicitly_stripped_subagents": sorted(STRIP_SUBAGENTS),
            "disabled_agents": sorted(STRIP_SUBAGENTS),
            "stripped_plugin_count": stripped_plugin_count,
            "stripped_mcp_keys": stripped_mcp_keys,
            "stripped_skills": sorted(set(stripped_skills)),
            "copied_prompts": copied_prompts,
            "prompts_copied": sorted(Path(path).name for path in copied_prompts),
            "stripped_prompts_not_copied": sorted(
                {
                    reference.relative.name
                    for reference in removed_agent_prompts
                    if reference.relative.as_posix() not in copied_prompt_set
                }
            ),
            "stripped_prompt_paths_not_copied": sorted(
                {
                    reference.relative.as_posix()
                    for reference in removed_agent_prompts
                    if reference.relative.as_posix() not in copied_prompt_set
                }
            ),
            "permission_removed_by_agent": permission_removed_by_agent,
            "benchmark_overrides": benchmark_overrides,
            "prompt_variant": "qwen" if use_qwen_overlay else "production",
            "prompt_overlays": prompt_overlays,
        }
        atomic_write_json(tmp_dir / "strip-diff-manifest.json", manifest)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        error_path.unlink(missing_ok=True)
        tmp_dir.replace(output_dir)
        return MaterializerResult(
            snapshot_dir=output_dir,
            config_path=output_dir / "opencode.jsonc",
            manifest_path=output_dir / "strip-diff-manifest.json",
            primary_agent=primary,
            kept_agents=tuple(sorted(kept_agent_names)),
            removed_agents=tuple(removed_agents),
        )
    except Exception as exc:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        if output_dir.exists():
            shutil.rmtree(output_dir)
        atomic_write_json(
            error_path,
            {
                "passed": False,
                "reason": str(exc),
                "production_config_dir": str(production_dir),
                "snapshot_dir": str(output_dir),
            },
        )
        if isinstance(exc, MaterializerError):
            raise
        raise MaterializerError(str(exc)) from exc
