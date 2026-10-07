# Session Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every shared session gets one hidden chat room beside its terminal, where people (and an @mentioned CEO) talk about the session without any path from "can chat" to "can type into the terminal".

**Architecture:** A new backend module `core/services/session_chat.py` owns the room: a deterministic id per session, a Clerk-only door `POST /messaging/sessions/{id}/chat` that creates the room lazily and seats whoever can read the session, and hooks that empty it on unshare/revoke and archive it on end. The web mounts a second `useRooms` instance in a right column of `SessionFocus` (the main room slot lends it the one live room subscription while it is open); the phone pushes the existing room screen. Agents learn the convention from the `lightbulb` skill.

**Tech Stack:** FastAPI + DynamoDB (Moto in tests), pytest; Next.js 16 / React 19 + vitest; `@lightbulb/shared` (vitest); Expo / React Native + jest; Hermes skill markdown.

**Spec:** `docs/superpowers/specs/2026-10-07-session-chat-design.md` (its citations are from `dfce908f8`; this plan cites the tree at `f65f41259`). Scope = P2 of `2026-10-07-terminal-everywhere.md`: S9 backend door + hooks (Tasks 1–2), S10 web (Tasks 3–5), S11 phone (Task 6), S12 skill (Task 7), Accept (Task 8).

## Global Constraints

- **Default agent members:** the workspace's CEO only, on `mentions` — `starter_agent_ids["ceo"]` when the Paperclip company row has it; for a legacy/founder roster with none, the roster's root agent (`role == "ceo"` and no `reportsTo`, the rule `core/services/onboarding_brief.py:579` uses); else no agent. The owner adds other agents through the normal member door (`POST /messaging/rooms/{id}/members`). Humans are never added or removed by hand.
- **Member cap:** `MAX_ROOM_MEMBERS` stays 32 (`messaging_repo.py:65`). A reader past the cap is not seated; emit `put_metric("messaging.session_room.capped")` like the vendor room (`messaging.py:2008-2012`, `2058-2061`). No new cap.
- **Search:** hidden session rooms stay indexed; change nothing in the indexer. (Found while planning: `searchable_rooms`, `messaging.py:1683`, reads `_visible_room_metas`, which drops `hidden` rooms at `:1673`, so search never returns session-chat hits. Leave it.)
- **Chat never grants control:** the chat composer posts through `create_human_message` only. Nothing in this work references `allowedSenders`, `teamControl`, `joinRequests`, `submit_command`, `may_control`, `request_join` or `decide_join` — enforced by a test (Task 1).
- **Layout:** ≥1180 px a 344 px right column on the `.room-has-context` grid pattern (`messaging.css:362-366`); 860–1179 px an overlay like Room details (`:368-377`); <860 px and the phone the chat replaces/pushes the session view. The column lives in `SessionFocus`, never in `LocalSessionPanel.tsx`. CSS is append-only new classes; existing session rules are not edited. `SessionTerminalView.tsx` is not touched.
- **Public-link guests get no chat;** the `/s` page is unchanged. The door takes `get_human_session` (Clerk session JWT only — `core/auth.py:255`).
- **Identity:** chat rows render through `RoomMessageItem`; a person's name colour comes from the same FNV-1a `colorFor` palette as the terminal mod (`apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/rules.ts:86-94`, keyed on the Clerk user id; the session's owner is the mod's `OWNER_COLOR` gray, which the web renders as `--fg-soft`). The web has no per-person colour today (only `agentTint` for agents), so Task 3 adds the shared port and Task 4 the tokens.
- **Copy:** chat composer label `Message the people here`; header toggle `Chat` (`Chat, N unread`); archived: `This session ended. Its chat is read-only.`; full: `This chat is full.`; any other failure: `Chat is not available for this session.` Room name `Session chat · <title>` (title = display title, title, folder, else `Session`, cut to 120).
- No CDK, no infra, no projector change: the projector already notifies a `channel` only on @mention and thread replies (`apps/infra/lambda/messaging-projector/index.py:691-701`, called at `:766`).
- Do not run the full suites locally; run only the files each task names (CI runs the rest).

## Review Focus

- **Two people open a brand-new session's chat at the same instant** → both land in one room, nobody sees an error (Task 1: `create_room` conflict re-reads).
- **A teammate has the chat open when the owner pauses sharing** → their live channel goes silent at once (delivery-key rotation) and their next open is a 404 (Task 2: Moto test through `set_sharing`).
- **A session ends, then the same conversation is resumed** (`local_session_relay.py:558-559` un-ends it under the same id) → the chat reopens for posting on the next open (Task 1: `_follow_lifecycle` test).
- **A push or Activity tap on a session-chat @mention** → opens the session with its chat column, not a blank or forbidden room (Task 5: notification routing test).
- **An agent posts into the session chat without a run id** (e.g. `lightbulb-post.sh`) → the post stays in the session chat; today any `hidden` room redirects such posts to `#general` because hidden meant "Meet room" (`messaging.py:4186-4210`) (Task 2: redirect narrowed to rooms with `meeting`, with a test).

---

### Task 1: Backend room door

**Files:**
- Create: `apps/backend/core/services/session_chat.py`
- Modify: `apps/backend/core/services/messaging.py` (add `SessionRef` before `RoomSummary` at `:244`; field on `RoomSummary` after `archived_by` at `:287`; `_room_summary_fields` `:1568-1592`; `is_session_room` after `is_default_room` `:1766-1775`)
- Modify: `apps/backend/routers/messaging.py` (import list `:32-41`; new route after `/input` `:250-255`)
- Test: `apps/backend/tests/unit/services/test_session_chat.py` (new)

**Interfaces:**
- Produces: `session_chat.session_room_id(owner_id: str, session_id: str) -> str`; `session_chat.open_chat(auth: AuthContext, session_id: str) -> RoomDetail`; `session_chat._ceo_member(company, roster: list[dict]) -> list[dict]`; `session_chat._set_archived(meta: dict, archived: bool) -> dict`; `messaging.is_session_room(meta: dict) -> bool`; `messaging.SessionRef(session_id: str)`; `RoomSummary.session: SessionRef | None` (wire `session: {sessionId} | null`); route `POST /api/v1/messaging/sessions/{session_id}/chat` → `RoomDetail`.

- [ ] **Step 1: Write the failing tests** — create `apps/backend/tests/unit/services/test_session_chat.py`:

```python
"""The session chat door (spec 2026-10-07-session-chat-design §2, §4.1)."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.auth import AuthContext
from core.services import messaging, session_chat
from core.services.messaging import ApiProblem

OWNER = "org_o"
ALICE = AuthContext(user_id="user_alice", org_id=OWNER)
BOB = AuthContext(user_id="user_bob", org_id=OWNER)
SID = "11111111-1111-4111-8111-111111111111"
CEO_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ENG_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ROOM = session_chat.session_room_id(OWNER, SID)
ROW = {"sessionId": SID, "ownerId": OWNER, "humanUserId": ALICE.user_id, "title": "Fix relay"}
META = {
    "roomId": ROOM, "ownerId": OWNER, "kind": "channel", "revision": 1, "lastSeq": 0,
    "hidden": True, "session": {"sessionId": SID, "ownerId": ALICE.user_id},
}
CEO = {"kind": "agent", "id": CEO_ID, "displayName": "CEO", "mentionHandle": "ceo", "role": "member", "listenMode": "mentions"}


@pytest.fixture
def env(monkeypatch):
    fakes = {
        "get_room": AsyncMock(return_value=None),
        "create_room": AsyncMock(return_value=META),
        "get_member": AsyncMock(return_value=None),
        "upsert_member": AsyncMock(return_value={**META, "revision": 2}),
        "ensure_user_stream": AsyncMock(return_value="key"),
        "patch_room_meta": AsyncMock(side_effect=lambda room_id, updates: {**META, **updates, "revision": 2}),
    }
    for name, fake in fakes.items():
        monkeypatch.setattr(session_chat.messaging_repo, name, fake)
    monkeypatch.setattr(session_chat.local_sessions, "authorize_session", AsyncMock(return_value=(ROW, {})))
    monkeypatch.setattr(session_chat, "_default_agents", AsyncMock(return_value=[CEO]))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(side_effect=lambda uid: uid.removeprefix("user_").title()))
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock(side_effect=lambda meta, user_id, member=None: meta))
    metrics: list[str] = []
    monkeypatch.setattr(session_chat, "put_metric", lambda name, *a, **k: metrics.append(name))
    fakes["metrics"] = metrics
    return fakes


@pytest.mark.asyncio
async def test_first_open_creates_one_hidden_room_and_seats_the_reader(env):
    await session_chat.open_chat(BOB, SID)
    session_chat.local_sessions.authorize_session.assert_awaited_once_with(BOB, None, SID)
    kwargs = env["create_room"].await_args.kwargs
    assert (kwargs["room_id"], kwargs["kind"], kwargs["creator_user_id"]) == (ROOM, "channel", ALICE.user_id)
    assert kwargs["name"] == "Session chat · Fix relay"
    assert kwargs["extra"] == {"hidden": True, "session": {"sessionId": SID, "ownerId": ALICE.user_id}}
    owner, ceo = kwargs["members"]
    assert (owner["kind"], owner["id"], owner["role"]) == ("human", ALICE.user_id, "owner")
    assert ceo == CEO
    seated = env["upsert_member"].await_args.kwargs["member"]
    assert (seated["kind"], seated["id"], seated["role"], seated["listenMode"]) == ("human", BOB.user_id, "member", None)
    env["ensure_user_stream"].assert_any_await(OWNER, BOB.user_id)


@pytest.mark.asyncio
async def test_a_seated_reader_is_never_seated_twice(env):
    env["get_room"].return_value = META
    env["get_member"].return_value = {"kind": "human", "id": BOB.user_id}
    assert await session_chat.open_chat(BOB, SID) == META
    env["create_room"].assert_not_awaited()
    env["upsert_member"].assert_not_awaited()


@pytest.mark.asyncio
async def test_two_first_opens_at_once_share_the_room(env):
    env["create_room"].side_effect = session_chat.messaging_repo.IdempotencyConflict("raced")
    env["get_room"].side_effect = [None, META]
    await session_chat.open_chat(BOB, SID)
    assert env["upsert_member"].await_args.kwargs["room_id"] == ROOM


@pytest.mark.asyncio
async def test_the_room_follows_the_session_end_and_a_resume(env):
    env["get_room"].return_value = META
    env["get_member"].return_value = {"kind": "human", "id": BOB.user_id}
    session_chat.local_sessions.authorize_session.return_value = ({**ROW, "endedAt": 10}, {})
    await session_chat.open_chat(BOB, SID)
    updates = env["patch_room_meta"].await_args.args[1]
    assert updates["archivedAt"] and updates["archivedBy"] == "__system__"
    # The same conversation resumed under the same id: the chat takes posts again.
    env["get_room"].return_value = {**META, "archivedAt": "2026-10-07T00:00:00+00:00"}
    session_chat.local_sessions.authorize_session.return_value = (ROW, {})
    await session_chat.open_chat(BOB, SID)
    assert env["patch_room_meta"].await_args.args[1] == {"archivedAt": None, "archivedBy": None}


@pytest.mark.asyncio
async def test_a_full_chat_refuses_with_the_vendor_rooms_metric(env):
    env["get_room"].return_value = META
    env["upsert_member"].side_effect = session_chat.messaging_repo.MemberLimitExceeded("full")
    with pytest.raises(ApiProblem) as refused:
        await session_chat.open_chat(BOB, SID)
    assert refused.value.error.code == "MEMBER_LIMIT"
    assert env["metrics"] == ["messaging.session_room.capped"]


@pytest.mark.asyncio
async def test_a_session_the_reader_cannot_read_has_no_chat(env):
    session_chat.local_sessions.authorize_session.side_effect = ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    with pytest.raises(ApiProblem):
        await session_chat.open_chat(BOB, SID)
    env["create_room"].assert_not_awaited()


def test_the_ceo_is_the_starter_ceo_else_the_legacy_root_else_nobody():
    roster = [
        {"id": CEO_ID, "name": "Friday", "role": "ceo", "reportsTo": None},
        {"id": ENG_ID, "name": "Atlas", "role": "engineer", "reportsTo": CEO_ID},
    ]
    starter = SimpleNamespace(status="active", starter_agent_ids={"ceo": ENG_ID})
    assert [m["id"] for m in session_chat._ceo_member(starter, roster)] == [ENG_ID]
    [ceo] = session_chat._ceo_member(SimpleNamespace(status="active", starter_agent_ids=None), roster)
    assert (ceo["id"], ceo["role"], ceo["listenMode"]) == (CEO_ID, "member", "mentions")
    assert session_chat._ceo_member(SimpleNamespace(status="active", starter_agent_ids=None), roster[1:]) == []
    assert session_chat._ceo_member(SimpleNamespace(status="pending", starter_agent_ids={"ceo": CEO_ID}), roster) == []


def test_a_session_room_projects_its_session_and_nothing_else_does():
    member = {"kind": "human", "id": BOB.user_id}
    fields = messaging._room_summary_fields({**META, "updatedAt": "t", "createdAt": "t"}, member, None)
    assert fields["session"] == messaging.SessionRef(session_id=SID)
    assert messaging.is_session_room(META) and not messaging.is_session_room({"roomId": ROOM})


def test_the_chat_door_never_touches_session_control():
    """Spec decision 3: chat posts through create_human_message only and never grants control."""
    from routers import messaging as router

    source = inspect.getsource(session_chat) + inspect.getsource(router.open_local_session_chat)
    for name in ("allowedSenders", "allowed_senders", "teamControl", "team_control", "joinRequests",
                 "join_requests", "submit_command", "may_control", "request_join", "decide_join"):
        assert name not in source, name
```

