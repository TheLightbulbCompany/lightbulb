import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers import agent_mail

OWNER, AGENT, TOKEN = "user_a", "agent_1", "hook-token-for-a"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _reset():
    agent_mail._RATE.clear()
    yield
    agent_mail._RATE.clear()


# `client` comes from tests/conftest.py: TestClient(app) with get_current_user
# overridden to AuthContext(user_id="user_test_123") (personal). Hook-token
# routes ignore that override; the /address routes rely on it.


def _container(owner_id=OWNER):
    return {"owner_id": owner_id, "hook_token": TOKEN}


def test_mail_view_defaults_channel_to_mail_and_passes_text():
    row = {"received_key": "k", "received_at": "t"}
    assert agent_mail._mail_view("a@b.c", row)["channel"] == "mail"
    assert agent_mail._mail_view("a@b.c", dict(row, channel="text"))["channel"] == "text"


def test_send_missing_bearer_401(client):
    assert (
        client.post(
            f"/api/v1/agent-mail/u/{OWNER}/send", json={"agent_id": AGENT, "subject": "s", "body_text": "b"}
        ).status_code
        == 401
    )


def test_send_wrong_token_401(client):
    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())):
        r = client.post(
            f"/api/v1/agent-mail/u/{OWNER}/send",
            json={"agent_id": AGENT, "subject": "s", "body_text": "b"},
            headers={"Authorization": "Bearer WRONG"},
        )
    assert r.status_code == 401


def test_send_both_to_and_reply_key_400(client):
    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())):
        r = client.post(
            f"/api/v1/agent-mail/u/{OWNER}/send",
            json={"agent_id": AGENT, "subject": "s", "body_text": "b", "to": "a@b.c", "reply_key": "x/y"},
            headers=H,
        )
    assert r.status_code == 400


def test_send_delegates(client):
    with (
        patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())),
        patch.object(agent_mail, "send", AsyncMock(return_value="sent")) as send,
    ):
        r = client.post(
            f"/api/v1/agent-mail/u/{OWNER}/send",
            json={"agent_id": AGENT, "subject": "s", "body_text": "b", "to": "v@x.y"},
            headers=H,
        )
    assert r.status_code == 200 and r.json() == {"status": "sent"}
    assert send.call_args.kwargs["to"] == "v@x.y" and send.call_args.kwargs["agent_id"] == AGENT


# ---- ported from the deleted routers/agent_mail_send.py test suite --------
# (same auth boundary, now on POST /u/{owner_id}/send instead of
# the deleted owner-send router — see git show 28e83f275:apps/backend/tests/unit/routers/test_agent_mail_send.py)


def _send_body(agent_id=AGENT):
    return {"agent_id": agent_id, "subject": "s", "body_text": "b"}


def test_send_unknown_owner_401(client):
    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=None)):
        r = client.post(f"/api/v1/agent-mail/u/{OWNER}/send", json=_send_body(), headers=H)
    assert r.status_code == 401


def test_send_owner_as_token_holder_cannot_send_for_a_different_owner(client):
    """owner_b's row carries a DIFFERENT hook_token, so owner_a's token fails
    the compare — the whole point of naming the owner in the path."""
    with patch.object(
        agent_mail.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_b", "hook_token": "hook-token-for-b"}),
    ):
        r = client.post(
            "/api/v1/agent-mail/u/user_b/send",
            json=_send_body(),
            headers=H,
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_send_non_ascii_bearer_is_401_not_500():
    """hmac.compare_digest raises TypeError on a non-ASCII `str`, which would
    surface as a 500 instead of a 401.

    Driven directly against `_require_hook_token`: httpx refuses to transmit a
    non-ASCII header at all, but Starlette decodes real headers as latin-1, so
    a raw ASGI request genuinely can hand us one.
    """
    from fastapi import HTTPException

    class _Req:
        headers = {"Authorization": "Bearer ünïcode"}

    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())):
        with pytest.raises(HTTPException) as exc:
            await agent_mail._require_hook_token(OWNER, _Req())
    assert exc.value.status_code == 401


def test_send_rate_limit_returns_429(client):
    with (
        patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())),
        patch.object(agent_mail, "send", AsyncMock(return_value="sent")),
    ):
        for _ in range(agent_mail.RATE_LIMIT_PER_MINUTE):
            assert client.post(f"/api/v1/agent-mail/u/{OWNER}/send", json=_send_body(), headers=H).status_code == 200
        assert client.post(f"/api/v1/agent-mail/u/{OWNER}/send", json=_send_body(), headers=H).status_code == 429


def test_send_401s_do_not_charge_the_rate_limit_bucket(client):
    """Auth runs before the limiter, so an unauthenticated flood cannot lock
    the real owner out."""
    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())):
        for _ in range(80):
            client.post(
                f"/api/v1/agent-mail/u/{OWNER}/send",
                json=_send_body(),
                headers={"Authorization": "Bearer WRONG"},
            )
    assert len(agent_mail._RATE._buckets[OWNER]) == 0


