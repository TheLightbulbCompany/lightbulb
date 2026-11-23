"""Unit tests for POST /api/v1/webhooks/ses-inbound (SES-inbound agent email ingest).

One test per branch of the orchestration in ``routers/ses_inbound.py``:
feature-gate disabled (secret unset, domain unset), missing/bad shared
secret, duplicate message_id (deduped up front — unlike the Resend path,
every gate here is a deterministic function of an already-SES-scanned
payload, so there's no transient-failure window to protect), an empty
message_id that skips dedup but still proceeds to delivery, unknown
recipient, an unlisted sender (stored to the inbox but NOT woken — see
`test_unlisted_sender_is_stored_not_woken`, and the domain-allowlist wake
in `test_domain_allowlist_entry_wakes`), over cap (answered via an
auto-reply rather than dropped silently -- see the `capped_owner` fixture
and its tests, plus the unknown-recipient test proving that one stays
silent), the SES-verdict gate (virus/spam explicit FAIL, DMARC-authoritative
sender-auth FAIL, the SPF+DKIM fallback when DMARC is absent, and a
missing/GRAY verdict set that is stored but not woken), the sender-identity
alignment gate on WAKING (an allowlisted `From` backed only by an unaligned
SPF-passing envelope is stored, not woken; an aligned DKIM signature with no
DMARC verdict still wakes), the auto-submitted loop guard, the happy path, and the transient-vs-terminal outcome split: an
unexpected exception or an "error" delivery result returns 503, while
"container_not_running" (the owner was over cap, or has no runtime row) and
every deterministic drop stay 200.

The 503 branches also ROLL BACK the dedup row they recorded at the top of the
request, and that is covered here with a real in-memory dedup store across
both seams (``_real_dedup``) driving TWO requests with the same message_id --
every other test in this file stubs one seam or the other, which is exactly
why a surviving dedup row silently swallowing the retry went unnoticed.

Seams (``get_by_address``, ``trial_block_reason``, ``deliver_to_agent``,
``record_event_or_skip``, …) are imported BY NAME into
``routers.ses_inbound``'s module namespace, so tests monkeypatch them as
attributes on that module — mirrors ``tests/unit/routers/test_agent_mail.py``'s
patch-target convention for the sibling Resend webhook.
"""

from unittest.mock import AsyncMock, call

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.services.webhook_dedup import WebhookDedupResult
from routers import ses_inbound

SECRET = "ses-test-secret"
DOMAIN = "agents.isol8.co"

OWNER_ID = "owner_1"
AGENT_ID = "agent_1"
MESSAGE_ID = "ses-msg-123"
SENDER = "sender@example.com"
RECIPIENT = "agents@acme-robotics.isol8.co"


@pytest.fixture(autouse=True)
def _team_mailbox(monkeypatch):
    """Every recipient in this file is the workspace's team address unless a test
    says otherwise; its mail is the CEO's (``AGENT_ID`` here) unless it answers
    an agent's own sent mail. A missing row cannot be created by default (terminally)."""
    monkeypatch.setattr(ses_inbound, "is_team_address", AsyncMock(return_value=(OWNER_ID, "acme-robotics")))
    monkeypatch.setattr(ses_inbound, "_owner_email", AsyncMock(return_value="owner@example.com"))
    monkeypatch.setattr(ses_inbound, "ensure_team_address", AsyncMock(return_value=(None, "disabled")))


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


def _row(*, verified_senders=None, address: str = RECIPIENT):
    return {
        "owner_id": OWNER_ID,
        "address": address,
        "verified_senders": verified_senders if verified_senders is not None else [SENDER],
        "status": "active",
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
        headers["X-Lightbulb-SES-Secret"] = secret
    return client.post("/api/v1/webhooks/ses-inbound", json=body, headers=headers)


def test_disabled_when_secret_unset(client, monkeypatch):
    monkeypatch.setattr(ses_inbound.settings, "SES_INBOUND_SECRET", "")
    monkeypatch.setattr(ses_inbound.settings, "AGENT_EMAIL_DOMAIN", DOMAIN)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"


def test_disabled_when_domain_unset(client, monkeypatch):
    monkeypatch.setattr(ses_inbound.settings, "SES_INBOUND_SECRET", SECRET)
    monkeypatch.setattr(ses_inbound.settings, "AGENT_EMAIL_DOMAIN", "")

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"


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
    get_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "get_by_address", get_mock)

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "duplicate"
    get_mock.assert_not_awaited()


