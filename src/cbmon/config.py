"""Runtime configuration.

Lambda reads it from environment variables set by the CloudFormation template.
The local CLI reads the same variables and can override some of them with flags.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

DEFAULT_INSTANCE_TYPES = "p6-b300.48xlarge,p6-b200.48xlarge"

# Every commercial US and EU Region. Discovery drops the ones that are not
# enabled, do not offer Capacity Blocks, or do not offer the instance type.
DEFAULT_REGIONS = (
    "us-east-1,us-east-2,us-west-1,us-west-2,"
    "eu-west-1,eu-west-2,eu-west-3,eu-central-1,eu-central-2,"
    "eu-north-1,eu-south-1,eu-south-2"
)

# 1-14 days in 1-day steps, then 21-175 days in 7-day steps (37 durations).
DEFAULT_DURATIONS = "1-14,21-175/7"


def split_list(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def parse_durations(spec: str) -> tuple[int, ...]:
    """Parse a duration spec in days, e.g. ``"1-14,21-175/7"``.

    Each comma-separated part is ``N``, ``A-B`` or ``A-B/STEP``. The result is
    checked against the Capacity Blocks rule: 1-14 days in 1-day increments,
    and multiples of 7 days up to 182 days.
    """
    days: set[int] = set()
    for raw in split_list(spec):
        part, step = raw, 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = int(step_text)
        if "-" in part:
            lo_text, hi_text = part.split("-", 1)
            lo, hi = int(lo_text), int(hi_text)
        else:
            lo = hi = int(part)
        if step < 1 or lo < 1 or hi < lo:
            raise ValueError(f"invalid duration range: {raw!r}")
        days.update(range(lo, hi + 1, step))
    if not days:
        raise ValueError("no durations configured")
    bad = [d for d in sorted(days) if not (d <= 14 or (d % 7 == 0 and d <= 182))]
    if bad:
        raise ValueError(
            "durations not allowed by Capacity Blocks "
            f"(1-14 days, or multiples of 7 days up to 182): {bad}"
        )
    return tuple(sorted(days))


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    instance_types: tuple[str, ...] = split_list(DEFAULT_INSTANCE_TYPES)
    regions: tuple[str, ...] = split_list(DEFAULT_REGIONS)
    durations_days: tuple[int, ...] = parse_durations(DEFAULT_DURATIONS)
    min_start_offset_days: float = 7.0
    max_advance_days: float = 56.0
    weekly_slices: bool = True
    max_instance_count: int = 64
    exclude_local_zones: bool = True
    # DescribeCapacityBlockOfferings refills at ~9 requests/minute per account and Region.
    api_calls_per_minute: float = 6.0
    display_timezone: str = "Asia/Shanghai"
    scan_interval_minutes: int = 15
    # A week's staircase (max count per duration) is re-measured this often;
    # weeks with no known capacity are checked on every run.
    detail_refresh_minutes: int = 60
    reappear_after_minutes: int = 120
    discovery_ttl_hours: int = 24
    table_name: str = ""
    topic_arn: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        d = cls()

        def get(name: str, default: object) -> str:
            value = env.get(name)
            return str(default) if value is None or not value.strip() else value.strip()

        cfg = cls(
            instance_types=split_list(get("INSTANCE_TYPES", ",".join(d.instance_types))),
            regions=split_list(get("CANDIDATE_REGIONS", ",".join(d.regions))),
            durations_days=parse_durations(get("DURATIONS_DAYS", DEFAULT_DURATIONS)),
            min_start_offset_days=float(get("MIN_START_OFFSET_DAYS", d.min_start_offset_days)),
            max_advance_days=float(get("MAX_ADVANCE_DAYS", d.max_advance_days)),
            weekly_slices=_bool(get("WEEKLY_SLICES", "true")),
            max_instance_count=int(get("MAX_INSTANCE_COUNT", d.max_instance_count)),
            exclude_local_zones=_bool(get("EXCLUDE_LOCAL_ZONES", "true")),
            api_calls_per_minute=float(get("API_CALLS_PER_MINUTE", d.api_calls_per_minute)),
            display_timezone=get("DISPLAY_TIMEZONE", d.display_timezone),
            scan_interval_minutes=int(get("SCAN_INTERVAL_MINUTES", d.scan_interval_minutes)),
            detail_refresh_minutes=int(get("DETAIL_REFRESH_MINUTES", d.detail_refresh_minutes)),
            reappear_after_minutes=int(get("REAPPEAR_AFTER_MINUTES", d.reappear_after_minutes)),
            discovery_ttl_hours=int(get("DISCOVERY_TTL_HOURS", d.discovery_ttl_hours)),
            table_name=env.get("TABLE_NAME", "").strip(),
            topic_arn=env.get("TOPIC_ARN", "").strip(),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.instance_types:
            raise ValueError("INSTANCE_TYPES is empty")
        if not self.regions:
            raise ValueError("CANDIDATE_REGIONS is empty")
        if not 1 <= self.max_instance_count <= 64:
            raise ValueError("MAX_INSTANCE_COUNT must be between 1 and 64")
        if self.min_start_offset_days < 0:
            raise ValueError("MIN_START_OFFSET_DAYS must be >= 0")
        if not self.min_start_offset_days < self.max_advance_days <= 56:
            raise ValueError("MAX_ADVANCE_DAYS must be greater than MIN_START_OFFSET_DAYS and at most 56")
        if not 0 < self.api_calls_per_minute <= 60:
            raise ValueError("API_CALLS_PER_MINUTE must be between 0 and 60")
