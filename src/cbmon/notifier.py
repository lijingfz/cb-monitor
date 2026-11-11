"""Publish messages to SNS (email), or print them."""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

SNS_MAX_BYTES = 256 * 1024
_BODY_BUDGET = SNS_MAX_BYTES - 4 * 1024
_TRUNCATED = "\n\n……（内容过长，已截断。完整结果见 Lambda 日志或 DynamoDB snapshot 项。）"


def _clean_subject(subject: str) -> str:
    subject = re.sub(r"[\r\n\t]+", " ", subject).strip()
    return subject[:99]


def _fit_body(body: str) -> str:
    data = body.encode()
    if len(data) <= _BODY_BUDGET:
        return body
    return data[:_BODY_BUDGET].decode(errors="ignore") + _TRUNCATED


class SnsNotifier:
    def __init__(self, topic_arn: str, client=None) -> None:
        if not topic_arn:
            raise ValueError("TOPIC_ARN is not set")
        import boto3

        self.topic_arn = topic_arn
        region = topic_arn.split(":")[3]
        self.sns = client or boto3.client("sns", region_name=region)

    def has_subscribers(self) -> bool:
        """True if at least one subscription is confirmed (or the check itself fails)."""
        try:
            attrs = self.sns.get_topic_attributes(TopicArn=self.topic_arn)["Attributes"]
            return int(attrs.get("SubscriptionsConfirmed", "0")) > 0
        except Exception:
            log.exception("could not read topic attributes; assuming subscribers exist")
            return True

    def publish(self, subject: str, body: str) -> str:
        resp = self.sns.publish(TopicArn=self.topic_arn, Subject=_clean_subject(subject), Message=_fit_body(body))
        log.info("published %s: %s", resp.get("MessageId"), subject)
        return resp.get("MessageId", "")


class PrintNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def has_subscribers(self) -> bool:
        return True

    def publish(self, subject: str, body: str) -> str:
        self.sent.append((subject, body))
        print(f"\n===== {subject} =====\n{body}\n===== end =====\n", flush=True)
        return "printed"
