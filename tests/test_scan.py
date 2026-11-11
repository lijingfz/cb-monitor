from datetime import timedelta

from conftest import NOW, FakeEC2, limiters

from cbmon.config import Config
from cbmon.ec2query import Counters, RateLimiter, discover, search_max, supported_targets
from cbmon.scanner import AVAILABLE, LIMITED, NONE, SKIPPED, details_from, run_scan, search_last

TYPES = ("p6-b300.48xlarge", "p6-b200.48xlarge")
B300, B200 = TYPES


def cfg(**kw):
    base = dict(instance_types=TYPES, regions=("us-east-1", "us-west-2"))
    base.update(kw)
    return Config(**base)


def scan(c, clients, clock, previous=None, budget=10_000, targets=None, now=NOW, **kw):
    targets = targets or [(r, t) for r in clients for t in c.instance_types]
    return run_scan(
        c, targets, lambda r: clients[r], previous or {}, limiters(clock), clock() + budget,
        now=now, clock=clock, **kw,
    )


LATER = NOW + timedelta(hours=2)  # past the 60-minute staircase refresh


def by_key(result):
    return {(c.instance_type, c.slice_key, c.duration_days): c for c in result.cells}


# ---------------------------------------------------------------- search ----


def test_search_max_finds_capacity_with_and_without_hint():
    for capacity in range(0, 65):
        for hint in (None, 1, 10, capacity, capacity + 1, 64):
            calls = []

            def probe(n):
                calls.append(n)
                return ["x"] if n <= capacity else []

            count, _ = search_max(probe, 64, hint)
            assert count == capacity, (capacity, hint)
            assert len(calls) <= 8
    calls = []
    search_max(lambda n: calls.append(n) or (["x"] if n <= 23 else []), 64, 23)
    assert calls == [23, 24]  # unchanged: two calls
    calls = []
    search_max(lambda n: calls.append(n) or ["x"], 64, 64)
    assert calls == [64]


def test_search_max_propagates_limited():
    assert search_max(lambda n: None, 64) is None


def test_search_last():
    for last in range(0, 37):
        for hint in (None, 0, 5, last, last + 1, 36):
            calls = []

            def pred(i):
                calls.append(i)
                return i <= last

            assert search_last(pred, 0, 36, hint) == last
            assert len(calls) <= 8


def test_rate_limiter_paces_calls(clock):
    limiter = RateLimiter(6, burst=3, clock=clock, sleep=clock.sleep)
    start = clock()
    for _ in range(9):
        assert limiter.acquire(deadline=start + 3600)
    # 3 at once, then one every 10 seconds
    assert abs(clock() - start - 60) < 0.1
    assert not limiter.acquire(deadline=clock() + 1)


# ------------------------------------------------------------- accounts ----


def restricted(region, capacity):
    """The account seen during development: start <= now+2d, end <= now+9d, duration <= 7d."""
    return FakeEC2(region, capacity, max_start_ahead=timedelta(days=2), max_end_ahead=timedelta(days=9), max_hours=168)


def test_restricted_account_is_limited_with_one_call_per_target(clock):
    clients = {r: restricted(r, lambda *a: 64) for r in ("us-east-1", "us-west-2")}
    result = scan(cfg(), clients, clock)
    assert {c.status for c in result.cells} == {LIMITED}
    assert len(result.cells) == 4 * 8 * 37
    assert result.counters["api_calls"] == 4
    assert result.limits[f"us-east-1|{B300}"]["max_start"].startswith("2026-11-13T11:4")


def test_restricted_account_with_zero_offset_finds_short_blocks(clock):
    clients = {"us-east-1": restricted("us-east-1", lambda t, s, h: 40)}
    c = cfg(regions=("us-east-1",), instance_types=(B300,), min_start_offset_days=0)
    result = scan(c, clients, clock)
    cells = by_key(result)
    week0 = "2026-11-09"
    assert [cells[(B300, week0, d)].max_count for d in range(1, 8)] == [40] * 7
    assert cells[(B300, week0, 8)].status == LIMITED
    assert cells[(B300, week0, 175)].status == LIMITED
    assert {c.status for c in result.cells if c.slice_key != week0} == {LIMITED}
    assert result.limits[f"us-east-1|{B300}"]["invalid_durations_days"] == [8]


