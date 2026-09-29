# Mac browser over raw CDP — implementation plan

Spec: `docs/superpowers/specs/2026-09-28-mac-browser-raw-cdp-design.md`. Read it first; section
numbers below (§) refer to it. Delete both docs when the train ships.

## Global Constraints

**Rulings (owner). Every task must honor these.**

- Consent is given once, at pairing (the Browser switch). There is never a per-operation prompt.
- Agents use only the agent's own Chrome (`<app_data>/agent-chrome`), never the person's everyday
  profile, and never an endpoint the backend or the model names.
- Room members see Mac frames **read-only**. Input is never dispatched to a Mac cast.
- No compat shims and no feature flags. Old and new paths coexist only until tasks 8-10, which
  delete the old one. Fix forward.
- Desktop checks happen on **prod as the founder**. Never propose a dev desktop build.
- Workspace sign-ins are shared, and the first owner wins per Mac. Keep these from #3377 / #3395 /
  #3384:
  - `load_id` fencing, meaning every claim, base write and fence is a conditional write;
  - vetting of Mac-supplied state;
  - clearing on unpair and on member removal;
  - wipe on Browser-off.

**Security invariants (the reviewer checks each).**

- No cookie, storage value, ticket, presigned URL or CDP payload appears in a log line, metric
  dimension, error message or tool result. Log exception class names only.
- Tickets are single-use and live 60 s (§5). Every door connection re-checks authority every 30 s.
- The Mac pipes only to its own `DevToolsActivePort` endpoint, through the method filter (§3).

**Working rules.**

- The disk is tight: run **only** the test commands each task lists. No full suites and no builds;
  CI is the full gate.
