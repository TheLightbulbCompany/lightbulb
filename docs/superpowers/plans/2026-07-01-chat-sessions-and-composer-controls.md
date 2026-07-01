# Chat sessions + composer controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add multi-session-per-agent chat + a per-session model·effort composer control to Isol8's `/chat`.

**Architecture:** Sessions are OpenClaw sessions keyed `agent:<agentId>:<userId>:<uuid>`, constructed server-side (the client controls only the trailing UUID). Phase 1 threads a client session id through the backend write path and fixes event routing / list-filtering; Phase 2 adds the nested-sidebar session UI + client-side event demux; Phase 3 adds the composer model·effort pill persisted via `sessions.patch`.

**Tech Stack:** FastAPI + WebSocket (backend), Next.js 16 / React 19 / SWR / vitest (frontend), OpenClaw gateway RPCs (`sessions.*`, `chat.*`, `models.list`).

## Global Constraints

- Session key is server-built `agent:<agentId>:<userId>:<sessionId>`; the client controls only the trailing `sessionId`.
- `sessionId` = `crypto.randomUUID()` (lowercase RFC-4122 v4) — NOT nanoid/base58; OpenClaw lowercases the whole key, so a mixed-case id alias-merges sessions.
- Server validates the trailing segment against `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$` before building the key.
- A missing `session_id` = the legacy 3-part `agent:<agentId>:<userId>` "General" session (never orphan existing chats).
- Message send stays on the `gate_chat` (subscription/credit) path — do NOT reroute through `sessions.send`.
- Per-session prefs persist via `sessions.patch {model|thinkingLevel|fastMode}` through the backend operator connection only (OpenClaw rejects webchat-connection session mutations).
- Model switch only when the session is idle (mid-run throws `LiveSessionModelSwitchError`); effort hidden on non-reasoning models; fast-mode shown only for `byo_key` openai (gate on owner `provider_choice`).
- OpenClaw's session store is the source of record (no new Isol8 table); `session.maintenance.mode:"warn"` so chats aren't auto-evicted.
- Verified against OpenClaw `v2026.6.10-isol8.1`.
- Frontend: never run full `pnpm test` (RAM-heavy) — targeted single-file vitest only; CI runs `tsc --noEmit` over test files, so mock builders need return-type annotations.

---

## Phase 1 — Backend session-key plumbing

Nothing user-visible ships in this phase; the single-session app keeps working (a client that sends no `session_id` gets the legacy 3-part key). All line numbers below are against current `main` at `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync`.

---

### Task 1: `build_chat_session_key` helper + UUID validation guard
**Files:**
- Modify: `apps/backend/routers/websocket_chat.py:15` (add `import re`), `:59` (add module-level `_SESSION_ID_RE` + `build_chat_session_key` after `logger = logging.getLogger(__name__)`)
- Test: `apps/backend/tests/unit/routers/test_build_chat_session_key.py` (new)

**Interfaces:**
- Consumes: nothing (leaf helper)
- Produces: `_SESSION_ID_RE` (compiled regex), `build_chat_session_key(agent_id: str, user_id: str, session_id: str | None) -> str` — returns legacy `agent:{agent_id}:{user_id}` when `session_id is None`, else validates against `_SESSION_ID_RE` (raises `ValueError` on mismatch) and returns `agent:{agent_id}:{user_id}:{session_id}`. Tasks 2 (write path) depends on it.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/routers/test_build_chat_session_key.py
"""build_chat_session_key: server-built key + trailing-UUID security guard."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from routers.websocket_chat import _SESSION_ID_RE, build_chat_session_key  # noqa: E402

_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def test_legacy_key_when_no_session_id():
    # None -> legacy 3-part "General" session so pre-sessions history is preserved.
    assert build_chat_session_key("luna", "user_1", None) == "agent:luna:user_1"


def test_four_part_key_for_valid_lowercase_uuid():
    assert build_chat_session_key("luna", "user_1", _UUID) == f"agent:luna:user_1:{_UUID}"


@pytest.mark.parametrize(
    "bad",
    [
        "3F2504E0-4F89-41D3-9A0C-0305E82C3301",  # uppercase — OpenClaw case-folds keys -> alias merge
        "not-a-uuid",
        "heartbeat",  # reserved reap token
        "group",  # reserved classify token
        "dashboard",
        f"{_UUID}:thread:x",  # colon child-key fork injection
        "user_1:subagent:evil",
        "",
    ],
)
def test_rejects_non_uuid_trailing_segment(bad):
    with pytest.raises(ValueError):
        build_chat_session_key("luna", "user_1", bad)


def test_regex_is_anchored():
    assert _SESSION_ID_RE.match(_UUID)
    assert not _SESSION_ID_RE.match(f" {_UUID}")  # leading space
    assert not _SESSION_ID_RE.match(f"{_UUID}x")  # trailing junk
```

- [ ] **Step 2: Run test to verify it fails**
```
cd apps/backend && uv run pytest tests/unit/routers/test_build_chat_session_key.py -v
```
Expected: `ImportError: cannot import name '_SESSION_ID_RE' from 'routers.websocket_chat'` (collection error) — the helper does not exist yet.

- [ ] **Step 3: Write minimal implementation**

Add `re` to the stdlib imports (currently `apps/backend/routers/websocket_chat.py:12-17`):
```python
import asyncio
import hmac
import json
import logging
import re
import secrets
```

Add the helper immediately after `logger = logging.getLogger(__name__)` (line 59):
```python
logger = logging.getLogger(__name__)

# Strict lowercase RFC-4122 v4 UUID. This is the load-bearing security guard for
# the trailing session-id segment: OpenClaw lowercases the ENTIRE session key
# before store/lookup, so a mixed-case id would alias-merge two sessions under
# the case-fold. It also blocks colon child-key forks (`:thread:`/`:subagent:`)
# and reserved tokens (`heartbeat` -> session reap; `group`/`channel`/`direct`/
# `dashboard` -> kind misclassification) — none of those can match this regex.
_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def build_chat_session_key(agent_id: str, user_id: str, session_id: str | None) -> str:
    """Construct the OpenClaw session key for a /chat conversation.

    - ``session_id is None`` -> legacy 3-part ``agent:{agent_id}:{user_id}`` (the
      "General" session) so existing single-session history is never orphaned.
    - Otherwise -> 4-part ``agent:{agent_id}:{user_id}:{session_id}`` where
      ``session_id`` MUST be a strict lowercase v4 UUID (client-minted via
      ``crypto.randomUUID()``).

    ``agent_id`` and ``user_id`` are server-supplied (``user_id`` from the JWT),
    never client-influenced — the client only ever sends the opaque trailing id.
    """
    if session_id is None:
        return f"agent:{agent_id}:{user_id}"
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"invalid session_id: {session_id!r}")
    return f"agent:{agent_id}:{user_id}:{session_id}"
```

- [ ] **Step 4: Run test to verify it passes**
```
cd apps/backend && uv run pytest tests/unit/routers/test_build_chat_session_key.py -v
```
Expected: all cases pass (1 legacy + 1 valid + 8 parametrized rejects + 1 anchor test = 11 passed).

- [ ] **Step 5: Commit**
```
git add apps/backend/routers/websocket_chat.py apps/backend/tests/unit/routers/test_build_chat_session_key.py
git commit -m "feat(chat): add build_chat_session_key helper with strict UUID guard"
```

---

### Task 2: thread `session_id` through agent_chat/abort/steer + swap the 3 hardcoded keys
**Files:**
- Modify: `apps/backend/routers/websocket_chat.py` — handlers at `:695` (agent_chat), `:744` (agent_abort), `:770` (agent_steer); background fns `_process_agent_chat_background` (`:1433` sig, `:1523` key), `_process_agent_abort_background` (`:1753` sig, `:1779` key), `_process_agent_steer_background` (`:1858` sig, `:1884` key)
- Test: `apps/backend/tests/unit/routers/test_websocket_session_key.py` (new)

**Interfaces:**
- Consumes: `build_chat_session_key` (Task 1)
- Produces: optional snake_case `session_id` field on the `agent_chat` / `agent_abort` / `agent_steer` WS envelopes; `_process_agent_*_background(..., session_id: str | None = None)`. Message send stays on the `gate_chat` path (`:1604`). Phase 3 (frontend) sends this field.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/routers/test_websocket_session_key.py
"""Write-path session-key threading: session_id -> server-built 4-part key."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from routers.websocket_chat import (  # noqa: E402
    router,
    _process_agent_abort_background,
    _process_agent_chat_background,
    _process_agent_steer_background,
)

_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


@pytest.fixture
def mock_ecs():
    with patch("routers.websocket_chat.get_ecs_manager") as g:
        m = AsyncMock()
        m.resolve_running_container = AsyncMock(
            return_value=({"gateway_token": "tok", "owner_id": "user-1"}, "10.0.1.5")
        )
        g.return_value = m
        yield m


@pytest.fixture
def mock_pool():
    with patch("routers.websocket_chat.get_gateway_pool") as g:
        p = AsyncMock()
        p.send_rpc = AsyncMock(return_value={"runId": "r"})
        p.gate_chat = AsyncMock(return_value={"blocked": False})
        p.mark_run_active = MagicMock()
        p.clear_active_run = MagicMock()
        p.mark_side_run = MagicMock()
        g.return_value = p
        yield p


@pytest.fixture
def mock_mgmt():
    with patch("routers.websocket_chat.get_management_api_client") as g:
        c = MagicMock()
        c.send_message = MagicMock(return_value=True)
        g.return_value = c
        yield c


@pytest.mark.asyncio
async def test_send_builds_4part_key_and_gate_still_fires(mock_ecs, mock_pool, mock_mgmt):
    await _process_agent_chat_background(
        connection_id="c", user_id="user-1", owner_id="user-1",
        agent_id="luna", message="hi", session_id=_UUID,
    )
    params = mock_pool.send_rpc.call_args[1]["params"]
    assert params["sessionKey"] == f"agent:luna:user-1:{_UUID}"
    # Send MUST stay on the gated path — gate_chat is the subscription/credit gate.
    mock_pool.gate_chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_legacy_key_when_no_session_id(mock_ecs, mock_pool, mock_mgmt):
    await _process_agent_chat_background(
        connection_id="c", user_id="user-1", owner_id="user-1",
        agent_id="luna", message="hi",
    )
    assert mock_pool.send_rpc.call_args[1]["params"]["sessionKey"] == "agent:luna:user-1"


@pytest.mark.asyncio
async def test_abort_builds_4part_key(mock_ecs, mock_pool, mock_mgmt):
    await _process_agent_abort_background(
        connection_id="c", user_id="user-1", owner_id="user-1",
        agent_id="luna", run_id="run-9", session_id=_UUID,
    )
    assert mock_pool.send_rpc.call_args[1]["params"]["sessionKey"] == f"agent:luna:user-1:{_UUID}"


@pytest.mark.asyncio
async def test_steer_builds_4part_key(mock_ecs, mock_pool, mock_mgmt):
    await _process_agent_steer_background(
        connection_id="c", user_id="user-1", owner_id="user-1",
        agent_id="luna", text="stop", session_id=_UUID,
    )
    assert mock_pool.send_rpc.call_args[1]["params"]["sessionKey"] == f"agent:luna:user-1:{_UUID}"


@pytest.mark.asyncio
async def test_handler_rejects_invalid_session_id_before_queue(mock_mgmt):
    """A malformed session_id is rejected at the HTTP handler; no bg task queued."""
    app = FastAPI()
    app.include_router(router, prefix="/ws")
    with patch("routers.websocket_chat.get_connection_service") as conn, patch(
        "routers.websocket_chat._process_agent_chat_background"
    ) as bg:
        conn.return_value.get_connection.return_value = {"user_id": "user-1", "org_id": None}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/ws/message",
                headers={"x-connection-id": "conn-1"},
                json={"type": "agent_chat", "agent_id": "luna", "message": "hi",
                      "session_id": "../evil"},
            )
    assert resp.status_code == 200
    bg.assert_not_called()
    assert mock_mgmt.send_message.call_args[0][1]["type"] == "error"
```

- [ ] **Step 2: Run test to verify it fails**
```
cd apps/backend && uv run pytest tests/unit/routers/test_websocket_session_key.py -v
```
Expected: `TypeError: _process_agent_chat_background() got an unexpected keyword argument 'session_id'` (the fns don't accept it yet), and `test_handler_rejects_invalid_session_id_before_queue` fails because the handler still queues the task.

- [ ] **Step 3: Write minimal implementation**

**3a. agent_chat handler** — after the missing-fields guard (`apps/backend/routers/websocket_chat.py:717-726`), before `background_tasks.add_task` at `:732`, add extraction + guard, and pass `session_id`:
```python
        # Optional client-minted session id (snake_case on the wire). None ->
        # legacy "General" session. Reject a malformed id at the handler so a
        # crafted trailing segment never reaches the key builder or gets queued.
        session_id = body.get("session_id")
        if session_id is not None:
            try:
                build_chat_session_key(agent_id, user_id, session_id)
            except ValueError:
                management_api = await get_management_api_client()
                management_api.send_message(
                    x_connection_id,
                    {"type": "error", "agent_id": agent_id, "message": "Invalid session_id"},
                )
                return Response(status_code=200)

        background_tasks.add_task(
            _process_agent_chat_background,
            connection_id=x_connection_id,
            user_id=user_id,
            owner_id=owner_id,
            agent_id=agent_id,
            message=message,
            client_run_id=client_run_id,
            side=side,
            session_id=session_id,
        )
        return Response(status_code=200)
```

**3b. agent_abort handler** (`:744-768`) — after the `if not agent_id:` guard, add the same session_id extraction/guard block, then add `session_id=session_id,` to that `background_tasks.add_task(_process_agent_abort_background, ...)` call.

**3c. agent_steer handler** (`:770-795`) — after the `if not agent_id or not text:` guard, add the same session_id extraction/guard block, then add `session_id=session_id,` to the `_process_agent_steer_background` `add_task` call.

**3d. `_process_agent_chat_background` signature** (`:1433-1441`) — add the param:
```python
async def _process_agent_chat_background(
    connection_id: str,
    user_id: str,
    owner_id: str,
    agent_id: str,
    message: str,
    client_run_id: str | None = None,
    side: bool = False,
    session_id: str | None = None,
) -> None:
```
Swap the key at `:1520-1523`:
```python
        # Session key: server-built agent:{agentId}:{userId}[:{sessionId}].
        # Every user gets their own session, isolating their chat history from
        # cron jobs, channels, and other system activity. session_id (client-
        # minted UUID) opens a distinct conversation; None = the "General" session.
        session_key = build_chat_session_key(agent_id, user_id, session_id)
```

**3e. `_process_agent_abort_background` signature** (`:1753-1759`) — add `session_id: str | None = None,`. Swap `:1779`:
```python
    session_key = build_chat_session_key(agent_id, user_id, session_id)
```

**3f. `_process_agent_steer_background` signature** (`:1858-1864`) — add `session_id: str | None = None,`. Swap `:1884`:
```python
    session_key = build_chat_session_key(agent_id, user_id, session_id)
