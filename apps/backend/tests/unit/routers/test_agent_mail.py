"""Unit tests for POST /api/v1/webhooks/resend-inbound (inbound agent email).

One test per branch of the orchestration in ``routers/agent_mail.py``:
feature-gate disabled, bad signature, duplicate svix-id (recorded immediately
before delivery, not up front — see below), unknown recipient, unverified
sender (+ the public-address bypass), over cap, fetch failure (a transient
503 so Resend retries — never deduped), auto-submitted loop guard, the
sender-authentication verdict gate (explicit fail always drops; unknown
always delivers — the common real Resend case, since Resend surfaces no
verdict field at all), the happy path, and the top-level catch-all that
guarantees the handler never returns 5xx.

Idempotency dedup (``record_event_or_skip``) is recorded immediately before
``deliver_to_agent`` is called, not up front after signature verification —
everything before that point (recipient/sender/cap gates, the Resend fetch)
can fail transiently and must remain retryable by Resend/Svix (which only
retries non-2xx responses), so only a message that is actually about to be
delivered gets marked as seen.

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
    """Dedup is now recorded immediately before delivery (after the fetch and
    all other gates succeed), so this test must get the happy path all the
    way to that point before the ALREADY_SEEN mock can take effect."""
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    monkeypatch.setattr(agent_mail, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(agent_mail, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_mail,
        "fetch_received_email",
        AsyncMock(return_value={"headers": {}, "text": "hi"}),
    )
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
        # No Authentication-Results header at all — the common real Resend
        # case — must still deliver (unknown verdict is never dropped).
        AsyncMock(return_value={"headers": {}, "text": "hi"}),
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


def test_fetch_failure_returns_503_retryable(client, monkeypatch):
    """A fetch failure is transient (Resend API blip, network error), so it
    must return a non-2xx status so Resend/Svix retries the delivery — a 200
    here would get the event deduped-away with no processing ever happening."""
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(agent_mail, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(agent_mail, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_mail, "fetch_received_email", AsyncMock(return_value=None))
    deliver_mock = AsyncMock()
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 503
    assert resp.json()["status"] == "fetch_failed_retryable"
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
        # No Authentication-Results header — default mode still delivers.
        AsyncMock(return_value={"headers": {}, "text": "hi"}),
    )
    deliver_mock = AsyncMock(return_value="delivered")
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body(to=[123, "known@agents.isol8.co"]))

    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    deliver_mock.assert_awaited_once()


def test_string_to_field_is_coerced_to_list_and_delivers(client, monkeypatch):
    """Resend's `data.to` is normally a list, but a single-string value must
    not be iterated char-by-char — each single character would fail
    `normalize()`'s lookup and the message would incorrectly drop as
    unknown_recipient."""
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    known_address = "agent@agents.isol8.co"

    async def fake_get_by_address(addr):
        return _row() if addr == known_address else None

    monkeypatch.setattr(agent_mail, "get_by_address", fake_get_by_address)
    monkeypatch.setattr(agent_mail, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_mail,
        "fetch_received_email",
        AsyncMock(return_value={"headers": {}, "text": "hi"}),
    )
    deliver_mock = AsyncMock(return_value="delivered")
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body(to=known_address))

    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    deliver_mock.assert_awaited_once()


def test_happy_path_delivers(client, monkeypatch):
    """Default mode: no Authentication-Results header at all is the common
    real Resend case, and it must still deliver — the sender allowlist
    (matched earlier via verified_senders), not this header, is the primary
    control. Also verifies dedup is recorded (keyed on svix-id) right before
    delivery on this success path."""
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    dedup_mock = AsyncMock(return_value=WebhookDedupResult.RECORDED)
    monkeypatch.setattr(agent_mail, "record_event_or_skip", dedup_mock)
    monkeypatch.setattr(agent_mail, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(agent_mail, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_mail,
        "fetch_received_email",
        AsyncMock(
            return_value={
                "headers": {},
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
    dedup_mock.assert_awaited_once_with("evt-1", source="resend_inbound")
    kwargs = deliver_mock.await_args.kwargs
    assert kwargs["owner_id"] == OWNER_ID
    assert kwargs["agent_id"] == AGENT_ID
    assert kwargs["idempotency_key"] == EMAIL_ID
    assert SENDER in kwargs["message"]
    # Reply token is embedded in the delivered message as "Reply-Token: <token>".
    reply_line = next(line for line in kwargs["message"].splitlines() if line.startswith("Reply-Token:"))
    token = reply_line.split("Reply-Token:", 1)[1].strip()
    assert len(token) > 0


def test_happy_path_with_attachments_includes_manifest_in_message(client, monkeypatch):
    """When the fetched email has attachments, the delivered message must
    include the attachment manifest — proven here by asserting the
    attachment's filename appears in the `message` passed to
    `deliver_to_agent`. Auth verdict stays "unknown" (no
    Authentication-Results header) so default mode still delivers, matching
    the other happy-path fixtures."""
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
                "headers": {},
                "text": "Hi there, please help.",
            }
        ),
    )
    monkeypatch.setattr(
        agent_mail,
        "list_received_attachments",
        AsyncMock(
            return_value=[
                {
                    "filename": "contract.pdf",
                    "content_type": "application/pdf",
                    "size": 10,
                    "download_url": "https://cdn/x",
                }
            ]
        ),
    )
    deliver_mock = AsyncMock(return_value="delivered")
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    deliver_mock.assert_awaited_once()
    assert "contract.pdf" in deliver_mock.await_args.kwargs["message"]


def test_happy_path_with_no_attachments_omits_manifest(client, monkeypatch):
    """When there are no attachments, the delivered message must NOT contain
    the attachment-manifest caution text."""
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
                "headers": {},
                "text": "Hi there, please help.",
            }
        ),
    )
    monkeypatch.setattr(agent_mail, "list_received_attachments", AsyncMock(return_value=[]))
    deliver_mock = AsyncMock(return_value="delivered")
    monkeypatch.setattr(agent_mail, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    deliver_mock.assert_awaited_once()
    assert "unverified sender" not in deliver_mock.await_args.kwargs["message"]


def test_container_not_running_status_is_surfaced(client, monkeypatch):
    _enable(monkeypatch)
    _valid_sig(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(agent_mail, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(agent_mail, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_mail,
        "fetch_received_email",
        AsyncMock(return_value={"headers": {}, "text": "hi"}),
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
