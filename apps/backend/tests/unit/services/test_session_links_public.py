"""A link's holder: the page model and the event pages (spec §3.2, §3.3)."""

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service, session_links as links
from core.services.local_sessions import PICKUP_RETENTION_SECONDS
from core.services.messaging import ApiProblem
from tests.unit.services import test_session_pickup as pickup_tests
from tests.unit.services.test_local_session_relay import relay
from tests.unit.services.test_local_sessions import ALICE, BOB, OWNER

db = pickup_tests.db
transport = pickup_tests.transport
s3 = pickup_tests.s3
_bob_is_a_member = pickup_tests._bob_is_a_member
claude_session = pickup_tests.claude_session
upload_and_commit = pickup_tests.upload_and_commit

STRANGER = AuthContext(user_id="user_elsewhere", org_id="org_other")
PUBLIC = {
    "remote": "github.com/acme/app",
    "branch": "main",
    "head": "a" * 40,
    "pushed": True,
    "public": True,
    "folder": "app",
}


@pytest.fixture(autouse=True)
def names():
    links._names.clear()
    with patch(
        "core.services.clerk_admin.get_user", AsyncMock(return_value={"first_name": "Alice", "last_name": "Doe"})
    ):
        yield


async def link_for(transport, s3, audience="anyone", repo_info=None):
    sid = await claude_session(transport)
    url = (await service.put_link(ALICE, sid, audience)).url
    extra = {"repo": repo_info} if repo_info is not None else {}
    assert (await upload_and_commit(s3, sid, **extra)).status_code == 204
    return sid, url.split("#", 1)[1]


async def say(transport, sid, texts, after=0):
    events = [
        {"kind": "text", "role": "assistant", "text": t, "truncated": False, "actor": {"userId": BOB.user_id}}
        for t in texts
    ]
    batch = {
        "type": "local.session.events",
        "sessionId": sid,
        "generation": "generation-1",
        "batchId": f"b-{after}",
        "afterSeq": after,
        "events": events,
    }
    await relay(transport).handle_message("conn-1", batch)


@pytest.mark.asyncio
async def test_anyone_link_page_names_no_ids_and_shows_a_public_repo(transport, s3):
    sid, token = await link_for(transport, s3, repo_info=PUBLIC)
    page = (await links.open_page(None, token)).model_dump(by_alias=True)
    assert page == {
        "title": page["title"],
        "ownerFirstName": "Alice",
        "provider": "claude",
        "state": "live",
        "code": {"visibility": "public", "remote": "github.com/acme/app", "branch": "main", "commit": "a" * 40},
        "continuable": True,
        "me": {"signedIn": False, "member": False},
    }
    text = json.dumps(page)
    assert OWNER not in text and ALICE.user_id not in text and sid not in text


@pytest.mark.asyncio
async def test_a_private_repo_carries_only_the_flag(transport, s3):
    _, token = await link_for(transport, s3, repo_info={**PUBLIC, "public": False})
    page = (await links.open_page(STRANGER, token)).model_dump(by_alias=True)
    assert page["code"] == {"visibility": "private"} and "acme" not in json.dumps(page)


@pytest.mark.asyncio
async def test_unpushed_or_unknown_code_is_not_shared(transport, s3):
    _, token = await link_for(transport, s3, repo_info={**PUBLIC, "pushed": False})
    assert (await links.open_page(None, token)).code.visibility == "none"


@pytest.mark.asyncio
async def test_an_older_mac_sends_no_repo_so_no_code_is_shared(transport, s3):
    _, token = await link_for(transport, s3)
    assert (await links.open_page(None, token)).code.visibility == "none"


@pytest.mark.asyncio
async def test_a_member_gets_the_session_and_the_workspace_repo_line(transport, s3):
    sid, token = await link_for(transport, s3, audience="workspace", repo_info={**PUBLIC, "public": False})
    page = await links.open_page(BOB, token)
    # No bundle was committed, so a member sees "Code not shared" too; the flag is for outsiders.
    assert page.me.member and page.session_id == sid and page.code.visibility == "none"


