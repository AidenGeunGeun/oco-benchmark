from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

import pytest

from controller.core import AttemptSpec, BenchmarkController, ControllerConfig
from controller.real_oco import RealOCOAdapter, extract_git_patch, parse_oco_json_stream
from controller.version_gate import REQUIRED_FEATURE_STRINGS


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fake_oco(path: Path, version: str) -> Path:
    features = "\n".join(f"# {feature}" for feature in REQUIRED_FEATURE_STRINGS)
    path.write_text(
        f'#!/bin/sh\n{features}\nif [ "$1" = "--version" ]; then echo \'{version}\'; exit 0; fi\nexit 0\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_oco_with_run(path: Path) -> Path:
    features = "\n".join(f"# {feature}" for feature in REQUIRED_FEATURE_STRINGS)
    event = json.dumps(
        {
            "type": "model_step",
            "step_role": "pm",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "tools_called": [],
            "finish_reason": "stop",
        }
    )
    path.write_text(
        f"#!/bin/sh\n{features}\n"
        'if [ "$1" = "--version" ]; then echo \'2.1.7\'; exit 0; fi\n'
        f"if [ \"$1\" = \"run\" ]; then printf '%s\\n' '{event}'; exit 0; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _production_config(path: Path) -> Path:
    prompts = path / "prompts"
    prompts.mkdir(parents=True)
    agents = {}
    for name in ("pm", "orchestrator", "auditor", "investigator", "compaction"):
        (prompts / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
        agents[name] = {
            "prompt": f"prompts/{name}.txt",
            "tools": ["read", "bash", "task"],
        }
    payload = {
        "agent": agents,
        "provider": {
            "selfhost": {"models": {"selfhost-qwen": {"id": "selfhost-qwen"}}}
        },
    }
    (path / "opencode.jsonc").write_text(json.dumps(payload), encoding="utf-8")
    return path


def _init_git_worktree_with_base(worktree: Path) -> str:
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "README.md").write_text("# fixture\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "OCO Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "OCO Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(["git", "-C", str(worktree), "init", "--quiet"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(worktree), "add", "README.md"], check=True, env=env
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "--quiet", "-m", "base"],
        check=True,
        env=env,
    )
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return head.stdout.strip()


def test_extract_git_patch_captures_committed_uncommitted_and_untracked(
    tmp_path: Path,
) -> None:
    """Patch extraction must include changes the model committed on top of the
    base, plus uncommitted edits, plus untracked files. The smoke caught this:
    a model that ran ``git commit`` had its work missed by ``git diff``.
    """
    worktree = tmp_path / "worktree"
    base_commit = _init_git_worktree_with_base(worktree)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "OCO Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "OCO Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }

    (worktree / "committed.txt").write_text("committed content\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "committed.txt"], check=True, env=env
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "--quiet", "-m", "model commit"],
        check=True,
        env=env,
    )
    (worktree / "README.md").write_text(
        "# fixture\nuncommitted edit\n", encoding="utf-8"
    )
    (worktree / "untracked.txt").write_text("untracked content\n", encoding="utf-8")

    patch = extract_git_patch(worktree, base_commit=base_commit)

    assert "committed.txt" in patch, (
        "committed file must appear in extracted patch when base_commit is supplied"
    )
    assert "committed content" in patch
    assert "uncommitted edit" in patch, "working-tree edits must appear in patch"
    assert "untracked.txt" in patch, "untracked files must appear in patch"
    assert "untracked content" in patch


def test_extract_git_patch_without_base_falls_back_to_uncommitted_diff(
    tmp_path: Path,
) -> None:
    """When the caller has no base_commit (older code paths), extraction
    falls back to the prior behavior: uncommitted changes only.
    """
    worktree = tmp_path / "worktree"
    _init_git_worktree_with_base(worktree)
    (worktree / "README.md").write_text("# fixture\nworking edit\n", encoding="utf-8")

    patch = extract_git_patch(worktree)

    assert "working edit" in patch


def test_parse_oco_json_stream_accepts_json_lines_and_text() -> None:
    events = parse_oco_json_stream('{"type":"one"}\nnot-json\n[{"type":"two"}]\n')

    assert events[0] == {"type": "one"}
    assert events[1] == {"type": "stdout_text", "message": "not-json"}
    assert events[2] == {"type": "two"}


