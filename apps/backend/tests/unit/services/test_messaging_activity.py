"""Activity feed v1 (rooms F5): mention / dm / thread_reply pointer rows written by
the REAL messaging projector, read back through ``core.services.activity``, plus
the Paperclip approval / assigned-issue items merged into the first page.

The projector module is loaded from ``apps/infra/lambda/messaging-projector`` and
pointed at the same moto table, so these tests prove the writer and the reader
agree on the row shape end to end.
"""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import importlib.util
import json
import re
from datetime import datetime, timezone
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import boto3
import pytest

from core.auth import AuthContext
from core.services import activity, messaging
from routers.teams.deps import TeamsContext
from tests.unit.repositories.test_messaging_repo import (  # noqa: F401
    FRIDAY,
    OWNER,
    USER_A,
    USER_B,
    _member,
    repo,
    table,
)

USER_C = "user_C"
ANN = {"kind": "human", "id": USER_A, "displayName": "Ann"}
BOB = {"kind": "human", "id": USER_B, "displayName": "Bob"}
CAROL = {"kind": "human", "id": USER_C, "displayName": "Carol"}
AGENT = {"kind": "agent", "id": FRIDAY, "displayName": "Friday"}

_INDEX = Path(__file__).resolve().parents[4] / "infra" / "lambda" / "messaging-projector" / "index.py"
_spec = importlib.util.spec_from_file_location("messaging_projector_index", _INDEX)
projector = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("messaging_projector_index", projector)
_spec.loader.exec_module(projector)


def auth(user_id=USER_B):
    return AuthContext(user_id=user_id, org_id=OWNER)


def ctx_for(user_id=USER_B, pc_user="pc-bob"):
    return TeamsContext(
        user_id=user_id,
        org_id=OWNER,
        owner_id=OWNER,
        company_id="co-1",
        paperclip_user_id=pc_user,
        session_cookie="cookie",
    )


@pytest.fixture(autouse=True)
def _no_user_lookups(monkeypatch):
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(side_effect=lambda uid: uid))


@pytest.fixture
def world(table):
    """The moto table with the real projector wired to it (AppSync publish stubbed)."""
    projector.MESSAGING_TABLE = "test-messaging"
    projector.USERS_TABLE = ""
    projector._ddb_client = boto3.client("dynamodb", region_name="us-east-1")
    with patch.object(projector, "_publish"):
        yield table
    projector._ddb_client = None


async def _room(kind="channel", members=(USER_A, USER_B, USER_C), name="general", client_room_id="c-1"):
    rows = [_member("human", uid, "owner" if uid == USER_A else "member") for uid in members]
    rows.append(_member("agent", FRIDAY, handle="friday"))
    room_id = repo.deterministic_room_id(OWNER, USER_A, client_room_id)
    await repo.create_room(
        owner_id=OWNER,
        room_id=room_id,
        client_room_id=client_room_id,
        creator_user_id=USER_A,
        name=None if kind == "dm" else name,
        kind=kind,
        members=rows,
        req_hash=repo.request_hash(client_room_id),
    )
    await repo.ensure_workspace_id(OWNER)
    return room_id


_ids = iter(range(10**6))


async def say(room_id, author, text="hi", *, mentions=(), thread_root=None):
    """Write through the canonical writer, then run the projector on the new row."""
    root_seq = None
    if thread_root:
        root_seq = int((await repo.get_message_by_id(room_id, thread_root))["messageSeq"])
    message, _ = await repo.create_message(
        room_id=room_id,
        owner_id=OWNER,
        idemp_pk=repo.room_pk(room_id),
        # "m-", not "c-": _room() stores its own IDEMP#human#<creator>#c-<n> row in the room
        # partition, so a "c-" message key collided whenever the module counter hit a room's
        # number -- which depended on how many tests ran before it in the process (xdist).
        idemp_sk=repo.human_idemp_sk(author["id"], f"m-{next(_ids)}"),
        req_hash=repo.request_hash(text, next(_ids)),
        author=author,
        text=text,
        mentions=[{"kind": "human", "id": uid} for uid in mentions],
        reply_to_message_id=None,
        thread_root_message_id=thread_root,
        thread_root_seq=root_seq,
        run_id=None,
        dispatch_targets=[],
        source_agent_id=None,
        client_message_id=None,
    )
    stored = await repo.get_message(room_id, message["messageSeq"])
    projector._project_message(stored)
    return message


def no_paperclip():
    return AsyncMock(side_effect=RuntimeError("no paperclip here"))


