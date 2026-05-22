#!/usr/bin/env python3
"""Opt-in local smoke exercising the PM->Orchestrator delegation path.

This smoke verifies that when the PM agent receives an explicit
delegation prompt, the `task` tool is invoked with `subagent_type =
"orchestrator"` and the orchestrator subagent does the requested work in
the materialized snapshot. It is the cheapest local check that the
benchmark's actual workflow (PM -> Orchestrator via the task tool) can
run end-to-end without burning pod money.

Outcomes:
- pass: delegation observed in events AND the requested file exists with
  the exact expected content
- partial: file was produced but no delegation seen (PM did the work
  directly); useful signal that orchestration was not exercised on this
  model, but does not indicate a broken benchmark
- fail: no file produced or a hard error from OCO

Env vars (same shape as scripts/smoke_real_oco.py):
- OCO_BENCHMARK_SMOKE_OPENAI_BASE_URL (required to run)
- OCO_BENCHMARK_SMOKE_MODEL (defaults to selfhost-qwen)
- OCO_BENCHMARK_SMOKE_API_KEY (defaults to sk-local-smoke)
- OCO_BENCHMARK_SMOKE_OCO_BINARY (defaults to oco on PATH)
- OCO_BENCHMARK_SMOKE_PRODUCTION_CONFIG_DIR (defaults to ~/.config/oco)
- OCO_BENCHMARK_SMOKE_RUN_ROOT (defaults to oco-benchmark/runs/<id>)
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from controller.artifacts import AttemptPaths  # noqa: E402
from controller.atomic import atomic_write_json, atomic_write_jsonl, atomic_write_text  # noqa: E402
from controller.materializer import MaterializerOptions, materialize_config  # noqa: E402
from controller.precheck import evaluate_patch_precheck  # noqa: E402
from controller.real_oco import RealOCOAdapter  # noqa: E402
from controller.seed import derive_task_seed  # noqa: E402
from controller.telemetry import normalize_events  # noqa: E402
from controller.version_gate import check_oco_binary, write_gate_artifact  # noqa: E402

URL_ENV = "OCO_BENCHMARK_SMOKE_OPENAI_BASE_URL"
MODEL_ENV = "OCO_BENCHMARK_SMOKE_MODEL"
API_KEY_ENV = "OCO_BENCHMARK_SMOKE_API_KEY"
OCO_BINARY_ENV = "OCO_BENCHMARK_SMOKE_OCO_BINARY"
PRODUCTION_CONFIG_ENV = "OCO_BENCHMARK_SMOKE_PRODUCTION_CONFIG_DIR"
RUN_ROOT_ENV = "OCO_BENCHMARK_SMOKE_RUN_ROOT"

EXPECTED_FILE = "orchestration_check.txt"
EXPECTED_CONTENT = "Orchestration handoff verified.\n"
DELEGATION_PROMPT = (
    "You are running inside the OCO benchmark orchestration smoke. "
    "You MUST delegate this work to the Orchestrator via the `task` tool "
    '(subagent_type="orchestrator"). This is a hard rule: do not skip '
    "delegation because the task looks trivial. The entire point of this "
    "smoke is to verify the task tool can reach the Orchestrator end-to-end. "
    "Writing the file directly with the write tool, OR describing the "
    "delegation in prose without actually invoking the task tool, counts as "
    "a smoke FAILURE. The work to delegate: create a single file named "
    f"`{EXPECTED_FILE}` in the current repository containing exactly this "
    f"one line: `{EXPECTED_CONTENT.strip()}`. Do not modify any other files. "
    "After the Orchestrator finishes, stop."
)


def main() -> int:
    base_url = os.environ.get(URL_ENV)
    if not base_url:
        print(f"SKIP: set {URL_ENV} to a local OpenAI-compatible base URL to run smoke")
        return 0

    run_id = "orchestration-smoke-" + datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    run_root = Path(os.environ.get(RUN_ROOT_ENV, str(PROJECT_ROOT / "runs" / run_id)))
    attempt_id = "orchestration-smoke"
    paths = AttemptPaths(run_root, attempt_id)
    paths.ensure()
    model = os.environ.get(MODEL_ENV, "selfhost-qwen")
    oco_binary = os.environ.get(OCO_BINARY_ENV, "oco")
    api_key = os.environ.get(API_KEY_ENV, "sk-local-smoke")

    gate = check_oco_binary(oco_binary)
    if not gate.passed:
        write_gate_artifact(paths.attempt_dir / "oco-version-gate.json", gate)
        _write_report(run_root, "fail", "version gate failed", paths, {})
        print(f"FAIL: {gate.reason}")
        return 2

    if not _endpoint_reachable(base_url, api_key):
        _write_report(run_root, "fail", "endpoint is unreachable", paths, {})
        print(f"FAIL: endpoint is unreachable: {base_url}")
        return 3

    production_config_dir = Path(
        os.environ.get(PRODUCTION_CONFIG_ENV, str(Path.home() / ".config" / "oco"))
    )
    try:
        materialize_config(
            MaterializerOptions(
                production_config_dir=production_config_dir,
                output_dir=run_root / "oco-config-snapshot",
                oco_version=gate.detected_version,
                model_name=model,
                endpoint_url=base_url,
                api_key=api_key,
            )
        )
    except Exception as exc:  # noqa: BLE001 - smoke writes a report for operator diagnosis.
        _write_report(run_root, "fail", f"materializer failed: {exc}", paths, {})
        print(f"FAIL: materializer failed: {exc}")
        return 4

    base_commit = _prepare_tiny_git_worktree(paths.worktree_dir)
    env_api_key_name = "OPENAI_API_KEY"
    os.environ[env_api_key_name] = api_key
    adapter = RealOCOAdapter(
        oco_binary=oco_binary,
        config_snapshot_dir=run_root / "oco-config-snapshot",
        timeout_seconds=900,
    )
    try:
        result = adapter.run(
            attempt_id=attempt_id,
            attempt_dir=paths.attempt_dir,
            worktree_dir=paths.worktree_dir,
            prompt=DELEGATION_PROMPT,
            seed=derive_task_seed(attempt_id),
            config_snapshot_dir=run_root / "oco-config-snapshot",
        )
    except Exception as exc:  # noqa: BLE001 - smoke writes a report for operator diagnosis.
        _write_report(run_root, "fail", f"real OCO run failed: {exc}", paths, {})
        print(f"FAIL: real OCO run failed: {exc}")
        return 5

    atomic_write_jsonl(paths.oco_events_path, result.events)
    normalized = normalize_events(result.events, attempt_id=attempt_id, run_id=run_id)
    patch_diff = str(normalized.pop("patch_diff", ""))
    normalized["seed"] = derive_task_seed(attempt_id)
    normalized.update(
        evaluate_patch_precheck(
            worktree_dir=paths.worktree_dir,
            patch_text=patch_diff,
            base_commit=base_commit,
            scratch_dir=paths.attempt_dir / "precheck-worktree",
        ).to_normalized_fields()
    )
    atomic_write_text(paths.patch_path, patch_diff)
    atomic_write_json(paths.normalized_path, normalized)

    expected_path = paths.worktree_dir / EXPECTED_FILE
    file_present = expected_path.exists()
    file_content = expected_path.read_text(encoding="utf-8") if file_present else ""
    content_matches = file_content == EXPECTED_CONTENT
    delegation_observed = bool(normalized.get("delegation_observed"))
    tools_seen = sorted(
        {
            tool
            for step in normalized.get("steps", [])
            for tool in step.get("tools_called", [])
        }
    )

    indicators = {
        "delegation_observed": delegation_observed,
        "file_present": file_present,
        "content_matches": content_matches,
        "step_count": normalized.get("step_count", 0),
        "tools_called_unique": tools_seen,
        "patch_extracted_nonempty": bool(patch_diff.strip()),
    }

    if not file_present:
        verdict = "fail"
        reason = "expected file not created by OCO"
    elif not content_matches:
        verdict = "fail"
        reason = f"expected file content {EXPECTED_CONTENT!r}, got {file_content!r}"
    elif not delegation_observed:
        verdict = "partial"
        reason = (
            "file produced but PM did not delegate via task tool; "
            "orchestration path not exercised on this model"
        )
    else:
        verdict = "pass"
        reason = "delegation observed and orchestrator produced expected file"

    _write_report(run_root, verdict, reason, paths, indicators)
    print(f"Report: {run_root / 'smoke-report.json'}")
    print(f"Verdict: {verdict.upper()} — {reason}")
    print(f"Indicators: {indicators}")
    return 0 if verdict in ("pass", "partial") else 6


def _endpoint_reachable(base_url: str, api_key: str) -> bool:
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {api_key}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - operator-supplied local URL.
            return 200 <= response.status < 500
    except Exception:
        return False


def _prepare_tiny_git_worktree(worktree: Path) -> str:
    worktree.mkdir(parents=True, exist_ok=True)
    atomic_write_text(worktree / "README.md", "# OCO orchestration smoke\n")
    _git(worktree, "init", "--quiet")
    _git(worktree, "add", "README.md")
    _git(
        worktree,
        "-c",
        "user.name=OCO Benchmark",
        "-c",
        "user.email=oco-benchmark@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "initial smoke fixture",
    )
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    return completed


def _write_report(
    run_root: Path,
    verdict: str,
    reason: str,
    paths: AttemptPaths,
    indicators: dict[str, object],
) -> None:
    atomic_write_json(
        run_root / "smoke-report.json",
        {
            "verdict": verdict,
            "reason": reason,
            "indicators": indicators,
            "patch_path": str(paths.patch_path),
            "normalized_path": str(paths.normalized_path),
            "events_path": str(paths.oco_events_path),
            "url_env": URL_ENV,
            "model_env": MODEL_ENV,
            "api_key_env": API_KEY_ENV,
            "run_root_env": RUN_ROOT_ENV,
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