def test_empty_message_id_skips_dedup(client, monkeypatch):
    """message_id is required in practice, but a Lambda payload that arrives
    with an empty one must skip dedup (there's no key to record) rather than
    treating it as an error and dropping the mail."""
    _enable(monkeypatch)
    dedup_mock = AsyncMock()
    monkeypatch.setattr(ses_inbound, "record_event_or_skip", dedup_mock)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))

    resp = _post(client, _body(message_id=""))

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_not_an_invite"
    dedup_mock.assert_not_awaited()


def test_unknown_recipient_is_dropped(client, monkeypatch):
    """Only the team address is a mailbox: a retired per-agent address is nobody."""
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "is_team_address", AsyncMock(return_value=None))
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))

    resp = _post(client, _body())

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_unknown_recipient"


def test_spam_is_dropped_before_store(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    r = _post(client, _body(verdicts={"spam": "FAIL"}))
    assert r.json()["status"] == "dropped_spam_or_virus"


# ---- open mode (owner ruling 2026-09-24): `send_to_anyone` also opens the WAKE ----------------
#
# `_row()` carries no `send_to_anyone`, i.e. a legacy/closed row, so every test above
# keeps pinning the closed policy. These rows opt in explicitly. SENDER is deliberately
# absent from `verified_senders` so a wake can only come from the open flag.


def test_virus_fail_verdict_is_dropped(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))

    resp = _post(
        client,
        _body(verdicts={"spf": "PASS", "dkim": "PASS", "dmarc": "PASS", "spam": "PASS", "virus": "FAIL"}),
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_spam_or_virus"


def test_spam_fail_verdict_is_dropped(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))

    resp = _post(
        client,
        _body(verdicts={"spf": "PASS", "dkim": "PASS", "dmarc": "PASS", "spam": "FAIL", "virus": "PASS"}),
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_spam_or_virus"


@pytest.mark.parametrize("dmarc", [None, "", "GRAY"])
def test_missing_dmarc_falls_back_to_spf_and_dkim_both_fail_drops(client, monkeypatch, dmarc):
    """When SES surfaces no dmarc verdict, the fallback requires BOTH spf and
    dkim to explicitly FAIL before dropping. The Lambda always sends the key
    ("" when SES ran no check) and SES says GRAY for a domain without a DMARC
    record — both mean "no verdict", not "passed"."""
    verdicts = {"spf": "FAIL", "dkim": "FAIL", "spam": "PASS", "virus": "PASS"}
    if dmarc is not None:
        verdicts["dmarc"] = dmarc
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))

    resp = _post(client, _body(verdicts=verdicts))

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_failed_authentication"


def test_auto_submitted_is_dropped(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))

    resp = _post(client, _body(headers={"Auto-Submitted": "auto-replied"}))

    assert resp.status_code == 200
    assert resp.json()["status"] == "dropped_auto_submitted"


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


def _real_dedup(monkeypatch):
    """A real (in-memory) dedup store across BOTH seams, so a test can send two
    requests with the same message_id and observe what the second one actually
    does. Every other test stubs one seam or the other, which is precisely why
    the rollback defect below was invisible."""
    seen: set[str] = set()

    async def _record(event_id, *, source):
        if event_id in seen:
            return WebhookDedupResult.ALREADY_SEEN
        seen.add(event_id)
        return WebhookDedupResult.RECORDED

    async def _delete(event_id):
        seen.discard(event_id)

    monkeypatch.setattr(ses_inbound, "record_event_or_skip", _record)
    monkeypatch.setattr(ses_inbound, "delete_event", _delete)
    return seen