def test_inbox_lists_with_keys_and_truncates(client):
    with (
        patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())),
        patch.object(agent_mail, "team_address", AsyncMock(return_value="agents@acme.isol8.co")),
        patch.object(agent_mail, "public_for", AsyncMock(side_effect=lambda a: a)),
        patch.object(
            agent_mail,
            "list_messages",
            AsyncMock(
                return_value=[
                    {
                        "received_key": "2026#m1",
                        "from_addr": "g@github.com",
                        "subject": "Verify",
                        "received_at": "2026",
                        "text": "x" * 9000,
                    }
                ]
            ),
        ) as listed,
    ):
        r = client.get(f"/api/v1/agent-mail/u/{OWNER}/inbox?agent_id={AGENT}&limit=5", headers=H)
    body = r.json()
    assert listed.call_args.kwargs["agent_id"] == AGENT  # the shared mailbox, filtered to this agent's mail
    assert body["address"] == "agents@acme.isol8.co"
    assert body["messages"][0]["key"] == "agents@acme.isol8.co/2026#m1"
    assert len(body["messages"][0]["text"]) == 8000
    assert body["messages"][0]["channel"] == "mail"


def test_inbox_without_address_is_empty(client):
    with (
        patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())),
        patch.object(agent_mail, "team_address", AsyncMock(return_value=None)),
    ):
        r = client.get(f"/api/v1/agent-mail/u/{OWNER}/inbox?agent_id={AGENT}", headers=H)
    assert r.json() == {"address": None, "messages": []}


@pytest.mark.parametrize("agent", ["agent_1", "agent_2", None])
def test_address_is_the_team_address_for_any_agent(client, agent):
    with (
        patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())),
        patch.object(agent_mail, "_owner_email", AsyncMock(return_value="o@x.y")),
        patch.object(agent_mail, "ensure_team_address", AsyncMock(return_value=("agents@acme.isol8.co", "ok"))) as e,
        patch.object(agent_mail, "public_for", AsyncMock(side_effect=lambda a: a)),
    ):
        r = client.post(f"/api/v1/agent-mail/u/{OWNER}/address", json={"agent_id": agent}, headers=H)
    assert r.json() == {"address": "agents@acme.isol8.co", "reason": "ok"}
    assert e.call_args.args == (OWNER, "o@x.y")


def test_validate_sender_entries():
    assert agent_mail._validate_senders(["Bob@X.y", "@ClientCo.com"]) == ["bob@x.y", "@clientco.com"]
    for bad in ["nope", "@", "a@", "@x", "two@@x.y"]:
        with pytest.raises(ValueError):
            agent_mail._validate_senders([bad])


# ---- Clerk routes (owner = user_test_123 via the shared client fixture) ----


TEAM = "agents@acme.isol8.co"


def _mailbox(row=None, ensure=(TEAM, "ok"), owner_email="o@x.y"):
    """The workspace's one row behind ``_team_row``."""
    lookup = (
        AsyncMock(side_effect=owner_email)
        if isinstance(owner_email, Exception)
        else AsyncMock(return_value=owner_email)
    )
    return (
        patch.object(agent_mail, "_owner_email", lookup),
        patch.object(agent_mail, "ensure_team_address", AsyncMock(return_value=ensure)),
        patch.object(agent_mail.agent_email_repo, "get_by_address", AsyncMock(return_value=row or {"address": TEAM})),
    )


def test_get_address_returns_the_single_row(client):
    row = {"address": TEAM, "owner_id": "org_test_456", "verified_senders": ["o@x.y"], "send_to_anyone": False}
    a, b, c = _mailbox(row)
    with a, b as ensure, c:
        r = client.get("/api/v1/agent-mail/address")
    assert r.status_code == 200
    assert ensure.call_args.args == ("org_test_456", "o@x.y")
    assert r.json() == {
        "address": TEAM,
        "invite_address": TEAM,
        "verified_senders": ["o@x.y"],
        "send_to_anyone": False,
        "footer_enabled": True,
        "outbound_disabled": None,
        "status": None,
    }


def test_the_per_agent_routes_are_gone(client):
    assert client.get("/api/v1/agent-mail/addresses").status_code in (404, 405)
    assert client.patch("/api/v1/agent-mail/addresses/agent_f", json={}).status_code in (404, 405)


