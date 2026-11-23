"""The workspace sessions channel: one publish per change; removal tells the leaver's client."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.repositories import messaging_repo
from core.services import live_events, local_session_relay, messaging
from routers.messaging import live_config
from tests.unit.services import test_local_sessions as registry_tests
from tests.unit.services.test_local_sessions import ALICE, BOB, OWNER

db = registry_tests.db


@pytest.fixture
def published(db):
    sent: list[dict] = []

    async def post(_self, endpoint, body, headers, tag=None):
        sent.append(json.loads(body))
        return httpx.Response(200, json={"successful": [{"identifier": "ok"}], "failed": []})

    with (
        patch("core.services.live_events.LiveEventPublisher._post", post),
        patch("core.services.live_events._publisher", None),
        patch.object(messaging_repo.settings, "APPSYNC_EVENTS_HTTP_ENDPOINT", "https://events.invalid/event"),
    ):
        yield sent


async def _member_row(user_id: str) -> dict | None:
    workspace = await messaging_repo.ensure_workspace_id(OWNER)
    return await messaging_repo._get(messaging_repo.workspace_pk(workspace), f"MEMBER#{user_id}")


@pytest.mark.asyncio
async def test_one_publish_per_change_whatever_the_member_count(published):
    for i in range(51):
        await messaging_repo.ensure_user_stream(OWNER, f"user_{i}")
    await local_session_relay.publish_resources_changed(OWNER, ALICE.user_id, transcript=True)
    workspace = await messaging_repo.ensure_workspace_id(OWNER)
    hints = [h["channel"] for h in published if "workspace.sessions.changed" in h["events"][0]]
    assert hints == [f"/ws/{workspace}/sessions"]


@pytest.mark.asyncio
async def test_live_config_admits_a_member_to_the_sessions_channel(published):
    config = await live_config(ALICE)
    assert str(config.workspace_id) == await messaging_repo.ensure_workspace_id(OWNER)
    # The rows the AppSync handler reads: the workspace's owner, and this member.
    lookup = await messaging_repo._get(messaging_repo.workspace_pk(str(config.workspace_id)), "META")
    assert lookup["ownerId"] == OWNER
    assert (await _member_row(ALICE.user_id))["active"] is True


@pytest.mark.asyncio
async def test_enrollment_after_a_concurrent_removal_is_refused(published):
    key = await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await messaging_repo.deactivate_user_stream(OWNER, BOB.user_id)
    assert await messaging_repo.enroll_sessions_member(OWNER, BOB.user_id, key) is None
    assert await _member_row(BOB.user_id) is None


@pytest.mark.asyncio
async def test_the_user_channel_refuses_the_sessions_hint(published):
    with pytest.raises(live_events.LivePublishError):
        await live_events.get_live_event_publisher().publish_user(
            OWNER, ALICE.user_id, messaging.make_live_event("workspace.sessions.changed", {"humanUserId": "x"})
        )


def _conflict():
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "conflict"},
            "CancellationReasons": [{"Code": "TransactionConflict"}, {"Code": "None"}],
        },
        "TransactWriteItems",
    )


def _conflicting(monkeypatch, times: int) -> list[list[dict]]:
    """``_transact`` refuses the first ``times`` calls with a TransactionConflict, as DynamoDB does
    when two transactions touch the same row at once; records every call's items."""
    calls: list[list[dict]] = []
    real = messaging_repo._transact

    async def transact(items):
        calls.append(items)
        if len(calls) <= times:
            raise _conflict()
        await real(items)

    monkeypatch.setattr(messaging_repo, "_transact", transact)
    monkeypatch.setattr("core.repositories.container_repo.asyncio.sleep", AsyncMock())
    return calls


@pytest.mark.asyncio
async def test_concurrent_enrollments_never_read_as_membership_loss(published, monkeypatch):
    await live_config(ALICE)  # the workspace lookup row exists now
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    calls = _conflicting(monkeypatch, 1)
    config = await live_config(BOB)  # a conflict is retried, not answered 403
    assert config.workspace_id
    assert (await _member_row(BOB.user_id))["active"] is True
    assert all(item.get("Put", {}).get("Item", {}).get("sk") != "META" for call in calls for item in call), (
        "the workspace META row is not rewritten on every live-config"
    )