def test_a_terminal_drop_keeps_its_dedup_row(client, monkeypatch):
    """The rollback is scoped to the 503s. A deterministic drop must stay
    deduped -- re-running it would produce the identical drop."""
    _enable(monkeypatch)
    seen = _real_dedup(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=None))

    assert _post(client, _body()).json()["status"] == "dropped_unknown_recipient"
    assert seen == {MESSAGE_ID}
    assert _post(client, _body()).json()["status"] == "duplicate"


def _claim_notice(monkeypatch, granted=True):
    """Stub the durable once-per-day claim guarding the complaint notice."""
    claim = AsyncMock(return_value=granted)
    monkeypatch.setattr(ses_inbound, "try_claim_daily_system_notice", claim)
    return claim


def _complaint(source=RECIPIENT, event_type="Complaint"):
    return {
        "detail-type": "Email Complaint Received",
        "source": "aws.ses",
        "detail": {
            "eventType": event_type,
            "mail": {"source": source},
            "complaint": {"complaintFeedbackType": "abuse"},
        },
    }


def test_ses_events_complaint_disables_the_one_mailbox(client, monkeypatch):
    _enable(monkeypatch)
    _claim_notice(monkeypatch)
    monkeypatch.setattr(ses_inbound, "resolve", AsyncMock(return_value=_row()))
    upd = AsyncMock(return_value={})
    monkeypatch.setattr(ses_inbound, "update_settings", upd)
    notify = AsyncMock(return_value="sent")
    monkeypatch.setattr(ses_inbound, "send_to_owner", notify)
    r = client.post("/api/v1/webhooks/ses-events", json=_complaint(), headers={"X-Lightbulb-SES-Secret": SECRET})
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    assert r.json()["notified"] is True
    upd.assert_awaited_once_with(RECIPIENT, outbound_disabled="complaint")
    assert notify.call_args.kwargs["owner_id"] == OWNER_ID
    # The notice is the PLATFORM speaking, not Friday saying hello — it must
    # not consume the owner's once-only first-contact slot.
    assert notify.call_args.kwargs["system_notice"] is True


def test_ses_events_ignores_non_complaint(client, monkeypatch):
    _enable(monkeypatch)
    r = client.post(
        "/api/v1/webhooks/ses-events",
        json=_complaint(event_type="Delivery"),
        headers={"X-Lightbulb-SES-Secret": SECRET},
    )
    assert r.json()["status"] == "ignored"


def test_ses_events_garbage_source_is_ignored_not_a_500(client, monkeypatch):
    """`normalize` would raise on a non-string source. An unhandled 500 makes
    EventBridge retry a permanently invalid event until it expires."""
    _enable(monkeypatch)
    r = client.post(
        "/api/v1/webhooks/ses-events", json=_complaint(source=123), headers={"X-Lightbulb-SES-Secret": SECRET}
    )
    assert r.status_code == 200 and r.json()["status"] == "ignored"


def test_ses_events_empty_source_is_terminal_200(client, monkeypatch):
    _enable(monkeypatch)
    r = client.post(
        "/api/v1/webhooks/ses-events", json=_complaint(source=""), headers={"X-Lightbulb-SES-Secret": SECRET}
    )
    assert r.status_code == 200 and r.json()["status"] == "unknown_source"


def test_ses_events_bad_secret_401(client, monkeypatch):
    _enable(monkeypatch)
    assert (
        client.post(
            "/api/v1/webhooks/ses-events", json=_complaint(), headers={"X-Lightbulb-SES-Secret": "nope"}
        ).status_code
        == 401
    )


def test_ses_events_non_dict_json_body_400(client, monkeypatch):
    """Valid JSON that isn't an object (a bare list, string, number, bool)
    must not crash `.get("detail")` on it -- same bad_json drop as a decode
    failure."""
    _enable(monkeypatch)
    r = client.post(
        "/api/v1/webhooks/ses-events",
        content=b"[1,2,3]",
        headers={"Content-Type": "application/json", "X-Lightbulb-SES-Secret": SECRET},
    )
    assert r.status_code == 400 and r.json()["status"] == "bad_json"


