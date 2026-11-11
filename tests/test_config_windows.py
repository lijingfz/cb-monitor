from datetime import timedelta

import pytest

from cbmon.config import Config, parse_durations
from cbmon.timeutil import UTC, parse_java_date, resolve_tz, week_start
from cbmon.windows import build_slices


def test_default_durations_are_1_to_14_then_weekly_to_175():
    days = parse_durations("1-14,21-175/7")
    assert len(days) == 37
    assert days[:14] == tuple(range(1, 15))
    assert days[14:16] == (21, 28)
    assert days[-1] == 175
    assert 182 not in days


@pytest.mark.parametrize("spec", ["15", "1-20", "0-3", "5-1", ""])
def test_invalid_durations_rejected(spec):
    with pytest.raises(ValueError):
        parse_durations(spec)


def test_config_from_env():
    cfg = Config.from_env(
        {
            "INSTANCE_TYPES": "p6-b200.48xlarge",
            "CANDIDATE_REGIONS": "us-east-1, eu-west-3",
            "MIN_START_OFFSET_DAYS": "0",
            "WEEKLY_SLICES": "false",
            "TABLE_NAME": "t",
        }
    )
    assert cfg.instance_types == ("p6-b200.48xlarge",)
    assert cfg.regions == ("us-east-1", "eu-west-3")
    assert cfg.min_start_offset_days == 0
    assert cfg.weekly_slices is False
    assert cfg.max_instance_count == 64
    assert len(cfg.durations_days) == 37


def test_config_rejects_bad_counts():
    with pytest.raises(ValueError):
        Config.from_env({"MAX_INSTANCE_COUNT": "65"})


def test_weekly_slices_cover_window(now):
    slices = build_slices(now, 7, 56, weekly=True)
    assert slices[0].start == now + timedelta(days=7)
    assert slices[-1].end == now + timedelta(days=56)
    assert len(slices) == 8
    for a, b in zip(slices, slices[1:]):
        assert a.end == b.start
        assert b.start == week_start(b.start)  # later slices begin on Monday 00:00 UTC
    assert slices[0].key == "2026-11-16"


def test_slice_keys_stable_while_window_moves(now):
    a = build_slices(now, 7, 56, weekly=True)
    b = build_slices(now + timedelta(hours=6), 7, 56, weekly=True)
    assert a[0].key == b[0].key
    assert a[0].start < b[0].start


def test_single_slice_mode(now):
    (s,) = build_slices(now, 7, 56, weekly=False)
    assert s.key == "all"


def test_parse_java_date():
    assert parse_java_date("Fri Nov 13 11:43:41 UTC 2026").isoformat() == "2026-11-13T11:43:41+00:00"


def test_resolve_tz_offsets():
    assert resolve_tz("UTC+8").utcoffset(None) == timedelta(hours=8)
    assert resolve_tz("no/such-zone") is UTC
