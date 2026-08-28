"""Tests for the cloudwatch_logs admin-side wrapper.

Covers:
- Per-user filter with structured-log JSON parse.
- nextToken → cursor pagination (CEO E4).
- ResourceNotFoundException → missing=true (fresh env).
- Malformed JSON line falls through (raw_json=None, message preserved).
- Fleet-scoped recent_errors_fleet returns list[dict].
- Log group ARN matches the actual /ecs/isol8-{env} (not the wrong
  /aws/ecs/isol8-{env}-backend that Phase A originally had).
"""

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
os.environ.setdefault("ENVIRONMENT", "dev")


@pytest.fixture
def boto_logs_mock():
    """Patch the boto3 logs client used by cloudwatch_logs."""
    client = MagicMock()
    # ResourceNotFoundException needs to be a real exception class for `except`
    # to match it; mimic boto3's exceptions namespace.
    client.exceptions = MagicMock()
    client.exceptions.ResourceNotFoundException = type("ResourceNotFoundException", (Exception,), {})
    with patch("core.services.cloudwatch_logs._client", new=client):
        yield client


@pytest.mark.asyncio
async def test_filter_user_logs_parses_structured_json(boto_logs_mock):
    from core.services.cloudwatch_logs import filter_user_logs

    boto_logs_mock.filter_log_events.return_value = {
        "events": [
            {
                "timestamp": 1700000000000,
                "message": '{"user_id":"u1","level":"ERROR","message":"boom","correlation_id":"abc"}',
            },
        ],
    }

    result = await filter_user_logs(user_id="u1", level="ERROR", hours=24, limit=20)

    assert result["missing"] is False
    assert len(result["events"]) == 1
    e = result["events"][0]
    assert e["level"] == "ERROR"
    assert e["message"] == "boom"
    assert e["correlation_id"] == "abc"
    assert e["raw_json"] == {
        "user_id": "u1",
        "level": "ERROR",
        "message": "boom",
        "correlation_id": "abc",
    }


@pytest.mark.asyncio
async def test_filter_user_logs_pagination_cursor(boto_logs_mock):
    from core.services.cloudwatch_logs import filter_user_logs

    boto_logs_mock.filter_log_events.return_value = {
        "events": [],
        "nextToken": "next-page-token",
    }

    result = await filter_user_logs(user_id="u1", level="ERROR", hours=24, limit=20)
    assert result["cursor"] == "next-page-token"


@pytest.mark.asyncio
async def test_filter_user_logs_threads_cursor_into_request(boto_logs_mock):
    from core.services.cloudwatch_logs import filter_user_logs

    boto_logs_mock.filter_log_events.return_value = {"events": []}

    await filter_user_logs(user_id="u1", level="ERROR", hours=24, limit=20, cursor="page-2")

    kwargs = boto_logs_mock.filter_log_events.call_args.kwargs
    assert kwargs["nextToken"] == "page-2"


@pytest.mark.asyncio
async def test_filter_user_logs_uses_correct_log_group(boto_logs_mock):
    """Phase A fix: log group is /ecs/isol8-{env}, NOT /aws/ecs/isol8-{env}-backend."""
    from core.services.cloudwatch_logs import filter_user_logs

    boto_logs_mock.filter_log_events.return_value = {"events": []}

    await filter_user_logs(user_id="u1", level="ERROR", hours=24, limit=20)

    kwargs = boto_logs_mock.filter_log_events.call_args.kwargs
    assert kwargs["logGroupName"] == "/ecs/lightbulb-dev"


@pytest.mark.asyncio
async def test_filter_user_logs_filter_pattern_includes_user_and_level(boto_logs_mock):
    from core.services.cloudwatch_logs import filter_user_logs

    boto_logs_mock.filter_log_events.return_value = {"events": []}

    await filter_user_logs(user_id="user_xyz", level="ERROR", hours=24, limit=20)

    kwargs = boto_logs_mock.filter_log_events.call_args.kwargs
    pattern = kwargs["filterPattern"]
    assert "user_xyz" in pattern
    assert "ERROR" in pattern


@pytest.mark.asyncio
async def test_filter_user_logs_handles_malformed_json(boto_logs_mock):
    from core.services.cloudwatch_logs import filter_user_logs

    boto_logs_mock.filter_log_events.return_value = {
        "events": [{"timestamp": 1700000000000, "message": "not json at all"}],
    }

    result = await filter_user_logs(user_id="u1", level="ERROR", hours=24, limit=20)
    assert result["events"][0]["raw_json"] is None
    assert result["events"][0]["message"] == "not json at all"
    assert result["events"][0]["level"] is None


@pytest.mark.asyncio
async def test_filter_user_logs_returns_missing_on_log_group_not_found(boto_logs_mock):
    from core.services.cloudwatch_logs import filter_user_logs

    boto_logs_mock.filter_log_events.side_effect = boto_logs_mock.exceptions.ResourceNotFoundException()

    result = await filter_user_logs(user_id="u1", level="ERROR", hours=24, limit=20)
    assert result["events"] == []
    assert result["cursor"] is None
    assert result["missing"] is True


@pytest.mark.asyncio
async def test_filter_user_logs_swallows_other_errors_and_returns_empty(boto_logs_mock):
    """Non-NotFound errors shouldn't crash the admin page — degrade gracefully."""
    from core.services.cloudwatch_logs import filter_user_logs

    boto_logs_mock.filter_log_events.side_effect = RuntimeError("transient_aws_blip")

    result = await filter_user_logs(user_id="u1", level="ERROR", hours=24, limit=20)
    assert result["events"] == []
    assert result["missing"] is False
    assert "error" in result


@pytest.mark.asyncio
async def test_recent_errors_fleet_returns_list(boto_logs_mock):
    from core.services.cloudwatch_logs import recent_errors_fleet

    boto_logs_mock.filter_log_events.return_value = {
        "events": [
            {"timestamp": 1700000000000, "message": '{"user_id":"u1","level":"ERROR","message":"boom"}'},
            {"timestamp": 1700000001000, "message": '{"user_id":"u2","level":"ERROR","message":"crash"}'},
        ],
    }

    result = await recent_errors_fleet(hours=24, limit=10)
    assert len(result) == 2
    assert result[0]["user_id"] == "u1"
    assert result[1]["user_id"] == "u2"


@pytest.mark.asyncio
async def test_recent_errors_fleet_log_group_not_found_returns_empty(boto_logs_mock):
    from core.services.cloudwatch_logs import recent_errors_fleet

    boto_logs_mock.filter_log_events.side_effect = boto_logs_mock.exceptions.ResourceNotFoundException()

    result = await recent_errors_fleet(hours=24, limit=10)
    assert result == []