def test_ses_events_disable_failure_returns_503_no_notify(client, monkeypatch):
    """A failed disable means nothing was disabled -- 503 so the EventBridge
    API destination retries, and the owner is NOT told sending is paused when
    it demonstrably isn't yet."""
    _enable(monkeypatch)
    _claim_notice(monkeypatch)
    monkeypatch.setattr(ses_inbound, "resolve", AsyncMock(return_value=_row()))
    monkeypatch.setattr(ses_inbound, "update_settings", AsyncMock(side_effect=RuntimeError("ddb blip")))
    notify = AsyncMock(return_value="sent")
    monkeypatch.setattr(ses_inbound, "send_to_owner", notify)

    r = client.post("/api/v1/webhooks/ses-events", json=_complaint(), headers={"X-Lightbulb-SES-Secret": SECRET})

    assert r.status_code == 503 and r.json()["status"] == "error"
    notify.assert_not_awaited()


@pytest.mark.parametrize(
    "sent_at, status",
    [
        ("2026-09-01T00:00:00.000Z", "unknown_source"),  # sent before this owner reserved the slug
        (None, "unknown_source"),
        ("not a time", "unknown_source"),
        ("2026-10-02T00:00:00.000Z", "disabled"),
    ],
)
def test_ses_events_retired_address_never_crosses_a_reused_slug(client, monkeypatch, sent_at, status):
    _enable(monkeypatch)
    _claim_notice(monkeypatch)
    monkeypatch.setattr(ses_inbound, "resolve", AsyncMock(return_value=None))
    monkeypatch.setattr(ses_inbound, "owner_for_host", AsyncMock(return_value=(OWNER_ID, "acme-robotics")))
    reserved = {"owner_id": OWNER_ID, "created_at": "2026-10-01T00:00:00+00:00"}
    monkeypatch.setattr(ses_inbound.company_slug_repo, "get_by_slug", AsyncMock(return_value=reserved))
    monkeypatch.setattr(ses_inbound, "ensure_team_address", AsyncMock(return_value=(RECIPIENT, None)))
    upd = AsyncMock(return_value={})
    monkeypatch.setattr(ses_inbound, "update_settings", upd)
    monkeypatch.setattr(ses_inbound, "send_to_owner", AsyncMock(return_value="sent"))
    event = _complaint(source="friday@acme-robotics.agents.isol8.co")
    event["detail"]["mail"]["timestamp"] = sent_at
    r = client.post("/api/v1/webhooks/ses-events", json=event, headers={"X-Lightbulb-SES-Secret": SECRET})
    assert r.status_code == 200 and r.json()["status"] == status
    assert upd.await_args_list == ([call(RECIPIENT, outbound_disabled="complaint")] if status == "disabled" else [])


def test_ses_events_notice_is_once_per_owner_per_day(client, monkeypatch):
    """SES fans one complaint out per recipient address, and the notice rides
    `send_to_owner(system_notice=True)` which bypasses the daily send cap -- the
    durable claim is the only bound. The rows are still disabled on the
    suppressed event."""
    _enable(monkeypatch)
    monkeypatch.setattr(ses_inbound, "resolve", AsyncMock(return_value=_row()))
    upd = AsyncMock(return_value={})
    monkeypatch.setattr(ses_inbound, "update_settings", upd)
    notify = AsyncMock(return_value="sent")
    monkeypatch.setattr(ses_inbound, "send_to_owner", notify)
    # First event claims the day, second one loses it.
    monkeypatch.setattr(ses_inbound, "try_claim_daily_system_notice", AsyncMock(side_effect=[True, False]))

    first = client.post("/api/v1/webhooks/ses-events", json=_complaint(), headers={"X-Lightbulb-SES-Secret": SECRET})
    second = client.post("/api/v1/webhooks/ses-events", json=_complaint(), headers={"X-Lightbulb-SES-Secret": SECRET})

    notify.assert_awaited_once()
    assert first.json()["notified"] is True
    assert second.status_code == 200
    assert second.json()["status"] == "disabled" and second.json()["notified"] is False
    # Disabling is NOT suppressed -- only the email is.
    assert upd.await_count == 2


