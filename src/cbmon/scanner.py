"""One scan of every (Region, instance type, start week).

DescribeCapacityBlockOfferings is expensive to call: about 9 requests per
minute per account and Region, plus an account-level describe quota
(``CapacityBlockDescribeLimitExceeded``). So the scanner never tries every
duration x count combination. For each start week it measures a *staircase*:
the largest single-block instance count for every configured duration. Two
assumptions make that cheap, and both follow from how capacity works:

* if a block of N instances is available, so is a block of fewer instances;
* if a block of D days is available, so is a shorter block starting the same day.

So the max count can only go down as the duration grows, and a staircase is
found with a few binary searches. The previous staircase is used as a hint,
which usually confirms an unchanged week with 3-6 calls.

Per (Region, instance type) a run does:

1. Detection, for weeks with no recent staircase: one query (1 instance,
   shortest duration) from the first such week to the end of the window. The
   API returns the earliest offering, so the weeks before it are empty. Repeat
   after that week. Weeks with no capacity cost nothing extra.
2. Staircases for newly found weeks, and for known weeks whose staircase is
   older than ``detail_refresh_minutes``; the oldest first, until the time
   budget runs out. Weeks that could not be refreshed keep their previous
   staircase for up to ``STALE_MAX_AGE``, marked stale.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from botocore.exceptions import ClientError

from .config import Config
from .ec2query import (
    ClientFactory,
    Counters,
    Deadline,
    Querier,
    RateLimiter,
    TargetLimits,
    Throttled,
    error_code,
    search_max,
)
from .timeutil import iso, parse_iso, utcnow
from .windows import Slice, build_slices

log = logging.getLogger(__name__)

AVAILABLE = "available"  # at least one offering
NONE = "none"  # queried, nothing available
LIMITED = "limited"  # outside the account's search limits
ERROR = "error"  # API error
SKIPPED = "skipped"  # not checked this run (time budget, throttling, paused)

STALE_MAX_AGE = timedelta(hours=6)


@dataclass
class Cell:
    """One (instance type, Region, start week, duration) result."""

    instance_type: str
    region: str
    slice_key: str
    slice_start: str
    slice_end: str
    duration_days: int
    status: str
    max_count: int = 0
    offers: list[dict] = field(default_factory=list)  # example offerings at max_count
    fresh: bool = False  # max_count measured in this run
    partial: bool = False  # max_count is a lower bound
    as_of: Optional[str] = None  # set when carried over from an earlier run after a failure
    error: str = ""

    @property
    def key(self) -> str:
        return f"{self.instance_type}|{self.region}|{self.slice_key}|{self.duration_days}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Cell":
        return cls(**data)


@dataclass
class WeekResult:
    instance_type: str
    region: str
    slice_key: str
    slice_start: str
    slice_end: str
    status: str = SKIPPED
    # [{"count", "from_days", "to_days", "offers", "offers_last"}], count strictly decreasing
    steps: list[dict] = field(default_factory=list)
    as_of: Optional[str] = None  # when the steps were measured
    fresh: bool = False  # measured in this run
    stale: bool = False  # carried over because this run could not refresh it
    partial: bool = False  # measurement interrupted; counts are lower bounds
    limited_from_days: Optional[int] = None
    error: str = ""

    @property
    def key(self) -> str:
        return f"{self.instance_type}|{self.region}|{self.slice_key}"

    def cells(self, durations: tuple[int, ...]) -> list[Cell]:
        base = dict(
            instance_type=self.instance_type,
            region=self.region,
            slice_key=self.slice_key,
            slice_start=self.slice_start,
            slice_end=self.slice_end,
        )
        if self.status != AVAILABLE:
            return [Cell(**base, duration_days=d, status=self.status, error=self.error) for d in durations]
        out = []
        for d in durations:
            step = next((s for s in self.steps if s["from_days"] <= d <= s["to_days"]), None)
            if step:
                if d == step["from_days"]:
                    offers = step["offers"]
                elif d == step["to_days"]:
                    offers = step.get("offers_last", [])
                else:
                    offers = []
                out.append(
                    Cell(
                        **base,
                        duration_days=d,
                        status=AVAILABLE,
                        max_count=step["count"],
                        offers=offers,
                        fresh=self.fresh,
                        partial=self.partial,
                        as_of=self.as_of if self.stale else None,
                    )
                )
            elif self.partial:
                out.append(Cell(**base, duration_days=d, status=SKIPPED))
            elif self.limited_from_days is not None and d >= self.limited_from_days:
                out.append(Cell(**base, duration_days=d, status=LIMITED))
            else:
                out.append(Cell(**base, duration_days=d, status=NONE))
        return out


@dataclass
class ScanResult:
    started: str
    finished: str
    duration_s: float
    complete: bool
    window_start: Optional[str]
    window_end: Optional[str]
    targets: list[list[str]]
    weeks: list[WeekResult]
    cells: list[Cell]
    limits: dict[str, dict]
    counters: dict[str, int]
    errors: list[str]
    stopped: dict[str, str] = field(default_factory=dict)  # region -> why it stopped early


def compact_offer(offer: dict) -> dict:
    return {
        "id": offer.get("CapacityBlockOfferingId", ""),
        "az": offer.get("AvailabilityZone", ""),
        "start": iso(offer["StartDate"]),
        "end": iso(offer["EndDate"]),
        "hours": offer.get("CapacityBlockDurationHours"),
        "fee": offer.get("UpfrontFee"),
        "currency": offer.get("CurrencyCode", "USD"),
        "count": offer.get("InstanceCount"),
    }


def _fee(offer: dict) -> float:
    try:
        return float(offer.get("UpfrontFee") or 0)
    except ValueError:
        return 0.0


def _in_slice(offers: list[dict], slc: Slice) -> list[dict]:
    kept = [o for o in offers if slc.start <= o["StartDate"] < slc.end]
    return sorted(kept, key=lambda o: (o["StartDate"], _fee(o)))


class WeekProber:
    """Memoised probes for one (Region, instance type, start week)."""

    def __init__(self, querier: Querier, slc: Slice, durations: tuple[int, ...]) -> None:
        self.q = querier
        self.slc = slc
        self.durations = durations
        self.memo: dict[tuple[int, int], Optional[list[dict]]] = {}
        self.limited_idx: set[int] = set()

    def seed(self, count: int, idx: int, offers: list[dict]) -> None:
        self.memo[(count, idx)] = offers

    def probe(self, count: int, idx: int) -> Optional[list[dict]]:
        key = (count, idx)
        if key not in self.memo:
            hours = self.durations[idx] * 24
            # Blocks end at 11:30 UTC; the extra day covers blocks starting late in the week.
            end = self.slc.end + timedelta(hours=hours, days=1)
            offers = self.q.offerings(self.slc.start, end, hours, count)
            if offers is None:
                self.limited_idx.add(idx)
            else:
                offers = _in_slice(offers, self.slc)
            self.memo[key] = offers
        return self.memo[key]

    def limited_from_days(self) -> Optional[int]:
        return self.durations[min(self.limited_idx)] if self.limited_idx else None


def search_last(pred: Callable[[int], bool], lo: int, hi: int, hint: Optional[int] = None) -> int:
    """Largest i in [lo, hi] with pred(i) true; pred(lo) is known to be true."""
    if lo >= hi:
        return lo
    if hint is not None:
        hint = min(max(hint, lo), hi)
        if hint > lo:
            if pred(hint):
                lo = hint
            else:
                hi = hint - 1
        if lo == hint and lo < hi:
            if not pred(lo + 1):
                return lo
            lo += 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if pred(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def staircase(
    prober: WeekProber, max_count: int, hint_steps: Optional[list[dict]] = None
) -> tuple[list[dict], Optional[Exception]]:
    """Max instance count per duration, as steps. Returns (steps, interruption)."""
    durations = prober.durations
    idx_of = {d: i for i, d in enumerate(durations)}
    hints = [
        (h["count"], idx_of[h["from_days"]], idx_of[h["to_days"]])
        for h in hint_steps or []
        if h.get("from_days") in idx_of and h.get("to_days") in idx_of
    ]
    steps: list[dict] = []
    start, cap = 0, max_count
    try:
        while start < len(durations) and cap >= 1:
            hint_count = next((c for c, f, l in hints if f <= start <= l), None)
            found = search_max(lambda k: prober.probe(k, start), cap, hint_count)
            if found is None or found[0] == 0:
                break
            count, offers = found
            hint_last = next((l for c, f, l in hints if c == count and f <= start <= l), None)
            last = search_last(lambda i: bool(prober.probe(count, i)), start, len(durations) - 1, hint_last)
            steps.append(
                {
                    "count": count,
                    "from_days": durations[start],
                    "to_days": durations[last],
                    "offers": [compact_offer(o) for o in offers],
                    "offers_last": [compact_offer(o) for o in prober.memo.get((count, last)) or []],
                }
            )
            start, cap = last + 1, count - 1
    except (Deadline, Throttled) as exc:
        return steps, exc
    return steps, None


def _detect(
    querier: Querier,
    slices: list[Slice],
    weeks: list[WeekResult],
    probers: list[WeekProber],
    durations: tuple[int, ...],
    known: frozenset[int] = frozenset(),
) -> None:
    """Set the status of every week not in ``known`` to AVAILABLE, NONE or LIMITED."""
    hours = durations[0] * 24
    unknown = [i for i in range(len(slices)) if i not in known]
    pos = 0
    while pos < len(unknown):
        first = unknown[pos]
        span = Slice("span", slices[first].start, slices[-1].end)
        found = querier.offerings(span.start, span.end + timedelta(hours=hours, days=1), hours, 1)
        if found is None:
            for i in unknown[pos:]:
                weeks[i].status = LIMITED
            return
        found = _in_slice(found, span)
        if not found:
            for i in unknown[pos:]:
                weeks[i].status = NONE
            return
        earliest = found[0]["StartDate"]
        hit = next(i for i, s in enumerate(slices) if s.start <= earliest < s.end)
        for i in unknown[pos:]:
            if i < hit:
                weeks[i].status = NONE
        if hit not in known:
            weeks[hit].status = AVAILABLE
            probers[hit].seed(1, 0, _in_slice(found, slices[hit]))
        pos = next((p for p, i in enumerate(unknown) if i > hit), len(unknown))


def _apply_previous(week: WeekResult, previous: dict, stale: bool) -> None:
    week.status = AVAILABLE
    week.steps = previous["steps"]
    week.as_of = previous["as_of"]
    week.limited_from_days = previous.get("limited_from_days")
    week.stale = stale


def _carry_over(week: WeekResult, previous: Optional[dict], now: datetime) -> bool:
    """Use the previous staircase after a failed refresh, if it is not too old."""
    if not previous or now - parse_iso(previous["as_of"]) > STALE_MAX_AGE:
        return False
    _apply_previous(week, previous, stale=True)
    return True


@dataclass
class _Job:
    week: WeekResult
    prober: WeekProber
    previous: Optional[dict]

    def priority(self) -> tuple:
        # Never measured first, then the oldest measurement.
        return (self.previous is not None, (self.previous or {}).get("as_of", ""))


def run_scan(
    cfg: Config,
    targets: list[tuple[str, str]],
    client_factory: ClientFactory,
    previous: dict[str, dict],
    limiters: dict[str, RateLimiter],
    deadline: float,
    now: Optional[datetime] = None,
    paused_regions: tuple[str, ...] = (),
    clock: Optional[Callable[[], float]] = None,
) -> ScanResult:
    """Scan all targets.

    ``previous`` maps week keys to the last measured staircases (see
    ``details_from``). Regions in ``paused_regions`` are not queried.
    """
    clock = clock or time.monotonic
    now = now or utcnow()
    t0 = clock()
    slices = build_slices(now, cfg.min_start_offset_days, cfg.max_advance_days, cfg.weekly_slices)
    durations = cfg.durations_days
    refresh = timedelta(minutes=cfg.detail_refresh_minutes)
    counters = Counters()
    limits = {(r, t): TargetLimits() for r, t in targets}
    errors: list[str] = []
    stopped: dict[str, str] = {}

    by_region: dict[str, list[str]] = {}
    for region, itype in targets:
        by_region.setdefault(region, []).append(itype)

    def region_job(region: str) -> list[WeekResult]:
        client = client_factory(region)
        weeks_out: list[WeekResult] = []
        jobs: list[_Job] = []
        stop: Optional[Exception] = Throttled("paused") if region in paused_regions else None

        # Step 1: detection for weeks without a recent staircase.
        for itype in by_region[region]:
            querier = Querier(
                client, region, itype, limits[(region, itype)], counters, limiters[region], deadline,
                cfg.exclude_local_zones,
            )
            weeks = [WeekResult(itype, region, s.key, iso(s.start), iso(s.end)) for s in slices]
            probers = [WeekProber(querier, s, durations) for s in slices]
            weeks_out.extend(weeks)
            recent = {
                i: previous[w.key]
                for i, w in enumerate(weeks)
                if w.key in previous and now - parse_iso(previous[w.key]["as_of"]) < refresh
            }
            for i, prev in recent.items():
                _apply_previous(weeks[i], prev, stale=False)
            if stop is not None:
                continue
            try:
                _detect(querier, slices, weeks, probers, durations, frozenset(recent))
            except (Deadline, Throttled) as exc:
                stop = exc
            except ClientError as err:
                errors.append(f"{region} {itype}: {err}")
                for i, week in enumerate(weeks):
                    if i not in recent and week.status == SKIPPED:
                        week.status, week.error = ERROR, error_code(err) or "ClientError"
            for i, (week, prober) in enumerate(zip(weeks, probers)):
                if i not in recent and week.status == AVAILABLE:
                    jobs.append(_Job(week, prober, previous.get(week.key)))
                elif i in recent and now - parse_iso(recent[i]["as_of"]) >= refresh / 2:
                    # Known week due for a refresh soon: measure it if time allows.
                    jobs.append(_Job(week, prober, recent[i]))

        # Step 2: staircases, least recently measured first.
        for job in sorted(jobs, key=_Job.priority):
            week, steps = job.week, []
            if stop is None:
                try:
                    steps, interrupted = staircase(
                        job.prober, cfg.max_instance_count, (job.previous or {}).get("steps")
                    )
                except ClientError as err:
                    errors.append(f"{week.key}: {err}")
                    if not _carry_over(week, job.previous, now):
                        week.status, week.error = ERROR, error_code(err) or "ClientError"
                    continue
                if interrupted is None:
                    week.status, week.steps, week.as_of = AVAILABLE, steps, iso(now)
                    week.fresh, week.stale = True, False
                    week.limited_from_days = job.prober.limited_from_days()
                    if not steps:  # capacity disappeared since it was last measured
                        week.status = NONE
                    continue
                stop = interrupted
            if week.as_of and not week.stale and week.status == AVAILABLE:
                continue  # recent staircase already applied; refresh can wait
            if _carry_over(week, job.previous, now):
                continue
            if not steps:
                seed = job.prober.memo.get((1, 0)) or []
                steps = [{"count": 1, "from_days": durations[0], "to_days": durations[0],
                          "offers": [compact_offer(o) for o in seed], "offers_last": []}]
            week.status, week.steps, week.as_of = AVAILABLE, steps, iso(now)
            week.fresh, week.partial = True, True

        for week in weeks_out:
            if week.status in (SKIPPED, ERROR):
                _carry_over(week, previous.get(week.key), now)
        if stop is not None:
            stopped[region] = f"{type(stop).__name__}: {stop}"
            errors.append(f"{region}: stopped early ({stopped[region]})")
        return weeks_out

    weeks: list[WeekResult] = []
    if slices and by_region:
        with ThreadPoolExecutor(max_workers=len(by_region)) as pool:
            for result in pool.map(region_job, list(by_region)):
                weeks.extend(result)

    cells = [c for w in weeks for c in w.cells(durations)]
    return ScanResult(
        started=iso(now),
        finished=iso(utcnow()),
        duration_s=round(clock() - t0, 1),
        complete=not any(w.status == SKIPPED or w.partial or w.stale for w in weeks),
        window_start=iso(slices[0].start) if slices else None,
        window_end=iso(slices[-1].end) if slices else None,
        targets=[[r, t] for r, t in targets],
        weeks=weeks,
        cells=cells,
        limits={f"{r}|{t}": lim.to_dict() for (r, t), lim in limits.items()},
        counters=counters.snapshot(),
        errors=errors[:50],
        stopped=stopped,
    )


def details_from(result: ScanResult, previous: dict[str, dict], now: datetime) -> dict[str, dict]:
    """Staircases to keep for the next run: fresh complete ones, else the previous ones."""
    out: dict[str, dict] = {}
    for week in result.weeks:
        if week.status == AVAILABLE and week.fresh and not week.partial:
            out[week.key] = {
                "steps": week.steps,
                "as_of": week.as_of,
                "limited_from_days": week.limited_from_days,
                "slice_end": week.slice_end,
            }
        elif week.status != NONE and week.key in previous:
            out[week.key] = previous[week.key]
    return {k: v for k, v in out.items() if parse_iso(v["slice_end"]) > now}