def standard_capacity(itype, start, hours):
    if itype == B200:
        return 0
    return 64 if hours <= 14 * 24 else 8


def test_standard_account_builds_staircase_for_every_week(clock):
    clients = {"us-east-1": FakeEC2("us-east-1", standard_capacity)}
    result = scan(cfg(regions=("us-east-1",)), clients, clock)
    assert result.complete and not result.errors
    cells = by_key(result)
    weeks = sorted({c.slice_key for c in result.cells})
    assert len(weeks) == 8
    for week in weeks:
        for d in cfg().durations_days:
            cell = cells[(B300, week, d)]
            assert cell.status == AVAILABLE
            assert cell.max_count == (64 if d <= 14 else 8)
            assert cells[(B200, week, d)].status == NONE
        first = cells[(B300, week, 1)]
        assert first.offers and all(first.slice_start <= o["start"] < first.slice_end for o in first.offers)
    b200_calls = [k for k in clients["us-east-1"].calls if k["InstanceType"] == B200]
    assert len(b200_calls) == 1  # one wide query proves the whole window is empty


def test_recent_staircase_is_reused_without_calls(clock):
    clients = {"us-east-1": FakeEC2("us-east-1", standard_capacity)}
    c = cfg(regions=("us-east-1",), instance_types=(B300,))
    first = scan(c, clients, clock)
    previous = details_from(first, {}, NOW)
    calls = len(clients["us-east-1"].calls)
    second = scan(c, clients, clock, previous=previous, now=NOW + timedelta(minutes=15))
    assert len(clients["us-east-1"].calls) == calls
    assert [x.max_count for x in second.cells] == [x.max_count for x in first.cells]
    assert not any(x.fresh for x in second.cells)


def test_previous_staircase_cuts_refresh_calls(clock):
    clients = {"us-east-1": FakeEC2("us-east-1", standard_capacity)}
    c = cfg(regions=("us-east-1",), instance_types=(B300,))
    first = scan(c, clients, clock)
    second = scan(c, clients, clock, previous=details_from(first, {}, NOW), now=LATER)
    assert second.counters["api_calls"] <= first.counters["api_calls"] / 3
    assert [x.max_count for x in second.cells] == [x.max_count for x in first.cells]
    assert all(x.fresh for x in second.cells if x.status == AVAILABLE)


def test_changed_capacity_is_measured_despite_hint(clock):
    clients = {"us-east-1": FakeEC2("us-east-1", standard_capacity)}
    c = cfg(regions=("us-east-1",), instance_types=(B300,))
    previous = details_from(scan(c, clients, clock), {}, NOW)
    clients["us-east-1"].capacity = lambda t, s, h: 20 if h <= 3 * 24 else 0
    cells = by_key(scan(c, clients, clock, previous=previous, now=LATER))
    week = "2026-11-23"
    assert [cells[(B300, week, d)].max_count for d in (1, 2, 3)] == [20, 20, 20]
    assert cells[(B300, week, 4)].status == NONE


def test_empty_weeks_are_skipped_with_one_query(clock):
    later = NOW + timedelta(days=26)  # first offering Tue Dec 8, i.e. the 4th week
    clients = {"us-east-1": FakeEC2("us-east-1", lambda t, s, h: 4 if s >= later else 0)}
    c = cfg(regions=("us-east-1",), instance_types=(B300,), durations_days=(1,))
    result = scan(c, clients, clock)
    statuses = [c.status for c in sorted(result.cells, key=lambda c: c.slice_key)]
    assert statuses == [NONE] * 3 + [AVAILABLE] * 5
    # Detection: one query finds the 4th week, then one per later week.
    detection = [k for k in clients["us-east-1"].calls if k["InstanceCount"] == 1]
    assert len(detection) == 5


