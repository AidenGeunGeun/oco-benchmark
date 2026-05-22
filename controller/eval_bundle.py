"""Prepare SWE-bench Pro evaluator bundles from completed attempts."""

from __future__ import annotations

import hashlib
import ast
import importlib.util
import io
import json
import sys
import types
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from controller.atomic import atomic_write_json, atomic_write_text
from controller.pro_tasks import read_task_list


UPSTREAM_EVALUATOR_REFERENCE: dict[str, str] = {
    "repository_url": "https://github.com/scaleapi/SWE-bench_Pro-os",
    "commit": "ca10a60a5fcae51e6948ffe1485d4153d421e6c5",
    "readme_url": "https://raw.githubusercontent.com/scaleapi/SWE-bench_Pro-os/ca10a60a5fcae51e6948ffe1485d4153d421e6c5/README.md",
    "evaluator_source_url": "https://raw.githubusercontent.com/scaleapi/SWE-bench_Pro-os/ca10a60a5fcae51e6948ffe1485d4153d421e6c5/swe_bench_pro_eval.py",
    "patch_helper_url": "https://raw.githubusercontent.com/scaleapi/SWE-bench_Pro-os/ca10a60a5fcae51e6948ffe1485d4153d421e6c5/helper_code/extract_gold_patches.py",
    "format_summary": "patches.json is a JSON array with instance_id, patch, and prefix; raw_sample.jsonl carries the matching dataset rows.",
}

PATCHES_FILENAME = "patches.json"
RAW_SAMPLE_FILENAME = "raw_sample.jsonl"
MANIFEST_FILENAME = "manifest.json"
BUNDLE_HASH_ALGORITHM = "sha256"

EXCLUSION_REASONS: tuple[str, ...] = (
    "precheck_failed",
    "no_patch",
    "attempt_incomplete",
    "attempt_missing",
    "artifact_inconsistent",
)


class EvalBundleError(RuntimeError):
    """Raised when bundle preparation cannot complete safely."""


@dataclass(frozen=True)
class AttemptBundleDecision:
    instance_id: str
    repo: str
    included: bool
    reason: str | None
    patch_text: str | None = None
    normalized: dict[str, Any] | None = None


def prepare_eval_bundle(
    *,
    run_root: Path,
    task_list_path: Path,
    output_dir: Path,
    run_id: str,
    task_manifest_path: Path | None = None,
    conformance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tasks = read_task_list(task_list_path)
    task_manifest = _load_json(task_manifest_path) if task_manifest_path else None
    decisions = [
        decide_attempt(run_root=run_root, task=task)
        for task in sorted(tasks, key=lambda item: item["instance_id"])
    ]

    included_tasks = [
        (task, decision)
        for task, decision in zip(
            sorted(tasks, key=lambda item: item["instance_id"]), decisions, strict=True
        )
        if decision.included
    ]
    patch_rows = [
        {
            "instance_id": decision.instance_id,
            "patch": decision.patch_text or "",
            "prefix": run_id,
        }
        for _, decision in included_tasks
    ]
    raw_rows = [to_upstream_raw_sample_row(task) for task, _ in included_tasks]

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
        tasks=sorted(tasks, key=lambda item: item["instance_id"]),
        run_id=run_id,
        task_manifest=task_manifest,
        conformance=conformance,
    )
    manifest["integrity"] = compute_bundle_integrity(output_dir)
    atomic_write_json(output_dir / MANIFEST_FILENAME, manifest)
    return manifest


