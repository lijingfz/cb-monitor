"""Calls to DescribeCapacityBlockOfferings.

The API has its own small request bucket per account and Region: 10 requests,
refilled at 0.15 per second (about 9 per minute), see
https://docs.aws.amazon.com/ec2/latest/devguide/ec2-api-throttling.html.
Every call therefore goes through a client-side ``RateLimiter`` that keeps the
monitor below that rate and leaves room for people using the console.

This module also:

* learns the account's search limits from the API's error messages and clamps
  later requests to them, so an account with a narrower window than the
  documented one (8 weeks ahead, up to 182 days) still works;
* finds the largest available instance count with a hinted binary search;
* discovers which Region / instance type pairs support Capacity Blocks.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable, Optional

from botocore.exceptions import BotoCoreError, ClientError

from .timeutil import iso, parse_java_date, utcnow

log = logging.getLogger(__name__)

_JAVA_DATE = r"[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} [A-Z]{3,4} \d{4}"
START_LIMIT_RE = re.compile(r"must be before (" + _JAVA_DATE + ")")
END_LIMIT_RE = re.compile(r"must not be later than (" + _JAVA_DATE + ")")
_AZ_RE = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d[a-z]$")

THROTTLE_CODES = {"Throttling", "ThrottlingException", "RequestLimitExceeded", "TooManyRequestsException"}
# A separate account-level quota on describe calls; retrying soon does not help.
DESCRIBE_LIMIT_CODE = "CapacityBlockDescribeLimitExceeded"
# How long a Region is left alone after hitting it (observed to last over 30 minutes).
DESCRIBE_LIMIT_PAUSE_MINUTES = 60
MAX_ATTEMPTS = 3

ClientFactory = Callable[[str], object]


class Deadline(Exception):
    """The run's time budget is used up."""


class Throttled(Exception):
    """Still throttled after backing off; stop using this Region for the run."""


def error_code(err: ClientError) -> str:
    return err.response.get("Error", {}).get("Code", "")


def error_message(err: ClientError) -> str:
    return err.response.get("Error", {}).get("Message", "")


class Counters:
    """Thread-safe counters for API usage."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, int] = {}

    def add(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._values[name] = self._values.get(name, 0) + n

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._values)


class RateLimiter:
    """Token bucket: ``burst`` requests at once, then ``per_minute`` per minute."""

    def __init__(self, per_minute: float, burst: int = 3, clock=time.monotonic, sleep=time.sleep) -> None:
        if per_minute <= 0 or burst < 1:
            raise ValueError("per_minute must be > 0 and burst >= 1")
        self.rate = per_minute / 60.0
        self.burst = burst
        self.tokens = float(burst)
        self.clock = clock
        self.sleep = sleep
        self.updated = clock()
        self.blocked_until = 0.0
        self._lock = threading.Lock()

    def acquire(self, deadline: float) -> bool:
        """Take one token, waiting if needed. False if that would pass ``deadline``."""
        while True:
            with self._lock:
                now = self.clock()
                if now > deadline:
                    return False
                self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if now >= self.blocked_until and self.tokens >= 1:
                    self.tokens -= 1
                    return True
                wait = max(self.blocked_until - now, (1 - self.tokens) / self.rate, 0.01)
            if now + wait > deadline:
                return False
            self.sleep(wait)

    def penalize(self, seconds: float) -> None:
        """After a throttling error: empty the bucket and pause."""
        with self._lock:
            self.tokens = 0.0
            self.updated = self.clock()
            self.blocked_until = max(self.blocked_until, self.updated + seconds)


def call_api(client, kwargs: dict, limiter: RateLimiter, deadline: float, counters: Counters) -> dict:
    """One DescribeCapacityBlockOfferings request, paced, with backoff on throttling."""
    for attempt in range(MAX_ATTEMPTS):
        if not limiter.acquire(deadline):
            raise Deadline("time budget used up")
        counters.add("api_calls")
        try:
            return client.describe_capacity_block_offerings(**kwargs)
        except ClientError as err:
            code = error_code(err)
            if code == DESCRIBE_LIMIT_CODE:
                counters.add("describe_limit_exceeded")
                limiter.penalize(600)
                raise Throttled(code) from err
            if code not in THROTTLE_CODES:
                raise
            counters.add("throttled")
            log.warning("throttled (%s), backing off", code)
            limiter.penalize(20 * (attempt + 1))
        except BotoCoreError as err:  # connection problems
            counters.add("api_errors")
            log.warning("request failed (%s), retrying", err)
            limiter.penalize(5)
    raise Throttled("RequestLimitExceeded")


class TargetLimits:
    """Search limits learned for one (Region, instance type) during a run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.max_start: Optional[datetime] = None  # latest accepted StartDateRange
        self.max_end: Optional[datetime] = None  # latest accepted EndDateRange
        self.invalid_hours: set[int] = set()
        self.invalid_counts: set[int] = set()

    def learn_start(self, before: datetime) -> None:
        bound = before - timedelta(minutes=1)
        with self._lock:
            self.max_start = bound if self.max_start is None else min(self.max_start, bound)

    def learn_end(self, latest: datetime) -> None:
        bound = latest - timedelta(minutes=1)
        with self._lock:
            self.max_end = bound if self.max_end is None else min(self.max_end, bound)

    def reject_hours(self, hours: int) -> None:
        with self._lock:
            self.invalid_hours.add(hours)

    def reject_count(self, count: int) -> None:
        with self._lock:
            self.invalid_counts.add(count)

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "max_start": iso(self.max_start) if self.max_start else None,
                "max_end": iso(self.max_end) if self.max_end else None,
                "invalid_durations_days": sorted(h // 24 for h in self.invalid_hours),
                "invalid_counts": sorted(self.invalid_counts),
            }