async def feed(user_id=USER_B, cursor=None, limit=30, teams_ctx=None):
    page = await activity.list_activity(
        auth(user_id), cursor=cursor, limit=limit, teams_ctx=teams_ctx or no_paperclip()
    )
    return page.model_dump(mode="json", by_alias=True)


# ---------------------------------------------------------------------------
# the message transaction's thread-participant set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_reply_records_repliers_and_mentions_on_the_root(world):
    room_id = await _room()
    root = await say(room_id, AGENT, "root")
    await say(room_id, ANN, "reply @carol", mentions=[USER_C], thread_root=root["messageId"])
    await say(room_id, AGENT, "agent reply, no humans", thread_root=root["messageId"])
    stored_root = await repo.get_message(room_id, root["messageSeq"])
    assert set(stored_root["threadParticipants"]) == {USER_A, USER_C}
    reply = await repo.get_message(room_id, 2)
    assert reply["threadRootSeq"] == root["messageSeq"]


@pytest.mark.asyncio
async def test_an_edit_or_delete_of_the_root_clears_the_thread_touch_marker(world):
    """The projector reads threadTouchedAt's PRESENCE as "reply bookkeeping, not an
    edit"; every edit/delete removes it so a late-committing edit is never skipped."""
    room_id = await _room()
    root = await say(room_id, ANN, "root")
    await say(room_id, ANN, "reply", thread_root=root["messageId"])
    assert "threadTouchedAt" in await repo.get_message(room_id, root["messageSeq"])
    await repo.update_message(OWNER, room_id, root["messageSeq"], USER_A, "fix", 0, "root, edited")
    assert "threadTouchedAt" not in await repo.get_message(room_id, root["messageSeq"])


# ---------------------------------------------------------------------------
# message kinds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mention_is_self_explaining_and_read_follows_the_room_cursor(world):
    room_id = await _room()
    message = await say(room_id, ANN, "**Ship** it @bob", mentions=[USER_B])
    page = await feed()
    [item] = page["items"]
    assert item["id"] == f"mention:{message['messageId']}:{room_id}:1"
    assert item["kind"] == "mention" and item["roomId"] == room_id and item["messageId"] == message["messageId"]
    assert item["actor"] == {"kind": "human", "id": USER_A, "name": "Ann"}
    assert item["summary"]["title"] == "Ann mentioned you in general"
    assert item["summary"]["requester"] == item["actor"]
    assert item["summary"]["preview"]["text"] == "Ship it @bob"
    assert item["state"] == "open" and item["read"] is False
    assert [a["id"] for a in item["actions"]] == ["reply", "mark_done", "open"]
    reply = item["actions"][0]
    assert reply["input"] == {"kind": "text", "label": "Reply to Ann", "required": True, "options": None, "hint": None}
    assert item["actions"][2]["navigate"]["kind"] in {"thread", "room"}
    assert page["unseenCount"] == 1 and page["sources"] == {"paperclip": "unavailable"}
    assert item["roomKind"] == "channel"
    # Viewing Activity (seen) does not clear the dot; opening the room does.
    await activity.mark_seen(auth(), activity.ActivitySeenRequest(up_to=message["createdAt"]))
    assert (await feed())["items"][0]["read"] is False
    await repo.set_read_cursor(room_id, USER_B, 1)
    assert (await feed())["items"][0]["read"] is True
    # The author and an unmentioned member see nothing.
    assert (await feed(USER_A))["items"] == []
    assert (await feed(USER_C))["items"] == []


@pytest.mark.asyncio
async def test_dm_to_me_is_an_item_but_never_for_its_author(world):
    room_id = await _room("dm", (USER_A, USER_B), client_room_id="dm-1")
    await say(room_id, ANN, "hey")
    [item] = (await feed())["items"]
    assert item["kind"] == "dm"
    assert item["summary"]["title"] == "Ann sent you a direct message"
    assert [a["id"] for a in item["actions"]] == ["reply", "open"]
    assert (await feed(USER_A))["items"] == []


@pytest.mark.asyncio
async def test_thread_reply_reaches_the_starter_repliers_and_the_mentioned(world):
    room_id = await _room()
    root = await say(room_id, BOB, "root by bob")
    await say(room_id, ANN, "first reply @carol", mentions=[USER_C], thread_root=root["messageId"])
    await say(room_id, AGENT, "friday replies", thread_root=root["messageId"])
    bob = [i["kind"] for i in (await feed(USER_B))["items"]]
    carol = [i["kind"] for i in (await feed(USER_C))["items"]]
    ann = [i["kind"] for i in (await feed(USER_A))["items"]]
    assert bob == ["thread_reply", "thread_reply"]  # started it
    assert carol == ["thread_reply", "mention"]  # mentioned in it
    assert ann == ["thread_reply"]  # replied in it (not her own reply)
    item = (await feed(USER_B))["items"][0]
    assert item["threadRootMessageId"] == root["messageId"]
    assert item["summary"]["title"] == "Friday replied in a thread in general"