def test_throttled_region_keeps_previous_results(clock):
    clients = {"us-east-1": FakeEC2("us-east-1", standard_capacity)}
    c = cfg(regions=("us-east-1",), instance_types=(B300,))
    previous = details_from(scan(c, clients, clock), {}, NOW)
    clients["us-east-1"] = FakeEC2("us-east-1", standard_capacity, fail_after=10)
    result = scan(c, clients, clock, previous=previous, now=LATER)
    assert not result.complete
    assert "us-east-1" in result.stopped
    available = [x for x in result.cells if x.status == AVAILABLE]
    assert len(available) == 8 * 37
    assert any(x.as_of for x in available)  # some weeks carried over from the previous run


def test_describe_limit_stops_region(clock):
    clients = {"us-east-1": FakeEC2("us-east-1", standard_capacity, fail_after=0,
                                    fail_code="CapacityBlockDescribeLimitExceeded")}
    result = scan(cfg(regions=("us-east-1",), instance_types=(B300,)), clients, clock)
    assert "CapacityBlockDescribeLimitExceeded" in result.stopped["us-east-1"]
    assert len(clients["us-east-1"].calls) == 1
    assert {x.status for x in result.cells} == {SKIPPED}


def test_deadline_marks_weeks_skipped(clock):
    clients = {"us-east-1": FakeEC2("us-east-1", standard_capacity)}
    result = scan(cfg(regions=("us-east-1",)), clients, clock, budget=-1)
    assert not result.complete
    assert {x.status for x in result.cells} == {SKIPPED}


def test_paused_region_is_not_queried(clock):
    clients = {"us-east-1": FakeEC2("us-east-1", standard_capacity)}
    result = scan(cfg(regions=("us-east-1",)), clients, clock, paused_regions=("us-east-1",))
    assert clients["us-east-1"].calls == []
    assert {x.status for x in result.cells} == {SKIPPED}


def test_local_zone_offerings_are_excluded(clock):
    clients = {"us-east-1": FakeEC2("us-east-1", lambda *a: 64, zone_type="local-zone")}
    result = scan(cfg(regions=("us-east-1",), durations_days=(1,)), clients, clock)
    assert {x.status for x in result.cells} == {NONE}


def test_discovery_classifies_regions(clock):
    clients = {
        "us-east-1": FakeEC2("us-east-1", lambda *a: 0),
        "us-west-2": FakeEC2("us-west-2", lambda *a: 0, unsupported_types=[B200]),
        "eu-west-1": FakeEC2("eu-west-1", lambda *a: 0, no_capacity_blocks=True),
    }
    regions = ("us-east-1", "us-west-2", "eu-west-1", "eu-south-1", "xx-nowhere-1")
    d = discover(regions, TYPES, lambda r: clients[r], "us-east-1", Counters(),
                 limiters(clock, regions), clock() + 100, NOW)
    r = d["regions"]
    assert r["us-east-1"]["types"] == {B300: "supported", B200: "supported"}
    assert r["us-west-2"]["types"][B200] == "type-unsupported"
    assert r["eu-west-1"]["status"] == "no-capacity-blocks"
    assert r["eu-south-1"]["status"] == "not-opted-in"
    assert r["xx-nowhere-1"]["status"] == "unknown-region"
    assert supported_targets(d, regions, TYPES) == [("us-east-1", B300), ("us-east-1", B200), ("us-west-2", B300)]


def test_new_capacity_in_unknown_week_found_while_known_weeks_wait(clock):
    # Weeks 2+ known and recent; week 1 newly gets capacity.
    clients = {"us-east-1": FakeEC2("us-east-1", lambda t, s, h: 8 if s >= NOW + timedelta(days=12) else 0)}
    c = cfg(regions=("us-east-1",), instance_types=(B300,), durations_days=(1, 2))
    previous = details_from(scan(c, clients, clock), {}, NOW)
    clients["us-east-1"].capacity = lambda t, s, h: 8
    clients["us-east-1"].calls.clear()
    cells = by_key(scan(c, clients, clock, previous=previous, now=NOW + timedelta(minutes=15)))
    assert cells[(B300, "2026-11-16", 1)].status == AVAILABLE and cells[(B300, "2026-11-16", 1)].fresh
    assert not cells[(B300, "2026-11-30", 1)].fresh
    assert len(clients["us-east-1"].calls) < 10
