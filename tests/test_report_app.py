from datetime import timedelta

from conftest import NOW, FakeClock, FakeEC2, limiters

from cbmon.app import scan_job, summary_job
from cbmon.config import Config
from cbmon.notifier import PrintNotifier, _fit_body
from cbmon.report import build_new_capacity_message, select_new
from cbmon.scanner import AVAILABLE, ERROR, NONE, Cell
from cbmon.state import LocalStore

TYPES = ("p6-b300.48xlarge", "p6-b200.48xlarge")


def cell(days=1, count=8, status=AVAILABLE, slice_key="2026-11-16"):
    start = "2026-11-18T11:30:00Z"
    offers = []
    if status == AVAILABLE:
        offers = [{
            "id": f"cb-{days}", "az": "us-east-1a", "start": start, "end": "2026-11-19T11:30:00Z",
            "hours": days * 24, "fee": str(100.0 * count * days * 24), "currency": "USD", "count": count,
        }]
    return Cell(TYPES[0], "us-east-1", slice_key, "2026-11-18T11:45:00Z", "2026-11-23T00:00:00Z",
                days, status, count if status == AVAILABLE else 0, offers, fresh=status == AVAILABLE)


def test_select_new_rules():
    new, rec = select_new([cell(count=8)], {}, NOW, 120)
    assert len(new) == 1
    # same again: not new
    later = NOW + timedelta(minutes=15)
    new, rec = select_new([cell(count=8)], rec, later, 120)
    assert new == []
    # decrease: not new, high-water mark kept
    new, rec = select_new([cell(count=4)], rec, later + timedelta(minutes=15), 120)
    assert new == [] and rec[cell().key]["count"] == 8
    # increase above notified: new
    new, rec = select_new([cell(count=16)], rec, later + timedelta(minutes=30), 120)
    assert len(new) == 1
    # an error run keeps it "seen"
    t = later + timedelta(minutes=45)
    _, rec = select_new([cell(status=ERROR)], rec, t, 120)
    # gone (queried, nothing) for 3 hours, then back: new
    _, rec = select_new([cell(status=NONE)], rec, t + timedelta(hours=1), 120)
    new, _ = select_new([cell(count=2)], rec, t + timedelta(hours=3), 120)
    assert len(new) == 1


def test_message_merges_durations_with_same_count():
    cells = [cell(days=d, count=64) for d in range(1, 8)] + [cell(days=8, count=16)]
    subject, body = build_new_capacity_message(cells, Config(), NOW)
    assert subject.startswith("[CB监控] 发现新容量: p6-b300 us-east-1")
    assert len(subject) < 100
    assert "1–7 天：最多 64 台" in body
    assert "8 天：最多 16 台" in body
    assert "purchase-capacity-block" in body
    assert "北京时间" in body


def test_long_body_is_truncated():
    body = "x" * 400_000
    assert len(_fit_body(body).encode()) < 256 * 1024


def test_scan_job_end_to_end(tmp_path):
    clients = {"us-east-1": FakeEC2("us-east-1", lambda t, s, h: 64 if t == TYPES[0] and h <= 72 else 0)}
    clients["us-east-1"].describe_regions = lambda AllRegions=True: {
        "Regions": [{"RegionName": "us-east-1", "OptInStatus": "opt-in-not-required"}]
    }
    c = Config(instance_types=TYPES, regions=("us-east-1",), durations_days=(1, 2, 3, 7))
    store, notifier = LocalStore(str(tmp_path)), PrintNotifier()
    factory = lambda r: clients[r]  # noqa: E731
    clock = FakeClock()
    kw = dict(limiters=limiters(clock), clock=clock)

    first = scan_job(c, store, notifier, factory, "us-east-1", "test", now=NOW, **kw)
    assert first["published"] == 1 and first["new_cells"] == 3 * 8
    assert "1–3 天：最多 64 台" in notifier.sent[0][1]

    second = scan_job(c, store, notifier, factory, "us-east-1", "test", now=NOW + timedelta(minutes=15), **kw)
    assert second["published"] == 0 and len(notifier.sent) == 1

    dry = scan_job(c, store, notifier, factory, "us-east-1", "test", now=NOW, dry_run=True, **kw)
    assert dry["dry_run"]

    result = summary_job(c, store, notifier, now=NOW + timedelta(hours=1))
    assert result["status"] == "ok"
    subject, body = notifier.sent[-1]
    assert "每日汇总" in subject
    assert "扫描 2 次" in body
    assert "p6-b300.48xlarge：us-east-1" in body
    assert "未发现" in body  # no account limits in this fake