- [ ] **Step 2: Run to verify it fails**

Run (from `apps/backend`): `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/services/test_session_chat.py -q --no-cov`
Expected: collection error `ImportError: cannot import name 'session_chat'`.

- [ ] **Step 3: Add the messaging projections** — in `core/services/messaging.py`, insert directly above `class RoomSummary(ContractModel):` (`:244`):

```python
class SessionRef(ContractModel):
    session_id: str
```

At the end of `RoomSummary` (after `archived_by: str | None = None`, `:287`):

```python
    # A session's chat room (hidden from the room list): the session it talks beside.
    session: SessionRef | None = None
```

In `_room_summary_fields`, add as the last dict entry (after `"archived_by": meta.get("archivedBy"),`):

```python
        "session": SessionRef(session_id=meta["session"]["sessionId"]) if meta.get("session") else None,
```

Directly after `is_default_room` (ends `:1775`):

```python
def is_session_room(meta: dict) -> bool:
    """A session's chat (spec 2026-10-07-session-chat-design): hidden, and its humans follow session read access."""
    return bool(meta.get("session"))
```

- [ ] **Step 4: Create `apps/backend/core/services/session_chat.py`**

```python
"""Session chat: one hidden channel per shared session, where people talk beside the terminal.

Spec docs/superpowers/specs/2026-10-07-session-chat-design.md. Membership is a projection of
session read access: whoever can read the session is seated when they open the chat, and nobody
is added or removed by hand. Nothing here reads or writes a session's control fields, so a chat
seat never lets anyone type into the terminal. Posting is the ordinary room message door.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from botocore.exceptions import ClientError

from core.auth import AuthContext, resolve_owner_id
from core.constants import SYSTEM_ACTOR_ID
from core.observability.metrics import put_metric
from core.repositories import messaging_repo
from core.services import local_sessions, messaging
from core.services.messaging import ApiProblem, RoomDetail

logger = logging.getLogger(__name__)

TITLE_MAX = 120


def session_room_id(owner_id: str, session_id: str) -> str:
    """Stable identity for one session's chat, like ``messaging.default_room_id``."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lightbulb-session-room|{owner_id}|{session_id}"))


def _ceo_member(company: Any, roster: list[dict]) -> list[dict]:
    """The workspace's CEO on `mentions` (ruling 2026-10-07): the role-keyed starter id, else a
    legacy roster's root agent (role ``ceo``, reporting to nobody), else nobody."""
    if getattr(company, "status", None) != "active":
        return []
    starter = (getattr(company, "starter_agent_ids", None) or {}).get("ceo")
    if starter:
        ceo = next((agent for agent in roster if agent["id"] == starter), None)
    else:
        ceo = next((agent for agent in roster if agent.get("role") == "ceo" and not agent.get("reportsTo")), None)
    if ceo is None:
        return []
    return [
        {
            "kind": "agent",
            "id": ceo["id"],
            "displayName": ceo["name"],
            "mentionHandle": messaging.mention_handles(roster)[ceo["id"]],
            "role": "member",
            "listenMode": "mentions",
        }
    ]


async def _default_agents(owner_id: str) -> list[dict]:
    from core.repositories.paperclip_repo import PaperclipRepo

    company = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id, consistent_read=True)
    if getattr(company, "status", None) != "active":
        return []
    return _ceo_member(company, await messaging.load_agent_roster(owner_id))


async def _create(owner_id: str, room_id: str, row: dict) -> dict:
    owner_user = row["humanUserId"]
    members = [
        {
            "kind": "human",
            "id": owner_user,
            "displayName": await messaging.human_display_name(owner_user),
            "mentionHandle": None,
            "role": "owner",
            "listenMode": None,
        },
        *await _default_agents(owner_id),
    ]
    title = (row.get("displayTitle") or row.get("title") or row.get("cwdName") or "Session")[:TITLE_MAX]
    await messaging_repo.ensure_user_stream(owner_id, owner_user)
    try:
        return await messaging_repo.create_room(
            owner_id=owner_id,
            room_id=room_id,
            client_room_id=f"session-room-v1:{row['sessionId']}",
            creator_user_id=owner_user,
            # The agent's turn carries the room name, so "Session chat" is how it knows where it is.
            name=f"Session chat · {title}",
            kind="channel",
            members=members,
            req_hash=messaging_repo.request_hash("session-room-v1", row["sessionId"]),
            extra={"hidden": True, "session": {"sessionId": row["sessionId"], "ownerId": owner_user}},
        )
    except messaging_repo.MessagingRepoError:
        # A concurrent first open created it (the META put cancels): use theirs.
        meta = await messaging_repo.get_room(room_id)
        if meta is None:
            raise
        return meta


async def _set_archived(meta: dict, archived: bool) -> dict:
    updates = (
        {"archivedAt": messaging.utc_now().isoformat(), "archivedBy": SYSTEM_ACTOR_ID}
        if archived
        else {"archivedAt": None, "archivedBy": None}
    )
    try:
        meta = await messaging_repo.patch_room_meta(meta["roomId"], updates)
    except ClientError as exc:
        if not messaging_repo._is_cancelled(exc):
            raise
        return await messaging_repo.get_room(meta["roomId"]) or meta  # another writer moved it first
    await messaging._publish_room_metadata(meta)
    return meta


async def _follow_lifecycle(meta: dict, row: dict) -> dict:
    """Ended sessions read only; a resumed one (same id, endedAt cleared) takes posts again."""
    ended = row.get("endedAt") is not None
    if ended != bool(meta.get("archivedAt")):
        return await _set_archived(meta, ended)
    return meta


async def _seat(owner_id: str, meta: dict, user_id: str) -> dict:
    """Seat one reader as a plain member. A lost revision race re-reads and retries."""
    await messaging_repo.ensure_user_stream(owner_id, user_id)
    member = {
        "kind": "human",
        "id": user_id,
        "displayName": await messaging.human_display_name(user_id),
        "mentionHandle": None,
        "role": "member",
        "listenMode": None,
    }
    for attempt in range(3):
        try:
            meta = await messaging_repo.upsert_member(room_id=meta["roomId"], owner_id=owner_id, member=member)
        except messaging_repo.MemberLimitExceeded as exc:
            # ponytail: MAX_ROOM_MEMBERS (32) bounds a session's chat, as it bounds the vendor room.
            put_metric("messaging.session_room.capped")
            logger.warning("session chat full; %s not seated in %s", user_id, meta["roomId"])
            raise ApiProblem("MEMBER_LIMIT", "This session's chat is full.") from exc
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc) or attempt == 2:
                raise
            meta = await messaging_repo.get_room(meta["roomId"]) or meta
            if await messaging_repo.get_member(meta["roomId"], "human", user_id) is not None:
                return meta
            continue
        await messaging._publish_room_metadata(meta)
        return meta
    return meta  # pragma: no cover -- the loop returns or raises


async def open_chat(auth: AuthContext, session_id: str) -> RoomDetail:
    """The chat door: whoever can read the session gets its room, created on the first open."""
    row, _ = await local_sessions.authorize_session(auth, None, session_id)
    owner_id = resolve_owner_id(auth)
    room_id = session_room_id(owner_id, row["sessionId"])
    meta = await messaging_repo.get_room(room_id) or await _create(owner_id, room_id, row)
    meta = await _follow_lifecycle(meta, row)
    if await messaging_repo.get_member(room_id, "human", auth.user_id) is None:
        meta = await _seat(owner_id, meta, auth.user_id)
    return await messaging._room_detail(meta, auth.user_id)
```