def test_patch_address_lowercases_and_keeps_the_owner(client):
    updated = {"address": TEAM, "verified_senders": ["bob@x.y", "@clientco.com", "o@x.y"], "send_to_anyone": True}
    a, b, c = _mailbox()
    with a, b, c, patch.object(agent_mail.agent_email_repo, "update_settings", AsyncMock(return_value=updated)) as upd:
        r = client.patch(
            "/api/v1/agent-mail/address",
            json={"verified_senders": ["Bob@X.y", "@ClientCo.com"], "send_to_anyone": True},
        )
    assert r.status_code == 200 and r.json()["send_to_anyone"] is True
    assert upd.call_args.args == (TEAM,)
    # The owner's own address ("o@x.y") is always kept in the saved list.
    assert upd.call_args.kwargs == {"verified_senders": ["bob@x.y", "@clientco.com", "o@x.y"], "send_to_anyone": True}


@pytest.mark.parametrize(
    "saved,expected",
    [
        ([], ["owner@x.y"]),  # an emptied list can never lock the owner out
        (["Bob@x.y"], ["bob@x.y", "owner@x.y"]),
        (["OWNER@x.y", "bob@x.y"], ["owner@x.y", "bob@x.y"]),  # already there (any case): no duplicate
    ],
)
def test_patch_address_always_keeps_the_owner_allowed(client, saved, expected):
    a, b, c = _mailbox(owner_email="Owner@X.y")
    with a, b, c, patch.object(agent_mail.agent_email_repo, "update_settings", AsyncMock(return_value={})) as upd:
        r = client.patch("/api/v1/agent-mail/address", json={"verified_senders": saved})
    assert r.status_code == 200
    assert upd.call_args.kwargs == {"verified_senders": expected}


def test_patch_address_saves_the_list_even_if_the_owner_lookup_fails(client):
    a, b, c = _mailbox(owner_email=RuntimeError("clerk down"))
    with a, b, c, patch.object(agent_mail.agent_email_repo, "update_settings", AsyncMock(return_value={})) as upd:
        r = client.patch("/api/v1/agent-mail/address", json={"verified_senders": ["bob@x.y"]})
    assert r.status_code == 200
    assert upd.call_args.kwargs == {"verified_senders": ["bob@x.y"]}


def test_patch_address_bad_sender_400(client):
    a, b, c = _mailbox()
    with a, b, c:
        r = client.patch("/api/v1/agent-mail/address", json={"verified_senders": ["nope"]})
    assert r.status_code == 400


def test_patch_address_reenable_sends_null(client):
    a, b, c = _mailbox()
    with (
        a,
        b,
        c,
        patch.object(agent_mail.agent_email_repo, "update_settings", AsyncMock(return_value={"address": TEAM})) as upd,
    ):
        r = client.patch("/api/v1/agent-mail/address", json={"outbound_disabled": None})
    assert r.status_code == 200
    assert upd.call_args.kwargs == {"outbound_disabled": None}


@pytest.mark.parametrize(
    "body",
    [{"outbound_disabled": None}, {"send_to_anyone": True}, {"verified_senders": ["bob@x.y"]}],
)
async def test_patch_address_is_admin_only(async_client_org_member, body):
    """A member could lift a spam-complaint pause, open the mailbox to anyone, or rewrite who
    may wake the agents. Reading stays open to members; changing it is the admin's."""
    a, b, c = _mailbox()
    with a, b, c, patch.object(agent_mail.agent_email_repo, "update_settings", AsyncMock()) as upd:
        r = await async_client_org_member.patch("/api/v1/agent-mail/address", json=body)
    assert r.status_code == 403
    upd.assert_not_called()


def test_patch_address_nonnull_outbound_disabled_400(client):
    """outbound_disabled is read-only from the owner settings surface except
    for the explicit-null re-enable — any other value must be rejected, not
    silently ignored."""
    a, b, c = _mailbox()
    with a, b, c, patch.object(agent_mail.agent_email_repo, "update_settings", AsyncMock()) as upd:
        r = client.patch("/api/v1/agent-mail/address", json={"outbound_disabled": "spam"})
    assert r.status_code == 400 and r.json()["detail"]["status"] == "outbound_disabled_readonly"
    upd.assert_not_called()


def test_address_mint_failure_409(client):
    a, b, c = _mailbox(ensure=(None, "slug_allocation_failed"))
    with a, b, c:
        r = client.patch("/api/v1/agent-mail/address", json={"send_to_anyone": True})
        g = client.get("/api/v1/agent-mail/address")
    assert r.status_code == 409 and r.json()["detail"]["status"] == "slug_allocation_failed"
    assert g.status_code == 409


# ---- the team inbox (any member) ---------------------------------------------

