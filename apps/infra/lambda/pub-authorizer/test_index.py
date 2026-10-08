"""Who may hear a public session link's terminal: a live lease, its own channel, nothing else."""

import hashlib
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PUBLIC = "0b0f1e8e-6f0a-4c0e-9d53-0c2a8f8d2a11"
KEY = "k" * 43


@pytest.fixture
def index(monkeypatch):
    monkeypatch.setenv("MESSAGING_TABLE", "t")
    with patch("boto3.resource", return_value=MagicMock()):
        spec = importlib.util.spec_from_file_location("pub_authorizer_index", Path(__file__).with_name("index.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    module._table = MagicMock()
    return module


def event(operation="EVENT_SUBSCRIBE", channel=f"/pub/{PUBLIC}", token=f"{PUBLIC}.{KEY}"):
    return {"authorizationToken": token, "requestContext": {"operation": operation, "channel": channel}}


SEAT = {"pk": f"PUBWATCH#{PUBLIC}", "sk": f"VIEWER#{hashlib.sha256(KEY.encode()).hexdigest()}"}


def _refusal():
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


def test_a_live_lease_connects_once_and_subscribes_once_to_its_own_channel_uncached(index):
    assert index.handler(event("EVENT_CONNECT", None)) == {"isAuthorized": True, "ttlOverride": 0}
    assert index.handler(event()) == {"isAuthorized": True, "ttlOverride": 0}
    connect, subscribe = (call.kwargs for call in index._table.update_item.call_args_list)
    # Each consumes the lease, in the same write that checks it is live.
    for write, mark in ((connect, "connectedAt"), (subscribe, "subscribedAt")):
        assert write["Key"] == SEAT
        assert write["UpdateExpression"] == f"SET {mark} = :now"
        assert f"attribute_not_exists({mark})" in write["ConditionExpression"]
        assert "expiresAt > :now" in write["ConditionExpression"]


@pytest.mark.parametrize("operation, channel", [("EVENT_CONNECT", None), ("EVENT_SUBSCRIBE", f"/pub/{PUBLIC}")])
def test_a_lease_is_good_for_one_connection_and_one_subscription(index, operation, channel):
    index._table.update_item.side_effect = [None, _refusal()]
    assert index.handler(event(operation, channel))["isAuthorized"] is True
    assert index.handler(event(operation, channel))["isAuthorized"] is False, "a second one needs its own seat"


def test_a_failed_write_is_an_error_not_an_admission(index):
    index._table.update_item.side_effect = RuntimeError("dynamo down")
    with pytest.raises(RuntimeError):  # AppSync reads a raised error as unauthorized
        index.handler(event())


@pytest.mark.parametrize(
    "refused",
    [
        event(channel="/pub/*"),  # a wildcard would hear every public session
        event(channel="/pub/11111111-1111-4111-8111-111111111111"),
        event(channel=None),  # a subscribe AppSync did not name fails closed
        event("EVENT_PUBLISH"),
        event(token=PUBLIC),
        event(token=""),
    ],
)
def test_anything_else_is_refused_without_a_read(index, refused):
    assert index.handler(refused)["isAuthorized"] is False
    index._table.update_item.assert_not_called()


def test_an_expired_or_unknown_lease_is_refused(index):
    index._table.update_item.side_effect = _refusal()  # the write's own condition: exists and unexpired
    assert index.handler(event("EVENT_CONNECT", None))["isAuthorized"] is False
    assert index.handler(event())["isAuthorized"] is False
