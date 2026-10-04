# Session sharing mod — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make session sharing visible and controllable inside Claude Code: a band above the prompt (shared, live or transcript, who watches and types, join requests with Allow/Decline, Pause), teammate rows that name the sender, and refused config writes while a teammate's request runs.

**Architecture:** A Claude Code mod (`lightbulb-sessions`) runs inside each Claude session and talks only to the Lightbulb desktop app over a uid-checked Unix socket (`mod.sock`), which maps the calling pid to its mirrored session. The backend pushes presence (viewers, typists, join requests, paused) to the Mac on the heartbeat ack and a `local.session.presence` frame, and takes the Mac's terminal decisions through one device-authed route. The Mac freezes remote typing for a session while a join request is pending, so a teammate's keys can never press Allow.

**Tech Stack:** Python 3 / FastAPI / pytest (backend); Rust / tokio / Tauri 2 (desktop); TypeScript Claude Code mods API v2.1.287+ (`claude plugin test`); React / vitest (frontend).

**Spec:** `docs/superpowers/specs/2026-10-04-session-sharing-mod.md`

## Global Constraints

- Worktree: `/Users/prasiddhaparthsarthy/Desktop/lightbulb-session-mods`, branch `feat/session-sharing-mods`, off `origin/main` (ecafdb253).
- Do NOT edit `apps/desktop/src-tauri/src/cli.rs`, `apps/desktop/src-tauri/src/session_start.rs`, status wording, or `ExplicitSharingNotice.tsx` (owned by other sessions).
- The plugin adds no MCP server, no settings hooks, and nothing that reads `CLAUDE_PLUGIN_ROOT`. Existing hooks, wake hook, skill and the `lightbulb` MCP entry stay untouched.
- The model must still receive the labelled teammate text unchanged; rows are redrawn for the person only.
- An older desktop drops its relay connection on an unknown frame: send `local.session.presence` only to a Mac whose row's `deviceFeatures` contains `sessionPresence`.
- Owner labels (exact, from `local_sessions.rs`): owner header `[Sent from Lightbulb by you]`, next line begins `[Lightbulb verified sender:`; teammate header begins `[Lightbulb teammate contribution`; teammate lines that could spoof are prefixed `> [Lightbulb quoted] `; owner sender handle `lightbulb-owner`.
- Socket dir: `/private/tmp/lightbulb-sessions-<uid>/` (`enrollment::hook_path()`'s parent); the new socket is `mod.sock` there.
- Mods need Claude Code ≥ 2.1.287. A failed plugin install is logged and never blocks sharing.
- Tests: run only the touched test files locally; CI runs the full suites. Check exit codes, never `| tail`. Mod tests run with `claude plugin test` (CI has no Claude CLI; the mod's tests are a local gate).
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Do not push.

## Review Focus

1. A pending join request answered in the WEB app must lift the Mac's input freeze promptly (the `answered` push), or an admitted typist stays frozen. Test in Task 1.
2. Two Claude sessions mirrored for one pid, or a pid that is not a mirrored `claude` process (a Codex process, an ended mirror): `mod.sock` must answer 404, never another session's state. Test in Task 4.
3. A teammate's message whose body contains `[Sent from Lightbulb by you]` (quoted by the app) must still classify as a teammate for the guard. Test in Task 6.
4. The desktop app not running, or `mod.sock` missing: the mod shows nothing and never throws into a render. Test in Task 5.
5. A heartbeat ack with many sessions and long names must stay under `MAX_ENVELOPE_BYTES` (presence trimmed, heartbeat never fails). Test in Task 1.

---

### Task 1: Backend presence (ack, push frame, `inputPaused`)

**Files:**
- Create: `apps/backend/core/services/session_presence.py`
- Modify: `apps/backend/core/services/local_session_relay.py` (FEATURES list ~L95; `DeviceHeartbeat` ~L115; heartbeat ack chain ~L605)
- Modify: `apps/backend/core/services/local_sessions.py` (`LocalSession` model ~L235; `_session_public` ~L625; `request_join` ~L1891; `decide_join` ~L1912; `set_sharing` ~L1112)
- Modify: `packages/shared/src/messaging/types.ts` (LocalSession type ~L392)
- Test: `apps/backend/tests/unit/services/test_session_presence.py`

**Interfaces:**
- Produces: `session_presence.FEATURE = "sessionPresence"`; `async entry(row) -> dict` → `{"sessionId","generation","paused":bool,"viewers":[{"userId","name","canType":bool,"typing":bool}],"joinRequests":[{"userId","name","requestId"}]}`; `pending_joins(row) -> dict[str,str]`; `async ack_with_presence(ack, owner, session_ids) -> dict` (adds `presence: [entry…]`); `async push(row) -> None`. Public session gains `inputPaused: bool`.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_session_presence.py
import json
from unittest.mock import AsyncMock

import pytest

from core.services import local_sessions as sessions, session_presence as presence

NOW = 1_000_000


def _row(**over):
    row = {
        "ownerId": "org_a", "sessionId": "11111111-1111-4111-8111-111111111111", "generation": "g1",
        "humanUserId": "user_owner", "deviceId": "mac_a", "connectionId": "c1",
        "deviceFeatures": ["sessionPresence"], "allowedSenders": ["user_aden"],
    }
    return {**row, **over}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(presence, "_clock", lambda: NOW)
    monkeypatch.setattr(presence, "human_display_name", AsyncMock(side_effect=lambda u: u.removeprefix("user_").title()))


def _rows(viewers=(), typed=()):
    async def query(pk, begins_with):
        if begins_with == "VIEWER#":
            return [{"userId": u, "expiresAt": NOW + 30} for u in viewers] + [{"userId": "user_old", "expiresAt": NOW - 1}]
        return [{"userId": u, "lastAt": NOW - 2} for u in typed] + [{"userId": "user_mira", "lastAt": NOW - 60}]
    return query


@pytest.mark.asyncio
async def test_entry_names_viewers_typists_and_pending_requests(monkeypatch):
    monkeypatch.setattr(presence.store, "_query", _rows(viewers=["user_aden", "user_mira", "user_owner"], typed=["user_aden"]))
    row = _row(joinRequests={"user_zoe": {"state": "pending", "requestId": "r1"}, "user_bo": {"state": "declined", "requestId": "r0"}})
    out = await presence.entry(row)
    assert out["paused"] is False
    assert out["viewers"] == [
        {"userId": "user_aden", "name": "Aden", "canType": True, "typing": True},
        {"userId": "user_mira", "name": "Mira", "canType": False, "typing": False},
    ]
    assert out["joinRequests"] == [{"userId": "user_zoe", "name": "Zoe", "requestId": "r1"}]


@pytest.mark.asyncio
async def test_ack_lists_only_sessions_with_something_to_show_and_stays_in_the_envelope(monkeypatch):
    monkeypatch.setattr(presence.store, "_query", _rows(viewers=["user_aden"]))
    rows = {f"s{i}": _row(sessionId=f"s{i}", generation="g" * 100) for i in range(24)}
    rows["quiet"] = _row(sessionId="quiet")
    monkeypatch.setattr(presence.repo, "get_session", AsyncMock(side_effect=lambda owner, sid: rows[sid]))
    monkeypatch.setattr(presence.human_display_name, "side_effect", lambda u: "N" * 120)
    quiet_query = _rows()

    async def query(pk, begins_with):
        return await (quiet_query if pk.endswith("quiet") else _rows(viewers=["user_aden"]))(pk, begins_with)

    monkeypatch.setattr(presence.store, "_query", query)
    ack = await presence.ack_with_presence({"type": "local.device.heartbeat.ack"}, "org_a", list(rows))
    assert "quiet" not in [e["sessionId"] for e in ack["presence"]]
    assert len(json.dumps(ack, separators=(",", ":")).encode()) <= sessions.MAX_ENVELOPE_BYTES


@pytest.mark.asyncio
async def test_ack_survives_a_read_failure(monkeypatch):
    monkeypatch.setattr(presence.repo, "get_session", AsyncMock(side_effect=RuntimeError("ddb")))
    ack = {"type": "local.device.heartbeat.ack"}
    assert await presence.ack_with_presence(ack, "org_a", ["s1"]) == ack


@pytest.mark.asyncio
async def test_push_sends_an_empty_entry_once_the_request_is_answered(monkeypatch):
    monkeypatch.setattr(presence.store, "_query", _rows())
    send = AsyncMock(return_value=True)
    from core.services import local_session_relay
    monkeypatch.setattr(local_session_relay.get_local_session_relay().management, "send_message_async", send)
    await presence.push(_row(joinRequests={}))
    frame = send.await_args.args[1]
    assert frame["type"] == "local.session.presence" and frame["joinRequests"] == [] and frame["viewers"] == []


@pytest.mark.asyncio
async def test_push_skips_a_mac_without_the_feature(monkeypatch):
    send = AsyncMock()
    from core.services import local_session_relay
    monkeypatch.setattr(local_session_relay.get_local_session_relay().management, "send_message_async", send)
    await presence.push(_row(deviceFeatures=["liveTerminal"]))
    send.assert_not_awaited()


def test_input_paused_is_public_only_with_a_pending_request_on_a_presence_mac(monkeypatch):
    monkeypatch.setattr(sessions, "session_terminal", lambda row: "held")
    base = _row(onlineUntil=9_999_999_999, capabilitiesVerified=True, provider="claude", status="idle", eventSeq=1)
    pending = {"joinRequests": {"user_zoe": {"state": "pending", "requestId": "r1"}}}
    assert sessions._session_public({**base, **pending}, "user_aden").input_paused is True
    assert sessions._session_public(base, "user_aden").input_paused is False
    assert sessions._session_public({**base, **pending, "deviceFeatures": []}, "user_aden").input_paused is False
```

If `_session_public` needs more row fields to validate, copy the minimal row from an existing `_session_public` test in `tests/unit/services/test_local_sessions.py` (search `_session_public(`) and merge it into `base`.

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_presence.py -q; echo exit=$?`
Expected: FAIL, `ModuleNotFoundError: core.services.session_presence`.

- [ ] **Step 3: Implement `session_presence.py`**

```python
"""What the owner's Mac shows in its Claude terminal about one shared session (spec
2026-10-04-session-sharing-mod §4.2): who watches, who may type, who is typing, pending join
requests, paused. A pending request also freezes remote typing on the Mac, so the request
being answered is pushed at once; everything else rides the heartbeat ack."""

from __future__ import annotations

import json
import logging
import time

from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions
from core.services.messaging import human_display_name

logger = logging.getLogger(__name__)

FEATURE = "sessionPresence"
TYPING_WINDOW_S = 10
_clock = time.time


def pending_joins(row: dict) -> dict[str, str]:
    """Each person still waiting for the owner's answer, with the request id they asked under."""
    if row.get("endedAt") is not None:
        return {}
    return {u: r["requestId"] for u, r in (row.get("joinRequests") or {}).items() if r.get("state") == "pending"}


async def _name(user: str) -> str:
    try:
        return await human_display_name(user)
    except Exception:  # noqa: BLE001 -- a name is decoration; the entry must still go out
        return "A teammate"


async def entry(row: dict) -> dict:
    now = int(_clock())
    pk = repo.session_pk(row["ownerId"], row["sessionId"])
    viewers = sorted(
        r["userId"]
        for r in await store._query(pk, begins_with="VIEWER#")
        if int(r.get("expiresAt", 0)) > now and r["userId"] != row["humanUserId"]
    )
    # ponytail: reads every TYPED# burst row (30-day TTL); add a lastAt index if a session's typing history grows large.
    typing = {r["userId"] for r in await store._query(pk, begins_with="TYPED#") if int(r.get("lastAt", 0)) > now - TYPING_WINDOW_S}
    return {
        "sessionId": row["sessionId"],
        "generation": row["generation"],
        "paused": bool(row.get("unshared")),
        "viewers": [
            {"userId": u, "name": await _name(u), "canType": sessions.may_control(row, u), "typing": u in typing}
            for u in viewers
        ],
        "joinRequests": [
            {"userId": u, "name": await _name(u), "requestId": rid} for u, rid in sorted(pending_joins(row).items())
        ],
    }


async def ack_with_presence(ack: dict, owner: str, session_ids: list[str]) -> dict:
    """The backstop: the heartbeat ack names presence for this Mac's sessions. Never costs the heartbeat."""
    try:
        out = []
        for sid in session_ids:
            row = await repo.get_session(owner, sid)
            if row is None or row.get("endedAt") is not None:
                continue
            e = await entry(row)
            if e["viewers"] or e["joinRequests"] or e["paused"]:
                out.append(e)
    except Exception:  # noqa: BLE001
        logger.warning("Session presence unavailable")
        return ack
    while True:
        candidate = {**ack, "presence": out}
        if len(json.dumps(candidate, separators=(",", ":")).encode()) <= sessions.MAX_ENVELOPE_BYTES or not out:
            return candidate
        out = out[:-1]


async def push(row: dict) -> None:
    """Best effort, now: a join request was made or answered, or sharing paused or resumed.
    Always a full entry, an empty one included, so the Mac can lift its input freeze."""
    if FEATURE not in row.get("deviceFeatures", ()):
        return
    from core.services.local_session_relay import check_envelope, get_local_session_relay  # imports this module

    try:
        envelope = {"type": "local.session.presence", **await entry(row)}
        check_envelope(envelope)
        await get_local_session_relay().management.send_message_async(row["connectionId"], envelope)
    except Exception as exc:  # noqa: BLE001 -- the next ack carries the same news
        logger.info("session presence push failed error=%s", type(exc).__name__)
```

- [ ] **Step 4: Wire it in**

`local_session_relay.py`: append `"sessionPresence"` to `FEATURES`; add to `DeviceHeartbeat` beside `remote_input`:

```python
    session_presence: bool = False
```

In the heartbeat branch, wrap the existing `ack_with_watching(...)` result:

```python
                    if message.session_presence:
                        response = await session_presence.ack_with_presence(
                            response, owner, [str(s.session_id) for s in message.sessions]
                        )
```

(Import `session_presence` at the top of the module beside `session_screen`. Use the `HeartbeatSession` attribute that holds the Lightbulb session id; check the class around L60-90.)

`local_sessions.py`:
- `LocalSession`: add `input_paused: bool = False`.
- `_session_public`, before `return`:

```python
    from core.services import session_presence  # lazy: it imports this module

    public["inputPaused"] = bool(
        session_presence.pending_joins(row)
        and session_presence.FEATURE in row.get("deviceFeatures", ())
        and public["terminal"] is not None
    )
```

- `request_join`: after `await _notify_join_request(row, me)` add `await _push_presence(row)`.
- `decide_join`: after `await _notify_join_answer(...)` add `await _push_presence(row)`.
- `set_sharing`: after the successful `put_session` (inside the loop, after `row = updated`) add `await _push_presence(row)`.
- Add near `_session_notice`:

```python
async def _push_presence(row: dict) -> None:
    from core.services import session_presence  # imports this module

    await session_presence.push(row)
```

`packages/shared/src/messaging/types.ts`, in the LocalSession type beside `pendingJoins?`:

```ts
  /** A join request is pending and the owner's Mac has paused remote typing until it is answered. */
  inputPaused?: boolean;
```

- [ ] **Step 5: Run tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_presence.py tests/unit/services/test_local_sessions.py tests/unit/services/test_local_session_relay.py tests/unit/services/test_session_screen.py -q; echo exit=$?` and `uv run ruff check core/services/session_presence.py core/services/local_sessions.py core/services/local_session_relay.py`
Expected: all pass, exit=0.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/session_presence.py apps/backend/core/services/local_sessions.py apps/backend/core/services/local_session_relay.py apps/backend/tests/unit/services/test_session_presence.py packages/shared/src/messaging/types.ts
git commit -m "feat(sessions): presence for the owner's Mac (viewers, typists, join requests, paused)"
```

---

### Task 2: Backend decision door

**Files:**
- Modify: `apps/backend/core/services/native_session_tools.py`
- Modify: `apps/backend/routers/messaging.py` (beside `/messaging/native-session-tools`; `device_human` docstring ~L114)
- Test: `apps/backend/tests/unit/services/test_native_session_tools.py`

**Interfaces:**
- Consumes: `local_sessions.decide_join(auth, sid, user_id, decision, request_id=)`, `local_sessions.set_sharing(auth, sid, shared)` (both push presence after Task 1).
- Produces: `POST /api/v1/messaging/native-session-decisions` body `{"sessionId","generation","decision":"allow|decline|pause|unpause","userId"?,"requestId"?}` → public `LocalSession` JSON; errors as `ApiProblem` JSON with `code`.

- [ ] **Step 1: Write failing tests** (append to `test_native_session_tools.py`)

```python
def _decision_device():
    return MachineIdentity(user_id="user_a", org_id="org_a", owner_id="org_a", kind="node",
                           auth_kind="device", client_id="openclaw-macos", device_id="mac_a")


def _decision_row(**over):
    return {"ownerId": "org_a", "sessionId": "11111111-1111-4111-8111-111111111111", "humanUserId": "user_a",
            "deviceId": "mac_a", "generation": "g1", "connectionId": "connection_a", **over}


def _connected(monkeypatch, device_id="mac_a"):
    from core.services import local_session_relay
    relay = local_session_relay.get_local_session_relay()
    monkeypatch.setattr(relay, "connect", AsyncMock(return_value=({"device_id": device_id, "user_id": "user_a"}, None)))


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [
    None,
    _decision_row(humanUserId="user_b"),
    _decision_row(deviceId="mac_b"),
    _decision_row(generation="g0"),
    _decision_row(endedAt=1),
])
async def test_decision_refuses_anything_but_this_macs_current_session(monkeypatch, row):
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=row))
    _connected(monkeypatch)
    body = tools.NativeDecisionRequest(session_id=uuid.UUID("11111111-1111-4111-8111-111111111111"), generation="g1", decision="pause")
    with pytest.raises(ApiProblem):
        await tools.decide(_decision_device(), body)


@pytest.mark.asyncio
async def test_decision_refuses_a_stale_connection(monkeypatch):
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=_decision_row()))
    _connected(monkeypatch, device_id="mac_other")
    body = tools.NativeDecisionRequest(session_id=uuid.UUID("11111111-1111-4111-8111-111111111111"), generation="g1", decision="pause")
    with pytest.raises(ApiProblem):
        await tools.decide(_decision_device(), body)


@pytest.mark.asyncio
async def test_allow_and_pause_run_as_the_owner(monkeypatch):
    monkeypatch.setattr(tools.sessions_repo, "get_session", AsyncMock(return_value=_decision_row()))
    _connected(monkeypatch)
    decide_join = AsyncMock(return_value="joined")
    set_sharing = AsyncMock(return_value="paused")
    monkeypatch.setattr(tools.local_sessions, "decide_join", decide_join)
    monkeypatch.setattr(tools.local_sessions, "set_sharing", set_sharing)
    sid = uuid.UUID("11111111-1111-4111-8111-111111111111")
    allow = tools.NativeDecisionRequest(session_id=sid, generation="g1", decision="allow", user_id="user_z", request_id="r1")
    assert await tools.decide(_decision_device(), allow) == "joined"
    auth = decide_join.await_args.args[0]
    assert (auth.user_id, auth.org_id) == ("user_a", "org_a")
    assert decide_join.await_args.args[1:4] == (str(sid), "user_z", "allow")
    assert decide_join.await_args.kwargs == {"request_id": "r1"}
    pause = tools.NativeDecisionRequest(session_id=sid, generation="g1", decision="pause")
    assert await tools.decide(_decision_device(), pause) == "paused"
    assert set_sharing.await_args.args[1:] == (str(sid), False)


@pytest.mark.parametrize("fields", [
    {"decision": "allow"},
    {"decision": "decline", "user_id": "user_z"},
    {"decision": "pause", "user_id": "user_z", "request_id": "r1"},
])
def test_decision_body_names_a_request_exactly_when_it_answers_one(fields):
    with pytest.raises(ValueError):
        tools.NativeDecisionRequest(session_id=uuid.uuid4(), generation="g1", **fields)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_native_session_tools.py -q -k decision; echo exit=$?`
Expected: FAIL, `AttributeError: ... NativeDecisionRequest`.

- [ ] **Step 3: Implement** (in `native_session_tools.py`, after `_human`)

```python
class NativeDecisionRequest(ContractModel):
    """The owner's own Mac answers from the Claude terminal (spec 2026-10-04-session-sharing-mod §4.3)."""

    session_id: uuid.UUID
    generation: str = Field(min_length=1, max_length=128)
    decision: Literal["allow", "decline", "pause", "unpause"]
    user_id: str | None = Field(default=None, max_length=128)
    request_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def names_one_request(self):
        answers = self.decision in ("allow", "decline")
        if answers != (self.user_id is not None) or answers != (self.request_id is not None):
            raise ValueError("allow and decline name one request; pause and unpause name none")
        return self


async def decide(device: MachineIdentity, body: NativeDecisionRequest) -> local_sessions.LocalSession:
    """The one write a device token reaches (owner approval 2026-10-04): only for the owner's own
    session on this Mac, at its current generation, while this Mac holds its relay connection.
    The Mac freezes remote typing while a request is pending, so only the owner can press it."""
    sid = str(body.session_id)
    row = await sessions_repo.get_session(device.owner_id, sid)
    if (
        not row
        or row.get("humanUserId") != device.user_id
        or row.get("deviceId") != device.device_id
        or row.get("generation") != body.generation
        or row.get("endedAt") is not None
    ):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Not this Mac's current session.")
    from core.services.local_session_relay import get_local_session_relay

    connection, _ = await get_local_session_relay().connect(row.get("connectionId") or "")
    if connection.get("device_id") != device.device_id or connection.get("user_id") != device.user_id:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "This Mac is not connected.")
    auth = _human(device)
    if body.decision in ("pause", "unpause"):
        return await local_sessions.set_sharing(auth, sid, body.decision == "unpause")
    return await local_sessions.decide_join(auth, sid, body.user_id, body.decision, request_id=body.request_id)
```

`routers/messaging.py`, after the `prepare-send` route:

```python
@router.post("/messaging/native-session-decisions", response_model=local_sessions.LocalSession)
async def native_session_decision(
    body: native_session_tools.NativeDecisionRequest,
    device=Depends(native_session_device),
):
    """The owner answers a join request or pauses sharing from their Claude terminal."""
    return await native_session_tools.decide(device, body)
```

Change `device_human`'s docstring to: `"""The paired Mac's enrolled human. Read routes only. The one device-token write is native-session-decisions (owner approval 2026-10-04), fenced in native_session_tools.decide."""`

- [ ] **Step 4: Run tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_native_session_tools.py -q; echo exit=$?` and `uv run ruff check core/services/native_session_tools.py routers/messaging.py`
Expected: pass, exit=0.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/native_session_tools.py apps/backend/routers/messaging.py apps/backend/tests/unit/services/test_native_session_tools.py
git commit -m "feat(sessions): the owner's Mac answers join requests and pauses sharing from the terminal"
```

---

### Task 3: Desktop presence store and input freeze

**Files:**
- Create: `apps/desktop/src-tauri/src/local_sessions/presence.rs`
- Modify: `apps/desktop/src-tauri/src/local_sessions.rs` (add `mod presence;` beside the other `mod` lines)
- Modify: `apps/desktop/src-tauri/src/local_sessions/transport.rs` (feature read ~L1378; heartbeat ack ~L1453; frame match ~L1556; `device_heartbeats_with_controls` ~L2135 and its callers)
- Modify: `apps/desktop/src-tauri/src/local_sessions/remote_input.rs` (`admit`)

**Interfaces:**
- Produces (`presence.rs`): `pub(super) struct Entry { pub paused: bool, pub generation: String, pub viewers: Vec<Viewer>, pub join_requests: Vec<JoinRequest> }`; `Viewer { user_id, name, can_type: bool, typing: bool }`; `JoinRequest { user_id, name, request_id }` (all `#[serde(rename_all = "camelCase")]`, `Serialize + Deserialize + Clone`); `pub(super) fn offer_ack(list: &Value)`; `pub(super) fn offer_frame(body: &Value)`; `pub(super) fn get(sid: &str) -> Option<Entry>`; `pub(super) fn frozen(sid: &str) -> bool` (true when a join request is pending); `pub(super) fn set(sid: &str, entry: Entry)`.

- [ ] **Step 1: Write failing tests** (bottom of `presence.rs`)

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const SID: &str = "11111111-1111-4111-8111-111111111111";

    fn entry(requests: serde_json::Value) -> serde_json::Value {
        json!({"sessionId":SID,"generation":"g1","paused":false,
               "viewers":[{"userId":"u1","name":"Aden","canType":true,"typing":false}],
               "joinRequests":requests})
    }

    #[test]
    fn presence_frame_with_a_request_freezes_and_an_empty_one_lifts() {
        offer_frame(&entry(json!([{"userId":"u2","name":"Mira","requestId":"r1"}])));
        assert!(frozen(SID));
        assert_eq!(get(SID).unwrap().viewers[0].name, "Aden");
        offer_frame(&entry(json!([])));
        assert!(!frozen(SID));
    }

    #[test]
    fn presence_ack_replaces_every_session_and_forgets_the_unlisted() {
        let other = "22222222-2222-4222-8222-222222222222";
        offer_frame(&json!({"sessionId":other,"generation":"g1","paused":true,"viewers":[],"joinRequests":[]}));
        offer_ack(&json!([entry(json!([]))]));
        assert!(get(other).is_none());
        assert!(get(SID).is_some());
    }

    #[test]
    fn presence_ignores_a_bad_session_id_and_absent_lists() {
        offer_frame(&json!({"sessionId":"nope","generation":"g1","paused":false,"viewers":[],"joinRequests":[]}));
        assert!(get("nope").is_none());
        offer_ack(&serde_json::Value::Null); // an older backend: nothing changes, nothing panics
    }
}
```

Tests share one global map; give each test its own session id if they interfere (`cargo test` runs them in parallel).

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/desktop/src-tauri && cargo test --lib -- presence; echo exit=$?`
Expected: compile error (module missing).

- [ ] **Step 3: Implement `presence.rs`**

```rust
//! Who watches, who may type, pending join requests and pause, per session, as the backend last
//! said (heartbeat ack `presence`, or a `local.session.presence` frame). Read by `mod_socket`
//! for the Claude terminal's band. A pending join request freezes remote typing for that session
//! (spec 2026-10-04-session-sharing-mod §2.4): the request is stored before the mod can read it,
//! so no teammate key can press Allow.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::sync::Mutex;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub(super) struct Viewer {
    pub user_id: String,
    pub name: String,
    pub can_type: bool,
    pub typing: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub(super) struct JoinRequest {
    pub user_id: String,
    pub name: String,
    pub request_id: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub(super) struct Entry {
    pub generation: String,
    pub paused: bool,
    pub viewers: Vec<Viewer>,
    pub join_requests: Vec<JoinRequest>,
}

static PRESENCE: Mutex<BTreeMap<String, Entry>> = Mutex::new(BTreeMap::new());

fn parse(body: &Value) -> Option<(String, Entry)> {
    let sid = body["sessionId"].as_str().filter(|s| uuid::Uuid::parse_str(s).is_ok())?.to_owned();
    let entry = serde_json::from_value(body.clone()).ok()?;
    Some((sid, entry))
}

/// A heartbeat ack's `presence`: the whole picture for this Mac. Absent (an older backend or a
/// failed read) leaves what we have.
pub(super) fn offer_ack(list: &Value) {
    let Some(list) = list.as_array() else { return };
    let fresh: BTreeMap<_, _> = list.iter().take(64).filter_map(parse).collect();
    if let Ok(mut map) = PRESENCE.lock() {
        *map = fresh;
    }
}

/// A `local.session.presence` frame: one session, now. Never answered.
pub(super) fn offer_frame(body: &Value) {
    if let Some((sid, entry)) = parse(body) {
        set(&sid, entry);
    }
}

pub(super) fn set(sid: &str, entry: Entry) {
    if let Ok(mut map) = PRESENCE.lock() {
        map.insert(sid.to_owned(), entry);
    }
}

pub(super) fn get(sid: &str) -> Option<Entry> {
    PRESENCE.lock().ok()?.get(sid).cloned()
}

/// Remote typing is dropped while the owner has a join request to answer.
pub(super) fn frozen(sid: &str) -> bool {
    get(sid).is_some_and(|e| !e.join_requests.is_empty())
}
```

- [ ] **Step 4: Wire transport and freeze**

`transport.rs`:
- Beside `relay_reads_terminal` declaration and its feature read, add `relay_reads_presence` read from `"sessionPresence"` exactly like the others (same `let mut` declaration site and same ACK line).
- Pass `relay_reads_presence` into `device_heartbeats_with_controls` as a new last parameter `presence: bool` and in `envelope`: `if presence { frame["sessionPresence"] = json!(true); }`. Update every caller and the existing tests that call it (add `false`).
- In the `local.device.heartbeat.ack` arm, after `super::screen::offer(&body["watching"]);`: `super::presence::offer_ack(&body["presence"]);`
- In the frame match, before `_ => return Err("unsupported session frame".into())`:

```rust
                        // Who watches, who may type, join requests, pause. Never answered.
                        Some("local.session.presence") => super::presence::offer_frame(&body),
```

`remote_input.rs`, in `admit`, after `let (sid, ctl) = relay_of(body)?;`:

```rust
    // A join request is waiting for the owner: their band has Allow on it, and a teammate's
    // key on this pty could press it. Dropped, not queued.
    if super::presence::frozen(&sid) {
        return None;
    }
```

Add a test in `remote_input.rs`'s test module that sets a frozen entry via `super::super::presence::set(sid, Entry{… join_requests: vec![…]})` and asserts `admit` returns `None` for a frame that otherwise passes (follow the existing `admit` tests there for building the frame and `relay_of` closure).

- [ ] **Step 5: Run tests**

Run: `cd apps/desktop/src-tauri && cargo check --tests && cargo test --lib -- presence remote_input transport; echo exit=$?`
Expected: pass (3 pre-existing process-spawn timing tests may fail on this Mac; compare with `git stash` if unsure).

- [ ] **Step 6: Commit**

```bash
git add apps/desktop/src-tauri/src/local_sessions/presence.rs apps/desktop/src-tauri/src/local_sessions.rs apps/desktop/src-tauri/src/local_sessions/transport.rs apps/desktop/src-tauri/src/local_sessions/remote_input.rs
git commit -m "feat(desktop): session presence from the relay; a pending join request freezes remote typing"
```

---

### Task 4: Desktop `mod.sock`

**Files:**
- Create: `apps/desktop/src-tauri/src/local_sessions/mod_socket.rs`
- Modify: `apps/desktop/src-tauri/src/local_sessions.rs` (`mod mod_socket;`; spawn `mod_socket::serve` wherever `native_tools::serve` is spawned — `grep -n "native_tools::serve" -r src`)
- Modify: `apps/desktop/src-tauri/src/local_sessions/native_tools.rs` (make `bind`'s socket-guard reusable: change `fn bind()` to `pub(super) fn bind(name: &str)` and pass `"tool.sock"` at its call site; make `peer_pid` and `SocketGuard` `pub(super)`)

**Interfaces:**
- Consumes: `presence::get`, `presence::set`, `presence::Entry` (Task 3); `native_tools::{bind, peer_pid}`; `Mirror` and `private_read`, `process` (as `native_tools::resolve_source` uses them); backend route from Task 2.
- Produces: socket `/private/tmp/lightbulb-sessions-<uid>/mod.sock`, HTTP/1.1:
  - `GET /v1/state` → `200 {"sessionId","paused","terminal":"held"|"attach"|null,"viewers":[…],"joinRequests":[…],"approvalWaiting":bool}` or `404 {"error":"not_shared"}`.
  - `POST /v1/decide` body `{"decision","userId"?,"requestId"?}` → `200 <state>` or `409 {"error":code,"message":text}`.

- [ ] **Step 1: Write failing tests** (bottom of `mod_socket.rs`)

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mod_socket_parses_a_bounded_http_request() {
        let raw = b"POST /v1/decide HTTP/1.1\r\nHost: x\r\nContent-Length: 20\r\n\r\n{\"decision\":\"pause\"}";
        let (method, path, body) = parse_request(raw).unwrap();
        assert_eq!((method.as_str(), path.as_str()), ("POST", "/v1/decide"));
        assert_eq!(body["decision"], "pause");
        assert!(parse_request(b"GET /v1/state HTTP/1.1\r\n\r\n").is_some());
        assert!(parse_request(b"garbage").is_none());
    }

    #[test]
    fn mod_socket_state_shape_has_no_presence_until_the_backend_sends_it() {
        let state = state_json("11111111-1111-4111-8111-111111111111", Some("held"), None, false);
        assert_eq!(state["paused"], false);
        assert_eq!(state["viewers"], serde_json::json!([]));
        assert_eq!(state["terminal"], "held");
    }

    #[test]
    fn mod_socket_refuses_a_decision_body_that_names_no_request() {
        assert!(decision_body("sid", "g1", &serde_json::json!({"decision":"allow"})).is_err());
        assert!(decision_body("sid", "g1", &serde_json::json!({"decision":"reboot"})).is_err());
        let ok = decision_body("sid", "g1", &serde_json::json!({"decision":"allow","userId":"u","requestId":"r"})).unwrap();
        assert_eq!(ok["generation"], "g1");
    }

    #[test]
    fn mod_socket_resolves_no_session_for_a_pid_that_is_not_mirrored() {
        let dir = tempfile::tempdir().unwrap();
        assert!(resolve(dir.path(), std::process::id()).is_none());
    }
}
```

(Use whatever temp-dir helper the crate's tests already use — `grep -n "tempdir" src/local_sessions/*.rs | head -3`.)

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/desktop/src-tauri && cargo test --lib -- mod_socket; echo exit=$?`
Expected: compile error.

- [ ] **Step 3: Implement `mod_socket.rs`**

```rust
//! The Claude Code mod's door to this app (spec 2026-10-04-session-sharing-mod §4.1). The mod
//! runs inside the `claude` process, so the peer pid IS the mirrored process: no parent step.
//! It reads the session's sharing state and sends the owner's terminal decisions. Answers only
//! a pid that is exactly one live, unrestricted, mirrored Claude session; anything else is 404.

use super::*;
use serde_json::{json, Value};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;

const MAX_REQUEST: usize = 8 * 1024;

pub(super) struct Caller {
    pub session_id: String,
    pub generation: String,
    pub terminal: Option<&'static str>,
    pub approval_waiting: bool,
}

/// The one mirrored, live Claude session whose process is `pid`.
fn resolve(scope: &Path, pid: u32) -> Option<Caller> {
    let me = process(pid).ok()?;
    let mut found = Vec::new();
    for entry in fs::read_dir(scope).ok()?.flatten() {
        let path = entry.path();
        let key = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
        if path.extension().is_none_or(|e| e != "json") || key.len() != 64 || !key.bytes().all(|b| b.is_ascii_hexdigit()) {
            continue;
        }
        let Ok(mirror) = private_read::<Mirror>(&path) else { continue };
        if mirror.ended_at.is_some()
            || mirror.native.restriction.is_some()
            || mirror.native.provider != "claude"
            || mirror.native.process != me
        {
            continue;
        }
        if let Some(sid) = &mirror.session_id {
            found.push(Caller {
                session_id: sid.clone(),
                generation: mirror.generation.clone(),
                terminal: terminal_of(&mirror),
                approval_waiting: approval_waiting(&mirror),
            });
        }
    }
    (found.len() == 1).then(|| found.remove(0))
}
```

`terminal_of(&Mirror) -> Option<&'static str>`: return the same value the heartbeat sends as `terminal` for this mirror (`grep -n '"terminal"' src/local_sessions/transport.rs` and reuse that function; map its `"none"` to `None`).
`approval_waiting(&Mirror) -> bool`: true when the mirror holds a prompt held for a remote answer — the field the hook's held datagram sets (`grep -n "build_held_datagram\|held" src/local_sessions.rs src/local_sessions/lifecycle.rs` to find where a `Held` is stored on the mirror; the heartbeat sends it as `pendingApproval`). Reuse the existing field; add nothing.

```rust
pub(super) fn state_json(sid: &str, terminal: Option<&str>, presence: Option<presence::Entry>, approval_waiting: bool) -> Value {
    let p = presence.unwrap_or(presence::Entry { generation: String::new(), paused: false, viewers: vec![], join_requests: vec![] });
    json!({"sessionId": sid, "paused": p.paused, "terminal": terminal,
           "viewers": p.viewers, "joinRequests": p.join_requests, "approvalWaiting": approval_waiting})
}

pub(super) fn decision_body(sid: &str, generation: &str, body: &Value) -> Result<Value> {
    let decision = body["decision"].as_str().filter(|d| ["allow", "decline", "pause", "unpause"].contains(d)).ok_or("invalid decision")?;
    let answers = matches!(decision, "allow" | "decline");
    let user = body["userId"].as_str().filter(|v| !v.is_empty() && v.len() <= 128);
    let request = body["requestId"].as_str().filter(|v| !v.is_empty() && v.len() <= 64);
    if answers != (user.is_some() && request.is_some()) {
        return Err("invalid decision".into());
    }
    let mut out = json!({"sessionId": sid, "generation": generation, "decision": decision});
    if answers {
        out["userId"] = json!(user);
        out["requestId"] = json!(request);
    }
    Ok(out)
}

pub(super) fn parse_request(raw: &[u8]) -> Option<(String, String, Value)> {
    let text = std::str::from_utf8(raw).ok()?;
    let (head, body) = text.split_once("\r\n\r\n")?;
    let mut parts = head.lines().next()?.split(' ');
    let (method, path) = (parts.next()?.to_owned(), parts.next()?.to_owned());
    let body = if body.is_empty() { Value::Null } else { serde_json::from_str(body).ok()? };
    Some((method, path, body))
}

async fn read_request(socket: &mut UnixStream) -> Option<(String, String, Value)> {
    let mut buf = Vec::with_capacity(1024);
    let mut chunk = [0u8; 1024];
    loop {
        let n = socket.read(&mut chunk).await.ok()?;
        if n == 0 { return parse_request(&buf); }
        buf.extend_from_slice(&chunk[..n]);
        if buf.len() > MAX_REQUEST { return None; }
        if let Some(at) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
            let head = std::str::from_utf8(&buf[..at]).ok()?;
            let length = head.lines().find_map(|l| l.to_ascii_lowercase().strip_prefix("content-length:").map(|v| v.trim().parse::<usize>().ok())).flatten().unwrap_or(0);
            if length > MAX_REQUEST { return None; }
            if buf.len() >= at + 4 + length { return parse_request(&buf[..at + 4 + length]); }
        }
    }
}

async fn reply(socket: &mut UnixStream, status: &str, body: &Value) {
    let body = body.to_string();
    let _ = socket.write_all(format!("HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).as_bytes()).await;
}
```

`serve` mirrors `native_tools::serve`'s signature and loop (same `scope`, `enrollment`, `identity`, `client` arguments; read it at `native_tools.rs:340`), binding `native_tools::bind("mod.sock")` and, per connection, with a 2 s timeout on the read:

```rust
async fn answer(mut socket: UnixStream, scope: PathBuf, enrollment: Enrollment, identity: crate::keychain::DeviceIdentity, client: reqwest::Client) {
    let Some(caller) = native_tools::peer_pid(&socket).ok().and_then(|pid| resolve(&scope, pid)) else {
        return reply(&mut socket, "404 Not Found", &json!({"error":"not_shared"})).await;
    };
    let Ok(Some((method, path, body))) = tokio::time::timeout(Duration::from_secs(2), read_request(&mut socket)).await else {
        return reply(&mut socket, "400 Bad Request", &json!({"error":"invalid_request"})).await;
    };
    let state = |c: &Caller| state_json(&c.session_id, c.terminal, presence::get(&c.session_id), c.approval_waiting);
    match (method.as_str(), path.as_str()) {
        ("GET", "/v1/state") => reply(&mut socket, "200 OK", &state(&caller)).await,
        ("POST", "/v1/decide") => {
            let Ok(payload) = decision_body(&caller.session_id, &caller.generation, &body) else {
                return reply(&mut socket, "400 Bad Request", &json!({"error":"invalid_decision"})).await;
            };
            let Ok(token) = crate::device_token::mint_device_token(&identity.private_key_pem, &enrollment.device_id, &enrollment.owner_id, 90) else {
                return reply(&mut socket, "409 Conflict", &json!({"error":"unavailable","message":"Device authentication unavailable."})).await;
            };
            let url = format!("{}/api/v1/messaging/native-session-decisions", crate::LIGHTBULB_API_BASE.trim_end_matches('/'));
            let sent = client.post(url).bearer_auth(token).json(&payload).send().await;
            let Ok(response) = sent else {
                return reply(&mut socket, "409 Conflict", &json!({"error":"unavailable","message":"Lightbulb is unreachable."})).await;
            };
            let ok = response.status().is_success();
            let data: Value = response.json().await.unwrap_or(Value::Null);
            if !ok {
                return reply(&mut socket, "409 Conflict", &json!({"error": data["code"], "message": data["message"]})).await;
            }
            // The backend pushes fresh presence after every decision; the mod polls in 2 s anyway.
            reply(&mut socket, "200 OK", &state(&caller)).await
        }
        _ => reply(&mut socket, "404 Not Found", &json!({"error":"not_found"})).await,
    }
}
```

The backend's error JSON field names: check one `ApiProblem` response in `apps/backend/core/services/messaging.py` (`class ApiProblem`) and use its actual `code`/`message` keys.

- [ ] **Step 4: Run tests**

Run: `cd apps/desktop/src-tauri && cargo check --tests && cargo test --lib -- mod_socket native_tools; echo exit=$?`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src/local_sessions/mod_socket.rs apps/desktop/src-tauri/src/local_sessions.rs apps/desktop/src-tauri/src/local_sessions/native_tools.rs
git commit -m "feat(desktop): mod.sock — a Claude session's sharing state and the owner's terminal decisions"
```

---

### Task 5: The mod — state and the band

**Files:**
- Create: `apps/desktop/src-tauri/mods/.claude-plugin/marketplace.json`
- Create: `apps/desktop/src-tauri/mods/lightbulb-sessions/.claude-plugin/plugin.json`
- Create: `apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/hooks.json`
- Create: `apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/register.tsx`
- Create: `apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/state.ts`
- Create: `apps/desktop/src-tauri/mods/lightbulb-sessions/types/index.d.ts`
- Test: `apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/band.test.ts`

**Interfaces:**
- Consumes: `mod.sock` (Task 4). Socket path read from `<plugin root>/socket` (one line, written by Task 8).
- Produces: `state.ts` exports `type SessionState`, `fetchState($): Promise<SessionState | null>`, `decide($, body): Promise<SessionState | null>`; `register.tsx` exports `register`, and (Tasks 6, 7) adds hooks to it.

Before writing: read the API declarations for `ui.render`, `AbovePrompt`, `atom`/`read`/`update`, `$.clock.every`, `$.http.fetch` and `$.fs.read` in `/private/tmp/claude-501/bundled-skills/*/plugin-authoring/types/claude-code.d.ts` (or, after the first load, `<mod>/.claude-plugin/types/claude-code/index.d.ts`), and the example `plugin-authoring/examples/band.tsx`. The declaration file is the authority over this plan's code where they differ.

- [ ] **Step 1: Manifests**

`mods/.claude-plugin/marketplace.json`:
```json
{
  "name": "lightbulb",
  "owner": { "name": "Lightbulb" },
  "plugins": [{ "name": "lightbulb-sessions", "source": "./lightbulb-sessions", "description": "Session sharing in Claude Code: who is watching, join requests, teammate messages." }]
}
```
(Check the marketplace manifest schema with `claude plugin validate apps/desktop/src-tauri/mods`.)

`lightbulb-sessions/.claude-plugin/plugin.json`:
```json
{ "name": "lightbulb-sessions", "version": "0.1.0", "description": "Session sharing in Claude Code: who is watching, join requests, teammate messages.", "types": "./types/index.d.ts" }
```

`hooks/hooks.json`: `{ "modules": ["./register.tsx"] }`

`types/index.d.ts`:
```ts
export type Viewer = { userId: string; name: string; canType: boolean; typing: boolean }
export type JoinRequest = { userId: string; name: string; requestId: string }
export type SessionState = {
  sessionId: string
  paused: boolean
  terminal: 'held' | 'attach' | null
  viewers: Viewer[]
  joinRequests: JoinRequest[]
  approvalWaiting: boolean
}

declare module 'claude-code' {
  interface PluginState {
    'lightbulb-sessions': { state: SessionState | null; error: string | null }
  }
}
```

- [ ] **Step 2: Write the failing test**

```ts
// hooks/band.test.ts
import { test, expect, mock } from 'claude-code/testing'
import { bandLine } from './state'

const base = { sessionId: 's', paused: false, terminal: 'held' as const, viewers: [], joinRequests: [], approvalWaiting: false }

test('band says live and nobody watching', () => {
  expect(bandLine(base)).toBe('● Live in Lightbulb')
})

test('band names watchers and typists', () => {
  const s = { ...base, viewers: [
    { userId: 'a', name: 'Aden Kim', canType: true, typing: true },
    { userId: 'm', name: 'Mira Shah', canType: false, typing: false },
  ] }
  expect(bandLine(s)).toBe('● Live · Aden Kim, Mira Shah watching · Aden Kim typing')
})

test('band says transcript for a plain session, and paused wins', () => {
  expect(bandLine({ ...base, terminal: null })).toBe('● Shared as a transcript')
  expect(bandLine({ ...base, paused: true })).toBe('○ Sharing paused')
})

test('band shows nothing when the app does not answer', async ({ on, $ }) => {
  // `$.http.fetch` rejects (no socket): the AbovePrompt hook must fall through to next(e).
  // Mount the band per the testing kit (`ui.mount({ plugin, surface, component: 'AbovePrompt' })`)
  // on 'terminal' and 'desktop', with $.http.fetch mocked to reject, and assert the drawing is the engine's own (empty).
})
```

Write the last test fully against the testing kit's actual API (`claude-code/testing` declarations: `mount`, `mock`, surfaces). It must assert no tree from this plugin and no thrown error for both surfaces.

- [ ] **Step 3: Run to verify failure**

Run: `claude plugin test apps/desktop/src-tauri/mods/lightbulb-sessions; echo exit=$?`
Expected: FAIL (`./state` missing).

- [ ] **Step 4: Implement `state.ts`**

```ts
import type { EngineInterface } from 'claude-code'
import type { SessionState } from '../types'

const URL = 'http://lightbulb'

async function socketPath($: EngineInterface): Promise<string | null> {
  try {
    return (await $.fs.read(`${$.plugin.root}/socket`)).trim() || null
  } catch {
    return null
  }
}

async function call($: EngineInterface, path: string, body?: object): Promise<SessionState | null> {
  const socket = await socketPath($)
  if (!socket) return null
  try {
    const res = await $.http.fetch(`${URL}${path}`, {
      method: body ? 'POST' : 'GET',
      headers: body ? { 'content-type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
      socketPath: socket,
    })
    if (res.status !== 200) return null
    return JSON.parse(res.body) as SessionState
  } catch {
    return null // the app is not running or the session is not shared: show nothing
  }
}

export const fetchState = ($: EngineInterface) => call($, '/v1/state')
export const decide = ($: EngineInterface, body: { decision: string; userId?: string; requestId?: string }) =>
  call($, '/v1/decide', body)

export function bandLine(s: SessionState): string {
  if (s.paused) return '○ Sharing paused'
  if (s.terminal === null) return '● Shared as a transcript'
  if (s.viewers.length === 0) return '● Live in Lightbulb'
  const watching = s.viewers.map(v => v.name).join(', ')
  const typing = s.viewers.filter(v => v.typing).map(v => v.name)
  return `● Live · ${watching} watching` + (typing.length ? ` · ${typing.join(', ')} typing` : '')
}
```

Match `$.http.fetch`'s real `HttpInit`/`HttpResponse` shapes (read the declaration; `res.status`/`res.body` names may differ) and `$.fs.read`'s return type.

- [ ] **Step 5: Implement the band in `register.tsx`**

```tsx
import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'
import type { SessionState } from '../types'
import { bandLine, decide, fetchState } from './state'

const P = 'lightbulb-sessions'
export const sharing = atom({ plugin: P, key: 'state' } as const, null as SessionState | null)
const problem = atom({ plugin: P, key: 'error' } as const, null as string | null)

const POLL_MS = 2000

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const refresh = async () => {
      const s = await fetchState($)
      await update($, sharing, () => s)
    }
    await refresh()
    $.clock.every(POLL_MS, refresh)
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const s = await read($, sharing)
    if (!s) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    const act = (body: { decision: string; userId?: string; requestId?: string }) => async () => {
      const after = await decide($, body)
      await update($, sharing, () => after ?? s)
      await update($, problem, () => (after ? null : 'Lightbulb did not take that. Try again in the app.'))
    }
    const ask = s.joinRequests[0]
    const err = await read($, problem)
    return (
      <Box flexDirection="column">
        <Box>
          <Text dimColor>{bandLine(s)} </Text>
          {s.paused
            ? <Button key="resume" label="Resume sharing" onPress={act({ decision: 'unpause' })} />
            : <Button key="pause" label="Pause" onPress={act({ decision: 'pause' })} />}
        </Box>
        {ask && (
          <Box>
            <Text>{ask.name} asked to join{s.joinRequests.length > 1 ? ` (+${s.joinRequests.length - 1} more)` : ''} </Text>
            <Button key="allow" variant="primary" label="Allow" onPress={act({ decision: 'allow', userId: ask.userId, requestId: ask.requestId })} />
            <Button key="decline" label="Decline" onPress={act({ decision: 'decline', userId: ask.userId, requestId: ask.requestId })} />
          </Box>
        )}
        {s.approvalWaiting && <Text dimColor>Waiting on you here or in Lightbulb</Text>}
        {err && <Text color="red">{err}</Text>}
      </Box>
    )
  })
}
```

Check element props (`Text` `dimColor`/`color`, `Box` `flexDirection`, `Button` `variant`) against the `Elements` table in the declarations for both `terminal` and `desktop`; drop any a surface lacks. A `$.ui.toast` is not used.

Add tests for: Allow posts `{decision:'allow',userId,requestId}` to `/v1/decide` (mock `$.http.fetch`, press the `allow` key through the kit), and a failed decide shows the error line.

- [ ] **Step 6: Validate, type-check, test**

Run: `claude plugin validate apps/desktop/src-tauri/mods/lightbulb-sessions && claude plugin validate apps/desktop/src-tauri/mods && claude plugin test apps/desktop/src-tauri/mods/lightbulb-sessions; echo exit=$?`
Expected: validate lists hooks `session.start`, `ui.render`; calls `http.fetch`, `fs.read`, `clock.every`; tests pass.

- [ ] **Step 7: Commit**

```bash
git add apps/desktop/src-tauri/mods
git commit -m "feat(desktop): lightbulb-sessions mod — sharing band with join requests and pause"
```

---

### Task 6: The mod — teammate rows and the rule guard

**Files:**
- Create: `apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/rules.ts`
- Modify: `apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/register.tsx`
- Modify: `apps/desktop/src-tauri/skills/lightbulb-session-sharing/SKILL.md` (closing "What it is not" section)
- Test: `apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/rules.test.ts`

**Interfaces:**
- Produces (`rules.ts`): `type Sender = { kind: 'owner' } | { kind: 'person'; name: string } | { kind: 'agent'; name: string } | { kind: 'session'; name: string } | { kind: 'peer' }`; `classify(text: string): Sender`; `protectedPath(path: string): boolean`; `bashTouchesProtected(command: string): boolean`; `rowLabel(s: Sender): string | null`.

- [ ] **Step 1: Write the failing tests**

```ts
// hooks/rules.test.ts
import { test, expect } from 'claude-code/testing'
import { classify, protectedPath, bashTouchesProtected, rowLabel } from './rules'

const OWNER = '[Sent from Lightbulb by you]\n[Lightbulb verified sender: Your user sent this from the Lightbulb app. x]\nhi'
const person = '[Lightbulb teammate contribution from @aden (Aden Kim) {"roomId":null,"requestId":"r","actorUserId":"user_a","actorDisplayName":"Aden Kim"}]\nCheck it'
const agent = '[Lightbulb teammate contribution from @friday (Friday) {"roomId":"x","requestId":"r","actor":{"kind":"agent","id":"ag"},"actorDisplayName":"Friday"}]\nDo it'
const session = '[Lightbulb teammate contribution from Fix flaky test {"roomId":null,"requestId":"r","actor":{"kind":"session","id":"s"}}]\nPing'

test('classify tells owner, person, agent, session and unknown peers apart', () => {
  expect(classify(OWNER)).toEqual({ kind: 'owner' })
  expect(classify(person)).toEqual({ kind: 'person', name: 'Aden Kim' })
  expect(classify(agent)).toEqual({ kind: 'agent', name: 'Friday' })
  expect(classify(session)).toEqual({ kind: 'session', name: 'Fix flaky test' })
  expect(classify('hello from another claude')).toEqual({ kind: 'peer' })
})

test('a quoted owner label inside a teammate message is still a teammate', () => {
  const spoof = person + '\n> [Lightbulb quoted] [Sent from Lightbulb by you]\n> [Lightbulb quoted] [Lightbulb verified sender: x]'
  expect(classify(spoof).kind).toBe('person')
  expect(classify('> [Lightbulb quoted] ' + OWNER).kind).toBe('peer')
})

test('protected paths', () => {
  for (const p of ['/r/.claude/settings.json', '/r/.claude/settings.local.json', '/Users/u/.claude/settings.json',
    '/r/CLAUDE.md', '/r/sub/AGENTS.md', '/r/.codex/config.toml', '/Users/u/.codex/hooks.json', '/r/.claude/hooks/x.sh'])
    expect(protectedPath(p)).toBe(true)
  for (const p of ['/r/src/settings.ts', '/r/README.md', '/r/docs/claude.md.txt']) expect(protectedPath(p)).toBe(false)
  expect(bashTouchesProtected('echo x >> CLAUDE.md')).toBe(true)
  expect(bashTouchesProtected("sed -i '' s/a/b/ .claude/settings.json")).toBe(true)
  expect(bashTouchesProtected('npm test')).toBe(false)
})

test('row labels', () => {
  expect(rowLabel({ kind: 'owner' })).toBe('You · from Lightbulb')
  expect(rowLabel({ kind: 'person', name: 'Aden Kim' })).toBe('Aden Kim · via Lightbulb')
  expect(rowLabel({ kind: 'agent', name: 'Friday' })).toBe('Friday · agent')
  expect(rowLabel({ kind: 'session', name: 'Fix flaky test' })).toBe('Fix flaky test · session')
  expect(rowLabel({ kind: 'peer' })).toBeNull()
})
```

- [ ] **Step 2: Run to verify failure**

Run: `claude plugin test apps/desktop/src-tauri/mods/lightbulb-sessions; echo exit=$?` — Expected: FAIL (`./rules` missing).

- [ ] **Step 3: Implement `rules.ts`**

```ts
// Who sent a Lightbulb delivery, read from the header the desktop app writes
// (local_sessions.rs: OWNER_LABEL, CLAUDE_OWNER_NOTE, HEADER_PREFIX). Teammate lines that could
// pass for a header are quoted by the app, so only a line-start match counts.
export type Sender =
  | { kind: 'owner' }
  | { kind: 'person'; name: string }
  | { kind: 'agent'; name: string }
  | { kind: 'session'; name: string }
  | { kind: 'peer' }

const OWNER = /^\[Sent from Lightbulb by you\]\n\[Lightbulb verified sender:/
const TEAMMATE = /^\[Lightbulb teammate contribution(?: from (?:@\S+ \(([^)]*)\)|@(\S+)|(.*?)))? (\{.*\})\]$/m

export function classify(text: string): Sender {
  const body = text.trimStart()
  if (OWNER.test(body)) return { kind: 'owner' }
  const firstLine = body.split('\n', 1)[0]
  const m = TEAMMATE.exec(firstLine)
  if (!m) return { kind: 'peer' }
  let provenance: { actor?: { kind?: string }; actorDisplayName?: string } = {}
  try { provenance = JSON.parse(m[4]) } catch { /* label only */ }
  const name = provenance.actorDisplayName || m[1] || m[2] || m[3] || 'A teammate'
  if (provenance.actor?.kind === 'agent') return { kind: 'agent', name }
  if (provenance.actor?.kind === 'session') return { kind: 'session', name }
  return { kind: 'person', name }
}

const PROTECTED = [
  /(^|\/)\.claude\/settings(\.[\w-]+)?\.json$/,
  /(^|\/)\.claude\/hooks\//,
  /(^|\/)CLAUDE\.md$/,
  /(^|\/)AGENTS\.md$/,
  /(^|\/)\.codex\//,
]
export const protectedPath = (path: string) => PROTECTED.some(r => r.test(path))

// ponytail: a word match on the command text, not a shell parser; a teammate can still ask for
// an indirect write (a script that edits CLAUDE.md). The skill and the owner's prompts stay the fence for that.
const BASH = /(\.claude\/settings[\w.-]*\.json|\.claude\/hooks\/|CLAUDE\.md|AGENTS\.md|\.codex\/)/
export const bashTouchesProtected = (command: string) => BASH.test(command)

export function rowLabel(s: Sender): string | null {
  switch (s.kind) {
    case 'owner': return 'You · from Lightbulb'
    case 'person': return `${s.name} · via Lightbulb`
    case 'agent': return `${s.name} · agent`
    case 'session': return `${s.name} · session`
    default: return null
  }
}
```

The `classify` test for the session label (`from Fix flaky test {…}`) pins the third alternative; adjust the regex until all `classify` tests pass, without loosening the line-start anchor.

- [ ] **Step 4: Hooks in `register.tsx`**

Add inside `register`, using `turn`-scoped state (read `SessionAppendInput`, `SessionAppendDoor`, `PromptOrigin` and `TurnCompleteInput` in the declarations first):

```tsx
  // A turn a teammate's message started (or joined) may not change config. Cleared when the turn ends.
  let teammateTurn = false

  on('session.append', async ($, e, next) => {
    if (e.origin.kind === 'peer' && e.message.role === 'user' && !e.agentId) {
      const text = e.message.content.filter(b => b.type === 'text').map(b => (b as { text: string }).text).join('\n')
      if (classify(text).kind !== 'owner') teammateTurn = true
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    teammateTurn = false
    return next(e)
  })

  const refuse = (path: string) => ({ deny: `Lightbulb: a teammate's request cannot change ${path}. Ask your user to make this change.` })

  for (const tool of ['Edit', 'Write', 'MultiEdit', 'NotebookEdit'] as const) {
    on('tool.call', { tool }, ($, e, next) => {
      const path = String((e as { file_path?: string; notebook_path?: string }).file_path ?? (e as { notebook_path?: string }).notebook_path ?? '')
      return teammateTurn && protectedPath(path) ? refuse(path) : next(e)
    })
  }

  on('tool.call', { tool: 'Bash' }, ($, e, next) =>
    teammateTurn && bashTouchesProtected(e.command) ? refuse('that file') : next(e))

  on('ui.render', { component: 'UserMessage', props: { origin: { kind: 'peer' } } }, async ($, e, next) => {
    if (e.props.isExpanded) return next(e)
    const label = rowLabel(classify(e.props.text))
    if (!label) return next(e)
    const body = e.props.text.split('\n').filter(l => !l.startsWith('[Lightbulb ') && !l.startsWith('[Sent from Lightbulb')).join(' ').trim()
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box>
        <Text bold>{label}</Text>
        <Text>  {body.length > 200 ? body.slice(0, 199) + '…' : body}</Text>
      </Box>
    )
  })
```

Drop any tool name the build does not register (check `claude-code-tools` declarations; compare with `String(e.tool)` per the reference for tools a build lacks). `teammateTurn` is a module variable on purpose: a reload mid-turn clears it (fail-open for one turn, accepted; the skill still applies).

Add tests: a `session.append` from a teammate then `tool.call` Edit on `CLAUDE.md` is denied; the same after an owner message passes; after `turn.complete` passes; the UserMessage row draws `Aden Kim · via Lightbulb` collapsed and the engine's own row when `isExpanded`.

- [ ] **Step 5: Skill text**

In `apps/desktop/src-tauri/skills/lightbulb-session-sharing/SKILL.md`, replace the closing section's first sentence ("Sharing is not enforcement. …") with:

```
Sharing is mostly not enforcement. Where the Lightbulb Claude Code plugin is installed, a turn a
teammate's message started cannot edit settings, hooks, CLAUDE.md, AGENTS.md or .codex/ config:
the edit is refused with a reason. Lightbulb's other guarantees are the header on every teammate
message and the owner's native permission prompts, which stay on this machine.
```

Run the desktop's skill tests if any read SKILL.md (`grep -rn "SKILL_MD\|lightbulb-session-sharing" apps/desktop/src-tauri/src | grep test`), and update any exact-text assertion.

- [ ] **Step 6: Validate and test**

Run: `claude plugin validate apps/desktop/src-tauri/mods/lightbulb-sessions && claude plugin test apps/desktop/src-tauri/mods/lightbulb-sessions; echo exit=$?`
Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add apps/desktop/src-tauri/mods apps/desktop/src-tauri/skills
git commit -m "feat(desktop): mod names teammate senders and refuses config edits in a teammate's turn"
```

---

### Task 7: Install the plugin with sharing

**Files:**
- Create: `apps/desktop/src-tauri/src/local_sessions/plugin.rs`
- Modify: `apps/desktop/src-tauri/src/local_sessions.rs` (`mod plugin;`)
- Modify: `apps/desktop/src-tauri/src/local_sessions/enrollment.rs` (`reassert` and the off path beside `remove_skill`, ~L996 and ~L1039)

**Interfaces:**
- Consumes: the mod files (Tasks 5-6), `provider_program(home, "claude")`, `private_dir`.
- Produces: `plugin::install(state_dir: &Path, home: &Path) -> Result<()>`, `plugin::remove(state_dir: &Path, home: &Path)`, `plugin::FILES: &[(&str, &str)]`.

- [ ] **Step 1: Write failing tests** (bottom of `plugin.rs`)

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plugin_files_land_with_the_socket_path() {
        let dir = tempfile::tempdir().unwrap();
        let root = write_files(dir.path()).unwrap();
        assert!(root.join(".claude-plugin/marketplace.json").is_file());
        assert!(root.join("lightbulb-sessions/hooks/register.tsx").is_file());
        let socket = std::fs::read_to_string(root.join("lightbulb-sessions/socket")).unwrap();
        assert!(socket.trim().ends_with("/mod.sock"));
    }

    #[test]
    fn plugin_commands_name_the_bundled_marketplace() {
        let cmds = commands(Path::new("/m"), true);
        assert_eq!(cmds[0], vec!["plugin", "marketplace", "add", "/m"]);
        assert_eq!(cmds[1], vec!["plugin", "install", "lightbulb-sessions@lightbulb"]);
        assert_eq!(commands(Path::new("/m"), false)[0], vec!["plugin", "uninstall", "lightbulb-sessions@lightbulb"]);
    }
}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/desktop/src-tauri && cargo test --lib -- plugin_; echo exit=$?` — Expected: compile error.

- [ ] **Step 3: Implement `plugin.rs`**

```rust
//! The `lightbulb-sessions` Claude Code plugin (spec 2026-10-04-session-sharing-mod §4.4). Its
//! files are compiled in, written to the app's state dir as a folder marketplace, and installed
//! with the person's own `claude`. Claude reads a folder marketplace from the folder itself, so
//! an app update rewrites the files and the next session loads them. Never fatal: the mod is an
//! addition to sharing, not part of it.

use super::*;

pub(crate) const FILES: &[(&str, &str)] = &[
    (".claude-plugin/marketplace.json", include_str!("../../mods/.claude-plugin/marketplace.json")),
    ("lightbulb-sessions/.claude-plugin/plugin.json", include_str!("../../mods/lightbulb-sessions/.claude-plugin/plugin.json")),
    ("lightbulb-sessions/hooks/hooks.json", include_str!("../../mods/lightbulb-sessions/hooks/hooks.json")),
    ("lightbulb-sessions/hooks/register.tsx", include_str!("../../mods/lightbulb-sessions/hooks/register.tsx")),
    ("lightbulb-sessions/hooks/state.ts", include_str!("../../mods/lightbulb-sessions/hooks/state.ts")),
    ("lightbulb-sessions/hooks/rules.ts", include_str!("../../mods/lightbulb-sessions/hooks/rules.ts")),
    ("lightbulb-sessions/types/index.d.ts", include_str!("../../mods/lightbulb-sessions/types/index.d.ts")),
];
const ID: &str = "lightbulb-sessions@lightbulb";

fn write_files(state_dir: &Path) -> Result<PathBuf> {
    let root = state_dir.join("claude-mods");
    for (rel, text) in FILES {
        let path = root.join(rel);
        fs::create_dir_all(path.parent().ok_or("plugin path")?).map_err(|_| "plugin directory unavailable")?;
        if fs::read_to_string(&path).ok().as_deref() != Some(*text) {
            fs::write(&path, text).map_err(|_| "plugin write failed")?;
        }
    }
    let socket = enrollment::hook_path().with_file_name("mod.sock");
    fs::write(root.join("lightbulb-sessions/socket"), format!("{}\n", socket.display())).map_err(|_| "plugin write failed")?;
    Ok(root)
}

fn commands(root: &Path, on: bool) -> Vec<Vec<String>> {
    let s = |v: &[&str]| v.iter().map(|x| x.to_string()).collect::<Vec<_>>();
    if on {
        vec![s(&["plugin", "marketplace", "add", &root.display().to_string()]), s(&["plugin", "install", ID])]
    } else {
        vec![s(&["plugin", "uninstall", ID])]
    }
}

fn run(home: &Path, args: &[String]) {
    let Some(claude) = provider_program(home, "claude") else { return };
    match std::process::Command::new(claude).args(args).stdin(std::process::Stdio::null()).output() {
        Ok(out) if out.status.success() => {}
        Ok(out) => eprintln!("lightbulb plugin: {:?} exited {}", args, out.status),
        Err(_) => eprintln!("lightbulb plugin: {:?} did not start", args),
    }
}

pub(crate) fn install(state_dir: &Path, home: &Path) -> Result<()> {
    let root = write_files(state_dir)?;
    for args in commands(&root, true) {
        run(home, &args);
    }
    Ok(())
}

pub(crate) fn remove(_state_dir: &Path, home: &Path) {
    for args in commands(Path::new(""), false) {
        run(home, &args);
    }
}
```

Replace the `eprintln!` with whatever logger `enrollment.rs` uses for skill-install notes (`grep -n "relay_log\|eprintln\|log::" src/local_sessions/enrollment.rs | head`).

- [ ] **Step 4: Wire into enrollment**

In `reassert(&dir)` (where `install_skill` is called), after the skill install, call `plugin::install(&dir, &home)` and push any `Err` into the same `notes` list as a non-fatal note, run off the async runtime with `tokio::task::spawn_blocking` if `reassert` is on it (check its caller). Where `remove_skill` runs on the off path, also call `plugin::remove(&dir, home)`.

- [ ] **Step 5: Run tests**

Run: `cd apps/desktop/src-tauri && cargo check --tests && cargo test --lib -- plugin_ enrollment; echo exit=$?` — Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add apps/desktop/src-tauri/src/local_sessions/plugin.rs apps/desktop/src-tauri/src/local_sessions.rs apps/desktop/src-tauri/src/local_sessions/enrollment.rs
git commit -m "feat(desktop): install the lightbulb-sessions Claude plugin with session sharing"
```

---

### Task 8: Web — tell a frozen typist why

**Files:**
- Modify: `apps/frontend/src/components/messaging/LocalSessionPanel.tsx` (~L239 `admitted=`; ~L281 the "Watching ·" line)
- Test: `apps/frontend/src/components/messaging/__tests__/LocalSessionPanel.liveTerminal.test.tsx`

**Interfaces:**
- Consumes: `LocalSession.inputPaused` (Task 1).

- [ ] **Step 1: Write the failing test** (follow the file's existing render helper and session fixture)

```tsx
it("pauses an admitted teammate's typing while the owner answers a join request", async () => {
  const session = { ...liveSession, allowedSenders: [teammateId], inputPaused: true };
  renderPanel({ session, viewer: teammateId });
  expect(await screen.findByText(/Paused while .* answers a request to join/)).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Live terminal (view only)" })).toBeInTheDocument();
});
```

Use the file's actual helper names for `liveSession`, `teammateId` and the render function.

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/frontend && npx vitest run src/components/messaging/__tests__/LocalSessionPanel.liveTerminal.test.tsx; echo exit=$?` — Expected: FAIL.

- [ ] **Step 3: Implement**

```tsx
        admitted={control && session.shared !== false && !(session.inputPaused && !isOwner)}
```

(`isOwner`: the panel's existing owner check — `grep -n "humanUserId\|isOwner\|own" LocalSessionPanel.tsx | head`.) And beside the live terminal, for a non-owner with `control` and `session.inputPaused`:

```tsx
        {terminalLive && control && !isOwner && session.inputPaused &&
          <p className="text-sm text-muted-foreground">Paused while {ownerName} answers a request to join.</p>}
```

- [ ] **Step 4: Run tests and lint**

Run: `cd apps/frontend && npx vitest run src/components/messaging/__tests__/LocalSessionPanel.liveTerminal.test.tsx && npx eslint src/components/messaging/LocalSessionPanel.tsx; echo exit=$?` — Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/messaging/LocalSessionPanel.tsx apps/frontend/src/components/messaging/__tests__/LocalSessionPanel.liveTerminal.test.tsx
git commit -m "feat(sessions): a teammate's typing pauses visibly while the owner answers a join request"
```

---

## After the tasks

- Whole-branch review, then a PR. Ships via: backend deploy (Tasks 1-2), web deploy (Task 8), desktop build (Tasks 3-7). The backend and web are safe before the desktop: no Mac advertises `sessionPresence` until the new build.
- Acceptance on prod after promotion (no dev desktop builds): owner's terminal shows the band; a teammate watches → name appears; teammate asks to join → band shows Allow/Decline and the teammate's live terminal says "Paused…"; Allow from the terminal → teammate can type; teammate sends "edit CLAUDE.md" → refused with the reason; Pause from the band → web shows paused.
- Delete this plan and the spec in the shipping PR, or once shipped (CLAUDE.md "Planning docs are disposable").