M1, M2, FOREIGN = (
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
)
_ROOMS = {
    M1: {
        "ownerId": "org_test_456",
        "meeting": {
            "title": "Weekly sync",
            "organizer": "pat@acme.com",
            "invitedBy": "member@acme.com",
            "status": "scheduled",
            "startAt": 200,
            "endAt": 260,
            "recurrence": {"rule": "FREQ=WEEKLY"},
            "transcriptId": "t-now",
            "transcriptRoomId": "room-now",
            "history": [{"startAt": 100, "transcriptId": "t-past", "transcriptRoomId": "room-past"}],
        },
    },
    # Ended a moment ago with no transcript: listed (only one a day old is hidden).
    M2: {
        "ownerId": "org_test_456",
        "meeting": {"title": "One-off", "status": "ended", "startAt": int(time.time()) - 600},
    },
    FOREIGN: {"ownerId": "org_other", "meeting": {"title": "Theirs", "status": "scheduled", "startAt": 400}},
}
_ROWS = [
    {
        "received_key": "k3",
        "direction": "out",
        "to": "pat@acme.com",
        "subject": "Re: hi",
        "received_at": "t3",
        "agent_id": "a1",
    },
    {
        "received_key": "k2",
        "from_addr": "pat@acme.com",
        "subject": "hi",
        "received_at": "t2",
        "agent_id": "a1",
        "text": "x" * 3000,
    },
]


def _inbox(rows=_ROWS, nxt="k2"):
    row = {"address": TEAM, "meetings": {M1, M2, FOREIGN, "gone"}}
    return (
        patch.object(agent_mail, "team_address", AsyncMock(return_value=TEAM)),
        patch.object(agent_mail.agent_email_repo, "get_by_address", AsyncMock(return_value=row)),
        # The inbox makes no Clerk call and mints nothing.
        patch.object(agent_mail, "_owner_email", AsyncMock(side_effect=AssertionError("no Clerk call"))),
        patch.object(agent_mail.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: _ROOMS.get(r))),
        patch.object(agent_mail, "list_page", AsyncMock(return_value=(rows, nxt))),
    )


async def test_a_member_reads_meetings_and_mail_both_ways(async_client_org_member):
    a, b, c, d, e = _inbox()
    with a, b, c, d, e as page:
        r = await async_client_org_member.get("/api/v1/agent-mail/inbox", params={"before": "k9", "limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["address"] == TEAM and body["next"] == "k2"
    assert page.await_args.args == (TEAM,) and page.await_args.kwargs == {"before": "k9", "limit": 2}
    # Newest first; another workspace's record and a deleted one are skipped.
    assert [m["id"] for m in body["meetings"]] == [M2, M1]
    assert body["meetings"][1] == {
        "id": M1,
        "title": "Weekly sync",
        "organizer": "pat@acme.com",
        "invited_by": "member@acme.com",
        "status": "scheduled",
        "last_bot_status": None,
        "in_call_at": None,
        "start_at": 200,
        "end_at": 260,
        "repeats": True,
        "transcripts": [
            {"start_at": 200, "ended_at": None, "notes": None, "unsaid": []},
            {"start_at": 100, "ended_at": None, "notes": None, "unsaid": []},
        ],
        "notes_room_id": "room-now",
        "reason": None,
        "ended_at": None,
        "notes": None,
        "unsaid": [],
    }
    assert body["meetings"][0]["repeats"] is False and body["meetings"][0]["transcripts"] == []
    assert body["meetings"][0]["invited_by"] is None
    out, inn = body["messages"]
    assert out == {
        "key": "k3",
        "direction": "out",
        "from": TEAM,
        "to": "pat@acme.com",
        "subject": "Re: hi",
        "at": "t3",
        "agent_id": "a1",
        "channel": "mail",
        "text": "",
        "truncated": False,
    }
    assert inn["direction"] == "in" and inn["from"] == "pat@acme.com" and inn["to"] == TEAM
    assert len(inn["text"]) == 2000 and inn["truncated"] is True


def test_inbox_limit_is_capped(client):
    assert client.get("/api/v1/agent-mail/inbox", params={"limit": 51}).status_code == 422


@pytest.mark.parametrize(("address", "row"), [(None, None), (TEAM, None)])
def test_inbox_without_a_team_address_is_409(client, address, row):
    with (
        patch.object(agent_mail, "team_address", AsyncMock(return_value=address)),
        patch.object(agent_mail.agent_email_repo, "get_by_address", AsyncMock(return_value=row)),
        patch.object(agent_mail, "ensure_team_address", AsyncMock(side_effect=AssertionError("no mint"))),
    ):
        r = client.get("/api/v1/agent-mail/inbox")
    assert r.status_code == 409 and r.json()["detail"]["status"] == "no_address"


async def test_inbox_reads_at_most_eight_meetings_at_once(async_client_org_member):
    ids = {f"{i:08d}-1111-4111-8111-111111111111" for i in range(20)}
    state = {"now": 0, "peak": 0}

    async def get_room(_r):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0)
        state["now"] -= 1
        return None

    with (
        patch.object(agent_mail, "team_address", AsyncMock(return_value=TEAM)),
        patch.object(
            agent_mail.agent_email_repo, "get_by_address", AsyncMock(return_value={"address": TEAM, "meetings": ids})
        ),
        patch.object(agent_mail.messaging_repo, "get_room", AsyncMock(side_effect=get_room)) as reads,
        patch.object(agent_mail, "list_page", AsyncMock(return_value=([], None))),
    ):
        r = await async_client_org_member.get("/api/v1/agent-mail/inbox")
    assert r.status_code == 200 and reads.await_count == 20 and state["peak"] == 8


@pytest.mark.parametrize(("start_at", "att", "room"), [(200, "t-now", "room-now"), (100, "t-past", "room-past")])
async def test_a_member_reads_current_and_past_transcripts(async_client_org_member, start_at, att, room):
    load = MagicMock(return_value=(None, b"[20:23] Pat: Hi\n"))
    with (
        patch.object(agent_mail.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: _ROOMS.get(r))),
        patch.object(agent_mail.messaging, "load_attachment", load),
    ):
        r = await async_client_org_member.get(f"/api/v1/agent-mail/meetings/{M1}/transcript?start_at={start_at}")
    assert r.status_code == 200 and r.text == "[20:23] Pat: Hi\n"
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    load.assert_called_once_with("org_test_456", room, att)