@pytest.mark.asyncio
async def test_thread_replies_never_badge_but_mentions_do(world):
    room_id = await _room()
    root = await say(room_id, BOB, "root by bob")
    await say(room_id, ANN, "first reply @carol", mentions=[USER_C], thread_root=root["messageId"])
    await say(room_id, AGENT, "friday replies", thread_root=root["messageId"])
    bob, carol = await feed(USER_B), await feed(USER_C)
    # Both still count as unseen; only needs-you, mentions and DMs reach the badge.
    assert bob["unseenCount"] == 2 and bob["attentionCount"] == 0
    assert carol["unseenCount"] == 2 and carol["attentionCount"] == 1


# ---------------------------------------------------------------------------
# pagination, membership, deletes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pagination_is_newest_first_with_an_opaque_cursor(world):
    room_id = await _room()
    sent = [await say(room_id, ANN, f"@bob {n}", mentions=[USER_B]) for n in range(5)]
    seen_ids = []
    cursor = None
    pages = 0
    while True:
        page = await feed(cursor=cursor, limit=2)
        pages += 1
        seen_ids += [i["messageId"] for i in page["items"]]
        cursor = page["next"]
        if pages > 1:
            assert page["unseenCount"] is None  # the badge rides the first page only
        if cursor is None:
            break
    assert seen_ids == [m["messageId"] for m in reversed(sent)]
    with pytest.raises(messaging.ApiProblem) as bad:
        await feed(cursor="not-a-cursor!!")
    assert bad.value.status_code == 400


@pytest.mark.parametrize(
    "item_id",
    [
        "0f8fad5b-d9cb-469f-a165-70867728950e",  # a message
        "session:0f8fad5b-d9cb-469f-a165-70867728950e",  # local_session_relay
        "login:0f8fad5bd9cb469fa16570867728950e",  # website_logins
        "extension:0f8fad5bd9cb469fa16570867728950e",  # browser_extensions
    ],
)
def test_every_cursor_we_mint_is_one_we_accept(item_id):
    """A page that ended on a row that is not a message handed back a cursor the next
    request refused ("Invalid activity cursor."), so the feed could not page past it."""
    sk = f"2026-09-28T18:10:41.123456+00:00#{item_id}"
    assert activity._decode_cursor(activity._encode_cursor(sk)) == sk


@pytest.mark.parametrize(
    "cursor", ["", "not-base64!", "aGVsbG8", activity._encode_cursor("2026-09-28T18:10:41+00:00#")]
)
def test_a_cursor_we_did_not_mint_is_refused(cursor):
    with pytest.raises(activity.ApiProblem):
        activity._decode_cursor(cursor)


@pytest.mark.asyncio
async def test_leaving_a_room_hides_its_items_immediately(world):
    room_id = await _room()
    other = await _room(client_room_id="c-2", name="random")
    await say(room_id, ANN, "@bob one", mentions=[USER_B])
    await say(other, ANN, "@bob two", mentions=[USER_B])
    assert len((await feed())["items"]) == 2
    world.delete_item(Key={"pk": repo.room_pk(room_id), "sk": repo.member_sk("human", USER_B)})
    items = (await feed())["items"]
    assert [i["roomId"] for i in items] == [other]
    assert (await feed())["unseenCount"] == 1


@pytest.mark.asyncio
async def test_deleted_messages_drop_and_edits_show_the_current_text(world):
    room_id = await _room()
    first = await say(room_id, ANN, "@bob secret plan", mentions=[USER_B])
    await say(room_id, ANN, "@bob typo", mentions=[USER_B])
    await repo.update_message(OWNER, room_id, 1, USER_A, "del", 0, None)
    await repo.update_message(OWNER, room_id, 2, USER_A, "fix", 0, "@bob fixed")
    page = await feed()
    assert [i["summary"]["preview"]["text"] for i in page["items"]] == ["@bob fixed"]
    assert first["messageId"] not in json.dumps(page)
    assert "secret plan" not in json.dumps(page)


