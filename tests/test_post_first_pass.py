from __future__ import annotations

import json
from pathlib import Path

import pytest

from controller.artifacts import AttemptPaths
from controller.atomic import atomic_write_json, atomic_write_text
from controller.phases import Phase
from controller.post_first_pass import (
    ATTEMPT_CLASS_CLOSED_SET,
    CLASS_DELEGATED_OR_MIDSTREAM_NO_PATCH,
    CLASS_EXPLICIT_NO_FIX,
    CLASS_MALFORMED_TOOL_PROSE_NO_PATCH,
    CLASS_OUTPUT_LENGTH_NO_PATCH,
    CLASS_STOPPED_NO_PATCH,
    CLASS_SUBPROCESS_PROVIDER_INFRA_FAILURE,
    INFRA_FAILURE_PATTERN,
    classify_run,
    package_delegated_diagnostics,
    prepare_continuation_run,
    prepare_post_continuation_eval_bundle,
)


FIXTURE = Path(__file__).parent / "fixtures" / "pro_tasks" / "pro_fixture.jsonl"


PATCH = "diff --git a/file.py b/file.py\n--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-a\n+b\n"


def _write_attempt(
    run_root: Path,
    attempt_id: str,
    normalized: dict,
    *,
    patch: str = "",
    stdout: str = "",
) -> None:
    paths = AttemptPaths(run_root, attempt_id)
    paths.ensure()
    payload = {
        "attempt_id": attempt_id,
        "run_id": "unit-run",
        "steps": [],
        "step_count": 0,
        "tool_call_count": 0,
        "no_patch": not bool(patch.strip()),
        "queued_for_evaluation": bool(patch.strip()),
        "precheck_failed": False,
        **normalized,
    }
    atomic_write_json(paths.normalized_path, payload)
    atomic_write_text(paths.patch_path, patch)
    atomic_write_json(paths.attempt_dir / "oco-subprocess.json", {"returncode": 0})
    if stdout:
        atomic_write_text(paths.attempt_dir / "oco-stdout.log", stdout)
    paths.write_phase_marker(Phase.DONE)


def _classification_fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    run_root = tmp_path / "first-pass"
    _write_attempt(run_root, "repo_a__task-001", {}, patch=PATCH)
    _write_attempt(run_root, "repo_a__task-002", {})
    _write_attempt(
        run_root,
        "repo_b__task-003",
        {},
        stdout="I will call <tool_call name='edit'> next",
    )
    _write_attempt(
        run_root,
        "repo_c__task-004",
        {
            "delegation_observed": True,
            "steps": [
                {
                    "finish_reason": "tool_calls",
                    "tools_called": ["task:orchestrator"],
                }
            ],
            "step_count": 1,
            "tool_call_count": 1,
        },
    )
    _write_attempt(
        run_root,
        "repo_d__task-005",
        {
            "steps": [{"finish_reason": "length", "completion_tokens": 32768}],
            "step_count": 1,
        },
    )
    _write_attempt(
        run_root,
        "repo_e__task-006",
        {},
        stdout="I verified there is no viable fix for this task.",
    )
    output_dir = tmp_path / "post-first-pass"
    summary = classify_run(
        run_root=run_root,
        task_list_path=FIXTURE,
        output_dir=output_dir,
        run_id="unit-run",
    )
    return run_root, output_dir, summary


