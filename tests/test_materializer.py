from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from controller.materializer import (
    HEADLESS_SAFE_PERMISSION_OVERRIDES,
    MaterializerError,
    MaterializerOptions,
    materialize_config,
)


PRODUCTION_PERMISSION_READ = {
    "**/.env*": "deny",
    "**/*.pem": "deny",
    "**/*.key": "deny",
    "**/secrets/**": "deny",
    "**/credentials/**": "deny",
    "**/.aws/**": "deny",
    "**/.ssh/**": "deny",
    "**/docker-compose*.yml": "deny",
    "**/config/database.yml": "deny",
    "*": "allow",
}


def _write_fixture_config(config_dir: Path) -> None:
    """Write a production-shaped fixture config.

    The fixture mirrors the structure of the user's real production config:
    agents declare prompts via {file:prompts/<name>.txt} references and use
    permission maps with allow/deny policy strings on path globs. No
    top-level `tools` field. This shape is what OCO's config schema accepts.
    """
    prompts = config_dir / "prompts"
    prompts.mkdir(parents=True)
    kept_prompt_names = ("pm", "orchestrator", "auditor", "investigator", "compaction")
    stripped_prompt_names = ("test_runner", "web-search", "docs")
    for name in (*kept_prompt_names, *stripped_prompt_names):
        (prompts / f"{name}.txt").write_text(f"{name} prompt\n", encoding="utf-8")

    def prompt_ref(name: str) -> str:
        return f"{{file:prompts/{name}.txt}}"

    standard_permission = {
        "read": dict(PRODUCTION_PERMISSION_READ),
        "edit": "allow",
    }
    payload = {
        "$schema": "https://opencode.ai/config.json",
        "snapshot": False,
        "compaction": {"auto": False},
        "default_agent": "build",
        "agent": {
            "pm": {
                "display_name": "PM",
                "description": "PM agent.",
                "model": "anthropic/claude-opus-4-7",
                "prompt": prompt_ref("pm"),
                "permission": dict(standard_permission),
            },
            "orchestrator": {
                "mode": "subagent",
                "description": "Orchestrator agent.",
                "model": "openai/gpt-5.5-fast",
                "prompt": prompt_ref("orchestrator"),
                "reasoningEffort": "xhigh",
                "permission": dict(standard_permission),
            },
            "auditor": {
                "mode": "subagent",
                "description": "Auditor agent.",
                "model": "openai/gpt-5.5",
                "prompt": prompt_ref("auditor"),
                "reasoningEffort": "high",
                "permission": {
                    "edit": "deny",
                    "write": "deny",
                    "apply_patch": "deny",
                    "bash": "allow",
                    "grep": "allow",
                    "glob": "allow",
                    "read": dict(PRODUCTION_PERMISSION_READ),
                },
            },
            "investigator": {
                "mode": "subagent",
                "description": "Investigator agent.",
                "model": "openai/gpt-5.5",
                "prompt": prompt_ref("investigator"),
                "reasoningEffort": "medium",
                "permission": {
                    "grep": "allow",
                    "glob": "allow",
                    "bash": "deny",
                    "edit": "deny",
                    "write": "deny",
                    "apply_patch": "deny",
                    "read": dict(PRODUCTION_PERMISSION_READ),
                },
            },
            "compaction": {
                "model": "openai/gpt-5.4-mini",
                "prompt": prompt_ref("compaction"),
                "reasoningEffort": "medium",
            },
            "test_runner": {
                "mode": "subagent",
                "description": "Test runner agent.",
                "model": "openai/gpt-5.4-mini",
                "prompt": prompt_ref("test_runner"),
                "permission": {
                    "webfetch": "deny",
                    "compress": "deny",
                    "bash": "allow",
                    "read": dict(PRODUCTION_PERMISSION_READ),
                },
            },
            "web-search": {
                "mode": "subagent",
                "description": "Web search agent.",
                "model": "openai/gpt-5.5",
                "prompt": prompt_ref("web-search"),
                "permission": {
                    "webfetch": "allow",
                    "bash": "allow",
                    "read": dict(PRODUCTION_PERMISSION_READ),
                },
            },
            "docs": {
                "mode": "subagent",
                "description": "Docs agent.",
                "model": "openai/gpt-5.5",
                "prompt": prompt_ref("docs"),
                "permission": {
                    "edit": "allow",
                    "write": "allow",
                    "read": dict(PRODUCTION_PERMISSION_READ),
                },
            },
        },
        "provider": {
            "selfhost": {
                "models": {"selfhost-qwen": {"id": "selfhost-qwen"}},
            },
        },
        "plugin": ["opencode-context-compress"],
        "mcp": {"perplexity": {"enabled": True}},
        "skills": {"recall": {"path": "x"}},
    }
    (config_dir / "opencode.jsonc").write_text(
        "// production fixture\n" + json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def test_materialized_snapshot_uses_oco_runtime_placements(
    tmp_path: Path,
) -> None:
    """The materialized snapshot must place every benchmark-controlled field
    where OCO actually reads it at run time. Drift was caught only after
    repeated smoke runs; this test pins each placement so a future regression
    fails at unit-test time.
    """
    production = tmp_path / "prod"
    production.mkdir()
    _write_fixture_config(production)
    output = tmp_path / "snapshot"

    result = materialize_config(
        MaterializerOptions(
            production_config_dir=production,
            output_dir=output,
            oco_version="2.1.7",
            endpoint_url="http://localhost:8001/v1",
            api_key="test-key-9999",
            context_window=200000,
            output_token_limit=32768,
            sampling_seed=12345,
        )
    )

    config = json.loads(result.config_path.read_text(encoding="utf-8"))
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    # Kept agents only; all addressed as "<provider_id>/<model_id>".
    assert set(config["agent"]) == {
        "pm",
        "orchestrator",
        "auditor",
        "investigator",
        "compaction",
    }
    for name, agent in config["agent"].items():
        assert agent["model"] == "selfhost/selfhost-qwen", (
            f"agent {name!r} has unexpected model address {agent['model']!r}"
        )
        assert "/" in agent["model"]
        assert "tools" not in agent, f"agent {name!r} should not have a tools field"

    # Sampling lives at AGENT level (OCO reads agent.temperature/top_p into
    # the top-level AI SDK request settings; model.options.temperature is
    # not the path OCO reads).
    for name, agent in config["agent"].items():
        assert agent["temperature"] == 1.0
        assert agent["top_p"] == 0.95

    # Stripped built-in tools are denied per kept agent. Dropping the legacy
    # tools field does not by itself disable webfetch/compress/etc.
    for name, agent in config["agent"].items():
        permission = agent.get("permission", {})
        assert permission.get("webfetch") == "deny"
        assert permission.get("compress") == "deny"
        assert permission.get("compress_map") == "deny"
        assert permission.get("browser") == "deny"
        assert permission.get("mcp") == "deny"

    # Permission policy structure remains a normal OCO permission map (no
    # redaction overreach into policy strings, no [REDACTED_SECRET]
    # corruption of "deny" / "allow" values, which was the round-2 failure
    # mode that motivated dropping the redactor entirely). The actual
    # action values are exercised by
    # test_headless_safe_permission_overrides_are_forced_on_every_kept_agent.
    auditor_permission = config["agent"]["auditor"]["permission"]
    assert isinstance(auditor_permission, dict)
    for value in auditor_permission.values():
        if isinstance(value, str):
            assert value in {"allow", "deny", "ask"}, (
                f"corrupted policy string {value!r} survived materialization"
            )

    # Benchmark-internal annotation never appears at the root of opencode.jsonc.
    assert "benchmarkSampling" not in config
    assert "selfHostedNonStreamingToolCalls" not in config
    assert "selfHostedParallelToolCalls" not in config
    assert "ocoVersion" not in config

    selfhost = config["provider"]["selfhost"]
    # baseURL, apiKey, and experimentalNonStreamingToolCalls live at
    # provider-level options. OCO's SDK init reads provider.options;
    # per-model baseURL/apiKey are silently ignored.
    provider_options = selfhost["options"]
    assert provider_options["baseURL"] == "http://localhost:8001/v1"
    assert provider_options["apiKey"] == "test-key-9999"
    assert provider_options["experimentalNonStreamingToolCalls"] is True

    target = selfhost["models"]["selfhost-qwen"]
    # Model entry carries id/name/capability flags/limit — OCO computes
    # maxOutputTokens from limit.output and checks capability flags before
    # applying sampling fields.
    assert target["id"] == "selfhost-qwen"
    assert target["name"] == "selfhost-qwen"
    assert target["reasoning"] is True
    assert target["temperature"] is True
    assert target["tool_call"] is True
    assert target["limit"] == {"context": 200000, "output": 32768}

    # parallelToolCalls and the rest of request-shaping sampling fields go
    # inside model.options. Model top-level parallelToolCalls (the previous
    # placement) is NOT read at runtime.
    target_options = target["options"]
    assert target_options["parallelToolCalls"] is False
    assert "parallelToolCalls" not in target  # only model.options.parallelToolCalls
    assert target_options["min_p"] == 0.0
    assert target_options["presence_penalty"] == 0.0
    assert target_options["repetition_penalty"] == 1.0
    assert target_options["seed"] == 12345
    assert target_options["extra_body"]["top_k"] == 20

    # OCO computes maxOutputTokens from model.limit.output. options.max_tokens
    # is not read; it was removed to avoid implying a control surface that
    # does not exist.
    assert "max_tokens" not in target_options

    # Per-model baseURL / experimentalNonStreamingToolCalls / providerOptions
    # are obsolete placements; the new shape must not emit them.
    assert "baseURL" not in target
    assert "providerOptions" not in target
    assert "apiKey" not in target

    # Compaction policy is OCO-recognized; lives in opencode.jsonc.
    assert config["compaction"] == {"auto": True, "prune": True, "reserved": 20000}
    assert config["agent"]["compaction"]["model"] == "selfhost/selfhost-qwen"

    # Plugins, MCP, and skills stripped.
    assert config["plugin"] == []
    assert config["mcp"] == {}
    assert config["skills"] == {}

    # Manifest still records benchmark overrides as a reproducibility record.
    overrides = manifest["benchmark_overrides"]
    assert overrides["oco_version"] == "2.1.7"
    assert overrides["model_name"] == "selfhost-qwen"
    assert overrides["context_window"] == 200000
    assert overrides["output_token_limit"] == 32768
    assert overrides["self_hosted_non_streaming_tool_calls"] is True
    assert overrides["self_hosted_parallel_tool_calls"] is False
    assert overrides["sampling"]["temperature"] == 1.0
    assert overrides["sampling"]["seed"] == 12345

    # Manifest still records stripped surface.
    assert manifest["removed_agents"] == ["docs", "test_runner", "web-search"]
    assert manifest["stripped_plugin_count"] == 1
    assert manifest["stripped_mcp_keys"] == ["perplexity"]
    assert set(manifest["prompts_copied"]) == {
        "pm.txt",
        "orchestrator.txt",
        "auditor.txt",
        "investigator.txt",
        "compaction.txt",
    }
    assert set(manifest["stripped_prompts_not_copied"]) == {
        "test_runner.txt",
        "web-search.txt",
        "docs.txt",
    }

    # Prompt files physically present, byte-equal to source.
    for name in ("pm", "orchestrator", "auditor", "investigator", "compaction"):
        assert (output / "prompts" / f"{name}.txt").read_bytes() == (
            production / "prompts" / f"{name}.txt"
        ).read_bytes()
    for name in ("test_runner", "web-search", "docs"):
        assert not (output / "prompts" / f"{name}.txt").exists()


def test_headless_safe_permission_overrides_are_forced_on_every_kept_agent(
    tmp_path: Path,
) -> None:
    """Determinism gate: every kept agent must end with the headless-safe
    permission overrides, even when the production fixture sets a conflicting
    value (production auditor has ``edit: "deny"``; benchmark forces
    ``edit: "allow"`` because non-interactive determinism beats production
    fidelity inside a sandboxed worktree).

    Verifies all six overrides on all five kept agents, the production
    deny/path-glob values are replaced, and the manifest surfaces the
    override list for reviewer visibility.
    """
    production = tmp_path / "prod"
    production.mkdir()
    _write_fixture_config(production)
    output = tmp_path / "snapshot"

    result = materialize_config(
        MaterializerOptions(
            production_config_dir=production,
            output_dir=output,
            endpoint_url="http://localhost:8001/v1",
            api_key="test-key",
        )
    )

    config = json.loads(result.config_path.read_text(encoding="utf-8"))
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    kept_agents = ("pm", "orchestrator", "auditor", "investigator", "compaction")
    for name in kept_agents:
        permission = config["agent"][name].get("permission", {})
        for tool, expected in HEADLESS_SAFE_PERMISSION_OVERRIDES.items():
            assert permission.get(tool) == expected, (
                f"agent {name!r} permission.{tool} is {permission.get(tool)!r} "
                f"but headless-safe override requires {expected!r}"
            )

    # Production deny choices are deliberately overridden. The benchmark
    # runs inside an isolated worktree on a throwaway pod where production
    # interactive-design choices like "auditor can't edit" or "investigator
    # can't bash" are not safety constraints — they are scoping rules that
    # would only cause hangs or unrecoverable tool failures in a headless
    # run.
    assert config["agent"]["auditor"]["permission"]["edit"] == "allow"
    assert config["agent"]["investigator"]["permission"]["bash"] == "allow"

    # The production `read` permission map (path-glob deny rules for
    # secrets/credentials/.env) is replaced by the blanket "allow" because
    # the snapshot lives only on the operator's disk and the inference pod;
    # both already have the same secrets via .env. Publication-time
    # sanitization, if ever needed, is a separate concern.
    assert config["agent"]["pm"]["permission"]["read"] == "allow"
    assert config["agent"]["auditor"]["permission"]["read"] == "allow"

    overrides = manifest["benchmark_overrides"]
    assert overrides["headless_safe_permission_overrides"] == dict(
        HEADLESS_SAFE_PERMISSION_OVERRIDES
    )


def test_materialized_snapshot_is_valid_oco_config(tmp_path: Path) -> None:
    """Programmatic gate: feed the materialized snapshot to OCO's config loader.

    This is the integration-strength test that step 3 was missing. It runs
    `oco debug config` against the materialized snapshot in an isolated HOME
    and asserts OCO does not emit a 'Configuration is invalid' error.
    """
    oco_binary = os.environ.get("OCO_BENCHMARK_TEST_OCO_BINARY") or shutil.which("oco")
    if not oco_binary:
        candidate = Path.home() / ".local" / "bin" / "oco"
        if candidate.exists():
            oco_binary = str(candidate)
    if not oco_binary:
        pytest.skip(
            "oco binary not available; set OCO_BENCHMARK_TEST_OCO_BINARY to enable"
        )

    production = tmp_path / "prod"
    production.mkdir()
    _write_fixture_config(production)
    output = tmp_path / "snapshot"

    materialize_config(
        MaterializerOptions(
            production_config_dir=production,
            output_dir=output,
            oco_version="2.1.7",
            endpoint_url="http://localhost:65535/v1",
            api_key="test-key-9999",
            sampling_seed=12345,
        )
    )

    isolated_home = tmp_path / "oco-home"
    opencode_dir = isolated_home / ".config" / "opencode"
    opencode_dir.mkdir(parents=True)
    for child in output.iterdir():
        target = opencode_dir / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)

    env = os.environ.copy()
    env["HOME"] = str(isolated_home)
    env["XDG_CONFIG_HOME"] = str(isolated_home / ".config")
    env["XDG_DATA_HOME"] = str(isolated_home / ".local" / "share")
    env["XDG_STATE_HOME"] = str(isolated_home / ".local" / "state")
    env["XDG_CACHE_HOME"] = str(isolated_home / ".cache")
    completed = subprocess.run(
        [oco_binary, "debug", "config"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    combined = completed.stdout + "\n" + completed.stderr
    assert "Configuration is invalid" not in combined, (
        f"OCO rejected the materialized snapshot:\n{combined}"
    )
    assert completed.returncode == 0, (
        f"oco debug config exited {completed.returncode}:\n{combined}"
    )


def test_malformed_config_fails_cleanly_with_no_partial_snapshot(
    tmp_path: Path,
) -> None:
    production = tmp_path / "bad-prod"
    production.mkdir()
    (production / "opencode.jsonc").write_text("{ invalid jsonc", encoding="utf-8")
    output = tmp_path / "snapshot"

    with pytest.raises(MaterializerError):
        materialize_config(
            MaterializerOptions(production_config_dir=production, output_dir=output)
        )

    assert not output.exists()
    error = json.loads(
        (tmp_path / "oco-config-materialization-error.json").read_text(encoding="utf-8")
    )
    assert error["passed"] is False
    assert "invalid JSON/JSONC" in error["reason"]


def test_missing_kept_prompt_fails_cleanly_with_no_partial_snapshot(
    tmp_path: Path,
) -> None:
    production = tmp_path / "missing-prompt-prod"
    production.mkdir()
    _write_fixture_config(production)
    (production / "prompts" / "auditor.txt").unlink()
    output = tmp_path / "snapshot"

    with pytest.raises(MaterializerError, match="auditor"):
        materialize_config(
            MaterializerOptions(production_config_dir=production, output_dir=output)
        )

    assert not output.exists()
    assert not (tmp_path / ".snapshot.tmp").exists()
    error = json.loads(
        (tmp_path / "oco-config-materialization-error.json").read_text(encoding="utf-8")
    )
    assert error["passed"] is False
    assert "auditor" in error["reason"]
    assert "prompts/auditor.txt" in error["reason"]


def test_missing_required_agent_fails_without_partial_snapshot(tmp_path: Path) -> None:
    production = tmp_path / "missing-agent"
    production.mkdir()
    (production / "opencode.jsonc").write_text(
        json.dumps({"agent": {"pm": {"prompt": "inline"}}}), encoding="utf-8"
    )
    output = tmp_path / "snapshot"

    with pytest.raises(MaterializerError):
        materialize_config(
            MaterializerOptions(production_config_dir=production, output_dir=output)
        )

    assert not output.exists()
