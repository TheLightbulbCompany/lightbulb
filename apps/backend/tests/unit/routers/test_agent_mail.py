"""Unit tests for POST /api/v1/webhooks/resend-inbound (inbound agent email).

One test per branch of the orchestration in ``routers/agent_mail.py``:
feature-gate disabled, bad signature, duplicate svix-id, unknown recipient,
unverified sender (+ the public-address bypass), over cap, fetch failure,
auto-submitted loop guard, the happy path, and the top-level catch-all that
guarantees the handler never returns 5xx.

Seams (``verify_svix_signature``, ``record_event_or_skip``,
``get_by_address``, ``trial_block_reason``, ``fetch_received_email``,
``deliver_to_agent``, …) are imported BY NAME into ``routers.agent_mail``'s
module namespace, so tests monkeypatch them as attributes on that module
rather than on their defining modules — this mirrors
``tests/unit/routers/test_metronome_webhook.py``'s ``routers.metronome_webhook.*``
patch-target convention.
"""

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.services.webhook_dedup import WebhookDedupResult
from routers import agent_mail

SECRET = "whsec_resend_test"
DOMAIN = "agents.isol8.co"

OWNER_ID = "owner_1"
AGENT_ID = "agent_1"
EMAIL_ID = "email_123"
SENDER = "sender@example.com"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(agent_mail.router, prefix="/api/v1/webhooks")
    return TestClient(app)


