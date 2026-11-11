"""Run the monitor locally.

    PYTHONPATH=src python3 -m cbmon scan --dry-run
    PYTHONPATH=src python3 -m cbmon scan --min-start-offset-days 0
    PYTHONPATH=src python3 -m cbmon summary
    PYTHONPATH=src python3 -m cbmon discover

Configuration comes from the same environment variables as the Lambda
function (see README); the flags below override them. State is kept in
``--state-dir``. Messages are printed unless ``--topic-arn`` is given.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import replace

from .app import make_client_factory, make_limiters, scan_job, summary_job, test_job
from .config import Config, parse_durations, split_list
from .ec2query import Counters, discover
from .notifier import PrintNotifier, SnsNotifier
from .state import LocalStore


def _home_region() -> str:
    import boto3

    return boto3.session.Session().region_name or "us-east-1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cbmon", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["scan", "summary", "discover", "test"])
    parser.add_argument("--dry-run", action="store_true", help="scan: do not publish or save state")
    parser.add_argument("--state-dir", default=".state")
    parser.add_argument("--topic-arn", default=None, help="publish to this SNS topic instead of printing")
    parser.add_argument("--instance-types")
    parser.add_argument("--regions")
    parser.add_argument("--durations", help='e.g. "1-14,21-175/7"')
    parser.add_argument("--min-start-offset-days", type=float)
    parser.add_argument("--max-instance-count", type=int)
    parser.add_argument("--no-weekly-slices", action="store_true")
    parser.add_argument("--time-budget", type=float, default=780.0, help="seconds")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    cfg = Config.from_env()
    overrides = {}
    if args.instance_types:
        overrides["instance_types"] = split_list(args.instance_types)
    if args.regions:
        overrides["regions"] = split_list(args.regions)
    if args.durations:
        overrides["durations_days"] = parse_durations(args.durations)
    if args.min_start_offset_days is not None:
        overrides["min_start_offset_days"] = args.min_start_offset_days
    if args.max_instance_count is not None:
        overrides["max_instance_count"] = args.max_instance_count
    if args.no_weekly_slices:
        overrides["weekly_slices"] = False
    if args.topic_arn is not None:
        overrides["topic_arn"] = args.topic_arn
    cfg = replace(cfg, **overrides)
    cfg.validate()

    notifier = SnsNotifier(cfg.topic_arn) if cfg.topic_arn else PrintNotifier()
    store = LocalStore(args.state_dir)
    factory = make_client_factory(cfg)

    if args.mode == "discover":
        result = discover(
            cfg.regions, cfg.instance_types, factory, _home_region(), Counters(),
            make_limiters(cfg), time.monotonic() + args.time_budget,
        )
    elif args.mode == "scan":
        result = scan_job(
            cfg, store, notifier, factory, _home_region(), owner="cli",
            time_budget_s=args.time_budget, dry_run=args.dry_run,
        )
    elif args.mode == "summary":
        result = summary_job(cfg, store, notifier)
    else:
        result = test_job(cfg, notifier)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
