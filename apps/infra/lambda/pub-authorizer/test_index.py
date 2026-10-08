"""Who may hear a public session link's terminal: a live lease, its own channel, nothing else."""

import hashlib
import importlib.util
import time
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
    module._table.get_item.return_value = {"Item": {"expiresAt": int(time.time()) + 30}}
    return module


def event(operation="EVENT_SUBSCRIBE", channel=f"/pub/{PUBLIC}", token=f"{PUBLIC}.{KEY}"):
    return {"authorizationToken": token, "requestContext": {"operation": operation, "channel": channel}}


SEAT = {"pk": f"PUBWATCH#{PUBLIC}", "sk": f"VIEWER#{hashlib.sha256(KEY.encode()).hexdigest()}"}


def _refusal():
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


def test_a_live_lease_connects_and_subscribes_to_its_own_channel_uncached(index):
    assert index.handler(event()) == {"isAuthorized": True, "ttlOverride": 0}
    update = index._table.update_item.call_args.kwargs
    assert update["Key"] == SEAT
    # The subscribe consumes the lease, in the same write that checks it is live.
    assert "attribute_not_exists(subscribedAt)" in update["ConditionExpression"]
    assert "expiresAt > :now" in update["ConditionExpression"]
    assert index.handler(event("EVENT_CONNECT", None))["isAuthorized"] is True
    index._table.get_item.assert_called_with(Key=SEAT, ConsistentRead=True)


def test_a_lease_subscribes_once(index):
    index._table.update_item.side_effect = [None, _refusal()]
    assert index.handler(event())["isAuthorized"] is True
    assert index.handler(event())["isAuthorized"] is False, "a second socket needs its own seat"


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
    index._table.get_item.assert_not_called()
    index._table.update_item.assert_not_called()


def test_an_expired_or_unknown_lease_is_refused(index):
    index._table.get_item.return_value = {"Item": {"expiresAt": int(time.time()) - 1}}
    assert index.handler(event("EVENT_CONNECT", None))["isAuthorized"] is False
    index._table.get_item.return_value = {}
    assert index.handler(event("EVENT_CONNECT", None))["isAuthorized"] is False
    index._table.update_item.side_effect = _refusal()
    assert index.handler(event())["isAuthorized"] is False