@pytest.mark.parametrize(
    "path",
    [
        f"{FOREIGN}/transcript?start_at=400",  # another workspace's meeting
        f"{M1}/transcript?start_at=150",  # no such occurrence
        f"{M2}/transcript?start_at=300",  # an occurrence with no transcript
        "not-a-uuid/transcript?start_at=1",
    ],
)
def test_transcript_404s(client, path):
    get_room = AsyncMock(side_effect=lambda r: _ROOMS.get(r))
    with patch.object(agent_mail.messaging_repo, "get_room", get_room):
        r = client.get(f"/api/v1/agent-mail/meetings/{path}")
    assert r.status_code == 404
    if path.startswith("not-a-uuid"):
        get_room.assert_not_awaited()


async def test_a_meeting_that_never_reached_a_call_drops_off_after_a_day(async_client_org_member):
    """A stale invite's record (never joined, over two days ago) is not listed; one with a
    transcript, or a call that happened with no transcript captured, always is, however old."""
    old = int(time.time()) - 2 * 86400
    rooms = {
        M1: {"ownerId": "org_test_456", "meeting": {"title": "Junk", "status": "bot_failed", "startAt": old}},
        M2: {
            "ownerId": "org_test_456",
            "meeting": {
                "title": "Kept",
                "status": "ended",
                "startAt": old,
                "endAt": old + 900,
                "history": [{"startAt": old - 86400, "transcriptId": "t", "transcriptRoomId": "r"}],
            },
        },
        FOREIGN: {
            "ownerId": "org_test_456",
            "meeting": {"title": "Held", "status": "ended", "startAt": old - 10, "inCallAt": old},
        },
    }
    a, b, c, d, e = _inbox()
    with a, b, c, d, e, patch.object(agent_mail.messaging_repo, "get_room", AsyncMock(side_effect=rooms.get)):
        r = await async_client_org_member.get("/api/v1/agent-mail/inbox")
    assert [m["title"] for m in r.json()["meetings"]] == ["Kept", "Held"]


PUBLIC = "agents@thelightbulb.company"


def test_the_agents_see_the_public_address_but_reply_keys_stay_on_the_routing_one(client):
    with (
        patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())),
        patch.object(agent_mail, "_owner_email", AsyncMock(return_value="o@x.y")),
        patch.object(agent_mail, "ensure_team_address", AsyncMock(return_value=(TEAM, "ok"))),
        patch.object(agent_mail, "team_address", AsyncMock(return_value=TEAM)),
        patch.object(agent_mail, "public_for", AsyncMock(return_value=PUBLIC)),
        patch.object(agent_mail, "list_messages", AsyncMock(return_value=[{"received_key": "k1"}])) as listed,
    ):
        minted = client.post(f"/api/v1/agent-mail/u/{OWNER}/address", json={}, headers=H).json()
        inbox = client.get(f"/api/v1/agent-mail/u/{OWNER}/inbox?agent_id={AGENT}", headers=H).json()
    assert minted == {"address": PUBLIC, "reason": "ok"}
    assert listed.await_args.args == (TEAM,)
    assert inbox["address"] == PUBLIC and inbox["messages"][0]["key"] == f"{TEAM}/k1"


def test_the_card_shows_the_public_address(client):
    a, b, c = _mailbox({"address": TEAM, "public_address": PUBLIC})
    with a, b, c:
        assert client.get("/api/v1/agent-mail/address").json()["address"] == PUBLIC