# ---------------------------------------------------------------------------
# seen
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seen_cursor_is_monotonic_and_leaves_thread_replies_unread_until_opened(world):
    room_id = await _room()
    root = await say(room_id, BOB, "root")
    reply = await say(room_id, ANN, "reply", thread_root=root["messageId"])
    page = await feed()
    assert page["unseenCount"] == 1 and page["items"][0]["read"] is False
    seen = await activity.mark_seen(auth(), activity.ActivitySeenRequest(up_to=reply["createdAt"]))
    assert seen.seen_up_to is not None
    page = await feed()
    # Seen clears the badge count, not the dot: the reply is read once the room cursor passes it.
    assert page["unseenCount"] == 0 and page["items"][0]["read"] is False
    await repo.set_read_cursor(room_id, USER_B, int(reply["messageSeq"]))
    assert (await feed())["items"][0]["read"] is True
    # An older upTo never moves the cursor back.
    await activity.mark_seen(auth(), activity.ActivitySeenRequest(up_to="2000-01-01T00:00:00+00:00"))
    assert (await feed())["unseenCount"] == 0


# ---------------------------------------------------------------------------
# Paperclip items
# ---------------------------------------------------------------------------

RAW_TYPE_KEY = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*[._][a-z0-9]+(?:[._-][a-z0-9]+)*$")


def approval(approval_id, type_, payload, *, status="pending", agent="agent-growth", created="2026-09-24T10:00:00Z"):
    return {
        "id": approval_id,
        "companyId": "co-1",
        "type": type_,
        "status": status,
        "requestedByAgentId": agent,
        "requestedByUserId": None,
        "payload": payload,
        "decidedByUserId": None,
        "decidedAt": None,
        "createdAt": created,
        "updatedAt": created,
    }


APPROVALS = [
    approval(
        "ap-hire",
        "hire_agent",
        {
            "name": "Designer",
            "role": "designer",
            "capabilities": "Brand work",
            "adapterConfig": {"authToken": "sekret"},
        },
    ),
    approval(
        "ap-board",
        "request_board_approval",
        {"title": "Approve hosting spend", "summary": "About $42/month", "recommendedAction": "Approve provider X"},
    ),
    approval(
        "ap-post",
        "request_board_approval",
        {
            "title": "distribution.public-post",
            "ruleKey": "distribution.public-post",
            "platform": "LinkedIn",
            "account": "Lightbulb company page",
            "text": "We just shipped **v2 pricing**.",
            "reason": "Launch announcement for the v2 pricing page",
        },
    ),
    approval(
        "ap-odd",
        "vendor.custom_flow",
        {"budget_code": "ops.spend_cap", "apiKey": "sk-live-123", "vendor": "Acme", "access_token": "tok"},
    ),
]


def fake_admin(approvals=APPROVALS, issues=(), runs=()):
    admin = AsyncMock()
    admin.list_agents.return_value = [{"id": "agent-growth", "name": "Growth"}, {"id": FRIDAY, "name": "Friday"}]
    admin.list_approvals.return_value = list(approvals)
    admin.list_issues.return_value = list(issues)
    admin.list_company_heartbeat_runs.return_value = list(runs)
    return admin


def teams(ctx=None):
    return AsyncMock(return_value=ctx or ctx_for())


@pytest.mark.asyncio
async def test_paperclip_items_merge_into_the_first_page_only(world, monkeypatch):
    admin = fake_admin(approvals=APPROVALS[:1])
    monkeypatch.setattr("routers.teams.agents._admin", lambda: admin)
    room_id = await _room()
    for n in range(3):
        await say(room_id, ANN, f"@bob {n}", mentions=[USER_B])
    page = await feed(limit=2, teams_ctx=teams())
    kinds = [i["kind"] for i in page["items"]]
    assert kinds.count("paperclip") == 1 and page["sources"] == {"paperclip": "ok"}
    [pc] = [i for i in page["items"] if i["kind"] == "paperclip"]
    assert pc["id"] == "paperclip:approval:ap-hire" and pc["subtype"] == "approval" and pc["read"] is False
    assert admin.list_approvals.await_args.kwargs["status"] == "pending"
    # Its 20-row page is what hid older approvals: now read only for interactions, unpaged.
    assert admin.list_attention.await_args.kwargs["params"] == {"all": "true"}
    page2 = await feed(cursor=page["next"], limit=2, teams_ctx=teams())
    assert [i["kind"] for i in page2["items"]] == ["mention"]
    assert page2["sources"] == {"paperclip": "ok"}  # not re-pulled, not failed


@pytest.mark.asyncio
async def test_paperclip_failure_never_fails_the_feed(world, monkeypatch):
    admin = fake_admin()
    admin.list_approvals.side_effect = RuntimeError("paperclip down")
    monkeypatch.setattr("routers.teams.agents._admin", lambda: admin)
    room_id = await _room()
    await say(room_id, ANN, "@bob", mentions=[USER_B])
    page = await feed(teams_ctx=teams())
    assert [i["kind"] for i in page["items"]] == ["mention"]
    assert page["sources"] == {"paperclip": "unavailable"}