@pytest.mark.asyncio
async def test_a_workspace_link_reads_as_gone_to_everyone_else(transport, s3):
    _, token = await link_for(transport, s3, audience="workspace")
    for caller in (None, STRANGER, AuthContext(user_id="user_noorg", org_id=None)):
        with pytest.raises(ApiProblem, match=links.GONE):
            await links.open_page(caller, token)
    await messaging_repo.deactivate_user_stream(OWNER, BOB.user_id)  # removed from the workspace
    with pytest.raises(ApiProblem, match=links.GONE):
        await links.open_page(BOB, token)


@pytest.mark.asyncio
async def test_every_closed_door_reads_the_same(transport, s3):
    sid, token = await link_for(transport, s3)
    with pytest.raises(ApiProblem, match=links.GONE):
        await links.open_page(None, token[:-1] + ("A" if token[-1] != "A" else "B"))
    with pytest.raises(ApiProblem, match=links.GONE):
        await links.open_page(None, "short")
    # An old many-links row with no META.shareLink match reads as turned off.
    old = "o" * 43
    messaging_repo._table().put_item(
        Item={
            "pk": service._share_pk(service._token_hash(old)),
            "sk": "META",
            "ownerId": OWNER,
            "sessionId": sid,
            "linkId": "x",
            "expiresAt": 2**31,
        }
    )
    with pytest.raises(ApiProblem, match=links.GONE):
        await links.open_page(None, old)
    with patch("time.time", return_value=time.time() + PICKUP_RETENTION_SECONDS + 7200):
        with pytest.raises(ApiProblem, match=links.GONE):
            await links.open_page(None, token)
    await service.set_sharing(ALICE, sid, False)
    with pytest.raises(ApiProblem, match=links.GONE):
        await links.open_page(None, token)
    await service.set_sharing(ALICE, sid, True)
    await service.delete_link(ALICE, sid)
    with pytest.raises(ApiProblem, match=links.GONE):
        await links.open_page(None, token)


@pytest.mark.asyncio
async def test_a_private_repos_folder_is_recorded_for_copies(transport, s3):
    sid, token = await link_for(transport, s3, repo_info={**PUBLIC, "public": False})
    await say(transport, sid, ["edited /Users/alice/app/src/a.py"])
    assert (await repo.get_session(OWNER, sid))["pickupSnapshot"]["privateFolder"] == "app"
    assert (await service.get_session(ALICE, sid)).pickup.private_repo is True


def test_there_is_no_public_events_route():
    """An "anyone" link page shows facts only; the conversation reaches an outsider only as a Continue copy."""
    from routers import messaging

    assert not [r for r in messaging.router.routes if getattr(r, "path", "").endswith("/public/session-links/events")]


@pytest.mark.asyncio
async def test_owner_name_never_leaks_a_user_id(transport, s3):
    _, token = await link_for(transport, s3)
    for user in ({"username": "alice99"}, {"email_addresses": [{"email_address": "alice@x.io"}]}, None):
        links._names.clear()
        with patch("core.services.clerk_admin.get_user", AsyncMock(return_value=user)):
            assert (await links.open_page(None, token)).owner_first_name == "Someone"


@pytest.mark.asyncio
async def test_the_owner_name_is_cached_not_fetched_per_request(transport, s3):
    _, token = await link_for(transport, s3)
    fetch = AsyncMock(return_value={"first_name": "Alice"})
    with patch("core.services.clerk_admin.get_user", fetch):
        for _ in range(3):
            assert (await links.open_page(None, token)).owner_first_name == "Alice"
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_the_commit_stores_repo_info_on_the_manifest(transport, s3):
    sid, _ = await link_for(transport, s3, repo_info=PUBLIC)
    manifest = await messaging_repo._get(repo.session_pk(OWNER, sid), "PICKUP")
    assert manifest["repoInfo"] == PUBLIC


@pytest.mark.asyncio
async def test_a_mac_that_stopped_sharing_reads_as_gone(transport, s3):
    _, token = await link_for(transport, s3)
    refusal = ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session sharing is disabled.")
    with patch.object(service, "_enrolled", AsyncMock(side_effect=refusal)):
        with pytest.raises(ApiProblem, match=links.GONE):
            await links.open_page(None, token)
