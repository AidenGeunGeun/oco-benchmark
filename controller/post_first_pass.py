"""Post-first-pass classification, continuation, and eval-bundle helpers.

The attempt class closed set is intentionally small and evaluator-blind:

- clean_patch
- stopped_no_patch
- malformed_tool_prose_no_patch
- delegated_or_midstream_no_patch
- output_length_no_patch
- explicit_no_fix
- precheck_failed
- timeout
- subprocess_provider_infra_failure
- incomplete
- unknown
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from controller.artifacts import AttemptPaths, RunPaths
from controller.atomic import atomic_write_json, atomic_write_text
from controller.constants import QWEN_OUTPUT_TOKEN_LIMIT
from controller.eval_bundle import (
    MANIFEST_FILENAME,
    PATCHES_FILENAME,
    RAW_SAMPLE_FILENAME,
    AttemptBundleDecision,
    EXCLUSION_REASONS,
    build_manifest,
    compute_bundle_integrity,
    decide_attempt,
    to_upstream_raw_sample_row,
)
from controller.phases import Phase
from controller.pro_tasks import read_task_list
from controller.telemetry import distribution_stats


CLASS_CLEAN_PATCH = "clean_patch"
CLASS_STOPPED_NO_PATCH = "stopped_no_patch"
CLASS_MALFORMED_TOOL_PROSE_NO_PATCH = "malformed_tool_prose_no_patch"
CLASS_DELEGATED_OR_MIDSTREAM_NO_PATCH = "delegated_or_midstream_no_patch"
CLASS_OUTPUT_LENGTH_NO_PATCH = "output_length_no_patch"
CLASS_EXPLICIT_NO_FIX = "explicit_no_fix"
CLASS_PRECHECK_FAILED = "precheck_failed"
CLASS_TIMEOUT = "timeout"
CLASS_SUBPROCESS_PROVIDER_INFRA_FAILURE = "subprocess_provider_infra_failure"
CLASS_INCOMPLETE = "incomplete"
CLASS_UNKNOWN = "unknown"

ATTEMPT_CLASS_CLOSED_SET: tuple[str, ...] = (
    CLASS_CLEAN_PATCH,
    CLASS_STOPPED_NO_PATCH,
    CLASS_MALFORMED_TOOL_PROSE_NO_PATCH,
    CLASS_DELEGATED_OR_MIDSTREAM_NO_PATCH,
    CLASS_OUTPUT_LENGTH_NO_PATCH,
    CLASS_EXPLICIT_NO_FIX,
    CLASS_PRECHECK_FAILED,
    CLASS_TIMEOUT,
    CLASS_SUBPROCESS_PROVIDER_INFRA_FAILURE,
    CLASS_INCOMPLETE,
    CLASS_UNKNOWN,
)

ATTEMPT_CLASS_DESCRIPTIONS: dict[str, str] = {
    CLASS_CLEAN_PATCH: "precheck-passing non-empty patch queued for evaluation",
    CLASS_STOPPED_NO_PATCH: "completed OCO attempt stopped without a captured patch",
    CLASS_MALFORMED_TOOL_PROSE_NO_PATCH: "no patch and evidence of prose/XML/JSON tool-call markup instead of a real tool call",
    CLASS_DELEGATED_OR_MIDSTREAM_NO_PATCH: "no patch after delegation or a tool-call/midstream stop",
    CLASS_OUTPUT_LENGTH_NO_PATCH: "no patch with finish-reason or token evidence of output-length truncation",
    CLASS_EXPLICIT_NO_FIX: "no patch because the agent explicitly concluded no viable fix",
    CLASS_PRECHECK_FAILED: "non-empty patch failed local apply/precheck and was not evaluator-ready",
    CLASS_TIMEOUT: "attempt timed out before producing an evaluator-ready patch",
    CLASS_SUBPROCESS_PROVIDER_INFRA_FAILURE: "OCO/provider/subprocess failed before an evaluator-ready patch",
    CLASS_INCOMPLETE: "attempt artifacts are missing or phase completion is incomplete",
    CLASS_UNKNOWN: "evidence is insufficient for any other closed-set class",
}

CONTINUATION_ELIGIBLE_CLASSES: tuple[str, ...] = (
    CLASS_STOPPED_NO_PATCH,
    CLASS_MALFORMED_TOOL_PROSE_NO_PATCH,
    CLASS_DELEGATED_OR_MIDSTREAM_NO_PATCH,
    CLASS_OUTPUT_LENGTH_NO_PATCH,
)

CLASSIFIER_EVIDENCE_FIELDS: tuple[str, ...] = (
    "patch_state",
    "step_count",
    "tool_count",
    "finish_reason",
    "output_length_signal",
    "delegation_signal",
    "subprocess_status",
    "timeout_flag",
    "evidence_excerpt",
    "evidence_artifact",
)

TOOL_MARKUP_PATTERN = re.compile(
    r"(<\/?(?:tool|function|invoke|tool_call|tool_use)\b|"
    r"<function_calls>|<tool_calls>|"
    r"```\s*(?:json|xml)?\s*[{<].*(?:tool|function)|"
    r"\bfunctions\.[A-Za-z_][\w.]*\s*\()",
    re.IGNORECASE | re.DOTALL,
)
NO_FIX_PATTERN = re.compile(
    r"\b(no viable fix|no fix possible|cannot fix|can't fix|unable to fix|"
    r"not possible to fix|not feasible to fix|give up)\b",
    re.IGNORECASE,
)
OUTPUT_LENGTH_PATTERN = re.compile(
    r"\b(length|max[_ -]?tokens?|output[_ -]?token|context[_ -]?length|truncat(?:ed|ion))\b",
    re.IGNORECASE,
)
INFRA_FAILURE_PATTERN = re.compile(
    r"\b(ECONNREFUSED|ETIMEDOUT|ECONNRESET|rate limit|429|5\d\d|"
    r"provider error|API error|Bad Gateway|Internal Server Error|"
    r"connection refused|vLLM error)\b",
    re.IGNORECASE,
)


def classify_run(
    *, run_root: Path, task_list_path: Path, output_dir: Path, run_id: str
) -> dict[str, Any]:
    tasks = sorted(read_task_list(task_list_path), key=lambda item: item["instance_id"])
    rows = [classify_attempt(run_root=run_root, task=task) for task in tasks]
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = build_classification_summary(
        rows=rows, run_root=run_root, task_list_path=task_list_path, run_id=run_id
    )
    atomic_write_json(output_dir / "attempt-classification.json", summary)
    atomic_write_text(
        output_dir / "attempt-classification.jsonl",
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
    )
    write_continuation_id_files(rows=rows, output_dir=output_dir)
    return summary


def classify_attempt(*, run_root: Path, task: dict[str, Any]) -> dict[str, Any]:
    attempt_id = str(task["instance_id"])
    repo = str(task.get("repo", "unknown"))
    paths = AttemptPaths(run_root, attempt_id)
    attempt_dir = paths.attempt_dir
    normalized_path = paths.normalized_path
    patch_path = paths.patch_path
    subprocess_path = attempt_dir / "oco-subprocess.json"

    normalized = _load_json(normalized_path)
    subprocess_record = _load_json(subprocess_path)
    patch_text = _read_text(patch_path)
    evidence_text, evidence_artifact = _combined_evidence_tail(attempt_dir)

    raw_steps = normalized.get("steps")
    steps: list[Any] = raw_steps if isinstance(raw_steps, list) else []
    step_count = int(normalized.get("step_count") or len(steps) or 0)
    tool_count = int(normalized.get("tool_call_count") or _count_step_tools(steps))
    finish_reason = _last_finish_reason(steps)
    patch_state = _patch_state(normalized, patch_path, patch_text)
    output_length_signal = _output_length_signal(
        steps=steps, finish_reason=finish_reason, evidence_text=evidence_text
    )
    delegation_signal = _delegation_signal(normalized, steps)
    midstream_signal = _midstream_signal(steps, tool_count)
    timeout_flag = (
        bool(subprocess_record.get("timed_out"))
        or "subprocess_timeout" in evidence_text
    )
    subprocess_status = _subprocess_status(subprocess_record, subprocess_path)

    evidence = {
        "patch_state": patch_state,
        "step_count": step_count,
        "tool_count": tool_count,
        "finish_reason": finish_reason,
        "output_length_signal": output_length_signal,
        "delegation_signal": delegation_signal,
        "subprocess_status": subprocess_status,
        "timeout_flag": timeout_flag,
        "evidence_excerpt": _excerpt(evidence_text),
        "evidence_artifact": evidence_artifact,
    }

    attempt_class = CLASS_UNKNOWN
    unknown_reason: str | None = None
    reason = ""

    if not attempt_dir.exists():
        attempt_class = CLASS_INCOMPLETE
        reason = "attempt directory is missing"
    elif patch_state["queued_for_evaluation"] and patch_state["patch_bytes"] > 0:
        attempt_class = CLASS_CLEAN_PATCH
        reason = "normalized.json queued a non-empty precheck-passing patch"
    elif timeout_flag:
        attempt_class = CLASS_TIMEOUT
        reason = "subprocess timeout flag or timeout event observed"
    elif _subprocess_failed(subprocess_record) or _infra_failure_signal(evidence_text):
        attempt_class = CLASS_SUBPROCESS_PROVIDER_INFRA_FAILURE
        reason = "subprocess/provider/infra failure signal observed"
    elif not normalized_path.exists():
        attempt_class = CLASS_INCOMPLETE
        reason = "normalized.json is missing"
    elif not paths.marker_exists(Phase.DONE):
        attempt_class = CLASS_INCOMPLETE
        reason = "DONE marker is missing"
    elif patch_state["precheck_failed"]:
        attempt_class = CLASS_PRECHECK_FAILED
        reason = str(patch_state.get("precheck_reason") or "patch precheck failed")
    elif patch_state["no_patch"] or patch_state["patch_bytes"] == 0:
        if _explicit_no_fix_signal(evidence_text):
            attempt_class = CLASS_EXPLICIT_NO_FIX
            reason = "agent explicitly concluded no viable fix"
        elif output_length_signal:
            attempt_class = CLASS_OUTPUT_LENGTH_NO_PATCH
            reason = "output-length finish reason or token cap signal observed"
        elif _malformed_tool_prose_signal(evidence_text):
            attempt_class = CLASS_MALFORMED_TOOL_PROSE_NO_PATCH
            reason = "tool-call-looking prose was emitted without a real patch"
        elif delegation_signal or midstream_signal:
            attempt_class = CLASS_DELEGATED_OR_MIDSTREAM_NO_PATCH
            reason = "delegation or midstream tool-call stop observed without a patch"
        else:
            attempt_class = CLASS_STOPPED_NO_PATCH
            reason = "completed attempt has no evaluator-ready patch"
    else:
        unknown_reason = "normalized state did not match queued, precheck-failed, or no-patch outcomes"
        reason = unknown_reason

    eligible = attempt_class in CONTINUATION_ELIGIBLE_CLASSES
    row = {
        "schema_version": 1,
        "instance_id": attempt_id,
        "repo": repo,
        "class": attempt_class,
        "class_label": ATTEMPT_CLASS_DESCRIPTIONS[attempt_class],
        "continuation_eligible": eligible,
        "reason": reason,
        "unknown_reason": unknown_reason,
        "evidence": evidence,
    }
    return row


def build_classification_summary(
    *, rows: list[dict[str, Any]], run_root: Path, task_list_path: Path, run_id: str
) -> dict[str, Any]:
    counts = Counter(str(row["class"]) for row in rows)
    counts_by_class = {name: counts.get(name, 0) for name in ATTEMPT_CLASS_CLOSED_SET}
    eligible_rows = [row for row in rows if row["continuation_eligible"]]
    excluded_rows = [row for row in rows if not row["continuation_eligible"]]
    step_counts = [int(row["evidence"].get("step_count") or 0) for row in rows]
    tool_counts = [int(row["evidence"].get("tool_count") or 0) for row in rows]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "source_run_root": str(run_root),
        "task_list_path": str(task_list_path),
        "total_attempts": len(rows),
        "attempt_class_closed_set": list(ATTEMPT_CLASS_CLOSED_SET),
        "attempt_class_descriptions": dict(ATTEMPT_CLASS_DESCRIPTIONS),
        "evidence_fields": list(CLASSIFIER_EVIDENCE_FIELDS),
        "counts_by_class": counts_by_class,
        "continuation_eligible_classes": list(CONTINUATION_ELIGIBLE_CLASSES),
        "continuation_eligible_count": len(eligible_rows),
        "continuation_excluded_count": len(excluded_rows),
        "continuation_eligible_ids": [row["instance_id"] for row in eligible_rows],
        "continuation_excluded_ids": [row["instance_id"] for row in excluded_rows],
        "turn_count_sensitivity": {
            "step_count_distribution": distribution_stats(step_counts),
            "tool_count_distribution": distribution_stats(tool_counts),
            "step_count_over_200": sum(value > 200 for value in step_counts),
            "tool_count_over_200": sum(value > 200 for value in tool_counts),
            "ids_with_step_count_over_200": [
                row["instance_id"]
                for row in rows
                if int(row["evidence"].get("step_count") or 0) > 200
            ],
            "ids_with_tool_count_over_200": [
                row["instance_id"]
                for row in rows
                if int(row["evidence"].get("tool_count") or 0) > 200
            ],
        },
    }


def write_continuation_id_files(
    *, rows: list[dict[str, Any]], output_dir: Path
) -> None:
    eligible = [row for row in rows if row["continuation_eligible"]]
    excluded = [row for row in rows if not row["continuation_eligible"]]
    atomic_write_text(
        output_dir / "continuation-eligible-ids.txt",
        "".join(str(row["instance_id"]) + "\n" for row in eligible),
    )
    atomic_write_text(
        output_dir / "continuation-excluded-ids.txt",
        "".join(str(row["instance_id"]) + "\n" for row in excluded),
    )
    by_class_dir = output_dir / "continuation-excluded-by-class"
    by_class_dir.mkdir(parents=True, exist_ok=True)
    for class_name in ATTEMPT_CLASS_CLOSED_SET:
        class_rows = [
            row
            for row in rows
            if row["class"] == class_name and not row["continuation_eligible"]
        ]
        atomic_write_text(
            by_class_dir / f"{class_name}.txt",
            "".join(str(row["instance_id"]) + "\n" for row in class_rows),
        )


def prepare_continuation_run(
    *,
    first_pass_run_root: Path,
    classification_path: Path,
    output_run_root: Path,
    force: bool = False,
) -> dict[str, Any]:
    _validate_continuation_output_root(first_pass_run_root, output_run_root)
    classification = _load_json(classification_path)
    rows = classification.get("rows")
    if not isinstance(rows, list):
        rows = _load_classification_jsonl(classification_path.with_suffix(".jsonl"))
    eligible_ids = [
        str(row["instance_id"]) for row in rows if row.get("continuation_eligible")
    ]

    run_paths = RunPaths(output_run_root)
    run_paths.ensure()
    prepared: list[str] = []
    missing_state: list[str] = []
    for attempt_id in eligible_ids:
        source = first_pass_run_root / "attempts" / attempt_id
        destination = output_run_root / "attempts" / attempt_id
        if destination.exists():
            if not force:
                raise RuntimeError(
                    f"continuation attempt already exists: {destination}"
                )
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        copied_any_state = False
        for name in ("worktree", "oco-home"):
            source_child = source / name
            if source_child.exists():
                shutil.copytree(source_child, destination / name, symlinks=True)
                copied_any_state = True
        if not (destination / "worktree").exists():
            missing_state.append(attempt_id)
            shutil.rmtree(destination, ignore_errors=True)
            continue
        paths = AttemptPaths(output_run_root, attempt_id)
        paths.write_phase_marker(Phase.SETUP)
        paths.append_phase_event(
            "CONTINUATION_SETUP_PREPARED",
            source_attempt_dir=str(source),
            copied_oco_home=(destination / "oco-home").exists(),
        )
        atomic_write_json(
            destination / "continuation-source.json",
            {
                "source_attempt_dir": str(source),
                "source_run_root": str(first_pass_run_root),
                "copied_existing_state": copied_any_state,
            },
        )
        prepared.append(attempt_id)

    manifest = {
        "schema_version": 1,
        "first_pass_run_root": str(first_pass_run_root),
        "output_run_root": str(output_run_root),
        "classification_path": str(classification_path),
        "output_token_limit_required": QWEN_OUTPUT_TOKEN_LIMIT,
        "eligible_count": len(eligible_ids),
        "prepared_count": len(prepared),
        "missing_state_count": len(missing_state),
        "eligible_ids": eligible_ids,
        "prepared_ids": prepared,
        "missing_state_ids": missing_state,
        "notes": [
            "First-pass artifacts are read-only inputs; continuation writes only to output_run_root.",
            "Use prepared-ids.txt for launching so attempts without copied worktree state are not rerun from scratch.",
        ],
    }
    atomic_write_json(output_run_root / "continuation-manifest.json", manifest)
    atomic_write_text(
        output_run_root / "prepared-ids.txt", "".join(item + "\n" for item in prepared)
    )
    atomic_write_text(
        output_run_root / "missing-state-ids.txt",
        "".join(item + "\n" for item in missing_state),
    )
    return manifest


def _validate_continuation_output_root(
    first_pass_run_root: Path, output_run_root: Path
) -> None:
    first_pass = first_pass_run_root.resolve()
    output = output_run_root.resolve()
    if output == first_pass or output.is_relative_to(first_pass):
        raise RuntimeError(
            "continuation output_run_root must be outside the first-pass run root"
        )
    if first_pass.is_relative_to(output):
        raise RuntimeError(
            "continuation output_run_root must not be a parent of the first-pass run root"
        )


def package_delegated_diagnostics(
    *,
    run_root: Path,
    classification_path: Path,
    output_dir: Path,
    archive_path: Path | None = None,
) -> dict[str, Any]:
    rows = _classification_rows_from_path(classification_path)
    delegated = [
        row for row in rows if row.get("class") == CLASS_DELEGATED_OR_MIDSTREAM_NO_PATCH
    ]
    bundle_root = output_dir / "delegated-midstream-diagnostics"
    if bundle_root.exists():
        shutil.rmtree(bundle_root)
    attempts_root = bundle_root / "attempts"
    attempts_root.mkdir(parents=True, exist_ok=True)

    included_files: list[str] = []
    for row in delegated:
        attempt_id = str(row["instance_id"])
        source = run_root / "attempts" / attempt_id
        target = attempts_root / attempt_id
        target.mkdir(parents=True, exist_ok=True)
        for name in (
            "normalized.json",
            "phase-log.jsonl",
            "oco-events.ndjson",
            "oco-subprocess.json",
            "patch.diff",
        ):
            path = source / name
            if path.exists():
                shutil.copy2(path, target / name)
                included_files.append(str((target / name).relative_to(bundle_root)))
        for name in ("oco-stdout.log", "oco-stderr.log"):
            path = source / name
            if path.exists():
                tail_path = target / f"{name}.tail"
                atomic_write_text(tail_path, _tail_text(path, max_bytes=20000))
                included_files.append(str(tail_path.relative_to(bundle_root)))
        log_target = target / "oco-log-tails"
        for log_path in _iter_oco_log_files(source / "oco-home"):
            relative = log_path.relative_to(source / "oco-home")
            tail_path = log_target / relative.with_suffix(relative.suffix + ".tail")
            atomic_write_text(tail_path, _tail_text(log_path, max_bytes=20000))
            included_files.append(str(tail_path.relative_to(bundle_root)))
        db_target = target / "oco-sqlite"
        for db_path in _iter_oco_sqlite_files(source / "oco-home"):
            relative = db_path.relative_to(source / "oco-home")
            copied = db_target / relative
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(db_path, copied)
            included_files.append(str(copied.relative_to(bundle_root)))

    manifest = {
        "schema_version": 1,
        "source_run_root": str(run_root),
        "classification_path": str(classification_path),
        "class_included": CLASS_DELEGATED_OR_MIDSTREAM_NO_PATCH,
        "attempt_count": len(delegated),
        "attempt_ids": [str(row["instance_id"]) for row in delegated],
        "exclusions": [
            "worktree/",
            "repo caches",
            "filesystem-trace.log",
            "broad raw logs outside explicit OCO stdout/stderr tails and OCO log tails",
        ],
        "included_files": sorted(included_files),
    }
    atomic_write_json(bundle_root / "manifest.json", manifest)
    if archive_path is not None:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(bundle_root, arcname=bundle_root.name)
        manifest["archive_path"] = str(archive_path)
        atomic_write_json(bundle_root / "manifest.json", manifest)
    return manifest


def prepare_post_continuation_eval_bundle(
    *,
    first_pass_run_root: Path,
    continuation_run_root: Path,
    task_list_path: Path,
    output_dir: Path,
    run_id: str,
    task_manifest_path: Path | None = None,
    classification_path: Path | None = None,
) -> dict[str, Any]:
    tasks = sorted(read_task_list(task_list_path), key=lambda item: item["instance_id"])
    task_manifest = _load_json(task_manifest_path) if task_manifest_path else None
    classification_summary = (
        _load_json(classification_path) if classification_path else {}
    )
    classification_rows = (
        _classification_rows_from_path(classification_path)
        if classification_path
        else []
    )
    class_by_id = {str(row["instance_id"]): row for row in classification_rows}

    decisions: list[AttemptBundleDecision] = []
    patch_sources: dict[str, str] = {}
    for task in tasks:
        instance_id = str(task["instance_id"])
        continuation = decide_attempt(run_root=continuation_run_root, task=task)
        first_pass = decide_attempt(run_root=first_pass_run_root, task=task)
        class_row = class_by_id.get(instance_id)
        continuation_allowed = not class_by_id or bool(
            class_row and class_row.get("continuation_eligible")
        )
        if continuation_allowed and continuation.included:
            decisions.append(continuation)
            patch_sources[instance_id] = "continuation"
            continue
        if first_pass.included:
            decisions.append(first_pass)
            patch_sources[instance_id] = "first_pass"
            continue
        reason = _final_exclusion_reason(continuation, first_pass, class_row)
        decisions.append(
            AttemptBundleDecision(
                instance_id=instance_id,
                repo=str(task.get("repo", "unknown")),
                included=False,
                reason=reason,
                normalized=continuation.normalized or first_pass.normalized,
            )
        )

    included = [decision for decision in decisions if decision.included]
    patch_rows = [
        {
            "instance_id": decision.instance_id,
            "patch": decision.patch_text or "",
            "prefix": run_id,
        }
        for decision in included
    ]
    raw_rows = [
        to_upstream_raw_sample_row(task)
        for task, decision in zip(tasks, decisions, strict=True)
        if decision.included
    ]
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / PATCHES_FILENAME, patch_rows)
    atomic_write_text(
        output_dir / RAW_SAMPLE_FILENAME,
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in raw_rows
        ),
    )

    manifest = build_manifest(
        decisions=decisions,
        tasks=tasks,
        run_id=run_id,
        task_manifest=task_manifest,
        conformance=None,
        bundle_stage="post_continuation_final",
        methodology_notes={
            "first_pass_and_post_continuation_are_reported_separately": True,
            "headline_denominator": 731,
            "continuation_policy": "uniform evaluator-blind continuation for predeclared eligible no-patch/stopped classes",
        },
        classification_summary=classification_summary,
    )
    source_counts = Counter(patch_sources.values())
    manifest["post_continuation"] = {
        "first_pass_run_root": str(first_pass_run_root),
        "continuation_run_root": str(continuation_run_root),
        "patch_source_counts": dict(sorted(source_counts.items())),
        "patch_sources_by_instance_id": dict(sorted(patch_sources.items())),
    }
    manifest["integrity"] = compute_bundle_integrity(output_dir)
    atomic_write_json(output_dir / MANIFEST_FILENAME, manifest)
    return manifest


def _patch_state(
    normalized: dict[str, Any], patch_path: Path, patch_text: str
) -> dict[str, Any]:
    return {
        "patch_exists": patch_path.exists(),
        "patch_bytes": len(patch_text.encode("utf-8")),
        "patch_non_empty": bool(patch_text.strip()),
        "queued_for_evaluation": bool(normalized.get("queued_for_evaluation")),
        "no_patch": bool(normalized.get("no_patch")) or not bool(patch_text.strip()),
        "precheck_passed": bool(normalized.get("precheck_passed")),
        "precheck_failed": bool(normalized.get("precheck_failed")),
        "precheck_reason": normalized.get("precheck_reason"),
    }


def _subprocess_status(record: dict[str, Any], path: Path) -> dict[str, Any]:
    return {
        "record_exists": path.exists(),
        "record_path": str(path) if path.exists() else None,
        "returncode": record.get("returncode"),
        "timed_out": bool(record.get("timed_out")),
        "timeout_seconds": record.get("timeout_seconds"),
        "filesystem_trace_enabled": record.get("filesystem_trace_enabled"),
        "output_token_env": record.get("output_token_env"),
    }


def _subprocess_failed(record: dict[str, Any]) -> bool:
    return record.get("returncode") not in (None, 0) and not bool(
        record.get("timed_out")
    )


def _last_finish_reason(steps: list[Any]) -> str | None:
    for step in reversed(steps):
        if isinstance(step, dict):
            return str(step.get("finish_reason") or "stop")
    return None


def _count_step_tools(steps: list[Any]) -> int:
    total = 0
    for step in steps:
        if isinstance(step, dict) and isinstance(step.get("tools_called"), list):
            total += len(step["tools_called"])
    return total


def _delegation_signal(normalized: dict[str, Any], steps: list[Any]) -> bool:
    if normalized.get("delegation_observed") is True:
        return True
    for step in steps:
        if not isinstance(step, dict):
            continue
        tools = step.get("tools_called")
        if isinstance(tools, list) and any(
            str(tool).startswith("task:") for tool in tools
        ):
            return True
    return False


def _midstream_signal(steps: list[Any], tool_count: int) -> bool:
    finish_reason = (_last_finish_reason(steps) or "").lower()
    return tool_count > 0 and finish_reason in {"tool", "tool_call", "tool_calls"}


def _output_length_signal(
    *, steps: list[Any], finish_reason: str | None, evidence_text: str
) -> bool:
    finish = (finish_reason or "").lower()
    if OUTPUT_LENGTH_PATTERN.search(finish):
        return True
    for step in steps:
        if not isinstance(step, dict):
            continue
        completion_tokens = int(step.get("completion_tokens") or 0)
        if completion_tokens >= 30000:
            return True
    return bool(OUTPUT_LENGTH_PATTERN.search(evidence_text))


def _explicit_no_fix_signal(text: str) -> bool:
    return bool(NO_FIX_PATTERN.search(text))


def _malformed_tool_prose_signal(text: str) -> bool:
    return bool(TOOL_MARKUP_PATTERN.search(text))


def _infra_failure_signal(text: str) -> bool:
    return bool(INFRA_FAILURE_PATTERN.search(text))


def _combined_evidence_tail(attempt_dir: Path) -> tuple[str, str | None]:
    candidates = [
        attempt_dir / "oco-stdout.log",
        attempt_dir / "oco-stderr.log",
        attempt_dir / "oco-events.ndjson",
        attempt_dir / "phase-log.jsonl",
    ]
    chunks: list[str] = []
    artifacts: list[str] = []
    for path in candidates:
        if not path.exists():
            continue
        chunks.append(f"\n--- {path.name} ---\n" + _tail_text(path, max_bytes=20000))
        artifacts.append(str(path))
    return "\n".join(chunks), artifacts[-1] if artifacts else None


def _excerpt(text: str, max_chars: int = 700) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[-max_chars:]


def _tail_text(path: Path, *, max_bytes: int = 12000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_classification_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _classification_rows_from_path(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    if path.suffix == ".jsonl":
        return _load_classification_jsonl(path)
    payload = _load_json(path)
    rows = payload.get("rows")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    jsonl_path = path.with_suffix(".jsonl")
    return _load_classification_jsonl(jsonl_path)


def _iter_oco_log_files(home: Path) -> Iterable[Path]:
    if not home.exists():
        return []
    roots = [home / ".local" / "state" / "oco", home / ".local" / "share" / "oco"]
    files: list[Path] = []
    for root in roots:
        if root.exists():
            files.extend(path for path in root.rglob("*.log") if path.is_file())
    return sorted(files)


def _iter_oco_sqlite_files(home: Path) -> Iterable[Path]:
    if not home.exists():
        return []
    roots = [home / ".local" / "share" / "oco", home / ".local" / "state" / "oco"]
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            name = path.name.lower()
            suffix = path.suffix.lower()
            if suffix in {".db", ".sqlite", ".sqlite3"} or name.endswith(
                (".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm")
            ):
                files.append(path)
    return sorted(files)


def _final_exclusion_reason(
    continuation: AttemptBundleDecision,
    first_pass: AttemptBundleDecision,
    class_row: dict[str, Any] | None,
) -> str:
    for reason in (continuation.reason, first_pass.reason):
        if reason in EXCLUSION_REASONS:
            return str(reason)
    if class_row and class_row.get("class") == CLASS_PRECHECK_FAILED:
        return "precheck_failed"
    if class_row and class_row.get("class") == CLASS_INCOMPLETE:
        return "attempt_incomplete"
    return "no_patch"