@pytest.mark.asyncio
async def test_no_raw_paperclip_type_key_reaches_the_client(world, monkeypatch):
    monkeypatch.setattr("routers.teams.agents._admin", lambda: fake_admin())
    await _room()
    page = await feed(teams_ctx=teams())
    body = json.dumps(page)
    for raw in (
        "hire_agent",
        "request_board_approval",
        "distribution.public-post",
        "vendor.custom_flow",
        "ops.spend_cap",
    ):
        assert raw not in body, raw
    assert "sekret" not in body and "sk-live-123" not in body and '"tok"' not in body

    def strings(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key not in {"id", "kind", "subtype", "state", "style", "roomId", "messageId", "value"}:
                    yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)
        elif isinstance(value, str):
            yield value

    for item in page["items"]:
        for text in strings({k: item[k] for k in ("summary", "actions")}):
            assert not RAW_TYPE_KEY.match(text), text


@pytest.mark.asyncio
async def test_per_type_renderers_and_the_unknown_type_fallback(world, monkeypatch):
    monkeypatch.setattr("routers.teams.agents._admin", lambda: fake_admin())
    await _room()
    items = {i["id"]: i for i in (await feed(teams_ctx=teams()))["items"]}
    hire = items["paperclip:approval:ap-hire"]["summary"]
    assert hire["title"] == "Growth wants to hire Designer"
    assert {"label": "Role", "value": "designer"} in hire["preview"]["fields"]
    board = items["paperclip:approval:ap-board"]["summary"]
    assert board["title"] == "Growth: Approve hosting spend" and board["reason"] == "About $42/month"
    post = items["paperclip:approval:ap-post"]["summary"]
    assert post["title"] == "Growth wants to publish a post on LinkedIn"
    assert post["preview"]["text"] == "We just shipped v2 pricing."
    assert post["preview"]["destination"] == "LinkedIn · Lightbulb company page"
    assert post["reason"] == "Launch announcement for the v2 pricing page"
    odd = items["paperclip:approval:ap-odd"]["summary"]
    assert odd["title"] == "Approval requested by Growth"
    assert odd["preview"]["fields"] == [{"label": "Vendor", "value": "Acme"}]
    actions = items["paperclip:approval:ap-post"]["actions"]
    assert [a["id"] for a in actions] == ["approve", "reject", "request_revision", "open"]
    assert actions[1]["confirm"] and actions[2]["input"]["required"] is True


@pytest.mark.asyncio
async def test_assigned_issues_come_from_the_human_assignee_filter(world, monkeypatch):
    issue = {
        "id": "iss-1",
        "companyId": "co-1",
        "identifier": "LB-12",
        "title": "Fix onboarding",
        "status": "in_progress",
        "description": "Steps **here**",
        "createdByAgentId": "agent-growth",
        "assigneeUserId": "pc-bob",
        "createdAt": "2026-09-24T09:00:00Z",
        "updatedAt": "2026-09-24T09:30:00Z",
    }
    admin = fake_admin(approvals=[], issues=[issue])
    monkeypatch.setattr("routers.teams.agents._admin", lambda: admin)
    await _room()
    [item] = (await feed(teams_ctx=teams()))["items"]
    assert admin.list_issues.await_args.kwargs["params"]["assigneeUserId"] == "me"
    assert item["id"] == "paperclip:assigned_issue:iss-1" and item["subtype"] == "assigned_issue"
    assert item["summary"]["title"] == "LB-12 is assigned to you: Fix onboarding"
    assert [a["id"] for a in item["actions"]] == ["open", "change_status", "reassign"]
    assert item["actions"][1]["input"]["kind"] == "select"
    assert item["actions"][2]["input"]["kind"] == "person"


# ---------------------------------------------------------------------------
# review fixes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stalled_paperclip_never_holds_page_one(world, monkeypatch):
    import asyncio
    import time

    stall = asyncio.Event()  # never set
    admin = fake_admin()

    async def hang(**_kwargs):
        await stall.wait()

    admin.list_approvals.side_effect = hang
    monkeypatch.setattr("routers.teams.agents._admin", lambda: admin)
    monkeypatch.setattr(activity, "PAPERCLIP_BUDGET_S", 0.2)
    room_id = await _room()
    await say(room_id, ANN, "@bob", mentions=[USER_B])
    for ctx in (teams(), AsyncMock(side_effect=lambda: stall.wait())):  # a stalled fetch, then a stalled session
        started = time.monotonic()
        page = await feed(teams_ctx=ctx)
        assert time.monotonic() - started < 2
        assert [i["kind"] for i in page["items"]] == ["mention"]
        assert page["sources"] == {"paperclip": "unavailable"}


