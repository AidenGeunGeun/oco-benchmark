#!/usr/bin/env python3
"""Run the OCO SWE-bench Pro benchmark over a chosen set of tasks.

Reads a materialized SWE-bench Pro tasks JSONL (produced by
`materialize_pro_tasks.py --public`) and an optional task-IDs file.
Builds AttemptSpecs with full task data (repo, repo_url, base_commit,
prompt) and hands them to the BenchmarkController for sequential
execution.

Sequential by design: the controller does not (yet) support concurrent
attempts. Use this script for calibration; full-731 concurrency is a
future enhancement that will need to coordinate watermarks/leases/
mirror writes across multiple workers.

The task prompt is intentionally minimal — operational guardrails only,
no orchestration nudges. We measure the natural delegation rate of the
production OCO prompts; we do not steer the model toward delegation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from controller.core import AttemptSpec, BenchmarkController, ControllerConfig  # noqa: E402
from controller.repo_cache import RepoCacheManager  # noqa: E402


PROMPT_TEMPLATE = """You are working on an engineering task in a checked-out repository at the current working directory.

Operational rules (non-negotiable):
- This is a non-interactive run; do not ask clarifying questions.
- Stay inside the current repository directory.
- Do not read .env files or any secrets.
- Produce your fix as edits to files in the repository.
- The benchmark extracts your final answer from the working-tree diff (`git diff` plus any new untracked files). Do not run state-changing git commands (no stash, reset, clean, checkout, restore, switch, revert, commit, merge, or rebase) — they can destroy the patch. `git diff`, `git status`, `git show`, `git log` are fine for inspection.

Problem statement:

{problem_statement}
"""


def build_prompt(task_row: dict) -> str:
    return PROMPT_TEMPLATE.format(problem_statement=task_row["problem_statement"])


def load_tasks(path: Path) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        by_id[row["instance_id"]] = row
    return by_id


def load_ids(path: Path) -> list[str]:
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            ids.append(stripped)
    # Deduplicate while preserving first-seen order
    seen: set[str] = set()
    deduped: list[str] = []
    for tid in ids:
        if tid not in seen:
            seen.add(tid)
            deduped.append(tid)
    return deduped


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tasks-file",
        required=True,
        type=Path,
        help="Materialized SWE-bench Pro tasks JSONL (output of materialize_pro_tasks.py --public).",
    )
    parser.add_argument(
        "--ids-file",
        type=Path,
        help="Optional task-ID list (one per line, # comments allowed). Defaults to all tasks in --tasks-file.",
    )
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--repo-cache-dir",
        required=True,
        type=Path,
        help="Bare-mirror cache directory shared across attempts.",
    )
    parser.add_argument(
        "--adapter-kind",
        choices=("fixture", "real"),
        default="real",
        help="Use 'real' to invoke OCO; 'fixture' for controller dry runs.",
    )
    parser.add_argument(
        "--production-config-dir",
        type=Path,
        required=True,
        help="Source of production OCO config + prompts for materialization.",
    )
    parser.add_argument("--oco-binary", default="oco")
    parser.add_argument("--model-name", default="selfhost-qwen")
    parser.add_argument(
        "--endpoint-url",
        required=True,
        help="OpenAI-compatible inference endpoint (e.g., http://localhost:8000/v1).",
    )
    parser.add_argument("--api-key")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=1800.0,
        help="Per-attempt OCO subprocess wall-time cap (default 1800s = 30 min).",
    )
    parser.add_argument("--force-rerun", action="append", default=[])
    parser.add_argument(
        "--disable-boundary",
        action="store_true",
        help="Skip strace-wrapped boundary proof. Use only when strace is unavailable.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    tasks = load_tasks(args.tasks_file)
    if not tasks:
        print(f"FAILED: no tasks loaded from {args.tasks_file}", file=sys.stderr)
        return 2

    if args.ids_file:
        ids = load_ids(args.ids_file)
        if not ids:
            print(f"FAILED: no task IDs loaded from {args.ids_file}", file=sys.stderr)
            return 2
    else:
        ids = sorted(tasks.keys())

    missing = [tid for tid in ids if tid not in tasks]
    if missing:
        print(
            f"FAILED: {len(missing)} task IDs from {args.ids_file} not present in {args.tasks_file}:",
            file=sys.stderr,
        )
        for mid in missing[:10]:
            print(f"  {mid}", file=sys.stderr)
        if len(missing) > 10:
            print(f"  ... and {len(missing) - 10} more", file=sys.stderr)
        return 3

    specs: list[AttemptSpec] = []
    for tid in ids:
        row = tasks[tid]
        specs.append(
            AttemptSpec(
                attempt_id=tid,
                prompt=build_prompt(row),
                base_commit=row["base_commit"],
                repo=row["repo"],
                repo_url=row["repo_url"],
                task_row=row,
            )
        )

    config = ControllerConfig(
        run_root=args.run_root,
        run_id=args.run_id,
        adapter_kind=args.adapter_kind,
        production_config_dir=args.production_config_dir,
        oco_binary=args.oco_binary,
        model_name=args.model_name,
        endpoint_url=args.endpoint_url,
        api_key=args.api_key,
        force_rerun=set(args.force_rerun),
        real_oco_timeout_seconds=args.timeout_seconds,
        disable_boundary=args.disable_boundary,
    )

    repo_cache = RepoCacheManager(
        cache_root=args.repo_cache_dir,
        # worktree_root is unused when the controller passes an explicit worktree_dir
        # (which it does in _setup), but the dataclass requires a value.
        worktree_root=args.repo_cache_dir / "_unused_worktree_root",
    )

    controller = BenchmarkController(config, repo_cache_manager=repo_cache)

    print(f"Running {len(specs)} attempts against run_id={args.run_id}")
    result = controller.run_attempts(specs)

    print()
    print(f"Completed (new): {len(result['completed'])}")
    print(f"Skipped (already DONE): {len(result['skipped'])}")
    if result["force_rerun"]:
        print(f"Force-rerun applied to: {len(result['force_rerun'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
