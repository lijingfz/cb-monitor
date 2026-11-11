"""AWS Lambda entry point.

Event payloads:
    {"mode": "scan"}               scheduled every N minutes
    {"mode": "summary"}            scheduled once a day
    {"mode": "test"}               send a test email
    {"mode": "scan", "dry_run": true}   scan without publishing or saving state
"""
from __future__ import annotations

import logging
import os

from .app import make_client_factory, scan_job, summary_job, test_job
from .config import Config
from .notifier import SnsNotifier
from .state import DynamoStore

logging.getLogger().setLevel(logging.INFO)
logging.getLogger("botocore").setLevel(logging.WARNING)

# Stop starting new queries this many seconds before the Lambda timeout.
SAFETY_MARGIN_S = 90


def lambda_handler(event, context):
    event = event or {}
    mode = event.get("mode", "scan")
    dry_run = bool(event.get("dry_run", False))
    cfg = Config.from_env()
    store = DynamoStore(cfg.table_name)
    notifier = SnsNotifier(cfg.topic_arn)

    if mode == "scan":
        budget = context.get_remaining_time_in_millis() / 1000 - SAFETY_MARGIN_S
        return scan_job(
            cfg,
            store,
            notifier,
            client_factory=make_client_factory(cfg),
            home_region=os.environ.get("AWS_REGION", "us-east-1"),
            owner=context.aws_request_id,
            time_budget_s=max(30.0, budget),
            dry_run=dry_run,
        )
    if mode == "summary":
        return summary_job(cfg, store, notifier, dry_run=dry_run)
    if mode == "test":
        return test_job(cfg, notifier)
    raise ValueError(f"unknown mode: {mode!r}")