- [ ] **Step 5: Add the route** — in `routers/messaging.py`, add `session_chat,` to the `from core.services import (...)` list (`:32-41`, alphabetical after `session_resume,`), and directly after `type_into_local_session` (`:250-255`):

```python
@router.post("/messaging/sessions/{session_id}/chat", response_model=RoomDetail)
async def open_local_session_chat(session_id: str, auth: AuthContext = Depends(get_human_session)):
    """The session's chat room, beside its terminal: created on first open, the reader seated.
    People only (a Clerk session); an unshared session has no chat for anyone but its owner."""
    return await session_chat.open_chat(auth, session_id)
```

- [ ] **Step 6: Run the tests**

Run: `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/services/test_session_chat.py tests/unit/services/test_messaging_default_room_flag.py tests/unit/services/test_messaging_shared_rooms.py -q --no-cov`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/session_chat.py apps/backend/core/services/messaging.py apps/backend/routers/messaging.py apps/backend/tests/unit/services/test_session_chat.py
git commit -m "feat(sessions): the session chat door — one hidden room per session, seated by read access"
```

---

### Task 2: Lifecycle hooks, guards and the session's chat fields

**Files:**
- Modify: `apps/backend/core/services/session_chat.py` (append)
- Modify: `apps/backend/core/services/messaging.py` — `_deny_vendor_human_change` `:1778-1780`; `patch_room` `:2169-2200`; `delete_room` guard `:2409`; `revoke_human_membership` `:2427-2439`; `create_agent_message` hidden redirect `:4186`
- Modify: `apps/backend/core/services/local_sessions.py` — `LocalSession` `:236-334` (fields + `_drop_none` tuple `:313-332`); `list_workspace_sessions` `:1065-1088`; `set_sharing` `:1154-1186`; `get_session` `:1189-1193`
- Modify: `apps/backend/core/services/local_session_relay.py` — `_ended` `:822-848`
- Test: `apps/backend/tests/unit/services/test_session_chat.py` (append); `apps/backend/tests/unit/services/test_session_chat_lifecycle.py` (new, Moto)

**Interfaces:**
- Consumes: Task 1's `session_room_id`, `_set_archived`, `is_session_room`.
- Produces: `session_chat.silence(owner_id: str, session_id: str, *, keep: str | None) -> int`; `session_chat.archive_for_session(owner_id: str, session_id: str) -> None`; `session_chat.chat_state(owner_id: str, viewer: str, session_ids: list[str]) -> dict[str, tuple[str, int]]`; `session_chat.with_chat(sessions: list[LocalSession], state) -> list[LocalSession]`; `LocalSession.chat_room_id: str | None`, `LocalSession.chat_unread: int | None` (wire `chatRoomId`, `chatUnread`, omitted when None).

- [ ] **Step 1: Append guard tests** to `tests/unit/services/test_session_chat.py`:

```python
@pytest.mark.asyncio
async def test_people_in_a_session_chat_follow_the_session_never_a_hand(monkeypatch):
    with pytest.raises(ApiProblem):
        messaging._deny_vendor_human_change(META, "human")
    messaging._deny_vendor_human_change(META, "agent")  # the owner may add agents
    monkeypatch.setattr(messaging, "require_room_member", AsyncMock(return_value=(META, {"role": "owner"})))
    for archived in (True, False):
        with pytest.raises(ApiProblem) as refused:
            await messaging.patch_room(ALICE, ROOM, messaging.PatchRoomRequest(archived=archived))
        assert refused.value.error.code == "ROOM_NOT_ARCHIVABLE"
    with pytest.raises(ApiProblem) as refused:
        await messaging.delete_room(ALICE, ROOM)
    assert refused.value.error.code == "ROOM_NOT_DELETABLE"


@pytest.mark.asyncio
async def test_an_agents_unbound_post_stays_in_the_session_chat(monkeypatch):
    from core.services import meeting_invites

    output_room = AsyncMock(return_value="general")
    monkeypatch.setattr(meeting_invites, "output_room", output_room)
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=META))
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
    request = messaging.AgentMessageRequest(
        event_id="evt-1", room_id=ROOM, agent_id=CEO_ID, text="hello", reply_to_message_id=None, thread_root_message_id=None
    )
    with pytest.raises(ApiProblem) as refused:
        await messaging.create_agent_message(OWNER, request)
    assert refused.value.error.code == "MEMBER_NOT_FOUND"  # judged in the session room itself
    output_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_silence_removes_every_human_but_the_kept_one_with_rotation(monkeypatch):
    members = [{"kind": "human", "id": ALICE.user_id}, {"kind": "human", "id": BOB.user_id}, {"kind": "agent", "id": CEO_ID}]
    monkeypatch.setattr(session_chat.messaging_repo, "list_members", AsyncMock(return_value=members))
    monkeypatch.setattr(session_chat.messaging_repo, "get_room", AsyncMock(return_value=META))
    remove = AsyncMock(return_value=META)
    monkeypatch.setattr(messaging, "_remove_member_with_notices", remove)
    assert await session_chat.silence(OWNER, SID, keep=ALICE.user_id) == 1
    remove.assert_awaited_once_with(META, "human", BOB.user_id)
    remove.reset_mock()
    assert await session_chat.silence(OWNER, SID, keep=None) == 2
```

- [ ] **Step 2: Create the Moto lifecycle test** `tests/unit/services/test_session_chat_lifecycle.py`:

```python
"""Session chat follows the session (spec §2.7, §5) on real Moto rows."""

import base64
from unittest.mock import AsyncMock

import pytest

from core.repositories import messaging_repo
from core.services import local_sessions as service, messaging, session_chat, session_screen
from core.services.messaging import ApiProblem
from tests.unit.services import test_local_session_relay as relay_tests, test_local_sessions as registry_tests
from tests.unit.services.test_local_session_relay import fixture, relay, start
from tests.unit.services.test_local_sessions import ALICE, BOB

db = registry_tests.db
transport = relay_tests.transport


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(session_chat, "_default_agents", AsyncMock(return_value=[]))
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(side_effect=lambda uid: uid))
    monkeypatch.setattr(messaging, "human_emails", AsyncMock(return_value={}))


@pytest.mark.asyncio
async def test_chat_seats_readers_without_admitting_them_and_follows_sharing_and_end(transport):
    sid = await start(transport)
    room = str((await session_chat.open_chat(BOB, sid)).room_id)
    assert {m.id for m in (await session_chat.open_chat(ALICE, sid)).members} == {ALICE.user_id, BOB.user_id}
    # A chat seat is not a control grant: the terminal's input door still refuses Bob.
    keys = session_screen.SessionInput(data=base64.b64encode(b"ls\r").decode(), epoch=0)
    with pytest.raises(ApiProblem) as refused:
        await session_screen.type_into(BOB, sid, keys)
    assert refused.value.error.code == "LOCAL_SESSION_FORBIDDEN"
    seen = await service.get_session(BOB, sid)
    assert (seen.chat_room_id, seen.chat_unread) == (room, 0)
    # Pausing sharing empties the chat of everyone but its owner, and Bob cannot reopen it.
    await service.set_sharing(ALICE, sid, False)
    assert await messaging_repo.get_member(room, "human", BOB.user_id) is None
    assert await messaging_repo.get_member(room, "human", ALICE.user_id) is not None
    with pytest.raises(ApiProblem):
        await session_chat.open_chat(BOB, sid)
    # Shared again: Bob is seated again on his next open.
    await service.set_sharing(ALICE, sid, True)
    await session_chat.open_chat(BOB, sid)
    assert await messaging_repo.get_member(room, "human", BOB.user_id) is not None
    # The session ends: its chat is archived and stays readable.
    ended = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 10005}
    await relay(transport).handle_message("conn-1", ended)
    assert (await messaging_repo.get_room(room)).get("archivedAt")
```

- [ ] **Step 3: Run to verify they fail**

Run: `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/services/test_session_chat.py tests/unit/services/test_session_chat_lifecycle.py -q --no-cov`
Expected: FAIL — `AttributeError: module 'core.services.session_chat' has no attribute 'silence'`, the guard test's `patch_room` archives instead of refusing, and `chat_room_id` is not a `LocalSession` field.

- [ ] **Step 4: Append to `session_chat.py`**

```python
async def silence(owner_id: str, session_id: str, *, keep: str | None) -> int:
    """Unshare or revoke: every human but ``keep`` leaves the chat, each with a delivery-key
    rotation, so nobody who lost the session keeps hearing it. Idempotent; returns how many left."""
    room_id = session_room_id(owner_id, session_id)
    removed = 0
    for member in await messaging_repo.list_members(room_id):
        if member.get("kind") != "human" or member["id"] == keep:
            continue
        for attempt in range(3):
            meta = await messaging_repo.get_room(room_id)
            if meta is None:
                return removed
            try:
                await messaging._remove_member_with_notices(meta, "human", member["id"])
                break
            except ClientError as exc:
                if not messaging_repo._is_cancelled(exc) or attempt == 2:
                    raise
        removed += 1
    return removed


async def archive_for_session(owner_id: str, session_id: str) -> None:
    meta = await messaging_repo.get_room(session_room_id(owner_id, session_id))
    if meta is not None and not meta.get("archivedAt"):
        await _set_archived(meta, True)


