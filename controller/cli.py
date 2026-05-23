"""Small CLI used by the dry-run gate to exercise controller resume."""

from __future__ import annotations

import argparse
from pathlib import Path

from controller.constants import QWEN_OUTPUT_TOKEN_LIMIT
from controller.core import AttemptSpec, BenchmarkController, ControllerConfig
from controller.fixtures import FixtureOCOAdapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the dry-run benchmark controller fixture."
    )
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempts", nargs="+", required=True)
    parser.add_argument("--slow-attempt", action="append", default=[])
    parser.add_argument("--slow-seconds", type=float, default=0.0)
    parser.add_argument("--guard-attempt", action="append", default=[])
    parser.add_argument("--no-patch-attempt", action="append", default=[])
    parser.add_argument("--force-rerun", action="append", default=[])
    parser.add_argument("--lease-stale-after", type=float, default=300.0)
    parser.add_argument("--backup-destination", type=Path)
    parser.add_argument("--backup-no-create", action="store_true")
    parser.add_argument("--backup-ssh-host")
    parser.add_argument("--backup-ssh-user")
    parser.add_argument("--backup-ssh-target-dir")
    parser.add_argument("--backup-ssh-key-path", type=Path)
    parser.add_argument("--backup-ssh-port", type=int)
    parser.add_argument("--backup-bandwidth-limit-kbps", type=int)
    parser.add_argument("--backup-timeout-seconds", type=int, default=30)
    parser.add_argument(
        "--adapter-kind", choices=("fixture", "real"), default="fixture"
    )
    parser.add_argument("--production-config-dir", type=Path)
    parser.add_argument("--oco-binary", default="oco")
    parser.add_argument("--model-name", default="selfhost-qwen")
    parser.add_argument("--endpoint-url")
    parser.add_argument("--primary-agent")
    parser.add_argument(
        "--output-token-limit", type=int, default=QWEN_OUTPUT_TOKEN_LIMIT
    )
    parser.add_argument("--disable-boundary", action="store_true")
    parser.add_argument("--continuation-mode", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    adapter = None
    if args.adapter_kind == "fixture":
        adapter = FixtureOCOAdapter(
            slow_attempt_ids=set(args.slow_attempt),
            slow_seconds=args.slow_seconds,
            guard_attempt_ids=set(args.guard_attempt),
            no_patch_attempt_ids=set(args.no_patch_attempt),
        )
    config = ControllerConfig(
        run_root=args.run_root,
        run_id=args.run_id,
        lease_stale_after_seconds=args.lease_stale_after,
        force_rerun=set(args.force_rerun),
        backup_destination=args.backup_destination,
        backup_create_destination=not args.backup_no_create,
        backup_ssh_host=args.backup_ssh_host,
        backup_ssh_user=args.backup_ssh_user,
        backup_ssh_target_dir=args.backup_ssh_target_dir,
        backup_ssh_key_path=args.backup_ssh_key_path,
        backup_ssh_port=args.backup_ssh_port,
        backup_bandwidth_limit_kbps=args.backup_bandwidth_limit_kbps,
        backup_timeout_seconds=args.backup_timeout_seconds,
        adapter_kind=args.adapter_kind,
        production_config_dir=args.production_config_dir,
        oco_binary=args.oco_binary,
        model_name=args.model_name,
        endpoint_url=args.endpoint_url,
        primary_agent=args.primary_agent,
        output_token_limit=args.output_token_limit,
        disable_boundary=args.disable_boundary,
        continuation_mode=args.continuation_mode,
    )
    controller = BenchmarkController(config, adapter=adapter)
    controller.run_attempts([AttemptSpec(attempt_id) for attempt_id in args.attempts])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
