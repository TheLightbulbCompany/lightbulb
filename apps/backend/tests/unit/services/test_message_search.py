import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.services import message_search
from core.services.messaging import ApiProblem

ROOM_A = "11111111-1111-4111-8111-111111111111"
ROOM_B = "22222222-2222-4222-8222-222222222222"


def _auth():
    auth = MagicMock()
    auth.org_id = "org_me"
    auth.user_id = "user_a"
    return auth


def _req(q="deploy", room_id=None, limit=30):
    return message_search.MessageSearchRequest(q=q, room_id=room_id, limit=limit)


def _response(status, body):
    return httpx.Response(status, content=json.dumps(body).encode())


def _rows(monkeypatch, rows):
    """rows: {(room, seq): canonical row}; patches the canonical re-read."""
    monkeypatch.setattr(message_search.messaging_repo, "get_messages_by_keys", AsyncMock(return_value=rows))


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    # Default canonical rows: every hit the tests build matches its indexed text.
    _rows(monkeypatch, {(r, s): {"text": t} for r in (ROOM_A, ROOM_B) for s, t in ((1, "x"), (4, "x"))})
    monkeypatch.setattr(message_search.settings, "SEARCH_ENDPOINT", "https://c.us-east-1.aoss.amazonaws.com")
    monkeypatch.setattr(message_search.messaging, "still_reads_room", AsyncMock(return_value=True))
    monkeypatch.setattr(message_search, "resolve_owner_id", lambda auth: "org_me")


def _bool(query):
    return query["query"]["function_score"]["query"]["bool"]


def test_index_name_matches_the_indexer_vector():
    assert message_search.index_name("org_owner") == "messages-603feeb09672e3cefd5c20fc9aa957ba"


def test_query_always_filters_to_the_given_rooms_and_keeps_old_messages_findable():
    query = message_search.build_query("deploy fix", [ROOM_A], 30)
    assert _bool(query)["filter"] == [{"terms": {"roomId": [ROOM_A]}}]
    scoring = query["query"]["function_score"]
    assert scoring["score_mode"] == "sum"
    assert {"weight": 0.5} in scoring["functions"]  # floor: an old exact match still ranks


def test_typo_tolerance_keeps_the_first_two_letters():
    fuzzy = _bool(message_search.build_query("tagline", [ROOM_A], 30))["should"][1]["multi_match"]
    assert fuzzy["fuzziness"] == "AUTO"
    assert fuzzy["prefix_length"] == 2


def test_operators_turn_off_the_fuzzy_clause():
    assert len(_bool(message_search.build_query("deploy fix", [ROOM_A], 30))["should"]) == 2
    for q in ['"deploy fix"', "deploy -prod", "depl*"]:
        assert len(_bool(message_search.build_query(q, [ROOM_A], 30))["should"]) == 1


async def test_search_queries_only_the_callers_rooms_in_their_owners_indexes():
    rooms = {ROOM_A: "org_me", ROOM_B: "org_vendor"}
    hit = {
        "_source": {
            "messageId": "m1",
            "roomId": ROOM_B,
            "messageSeq": 4,
            "threadRootMessageId": None,
            "authorName": "Ann",
            "authorKind": "human",
            "createdAt": "2026-10-04T00:00:00Z",
            "text": "x",
        },
        "highlight": {"text": ["the \u0001deploy\u0002 fix"]},
    }
    request = AsyncMock(return_value=_response(200, {"hits": {"hits": [hit]}}))
    with (
        patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value=rooms)),
        patch.object(message_search, "_request", request),
    ):
        result = await message_search.search(_auth(), _req())
    method, path, body = request.await_args.args[:3]
    assert method == "POST"
    for owner in ("org_me", "org_vendor"):
        assert message_search.index_name(owner) in path
    assert sorted(_bool(body)["filter"][0]["terms"]["roomId"]) == sorted(rooms)
    assert result.hits[0].snippet == "the \u0001deploy\u0002 fix"
    assert str(result.hits[0].room_id) == ROOM_B


async def test_a_hit_from_a_room_outside_the_callers_set_is_dropped():
    def src(room):
        return {"_source": {"messageId": "m", "roomId": room, "messageSeq": 1, "createdAt": "t", "text": "x"}}

    body = {"hits": {"hits": [src(ROOM_B), src(ROOM_A)]}}
    with (
        patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value={ROOM_A: "org_me"})),
        patch.object(message_search, "_request", AsyncMock(return_value=_response(200, body))),
    ):
        result = await message_search.search(_auth(), _req())
    assert [str(h.room_id) for h in result.hits] == [ROOM_A]


@pytest.mark.parametrize(
    "row",
    [None, {"text": "x", "deletedAt": "t"}, {"text": "old", "editedText": "new"}, {"text": "x", "editedText": "y"}],
    ids=["missing", "deleted", "edited", "edited-differs"],
)
async def test_a_hit_whose_canonical_row_is_gone_or_changed_is_dropped(monkeypatch, row):
    _rows(monkeypatch, {} if row is None else {(ROOM_A, 1): row})
    src = {"_source": {"messageId": "m", "roomId": ROOM_A, "messageSeq": 1, "createdAt": "t", "text": "x"}}
    with (
        patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value={ROOM_A: "org_me"})),
        patch.object(message_search, "_request", AsyncMock(return_value=_response(200, {"hits": {"hits": [src]}}))),
    ):
        assert (await message_search.search(_auth(), _req())).hits == []


