#!/usr/bin/env python3
"""Materialize a deterministic SWE-bench Pro task list."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from controller.pro_tasks import (  # noqa: E402
    DatasetMetadata,
    TaskLoadError,
    load_fixture_rows,
    load_public_rows,
    materialize_task_list,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--fixture",
        type=Path,
        help="Load an offline fixture JSON/JSONL instead of the pinned public dataset.",
    )
    parser.add_argument(
        "--public", action="store_true", help="Load the pinned public 731 dataset."
    )
    parser.add_argument("--page-size", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if bool(args.fixture) == bool(args.public):
        print("Choose exactly one of --fixture or --public.", file=sys.stderr)
        return 2
    try:
        rows = (
            load_fixture_rows(args.fixture)
            if args.fixture
            else load_public_rows(page_size=args.page_size)
        )
        manifest = materialize_task_list(
            rows,
            output_path=args.output,
            manifest_path=args.manifest,
            dataset=DatasetMetadata(expected_row_count=None)
            if args.fixture
            else DatasetMetadata(),
            require_expected_count=args.public,
        )
    except TaskLoadError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    dataset = manifest["dataset"]
    print(
        f"Wrote {dataset['row_count']} tasks to {args.output} with {dataset['content_hash_algorithm']}={dataset['content_hash']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
