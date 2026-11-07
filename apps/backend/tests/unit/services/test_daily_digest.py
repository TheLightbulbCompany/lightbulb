"""Daily "needs you" digest: local-08:00 selection, the no-unseen rule, the
per-day claim, pass admission, the one shared renderer, the RFC 8058 email and
its signed unsubscribe, and the timezone/daily-summary prefs."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import jwt as pyjwt
import pytest
from moto import mock_aws

from core.config import settings
from core.dynamodb import table_name
from core.repositories import user_repo
from core.services import daily_digest, ses_email
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401

KEY = "test-service-token-key"
OWNER = "org_1"


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setattr(settings, "PAPERCLIP_SERVICE_TOKEN_KEY", KEY)
    monkeypatch.setattr(settings, "PUBLIC_API_BASE", "https://api-dev.example")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://dev.example")


@pytest.fixture
def users_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=table_name("users"),
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch("core.dynamodb._dynamodb_resource", resource):
            yield


# ---------------------------------------------------------------------------
# local 08:00 selection
# ---------------------------------------------------------------------------


def test_due_local_date_picks_the_humans_whose_local_hour_is_eight():
    now = datetime(2026, 9, 24, 12, 30, tzinfo=timezone.utc)
    due = daily_digest.due_local_date
    assert due({"timezone": "America/New_York"}, now) == "2026-09-24"  # 08:30 EDT
    assert due({}, now) == "2026-09-24"  # never set -> America/New_York
    assert due({"timezone": "Not/AZone"}, now) == "2026-09-24"  # unloadable -> default
    assert due({"timezone": "Europe/Paris"}, now) is None  # 14:30
    assert due({"timezone": "America/Los_Angeles"}, now) is None  # 05:30
    # Across the date line: 20:30Z is 08:30 the NEXT day in Auckland.
    assert due({"timezone": "Pacific/Auckland"}, datetime(2026, 9, 24, 20, 30, tzinfo=timezone.utc)) == "2026-09-25"
    assert due({"timezone": "Asia/Kolkata"}, datetime(2026, 9, 24, 2, 45, tzinfo=timezone.utc)) == "2026-09-24"
    # The pref off means never due.
    assert due({"timezone": "America/New_York", "daily_summary": False}, now) is None


@pytest.mark.asyncio
async def test_run_once_digests_only_due_members_of_active_workspaces(monkeypatch):
    now = datetime(2026, 9, 24, 12, 30, tzinfo=timezone.utc)
    rows = [
        {"user_id": "u_ny", "timezone": "America/New_York"},
        {"user_id": "u_paris", "timezone": "Europe/Paris"},
        {"user_id": "u_off", "daily_summary": False},
        {"user_id": "u_two"},
    ]
    monkeypatch.setattr(user_repo, "list_all", AsyncMock(return_value=rows))
    repo = MagicMock()
    repo.list_active_workspace_members = AsyncMock(
        return_value={"org_a": {"u_ny", "u_paris", "u_off", "u_two"}, "org_b": {"u_two", "u_stranger"}}
    )
    monkeypatch.setattr("routers.teams.agents._repo", lambda: repo)
    one = AsyncMock(return_value=True)
    monkeypatch.setattr(daily_digest, "digest_one", one)
    assert await daily_digest.run_once(now) == 3
    calls = {(c.args[0], c.args[1]["user_id"], c.args[2]) for c in one.await_args_list}
    assert calls == {
        ("org_a", "u_ny", "2026-09-24"),
        ("org_a", "u_two", "2026-09-24"),
        ("org_b", "u_two", "2026-09-24"),
    }


@pytest.mark.asyncio
async def test_run_once_skips_the_workspace_scan_when_nobody_is_due(monkeypatch):
    monkeypatch.setattr(user_repo, "list_all", AsyncMock(return_value=[{"user_id": "u", "timezone": "Europe/Paris"}]))
    scan = MagicMock()
    monkeypatch.setattr("routers.teams.agents._repo", lambda: scan)
    assert await daily_digest.run_once(datetime(2026, 9, 24, 12, 30, tzinfo=timezone.utc)) == 0
    scan.list_active_workspace_members.assert_not_called()


# ---------------------------------------------------------------------------
# one digest: no-unseen, idempotency, the shared renderer
# ---------------------------------------------------------------------------


@pytest.fixture
def sends(monkeypatch):
    push = AsyncMock()
    email = AsyncMock(return_value="ses-1")
    publisher = MagicMock()
    publisher.publish_user = AsyncMock(return_value=True)
    monkeypatch.setattr(daily_digest.push_service, "send_push_to_user", push)
    monkeypatch.setattr(daily_digest, "_send_email", email)
    monkeypatch.setattr("core.services.live_events.get_live_event_publisher", lambda: publisher)
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    return push, email, publisher.publish_user


ROW = {"user_id": "u_1", "email": "ann@example.com"}


@pytest.mark.asyncio
async def test_no_unseen_items_sends_nothing(table, sends, monkeypatch):
    monkeypatch.setattr(daily_digest.activity, "unseen_counts", AsyncMock(return_value={"dm": 4, "thread_reply": 2}))
    assert await daily_digest.digest_one(OWNER, ROW, "2026-09-24") is False
    for mock in sends:
        mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_digest_per_human_workspace_and_local_day(table, sends, monkeypatch):
    push, email, publish = sends
    monkeypatch.setattr(
        daily_digest.activity, "unseen_counts", AsyncMock(return_value={"approval": 2, "mention": 1, "dm": 3})
    )
    assert await daily_digest.digest_one(OWNER, ROW, "2026-09-24") is True
    assert await daily_digest.digest_one(OWNER, ROW, "2026-09-24") is False  # same day: nothing
    assert push.await_count == email.await_count == publish.await_count == 1
    # Another workspace, or the next local day, is its own digest.
    assert await daily_digest.digest_one("org_2", ROW, "2026-09-24") is True
    assert await daily_digest.digest_one(OWNER, ROW, "2026-09-25") is True
    item = table.get_item(Key={"pk": f"DIGEST#u_1#{OWNER}#2026-09-24", "sk": "DIGEST"})["Item"]
    assert 0 < int(item["expiresAt"]) - int(datetime.now(timezone.utc).timestamp()) <= 3 * 24 * 3600


@pytest.mark.asyncio
async def test_push_and_digest_ready_carry_the_same_rendered_text(table, sends, monkeypatch):
    push, email, publish = sends
    monkeypatch.setattr(
        daily_digest.activity, "unseen_counts", AsyncMock(return_value={"approval": 2, "mention": 1, "dm": 9})
    )
    await daily_digest.digest_one(OWNER, ROW, "2026-09-24")
    rendered = {
        "title": "3 things need you",
        "body": "2 approvals · 1 mention",
        "data": {"url": "https://dev.example/chat?panel=activity"},
    }
    assert daily_digest.render_push({"approval": 2, "mention": 1}) == rendered
    assert push.await_args.args == ("u_1", rendered["title"], rendered["body"], rendered["data"])
    assert email.await_args.args == ("ann@example.com", "u_1", rendered)
    owner, user, event = publish.await_args.args
    assert (owner, user, event["type"]) == (OWNER, "u_1", "digest.ready")
    assert event["payload"] == {
        "countsByKind": {"approval": 2, "assigned_issue": 0, "mention": 1},
        "total": 3,
        "activityUrl": "https://dev.example/chat?panel=activity",
        "push": rendered,
    }


def test_render_push_singular():
    assert daily_digest.render_push({"assigned_issue": 1})["title"] == "1 thing needs you"
    assert daily_digest.render_push({"assigned_issue": 1})["body"] == "1 assigned issue"


@pytest.mark.asyncio
async def test_non_prod_mails_only_test_and_company_inboxes(table, sends, monkeypatch):
    _push, email, _publish = sends
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(daily_digest.activity, "unseen_counts", AsyncMock(return_value={"mention": 1}))
    await daily_digest.digest_one(OWNER, {"user_id": "a", "email": "real@customer.com"}, "d")
    await daily_digest.digest_one(OWNER, {"user_id": "b", "email": "x+clerk_test@example.com"}, "d")
    await daily_digest.digest_one(OWNER, {"user_id": "c", "email": "ops@thelightbulb.company"}, "d")
    assert [c.args[0] for c in email.await_args_list] == ["x+clerk_test@example.com", "ops@thelightbulb.company"]


# ---------------------------------------------------------------------------
# pass admission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("won", [True, False])
async def test_only_the_admitted_task_runs_the_pass(monkeypatch, won):
    claim = AsyncMock(return_value=won)
    run = AsyncMock(return_value=0)
    monkeypatch.setattr(daily_digest.container_repo, "try_claim_pass", claim)
    monkeypatch.setattr(daily_digest, "run_once", run)
    from core.services import week4_active

    week4 = AsyncMock(return_value=0)
    monkeypatch.setattr(week4_active, "run_once", week4)
    stop = asyncio.Event()
    task = asyncio.create_task(daily_digest.run_digest_sweeper(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert claim.await_args_list[0].args == ("daily_digest",)
    assert claim.await_args_list[0].kwargs == {"interval_s": daily_digest.PASS_SECONDS}
    assert claim.await_args_list[1].args == ("week4_active",)  # T23 rides the same loop, its own pass
    assert run.await_count == (1 if won else 0)
    assert week4.await_count == (1 if won else 0)


# ---------------------------------------------------------------------------
# email + unsubscribe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_carries_rfc8058_one_click_headers_and_a_footer_link(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "thelightbulb.company")
    client = MagicMock()
    client.send_email.return_value = {"MessageId": "m-1"}
    monkeypatch.setattr(ses_email, "_client", lambda: client)
    push = daily_digest.render_push({"mention": 2})
    sent_id = await daily_digest._send_email("x+clerk_test@example.com", "u_1", push)
    raw = client.send_email.call_args.kwargs["Content"]["Raw"]["Data"].decode()
    assert f"Message-ID: {sent_id}" in raw  # our own RFC id, not SES's
    headers = dict(line.split(": ", 1) for line in raw.split("\n\n", 1)[0].splitlines())
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    # One unencoded header line (never RFC 2047-folded) that parses as <url>.
    url = headers["List-Unsubscribe"].removeprefix("<").removesuffix(">")
    assert url.startswith("https://api-dev.example/api/v1/users/digest/unsubscribe?token=")
    assert daily_digest.verify_unsubscribe_token(url.split("token=", 1)[1]) == "u_1"
    text, html, headers = daily_digest.render_email(push, "https://u.example/x?token=t")
    assert headers == {
        "List-Unsubscribe": "<https://u.example/x?token=t>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    assert "https://u.example/x?token=t" in text and 'href="https://u.example/x?token=t"' in html


def _token(**overrides):
    now = datetime.now(timezone.utc)
    claims = {"sub": "u_1", "kind": daily_digest.UNSUBSCRIBE_KIND, "iat": now, "exp": now + timedelta(days=1)}
    claims.update(overrides)
    key = claims.pop("key", KEY)
    return pyjwt.encode(claims, key, algorithm="HS256")


def test_unsubscribe_token_verifies_signature_expiry_and_purpose():
    assert daily_digest.verify_unsubscribe_token(daily_digest.mint_unsubscribe_token("u_9")) == "u_9"
    assert daily_digest.verify_unsubscribe_token(_token()) == "u_1"
    assert daily_digest.verify_unsubscribe_token(_token(key="another-key")) is None
    assert daily_digest.verify_unsubscribe_token(_token(exp=datetime.now(timezone.utc) - timedelta(seconds=1))) is None
    assert daily_digest.verify_unsubscribe_token(_token(kind="paperclip_service")) is None
    assert daily_digest.verify_unsubscribe_token(_token()[:-2] + "xx") is None


@pytest.mark.asyncio
async def test_one_click_unsubscribe_flips_the_pref_off_idempotently(users_table, async_client):
    await user_repo.put("u_1", "ann@example.com")
    token = daily_digest.mint_unsubscribe_token("u_1")
    # The footer GET never changes anything (link scanners prefetch it).
    page = await async_client.get("/api/v1/users/digest/unsubscribe", params={"token": token})
    assert page.status_code == 200 and "<form" in page.text
    assert user_repo.daily_summary_on(await user_repo.get("u_1")) is True
    for _ in range(2):
        resp = await async_client.post(
            "/api/v1/users/digest/unsubscribe",
            params={"token": token},
            content="List-Unsubscribe=One-Click",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200
    assert (await user_repo.get("u_1"))["daily_summary"] is False


@pytest.mark.asyncio
async def test_a_bad_unsubscribe_token_changes_nothing(users_table, async_client):
    await user_repo.put("u_1")
    resp = await async_client.post("/api/v1/users/digest/unsubscribe", params={"token": _token(key="forged")})
    assert resp.status_code == 400
    assert "daily_summary" not in await user_repo.get("u_1")


# ---------------------------------------------------------------------------
# prefs + timezone storage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prefs_default_on_new_york_and_round_trip(users_table, async_client):
    assert (await async_client.get("/api/v1/users/me/prefs")).json() == {
        "timezone": "America/New_York",
        "dailySummary": True,
        "awayEmail": True,
        "chatTheme": None,
        "sessionsReachableFromBackgroundWork": True,  # on by default since #4378
        "sessionNotifications": user_repo.SESSION_NOTIFICATION_DEFAULTS,
    }
    off = {
        "sessionsReachableFromBackgroundWork": True,
        "awayEmail": True,
        "sessionNotifications": user_repo.SESSION_NOTIFICATION_DEFAULTS,
    }
    resp = await async_client.put("/api/v1/users/me/prefs", json={"timezone": "Europe/Paris"})
    assert resp.json() == {"timezone": "Europe/Paris", "dailySummary": True, "chatTheme": None, **off}
    resp = await async_client.put("/api/v1/users/me/prefs", json={"dailySummary": False})
    assert resp.json() == {"timezone": "Europe/Paris", "dailySummary": False, "chatTheme": None, **off}
    resp = await async_client.put("/api/v1/users/me/prefs", json={"chatTheme": "dark"})
    assert resp.json() == {"timezone": "Europe/Paris", "dailySummary": False, "chatTheme": "dark", **off}
    assert (await async_client.put("/api/v1/users/me/prefs", json={"timezone": "Mars/Base"})).status_code == 422
    assert await user_repo.get_timezone("user_test_123") == "Europe/Paris"
    # Session notifications: stored whole, read back whole; a partial body fills in the defaults.
    quiet = {"waiting": "badge", "finished": "off", "attention": "banner", "onlyUnfocused": True, "sound": False}
    resp = await async_client.put("/api/v1/users/me/prefs", json={"sessionNotifications": quiet})
    assert resp.json()["sessionNotifications"] == quiet
    assert (await async_client.get("/api/v1/users/me/prefs")).json()["sessionNotifications"] == quiet
    resp = await async_client.put("/api/v1/users/me/prefs", json={"sessionNotifications": {"finished": "badge"}})
    assert resp.json()["sessionNotifications"] == {**user_repo.SESSION_NOTIFICATION_DEFAULTS, "finished": "badge"}
    assert await user_repo.get_timezone("nobody") == "America/New_York"


@pytest.mark.asyncio
async def test_device_registration_stores_a_valid_timezone_and_ignores_junk(users_table, async_client):
    body = {"token": "ExponentPushToken[abc]", "platform": "ios", "deviceId": "d1"}
    assert (await async_client.post("/api/v1/devices", json={**body, "timezone": "Asia/Tokyo"})).status_code == 200
    assert await user_repo.get_timezone("user_test_123") == "Asia/Tokyo"
    assert (await async_client.post("/api/v1/devices", json={**body, "timezone": "junk"})).status_code == 200
    assert await user_repo.get_timezone("user_test_123") == "Asia/Tokyo"


# These tests drive the lifespan workers themselves (tests/conftest.py stubs them otherwise).
pytestmark = pytest.mark.real_background_workers