async def test_a_hit_matching_an_edited_row_is_returned(monkeypatch):
    _rows(monkeypatch, {(ROOM_A, 1): {"text": "old", "editedText": "x"}})
    src = {"_source": {"messageId": "m", "roomId": ROOM_A, "messageSeq": 1, "createdAt": "t", "text": "x"}}
    with (
        patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value={ROOM_A: "org_me"})),
        patch.object(message_search, "_request", AsyncMock(return_value=_response(200, {"hits": {"hits": [src]}}))),
    ):
        assert len((await message_search.search(_auth(), _req())).hits) == 1


async def test_a_room_the_caller_cannot_read_is_not_searchable():
    with (
        patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value={ROOM_A: "org_me"})),
        patch.object(message_search, "_request", AsyncMock()) as request,
    ):
        with pytest.raises(ApiProblem) as error:
            await message_search.search(_auth(), _req(room_id=ROOM_B))
    assert error.value.error.code == "ROOM_NOT_FOUND"
    request.assert_not_awaited()


async def test_no_rooms_means_no_request():
    with (
        patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value={})),
        patch.object(message_search, "_request", AsyncMock()) as request,
    ):
        assert (await message_search.search(_auth(), _req())).hits == []
    request.assert_not_awaited()


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("slow"), _response(503, {})])
async def test_cold_start_is_a_retryable_503(failure):
    request = AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(return_value=failure)
    with (
        patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value={ROOM_A: "org_me"})),
        patch.object(message_search, "_request", request),
    ):
        with pytest.raises(ApiProblem) as error:
            await message_search.search(_auth(), _req())
    assert error.value.error.code == "SEARCH_UNAVAILABLE"
    assert error.value.status_code == 503


async def test_a_policy_mistake_is_not_reported_as_warm_up():
    with (
        patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value={ROOM_A: "org_me"})),
        patch.object(message_search, "_request", AsyncMock(return_value=_response(403, {}))),
    ):
        with pytest.raises(ApiProblem) as error:
            await message_search.search(_auth(), _req())
    assert error.value.error.code == "INTERNAL_ERROR"


async def test_search_off_is_a_503(monkeypatch):
    monkeypatch.setattr(message_search.settings, "SEARCH_ENDPOINT", "")
    with pytest.raises(ApiProblem) as error:
        await message_search.search(_auth(), _req())
    assert error.value.error.code == "SEARCH_UNAVAILABLE"


async def test_delete_owner_index_accepts_404_and_is_a_noop_when_off(monkeypatch):
    with patch.object(message_search, "_request", AsyncMock(return_value=_response(404, {}))) as request:
        await message_search.delete_owner_index("org_me")
    assert request.await_args.args[:2] == ("DELETE", f"/{message_search.index_name('org_me')}")
    monkeypatch.setattr(message_search.settings, "SEARCH_ENDPOINT", "")
    with patch.object(message_search, "_request", AsyncMock()) as request:
        await message_search.delete_owner_index("org_me")
    request.assert_not_awaited()


async def test_searchable_rooms_uses_the_room_doors_membership_rule(monkeypatch):
    from core.services import messaging

    monkeypatch.undo()  # the autouse stub of still_reads_room would hide the real rule

    discovered = [
        {"meta": {"roomId": ROOM_A, "ownerId": "org_me"}},
        {"meta": {"roomId": ROOM_B, "ownerId": "org_vendor"}},
        {"meta": {"roomId": "gone", "ownerId": "org_me", "deletedAt": "x"}},
        {"meta": {"roomId": "meet", "ownerId": "org_me", "hidden": True}},
        {"meta": {"roomId": "wrong-org", "ownerId": "org_vendor"}},
    ]
    members = {
        ROOM_A: None,
        ROOM_B: {"id": "user_a", "homeOwnerId": "org_me"},
        "wrong-org": {"id": "user_a", "homeOwnerId": "org_other"},
    }
    with (
        patch.object(messaging.messaging_repo, "list_rooms_for_member", AsyncMock(return_value=discovered)),
        patch.object(
            messaging.messaging_repo, "get_member", AsyncMock(side_effect=lambda room, kind, user: members.get(room))
        ),
    ):
        assert await messaging.searchable_rooms("org_me", "user_a") == {ROOM_B: "org_vendor"}


async def test_membership_revoked_during_the_search_drops_only_that_rooms_hits(monkeypatch):
    def src(room):
        return {"_source": {"messageId": "m", "roomId": room, "messageSeq": 1, "createdAt": "t", "text": "x"}}

    recheck = AsyncMock(side_effect=lambda owner, user, room, room_owner: room == ROOM_A)
    monkeypatch.setattr(message_search.messaging, "still_reads_room", recheck)
    rooms = {ROOM_A: "org_me", ROOM_B: "org_me"}
    body = {"hits": {"hits": [src(ROOM_A), src(ROOM_B)]}}
    with (
        patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value=rooms)),
        patch.object(message_search, "_request", AsyncMock(return_value=_response(200, body))),
    ):
        result = await message_search.search(_auth(), _req())
    assert [str(h.room_id) for h in result.hits] == [ROOM_A]


async def test_the_recheck_only_queries_rooms_that_have_hits(monkeypatch):
    src = {"_source": {"messageId": "m", "roomId": ROOM_A, "messageSeq": 1, "createdAt": "t", "text": "x"}}
    recheck = AsyncMock(return_value=True)
    monkeypatch.setattr(message_search.messaging, "still_reads_room", recheck)
    rooms = {ROOM_A: "org_me", ROOM_B: "org_me"}
    with (
        patch.object(message_search.messaging, "searchable_rooms", AsyncMock(return_value=rooms)),
        patch.object(message_search, "_request", AsyncMock(return_value=_response(200, {"hits": {"hits": [src]}}))),
    ):
        await message_search.search(_auth(), _req())
    assert [c.args[2] for c in recheck.await_args_list] == [ROOM_A]