def test_summary_reports_account_limits(tmp_path):
    clients = {"us-east-1": FakeEC2("us-east-1", lambda *a: 64, max_start_ahead=timedelta(days=2),
                                    max_end_ahead=timedelta(days=9), max_hours=168)}
    clients["us-east-1"].describe_regions = lambda AllRegions=True: {
        "Regions": [{"RegionName": "us-east-1", "OptInStatus": "opt-in-not-required"}]
    }
    c = Config(instance_types=TYPES, regions=("us-east-1",))
    store, notifier = LocalStore(str(tmp_path)), PrintNotifier()
    clock = FakeClock()
    out = scan_job(c, store, notifier, lambda r: clients[r], "us-east-1", "t", now=NOW,
                   limiters=limiters(clock), clock=clock)
    assert out["published"] == 0
    summary_job(c, store, notifier, now=NOW + timedelta(minutes=5))
    body = notifier.sent[-1][1]
    assert "账号查询限制" in body
    assert "不重叠" in body
    assert "当前没有符合条件的容量" in body


def test_summary_without_snapshot(tmp_path):
    notifier = PrintNotifier()
    summary_job(Config(), LocalStore(str(tmp_path)), notifier, now=NOW)
    assert "还没有任何扫描结果" in notifier.sent[0][1]


def test_describe_limit_pauses_region(tmp_path):
    clients = {"us-east-1": FakeEC2("us-east-1", lambda *a: 64)}
    c = Config(instance_types=TYPES[:1], regions=("us-east-1",))
    store, notifier, clock = LocalStore(str(tmp_path)), PrintNotifier(), FakeClock()
    kw = dict(limiters=limiters(clock), clock=clock)
    # discovery succeeds, then the account-level describe limit kicks in
    clients["us-east-1"].fail_after, clients["us-east-1"].fail_code = 1, "CapacityBlockDescribeLimitExceeded"
    out = scan_job(c, store, notifier, lambda r: clients[r], "us-east-1", "t", now=NOW, **kw)
    assert "us-east-1" in out["stopped"]
    assert "us-east-1" in store.load("state")["paused"]
    calls = len(clients["us-east-1"].calls)
    scan_job(c, store, notifier, lambda r: clients[r], "us-east-1", "t", now=NOW + timedelta(minutes=15), **kw)
    assert len(clients["us-east-1"].calls) == calls  # paused: no calls
    scan_job(c, store, notifier, lambda r: clients[r], "us-east-1", "t", now=NOW + timedelta(minutes=75), **kw)
    assert len(clients["us-east-1"].calls) > calls  # pause over


def test_new_capacity_is_held_until_someone_subscribes(tmp_path):
    clients = {"us-east-1": FakeEC2("us-east-1", lambda *a: 2)}
    c = Config(instance_types=TYPES[:1], regions=("us-east-1",), durations_days=(1,))
    store, clock = LocalStore(str(tmp_path)), FakeClock()
    kw = dict(limiters=limiters(clock), clock=clock)

    class NoSubscribers(PrintNotifier):
        def has_subscribers(self):
            return False

    quiet = NoSubscribers()
    out = scan_job(c, store, quiet, lambda r: clients[r], "us-east-1", "t", now=NOW, **kw)
    assert out["new_cells"] > 0 and out["published"] == 0 and quiet.sent == []
    loud = PrintNotifier()
    out = scan_job(c, store, loud, lambda r: clients[r], "us-east-1", "t", now=NOW + timedelta(minutes=15), **kw)
    assert out["published"] == 1 and len(loud.sent) == 1
