import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cbmon.ec2query import RateLimiter  # noqa: E402
from cbmon.timeutil import UTC  # noqa: E402

# A Wednesday
NOW = datetime(2026, 11, 11, 11, 45, tzinfo=UTC)
REGIONS = ("us-east-1", "us-west-2", "eu-west-1", "eu-south-1")


def client_error(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "DescribeCapacityBlockOfferings")


def java(dt: datetime) -> str:
    return dt.strftime("%a %b %d %H:%M:%S UTC %Y")


class FakeClock:
    """Monotonic clock whose sleep() just moves time forward."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def limiters(clock: FakeClock, regions=REGIONS, per_minute=600.0):
    return {r: RateLimiter(per_minute, burst=10, clock=clock, sleep=clock.sleep) for r in regions}


class FakeEC2:
    """Simulates DescribeCapacityBlockOfferings for one Region.

    ``capacity(instance_type, start, hours)`` returns how many instances are
    available for a block starting at ``start`` (11:30 UTC each day). Like the
    real API it returns the earliest matching offerings first.
    ``max_start_ahead`` / ``max_end_ahead`` / ``max_hours`` reproduce an
    account with a narrower search window than the documented one.
    ``fail_after`` makes every call after that many fail with ``fail_code``.
    """

    def __init__(
        self,
        region,
        capacity,
        now=NOW,
        max_start_ahead=None,
        max_end_ahead=None,
        max_hours=None,
        unsupported_types=(),
        no_capacity_blocks=False,
        zone_type="availability-zone",
        fail_after=None,
        fail_code="RequestLimitExceeded",
    ):
        self.region = region
        self.capacity = capacity
        self.now = now
        self.max_start_ahead = max_start_ahead
        self.max_end_ahead = max_end_ahead
        self.max_hours = max_hours
        self.unsupported_types = set(unsupported_types)
        self.no_capacity_blocks = no_capacity_blocks
        self.zone_type = zone_type
        self.fail_after = fail_after
        self.fail_code = fail_code
        self.calls = []

    def describe_regions(self, AllRegions=True):
        return {
            "Regions": [
                {"RegionName": "us-east-1", "OptInStatus": "opt-in-not-required"},
                {"RegionName": "us-west-2", "OptInStatus": "opt-in-not-required"},
                {"RegionName": "eu-west-1", "OptInStatus": "opt-in-not-required"},
                {"RegionName": "eu-south-1", "OptInStatus": "not-opted-in"},
            ]
        }

    def describe_capacity_block_offerings(self, **kw):
        self.calls.append(kw)
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise client_error(self.fail_code, "Request limit exceeded.")
        if self.no_capacity_blocks:
            raise client_error("InvalidAction", "The action DescribeCapacityBlockOfferings is not valid for this web service.")
        itype = kw["InstanceType"]
        if itype in self.unsupported_types:
            raise client_error(
                "InvalidParameterValue",
                f"The instance type '{itype}' is not supported for Capacity Blocks in '{self.region}'.",
            )
        count, hours = kw["InstanceCount"], kw["CapacityDurationHours"]
        start = kw.get("StartDateRange", self.now)
        default_end = self.now + (self.max_end_ahead or timedelta(days=60 + hours / 24))
        end = kw.get("EndDateRange", default_end)
        if self.max_start_ahead is not None and start >= self.now + self.max_start_ahead:
            raise client_error(
                "InvalidParameterValue",
                f"The start date is not valid. Start date {java(start)} must be before "
                f"{java(self.now + self.max_start_ahead)}. Specify a new start date and try again.",
            )
        if self.max_end_ahead is not None and end > self.now + self.max_end_ahead:
            raise client_error(
                "InvalidParameterValue",
                f"The end date is not valid. End date must not be later than "
                f"{java(self.now + self.max_end_ahead)}. Specify a new end date and try again.",
            )
        if (self.max_hours is not None and hours > self.max_hours) or not (
            hours <= 14 * 24 or (hours % 168 == 0 and hours <= 182 * 24)
        ):
            raise client_error("InvalidParameterValue", "The duration is not valid. Specify a new duration and try again.")
        if not 1 <= count <= 64:
            raise client_error("InvalidParameterValue", "The instance count is not valid.")
        day = datetime(start.year, start.month, start.day, 11, 30, tzinfo=UTC)
        if day < start:
            day += timedelta(days=1)
        offers = []
        while day + timedelta(hours=hours) <= end and len(offers) < 2:
            if self.capacity(itype, day, hours) >= count:
                offers.append(
                    {
                        "CapacityBlockOfferingId": f"cb-{self.region}-{day:%m%d}-{hours}-{count}",
                        "InstanceType": itype,
                        "AvailabilityZone": f"{self.region}a",
                        "InstanceCount": count,
                        "StartDate": day,
                        "EndDate": day + timedelta(hours=hours),
                        "CapacityBlockDurationHours": hours,
                        "UpfrontFee": f"{100.0 * count * hours:.4f}",
                        "CurrencyCode": "USD",
                        "Tenancy": "default",
                        "ZoneType": self.zone_type,
                    }
                )
            day += timedelta(days=1)
        return {"CapacityBlockOfferings": offers}


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def now():
    return NOW