def test_people_invite_the_routing_address_which_always_reaches_ses(client):
    """8 Nov prod Meet: the Agent page said to invite agents@thelightbulb.company; that domain's mail goes to
    Google, which bounced it. The routing address (MX on SES) received the same invite."""
    a, b, c = _mailbox({"address": TEAM, "public_address": PUBLIC})
    with a, b, c:
        assert client.get("/api/v1/agent-mail/address").json()["invite_address"] == TEAM


async def test_the_team_inbox_shows_the_public_address_and_reads_the_routing_one(async_client_org_member):
    a, b, c, d, e = _inbox()
    row = {"address": TEAM, "public_address": PUBLIC}
    with a, patch.object(agent_mail.agent_email_repo, "get_by_address", AsyncMock(return_value=row)), c, d, e as page:
        body = (await async_client_org_member.get("/api/v1/agent-mail/inbox")).json()
    assert page.await_args.args == (TEAM,)
    assert body["address"] == PUBLIC
    out, inn = body["messages"]
    assert out["from"] == PUBLIC and inn["to"] == PUBLIC


# ---- one mail in full, and the meeting's notes room + failure reason ----

_FULL = {
    "received_key": "2026-10-03T00:00:00Z#<a@b>",
    "owner_id": "org_test_456",
    "from_addr": "pat@acme.com",
    "subject": "hi",
    "received_at": "t1",
    "agent_id": "a1",
    "text": "y" * 5000,
}


async def test_a_member_reads_one_mail_in_full_from_the_public_address(async_client_org_member):
    with (
        patch.object(agent_mail, "team_address", AsyncMock(return_value=TEAM)),
        patch.object(agent_mail, "public_for", AsyncMock(return_value=PUBLIC)),
        patch.object(agent_mail, "get_message", AsyncMock(return_value=_FULL)) as got,
    ):
        r = await async_client_org_member.get("/api/v1/agent-mail/message", params={"key": _FULL["received_key"]})
    assert r.status_code == 200
    body = r.json()
    assert got.await_args.args == (TEAM, _FULL["received_key"])  # the caller's own address, nobody else's
    assert len(body["text"]) == 5000 and body["truncated"] is False
    assert body["to"] == PUBLIC and body["from"] == "pat@acme.com" and body["key"] == _FULL["received_key"]


async def test_a_mail_cut_at_the_storage_cap_says_so(async_client_org_member):
    from core.repositories.agent_mail_inbox_repo import MAX_TEXT_CHARS

    with (
        patch.object(agent_mail, "team_address", AsyncMock(return_value=TEAM)),
        patch.object(agent_mail, "public_for", AsyncMock(return_value=PUBLIC)),
        patch.object(agent_mail, "get_message", AsyncMock(return_value={**_FULL, "text": "y" * MAX_TEXT_CHARS})),
    ):
        r = await async_client_org_member.get("/api/v1/agent-mail/message", params={"key": "k"})
    assert r.json()["truncated"] is True


@pytest.mark.parametrize(
    ("address", "item"),
    [(TEAM, None), (TEAM, {**_FULL, "owner_id": "org_other"}), (None, _FULL)],
)
async def test_a_key_outside_the_callers_mailbox_is_404(async_client_org_member, address, item):
    with (
        patch.object(agent_mail, "team_address", AsyncMock(return_value=address)),
        patch.object(agent_mail, "public_for", AsyncMock(return_value=PUBLIC)),
        patch.object(agent_mail, "get_message", AsyncMock(return_value=item)),
    ):
        r = await async_client_org_member.get("/api/v1/agent-mail/message", params={"key": "k"})
    assert r.status_code == 404 and r.json()["detail"] == {"status": "not_found"}


def test_an_overlong_message_key_is_rejected(client):
    with patch.object(agent_mail, "get_message", AsyncMock(side_effect=AssertionError("no read"))):
        assert client.get("/api/v1/agent-mail/message", params={"key": "k" * 513}).status_code == 422


@pytest.mark.parametrize(
    ("meeting", "default", "want"),
    [
        ({"status": "ended", "inCallAt": 1, "transcriptRoomId": "general"}, None, "general"),
        ({"status": "ended", "inCallAt": 1}, "general", "general"),  # no stored room: the default one
        ({"status": "ended", "inCallAt": 1}, M1, None),  # no default room: posted in its own hidden room
        ({"status": "ended", "inCallAt": 1, "transcriptRoomId": M1}, None, None),
        ({"status": "scheduled", "history": [{"transcriptRoomId": "general"}]}, None, "general"),
        ({"status": "bot_failed"}, "general", None),  # never in the call: no notes
    ],
)
def test_notes_room_is_the_default_room_never_the_hidden_one(meeting, default, want):
    assert agent_mail._meeting_view(M1, meeting, default)["notes_room_id"] == want