async def chat_state(owner_id: str, viewer: str, session_ids: list[str]) -> dict[str, tuple[str, int]]:
    """``{sessionId: (roomId, unread)}`` for the chats the viewer sits in: one batch read."""
    rooms = {sid: session_room_id(owner_id, sid) for sid in session_ids}
    keys: list[tuple[str, str]] = []
    for room_id in rooms.values():
        pk = messaging_repo.room_pk(room_id)
        keys += [(pk, "META"), (pk, messaging_repo.member_sk("human", viewer)), (pk, messaging_repo.read_sk(viewer))]
    rows = await messaging_repo.batch_get(keys) if keys else {}
    out: dict[str, tuple[str, int]] = {}
    for sid, room_id in rooms.items():
        pk = messaging_repo.room_pk(room_id)
        meta = rows.get((pk, "META"))
        member = rows.get((pk, messaging_repo.member_sk("human", viewer)))
        if meta and member and not meta.get("deletedAt"):
            out[sid] = (room_id, messaging_repo.unread_count(meta, member, rows.get((pk, messaging_repo.read_sk(viewer)))))
    return out


def with_chat(sessions: list, state: dict[str, tuple[str, int]]) -> list:
    return [
        s.model_copy(update={"chat_room_id": state[s.session_id][0], "chat_unread": state[s.session_id][1]})
        if s.session_id in state
        else s
        for s in sessions
    ]
```

- [ ] **Step 5: Guards in `core/services/messaging.py`**

Replace `_deny_vendor_human_change` (`:1778-1780`):

```python
def _deny_vendor_human_change(meta: dict, kind: str) -> None:
    # A session chat's people follow session read access (spec 2026-10-07 decision 2).
    if kind == "human" and (is_vendor_room(meta) or is_session_room(meta)):
        raise ApiProblem("ROOM_FORBIDDEN", "Membership of this channel is managed by Lightbulb.")
```

In `patch_room`, directly after the `if "name" in updates and is_vendor_room(meta):` block (`:2186-2187`):

```python
    if request.archived is not None and is_session_room(meta):
        raise ApiProblem("ROOM_NOT_ARCHIVABLE", "This chat opens and closes with its session.")
```

In `delete_room` (`:2409`) change the refusal condition to:

```python
        if meta.get("kind") != "channel" or is_default_room(meta) or is_vendor_room(meta) or is_session_room(meta):
```

In `revoke_human_membership` (`:2427-2439`) replace the loop's last line `await _remove_member_with_notices(meta, "human", user_id)` with:

```python
        await _remove_member_with_notices(meta, "human", user_id)
        if is_session_room(meta) and meta["session"].get("ownerId") == user_id:
            # Their session's chat goes silent for everyone: the person it was about is gone.
            from core.services import session_chat  # lazy: it imports this module

            await session_chat.silence(owner_id, meta["session"]["sessionId"], keep=None)
```

In `create_agent_message` (`:4186`) change `if meta.get("hidden") and not (bound or request.uploaded_attachments):` to:

```python
    if meta.get("hidden") and meta.get("meeting") and not (bound or request.uploaded_attachments):
```

and change the comment line above it from `# A Google Meet's room is read by nobody:` to `# A Google Meet's room (hidden, with a meeting record) is read by nobody; a session's chat is read, so it keeps its posts:`.

- [ ] **Step 6: `core/services/local_sessions.py`**

In `LocalSession`, after `input_paused: bool = False` (`:308`):

```python
    # The viewer's seat in this session's chat, once they are in it (spec 2026-10-07-session-chat-design §4.1).
    chat_room_id: str | None = None
    chat_unread: int | None = None
```

and add `"chat_room_id",` and `"chat_unread",` to the end of the `_drop_none` names tuple (after `"pending_pickup_ids",`).

In `set_sharing`, replace the loop's `return _session_public(row, auth.user_id)` (`:1185`) with:

```python
        if not shared:
            from core.services import session_chat  # lazy: it imports this module

            # On every pause, not only a change: a retry after a failed silence finishes it.
            await session_chat.silence(row["ownerId"], row["sessionId"], keep=row["humanUserId"])
        return _session_public(row, auth.user_id)
```

Replace `get_session`'s body (`:1192-1193`):

```python
    row, _ = await authorize_session(auth, None, session_id)
    viewer = _viewer(auth)
    public = _session_public(row, viewer)
    if viewer is None:
        return public
    from core.services import session_chat  # lazy: it imports this module

    return session_chat.with_chat([public], await session_chat.chat_state(row["ownerId"], viewer, [row["sessionId"]]))[0]
```

Replace `list_workspace_sessions`'s last two lines (`:1087-1088`):

```python
    live = [s for s in found if s.status != "ended"][:WORKSPACE_SESSION_CAP]
    sessions = live + [s for s in found if s.status == "ended"][:RECENT_ENDED_CAP]
    if viewer is not None and sessions:
        from core.services import session_chat  # lazy: it imports this module

        sessions = session_chat.with_chat(
            sessions, await session_chat.chat_state(owner, viewer, [s.session_id for s in sessions])
        )
    return WorkspaceSessions(sessions=sessions)
```

- [ ] **Step 7: Archive on end** — in `local_session_relay.py` `_ended`, after `_capture_lifecycle(updated, "local_session_ended", end_reason=message.reason)` (`:846`):

```python
        from core.services import session_chat  # lazy: it imports local_sessions

        try:
            await session_chat.archive_for_session(owner, row["sessionId"])
        except Exception:  # the next chat open archives it too (session_chat._follow_lifecycle)
            logger.warning("session chat archive failed for %s", row["sessionId"], exc_info=True)
```

- [ ] **Step 8: Run the tests**

Run: `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/services/test_session_chat.py tests/unit/services/test_session_chat_lifecycle.py tests/unit/services/test_local_sessions.py tests/unit/services/test_local_session_controls.py tests/unit/services/test_local_session_relay.py tests/unit/services/test_session_input.py tests/unit/services/test_messaging_room_delete.py tests/unit/services/test_messaging_shared_rooms.py tests/unit/services/test_meeting_invites.py tests/unit/routers/test_messaging_agent_posts.py tests/unit/routers/test_messaging_room_membership.py -q --no-cov`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add apps/backend/core/services/session_chat.py apps/backend/core/services/messaging.py apps/backend/core/services/local_sessions.py apps/backend/core/services/local_session_relay.py apps/backend/tests/unit/services/test_session_chat.py apps/backend/tests/unit/services/test_session_chat_lifecycle.py
git commit -m "feat(sessions): session chat follows sharing, revocation and the session's end"
```

---

### Task 3: Shared types, client door and the person palette

**Files:**
- Modify: `packages/shared/src/messaging/types.ts` (`RoomSummary` `:33-81`; `LocalSession` near `inputPaused` `:412`)
- Modify: `packages/shared/src/messaging/client.ts` (interface near `getLocalSession` `:80`; impl near `:164`)
- Modify: `packages/shared/src/messaging/useLocalSessionHistory.ts:10-11` (`SessionSource.client`)
- Create: `packages/shared/src/messaging/personColor.ts`, `personColor.test.ts`
- Modify: `packages/shared/src/index.ts` (next to the `createMessagingClient` export `:57`)
- Test: `packages/shared/src/messaging/client.test.ts`, `packages/shared/src/messaging/useRooms.test.tsx` (append)

**Interfaces:**
- Produces: `RoomSummary.session?: { sessionId: string } | null`; `LocalSession.chatRoomId?: string`, `LocalSession.chatUnread?: number`; `MessagingClient.openSessionChat(sessionId: string): Promise<RoomDetail>`; `SessionSource["client"].openSessionChat?`; `PERSON_COLORS`; `type PersonColor = (typeof PERSON_COLORS)[number] | "owner"`; `personColor(userId: string, sessionOwnerId: string): PersonColor`.

- [ ] **Step 1: Write the failing tests**

`packages/shared/src/messaging/personColor.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { PERSON_COLORS, personColor } from "./personColor";

describe("personColor", () => {
  it("matches the terminal mod's colorFor (rules.ts) for the same Clerk ids", () => {
    // Values computed with apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/rules.ts colorFor.
    expect(personColor("user_alice", "owner")).toBe("magenta");
    expect(personColor("user_bob", "owner")).toBe("green");
    expect(personColor("user_carol", "owner")).toBe("cyan");
    expect(personColor("user_dave", "owner")).toBe("blueBright");
    expect(personColor("user_erin", "owner")).toBe("yellow");
    expect(personColor("user_2abcDEF", "owner")).toBe("blue");
    expect(PERSON_COLORS).toEqual(["cyan", "magenta", "green", "yellow", "blue", "blueBright"]);
  });
  it("gives the session's own person the owner colour", () => {
    expect(personColor("user_alice", "user_alice")).toBe("owner");
  });
});
```

Append inside `describe("messaging HTTP client", ...)` in `client.test.ts`:

```ts
  it("opens a session's chat through its own door", async () => {
    const { client, methods } = setup();
    await client.openSessionChat("sess 1");
    expect(methods.post).toHaveBeenCalledWith("/messaging/sessions/sess%201/chat", {});
  });
```

Append inside the top-level `describe` in `useRooms.test.tsx` (after the `autoSelect` test, `:126`):

```tsx
  it("opens a room the list does not carry (a hidden session chat) through its detail", async () => {
    messaging.listRooms.mockResolvedValue({ rooms: [], sections: [] });
    const { result } = renderHook(() => useRooms(platform(), { autoSelect: false }), { wrapper });
    act(() => result.current.selectRoom(room.roomId));
    await waitFor(() => expect(result.current.activeRoom?.roomId).toBe(room.roomId));
    expect(messaging.getRoom).toHaveBeenCalledWith(room.roomId);
    await waitFor(() => expect(result.current.messages.map((m) => m.messageId)).toEqual(["msg-1"]));
    expect(live.claimActiveRoomSubscription).toHaveBeenCalledWith(expect.objectContaining({ roomId: room.roomId }));
  });
```

- [ ] **Step 2: Run to verify they fail**

Run (from `packages/shared`): `pnpm vitest run src/messaging/personColor.test.ts src/messaging/client.test.ts src/messaging/useRooms.test.tsx`
Expected: personColor import fails; `client.openSessionChat is not a function`; the useRooms test passes already (it proves the hidden room loads; keep it as the regression pin).

- [ ] **Step 3: Implement**

`packages/shared/src/messaging/personColor.ts`:

```ts
/**
 * One colour per person, the same in the terminal and in the session's chat. A port of the desktop
 * mod's `colorFor` (apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/rules.ts): FNV-1a over the
 * Clerk user id, mod the same ANSI palette in the same order. The session's own person is the mod's
 * OWNER_COLOR (gray), here "owner". Keep the two in step.
 */