```

- [ ] **Step 4: Run test to verify it passes**
```
cd apps/backend && uv run pytest tests/unit/routers/test_websocket_session_key.py tests/unit/routers/test_websocket_agent_chat.py -v
```
Expected: the new file's 5 tests pass; the existing `test_websocket_agent_chat.py` suite still passes (its `test_sends_chat_rpc`/`test_abort_*` assert the legacy `agent:luna:user-1` key, which `build_chat_session_key(..., None)` still returns — no regression).

- [ ] **Step 5: Commit**
```
git add apps/backend/routers/websocket_chat.py apps/backend/tests/unit/routers/test_websocket_session_key.py
git commit -m "feat(chat): thread optional session_id through send/abort/steer"
```

---

### Task 3: `_parse_session_key` — recognize the 4-part webchat key
**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py:2156-2165` (add a 4-part branch after the 3-part webchat block)
- Test: `apps/backend/tests/unit/gateway/test_session_key_parser.py` (extend)

**Interfaces:**
- Consumes: nothing
- Produces: `_parse_session_key("agent:{a}:{u}:{uuid}")` returns `{"agent_id": a, "source": "webchat", "member_id": u}`. Because `source="webchat"` (not `"unknown"`/`"paperclip"`), `is_isolated_session` in `_handle_message` is False, so streamed frames are not dropped and `member_id` is preserved for org fan-out/billing. Task 4 relies on `source == "webchat"`.

- [ ] **Step 1: Write the failing test** — add to the existing `@pytest.mark.parametrize` table in `test_session_key_parser.py`:
```python
        # 4-part webchat named session (server-built by build_chat_session_key).
        # parts[2] = userId (member routing target), parts[3] = opaque UUID.
        (
            "agent:luna:user_1:3f2504e0-4f89-41d3-9a0c-0305e82c3301",
            {"agent_id": "luna", "source": "webchat", "member_id": "user_1"},
        ),
```
and add a standalone test at the end of the file:
```python
def test_four_part_webchat_is_not_isolated_source():
    """The 4-part key must classify as webchat, not 'unknown' — otherwise
    _handle_message treats it as isolated and silently drops streamed frames."""
    parsed = _parse_session_key("agent:luna:user_1:3f2504e0-4f89-41d3-9a0c-0305e82c3301")
    assert parsed["source"] == "webchat"
    assert parsed["source"] not in ("paperclip", "unknown")
```

- [ ] **Step 2: Run test to verify it fails**
```
cd apps/backend && uv run pytest tests/unit/gateway/test_session_key_parser.py -v
```
Expected: the new parametrized case + `test_four_part_webchat_is_not_isolated_source` fail — the 4-part key currently falls through to `return {"agent_id": agent_id, "source": "unknown"}` (`connection_pool.py:2196`).

- [ ] **Step 3: Write minimal implementation** — insert a 4-part branch after the 3-part webchat block (`connection_pool.py:2165`), before the Channel-DM block:
```python
    # Webchat named session: agent:<agentId>:<userId>:<opaqueSessionId>
    # (4-part, server-built by build_chat_session_key). parts[2] is the Clerk
    # user_id (member routing target); parts[3] is the client-minted UUID.
    # Cron/paperclip 4-part keys are already handled above, and channel keys are
    # >=5 parts (group/room) or 6 (DM), so an exactly-4-part key here is
    # unambiguously a webchat session. Must classify as webchat so it is NOT
    # treated as isolated in _handle_message (which would drop streamed frames).
    if len(parts) == 4:
        return {
            "agent_id": agent_id,
            "source": "webchat",
            "member_id": parts[2],
        }
```

- [ ] **Step 4: Run test to verify it passes**
```
cd apps/backend && uv run pytest tests/unit/gateway/test_session_key_parser.py -v
```
Expected: all parser cases pass, including the new 4-part case and the not-isolated assertion. Channel DM/group/room cases (5-6 parts) still route to their own branches.

- [ ] **Step 5: Commit**
```
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/gateway/test_session_key_parser.py
git commit -m "feat(chat): parse 4-part webchat session key as non-isolated"
```

---

### Task 4: add `session_id` to forwarded event frames
**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` — compute `session_id` after `:1121`; tag it onto the transformed agent-event frame (`:1206-1214`) and each chat-terminal frame (thinking `:1291`, chunk `:1304`, done `:1309`, error `:1340`, aborted `:1349`, run_ended `:1367`)
- Test: `apps/backend/tests/gateway/test_event_session_id.py` (new)

**Interfaces:**
- Consumes: `_parse_session_key` webchat classification (Task 3)
- Produces: forwarded `chunk` / `done` / `error` / `run_ended` / `thinking` frames carry `session_id` = the trailing UUID of the 4-part key (absent/omitted for the legacy 3-part "General" key). Phase 3 (frontend `useAgentChat`) demuxes streamed events by matching `msg.session_id` to the active session.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/gateway/test_event_session_id.py
"""Forwarded event frames carry the originating session_id for per-session demux."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from core.gateway.connection_pool import GatewayConnection  # noqa: E402

_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


@pytest.fixture
def conn():
    c = GatewayConnection(
        frontend_connections=set(),
        user_id="user-1",
        ip="10.0.0.1",
        token="tok",
        management_api=MagicMock(),
        owner_id="user-1",
    )
    captured: list[dict] = []

    async def _stub_forward(message, target_member_id=None):
        captured.append(message)

    c._forward_to_frontends = _stub_forward  # type: ignore[assignment]
    c._captured = captured  # type: ignore[attr-defined]
    return c


async def _final(conn, session_key):
    await conn._handle_message(
        {
            "type": "event",
            "event": "chat",
            "payload": {"sessionKey": session_key, "state": "final", "runId": "run_1"},
        }
    )


@pytest.mark.asyncio
async def test_done_frame_carries_session_id_for_4part_key(conn):
    await _final(conn, f"agent:luna:user-1:{_UUID}")
    done = [f for f in conn._captured if f.get("type") == "done"]
    assert done and done[0]["session_id"] == _UUID


@pytest.mark.asyncio
async def test_legacy_key_omits_session_id(conn):
    await _final(conn, "agent:luna:user-1")
    done = [f for f in conn._captured if f.get("type") == "done"]
    assert done and "session_id" not in done[0]


@pytest.mark.asyncio
async def test_error_frame_carries_session_id(conn):
    await conn._handle_message(
        {
            "type": "event",
            "event": "chat",
            "payload": {
                "sessionKey": f"agent:luna:user-1:{_UUID}",
                "state": "error",
                "runId": "run_2",
                "errorMessage": "boom",
            },
        }
    )
    err = [f for f in conn._captured if f.get("type") == "error"]
    assert err and err[0]["session_id"] == _UUID
```

- [ ] **Step 2: Run test to verify it fails**
```
cd apps/backend && uv run pytest tests/gateway/test_event_session_id.py -v
```
Expected: `test_done_frame_carries_session_id_for_4part_key` and `test_error_frame_carries_session_id` fail with `KeyError: 'session_id'` — frames don't carry it yet. (`test_legacy_key_omits_session_id` passes trivially.)

- [ ] **Step 3: Write minimal implementation**

Compute `session_id` once, right after the `is_isolated_session` assignment (`connection_pool.py:1121`):
```python
            is_isolated_session = session_source in ("paperclip", "unknown")

            # Trailing opaque session id for the 4-part webchat key
            # (build_chat_session_key). None for the legacy 3-part "General"
            # key and for any non-webchat key. The client demuxes streamed
            # frames per session by matching this to its active session.
            session_parts = session_key.split(":") if session_key else []
            session_id = (
                session_parts[3]
                if len(session_parts) == 4 and session_source == "webchat"
                else None
            )
```

Tag the transformed agent-event frame (inside `if transformed:` at `:1207-1214`, after the `agent_id` tag):
```python
                    if event_agent_id:
                        transformed["agent_id"] = event_agent_id
                    if session_id is not None:
                        transformed["session_id"] = session_id
                    await self._forward_to_frontends(transformed, target_member)
```

For each chat-terminal `fwd` — thinking (`:1291-1295`), chunk (`:1304-1308`), done (`:1309-1313`), error (`:1340-1346`), aborted (`:1349-1353`) — add, right before the matching `await self._forward_to_frontends(fwd, target_member)`:
```python
                        if session_id is not None:
                            fwd["session_id"] = session_id
```
And for the `run_ended` frame (`:1367-1374`), before its forward:
```python
                    if session_id is not None:
                        run_ended["session_id"] = session_id
```
(Indentation matches each block; the thinking/chunk/done/error/aborted `fwd` blocks are nested one level deeper than `run_ended`.)

- [ ] **Step 4: Run test to verify it passes**
```
cd apps/backend && uv run pytest tests/gateway/test_event_session_id.py tests/gateway/test_side_result_forward.py -v
```
Expected: all pass — 4-part frames carry `session_id`, legacy frames omit it, and the existing side-result forwarding is unaffected.

- [ ] **Step 5: Commit**
```
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/gateway/test_event_session_id.py
git commit -m "feat(chat): tag forwarded event frames with originating session_id"
```

---

### Task 5: `sessions.list` ownership filter in the RPC proxy
**Files:**
- Modify: `apps/backend/routers/websocket_chat.py` — add `_filter_sessions_for_user` helper (near `build_chat_session_key`) and apply it in `_process_rpc_background` after the `send_rpc` result (`:1304-1312`)
- Test: `apps/backend/tests/unit/routers/test_sessions_list_filter.py` (new)

