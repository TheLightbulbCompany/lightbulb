"""dlq.queue_name(): derived from the deployed work-queue URL, never guessed."""

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.ops import dlq  # noqa: E402


def test_queue_name_is_derived_from_the_deployed_work_queue_url(monkeypatch):
    """api-stack.ts creates both queues off one prefix, so the DLQ's name follows
    whatever CDK actually wired in — a re-prefixed queue cannot leave this reader
    pointed at a name nothing publishes."""
    monkeypatch.setattr(
        dlq.settings,
        "OPENCLAW_WORK_QUEUE_URL",
        "https://sqs.us-east-1.amazonaws.com/877352799272/lightbulb-prod-openclaw-work.fifo",
        raising=False,
    )
    assert dlq.queue_name() == "lightbulb-prod-openclaw-work-dlq.fifo"


def test_queue_name_falls_back_to_the_env_literal_when_unset(monkeypatch):
    monkeypatch.setattr(dlq.settings, "OPENCLAW_WORK_QUEUE_URL", "", raising=False)
    monkeypatch.setattr(dlq.settings, "ENVIRONMENT", "dev", raising=False)
    assert dlq.queue_name() == "lightbulb-dev-openclaw-work-dlq.fifo"


@pytest.mark.asyncio
async def test_incomplete_metric_read_raises_rather_than_reporting_zero(monkeypatch):
    """A Forbidden/PartialData result read as 0 would show an empty DLQ on the
    dashboard while replies pile up in it."""
    monkeypatch.setattr(dlq.settings, "OPENCLAW_WORK_QUEUE_URL", "", raising=False)
    cw = MagicMock()
    cw.get_metric_data.return_value = {
        "MetricDataResults": [{"Id": "dlq", "StatusCode": "Forbidden", "Messages": [{"Value": "denied"}]}]
    }
    with patch("boto3.client", return_value=cw), pytest.raises(RuntimeError, match="Forbidden"):
        await dlq.work_dlq_depth()


@pytest.mark.asyncio
async def test_depth_is_the_newest_datapoint(monkeypatch):
    monkeypatch.setattr(dlq.settings, "OPENCLAW_WORK_QUEUE_URL", "", raising=False)
    cw = MagicMock()
    cw.get_metric_data.return_value = {
        "MetricDataResults": [{"Id": "dlq", "StatusCode": "Complete", "Values": [4.0, 9.0]}]
    }
    with patch("boto3.client", return_value=cw):
        assert await dlq.work_dlq_depth() == 4  # ScanBy=TimestampDescending
    assert cw.get_metric_data.call_args.kwargs["ScanBy"] == "TimestampDescending"


@pytest.mark.asyncio
async def test_no_datapoints_is_an_empty_queue(monkeypatch):
    monkeypatch.setattr(dlq.settings, "OPENCLAW_WORK_QUEUE_URL", "", raising=False)
    cw = MagicMock()
    cw.get_metric_data.return_value = {"MetricDataResults": [{"Id": "dlq", "StatusCode": "Complete", "Values": []}]}
    with patch("boto3.client", return_value=cw):
        assert await dlq.work_dlq_depth() == 0
