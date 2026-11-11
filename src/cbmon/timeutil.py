"""Time helpers. All internal times are timezone-aware UTC datetimes."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

UTC = timezone.utc

# Used only if the runtime has no IANA time zone database.
_FALLBACK_OFFSETS_HOURS = {
    "UTC": 0,
    "Etc/UTC": 0,
    "Asia/Shanghai": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Taipei": 8,
    "Asia/Singapore": 8,
    "Asia/Tokyo": 9,
    "Asia/Seoul": 9,
}

_TZ_LABELS = {"Asia/Shanghai": "北京时间", "UTC": "UTC", "Etc/UTC": "UTC"}


def utcnow() -> datetime:
    return datetime.now(UTC)


def resolve_tz(name: str) -> tzinfo:
    """Return a tzinfo for an IANA name (e.g. Asia/Shanghai) or "UTC+8"."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # missing tzdata or unknown name
        pass
    match = re.fullmatch(r"UTC([+-])(\d{1,2})(?::?(\d{2}))?", name.strip())
    if match:
        sign = 1 if match.group(1) == "+" else -1
        minutes = int(match.group(2)) * 60 + int(match.group(3) or 0)
        return timezone(sign * timedelta(minutes=minutes), name)
    if name in _FALLBACK_OFFSETS_HOURS:
        return timezone(timedelta(hours=_FALLBACK_OFFSETS_HOURS[name]), name)
    return UTC


def tz_label(name: str) -> str:
    return _TZ_LABELS.get(name, name)


def is_utc(name: str) -> bool:
    return name in ("UTC", "Etc/UTC", "UTC+0", "UTC+00:00")


def week_start(dt: datetime) -> datetime:
    """Monday 00:00 UTC of the week that contains ``dt``."""
    d = dt.astimezone(UTC)
    midnight = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return midnight - timedelta(days=d.weekday())


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_java_date(text: str) -> datetime:
    """Parse dates in EC2 error messages, e.g. "Fri Nov 13 11:43:41 UTC 2026"."""
    parts = text.split()
    if len(parts) != 6 or parts[4] not in ("UTC", "GMT"):
        raise ValueError(f"unexpected date format: {text!r}")
    dt = datetime.strptime(f"{parts[1]} {parts[2]} {parts[3]} {parts[5]}", "%b %d %H:%M:%S %Y")
    return dt.replace(tzinfo=UTC)