@pytest.mark.parametrize(
    ("meeting", "want"),
    [
        ({"status": "bot_failed", "botError": "google_meet_knocking_disabled"}, "not_admitted"),
        ({"status": "bot_failed", "botError": "timeout_exceeded_waiting_room"}, "not_admitted"),
        ({"status": "bot_failed", "botError": "meeting_not_found"}, None),
        ({"status": "bot_failed", "botError": "400"}, None),  # a booking failure
        ({"status": "ended", "inCallAt": 1, "botError": "google_meet_knocking_disabled"}, None),
    ],
)
def test_reason_says_only_whether_the_host_kept_the_agents_out(meeting, want):
    assert agent_mail._meeting_view(M1, meeting)["reason"] == want


async def test_the_inbox_reads_the_default_room_once_for_ended_calls(async_client_org_member):
    rooms = {
        M1: {"ownerId": "org_test_456", "meeting": {"status": "ended", "startAt": 2, "inCallAt": 2}},
        M2: {"ownerId": "org_test_456", "meeting": {"status": "ended", "startAt": 1, "inCallAt": 1}},
    }
    a, b, c, d, e = _inbox()
    with (
        a,
        b,
        c,
        d,
        e,
        patch.object(agent_mail.messaging_repo, "get_room", AsyncMock(side_effect=rooms.get)),
        patch.object(agent_mail.meeting_invites, "output_room", AsyncMock(return_value="general")) as out,
    ):
        body = (await async_client_org_member.get("/api/v1/agent-mail/inbox")).json()
    assert out.await_count == 1
    assert [m["notes_room_id"] for m in body["meetings"]] == ["general", "general"]


async def test_the_inbox_still_loads_when_the_default_room_read_fails(async_client_org_member):
    rooms = {M1: {"ownerId": "org_test_456", "meeting": {"status": "ended", "startAt": 2, "inCallAt": 2}}}
    a, b, c, d, e = _inbox()
    with (
        a,
        b,
        c,
        d,
        e,
        patch.object(agent_mail.messaging_repo, "get_room", AsyncMock(side_effect=rooms.get)),
        patch.object(agent_mail.meeting_invites, "output_room", AsyncMock(side_effect=RuntimeError("ddb"))),
    ):
        r = await async_client_org_member.get("/api/v1/agent-mail/inbox")
    assert r.status_code == 200 and r.json()["meetings"][0]["notes_room_id"] is None


async def test_a_sent_mail_shows_its_text_cut_to_the_preview(async_client_org_member):
    sent = {**_ROWS[0], "text": "z" * 2500}
    a, b, c, d, e = _inbox(rows=[sent], nxt=None)
    with a, b, c, d, e:
        out = (await async_client_org_member.get("/api/v1/agent-mail/inbox")).json()["messages"][0]
    assert out["direction"] == "out" and len(out["text"]) == 2000 and out["truncated"] is True


@pytest.mark.parametrize(
    ("meeting", "status", "reason", "notes"),
    [
        # Left the lobby (timed out, refused, removed): never in the call, so no notes either.
        ({"status": "ended", "history": [{"transcriptRoomId": "general"}]}, "bot_failed", "not_admitted", None),
        ({"status": "ended", "inCallAt": 1}, "ended", None, "general"),
        # A series already moved on to its next occurrence is untouched.
        ({"status": "scheduled", "recurrence": {"rule": "FREQ=WEEKLY"}}, "scheduled", None, None),
    ],
)
def test_an_ended_meeting_the_agents_never_entered_shows_as_not_admitted(meeting, status, reason, notes):
    view = agent_mail._meeting_view(M1, meeting, "general")
    assert (view["status"], view["reason"], view["notes_room_id"]) == (status, reason, notes)


@pytest.mark.parametrize(
    ("meeting", "want"),
    [
        ({"status": "in_call", "inCallAt": 1}, None),  # not over yet
        ({"status": "ended", "inCallAt": 1, "endedAt": 9}, "pending"),
        ({"status": "bot_failed", "inCallAt": 1, "endedAt": 9}, "pending"),  # died mid-call: notes still owed
        ({"status": "ended", "inCallAt": 1, "endedAt": 9, "notesRunId": "run-1"}, "posted"),
        ({"status": "ended", "inCallAt": 1, "endedAt": 9, "notesSkipped": "no_agent"}, "skipped"),
        ({"status": "ended", "endedAt": 9}, None),  # never admitted: no call, no notes
    ],
)
def test_the_view_says_whether_notes_exist_and_when_the_call_ended(meeting, want):
    view = agent_mail._meeting_view(M1, meeting)
    assert view["notes"] == want and view["ended_at"] == meeting.get("endedAt")


def test_the_junk_filter_still_reads_the_stored_status():
    old = time.time() - 2 * 86400
    assert agent_mail._junk({"status": "ended", "startAt": old}, time.time()) is True
    assert agent_mail._junk({"status": "ended", "startAt": time.time() - 600}, time.time()) is False


