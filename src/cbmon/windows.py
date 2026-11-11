"""Split the search window into start-date slices."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .timeutil import week_start

# Never ask for offerings that start sooner than this.
MIN_LEAD = timedelta(minutes=10)


@dataclass(frozen=True)
class Slice:
    """A range of allowed start times.

    ``key`` stays the same for the whole calendar week, so a capacity cell is
    recognised across runs even though the window itself moves with the clock.
    """

    key: str  # Monday of the UTC week (YYYY-MM-DD), or "all"
    start: datetime  # inclusive
    end: datetime  # exclusive


def build_slices(
    now: datetime, min_offset_days: float, max_advance_days: float, weekly: bool
) -> list[Slice]:
    window_start = max(now + timedelta(days=min_offset_days), now + MIN_LEAD)
    window_end = now + timedelta(days=max_advance_days)
    if window_start >= window_end:
        return []
    if not weekly:
        return [Slice("all", window_start, window_end)]
    slices = []
    week = week_start(window_start)
    while week < window_end:
        start = max(week, window_start)
        end = min(week + timedelta(days=7), window_end)
        if start < end:
            slices.append(Slice(week.strftime("%Y-%m-%d"), start, end))
        week += timedelta(days=7)
    return slices