class Querier:
    """Queries one (Region, instance type) and applies the learned limits."""

    def __init__(
        self,
        client,
        region: str,
        instance_type: str,
        limits: TargetLimits,
        counters: Counters,
        limiter: RateLimiter,
        deadline: float,
        exclude_local_zones: bool = True,
    ) -> None:
        self.client = client
        self.region = region
        self.instance_type = instance_type
        self.limits = limits
        self.counters = counters
        self.limiter = limiter
        self.deadline = deadline
        self.exclude_local_zones = exclude_local_zones

    def effective_end(self, end: datetime) -> datetime:
        return min(end, self.limits.max_end) if self.limits.max_end else end

    def offerings(self, start: datetime, end: datetime, hours: int, count: int) -> Optional[list[dict]]:
        """Offerings with start >= ``start`` and end <= ``end``.

        Returns None when this request cannot be made within the account's
        limits, [] when nothing is available (or the count is not supported).
        Raises Deadline / Throttled to stop the Region's work for this run.
        """
        if hours in self.limits.invalid_hours:
            return None
        if count in self.limits.invalid_counts:
            return []
        for _ in range(4):
            if self.limits.max_start is not None and start > self.limits.max_start:
                return None
            hi = self.effective_end(end)
            if hi - start < timedelta(hours=hours):
                return None
            try:
                raw = self._call(start, hi, hours, count)
            except ClientError as err:
                if not self._learn(err, hours, count):
                    self.counters.add("api_errors")
                    raise
                self.counters.add("limit_responses")
                if hours in self.limits.invalid_hours:
                    return None
                if count in self.limits.invalid_counts:
                    return []
                continue
            return [o for o in raw if self._keep(o)]
        return None

    def _learn(self, err: ClientError, hours: int, count: int) -> bool:
        """Record a limit from an InvalidParameterValue error. True if handled."""
        if error_code(err) != "InvalidParameterValue":
            return False
        msg = error_message(err)
        low = msg.lower()
        match = START_LIMIT_RE.search(msg)
        if "start date" in low and match:
            self.limits.learn_start(parse_java_date(match.group(1)))
            log.info("%s %s: start limit %s", self.region, self.instance_type, match.group(1))
            return True
        match = END_LIMIT_RE.search(msg)
        if "end date" in low and match:
            self.limits.learn_end(parse_java_date(match.group(1)))
            log.info("%s %s: end limit %s", self.region, self.instance_type, match.group(1))
            return True
        if "duration is not valid" in low:
            self.limits.reject_hours(hours)
            return True
        if "instance count is not valid" in low:
            self.limits.reject_count(count)
            return True
        return False

    def _call(self, start: datetime, end: datetime, hours: int, count: int) -> list[dict]:
        out: list[dict] = []
        token = None
        while True:
            kwargs = {
                "InstanceType": self.instance_type,
                "InstanceCount": count,
                "CapacityDurationHours": hours,
                "StartDateRange": start,
                "EndDateRange": end,
            }
            if token:
                kwargs["NextToken"] = token
            resp = call_api(self.client, kwargs, self.limiter, self.deadline, self.counters)
            out.extend(resp.get("CapacityBlockOfferings", []))
            token = resp.get("NextToken")
            if not token:
                return out

    def _keep(self, offer: dict) -> bool:
        if not self.exclude_local_zones:
            return True
        zone_type = offer.get("ZoneType")
        if zone_type:
            return zone_type == "availability-zone"
        return bool(_AZ_RE.match(offer.get("AvailabilityZone", "")))