export const PERSON_COLORS = ["cyan", "magenta", "green", "yellow", "blue", "blueBright"] as const;
export type PersonColor = (typeof PERSON_COLORS)[number] | "owner";

export function personColor(userId: string, sessionOwnerId: string): PersonColor {
  if (userId === sessionOwnerId) return "owner";
  let h = 0x811c9dc5;
  for (let i = 0; i < userId.length; i++) h = Math.imul(h ^ userId.charCodeAt(i), 0x01000193);
  return PERSON_COLORS[(h >>> 0) % PERSON_COLORS.length];
}
```

`index.ts`, after `export { createMessagingClient } from "./messaging/client";`:

```ts
export { PERSON_COLORS, personColor } from "./messaging/personColor";
export type { PersonColor } from "./messaging/personColor";
```

`types.ts` — at the end of `RoomSummary` (after `deletedAt?`):

```ts
  /** A session's chat (hidden from the room list): the session it talks beside. Absent on every other room. */
  session?: { sessionId: string } | null;
```

and in `LocalSession`, after `inputPaused?: boolean;`:

```ts
  /** The viewer's seat in this session's chat, once they are in it. */
  chatRoomId?: string;
  /** Top-level chat messages above the viewer's read point, capped at 99. */
  chatUnread?: number;
```

`client.ts` — interface, after `getLocalSession(...)`:

```ts
  /** The session's chat room: created on the first open, the caller seated (people only). */
  openSessionChat(sessionId: string): Promise<RoomDetail>;
```

implementation, after the `getLocalSession:` line:

```ts
    openSessionChat: (sessionId) => api.post(`${sessionPath(sessionId)}/chat`, {}) as Promise<RoomDetail>,
```

`useLocalSessionHistory.ts:11` — extend the `Partial<Pick<...>>` key list with `| "openSessionChat"`.

- [ ] **Step 4: Run tests and typecheck**

Run (from `packages/shared`): `pnpm vitest run src/messaging/personColor.test.ts src/messaging/client.test.ts src/messaging/useRooms.test.tsx && pnpm exec tsc --noEmit && pnpm lint`
Expected: PASS, no type or lint errors.

- [ ] **Step 5: Commit**

```bash
git add packages/shared/src
git commit -m "feat(shared): session chat door, room session ref, and the terminal's person palette"
```

---

### Task 4: Web chat column (component, person colour, CSS)

**Files:**
- Modify: `apps/frontend/src/hooks/useRooms.ts:12` and `:48-51` (4th param `autoSelect`)
- Modify: `apps/frontend/src/components/messaging/RoomMessageItem.tsx` (props `:24-59`, signature `:76`, `<article>` `:94`)
- Create: `apps/frontend/src/components/messaging/SessionChat.tsx`
- Modify: `apps/frontend/src/components/messaging/messaging.css` (append only, end of file)
- Modify: `apps/frontend/src/app/globals.css` (tokens after `--on-agent-tint` at `:196` and `:222`)
- Modify: `apps/frontend/src/__tests__/chatThemeContrast.test.ts:19` (`TEXT` list)
- Test: `apps/frontend/src/components/messaging/__tests__/SessionChat.test.tsx` (new)

**Interfaces:**
- Consumes: Task 3's `personColor`, `RoomDetail`, `openSessionChat`.
- Produces: `useRooms(initialAgentId?, subscribeToRoom = true, roomVisible = true, autoSelect = true)`; `RoomMessageItem` prop `personColor?: string` → `data-person-color`; `SessionChat({ sessionId, sessionOwnerId, openChat, onClose, onRoom? })` rendering `<aside className="session-chat" aria-label="Session chat">`; CSS classes `.session-chat`, `.session-chat-open` (on `.room-session-focus`), `.session-chat-unread`; tokens `--person-cyan|magenta|green|yellow|blue|blue-bright`.

- [ ] **Step 1: Write the failing test** `src/components/messaging/__tests__/SessionChat.test.tsx`:

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { ApiError, type CanonicalRoomMessage, type RoomDetail } from "@lightbulb/shared";

vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "user_alice" }) }));
const rooms = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
vi.mock("@/hooks/useRooms", () => ({ useRooms: (...args: unknown[]) => { rooms.value.args = args; return rooms.value; } }));

import { SessionChat } from "../SessionChat";

const member = (id: string, displayName: string, role = "member") =>
  ({ kind: "human" as const, id, displayName, mentionHandle: null, role, listenMode: null, joinedAt: "t", joinedSeq: 0 });
const detail: RoomDetail = {
  roomId: "chat-1", name: "Session chat · Fix relay", kind: "channel", roomRevision: 1, deliveryKey: "k",
  lastMessageSeq: 1, lastMessageAt: null, unreadCount: 0, updatedAt: "t", topic: null, starred: false, muted: false,
  sectionId: null, lastReadSeq: 0, createdAt: "t", pinnedMessageIds: [], session: { sessionId: "s1" },
  members: [member("user_alice", "Alice", "owner"), member("user_bob", "Bob")],
};
const fromBob: CanonicalRoomMessage = {
  messageId: "m1", roomId: "chat-1", messageSeq: 1, roomRevision: 1, author: { kind: "human", id: "user_bob", displayName: "Bob" },
  text: "is it the relay?", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-10-07T10:00:00Z", runId: null,
};
const selectRoom = vi.fn();
beforeEach(() => {
  selectRoom.mockClear();
  rooms.value = {
    selectRoom, sendMessage: vi.fn(), activeRoomId: "chat-1", activeRoom: detail, messages: [fromBob],
    optimisticMessages: [], historyComplete: true, historyLoading: false, historyErrorMessage: null,
    loadOlder: vi.fn(), sendState: () => "idle", reconcileSend: vi.fn(), httpReady: true,
  };
});

it("opens the session's own room in a second slot and colours people as the terminal does", async () => {
  const onRoom = vi.fn();
  const view = render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" openChat={vi.fn(async () => detail)} onClose={vi.fn()} onRoom={onRoom} />);
  await waitFor(() => expect(selectRoom).toHaveBeenCalledWith("chat-1"));
  expect(rooms.value.args).toEqual([null, true, true, false]); // its own slot: never auto-selects a listed room
  expect(onRoom).toHaveBeenCalledWith("chat-1");
  expect(screen.getByRole("complementary", { name: "Session chat" })).toBeInTheDocument();
  expect(document.querySelector('[data-person-color="green"]')).toHaveTextContent("is it the relay?");
  expect(screen.getByLabelText("Message the people here")).toBeInTheDocument();
  view.unmount();
  expect(onRoom).toHaveBeenLastCalledWith(null);
});

it("reads an ended session's chat without a composer", async () => {
  rooms.value.activeRoom = { ...detail, archivedAt: "2026-10-07T11:00:00Z" };
  render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" openChat={vi.fn(async () => detail)} onClose={vi.fn()} />);
  expect(await screen.findByText("This session ended. Its chat is read-only.")).toBeInTheDocument();
  expect(screen.queryByLabelText("Message the people here")).not.toBeInTheDocument();
});

it("says when the chat is full or unavailable", async () => {
  const { unmount } = render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" openChat={vi.fn(async () => { throw new ApiError(409, null); })} onClose={vi.fn()} />);
  expect(await screen.findByText("This chat is full.")).toBeInTheDocument();
  unmount();
  render(<SessionChat sessionId="s1" sessionOwnerId="user_alice" openChat={vi.fn(async () => { throw new ApiError(404, null); })} onClose={vi.fn()} />);
  expect(await screen.findByText("Chat is not available for this session.")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run to verify it fails**

Run (from `apps/frontend`): `pnpm vitest run src/components/messaging/__tests__/SessionChat.test.tsx`
Expected: FAIL — `Failed to resolve import "../SessionChat"`.

- [ ] **Step 3: Implement**

`hooks/useRooms.ts` — signature `export function useRooms(initialAgentId?: string | null, subscribeToRoom = true, roomVisible = true, autoSelect = true)` and the options literal `{ initialAgentId, subscribeToRoom, autoSelect, boundedHistory: true }`.

`RoomMessageItem.tsx` — add to `RoomMessageItemProps` after `menuExtras?: ReactNode;`:

```ts
  /** A person's colour in a session's chat (`personColor`), the same as in its terminal. */
  personColor?: string;
```

add `personColor` to the destructured parameter list (after `menuExtras`), and add `data-person-color={personColor}` to the `<article ...>` at `:94` (after `aria-label=...`).

`components/messaging/SessionChat.tsx`:

```tsx
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { useAuth } from "@clerk/nextjs";
import { ApiError, personColor, type CreateMessageRequest, type RoomDetail } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { useRooms } from "@/hooks/useRooms";
import { roomUrlTransform } from "./markdown";
import { roomMarkdown } from "./roomMarkdown";
import { RoomComposer } from "./RoomComposer";
import { RoomMessageItem } from "./RoomMessageItem";
import { RoomTimeline } from "./RoomTimeline";
import type { RoomMessage, RoomParticipant } from "./types";
import "./messaging.css";

const NO_REACTIONS: readonly string[] = [];

/**
 * The session's chat (spec 2026-10-07-session-chat-design §4.2): people talk beside the terminal,
 * never into it. Its own room slot (subscription, scroll, draft), so a terminal reset never touches
 * the draft. It posts only through the room message door; a chat seat never lets anyone type into
 * the session. Threads and reactions are left out: replies render inline.
 */
