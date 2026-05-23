#!/usr/bin/env python3
"""Post-first-pass classification, continuation, diagnostics, and bundle prep."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from controller.post_first_pass import (  # noqa: E402
    classify_run,
    package_delegated_diagnostics,
    prepare_continuation_run,
    prepare_post_continuation_eval_bundle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify = subparsers.add_parser(
        "classify", help="Classify first-pass attempts and emit continuation ID files."
    )
    classify.add_argument("--run-root", required=True, type=Path)
    classify.add_argument("--run-id", required=True)
    classify.add_argument("--task-list", required=True, type=Path)
    classify.add_argument("--output-dir", required=True, type=Path)

    continuation = subparsers.add_parser(
        "prepare-continuation-run",
        help="Copy eligible attempt state into a separate continuation run root.",
    )
    continuation.add_argument("--first-pass-run-root", required=True, type=Path)
    continuation.add_argument("--classification", required=True, type=Path)
    continuation.add_argument("--output-run-root", required=True, type=Path)
    continuation.add_argument("--force", action="store_true")

    diagnostics = subparsers.add_parser(
        "package-delegated-diagnostics",
        help="Package delegated/midstream no-patch diagnostics without worktrees.",
    )
    diagnostics.add_argument("--run-root", required=True, type=Path)
    diagnostics.add_argument("--classification", required=True, type=Path)
    diagnostics.add_argument("--output-dir", required=True, type=Path)
    diagnostics.add_argument("--archive-path", type=Path)

    final_bundle = subparsers.add_parser(
        "prepare-final-bundle",
        help="Prepare a post-continuation final evaluator bundle.",
    )
    final_bundle.add_argument("--first-pass-run-root", required=True, type=Path)
    final_bundle.add_argument("--continuation-run-root", required=True, type=Path)
    final_bundle.add_argument("--task-list", required=True, type=Path)
    final_bundle.add_argument("--task-manifest", type=Path)
    final_bundle.add_argument("--classification", type=Path)
    final_bundle.add_argument("--output-dir", required=True, type=Path)
    final_bundle.add_argument("--run-id", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "classify":
        summary = classify_run(
            run_root=args.run_root,
            task_list_path=args.task_list,
            output_dir=args.output_dir,
            run_id=args.run_id,
        )
        print(
            "Classified "
            f"{summary['total_attempts']} attempts: "
            f"eligible={summary['continuation_eligible_count']} "
            f"excluded={summary['continuation_excluded_count']}"
        )
        return 0
    if args.command == "prepare-continuation-run":
        manifest = prepare_continuation_run(
            first_pass_run_root=args.first_pass_run_root,
            classification_path=args.classification,
            output_run_root=args.output_run_root,
            force=args.force,
        )
        print(
            f"Prepared {manifest['prepared_count']} continuation attempts "
            f"({manifest['missing_state_count']} missing copied state)"
        )
        return 0
    if args.command == "package-delegated-diagnostics":
        manifest = package_delegated_diagnostics(
            run_root=args.run_root,
            classification_path=args.classification,
            output_dir=args.output_dir,
            archive_path=args.archive_path,
        )
        archive = (
            f" archive={manifest['archive_path']}" if "archive_path" in manifest else ""
        )
        print(
            f"Packaged {manifest['attempt_count']} delegated/midstream attempts{archive}"
        )
        return 0
    if args.command == "prepare-final-bundle":
        manifest = prepare_post_continuation_eval_bundle(
            first_pass_run_root=args.first_pass_run_root,
            continuation_run_root=args.continuation_run_root,
            task_list_path=args.task_list,
            task_manifest_path=args.task_manifest,
            classification_path=args.classification,
            output_dir=args.output_dir,
            run_id=args.run_id,
        )
        print(
            f"Wrote final bundle to {args.output_dir}: "
            f"included={manifest['included_count']} excluded={manifest['excluded_count']}"
        )
        return 0
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
