"""Persistent state: DynamoDB in Lambda, JSON files for local runs.

Items (partition key ``pk``):

* ``lock``            lease that keeps two scans from overlapping
* ``state``           what was already notified, max-count hints, discovery cache
* ``snapshot``        result of the latest scan, read by the daily summary
* ``stats#<hour>``    per-hour counters (runs, API calls, errors, notifications)

``state`` and ``snapshot`` are gzip-compressed JSON in a binary attribute.
"""
from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from botocore.exceptions import ClientError

MAX_BLOB_BYTES = 380_000  # DynamoDB items are limited to 400 KB
STATS_TTL = timedelta(days=3)


def _encode(data: dict) -> bytes:
    return gzip.compress(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode())


def _decode(blob: bytes) -> dict:
    return json.loads(gzip.decompress(blob))


def _hour_key(dt: datetime) -> str:
    return "stats#" + dt.strftime("%Y-%m-%dT%H")


class DynamoStore:
    def __init__(self, table_name: str, client=None) -> None:
        if not table_name:
            raise ValueError("TABLE_NAME is not set")
        import boto3

        self.table = table_name
        self.ddb = client or boto3.client("dynamodb")

    def acquire_lock(self, owner: str, lease_seconds: int, now: datetime) -> bool:
        now_s = int(now.timestamp())
        try:
            self.ddb.put_item(
                TableName=self.table,
                Item={
                    "pk": {"S": "lock"},
                    "owner": {"S": owner},
                    "expires": {"N": str(now_s + lease_seconds)},
                    "ttl": {"N": str(now_s + lease_seconds + 3600)},
                },
                ConditionExpression="attribute_not_exists(pk) OR #e < :now",
                ExpressionAttributeNames={"#e": "expires"},
                ExpressionAttributeValues={":now": {"N": str(now_s)}},
            )
            return True
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def release_lock(self, owner: str) -> None:
        try:
            self.ddb.delete_item(
                TableName=self.table,
                Key={"pk": {"S": "lock"}},
                ConditionExpression="#o = :o",
                ExpressionAttributeNames={"#o": "owner"},
                ExpressionAttributeValues={":o": {"S": owner}},
            )
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    def load(self, name: str) -> Optional[dict]:
        resp = self.ddb.get_item(TableName=self.table, Key={"pk": {"S": name}}, ConsistentRead=True)
        item = resp.get("Item")
        return _decode(item["data"]["B"]) if item and "data" in item else None

    def save(self, name: str, data: dict) -> None:
        blob = _encode(data)
        if len(blob) > MAX_BLOB_BYTES:
            raise ValueError(f"{name} is {len(blob)} bytes compressed, above the DynamoDB item limit")
        self.ddb.put_item(TableName=self.table, Item={"pk": {"S": name}, "data": {"B": blob}})

    def add_stats(self, now: datetime, counters: dict[str, int]) -> None:
        counters = {k: int(v) for k, v in counters.items() if v}
        names = {"#ttl": "ttl"}
        values = {":ttl": {"N": str(int((now + STATS_TTL).timestamp()))}}
        adds = []
        for i, (name, value) in enumerate(sorted(counters.items())):
            names[f"#c{i}"] = name
            values[f":c{i}"] = {"N": str(value)}
            adds.append(f"#c{i} :c{i}")
        expr = "SET #ttl = :ttl" + (" ADD " + ", ".join(adds) if adds else "")
        self.ddb.update_item(
            TableName=self.table,
            Key={"pk": {"S": _hour_key(now)}},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    def get_stats(self, now: datetime, hours: int = 24) -> dict[str, int]:
        keys = [{"pk": {"S": _hour_key(now - timedelta(hours=i))}} for i in range(hours)]
        totals: dict[str, int] = {}
        pending = {self.table: {"Keys": keys}}
        while pending:
            resp = self.ddb.batch_get_item(RequestItems=pending)
            for item in resp.get("Responses", {}).get(self.table, []):
                for name, value in item.items():
                    if name not in ("pk", "ttl") and "N" in value:
                        totals[name] = totals.get(name, 0) + int(value["N"])
            pending = resp.get("UnprocessedKeys") or {}
        return totals


class LocalStore:
    """File-based store for running the monitor on a workstation."""

    def __init__(self, directory: str) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def acquire_lock(self, owner: str, lease_seconds: int, now: datetime) -> bool:
        return True

    def release_lock(self, owner: str) -> None:
        return None

    def _path(self, name: str) -> Path:
        return self.dir / f"{name.replace('#', '_')}.json"

    def load(self, name: str) -> Optional[dict]:
        path = self._path(name)
        return json.loads(path.read_text()) if path.exists() else None

    def save(self, name: str, data: dict) -> None:
        tmp = self._path(name).with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, self._path(name))

    def add_stats(self, now: datetime, counters: dict[str, int]) -> None:
        stats = self.load("stats") or {}
        bucket = stats.setdefault(_hour_key(now), {})
        for name, value in counters.items():
            bucket[name] = bucket.get(name, 0) + int(value)
        cutoff = _hour_key(now - STATS_TTL)
        self.save("stats", {k: v for k, v in stats.items() if k >= cutoff})

    def get_stats(self, now: datetime, hours: int = 24) -> dict[str, int]:
        stats = self.load("stats") or {}
        totals: dict[str, int] = {}
        for i in range(hours):
            for name, value in stats.get(_hour_key(now - timedelta(hours=i)), {}).items():
                totals[name] = totals.get(name, 0) + value
        return totals
