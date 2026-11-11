"""Jobs shared by the Lambda handler and the local CLI."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Optional

from .config import Config
from .ec2query import (
    DESCRIBE_LIMIT_CODE,
    DESCRIBE_LIMIT_PAUSE_MINUTES,
    ClientFactory,
    Counters,
    RateLimiter,
    discover,
    discovery_has_errors,
    supported_targets,
)
from .report import build_new_capacity_message, build_summary_message, build_test_message, prune, select_new
from .scanner import AVAILABLE, details_from, run_scan
from .timeutil import iso, parse_iso, utcnow

log = logging.getLogger(__name__)

DESCRIBE_LIMIT_PAUSE = timedelta(minutes=DESCRIBE_LIMIT_PAUSE_MINUTES)


def make_client_factory(cfg: Config) -> ClientFactory:
    import boto3
    from botocore.config import Config as BotoConfig

    boto_cfg = BotoConfig(
        # Throttling is handled by cbmon's own rate limiter and backoff.
        retries={"mode": "standard", "max_attempts": 2},
        max_pool_connections=8,
        connect_timeout=10,
        read_timeout=30,
        user_agent_extra="cb-monitor",
    )
    # One session for all clients: the EC2 service model (tens of MB) is then
    # loaded once instead of once per Region. Sessions are not thread-safe, so
    # clients are created under a lock; the clients themselves are thread-safe.
    session = boto3.session.Session()
    cache: dict[str, object] = {}
    lock = threading.Lock()

    def factory(region: str):
        with lock:
            if region not in cache:
                cache[region] = session.client("ec2", region_name=region, config=boto_cfg)
            return cache[region]

    return factory


def make_limiters(cfg: Config) -> dict[str, RateLimiter]:
    return {r: RateLimiter(cfg.api_calls_per_minute, burst=3) for r in cfg.regions}


def _discovery_fresh(discovery: Optional[dict], cfg: Config, now: datetime) -> bool:
    if not discovery or discovery_has_errors(discovery):
        return False
    if set(discovery.get("regions", {})) != set(cfg.regions):
        return False
    for info in discovery["regions"].values():
        if info.get("status") == "ok" and set(info.get("types", {})) != set(cfg.instance_types):
            return False
    return now - parse_iso(discovery["at"]) < timedelta(hours=cfg.discovery_ttl_hours)


def scan_job(
    cfg: Config,
    store,
    notifier,
    client_factory: ClientFactory,
    home_region: str,
    owner: str,
    time_budget_s: float = 780.0,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    limiters: Optional[dict[str, RateLimiter]] = None,
    clock=time.monotonic,
) -> dict:
    """Run one scan, notify about new capacity, persist state.

    With ``dry_run`` nothing is published or saved; messages go to the log.
    """
    now = now or utcnow()
    deadline = clock() + time_budget_s
    limiters = limiters or make_limiters(cfg)
    if not store.acquire_lock(owner, int(time_budget_s) + 120, now):
        log.warning("another scan holds the lock; skipping")
        return {"status": "skipped", "reason": "locked"}
    try:
        state = store.load("state") or {}
        paused = {r: t for r, t in state.get("paused", {}).items() if parse_iso(t) > now}
        discovery = state.get("discovery")
        discovery_counters = Counters()
        if not _discovery_fresh(discovery, cfg, now):
            discovery = discover(
                cfg.regions, cfg.instance_types, client_factory, home_region, discovery_counters,
                limiters, deadline, now,
            )
            log.info("discovery: %s", discovery)
        targets = supported_targets(discovery, cfg.regions, cfg.instance_types)
        log.info("targets: %s, paused regions: %s", targets, paused or "none")

        previous = state.get("details", {})
        result = run_scan(
            cfg, targets, client_factory, previous, limiters, deadline, now,
            paused_regions=tuple(paused), clock=clock,
        )
        counters = dict(result.counters)
        for name, value in discovery_counters.snapshot().items():
            counters[name] = counters.get(name, 0) + value
        for region, reason in result.stopped.items():
            if DESCRIBE_LIMIT_CODE in reason and region not in paused:
                paused[region] = iso(now + DESCRIBE_LIMIT_PAUSE)
                log.warning("%s hit the describe limit; pausing it until %s", region, paused[region])
        statuses: dict[str, int] = {}
        for week in result.weeks:
            statuses[week.status] = statuses.get(week.status, 0) + 1
        log.info(
            "scan finished in %.1fs complete=%s week statuses=%s counters=%s stopped=%s",
            result.duration_s, result.complete, statuses, counters, result.stopped,
        )

        old_notified = state.get("notified", {})
        new_cells, notified = select_new(result.cells, old_notified, now, cfg.reappear_after_minutes)
        published = 0
        if new_cells:
            subject, body = build_new_capacity_message(new_cells, cfg, now)
            if dry_run:
                log.info("dry run, not publishing:\n%s\n%s", subject, body)
            elif not notifier.has_subscribers():
                # Keep them "new" so the first subscriber receives them.
                log.warning("no confirmed subscription on the topic; holding %d new cells", len(new_cells))
                notified = old_notified
                counters["notify_held"] = counters.get("notify_held", 0) + 1
            else:
                try:
                    notifier.publish(subject, body)
                    published = 1
                except Exception:
                    log.exception("publish failed; these cells will be notified next run")
                    notified = old_notified
                    counters["notify_errors"] = counters.get("notify_errors", 0) + 1

        summary = {
            "status": "ok",
            "complete": result.complete,
            "duration_s": result.duration_s,
            "targets": len(targets),
            "weeks": statuses,
            "new_cells": len(new_cells),
            "published": published,
            "counters": counters,
            "stopped": result.stopped,
            "limits": {k: v for k, v in result.limits.items() if any(v.values())},
        }
        if dry_run:
            summary["dry_run"] = True
            return summary

        store.save(
            "state",
            {
                "discovery": discovery,
                "notified": prune(notified, now),
                "details": details_from(result, previous, now),
                "paused": paused,
            },
        )
        store.save(
            "snapshot",
            {
                "scan": {
                    "started": result.started,
                    "finished": result.finished,
                    "duration_s": result.duration_s,
                    "complete": result.complete,
                    "window_start": result.window_start,
                    "window_end": result.window_end,
                    "statuses": statuses,
                    "counters": counters,
                    "stopped": result.stopped,
                    "errors": result.errors,
                },
                "discovery": discovery,
                "limits": result.limits,
                "paused": paused,
                "weeks": [asdict(w) for w in result.weeks if w.status == AVAILABLE],
            },
        )
        store.add_stats(
            now,
            {
                "runs": 1,
                "runs_complete": int(result.complete),
                "api_calls": counters.get("api_calls", 0),
                "api_errors": counters.get("api_errors", 0),
                "throttled": counters.get("throttled", 0),
                "describe_limit_exceeded": counters.get("describe_limit_exceeded", 0),
                "notifications": published,
            },
        )
        return summary
    finally:
        store.release_lock(owner)


def summary_job(cfg: Config, store, notifier, dry_run: bool = False, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    snapshot = store.load("snapshot")
    stats = store.get_stats(now, 24)
    subject, body = build_summary_message(snapshot, stats, cfg, now)
    if dry_run:
        log.info("dry run, not publishing:\n%s\n%s", subject, body)
        return {"status": "ok", "dry_run": True, "subject": subject}
    message_id = notifier.publish(subject, body)
    return {"status": "ok", "subject": subject, "message_id": message_id}


def test_job(cfg: Config, notifier, now: Optional[datetime] = None) -> dict:
    subject, body = build_test_message(cfg, now or utcnow())
    return {"status": "ok", "message_id": notifier.publish(subject, body)}