def test_smoke_skips_cleanly_when_endpoint_env_is_unset() -> None:
    env = os.environ.copy()
    env.pop("OCO_BENCHMARK_SMOKE_OPENAI_BASE_URL", None)
    for script in (
        "scripts/smoke_real_oco.py",
        "scripts/smoke_orchestration_real_oco.py",
    ):
        completed = subprocess.run(
            [sys.executable, script],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert completed.returncode == 0, completed.stderr
        assert "SKIP" in completed.stdout


def test_modal_and_ssh_integration_checks_skip_cleanly_when_unset() -> None:
    env = os.environ.copy()
    env.pop("OCO_BENCHMARK_MODAL_INTEGRATION", None)
    env.pop("OCO_BENCHMARK_SSH_RSYNC_INTEGRATION", None)
    for script in (
        "scripts/check_modal_integration.py",
        "scripts/check_ssh_rsync_integration.py",
    ):
        completed = subprocess.run(
            [sys.executable, script],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0
        assert "SKIP" in completed.stdout


def test_smoke_version_gate_failure_writes_artifact(tmp_path: Path) -> None:
    old_binary = _fake_oco(tmp_path / "oco-old", "2.1.6")
    run_root = PROJECT_ROOT / "runs" / "unit-smoke-version-fail"
    if run_root.exists():
        import shutil

        shutil.rmtree(run_root)
    env = os.environ.copy()
    env["OCO_BENCHMARK_SMOKE_OPENAI_BASE_URL"] = "http://127.0.0.1:9/v1"
    env["OCO_BENCHMARK_SMOKE_OCO_BINARY"] = str(old_binary)
    env["OCO_BENCHMARK_SMOKE_RUN_ROOT"] = str(run_root)
    completed = subprocess.run(
        [sys.executable, "scripts/smoke_real_oco.py"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    try:
        assert completed.returncode != 0
        artifact = run_root / "attempts" / "smoke-task" / "oco-version-gate.json"
        assert artifact.exists()
        assert "2.1.6" in artifact.read_text(encoding="utf-8")
    finally:
        import shutil

        shutil.rmtree(run_root, ignore_errors=True)


def test_cli_real_adapter_uses_subprocess_not_fixture(tmp_path: Path) -> None:
    fake_oco = _fake_oco_with_run(tmp_path / "oco-realish")
    production = _production_config(tmp_path / "prod")
    run_root = tmp_path / "run"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "controller.cli",
            "--run-root",
            str(run_root),
            "--run-id",
            "cli-real",
            "--adapter-kind",
            "real",
            "--production-config-dir",
            str(production),
            "--oco-binary",
            str(fake_oco),
            "--disable-boundary",
            "--attempts",
            "cli-real-attempt",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    attempt_dir = run_root / "attempts" / "cli-real-attempt"
    assert (attempt_dir / "oco-subprocess.json").exists()
    assert not (run_root / "oco-config-snapshot" / "placeholder.json").exists()


def test_real_oco_adapter_persists_stdout_and_stderr_on_subprocess_timeout(
    tmp_path: Path,
) -> None:
    """Subprocess timeout must persist whatever stdout/stderr the subprocess
    managed to emit before the kill. The prior ``subprocess.run(capture_output)``
    path buffered everything in memory and dropped every byte on timeout —
    a smoke test that hit a 900s outer timeout had an empty attempt
    directory and a lost event stream. The Popen-to-disk path keeps
    diagnostics intact across the kill, and records ``timed_out=True``
    plus the timeout sentinel event for downstream classification.
    """
    fake_oco = tmp_path / "oco-slow"
    features = "\n".join(f"# {feature}" for feature in REQUIRED_FEATURE_STRINGS)
    fake_oco.write_text(
        "#!/bin/sh\n"
        f"{features}\n"
        'if [ "$1" = "--version" ]; then echo \'2.1.7\'; exit 0; fi\n'
        'if [ "$1" = "run" ]; then\n'
        "  printf '%s\\n' '{\"type\":\"stdout_before_kill\"}'\n"
        "  printf '%s\\n' 'partial stderr line' >&2\n"
        "  sleep 30\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_oco.chmod(0o755)

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "opencode.jsonc").write_text(
        json.dumps({"agent": {}}), encoding="utf-8"
    )

    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    (attempt_dir / "worktree").mkdir()

    adapter = RealOCOAdapter(
        oco_binary=str(fake_oco),
        config_snapshot_dir=snapshot,
        timeout_seconds=1.5,
    )

    result = adapter.run(attempt_id="timeout-attempt", attempt_dir=attempt_dir)

    stdout_text = (attempt_dir / "oco-stdout.log").read_text(encoding="utf-8")
    stderr_text = (attempt_dir / "oco-stderr.log").read_text(encoding="utf-8")
    record = json.loads(
        (attempt_dir / "oco-subprocess.json").read_text(encoding="utf-8")
    )

    assert "stdout_before_kill" in stdout_text, (
        "stdout emitted before kill must survive subprocess timeout"
    )
    assert "partial stderr line" in stderr_text, (
        "stderr emitted before kill must survive subprocess timeout"
    )
    assert record["timed_out"] is True
    assert record["timeout_seconds"] == 1.5
    assert record["returncode"] != 0

    event_types = [event.get("type") for event in result.events]
    assert "subprocess_timeout" in event_types, (
        "timeout sentinel event must appear in result so downstream "
        "classification can distinguish timeout from non-timeout failure"
    )
    # The pre-kill JSON line should also be parsed back into the event stream.
    assert {"type": "stdout_before_kill"} in result.events


def test_real_controller_requires_materialized_production_config(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "missing-prod-config"
    controller = BenchmarkController(
        ControllerConfig(
            run_root=run_root,
            run_id="missing-prod-config",
            adapter_kind="real",
            disable_boundary=True,
        )
    )

    with pytest.raises(RuntimeError, match="production_config_dir"):
        controller.run_attempts([AttemptSpec("attempt")])

    artifact = (
        run_root / "oco-config-snapshot" / "oco-config-materialization-error.json"
    )
    assert artifact.exists()