**Interfaces:**
- Consumes: nothing (parses keys inline)
- Produces: `_filter_sessions_for_user(result: dict, user_id: str) -> dict`. The browser's `sessions.list` response is filtered to rows whose key segment after `agent:<agentId>:` equals the caller's `user_id` or starts with `<user_id>:` — dropping `paperclip:`/`cron:`/channel/other-member rows. Phase 2 (sidebar) consumes the filtered list.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/routers/test_sessions_list_filter.py
"""sessions.list ownership filter: browser only sees the caller's own rows."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from routers.websocket_chat import _filter_sessions_for_user  # noqa: E402

_UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def test_keeps_own_general_and_named_drops_others():
    result = {
        "sessions": [
            {"key": "agent:luna:user_1"},  # own General (3-part)
            {"key": f"agent:luna:user_1:{_UUID}"},  # own named (4-part)
            {"key": "agent:luna:user_2"},  # another org member
            {"key": "agent:luna:paperclip"},  # paperclip
            {"key": "agent:luna:cron:job1"},  # cron
            {"key": "agent:luna:user_10"},  # prefix-collision guard: must NOT match user_1
        ]
    }
    out = _filter_sessions_for_user(result, "user_1")
    assert [s["key"] for s in out["sessions"]] == [
        "agent:luna:user_1",
        f"agent:luna:user_1:{_UUID}",
    ]


def test_non_dict_and_missing_sessions_pass_through():
    assert _filter_sessions_for_user({"other": 1}, "user_1") == {"other": 1}
    assert _filter_sessions_for_user("nope", "user_1") == "nope"  # type: ignore[arg-type]
```

- [ ] **Step 2: Run test to verify it fails**
```
cd apps/backend && uv run pytest tests/unit/routers/test_sessions_list_filter.py -v
```
Expected: `ImportError: cannot import name '_filter_sessions_for_user'`.

- [ ] **Step 3: Write minimal implementation**

Add the helper (near `build_chat_session_key`, after the `_SESSION_ID_RE`/`build_chat_session_key` definitions around `:80`):
```python
def _filter_sessions_for_user(result, user_id: str):
    """Drop sessions.list rows the caller doesn't own.

    sessions.list returns EVERY session on the shared per-owner container —
    paperclip:/cron:/channel and other org members' rows included — all
    collapsed to kind='direct'. Keep only rows whose key, after the
    ``agent:<agentId>:`` prefix, is exactly the caller's user_id (legacy
    "General" 3-part key) or starts with ``<user_id>:`` (a 4-part named
    session). The ``<user_id>:`` boundary check (not a bare startswith) stops
    ``user_1`` from matching ``user_10``. Ownership guard, not cosmetic.
    """
    if not isinstance(result, dict):
        return result
    sessions = result.get("sessions")
    if not isinstance(sessions, list):
        return result
    kept = []
    for s in sessions:
        key = s.get("key", "") if isinstance(s, dict) else ""
        parts = key.split(":", 2)
        if len(parts) < 3 or parts[0] != "agent":
            continue
        rest = parts[2]  # everything after agent:<agentId>:
        if rest == user_id or rest.startswith(f"{user_id}:"):
            kept.append(s)
    return {**result, "sessions": kept}
```

Apply it in `_process_rpc_background` — between the `send_rpc` result (`:1304-1311`) and the `management_api.send_message(..., "payload": result)` (`:1312`):
```python
        result = await pool.send_rpc(
            user_id=owner_id,
            req_id=req_id,
            method=method,
            params=params,
            ip=ip,
            token=container["gateway_token"],
        )
        # Scope sessions.list to the caller — the container returns every
        # session on the shared per-owner box (paperclip/cron/other members).
        if method == "sessions.list":
            result = _filter_sessions_for_user(result, user_id)
        management_api.send_message(
            connection_id,
            {
                "type": "res",
                "id": req_id,
                "ok": True,
                "payload": result,
            },
        )
```

- [ ] **Step 4: Run test to verify it passes**
```
cd apps/backend && uv run pytest tests/unit/routers/test_sessions_list_filter.py -v
```
Expected: both tests pass (own rows kept, `user_10` dropped, non-dict passthrough).

- [ ] **Step 5: Commit**
```
git add apps/backend/routers/websocket_chat.py apps/backend/tests/unit/routers/test_sessions_list_filter.py
git commit -m "feat(chat): filter sessions.list to the caller's own sessions"
```

---

### Task 6: `session.maintenance.mode: "warn"` in openclaw config
**Files:**
- Modify: `apps/backend/core/containers/config.py:1224-1226` (the shared `session` block)
- Test: `apps/backend/tests/unit/containers/test_config.py` (extend `TestOpenclawConfigShape`, next to `test_session_dm_scope` at `:221`)

**Interfaces:**
- Consumes: nothing
- Produces: emitted `openclaw.json` `session` block gains `"maintenance": {"mode": "warn"}`. No downstream code depends on it; OpenClaw reads it at boot so it never auto-evicts user chats.

- [ ] **Step 1: Write the failing test** — add to `TestOpenclawConfigShape` in `tests/unit/containers/test_config.py`, immediately after `test_session_dm_scope`:
```python
    def test_session_maintenance_warn(self):
        """OpenClaw's session store defaults to enforce mode (evict past
        500 entries / 30d). Heavy Paperclip owners exceed 500 sessions on the
        shared per-owner container, which would silently delete users' oldest
        /chat conversations once one agent has many sessions. warn never deletes."""
        config = _cfg()
        assert config["session"]["maintenance"] == {"mode": "warn"}
```

- [ ] **Step 2: Run test to verify it fails**
```
cd apps/backend && uv run pytest tests/unit/containers/test_config.py::TestOpenclawConfigShape::test_session_maintenance_warn -v
```
Expected: `KeyError: 'maintenance'` — the `session` block only has `dmScope` today.

- [ ] **Step 3: Write minimal implementation** — extend the `session` block at `config.py:1224-1226`:
```python
        "session": {
            "dmScope": "per-account-channel-peer",
            # OpenClaw's session-store maintenance defaults to "enforce" (evict
            # past 500 entries / 30 days). Heavy Paperclip owners blow past 500
            # sessions on the shared per-owner container, which would silently
            # reap users' oldest /chat conversations now that one agent has many
            # sessions. "warn" logs pressure but never deletes.
            "maintenance": {"mode": "warn"},
        },
```

- [ ] **Step 4: Run test to verify it passes**
```
cd apps/backend && uv run pytest tests/unit/containers/test_config.py -v
```
Expected: `test_session_maintenance_warn` passes and the rest of `test_config.py` (including `test_session_dm_scope`) still passes. Optionally, if the vendored schema bundle is present, run `uv run pytest tests/chaos/invariants/test_config_schema.py -v` to confirm the emitted config still validates against upstream's zod schema (auto-skips without the bundle).

- [ ] **Step 5: Commit**
```
git add apps/backend/core/containers/config.py apps/backend/tests/unit/containers/test_config.py
git commit -m "feat(chat): set session.maintenance warn so chats are never auto-reaped"
```

---

---

## Phase 2 — Sidebar sessions UI

Builds directly on Phase 1's backend plumbing. **CONSUMES from Phase 1:** the optional `session_id` field on the `agent_chat`/`agent_abort`/`agent_steer` client→server envelopes, and the `session_id` field the backend now stamps onto every forwarded `chunk`/`done`/`error`/tool/`run_ended` frame (trailing UUID segment of the 4-part key, or `null`/absent for the legacy 3-part "General" key). The backend `sessions.list` proxy already drops paperclip/cron/channel/other-member rows, so the frontend only parses agent + user-owned rows.

All frontend tests are **targeted single-file vitest only** (never full `pnpm test` — it is RAM-heavy). CI runs `tsc --noEmit` over test files, so every mock/helper that returns a value carries an explicit return-type annotation.

---

### Task 7: Shared `activeSessionId` store + `newSession()`, wired into `useAgentChat`

**Files:**
- Create: `apps/frontend/src/hooks/useActiveSession.ts`
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (signature :289, `cacheKey` :293, history key :634, effect deps :656, `invalidateHistoryAndRefetch` key :667, `clearMessages` key :1772, `renderItems` sessionKey :1807, `UseAgentChatReturn` :132-151, return object :1833-1849)
- Test: `apps/frontend/src/hooks/__tests__/useActiveSession.test.tsx`, `apps/frontend/src/hooks/__tests__/useAgentChat.sessions.test.tsx`

**Interfaces:**
- Consumes: nothing from earlier Phase-2 tasks (first task).
- Produces: `useActiveSession(agentId: string | null): { activeSessionId: string | null; newSession: () => string; selectSession: (sessionId: string | null) => void }`; a module-level session store shared across React subtrees; `buildChatSessionKey(agentId, sessionName, sessionId)` (local to `useAgentChat`); and `activeSessionId` / `newSession` / `selectSession` on `UseAgentChatReturn`. The trailing id is a lowercase RFC-4122 v4 from `crypto.randomUUID()` (matches the backend `_SESSION_ID_RE` guard). Later tasks (8, 9, 11) depend on `activeSessionId`, `selectSession`, `newSession`, and the store being the single source of truth so the sidebar (ChatLayout) and chat window (AgentChatWindow) agree.

- [ ] **Step 1: Write the failing test**

`apps/frontend/src/hooks/__tests__/useActiveSession.test.tsx`:
```tsx
import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

describe("useActiveSession — shared per-agent session store", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("defaults to the General session (null) for an agent", async () => {
    const { useActiveSession } = await import("@/hooks/useActiveSession");
    const { result } = renderHook(() => useActiveSession("agent-X"));
    expect(result.current.activeSessionId).toBeNull();
  });

  it("newSession() mints a lowercase UUIDv4 and sets it active", async () => {
    const { useActiveSession } = await import("@/hooks/useActiveSession");
    const { result } = renderHook(() => useActiveSession("agent-X"));
    let minted = "";
    act(() => {
      minted = result.current.newSession();
    });
    expect(minted).toMatch(UUID_RE);
    expect(result.current.activeSessionId).toBe(minted);
  });

  it("two hooks on the same agent see the same value (cross-subtree sharing)", async () => {
    const { useActiveSession } = await import("@/hooks/useActiveSession");
    const a = renderHook(() => useActiveSession("agent-X"));
    const b = renderHook(() => useActiveSession("agent-X"));
    act(() => {
      a.result.current.selectSession("11111111-1111-4111-8111-111111111111");
    });
    expect(b.result.current.activeSessionId).toBe(
      "11111111-1111-4111-8111-111111111111",
    );
  });

  it("keeps per-agent state independent", async () => {
    const { useActiveSession } = await import("@/hooks/useActiveSession");
    const x = renderHook(() => useActiveSession("agent-X"));
    const y = renderHook(() => useActiveSession("agent-Y"));
    act(() => x.result.current.newSession());
    expect(y.result.current.activeSessionId).toBeNull();
  });
});
```

`apps/frontend/src/hooks/__tests__/useAgentChat.sessions.test.tsx` (verifies `useAgentChat` builds the 4-part history key after a session switch):
```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/lib/notify", () => ({
  notifyApiError: vi.fn(),
  notifyError: vi.fn(),
  notify: vi.fn(),
  notifySuccess: vi.fn(),
}));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
const sendReq = vi.fn().mockResolvedValue({ messages: [] });
const sendChat = vi.fn();
const send = vi.fn();

vi.mock("@/hooks/useGateway", () => ({
  useGateway: (): {
    isConnected: boolean;
    nodeConnected: boolean;
    error: null;
    reconnectAttempt: number;
    send: typeof send;
    sendReq: typeof sendReq;
    sendChat: typeof sendChat;
    onEvent: (h: (n: string, d: unknown) => void) => () => void;
    onChatMessage: (h: (m: ChatIncomingMessage) => void) => () => void;
    reconnect: () => void;
  } => ({
    isConnected: true,
    nodeConnected: false,
    error: null,
    reconnectAttempt: 0,
    send,
    sendReq,
    sendChat,
    onEvent: () => () => {},
    onChatMessage: (h) => {
      chatHandlers.push(h);
      return () => {
        chatHandlers = chatHandlers.filter((x) => x !== h);
      };
    },
    reconnect: vi.fn(),
  }),
}));

describe("useAgentChat — session-aware history key", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendReq.mockReset().mockResolvedValue({ messages: [] });
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    vi.resetModules();
  });

  it("fetches history for the General 3-part key by default", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    renderHook(() => useAgentChat("agent-X", "user-1"));
    await waitFor(() => {
      expect(sendReq).toHaveBeenCalledWith(
        "chat.history",
        expect.objectContaining({ sessionKey: "agent:agent-X:user-1" }),
      );
    });
  });

  it("fetches history for the 4-part key after newSession()", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    let minted = "";
    act(() => {
      minted = result.current.newSession();
    });
    await waitFor(() => {
      expect(sendReq).toHaveBeenCalledWith(
        "chat.history",
        expect.objectContaining({
          sessionKey: `agent:agent-X:user-1:${minted}`,
        }),
      );
    });
    expect(result.current.activeSessionId).toBe(minted);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**
```
cd apps/frontend && pnpm exec vitest run src/hooks/__tests__/useActiveSession.test.tsx src/hooks/__tests__/useAgentChat.sessions.test.tsx
```
Expected: FAIL — `Cannot find module '@/hooks/useActiveSession'`, and `result.current.newSession is not a function` from the `useAgentChat` test.

- [ ] **Step 3: Write minimal implementation**

Create `apps/frontend/src/hooks/useActiveSession.ts`:
```ts
"use client";

import { useCallback, useSyncExternalStore } from "react";

// Module-level per-agent active-session store. Survives component unmounts and
// is shared across React subtrees (the sidebar SessionList in ChatLayout and
// the transcript in AgentChatWindow live in different subtrees but must agree
// on which session is active). Mirrors the _messageCache module-store pattern
// already used in useAgentChat. `null` == the legacy "General" session.
const _activeByAgent = new Map<string, string | null>();
const _listeners = new Set<() => void>();

function emit(): void {
  _listeners.forEach((l) => l());
}

function subscribe(listener: () => void): () => void {
  _listeners.add(listener);
  return () => {
    _listeners.delete(listener);
  };
}

function read(agentId: string | null): string | null {
  if (!agentId) return null;
  return _activeByAgent.get(agentId) ?? null;
}

function write(agentId: string, sessionId: string | null): void {
  _activeByAgent.set(agentId, sessionId);
  emit();
}

export interface UseActiveSessionReturn {
  activeSessionId: string | null;
  /** Mint a fresh lowercase UUIDv4, set it active, and return it. */
  newSession: () => string;
  selectSession: (sessionId: string | null) => void;
}

export function useActiveSession(agentId: string | null): UseActiveSessionReturn {
  const activeSessionId = useSyncExternalStore(
    subscribe,
    () => read(agentId),
    () => read(agentId),
  );

  const newSession = useCallback((): string => {
    // crypto.randomUUID() is RFC-4122 v4 and already lowercase — matches the
    // backend _SESSION_ID_RE guard exactly, so the 4-part key is accepted.
    const id = crypto.randomUUID();
    if (agentId) write(agentId, id);
    return id;
  }, [agentId]);

  const selectSession = useCallback(
    (sessionId: string | null): void => {
      if (agentId) write(agentId, sessionId);
    },
    [agentId],
  );

  return { activeSessionId, newSession, selectSession };
}
```

In `apps/frontend/src/hooks/useAgentChat.ts`, add the import and a key builder near the top (after the existing imports, ~line 40):
```ts
import { useActiveSession } from "@/hooks/useActiveSession";
```
```ts
// Builds the OpenClaw session key the read path (chat.history) queries. The
// write path constructs the authoritative key server-side; this must mirror
// it: 4-part `agent:{agentId}:{userId}:{sessionId}` for a real session, or the
// legacy 3-part `agent:{agentId}:{userId}` "General" session when sessionId is
// null so no history is orphaned.
function buildChatSessionKey(
  agentId: string,
  sessionName: string,
  sessionId: string | null,
): string {
  return sessionId
    ? `agent:${agentId}:${sessionName}:${sessionId}`
    : `agent:${agentId}:${sessionName}`;
}
```

Inside `useAgentChat`, pull session state from the store (right after line 290's `useGateway()` call):
```ts
  const { activeSessionId, newSession, selectSession } =
    useActiveSession(agentId);
  const activeSessionIdRef = useRef(activeSessionId);
  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);
```

Make `cacheKey` session-aware — replace line 293:
```ts
  // Cache key includes session name AND active session id so distinct sessions
  // (and org members) keep independent history caches.
  const cacheKey = agentId
    ? `${agentId}:${sessionName}:${activeSessionId ?? ""}`
    : null;
```

Replace the history-fetch key at line 634:
```ts
    const sessionKey = buildChatSessionKey(agentId, sessionName, activeSessionId);
```
Add `activeSessionId` to that effect's dep array (line 656): change `[agentId, cacheKey, sessionName, isConnected, sendReq, historyRefetchNonce]` to `[agentId, cacheKey, sessionName, activeSessionId, isConnected, sendReq, historyRefetchNonce]`.

Update `invalidateHistoryAndRefetch` key (line 667):
```ts
    const key = agentIdRef.current
      ? `${agentIdRef.current}:${sessionName}:${activeSessionIdRef.current ?? ""}`
      : null;
```
and add `activeSessionId` to its dep list on line 673.

Update `clearMessages` cache key (line 1772):
```ts
    const key = agentIdRef.current
      ? `${agentIdRef.current}:${sessionName}:${activeSessionIdRef.current ?? ""}`
      : null;
```

Update `renderItems` sessionKey (line 1807):
```ts
      sessionKey: buildChatSessionKey(agentId ?? "", sessionName, activeSessionId),
```
and add `activeSessionId` to that `useMemo` dep array on line 1822.

Add a session-switch reset effect (place after the history effect, ~line 656) so switching sessions swaps in the right transcript and drops the previous session's live stream:
```ts
  // ---- Reset live + persisted view when the active session switches ----
  // Agent switches are handled upstream (AgentChatWindow's prevAgentIdRef);
  // this covers switching sessions WITHIN one agent, which shares the same
  // useAgentChat instance.
  const prevSessionRef = useRef<string | null>(activeSessionId);
  useEffect(() => {
    if (prevSessionRef.current === activeSessionId) return;
    prevSessionRef.current = activeSessionId;
    setChatMessages(cacheKey ? _messageCache.get(cacheKey) ?? [] : []);
    setChatStream(null);
    chatStreamRunIdRef.current = null;
    streamCommittedOffsetRef.current = 0;
    setChatStreamThinking(null);
    chatStreamThinkingRunIdRef.current = null;
    setChatStreamSegments([]);
    setChatToolMessages([]);
    toolStreamByIdRef.current.clear();
    toolStreamOrderRef.current = [];
    setBtwResults([]);
    setError(null);
    setIsStreaming(false);
    clientRunIdRef.current = null;
    resumeExpectedRef.current = false;
  }, [
    activeSessionId,
    cacheKey,
    setChatStream,
    setChatStreamThinking,
    setChatStreamSegments,
    setChatToolMessages,
    setIsStreaming,
  ]);
```

Extend the public interface (`UseAgentChatReturn`, after line 151's close is fine — add before the `}`):
```ts
  /** The active session id for this agent; null == the legacy General session. */
  activeSessionId: string | null;
  /** Mint + switch to a fresh session. Returns the new session id. */
  newSession: () => string;
  /** Switch the active session (null selects General). */
  selectSession: (sessionId: string | null) => void;
```
and add them to the returned object (line 1848, before the closing `};`):
```ts
    activeSessionId,
    newSession,
    selectSession,
```

- [ ] **Step 4: Run test to verify it passes**
```
cd apps/frontend && pnpm exec vitest run src/hooks/__tests__/useActiveSession.test.tsx src/hooks/__tests__/useAgentChat.sessions.test.tsx
```
Expected: PASS (6 tests). Then guard CI: `pnpm exec tsc --noEmit` succeeds.

- [ ] **Step 5: Commit**
```
git add apps/frontend/src/hooks/useActiveSession.ts apps/frontend/src/hooks/useAgentChat.ts apps/frontend/src/hooks/__tests__/useActiveSession.test.tsx apps/frontend/src/hooks/__tests__/useAgentChat.sessions.test.tsx
git commit -m "feat(chat): shared per-agent activeSessionId store + session-aware history key"
```

---

### Task 8: Send `session_id` on chat / abort / steer envelopes

**Files:**
- Modify: `apps/frontend/src/hooks/useGateway.tsx` (`GatewayContextValue.sendChat` type :135, `sendChat` impl :533-553)
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (`dispatchNow` sendChat call :1329, `steerMessage` send :527-532, `sendMessage` `/btw` send :1559-1565, `cancelMessage` abort send :1678-1682, reconnect-replay abort :1716-1720)
- Test: `apps/frontend/src/hooks/__tests__/useAgentChat.sessionRouting.test.tsx`

**Interfaces:**
- Consumes: `activeSessionIdRef` / `activeSessionId` / `newSession` from Task 7.
- Produces: `sendChat(agentId, message, clientRunId, sessionId?)` — the `sessionId` is 4th so index `[2]` stays `clientRunId` (existing steer/queue tests keep asserting `sendChat.mock.calls[...][2]`). All three write envelopes carry `session_id` (snake_case) **only when a real session is active** — General (`null`) omits the field, so Phase 1's `build_chat_session_key(..., None)` yields the legacy key and every existing test stays green.

- [ ] **Step 1: Write the failing test**

`apps/frontend/src/hooks/__tests__/useAgentChat.sessionRouting.test.tsx`:
```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/lib/notify", () => ({
  notifyApiError: vi.fn(),
  notifyError: vi.fn(),
  notify: vi.fn(),
  notifySuccess: vi.fn(),
}));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
const sendReq = vi.fn().mockResolvedValue({ messages: [] });
const sendChat = vi.fn();
const send = vi.fn();

vi.mock("@/hooks/useGateway", () => ({
  useGateway: (): {
    isConnected: boolean;
    nodeConnected: boolean;
    error: null;
    reconnectAttempt: number;
    send: typeof send;
    sendReq: typeof sendReq;
    sendChat: typeof sendChat;
    onEvent: (h: (n: string, d: unknown) => void) => () => void;
    onChatMessage: (h: (m: ChatIncomingMessage) => void) => () => void;
    reconnect: () => void;
  } => ({
    isConnected: true,
    nodeConnected: false,
    error: null,
    reconnectAttempt: 0,
    send,
    sendReq,
    sendChat,
    onEvent: () => () => {},
    onChatMessage: (h) => {
      chatHandlers.push(h);
      return () => {
        chatHandlers = chatHandlers.filter((x) => x !== h);
      };
    },
    reconnect: vi.fn(),
  }),
}));

function dispatch(msg: ChatIncomingMessage): void {
  chatHandlers.forEach((h) => h(msg));
}

describe("useAgentChat — session_id routing on write envelopes", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendReq.mockReset().mockResolvedValue({ messages: [] });
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    vi.resetModules();
  });

  it("General session sends NO session_id (backward compatible)", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await act(async () => {
      await result.current.sendMessage("hi");
    });
    expect(sendChat).toHaveBeenCalledTimes(1);
    expect(sendChat.mock.calls[0][3]).toBeUndefined();
  });

  it("active session sends session_id on chat, abort, and steer", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    let sid = "";
    act(() => {
      sid = result.current.newSession();
    });

    await act(async () => {
      await result.current.sendMessage("hello");
    });
    expect(sendChat.mock.calls[0][3]).toBe(sid);
    const runId = sendChat.mock.calls[0][2] as string;

    // Abort carries session_id.
    act(() => dispatch({ type: "chunk", agent_id: "agent-X", runId, content: "x" } as ChatIncomingMessage));
    await act(async () => {
      await result.current.cancelMessage();
    });
    expect(send).toHaveBeenCalledWith(
      expect.objectContaining({ type: "agent_abort", agent_id: "agent-X", session_id: sid }),
    );

    // Queue a second message, then steer it — carries session_id.
    await act(async () => {
      await result.current.sendMessage("steer me");
    });
    const qid = result.current.queuedMessages[0].id;
    act(() => {
      result.current.steerMessage(qid);
    });
    expect(send).toHaveBeenCalledWith(
      expect.objectContaining({ type: "agent_steer", agent_id: "agent-X", text: "steer me", session_id: sid }),
    );
  });
});
```

- [ ] **Step 2: Run test to verify it fails**
```
cd apps/frontend && pnpm exec vitest run src/hooks/__tests__/useAgentChat.sessionRouting.test.tsx
```
Expected: FAIL — the abort/steer `send` payloads lack `session_id`, and `sendChat` is called with only 3 args so `[3]` is `undefined` even for the active-session case.

- [ ] **Step 3: Write minimal implementation**

`apps/frontend/src/hooks/useGateway.tsx` — widen the type (line 135):
```ts
  sendChat: (
    agentId: string,
    message: string,
    clientRunId: string,
    sessionId?: string,
  ) => void;
```
and the impl (lines 533-553):
```ts
  const sendChat = useCallback(
    (agentId: string, message: string, clientRunId: string, sessionId?: string) => {
      // Throw on closed socket instead of silently no-op'ing. Callers like
      // useAgentChat.dispatchNow rely on a thrown error to reset their per-
      // user-turn busy gate (clientRunIdRef). Silent no-op leaves the turn
      // marked in-flight with no chat.final ever arriving, wedging the
      // local chat queue.
      if (wsRef.current?.readyState !== WebSocket.OPEN) {
        throw new Error("Gateway connection lost");
      }
      wsRef.current.send(
        JSON.stringify({
          type: "agent_chat",
          agent_id: agentId,
          message,
          clientRunId,
          // Omitted for the General session so the backend builds the legacy
          // 3-part key; present only for a real session (validated server-side
          // against _SESSION_ID_RE before the 4-part key is constructed).
          ...(sessionId ? { session_id: sessionId } : {}),
        }),
      );
    },
    [],
  );
```

`apps/frontend/src/hooks/useAgentChat.ts` — pass the id through `dispatchNow`'s send (line 1329):
```ts
        sendChat(agentId, message, newClientRunId, activeSessionIdRef.current ?? undefined);
```

`steerMessage` send (lines 527-532):
```ts
      send({
        type: "agent_steer",
        agent_id: agentIdRef.current,
        text,
        runId: activeRunId,
        ...(activeSessionIdRef.current ? { session_id: activeSessionIdRef.current } : {}),
      });
```

`sendMessage`'s `/btw` side send (lines 1559-1565):
```ts
        send({
          type: "agent_chat",
          agent_id: agentIdRef.current,
          message: trimmed,
          side: true,
          clientRunId: sideRunId,
          ...(activeSessionIdRef.current ? { session_id: activeSessionIdRef.current } : {}),
        });
```

`cancelMessage`'s abort send (lines 1678-1682):
```ts
    const delivered = send({
      type: "agent_abort",
      agent_id: agentIdRef.current,
      ...(activeRunId ? { runId: activeRunId } : {}),
      ...(activeSessionIdRef.current ? { session_id: activeSessionIdRef.current } : {}),
    });
```

Reconnect-replay abort (lines 1716-1720):
```ts
      send({
        type: "agent_abort",
        agent_id: pending.agentId,
        ...(pending.runId ? { runId: pending.runId } : {}),
        ...(activeSessionIdRef.current ? { session_id: activeSessionIdRef.current } : {}),
      });
```

- [ ] **Step 4: Run test to verify it passes**
```
cd apps/frontend && pnpm exec vitest run src/hooks/__tests__/useAgentChat.sessionRouting.test.tsx src/hooks/__tests__/useAgentChat.steer.test.tsx src/hooks/__tests__/useAgentChat.cancel.test.tsx
```
Expected: PASS (new file + the steer/cancel regression files stay green — General omits `session_id`). Then `pnpm exec tsc --noEmit` succeeds.

- [ ] **Step 5: Commit**
```
git add apps/frontend/src/hooks/useGateway.tsx apps/frontend/src/hooks/useAgentChat.ts apps/frontend/src/hooks/__tests__/useAgentChat.sessionRouting.test.tsx
git commit -m "feat(chat): thread active session_id onto chat/abort/steer envelopes"
```

---

### Task 9: Demux incoming events by `msg.session_id`

**Files:**
- Modify: `apps/frontend/src/hooks/useGateway.tsx` (`ChatIncomingMessage` union :37-110)
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (chat handler, insert guard after the agent-id filter at :684-686)
- Test: `apps/frontend/src/hooks/__tests__/useAgentChat.demux.test.tsx`

**Interfaces:**
- Consumes: `activeSessionIdRef` from Task 7; the event-frame `session_id` field the Phase-1 backend stamps onto forwarded frames.
- Produces: `session_id?: string` on every runId-bearing `ChatIncomingMessage` variant; a demux guard that drops any session-bearing frame whose `(msg.session_id ?? null) !== activeSessionIdRef.current` (null == General). Broadcasts (`heartbeat`, `update_available`) and untagged errors (`agent_id === undefined`) stay exempt.

- [ ] **Step 1: Write the failing test**

`apps/frontend/src/hooks/__tests__/useAgentChat.demux.test.tsx`:
```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("@/lib/notify", () => ({
  notifyApiError: vi.fn(),
  notifyError: vi.fn(),
  notify: vi.fn(),
  notifySuccess: vi.fn(),
}));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
const sendReq = vi.fn().mockResolvedValue({ messages: [] });
const sendChat = vi.fn();
const send = vi.fn();

vi.mock("@/hooks/useGateway", () => ({
  useGateway: (): {
    isConnected: boolean;
    nodeConnected: boolean;
    error: null;
    reconnectAttempt: number;
    send: typeof send;
    sendReq: typeof sendReq;
    sendChat: typeof sendChat;
    onEvent: (h: (n: string, d: unknown) => void) => () => void;
    onChatMessage: (h: (m: ChatIncomingMessage) => void) => () => void;
    reconnect: () => void;
  } => ({
    isConnected: true,
    nodeConnected: false,
    error: null,
    reconnectAttempt: 0,
    send,
    sendReq,
    sendChat,
    onEvent: () => () => {},
    onChatMessage: (h) => {
      chatHandlers.push(h);
      return () => {
        chatHandlers = chatHandlers.filter((x) => x !== h);
      };
    },
    reconnect: vi.fn(),
  }),
}));

function dispatch(msg: ChatIncomingMessage): void {
  chatHandlers.forEach((h) => h(msg));
}

describe("useAgentChat — event demux by session_id", () => {
  beforeEach(() => {
    chatHandlers = [];
    sendReq.mockReset().mockResolvedValue({ messages: [] });
    sendChat.mockReset();
    send.mockReset().mockReturnValue(true);
    vi.resetModules();
  });

  it("drops a chunk stamped with a different session_id", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    let sid = "";
    act(() => {
      sid = result.current.newSession();
    });
    await act(async () => {
      await result.current.sendMessage("hi");
    });
    const runId = sendChat.mock.calls[0][2] as string;

    // Frame for a DIFFERENT session (a background tab's run) must not paint.
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId,
        content: "leak",
        session_id: "99999999-9999-4999-8999-999999999999",
      } as ChatIncomingMessage),
    );
    const leaked = result.current.renderItems.some(
      (i) =>
        i.kind === "message" &&
        Array.isArray(i.message.content) &&
        i.message.content.some((b) => b.type === "text" && b.text === "leak"),
    );
    expect(leaked).toBe(false);

    // Frame for the ACTIVE session paints.
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId,
        content: "mine",
        session_id: sid,
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);
  });

  it("General session (null) accepts frames with no session_id and drops 4-part frames", async () => {
    const { useAgentChat } = await import("@/hooks/useAgentChat");
    const { result } = renderHook(() => useAgentChat("agent-X", "user-1"));
    await act(async () => {
      await result.current.sendMessage("hi");
    });
    const runId = sendChat.mock.calls[0][2] as string;

    // A frame tagged for a real session must not reach the General view.
    act(() =>
      dispatch({
        type: "chunk",
        agent_id: "agent-X",
        runId,
        content: "other",
        session_id: "11111111-1111-4111-8111-111111111111",
      } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(false);

    // Legacy frame (no session_id) belongs to General → accepted.
    act(() =>
      dispatch({ type: "chunk", agent_id: "agent-X", runId, content: "general" } as ChatIncomingMessage),
    );
    expect(result.current.isStreaming).toBe(true);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**
```
cd apps/frontend && pnpm exec vitest run src/hooks/__tests__/useAgentChat.demux.test.tsx
```
Expected: FAIL — cross-session chunk paints (`leaked === true` / `isStreaming` flips on the wrong session), and `tsc` would reject `session_id` on the frame literals.

- [ ] **Step 3: Write minimal implementation**

`apps/frontend/src/hooks/useGateway.tsx` — add `session_id?: string` to each runId-bearing variant. Replace the union body at lines 38-109 (leaving `heartbeat` and `update_available` untouched):
```ts
  | { type: "chunk"; content: string; agent_id?: string; runId: string; session_id?: string }
  | { type: "thinking"; content: string; agent_id?: string; runId: string; session_id?: string }
  | { type: "done"; agent_id?: string; runId: string; session_id?: string }
  | { type: "error"; message: string; code?: string; agent_id?: string; runId?: string; session_id?: string }
  // Authoritative "run truly ended" frame. ADDITIONAL to done/error/aborted —
  // never a replacement. The backend forwards it EXACTLY ONCE per run on a
  // genuine termination (NOT on the interim chat.final that closes a
  // tool-approval bracket). The chat queue drains off this signal, not a
  // local terminal heuristic. See backend connection_pool.py run_ended forward.
  | { type: "run_ended"; runId: string; outcome: "done" | "error" | "aborted"; agent_id?: string; session_id?: string }
  | { type: "heartbeat" }
  | {
      type: "tool_start";
      tool: string;
      toolCallId?: string;
      args?: Record<string, unknown>;
      agent_id?: string;
      runId: string;
      session_id?: string;
    }
  | {
      type: "tool_end";
      tool: string;
      toolCallId?: string;
      result?: ToolResultBlock[];
      meta?: string;
      agent_id?: string;
      runId: string;
      session_id?: string;
    }
  | {
      type: "tool_error";
      tool: string;
      toolCallId?: string;
      result?: ToolResultBlock[];
      meta?: string;
      agent_id?: string;
      runId: string;
      session_id?: string;
    }
  // Live incremental tool output (OpenClaw tool stream phase:"update").
  // Attached to the running tool pill addressed by toolCallId.
  | {
      type: "tool_progress";
      tool: string;
      toolCallId: string;
      partialResult?: ToolResultBlock[];
      agent_id?: string;
      runId: string;
      session_id?: string;
    }
  // Live command stdout/stderr streamed mid-exec, appended to the tool pill
  // addressed by toolCallId.
  | {
      type: "command_output";
      toolCallId: string;
      output?: string;
      agent_id?: string;
      runId: string;
      session_id?: string;
    }
  // Low-noise agent-activity surfaces (proposed plan, patch summary, item
  // lifecycle, ACP step) folded into one pill. `detail` carries the full
  // upstream payload for the pill's expansion.
  | {
      type: "agent_activity";
      activity: "plan" | "patch" | "item" | "acp";
      title: string;
      detail?: Record<string, unknown>;
      agent_id?: string;
      runId: string;
      session_id?: string;
    }
  // A `/btw` (detached side-channel) answer. OpenClaw renders these as a
  // separate side card ("Not saved to chat history"); the backend maps
  // chat.side_result → this frame. Ephemeral — never folded into the
  // transcript/history. runId is the side run's id (optional).
  | { type: "btw_result"; question: string; text: string; isError?: boolean; agent_id?: string; runId?: string; session_id?: string }
  | { type: "update_available" };
```

`apps/frontend/src/hooks/useAgentChat.ts` — insert the demux guard immediately after the agent-id broadcast filter (right after line 686's closing `}`):
```ts
      // Session demux. Once one agent has many sessions (or background tabs),
      // the owner-keyed fan-out delivers frames for OTHER sessions here. A
      // session-bearing frame is only ours when its stamped session_id matches
      // the active session (a missing/null id == the legacy "General" session).
      // Broadcasts (heartbeat/update_available) carry no session_id; untagged
      // errors were already allowed through above.
      if (
        !isUntaggedBroadcast &&
        !isUntaggedError &&
        msg.type !== "heartbeat" &&
        msg.type !== "update_available" &&
        "session_id" in msg &&
        (msg.session_id ?? null) !== activeSessionIdRef.current
      ) {
        return;
      }
```

- [ ] **Step 4: Run test to verify it passes**
```
cd apps/frontend && pnpm exec vitest run src/hooks/__tests__/useAgentChat.demux.test.tsx src/hooks/__tests__/useAgentChat.subStreams.test.tsx src/hooks/__tests__/useAgentChat.busyGate.test.tsx
```
Expected: PASS — demux drops cross-session frames; existing sub-stream/busy-gate suites stay green (their frames omit `session_id`, so `"session_id" in msg` is false and the guard is skipped). Then `pnpm exec tsc --noEmit` succeeds.

- [ ] **Step 5: Commit**
```
git add apps/frontend/src/hooks/useGateway.tsx apps/frontend/src/hooks/useAgentChat.ts apps/frontend/src/hooks/__tests__/useAgentChat.demux.test.tsx
git commit -m "feat(chat): demux incoming gateway frames by session_id"
```

---

### Task 10: Filtered per-agent `useAgentSessions` hook

**Files:**
- Create: `apps/frontend/src/hooks/useAgentSessions.ts`
- Test: `apps/frontend/src/hooks/__tests__/useAgentSessions.test.tsx`

**Interfaces:**
- Consumes: `useGatewayRpc` (existing) — reads `sessions.list` (backend Phase-1 proxy already drops paperclip/cron/channel/other-member rows). No dependency on Tasks 7-9 at runtime, but the `AgentSession.sessionId` values feed Task 11's `selectSession` (Task 7).
- Produces: `interface AgentSession { sessionId: string | null; key: string; label: string; updatedAt?: number; totalTokens?: number }` and `useAgentSessions(agentId, userId): { sessions: AgentSession[]; isLoading: boolean; mutate: () => Promise<unknown> }`. This task emits **4-part** session rows only; Task 12 adds the null "General" row. `_SESSION_ID_RE` mirrors the backend guard so a malformed trailing segment is never surfaced as a selectable session.

- [ ] **Step 1: Write the failing test**

`apps/frontend/src/hooks/__tests__/useAgentSessions.test.tsx`:
```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

const useGatewayRpc = vi.fn();
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (...args: unknown[]): unknown => useGatewayRpc(...args),
}));

interface SessionRow {
  key: string;
  derivedTitle?: string;
  title?: string;
  updatedAt?: number;
  totalTokens?: number;
}
function rpcReturn(rows: SessionRow[]): {
  data: { sessions: SessionRow[] };
  error: undefined;
  isLoading: boolean;
  mutate: () => Promise<undefined>;
} {
  return { data: { sessions: rows }, error: undefined, isLoading: false, mutate: async () => undefined };
}

describe("useAgentSessions — filtered per-agent 4-part rows", () => {
  beforeEach(() => {
    vi.resetModules();
    useGatewayRpc.mockReset();
  });

  it("returns only this agent's user-owned 4-part sessions with a label", async () => {
    useGatewayRpc.mockReturnValue(
      rpcReturn([
        { key: "agent:agent-X:user-1:11111111-1111-4111-8111-111111111111", derivedTitle: "Taxes", updatedAt: 200, totalTokens: 5 },
        { key: "agent:agent-X:user-1:22222222-2222-4222-8222-222222222222", updatedAt: 300 },
        // legacy General (3-part) — excluded here, added in Task 12
        { key: "agent:agent-X:user-1", updatedAt: 100 },
        // other agent — excluded
        { key: "agent:agent-Y:user-1:33333333-3333-4333-8333-333333333333" },
        // other member — excluded
        { key: "agent:agent-X:user-2:44444444-4444-4444-8444-444444444444" },
        // malformed trailing segment (not a UUID) — excluded
        { key: "agent:agent-X:user-1:not-a-uuid" },
      ]),
    );
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions("agent-X", "user-1"));

    expect(result.current.sessions.map((s) => s.sessionId)).toEqual([
      "22222222-2222-4222-8222-222222222222", // newer first
      "11111111-1111-4111-8111-111111111111",
    ]);
    expect(result.current.sessions[1].label).toBe("Taxes");
    // Untitled falls back to a short id label, never blank.
    expect(result.current.sessions[0].label).not.toBe("");
  });

  it("returns empty and does not query when agentId is null", async () => {
    useGatewayRpc.mockReturnValue(rpcReturn([]));
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions(null, "user-1"));
    expect(result.current.sessions).toEqual([]);
    expect(useGatewayRpc).toHaveBeenCalledWith(null, expect.anything(), expect.anything());
  });
});
```

- [ ] **Step 2: Run test to verify it fails**
```
cd apps/frontend && pnpm exec vitest run src/hooks/__tests__/useAgentSessions.test.tsx
```
Expected: FAIL — `Cannot find module '@/hooks/useAgentSessions'`.

- [ ] **Step 3: Write minimal implementation**

`apps/frontend/src/hooks/useAgentSessions.ts`:
```ts
"use client";

import { useMemo } from "react";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";

// Mirror of the backend guard (websocket_chat.py _SESSION_ID_RE): a strict
// lowercase RFC-4122 v4. Anything else in the trailing segment is not a
// session we minted and must never be surfaced as selectable.
const SESSION_ID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export interface AgentSession {
  /** Trailing UUID for a real session; null == the legacy "General" session. */
  sessionId: string | null;
  /** The full OpenClaw session key. */
  key: string;
  label: string;
  updatedAt?: number;
  totalTokens?: number;
}

interface RawSessionRow {
  key: string;
  derivedTitle?: string;
  title?: string;
  lastMessage?: { text?: string };
  updatedAt?: number;
  totalTokens?: number;
}

function labelFor(row: RawSessionRow, sessionId: string): string {
  const raw = (row.derivedTitle || row.title || row.lastMessage?.text || "").trim();
  if (raw) return raw.length > 48 ? `${raw.slice(0, 48)}…` : raw;
  // Never blank — fall back to a short id so the row is always clickable.
  return `Session ${sessionId.slice(0, 8)}`;
}

/**
 * Per-agent session list for the sidebar. The backend sessions.list proxy has
 * already dropped paperclip/cron/channel/other-member rows; here we keep only
 * 4-part `agent:{agentId}:{userId}:{uuid}` rows for THIS agent + user, newest
 * first. The legacy General (3-part) row is added by useAgentSessions' Task-12
 * extension.
 */
export function useAgentSessions(
  agentId: string | null,
  userId: string | null,
): { sessions: AgentSession[]; isLoading: boolean; mutate: () => Promise<unknown> } {
  const { data, isLoading, mutate } = useGatewayRpc<
    { sessions?: RawSessionRow[] } | RawSessionRow[]
  >(
    agentId ? "sessions.list" : null,
    {
      includeGlobal: false,
      includeUnknown: false,
      includeDerivedTitles: true,
      includeLastMessage: true,
      limit: 50,
    },
    { dedupingInterval: 10_000 },
  );

  const sessions = useMemo<AgentSession[]>(() => {
    if (!agentId || !userId) return [];
    const rows: RawSessionRow[] = Array.isArray(data) ? data : data?.sessions ?? [];
    const out: AgentSession[] = [];
    for (const row of rows) {
      if (typeof row?.key !== "string") continue;
      const parts = row.key.split(":");
      if (parts.length !== 4) continue;
      if (parts[0] !== "agent" || parts[1] !== agentId || parts[2] !== userId) continue;
      const sessionId = parts[3];
      if (!SESSION_ID_RE.test(sessionId)) continue;
      out.push({
        sessionId,
        key: row.key,
        label: labelFor(row, sessionId),
        updatedAt: row.updatedAt,
        totalTokens: row.totalTokens,
      });
    }
    out.sort((a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0));
    return out;
  }, [data, agentId, userId]);

  return { sessions, isLoading, mutate };
}
```

- [ ] **Step 4: Run test to verify it passes**
```
cd apps/frontend && pnpm exec vitest run src/hooks/__tests__/useAgentSessions.test.tsx
```
Expected: PASS (2 tests). Then `pnpm exec tsc --noEmit` succeeds.

- [ ] **Step 5: Commit**
```
git add apps/frontend/src/hooks/useAgentSessions.ts apps/frontend/src/hooks/__tests__/useAgentSessions.test.tsx
git commit -m "feat(chat): filtered per-agent useAgentSessions hook"
```

---

### Task 11: `SessionList` sidebar component + "New session", wired under each agent

**Files:**
- Create: `apps/frontend/src/components/chat/SessionList.tsx`
- Modify: `apps/frontend/src/components/chat/ChatLayout.tsx` (imports; the `agents.map` agent-item block :583-663)
- Test: `apps/frontend/src/components/chat/__tests__/SessionList.test.tsx`

**Interfaces:**
- Consumes: `AgentSession` + `useAgentSessions` (Task 10); `useActiveSession` (Task 7). ChatLayout wires `useActiveSession(currentAgentId)` and `useAgentSessions(currentAgentId, userId)` into the presentational `SessionList`. Because `useActiveSession` is a shared module store, selecting a row here drives the transcript in `AgentChatWindow`'s `useAgentChat` (same store) — no prop drilling across subtrees.
- Produces: `SessionList` (presentational: `{ sessions, activeSessionId, onSelect, onNew }`), rendered nested under the active agent row.

- [ ] **Step 1: Write the failing test**

`apps/frontend/src/components/chat/__tests__/SessionList.test.tsx`:
```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { SessionList } from "@/components/chat/SessionList";
import type { AgentSession } from "@/hooks/useAgentSessions";

function makeSessions(): AgentSession[] {
  return [
    { sessionId: "11111111-1111-4111-8111-111111111111", key: "k1", label: "Taxes", updatedAt: 2 },
    { sessionId: "22222222-2222-4222-8222-222222222222", key: "k2", label: "Trip plan", updatedAt: 1 },
  ];
}

describe("SessionList", () => {
  it("renders a row per session and a New session control", () => {
    render(
      <SessionList
        sessions={makeSessions()}
        activeSessionId={null}
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );
    expect(screen.getByText("Taxes")).toBeTruthy();
    expect(screen.getByText("Trip plan")).toBeTruthy();
    expect(screen.getByRole("button", { name: /new session/i })).toBeTruthy();
  });

  it("calls onSelect with the session id when a row is clicked", () => {
    const onSelect = vi.fn();
    render(
      <SessionList
        sessions={makeSessions()}
        activeSessionId={null}
        onSelect={onSelect}
        onNew={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("Trip plan"));
    expect(onSelect).toHaveBeenCalledWith("22222222-2222-4222-8222-222222222222");
  });

  it("calls onNew when New session is clicked", () => {
    const onNew = vi.fn();
    render(
      <SessionList sessions={[]} activeSessionId={null} onSelect={vi.fn()} onNew={onNew} />,
    );
    fireEvent.click(screen.getByRole("button", { name: /new session/i }));
    expect(onNew).toHaveBeenCalledTimes(1);
  });

  it("marks the active row via aria-current", () => {
    render(
      <SessionList
        sessions={makeSessions()}
        activeSessionId="11111111-1111-4111-8111-111111111111"
        onSelect={vi.fn()}
        onNew={vi.fn()}
      />,
    );
    const active = screen.getByText("Taxes").closest("[aria-current]");
    expect(active?.getAttribute("aria-current")).toBe("true");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**
```
cd apps/frontend && pnpm exec vitest run src/components/chat/__tests__/SessionList.test.tsx
```
Expected: FAIL — `Cannot find module '@/components/chat/SessionList'`.

- [ ] **Step 3: Write minimal implementation**

`apps/frontend/src/components/chat/SessionList.tsx`:
```tsx
"use client";

import { Plus } from "lucide-react";
import type { AgentSession } from "@/hooks/useAgentSessions";

interface SessionListProps {
  sessions: AgentSession[];
  activeSessionId: string | null;
  onSelect: (sessionId: string | null) => void;
  onNew: () => void;
}

/**
 * Nested per-agent session list shown under the active agent in the sidebar.
 * Presentational only — the caller (ChatLayout) supplies sessions from
 * useAgentSessions and select/new handlers from useActiveSession.
 */
export function SessionList({
  sessions,
  activeSessionId,
  onSelect,
  onNew,
}: SessionListProps): React.JSX.Element {
  return (
    <div className="session-list">
      {sessions.map((s) => {
        const isActive = s.sessionId === activeSessionId;
        return (
          <button
            key={s.key}
            type="button"
            className={`session-item${isActive ? " active" : ""}`}
            aria-current={isActive ? "true" : undefined}
            onClick={(e) => {
              e.stopPropagation();
              onSelect(s.sessionId);
            }}
          >
            <span className="session-label">{s.label}</span>
          </button>
        );
      })}
      <button
        type="button"
        className="session-new"
        aria-label="New session"
        onClick={(e) => {
          e.stopPropagation();
          onNew();
        }}
      >
        <Plus size={13} />
        <span>New session</span>
      </button>
    </div>
  );
}
```

Wire into `ChatLayout.tsx`. Add imports at the top:
```tsx
import { SessionList } from "@/components/chat/SessionList";
import { useActiveSession } from "@/hooks/useActiveSession";
import { useAgentSessions } from "@/hooks/useAgentSessions";
import { useAuth } from "@clerk/nextjs";
```
Inside the component body (near the other hooks, after `currentAgentId` is derived at :137):
```tsx
  const { userId: clerkUserId } = useAuth();
  const { activeSessionId, newSession, selectSession } = useActiveSession(currentAgentId);
  const { sessions: agentSessions } = useAgentSessions(currentAgentId, clerkUserId ?? null);
```
Render the list inside the agent-item, immediately after the `<StatusDot />` at line 660 and before the closing `</div>` at 661 — scoped to the active agent so only the open agent expands its sessions:
```tsx
                      <StatusDot status="active" />
                      {currentAgentId === agent.id && (
                        <SessionList
                          sessions={agentSessions}
                          activeSessionId={activeSessionId}
                          onSelect={(sid) => {
                            selectSession(sid);
                            setSidebarOpen(false);
                          }}
                          onNew={() => {
                            newSession();
                            setSidebarOpen(false);
                          }}
                        />
                      )}
```

- [ ] **Step 4: Run test to verify it passes**
```
cd apps/frontend && pnpm exec vitest run src/components/chat/__tests__/SessionList.test.tsx src/components/chat/__tests__/ChatLayout.unifiedSidebar.test.ts
```
Expected: PASS — SessionList renders/handles clicks; the existing ChatLayout sidebar suite stays green. Then `pnpm exec tsc --noEmit` succeeds.

- [ ] **Step 5: Commit**
```
git add apps/frontend/src/components/chat/SessionList.tsx apps/frontend/src/components/chat/ChatLayout.tsx apps/frontend/src/components/chat/__tests__/SessionList.test.tsx
git commit -m "feat(chat): nested per-agent SessionList sidebar + New session"
```

---

### Task 12: Surface the legacy General session as one row

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentSessions.ts` (add the 3-part General row)
- Modify: `apps/frontend/src/components/chat/SessionList.tsx` (render the `sessionId === null` General row)
- Test: `apps/frontend/src/hooks/__tests__/useAgentSessions.general.test.tsx`, extend `SessionList.test.tsx`

**Interfaces:**
- Consumes: `AgentSession` (Task 10); `SessionList` (Task 11).
- Produces: `useAgentSessions` now also emits `{ sessionId: null, label: "General", key: agent:{agentId}:{userId} }` when the legacy 3-part key exists for this agent+user, pinned first. `SessionList` renders the null row (selecting it calls `onSelect(null)` → General), so the legacy `agent:{agentId}:{userId}` conversation is never orphaned.

- [ ] **Step 1: Write the failing test**

`apps/frontend/src/hooks/__tests__/useAgentSessions.general.test.tsx`:
```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

const useGatewayRpc = vi.fn();
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (...args: unknown[]): unknown => useGatewayRpc(...args),
}));

interface SessionRow { key: string; updatedAt?: number }
function rpcReturn(rows: SessionRow[]): {
  data: { sessions: SessionRow[] };
  error: undefined;
  isLoading: boolean;
  mutate: () => Promise<undefined>;
} {
  return { data: { sessions: rows }, error: undefined, isLoading: false, mutate: async () => undefined };
}

describe("useAgentSessions — legacy General row", () => {
  beforeEach(() => {
    vi.resetModules();
    useGatewayRpc.mockReset();
  });

  it("pins a General (null) row first when the 3-part key exists", async () => {
    useGatewayRpc.mockReturnValue(
      rpcReturn([
        { key: "agent:agent-X:user-1", updatedAt: 50 },
        { key: "agent:agent-X:user-1:11111111-1111-4111-8111-111111111111", updatedAt: 500 },
      ]),
    );
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions("agent-X", "user-1"));
    expect(result.current.sessions[0].sessionId).toBeNull();
    expect(result.current.sessions[0].label).toBe("General");
    expect(result.current.sessions).toHaveLength(2);
  });

  it("omits the General row when no legacy key exists", async () => {
    useGatewayRpc.mockReturnValue(
      rpcReturn([
        { key: "agent:agent-X:user-1:22222222-2222-4222-8222-222222222222" },
      ]),
    );
    const { useAgentSessions } = await import("@/hooks/useAgentSessions");
    const { result } = renderHook(() => useAgentSessions("agent-X", "user-1"));
    expect(result.current.sessions.some((s) => s.sessionId === null)).toBe(false);
  });
});
```

Append to `apps/frontend/src/components/chat/__tests__/SessionList.test.tsx`:
```tsx
  it("renders the General row and selects null when clicked", () => {
    const onSelect = vi.fn();
    render(
      <SessionList
        sessions={[{ sessionId: null, key: "agent:a:u", label: "General" }]}
        activeSessionId={null}
        onSelect={onSelect}
        onNew={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("General"));
    expect(onSelect).toHaveBeenCalledWith(null);
  });
```

- [ ] **Step 2: Run test to verify it fails**
```
cd apps/frontend && pnpm exec vitest run src/hooks/__tests__/useAgentSessions.general.test.tsx src/components/chat/__tests__/SessionList.test.tsx
```
Expected: FAIL — no General row is emitted (`sessions[0].sessionId` is the UUID, length 1), and the appended SessionList case passes only once the null row renders (it already does via Task 11's `.map`, but the hook must supply it).

- [ ] **Step 3: Write minimal implementation**

In `apps/frontend/src/hooks/useAgentSessions.ts`, inside the `useMemo`, after the `for` loop populates `out` and before the final `sort`, detect the legacy key and unshift a General row:
```ts
    // Surface the legacy 3-part conversation as a normal "General" session so
    // no pre-sessions history is orphaned. The backend proxy has already
    // scoped rows to this user, so the only 3-part row for agentId+userId is
    // the legacy key `agent:{agentId}:{userId}`.
    const legacyKey = `agent:${agentId}:${userId}`;
    const hasGeneral = rows.some((row) => row?.key === legacyKey);

    out.sort((a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0));
    if (hasGeneral) {
      out.unshift({ sessionId: null, key: legacyKey, label: "General" });
    }
    return out;
```
(Remove the standalone `out.sort(...); return out;` lines that Task 10 added so the sort/return happen once, as shown.)

`SessionList.tsx` already renders `sessions.map` including the `sessionId === null` row and calls `onSelect(s.sessionId)` (which passes `null`) — no change needed for rendering. Confirm the active-state check `s.sessionId === activeSessionId` correctly highlights General when `activeSessionId === null` (it does, since `null === null`).

- [ ] **Step 4: Run test to verify it passes**
```
cd apps/frontend && pnpm exec vitest run src/hooks/__tests__/useAgentSessions.general.test.tsx src/hooks/__tests__/useAgentSessions.test.tsx src/components/chat/__tests__/SessionList.test.tsx
```
Expected: PASS — General pinned first when present, absent otherwise; Task 10's 4-part suite still green; SessionList General-click selects `null`. Then `pnpm exec tsc --noEmit` succeeds.

- [ ] **Step 5: Commit**
```
git add apps/frontend/src/hooks/useAgentSessions.ts apps/frontend/src/components/chat/SessionList.tsx apps/frontend/src/hooks/__tests__/useAgentSessions.general.test.tsx apps/frontend/src/components/chat/__tests__/SessionList.test.tsx
git commit -m "feat(chat): surface legacy General session as a selectable row"
```

---

---

## Phase 3 — Composer model·effort controls

**CONSUMES from Phase 2:** `useAgentChat(...)` now returns `activeSessionId: string | null` (the client-minted lowercase UUID for the active session, or `null` for the legacy "General" session), and `AgentChatWindow` destructures it alongside `isStreaming`. This phase wires the combined **model · effort** pill onto the composer, resolving each patch through the existing `useGatewayRpcMutation()` → `callRpc("sessions.patch", { key, ... })` operator path (the same path `SessionsPanel.tsx:159` uses today; the backend proxies it to the operator connection).

Verified against current code:
- `ModelSelector.extractProvider` (`apps/frontend/src/components/chat/ModelSelector.tsx:69-83`) splits `modelId` on `"."`, which corrupts Bedrock application-inference-profile ARNs. We replace it in the composer path with an explicit `provider` + `id` from `models.list`.
- `sessions.patch` operator path already works from the browser: `SessionsPanel.tsx:148-166` calls `callRpc("sessions.patch", { key, ...patch })`; backend forwards it (`websocket_chat.py:1570`). We reuse it — **no new mutation route**.
- Owner provider gate source: `useBilling()` returns `provider_choice` + `byo_provider` on the billing account (`apps/frontend/src/hooks/useBilling.ts:21-23`).
- `useGatewayRpcMutation()` (`apps/frontend/src/hooks/useGatewayRpc.ts:119-132`) and `useGatewayRpc()` (`:24-108`) are the read/write RPC hooks.

---

### Task 13: Pure model helpers — provider/id ref builder + reasoning detection
**Files:**
- Create: `apps/frontend/src/lib/models.ts`
- Test: `apps/frontend/src/lib/__tests__/models.test.ts`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces: `interface ModelEntry { id: string; provider: string; name?: string; available?: boolean; reasoning?: boolean }`, `buildModelRef(provider: string, id: string): string`, `isReasoningModel(entry: ModelEntry): boolean`, `providerLabelFor(entry: ModelEntry): string`.

- [ ] **Step 1: Write the failing test**

```ts
// apps/frontend/src/lib/__tests__/models.test.ts
import { describe, it, expect } from "vitest";
import {
  buildModelRef,
  isReasoningModel,
  providerLabelFor,
  type ModelEntry,
} from "@/lib/models";

const entry = (over: Partial<ModelEntry>): ModelEntry => ({
  id: "gpt-4o",
  provider: "openai",
  ...over,
});

describe("buildModelRef", () => {
  it("joins provider and id verbatim without splitting on dots", () => {
    // Bedrock application-inference-profile ARN — the '.'-split in
    // ModelSelector.extractProvider mangled this; we must not touch it.
    const id =
      "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/abc.def";
    expect(buildModelRef("amazon-bedrock", id)).toBe(`amazon-bedrock/${id}`);
  });
  it("builds a plain openai ref", () => {
    expect(buildModelRef("openai", "gpt-5")).toBe("openai/gpt-5");
  });
});

describe("isReasoningModel", () => {
  it("honors an explicit reasoning flag over heuristics", () => {
    expect(isReasoningModel(entry({ id: "gpt-4o", reasoning: true }))).toBe(true);
    expect(isReasoningModel(entry({ id: "o3", reasoning: false }))).toBe(false);
  });
  it("detects reasoning families by id when no flag is present", () => {
    expect(isReasoningModel(entry({ id: "o3-mini" }))).toBe(true);
    expect(isReasoningModel(entry({ id: "gpt-5" }))).toBe(true);
    expect(
      isReasoningModel(entry({ id: "claude-sonnet-4-20250514", provider: "anthropic" })),
    ).toBe(true);
  });
  it("returns false for non-reasoning models", () => {
    expect(isReasoningModel(entry({ id: "gpt-4o" }))).toBe(false);
    expect(isReasoningModel(entry({ id: "claude-3-5-haiku", provider: "anthropic" }))).toBe(false);
  });
});

describe("providerLabelFor", () => {
  it("uses the entry provider field, never a dot-split", () => {
    expect(
      providerLabelFor(entry({ id: "us.anthropic.claude-x", provider: "amazon-bedrock" })),
    ).toBe("amazon-bedrock");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**
  - `cd apps/frontend && pnpm exec vitest run src/lib/__tests__/models.test.ts`
  - Expected: fails at import — `Failed to resolve import "@/lib/models"` (module does not exist yet).

- [ ] **Step 3: Write minimal implementation**

```ts
// apps/frontend/src/lib/models.ts

/**
 * A single model from OpenClaw's per-agent `models.list`. Unlike the legacy
 * ModelSelector which derived the provider by splitting the id on ".", the
 * catalog carries `provider` and `id` as SEPARATE fields — the composer passes
 * both to sessions.patch and rebuilds `${provider}/${id}` (Bedrock ids are
 * application-inference-profile ARNs full of dots that a split corrupts).
 */
export interface ModelEntry {
  id: string;
  provider: string;
  name?: string;
  /** Owner has access to this model (drives the disabled + "access denied" hint). */
  available?: boolean;
  /** Explicit reasoning capability if the catalog reports it. */
  reasoning?: boolean;
}

export function buildModelRef(provider: string, id: string): string {
  return `${provider}/${id}`;
}

// Reasoning families we know support a thinking/effort budget. Kept strict to
// current usage (no migration-history widening): OpenAI o-series + gpt-5, and
// Anthropic Claude 3.7 / 4 (extended thinking). Non-matches hide the effort UI.
const REASONING_ID_RE =
  /(^|[/.:_-])(o1|o3|o4|gpt-5|claude-3-7|claude-sonnet-4|claude-opus-4|sonnet-4|opus-4)/i;

export function isReasoningModel(entry: ModelEntry): boolean {
  if (typeof entry.reasoning === "boolean") return entry.reasoning;
  return REASONING_ID_RE.test(entry.id);
}

export function providerLabelFor(entry: ModelEntry): string {
  return entry.provider || "unknown";
}
```

- [ ] **Step 4: Run test to verify it passes**
  - `cd apps/frontend && pnpm exec vitest run src/lib/__tests__/models.test.ts`
  - Expected: `Test Files 1 passed`, all cases green.

- [ ] **Step 5: Commit**
  - `git add apps/frontend/src/lib/models.ts apps/frontend/src/lib/__tests__/models.test.ts`
  - `git commit -m "feat(chat): pure model-ref + reasoning-detection helpers for composer pill"`

---

### Task 14: Frontend session-key builder mirroring the backend contract
**Files:**
- Create: `apps/frontend/src/lib/sessionKey.ts`
- Test: `apps/frontend/src/lib/__tests__/sessionKey.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `buildChatSessionKey(agentId: string, userId: string, sessionId: string | null): string` — returns the legacy 3-part `agent:${agentId}:${userId}` when `sessionId` is `null`, else validates the UUID (same regex as the backend `_SESSION_ID_RE`) and returns `agent:${agentId}:${userId}:${sessionId}`. Used by `sessions.patch` to target the exact active session; the backend still re-validates on the write path, so this is a UX convenience, not the security boundary.

- [ ] **Step 1: Write the failing test**

```ts
// apps/frontend/src/lib/__tests__/sessionKey.test.ts
import { describe, it, expect } from "vitest";
import { buildChatSessionKey } from "@/lib/sessionKey";

describe("buildChatSessionKey", () => {
  it("returns the legacy 3-part key for the General session (null id)", () => {
    expect(buildChatSessionKey("agent_a", "user_1", null)).toBe("agent:agent_a:user_1");
  });
  it("returns a 4-part key for a valid lowercase UUID", () => {
    const id = "3f2504e0-4f89-41d3-9a0c-0305e82c3301";
    expect(buildChatSessionKey("agent_a", "user_1", id)).toBe(
      `agent:agent_a:user_1:${id}`,
    );
  });
  it("rejects colon-injection and reserved-token ids", () => {
    expect(() => buildChatSessionKey("agent_a", "user_1", "foo:heartbeat")).toThrow();
    expect(() => buildChatSessionKey("agent_a", "user_1", "NOT-A-UUID")).toThrow();
    // Uppercase UUID would alias-merge under OpenClaw's lowercase fold.
    expect(() =>
      buildChatSessionKey("agent_a", "user_1", "3F2504E0-4F89-41D3-9A0C-0305E82C3301"),
    ).toThrow();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**
  - `cd apps/frontend && pnpm exec vitest run src/lib/__tests__/sessionKey.test.ts`
  - Expected: fails — `Failed to resolve import "@/lib/sessionKey"`.

- [ ] **Step 3: Write minimal implementation**

```ts
// apps/frontend/src/lib/sessionKey.ts

// Mirrors the backend guard (websocket_chat.py `_SESSION_ID_RE`): strict
// lowercase RFC-4122 v4. OpenClaw lowercases the whole key before store/lookup,
// so a mixed-case id would alias-merge two sessions.
const SESSION_ID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * Build the OpenClaw session key the backend expects for sessions.patch.
 * `null` sessionId → the legacy "General" 3-part key; otherwise the validated
 * 4-part key. The backend re-validates on the write path; this just keeps the
 * composer from targeting a malformed key.
 */
export function buildChatSessionKey(
  agentId: string,
  userId: string,
  sessionId: string | null,
): string {
  if (sessionId === null) return `agent:${agentId}:${userId}`;
  if (!SESSION_ID_RE.test(sessionId)) {
    throw new Error(`Invalid session id: ${sessionId}`);
  }
  return `agent:${agentId}:${userId}:${sessionId}`;
}
```

- [ ] **Step 4: Run test to verify it passes**
  - `cd apps/frontend && pnpm exec vitest run src/lib/__tests__/sessionKey.test.ts`
  - Expected: `Test Files 1 passed`, 3 cases green.

- [ ] **Step 5: Commit**
  - `git add apps/frontend/src/lib/sessionKey.ts apps/frontend/src/lib/__tests__/sessionKey.test.ts`
  - `git commit -m "feat(chat): frontend session-key builder mirroring backend UUID guard"`

---

### Task 15: `useAgentModels` — `models.list` scoped to the active agent with default-model fallback
**Files:**
- Create: `apps/frontend/src/hooks/useAgentModels.ts`
- Test: `apps/frontend/src/hooks/__tests__/useAgentModels.test.ts`

**Interfaces:**
- Consumes: `useGatewayRpc` (`@/hooks/useGatewayRpc`), `ModelEntry` (Task 13).
- Produces: `interface UseAgentModelsResult { models: ModelEntry[]; defaultModel: string; isLoading: boolean; usingFallback: boolean }`, `useAgentModels(agentId: string | null): UseAgentModelsResult`. On `models.list` error/empty (catalog discovery timeout, ~750ms) it falls back to the agent's default model derived from `config.get` so the picker never renders "no models".

- [ ] **Step 1: Write the failing test**

```ts
// apps/frontend/src/hooks/__tests__/useAgentModels.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import type { ModelEntry } from "@/lib/models";

interface RpcResult<T> {
  data: T | undefined;
  error: Error | undefined;
  isLoading: boolean;
  mutate: () => Promise<T | undefined>;
}

const mockRpc = vi.fn();
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (method: string | null, params?: Record<string, unknown>) =>
    mockRpc(method, params) as RpcResult<unknown>,
}));

import { useAgentModels } from "@/hooks/useAgentModels";

const rpc = <T,>(over: Partial<RpcResult<T>>): RpcResult<T> => ({
  data: undefined,
  error: undefined,
  isLoading: false,
  mutate: async () => undefined,
  ...over,
});

const CONFIG = {
  config: {
    agents: {
      defaults: { model: "amazon-bedrock/default-x" },
      list: [{ id: "agent_a", model: "openai/gpt-5" }],
    },
  },
};

beforeEach(() => mockRpc.mockReset());

describe("useAgentModels", () => {
  it("returns models.list entries when the catalog resolves", () => {
    const models: ModelEntry[] = [
      { id: "gpt-5", provider: "openai", name: "GPT-5", available: true },
    ];
    mockRpc.mockImplementation((method: string) =>
      method === "models.list" ? rpc({ data: { models } }) : rpc({ data: CONFIG }),
    );
    const { result } = renderHook(() => useAgentModels("agent_a"));
    expect(result.current.usingFallback).toBe(false);
    expect(result.current.models).toHaveLength(1);
    expect(result.current.models[0].id).toBe("gpt-5");
  });

  it("falls back to the agent default model on catalog error", () => {
    mockRpc.mockImplementation((method: string) =>
      method === "models.list"
        ? rpc({ error: new Error("timeout") })
        : rpc({ data: CONFIG }),
    );
    const { result } = renderHook(() => useAgentModels("agent_a"));
    expect(result.current.usingFallback).toBe(true);
    expect(result.current.defaultModel).toBe("openai/gpt-5");
    expect(result.current.models[0].id).toBe("openai/gpt-5");
    expect(result.current.models[0].provider).toBe("openai");
  });

  it("falls back to defaults.model when the agent has no per-agent model and list is empty", () => {
    mockRpc.mockImplementation((method: string) =>
      method === "models.list"
        ? rpc({ data: { models: [] } })
        : rpc({ data: { config: { agents: { defaults: { model: "amazon-bedrock/default-x" } } } } }),
    );
    const { result } = renderHook(() => useAgentModels("agent_z"));
    expect(result.current.usingFallback).toBe(true);
    expect(result.current.defaultModel).toBe("amazon-bedrock/default-x");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**
  - `cd apps/frontend && pnpm exec vitest run src/hooks/__tests__/useAgentModels.test.ts`
  - Expected: fails — `Failed to resolve import "@/hooks/useAgentModels"`.

- [ ] **Step 3: Write minimal implementation**

```ts
// apps/frontend/src/hooks/useAgentModels.ts
"use client";

import { useMemo } from "react";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import type { ModelEntry } from "@/lib/models";

interface ModelsListResponse {
  models?: ModelEntry[];
}

interface ConfigGet {
  config?: {
    agents?: {
      defaults?: { model?: string | { primary?: string } };
      list?: { id?: string; model?: string | { primary?: string } }[];
    };
  };
}

function primary(model?: string | { primary?: string }): string | undefined {
  if (typeof model === "string") return model.trim() || undefined;
  if (model && typeof model === "object") return model.primary?.trim() || undefined;
  return undefined;
}

export interface UseAgentModelsResult {
  models: ModelEntry[];
  /** Model ref (`${provider}/${id}`) to fall back to; also the "Included" hint. */
  defaultModel: string;
  isLoading: boolean;
  usingFallback: boolean;
}

/**
 * The owner's models for the active agent via OpenClaw `models.list` (per-agent
 * discovery). On timeout/empty we fall back to the agent's configured default
 * model from config.get so the picker never shows "no models" — see the design
 * doc's error-handling section.
 */
export function useAgentModels(agentId: string | null): UseAgentModelsResult {
  const { data, error, isLoading } = useGatewayRpc<ModelsListResponse | ModelEntry[]>(
    agentId ? "models.list" : null,
    agentId ? { agentId } : undefined,
  );
  const { data: cfg } = useGatewayRpc<ConfigGet>("config.get");

  return useMemo(() => {
    const raw: ModelEntry[] = Array.isArray(data)
      ? data
      : (data as ModelsListResponse | undefined)?.models ?? [];

    const agentEntry = cfg?.config?.agents?.list?.find((a) => a?.id === agentId);
    const defaultModel =
      primary(agentEntry?.model) ?? primary(cfg?.config?.agents?.defaults?.model) ?? "";

    if (error || raw.length === 0) {
      const fallback: ModelEntry[] = defaultModel
        ? [
            {
              id: defaultModel,
              provider: defaultModel.split("/")[0] || "unknown",
              name: defaultModel,
              available: true,
            },
          ]
        : [];
      return { models: fallback, defaultModel, isLoading, usingFallback: true };
    }
    return { models: raw, defaultModel, isLoading, usingFallback: false };
  }, [data, cfg, agentId, error, isLoading]);
}
```

- [ ] **Step 4: Run test to verify it passes**
  - `cd apps/frontend && pnpm exec vitest run src/hooks/__tests__/useAgentModels.test.ts`
  - Expected: `Test Files 1 passed`, 3 cases green.
  - Note: the `rpc<T>()`/`RpcResult<T>` builders are return-type annotated so `tsc --noEmit` (which CI runs over test files) type-checks the mocks.

- [ ] **Step 5: Commit**
  - `git add apps/frontend/src/hooks/useAgentModels.ts apps/frontend/src/hooks/__tests__/useAgentModels.test.ts`
  - `git commit -m "feat(chat): useAgentModels — models.list scoped to agent with default-model fallback"`

---

### Task 16: `useSessionModelControls` — persist model/effort/fast-mode via `sessions.patch`, idle-only model switch
**Files:**
- Create: `apps/frontend/src/hooks/useSessionModelControls.ts`
- Test: `apps/frontend/src/hooks/__tests__/useSessionModelControls.test.ts`

**Interfaces:**
- Consumes: `useGatewayRpcMutation` (`@/hooks/useGatewayRpc`), `buildChatSessionKey` (Task 14), `buildModelRef` + `ModelEntry` (Task 13), Clerk `useUser`.
- Produces: `class LiveSessionModelSwitchError extends Error`, `useSessionModelControls(agentId, activeSessionId, isStreaming) → { setModel(entry: ModelEntry): Promise<void>; setThinkingLevel(level: string): Promise<void>; setFastMode(fast: boolean): Promise<void> }`. All three route through `callRpc("sessions.patch", { key, model | thinkingLevel | fastMode })` — the existing operator path. `setModel` throws `LiveSessionModelSwitchError` when `isStreaming` (a mid-run switch throws server-side too).

- [ ] **Step 1: Write the failing test**

```ts
// apps/frontend/src/hooks/__tests__/useSessionModelControls.test.ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import type { ModelEntry } from "@/lib/models";

const callRpc = vi.fn<(method: string, params?: Record<string, unknown>) => Promise<unknown>>(
  async () => ({}),
);
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpcMutation: () => callRpc,
}));
vi.mock("@clerk/nextjs", () => ({
  useUser: (): { user: { id: string } | null } => ({ user: { id: "user_1" } }),
}));

import {
  useSessionModelControls,
  LiveSessionModelSwitchError,
} from "@/hooks/useSessionModelControls";

const model: ModelEntry = {
  id: "arn:aws:bedrock:...:profile/abc.def",
  provider: "amazon-bedrock",
};

beforeEach(() => callRpc.mockClear());

describe("useSessionModelControls", () => {
  it("patches model with the exact provider/id ref against the 4-part key", async () => {
    const sid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301";
    const { result } = renderHook(() =>
      useSessionModelControls("agent_a", sid, false),
    );
    await result.current.setModel(model);
    expect(callRpc).toHaveBeenCalledWith("sessions.patch", {
      key: `agent:agent_a:user_1:${sid}`,
      model: `amazon-bedrock/${model.id}`,
    });
  });

  it("targets the legacy 3-part key when activeSessionId is null", async () => {
    const { result } = renderHook(() =>
      useSessionModelControls("agent_a", null, false),
    );
    await result.current.setThinkingLevel("high");
    expect(callRpc).toHaveBeenCalledWith("sessions.patch", {
      key: "agent:agent_a:user_1",
      thinkingLevel: "high",
    });
  });

  it("throws LiveSessionModelSwitchError and does NOT call RPC mid-run", async () => {
    const { result } = renderHook(() =>
      useSessionModelControls("agent_a", null, true),
    );
    await expect(result.current.setModel(model)).rejects.toBeInstanceOf(
      LiveSessionModelSwitchError,
    );
    expect(callRpc).not.toHaveBeenCalled();
  });

  it("patches fastMode as a boolean", async () => {
    const { result } = renderHook(() =>
      useSessionModelControls("agent_a", null, false),
    );
    await result.current.setFastMode(true);
    expect(callRpc).toHaveBeenCalledWith("sessions.patch", {
      key: "agent:agent_a:user_1",
      fastMode: true,
    });
  });
});
```

- [ ] **Step 2: Run test to verify it fails**
  - `cd apps/frontend && pnpm exec vitest run src/hooks/__tests__/useSessionModelControls.test.ts`
  - Expected: fails — `Failed to resolve import "@/hooks/useSessionModelControls"`.

- [ ] **Step 3: Write minimal implementation**

```ts
// apps/frontend/src/hooks/useSessionModelControls.ts
"use client";

import { useCallback } from "react";
import { useUser } from "@clerk/nextjs";
import { useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { buildChatSessionKey } from "@/lib/sessionKey";
import { buildModelRef, type ModelEntry } from "@/lib/models";

/**
 * Thrown when the user tries to switch the model while a run is live. OpenClaw
 * also rejects a mid-run model change (LiveSessionModelSwitchError), so we gate
 * client-side to keep the UI honest and skip a doomed RPC.
 */
export class LiveSessionModelSwitchError extends Error {
  constructor() {
    super("Stop the current run before switching model.");
    this.name = "LiveSessionModelSwitchError";
  }
}

export interface SessionModelControls {
  setModel: (entry: ModelEntry) => Promise<void>;
  setThinkingLevel: (level: string) => Promise<void>;
  setFastMode: (fast: boolean) => Promise<void>;
}

/**
 * Persist composer selections onto the active session via sessions.patch — the
 * EXISTING gateway operator path (OpenClaw rejects session mutations from
 * webchat connections; the backend connects as operator). No new route.
 */
export function useSessionModelControls(
  agentId: string | null,
  activeSessionId: string | null,
  isStreaming: boolean,
): SessionModelControls {
  const { user } = useUser();
  const callRpc = useGatewayRpcMutation();

  const keyOf = useCallback((): string => {
    if (!agentId || !user?.id) throw new Error("No active agent or user");
    return buildChatSessionKey(agentId, user.id, activeSessionId);
  }, [agentId, user?.id, activeSessionId]);

  const setModel = useCallback(
    async (entry: ModelEntry): Promise<void> => {
      // Idle-only: a mid-run switch throws server-side (LiveSessionModelSwitchError).
      if (isStreaming) throw new LiveSessionModelSwitchError();
      await callRpc("sessions.patch", {
        key: keyOf(),
        model: buildModelRef(entry.provider, entry.id),
      });
    },
    [callRpc, keyOf, isStreaming],
  );

  const setThinkingLevel = useCallback(
    async (level: string): Promise<void> => {
      await callRpc("sessions.patch", { key: keyOf(), thinkingLevel: level });
    },
    [callRpc, keyOf],
  );

  const setFastMode = useCallback(
    async (fast: boolean): Promise<void> => {
      await callRpc("sessions.patch", { key: keyOf(), fastMode: fast });
    },
    [callRpc, keyOf],
  );

  return { setModel, setThinkingLevel, setFastMode };
}
```

- [ ] **Step 4: Run test to verify it passes**
  - `cd apps/frontend && pnpm exec vitest run src/hooks/__tests__/useSessionModelControls.test.ts`
  - Expected: `Test Files 1 passed`, 4 cases green.
  - Note: `callRpc` is typed via `vi.fn<(...)=>Promise<unknown>>` and the Clerk mock returns an annotated shape so CI's `tsc --noEmit` over the test file passes.

- [ ] **Step 5: Commit**
  - `git add apps/frontend/src/hooks/useSessionModelControls.ts apps/frontend/src/hooks/__tests__/useSessionModelControls.test.ts`
  - `git commit -m "feat(chat): useSessionModelControls — sessions.patch model/effort/fastMode, idle-only model switch"`

---

### Task 17: `ModelEffortPill` — combined pill → sectioned Model/Reasoning/Speed menu with provider gating
**Files:**
- Create: `apps/frontend/src/components/chat/ModelEffortPill.tsx`
- Test: `apps/frontend/src/components/chat/__tests__/ModelEffortPill.test.tsx`

**Interfaces:**
- Consumes: `useAgentModels` (Task 15), `useSessionModelControls` + `LiveSessionModelSwitchError` (Task 16), `isReasoningModel` + `providerLabelFor` + `ModelEntry` (Task 13), `Popover*` (`@/components/ui/popover`), `Button` (`@/components/ui/button`).
- Produces: `interface ModelEffortPillProps { agentId: string | null; activeSessionId: string | null; isStreaming: boolean; selectedModelRef: string; thinkingLevel: string; fastMode: boolean; providerChoice: string | null; byoProvider: string | null; onChanged?: () => void }`, `ModelEffortPill(props): JSX.Element`. Effort section hidden when the selected model is non-reasoning; Speed (fast-mode) section rendered only when `providerChoice === "byo_key" && byoProvider === "openai"`; model rows disabled while `isStreaming`.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/components/chat/__tests__/ModelEffortPill.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { ModelEntry } from "@/lib/models";
import type { UseAgentModelsResult } from "@/hooks/useAgentModels";
import type { SessionModelControls } from "@/hooks/useSessionModelControls";

const models: ModelEntry[] = [
  { id: "gpt-5", provider: "openai", name: "GPT-5", available: true, reasoning: true },
  { id: "gpt-4o", provider: "openai", name: "GPT-4o", available: true, reasoning: false },
];

const agentModels = vi.fn<(agentId: string | null) => UseAgentModelsResult>(() => ({
  models,
  defaultModel: "openai/gpt-5",
  isLoading: false,
  usingFallback: false,
}));
vi.mock("@/hooks/useAgentModels", () => ({ useAgentModels: (a: string | null) => agentModels(a) }));

const setModel = vi.fn<(e: ModelEntry) => Promise<void>>(async () => {});
const setThinkingLevel = vi.fn<(l: string) => Promise<void>>(async () => {});
const setFastMode = vi.fn<(f: boolean) => Promise<void>>(async () => {});
vi.mock("@/hooks/useSessionModelControls", () => ({
  useSessionModelControls: (): SessionModelControls => ({ setModel, setThinkingLevel, setFastMode }),
  LiveSessionModelSwitchError: class extends Error {},
}));

import { ModelEffortPill } from "@/components/chat/ModelEffortPill";

const base = {
  agentId: "agent_a",
  activeSessionId: null,
  isStreaming: false,
  selectedModelRef: "openai/gpt-5",
  thinkingLevel: "medium",
  fastMode: false,
  providerChoice: "bedrock_claude" as string | null,
  byoProvider: null as string | null,
};

beforeEach(() => {
  setModel.mockClear();
  setThinkingLevel.mockClear();
  setFastMode.mockClear();
});

describe("ModelEffortPill", () => {
  it("shows the Reasoning section for a reasoning model", () => {
    render(<ModelEffortPill {...base} />);
    fireEvent.click(screen.getByTestId("model-effort-pill"));
    expect(screen.getByText("Reasoning")).toBeInTheDocument();
  });

  it("hides the Reasoning section when the selected model is non-reasoning", () => {
    render(<ModelEffortPill {...base} selectedModelRef="openai/gpt-4o" />);
    fireEvent.click(screen.getByTestId("model-effort-pill"));
    expect(screen.queryByText("Reasoning")).not.toBeInTheDocument();
  });

  it("shows Speed (fast-mode) only for BYO-OpenAI", () => {
    const { rerender } = render(<ModelEffortPill {...base} />);
    fireEvent.click(screen.getByTestId("model-effort-pill"));
    expect(screen.queryByText("Speed")).not.toBeInTheDocument();

    rerender(<ModelEffortPill {...base} providerChoice="byo_key" byoProvider="openai" />);
    expect(screen.getByText("Speed")).toBeInTheDocument();
  });

  it("calls setModel with the exact catalog entry (provider+id) on select", () => {
    render(<ModelEffortPill {...base} selectedModelRef="openai/gpt-4o" />);
    fireEvent.click(screen.getByTestId("model-effort-pill"));
    fireEvent.click(screen.getByRole("button", { name: /GPT-5/ }));
    expect(setModel).toHaveBeenCalledWith(models[0]);
  });

  it("disables model rows while a run is active", () => {
    render(<ModelEffortPill {...base} selectedModelRef="openai/gpt-4o" isStreaming />);
    fireEvent.click(screen.getByTestId("model-effort-pill"));
    expect(screen.getByRole("button", { name: /GPT-5/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /GPT-5/ }));
    expect(setModel).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**
  - `cd apps/frontend && pnpm exec vitest run src/components/chat/__tests__/ModelEffortPill.test.tsx`
  - Expected: fails — `Failed to resolve import "@/components/chat/ModelEffortPill"`.

- [ ] **Step 3: Write minimal implementation**

```tsx
// apps/frontend/src/components/chat/ModelEffortPill.tsx
"use client";

import * as React from "react";
import { ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import { useAgentModels } from "@/hooks/useAgentModels";
import {
  useSessionModelControls,
  LiveSessionModelSwitchError,
} from "@/hooks/useSessionModelControls";
import { isReasoningModel, providerLabelFor, type ModelEntry } from "@/lib/models";

const REASONING_LEVELS = ["off", "low", "medium", "high"] as const;

export interface ModelEffortPillProps {
  agentId: string | null;
  activeSessionId: string | null;
  isStreaming: boolean;
  /** Current session model ref, `${provider}/${id}`. */
  selectedModelRef: string;
  thinkingLevel: string;
  fastMode: boolean;
  providerChoice: string | null;
  byoProvider: string | null;
  onChanged?: () => void;
}

function refOf(entry: ModelEntry): string {
  return `${entry.provider}/${entry.id}`;
}

export function ModelEffortPill({
  agentId,
  activeSessionId,
  isStreaming,
  selectedModelRef,
  thinkingLevel,
  fastMode,
  providerChoice,
  byoProvider,
  onChanged,
}: ModelEffortPillProps) {
  const [open, setOpen] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const { models, defaultModel } = useAgentModels(agentId);
  const { setModel, setThinkingLevel, setFastMode } = useSessionModelControls(
    agentId,
    activeSessionId,
    isStreaming,
  );

  const selected =
    models.find((m) => refOf(m) === selectedModelRef) ??
    models.find((m) => refOf(m) === defaultModel) ??
    models[0];

  const showEffort = selected ? isReasoningModel(selected) : false;
  const showSpeed = providerChoice === "byo_key" && byoProvider === "openai";
  const effortLabel = showEffort && thinkingLevel !== "off" ? ` · ${thinkingLevel}` : "";
  const pillLabel = `${selected?.name ?? "Model"}${effortLabel}`;

  const pick = async (entry: ModelEntry) => {
    if (isStreaming) return; // rows are disabled; guard the handler too
    setError(null);
    try {
      await setModel(entry);
      onChanged?.();
      setOpen(false);
    } catch (err) {
      setError(
        err instanceof LiveSessionModelSwitchError
          ? err.message
          : err instanceof Error
            ? err.message
            : "Failed to set model",
      );
    }
  };

  const pickEffort = async (level: string) => {
    setError(null);
    try {
      await setThinkingLevel(level);
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to set reasoning");
    }
  };

  const toggleFast = async (next: boolean) => {
    setError(null);
    try {
      await setFastMode(next);
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to set speed");
    }
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="sm"
          data-testid="model-effort-pill"
          className="gap-1.5 h-7 px-2.5 rounded-full bg-white border-[#e0dbd0] text-[#5a5549] hover:bg-[#f3efe6] text-xs"
        >
          <span className="truncate max-w-40">{pillLabel}</span>
          <ChevronDown className="h-3 w-3 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        side="top"
        sideOffset={8}
        className="!w-[300px] !p-0 border-[#333] text-white shadow-2xl"
        style={{ background: "#111111", maxHeight: 360 }}
      >
        {/* Model section */}
        <div className="p-2 border-b border-white/10">
          <div className="px-2 py-1 text-[11px] uppercase tracking-wide text-white/40">Model</div>
          <div className="max-h-48 overflow-y-auto">
            {models.map((m) => {
              const isSel = refOf(m) === selectedModelRef;
              const denied = m.available === false;
              return (
                <button
                  key={refOf(m)}
                  type="button"
                  disabled={isStreaming || denied}
                  title={denied ? "Model access denied" : m.id}
                  onClick={() => pick(m)}
                  className={cn(
                    "w-full flex items-center justify-between px-2 py-1.5 rounded text-sm transition-colors",
                    isSel ? "bg-white/10 text-white" : "text-white/70 hover:bg-white/5",
                    (isStreaming || denied) && "opacity-40 cursor-not-allowed",
                  )}
                >
                  <span className="truncate text-left">{m.name ?? m.id}</span>
                  <span className="ml-2 shrink-0 text-[10px] text-white/30">
                    {providerLabelFor(m)}
                  </span>
                </button>
              );
            })}
          </div>
          {isStreaming && (
            <div className="px-2 pt-1 text-[10px] text-white/30">
              Stop the run to switch model.
            </div>
          )}
        </div>

        {/* Reasoning section — only for reasoning models */}
        {showEffort && (
          <div className="p-2 border-b border-white/10">
            <div className="px-2 py-1 text-[11px] uppercase tracking-wide text-white/40">
              Reasoning
            </div>
            <div className="flex gap-1 px-1">
              {REASONING_LEVELS.map((lvl) => (
                <button
                  key={lvl}
                  type="button"
                  onClick={() => pickEffort(lvl)}
                  className={cn(
                    "flex-1 py-1 rounded text-xs capitalize transition-colors",
                    thinkingLevel === lvl
                      ? "bg-white/15 text-white"
                      : "text-white/60 hover:bg-white/5",
                  )}
                >
                  {lvl}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Speed (fast-mode) — only BYO-OpenAI */}
        {showSpeed && (
          <div className="p-2">
            <div className="px-2 py-1 text-[11px] uppercase tracking-wide text-white/40">Speed</div>
            <button
              type="button"
              onClick={() => toggleFast(!fastMode)}
              className="w-full flex items-center justify-between px-2 py-1.5 rounded text-sm text-white/70 hover:bg-white/5"
            >
              <span>Fast mode</span>
              <span
                className={cn(
                  "text-[10px] px-1.5 py-0.5 rounded-full",
                  fastMode
                    ? "bg-emerald-500/15 text-emerald-400"
                    : "bg-white/5 text-white/40",
                )}
              >
                {fastMode ? "On" : "Off"}
              </span>
            </button>
          </div>
        )}

        {error && (
          <div className="p-2 border-t border-white/10 text-[11px] text-red-400">{error}</div>
        )}
      </PopoverContent>
    </Popover>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**
  - `cd apps/frontend && pnpm exec vitest run src/components/chat/__tests__/ModelEffortPill.test.tsx`
  - Expected: `Test Files 1 passed`, 5 cases green.
  - Note: the `agentModels`/`setModel`/etc. mocks are typed against the real `UseAgentModelsResult`/`SessionModelControls`/`ModelEntry` types so CI `tsc --noEmit` over the test file passes.

- [ ] **Step 5: Commit**
  - `git add apps/frontend/src/components/chat/ModelEffortPill.tsx apps/frontend/src/components/chat/__tests__/ModelEffortPill.test.tsx`
  - `git commit -m "feat(chat): ModelEffortPill — sectioned Model/Reasoning/Speed menu with provider gating"`

---

### Task 18: Mount the pill on the composer — `ChatInput` `controls` slot + `AgentChatWindow` wiring
**Files:**
- Modify: `apps/frontend/src/components/chat/ChatInput.tsx` (props `19-48`, destructure `56-71`, return `293-298`, composer bar `430`)
- Modify: `apps/frontend/src/components/chat/AgentChatWindow.tsx` (`ChatInput` render sites `331`, `364`; consumes `activeSessionId` from Phase 2's `useAgentChat`, `useBilling` for provider gating)
- Test: `apps/frontend/src/components/chat/__tests__/ChatInput.controls.test.tsx`

**Interfaces:**
- Consumes: Phase 2's `activeSessionId` off `useAgentChat`, `isStreaming` (existing), `ModelEffortPill` (Task 17), `useBilling` (`provider_choice`/`byo_provider`).
- Produces: `ChatInputProps.controls?: React.ReactNode` rendered in a row above the composer bar. No new hook exports.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/components/chat/__tests__/ChatInput.controls.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@/hooks/useSlashCommands", () => ({
  useSlashCommands: (): { commands: [] } => ({ commands: [] }),
}));
vi.mock("@/lib/analytics", () => ({ capture: () => {} }));

import { ChatInput } from "@/components/chat/ChatInput";

describe("ChatInput controls slot", () => {
  it("renders the controls node above the composer bar", () => {
    render(
      <ChatInput
        onSend={() => {}}
        controls={<div data-testid="pill-slot">pill</div>}
      />,
    );
    expect(screen.getByTestId("pill-slot")).toBeInTheDocument();
  });

  it("renders nothing extra when controls is omitted", () => {
    render(<ChatInput onSend={() => {}} />);
    expect(screen.queryByTestId("pill-slot")).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**
  - `cd apps/frontend && pnpm exec vitest run src/components/chat/__tests__/ChatInput.controls.test.tsx`
  - Expected: first case fails — `controls` is not a prop, so the slot never renders and `getByTestId("pill-slot")` throws "Unable to find an element".

- [ ] **Step 3: Write minimal implementation**

Add the prop to `ChatInputProps` (after `onRetryQueued` at `ChatInput.tsx:47`):

```tsx
  onRetryQueued?: (id: string) => void;
  /**
   * Composer controls (the model · effort pill) rendered in a row directly
   * above the input bar. Kept as a slot so ChatInput stays free of Clerk /
   * billing / gateway coupling — AgentChatWindow supplies the wired pill.
   */
  controls?: React.ReactNode;
}
```

Destructure it in the component signature (after `onRetryQueued,` at `ChatInput.tsx:70`):

```tsx
  onRetryQueued,
  controls,
}: ChatInputProps) {
```

Render it just before the composer bar. Change `ChatInput.tsx:430`:

```tsx
        <div className="relative flex items-end gap-2 border border-[#e0dbd0] rounded-full bg-white px-3 py-2 focus-within:ring-1 focus-within:ring-[#06402B]/20 focus-within:border-[#06402B]/30 transition-all">
```

to:

```tsx
        {controls && (
          <div className="flex items-center gap-2 mb-2 px-1">{controls}</div>
        )}

        <div className="relative flex items-end gap-2 border border-[#e0dbd0] rounded-full bg-white px-3 py-2 focus-within:ring-1 focus-within:ring-[#06402B]/20 focus-within:border-[#06402B]/30 transition-all">
```

Then wire the pill in `AgentChatWindow.tsx`. Add imports near the top (after the `ChatInput` import at `:7`):

```tsx
import { ModelEffortPill } from "./ModelEffortPill";
import { useBilling } from "@/hooks/useBilling";
```

Destructure `activeSessionId` from the Phase-2 `useAgentChat` return (extend the existing destructure block ending at `AgentChatWindow.tsx:251`) and read billing:

```tsx
    activeSessionId,
  } = useAgentChat(agentId, sessionName);
  const { account } = useBilling();
```

Build the pill once and pass it to BOTH `ChatInput` render sites (`:331` and `:364`) via the new `controls` prop:

```tsx
  const modelEffortPill =
    agentId != null ? (
      <ModelEffortPill
        agentId={agentId}
        activeSessionId={activeSessionId}
        isStreaming={isStreaming}
        selectedModelRef=""
        thinkingLevel="medium"
        fastMode={false}
        providerChoice={account?.provider_choice ?? null}
        byoProvider={account?.byo_provider ?? null}
      />
    ) : null;
```

and add `controls={modelEffortPill}` to each `<ChatInput ... />`. (The current selection surface — `selectedModelRef`/`thinkingLevel`/`fastMode` — reads defaults here; `useAgentModels` resolves the real default model inside the pill. Hydrating these from the active `sessions.list` row is a follow-up, out of this phase's scope.)

- [ ] **Step 4: Run test to verify it passes**
  - `cd apps/frontend && pnpm exec vitest run src/components/chat/__tests__/ChatInput.controls.test.tsx`
  - Expected: `Test Files 1 passed`, 2 cases green.
  - Then re-run the existing composer suite to confirm no regression: `cd apps/frontend && pnpm exec vitest run src/components/chat/__tests__/ChatInput.test.tsx` — expected still green.

- [ ] **Step 5: Commit**
  - `git add apps/frontend/src/components/chat/ChatInput.tsx apps/frontend/src/components/chat/AgentChatWindow.tsx apps/frontend/src/components/chat/__tests__/ChatInput.controls.test.tsx`
  - `git commit -m "feat(chat): mount model·effort pill on the composer via ChatInput controls slot"`