export function SessionChat({ sessionId, sessionOwnerId, openChat, onClose, onRoom }: {
  sessionId: string;
  /** The session's own person: their name takes the owner colour, as in the terminal. */
  sessionOwnerId: string;
  openChat: (sessionId: string) => Promise<RoomDetail>;
  onClose: () => void;
  /** The chat room on screen, or null when the column closes (its notifications are not toasted while shown). */
  onRoom?: (roomId: string | null) => void;
}) {
  const { userId } = useAuth();
  const rooms = useRooms(null, true, true, false);
  const { selectRoom, sendMessage } = rooms;
  const [roomId, setRoomId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    openChat(sessionId).then((room) => {
      if (!alive) return;
      setRoomId(room.roomId);
      selectRoom(room.roomId);
      onRoom?.(room.roomId);
    }, (e: unknown) => {
      if (alive) setError(e instanceof ApiError && e.status === 409 ? "This chat is full." : "Chat is not available for this session.");
    });
    return () => { alive = false; onRoom?.(null); };
  }, [sessionId, openChat, selectRoom, onRoom]);

  const [text, setText] = useState("");
  const [clientMessageId, setClientMessageId] = useState(() => crypto.randomUUID());
  const textRef = useRef(text);
  useEffect(() => { textRef.current = text; }, [text]);
  const send = useCallback(async (request: CreateMessageRequest) => {
    // The box empties now; a failed send gives its text back if the box is still empty.
    setText("");
    setClientMessageId(crypto.randomUUID());
    try {
      await sendMessage(request);
    } catch (e) {
      if (!textRef.current.trim()) {
        setText(request.text);
        setClientMessageId(request.clientMessageId);
      }
      throw e;
    }
  }, [sendMessage]);

  const room = roomId && rooms.activeRoom?.roomId === roomId ? rooms.activeRoom : undefined;
  const members = (room?.members ?? []) as RoomParticipant[];
  const messages = roomId && rooms.activeRoomId === roomId ? rooms.messages : [];
  const archived = Boolean(room?.archivedAt);
  const me = userId ?? "";
  const renderMessage = (message: RoomMessage, collapsed: boolean) => <RoomMessageItem key={message.messageId}
    id={`session-chat-${message.messageId}`} message={message} text={message.text}
    participant={members.find((m) => m.kind === message.author.kind && m.id === message.author.id)}
    currentUserId={me} actionable={!archived} inThread={false} collapsed={collapsed} replyCount={0} highlighted={false}
    mentionsViewer={message.mentions.some((m) => m.kind === "human" && m.id === me)}
    markdown={roomMarkdown} urlTransform={roomUrlTransform} quickReactions={NO_REACTIONS} reactionPending={false} reactionError={false}
    personColor={message.author.kind === "human" ? personColor(message.author.id, sessionOwnerId) : undefined} />;

  return <aside className="session-chat" aria-label="Session chat">
    <header className="room-context-header">
      <h3 className="font-semibold">Chat</h3>
      <Button className="ml-auto" variant="ghost" size="icon-sm" aria-label="Close chat" onClick={onClose}><X aria-hidden="true" /></Button>
    </header>
    {error ? <p role="status" className="p-4 text-sm text-muted-foreground">{error}</p> : <>
      <RoomTimeline roomId={roomId ?? "opening"} messages={messages} readSeq={null} ownLastSentSeq={null}
        renderMessage={renderMessage} historyComplete={rooms.historyComplete} historyLoading={rooms.historyLoading}
        historyError={rooms.historyErrorMessage} onLoadOlder={() => void rooms.loadOlder()}
        empty={<p className="room-empty">{roomId ? "Talk about the session here. The terminal stays the session's." : "Opening chat…"}</p>}>
        {rooms.optimisticMessages.filter((m) => m.roomId === roomId).map((m) =>
          <p key={m.clientMessageId} className="room-message opacity-60">{m.text}</p>)}
      </RoomTimeline>
      {archived
        ? <p role="status" className="room-composer room-archived text-sm text-muted-foreground">This session ended. Its chat is read-only.</p>
        : roomId && <RoomComposer text={text} onTextChange={setText} members={members} clientMessageId={clientMessageId}
          sendState={rooms.sendState(clientMessageId)} onSubmit={send} onRetry={send}
          onReconcile={(id) => rooms.reconcileSend(id)} disabled={!rooms.httpReady} label="Message the people here" roomKind="channel" />}
    </>}
  </aside>;
}
```

`globals.css` — in the first `:root[data-chat-theme] {` block, after `--on-agent-tint: #FFFFFF;`:

```css
  /* People in a session's chat: the terminal mod's ANSI palette (rules.ts PALETTE), AA on every plane. */
  --person-cyan: #0B6373;
  --person-magenta: #8E1F9A;
  --person-green: #1E6B35;
  --person-yellow: #7A5A00;
  --person-blue: #2148B8;
  --person-blue-bright: #4B3BC4;
```

and in `:root[data-chat-theme="dark"] {`, after `--on-agent-tint: #101012;`:

```css
  --person-cyan: #67D4E4;
  --person-magenta: #E39AF0;
  --person-green: #7FD69A;
  --person-yellow: #E3C35A;
  --person-blue: #93B4FA;
  --person-blue-bright: #B3A8FA;
```

`chatThemeContrast.test.ts:19` — extend `TEXT` with `"--person-cyan", "--person-magenta", "--person-green", "--person-yellow", "--person-blue", "--person-blue-bright"` (worst-plane ratios computed while planning: light ≥ 4.74, dark ≥ 6.37).

Append to the end of `messaging.css`:

```css
/* Session chat (2026-10-07-session-chat): people talk beside the terminal. New classes only. */
.session-chat { display: flex; flex-direction: column; min-width: 0; min-height: 0; border-left: 1px solid var(--border); background: var(--background); }
.session-chat .room-composer { margin: 8px 12px 16px; }
.session-chat-unread { min-width: 18px; padding: 0 5px; border-radius: 9px; background: var(--link); color: var(--on-link); font-size: .75rem; line-height: 18px; text-align: center; }
.session-chat .room-message[data-person-color] .room-message-byline > :first-child,
.session-chat .room-message[data-person-color] .room-message-byline > :first-child * { color: var(--person-color); }
.session-chat .room-message[data-person-color="owner"] { --person-color: var(--fg-soft); }
.session-chat .room-message[data-person-color="cyan"] { --person-color: var(--person-cyan); }
.session-chat .room-message[data-person-color="magenta"] { --person-color: var(--person-magenta); }
.session-chat .room-message[data-person-color="green"] { --person-color: var(--person-green); }
.session-chat .room-message[data-person-color="yellow"] { --person-color: var(--person-yellow); }
.session-chat .room-message[data-person-color="blue"] { --person-color: var(--person-blue); }
.session-chat .room-message[data-person-color="blueBright"] { --person-color: var(--person-blue-bright); }
@media (min-width: 1180px) {
  .room-session-focus.session-chat-open { grid-template-columns: minmax(0, 1fr) 344px; }
}
@media (min-width: 860px) and (max-width: 1179px) {
  .room-session-focus.session-chat-open > .session-chat {
    position: absolute; z-index: 3; inset: 0 0 0 auto; width: 344px;
    box-shadow: -16px 0 32px color-mix(in srgb, var(--midnight) 12%, transparent);
  }
}
@media (max-width: 859px) {
  /* The chat takes the screen, like a pushed page; the terminal stays mounted underneath. */
  .room-session-focus.session-chat-open > .room-session-main { display: none; }
  .room-session-focus.session-chat-open > .session-chat { border-left: 0; }
}
```

(`.room-session-focus` is `position: relative` through `.room-panel`, `messaging.css:12`, so the 860–1179 overlay anchors to it. The terminal never resizes the Mac — `SessionTerminalView.tsx:23,85-88` scales only and skips a zero-width box.)

- [ ] **Step 4: Run tests, types, lint**

Run (from `apps/frontend`): `pnpm vitest run src/components/messaging/__tests__/SessionChat.test.tsx src/__tests__/chatThemeContrast.test.ts src/__tests__/brandTokens.test.ts && pnpm exec tsc --noEmit && pnpm lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src
git commit -m "feat(web): session chat column — its own room slot, coloured like the terminal"
```

---

### Task 5: Web wiring — toggle, unread, subscription hand-off, notification routing

**Files:**
- Modify: `apps/frontend/src/components/messaging/SessionFocus.tsx` (imports `:3-11`; `SessionFocusTarget` `:14`; props `:25-33`; header `:110-114`; section `:108`, `:126`)
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspace.tsx:52-56`
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx` (interface `:29-52`; `:169-183`; `applySession`/`openSession` `:670-685`; `openNotificationTarget` `:875-883`; value `:898-914`; `MessageNotifications` `:915-917`)
- Modify: `apps/frontend/src/components/messaging/SessionListRow.tsx:15-27`
- Test: `__tests__/SessionFocus.test.tsx`, `__tests__/MessagingWorkspaceContext.test.tsx` (append; one existing test updated)

**Interfaces:**
- Consumes: Task 4's `SessionChat`; Task 3's `LocalSession.chatUnread/chatRoomId`, `RoomSummary.session`, `client.openSessionChat?`.
- Produces: `SessionFocusTarget.chat?: boolean`; `SessionFocus` props `chatOpen?: boolean`, `onChatOpenChange?: (open: boolean) => void`, `onChatRoom?: (roomId: string | null) => void`; context `openSession(sessionId: string, chat?: boolean)`, `setSessionChat(open: boolean)`, `setSessionChatRoomId(roomId: string | null)`.

- [ ] **Step 1: Write the failing tests**

Append to `SessionFocus.test.tsx` (it already mocks nothing for SessionChat; add this mock at the top of the file, after the imports):

```tsx
vi.mock("../SessionChat", () => ({ SessionChat: ({ onClose }: { onClose: () => void }) =>
  <aside aria-label="Session chat"><button onClick={onClose}>Close chat</button></aside> }));
```

and the tests:

```tsx
function chatFocus(chatOpen: boolean, chatUnread?: number) {
  const onChatOpenChange = vi.fn();
  const client = { listWorkspaceSessions: vi.fn(), getLocalSession: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })),
    submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), openSessionChat: vi.fn() } as unknown as SessionSource["client"];
  render(<SessionFocus focus={{ sessionId: "s1" }} entries={[{ session: { ...session, chatUnread, chatRoomId: "chat-1" }, ownerName: "Alice", own: false }]}
    loading={false} hintRevision={0} client={client} resolveSessionOwner={resolveSessionOwner} onAccessLost={vi.fn()} onClose={vi.fn()}
    chatOpen={chatOpen} onChatOpenChange={onChatOpenChange} />);
  return onChatOpenChange;
}

it("offers the session's chat with its unread count and opens it beside the terminal", async () => {
  const onChatOpenChange = chatFocus(false, 2);
  await userEvent.click(await screen.findByRole("button", { name: "Chat, 2 unread" }));
  expect(onChatOpenChange).toHaveBeenCalledWith(true);
  expect(screen.queryByRole("complementary", { name: "Session chat" })).not.toBeInTheDocument();
});

it("renders the chat column while open and closes it from either side", async () => {
  const onChatOpenChange = chatFocus(true, 2);
  expect(await screen.findByRole("complementary", { name: "Session chat" })).toBeInTheDocument();
  expect(document.querySelector(".room-session-focus.session-chat-open")).not.toBeNull();
  expect(screen.getByRole("button", { name: "Chat" })).toHaveAttribute("aria-pressed", "true"); // no count while reading
  await userEvent.click(screen.getByRole("button", { name: "Close chat" }));
  expect(onChatOpenChange).toHaveBeenCalledWith(false);
});
```

In `MessagingWorkspaceContext.test.tsx`: add `getRoom: vi.fn(async () => room)` to `state.resourceSource.client`, and replace the existing test `"opens the room a failed-run toast names"` (`:159-164`) with:

```tsx
it("opens the room a failed-run toast names", async () => {
  state.resourceSource.client.getRoom.mockResolvedValueOnce({ ...room, roomId: "room-9" });
  const view = compose();
  act(() => { window.dispatchEvent(new CustomEvent("openRoom", { detail: { roomId: "room-9", threadId: null } })); });
  await waitFor(() => expect(state.selectRoom).toHaveBeenCalledWith("room-9"));
  view.unmount();
});
```

and append:

```tsx
it("opens a session with its chat when a notification names the session's hidden room", async () => {
  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [] });
  state.resourceSource.client.getRoom.mockResolvedValueOnce({ ...room, roomId: "chat-1", kind: "channel", session: { sessionId: "s1" } });
  compose();
  await act(async () => {});
  act(() => notifications.onOpen("chat-1", null));
  await waitFor(() => expect(workspaceValue.sessionFocus).toEqual({ sessionId: "s1", chat: true }));
  expect(state.selectRoom).not.toHaveBeenCalledWith("chat-1");
});

it("lends the one live room subscription to the session chat while it is open", async () => {
  state.resourceSource.client.listWorkspaceSessions.mockResolvedValue({ sessions: [] });
  compose();
  await act(async () => {});
  act(() => workspaceValue.openSession("s1", true));
  expect(mocks.rooms.mock.calls.at(-1)?.[1]).toBe(false);
  act(() => workspaceValue.setSessionChat(false));
  expect(mocks.rooms.mock.calls.at(-1)?.[1]).toBe(true);
});
```

- [ ] **Step 2: Run to verify they fail**

Run (from `apps/frontend`): `pnpm vitest run src/components/messaging/__tests__/SessionFocus.test.tsx src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx`
Expected: FAIL — no "Chat, 2 unread" button; `workspaceValue.setSessionChat is not a function`; `sessionFocus` lacks `chat`.

- [ ] **Step 3: `SessionFocus.tsx`**

Imports: `import { MessageSquare, X } from "lucide-react";` and `import { SessionChat } from "./SessionChat";`.
`export interface SessionFocusTarget { sessionId: string | null; all?: boolean; chat?: boolean }`.
Add `chatOpen = false, onChatOpenChange, onChatRoom` to the destructured props and to the props type:

```ts
  /** The session's chat column is open (the context owns it: the main room lends it the live subscription). */
  chatOpen?: boolean;
  onChatOpenChange?: (open: boolean) => void;
  onChatRoom?: (roomId: string | null) => void;
```

Before `return <section ...` (`:108`):

```tsx
  // Chat beside the terminal (spec 2026-10-07-session-chat-design §3). Unread: the server's count from the
  // session list, plus live arrivals since; reading the chat zeroes both until the list refreshes.
  const openChat = client.openSessionChat;
  const [liveUnread, setLiveUnread] = useState(0);
  const [readThrough, setReadThrough] = useState<LocalSession | null>(null);
  const chatRoomId = session?.chatRoomId;
  useEffect(() => onEvent?.((event) => {
    if (event.type === "room.message.available" && event.payload.roomId === chatRoomId
      && !event.payload.threadRootMessageId && !event.payload.previewOnly) setLiveUnread((n) => n + 1);
  }), [onEvent, chatRoomId]);
  const toggleChat = () => {
    setLiveUnread(0);
    if (chatOpen) setReadThrough(session);
    onChatOpenChange?.(!chatOpen);
  };
  const unread = chatOpen ? 0 : (readThrough === session ? 0 : session?.chatUnread ?? 0) + liveUnread;
  const showChat = Boolean(session && openChat && onChatOpenChange);
```

Section and header:

```tsx
  return <section className={`room-panel room-session-focus${showChat && chatOpen ? " session-chat-open" : ""}`} aria-label="Shared session">
```

Replace the Close button line (`:113`) with:

```tsx
        {showChat && <Button className="ml-auto" variant="ghost" size="sm" aria-pressed={chatOpen}
          aria-label={unread ? `Chat, ${unread} unread` : "Chat"} onClick={toggleChat}>
          <MessageSquare aria-hidden="true" />Chat{unread > 0 && <span className="session-chat-unread" aria-hidden="true">{unread > 99 ? "99+" : unread}</span>}
        </Button>}
        <Button className={showChat ? undefined : "ml-auto"} variant="ghost" size="icon-sm" aria-label="Close session" onClick={onClose}><X aria-hidden="true" /></Button>
```

Before the closing `</section>` (`:126`), after `.room-session-main`'s closing `</div>`:

```tsx
    {showChat && chatOpen && session && openChat && <SessionChat key={session.sessionId} sessionId={session.sessionId}
      sessionOwnerId={session.humanUserId} openChat={openChat} onClose={toggleChat} onRoom={onChatRoom} />}
```

- [ ] **Step 4: `MessagingWorkspace.tsx`** — read `setSessionChat, setSessionChatRoomId` from `useMessagingWorkspace()` and add to the `<SessionFocus ...>` props (`:52-56`):

```tsx
      chatOpen={!!sessionFocus.chat} onChatOpenChange={setSessionChat} onChatRoom={setSessionChatRoomId}
```

- [ ] **Step 5: `MessagingWorkspaceContext.tsx`**

Interface (`:37`): `openSession: (sessionId: string, chat?: boolean) => void;` and add:

```ts
  /** Open or close the focused session's chat column. */
  setSessionChat: (open: boolean) => void;
  /** The chat room on screen beside a session (SessionChat reports it), or null. */
  setSessionChatRoomId: (roomId: string | null) => void;
```

Replace `:171-183` (`roomVisible` through the `useRooms(...)` call):

```tsx
  const roomVisible = subscribeToRoom && !sessionFocus;
  // The live provider carries ONE room channel (useLiveEvents.tsx:118-133, appsyncEvents.ts:115-145). While a
  // session's chat is open the covered room lends it that channel, and catches up when it gets it back.
  const sessionChatOpen = !!sessionFocus?.chat;
  const [sessionChatRoomId, setSessionChatRoomId] = useState<string | null>(null);
```

then keep the comment block and change the `useRooms` call's second argument to `(subscribeToRoom || observeRuns) && !sessionChatOpen`.

`applySession` (`:670`): signature `(sessionId: string, chat = false)`, body's `setSessionFocus({ sessionId });` → `setSessionFocus({ sessionId, chat });`, and above `heldLink.current = sessionId;` add `// ponytail: a held link drops the chat flag; carry it in heldLink if cold-load notification taps miss it.`. `openSession` (`:683`):

```tsx
  const openSession = useCallback((sessionId: string, chat = false) => {
    if (applySession(sessionId, chat)) bumpPick();
  }, [applySession, bumpPick]);
  const setSessionChat = useCallback((open: boolean) => {
    setSessionFocus((current) => current?.sessionId ? { ...current, chat: open } : current);
  }, []);
```

Rename the existing `openNotificationTarget` (`:875-883`) to `openRoomTarget` (body unchanged) and add after it:

```tsx
  const listedRoomIds = useMemo(() => new Set(rooms.rooms.map((room) => room.roomId)), [rooms.rooms]);
  const lookupRoom = rooms.resourceSource.client.getRoom;
  const openNotificationTarget = useCallback((roomId: string, targetThreadId: string | null, messageId?: string) => {
    if (listedRoomIds.has(roomId)) { openRoomTarget(roomId, targetThreadId, messageId); return; }
    // A session's chat is hidden from the room list: ask the room what it belongs to, and open the
    // session with its chat beside it rather than a bare room.
    void lookupRoom(roomId).then((room) => {
      if (room.session) { openSession(room.session.sessionId, true); activateMessagesPanel(); }
      else openRoomTarget(roomId, targetThreadId, messageId);
    }, () => openRoomTarget(roomId, targetThreadId, messageId));
  }, [listedRoomIds, lookupRoom, openRoomTarget, openSession]);
```

Add `setSessionChat,` and `setSessionChatRoomId,` to the provider value, and change the `MessageNotifications` props to `activeRoomId={sessionChatRoomId ?? rooms.activeRoomId}` and `roomVisible={sessionChatRoomId ? true : roomVisible}`.

- [ ] **Step 6: `SessionListRow.tsx`** — after `const news = ...` add `const chat = session.chatUnread ? `, ${session.chatUnread} unread in chat` : "";`, append `${chat}` to the button's `aria-label` template (after `${news}`), and after the `{unread && <span ...session-unread...>}` line add:

```tsx
      {!!session.chatUnread && <span className="session-chat-unread" aria-hidden="true">{session.chatUnread > 99 ? "99+" : session.chatUnread}</span>}
```

- [ ] **Step 7: Run tests, types, lint**

Run (from `apps/frontend`): `pnpm vitest run src/components/messaging/__tests__/SessionFocus.test.tsx src/components/messaging/__tests__/SessionFocus.retry.test.tsx src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx src/components/messaging/__tests__/SessionChat.test.tsx && pnpm exec tsc --noEmit && pnpm lint`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/frontend/src
git commit -m "feat(web): Chat toggle with unread beside the session; chat notifications open the session"
```

---

### Task 6: Phone — Chat button that pushes the room screen

**Files:**
- Modify: `apps/mobile/src/app/(app)/session/[sessionId].tsx` (hooks above early returns near `messageOwner` `:181-190`; `headerRight` `:214-226`; banner near `selectRoomError` `:264-271`; `styles`)
- Test: `apps/mobile/src/app/(app)/__tests__/session.test.tsx` (append; add `openSessionChat` to `mockLive.messaging` `:88-110`)

**Interfaces:**
- Consumes: Task 3's `client.openSessionChat`, `LocalSession.chatUnread`. The room route `/(app)/room/[roomId]` already loads an unlisted room through `getRoom` (pinned by Task 3's shared test).

- [ ] **Step 1: Write the failing tests** — add `openSessionChat: jest.fn(async () => ({ ...mockRoom, roomId: "chat-1" })),` inside `mockLive.messaging`, then append inside `describe("session screen", ...)`:

```tsx
  it("opens the session's chat as its own screen, with its unread count", async () => {
    (router.push as jest.Mock).mockClear();
    mockSession = { ...base, chatUnread: 2 };
    await render(<SessionScreen />);
    await fireEvent.press(await screen.findByLabelText("Chat, 2 unread"));
    expect(mockLive.messaging.openSessionChat).toHaveBeenCalledWith("sess-1");
    await waitFor(() => expect(router.push).toHaveBeenCalledWith("/(app)/room/chat-1"));
  });

  it("says so when the session's chat cannot open", async () => {
    mockLive.messaging.openSessionChat.mockRejectedValueOnce(new ApiError(404, null));
    await render(<SessionScreen />);
    await fireEvent.press(await screen.findByLabelText("Chat"));
    expect(await screen.findByText("Chat is not available for this session.")).toBeTruthy();
  });
```

- [ ] **Step 2: Run to verify they fail**

Run (from `apps/mobile`): `pnpm test -- "src/app/(app)/__tests__/session.test.tsx"`
Expected: FAIL — no element labelled "Chat, 2 unread".

- [ ] **Step 3: Implement** — directly after the `messageOwner` callback (`:190`), still above every early return:

```tsx
  const [chatError, setChatError] = useState<string | null>(null);
  // The session's chat (spec 2026-10-07-session-chat-design §3): the room screen, pushed; back returns here.
  const openChat = useCallback(() => {
    if (!session) return;
    setChatError(null);
    rooms.resourceSource.client.openSessionChat(session.sessionId)
      .then((room) => router.push(`/(app)/room/${room.roomId}`))
      .catch(() => setChatError("Chat is not available for this session."));
  }, [rooms.resourceSource.client, session]);
```

Replace `headerRight` (`:213-226`):

```tsx
    // Chat for everyone who can read the session; the DM button only on someone else's.
    headerRight: session ? () => (
      <View style={styles.headerActions}>
        <Pressable accessibilityRole="button" accessibilityLabel={session.chatUnread ? `Chat, ${session.chatUnread} unread` : "Chat"}
          hitSlop={12} onPress={openChat} style={styles.chatButton}>
          <SymbolView accessibilityElementsHidden name="bubble.left.and.bubble.right" size={20} tintColor={palette.fgSoft} />
          {!!session.chatUnread && <Text style={[styles.chatCount, { color: palette.link }]}>{session.chatUnread > 99 ? "99+" : session.chatUnread}</Text>}
        </Pressable>
        {session.humanUserId !== userId && (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`Message ${ownerName}`}
            // 20pt icon + 12pt hitSlop on every side reaches the 44pt tap-target floor.
            hitSlop={12}
            onPress={messageOwner}
          >
            <SymbolView accessibilityElementsHidden name="bubble.left" size={20} tintColor={palette.fgSoft} />
          </Pressable>
        )}
      </View>
    ) : undefined,