def decide_attempt(*, run_root: Path, task: dict[str, Any]) -> AttemptBundleDecision:
    instance_id = str(task["instance_id"])
    repo = str(task.get("repo", "unknown"))
    attempt_dir = run_root / "attempts" / instance_id
    if not attempt_dir.exists():
        return AttemptBundleDecision(instance_id, repo, False, "attempt_missing")

    normalized_path = attempt_dir / "normalized.json"
    if not normalized_path.exists():
        return AttemptBundleDecision(instance_id, repo, False, "attempt_incomplete")
    try:
        normalized = json.loads(normalized_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AttemptBundleDecision(instance_id, repo, False, "attempt_incomplete")

    if normalized.get("queued_for_evaluation") is True:
        patch_path = attempt_dir / "patch.diff"
        try:
            patch_text = patch_path.read_text(encoding="utf-8")
        except OSError:
            return AttemptBundleDecision(
                instance_id, repo, False, "artifact_inconsistent", normalized=normalized
            )
        if not patch_text.strip():
            return AttemptBundleDecision(
                instance_id, repo, False, "artifact_inconsistent", normalized=normalized
            )
        return AttemptBundleDecision(
            instance_id,
            repo,
            True,
            None,
            patch_text=patch_text,
            normalized=normalized,
        )

    if normalized.get("precheck_failed") is True:
        return AttemptBundleDecision(
            instance_id, repo, False, "precheck_failed", normalized=normalized
        )
    if normalized.get("no_patch") is True:
        return AttemptBundleDecision(
            instance_id, repo, False, "no_patch", normalized=normalized
        )
    return AttemptBundleDecision(
        instance_id, repo, False, "attempt_incomplete", normalized=normalized
    )


def to_upstream_raw_sample_row(task: dict[str, Any]) -> dict[str, Any]:
    """Convert canonical task-list rows back to the evaluator's raw-sample shape.

    The pinned evaluator reads JSONL with pandas and later calls eval() on these
    list-like fields, so they must be serialized as Python-list strings rather
    than JSON arrays.
    """

    row = dict(task)
    for field in (
        "fail_to_pass",
        "pass_to_pass",
        "selected_test_files_to_run",
        "issue_categories",
    ):
        value = row.get(field, [])
        if isinstance(value, str):
            continue
        if value is None:
            value = []
        row[field] = repr([str(item) for item in value])
    for key, value in list(row.items()):
        if value is None:
            row[key] = ""
    return row


def build_manifest(
    *,
    decisions: list[AttemptBundleDecision],
    tasks: list[dict[str, Any]],
    run_id: str,
    task_manifest: dict[str, Any] | None,
    conformance: dict[str, Any] | None,
) -> dict[str, Any]:
    excluded = [decision for decision in decisions if not decision.included]
    included = [decision for decision in decisions if decision.included]
    exclusion_counts = {reason: 0 for reason in EXCLUSION_REASONS}
    for decision in excluded:
        if decision.reason not in exclusion_counts:
            raise EvalBundleError(f"unexpected exclusion reason {decision.reason!r}")
        exclusion_counts[str(decision.reason)] += 1

    per_repo = _per_repo_breakdown(tasks, decisions)
    dataset = (task_manifest or {}).get("dataset") or {}
    return {
        "schema_version": 1,
        "run_id": run_id,
        "upstream_evaluator_reference": dict(UPSTREAM_EVALUATOR_REFERENCE),
        "dataset": dataset,
        "bundle_candidate_attempt_count": len(decisions),
        "bundle_candidate_denominator_note": "This is the number of attempts considered for this eval bundle only. The benchmark headline denominator remains the full 731 tasks per plan section 11.3.",
        "included_count": len(included),
        "excluded_count": len(excluded),
        "exclusion_reasons_closed_set": list(EXCLUSION_REASONS),
        "exclusion_counts": exclusion_counts,
        "per_repo_breakdown": per_repo,
        "included_instance_ids": [decision.instance_id for decision in included],
        "excluded_instances": [
            {
                "instance_id": decision.instance_id,
                "repo": decision.repo,
                "reason": decision.reason,
            }
            for decision in excluded
        ],
        "conformance": conformance
        or {
            "status": "not_run",
            "note": "Run scripts/validate_pro_eval_bundle.py to record the upstream-reference conformance check.",
        },
    }


def compute_bundle_integrity(bundle_dir: Path) -> dict[str, Any]:
    files = [PATCHES_FILENAME, RAW_SAMPLE_FILENAME]
    digest = hashlib.sha256()
    file_records: list[dict[str, Any]] = []
    for relative in sorted(files):
        path = bundle_dir / relative
        data = path.read_bytes()
        file_digest = hashlib.sha256(data).hexdigest()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\n")
        digest.update(data)
        digest.update(b"\n")
        file_records.append(
            {
                "path": relative,
                "size_bytes": len(data),
                "sha256": file_digest,
            }
        )
    return {
        "algorithm": BUNDLE_HASH_ALGORITHM,
        "canonical_input_ordering": "relative file paths sorted lexicographically; for each file hash path, newline, raw bytes, newline",
        "files": file_records,
        "bundle_digest": digest.hexdigest(),
    }


def recompute_bundle_digest(bundle_dir: Path, manifest: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for record in sorted(
        manifest["integrity"]["files"], key=lambda item: str(item["path"])
    ):
        relative = str(record["path"])
        data = (bundle_dir / relative).read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\n")
        digest.update(data)
        digest.update(b"\n")
    return digest.hexdigest()


def validate_bundle_conformance(
    *,
    bundle_dir: Path,
    upstream_checkout: Path | None = None,
    require_upstream: bool = False,
) -> dict[str, Any]:
    errors: list[str] = []
    patches_path = bundle_dir / PATCHES_FILENAME
    raw_path = bundle_dir / RAW_SAMPLE_FILENAME
    try:
        patches = json.loads(patches_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        patches = []
        errors.append(f"patches.json unreadable: {exc}")
    raw_rows = []
    try:
        raw_rows = [
            json.loads(line)
            for line in raw_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"raw_sample.jsonl unreadable: {exc}")

    if not isinstance(patches, list):
        errors.append("patches.json must be a JSON array")
        patches = []
    patch_ids = []
    for index, row in enumerate(patches):
        if not isinstance(row, dict):
            errors.append(f"patch row {index} is not an object")
            continue
        for field in ("instance_id", "patch", "prefix"):
            if not isinstance(row.get(field), str) or not row.get(field):
                errors.append(f"patch row {index} missing non-empty {field}")
        patch_ids.append(str(row.get("instance_id", "")))
    raw_ids = [
        str(row.get("instance_id", "")) for row in raw_rows if isinstance(row, dict)
    ]
    if patch_ids != raw_ids:
        errors.append("patches.json and raw_sample.jsonl instance_id order differ")
    for index, row in enumerate(raw_rows):
        if not isinstance(row, dict):
            errors.append(f"raw row {index} is not an object")
            continue
        for field in ("fail_to_pass", "pass_to_pass"):
            value = row.get(field)
            if not isinstance(value, str):
                errors.append(
                    f"raw row {index} field {field} must be a string for upstream eval() parsing"
                )
                continue
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError) as exc:
                errors.append(
                    f"raw row {index} field {field} is not a parseable list string: {exc}"
                )
                continue
            if not isinstance(parsed, list):
                errors.append(f"raw row {index} field {field} must parse to a list")

    upstream_present = False
    upstream_probe: dict[str, Any] | None = None
    if upstream_checkout is not None:
        upstream_present = (upstream_checkout / "swe_bench_pro_eval.py").exists() and (
            upstream_checkout / "README.md"
        ).exists()
        if upstream_present and not errors:
            upstream_probe = run_upstream_evaluator_schema_probe(
                bundle_dir=bundle_dir, upstream_checkout=upstream_checkout
            )
            if upstream_probe["status"] != "passed":
                errors.append(
                    "upstream evaluator schema probe failed: "
                    + str(upstream_probe.get("reason", "unknown"))
                )
    if require_upstream and not upstream_present:
        errors.append("pinned upstream checkout was required but not found")

    return {
        "status": "passed" if not errors else "failed",
        "mode": "upstream-evaluator-schema-probe"
        if upstream_present
        else "offline-structural",
        "upstream_evaluator_reference": dict(UPSTREAM_EVALUATOR_REFERENCE),
        "checked_files": [PATCHES_FILENAME, RAW_SAMPLE_FILENAME],
        "patch_count": len(patches),
        "upstream_probe": upstream_probe,
        "errors": errors,
    }


def run_upstream_evaluator_schema_probe(
    *, bundle_dir: Path, upstream_checkout: Path
) -> dict[str, Any]:
    """Run the pinned evaluator's own loader/result path with heavy eval stubbed.

    This executes `swe_bench_pro_eval.py` from the pinned checkout and lets its
    main() load raw_sample.jsonl, load patches.json, match instance_ids, read
    `patch`/`prefix`, and compute pass/fail from fail_to_pass/pass_to_pass. Only
    the Modal/Docker execution function is replaced with a tiny in-memory result
    so the conformance probe stays safe and cheap.
    """

    patches = json.loads((bundle_dir / PATCHES_FILENAME).read_text(encoding="utf-8"))
    if not patches:
        return {
            "status": "skipped",
            "reason": "bundle has no patches to probe against upstream evaluator",
        }
    evaluator_path = upstream_checkout / "swe_bench_pro_eval.py"
    if not evaluator_path.exists():
        return {"status": "failed", "reason": "swe_bench_pro_eval.py not found"}

    output_dir = bundle_dir / ".upstream-conformance-output"
    scripts_dir = bundle_dir / ".upstream-conformance-scripts"
    output_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)

    module_names = ("pandas", "tqdm", "helper_code", "helper_code.image_uri")
    previous_modules = {name: sys.modules.get(name) for name in module_names}
    previous_argv = list(sys.argv)
    stdout = io.StringIO()
    stderr = io.StringIO()
    module_name = "_oco_swe_bench_pro_eval_probe"
    try:
        _install_upstream_probe_stubs()
        spec = importlib.util.spec_from_file_location(module_name, evaluator_path)
        if spec is None or spec.loader is None:
            return {
                "status": "failed",
                "reason": "could not load evaluator module spec",
            }
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        module.eval_with_modal = _fake_upstream_eval
        module.eval_with_docker = _fake_upstream_eval
        sys.argv = [
            str(evaluator_path),
            "--raw_sample_path",
            str(bundle_dir / RAW_SAMPLE_FILENAME),
            "--patch_path",
            str(bundle_dir / PATCHES_FILENAME),
            "--output_dir",
            str(output_dir),
            "--scripts_dir",
            str(scripts_dir),
            "--num_workers",
            "1",
            "--dockerhub_username",
            "oco-conformance-probe",
        ]
        with redirect_stdout(stdout), redirect_stderr(stderr):
            module.main()
        result_path = output_dir / "eval_results.json"
        results = json.loads(result_path.read_text(encoding="utf-8"))
        if set(results) != {str(row["instance_id"]) for row in patches}:
            return {
                "status": "failed",
                "reason": "upstream evaluator did not produce one result per patch",
                "results": results,
            }
        return {
            "status": "passed",
            "executed": str(evaluator_path),
            "result_count": len(results),
            "stdout_tail": stdout.getvalue()[-500:],
            "stderr_tail": stderr.getvalue()[-500:],
        }
    except Exception as exc:  # noqa: BLE001 - result must be recorded, not raised.
        return {
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "stdout_tail": stdout.getvalue()[-500:],
            "stderr_tail": stderr.getvalue()[-500:],
        }
    finally:
        sys.argv = previous_argv
        sys.modules.pop(module_name, None)
        for name, module in previous_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _install_upstream_probe_stubs() -> None:
    pandas_module = types.ModuleType("pandas")
    pandas_module.read_json = _probe_read_json  # type: ignore[attr-defined]
    pandas_module.read_csv = _probe_read_json  # type: ignore[attr-defined]
    sys.modules["pandas"] = pandas_module

    tqdm_module = types.ModuleType("tqdm")
    tqdm_module.tqdm = _ProbeTqdm  # type: ignore[attr-defined]
    sys.modules["tqdm"] = tqdm_module

    helper_package = types.ModuleType("helper_code")
    image_uri_module = types.ModuleType("helper_code.image_uri")
    image_uri_module.get_dockerhub_image_uri = lambda *_, **__: "oco/probe:latest"  # type: ignore[attr-defined]
    sys.modules["helper_code"] = helper_package
    sys.modules["helper_code.image_uri"] = image_uri_module


def _probe_read_json(path: str | Path, *_: Any, **__: Any) -> "_ProbeFrame":
    rows = [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return _ProbeFrame(rows)


class _ProbeFrame:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.index: list[str] = []
        self.loc = _ProbeLoc(self)

    def fillna(self, value: str) -> "_ProbeFrame":
        self.rows = [
            {key: (value if child is None else child) for key, child in row.items()}
            for row in self.rows
        ]
        return self

    def set_index(self, field: str, *, drop: bool = False) -> "_ProbeFrame":
        del drop
        self.index = [str(row[field]) for row in self.rows]
        self.loc = _ProbeLoc(self)
        return self


class _ProbeLoc:
    def __init__(self, frame: _ProbeFrame) -> None:
        self.frame = frame

    def __getitem__(self, key: str) -> dict[str, Any]:
        for row in self.frame.rows:
            if str(row.get("instance_id")) == key:
                return row
        raise KeyError(key)


class _ProbeTqdm:
    def __init__(self, iterable: Iterable[Any], *_: Any, **__: Any) -> None:
        self.iterable = iterable
        self.description = ""

    def __iter__(self) -> Iterable[Any]:
        return iter(self.iterable)

    def set_description(self, description: str) -> None:
        self.description = description


def _fake_upstream_eval(
    patch: str,
    raw_sample: dict[str, Any],
    *_: Any,
    **__: Any,
) -> dict[str, Any]:
    if not isinstance(patch, str) or not patch:
        raise ValueError("patch was not passed through upstream evaluator call path")
    tests = [
        {"name": name, "status": "PASSED"}
        for name in ast.literal_eval(raw_sample["fail_to_pass"])
        + ast.literal_eval(raw_sample["pass_to_pass"])
    ]
    return {"tests": tests}


def record_conformance(bundle_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    manifest_path = bundle_dir / MANIFEST_FILENAME
    manifest = _load_json(manifest_path)
    manifest["conformance"] = result
    atomic_write_json(manifest_path, manifest)
    return manifest


def _per_repo_breakdown(
    tasks: Iterable[dict[str, Any]], decisions: Iterable[AttemptBundleDecision]
) -> dict[str, Any]:
    by_id = {decision.instance_id: decision for decision in decisions}
    result: dict[str, Any] = {}
    for task in tasks:
        repo = str(task.get("repo", "unknown"))
        decision = by_id[str(task["instance_id"])]
        entry = result.setdefault(
            repo,
            {
                "considered": 0,
                "included": 0,
                "excluded": 0,
                "excluded_by_reason": {reason: 0 for reason in EXCLUSION_REASONS},
            },
        )
        entry["considered"] += 1
        if decision.included:
            entry["included"] += 1
        else:
            entry["excluded"] += 1
            entry["excluded_by_reason"][str(decision.reason)] += 1
    return dict(sorted(result.items()))


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