def _enable(monkeypatch):
    monkeypatch.setattr(agent_mail.settings, "RESEND_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(agent_mail.settings, "AGENT_EMAIL_DOMAIN", DOMAIN)


def _valid_sig(monkeypatch):
    monkeypatch.setattr(agent_mail, "verify_svix_signature", lambda **kwargs: True)


def _dedup_recorded(monkeypatch):
    monkeypatch.setattr(
        agent_mail,
        "record_event_or_skip",
        AsyncMock(return_value=WebhookDedupResult.RECORDED),
    )


def _row(*, public: bool = False, verified_senders=None):
    return {
        "owner_id": OWNER_ID,
        "agent_id": AGENT_ID,
        "public": public,
        "verified_senders": verified_senders if verified_senders is not None else [SENDER],
    }


def _body(**data_overrides):
    payload = {
        "type": "email.received",
        "data": {
            "email_id": EMAIL_ID,
            "to": ["agent@agents.isol8.co"],
            "from": f"Sender Name <{SENDER}>",
            "subject": "Hello",
            "message_id": "<msg-1@example.com>",
        },
    }
    payload["data"].update(data_overrides)
    return json.dumps(payload).encode()


def _post(client, body: bytes):
    return client.post(
        "/api/v1/webhooks/resend-inbound",
        content=body,
        headers={
            "svix-id": "evt-1",
            "svix-timestamp": "1700000000",
            "svix-signature": "v1,deadbeef",
        },
    )


def test_disabled_when_secret_unset(client, monkeypatch):
    monkeypatch.setattr(agent_mail.settings, "RESEND_WEBHOOK_SECRET", "")
    monkeypatch.setattr(agent_mail.settings, "AGENT_EMAIL_DOMAIN", DOMAIN)
    deliver_mock = AsyncMock()
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    deliver_mock.assert_not_called()


def test_bad_signature_returns_401(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(agent_mail, "verify_svix_signature", lambda **kwargs: False)

    resp = _post(client, _body())

    assert resp.status_code == 401
    assert resp.json()["status"] == "bad_signature"


def test_duplicate_svix_id_returns_duplicate(client, monkeypatch):
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    monkeypatch.setattr(
        agent_mail,
        "record_event_or_skip",
        AsyncMock(return_value=WebhookDedupResult.ALREADY_SEEN),
    )
    deliver_mock = AsyncMock()
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "duplicate"
    deliver_mock.assert_not_called()


def test_unknown_recipient_is_dropped(client, monkeypatch):
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(agent_mail, "get_by_address", AsyncMock(return_value=None))
    deliver_mock = AsyncMock()
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_unknown_recipient"
    deliver_mock.assert_not_called()


def test_unverified_sender_is_dropped(client, monkeypatch):
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(
        agent_mail,
        "get_by_address",
        AsyncMock(return_value=_row(public=False, verified_senders=["someone-else@example.com"])),
    )
    deliver_mock = AsyncMock()
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_unverified_sender"
    deliver_mock.assert_not_called()


def test_public_address_bypasses_unverified_sender_and_delivers(client, monkeypatch):
    """A public address has no verified-sender allowlist to enforce."""
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(
        agent_mail,
        "get_by_address",
        AsyncMock(return_value=_row(public=True, verified_senders=[])),
    )
    monkeypatch.setattr(agent_mail, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_mail,
        "fetch_received_email",
        AsyncMock(return_value={"headers": {"Authentication-Results": "dmarc=pass"}, "text": "hi"}),
    )
    deliver_mock = AsyncMock(return_value="delivered")
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    deliver_mock.assert_awaited_once()


def test_over_cap_is_dropped(client, monkeypatch):
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(agent_mail, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(agent_mail, "trial_block_reason", AsyncMock(return_value="trial_daily_cap"))
    fetch_mock = AsyncMock()
    monkeypatch.setattr(agent_mail, "fetch_received_email", fetch_mock)
    deliver_mock = AsyncMock()
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_over_cap"
    fetch_mock.assert_not_awaited()
    deliver_mock.assert_not_called()


def test_fetch_failure_is_dropped(client, monkeypatch):
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(agent_mail, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(agent_mail, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_mail, "fetch_received_email", AsyncMock(return_value=None))
    deliver_mock = AsyncMock()
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_fetch_failed"
    deliver_mock.assert_not_called()


def test_auto_submitted_is_dropped(client, monkeypatch):
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(agent_mail, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(agent_mail, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_mail,
        "fetch_received_email",
        AsyncMock(return_value={"headers": {"Auto-Submitted": "auto-replied"}, "text": "beep boop"}),
    )
    deliver_mock = AsyncMock()
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_auto_submitted"
    deliver_mock.assert_not_called()


def test_failed_authentication_is_dropped(client, monkeypatch):
    """A message whose fetched headers fail SPF/DKIM/DMARC must be dropped even
    when the From address matches an owner's verified_senders — the From
    header alone is forgeable, so allowlist membership is not sufficient."""
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(agent_mail, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(agent_mail, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_mail,
        "fetch_received_email",
        AsyncMock(
            return_value={
                "headers": {"Authentication-Results": "spf=fail; dkim=fail; dmarc=fail"},
                "text": "gimme the wire instructions",
            }
        ),
    )
    deliver_mock = AsyncMock()
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_failed_authentication"
    deliver_mock.assert_not_called()


def test_require_auth_false_delivers_despite_failed_authentication(client, monkeypatch):
    """AGENT_EMAIL_REQUIRE_AUTH=False governs the gate: with it off, a failing
    authentication verdict no longer blocks delivery."""
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(agent_mail.settings, "AGENT_EMAIL_REQUIRE_AUTH", False)
    monkeypatch.setattr(agent_mail, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(agent_mail, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_mail,
        "fetch_received_email",
        AsyncMock(
            return_value={
                "headers": {"Authentication-Results": "spf=fail; dkim=fail; dmarc=fail"},
                "text": "hi",
            }
        ),
    )
    deliver_mock = AsyncMock(return_value="delivered")
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    deliver_mock.assert_awaited_once()


def test_non_string_recipient_entry_is_skipped_and_scanning_continues(client, monkeypatch):
    """A malformed (non-string) entry in `to` must not abort recipient
    resolution — scanning should continue to the next entry."""
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)

    async def fake_get_by_address(addr):
        return _row() if addr == "known@agents.isol8.co" else None

    monkeypatch.setattr(agent_mail, "get_by_address", fake_get_by_address)
    monkeypatch.setattr(agent_mail, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_mail,
        "fetch_received_email",
        AsyncMock(return_value={"headers": {"Authentication-Results": "dmarc=pass"}, "text": "hi"}),
    )
    deliver_mock = AsyncMock(return_value="delivered")
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body(to=[123, "known@agents.isol8.co"]))

    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    deliver_mock.assert_awaited_once()


def test_happy_path_delivers(client, monkeypatch):
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(agent_mail, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(agent_mail, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_mail,
        "fetch_received_email",
        AsyncMock(
            return_value={
                "headers": {"Authentication-Results": "dmarc=pass header.from=example.com"},
                "text": "Hi there, please help.",
            }
        ),
    )
    deliver_mock = AsyncMock(return_value="delivered")
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    deliver_mock.assert_awaited_once()
    kwargs = deliver_mock.await_args.kwargs
    assert kwargs["owner_id"] == OWNER_ID
    assert kwargs["agent_id"] == AGENT_ID
    assert kwargs["idempotency_key"] == EMAIL_ID
    assert SENDER in kwargs["message"]
    # Reply token is embedded in the delivered message as "Reply-Token: <token>".
    reply_line = next(line for line in kwargs["message"].splitlines() if line.startswith("Reply-Token:"))
    token = reply_line.split("Reply-Token:", 1)[1].strip()
    assert len(token) > 0


def test_container_not_running_status_is_surfaced(client, monkeypatch):
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(agent_mail, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(agent_mail, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_mail,
        "fetch_received_email",
        AsyncMock(return_value={"headers": {"Authentication-Results": "dmarc=pass"}, "text": "hi"}),
    )
    monkeypatch.setattr(agent_mail, "deliver_to_agent", AsyncMock(return_value="container_not_running"))

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "container_not_running"


def test_unexpected_seam_error_never_returns_5xx(client, monkeypatch):
    """A raising seam must degrade to a 200 {"status": "error"} ack, never a 500 —
    Resend would otherwise retry-storm a permanently-broken event."""
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(agent_mail, "get_by_address", AsyncMock(side_effect=RuntimeError("ddb blip")))

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "error"


def test_ignored_for_non_email_received_event(client, monkeypatch):
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    deliver_mock = AsyncMock()
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    body = json.dumps({"type": "email.bounced", "data": {}}).encode()
    resp = _post(client, body)

    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    deliver_mock.assert_not_called()
