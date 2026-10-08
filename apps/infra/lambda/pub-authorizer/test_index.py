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


def test_a_live_lease_connects_and_subscribes_to_its_own_channel_uncached(index):
    assert index.handler(event()) == {"isAuthorized": True, "ttlOverride": 0}
    assert index.handler(event("EVENT_CONNECT", None))["isAuthorized"] is True
    index._table.get_item.assert_called_with(
        Key={"pk": f"PUBWATCH#{PUBLIC}", "sk": f"VIEWER#{hashlib.sha256(KEY.encode()).hexdigest()}"},
        ConsistentRead=True,
    )


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


def test_an_expired_or_unknown_lease_is_refused(index):
    index._table.get_item.return_value = {"Item": {"expiresAt": int(time.time()) - 1}}
    assert index.handler(event())["isAuthorized"] is False
    index._table.get_item.return_value = {}
    assert index.handler(event())["isAuthorized"] is False
