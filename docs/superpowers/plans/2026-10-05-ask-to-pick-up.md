# Ask to pick up — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A teammate reading a Claude session can ask the owner to allow pick-up; the owner answers from notifications, Activity, the session view (web and phone) or the Claude terminal band; Allow turns pick-up on for the session.

**Architecture:** A `pickupRequests` map on the session row mirrors `joinRequests`. Everything downstream of a request (activity row, attention event, presence, Mac freeze, mod band, device door) reuses the join-request plumbing with a `kind`/`request` = `"pickup"` variant; Allow calls the existing `session_pickup.set_pickup(…, True)`.

**Tech Stack:** Python/FastAPI/pytest; Rust/Tauri; Claude Code mods (TS); React/vitest; Expo/jest.

**Spec:** `docs/superpowers/specs/2026-10-05-ask-to-pick-up.md`

## Global Constraints

- Worktree `/Users/prasiddhaparthsarthy/Desktop/lightbulb-ask-pickup`, branch `feat/ask-to-pick-up`, off `origin/main` (86ade1fd2). Do not push.
- Claude sessions only (`row["provider"] == "claude"`), as pick-up itself.
- Pick-up permission stays per session; Allow = `set_pickup(auth, sid, True)`; turning pick-up on by any route clears every pending pick-up request.
- Activity subtype stays `join_request` with `request: "pickup"` on the row; activity item id `pickup:<sid>:<user>:<requestId>`; attention states stay `join_request` / `join_allowed` / `join_declined`.
- Presence `joinRequests` items carry `kind: "join" | "pickup"`; the device door tells them apart by request id.
- Copy (exact): requester button "Ask to pick up" / "Requested" / "<Owner> declined. Ask again"; owner row "<Name> wants to pick up this session."; band row "<Name> wants to pick this up"; activity title "<Name> wants to pick up <title>"; activity reason "Allow lets everyone in the workspace pick up this session on their Mac."; requester notices "<Owner> let you pick up <title>" / "<Owner> declined your request to pick up <title>".
- Do not edit `cli.rs`, `session_start.rs`, `ExplicitSharingNotice.tsx`.
- Tests: only touched files locally; check exit codes. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A request made, then pick-up turned on by the owner's switch (not via the request): the request must clear and its activity item resolve. Test in Task 1.
2. A pick-up request and a join request from the same person on the same session at once: the device door must route each by its own request id. Test in Task 2.
3. An older desktop (from #3729, no `kind`) receiving a pick-up entry: it shows "asked to join" but Allow still routes correctly by request id. Test in Task 2 (door routing without kind).
4. The requester asking on a Codex session or their own session: refused. Test in Task 1.
5. Allow when the owner's session is paused: `set_pickup` refuses ("Resume sharing before allowing pick-up"); the request must stay pending, not be consumed. Test in Task 1.

---

### Task 1: Backend requests, decisions, routes, public fields, notices

**Files:** Modify `apps/backend/core/services/local_sessions.py` (beside `request_join`/`decide_join` ~L1891-1960, `_notify_join_request` ~L1969, `_notify_join_answer`, `_session_public` ~L625, `LocalSession` ~L235, audit actions ~L1700-1725); `apps/backend/core/services/session_pickup.py` (`set_pickup` ~L327); `apps/backend/routers/messaging.py` (beside the join routes ~L281-296). Test: `apps/backend/tests/unit/services/test_pickup_requests.py` (new).

**Interfaces — Produces:** `local_sessions.request_pickup_access(auth, sid) -> LocalSession`; `local_sessions.decide_pickup_request(auth, sid, user_id, decision, *, request_id) -> LocalSession`; `local_sessions.pending_pickups(row) -> dict[str, str]`; public fields `pickupRequest` (requester), `pendingPickups`, `pendingPickupIds` (owner); routes `POST /messaging/sessions/{id}/pickup-requests` and `POST /messaging/sessions/{id}/pickup-requests/{user_id}` (body = existing `JoinDecisionRequest`); audit actions `pickup_requested`, `pickup_request_declined` (Allow is audited by `set_pickup`'s existing `pickup_on`); activity row field `request: "pickup"`.

- [ ] **Step 1: failing tests** — write `test_pickup_requests.py` following the fixtures of the existing join-request tests (`grep -n "request_join\|decide_join" tests/unit/services/test_local_sessions.py` and copy their row/auth/mocks). Cases:
  1. a teammate asks → row `pickupRequests[user].state == "pending"`, notice sent once (owner `_session_notice` with state `join_request`), activity row has `request: "pickup"`; asking again while pending → no write.
  2. the owner asking on their own session → `LOCAL_SESSION_FORBIDDEN`; a Codex session → `LOCAL_SESSION_UNSUPPORTED`; pick-up already on → no write, no notice.
  3. owner `decide_pickup_request(..., "allow", request_id=r)` → calls `session_pickup.set_pickup(auth, sid, True)` once, removes the entry, notifies the requester with state `join_allowed` and title "<Owner> let you pick up <title>"; stale `request_id` → `LOCAL_SESSION_CONFLICT`; a non-owner → `LOCAL_SESSION_FORBIDDEN`.
  4. decline → state `declined`, requester notified (`join_declined`); asking again after decline → pending with a new request id.
  5. Allow while `set_pickup` raises (paused session) → the error propagates and the entry stays pending.
  6. `set_pickup(True)` from the owner's switch clears every pending `pickupRequests` entry (assert the written row).
  7. `_session_public`: requester sees `pickupRequest == "pending"`; owner sees `pendingPickups == [user]` and `pendingPickupIds == {user: r}`; a third person sees neither.

- [ ] **Step 2: run, expect failures** — `cd apps/backend && uv run pytest tests/unit/services/test_pickup_requests.py -q; echo exit=$?`

- [ ] **Step 3: implement** in `local_sessions.py`:

```python
def pending_pickups(row: dict) -> dict[str, str]:
    """Each person waiting for the owner to allow pick-up, with the request id they asked under."""
    if row.get("pickupAllowed"):
        return {}
    return {u: r["requestId"] for u, r in (row.get("pickupRequests") or {}).items() if r.get("state") == "pending"}


async def request_pickup_access(auth: AuthContext, session_id: str) -> LocalSession:
    """A teammate asks the owner to allow pick-up (spec 2026-10-05-ask-to-pick-up). Claude only;
    asking while pending or while pick-up is on changes nothing; a declined person may ask again."""
    me = auth.user_id

    def change(row: dict) -> dict | None:
        if me == row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "This is your own session.")
        if row["provider"] != "claude":
            raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "Only Claude Code sessions can be picked up.")
        requests = dict(row.get("pickupRequests") or {})
        if row.get("pickupAllowed") or (requests.get(me) or {}).get("state") == "pending":
            return None
        requests[me] = {"state": "pending", "requestId": str(uuid.uuid4()), "createdAt": utc_now_iso()}
        return {**row, "pickupRequests": requests}

    row, audit = await _change_session(auth, session_id, change, "pickup_requested", None)
    if audit is not None:
        await _notify_join_request(row, me, kind="pickup")
        await _push_presence(row)
    return _session_public(row, me)
```

`_change_session` refuses an ended session (`LOCAL_SESSION_OFFLINE`). The spec allows asking for an ended session, so give `_change_session` a keyword `allow_ended: bool = False` and pass `True` here and in `decide_pickup_request`.

```python
async def decide_pickup_request(
    auth: AuthContext, session_id: str, user_id: str, decision: str, *, request_id: str | None = None
) -> LocalSession:
    """The owner answers one pick-up request. Allow turns pick-up on for the session (every
    member may then pick it up) through set_pickup, which also clears every pending request."""
    from core.services import session_pickup

    user_id = _valid_user(user_id)
    row, _ = await authorize_session(auth, None, session_id)
    if auth.user_id != row["humanUserId"]:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can answer a request to pick up.")
    current = (row.get("pickupRequests") or {}).get(user_id) or {}
    if current.get("state") != "pending" or (request_id is not None and current.get("requestId") != request_id):
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "That request was already answered.")
    if decision == "allow":
        public = await session_pickup.set_pickup(auth, session_id, True)  # raises (e.g. paused): entry stays pending; audits pickup_on
        await _notify_join_answer(row, user_id, "join_allowed", current.get("requestId"), kind="pickup")
        return public

    def change(r: dict) -> dict | None:
        reqs = dict(r.get("pickupRequests") or {})
        cur = reqs.get(user_id) or {}
        if cur.get("state") != "pending" or cur.get("requestId") != current.get("requestId"):
            return None
        reqs[user_id] = {**cur, "state": "declined"}
        return {**r, "pickupRequests": reqs}

    row, audit = await _change_session(auth, session_id, change, "pickup_request_declined", user_id, allow_ended=True)
    if audit is not None:
        await _notify_join_answer(row, user_id, "join_declined", current.get("requestId"), kind="pickup")
        await _push_presence(row)
    return _session_public(row, auth.user_id)
```

Add the two audit actions to the action list (~L1700).

`_notify_join_request(row, requester, *, kind="join")`: read `row["pickupRequests"]` when `kind == "pickup"`; title `"<name> wants to pick up <session>"` (add `pickup_request_title(name, row)` beside `join_request_title`), body `"Allow lets everyone in the workspace pick up this session on their Mac."`; activity row gets `"request": "pickup"`; event id namespace `local-session-pickup|…`. `_notify_join_answer(..., *, kind="join")`: pick-up copy per Global Constraints; event id namespace `local-session-pickup-answer|…`.

`_session_public`, beside the join block:

```python
    pick = (row.get("pickupRequests") or {}).get(viewer) if viewer else None
    if pick and not row.get("pickupAllowed"):
        public["pickupRequest"] = pick["state"]
    if viewer and viewer == row["humanUserId"]:
        waiting = pending_pickups(row)
        if waiting:
            public["pendingPickups"] = sorted(waiting)
            public["pendingPickupIds"] = waiting
```

and `LocalSession` fields `pickup_request: Literal["pending", "declined"] | None = None`, `pending_pickups: list[str] | None = None`, `pending_pickup_ids: dict[str, str] | None = None` (add the camel names to whatever owner-only/field list `pending_joins` appears in, ~L276/L321).

`session_pickup.set_pickup`: in the `updated = {...}` dict add `"pickupRequests": {u: r for u, r in (row.get("pickupRequests") or {}).items() if r.get("state") != "pending"}`.

Routes in `routers/messaging.py`, mirroring the join routes:

```python
@router.post("/messaging/sessions/{session_id}/pickup-requests", response_model=local_sessions.LocalSession)
async def request_local_session_pickup(session_id: str, auth: AuthContext = Depends(get_current_user)):
    """A teammate asks the owner to allow pick-up; asking again while pending changes nothing."""
    return await local_sessions.request_pickup_access(auth, session_id)


@router.post("/messaging/sessions/{session_id}/pickup-requests/{user_id}", response_model=local_sessions.LocalSession)
async def decide_local_session_pickup(
    session_id: str, user_id: str, body: local_sessions.JoinDecisionRequest, auth: AuthContext = Depends(get_current_user)
):
    """The owner allows (pick-up on for the session) or declines one person's request."""
    return await local_sessions.decide_pickup_request(auth, session_id, user_id, body.decision, request_id=body.request_id)
```

- [ ] **Step 4: run** the new file plus `test_local_sessions.py test_session_pickup.py test_session_presence.py`; ruff check + format. Expect pass.
- [ ] **Step 5: commit** `feat(sessions): ask the owner to allow pick-up`.

---

### Task 2: Backend presence, device door, activity

**Files:** `apps/backend/core/services/session_presence.py`; `apps/backend/core/services/native_session_tools.py` (`decide`); `apps/backend/core/services/activity.py` (`render_join_request` ~L453, `_run_join_action` ~L495, action routing ~L1745, session rows ~L412); `apps/backend/core/services/local_sessions.py` (`inputPaused` ~L678). Tests: `test_session_presence.py`, `test_native_session_tools.py`, the activity test file (`grep -ln render_join_request tests/`).

**Interfaces — Consumes:** Task 1's `pending_pickups`, `decide_pickup_request`, row field `request: "pickup"`.

- [ ] **Step 1: failing tests**
  1. presence `entry()` lists a pending pick-up request as `{"userId","name","requestId","kind":"pickup"}` and join requests with `"kind":"join"`; the ack trim treats entries with either as holding a request.
  2. `inputPaused` true with only a pending pick-up request (live terminal, presence Mac).
  3. door: `allow` with a `requestId` equal to the user's pending pick-up request → `decide_pickup_request` called, `decide_join` not; a join request id → `decide_join`; same user with both pending → each id routes to its own; an unknown id → `decide_join` (which raises its conflict).
  4. activity: a row with `request: "pickup"` renders title "<Name> wants to pick up <title>", the pick-up reason, id `pickup:<sid>:<user>:<rid>`, open while that pick-up request is pending; running `allow` on it calls `decide_pickup_request`.

- [ ] **Step 2: run, expect failures.**
- [ ] **Step 3: implement**
  - `session_presence.entry`: `joinRequests` = join entries with `"kind": "join"` plus pick-up entries from `sessions.pending_pickups(row)` with `"kind": "pickup"`, sorted by user then kind.
  - `inputPaused`: `(session_presence.pending_joins(row) or sessions.pending_pickups(row)) and …` (rest unchanged).
  - `native_session_tools.decide`, before the join call:

```python
    if body.decision in ("allow", "decline") and local_sessions.pending_pickups(row).get(body.user_id) == body.request_id:
        return await local_sessions.decide_pickup_request(auth, sid, body.user_id, body.decision, request_id=body.request_id)
```

  - `activity.render_join_request`: `pickup = row.get("request") == "pickup"`; read `pickupRequests` when pickup (open also requires `not session.get("pickupAllowed")`); title via `pickup_request_title`; reason per Global Constraints; id `pickup_item_id(...)` = `f"pickup:{sid}:{requester}:{rid}"`. `_run_join_action(..., *, pickup=False)` builds the row with `request` and calls `decide_pickup_request` when pickup. The router at ~L1745 also accepts `parts[0] == "pickup"` → `_run_join_action(..., pickup=True)`.
- [ ] **Step 4: run** those test files; ruff. **Step 5: commit** `feat(sessions): pick-up requests reach presence, the Mac's terminal decisions and Activity`.

---

### Task 3: Desktop passthrough and mod band

**Files:** `apps/desktop/src-tauri/src/local_sessions/presence.rs` (`JoinRequest`); `apps/desktop/src-tauri/mods/lightbulb-sessions/types/index.d.ts`; `.../hooks/register.tsx` (band request row); `.../hooks/state.ts` (add `requestLine`); tests in `presence.rs` and `hooks/band.test.ts`.

- [ ] **Step 1: failing tests**
  - Rust: a frame whose request has `"kind":"pickup"` round-trips through `get()` with `kind == "pickup"`; one without `kind` reads `"join"`; `frozen()` is true for a pickup-only entry.
  - Mod: `requestLine({name:'Mira Shah', kind:'pickup', …}, 1)` → `"Mira Shah wants to pick this up"`; `kind:'join'` → `"Mira Shah asked to join"`; with 3 requests → suffix `" (+2 more)"`. The band test: a state with a pick-up request draws that line and Allow posts `{decision:'allow', userId, requestId}`.
- [ ] **Step 2: run, expect failures** (`cargo test --lib -- presence`, `claude plugin test …`).
- [ ] **Step 3: implement**
  - `JoinRequest`: `#[serde(default = "join_kind")] pub kind: String` with `fn join_kind() -> String { "join".into() }`.
  - `types/index.d.ts`: `JoinRequest` gains `kind: 'join' | 'pickup'`.
  - `state.ts`:

```ts
export function requestLine(r: JoinRequest, count: number): string {
  const verb = r.kind === 'pickup' ? 'wants to pick this up' : 'asked to join'
  return `${r.name} ${verb}` + (count > 1 ? ` (+${count - 1} more)` : '')
}
```

  - `register.tsx`: the request row text becomes `requestLine(ask, s.joinRequests.length)` (helpers taking `$` stay in register.tsx; `requestLine` takes none).
- [ ] **Step 4: run** `cargo check --tests`, `cargo test --lib -- presence mod_socket`, `claude plugin validate` + `claude plugin test` on the mod. The `plugin_files_list` drift test must still pass (no new files). **Step 5: commit** `feat(desktop): the terminal band names pick-up requests`.

---

### Task 4: Shared client and web

**Files:** `packages/shared/src/messaging/types.ts` (LocalSession ~L388-396; audit action union ~L497); `packages/shared/src/messaging/client.ts` (~L92-174); `packages/shared/src/messaging/sessionControls.ts` (audit copy ~L143-154; `useSessionJoinRequest` ~L42 gains a `kind` argument); `apps/frontend/src/components/messaging/LocalSessionPanel.tsx` (~L213 pick-up button; `JoinRequests` ~L297); `apps/frontend/src/components/teams/inbox/AttentionList.tsx` (L37 label). Tests: `sessionControls.test.ts`, `LocalSessionPanel.liveTerminal.test.tsx` (or the panel's main test file).

- [ ] **Step 1: failing tests**
  - shared: `auditLine` for `pickup_requested` → "Aden asked to pick up this session"; `pickup_request_declined` → "declined Mira's request to pick up this session". `useSessionJoinRequest(client, session, "pickup")` posts to `/pickup-requests` and reflects `pickupRequest`; the default kind stays join.
  - web: a non-owner on a Claude session with pick-up off sees "Ask to pick up"; clicking calls `requestLocalSessionPickup`; with `pickupRequest: "pending"` the button reads "Requested" (disabled); with `"declined"` "<Owner> declined. Ask again"; with pick-up allowed and ready the existing "Pick up" shows instead; a Codex session shows neither. The owner sees "<Name> wants to pick up this session." with Allow/Decline calling `decideLocalSessionPickup(sid, who, decision, pendingPickupIds[who])`.
- [ ] **Step 2: run, expect failures** (`cd packages/shared && npx vitest run src/messaging/sessionControls.test.ts`; frontend vitest on the panel test).
- [ ] **Step 3: implement**
  - types: `pickupRequest?: "pending" | "declined" | null; pendingPickups?: string[]; pendingPickupIds?: Record<string, string>;` and the two audit actions.
  - client: `requestLocalSessionPickup(sessionId)` → `POST …/pickup-requests`; `decideLocalSessionPickup(sessionId, userId, decision, requestId?)` → `POST …/pickup-requests/{userId}`, mirroring the join pair (interface + implementation).
  - `sessionControls.ts`: audit copy; `useSessionJoinRequest(client, session, kind = "join")`: for `"pickup"` it reads `session.pickupRequest` and calls `requestLocalSessionPickup` (same return shape); add `requestLocalSessionPickup` to the `SessionControlClient` pick list (~L19).
  - `LocalSessionPanel.tsx`: generalize `JoinRequests` to take `{ waiting: string[]; ids?: Record<string,string>; line: (name: string) => string; decide: (who, decision, id?) => Promise<unknown> }` and render it twice (join, pick-up) for the owner. For a non-owner Claude session: `canPickUp(session)` → existing `SessionPickup`; else an `AskToPickUp` button using `useSessionJoinRequest(client, session, "pickup")` with the copy above.
  - `AttentionList.tsx`: label `join_request: "Request"`.
- [ ] **Step 4: run** the tests + `npx eslint` on changed files. **Step 5: commit** `feat(sessions): Ask to pick up on web`.

---

### Task 5: Phone — the owner answers

**Files:** `apps/mobile/src/components/session/SessionJoin.tsx` (`JoinRequests` ~L56); the session screen that renders it (`grep -rn "JoinRequests" apps/mobile/src/app`); test `apps/mobile/src/app/(app)/__tests__/session.test.tsx`.

- [ ] **Step 1: failing test** — the owner's session screen with `pendingPickups: ["user_mira"]` shows "Mira wants to pick up this session." and Allow calls `decideLocalSessionPickup` with `pendingPickupIds.user_mira`.
- [ ] **Step 2: run, expect failure** (`cd apps/mobile && npx jest src/app/\(app\)/__tests__/session.test.tsx`).
- [ ] **Step 3: implement** — generalize `JoinRequests` the same way as web (`waiting`, `ids`, `line`, `decide`) and render it for pick-up requests too; the decider type gains `decideLocalSessionPickup?`.
- [ ] **Step 4: run** the test + `npx tsc --noEmit -p apps/mobile` (or the project's typecheck script) + eslint on changed files. **Step 5: commit** `feat(mobile): answer pick-up requests on the phone`.

---

## After the tasks

Whole-branch review, PR. Ships: backend + web via the next prod dispatch; desktop build (Task 3) after the backend; the merge publishes an iPhone OTA (Tasks 4-5 touch `packages/shared` and `apps/mobile`). Delete this plan in the PR; delete the spec after prod acceptance.
