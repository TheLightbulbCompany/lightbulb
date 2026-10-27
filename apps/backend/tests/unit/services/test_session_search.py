"""Session search: who may find what was said in a coding session."""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service, message_search
from tests.unit.services.test_local_sessions import ALICE, BOB, MAC, OWNER, db, registered  # noqa: F401 - fixture


async def _change(sid, **fields):
    row = await repo.get_session(OWNER, sid)
    await repo.put_session({**row, **fields}, row, await repo.get_enrollment(OWNER, ALICE.user_id, MAC))


@pytest.mark.usefixtures("db")
async def test_own_sessions_in_any_state_and_teammates_only_while_shared():
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await messaging_repo.ensure_user_stream(OWNER, ALICE.user_id)
    shared, only_me, on_purpose, old = [await registered(conversation=c) for c in ("a", "b", "c", "d")]
    await _change(only_me, unshared=True)
    await _change(on_purpose, endedAt=1, endedReason=service.ENDED_ON_PURPOSE)
    await _change(old, endedAt=1, endedReason="process-exited", eventSeq=3)  # ended long ago: still searchable
    assert set(await service.searchable_sessions(ALICE)) == {shared, only_me, old}
    assert set(await service.searchable_sessions(BOB)) == {shared, old}
    # A removed member searches nothing, whatever their JWT still says.
    await messaging_repo.deactivate_user_stream(OWNER, BOB.user_id)
    assert await service.searchable_sessions(BOB) == {}


def test_a_paused_removed_or_ended_on_purpose_row_stops_being_searchable():
    row = {"humanUserId": ALICE.user_id}
    assert service.still_searchable(row, BOB.user_id)
    assert service.still_searchable({**row, "unshared": True}, ALICE.user_id)
    assert not service.still_searchable({**row, "unshared": True}, BOB.user_id)
    assert not service.still_searchable({**row, "removedAt": 1}, ALICE.user_id)
    assert not service.still_searchable({**row, "endedAt": 1, "endedReason": service.ENDED_ON_PURPOSE}, ALICE.user_id)
    assert not service.still_searchable(None, ALICE.user_id)


S1, S2, S3 = (f"{n}{n}{n}{n}{n}{n}{n}{n}-0000-4000-8000-000000000000" for n in (1, 2, 3))


def _session(sid):
    return service.LocalSession.model_construct(
        session_id=sid,
        display_title=None,
        title="Fix deploy",
        cwd_name="app",
        provider="claude",
        human_user_id="user_alice",
    )


def _hit(sid, seq=4):
    return {
        "_source": {
            "sessionId": sid,
            "seq": seq,
            "authorKind": "person",
            "authorId": "user_alice",
            "authorName": "",
            "createdAt": "2026-10-26T00:00:00Z",
            "text": "why does deploy fail",
        },
        "highlight": {"text": ["why does \u0001deploy\u0002 fail"]},
    }


@pytest.fixture
def search_on(monkeypatch):
    monkeypatch.setattr(message_search.settings, "SEARCH_ENDPOINT", "https://c.us-east-1.aoss.amazonaws.com")
    monkeypatch.setattr(message_search, "resolve_owner_id", lambda auth: OWNER)


async def test_search_filters_to_readable_sessions_and_rechecks_after_the_index(search_on):
    readable = {S1: _session(S1), S2: _session(S2)}
    body = {"hits": {"hits": [_hit(S1), _hit(S2), _hit(S3)]}}
    request = AsyncMock(return_value=httpx.Response(200, content=json.dumps(body).encode()))
    # S2 was paused while the index answered; S3 was never readable.
    rows = AsyncMock(return_value=[{"humanUserId": "user_alice"}, {"humanUserId": "user_alice", "unshared": True}])
    with (
        patch.object(service, "searchable_sessions", AsyncMock(return_value=readable)),
        patch.object(service, "still_in_workspace", AsyncMock(return_value=True)),
        patch.object(message_search, "_request", request),
        patch.object(repo, "get_sessions", rows),
    ):
        result = await message_search.search_sessions(BOB, message_search.SessionSearchRequest(q="deploy"))
    method, path, query = request.await_args.args[:3]
    assert (method, path) == ("POST", f"/{message_search.index_name(OWNER)}/_search?ignore_unavailable=true")
    assert query["query"]["function_score"]["query"]["bool"]["filter"] == [{"terms": {"sessionId": [S1, S2]}}]
    assert rows.await_args.args == (OWNER, [S1, S2])
    assert [(h.session_id, h.seq, h.session_title) for h in result.hits] == [(S1, 4, "Fix deploy")]
    assert result.hits[0].snippet == "why does \u0001deploy\u0002 fail"


async def test_a_member_removed_while_the_index_answered_gets_nothing(search_on):
    body = {"hits": {"hits": [_hit(S1)]}}
    rows = AsyncMock(return_value=[{"humanUserId": "user_alice"}])
    with (
        patch.object(service, "searchable_sessions", AsyncMock(return_value={S1: _session(S1)})),
        patch.object(service, "still_in_workspace", AsyncMock(return_value=False)),
        patch.object(
            message_search, "_request", AsyncMock(return_value=httpx.Response(200, content=json.dumps(body).encode()))
        ),
        patch.object(repo, "get_sessions", rows),
    ):
        result = await message_search.search_sessions(BOB, message_search.SessionSearchRequest(q="deploy"))
    assert result.hits == []
    rows.assert_not_called()


async def test_no_readable_session_never_reaches_the_index(search_on):
    request = AsyncMock()
    with (
        patch.object(service, "searchable_sessions", AsyncMock(return_value={})),
        patch.object(message_search, "_request", request),
    ):
        result = await message_search.search_sessions(BOB, message_search.SessionSearchRequest(q="deploy"))
    assert result.hits == []
    request.assert_not_called()


def test_the_limit_caps_at_fifty():
    with pytest.raises(ValueError):
        message_search.SessionSearchRequest(q="x", limit=51)
