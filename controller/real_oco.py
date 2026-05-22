"""Real OCO subprocess adapter and structured event-stream parsing."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from controller.atomic import atomic_write_json, atomic_write_text
from controller.version_gate import check_oco_binary, write_gate_artifact


@dataclass(frozen=True)
class RealOCORunResult:
    events: list[dict[str, Any]]


@dataclass(frozen=True)
class RealOCOAdapter:
    oco_binary: str | Path = "oco"
    config_snapshot_dir: Path | None = None
    agent: str | None = None
    model: str | None = None
    timeout_seconds: float = 1800.0

    def run(
        self,
        *,
        attempt_id: str,
        attempt_dir: Path,
        worktree_dir: Path | None = None,
        prompt: str | None = None,
        seed: int | None = None,
        config_snapshot_dir: Path | None = None,
        **_: Any,
    ) -> RealOCORunResult:
        gate_result = check_oco_binary(self.oco_binary)
        if not gate_result.passed:
            write_gate_artifact(attempt_dir / "oco-version-gate.json", gate_result)
            raise RuntimeError(gate_result.reason)

        snapshot_dir = config_snapshot_dir or self.config_snapshot_dir
        if snapshot_dir is None:
            raise RuntimeError(
                "real OCO adapter requires a materialized config snapshot"
            )
        worktree = worktree_dir or attempt_dir / "worktree"
        base_commit = _capture_base_commit(worktree)
        isolated_home = prepare_isolated_config_home(
            snapshot_dir=snapshot_dir,
            attempt_dir=attempt_dir,
            seed=seed,
        )
        env = os.environ.copy()
        env["HOME"] = str(isolated_home)
        env["XDG_CONFIG_HOME"] = str(isolated_home / ".config")
        if seed is not None:
            env["OCO_BENCHMARK_TASK_SEED"] = str(seed)

        message = prompt or f"Solve benchmark task {attempt_id}."
        command = [str(self.oco_binary), "run", "--format", "json"]
        if self.agent:
            command.extend(["--agent", self.agent])
        if self.model:
            command.extend(["--model", self.model])
        command.append(message)

        started = time.monotonic()
        executed_command = command_with_filesystem_trace(
            command, attempt_dir / "filesystem-trace.log"
        )
        stdout_path = attempt_dir / "oco-stdout.log"
        stderr_path = attempt_dir / "oco-stderr.log"
        # Stream subprocess stdout/stderr directly to disk via the file
        # descriptor passed to Popen. This bypasses Python in-memory
        # buffering so that even when the subprocess is killed by the
        # outer timeout (the exact failure mode the previous
        # subprocess.run(capture_output=True) path hid by buffering until
        # exit) we still keep every byte the subprocess managed to write
        # before its end-of-life. The kernel flushes the FD on subprocess
        # termination; only the very last unflushed bytes from the
        # subprocess's own stdio buffer can be lost on hard kill.
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        timed_out = False
        returncode: int
        with (
            open(stdout_path, "w", encoding="utf-8") as stdout_handle,
            open(stderr_path, "w", encoding="utf-8") as stderr_handle,
        ):
            process = subprocess.Popen(
                executed_command,
                cwd=worktree,
                env=env,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
            )
            try:
                returncode = process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                returncode = (
                    process.returncode if process.returncode is not None else -1
                )
        wall_time_ms = int((time.monotonic() - started) * 1000)
        stdout_text = stdout_path.read_text(encoding="utf-8")
        stderr_text = stderr_path.read_text(encoding="utf-8")
        subprocess_record: dict[str, Any] = {
            "command": command,
            "executed_command": executed_command,
            "returncode": returncode,
            "wall_time_ms": wall_time_ms,
            "isolated_home": str(isolated_home),
            "timed_out": timed_out,
            "timeout_seconds": self.timeout_seconds,
        }
        atomic_write_json(attempt_dir / "oco-subprocess.json", subprocess_record)
        events = parse_oco_json_stream(stdout_text)
        if timed_out:
            events.append(
                {
                    "type": "subprocess_timeout",
                    "returncode": returncode,
                    "timeout_seconds": self.timeout_seconds,
                    "stderr_tail": stderr_text[-2000:],
                    "stdout_tail": stdout_text[-2000:],
                    "wall_time_ms": wall_time_ms,
                }
            )
        elif returncode != 0:
            events.append(
                {
                    "type": "subprocess_error",
                    "returncode": returncode,
                    "message": stderr_text[-2000:],
                    "wall_time_ms": wall_time_ms,
                }
            )
        patch_text = extract_git_patch(worktree, base_commit=base_commit)
        events.append({"type": "patch_diff", "diff": patch_text})
        return RealOCORunResult(events=events)


def parse_oco_json_stream(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            events.append({"type": "stdout_text", "message": stripped})
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
        elif isinstance(parsed, list):
            events.extend(item for item in parsed if isinstance(item, dict))
        else:
            events.append({"type": "stdout_value", "value": parsed})
    return events


def command_with_filesystem_trace(command: list[str], trace_path: Path) -> list[str]:
    strace = shutil.which("strace")
    if not strace:
        return command
    return [
        strace,
        "-f",
        "-qq",
        "-e",
        "trace=file",
        "-o",
        str(trace_path),
        *command,
    ]


def prepare_isolated_config_home(
    *, snapshot_dir: Path, attempt_dir: Path, seed: int | None
) -> Path:
    home = attempt_dir / "oco-home"
    if home.exists():
        shutil.rmtree(home)
    for config_name in ("oco", "opencode"):
        target = home / ".config" / config_name
        shutil.copytree(snapshot_dir, target)
        if seed is not None:
            _inject_seed(target / "opencode.jsonc", seed)
    return home


def _inject_seed(config_path: Path, seed: int) -> None:
    if not config_path.exists():
        return
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    # Per-attempt seed lives only in provider model options (OCO-recognized).
    # Benchmark-internal seed/policy annotation lives in the manifest, not
    # in opencode.jsonc, because OCO rejects unknown root keys.
    providers = payload.get("provider", {})
    if isinstance(providers, dict):
        for provider in providers.values():
            if not isinstance(provider, dict):
                continue
            models = provider.get("models", {})
            if not isinstance(models, dict):
                continue
            for model in models.values():
                if isinstance(model, dict):
                    options = model.setdefault("options", {})
                    if isinstance(options, dict):
                        options["seed"] = seed
    atomic_write_text(config_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _capture_base_commit(worktree: Path) -> str | None:
    """Capture the worktree's HEAD before the OCO subprocess runs.

    The benchmark uses this as the diff base so committed changes from the
    OCO run are not missed by patch extraction. Returns None if the worktree
    has no .git directory or no HEAD yet.
    """
    if not (worktree / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def extract_git_patch(worktree: Path, base_commit: str | None = None) -> str:
    """Extract the model's net changes against base_commit.

    Captures committed, staged, and working-tree changes via
    ``git diff <base_commit>`` (which diffs working tree against the base
    commit, naturally including any commits the model made on top). Falls
    back to ``git diff`` (uncommitted vs HEAD) when base_commit is unknown,
    matching prior behavior for callers that don't capture a base commit.
    Untracked files are appended individually.
    """
    if not (worktree / ".git").exists():
        return ""
    if base_commit:
        diff_args = ["diff", base_commit, "--binary", "--no-ext-diff"]
    else:
        diff_args = ["diff", "--binary", "--no-ext-diff"]
    tracked = subprocess.run(
        ["git", "-C", str(worktree), *diff_args],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    patch = tracked.stdout if tracked.returncode == 0 else ""
    untracked = subprocess.run(
        ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if untracked.returncode != 0:
        return patch
    for relative in [line for line in untracked.stdout.splitlines() if line.strip()]:
        add = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "diff",
                "--no-index",
                "--binary",
                "--",
                "/dev/null",
                relative,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if add.stdout:
            patch += add.stdout.replace("/dev/null", f"a/{relative}", 1)
    return patch
