#!/usr/bin/env python3
"""Prepare a SWE-bench Pro evaluator bundle from a completed run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from controller.eval_bundle import prepare_eval_bundle, validate_bundle_conformance  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--task-list", required=True, type=Path)
    parser.add_argument("--task-manifest", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--record-conformance", action="store_true")
    parser.add_argument("--upstream-checkout", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conformance = None
    manifest = prepare_eval_bundle(
        run_root=args.run_root,
        task_list_path=args.task_list,
        task_manifest_path=args.task_manifest,
        output_dir=args.output_dir,
        run_id=args.run_id,
        conformance=conformance,
    )
    if args.record_conformance:
        conformance = validate_bundle_conformance(
            bundle_dir=args.output_dir, upstream_checkout=args.upstream_checkout
        )
        from controller.eval_bundle import record_conformance

        manifest = record_conformance(args.output_dir, conformance)
    print(
        f"Wrote eval bundle to {args.output_dir}: included={manifest['included_count']} excluded={manifest['excluded_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