```

After the `{rooms.selectRoomError && (...)}` banner (`:264-271`):

```tsx
      {chatError && <View style={styles.banner}>
        <Text accessibilityRole="alert" style={[styles.errorText, { color: palette.alert }]}>{chatError}</Text>
      </View>}
```

Add to `StyleSheet.create({...})`:

```ts
  headerActions: { flexDirection: "row", alignItems: "center", gap: 20 },
  chatButton: { flexDirection: "row", alignItems: "center", gap: 4 },
  chatCount: { fontSize: 13, fontWeight: "600" },
```

- [ ] **Step 4: Run tests, types, lint**

Run (from `apps/mobile`): `pnpm test -- "src/app/(app)/__tests__/session.test.tsx" && pnpm typecheck && pnpm lint`
Expected: PASS (the existing "Message Bob" tests still pass: the DM button is unchanged beside Chat).

- [ ] **Step 5: Commit**

```bash
git add "apps/mobile/src/app/(app)/session/[sessionId].tsx" "apps/mobile/src/app/(app)/__tests__/session.test.tsx"
git commit -m "feat(mobile): Chat button on a session pushes its chat room"
```

---

### Task 7: Skill text

**Files:**
- Modify: `apps/infra/hermes/skills-src/lightbulb/references/surfaces.md` (new paragraph after "Agents cannot tag sessions in their own room posts." `:157`)
- Modify: `apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md` (after the "Session Details dialog" anchor `:129`)
- Test: `apps/infra/hermes/tests/test_session_chat_skill.py` (new)

- [ ] **Step 1: Write the failing test**

```python
"""The lightbulb skill tells agents a session's chat is for people, and the built tree ships it."""
import pathlib
import subprocess