def search_max(
    probe: Callable[[int], Optional[list]], upper: int, hint: Optional[int] = None
) -> Optional[tuple[int, list]]:
    """Largest n in [0, upper] for which ``probe(n)`` returns offerings.

    Availability is assumed to be monotone in n (if 16 instances are available,
    so are 8). ``hint`` is the expected answer; when it is right the search
    costs one call (hint == upper) or two. Returns None if ``probe`` returns
    None, meaning the request is outside the account's limits.
    """
    if upper < 1:
        return 0, []
    first = 1 if not hint or hint < 1 else min(hint, upper)
    result = probe(first)
    if result is None:
        return None
    lo, hi, best = 0, upper, []
    if result:
        lo, best = first, result
        if lo == upper:
            return lo, best
        if hint and hint >= 1:
            result = probe(lo + 1)
            if result is None:
                return None
            if not result:
                return lo, best
            lo, best = lo + 1, result
    else:
        hi = first - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        result = probe(mid)
        if result is None:
            return None
        if result:
            lo, best = mid, result
        else:
            hi = mid - 1
    return lo, best


# Discovery statuses
SUPPORTED = "supported"
TYPE_UNSUPPORTED = "type-unsupported"
REGION_UNSUPPORTED = "no-capacity-blocks"
NOT_OPTED_IN = "not-opted-in"
UNKNOWN_REGION = "unknown-region"


def discover(
    regions: tuple[str, ...],
    instance_types: tuple[str, ...],
    client_factory: ClientFactory,
    home_region: str,
    counters: Counters,
    limiters: dict[str, RateLimiter],
    deadline: float,
    now: Optional[datetime] = None,
) -> dict:
    """Probe which (Region, instance type) pairs support Capacity Blocks.

    Returns ``{"at": iso, "regions": {region: {"status": ..., "types": {type: status}}}}``.
    """
    home = client_factory(home_region)
    resp = home.describe_regions(AllRegions=True)
    opt_in = {r["RegionName"]: r.get("OptInStatus", "") for r in resp.get("Regions", [])}

    def probe_region(region: str) -> tuple[str, dict]:
        if region not in opt_in:
            return region, {"status": UNKNOWN_REGION, "types": {}}
        if opt_in[region] == "not-opted-in":
            return region, {"status": NOT_OPTED_IN, "types": {}}
        client = client_factory(region)
        types: dict[str, str] = {}
        for itype in instance_types:
            kwargs = {"InstanceType": itype, "InstanceCount": 1, "CapacityDurationHours": 24}
            try:
                call_api(client, kwargs, limiters[region], deadline, counters)
                types[itype] = SUPPORTED
            except ClientError as err:
                code, msg = error_code(err), error_message(err).lower()
                if code == "InvalidAction":
                    return region, {"status": REGION_UNSUPPORTED, "types": {}}
                if code == "InvalidParameterValue" and "not supported" in msg:
                    types[itype] = TYPE_UNSUPPORTED
                elif code == "InvalidParameterValue" and ("date is not valid" in msg or "duration is not valid" in msg):
                    types[itype] = SUPPORTED  # the type is accepted; only the dates were out of range
                else:
                    counters.add("api_errors")
                    types[itype] = f"error:{code or 'unknown'}"
                    log.warning("discovery %s %s failed: %s", region, itype, err)
            except (Throttled, Deadline) as err:
                types[itype] = f"error:{type(err).__name__}"
        return region, {"status": "ok", "types": types}

    with ThreadPoolExecutor(max_workers=max(1, min(len(regions), 12))) as pool:
        results = dict(pool.map(probe_region, regions))
    return {"at": iso(now or utcnow()), "regions": results}


def supported_targets(discovery: dict, regions: tuple[str, ...], instance_types: tuple[str, ...]):
    """(region, instance_type) pairs that discovery marked as supported, in config order."""
    out = []
    for region in regions:
        info = discovery.get("regions", {}).get(region, {})
        for itype in instance_types:
            if info.get("types", {}).get(itype) == SUPPORTED:
                out.append((region, itype))
    return out


def discovery_has_errors(discovery: dict) -> bool:
    for info in discovery.get("regions", {}).values():
        for status in info.get("types", {}).values():
            if status.startswith("error:"):
                return True
    return False
