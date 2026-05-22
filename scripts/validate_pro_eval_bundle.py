#!/usr/bin/env python3
"""Run the pinned SWE-bench Pro bundle conformance check."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from controller.eval_bundle import record_conformance, validate_bundle_conformance  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--upstream-checkout", type=Path)
    parser.add_argument("--require-upstream", action="store_true")
    parser.add_argument("--record", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = validate_bundle_conformance(
        bundle_dir=args.bundle_dir,
        upstream_checkout=args.upstream_checkout,
        require_upstream=args.require_upstream,
    )
    if args.record:
        record_conformance(args.bundle_dir, result)
    print(
        f"Conformance {result['status']} ({result['mode']}), patches={result['patch_count']}"
    )
    for error in result.get("errors", []):
        print(f"- {error}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
