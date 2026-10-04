import asyncio
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
        "verified_senders": ["o@x.y"],
        "send_to_anyone": False,
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
    M2: {"ownerId": "org_test_456", "meeting": {"title": "One-off", "status": "ended", "startAt": 300}},
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
        "start_at": 200,
        "end_at": 260,
        "repeats": True,
        "transcripts": [{"start_at": 200}, {"start_at": 100}],
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
        "text": "",
    }
    assert inn["direction"] == "in" and inn["from"] == "pat@acme.com" and inn["to"] == TEAM
    assert len(inn["text"]) == 2000


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
