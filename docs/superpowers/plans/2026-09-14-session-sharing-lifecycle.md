# Shared-session lifecycle (hooks-first) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 2-second poll and per-session lease under shared Claude/Codex sessions with hook-driven lifecycle, a device-level heartbeat, an `ended` state with reaping, and a launch path that resumes mirroring after an update.

**Architecture:** Claude Code and Codex hooks send a small JSON datagram to the desktop app's existing wake socket; the desktop verifies the process as it does today, derives status from events, checks pid liveness on its 100 ms tick, and sweeps every 60 s as a backstop. One `local.device.heartbeat` renews every live session's lease; `local.session.ended` records `endedAt`. The backend hides ended sessions and sorts by `lastEventAt`; web and mobile show the label and a "show recent" toggle.

**Tech Stack:** Rust (Tauri 2, tokio, serde_json with `preserve_order`, sha2, hex, libc), Python (FastAPI, pydantic `ContractModel`, moto tests), TypeScript (packages/shared, Next.js, Expo).

**Spec:** `docs/superpowers/specs/2026-09-14-session-sharing-lifecycle-design.md`

## Global Constraints

- No wire compatibility window: the backend accepts only the new frames once promoted; an old desktop's `local.session.heartbeat` is rejected as `INVALID_REQUEST` until it auto-updates.
- Ended session rows are never deleted from DynamoDB; only desktop mirror files are reaped, 24 h after `endedAt`.
- Datagrams are hints, never authority: nothing from a datagram is trusted until the desktop has verified the process exactly as today (`claude.rs:23-119`, `codex.rs:49-74`).
- The desktop never sends the status `offline`; `offline` is only the backend's lapsed-lease verdict.
- `LIVENESS_SECONDS = 30` and the 10 s heartbeat cadence are unchanged.
- Backend wire models are `ContractModel` (`extra="forbid"`, camelCase aliases); an undeclared key in a frame is a hard reject.
- Status literal `ended` must land in every copy simultaneously: `local_sessions.py:98`, `:116`, `local_session_relay.py:39` (`Descriptor`), the new heartbeat/ended models, `packages/shared/src/messaging/types.ts:222`, and mobile `SessionStatusDot.tsx:6`.
- Desktop Rust tests: `cd apps/desktop/src-tauri && TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' CARGO_TARGET_DIR=$HOME/.cache/lightbulb-cargo-target cargo test --lib local_sessions`.
- Backend tests: `cd apps/backend && uv run pytest tests -k local_session -q`.
- Web tests: `cd apps/frontend && pnpm test -- src/components/messaging`. Shared: `cd packages/shared && pnpm test`. Mobile: `cd apps/mobile && pnpm test -- room` run alone with a Bash tool timeout of 600000.
- Never `sleep` or `tail` in shell. Commit messages end with the session's attribution lines.
- Three PRs, merged in order: A backend (Tasks 2-4), B clients (Tasks 5-7), C desktop (Tasks 8-13). Task 14 deletes this plan and the spec in PR C.

---

## Spike results (Task 1, already run on the owner's Mac 2026-09-14; do not redo, verify by reading)

**Claude Code 2.1.270 hook JSON (measured with a headless `claude -p --settings` run):** every hook receives `session_id`, `transcript_path`, `cwd`, `hook_event_name`. `SessionStart` adds `source` (`startup|resume|clear|compact|fork`). `UserPromptSubmit` adds `prompt_id`, `permission_mode`, `prompt`. `Stop` adds `last_assistant_message`. `SessionEnd` adds `reason` (`clear|resume|logout|prompt_input_exit|other`). `Notification` adds `notification_type` (`permission_prompt`, `idle_prompt`, `auth_success`, `elicitation_*`, `agent_needs_input`, `agent_completed`, `quota_*`) per the hooks reference; it did not fire headless. Command hooks accept `"async": true`; SessionEnd hooks share a 1.5 s budget.

**Codex 0.149.0 hooks (measured with `codex exec`):** `~/.codex/hooks.json` is loaded (Claude-shaped `{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":…,"timeout":5,"async":true}]}],…}}`). Supported events used here: `SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`. Payloads: `session_id` (the thread id), `transcript_path` (the rollout path), `cwd`, `hook_event_name`, `model`, `permission_mode`; `UserPromptSubmit`/`Stop` add `turn_id`; `Stop` adds `last_assistant_message`; `SessionEnd` adds `reason`. An untrusted hook is skipped silently. Trust lives in `~/.codex/config.toml` as

```toml
[hooks.state."/Users/<user>/.codex/hooks.json:session_start:0:0"]
trusted_hash = "sha256:<hex>"
```

key = `<absolute hooks.json path>:<event_snake>:<groupIndex>:<handlerIndex>`; hash = SHA-256 over the compact JSON (keys sorted recursively, no trailing newline) of `{"event_name":"<snake>","hooks":[{"async":<bool>,"command":"<cmd>","timeout":<normalized>,"type":"command"}]}` plus `"matcher":"<m>"` only when the group declares a matcher (absent → key omitted; `""` → `""`), and `"statusMessage"` only when set. Normalized timeout: non-SessionEnd `timeout.unwrap_or(600).max(1)`; SessionEnd `timeout.unwrap_or(1).clamp(1, max)`. Reproduced against three live entries (ponytail `session_start` `5f81d38f…`, ponytail `user_prompt_submit` `6a6f42bc…`, session-bridge `session_end` `08cef400…`), and self-written entries made all four of our events fire without `--dangerously-bypass-hook-trust`. **Conclusion: Codex follows the same hook table; the 10 s poll fallback is not needed.**

## File structure

**Backend (PR A)**
- Modify `apps/backend/core/services/local_session_relay.py` — new `DeviceHeartbeat`/`Ended` models and handlers, delete `Heartbeat`, `lastEventAt` stamping.
- Modify `apps/backend/core/services/local_sessions.py` — `ended` literal, `last_event_at`/`ended_at`/`last_assistant_message` fields, coercion, sort/hide, ended control gate.
- Modify `apps/backend/core/repositories/local_sessions_repo.py` — `attribute_not_exists(endedAt)` on command admission.
- Modify `apps/backend/routers/messaging.py` — `includeEnded` query param on both resources routes.
- Create `packages/shared/fixtures/local-session-frames/{device-heartbeat,device-heartbeat-ack,session-ended,session-ended-ack}.json` — golden frames asserted by pytest and cargo.
- Tests: `apps/backend/tests/unit/services/test_local_session_relay.py`, `test_local_sessions.py`.

**Clients (PR B)**
- Modify `packages/shared/src/messaging/types.ts`, `localSessionLabels.ts`, `client.ts`, `useRoomResources.ts` (+ `useRoomResources.test.ts`, new `localSessionLabels.test.ts`).
- Modify `apps/frontend/src/components/messaging/RoomResources.tsx`, `LocalSessionPanel.tsx`, `messaging.css`, `__tests__/RoomResources.test.tsx`; `apps/frontend/src/hooks/useDesktopAuth.ts`.
- Modify `apps/mobile/src/app/(app)/room/[roomId]/devices.tsx`, `src/components/session/SessionStatusDot.tsx`, `src/app/(app)/room/__tests__/devices.test.tsx`.
- Modify `apps/infra/openclaw/skills/lightbulb/references/surfaces.md` (one sentence: ended sessions).

**Desktop (PR C)**
- Create `apps/desktop/src-tauri/src/local_sessions/lifecycle.rs` — datagram parse, event→transition table, Codex trust hash and key.
- Modify `local_sessions.rs` (fields on `NativeSession`/`Mirror`, `authenticate`, `reconcile_sessions`), `enrollment.rs` (hook binary, listener, restore), `claude.rs` (five hooks), `codex.rs` (hooks.json + trust), `transport.rs` (sweep, liveness, device heartbeat, ended, reaping), `lib.rs` (restore after pairing), `main.rs` (provider arg).

---

### Task 1: Record the spike (no code)

**Files:** none.

- [ ] **Step 1: Read the "Spike results" section above and `docs/superpowers/specs/2026-09-14-session-sharing-lifecycle-design.md` §3.1.** Confirm the Codex path in this plan uses hooks (Tasks 8, 10) and that no poll-fallback task exists. If anything in later tasks contradicts the recorded payload field names, the spike section wins.

---

### Task 2: Backend data model — `ended`, timestamps, control gate

**Files:**
- Modify: `apps/backend/core/services/local_sessions.py:91-124` (models), `:306-310` (`_public`), `:468-474` (coercion), `:493-497` (`_require_control`)
- Modify: `apps/backend/core/services/local_session_relay.py:32-43` (`Descriptor.status`)
- Modify: `apps/backend/core/repositories/local_sessions_repo.py:217`
- Test: `apps/backend/tests/unit/services/test_local_sessions.py`

**Interfaces:**
- Produces: `LocalSession.status` literal gains `"ended"`; new optional fields `last_event_at: str | None`, `ended_at: str | None`, `last_assistant_message: str | None` (aliases `lastEventAt`, `endedAt`, `lastAssistantMessage`); helper `def iso(epoch: int | None) -> str | None` in `local_sessions.py`; META attributes `lastEventAt` (int epoch s), `endedAt` (int epoch s), `lastAssistantMessage` (str ≤ 300).
- Consumed by: Tasks 3, 4, 5.

- [ ] **Step 1: Write the failing tests** (append to `test_local_sessions.py`, using the existing `db`, `registered`, `command` fixtures and `OWNER`/`BOB`/`ROOM` constants at `:21-28`)