@pytest.mark.asyncio
async def test_assigned_issue_is_dated_by_creation_not_by_every_edit(world, monkeypatch):
    issue = {
        "id": "iss-1",
        "companyId": "co-1",
        "identifier": "LB-1",
        "title": "Fix",
        "status": "todo",
        "createdAt": "2026-09-20T09:00:00Z",
        "updatedAt": "2026-09-24T09:30:00Z",
    }
    monkeypatch.setattr("routers.teams.agents._admin", lambda: fake_admin(approvals=[], issues=[issue]))
    await _room()
    [item] = (await feed(teams_ctx=teams()))["items"]
    assert item["createdAt"].startswith("2026-09-20T09:00:00")


@pytest.mark.asyncio
async def test_only_type_key_shaped_discriminators_are_scrubbed(world, monkeypatch):
    board = approval("ap-1", "request_board_approval", {"action": "Launch", "title": "Launch the LinkedIn page"})
    monkeypatch.setattr("routers.teams.agents._admin", lambda: fake_admin(approvals=[board]))
    await _room()
    [item] = (await feed(teams_ctx=teams()))["items"]
    assert item["summary"]["title"] == "Growth: Launch the LinkedIn page"


@pytest.mark.asyncio
async def test_a_teammate_requester_is_named_by_their_clerk_id(world, monkeypatch):
    from types import SimpleNamespace

    asked = {
        **approval("ap-1", "request_board_approval", {"title": "Budget"}),
        "requestedByAgentId": None,
        "requestedByUserId": "pc-ann",
    }
    monkeypatch.setattr("routers.teams.agents._admin", lambda: fake_admin(approvals=[asked]))
    identities = {USER_A: "pc-ann", USER_B: "pc-bob"}
    monkeypatch.setattr(
        "routers.teams.agents._repo",
        lambda: SimpleNamespace(
            get_workspace=AsyncMock(return_value=SimpleNamespace(member_user_ids=[USER_A, USER_B])),
            get_identity=AsyncMock(side_effect=lambda uid: SimpleNamespace(paperclip_user_id=identities[uid])),
        ),
    )
    await _room()
    [item] = (await feed(teams_ctx=teams()))["items"]
    assert item["actor"] == {"kind": "human", "id": USER_A, "name": USER_A}
    assert item["summary"]["title"] == f"{USER_A}: Budget"


@pytest.mark.asyncio
async def test_a_wrong_thread_root_seq_is_not_retried_as_a_sequence_race(world, monkeypatch):
    room_id = await _room()
    root = await say(room_id, ANN, "root")
    await say(room_id, ANN, "other")
    calls = []
    real = repo._transact

    async def counting(items):
        calls.append(1)
        await real(items)

    monkeypatch.setattr(repo, "_transact", counting)
    with pytest.raises(repo.ThreadRootMismatch):
        await repo.create_message(
            room_id=room_id,
            owner_id=OWNER,
            idemp_pk=repo.room_pk(room_id),
            idemp_sk=repo.human_idemp_sk(USER_A, "c-wrong-root"),
            req_hash=repo.request_hash("x"),
            author=ANN,
            text="x",
            mentions=[],
            reply_to_message_id=None,
            thread_root_message_id=root["messageId"],
            thread_root_seq=2,
            run_id=None,
            dispatch_targets=[],
            source_agent_id=None,
            client_message_id=None,
        )
    assert len(calls) == 1
    assert int((await repo.get_room(room_id))["lastSeq"]) == 2  # nothing landed


@pytest.mark.asyncio
async def test_unseen_counts_is_the_badge_by_kind(world, monkeypatch):
    """The daily digest's source: the same unseen rule as ``unseenCount``, split by kind."""
    admin = fake_admin(approvals=APPROVALS[:2])
    monkeypatch.setattr("routers.teams.agents._admin", lambda: admin)
    room_id = await _room()
    await say(room_id, ANN, "@bob one", mentions=[USER_B])
    await say(room_id, ANN, "@bob two", mentions=[USER_B])
    dm = await _room("dm", members=(USER_A, USER_B), client_room_id="dm-1")
    await say(dm, ANN, "psst")
    page = await feed(teams_ctx=teams())
    counts = await activity.unseen_counts(OWNER, USER_B, teams())
    assert counts == {"mention": 2, "dm": 1, "approval": 2}
    assert sum(counts.values()) == page["unseenCount"]
    # Seen moves everything out, exactly like the badge.
    await activity.mark_seen(auth(), activity.ActivitySeenRequest(up_to="2100-01-01T00:00:00Z"))
    assert await activity.unseen_counts(OWNER, USER_B, teams()) == {}
    # Paperclip down: None, never an under-count.
    assert await activity.unseen_counts(OWNER, USER_B, no_paperclip()) is None