def test_classification_closed_set_and_continuation_ids(tmp_path: Path) -> None:
    _, output_dir, summary = _classification_fixture(tmp_path)
    rows = [
        json.loads(line)
        for line in (output_dir / "attempt-classification.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    by_id = {row["instance_id"]: row for row in rows}

    assert summary["attempt_class_closed_set"] == list(ATTEMPT_CLASS_CLOSED_SET)
    assert by_id["repo_a__task-002"]["class"] == CLASS_STOPPED_NO_PATCH
    assert by_id["repo_b__task-003"]["class"] == CLASS_MALFORMED_TOOL_PROSE_NO_PATCH
    assert by_id["repo_c__task-004"]["class"] == CLASS_DELEGATED_OR_MIDSTREAM_NO_PATCH
    assert by_id["repo_d__task-005"]["class"] == CLASS_OUTPUT_LENGTH_NO_PATCH
    assert by_id["repo_e__task-006"]["class"] == CLASS_EXPLICIT_NO_FIX

    for row in rows:
        assert set(summary["evidence_fields"]).issubset(row["evidence"])

    assert (output_dir / "continuation-eligible-ids.txt").read_text(
        encoding="utf-8"
    ).splitlines() == [
        "repo_a__task-002",
        "repo_b__task-003",
        "repo_c__task-004",
        "repo_d__task-005",
    ]
    assert (output_dir / "continuation-excluded-ids.txt").read_text(
        encoding="utf-8"
    ).splitlines() == ["repo_a__task-001", "repo_e__task-006"]


def test_delegated_diagnostics_bundle_excludes_worktrees_and_trace(
    tmp_path: Path,
) -> None:
    run_root, output_dir, _ = _classification_fixture(tmp_path)
    delegated = run_root / "attempts" / "repo_c__task-004"
    (delegated / "worktree").mkdir()
    atomic_write_text(delegated / "filesystem-trace.log", "must not be bundled\n")
    log_dir = delegated / "oco-home" / ".local" / "state" / "oco"
    log_dir.mkdir(parents=True)
    atomic_write_text(log_dir / "oco.log", "diagnostic log tail\n")
    db_dir = delegated / "oco-home" / ".local" / "share" / "oco"
    db_dir.mkdir(parents=True)
    atomic_write_text(db_dir / "state.db", "sqlite bytes\n")

    manifest = package_delegated_diagnostics(
        run_root=run_root,
        classification_path=output_dir / "attempt-classification.json",
        output_dir=tmp_path / "diagnostics",
    )

    bundle_root = tmp_path / "diagnostics" / "delegated-midstream-diagnostics"
    assert manifest["attempt_ids"] == ["repo_c__task-004"]
    assert not (bundle_root / "attempts" / "repo_c__task-004" / "worktree").exists()
    assert not (
        bundle_root / "attempts" / "repo_c__task-004" / "filesystem-trace.log"
    ).exists()
    assert (
        bundle_root
        / "attempts"
        / "repo_c__task-004"
        / "oco-log-tails"
        / ".local"
        / "state"
        / "oco"
        / "oco.log.tail"
    ).exists()
    assert (
        bundle_root
        / "attempts"
        / "repo_c__task-004"
        / "oco-sqlite"
        / ".local"
        / "share"
        / "oco"
        / "state.db"
    ).exists()


def test_prepare_continuation_run_copies_state_without_first_pass_artifacts(
    tmp_path: Path,
) -> None:
    run_root, output_dir, _ = _classification_fixture(tmp_path)
    source = run_root / "attempts" / "repo_a__task-002"
    (source / "worktree").mkdir()
    atomic_write_text(source / "worktree" / "state.txt", "partial state\n")
    (source / "oco-home" / ".local" / "share" / "oco").mkdir(parents=True)
    atomic_write_text(
        source / "oco-home" / ".local" / "share" / "oco" / "state.db", "db\n"
    )

    continuation_root = tmp_path / "continuation"
    manifest = prepare_continuation_run(
        first_pass_run_root=run_root,
        classification_path=output_dir / "attempt-classification.json",
        output_run_root=continuation_root,
    )

    prepared = continuation_root / "attempts" / "repo_a__task-002"
    assert "repo_a__task-002" in manifest["prepared_ids"]
    assert (prepared / "worktree" / "state.txt").exists()
    assert (prepared / "oco-home" / ".local" / "share" / "oco" / "state.db").exists()
    assert not (prepared / "normalized.json").exists()
    assert AttemptPaths(continuation_root, "repo_a__task-002").marker_exists(
        Phase.SETUP
    )


def test_prepare_continuation_run_rejects_output_inside_first_pass_root(
    tmp_path: Path,
) -> None:
    run_root, output_dir, _ = _classification_fixture(tmp_path)

    with pytest.raises(RuntimeError, match="outside the first-pass run root"):
        prepare_continuation_run(
            first_pass_run_root=run_root,
            classification_path=output_dir / "attempt-classification.json",
            output_run_root=run_root / "continuation",
            force=True,
        )


def test_post_continuation_final_bundle_keeps_source_counts(tmp_path: Path) -> None:
    first_pass, output_dir, _ = _classification_fixture(tmp_path)
    continuation = tmp_path / "continuation-run"
    _write_attempt(continuation, "repo_a__task-002", {}, patch=PATCH)

    manifest = prepare_post_continuation_eval_bundle(
        first_pass_run_root=first_pass,
        continuation_run_root=continuation,
        task_list_path=FIXTURE,
        classification_path=output_dir / "attempt-classification.json",
        output_dir=tmp_path / "final-bundle",
        run_id="final-unit",
    )

    assert manifest["bundle_stage"] == "post_continuation_final"
    assert manifest["included_count"] == 2
    assert manifest["post_continuation"]["patch_source_counts"] == {
        "continuation": 1,
        "first_pass": 1,
    }
    assert manifest["bundle_candidate_denominator_note"].endswith(
        "per plan section 11.3."
    )


def test_infra_failure_pattern_ignores_benign_timestamp_and_token_numbers() -> None:
    """Regression: bare 3-digit numbers must not trigger infra-failure class.

    Phase-log lines carry Unix timestamps such as 1779532136.04 and token
    counts such as completion_tokens=512 that previously matched a loose
    ``5\\d\\d`` alternation and forced legitimate stopped/output-length
    attempts into ``subprocess_provider_infra_failure``.
    """

    benign_blobs = [
        '{"event":"BACKUP_NOOP","reason":"backup target not configured",'
        '"retryable":true,"success":true,"timestamp":1779532136.0461133}',
        '{"event":"step_finish","usage":{"completion_tokens":523,'
        '"prompt_tokens":12999}}',
        '{"event":"step_finish","usage":{"completion_tokens":429,'
        '"prompt_tokens":4290}}',
        '{"event":"PROGRESS","done":585,"clean":542,"bad":43}',
        '{"timestamp":1779532735.0261126,"event":"BACKUP_NOOP"}',
    ]
    for blob in benign_blobs:
        assert INFRA_FAILURE_PATTERN.search(blob) is None, (
            f"benign blob matched infra pattern: {blob!r}"
        )


def test_infra_failure_pattern_still_matches_real_provider_signals() -> None:
    real_signals = [
        "fetch failed: ECONNREFUSED 127.0.0.1:8000",
        "Error: ECONNRESET while reading response",
        "upstream timeout from vLLM",
        "vLLM error: engine core dead",
        "POST /v1/chat/completions HTTP/1.1 503 Service Unavailable",
        "status_code: 429",
        "HTTP/1.1 502 Bad Gateway",
        "provider error: Internal Server Error",
        "rate limited by upstream",
        "rate-limit exceeded",
        "connection refused on port 8000",
    ]
    for blob in real_signals:
        assert INFRA_FAILURE_PATTERN.search(blob) is not None, (
            f"real signal did not match infra pattern: {blob!r}"
        )


def test_attempt_with_timestamp_only_phase_log_is_not_classified_as_infra(
    tmp_path: Path,
) -> None:
    """End-to-end: phase-log noise alone must not force infra-failure class.

    Mirrors what we saw on the live H200 run: completed attempts with no
    patch, real step/tool work, and a phase-log full of BACKUP_NOOP timestamp
    entries were misclassified as ``subprocess_provider_infra_failure`` due
    to a loose ``5\\d\\d`` match against the Unix timestamps. After the fix
    they must fall through to a continuation-eligible class.
    """
    run_root = tmp_path / "first-pass"
    paths = AttemptPaths(run_root, "repo_a__task-002")
    paths.ensure()
    normalized = {
        "attempt_id": "repo_a__task-002",
        "run_id": "unit-run",
        "steps": [
            {
                "finish_reason": "stop",
                "completion_tokens": 523,
                "tools_called": ["task:orchestrator"],
            }
        ],
        "step_count": 1,
        "tool_call_count": 1,
        "no_patch": True,
        "queued_for_evaluation": False,
        "precheck_failed": False,
        "delegation_observed": True,
    }
    atomic_write_json(paths.normalized_path, normalized)
    atomic_write_text(paths.patch_path, "")
    atomic_write_json(
        paths.attempt_dir / "oco-subprocess.json",
        {"returncode": 0, "timed_out": False, "timeout_seconds": 7200.0},
    )
    atomic_write_text(
        paths.attempt_dir / "phase-log.jsonl",
        '{"event":"BACKUP_NOOP","reason":"backup target not configured",'
        '"retryable":true,"success":true,"timestamp":1779532136.0461133}\n'
        '{"event":"BACKUP_NOOP","reason":"backup target not configured",'
        '"retryable":true,"success":true,"timestamp":1779532734.8661547}\n',
    )
    paths.write_phase_marker(Phase.DONE)

    output_dir = tmp_path / "classify"
    classify_run(
        run_root=run_root,
        task_list_path=FIXTURE,
        output_dir=output_dir,
        run_id="unit-run",
    )

    by_id = {
        json.loads(line)["instance_id"]: json.loads(line)
        for line in (output_dir / "attempt-classification.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    }
    row = by_id["repo_a__task-002"]
    assert row["class"] != CLASS_SUBPROCESS_PROVIDER_INFRA_FAILURE
    assert row["continuation_eligible"] is True
