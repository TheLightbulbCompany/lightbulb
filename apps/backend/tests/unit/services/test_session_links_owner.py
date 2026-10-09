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
async def test_owner_creates_one_link_and_it_only_shows_the_session(transport, s3):
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
    assert "pickupAllowed" not in row  # watch only: continuing is the owner's own switch
    # Same link again; the audience changes in place.
    assert (await service.get_link(ALICE, sid)).url == link.url
    again = await service.put_link(ALICE, sid, "anyone")
    assert again.url == link.url and again.audience == "anyone"
    assert (await audit(sid))[-2:] == ["share_link_created", "share_link_audience"]


@pytest.mark.asyncio
async def test_a_link_is_public_only_with_a_public_id_minted_each_time_it_is_made_public(transport, s3):
    sid = await claude_session(transport)
    await service.put_link(ALICE, sid, "anyone")
    first = (await repo.get_session(OWNER, sid))["shareLink"]["publicId"]
    assert (await service.put_link(ALICE, sid, "workspace")).audience == "workspace"
    assert "publicId" not in (await repo.get_session(OWNER, sid))["shareLink"]
    await service.put_link(ALICE, sid, "anyone")
    assert (await repo.get_session(OWNER, sid))["shareLink"]["publicId"] != first, "an old watcher's channel is dead"
    # Only me narrows it: resuming sharing never brings a public link back.
    await service.set_sharing(ALICE, sid, False)
    await service.set_sharing(ALICE, sid, True)
    assert (await service.get_link(ALICE, sid)).audience == "workspace"


@pytest.mark.asyncio
async def test_an_anyone_link_from_before_public_watch_is_a_workspace_link(transport, s3):
    sid = await claude_session(transport)
    link = await service.put_link(ALICE, sid, "anyone")
    token = link.url.split("#", 1)[1]
    messaging_repo._table().update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"}, UpdateExpression="REMOVE shareLink.publicId"
    )
    assert (await service.get_link(ALICE, sid)).audience == "workspace"
    from core.services import session_links

    with pytest.raises(ApiProblem, match="turned off or is not for you"):
        await session_links.resolve(None, token)
    assert (await session_links.resolve(BOB, token))[2] is True  # a member still opens it
    assert (await service.put_link(ALICE, sid, "anyone")).audience == "anyone"  # the owner picks Anyone again
    assert (await session_links.resolve(None, token))[0]["sessionId"] == sid


@pytest.mark.asyncio
async def test_turning_continue_off_keeps_the_link(transport, s3):
    sid = await claude_session(transport)
    link = await service.put_link(ALICE, sid, "anyone")
    await pickup.set_pickup(ALICE, sid, True)
    await pickup.set_pickup(ALICE, sid, False)
    row = await repo.get_session(OWNER, sid)
    assert not row.get("pickupAllowed") and (await service.get_link(ALICE, sid)).url == link.url


@pytest.mark.asyncio
async def test_turning_the_link_off_deletes_it_and_retires_pickup(transport, s3):
    sid = await claude_session(transport)
    link = await service.put_link(ALICE, sid, "anyone")
    await pickup.set_pickup(ALICE, sid, True)
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
    with pytest.raises(ApiProblem, match="with your workspace"):
        await service.put_link(ALICE, sid, "workspace")


@pytest.mark.asyncio
async def test_a_session_with_no_terminal_is_never_anyone_with_the_link(transport, s3):
    """It runs inside an app: there is no public page for a conversation."""
    from core.services import session_links

    sid = await claude_session(transport)
    token = (await service.put_link(ALICE, sid, "anyone")).url.split("#", 1)[1]
    assert (await session_links.resolve(None, token))[0]["sessionId"] == sid
    row = await repo.get_session(OWNER, sid)
    row.pop("holderId", None)
    messaging_repo._table().put_item(Item={**row, "terminal": "none", "terminalReason": "app"})
    # The public link made earlier opens for the workspace only, and its owner reads it so.
    with pytest.raises(ApiProblem):
        await session_links.resolve(None, token)
    assert (await session_links.resolve(BOB, token))[2] is True
    assert (await service.get_link(ALICE, sid)).audience == "workspace"
    with pytest.raises(ApiProblem, match="with your workspace only"):
        await service.put_link(ALICE, sid, "anyone")
    assert (await service.put_link(ALICE, sid, "workspace")).audience == "workspace"
    assert "publicId" not in (await repo.get_session(OWNER, sid))["shareLink"]


@pytest.mark.asyncio
async def test_a_public_link_does_not_come_back_when_an_app_session_later_has_a_terminal(transport, s3):
    """Codex on #3950: masking the stored public id would restore it with the terminal."""
    from core.services import session_links
    from tests.unit.services.test_local_session_relay import heartbeat

    sid = await claude_session(transport)
    token = (await service.put_link(ALICE, sid, "anyone")).url.split("#", 1)[1]
    # Its Mac says it runs inside an app: the link is narrowed on the row, not only read as narrow.
    await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="none", terminalReason="app"))
    assert "publicId" not in (await repo.get_session(OWNER, sid))["shareLink"]
    # The conversation later runs in a terminal: still not public, until its owner chooses Anyone again.
    await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="held"))
    row = await repo.get_session(OWNER, sid)
    assert service.session_terminal(row) == "held" and "publicId" not in row["shareLink"]
    with pytest.raises(ApiProblem):
        await session_links.resolve(None, token)
    assert (await service.get_link(ALICE, sid)).audience == "workspace"
    assert (await service.put_link(ALICE, sid, "anyone")).audience == "anyone"


@pytest.mark.asyncio
async def test_pickup_expiry_takes_the_link_with_it(transport, s3):
    sid = await claude_session(transport)
    link = await service.put_link(ALICE, sid, "workspace")
    await pickup.set_pickup(ALICE, sid, True)
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
    first = await service.put_link(ALICE, sid, None)  # Share: a workspace link
    assert first.audience == "workspace"
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