# ---------------------------------------------------------------------------
# needs you: every pending approval, pinned first; failed runs; the attention badge
# ---------------------------------------------------------------------------


def run(run_id, agent, status, created):
    return {"id": run_id, "companyId": "co-1", "agentId": agent, "status": status, "createdAt": created}


@pytest.mark.asyncio
async def test_every_pending_approval_is_pinned_above_newer_messages(world, monkeypatch):
    # 25 old approvals: more than the attention page (20) that used to hide them.
    old = [
        approval(
            f"ap-{n:02d}",
            "request_board_approval",
            {"title": f"Ask {n}"},
            created=f"2026-09-{n % 20 + 1:02d}T10:00:00Z",
        )
        for n in range(25)
    ]
    decided = approval("ap-done", "request_board_approval", {"title": "Done"}, status="approved")
    monkeypatch.setattr("routers.teams.agents._admin", lambda: fake_admin(approvals=old + [decided]))
    room_id = await _room()
    await say(room_id, ANN, "@bob newer than every approval", mentions=[USER_B])
    page = await feed(teams_ctx=teams())
    ids = [i["id"] for i in page["items"]]
    assert ids[:25] == [
        f"paperclip:approval:{a['id']}" for a in sorted(old, key=lambda a: a["createdAt"], reverse=True)
    ]
    assert ids[25].startswith("mention:") and "paperclip:approval:ap-done" not in ids
    first = page["items"][0]
    assert first["summary"]["title"].startswith("Growth: Ask") and first["actor"]["name"] == "Growth"
    assert [a["id"] for a in first["actions"]] == ["approve", "reject", "request_revision", "open"]


@pytest.mark.asyncio
async def test_failed_runs_are_each_agents_latest_and_count_on_the_badge(world, monkeypatch):
    runs = [
        run("r-old-fail", "agent-growth", "failed", "2026-09-24T08:00:00Z"),
        run("r-new-ok", "agent-growth", "succeeded", "2026-09-24T09:00:00Z"),  # growth recovered
        run("r-fri", FRIDAY, "timed_out", "2026-09-24T09:30:00Z"),
    ]
    admin = fake_admin(approvals=APPROVALS[1:2], runs=runs)
    monkeypatch.setattr("routers.teams.agents._admin", lambda: admin)
    await _room()
    page = await feed(teams_ctx=teams())
    ids = [i["id"] for i in page["items"]]
    assert ids == ["paperclip:approval:ap-board", "paperclip:failed_run:r-fri"]  # approvals, then runs
    fri = page["items"][1]
    assert fri["subtype"] == "failed_run" and fri["summary"]["title"] == "Friday's last run timed out"
    assert [(a["id"], a["navigate"]["kind"], a["navigate"]["id"]) for a in fri["actions"]] == [("open", "run", "r-fri")]
    assert admin.list_company_heartbeat_runs.await_args.kwargs["summary"] is True
    assert page["attentionCount"] == 2
    # Seen: still counted, because each still needs a decision or a look.
    await activity.mark_seen(auth(), activity.ActivitySeenRequest(up_to="2026-09-25T00:00:00Z"))
    page = await feed(teams_ctx=teams())
    assert page["unseenCount"] == 0 and page["attentionCount"] == 2


@pytest.mark.asyncio
async def test_a_runs_failure_never_costs_the_approvals(world, monkeypatch):
    admin = fake_admin(approvals=APPROVALS[1:2])
    admin.list_company_heartbeat_runs.side_effect = RuntimeError("403")
    monkeypatch.setattr("routers.teams.agents._admin", lambda: admin)
    await _room()
    page = await feed(teams_ctx=teams())
    assert [i["id"] for i in page["items"]] == ["paperclip:approval:ap-board"]
    assert page["sources"] == {"paperclip": "ok"}


# ---------------------------------------------------------------------------
# issue-thread interactions: an agent waiting on a human, pinned beside approvals
# ---------------------------------------------------------------------------


def interaction(interaction_id, kind, payload, *, issue_id="iss-1", status="pending", created="2026-09-24T12:00:00Z"):
    return {
        "id": interaction_id,
        "companyId": "co-1",
        "issueId": issue_id,
        "kind": kind,
        "status": status,
        "title": None,
        "createdByAgentId": "agent-growth",
        "payload": payload,
        "createdAt": created,
        "updatedAt": created,
    }


