"""A refused refresh records WHY on the integration row (2026-10-25: Stripe's invalid_grant was
only found by replaying the refresh by hand), never a token, and a reconnect clears it."""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import boto3
import httpx
import pytest
from moto import mock_aws

from core.services.credentials import access_token

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

_EXPIRED = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
_STORED = {"access_token": "AT-secret", "refresh_token": "RT-secret", "token_expiry": _EXPIRED}


@asynccontextmanager
async def _no_lease(owner_id):
    yield


@pytest.fixture
def wired(monkeypatch):
    set_status = AsyncMock()
    monkeypatch.setattr(access_token.owner_lifecycle, "owner_lifecycle_guard", _no_lease)
    monkeypatch.setattr(access_token.integrations_repo, "get", AsyncMock(return_value={"status": "connected"}))
    monkeypatch.setattr(access_token.integrations_repo, "set_status", set_status)
    monkeypatch.setattr(access_token.integration_secret_store, "get", AsyncMock(return_value=dict(_STORED)))
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())
    monkeypatch.setattr(access_token, "_announce_reconnect", AsyncMock())
    monkeypatch.setattr(access_token.oauth_dance, "_oauth_client", AsyncMock(return_value=("cid", "CS-store-secret")))
    return set_status


def _refused(status: int, body) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://connect.stripe.com/oauth/token")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("refused", request=request, response=response)


async def _reason(monkeypatch, set_status, exc) -> str:
    monkeypatch.setattr(access_token.oauth_dance, "refresh", AsyncMock(side_effect=exc))
    with pytest.raises(access_token.ReconnectRequired):
        await access_token.get("org_a", "stripe")
    assert set_status.await_args.args == ("org_a", "stripe", "reconnect_required")
    return set_status.await_args.kwargs["reason"]


@pytest.mark.asyncio
async def test_invalid_grant_is_recorded_and_logged(monkeypatch, wired, caplog):
    exc = _refused(400, {"error": "invalid_grant", "error_description": "Invalid refresh token"})
    with caplog.at_level(logging.WARNING, logger=access_token.__name__):
        reason = await _reason(monkeypatch, wired, exc)

    assert reason == "invalid_grant: Invalid refresh token"
    lines = [r.getMessage() for r in caplog.records if "reconnect_required" in r.getMessage()]
    assert lines == ["integration reconnect_required owner=org_a provider=stripe reason=" + reason]
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_a_body_without_an_oauth_error_records_the_http_status(monkeypatch, wired):
    assert await _reason(monkeypatch, wired, _refused(401, ["nope"])) == "http_401"


@pytest.mark.asyncio
async def test_a_value_error_is_recorded(monkeypatch, wired):
    reason = await _reason(monkeypatch, wired, ValueError("stripe requires your own OAuth app"))
    assert reason == "refresh_error: stripe requires your own OAuth app"


@pytest.mark.asyncio
async def test_a_caller_without_a_reason_records_a_local_code(wired):
    await access_token._mark_reconnect("org_a", "google")
    assert wired.await_args.kwargs["reason"] == "unspecified"  # never phrased as a provider error


@pytest.mark.asyncio
async def test_a_token_echoed_by_the_provider_never_lands_on_the_row(monkeypatch, wired):
    exc = _refused(400, {"error": "invalid_grant", "error_description": "token RT-secret revoked (AT-secret)"})
    reason = await _reason(monkeypatch, wired, exc)
    assert "secret" not in reason
    assert reason == "invalid_grant: token [redacted] revoked ([redacted])"


@pytest.mark.asyncio
async def test_client_secrets_echoed_by_the_provider_are_redacted(monkeypatch, wired):
    # A BYO credential carries its own client_secret, a DCR one a nested copy, and the
    # refresh also sends the app store's secret: none may reach the row or the log.
    stored = {
        **_STORED,
        "client_secret": "CS-byo-secret",
        "client_information": {"client_id": "cid", "client_secret": "CS-dcr-secret"},
    }
    monkeypatch.setattr(access_token.integration_secret_store, "get", AsyncMock(return_value=stored))
    exc = _refused(
        400,
        {"error": "invalid_client", "error_description": "bad CS-byo-secret / CS-dcr-secret / CS-store-secret"},
    )
    reason = await _reason(monkeypatch, wired, exc)
    assert reason == "invalid_client: bad [redacted] / [redacted] / [redacted]"


@pytest.mark.asyncio
async def test_no_oauth_app_still_records_a_reason(monkeypatch, wired):
    monkeypatch.setattr(access_token.oauth_dance, "_oauth_client", AsyncMock(side_effect=ValueError("no app")))
    assert await _reason(monkeypatch, wired, _refused(400, {"error": "invalid_grant"})) == "invalid_grant"


@pytest.fixture
def table():
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-integrations",
            KeySchema=[
                {"AttributeName": "owner_id", "KeyType": "HASH"},
                {"AttributeName": "provider", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "provider", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", client):
            yield


@pytest.mark.asyncio
async def test_a_reconnect_clears_the_reason(table):
    from core.repositories import integrations_repo

    await integrations_repo.put_status("org_a", "stripe", "connected")
    await integrations_repo.set_status("org_a", "stripe", "reconnect_required", reason="invalid_grant")
    row = await integrations_repo.get("org_a", "stripe", consistent=True)
    assert row["reconnect_reason"] == "invalid_grant" and row["reconnect_reason_at"] == row["updated_at"]

    await integrations_repo.put_status("org_a", "stripe", "connected")  # the OAuth callback's write
    row = await integrations_repo.get("org_a", "stripe", consistent=True)
    assert "reconnect_reason" not in row and "reconnect_reason_at" not in row
