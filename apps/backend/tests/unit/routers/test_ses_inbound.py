"""Unit tests for POST /api/v1/webhooks/ses-inbound (SES-inbound agent email ingest).

One test per branch of the orchestration in ``routers/ses_inbound.py``:
feature-gate disabled (secret unset, domain unset), missing/bad shared
secret, duplicate message_id (deduped up front — unlike the Resend path,
every gate here is a deterministic function of an already-SES-scanned
payload, so there's no transient-failure window to protect), an empty
message_id that skips dedup but still proceeds to delivery, unknown
recipient, unverified sender (+ the public-address bypass), over cap
(answered via an auto-reply rather than dropped silently -- see the
`capped_owner` fixture and its tests, plus the unknown-recipient/
unverified-sender tests proving those two stay silent), the
SES-verdict gate (virus/spam explicit FAIL, DMARC-authoritative
sender-auth FAIL, the SPF+DKIM fallback when DMARC is absent, and a
missing/GRAY verdict set that must still deliver), the auto-submitted loop
guard, the happy path, and the transient-vs-terminal outcome split: an
unexpected exception or a "error" delivery result return 503 (retry), while
"container_not_running" (scaled-to-zero, intentionally silent) and every
deterministic drop stay 200.

Seams (``get_by_address``, ``trial_block_reason``, ``deliver_to_agent``,
``record_event_or_skip``, …) are imported BY NAME into
``routers.ses_inbound``'s module namespace, so tests monkeypatch them as
attributes on that module — mirrors ``tests/unit/routers/test_agent_mail.py``'s
patch-target convention for the sibling Resend webhook.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.services import trial_cap
from core.services.webhook_dedup import WebhookDedupResult
from routers import ses_inbound

SECRET = "ses-test-secret"
DOMAIN = "agents.isol8.co"

OWNER_ID = "owner_1"
AGENT_ID = "agent_1"
MESSAGE_ID = "ses-msg-123"
SENDER = "sender@example.com"
RECIPIENT = "friday@acme-robotics.isol8.co"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ses_inbound.router, prefix="/api/v1/webhooks")
    return TestClient(app)


def _enable(monkeypatch):
    monkeypatch.setattr(ses_inbound.settings, "SES_INBOUND_SECRET", SECRET)
    monkeypatch.setattr(ses_inbound.settings, "AGENT_EMAIL_DOMAIN", DOMAIN)


def _dedup_recorded(monkeypatch):
    monkeypatch.setattr(
        ses_inbound,
        "record_event_or_skip",
        AsyncMock(return_value=WebhookDedupResult.RECORDED),
    )


def _row(*, public: bool = False, verified_senders=None, address: str = RECIPIENT):
    return {
        "owner_id": OWNER_ID,
        "agent_id": AGENT_ID,
        "address": address,
        "public": public,
        "verified_senders": verified_senders if verified_senders is not None else [SENDER],
    }


def _body(**overrides):
    payload = {
        "message_id": MESSAGE_ID,
        "to": [RECIPIENT],
        "from": f"Sender Name <{SENDER}>",
        "subject": "Need a hand",
        "text": "the plain-text body",
        "headers": {"Auto-Submitted": "no", "Message-ID": "<orig@x>"},
        "verdicts": {"spf": "PASS", "dkim": "PASS", "dmarc": "PASS", "spam": "PASS", "virus": "PASS"},
        "attachments": [],
    }
    payload.update(overrides)
    return payload


def _post(client, body, secret=SECRET):
    headers = {}
    if secret is not None:
        headers["X-Isol8-SES-Secret"] = secret
    return client.post("/api/v1/webhooks/ses-inbound", json=body, headers=headers)


def test_disabled_when_secret_unset(client, monkeypatch):
    monkeypatch.setattr(ses_inbound.settings, "SES_INBOUND_SECRET", "")
    monkeypatch.setattr(ses_inbound.settings, "AGENT_EMAIL_DOMAIN", DOMAIN)
    deliver_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    deliver_mock.assert_not_called()


def test_disabled_when_domain_unset(client, monkeypatch):
    monkeypatch.setattr(ses_inbound.settings, "SES_INBOUND_SECRET", SECRET)
    monkeypatch.setattr(ses_inbound.settings, "AGENT_EMAIL_DOMAIN", "")
    deliver_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    deliver_mock.assert_not_called()


def test_missing_secret_header_returns_401(client, monkeypatch):
    _enable(monkeypatch)

    resp = _post(client, _body(), secret=None)

    assert resp.status_code == 401
    assert resp.json()["status"] == "bad_secret"


def test_bad_secret_returns_401(client, monkeypatch):
    _enable(monkeypatch)

    resp = _post(client, _body(), secret="wrong-secret")

    assert resp.status_code == 401
    assert resp.json()["status"] == "bad_secret"


def test_duplicate_message_id_returns_duplicate(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        ses_inbound,
        "record_event_or_skip",
        AsyncMock(return_value=WebhookDedupResult.ALREADY_SEEN),
    )
    deliver_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)
    get_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "get_by_address", get_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "duplicate"
    deliver_mock.assert_not_called()
    get_mock.assert_not_awaited()


def test_empty_message_id_skips_dedup_and_delivers(client, monkeypatch):
    """message_id is required in practice, but a Lambda payload that arrives
    with an empty one must skip dedup (there's no key to record) rather than
    treating it as an error and dropping the mail."""
    _enable(monkeypatch)
    dedup_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "record_event_or_skip", dedup_mock)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(ses_inbound, "trial_block_reason", AsyncMock(return_value=None))
    deliver_mock = AsyncMock(return_value="delivered")
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body(message_id=""))

    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    dedup_mock.assert_not_awaited()
    deliver_mock.assert_awaited_once()


def test_unknown_recipient_is_dropped(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=None))
    deliver_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_unknown_recipient"
    deliver_mock.assert_not_called()


def test_unverified_sender_is_dropped(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(
        ses_inbound,
        "get_by_address",
        AsyncMock(return_value=_row(public=False, verified_senders=["someone-else@example.com"])),
    )
    deliver_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_unverified_sender"
    deliver_mock.assert_not_called()


def test_public_address_bypasses_unverified_sender_and_delivers(client, monkeypatch):
    """A public address has no verified-sender allowlist to enforce."""
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(
        ses_inbound,
        "get_by_address",
        AsyncMock(return_value=_row(public=True, verified_senders=[])),
    )
    monkeypatch.setattr(ses_inbound, "trial_block_reason", AsyncMock(return_value=None))
    deliver_mock = AsyncMock(return_value="delivered")
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    deliver_mock.assert_awaited_once()


def test_over_cap_reports_capped_notified_when_autoreply_succeeds(client, monkeypatch):
    """Over cap is ANSWERED, not dropped in silence -- the sender allowlist
    above already proved this is the owner, so replying is safe here."""
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(ses_inbound, "trial_block_reason", AsyncMock(return_value=trial_cap.DAILY_CAP))
    monkeypatch.setattr(ses_inbound, "send_cap_autoreply", AsyncMock(return_value=True))
    deliver_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "capped_notified"
    deliver_mock.assert_not_called()


@pytest.fixture
def capped_owner(monkeypatch):
    """A verified sender emailing an over-cap owner -- eligible for an autoreply."""
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    row = {
        "owner_id": OWNER_ID,
        "agent_id": AGENT_ID,
        "address": "friday@acme.isol8.co",
        "public": False,
        "verified_senders": ["owner@example.com"],
    }
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=row))
    monkeypatch.setattr(ses_inbound, "trial_block_reason", AsyncMock(return_value=trial_cap.DAILY_CAP))
    return row


def _capped_body(**overrides):
    overrides.setdefault("from", "Owner <owner@example.com>")
    return _body(**overrides)


def test_over_cap_sends_an_autoreply_and_reports_capped_notified(client, monkeypatch, capped_owner):
    calls = []

    async def _autoreply(*, reason, from_addr, to, in_reply_to, subject):
        calls.append({"reason": reason, "from": from_addr, "to": to})
        return True

    monkeypatch.setattr(ses_inbound, "send_cap_autoreply", _autoreply)

    resp = _post(client, _capped_body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "capped_notified"
    assert len(calls) == 1
    # Sent FROM the agent's own address, TO the owner who wrote in.
    assert calls[0]["from"] == "friday@acme.isol8.co"
    assert calls[0]["to"] == "owner@example.com"


def test_over_cap_autoreply_threads_on_message_id_header_not_ses_id(client, monkeypatch, capped_owner):
    """`in_reply_to` must be the RFC-5322 `Message-ID` header the sender's own
    mail client threads on -- NOT SES's internal `message_id`, which matches
    nothing in any mail client. SES's id and the mail header are set to
    different values here so this test fails if someone reverts to the SES id."""
    calls = []

    async def _autoreply(*, reason, from_addr, to, in_reply_to, subject):
        calls.append(in_reply_to)
        return True

    monkeypatch.setattr(ses_inbound, "send_cap_autoreply", _autoreply)

    resp = _post(
        client,
        _capped_body(
            message_id="ses-internal-id-999",
            headers={"Auto-Submitted": "no", "Message-ID": "<real-thread@owner-mailbox>"},
        ),
    )

    assert resp.json()["status"] == "capped_notified"
    assert calls == ["<real-thread@owner-mailbox>"]


def test_over_cap_still_reports_dropped_when_the_autoreply_fails(client, monkeypatch, capped_owner):
    async def _autoreply(**kwargs):
        return False

    monkeypatch.setattr(ses_inbound, "send_cap_autoreply", _autoreply)

    resp = _post(client, _capped_body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_over_cap"


def test_over_cap_never_delivers_to_the_agent(client, monkeypatch, capped_owner):
    deliver_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    async def _autoreply(**kwargs):
        return True

    monkeypatch.setattr(ses_inbound, "send_cap_autoreply", _autoreply)

    _post(client, _capped_body())

    deliver_mock.assert_not_called()


def test_over_cap_and_dmarc_fail_drops_before_the_autoreply_can_send(client, monkeypatch, capped_owner):
    """Pins ordering: the cap check must run AFTER the anti-spoof gate. `From`
    is a bare header parse with no DKIM/SPF binding, so the sender allowlist
    alone doesn't prove this wasn't spoofed -- if the cap check ran first, a
    spoofed sender for an over-cap owner would still trigger a real outbound
    email. Fails if the cap-check block is moved back above this gate."""
    autoreply_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "send_cap_autoreply", autoreply_mock)

    resp = _post(
        client,
        _capped_body(verdicts={"spf": "PASS", "dkim": "PASS", "dmarc": "FAIL", "spam": "PASS", "virus": "PASS"}),
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_failed_authentication"
    autoreply_mock.assert_not_called()


def test_over_cap_and_auto_submitted_drops_before_the_autoreply_can_send(client, monkeypatch, capped_owner):
    """Pins ordering: the cap check must run AFTER the auto-submitted loop
    guard, or an out-of-office autoresponder from the owner's own mail
    infrastructure would trigger a real outbound reply -- a mail loop. Fails
    if the cap-check block is moved back above this guard."""
    autoreply_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "send_cap_autoreply", autoreply_mock)

    resp = _post(
        client,
        _capped_body(headers={"Auto-Submitted": "auto-replied", "Message-ID": "<orig@x>"}),
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_auto_submitted"
    autoreply_mock.assert_not_called()


def test_unverified_sender_gets_NO_autoreply(client, monkeypatch):
    """Replying would confirm the address exists and make this a reflector."""
    calls = []

    async def _autoreply(**kwargs):
        calls.append(kwargs)
        return True

    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(ses_inbound, "send_cap_autoreply", _autoreply)

    resp = _post(client, _body(**{"from": "Stranger <stranger@example.com>"}))

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_unverified_sender"
    assert calls == []


def test_unknown_recipient_gets_NO_autoreply(client, monkeypatch):
    calls = []

    async def _autoreply(**kwargs):
        calls.append(kwargs)
        return True

    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=None))
    monkeypatch.setattr(ses_inbound, "send_cap_autoreply", _autoreply)

    resp = _post(client, _body(to=["nobody@nowhere.isol8.co"]))

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_unknown_recipient"
    assert calls == []


def test_virus_fail_verdict_is_dropped(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(ses_inbound, "trial_block_reason", AsyncMock(return_value=None))
    deliver_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    resp = _post(
        client,
        _body(verdicts={"spf": "PASS", "dkim": "PASS", "dmarc": "PASS", "spam": "PASS", "virus": "FAIL"}),
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_spam_or_virus"
    deliver_mock.assert_not_called()


def test_spam_fail_verdict_is_dropped(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(ses_inbound, "trial_block_reason", AsyncMock(return_value=None))
    deliver_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    resp = _post(
        client,
        _body(verdicts={"spf": "PASS", "dkim": "PASS", "dmarc": "PASS", "spam": "FAIL", "virus": "PASS"}),
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_spam_or_virus"
    deliver_mock.assert_not_called()


def test_dmarc_fail_is_dropped_and_deliver_not_called(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(ses_inbound, "trial_block_reason", AsyncMock(return_value=None))
    deliver_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    resp = _post(
        client,
        _body(verdicts={"spf": "PASS", "dkim": "PASS", "dmarc": "FAIL", "spam": "PASS", "virus": "PASS"}),
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_failed_authentication"
    deliver_mock.assert_not_called()


def test_missing_dmarc_falls_back_to_spf_and_dkim_both_fail_drops(client, monkeypatch):
    """When SES surfaces no dmarc verdict at all, the fallback requires BOTH
    spf and dkim to explicitly FAIL before dropping."""
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(ses_inbound, "trial_block_reason", AsyncMock(return_value=None))
    deliver_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    resp = _post(
        client,
        _body(verdicts={"spf": "FAIL", "dkim": "FAIL", "spam": "PASS", "virus": "PASS"}),
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_failed_authentication"
    deliver_mock.assert_not_called()


def test_missing_or_gray_verdicts_still_deliver(client, monkeypatch):
    """A PASS or a missing/GRAY verdict must never hard-drop — only an
    explicit FAIL does."""
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(ses_inbound, "trial_block_reason", AsyncMock(return_value=None))
    deliver_mock = AsyncMock(return_value="delivered")
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body(verdicts={}))

    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    deliver_mock.assert_awaited_once()


def test_auto_submitted_is_dropped(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(ses_inbound, "trial_block_reason", AsyncMock(return_value=None))
    deliver_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body(headers={"Auto-Submitted": "auto-replied"}))

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_auto_submitted"
    deliver_mock.assert_not_called()


def test_happy_path_delivers(client, monkeypatch):
    _enable(monkeypatch)
    dedup_mock = AsyncMock(return_value=WebhookDedupResult.RECORDED)
    monkeypatch.setattr(ses_inbound, "record_event_or_skip", dedup_mock)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(ses_inbound, "trial_block_reason", AsyncMock(return_value=None))
    deliver_mock = AsyncMock(return_value="delivered")
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "delivered"
    deliver_mock.assert_awaited_once()
    dedup_mock.assert_awaited_once_with(MESSAGE_ID, source="ses_inbound")
    kwargs = deliver_mock.await_args.kwargs
    assert kwargs["owner_id"] == OWNER_ID
    assert kwargs["agent_id"] == AGENT_ID
    assert kwargs["idempotency_key"] == MESSAGE_ID
    assert SENDER in kwargs["message"]
    # Reply token is embedded in the delivered message as "Reply-Token: <token>".
    reply_line = next(line for line in kwargs["message"].splitlines() if line.startswith("Reply-Token:"))
    token = reply_line.split("Reply-Token:", 1)[1].strip()
    assert len(token) > 0


def test_unexpected_seam_error_returns_503_for_retry(client, monkeypatch):
    """A raising seam (e.g. a DynamoDB blip) is a TRANSIENT failure — it must
    return 503 so SES/the Lambda's bounded retry policy gets a chance to
    succeed later, never a 200 (which would silently and permanently drop the
    mail) and never an unhandled 500."""
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(side_effect=RuntimeError("ddb blip")))

    resp = _post(client, _body())

    assert resp.status_code == 503
    assert resp.json()["status"] == "error"


def test_deliver_error_returns_503_for_retry(client, monkeypatch):
    """`deliver_to_agent` returning "error" (e.g. a delivery RPC failure
    against a RUNNING container) is transient — 503 so SES retries."""
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(ses_inbound, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", AsyncMock(return_value="error"))

    resp = _post(client, _body())

    assert resp.status_code == 503
    assert resp.json()["status"] == "error"


def test_deliver_container_not_running_stays_200_silent(client, monkeypatch):
    """`deliver_to_agent` returning "container_not_running" means a
    scaled-to-zero free-tier box — intentionally silent, so this stays 200
    rather than retrying (we do not wake the container or hammer it with
    SES retries just because mail arrived)."""
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(ses_inbound, "trial_block_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", AsyncMock(return_value="container_not_running"))

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "container_not_running"