INTERACTIONS = [
    interaction(
        "int-confirm", "request_confirmation", {"version": 1, "prompt": "Send the launch email to 2,000 people?"}
    ),
    interaction(
        "int-q",
        "ask_user_questions",
        {
            "version": 1,
            "questions": [
                {
                    "id": "q1",
                    "prompt": "Which plan name?",
                    "selectionMode": "single",
                    "options": [{"id": "o1", "label": "Pro"}, {"id": "o2", "label": "Team"}],
                }
            ],
        },
        issue_id="iss-2",
        created="2026-09-24T11:00:00Z",
    ),
]
ISSUES = {
    "iss-1": {"id": "iss-1", "companyId": "co-1", "identifier": "LB-12", "title": "Launch email"},
    "iss-2": {"id": "iss-2", "companyId": "co-1", "identifier": "LB-13", "title": "Pricing page"},
}


def with_interactions(admin, rows=INTERACTIONS):
    """The attention feed lists each pending interaction; the issue reads hydrate them."""
    admin.list_attention.return_value = {
        "items": [
            {
                "sourceKind": "issue_thread_interaction",
                "subject": {"kind": "interaction", "id": r["id"], "metadata": {"issueId": r["issueId"]}},
                "relatedIssue": {"kind": "issue", **ISSUES[r["issueId"]]},
            }
            for r in rows
        ]
        + [{"sourceKind": "failed_run", "subject": {"id": "run-x"}}]
    }
    admin.list_issue_interactions.side_effect = lambda issue_id, **_: [r for r in rows if r["issueId"] == issue_id]
    admin.get_issue.side_effect = lambda issue_id, **_: ISSUES[issue_id]
    return admin


@pytest.mark.asyncio
async def test_pending_interactions_are_pinned_after_approvals_and_counted(world, monkeypatch):
    admin = with_interactions(fake_admin(approvals=APPROVALS[1:2]))
    monkeypatch.setattr("routers.teams.agents._admin", lambda: admin)
    room_id = await _room()
    await say(room_id, ANN, "@bob newer than both", mentions=[USER_B])
    page = await feed(teams_ctx=teams())
    ids = [i["id"] for i in page["items"]]
    assert ids[:3] == [
        "paperclip:approval:ap-board",
        "paperclip:interaction:iss-1:int-confirm",
        "paperclip:interaction:iss-2:int-q",
    ]
    assert ids[3].startswith("mention:")
    confirm, question = page["items"][1:3]
    assert confirm["subtype"] == "interaction" and confirm["actor"]["name"] == "Growth"
    assert confirm["summary"]["title"] == "Growth needs your confirmation: Launch email"
    assert confirm["summary"]["reason"] == "Send the launch email to 2,000 people?"
    assert confirm["summary"]["links"] == [{"kind": "issue", "id": "iss-1", "label": "LB-12"}]
    assert [a["id"] for a in confirm["actions"]] == ["accept", "reject", "open"]
    assert confirm["actions"][2]["navigate"] == {"kind": "issue", "id": "iss-1", "roomId": None}
    assert question["summary"]["title"] == "Growth has a question for you: Pricing page"
    answer = question["actions"][0]
    assert answer["id"] == "answer" and [o["value"] for o in answer["input"]["options"]] == ["o1", "o2"]
    assert page["attentionCount"] == 4  # approval + two interactions + the unseen mention
    await activity.mark_seen(auth(), activity.ActivitySeenRequest(up_to=datetime.now(timezone.utc)))
    assert (await feed(teams_ctx=teams()))["attentionCount"] == 3  # still waiting on you once seen


@pytest.mark.asyncio
async def test_a_richer_question_set_is_answered_in_the_task(world, monkeypatch):
    rows = [
        interaction(
            "int-multi",
            "ask_user_questions",
            {"version": 1, "questions": [{"id": "a", "prompt": "One?"}, {"id": "b", "prompt": "Two?"}]},
        )
    ]
    monkeypatch.setattr("routers.teams.agents._admin", lambda: with_interactions(fake_admin(approvals=[]), rows))
    await _room()
    [item] = (await feed(teams_ctx=teams()))["items"]
    assert [(a["id"], a["label"]) for a in item["actions"]] == [("open", "Answer in task")]
    assert [f["value"] for f in item["summary"]["preview"]["fields"]] == ["One?", "Two?"]


@pytest.mark.asyncio
async def test_an_attention_failure_costs_only_the_interactions(world, monkeypatch):
    admin = fake_admin(approvals=APPROVALS[1:2])
    admin.list_attention.side_effect = RuntimeError("403")
    monkeypatch.setattr("routers.teams.agents._admin", lambda: admin)
    await _room()
    page = await feed(teams_ctx=teams())
    assert [i["id"] for i in page["items"]] == ["paperclip:approval:ap-board"]