```python
@pytest.mark.asyncio
async def test_ended_row_reads_ended_even_while_lease_is_live(db):
    with patch("time.time", return_value=10000):
        sid = await registered()
    row = await repo.get_session(OWNER, sid)
    await repo.put_session({**row, "endedAt": 10005, "lastEventAt": 10005, "revision": row["revision"] + 1}, row, await repo.get_enrollment(OWNER, ALICE, MAC))
    with patch("time.time", return_value=10010):
        resources = await service.list_resources(BOB, ROOM, include_ended=True)
    [session] = [s for s in resources.sessions if s.session_id == sid]
    assert session.status == "ended"
    assert session.ended_at == "1970-01-01T02:46:45Z"
    assert session.last_event_at == "1970-01-01T02:46:45Z"


@pytest.mark.asyncio
async def test_command_to_ended_session_is_refused(db):
    with patch("time.time", return_value=10000):
        sid = await registered()
    row = await repo.get_session(OWNER, sid)
    await repo.put_session({**row, "endedAt": 10005, "revision": row["revision"] + 1}, row, await repo.get_enrollment(OWNER, ALICE, MAC))
    with patch("time.time", return_value=10010), pytest.raises(ApiProblem) as excinfo:
        await service.submit_command(BOB, ROOM, sid, command(service))
    assert excinfo.value.error.code == "LOCAL_SESSION_OFFLINE"
    assert "ended" in excinfo.value.error.message
```