HERMES = pathlib.Path(__file__).resolve().parents[1]


def test_the_skill_keeps_session_chat_answers_in_the_chat(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(HERMES / "build-skills.sh"), str(out)], check=True, capture_output=True)
    surfaces = (out / "lightbulb/references/surfaces.md").read_text()
    assert "**Session chat**" in surfaces
    assert "`Session chat · <title>`" in surfaces
    assert "Never send a chat message into the session" in surfaces
    assert "Session chat" in (HERMES / "skills-src/lightbulb/MAINTENANCE.md").read_text()
```

- [ ] **Step 2: Run to verify it fails**

Run (from `apps/infra/hermes`): `uv run --no-project --with pytest --with botocore --with pyyaml --with websockets python -m pytest tests/test_session_chat_skill.py -q`
Expected: FAIL on `"**Session chat**" in surfaces`.

- [ ] **Step 3: Add the paragraph** to `references/surfaces.md` after `Agents cannot tag sessions in their own room posts.`:

```markdown

**Session chat**: every shared session has a **Chat** beside its terminal (web and desktop: a
right column with an unread count on its **Chat** button; narrow windows and the iPhone: a
**Chat** button that opens it as its own screen). It is a hidden channel named
`Session chat · <title>`; everyone who can read the session is in it, nobody adds or removes
people by hand, and its owner may add agents. The workspace's CEO sits in it on **@mentions
only**. When you are mentioned there, the people are talking about the session, not to it:
answer in that chat as a normal reply, after you `read` the session with
`lightbulb_local_sessions` (the session is in your turn's `sessions` list under the same
title). Never send a chat message into the session, and never send the session work because
of a chat message: a person who wants the session to do something types in its terminal (if
admitted) or uses **Ask to join**. Pausing sharing empties the chat of everyone but its owner;
ending the session makes the chat read-only. Session chats do not appear in search.
```

In `MAINTENANCE.md`, after the `Session Details dialog` anchor block:

```
  - Session chat ............ references/surfaces.md ("Session chat" paragraph: hidden room, CEO on mentions,
                              answer in the chat, never into the session)
                              <- apps/backend/core/services/session_chat.py; apps/backend/routers/messaging.py (POST .../chat);
                                 apps/frontend/src/components/messaging/{SessionChat,SessionFocus}.tsx;
                                 apps/mobile/src/app/(app)/session/[sessionId].tsx (Chat button)
```

- [ ] **Step 4: Run the test**

Run (from `apps/infra/hermes`): `uv run --no-project --with pytest --with botocore --with pyyaml --with websockets python -m pytest tests/test_session_chat_skill.py tests/test_voice_skill.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/hermes/skills-src/lightbulb apps/infra/hermes/tests/test_session_chat_skill.py
git commit -m "docs(skill): agents answer in a session's chat, never into the session"
```

---

### Task 8: Prod acceptance (manual, after merge and promotion)

Merging deploys dev only; prod needs a human `gh workflow run deploy.yml --ref main`. The skill reaches agents only after `build-hermes-image.yml` builds `h-<date>-<sha7>`, a PR flips `hermes-version.json` `prod.tag`, `scripts/roll_hermes_runtimes.py` runs and `scripts/reapply_hermes_skills_dirs.py --all` re-projects the skills. Verify the backend image with `aws ecs describe-task-definition`, never from `main`. Use two real workspace accounts (A = session owner with a paired Mac, B = teammate), A not having B in "Let teammates control" or allowed senders.

- [ ] **Step 1: Chat beside a running session.** A starts a Claude session in Lightbulb (shared). A and B open it at ≥1180 px; both click **Chat**. Expect: a 344 px right column on both; B types a draft, A's terminal output scrolls and the terminal snapshot resets — B's draft, B's chat scroll position and the terminal's own scroll are independent. Messages appear on both within a second. B's name has the same colour as B's label in A's terminal band.
- [ ] **Step 2: Chat is not control.** B (not admitted) tries to type into the terminal: the view shows "Watching · Ask to join"; `POST /api/v1/messaging/sessions/{id}/input` as B answers **403** `LOCAL_SESSION_FORBIDDEN` (browser console fetch with B's Clerk token).
- [ ] **Step 3: Narrow layouts.** At 1000 px the chat overlays the right edge; at 700 px and on the iPhone the **Chat** button opens the chat as its own screen, and back returns to the session.
- [ ] **Step 4: Unshare silences within 5 s.** With B's chat open, A pauses sharing. Within 5 s B's column stops receiving A's new chat messages (B's session view says no longer shared); B's next open is refused.
- [ ] **Step 5: End archives.** A re-shares, B reopens chat, A ends the session. B's chat shows "This session ended. Its chat is read-only." with history intact.
- [ ] **Step 6: CEO answers in chat, never in the terminal.** In a running session's chat, B writes `@<CEO handle> what is this session doing?`. The CEO replies in the chat; A's terminal shows no new input and the session's audit (Details → Access) has no new command.
- [ ] **Step 7: Notification routing.** A @mentions B in the chat while B is elsewhere; B's desktop/phone notification opens the session with the chat column (web) or the chat screen (phone).
- [ ] **Step 8: Public link.** A turns on **Anyone with the link**; the `/s/<token>` page opened signed-out shows no Chat button and no chat.
- [ ] **Step 9: Record receipts** (screenshots + the 403 response) on the PR.
