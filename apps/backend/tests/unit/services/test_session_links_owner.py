"""The one link (spec §3.1): owner-only, one per session, the pick-up permission on Claude."""

from unittest.mock import AsyncMock, patch

import pytest

from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service, session_pickup as pickup
from core.services.messaging import ApiProblem
from tests.unit.services import test_session_pickup as pickup_tests
from tests.unit.services.test_local_session_relay import connect, registration, relay
from tests.unit.services.test_local_sessions import ALICE, BOB, MAC, OWNER

db = pickup_tests.db
transport = pickup_tests.transport
s3 = pickup_tests.s3
_bob_is_a_member = pickup_tests._bob_is_a_member
claude_session = pickup_tests.claude_session


async def codex_session(transport) -> str:
    await claude_session(transport)
    connect(transport, cid="conn-2", stamp="1001")
    await relay(transport).handle_message("conn-2", registration(nativeConversationId="codex-thread"))
    return repo.session_id(OWNER, MAC, "codex", "f" * 64, "codex-thread")


async def audit(sid):
    return [e.action for e in (await service.list_audit(ALICE, sid)).entries]


@pytest.mark.asyncio
async def test_owner_creates_one_link_that_is_the_pickup_permission(transport, s3):
    sid = await claude_session(transport)
    with pytest.raises(ApiProblem, match="owner"):
        await service.put_link(BOB, sid, "workspace")
    link = await service.put_link(ALICE, sid, "workspace")
    assert link.audience == "workspace" and "/s#" in link.url
    token = link.url.split("#", 1)[1]
    row = await repo.get_session(OWNER, sid)
    assert row["shareLink"]["tokenHash"] == service._token_hash(token) and "tokenEnc" in row["shareLink"]
    assert await messaging_repo._get(service._share_pk(service._token_hash(token)), "META") == {
        "pk": service._share_pk(service._token_hash(token)),
        "sk": "META",
        "ownerId": OWNER,
        "sessionId": sid,
    }
    assert row["pickupAllowed"] is True  # the link is the permission
    # Same link again; the audience changes in place.
    assert (await service.get_link(ALICE, sid)).url == link.url
    again = await service.put_link(ALICE, sid, "anyone")
    assert again.url == link.url and again.audience == "anyone"
    assert (await audit(sid))[-3:] == ["share_link_created", "pickup_on", "share_link_audience"]


@pytest.mark.asyncio
async def test_turning_the_link_off_deletes_it_and_retires_pickup(transport, s3):
    sid = await claude_session(transport)
    link = await service.put_link(ALICE, sid, "anyone")
    digest = service._token_hash(link.url.split("#", 1)[1])
    with pytest.raises(ApiProblem, match="owner"):
        await service.delete_link(BOB, sid)
    await service.delete_link(ALICE, sid)
    row = await repo.get_session(OWNER, sid)
    assert "shareLink" not in row and not row.get("pickupAllowed")
    assert await messaging_repo._get(service._share_pk(digest), "META") is None
    assert (await audit(sid))[-1] == "share_link_revoked"
    with pytest.raises(ApiProblem) as missing:
        await service.get_link(ALICE, sid)
    assert missing.value.status_code == 404
    await service.delete_link(ALICE, sid)  # a repeat is a no-op


@pytest.mark.asyncio
async def test_a_codex_link_never_touches_pickup(transport, s3):
    codex = await codex_session(transport)
    await service.put_link(ALICE, codex, "anyone")
    assert not (await repo.get_session(OWNER, codex)).get("pickupAllowed")
    await service.delete_link(ALICE, codex)
    assert "shareLink" not in await repo.get_session(OWNER, codex)


@pytest.mark.asyncio
async def test_a_paused_session_cannot_get_a_link(transport, s3):
    sid = await claude_session(transport)
    await service.set_sharing(ALICE, sid, False)
    with pytest.raises(ApiProblem, match="Resume sharing"):
        await service.put_link(ALICE, sid, "workspace")


@pytest.mark.asyncio
async def test_pickup_expiry_takes_the_link_with_it(transport, s3):
    sid = await claude_session(transport)
    link = await service.put_link(ALICE, sid, "workspace")
    await pickup.retire(OWNER, sid, await repo.get_session(OWNER, sid))
    assert "shareLink" not in await repo.get_session(OWNER, sid)
    assert await messaging_repo._get(service._share_pk(service._token_hash(link.url.split("#")[1])), "META") is None


@pytest.mark.asyncio
async def test_allowing_an_ask_to_pick_up_creates_a_workspace_link(transport, s3):
    sid = await claude_session(transport)
    asked = await service.request_pickup_access(BOB, sid)
    rid = (await repo.get_session(OWNER, sid))["pickupRequests"][BOB.user_id]["requestId"]
    await service.decide_pickup_request(ALICE, sid, BOB.user_id, "allow", request_id=rid)
    assert asked is not None
    assert (await service.get_link(ALICE, sid)).audience == "workspace"


@pytest.mark.asyncio
async def test_a_failed_allow_takes_its_new_link_back(transport, s3):
    sid = await claude_session(transport)
    await service.request_pickup_access(BOB, sid)
    rid = (await repo.get_session(OWNER, sid))["pickupRequests"][BOB.user_id]["requestId"]
    boom = ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")
    with patch.object(pickup, "set_pickup", AsyncMock(side_effect=boom)), pytest.raises(ApiProblem):
        await service.decide_pickup_request(ALICE, sid, BOB.user_id, "allow", request_id=rid)
    row = await repo.get_session(OWNER, sid)
    assert "shareLink" not in row and not row.get("pickupAllowed")


@pytest.mark.asyncio
async def test_share_without_an_audience_never_narrows_a_link(transport, s3):
    sid = await claude_session(transport)
    first = await service.put_link(ALICE, sid, None)  # Share: a workspace link, pick-up on
    assert first.audience == "workspace" and (await repo.get_session(OWNER, sid))["pickupAllowed"] is True
    await service.put_link(ALICE, sid, "anyone")
    again = await service.put_link(ALICE, sid, None)  # Share again (a stale or failed read)
    assert again.url == first.url and again.audience == "anyone"


@pytest.mark.asyncio
async def test_turning_off_with_no_link_still_turns_legacy_pickup_off(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)  # the removed switch: pick-up on, no link
    assert "shareLink" not in await repo.get_session(OWNER, sid)
    await service.delete_link(ALICE, sid)
    assert not (await repo.get_session(OWNER, sid)).get("pickupAllowed")


@pytest.mark.asyncio
async def test_a_session_past_retention_cannot_get_a_link(transport, s3):
    sid = await claude_session(transport)
    with patch.object(service, "pickup_expired", return_value=True):
        with pytest.raises(ApiProblem, match="too old to share"):
            await service.put_link(ALICE, sid, "workspace")