# ---- GET /agent-mail/inbox/new: the rail badge ----


async def test_the_badge_counts_the_callers_own_mail_after_the_mark(async_client_org_member):
    with (
        patch.object(agent_mail, "team_address", AsyncMock(return_value=TEAM)),
        patch.object(agent_mail, "count_received_after", AsyncMock(return_value=3)) as count,
        patch.object(agent_mail, "_meeting", AsyncMock(side_effect=AssertionError("no meeting reads"))),
    ):
        r = await async_client_org_member.get(
            "/api/v1/agent-mail/inbox/new", params={"since": "2026-10-03T12:00:00.123Z"}
        )
    assert r.status_code == 200 and r.json() == {"count": 3}
    # The caller's own team address only; past the mark's whole millisecond (stored keys carry microseconds).
    assert count.await_args.args == (TEAM, "2026-10-03T12:00:00.123999+00:00")
    assert count.await_args.kwargs == {"cap": 10}


@pytest.mark.parametrize("params", [{}, {"since": "yesterday"}, {"since": ""}])
async def test_the_badge_is_zero_without_a_usable_mark(async_client_org_member, params):
    with (
        patch.object(agent_mail, "team_address", AsyncMock(return_value=TEAM)),
        patch.object(agent_mail, "count_received_after", AsyncMock(side_effect=AssertionError("no read"))),
    ):
        r = await async_client_org_member.get("/api/v1/agent-mail/inbox/new", params=params)
    assert r.status_code == 200 and r.json() == {"count": 0}


async def test_the_badge_is_zero_before_the_workspace_has_an_address(async_client_org_member):
    with (
        patch.object(agent_mail, "team_address", AsyncMock(return_value=None)),
        patch.object(agent_mail, "count_received_after", AsyncMock(side_effect=AssertionError("no read"))),
    ):
        r = await async_client_org_member.get("/api/v1/agent-mail/inbox/new", params={"since": "2026-10-03T12:00:00Z"})
    assert r.status_code == 200 and r.json() == {"count": 0}


def test_the_view_returns_the_unsaid_answers_in_snake_case():
    unsaid = [{"runId": "r1", "agentId": "a1", "agentName": "Forge", "request": "q", "answer": "a", "at": 5}]
    view = agent_mail._meeting_view(M1, {"status": "ended", "inCallAt": 1, "unsaid": unsaid})
    assert view["unsaid"] == [{"agent_id": "a1", "agent_name": "Forge", "request": "q", "answer": "a", "at": 5}]


def test_each_transcript_row_reads_its_own_occurrence():
    """Codex P2: a past occurrence's end, notes and unsaid come from its history entry, not the
    current (next, empty) occurrence."""
    unsaid = [{"runId": "r1", "agentId": "a1", "agentName": "Forge", "request": "q", "answer": "a", "at": 5}]
    meeting = {
        "status": "scheduled",
        "history": [
            {"startAt": 200, "transcriptId": "t2", "endedAt": 260, "notesRunId": "run-2", "unsaid": unsaid},
            {"startAt": 100, "transcriptId": "t1", "endedAt": 160, "notesSkipped": "no_agent"},
            {"startAt": 50, "transcriptId": "t0"},
        ],
    }
    view = agent_mail._meeting_view(M1, meeting)
    assert view["transcripts"] == [
        {
            "start_at": 200,
            "ended_at": 260,
            "notes": "posted",
            "unsaid": [{"agent_id": "a1", "agent_name": "Forge", "request": "q", "answer": "a", "at": 5}],
        },
        {"start_at": 100, "ended_at": 160, "notes": "skipped", "unsaid": []},
        # archived before the notes state was kept: unknown, never "pending" for ever
        {"start_at": 50, "ended_at": None, "notes": None, "unsaid": []},
    ]
    # An archived occurrence is final: one whose notes failed every attempt has no marker either.
    assert agent_mail._occurrence_notes(meeting, {"endedAt": 9}) is None
    current = {"status": "ended", "inCallAt": 1, "endedAt": 9, "transcriptId": "t"}
    assert agent_mail._meeting_view(M1, current)["transcripts"][0]["notes"] == "pending"  # still owed
    assert (view["ended_at"], view["notes"], view["unsaid"]) == (None, None, [])  # the current occurrence


@pytest.fixture(autouse=True)
def _intent_allowed(monkeypatch):
    """These pin the route's delegation. The intent gate (always on since the switch retired) refuses a call with no
    matching Effect row; its own suite is tests/unit/super_agent/questions/test_effect_doors.py."""
    from unittest.mock import AsyncMock as _AsyncMock

    from core.services import effect_doors

    monkeypatch.setattr(effect_doors, "require_intent", _AsyncMock(return_value=None))