TEAM_ADDRESS = "agents@acme.agents.isol8.co"
CEO_ID = "ceo-1"
INVITE = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        "UID:evt-1@google.com",
        "DTSTART:20361003T190000Z",
        "ORGANIZER;CN=Stranger:mailto:stranger@elsewhere.com",
        f"ATTENDEE;CN=agents:mailto:{TEAM_ADDRESS}",
        "X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
)


TEAM_ORG = "org_acme"


def _team(monkeypatch):
    """An org's team address: its members' invites are meetings."""
    from core.repositories import messaging_repo
    from core.voice.meetings import meeting_invites

    monkeypatch.setattr(ses_inbound, "is_team_address", AsyncMock(return_value=(TEAM_ORG, "acme")))
    team_row = {"owner_id": TEAM_ORG, "address": TEAM_ADDRESS, "send_to_anyone": True}
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=team_row))
    monkeypatch.setattr(messaging_repo, "get_room", AsyncMock(side_effect=[None, *[{"ownerId": TEAM_ORG}] * 3]))
    monkeypatch.setattr(meeting_invites.container_repo, "try_claim_owner_lifecycle", AsyncMock(return_value="tok"))
    monkeypatch.setattr(meeting_invites.container_repo, "release_owner_lifecycle_claim", AsyncMock())
    put = AsyncMock()
    monkeypatch.setattr(meeting_invites, "_create_room", put)
    monkeypatch.setattr(meeting_invites, "_post_line", AsyncMock())
    monkeypatch.setattr(messaging_repo, "patch_room_meta", AsyncMock())
    return put, None


def test_the_first_inbound_creates_the_team_row(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    _team(monkeypatch)
    row = {"owner_id": TEAM_ORG, "address": TEAM_ADDRESS, "send_to_anyone": True}
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(side_effect=[None, row]))
    ensure = AsyncMock(return_value=(TEAM_ADDRESS, "ok"))
    monkeypatch.setattr(ses_inbound, "ensure_team_address", ensure)

    assert _post(client, _body(to=[TEAM_ADDRESS])).json()["status"] == "dropped_not_an_invite"
    ensure.assert_awaited_once_with(TEAM_ORG, "owner@example.com")


def test_a_complaint_whose_source_carries_a_display_name_pauses_the_mailbox(client, monkeypatch):
    _enable(monkeypatch)
    _claim_notice(monkeypatch)
    get = AsyncMock(return_value=_row())
    monkeypatch.setattr(ses_inbound, "resolve", get)
    monkeypatch.setattr(ses_inbound, "ensure_team_address", AsyncMock(return_value=(RECIPIENT, "ok")))
    upd = AsyncMock(return_value={})
    monkeypatch.setattr(ses_inbound, "update_settings", upd)
    monkeypatch.setattr(ses_inbound, "send_to_owner", AsyncMock(return_value="sent"))
    source = f"=?utf-8?q?Friday_=C2=B7_Acme_agents?= <{RECIPIENT}>"

    r = client.post(
        "/api/v1/webhooks/ses-events", json=_complaint(source=source), headers={"X-Lightbulb-SES-Secret": SECRET}
    )

    assert r.json()["status"] == "disabled"
    get.assert_awaited_once_with(RECIPIENT)
    upd.assert_awaited_once_with(RECIPIENT, outbound_disabled="complaint")


def test_a_complaint_on_a_retired_per_agent_address_pauses_the_team_mailbox(client, monkeypatch):
    _enable(monkeypatch)
    _claim_notice(monkeypatch)
    monkeypatch.setattr(ses_inbound, "resolve", AsyncMock(return_value=_row(address="friday@acme.agents.isol8.co")))
    ensure = AsyncMock(return_value=(TEAM_ADDRESS, "ok"))
    monkeypatch.setattr(ses_inbound, "ensure_team_address", ensure)
    upd = AsyncMock(return_value={})
    monkeypatch.setattr(ses_inbound, "update_settings", upd)
    monkeypatch.setattr(ses_inbound, "send_to_owner", AsyncMock(return_value="sent"))

    r = client.post(
        "/api/v1/webhooks/ses-events",
        json=_complaint(source="friday@acme.agents.isol8.co"),
        headers={"X-Lightbulb-SES-Secret": SECRET},
    )

    assert r.json()["status"] == "disabled"
    ensure.assert_awaited_once_with(OWNER_ID, "owner@example.com")
    upd.assert_awaited_once_with(TEAM_ADDRESS, outbound_disabled="complaint")