(`registered()` at `test_local_sessions.py:69` registers under `ALICE`'s Mac; if its signature needs `sid` handling, copy how the neighbouring tests obtain `sid`.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_local_sessions.py -k "ended" -q`
Expected: FAIL (`list_resources() got an unexpected keyword argument 'include_ended'` / `ended` not a valid status).

- [ ] **Step 3: Implement**

In `local_sessions.py`:

```python
STATUSES = Literal["idle", "busy", "waiting-approval", "unknown", "offline", "ended"]

def iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

Use `STATUSES` at `:98` (`SessionRegistration.status`, default stays `"offline"`) and `:116`. Add to `LocalSession` after `cwd_name`:

```python
    last_event_at: str | None = None
    ended_at: str | None = None
    last_assistant_message: Annotated[str, Field(max_length=300)] | None = None
```

Replace the coercion at `:468-474` with:

```python
            public = dict(row)
            public["latestEventSeq"] = row.get("eventSeq", 0)
            public["lastEventAt"] = iso(row.get("lastEventAt"))
            public["endedAt"] = iso(row.get("endedAt"))
            if row.get("endedAt") is not None:
                public["status"] = "ended"
            elif row["onlineUntil"] <= int(time.time()):
                public["status"] = "offline"
            if not row["capabilitiesVerified"]:
                public["capabilities"] = Capabilities().model_dump(by_alias=True)
            sessions.append(_public(LocalSession, public))
```

`_require_control` (`:493`) gains, before the offline check:

```python
    if row.get("endedAt") is not None:
        raise ApiProblem("LOCAL_SESSION_OFFLINE", "The desktop session has ended.")
```

`list_resources` signature becomes `async def list_resources(auth: Caller, room_id: str, *, include_ended: bool = False) -> RoomResources:`; sorting and hiding are Task 4, but accept the argument now so the test compiles.

In `local_session_relay.py:39` use `sessions.STATUSES` for `Descriptor.status` (import from the service module the way the file already imports `sessions`).

In `local_sessions_repo.py:217`:

```python
                "ConditionExpression": "revision = :revision AND generation = :generation AND onlineUntil > :now AND attribute_not_exists(endedAt)",
```

- [ ] **Step 4: Run the whole local-session suite**

Run: `cd apps/backend && uv run pytest tests -k local_session -q`
Expected: PASS (106 existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/local_sessions.py apps/backend/core/services/local_session_relay.py apps/backend/core/repositories/local_sessions_repo.py apps/backend/tests/unit/services/test_local_sessions.py
git commit -m "feat(sessions): ended state, lastEventAt/endedAt, ended control gate"
```

---

### Task 3: Relay — device heartbeat, ended frame, golden fixtures

**Files:**
- Modify: `apps/backend/core/services/local_session_relay.py:52-61` (delete `Heartbeat`), `:86` (`_WIRE`), `:207-360` (dispatch), `:237-255` (register writes `lastEventAt`), `:401-406` (events write `lastEventAt`)
- Create: `packages/shared/fixtures/local-session-frames/device-heartbeat.json`, `device-heartbeat-ack.json`, `session-ended.json`, `session-ended-ack.json`
- Test: `apps/backend/tests/unit/services/test_local_session_relay.py`

**Interfaces:**
- Consumes: Task 2 (`STATUSES`, META fields).
- Produces the wire contract (also consumed by Task 12):

```json
{"type":"local.device.heartbeat","sessions":[{"sessionId":"s","generation":"g","status":"busy","activeTurnId":null,"controlUnavailableReason":null,"title":"t","cwdName":"c","lastAssistantMessage":null,"workspace":{"revision":0,"coverage":"unknown","changed":false,"overlap":false}}]}
{"type":"local.device.heartbeat.ack","rejected":["s2"],"reconcile":[{"sessionId":"s","requestId":"r", "…":"as today's per-session reconcile object"}],"serverTime":10010}
{"type":"local.session.ended","sessionId":"s","generation":"g","reason":"exited","endedAt":10005}
{"type":"local.session.ended.ack","sessionId":"s"}
```

- [ ] **Step 1: Write the golden fixtures** with exactly the four JSON documents above (the `reconcile` entry in the ack fixture is the per-session object `:333-351` builds today, with `sessionId` added; copy its real keys from a passing run of the existing heartbeat test).

- [ ] **Step 2: Write the failing tests** (append to `test_local_session_relay.py`; `transport` is the 5-tuple fixture at `:27`, `transport[4]` the hint list; `start(transport)` at `:116` registers one session and returns `sid`)

```python
FIXTURES = Path(__file__).resolve().parents[4] / "packages" / "shared" / "fixtures" / "local-session-frames"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.asyncio
async def test_device_heartbeat_renews_listed_sessions_and_rejects_foreign_or_ended(transport, db):
    with patch("time.time", return_value=10000):
        sid = await start(transport)
    before = await repo.get_session(OWNER, sid)
    frame = fixture("device-heartbeat")
    frame["sessions"][0].update({"sessionId": sid, "generation": "generation-1", "status": "idle"})
    frame["sessions"].append({**frame["sessions"][0], "sessionId": "not-mine", "generation": "x"})
    with patch("time.time", return_value=10010):
        response = await relay(transport).handle_message("conn-1", frame)
    after = await repo.get_session(OWNER, sid)
    assert after["onlineUntil"] == 10010 + 30 > before["onlineUntil"]
    assert after["status"] == "idle" and after["lastEventAt"] == 10010
    assert response["type"] == "local.device.heartbeat.ack"
    assert response["rejected"] == ["not-mine"]
    assert response["serverTime"] == 10010
    assert set(response) == set(fixture("device-heartbeat-ack"))


@pytest.mark.asyncio
async def test_unchanged_device_heartbeat_keeps_last_event_at_and_sends_no_hint(transport):
    with patch("time.time", return_value=10000):
        sid = await start(transport)
    transport[4].clear()
    frame = fixture("device-heartbeat")
    row = await repo.get_session(OWNER, sid)
    frame["sessions"][0].update({"sessionId": sid, "generation": "generation-1", "status": row["status"], "title": row.get("title"), "cwdName": row.get("cwdName")})
    with patch("time.time", return_value=10010):
        await relay(transport).handle_message("conn-1", frame)
    after = await repo.get_session(OWNER, sid)
    assert after["lastEventAt"] == 10000
    assert transport[4] == []


@pytest.mark.asyncio
async def test_ended_frame_marks_row_ended_idempotently_and_hints(transport):
    with patch("time.time", return_value=10000):
        sid = await start(transport)
    transport[4].clear()
    frame = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": 10005}
    with patch("time.time", return_value=10006):
        first = await relay(transport).handle_message("conn-1", frame)
        second = await relay(transport).handle_message("conn-1", frame)
    row = await repo.get_session(OWNER, sid)
    assert row["endedAt"] == 10005 and row["status"] == "ended" and row["lastEventAt"] == 10005
    assert first == second == {"type": "local.session.ended.ack", "sessionId": sid}
    assert len(transport[4]) == 1
    # a device heartbeat that still lists the ended session gets it back as rejected
    hb = fixture("device-heartbeat")
    hb["sessions"][0].update({"sessionId": sid, "generation": "generation-1"})
    with patch("time.time", return_value=10007):
        ack = await relay(transport).handle_message("conn-1", hb)
    assert ack["rejected"] == [sid]


@pytest.mark.asyncio
async def test_legacy_per_session_heartbeat_is_rejected(transport):
    with patch("time.time", return_value=10000):
        sid = await start(transport)
    with pytest.raises(ApiProblem) as excinfo:
        await relay(transport).handle_message("conn-1", {"type": "local.session.heartbeat", "sessionId": sid, "generation": "generation-1", "status": "busy"})
    assert excinfo.value.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_register_and_events_stamp_last_event_at(transport):
    with patch("time.time", return_value=10000):
        sid = await start(transport)
    assert (await repo.get_session(OWNER, sid))["lastEventAt"] == 10000
    # reuse the events frame the existing upload test sends (search this file for "local.session.events")
```

Delete the existing per-session heartbeat tests that send `local.session.heartbeat` (search the file; there are several around `:780-834`), or rewrite them onto `device-heartbeat` — rewrite the hint-fanout parametrize at `:807-834` so it still proves "a status change hints once, an unchanged heartbeat hints nothing".

- [ ] **Step 3: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_local_session_relay.py -q`
Expected: new tests FAIL with `INVALID_REQUEST` for `local.device.heartbeat`.

- [ ] **Step 4: Implement**

Models (replace `Heartbeat` at `:52-61`):

```python
class HeartbeatSession(ContractModel):
    session_id: str
    generation: str
    status: sessions.STATUSES
    active_turn_id: str | None = None
    control_unavailable_reason: sessions.ControlUnavailableReason | None = None
    title: Annotated[str, Field(max_length=120)] | None = None
    cwd_name: Annotated[str, Field(max_length=128)] | None = None
    last_assistant_message: Annotated[str, Field(max_length=300)] | None = None
    workspace: sessions.WorkspaceSummary = Field(default_factory=sessions.WorkspaceSummary)


class DeviceHeartbeat(ContractModel):
    type: Literal["local.device.heartbeat"]
    sessions: Annotated[list[HeartbeatSession], Field(max_length=64)]


class Ended(ContractModel):
    type: Literal["local.session.ended"]
    session_id: str
    generation: str
    reason: Literal["exited", "process-exited"]
    ended_at: int = Field(ge=0)


_WIRE = TypeAdapter(Register | DeviceHeartbeat | Events | Receipt | Ended)
```

Handlers, added to the `isinstance` chain in `handle_message`:

```python
            if isinstance(message, DeviceHeartbeat):
                return await self._device_heartbeat(cid, connection, enrollment, message)
            if isinstance(message, Ended):
                return await self._ended(cid, connection, enrollment, message)
```

```python
    async def _device_heartbeat(self, cid, connection, enrollment, message: DeviceHeartbeat) -> dict:
        now = int(time.time())
        rejected: list[str] = []
        reconcile: list[dict] = []
        changed: set[tuple[str, str]] = set()
        for item in message.sessions:
            row = await repo.get_session(connection["ownerId"], item.session_id)
            if row is None or row["deviceId"] != connection["deviceId"] or row["generation"] != item.generation or row.get("endedAt") is not None:
                rejected.append(item.session_id)
                continue
            labels = {k: v for k, v in (("title", item.title), ("cwdName", item.cwd_name), ("lastAssistantMessage", item.last_assistant_message)) if v is not None}
            public_before = (row["status"], row.get("activeTurnId"), row.get("title"), row.get("cwdName"), row.get("lastAssistantMessage"), row.get("controlUnavailableReason"))
            updated = _revised(row, status=item.status, activeTurnId=item.active_turn_id, controlUnavailableReason=item.control_unavailable_reason,
                               onlineUntil=now + LIVENESS_SECONDS, workspace=item.workspace.model_dump(by_alias=True), **labels)
            public_after = (updated["status"], updated.get("activeTurnId"), updated.get("title"), updated.get("cwdName"), updated.get("lastAssistantMessage"), updated.get("controlUnavailableReason"))
            if public_before != public_after:
                updated["lastEventAt"] = now
                changed.add((connection["ownerId"], row["humanUserId"]))
            await repo.put_session(updated, row, enrollment, extra=[self._connection_check(cid, connection)])
            pending = await self._reconcile_for(connection["ownerId"], item.session_id)   # the per-session object built at :333-351 today, factored into a helper
            if pending is not None:
                reconcile.append({"sessionId": item.session_id, **pending})
            await self.dispatch_session(connection["ownerId"], item.session_id)
        for owner, human in changed:
            await publish_resources_changed(owner, human)
        return {"type": "local.device.heartbeat.ack", "rejected": rejected, "reconcile": reconcile, "serverTime": now}

    async def _ended(self, cid, connection, enrollment, message: Ended) -> dict:
        row = await repo.get_session(connection["ownerId"], message.session_id)
        ack = {"type": "local.session.ended.ack", "sessionId": message.session_id}
        if row is None or row["deviceId"] != connection["deviceId"] or row["generation"] != message.generation or row.get("endedAt") is not None:
            return ack
        updated = _revised(row, status="ended", endedAt=message.ended_at, lastEventAt=message.ended_at, endedReason=message.reason)
        await repo.put_session(updated, row, enrollment, extra=[self._connection_check(cid, connection)])
        await publish_resources_changed(connection["ownerId"], row["humanUserId"])
        return ack
```

Register (`:237-255`): add `lastEventAt=int(time.time())` and, because a resumed conversation re-registers under the same session id, remove `endedAt`/`endedReason` from the new row (`row.pop("endedAt", None)` on the descriptor-derived row before `put_session`). Events (`:401-406`): include `lastEventAt = :now` in the META `UpdateExpression`. Delete the old heartbeat branch `:280-327` and move the `reconcile` computation `:333-351` into `_reconcile_for(owner, sid)` so register and the device heartbeat share it; register keeps `response["reconcile"]` and `response["serverTime"]` as today.

- [ ] **Step 5: Run the suite**

Run: `cd apps/backend && uv run pytest tests -k local_session -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/local_session_relay.py apps/backend/tests/unit/services/test_local_session_relay.py packages/shared/fixtures/local-session-frames
git commit -m "feat(sessions): device heartbeat and ended frames replace per-session heartbeat"
```

---

### Task 4: Listing — sort by activity, hide ended, `includeEnded` on both routes

**Files:**
- Modify: `apps/backend/core/services/local_sessions.py:412-479`
- Modify: `apps/backend/routers/messaging.py:71-73`, `:120-122`
- Test: `apps/backend/tests/unit/services/test_local_sessions.py`

**Interfaces:**
- Consumes: Task 2 `list_resources(auth, room_id, *, include_ended=False)`.
- Produces: `GET /messaging/rooms/{room_id}/resources?includeEnded=true` and `GET /messaging/local-session-agent/resources?includeEnded=true`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_list_resources_sorts_by_activity_hides_ended_and_caps_recent(db):
    sids = []
    for stamp in (10000, 10100, 10200):
        with patch("time.time", return_value=stamp):
            sids.append(await registered(conversation=f"conv-{stamp}"))
    enrollment = await repo.get_enrollment(OWNER, ALICE, MAC)
    rows = [await repo.get_session(OWNER, s) for s in sids]
    # end the middle one, and 21 more ended rows to prove the cap
    await repo.put_session({**rows[1], "endedAt": 10150, "lastEventAt": 10150, "revision": rows[1]["revision"] + 1}, rows[1], enrollment)
    for i in range(21):
        with patch("time.time", return_value=20000 + i):
            s = await registered(conversation=f"ended-{i}")
        r = await repo.get_session(OWNER, s)
        await repo.put_session({**r, "endedAt": 20000 + i, "lastEventAt": 20000 + i, "revision": r["revision"] + 1}, r, enrollment)
    with patch("time.time", return_value=30000):
        live = await service.list_resources(BOB, ROOM)
        with_ended = await service.list_resources(BOB, ROOM, include_ended=True)
    assert [s.session_id for s in live.sessions] == [sids[2], sids[0]]
    assert [s.status for s in live.sessions] == ["offline", "offline"]
    ended = [s for s in with_ended.sessions if s.status == "ended"]
    assert len(ended) == 20 and ended[0].last_event_at == "1970-01-01T05:33:40Z"
    assert with_ended.sessions[:2] == live.sessions
```

(If `registered()` has no `conversation` kwarg, add one that flows into the descriptor's `conversation` so each call mints a distinct session id.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_local_sessions.py -k sorts_by_activity -q`
Expected: FAIL on ordering.

- [ ] **Step 3: Implement** — after the member loop in `list_resources` (before `require_caller` at `:478`):

```python
    live = sorted((s for s in sessions if s.status != "ended"), key=lambda s: s.last_event_at or "", reverse=True)
    if include_ended:
        recent = sorted((s for s in sessions if s.status == "ended"), key=lambda s: s.last_event_at or "", reverse=True)[:20]
        sessions = live + recent
    else:
        sessions = live
```

(ISO strings sort lexically; `None` sorts last.) Routers:

```python
@router.get("/messaging/rooms/{room_id}/resources")
async def list_room_resources(room_id: str, include_ended: bool = Query(False, alias="includeEnded"), auth=Depends(get_current_user)):
    return await local_sessions.list_resources(auth, room_id, include_ended=include_ended)
```

and the same parameter on `agent_local_session_resources` (`:71-73`).

- [ ] **Step 4: Run the suite**

Run: `cd apps/backend && uv run pytest tests -k local_session -q`
Expected: PASS.

- [ ] **Step 5: Commit, open PR A**

```bash
git add apps/backend
git commit -m "feat(sessions): sort shared sessions by activity and hide ended unless asked"
```

PR A title: `feat(sessions): backend lifecycle protocol — device heartbeat, ended, lastEventAt`. Body: the wire contract from Task 3, "no compat window", link to the spec. Run the local `codex` review on the diff and paste the result. Merge on green.

---

### Task 5: Shared types, labels, client, hook

**Files:**
- Modify: `packages/shared/src/messaging/types.ts:215-234`, `localSessionLabels.ts:5-20`, `client.ts:42,73`, `useRoomResources.ts:21-101`
- Test: `packages/shared/src/messaging/useRoomResources.test.ts`, create `packages/shared/src/messaging/localSessionLabels.test.ts`

**Interfaces:**
- Produces: `LocalSession.status` union adds `"ended"`; `lastEventAt?: string | null`, `endedAt?: string | null`, `lastAssistantMessage?: string | null`; `sessionStatus.ended === "Ended"`; `canSendToSession` false for ended; `client.listResources(roomId, { includeEnded?: boolean })`; `useRoomResources(roomId, source, { enabled?, includeEnded? })`.

- [ ] **Step 1: Write the failing tests**

`localSessionLabels.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { canSendToSession, sessionStatus } from "./localSessionLabels";
import type { LocalSession } from "./types";

const base = { capabilities: { sendMode: "queue" }, controlUnavailableReason: null } as unknown as LocalSession;

describe("session labels", () => {
  it("labels ended sessions and refuses to send to them", () => {
    expect(sessionStatus.ended).toBe("Ended");
    expect(canSendToSession({ ...base, status: "ended" })).toBe(false);
    expect(canSendToSession({ ...base, status: "idle" })).toBe(true);
  });
});
```

Append to `useRoomResources.test.ts` (mirror the fixture style at `:10-16`):

```ts
it("passes includeEnded through to the client and refetches when it flips", async () => {
  const { rerender } = renderHook(({ includeEnded }) => useRoomResources("room-1", source, { includeEnded }), { initialProps: { includeEnded: false } });
  await waitFor(() => expect(source.client.listResources).toHaveBeenCalledWith("room-1", { includeEnded: false }));
  rerender({ includeEnded: true });
  await waitFor(() => expect(source.client.listResources).toHaveBeenCalledWith("room-1", { includeEnded: true }));
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd packages/shared && pnpm test`
Expected: FAIL (type error on `ended`, `listResources` called with one argument).

- [ ] **Step 3: Implement**

`types.ts`: `status: "idle" | "busy" | "waiting-approval" | "unknown" | "offline" | "ended";` and after `cwdName`:

```ts
  lastEventAt?: string | null;
  endedAt?: string | null;
  lastAssistantMessage?: string | null;
```

`localSessionLabels.ts`: add `ended: "Ended"` to `sessionStatus`; `canSendToSession` returns false when `session.status === "offline" || session.status === "ended"`.

`client.ts:42` interface: `listResources(roomId: string, options?: { includeEnded?: boolean }): Promise<RoomResources>;` and `:73`:

```ts
  listResources: (roomId, options) =>
    api.get(`${roomPath(roomId)}/resources${options?.includeEnded ? "?includeEnded=true" : ""}`) as Promise<RoomResources>,
```

`useRoomResources.ts`: third parameter `{ enabled = true, includeEnded = false }`; `:59` becomes `client.listResources(roomId, { includeEnded })`; add `includeEnded` to the `useCallback` deps at `:75` and to the effect that owns the interval at `:92`.

- [ ] **Step 4: Run**

Run: `cd packages/shared && pnpm test && pnpm typecheck` (use the package's typecheck script name; `pnpm -w turbo run typecheck --filter=@lightbulb/shared` if none).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/shared/src/messaging
git commit -m "feat(sessions): shared types and hook for ended sessions and includeEnded"
```

---

### Task 6: Web — last activity, "Show recent", ended dot, auth warning

**Files:**
- Modify: `apps/frontend/src/components/messaging/RoomResources.tsx:11-15,86-99`, `LocalSessionPanel.tsx:27`, `messaging.css:138-142`, `apps/frontend/src/hooks/useDesktopAuth.ts:25-27`
- Modify: `apps/infra/openclaw/skills/lightbulb/references/surfaces.md:79-93`
- Test: `apps/frontend/src/components/messaging/__tests__/RoomResources.test.tsx`

**Interfaces:** consumes Task 5.

- [ ] **Step 1: Write the failing tests** (fixtures `session`/`resources` at `:8-21`, `setup()` at `:29-53`)

```tsx
it("shows last activity, hides ended sessions until Show recent is pressed", async () => {
  const f = setup();
  const ended = { ...session, sessionId: "sess-ended", title: "Old run", status: "ended" as const, endedAt: "2026-09-14T03:00:00Z", lastEventAt: "2026-09-14T03:00:00Z" };
  f.client.listResources.mockImplementation((_room: string, options?: { includeEnded?: boolean }) =>
    Promise.resolve({ ...resources, sessions: options?.includeEnded ? [{ ...session, lastEventAt: "2026-09-14T04:00:00Z" }, ended] : [{ ...session, lastEventAt: "2026-09-14T04:00:00Z" }] }));
  await screen.findByText(/Working/);
  expect(screen.getByText(/ago/)).toBeInTheDocument();
  expect(screen.queryByText("Claude · Old run")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /show recent/i }));
  expect(await screen.findByText("Claude · Old run")).toBeInTheDocument();
  expect(screen.getByText(/Ended/)).toBeInTheDocument();
  expect(f.client.listResources).toHaveBeenLastCalledWith("room-1", { includeEnded: true });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/frontend && pnpm test -- RoomResources`
Expected: FAIL (no "Show recent" button).

- [ ] **Step 3: Implement**

`RoomResources.tsx`: `const [includeEnded, setIncludeEnded] = useState(false);` pass `{ includeEnded }` into `useRoomResources` at `:15`; the meta span at `:96` becomes

```tsx
<span className="text-sm text-muted-foreground">
  {sessionStatus[session.status]}{session.lastEventAt ? ` · ${timeAgo(session.lastEventAt)}` : ""}
</span>
```

importing `timeAgo` from `@/components/teams/shared/lib/timeAgo`. Under the sessions list add

```tsx
<button type="button" className="text-sm text-muted-foreground underline" onClick={() => setIncludeEnded(v => !v)}>
  {includeEnded ? "Hide recent" : "Show recent"}
</button>
```

`messaging.css`: `.native-session-dot-ended { background: var(--border); opacity: 0.5; }`. `LocalSessionPanel.tsx:27` needs no change (the class name derives from status).

`useDesktopAuth.ts:25-27`: replace the empty catch with `.catch((err: unknown) => { console.warn("[desktop-auth] local_sessions_auth failed", err); })`.

`surfaces.md` (Devices & sessions paragraph): add one sentence: "Sessions whose process has exited read **Ended** and are hidden until a member presses Show recent; they cannot receive follow-ups."

- [ ] **Step 4: Run**

Run: `cd apps/frontend && pnpm test -- src/components/messaging && pnpm lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend apps/infra/openclaw/skills/lightbulb/references/surfaces.md
git commit -m "feat(sessions): web shows last activity, hides ended sessions behind Show recent"
```

---

### Task 7: Mobile — last activity, "Show recent", ended dot

**Files:**
- Modify: `apps/mobile/src/app/(app)/room/[roomId]/devices.tsx:33,124-126`, `apps/mobile/src/components/session/SessionStatusDot.tsx:6-12`
- Test: `apps/mobile/src/app/(app)/room/__tests__/devices.test.tsx:81-92`

**Interfaces:** consumes Task 5. Uses the existing `relativeTime(ts: number)` at `apps/mobile/src/lib/relativeTime.ts:2` with `Date.parse(lastEventAt)`.

- [ ] **Step 1: Update the failing test** — `:87` asserts the literal `"Working · ~/lightbulb"`; give the fixture session `lastEventAt: new Date(Date.now() - 5 * 60_000).toISOString()` and assert `screen.getByText(/Working · 5m · ~\/lightbulb/)`. Add:

```tsx
it("hides ended sessions until Show recent", async () => {
  mockListResources.mockImplementation((_room: string, options?: { includeEnded?: boolean }) =>
    Promise.resolve({ ...fixtures, sessions: options?.includeEnded ? [...fixtures.sessions, endedSession] : fixtures.sessions }));
  await render(<DevicesScreen />);
  await waitFor(() => expect(screen.getByText("Claude · Fix relay")).toBeTruthy());
  expect(screen.queryByText("Claude · Old run")).toBeNull();
  await fireEvent.press(screen.getByText("Show recent"));
  await waitFor(() => expect(screen.getByText("Claude · Old run")).toBeTruthy());
  expect(screen.getByText(/Ended/)).toBeTruthy();
});
```

with `endedSession = { ...fixtures.sessions[0], sessionId: "sess-old", title: "Old run", status: "ended", endedAt: "2026-09-14T03:00:00Z", lastEventAt: "2026-09-14T03:00:00Z" }` next to the fixtures at `:24-48`.

- [ ] **Step 2: Run to verify it fails** (alone, Bash timeout 600000)

Run: `cd apps/mobile && pnpm test -- room/__tests__/devices`
Expected: FAIL.

- [ ] **Step 3: Implement** — `devices.tsx`: `const [includeEnded, setIncludeEnded] = useState(false);` pass `{ enabled: focused && !!roomId, includeEnded }`; meta line `:124-126`:

```tsx
{sessionStatus[session.status]}{session.lastEventAt ? ` · ${relativeTime(Date.parse(session.lastEventAt))}` : ""}{session.cwdName ? ` · ~/${session.cwdName}` : ""}
```

Add a `SectionList` footer `Pressable` labelled `Show recent` / `Hide recent` toggling `includeEnded`. `SessionStatusDot.tsx:6-12`: `ended: { color: border, hollow: true }`.

- [ ] **Step 4: Run** (alone, Bash timeout 600000)

Run: `cd apps/mobile && pnpm test -- room && pnpm typecheck`
Expected: PASS.

- [ ] **Step 5: Commit, open PR B**

```bash
git add apps/mobile
git commit -m "feat(sessions): mobile shows last activity and hides ended sessions behind Show recent"
```

PR B title: `feat(sessions): clients — ended sessions, last activity, Show recent`. Merging ships a mobile OTA; the change is additive. Run the local `codex` review and paste it. Merge on green.

---

### Task 8: Desktop `lifecycle.rs` — datagram, transitions, Codex trust hash

**Files:**
- Create: `apps/desktop/src-tauri/src/local_sessions/lifecycle.rs`
- Modify: `apps/desktop/src-tauri/src/local_sessions.rs` (add `pub(crate) mod lifecycle;` next to the other module declarations; add `#[serde(default)] pub last_assistant_message: Option<String>` to `NativeSession` `:749-767`; add `#[serde(default)] pub ended_at: Option<u64>` and `#[serde(default)] pub ended_reason: Option<String>` to `Mirror` `:1056-1073`)

**Interfaces (produced):**

```rust
pub(crate) const DATAGRAM_MAX: usize = 1500;
pub(crate) const LAST_MESSAGE_MAX: usize = 300;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Provider { Claude, Codex }

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct HookEvent {
    pub provider: Provider,
    pub event: String,                 // hook_event_name as sent
    pub session_id: String,
    pub transcript_path: Option<String>,
    pub cwd: Option<String>,
    pub notification_type: Option<String>,
    pub message: Option<String>,       // Notification message (for the "permission" fallback)
    pub reason: Option<String>,
    pub last_assistant_message: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Transition { Start, Status(&'static str), Ended(&'static str), Ignore }

pub(crate) fn build_datagram(provider: &str, stdin_json: &serde_json::Value) -> Vec<u8>;   // ≤ DATAGRAM_MAX, drop order lastAssistantMessage → cwd → transcriptPath
pub(crate) fn parse_datagram(bytes: &[u8]) -> Option<HookEvent>;                          // None for b"1" and garbage (= plain wake)
pub(crate) fn transition(event: &HookEvent) -> Transition;
pub(crate) fn codex_trust_hash(event_label: &str, matcher: Option<&str>, command: &str, timeout: u64, r#async: bool) -> String;
pub(crate) fn codex_trust_key(hooks_json: &std::path::Path, event_label: &str, group_index: usize) -> String;
pub(crate) fn codex_event_label(event: &str) -> Option<&'static str>;  // "SessionStart"→"session_start", "UserPromptSubmit"→"user_prompt_submit", "Stop"→"stop", "SessionEnd"→"session_end"
```

- [ ] **Step 1: Write the failing tests** (bottom of `lifecycle.rs`)

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ev(event: &str) -> HookEvent {
        HookEvent { provider: Provider::Claude, event: event.into(), session_id: "s".into(), transcript_path: None, cwd: None, notification_type: None, message: None, reason: None, last_assistant_message: None }
    }

    #[test]
    fn lifecycle_transition_table() {
        assert_eq!(transition(&ev("SessionStart")), Transition::Start);
        assert_eq!(transition(&ev("UserPromptSubmit")), Transition::Status("busy"));
        assert_eq!(transition(&ev("Stop")), Transition::Status("idle"));
        assert_eq!(transition(&HookEvent { notification_type: Some("permission_prompt".into()), ..ev("Notification") }), Transition::Status("waiting-approval"));
        assert_eq!(transition(&HookEvent { notification_type: Some("idle_prompt".into()), ..ev("Notification") }), Transition::Status("idle"));
        assert_eq!(transition(&HookEvent { notification_type: Some("auth_success".into()), ..ev("Notification") }), Transition::Ignore);
        assert_eq!(transition(&HookEvent { message: Some("Claude needs your permission to run Bash".into()), ..ev("Notification") }), Transition::Status("waiting-approval"));
        assert_eq!(transition(&HookEvent { message: Some("Done".into()), ..ev("Notification") }), Transition::Ignore);
        assert_eq!(transition(&ev("SessionEnd")), Transition::Ended("exited"));
        assert_eq!(transition(&ev("PreToolUse")), Transition::Ignore);
    }

    #[test]
    fn lifecycle_datagram_roundtrip_and_size_fallback() {
        let stdin = json!({"session_id":"abc","transcript_path":"/t/x.jsonl","cwd":"/w","hook_event_name":"Stop","last_assistant_message":"x".repeat(1000)});
        let bytes = build_datagram("claude", &stdin);
        assert!(bytes.len() <= DATAGRAM_MAX);
        let parsed = parse_datagram(&bytes).unwrap();
        assert_eq!(parsed.provider, Provider::Claude);
        assert_eq!(parsed.session_id, "abc");
        assert_eq!(parsed.last_assistant_message.as_deref().map(str::len), Some(LAST_MESSAGE_MAX));
        let long = json!({"session_id":"abc","transcript_path":"/".repeat(1400),"cwd":"/".repeat(200),"hook_event_name":"SessionStart"});
        let bytes = build_datagram("codex", &long);
        assert!(bytes.len() <= DATAGRAM_MAX);
        let parsed = parse_datagram(&bytes).unwrap();
        assert_eq!(parsed.provider, Provider::Codex);
        assert!(parsed.transcript_path.is_none() && parsed.cwd.is_none());
        assert!(parse_datagram(b"1").is_none());
        assert!(parse_datagram(b"{not json").is_none());
        assert!(parse_datagram(br#"{"v":1,"provider":"claude","event":"Stop"}"#).is_none(), "missing sessionId is a wake, not an event");
    }

    #[test]
    fn lifecycle_codex_trust_hash_matches_live_entries() {
        assert_eq!(codex_trust_hash("session_start", Some("startup|resume|clear|compact"), "node \"${CLAUDE_PLUGIN_ROOT}/hooks/ponytail-activate.js\"", 5, false),
            "sha256:5f81d38f47448a1581c08ec877e044d9e04dd6f814dce3f2671f7a8edadd719b");
        assert_eq!(codex_trust_hash("session_end", Some(""), "bash \"${CLAUDE_PLUGIN_ROOT}/scripts/cleanup.sh\"", 1, false),
            "sha256:08cef4005eb5f328882260c2e8cb44a5d31551a0db0d04273fb07ce8e39cbfc9");
        assert_eq!(codex_trust_key(std::path::Path::new("/Users/me/.codex/hooks.json"), "stop", 2), "/Users/me/.codex/hooks.json:stop:2:0");
        assert_eq!(codex_event_label("UserPromptSubmit"), Some("user_prompt_submit"));
        assert_eq!(codex_event_label("Notification"), None);
    }
}
```

The ponytail hook in the first assertion carries `statusMessage: "Loading ponytail mode..."`; add an optional `status_message: Option<&str>` parameter to `codex_trust_hash` and pass it there (our own hooks pass `None`). The `user_prompt_submit` live entry (`6a6f42bc…`, no matcher, timeout 5, `statusMessage: "Tracking ponytail mode..."`, command `node "${CLAUDE_PLUGIN_ROOT}/hooks/ponytail-mode-tracker.js"`) is a third fixture worth asserting.

- [ ] **Step 2: Run to verify they fail** — `cargo test --lib local_sessions::lifecycle` (compile error: module missing).

- [ ] **Step 3: Implement**

```rust
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

pub(crate) fn build_datagram(provider: &str, stdin_json: &Value) -> Vec<u8> {
    let s = |k: &str| stdin_json[k].as_str().map(str::to_owned);
    let mut fields: Vec<(&str, Option<String>)> = vec![
        ("v", Some("1".into())), ("provider", Some(provider.into())), ("event", s("hook_event_name")),
        ("sessionId", s("session_id")), ("reason", s("reason")), ("notificationType", s("notification_type")),
        ("message", s("message").map(|m| m.chars().take(120).collect())),
        ("transcriptPath", s("transcript_path")), ("cwd", s("cwd")),
        ("lastAssistantMessage", s("last_assistant_message").map(|m| m.chars().take(LAST_MESSAGE_MAX).collect())),
    ];
    for drop in ["lastAssistantMessage", "cwd", "transcriptPath", ""] {
        let mut map = Map::new();
        for (k, v) in &fields { if let Some(v) = v { map.insert((*k).into(), if *k == "v" { json!(1) } else { json!(v) }); } }
        let bytes = serde_json::to_vec(&Value::Object(map)).unwrap_or_default();
        if bytes.len() <= DATAGRAM_MAX || drop.is_empty() { return bytes; }
        for (k, v) in fields.iter_mut() { if *k == drop { *v = None; } }
    }
    unreachable!()
}

pub(crate) fn parse_datagram(bytes: &[u8]) -> Option<HookEvent> {
    let v: Value = serde_json::from_slice(bytes).ok()?;
    let provider = match v["provider"].as_str()? { "claude" => Provider::Claude, "codex" => Provider::Codex, _ => return None };
    let s = |k: &str| v[k].as_str().map(str::to_owned);
    Some(HookEvent { provider, event: s("event")?, session_id: s("sessionId").filter(|x| !x.is_empty() && x.len() <= 128)?,
        transcript_path: s("transcriptPath"), cwd: s("cwd"), notification_type: s("notificationType"), message: s("message"),
        reason: s("reason"), last_assistant_message: s("lastAssistantMessage") })
}

pub(crate) fn transition(event: &HookEvent) -> Transition {
    match event.event.as_str() {
        "SessionStart" => Transition::Start,
        "UserPromptSubmit" => Transition::Status("busy"),
        "Stop" => Transition::Status("idle"),
        "SessionEnd" => Transition::Ended("exited"),
        "Notification" => match event.notification_type.as_deref() {
            Some("permission_prompt") => Transition::Status("waiting-approval"),
            Some("idle_prompt") => Transition::Status("idle"),
            Some(_) => Transition::Ignore,
            None if event.message.as_deref().is_some_and(|m| m.to_ascii_lowercase().contains("permission")) => Transition::Status("waiting-approval"),
            None => Transition::Ignore,
        },
        _ => Transition::Ignore,
    }
}

fn canonical(v: &Value) -> Value {
    match v {
        Value::Object(m) => { let mut keys: Vec<_> = m.keys().cloned().collect(); keys.sort();
            Value::Object(keys.into_iter().map(|k| (k.clone(), canonical(&m[&k]))).collect()) }
        Value::Array(a) => Value::Array(a.iter().map(canonical).collect()),
        other => other.clone(),
    }
}

pub(crate) fn codex_trust_hash(event_label: &str, matcher: Option<&str>, command: &str, timeout: u64, r#async: bool, status_message: Option<&str>) -> String {
    let mut hook = json!({"type":"command","command":command,"timeout":timeout,"async":r#async});
    if let Some(s) = status_message { hook["statusMessage"] = json!(s); }
    let mut ident = json!({"event_name":event_label,"hooks":[hook]});
    if let Some(m) = matcher { ident["matcher"] = json!(m); }
    let bytes = serde_json::to_vec(&canonical(&ident)).unwrap_or_default();
    format!("sha256:{}", hex::encode(Sha256::digest(bytes)))
}

pub(crate) fn codex_trust_key(hooks_json: &std::path::Path, event_label: &str, group_index: usize) -> String {
    format!("{}:{}:{}:0", hooks_json.display(), event_label, group_index)
}

pub(crate) fn codex_event_label(event: &str) -> Option<&'static str> {
    Some(match event { "SessionStart" => "session_start", "UserPromptSubmit" => "user_prompt_submit", "Stop" => "stop", "SessionEnd" => "session_end", _ => return None })
}
```

(`serde_json` in this crate has `preserve_order`, so `canonical` must sort explicitly, as above; do not rely on `Map` ordering.)

- [ ] **Step 4: Run** — `cargo test --lib local_sessions::lifecycle` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src/local_sessions.rs apps/desktop/src-tauri/src/local_sessions/lifecycle.rs
git commit -m "feat(desktop): lifecycle module — hook datagram, transitions, Codex trust hash"
```

---

### Task 9: Hook binary sends the datagram; listener forwards events

**Files:**
- Modify: `apps/desktop/src-tauri/src/main.rs:4-7`, `enrollment.rs:305-314` (`hook`), `:83-88` (`PreparedRun`), `:186-200` (listener), `transport.rs:370` (`supervise` gains a `hooks: tokio::sync::mpsc::Receiver<lifecycle::HookEvent>` parameter, stored on `Host` for Task 11)
- Test: `enrollment.rs` tests (fixture `prepared(dir)` at `:807`)

**Interfaces:**
- `pub fn hook(provider: &str)` reads ≤ 64 KiB of stdin, parses JSON (on failure sends `b"1"`), sends `lifecycle::build_datagram(provider, &json)`.
- `main.rs`: `local-session-hook [claude|codex]`; missing arg = `"claude"` (legacy installs).
- Listener: `recv` up to 2048 bytes; `parse_datagram` → `Some(event)` with `transition(&event) == Start` → `wake.notify_one()` and also `hooks_tx.send(event)`; other events → `hooks_tx.send(event)` only; `None` → `wake.notify_one()`.

- [ ] **Step 1: Write the failing test** (enrollment tests module)

```rust
#[tokio::test]
async fn local_sessions_hook_binary_datagram_reaches_listener() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("wake.sock");
    let socket = tokio::net::UnixDatagram::bind(&path).unwrap();
    let stdin = serde_json::json!({"session_id":"abc","transcript_path":"/t","cwd":"/w","hook_event_name":"Stop","last_assistant_message":"hi"});
    send_hook_datagram(&path, "claude", &stdin);   // the helper `hook()` calls after reading stdin; takes the socket path for tests
    let mut buf = [0u8; 2048];
    let n = socket.recv(&mut buf).await.unwrap();
    let event = super::super::lifecycle::parse_datagram(&buf[..n]).unwrap();
    assert_eq!(event.session_id, "abc");
    assert_eq!(event.last_assistant_message.as_deref(), Some("hi"));
}
```

- [ ] **Step 2: Run to verify it fails** — `cargo test --lib local_sessions::enrollment::tests::local_sessions_hook_binary` → compile error.

- [ ] **Step 3: Implement**

```rust
pub fn hook(provider: &str) {
    let mut raw = Vec::new();
    let _ = std::io::Read::take(std::io::stdin(), 65536).read_to_end(&mut raw);
    let json: serde_json::Value = serde_json::from_slice(&raw).unwrap_or(serde_json::Value::Null);
    send_hook_datagram(&hook_path(), provider, &json);
}

pub(crate) fn send_hook_datagram(path: &Path, provider: &str, json: &serde_json::Value) {
    if private_dir(path.parent().unwrap()).is_err() { return; }
    let bytes = if json.is_object() { super::lifecycle::build_datagram(provider, json) } else { b"1".to_vec() };
    if let Ok(socket) = std::os::unix::net::UnixDatagram::unbound() {
        let _ = socket.set_write_timeout(Some(std::time::Duration::from_millis(100)));
        let _ = socket.send_to(&bytes, path);
    }
}
```

`main.rs`: `let provider = std::env::args().nth(2).unwrap_or_else(|| "claude".into()); …::hook(&provider);`. Listener in `publish`:

```rust
let (hooks_tx, hooks_rx) = tokio::sync::mpsc::channel::<super::lifecycle::HookEvent>(64);
let hooks = async move {
    let mut bytes = [0u8; 2048];
    loop {
        let n = match socket.recv(&mut bytes).await { Ok(n) => n, Err(_) => { transport::relay_log("hook socket error; listener stopped"); std::future::pending::<()>().await; unreachable!() } };
        match super::lifecycle::parse_datagram(&bytes[..n]) {
            Some(event) => { if super::lifecycle::transition(&event) == super::lifecycle::Transition::Start { hook_wake.notify_one(); } let _ = hooks_tx.try_send(event); }
            None => hook_wake.notify_one(),
        }
    }
};
tokio::select! {_ = hooks => {}, _ = host.supervise(enrollment, identity, wake, status, hooks_rx) => {}}
```

`supervise`/`run_once` accept `hooks_rx` and store it on `Host` as `hooks: tokio::sync::mpsc::Receiver<HookEvent>` (consumed in Task 11). Update `test_host`/`prepared` fixtures to build a channel.

- [ ] **Step 4: Run** — `cargo test --lib local_sessions` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src
git commit -m "feat(desktop): hook binary sends lifecycle datagrams; listener forwards events"
```

---

### Task 10: Install Claude (five events) and Codex hooks with self-written trust; re-assert on restore

**Files:**
- Modify: `claude.rs:626-686` (`hook_settings`, `configure_hooks`, `preflight_hooks`)
- Modify: `codex.rs` (new `hooks_path`, `configure_hooks`, `write_trust`, `remove_trust`)
- Modify: `enrollment.rs:507-522` (enable/disable call both), `:206-229` (`restore` re-asserts hooks + skill after `prepare`)
- Test: `claude.rs` tests (`:791` is the existing hook install test), new `codex.rs` tests

**Interfaces:**
- `claude::configure_hooks(settings: &Path, executable: &Path, enabled: bool)` installs, for each of `SessionStart`, `UserPromptSubmit`, `Stop`, `Notification`, `SessionEnd`, the entry `{"hooks":[{"type":"command","command":"'<exe>' local-session-hook claude","async":true}]}` (SessionEnd without `async`). Removal-exact for that entry **and** for the legacy `{"hooks":[{"type":"command","command":"'<exe>' local-session-hook"}]}` under `SessionStart`.
- `codex::configure_hooks(home: &Path, executable: &Path, enabled: bool) -> Result<()>`: edits `<home>/.codex/hooks.json` (create if missing; ≤ 1 MiB; removal-exact) with, per event, `{"hooks":[{"type":"command","command":"'<exe>' local-session-hook codex","timeout":5,"async":true}]}` for `SessionStart`/`UserPromptSubmit`/`Stop` and `{"hooks":[{"type":"command","command":…,"timeout":1,"async":false}]}` for `SessionEnd`; then computes each event's group index and writes/updates `[hooks.state."<key>"]\ntrusted_hash = "sha256:…"` blocks in `<home>/.codex/config.toml` (line-based: if the exact header line exists, replace the following `trusted_hash = ` line; else append `\n[hooks.state."<key>"]\ntrusted_hash = "…"\n`); on disable, remove our groups and our state blocks. Writes are temp-file + rename, preserving mode.

- [ ] **Step 1: Write the failing tests**

`claude.rs` (next to `:791`):

```rust
#[test]
fn local_sessions_claude_hooks_cover_five_events_and_strip_legacy_entry() {
    let dir = tempfile::tempdir().unwrap();
    let settings = dir.path().join("settings.json");
    fs::write(&settings, r#"{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"'/app' local-session-hook"}]}]},"other":1}"#).unwrap();
    configure_hooks(&settings, Path::new("/app"), true).unwrap();
    let v: Value = serde_json::from_str(&fs::read_to_string(&settings).unwrap()).unwrap();
    for event in ["SessionStart", "UserPromptSubmit", "Stop", "Notification", "SessionEnd"] {
        let entries = v["hooks"][event].as_array().unwrap();
        assert_eq!(entries.len(), 1, "{event}");
        assert_eq!(entries[0]["hooks"][0]["command"], "'/app' local-session-hook claude");
        assert_eq!(entries[0]["hooks"][0]["async"], if event == "SessionEnd" { Value::Null } else { json!(true) });
    }
    assert_eq!(v["other"], 1);
    configure_hooks(&settings, Path::new("/app"), false).unwrap();
    let v: Value = serde_json::from_str(&fs::read_to_string(&settings).unwrap()).unwrap();
    assert!(v.get("hooks").is_none());
}
```

`codex.rs`:

```rust
#[test]
fn local_sessions_codex_hooks_install_trust_and_remove() {
    let home = tempfile::tempdir().unwrap();
    let codex = home.path().join(".codex");
    fs::create_dir_all(&codex).unwrap();
    fs::write(codex.join("config.toml"), "model = \"x\"\n\n[hooks.state]\n\n[hooks.state.\"other:stop:0:0\"]\ntrusted_hash = \"sha256:aa\"\n").unwrap();
    fs::write(codex.join("hooks.json"), r#"{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"theirs"}]}]}}"#).unwrap();
    configure_hooks(home.path(), Path::new("/app"), true).unwrap();
    let hooks: Value = serde_json::from_str(&fs::read_to_string(codex.join("hooks.json")).unwrap()).unwrap();
    assert_eq!(hooks["hooks"]["Stop"].as_array().unwrap().len(), 2, "theirs kept, ours appended");
    assert_eq!(hooks["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"], 1);
    let config = fs::read_to_string(codex.join("config.toml")).unwrap();
    let key = format!("{}:stop:1:0", codex.join("hooks.json").display());
    assert!(config.contains(&format!("[hooks.state.\"{key}\"]")));
    let expected = super::lifecycle::codex_trust_hash("stop", None, "'/app' local-session-hook codex", 5, true, None);
    assert!(config.contains(&format!("trusted_hash = \"{expected}\"")));
    assert!(config.contains("other:stop:0:0"));
    configure_hooks(home.path(), Path::new("/app"), true).unwrap();   // idempotent
    assert_eq!(fs::read_to_string(codex.join("config.toml")).unwrap().matches(&key).count(), 1);
    configure_hooks(home.path(), Path::new("/app"), false).unwrap();
    let hooks: Value = serde_json::from_str(&fs::read_to_string(codex.join("hooks.json")).unwrap()).unwrap();
    assert_eq!(hooks["hooks"]["Stop"].as_array().unwrap().len(), 1);
    assert!(hooks["hooks"].get("SessionEnd").is_none());
    let config = fs::read_to_string(codex.join("config.toml")).unwrap();
    assert!(!config.contains(&key) && config.contains("other:stop:0:0") && config.starts_with("model = \"x\""));
}
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** — generalize `hook_settings` over `[("SessionStart", true), ("UserPromptSubmit", true), ("Stop", true), ("Notification", true), ("SessionEnd", false)]`; build `entry(event)`; `retain` both the new entry and the legacy entry; push when enabling; prune empty arrays/objects as today. For Codex, follow the same structure in `codex.rs`, plus:

```rust
fn upsert_trust(config: &str, key: &str, hash: &str) -> String {
    let header = format!("[hooks.state.\"{key}\"]");
    let mut lines: Vec<String> = config.lines().map(str::to_owned).collect();
    if let Some(i) = lines.iter().position(|l| l.trim() == header) {
        match lines[i + 1..].iter().position(|l| l.trim_start().starts_with("trusted_hash")) {
            Some(j) => lines[i + 1 + j] = format!("trusted_hash = \"{hash}\""),
            None => lines.insert(i + 1, format!("trusted_hash = \"{hash}\"")),
        }
    } else {
        if !config.ends_with('\n') && !config.is_empty() { lines.push(String::new()); }
        lines.extend([String::new(), header, format!("trusted_hash = \"{hash}\"")]);
    }
    let mut out = lines.join("\n"); out.push('\n'); out
}

fn remove_trust(config: &str, key: &str) -> String   // drops the header line and the following lines up to the next line starting with '[' or EOF
```

Enable path (`enrollment.rs:507-513`): after `claude::configure_hooks`, call `codex::configure_hooks(&home()?, &executable, true)`; a Codex failure degrades to a status string like the skill does (Codex may be absent). Disable: both, best effort. `restore` (after `prepare` succeeds, before `publish`): `claude::configure_hooks(&settings, &installed, true)`, `codex::configure_hooks(&home, &installed, true)`, `install_skill(&dir, &home)`, each failure appended to the status string, none fatal. The hook executable path comes from `hook-executable.json` as `local_sessions_set` reads it at `:432-437`.

- [ ] **Step 4: Run** — `cargo test --lib local_sessions` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src
git commit -m "feat(desktop): install five Claude hooks and self-trusted Codex hooks; re-assert on restore"
```

---

### Task 11: Transport discovery — 60 s sweep, hook events, pid liveness, restore/reap

**Files:**
- Modify: `transport.rs:36-42` (`Host` fields), `:115-200` (`observe`), `:214-252` (`restore`), `:263-282` (`run_once` discovery), `:412` (tick), `:534-541` (select)
- Modify: `local_sessions.rs:888-891` (`reconcile_sessions`)
- Test: `transport.rs` tests (fixtures `test_native` `:934`, `test_mirror` `:952`, `test_host` `:968`)

**Interfaces (produced, consumed by Task 12):**
- `Host { …, hooks: mpsc::Receiver<HookEvent>, last_discovery: Instant, invalidated: Vec<String> }`
- `fn apply_hook(&mut self, event: &HookEvent) -> Result<Option<String>>` — returns the key whose status changed (for an early heartbeat), or marks ended.
- `fn end_session(&mut self, key: &str, reason: &'static str)` — sets `mirror.native.status = "ended"`, `mirror.ended_at = Some(now)`, `mirror.ended_reason`, persists; pushes key onto `self.ended_pending: Vec<String>` (Task 12 sends the frame).
- `fn liveness_tick(&mut self)` — for each non-ended mirror: `if unsafe { libc::kill(pid as i32, 0) } == -1 && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)` → `end_session(key, "process-exited")`.
- `fn reap(&mut self)` — removes mirror files and map entries with `ended_at + 86_400 < now`.

- [ ] **Step 1: Write the failing tests**

```rust
#[test]
fn local_sessions_reconcile_never_marks_offline_and_keeps_unlisted_sessions() {
    let mut current = vec![test_native(SID)];
    current[0].status = "busy".into();
    reconcile_sessions(&mut current, vec![]);
    assert_eq!(current[0].status, "busy");
}

#[tokio::test]
async fn local_sessions_hook_events_drive_status_and_end() {
    let dir = tempfile::tempdir().unwrap();
    let mut host = test_host(dir.path(), vec![(SID, test_mirror(SID, "gen-1"))]);
    let key = host.mirrors.keys().next().unwrap().clone();
    let mut ev = super::super::lifecycle::HookEvent { provider: super::super::lifecycle::Provider::Claude, event: "UserPromptSubmit".into(), session_id: SID.into(), transcript_path: None, cwd: None, notification_type: None, message: None, reason: None, last_assistant_message: None };
    assert_eq!(host.apply_hook(&ev).unwrap(), Some(key.clone()));
    assert_eq!(host.mirrors[&key].native.status, "busy");
    ev.event = "Stop".into(); ev.last_assistant_message = Some("done".into());
    host.apply_hook(&ev).unwrap();
    assert_eq!(host.mirrors[&key].native.status, "idle");
    assert_eq!(host.mirrors[&key].native.last_assistant_message.as_deref(), Some("done"));
    ev.event = "SessionEnd".into();
    host.apply_hook(&ev).unwrap();
    assert_eq!(host.mirrors[&key].native.status, "ended");
    assert!(host.mirrors[&key].ended_at.is_some() && host.ended_pending == vec![key.clone()]);
    ev.event = "UserPromptSubmit".into();
    assert_eq!(host.apply_hook(&ev).unwrap(), None, "events after end are ignored");
}

#[test]
fn local_sessions_dead_pid_ends_within_one_tick_and_is_reaped_after_a_day() {
    let dir = tempfile::tempdir().unwrap();
    let mut mirror = test_mirror(SID, "gen-1");
    mirror.native.process.pid = 2_147_483_000;   // no such process
    let mut host = test_host(dir.path(), vec![(SID, mirror)]);
    let key = host.mirrors.keys().next().unwrap().clone();
    host.liveness_tick();
    assert_eq!(host.mirrors[&key].native.status, "ended");
    assert_eq!(host.mirrors[&key].ended_reason.as_deref(), Some("process-exited"));
    let file = dir.path().join(format!("{key}.json"));
    assert!(file.exists());
    host.mirrors.get_mut(&key).unwrap().ended_at = Some(0);
    host.reap();
    assert!(!file.exists() && host.mirrors.is_empty());
}

#[test]
fn local_sessions_restore_loads_ended_mirrors_only_for_reaping_and_marks_live_unknown() {
    let dir = tempfile::tempdir().unwrap();
    let mut ended = test_mirror(SID_B, "gen-1"); ended.native.status = "ended".into(); ended.ended_at = Some(0);
    let host = test_host(dir.path(), vec![(SID, test_mirror(SID, "gen-1")), (SID_B, ended)]);
    drop(host);
    let restored = Host::restore(dir.path().to_path_buf(), dir.path().join("home"), Arc::new(AtomicBool::new(false)), tokio::sync::mpsc::channel(1).1).unwrap();
    let live = restored.mirrors.values().find(|m| m.native.conversation == SID).unwrap();
    assert_eq!(live.native.status, "unknown");
    assert!(restored.mirrors.values().find(|m| m.native.conversation == SID_B).is_none(), "ended a day ago → reaped on restore");
}
```

(`test_host` builds a `Host` directly; add the `hooks` receiver and new fields to it.)

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement**
  - `reconcile_sessions`: delete the loop at `local_sessions.rs:889-891`. Sessions absent from `discovered` keep their status.
  - `run_once` discovery closure: `select!{ _ = sleep(60s) => {}, _ = wake.notified() => {} }`, and throttle so at most one discovery runs per second (`last_discovery`). On discovery error: `relay_log` once per distinct error string (keep a `last_discovery_error: Option<String>`), and send nothing (keep state).
  - `observe`: only new mirrors and generation changes are pushed to `invalidated` (the value returned to the caller); a status/title/active-turn/cwd change updates the mirror and marks `heartbeat_dirty = true` but does not invalidate. The sweep's listing status overwrites the hook-derived status. Both 512 caps count only mirrors with `ended_at.is_none()`.
  - `apply_hook`: find the mirror by `(provider, conversation == event.session_id)`; ignore unknown or ended mirrors (for unknown, `wake.notify_one()` was already done by the listener for Start; for other events on unknown sessions also notify the wake so the sweep picks them up). `Transition::Status(s)` → set status (and `last_assistant_message` when present), persist, return the key; `Ended(r)` → `end_session`.
  - `restore` (`:232`): `mirror.native.status = "unknown"` unless `ended_at.is_some()`; then `reap()`.
  - `connection()` select: add `Some(event) = self.hooks.recv() => { if let Some(key) = self.apply_hook(&event)? { heartbeat_dirty = true; } }` and call `self.liveness_tick()` and, every 60 s, `self.reap()` on the tick arm.

- [ ] **Step 4: Run** — `cargo test --lib local_sessions` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src
git commit -m "feat(desktop): hook-driven status, pid liveness, 60s sweep that never marks offline, reaping"
```

---

### Task 12: Transport wire — device heartbeat, ended frame, golden fixtures

**Files:**
- Modify: `transport.rs:406` (`pending_rpc`), `:456-495` (register/heartbeat selection), `:549-615` (ack arms), `:588-598` (delete the changed-means-re-register rule), `:669-671` (`verified` refuses `ended` instead of `offline`)
- Test: `transport.rs` socket tests (`next_frame` `:979`, `reply` `:988`)

**Interfaces:** consumes Task 3's frames (fixtures at `packages/shared/fixtures/local-session-frames/`, `include_str!("../../../../../packages/shared/fixtures/local-session-frames/device-heartbeat.json")` from `transport.rs`).

- [ ] **Step 1: Write the failing tests**

```rust
#[tokio::test]
async fn local_sessions_device_heartbeat_lists_live_sessions_and_rejected_reregisters() {
    // server: expect register(A), register(B), then one local.device.heartbeat listing both; reply rejected:[B]; expect register(B) again
    let dir = tempfile::tempdir().unwrap();
    let mut host = test_host(dir.path(), vec![(SID, test_mirror(SID, "gen-1")), (SID_B, test_mirror(SID_B, "gen-1"))]);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        let mut registered = vec![];
        for _ in 0..2 {
            let frame = next_frame(&mut socket).await;
            assert_eq!(frame["type"], "local.session.register");
            registered.push(frame["sessionId"].clone());
            reply(&mut socket, &json!({"type":"local.session.registered","sessionId":frame["sessionId"],"generation":frame["generation"],"afterSeq":0,"serverTime":1000})).await;
        }
        let hb = next_frame(&mut socket).await;
        assert_eq!(hb["type"], "local.device.heartbeat");
        let golden: Value = serde_json::from_str(include_str!("../../../../../packages/shared/fixtures/local-session-frames/device-heartbeat.json")).unwrap();
        assert_eq!(hb["sessions"][0].as_object().unwrap().keys().collect::<std::collections::BTreeSet<_>>(), golden["sessions"][0].as_object().unwrap().keys().collect());
        assert_eq!(hb["sessions"].as_array().unwrap().len(), 2);
        let rejected = hb["sessions"][1]["sessionId"].clone();
        reply(&mut socket, &json!({"type":"local.device.heartbeat.ack","rejected":[rejected],"reconcile":[],"serverTime":1010})).await;
        let again = next_frame(&mut socket).await;
        assert_eq!(again["type"], "local.session.register");
        assert_eq!(again["sessionId"], rejected);
    });
    let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{addr}")).await.unwrap();
    let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
    host.heartbeat_interval = Duration::from_millis(50);   // test seam; default 10 s
    let _ = tokio::time::timeout(Duration::from_secs(5), host.connection(&mut socket, &mut rx)).await;
    server.await.unwrap();
}

#[tokio::test]
async fn local_sessions_ended_frame_is_sent_once_and_stops_heartbeats() {
    // server: register(A); ack; expect local.session.ended for A after host.end_session; ack; then assert the next device heartbeat lists no sessions (or none is sent within 200 ms)
    // build exactly as above; after the register ack, call host.end_session(&key, "exited") through a hook event on the hooks channel:
    // hooks_tx.send(HookEvent{ event:"SessionEnd", session_id: SID, .. }).await
    // assert the ended frame's key set equals the golden session-ended.json
}
```

Write the second test fully in the same style as the first (it is the same scaffold with one more exchange).

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement**
  - Replace the `pending_rpc` tuple with `enum Rpc { Register { key, sent, wall, metadata }, Heartbeat { sent, wall }, Ended { key, sent } }` and `pending_rpc: Option<Rpc>`.
  - Selection order when `pending_rpc.is_none()`: (1) an unregistered, non-quarantined, non-ended key → register; (2) `ended_pending.pop()` → send `{"type":"local.session.ended","sessionId":…,"generation":…,"reason":…,"endedAt":…}`; (3) `heartbeat_dirty && last_early.elapsed() >= 1 s` or `last_heartbeat.elapsed() >= heartbeat_interval` → send `local.device.heartbeat` with every registered non-ended mirror, in the exact per-session key set of the golden fixture (`sessionId, generation, status, activeTurnId, controlUnavailableReason, title, cwdName, lastAssistantMessage, workspace`).
  - Ack arms: `local.session.registered` (as today, minus the `:592-598` rule: after `advance()` and persist, always `registered.insert(key, now)`); `local.device.heartbeat.ack` → for each `rejected` key `registered.remove(key)`; process each `reconcile` entry exactly as the per-session reconcile was processed (look up the mirror by `sessionId`); update the clock anchor from `serverTime`; `local.session.ended.ack` → clear the mirror's `pending` frame state and stop tailing it (skip ended mirrors in `capture_batch` and in the upload loop).
  - `verified` (`:670`): `if native.status == "ended" || native.restriction.is_some()` (offline is no longer a desktop-side status).
  - Remove all `"offline"` writes in `transport.rs` (`:157`, `:232`, `:241`, `:852` → use `"unknown"` where a placeholder is needed; `:852` on a dead pid becomes `end_session`).

- [ ] **Step 4: Run** — `cargo test --lib local_sessions` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src
git commit -m "feat(desktop): device heartbeat and ended frames; no more per-session heartbeats or re-register on change"
```

---

### Task 13: Launch and restore fixed forward

**Files:**
- Modify: `local_sessions.rs:238-240` (`authenticate`), `enrollment.rs:206-229` (`restore`), `:316-333` (`local_sessions_auth` logging), `lib.rs:395-406` (restore after pairing)
- Test: `enrollment.rs:818-845` (extend the truth-table test)

**Interfaces:**
- `fn authenticate(&mut self, human: Option<Option<&str>>)`: `None` → unchanged; `Some(None)` → `paused = true`; `Some(Some(id))` → `paused = id != human_user_id`.

- [ ] **Step 1: Extend the failing test** at `:818-845` so the loop iterates `[(None, keeps previous paused), (Some(None), paused), (Some(Some("bob")), paused), (Some(Some("alice")), running)]`, and add:

```rust
#[test]
fn local_sessions_authenticate_treats_unreported_as_unchanged() {
    let mut e = candidate();               // human_user_id "alice", paused false
    e.authenticate(None);
    assert!(!e.paused);
    e.authenticate(Some(None));
    assert!(e.paused);
    e.authenticate(None);
    assert!(e.paused, "unreported never un-pauses either");
    e.authenticate(Some(Some("alice")));
    assert!(!e.paused);
    e.authenticate(Some(Some("bob")));
    assert!(e.paused);
}
```

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement** — `authenticate` per the interface; `restore` `:213-219` passes `self.human.lock()…clone()` (the `Option<Option<String>>`, mapped to `Option<Option<&str>>`) instead of flattening; `local_sessions_auth` `:332`: `state.restore(&app).await.map_err(|e| { crate::log(&format!("[local-sessions] auth restore failed: {e}")); e })`; `lib.rs` after `:402` (pair state saved):

```rust
let controller = app.state::<local_sessions::enrollment::Controller>();
if let Err(e) = controller.restore(&app).await {
    log(&format!("[local-sessions] restore after pairing failed: {e}"));
}
```

(`run_bootstrap` already has the `app` handle; if it only has `&AppHandle`, `app.state::<…>()` works on it.)

- [ ] **Step 4: Run** — `cargo test --lib local_sessions` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src
git commit -m "fix(desktop): resume sharing after pairing; unreported sign-in no longer pauses it; log restore failures"
```

---

### Task 14: PR C, cleanup, and rollout

**Files:**
- Delete: `docs/superpowers/specs/2026-09-14-session-sharing-lifecycle-design.md`, `docs/superpowers/plans/2026-09-14-session-sharing-lifecycle.md`

- [ ] **Step 1: Full desktop test run** — `cargo test --lib local_sessions` and `cargo test --example local_session_probe` (CI's second command) → PASS.
- [ ] **Step 2: Delete the spec and this plan; commit** `docs: retire the shipped session-lifecycle spec and plan`.
- [ ] **Step 3: Open PR C** `feat(sessions): desktop hooks-first lifecycle, device heartbeat, ended, restore fix`. Body: the datagram contract, the Codex trust recipe (from the spike section), the rollout order. Local `codex` review pasted. Merge on green (`test-desktop` runs on the self-hosted Mac).
- [ ] **Step 4: Promote.** `gh run list --repo TheLightbulbCompany/lightbulb --workflow deploy.yml --limit 10 --json status --jq '[.[]|select(.status!="completed")]|length'` must read 0; then `gh workflow run deploy.yml --ref main`; `gh run watch <id> --exit-status` (single invocation, no loop). Then dispatch `desktop-build.yml` for prod and confirm a `desktop-v0.4.*` release exists.
- [ ] **Step 5: Live check on this Mac** exactly as the goal prompt's step 5: relaunch `open -a Lightbulb`, confirm the version, `grep '\[local-sessions\] connected' /tmp/lightbulb-desktop.log`, start a real `claude` via `env -i` + expect, watch the room's Devices & sessions panel in Chrome: Working within a second, Ended after exit, the 22 stale sessions Ended, no new dead mirror files under `~/Library/Application Support/company.thelightbulb.desktop/local-sessions/`.
- [ ] **Step 6: Report** PR numbers, commits, deploy run id, desktop version, what the live check showed, anything open.

---

## Self-review notes

- Spec §3.1 hook table → Task 8 `transition`; datagram cap and drop order → Task 8 `build_datagram`; five Claude hooks → Task 10; Codex hooks + trust → Tasks 8/10 (spike replaced the fallback); pid liveness, 60 s sweep, never-offline, listing wins → Task 11; §3.2 frames and no compat → Tasks 3/12; §3.3 fields, coercion, sort, hide, cap, admission → Tasks 2/4; §3.4 → Tasks 5-7; §3.5 → Tasks 10/13; §3.6 reaping and cap → Task 11; §5 tests → per task; §6 rollout → Task 14.
- Type consistency: `STATUSES` (Task 2) is reused by Task 3's models; `HookEvent`/`Transition`/`codex_trust_hash(…, status_message)` (Task 8) are the names Tasks 9-12 use; `list_resources(..., include_ended=)` (Task 2) is what Task 4's router passes; `client.listResources(roomId, { includeEnded })` (Task 5) is what Tasks 6-7 assert.
- Additions beyond the spec, both cheap and both deliberate: `lastAssistantMessage` (300 chars, from the Stop hook) rides the datagram, mirror, heartbeat, META and public JSON so the phase-two recap line needs no LLM; the golden fixtures are asserted from both `cargo test` and `pytest` because Rust-versus-Python contract skew has already caused one prod outage.