- Each task ships as **its own PR to `main`**, off fresh `origin/main` in its own worktree (never
  stacked on another agent's branch), and each is safe to deploy alone in the order below. Do not
  push unless told to. Commit messages end with the session's attribution lines.
- **Rulings 2026-09-28 (ledger `.superpowers/sdd/2026-09-28-mac-browser-raw-cdp/progress.md`):**
  Paperclip runs may use a Mac's browser via the existing `desktop.operation.approve.v1` consent
  step; **no `pair_state` bump** (grants are derived server-side and the derivation survives task
  9); no Chrome policy file; the legacy profile folder is wiped once in task 10.
- **Architecture-review rulings 2026-09-28:** ONE sync engine — the bridge runs the Mac's load and
  save with the cloud's `browser_state.py` over the run's own pipe, before `run.ended`; the backend
  only decides, fences and publishes (no state tickets, no backend `browser.state` callback).
  Zero-argument `use_browser` (the requester's ready Mac; generation resolved at acquire). The Mac
  browser may reach localhost and the local network. The agent window is a "Lightbulb agent"
  profile with a theme colour. CDP frames get their own send lane, ahead of invoke traffic, on both
  ends of the node socket.
- Backend Python: repositories return plain dicts. Use `core.dynamodb.run_in_thread` for boto3.
  Raise `DesktopError(code, status, message)` for anything user-facing; the message is fixed text
  from spec §7.
- Any change under `apps/infra/lib/`: run
  `cd apps/infra && CDK_DOCKER=/usr/bin/false npx cdk synth > /dev/null` and commit a regenerated
  `.github/workflows/deploy.yml` if it changed (`infra-ci.yml` enforces this).
- Desktop signing (#3371 / #3395): this train adds **no** new binary and no entitlement. Any
  binary that stays is signed in `Contents/Resources/bin` with the hardened runtime.
  `entitlements.plist` is not touched.
- Code review: Codex only. Check all four surfaces (review, inline, issue comment, 👍).

**Deploy units and order.** Each merge is safe to deploy alone (spec §8).

| Order | Tasks | Unit |
|---|---|---|
| 1 | 1, 2, 3 | backend + infra (`gh workflow run deploy.yml --ref main` for prod) |
| 2 | 4, 5 | Hermes image: build → `hermes-version.json` `dev.tag` → validate → `prod.tag` → `apps/backend/scripts/roll_hermes_runtimes.py` |
| 3 | 6 | frontend (with the next backend dispatch) |
| 4 | 7 | desktop (`desktop-build.yml`, signed) |
| — | — | prod verification checklist (end of this file) |
| 5 | 8 | Hermes |
| 6 | 9 | backend + frontend |
| 7 | 10 | desktop |

**Test commands (only the files a task names).**

- Backend: `cd apps/backend && uv run pytest <files> -q`
- Bridge / plugin: `cd apps/infra/hermes && uv run --no-project --with pytest --with botocore --with pyyaml --with websockets python -m pytest <files> -q`
- Frontend: `cd apps/frontend && npx vitest run <files>`
- Infra: `cd apps/infra && npx vitest run test/service-stack.test.ts`
- Desktop: CI runs `cargo test --lib` on the PR (`desktop-build.yml`). Locally run
  `cd apps/desktop/src-tauri && TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --lib <module>::`
  **only if more than 10 GB is free**; otherwise say so in the report and rely on CI.

---

### Task 1: Backend — CDP channel on the native node socket, and the two new grants

**Goal.** A backend task can open, feed and close a CDP channel on a paired Mac's native socket.
Nothing calls it yet.

**Files.**

- `apps/backend/core/services/node_broker.py`
- `apps/backend/core/services/desktop_pair.py`
- `apps/backend/core/services/desktop_execution.py` (only `PUBLIC_COMMANDS`)
- `apps/backend/core/services/connection_service.py` is **not** touched: `_resolve_desktop_target`
  already intersects the ready row with `granted_commands`, so the derivation below reaches it.
- Tests: `apps/backend/tests/unit/services/test_node_broker.py`,
  `apps/backend/tests/unit/services/test_desktop_pair.py`,
  `apps/backend/tests/unit/services/test_desktop_execution.py`

**Interfaces (`node_broker.py`).**

```python
SEND_QUEUE = 1024                      # NodeSession.send maxsize (was 16)
CDP_COMMAND = "browser.cdp.v1"
MAX_CDP_CHANNELS = 4
MAX_CDP_FRAME = 16 * 1024 * 1024       # bytes of one `data` string, either direction
CDP_INBOUND_QUEUE = 256
CDP_OPEN_TIMEOUT_S = 15

class CdpClosed(Exception):
    def __init__(self, code: str): self.code = code   # Mac close code or "CLOSED"/"TOO_LARGE"/"TIMEOUT"/"OVERFLOW"

class CdpChannel:
    id: str                              # uuid4
    inbound: asyncio.Queue               # str frames from the Mac (maxsize CDP_INBOUND_QUEUE)
    closed: asyncio.Event                # set exactly once when the channel ends (any cause)
    opened: asyncio.Future               # resolves on browser.cdp.opened; CdpClosed on close
    close_code: str | None
    async def next(self) -> str | None   # the next frame, or None once `closed` is set and inbound is drained

class NodeSession:
    cdp: dict[str, CdpChannel]
    async def cdp_open(self, profile_id: str) -> CdpChannel
    def cdp_send(self, channel: CdpChannel, data: str) -> None
    def cdp_close(self, channel: CdpChannel, code: str = "CLOSED") -> None
```

**Behavior.**

- **`cdp_open`**
  - Raises `CommandNotAllowed(CDP_COMMAND)` unless `CDP_COMMAND in self.commands`.
  - Raises `NodeCapacity` at `MAX_CDP_CHANNELS` open channels or when `send` is full.
  - Otherwise it queues `{"type":"event","event":"browser.cdp.open","payload":{"channel","nodeId","profileId"}}`
    and awaits `opened` for up to `CDP_OPEN_TIMEOUT_S`. On timeout it calls `cdp_close(...,"TIMEOUT")`
    and raises `CdpClosed("TIMEOUT")`.
- **`cdp_send`**
  - Raises `CdpClosed("TOO_LARGE")` when `len(data.encode()) > MAX_CDP_FRAME`, and `CdpClosed`
    when the channel is closed.
  - `put_nowait`s the frame event; a full queue raises `NodeCapacity`.
- **`cdp_close`**
  - Idempotent. Sends one `browser.cdp.close` event when the channel is still registered (if
    `send` is full, the close event is dropped: the Mac closes it on its side when the socket
    or Chrome ends), removes it, records `close_code`, and sets `closed`. It **never** puts into
    `inbound` (a full queue must not raise inside the relay reader).
- **`resolve(frame)`**
  - An `event` frame named `browser.cdp.opened` / `browser.cdp.frame` / `browser.cdp.close` is
    routed by `payload.channel` and **returns None** (no ack). An unknown channel is ignored.
  - `opened` resolves the future.
  - `frame` puts `data` on `inbound` with `put_nowait`. `data` over `MAX_CDP_FRAME` or not a `str`
    leads to close `TOO_LARGE`; a full `inbound` (`QueueFull`) leads to close `OVERFLOW`. Neither
    path may raise out of `resolve`.
  - `close` sets `close_code`, fails `opened` with `CdpClosed(code)`, sets `closed`, and removes
    the channel (no close event is echoed back).
- **`fail_pending`**
  - Closes every channel locally: `closed` set, `opened` failed with `CdpClosed("CLOSED")`.
- **Invokes are unchanged.** `invoke()` still raises `NodeCapacity` when `self.send.full()`, now
  at 1024. Existing tests that fill the queue fill `SEND_QUEUE`.
- (Built as cd0ff3911 + 543110f29. Superseded by Task 3 step 3a: CDP moves to its own lane and
  `SEND_QUEUE` returns to 16.)

**Interfaces (`desktop_pair.py`).**

- `DESKTOP_MAC_COMMANDS` gains `"browser.cdp.v1"` and `"browser.wipe.v1"`.
- `DESKTOP_MAC_BASE_COMMANDS` gains `"browser.wipe.v1"` (always granted).
- `DesktopPermissionGrant.mac_commands()` adds `"browser.cdp.v1"` when `self.browser`.
- `granted_commands(devices, entry)` **derives** both new commands for an `openclaw-macos` entry
  (ruling: no re-pair, no `pair_state` bump). Existing pairs keep their stored `commands`, and
  `NodeSession.commands` is fixed at connect from `declared_commands_for` → `granted_commands`
  (gateway_relay.py:265), so without the derivation `cdp_open` and the wipe raise
  `CommandNotAllowed` on every pair made before this task:
  - `browser.wipe.v1`: always, for every `openclaw-macos` entry with a `pairingGroupId`;
  - `browser.cdp.v1`: when the reciprocal `node-host` peer (same `pairingGroupId`, same `userId`)
    has `"browser.proxy"` in its stored `commands` — the same predicate that grants
    `browser.state.v1` today (#3395). Factor the predicate into `_browser_granted(devices, entry)`
    and use it for both.
  - Task 9 keeps this derivation (it keys on the stored string, not on the constant it deletes).

**`desktop_execution.py`.** `PUBLIC_COMMANDS |= {"browser.cdp.v1"}`. It is listed by `devices`, and
`validate_request` still refuses it with `unsupported_command`.

**Tests to add.**

- `test_node_broker.py`:
  - `test_cdp_open_sends_open_event_and_waits_for_opened`
  - `test_cdp_open_refused_by_mac_raises_with_code`
  - `test_cdp_open_times_out_and_sends_close`
  - `test_cdp_open_requires_granted_command`
  - `test_cdp_channel_cap`
  - `test_cdp_frames_route_to_their_channel_only`
  - `test_cdp_oversized_mac_frame_closes_channel`
  - `test_cdp_inbound_overflow_closes_with_overflow_without_raising` (fill `inbound`, then one more
    frame through `resolve`: no exception, `close_code == "OVERFLOW"`, `closed` set)
  - `test_cdp_send_rejects_oversized_frame`
  - `test_cdp_close_is_idempotent_and_sends_one_close_event`
  - `test_fail_pending_closes_cdp_channels`
  - `test_invokes_still_admitted_while_cdp_frames_queued`
- `test_desktop_pair.py`:
  - `test_browser_grant_adds_cdp_command` (new pairs, via `mac_commands`)
  - `test_existing_pair_derives_cdp_from_browser_grant`
  - `test_existing_pair_without_browser_gets_no_cdp`
  - `test_wipe_is_derived_for_every_native_entry`
  - `test_declared_commands_for_includes_derived_commands`
- `test_desktop_execution.py`:
  - `test_cdp_command_is_listed_but_not_submittable`

**Run.** `cd apps/backend && uv run pytest tests/unit/services/test_node_broker.py tests/unit/services/test_desktop_pair.py tests/unit/services/test_desktop_execution.py tests/unit/routers/test_gateway_relay_agentcore.py -q`

**Done when.**

- The listed tests pass, and the relay test still passes unchanged: `gateway_relay._pump` needs no
  edit, because its reader already passes every frame to `resolve`.
- No other module changed.

---

### Task 2: Backend — the ticket and the Mac CDP door (+ infra ingress)

**Goal.** A runtime in the VPC can dial `ws://<holder IP>:8000/api/v1/internal/mac-cdp/<ticket>`
and get a byte pipe to the Mac's agent Chrome, with authority re-checked every 30 s.

**Files.**

- New `apps/backend/core/services/mac_cdp.py`
- New `apps/backend/routers/mac_cdp.py`
- `apps/backend/main.py`: `app.include_router(mac_cdp.router, prefix="/api/v1/internal/mac-cdp", tags=["mac-cdp"])`
- `apps/infra/lib/stacks/service-stack.ts`
- `apps/infra/test/service-stack.test.ts`
- Tests: `apps/backend/tests/unit/services/test_mac_cdp.py`,
  `apps/backend/tests/unit/routers/test_mac_cdp_door.py`

**Interfaces (`mac_cdp.py`).**

```python
TICKET_SECONDS = 60
RECHECK_SECONDS = 30
AGENT_MAX_SECONDS = 9 * 3600
PATH = "/api/v1/internal/mac-cdp/"

class TicketError(Exception):
    def __init__(self, code: str): self.code = code   # "invalid" | "expired" | "replayed" | "wrong_task"

def mint(*, target: dict, profile_id: str, auth: dict) -> str
    # Agent tickets only (no "state" use: the bridge syncs over the run's own pipe).
    # target = a resolve_desktop_target() dict for the NATIVE device; auth = exactly what
    # authorize_run returned, room
    # {ownerId, memberId, runId, agentId, family, roomId} or Paperclip
    # {ownerId, companyId, runId, agentId, family}. Claims carry family + companyId (spec §5);
    # absent fields are "".
def door_url(target: dict, ticket: str) -> str
    # "ws://" + urlsplit(target["taskAddress"]).netloc + PATH + ticket
def redeem(ticket: str) -> dict
    # decrypt (Fernet, key = sha256("mac-cdp-ticket-v1:" + WS_INTERNAL_SECRET)), exact claim set
    # per spec §5, purpose check, iat/exp (exp == iat + TICKET_SECONDS, 5 s skew), address ==
    # task_address.own_address(), nonce unused (in-process dict pruned after exp + 5). Raises TicketError.
def auth_from_claims(claims: dict) -> dict
    # Rebuild the exact authorize_run dict for the claim's family (valid_auth() must hold).
async def check(claims: dict) -> dict
    # Re-resolve the native target (ConnectionService._resolve_desktop_target with "browser.cdp.v1"),
    # generation == claims["generation"], desktop_execution.submissions_enabled(owner),
    # desktop_execution._reauthorize(auth_from_claims(claims)) (active=True). Returns the target.
    # Raises DesktopError. Task 3 adds mac_browser.check_holder(owner, device, profileId) here.
async def serve(websocket, ticket: str) -> None
```

**How `serve` behaves.**

1. `accept()`.
2. `redeem`. A `TicketError` closes 4401.
3. `check`. A generation or task mismatch closes 4409; any other `DesktopError` closes 4403.
4. `session = desktop_execution._session(target)` (it already checks `node_sessions` and the
   generation); its `DesktopError` closes 4409.
5. `channel = await session.cdp_open(claims["profileId"])`. `CdpClosed(code)` closes 4502 with
   `reason=code`; `NodeCapacity` closes 4503 (`reason="CAPACITY"`); `CommandNotAllowed` closes 4403.
6. Pump both ways until either side ends:
   - client text → `session.cdp_send`; `CdpClosed(code)` closes 4502 with that code,
     `NodeCapacity` closes 4503 (a burst never becomes an unmapped 1011);
   - `await channel.next()` → client. `None` ends it: 1000 only when the **Mac** sent
     `close{CLOSED}`; `OVERFLOW`, `TOO_LARGE`, `TIMEOUT` or any Mac error code close 4502 with
     that code, so an abnormal loss never looks like a normal close.
7. A recheck task runs `check(claims)` every `RECHECK_SECONDS`; a failure closes 4403.
8. The connection is capped at `AGENT_MAX_SECONDS`.
9. `finally`: `session.cdp_close(channel)`.
10. Never log the ticket, the frames or a claim value other than the owner.
11. Meter `mac_cdp.door` with dims `outcome` ∈ {ok, bad_ticket, lost, forbidden, refused, error}.

**Router (`routers/mac_cdp.py`).**

```python
router = APIRouter()

@router.websocket("/{ticket}")
async def mac_cdp_door(websocket: WebSocket, ticket: str) -> None:
    await mac_cdp.serve(websocket, ticket)
```

**Infra.** In `service-stack.ts`, next to the peer-forwarding rule:

```ts
serviceSg.addIngressRule(props.container.containerSecurityGroup, ec2.Port.tcp(8000),
  "AgentCore runtimes to the ticketed Mac CDP door on the task holding the Mac's socket");
```

Test in `service-stack.test.ts`, named `"lets AgentCore runtimes reach the Mac CDP door on 8000"`:
the template has an `AWS::EC2::SecurityGroupIngress` (or an inline ingress) on the service SG with
`FromPort: 8000`, `ToPort: 8000` and `SourceSecurityGroupId` = the container SG.

**Tests to add.**

- `test_mac_cdp.py`:
  - `test_ticket_round_trip_claims`
  - `test_ticket_expired`
  - `test_ticket_single_use`
  - `test_ticket_minted_for_another_task_is_refused`
  - `test_tampered_ticket_is_invalid`
  - `test_mint_takes_no_state_use`
  - `test_paperclip_ticket_rebuilds_paperclip_auth` (`auth_from_claims` round trip, `valid_auth`)
  - `test_door_url_uses_task_host_and_port`
  - `test_check_refuses_disabled_owner`
  - `test_check_refuses_generation_change`
- `test_mac_cdp_door.py` uses Starlette `TestClient` websockets and a fake `NodeSession` registered
  in `gateway_relay.node_sessions`:
  - `test_door_pipes_frames_both_ways`
  - `test_door_closes_4401_on_bad_ticket`
  - `test_door_closes_4409_when_socket_generation_gone`
  - `test_door_closes_4502_with_mac_code`
  - `test_door_overflow_is_4502_not_1000`
  - `test_door_send_capacity_closes_4503`
  - `test_door_recheck_closes_4403_when_run_ends`, with `RECHECK_SECONDS` patched to 0.05
  - `test_agent_connection_is_capped`
  - `test_client_close_closes_the_channel`
  - `test_ticket_never_logged`, via `caplog`

**Run.**

- `cd apps/backend && uv run pytest tests/unit/services/test_mac_cdp.py tests/unit/routers/test_mac_cdp_door.py -q`
- `cd apps/infra && npx vitest run test/service-stack.test.ts`, then the synth rule from Global
  Constraints.

**Done when.** The tests pass, `deploy.yml` drift is committed (or none), and nothing outside these
files changed.

---

### Task 3: Backend — CDP priority lane, Mac browser lease + sync instructions, revocation, frames

**Goal.** The bridge can `acquire` and `release` a Mac's browser for a room or Paperclip run, with
the Mac picked by default. The backend decides and fences the sign-in sync (first owner wins,
`load_id`), but it **never runs it**: the bridge runs load and save with its own
`browser_state.py`, exactly like a cloud session (ruling: ONE sync engine). Unpair or member
removal wipes the profile. CDP frames get their own send lane on the node socket.

**Files.**

- Step 3a (lane): `apps/backend/core/services/node_broker.py`, `apps/backend/routers/gateway_relay.py`
  (the `writer` leg of `_pump`), tests `tests/unit/services/test_node_broker.py`,
  `tests/unit/routers/test_gateway_relay_agentcore.py`.
- New `apps/backend/core/services/mac_browser.py`.
- `apps/backend/core/services/mac_cdp.py`: `check()` also calls
  `mac_browser.check_holder(owner, device, claims["profileId"])`.
- `apps/backend/routers/container.py`: `browser_internal_router` gains `POST /{owner_id}/mac/acquire`,
  `POST /{owner_id}/mac/release` and `POST /{owner_id}/mac/synced`, all behind hook-token auth like
  `/acquire`, with `Cache-Control: no-store`. **Each route then builds `auth` with
  `desktop_execution.authorize_run(owner_id, body.runId, body.agentId, body.family, active=True)`**
  — for all three. The bridge checkpoints the Mac **before** it reports `run.ended` (Task 5), so the
  run is still active; a `run_not_active` on release/synced answers `{"sync": null}` / `{"ok": false}`
  (the Mac keeps its changes). The hook token alone never authorizes a runId. Errors answer
  `HTTPException(exc.status, detail={"code": exc.code, "message": exc.message})`.
- `apps/backend/routers/desktop_execution.py`: the `/internal` action set gains `mac_acquire` and
  `mac_release`, body `{"action","auth","request":{"deviceId"}}`. The existing `authorize_run`
  re-check uses `active=True` for both (today it is `active=action == "admit"`); a `DesktopError`
  answers `detail={"code","message"}`. These actions
  run outside `_boundary()`'s 9 s timeout, under `mac_browser.FORWARD_TIMEOUT_S`.
- `apps/backend/core/services/browser_control.py`: in `watch()`, the frames RPC also fires while
  `mac_browsing_at` is younger than `mac_browser.LEASE_SECONDS`.
- `apps/backend/routers/desktop_pair.py`: `delete_device` also awaits
  `mac_browser.clear_device(owner_id, device_id)` (bounded `mac_browser.CLEAR_SECONDS`, logged,
  never raises), beside the old clear.
- `apps/backend/core/services/desktop_browser_state.py`: `revoke_member_soon.run()` also awaits
  `mac_browser.clear_member(owner, member_id)` inside the same `CLEAR_MEMBER_SECONDS` budget. This
  is the only edit to the old module; task 9 moves `revoke_member_soon` into `mac_browser.py`.
- Tests:
  - `apps/backend/tests/unit/services/test_mac_browser.py`
  - `apps/backend/tests/unit/routers/test_mac_browser_routes.py`
  - `apps/backend/tests/unit/services/test_browser_control.py` (extend)
  - `apps/backend/tests/unit/routers/test_desktop_pair_router.py` (extend)

#### Step 3a — CDP frames get their own send lane (do this first; Tasks 1-2 are already built)

CDP frames must never queue behind node invoke traffic (owner ruling: the proven fast way). Today
(after Task 1's fixes) `NodeSession.send` is one `_ByteQueue(SEND_QUEUE=1024)` shared by invokes,
cancels and `browser.cdp.*` events, with `CDP_SEND_BYTES` checked against `send.bytes`.

```python
# node_broker.py
SEND_QUEUE = 16                        # invoke/control lane, back to its pre-Task-1 size
CDP_SEND_QUEUE = 1024                  # CDP lane, frames
CDP_SEND_BYTES = 64 * 1024 * 1024      # unchanged; now checked against cdp_out.bytes
CDP_BURST = 64                         # max consecutive CDP frames before one waiting control frame

class NodeSession:
    send: _ByteQueue                   # invokes, cancels, node.invoke.* only
    cdp_out: _ByteQueue                # browser.cdp.open/frame/close only (_ByteQueue(CDP_SEND_QUEUE))
    async def next_outbound(self) -> dict
        # cdp_out first; after CDP_BURST CDP frames in a row, one waiting `send` frame goes
        # (no starvation). Waits on whichever lane fills first when both are empty.
```

- `cdp_open`, `cdp_send`, `cdp_close` put on `cdp_out`, never on `send`. `cdp_send` raises
  `NodeCapacity` when `cdp_out` is full or `cdp_out.bytes + len(data) > CDP_SEND_BYTES` (the byte
  budget is kept; `MAX_CDP_FRAME` still applies per frame). `cdp_close` drops its close event when
  `cdp_out` is full, as it does today with `send`.
- `invoke` / `cancel` / `_drop_queued` keep using `send`; their `full()` checks regain their
  original meaning at 16. `fail_pending` also drains `cdp_out`.
- `gateway_relay._pump`'s `writer` becomes
  `while True: await websocket.send_text(json.dumps(await session.next_outbound()))` — still one
  writer, so starlette never sees concurrent sends. `test_gateway_relay_agentcore.py` fakes that
  read `session.send` directly move to `next_outbound`.
- Tests: `test_cdp_frames_skip_ahead_of_queued_invokes`, `test_control_frame_not_starved_by_cdp_burst`,
  `test_cdp_lane_byte_budget_raises_capacity`, `test_invoke_capacity_unaffected_by_cdp_backlog`,
  `test_fail_pending_drains_cdp_lane` (node_broker); `test_writer_drains_cdp_lane_first`
  (gateway_relay). Task 1 tests that fill `send` with CDP frames are re-pointed at `cdp_out`.

**No state tickets.** Task 2 mints **agent tickets only**; nothing here mints or relies on a
`use: "state"` ticket, and the backend never calls `container_rpc` `browser.state` for a Mac. The
bridge runs every sync over the run's own agent-ticket pipe.

**Messages on every error.** Every `DesktopError` that leaves `mac_browser` carries the spec §7
text: raise through `_err(code, status) -> DesktopError(code, status, MESSAGES[code])`, and wrap
anything re-raised from `desktop_execution._resolve`, `_dispatch`, `_resolve_companion` or
`mac_cdp.check()` (if reused) with `_with_message(exc)`, which fills `exc.message` from `MESSAGES`
when it is empty. Test: `test_every_raised_error_has_a_message`.

**Interfaces (`mac_browser.py`).**

```python
ROW_PREFIX = "__macchrome__#"            # + native device id; reserved key, skipped by scans
ENSURE_TIMEOUT_S = 20
WIPE_TIMEOUT_S = 20
FORWARD_TIMEOUT_S = 40                    # > ENSURE_TIMEOUT_S + a Chrome launch; forwarded acquire/release
LEASE_SECONDS = 300
CLAIM_SECONDS = 180                       # the bridge's sync must report back within this
DEFER_SECONDS = 60
CLEAR_SECONDS = 15
STATE_URL_SECONDS = 180                   # presign lifetime (browser_control._state_url)
STARTING = {"status": "starting"}
MESSAGES: dict[str, str]                  # every spec §7 code -> its exact message

async def acquire(auth: dict, device_id: str | None, *, forwarded: bool = False) -> dict
async def release(auth: dict, device_id: str, generation: str, *, forwarded: bool = False) -> dict
async def synced(auth: dict, device_id: str, sync_id: str, ok: bool) -> dict
async def check_holder(owner: str, device_id: str, profile_id: str) -> None   # DesktopError 403
    # row loaded_owner == owner and row profile_id == profile_id. No other exception: a Fresh claim
    # relabels the row to this owner at claim time (below), so the run's own ticket passes.
async def clear_device(owner: str, device_id: str) -> bool                    # either device of the pair
async def clear_member(owner: str, member_id: str) -> None                    # never raises
```

**Row `__macchrome__#<native device>`** (containers table). Fields:

| Field | Meaning |
|---|---|
| `loaded_owner` | str or None — the workspace whose sign-ins this profile holds |
| `profile_id` | the Mac's agent-chrome id |
| `load_id` | the fence: new hex on every claim or fence |
| `base_key` | the Mac's merge base, `browser-state/<owner>/macbase-<hex>.json`, or None |
| `synced_key` | the workspace `saved_key` as of the Mac's last load or save |
| `loading_owner`, `loading_expires` | the sync claim (serializes syncs; `load_id` names it) |
| `pending` | the claimed sync: `{"steps": [...keys...], "expected": <workspace saved_key>, "base_out": <key>, "saved_out": <key or None>}` |
| `deferred_until` | epoch; a failed load's 60 s back-off |
| `leases` | map runId → epoch until |

Every write is conditional on the `load_id` it read, as in the old `_put_mac_row`. Port its
condition logic and its `LoadSuperseded` exception; do not import the old module.

**Acquire flow (spec §6.1).** `auth` is what the route got from `authorize_run(..., active=True)`
(room **or** Paperclip).

1. **Pick the Mac** (`use_browser` takes no generation and an optional device):
   - `device_id` given: either device of a pair; map it to the pair's native (`openclaw-macos`)
     device with `paired_physical_mac`.
   - Omitted, room run: the requester's (`auth["memberId"]`) paired native devices whose ready row
     exists. None → `no_mac` (409). More than one → `choose_mac` (409, the message lists nothing
     but asks the agent to pass the `deviceId` from `lightbulb_desktop devices`).
   - Omitted, Paperclip run: exactly one ready native Mac in the workspace whose Paperclip consent
     can be asked; otherwise `choose_mac` / `no_mac`.
   - The **generation comes from the ready row** (`ConnectionService.desktop_ready_id`), never from
     the agent, so a Mac that slept and reconnected is picked up on the next acquire.
2. Resolve the native target: `desktop_execution._resolve(auth, {"deviceId", "expectedConnectionGeneration": <ready generation>, "command": "browser.cdp.v1"})`.
   `_resolve` already converts the connection errors into `DesktopError` codes
   (desktop_execution.py:889-890), so map **codes**, not exception classes:
   - on `desktop_forbidden` (the command is not effective), read the native ready row: `commands`
     lack `browser.wipe.v1` → `desktop_update_required` (old app; `browser.wipe.v1` is the new-app
     marker, spec §4.1); have it but lack `browser.cdp.v1` → `browser_off`; otherwise keep
     `desktop_forbidden`;
   - `desktop_not_ready` / `desktop_generation_stale` → `desktop_generation_lost`.
3. If `target["taskAddress"] != own_address()`, forward the whole acquire to the holder with a
   dedicated helper `_forward_mac(address, body)` (httpx, `timeout=FORWARD_TIMEOUT_S`, no retry,
   internal-secret header). Do **not** reuse `desktop_execution._forward` as is: its 8 s timeout is
   shorter than `ensure`, and it drops `detail.message`. `_forward_mac` re-raises
   `DesktopError(detail.code, status, detail.message or MESSAGES.get(code))`. A forwarded call that
   lands on a non-holder raises `desktop_forward_loop`.
4. `submissions_enabled(owner)` else `desktop_disabled`.
5. Paperclip only: run the existing consent step once for this acquire —
   `desktop_execution._resolve_companion(auth, target, CONSENT_COMMAND)`, build the same
   binding/params `DesktopOperations._paperclip_consent` builds (factor that body into a module
   function `paperclip_consent(auth, target, operation: dict, deadline: float) -> None` and call it
   from both places; `operation = {"command": "browser.cdp.v1", "deviceId", "expectedConnectionGeneration", "runId", "agentId"}`),
   and dispatch `desktop.operation.approve.v1`. The Mac answers from its stored switches with no
   prompt. Anything but `{"approved": True, "binding": ...}` → `desktop_consent_denied` (403).
   Extend `handle_approval_peer`'s `PUBLIC_COMMANDS` check if it refuses `browser.cdp.v1`.
6. Dispatch `browser.cdp.v1 {"action":"ensure"}` (`desktop_execution._dispatch`,
   `ENSURE_TIMEOUT_S`). Map `BROWSER_OFF` → `browser_off`, `CHROME_MISSING` → `chrome_missing`,
   anything else → `chrome_start_failed`. Keep `profileId`.
7. Read the row. Holder is another still-paired workspace (`_still_paired`, as in the old
   `_row_holder`), or an unexpired claim by another owner → `browser_profile_held_by_another_workspace`.
   An unexpired claim by this owner → `STARTING` (a sync is running in some bridge).
8. **Stale holder, same profile** (the row names another owner who is no longer paired, and
   `profile_id == profileId`): the profile still holds their sign-ins. Fence the row, `_spawn` a
   `browser.wipe.v1` dispatch (`WIPE_TIMEOUT_S`) on the holder, answer `STARTING`. The next acquire
   sees a new profile id.
9. Decide, **in this order**:
   1. **Deferred**: `loaded_owner == owner`, `profile_id == profileId`, `base_key` None and
      `deferred_until > now` → ready, no sync.
   2. **Fresh** (`loaded_owner != owner`, or `profile_id != profileId`, or no `base_key`): **claim
      and relabel in one conditional write** — `loaded_owner=owner`, `profile_id=profileId`,
      `base_key=None`, new `load_id`, `loading_owner=owner`, `loading_expires=now+CLAIM_SECONDS`,
      `pending`. Sync = one step:
      - with a workspace save: `{"action": "load", "stateUrl": <saved_key GET>, "baseOutUrl": <new Mac base key PUT>}`;
      - without one: `{"action": "base", "stateUrl": <new Mac base key PUT>}` (the bridge's `base`
        writes its export to `stateUrl`; that key becomes `base_key`).
   3. **Stale** (`synced_key != workspace saved_key`, base present, and no other run's lease on this
      device is live): claim (not relabel). Sync = two steps, in order:
      `{"action": "save", "previousStateUrl": <saved_key GET>, "baseStateUrl": <base_key GET>, "stateUrl": <new workspace key PUT>, "baseOutUrl": <throwaway base PUT>}`
      then `{"action": "load", "stateUrl": <that new workspace key GET>, "baseOutUrl": <new Mac base key PUT>}`.
   4. Otherwise ready, no sync.
10. **Answer** (200), always with the pipe:
    ```json
    {"deviceId": "<native>", "generation": "<ready generation>", "cdpUrl": "<door url, agent ticket>",
     "humanInputAt": 0, "framesAt": <human_frames_at or null>,
     "sync": null | {"syncId": "<load_id>", "steps": [ ... as above ... ]}}
    ```
    plus `leases[runId] = now + LEASE_SECONDS` (conditional on `load_id`) and
    `container_repo.touch_browser_input(owner, now, field="mac_browsing_at", create=True)`.
    All presigned URLs come from `browser_control._state_url` (180 s) and live under
    `browser-state/<owner>/`. The keys are recorded in `pending` so `synced` never trusts a key the
    bridge names.

**Release flow (spec §6.2).** `POST mac/release {runId, agentId, family, deviceId, generation}`,
called by the bridge while the run is still active (before `run.ended`).

- Drop `leases[runId]`. Another lease live → `{"sync": null}`.
- Row has **no `base_key`** (a failed, deferred or not-yet-reported load) → `{"sync": null}`: the
  bridge treats a missing base as empty (browser_state.py:296), which would let every stale Mac
  domain overwrite the workspace's sign-ins.
- Holder is not this owner, or an unexpired claim exists → `{"sync": null}`.
- Otherwise claim and answer `{"sync": {"syncId", "steps": [{"action": "save", "previousStateUrl", "baseStateUrl", "stateUrl": <new workspace key PUT>, "baseOutUrl": <new Mac base key PUT>}]}}`.
  The bridge runs it over the run's still-open Mac pipe, then calls `synced`.
- Release never forwards: it needs no node invoke (DynamoDB + S3 only).

**`synced(auth, device, sync_id, ok)`** (`POST mac/synced {runId, agentId, family, deviceId, syncId, ok}`):

1. Read the row; `loading_owner != owner` or `load_id != sync_id` or the claim expired →
   `{"ok": False, "superseded": True}` and nothing is written (meter `result=superseded`).
2. `ok is False`: a load/base claim → `base_key=None`, `deferred_until=now+DEFER_SECONDS`; a save
   claim → nothing (the Mac keeps its changes). Clear the claim. Delete every `pending` key.
3. `ok is True`, by the claimed steps:
   - a `save` step → `_publish_save(owner, expected=pending.expected, new_key=pending.saved_out)`:
     CAS through `browser_control._publish`, refuse `mode == "transition"` (retry up to 3, then give
     up, meter `busy`), copy `browser_control._ext_key` of the previous save to the new key (only
     `NoSuchKey`/`404` may be skipped), delete the previous save object. A lost CAS (the cloud saved
     meanwhile) deletes the new key; the row keeps its old `synced_key`, so the next acquire is
     Stale again and self-heals.
   - a `load`/`base` step → row `base_key=pending.base_out`, `synced_key=<the key it loaded or None>`.
   - Clear the claim and `pending`, delete the replaced base; all conditional on `load_id`.
4. `synced` never forwards (DynamoDB + S3 only).

**`_spawn`.** A module-level task set so the tasks survive the request, with a done-callback that
logs the exception class. Serialize per device with an in-process `asyncio.Lock` on the holder.
The DynamoDB claim is the cross-task fence.

**`clear_device` (spec §6.3).**

1. Resolve the pair's native device through `paired_physical_mac`.
2. If the row holder is another owner → return True, untouched.
3. Fence: owner None, new `load_id`, claim and `pending` cleared, conditional on the one read.
4. Delete `base_key` and any `pending` keys.
5. Resolve with `allow_revoking=True, allow_ungranted=True`, and dispatch `browser.wipe.v1`,
   forwarding to the holder with `_forward_mac` when needed. Best effort.
6. Meter `mac_browser.clear` with `result` ∈ {ok, skipped, error}.

**Metrics.** `mac_browser.sync` with dims `kind` ∈ {load, base, save} and `result` ∈
{claimed, ok, error, superseded, busy}. No values.

**Tests to add.**

- `test_mac_browser.py`:
  - `test_zero_arg_picks_the_requesters_ready_mac`
  - `test_zero_arg_without_a_ready_mac_is_no_mac`
  - `test_zero_arg_with_two_ready_macs_is_choose_mac`
  - `test_device_id_of_either_pair_member_maps_to_native`
  - `test_generation_comes_from_ready_row_after_reconnect`
  - `test_paperclip_acquire_runs_consent_once`
  - `test_paperclip_acquire_refused_consent_is_desktop_consent_denied`
  - `test_acquire_update_required_when_ready_row_lacks_wipe_marker`
  - `test_acquire_browser_off_when_new_app_lacks_cdp`
  - `test_acquire_maps_not_ready_and_stale_codes_to_generation_lost`
  - `test_acquire_maps_mac_browser_off`
  - `test_acquire_maps_chrome_missing`
  - `test_acquire_disabled_owner`
  - `test_acquire_held_by_other_workspace`
  - `test_acquire_own_claim_answers_starting`
  - `test_stale_holder_same_profile_fences_and_wipes_in_background`
  - `test_fresh_claim_relabels_row_and_returns_load_step`
  - `test_fresh_without_workspace_save_returns_base_step_into_state_url`
  - `test_stale_returns_save_then_load_steps`
  - `test_stale_skipped_while_other_run_leases_device`
  - `test_deferred_is_checked_before_fresh`
  - `test_acquire_returns_pipe_lease_and_frames_stamp`
  - `test_acquire_forwards_to_holder_task`
  - `test_forward_timeout_exceeds_ensure` (`FORWARD_TIMEOUT_S > ENSURE_TIMEOUT_S`)
  - `test_forward_keeps_error_message`
  - `test_release_returns_save_step_for_last_lease`
  - `test_release_waits_for_last_lease`
  - `test_release_skips_save_without_base`
  - `test_synced_load_writes_base_conditionally_on_load_id`
  - `test_synced_superseded_writes_nothing`
  - `test_synced_failed_load_defers`
  - `test_synced_save_publishes_cas_and_carries_extension_storage`
  - `test_synced_save_lost_cas_deletes_new_key`
  - `test_synced_refuses_cloud_transition`
  - `test_synced_ignores_bridge_named_keys` (only `pending` keys are ever used)
  - `test_clear_device_fences_and_wipes`
  - `test_clear_device_skips_other_workspace`
  - `test_clear_device_wipes_when_browser_switched_off`
  - `test_clear_member_clears_each_paired_mac`
  - `test_check_holder_refuses_profile_change`
  - `test_no_values_in_logs`
- `test_mac_browser_routes.py`:
  - `test_mac_acquire_requires_hook_token`
  - `test_mac_acquire_authorizes_run_active` (a valid hook token with an ended run → 403; the route
    calls `authorize_run(..., active=True)`)
  - `test_mac_release_and_synced_require_active_run` (ended run → `{"sync": null}` / `{"ok": false}`)
  - `test_internal_mac_acquire_reauthorizes_active`
  - `test_mac_acquire_starting_is_202`
  - `test_mac_acquire_error_detail_has_code_and_message`
  - `test_internal_mac_actions_require_internal_secret`
- `test_browser_control.py`: `test_watch_asks_frames_while_a_mac_run_browses`
- `test_desktop_pair_router.py`: `test_delete_device_also_clears_mac_chrome`

**Run.** `cd apps/backend && uv run pytest tests/unit/services/test_node_broker.py tests/unit/routers/test_gateway_relay_agentcore.py tests/unit/services/test_mac_browser.py tests/unit/routers/test_mac_browser_routes.py tests/unit/services/test_browser_control.py tests/unit/routers/test_desktop_pair_router.py tests/unit/services/test_mac_cdp.py tests/unit/services/test_desktop_paperclip.py tests/unit/routers/test_desktop_execution.py -q`

**Done when.** The tests pass; every row write in the new module is conditional; `mac_browser.py`
never calls `container_rpc` (the bridge runs the sync); and grep shows no cookie or URL value in any
`logger.` call in `mac_browser.py`.

---

### Task 4: Bridge — `cdp_mux` with more than one upstream

**Goal.** One loopback server that serves the cloud upstream exactly as today, plus any number of
named upstreams (`mac:<device>:<runId>`). No behavior changes for the cloud.

**Files.** `apps/infra/hermes/bridge/cdp_mux.py`, `apps/infra/hermes/tests/test_cdp_mux.py`

**Interfaces.**

```python
CLOUD = "cloud"
def url_for(run_id: str, upstream: str = CLOUD) -> str
def set_upstream(url: str, generation, upstream: str = CLOUD) -> None
def close_run(run_id: str, upstream: str | None = None) -> None   # None: every upstream the run is on
def cast(run_id: str, sink, ttl: float, resend: bool = False, upstream: str = CLOUD) -> None
def dispatch(run_id: str | None, event: dict) -> None             # CLOUD casts only; else LookupError
def disconnect(upstream: str = CLOUD) -> None
def drop(upstream: str) -> None                                   # close + forget a non-cloud upstream
```

**Refactor.**

- `_State` becomes one instance per upstream: `_UPS: dict[str, _State]`, with `CLOUD` created at
  import. **Keep `S` as a module-level alias of `_UPS[CLOUD]`** — the existing tests read
  `cdp_mux.S.runs/want/generation/casts` (test_cdp_mux.py:86-88,229,242) and must pass unmodified.
- Each loop-side function takes the `_State`.
- The run key for `CLOUD` stays `key_for(run_id)`; any other upstream uses
  `key_for(upstream + "\x00" + run_id)`.
- `_KEYS: dict[key, upstream]` routes `_serve_client`.
- `MAX_CASTS` applies per upstream.
- `drop` refuses `CLOUD` (`ValueError`).

**Tests.**

- Every existing test passes **unmodified**. New tests reset named upstreams themselves
  (`_UPS` minus `CLOUD`, and `_KEYS`) in a fixture, since the existing fixture only resets `S`.
- Add:
  - `test_two_upstreams_route_independently`
  - `test_named_upstream_client_gets_its_own_tab`
  - `test_close_run_on_one_upstream_leaves_the_other`
  - `test_dispatch_never_reaches_a_named_upstream_cast`
  - `test_drop_closes_named_clients_and_forgets_upstream`
  - `test_named_upstream_cast_publishes_frames`
  - `test_drop_refuses_cloud`

**Run.** `cd apps/infra/hermes && uv run --no-project --with pytest --with botocore --with pyyaml --with websockets python -m pytest tests/test_cdp_mux.py tests/test_browser_control.py -q`

**Done when.** The tests pass, and the cloud call sites in `browser_control.py` / `server.py` are
unchanged.

---

### Task 5: Bridge + plugin — zero-arg `use_browser`, the Mac target, one sync engine

**Goal.** `lightbulb_desktop use_browser` (no arguments needed) points the run's `browser_*` tools
at the requester's Mac. The bridge runs the Mac's sign-in load and save with the **same**
`browser_state.checkpoint` the cloud uses (ruling: ONE sync engine), through the run's own Mac pipe
on `cdp_mux`, and checkpoints **before** the run reports `run.ended`. The skill text tells agents
how.

**Files.**

- `apps/infra/hermes/bridge/browser_control.py`
- `apps/infra/hermes/bridge/browser_state.py`
- `apps/infra/hermes/bridge/turn.py`: the two `begin_turn` call sites (turn.py:2263 room,
  turn.py:2358 Paperclip) pass `family="room"` / `family="paperclip"`. Confirm that
  `browser_control.release(browser_lease)` (turn.py:2311, 2377) still runs **before** the
  `run.ended` POST (turn.py:2645) and while the run heartbeat is still beating; the Mac save below
  depends on it. A runless room turn gets a synthetic `browser:` runId, which `authorize_run`
  rejects — intended (no Mac browser without a run).
- `apps/infra/hermes/plugins/lightbulb/desktop.py`: new actions only; the `browser` action stays
  until task 8.
- `apps/infra/hermes/skills-src/lightbulb/references/surfaces.md`: the rows "Paired Mac tools",
  "Local browser setup", and the "Browser on a paired Mac" paragraph.
- `apps/infra/hermes/skills-src/lightbulb/references/platform-primitives.md`: the
  `lightbulb_desktop` paragraph.
- `apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md`: anchors.
- Tests: `tests/test_browser_control.py`, `tests/test_browser_state.py`,
  `tests/test_plugin_desktop.py`.

**Interfaces (`browser_control.py`).**

```python
MAC_SYNC_BUDGET_S = 120        # one sync (all steps) incl. the synced report
MAC_SYNCING = ("The Mac's browser is loading the workspace's sign-ins; retry this browser "
               "action in a few seconds.")
def begin_turn(run_id: str, agent_id: str, frame_sink=None, family: str = "room") -> dict
def select_mac(device_id: str | None = None) -> str | None   # None = ready; else the message to show
def select_cloud() -> None
def _mac_upstream(state: dict) -> str                       # f"mac:{deviceId}:{runId}"
def _post_mac(path: str, body: dict, timeout: float = 20.0) -> tuple[int, dict]
def _run_mac_sync(state: dict, sync: dict) -> bool          # every step, then POST mac/synced
```

**Behavior.**

- **Turn state.** `state` gains `"family"`, `"mac": None | {"deviceId": str | None, "generation": str | None}`,
  `"error"` and `"macSync"` (a running sync thread, or None).
- **`_post` keeps its shape** (`dict | None`; ~10 existing tests monkeypatch it, and `renew` /
  `_release_if_idle` call it). `_post_mac` is separate: it catches `urllib.error.HTTPError` (an
  `OSError`, raised on every 4xx) and returns `(exc.code, detail)`, and `(0, {})` on other
  `OSError`/`ValueError`.
- **`select_mac(device_id=None)`**
  - Validates a given id (`[A-Za-z0-9_-]{1,128}`); **no generation argument** — the backend picks
    the ready generation.
  - Ends the run's cloud clients: `cdp_mux.close_run(run, CLOUD)` and `_disconnect(task)` for its
    Hermes tasks.
  - Sets `state["mac"] = {"deviceId": device_id, "generation": None}`, clears the snapshot
    generation, then `_refresh(state)`.
  - On failure it resets `state["mac"] = None` and returns the backend message (or
    "The Mac's browser is unavailable.").
- **`select_cloud`.** Runs the Mac release (below, with its save), then `state["mac"] = None`.
- **`cdp_url()`.** Returns `cdp_mux.url_for(run, _mac_upstream(state))` while `state["mac"]`.
- **`_refresh`, Mac branch**
  - POSTs `mac/acquire` with `{runId, agentId, family, deviceId?}` (same 202 polling as the cloud).
  - **Adopts** the answer's `deviceId` and `generation` into `state["mac"]` every time. A changed
    generation (the Mac slept and reconnected) is handled like the cloud's: `cdp_mux.set_upstream`
    drops the old pipe, the run's Hermes tasks are disconnected, and a fresh snapshot is required.
  - `cdp_mux.set_upstream(cdpUrl, generation, upstream=_mac_upstream(state))`.
  - If the answer carries `sync`, start `_run_mac_sync` in a thread (`state["macSync"]`) and return
    "syncing": `pre_tool_call` blocks with `MAC_SYNCING` until that thread finishes (it waits for it
    within its `HOOK_BUDGET_S` first, so a quick cookies-only load is invisible).
  - Never touches `_LEASE` / `RELEASE_GRACE_S`, which are the cloud's. `humanInputAt` is always 0.
- **`_run_mac_sync(state, sync)`** — the one sync engine:
  - `url = cdp_mux.url_for(f"sync:{runId}", upstream=_mac_upstream(state))` — a loopback client on
    the run's own Mac pipe (no state ticket, no second door connection).
  - For each step: `browser_state.checkpoint({**step, "source": "mac", "cdpUrl": url})`, in order,
    within `MAC_SYNC_BUDGET_S`; stop at the first failure.
  - Then `cdp_mux.close_run(f"sync:{runId}", upstream)` and
    `_post_mac("mac/synced", {runId, agentId, family, deviceId, syncId, ok})`.
  - Never logs a URL, a step body or an exception message (class names only).
- **Mac release — before `run.ended`.** `release(state)` / `_end_run` for a Mac run, **synchronously**
  (not a daemon thread) because `turn.py` calls it before posting `run.ended` and the door's 30 s
  re-check and every route need the run active:
  1. wait for a running `macSync` (bounded by `MAC_SYNC_BUDGET_S`);
  2. `_post_mac("mac/release", {runId, agentId, family, deviceId, generation})`;
  3. if the answer carries `sync`, `_run_mac_sync` it over the still-open pipe;
  4. then `cdp_mux.close_run(run, name)` and `cdp_mux.drop(name)`.
  `abort(run_id)` (Stop) runs steps 2-3 with a 10 s budget (UX #13 below), then 4.
- **Frames heartbeat covers Mac upstreams.** `_SINKS` becomes `run id -> (sink, upstream)`;
  `pre_tool_call` records the run's current upstream (`CLOUD` or `_mac_upstream(state)`), and
  `_cast` / `frames_wanted` pass it to `cdp_mux.cast`, so the watcher heartbeat and its resend
  reach a Mac cast. `select_mac` / `select_cloud` update the entry.
- **`pre_tool_call`.**
  - `computer_use` while `state["mac"]` → block: "computer_use works on the cloud browser's screen
    only; on a Mac use the browser_* tools."
  - A running `macSync` → block `MAC_SYNCING` (after waiting within the hook budget).
  - Any other failure blocks with `state["error"] or` the existing message.
- **`dispatch_input`.** Unchanged, which makes it cloud-only by task 4.

**Interfaces (`browser_state.py`).**

```python
_MUX_URL = re.compile(r"^ws://127\.0\.0\.1:9222/devtools/browser/[0-9a-f]{24}$")  # cdp_mux PORT/PREFIX/key_for
def _valid_cdp_url(value: object) -> str
    # New helper; today's check is _valid_url(value, "wss") (AgentCore only). Accepts that, OR a
    # cdp_mux loopback URL (_MUX_URL). checkpoint() calls it instead of _valid_url(..., "wss").
    # The door URL itself never reaches browser_state.
def _vet(state: dict) -> tuple[dict, set[str]]
    # Mac export -> (vetted state, domains to keep from the workspace). Rules = spec §3, ported from
    # desktop_browser_state._vetted/_good_cookie/_good_storage.
def _merge_three(base, current, previous, open_origins=frozenset(), *, keep=frozenset(), hold_back=False) -> dict
    # keep: domains taken from `previous`; hold_back: if more than max(5, len(base domains) // 2)
    # base domains vanish from `current`, those domains are taken from `previous`.
```

`checkpoint(params)` — the shared path, cloud and Mac:

- accepts an optional `"source" in {"cloud", "mac"}` (default `"cloud"`);
- `"mac"` **load is cookies only**: clear cookies, load the saved cookies, then the post-load export
  as the next base — **skip the per-origin localStorage loop** (no navigation flashes on the
  person's screen). The base still records the saved origins as carried-over entries so a later
  save never mistakes them for cleared;
- `"mac"` applies `_vet` to every Mac-side export (the save's `current` and the load's post-load
  export) and passes `keep` / `hold_back=True` to the save merge — vetting lives here, in the
  shared checkpoint path;
- `base` keeps writing its export to `stateUrl`;
- leaves cloud behavior byte-identical.

**Interfaces (plugin `desktop.py`).**

- `ACTION_FIELDS` gains `"use_browser": ({"deviceId": ID, "show": BOOL}, [])` — **zero arguments by default**,
  `deviceId` optional, **no `generation`** — and `"use_cloud_browser": ({}, [])`.
- `handle`, after the existing ctx checks:
  - `use_browser` → `from bridge import browser_control`,
    `msg = browser_control.select_mac(args.get("deviceId"))`, then return
    `{"browser": "mac", "deviceId": <adopted id>}` or `{"error": msg}`;
  - `use_cloud_browser` → `select_cloud()`, then `{"browser": "cloud"}`.
- `SCHEMA["description"]` adds: `use_browser` (no arguments) switches this run's `browser_*` tools
  to the requester's Mac — the agent's own "Lightbulb agent" Chrome window there; pass `deviceId`
  only when told to choose; `use_cloud_browser` switches back; `computer_use` stays cloud-only.

**Skill text** (`references/`, never `SKILL.md`):

- After `use_browser` the same `browser_*` tools drive the requester's Mac, in a separate
  "Lightbulb agent" Chrome window — never the person's everyday Chrome.
- It has the workspace sign-ins, which sync automatically.
- It can reach localhost and the person's local network (their dev servers, routers, NAS).
- **A login, 2FA or CAPTCHA:** ask the requester to finish it themselves in the "Lightbulb agent"
  window on their Mac, then continue; never ask for the code or password in chat.
- Every page works, `file://` and `chrome://` included, and uploads (owner ruling 2026-09-29); downloads land in the Mac's
  `~/Downloads/Lightbulb agent`.
- Watchers see it view-only.
- Relay each error message verbatim, especially "Update the Lightbulb desktop app …".

**Tests to add.**

- `test_browser_control.py`:
  - `test_post_mac_returns_status_and_detail_on_http_error`
  - `test_select_mac_without_device_lets_backend_pick`
  - `test_refresh_adopts_device_and_generation_from_acquire`
  - `test_new_generation_after_wake_drops_pipe_and_requires_snapshot`
  - `test_select_mac_surfaces_backend_message`
  - `test_select_mac_disconnects_cloud_clients`
  - `test_acquire_sync_runs_checkpoint_over_the_mux_then_reports_synced`
  - `test_sync_failure_reports_ok_false`
  - `test_tool_call_blocked_while_mac_sync_runs`
  - `test_release_saves_synchronously_before_returning`
  - `test_select_cloud_releases_mac`
  - `test_frames_wanted_resends_mac_casts_on_the_mac_upstream`
  - `test_computer_use_blocked_on_mac`
  - `test_mac_never_yields_to_cloud_human_input`
- `test_browser_state.py`:
  - `test_mux_url_accepted`
  - `test_private_or_public_ws_url_refused` (door URLs and other hosts never pass)
  - `test_mac_load_is_cookies_only_no_origin_navigation`
  - `test_mac_load_base_keeps_saved_origins`
  - `test_mac_source_bad_cookie_keeps_workspace_domain`
  - `test_mac_source_drops_bad_origin`
  - `test_mac_source_holds_back_mass_signout`
  - `test_cloud_source_unchanged`
- `test_plugin_desktop.py`:
  - `test_use_browser_with_no_arguments`
  - `test_use_browser_with_device_id`
  - `test_use_browser_rejects_generation_arg`
  - `test_use_browser_returns_backend_error_verbatim`
  - `test_use_cloud_browser`
  - `test_use_browser_in_a_paperclip_turn_passes_family`


**UX-flow rulings (ledger "UX-flow gap rulings", FIX NOW) — Task 5 part.** Each is small; each has
its test.

- **#2 skill routing.** Skill text: when the person mentions localhost, their local network (a
  192.168.x / `.local` address, their router, NAS, dev server) or "my Mac", call `use_browser`
  first. Test: none (skill text); `build-skills.sh` must still build.
- **#3 tabs close at run end.** The Mac release (below) calls `cdp_mux.close_run(run, name)`
  **before** `drop(name)`, so the run's tabs are closed on the Mac (`_close_run` sends
  `Target.closeTarget`). Test: `test_mac_run_end_closes_its_tabs_before_dropping_the_pipe`.
- **#6 display name.** Acquire answers `deviceName` (Task 3). The bridge keeps it in
  `state["mac"]` and `use_browser` returns `{"browser": "mac", "deviceId", "deviceName"}`.
  Test: `test_use_browser_result_carries_device_name`.
- **#9 the person's own input on the agent window makes the agent yield.** The Mac sends a
  synthetic CDP event on every open channel, `{"method": "Lightbulb.humanInput", "params": {"at": <epoch s>}}`
  (Task 7). `cdp_mux` swallows that method on a named upstream (never forwarded to clients) and
  records it: `cdp_mux.human_input_at(upstream) -> float`. The Mac branch of `_refresh` sets
  `state["humanInputAt"] = cdp_mux.human_input_at(name)` instead of 0, so the existing
  `YIELD_QUIET_S` yield and the fresh-snapshot fence apply unchanged. Tests:
  `test_mux_swallows_and_records_lightbulb_human_input` (test_cdp_mux.py),
  `test_mac_agent_yields_after_local_human_input`.
- **#10/#11 takeover.** `use_browser` accepts `show: bool` (default false). With `show: true` and
  the Mac already selected, the bridge sends `{"method": "Lightbulb.show", "params": {"targetId": <the run's first tab>}}`
  on the run's pipe (the Mac raises the window, Task 7) and returns
  `{"browser": "mac", "shown": true}`. Skill text: when a login, 2FA or CAPTCHA needs the person,
  post "Finish signing in in the 'Lightbulb agent' window on your Mac, then reply done", call
  `use_browser {"show": true}` **only then**, and wait for their reply. Tests:
  `test_use_browser_show_sends_lightbulb_show_once`, `test_use_browser_default_never_shows`.
- **#13 Stop still saves.** `abort(run_id)` on a Mac run runs the release save with a **10 s**
  budget (`MAC_STOP_SAVE_S = 10`, inside the backend's 20 s abort grace) instead of skipping it;
  past the budget it gives up and the Mac keeps its changes. Test:
  `test_abort_saves_within_ten_seconds_then_gives_up`.
- **#14 failed save.** A failed save step reports `mac/synced {"ok": false, "kind": "save", "roomId": <the room, room turns>}`.
  The muted room note itself is posted by the backend's `synced` handler (Task 3); if Task 3
  shipped without it, that is a Task 3 follow-up, not bridge code. Test:
  `test_failed_save_reports_kind_and_room`.
- **#17 access ended.** `cdp_mux.last_close(upstream) -> tuple[int, str] | None` records the door's
  close code/reason. When the Mac pipe closed with 4403, or 4502 with reason `BROWSER_OFF` or
  `PROFILE_CHANGED`, the next browser tool call blocks with **"Browser access on that Mac was
  turned off."** and clears `state["mac"]` (the agent must `use_browser` again). Test:
  `test_access_ended_close_gives_specific_message_and_deselects_mac`.
- **#20 re-attach after reconnect.** For a named (Mac) upstream, `cdp_mux._reset` keeps the run's
  tab ids; the next client for that run checks them with `Target.getTargets` and reuses a tab that
  still exists (Chrome survives a Mac sleep), creating a new one only when it is gone. The cloud
  upstream is unchanged. Test (test_cdp_mux.py):
  `test_named_upstream_reconnect_reuses_the_runs_existing_tab`,
  `test_named_upstream_reconnect_creates_tab_when_gone`.
- **#21 busy.** A Mac pipe closed with 4503 (or 4502 `CAPACITY`) blocks the next call with
  **"This Mac's browser is busy; try again shortly."** Test: `test_capacity_close_gives_busy_message`.
- **#24 Paperclip 2FA.** Skill text: in a Paperclip task, ask for the login/2FA in the task's issue
  thread, not a room.

Files this adds to Task 5: `apps/infra/hermes/bridge/cdp_mux.py` (`human_input_at`, `last_close`,
the `Lightbulb.humanInput` swallow, Mac-upstream tab re-attach) and `tests/test_cdp_mux.py`.

**Run.** `cd apps/infra/hermes && uv run --no-project --with pytest --with botocore --with pyyaml --with websockets python -m pytest tests/test_browser_control.py tests/test_browser_state.py tests/test_plugin_desktop.py tests/test_cdp_mux.py -q`,
then `apps/infra/hermes/build-skills.sh` into a temp dir to confirm the skills tree builds.

**Deploy.**

1. The image builds on merge.
2. Flip `dev.tag` in `hermes-version.json` and roll dev (no Mac check on dev).
3. Flip `prod.tag` and run `roll_hermes_runtimes.py`.
4. Verify `containerUri` with `aws bedrock-agentcore-control get-agent-runtime`.

**Done when.** The tests pass. With the current desktop app, `use_browser` answers the
update-required message; that is the prod smoke once rolled.

---

### Task 6: Frontend — a run is a Mac run after `use_browser`

**Files.**

- `apps/frontend/src/components/messaging/BrowserRunPreview.tsx`
- `apps/frontend/src/components/messaging/__tests__/BrowserRunPreview.test.tsx`

**Interfaces.**

```ts
export function usesMacBrowser(run): boolean
// Look at the run's last lightbulb_desktop call whose action is one of
// "use_browser" | "use_cloud_browser" | "browser". It is a Mac run if that action is
// "use_browser" **and the call succeeded** (UX #7): status "done" and its result (the tool's
// JSON text in `RunToolCall.result`, packages/shared/src/messaging/timeline.ts:126) parses to
// {"browser": "mac"}. A failed or still-running use_browser is not a Mac run. The legacy
// "browser" action counts as before until task 8.
export function macBrowser(run): { deviceId?: string; deviceName?: string } | undefined
// From that same successful result (UX #6): the Mac's display name comes from the acquire, so
// the preview reads "<agent> is browsing on <deviceName>" and the Browser panel's Mac tab shows
// deviceName, falling back to the devices lookup and then "a Mac". Replaces macBrowserDevice;
// update its two callers (BrowserDockPanel.tsx, RoomPanel.tsx) in this task.
```

Files this adds: `apps/frontend/src/components/workspace/panels/BrowserDockPanel.tsx`,
`apps/frontend/src/components/messaging/RoomPanel.tsx` (callers only), and
`__tests__/BrowserRunPreview.frames.test.tsx`.

**Tests to add.**

- `it("treats a run whose last selection is a successful use_browser as a Mac run")`
- `it("a failed or running use_browser is not a Mac run")` (UX #7)
- `it("a run that switched back with use_cloud_browser is not a Mac run")`
- `it("reads deviceName from the use_browser result")` (UX #6)
- `it("preview label names the Mac")`, `it("panel tab names the Mac, else falls back to a Mac")`
- Keep the existing `browser`-action cases.

**Run.** `cd apps/frontend && npx vitest run src/components/messaging/__tests__/BrowserRunPreview.test.tsx src/components/messaging/__tests__/BrowserRunPreview.frames.test.tsx`

**Done when.** The tests pass, and `RoomPanel` / `BrowserDockPanel` read `macBrowser(run)`. The Mac view stays view-only.

---

### Task 7: Desktop — the agent Chrome, the CDP channel, the filter, the wipe

**Goal.** A new signed desktop build serves `browser.cdp.v1` and `browser.wipe.v1` on the native
socket. It launches and pipes its own Chrome, filters methods, and wipes on unpair and Browser-off.
It leaves the OpenClaw browser path untouched (task 10 removes it).

**Files (all under `apps/desktop/src-tauri/src/`).**

- New `agent_chrome.rs`.
- `node_client.rs`:
  - add `"browser.cdp.v1"` and `"browser.wipe.v1"` to the connect `commands` list;
  - **CDP lanes on the Mac side (ruling: CDP never queues behind invoke traffic):**
    - inbound: route `browser.cdp.open|frame|close` events to their **own** mpsc
      (`cdp_in`, capacity 1024) read by `Channels`, never through the 32-slot invoke channel;
    - outbound: a separate `cdp_out` mpsc (capacity 1024) next to `write_rx`; the connection loop's
      `tokio::select! { biased; ... }` drains `cdp_out` before `write_rx`, but after 64 CDP frames
      in a row lets one waiting `write_rx` frame through (no starvation). Reconnect drains both.
    - `NodeClient::event_sender() -> EventSender` wraps `cdp_out` with an async
      `send(event, payload)` (awaits = backpressure, never drops);
  - `write_tx` stays at 16 (invoke/control lane).
- `node_invoke.rs`:
  - `"browser.cdp.v1"`: if Browser is off → `BROWSER_OFF`; else `agent_chrome::ensure_running` →
    `{"profileId"}` or its failure code;
  - `"browser.wipe.v1"` → `agent_chrome::wipe` → `{"wiped"}`, no switch check;
  - `Channels` reads `cdp_in` in its own task; `on_event` **never awaits `ensure_running` or a
    connect inline** — `open` spawns its own task (see below), so a Chrome launch never stalls
    invokes, cancels or other channels' frames. `run()` calls `close_all()` on `Disconnected`.
- `lib.rs`: `mod agent_chrome;`. `AuthState` gains `agent_chrome: agent_chrome::Channels` (a
  cheap `Clone` handle around `Arc<Mutex<..>>`), created once and handed to `node_invoke::run`, so
  `forget_pairing` and `revoke_agent_browser` can `close_all()` and then call
  `agent_chrome::wipe(&app_data)`, logging failures.
- **No `pair_state` change** (ruling: no re-pair). The backend derives `browser.cdp.v1` and
  `browser.wipe.v1` for existing pairs (task 1).
- `agent_browser.rs`: make `stop_chrome_for` and `singleton_pid` `pub(crate)` for reuse; no other
  change.

**Interfaces (`agent_chrome.rs`).**

```rust
pub const DIR: &str = "agent-chrome";
pub const PROFILE_ID_FILE: &str = "lightbulb-profile-id";
pub type Failure = (&'static str, String);          // BROWSER_OFF | CHROME_MISSING | CHROME_START_FAILED | PROFILE_CHANGED | CAPACITY | TOO_LARGE | CLOSED
pub struct Endpoint { pub port: u16, pub path: String, pub profile_id: String }
pub fn user_data_dir(app_data: &Path) -> PathBuf;                     // app_data/agent-chrome
pub fn downloads_dir() -> PathBuf;                                    // ~/Downloads/Lightbulb agent
pub fn profile_id(dir: &Path) -> std::io::Result<String>;            // created once (O_EXCL, 0600), 32 hex
pub fn seed_preferences(dir: &Path, downloads: &Path) -> std::io::Result<()>; // only when Default/Preferences is absent:
    // download.default_directory/prompt_for_download=false/directory_upgrade, savefile.default_directory,
    // profile.name = PROFILE_NAME, browser.theme.user_color = THEME_COLOR (Chrome 120+ key; if the
    // founder's Chrome ignores it, also set autogenerated.theme.color), credentials_enable_service=false
pub fn parse_active_port(text: &str) -> Option<(u16, String)>;        // "port\n/devtools/browser/<id>\n"
pub async fn ensure_running(app_data: &Path) -> Result<Endpoint, Failure>;
pub fn wipe(app_data: &Path) -> Result<bool, String>;                 // symlink-safe like agent_browser::wipe_target
pub const PROFILE_NAME: &str = "Lightbulb agent";
pub const THEME_COLOR: i64 = /* ARGB of the brand bulb token, e.g. */ 0xFFF5B82E;
pub enum Filtered { Pass(String), Answer(String /* {"id",..,"result":{}} */), Refuse(String /* CDP error reply */) }
pub fn filter(raw: &str, downloads: &Path) -> Filtered;
#[derive(Clone)]
pub struct Channels { /* Arc<Mutex<channel id -> (mpsc tx to that channel's Chrome task, JoinHandle)>> */ }
impl Channels {
    pub fn new(app: tauri::AppHandle, out: EventSender) -> Self;
    pub fn on_event(&self, event: &str, payload: &Value);   // non-blocking: open spawns, frame try_sends, close aborts
    pub fn close_all(&self);
}
pub const MAX_ENVELOPE: usize = 24 * 1024 * 1024;   // encoded frame event, under the relay's 25 MiB
```

**`ensure_running`.**

1. Create the dir (0700) with `seed_preferences` if it is new, and read `profile_id`.
2. If `DevToolsActivePort` parses and `GET http://127.0.0.1:<port>/json/version` answers within
   1 s, return.
3. Otherwise delete a stale `DevToolsActivePort` and run
   `open -nga "Google Chrome" --args --user-data-dir=<dir> --remote-debugging-port=0 --no-first-run --no-default-browser-check --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling --disable-search-engine-choice-screen`
   (the three backgrounding flags keep an agent tab working while the window is hidden behind the
   person's own windows) with `tokio::process::Command` (awaited with a 10 s timeout, never `std::process` on the
   socket loop). A non-zero exit whose stderr names the app → `CHROME_MISSING`.
4. Poll `DevToolsActivePort` for up to 15 s, else `CHROME_START_FAILED`.
5. Never follow a symlinked dir.

**`Channels::on_event("browser.cdp.open")`.**

`open` registers the channel and `tokio::spawn`s steps 3-8; `on_event` returns at once.

1. Browser switch (`desktop_permissions::load(app).browser`) off → close `BROWSER_OFF`.
2. At 4 channels → close `CAPACITY`.
3. `ensure_running` (inside the spawned task); failure → close with its code.
4. `payload.profileId != endpoint.profile_id` → close `PROFILE_CHANGED`.
5. `connect_async("ws://127.0.0.1:<port><path>")`.
6. Spawn the pump:
   - Chrome text → `browser.cdp.frame`; `data` over 16 MiB **or** the serialized event over
     `MAX_ENVELOPE` → close `TOO_LARGE` (a JSON-escaped 16 MiB string can pass the relay's
     25 MiB cap and would drop the whole node socket with 1009);
   - backend `frame` → `filter` → `Pass` goes to Chrome, `Refuse` goes back as a `browser.cdp.frame`
     reply without touching Chrome.
7. Reply `browser.cdp.opened`.
8. Either side ending → `browser.cdp.close {code:"CLOSED"}` (once).

**`filter`, exactly the §3 list.**

- Refuse: `DOM.setFileInputFiles`, `Input.dispatchDragEvent`, `Page.handleFileChooser`,
  `Browser.grantPermissions`, `Browser.setPermission`, `Browser.executeBrowserCommand`,
  `Browser.close`, `Browser.crash`, `Browser.crashGpuProcess`, `Target.openDevTools`,
  `Target.exposeDevToolsProtocol`.
- Refuse whole domains: `Tethering.`, `Extensions.`, `PWA.`, `Cast.`, `DeviceAccess.`, `SystemInfo.`.
- Check the URL of `Page.navigate`, `Target.createTarget` and `Network.loadNetworkResource`: only
  `http:`, `https:` or exactly `about:blank` pass (parse with `url::Url`; an unparseable URL is
  refused).
- **Answer without executing** (`Filtered::Answer`, `{"id": <same>, ["sessionId",] "result": {}}`):
  `Page.bringToFront`, `Target.activateTarget`, `Browser.setWindowBounds` — the agent window never
  jumps in front of, or resizes over, the person's work.
- Rewrite `Browser.setDownloadBehavior` and `Page.setDownloadBehavior` so `downloadPath` is the
  pinned dir, and leave `behavior` as sent.
- **Private hosts are allowed** (owner ruling): `http://localhost:3000`, `http://127.0.0.1`,
  `http://192.168.1.1`, `http://nas.local` all pass — the check is on the scheme only.
- A refusal reply is `{"id": <same>, ["sessionId": <same>,] "error": {"code": -32000, "message": "Refused by Lightbulb on this Mac: <Method>"}}`.
- Unparseable JSON is refused with id 0.
- Everything else passes byte-identical.


**UX-flow rulings (ledger, FIX NOW) — Task 7 part.**

- **#1 status for Settings.** Tauri command `agent_chrome_status(app) -> {"state": "ready" | "stopped" | "chrome_missing" | "browser_off"}`
  (Browser switch off → `browser_off`; `open -Ra "Google Chrome"` fails → `chrome_missing`; a live
  `DevToolsActivePort` → `ready`; else `stopped`). Registered in `lib.rs`. No values beyond the
  state. Test: `status_maps_switch_chrome_and_port`.
- **#3 idle quit.** `IDLE_QUIT_S = 600`: when the last channel closes, `Channels` starts a timer;
  if no channel opened meanwhile, it stops the agent Chrome (`stop_chrome_for`, SIGTERM first).
  The run's tabs are closed by the bridge at run end (Task 5). Test:
  `idle_timer_quits_chrome_only_with_no_channel_for_ten_minutes` (tokio paused time).
- **#9 local human input.** One private "watch" CDP connection per running Chrome (never exposed to
  the backend): `Target.setDiscoverTargets`, attach to each page (flat), `Runtime.addBinding("__lbInput")`
  and `Page.addScriptToEvaluateOnNewDocument` + `Runtime.evaluate` of a capture-phase listener for
  `pointerdown`, `keydown` and `wheel` that calls `__lbInput` at most once a second. On
  `Runtime.bindingCalled`, if no `Input.*` command passed the filter in the last 1 s (agent
  input is trusted too, so this window tells them apart), send
  `{"method": "Lightbulb.humanInput", "params": {"at": <epoch s>}}` as a Chrome→backend
  `browser.cdp.frame` on every open channel. `ponytail:` a page can call the binding itself; the
  worst it does is make the agent wait ≤10 s. Tests: `human_input_forwarded_when_no_recent_agent_input`,
  `human_input_suppressed_right_after_agent_input`.
- **#11 show on ask.** The filter answers `{"method": "Lightbulb.show", "params": {"targetId"}}`
  itself: it sends the real `Target.activateTarget` to Chrome (bypassing the swallow, this one
  call only) and activates the Chrome app with `NSRunningApplication::runningApplicationWithProcessIdentifier(pid).activateWithOptions(...)`
  (objc2-app-kit; pid from `SingletonLock`, checked like `stop_chrome_for`; no Automation prompt).
  Every other `Page.bringToFront` / `Target.activateTarget` stays swallowed. Tests:
  `lightbulb_show_activates_target_and_app`, `bring_to_front_still_swallowed`.
- **#19 stay awake.** While at least one channel is open, hold a `caffeinate -i -w <app pid>` child
  (`tokio::process`), killed when the last channel closes (and on disconnect). Test:
  `caffeinate_held_exactly_while_channels_open`.
- **#23 Paperclip notice.** If a `browser.cdp.open` payload carries `"notice": "<text>"` (the
  backend sets it for Paperclip runs: "<agent> is using your Mac's browser for a team task"),
  show it once per channel with `notify.rs` (plain text, ≤ 200 chars, no link). Test:
  `open_with_notice_notifies_once`. The backend half is a Task 3 follow-up.

**Tests to add (Rust `#[cfg(test)]`).**

- `agent_chrome::tests` (plus the UX tests above):
  - `filter_refuses_file_input_and_drag`
  - `filter_refuses_non_web_navigation`: `file:`, `chrome:`, `chrome-extension:`, `devtools:`,
    `javascript:`, and unparseable
  - `filter_allows_http_https_and_about_blank`
  - `filter_allows_localhost_and_local_network`
  - `filter_answers_bring_to_front_activate_and_window_bounds_without_passing`
  - `filter_rewrites_download_path`
  - `filter_refuses_denied_domains_and_methods`
  - `filter_refusal_echoes_id_and_session`
  - `filter_passes_other_methods_byte_identical`
  - `parse_active_port_reads_port_and_path`
  - `profile_id_is_stable_until_wipe`
  - `wipe_refuses_symlinks_and_parent_components`
  - `seed_preferences_pins_downloads_only_on_a_fresh_profile`
  - `seed_preferences_names_the_profile_and_sets_a_theme_colour`
  - `launch_args_include_the_backgrounding_flags`
  - `oversized_envelope_closes_channel_too_large`
  - `on_event_open_returns_without_awaiting_launch`
- `node_client` tests:
  - `signed_connect_declares_cdp_and_wipe`
  - `handle_message_routes_cdp_events_to_their_own_lane`
  - `cdp_out_drains_before_invoke_results`
  - `invoke_result_not_starved_by_cdp_burst`

**Run.** CI (`desktop-build.yml`, `cargo test --lib`). Locally, only with more than 10 GB free:
`cd apps/desktop/src-tauri && TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --lib agent_chrome:: node_client::`

**Signing.**

- No new binary, no `tauri.conf.json` resource change, and `entitlements.plist` untouched. The app
  spawns `/usr/bin/open`, which needs no entitlement; the app is not sandboxed.
- Confirm in the PR that the `desktop-build.yml` signing assertions are unchanged.

**Done when.**

- CI is green.
- The PR description lists the prod checks this build unlocks (§ checklist).
- The founder installs the prod build and passes checklist items 1-3.

---

### Task 8: Delete the old path — Hermes plugin (after the checklist passes)

**Files.**

- `apps/infra/hermes/plugins/lightbulb/desktop.py`: remove `BROWSER`, the `browser` entry of
  `ACTION_FIELDS`, the `browser` branch in `handle`, and its description text.
- `apps/infra/hermes/plugins/lightbulb/desktop_browser.py`: remove `browser_action`, then
  `git mv` it to `desktop_transport.py`, since `_http`, `submit_request`, `receipt_status`,
  `materialize` and `upload_files` stay shared by files and screen. Update the imports in
  `desktop.py` and `desktop_resources.py`.
- `apps/infra/hermes/tests/test_plugin_desktop_browser.py`: delete the browser-action tests and
  move the transport tests to `tests/test_plugin_desktop_transport.py`.
- `apps/infra/hermes/tests/test_plugin_desktop.py`: drop the browser-action cases.
- Skill references: remove every mention of the typed Mac browser actions and the `lightbulb`
  profile name.
- `apps/frontend/src/components/messaging/BrowserRunPreview.tsx` (+ test): drop `"browser"` from
  the detector. This ships on the next frontend deploy and is harmless in either order.

**Tests to add.** `test_browser_action_is_gone`: `validate` rejects `{"action":"browser"}`.

**Run.** `cd apps/infra/hermes && uv run --no-project --with pytest --with botocore --with pyyaml --with websockets python -m pytest tests/test_plugin_desktop.py tests/test_plugin_desktop_transport.py -q`

**Done when.**

- `git grep -n "browser_action\|browserAction\|\"browser.proxy\"" apps/infra/hermes` is empty.
- The image is built, pinned and rolled in prod.

---

### Task 9: Delete the old path — backend (+ the desktop settings component)

**Files.**

- **Delete** `apps/backend/core/services/desktop_browser.py`,
  `apps/backend/core/services/desktop_frames.py` and
  `apps/backend/core/services/desktop_browser_state.py`, with their tests
  `tests/unit/services/test_desktop_browser.py`, `test_desktop_frames.py` and
  `test_desktop_browser_state.py`.
- `core/services/desktop_execution.py`:
  - move `MAX_UPLOAD_REQUEST` and `CHUNK_BYTES` in as module constants, and re-point
    `routers/desktop_execution.py:13` (`from core.services.desktop_browser import MAX_UPLOAD_REQUEST`)
    to `desktop_execution` (`service.MAX_UPLOAD_REQUEST`);
  - drop `"browser.proxy"` from `PUBLIC_COMMANDS`, the `browser.proxy` branches in
    `validate_request` / `_execute` / `_execute_before_deadline` / `_dispatch` (`browser_media`),
    and the `BrowserScopes` / `browser_state` constructor argument.
- `core/services/node_broker.py`: drop `browser_media` and `browser.proxy.upload.v1`.
- `core/services/desktop_pair.py`:
  - remove `browser.state.v1`, and `"browser"` from `cli_caps`;
  - `DESKTOP_CLI_BROWSER_COMMANDS` goes, **but the Browser grant still has to be stored on the
    CLI entry** for the derivation to work on new pairs: `cli_commands()` keeps emitting the literal
    `"browser.proxy"` as the pair's stored Browser marker (the sidecar no longer declares it, so it
    grants nothing at the relay). Name it `BROWSER_GRANT_MARKER = "browser.proxy"` with a comment.
  - delete `NODE_COMMANDS_DECLARED_UNCONDITIONALLY` (nothing reads it; `desktop_pair_live` does not
    exist) and its comment block;
  - **keep `granted_commands`' derivation** (ruling): it now derives only `browser.cdp.v1` (from
    `BROWSER_GRANT_MARKER` on the reciprocal peer) and `browser.wipe.v1` (every native entry); only
    the `browser.state.v1` line goes. Pairs made before task 1 keep working without a re-pair.
- `routers/desktop_execution.py`: delete `/internal/browser-clear` and `/internal/browser-state`,
  and the `browser.proxy` upload size carve-out in `_body`.
- `routers/desktop_pair.py`: drop the old `clear_device` call (keep `mac_browser.clear_device`).
- `routers/webhooks.py`: import `revoke_member_soon` from `core.services.mac_browser`. Move
  `revoke_member_soon` there verbatim, calling only `mac_browser.clear_member`, together with its
  test.
- `core/services/browser_control.py`: fix the comment naming `desktop_browser_state`.
- The `__macbrowser__#` rows stay in place. They are harmless reserved keys that every scan skips,
  and nothing reads them any more. Say so in the PR.
- Frontend (UX #1): **rewrite** `apps/frontend/src/components/desktop/LocalBrowserSetup.tsx` to a
  one-line agent-Chrome status under the Browser switch, from the Task 7 command
  `invoke("agent_chrome_status")`: "Agents use a separate 'Lightbulb agent' Chrome window —
  ready" / "— starts when an agent needs it" / "Install Google Chrome to let agents browse on this
  Mac" / nothing when Browser is off. It must no longer call `local_browser_status` /
  `local_browser_configure` (task 10 removes them); an invoke error (an older app) renders
  nothing. Rewrite its test: `it("shows ready")`, `it("asks to install Chrome")`,
  `it("renders nothing on an older app")`, `it("never calls local_browser_*")`. Keep its use in
  `LocalSessions.tsx`. The old component called desktop commands task 10 removes, so this
  rewrite must ship first; this backend dispatch carries the frontend.
- Docs:
  - `docs/runbooks/desktop-execution-staged-acceptance.md`: the Mac browser section now describes
    `use_browser`, the door, and wipe-on-revoke;
  - `docs/architecture/current-system.md` if it names the old modules;
  - `CLAUDE.md`, Desktop App section: replace the `browser.state.v1` / `__macbrowser__` / sidecar
    `agent-browser` paragraphs with 5-8 lines on this design (native-socket CDP channel,
    `__macchrome__#` row, door on :8000, profile id, wipe) and a pointer to `mac_browser.py`;
  - `AGENTS.md`, if it mirrors those lines.

**Tests to add.**

- `test_desktop_execution.py`: `test_browser_proxy_is_no_longer_a_command`
- `test_desktop_pair.py`: `test_browser_grant_is_cdp_only`,
  `test_pre_task1_pair_still_derives_cdp_and_wipe`
- `test_mac_browser.py`: `test_revoke_member_soon_marks_clears_and_unpairs` (moved)

**Run.** `cd apps/backend && uv run pytest tests/unit/services/test_desktop_execution.py tests/unit/services/test_desktop_pair.py tests/unit/services/test_mac_browser.py tests/unit/routers/test_desktop_execution.py tests/unit/routers/test_desktop_pair_router.py tests/unit/routers/test_webhooks_paperclip.py tests/unit/services/test_node_broker.py -q`
and `cd apps/frontend && npx vitest run src/components/desktop`

**Done when.** `git grep -n "desktop_browser_state\|desktop_frames\|browser\.proxy\|browser\.state\.v1\|BrowserScopes" apps/backend apps/frontend CLAUDE.md`
returns only intentional history mentions (none expected). Deployed with a prod dispatch.

---

### Task 10: Delete the old path — desktop

**Files.**

- **Delete** `apps/desktop/src-tauri/src/browser_state.rs` and its `mod`, and the
  `"browser.state.v1"` arms in `node_invoke.rs` and in `node_client.rs`'s commands list.
- `local_mcp.rs`:
  - delete `configure_browser`, `ensure_browser_for_consent`, `browser_setup_status`,
    `browser_pairing` and their tests;
  - `restrict_node_integrations` now always writes `nodeHost.browserProxy.enabled = false` and
    removes the `browser.profiles.lightbulb` / `chrome` entries it created.
  - Test: `node_scope_disables_browser_proxy_and_drops_agent_profiles`.
- `subprocess.rs`: drop the `ensure_browser_for_consent` call.
- `lib.rs`: delete the `local_browser_status`, `local_browser_configure` and
  `local_browser_pairing` commands and their registrations. `revoke_agent_browser` no longer
  restarts the sidecar for the browser; it closes channels and calls `agent_chrome::wipe`.
- `agent_browser.rs`: move `stop_chrome_for` and `singleton_pid` into `agent_chrome.rs`, and
  delete the module. **One-release cleanup:** at startup, `agent_chrome::remove_legacy_profile(state_dir)`
  deletes `<openclaw-state>/browser/lightbulb/user-data` with the same symlink-safe wipe, marked
  `// ponytail: remove in the next desktop release (legacy OpenClaw profile, spec D2)`.
  Test: `legacy_profile_removed_once_and_symlink_safe`.
- `apps/desktop/src-tauri/scripts/vendor-sidecars.sh`: remove `vendor_agent_browser`, its call and
  `AGENT_BROWSER_VERSION`.
- `apps/desktop/src-tauri/tauri.conf.json`: remove `bin/agent-browser-*`.
- `.github/workflows/desktop-build.yml`: remove **both** agent-browser blocks — the dev job's
  (find `-name 'agent-browser-*-apple-darwin'` ~line 166, `case` arm ~187, sealed-in-Resources +
  no-entitlements + hardened-runtime asserts ~228-236) **and** the prod job's identical block
  (~375, ~396, ~438-446). Leaving either fails the build on a binary that no longer exists. Leave
  every other binary's hardened-runtime and entitlement check intact.
- `CLAUDE.md`, Desktop App: drop `agent-browser` from the sidecar list.

**Run.** CI `cargo test --lib`. Locally only with more than 10 GB free:
`cargo test --lib local_mcp:: agent_chrome::`

**Done when.**

- CI is green.
- `git grep -n "agent-browser\|browser_state::\|browser\.state\.v1\|local_browser_" apps/desktop .github`
  is empty.
- `git grep -n "agent-browser" .github/workflows/desktop-build.yml` is empty (both jobs).
- The signed prod build is installed by the founder, and checklist items 1 and 5 pass again.

---

## Prod verification checklist (founder, prod; after tasks 1-7 are live; again after 10)

Record each result on the PR, with the Mac's `~/Library/Logs/…` desktop log lines and backend
CloudWatch `mac_cdp.door` / `mac_browser.sync{kind,result}` / `mac_browser.clear{result}` metrics.

1. **Founder Mac, cold start.**
   - Quit the agent Chrome, and delete `<app_data>/agent-chrome`.
   - In a room: "@CEO use my Mac's browser to open news.ycombinator.com and tell me the top story".
   - Expect: `use_browser` with **no arguments** → a "Lightbulb agent" Chrome window (its own name
     and theme colour) opens on the Mac without taking focus → a short "loading sign-ins" (cookies
     only, no tab flashing) → the answer.
   - Put the Mac to sleep for a minute, wake it, and ask again in the same room: it works with no
     device or generation named (the new generation is adopted).
   - Ask it to open `http://localhost:<a local dev server>`: it loads.
   - `mac_browser.sync{kind=load|base,result=ok}` is emitted once.
2. **Aden-style second member.**
   - Aden (a non-owner member with his own paired Mac) asks the same in the same workspace.
   - Expect: his Mac, not the founder's.
   - The founder asking the agent to use **Aden's** device → `desktop_forbidden` message.
   - With both runs live, each Mac shows only its own tabs.
3. **Frames.**
   - While run 1 browses, open the room on the web as a second viewer.
   - Expect: the "is browsing on a Mac" preview updates at ~5 fps. Clicking opens the large Mac
     view.
   - Clicks and keys on it do nothing. `browser/input` answers `NOT_CASTING`, and nothing reaches
     the Mac: check the desktop log shows no `Input.dispatch*` from a watcher.
4. **Sign-in sync.**
   - Sign in to a test site in the **cloud** Browser panel and Stop; the cloud saves.
   - An agent on the founder's Mac opens that site: expect signed in (stale → save-then-load).
   - Sign in to a second test site inside the Mac agent Chrome during a run, and end the run.
     Within ~30 s, start the cloud browser: expect signed in there too.
   - `mac_browser.sync{kind=save,result=ok}`, reported **before** the run's `run.ended`.
   - Ask an agent to sign in to a site with 2FA on the Mac: it asks you to finish the 2FA in the
     "Lightbulb agent" window, then continues.
5. **Browser-off wipe.**
   - Switch Browser off in the desktop app's Settings.
   - Expect: the agent Chrome quits, `<app_data>/agent-chrome` is gone, and an open run's next
     browser call answers "Browser is switched off for that Mac …".
   - Switch it back on and ask again: expect a fresh `load` (new profile id), with the workspace
     sign-ins present.
6. **Paperclip.** With the founder's Mac "Paperclip actions" switch on, a Paperclip issue asking
   the CEO to read a page on that Mac succeeds with no prompt; with it off, `use_browser` answers
   "That Mac doesn't allow Paperclip actions …".
7. **Old app message.** On a Mac still on the previous build (or before task 7 ships), `use_browser`
   answers exactly "Update the Lightbulb desktop app on that Mac to let agents use its browser."
8. **Unpair / member removal.** Unpair Aden's Mac from Settings → Devices. Expect
   `mac_browser.clear{result=ok}`, and Aden's `agent-chrome` dir is gone if his Mac was online.
9. **Filter.**
   - Owner ruling 2026-09-29: no restrictions. `file:///etc/hosts`, `chrome://settings`
     and a form upload all work.
   - A download lands in `~/Downloads/Lightbulb agent`.