def test_a_late_complaint_on_a_deleted_per_agent_address_pauses_the_team_mailbox(client, monkeypatch):
    """The cleanup script deleted ``friday@acme…``'s row; its host still names the workspace."""
    from core.repositories import agent_email_repo

    _enable(monkeypatch)
    _claim_notice(monkeypatch)
    monkeypatch.setattr(ses_inbound, "resolve", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_email_repo.company_slug_repo,
        "get_by_slug",
        AsyncMock(return_value={"slug": "acme", "owner_id": OWNER_ID, "created_at": "2026-09-01T00:00:00+00:00"}),
    )
    ensure = AsyncMock(return_value=(TEAM_ADDRESS, "ok"))
    monkeypatch.setattr(ses_inbound, "ensure_team_address", ensure)
    upd = AsyncMock(return_value={})
    monkeypatch.setattr(ses_inbound, "update_settings", upd)
    monkeypatch.setattr(ses_inbound, "send_to_owner", AsyncMock(return_value="sent"))

    event = _complaint(source=f"friday@acme.{DOMAIN}")
    event["detail"]["mail"]["timestamp"] = "2026-10-01T00:00:00.000Z"  # sent while this owner held the slug
    r = client.post("/api/v1/webhooks/ses-events", json=event, headers={"X-Lightbulb-SES-Secret": SECRET})

    assert r.json()["status"] == "disabled"
    ensure.assert_awaited_once_with(OWNER_ID, "owner@example.com")
    upd.assert_awaited_once_with(TEAM_ADDRESS, outbound_disabled="complaint")


def test_a_complaint_from_an_unknown_domain_is_unknown_source(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(ses_inbound, "resolve", AsyncMock(return_value=None))
    upd = AsyncMock()
    monkeypatch.setattr(ses_inbound, "update_settings", upd)

    r = client.post(
        "/api/v1/webhooks/ses-events",
        json=_complaint(source="friday@acme.example.com"),
        headers={"X-Lightbulb-SES-Secret": SECRET},
    )

    assert r.status_code == 200 and r.json()["status"] == "unknown_source"
    upd.assert_not_awaited()


@pytest.mark.parametrize(
    ("reason", "code", "status", "rollbacks"),
    [
        ("mint_failed", 503, "error", 1),
        ("slug_allocation_failed", 503, "error", 1),
        ("disabled", 200, "dropped_unknown_recipient", 0),
    ],
)
def test_a_transient_team_row_failure_on_the_first_mail_is_retried(
    client, monkeypatch, reason, code, status, rollbacks
):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=None))
    monkeypatch.setattr(ses_inbound, "ensure_team_address", AsyncMock(return_value=(None, reason)))
    monkeypatch.setattr(ses_inbound, "delete_event", delete := AsyncMock())

    resp = _post(client, _body())

    assert (resp.status_code, resp.json()["status"]) == (code, status)
    assert delete.await_count == rollbacks


def test_dmarc_fail_is_dropped(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    verdicts = {"spf": "PASS", "dkim": "PASS", "dmarc": "FAIL", "spam": "PASS", "virus": "PASS"}

    assert _post(client, _body(verdicts=verdicts)).json()["status"] == "dropped_failed_authentication"


def test_mail_that_is_not_an_invite_is_dropped_not_stored_or_answered(client, monkeypatch):
    """Agent email is retired: only a calendar invite does anything at the team address."""
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))

    resp = _post(client, _body())

    assert (resp.status_code, resp.json()["status"]) == (200, "dropped_not_an_invite")
