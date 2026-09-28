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
- Work on a branch off `origin/main` in its own worktree (never stack on another agent's branch).
  Do not push unless told to. Commit messages end with the session's attribution lines.
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
    def __init__(self, code: str): self.code = code   # Mac close code or "CLOSED"/"TOO_LARGE"/"TIMEOUT"

class CdpChannel:
    id: str                              # uuid4
    inbound: asyncio.Queue               # str frames from the Mac; None = closed
    opened: asyncio.Future               # resolves on browser.cdp.opened; CdpClosed on close
    close_code: str | None

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
  - Idempotent. Sends one `browser.cdp.close` event when the channel is still registered, removes
    it, and puts `None` on `inbound`.
- **`resolve(frame)`**
  - An `event` frame named `browser.cdp.opened` / `browser.cdp.frame` / `browser.cdp.close` is
    routed by `payload.channel` and **returns None** (no ack). An unknown channel is ignored.
  - `opened` resolves the future.
  - `frame` puts `data` on `inbound`. `data` over `MAX_CDP_FRAME` or not a `str` leads to
    close `TOO_LARGE`; a full `inbound` leads to close `CLOSED`.
  - `close` sets `close_code`, fails `opened` with `CdpClosed(code)`, puts `None`, and removes
    the channel (no close event is echoed back).
- **`fail_pending`**
  - Closes every channel locally: `None` on `inbound`, `opened` failed with `CdpClosed("CLOSED")`.
- **Invokes are unchanged.** `invoke()` still raises `NodeCapacity` when `self.send.full()`, now
  at 1024. Existing tests that fill the queue fill `SEND_QUEUE`.

**Interfaces (`desktop_pair.py`).**

- `DESKTOP_MAC_COMMANDS` gains `"browser.cdp.v1"` and `"browser.wipe.v1"`.
- `DESKTOP_MAC_BASE_COMMANDS` gains `"browser.wipe.v1"` (always granted).
- `DesktopPermissionGrant.mac_commands()` adds `"browser.cdp.v1"` when `self.browser`.
- Leave `granted_commands()`'s `browser.state.v1` derivation alone; task 9 deletes it. **Do not**
  add a derivation for `browser.cdp.v1`, because task 7's pair-state bump re-pairs every updated Mac.

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
  - `test_cdp_inbound_overflow_closes_channel`
  - `test_cdp_send_rejects_oversized_frame`
  - `test_cdp_events_are_not_acked`
  - `test_fail_pending_closes_cdp_channels`
  - `test_invokes_still_admitted_while_cdp_frames_queued`
- `test_desktop_pair.py`:
  - `test_browser_grant_adds_cdp_command`
  - `test_wipe_command_is_always_granted`
  - `test_mac_and_cli_command_sets_stay_disjoint`
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
STATE_MAX_SECONDS = 200
PATH = "/api/v1/internal/mac-cdp/"

class TicketError(Exception):
    def __init__(self, code: str): self.code = code   # "invalid" | "expired" | "replayed" | "wrong_task"

def mint(*, use: str, target: dict, profile_id: str, auth: dict | None = None) -> str
    # use in {"agent","state"}; target = a resolve_desktop_target() dict for the NATIVE device;
    # auth required iff use == "agent" (room auth: ownerId, memberId, runId, agentId, roomId).
def door_url(target: dict, ticket: str) -> str
    # "ws://" + urlsplit(target["taskAddress"]).netloc + PATH + ticket
def redeem(ticket: str) -> dict
    # decrypt (Fernet, key = sha256("mac-cdp-ticket-v1:" + WS_INTERNAL_SECRET)), exact claim set
    # per spec §5, purpose check, iat/exp (exp == iat + TICKET_SECONDS, 5 s skew), address ==
    # task_address.own_address(), nonce unused (in-process dict pruned after exp + 5). Raises TicketError.
async def check(claims: dict) -> dict
    # Re-resolve the native target (ConnectionService._resolve_desktop_target with "browser.cdp.v1"),
    # generation == claims["generation"], desktop_execution.submissions_enabled(owner); use == "agent":
    # desktop_execution._reauthorize(auth rebuilt from claims). Returns the target. Raises DesktopError.
    # Task 3 adds the holder/profile check here.
async def serve(websocket, ticket: str) -> None
```

**How `serve` behaves.**

1. `accept()`.
2. `redeem`. A `TicketError` closes 4401.
3. `check`. A generation or task mismatch closes 4409; any other `DesktopError` closes 4403.
4. Fetch `gateway_relay.node_sessions[(owner, device)]`. It must exist with a matching generation,
   else close 4409.
5. `channel = await session.cdp_open(claims["profileId"])`. `CdpClosed(code)` closes 4502 with
   `reason=code`.
6. Pump both ways until either side ends:
   - client text → `session.cdp_send`;
   - `channel.inbound` → client (`None` ends it with 1000 if `close_code == "CLOSED"`, else 4502
     with that code).
7. A recheck task runs `check(claims)` every `RECHECK_SECONDS`; a failure closes 4403.
8. The connection is capped at `AGENT_MAX_SECONDS` or `STATE_MAX_SECONDS`.
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
  - `test_agent_ticket_requires_auth`
  - `test_door_url_uses_task_host_and_port`
  - `test_check_refuses_disabled_owner`
  - `test_check_refuses_generation_change`
- `test_mac_cdp_door.py` uses Starlette `TestClient` websockets and a fake `NodeSession` registered
  in `gateway_relay.node_sessions`:
  - `test_door_pipes_frames_both_ways`
  - `test_door_closes_4401_on_bad_ticket`
  - `test_door_closes_4409_when_socket_generation_gone`
  - `test_door_closes_4502_with_mac_code`
  - `test_door_recheck_closes_4403_when_run_ends`, with `RECHECK_SECONDS` patched to 0.05
  - `test_state_ticket_connection_is_capped`
  - `test_client_close_closes_the_channel`
  - `test_ticket_never_logged`, via `caplog`

**Run.**

- `cd apps/backend && uv run pytest tests/unit/services/test_mac_cdp.py tests/unit/routers/test_mac_cdp_door.py -q`
- `cd apps/infra && npx vitest run test/service-stack.test.ts`, then the synth rule from Global
  Constraints.

**Done when.** The tests pass, `deploy.yml` drift is committed (or none), and nothing outside these
files changed.

---

### Task 3: Backend — Mac browser lease, sign-in sync, revocation, frames

**Goal.** The bridge can `acquire` and `release` a Mac's browser for a room run. The first owner
wins, sign-ins load and save through the bridge's `browser.state` RPC on a state ticket, and
unpair or member removal wipes the profile.

**Files.**

- New `apps/backend/core/services/mac_browser.py`.
- `apps/backend/core/services/mac_cdp.py`: `check()` also calls `mac_browser.check_holder(owner, device, claims["profileId"])`.
- `apps/backend/routers/container.py`: `browser_internal_router` gains `POST /{owner_id}/mac/acquire`
  and `POST /{owner_id}/mac/release`, both behind hook-token auth like `/acquire`, and
  `Cache-Control: no-store`.
- `apps/backend/routers/desktop_execution.py`: the `/internal` action set gains `mac_acquire` and
  `mac_release`, with body `{"action","auth","request":{"deviceId","generation"}}`.
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

**Interfaces (`mac_browser.py`).**

```python
ROW_PREFIX = "__macchrome__#"            # + native device id; reserved key, skipped by scans
LEASE_SECONDS = 300
CLAIM_SECONDS = 180
DEFER_SECONDS = 60
SYNC_RPC_TIMEOUT_S = 180
CLEAR_SECONDS = 15
STARTING = {"status": "starting"}
MESSAGES: dict[str, str]                  # every §7 code -> its exact message

async def acquire(auth: dict, device_id: str, generation: str, *, forwarded: bool = False) -> dict
async def release(auth: dict, device_id: str, generation: str, *, forwarded: bool = False) -> dict
async def check_holder(owner: str, device_id: str, profile_id: str) -> None   # DesktopError 403
async def clear_device(owner: str, device_id: str) -> bool                     # either device of the pair
async def clear_member(owner: str, member_id: str) -> None                     # never raises
```

**Row `__macchrome__#<native device>`** (containers table). Fields:

| Field | Meaning |
|---|---|
| `loaded_owner` | str or None |
| `profile_id` | the Mac's agent-chrome id at the last load |
| `load_id` | the fence: new hex on every claim or fence |
| `base_key` | the Mac's merge base, `browser-state/<owner>/macbase-<hex>.json` |
| `synced_key` | the workspace `saved_key` as of the Mac's last load or save |
| `loading_owner`, `loading_expires` | the sync claim |
| `leases` | map runId → epoch until |

Every write is conditional on the `load_id` it read, as in the old `_put_mac_row`. Port its
condition logic and its `LoadSuperseded` exception; do not import the old module.

**`acquire` flow (spec §6.1).**

1. `auth["family"] != "room"` → `DesktopError("unsupported_run_family", 403, MESSAGES[...])`.
2. Resolve the native target: `desktop_execution._resolve(auth, {"deviceId", "expectedConnectionGeneration": generation, "command": "browser.cdp.v1"})`.
   - Before mapping `desktop_forbidden`, read the ready row. If it lacks `browser.cdp.v1`, raise
     `desktop_update_required`. If the grant lacks it (the Mac declares it, but the pair did not
     grant it), raise `browser_off`.
   - `DesktopTargetNotReady`/`DesktopTargetStale` → `desktop_generation_lost`.
3. If `target["taskAddress"] != own_address()` → `desktop_execution._forward(..., {"action":"mac_acquire",...})`.
   A forwarded call that lands on a non-holder raises `desktop_forward_loop`.
4. `submissions_enabled(owner)` else `desktop_disabled`.
5. Dispatch `browser.cdp.v1 {"action":"ensure"}`: `desktop_execution._dispatch(target, {...}, uuid4)`,
   20 s. Map `BROWSER_OFF` → `browser_off`, `CHROME_MISSING` → `chrome_missing`, anything else →
   `chrome_start_failed`. Keep `profileId`.
6. Read the row. If the holder is another still-paired workspace (`_still_paired`, as in the old
   `_row_holder`), or an unexpired claim by another owner → `browser_profile_held_by_another_workspace`.
   An unexpired claim by this owner → `STARTING`.
7. **Fresh** (`loaded_owner != owner`, or `profile_id != profileId`, or no `base_key`): claim, then
   `_spawn(_fresh_sync)`, then `STARTING`.
   `_fresh_sync` does this:
   - if the row named another (stale) owner with the same `profile_id`, dispatch `browser.wipe.v1`
     and re-ensure for the new id;
   - `container_rpc.call(owner, "browser.state", {"action": "load" if workspace saved_key else "base", "source": "mac", "cdpUrl": door_url(state ticket), "stateUrl": ..., "baseOutUrl": new base key PUT}, timeout_s=SYNC_RPC_TIMEOUT_S)`;
   - write the row `loaded_owner`, `profile_id`, `base_key`, `synced_key` and clear the claim, all
     conditional on the claim's `load_id`.
   - On failure, write `loaded_owner=owner`, `base_key=None` and defer `DEFER_SECONDS`. The next
     acquire is ready without a sync, and nothing is saved back.
8. **Stale** (`synced_key != workspace saved_key`, and no other run's lease on this device is live):
   claim, `_spawn(_save_then_load)`, then `STARTING`.
9. **Ready**:
   - `leases[runId] = now + LEASE_SECONDS`, conditional on `load_id`;
   - `container_repo.touch_browser_input(owner, now, field="mac_browsing_at", create=True)`;
   - answer `{"generation": target["generation"], "cdpUrl": door_url(mint("agent", ...)), "humanInputAt": 0, "framesAt": await container_repo.get_browser_input_at(owner, "human_frames_at")}`.

**`release` flow (spec §6.2).**

- Drop `leases[runId]`. If another lease is live, return `{"ok": True}`.
- Otherwise `_spawn(_save)`:
  - bridge `save` with `source: "mac"`, `previousStateUrl` (workspace save), `baseStateUrl` (row
    `base_key`) and fresh `stateUrl` / `baseOutUrl`;
  - `_still_loaded(load_id)`;
  - `_publish_save(owner, expected=workspace saved_key, new_key)`: CAS through
    `browser_control._publish`, refuse `mode == "transition"` (retry up to 3, then give up and
    log), copy `browser_control._ext_key` of the previous save to the new key (only a
    `NoSuchKey`/`404` may be skipped), delete the previous save object;
  - row `base_key`/`synced_key`, conditional on `load_id`;
  - delete the replaced base.
- On the Mac side a failure only logs and meters; the Mac keeps its changes.

**`_spawn`.** A module-level task set, so the tasks survive the request, with a done-callback that
logs the exception class. Serialize per device with an in-process `asyncio.Lock` on the holder.
The DynamoDB claim is the cross-task fence.

**`clear_device` (spec §6.3).**

1. Resolve the pair's native device through `paired_physical_mac`.
2. If the row holder is another owner → return True, untouched.
3. Fence: owner None, new `load_id`, conditional on the one read.
4. Delete `base_key`.
5. Resolve with `allow_revoking=True, allow_ungranted=True`, and dispatch `browser.wipe.v1`,
   forwarding to the holder with `desktop_execution._forward` when needed. Best effort.
6. Meter `mac_browser.clear` with `result` ∈ {ok, skipped, error}.

**Metrics.** Meter `mac_browser.sync` with dims `kind` ∈ {load, base, save} and `result` ∈
{ok, error, superseded, busy}. No values.

**Tests to add.**

- `test_mac_browser.py`:
  - `test_acquire_refuses_paperclip_family`
  - `test_acquire_update_required_when_ready_row_lacks_cdp`
  - `test_acquire_browser_off_when_grant_lacks_cdp`
  - `test_acquire_maps_mac_browser_off`
  - `test_acquire_maps_chrome_missing`
  - `test_acquire_disabled_owner`
  - `test_acquire_held_by_other_workspace`
  - `test_acquire_own_claim_answers_starting`
  - `test_acquire_fresh_profile_claims_and_starts_load`
  - `test_acquire_profile_id_change_forces_fresh_load`
  - `test_fresh_sync_wipes_a_stale_owners_profile_first`
  - `test_fresh_sync_without_workspace_save_writes_base`
  - `test_fresh_sync_writes_row_conditionally_on_load_id`
  - `test_fresh_sync_superseded_writes_nothing`
  - `test_load_failure_leaves_no_base_and_defers`
  - `test_acquire_stale_sync_skipped_while_other_run_leases_device`
  - `test_acquire_ready_mints_agent_ticket_records_lease_and_frames_stamp`
  - `test_acquire_forwards_to_holder_task`
  - `test_release_saves_three_way_and_publishes_cas`
  - `test_release_waits_for_last_lease`
  - `test_release_save_dropped_when_load_id_changed`
  - `test_publish_save_refuses_cloud_transition`
  - `test_publish_save_carries_extension_storage`
  - `test_clear_device_fences_and_wipes`
  - `test_clear_device_skips_other_workspace`
  - `test_clear_device_wipes_when_browser_switched_off`
  - `test_clear_member_clears_each_paired_mac`
  - `test_check_holder_refuses_profile_change`
  - `test_no_values_in_logs`
- `test_mac_browser_routes.py`:
  - `test_mac_acquire_requires_hook_token`
  - `test_mac_acquire_starting_is_202`
  - `test_mac_acquire_error_detail_has_code_and_message`
  - `test_mac_release_ok`
  - `test_internal_mac_actions_require_internal_secret`
- `test_browser_control.py`: `test_watch_asks_frames_while_a_mac_run_browses`
- `test_desktop_pair_router.py`: `test_delete_device_also_clears_mac_chrome`

**Run.** `cd apps/backend && uv run pytest tests/unit/services/test_mac_browser.py tests/unit/routers/test_mac_browser_routes.py tests/unit/services/test_browser_control.py tests/unit/routers/test_desktop_pair_router.py tests/unit/services/test_mac_cdp.py -q`

**Done when.** The tests pass; every row write in the new module is conditional; and grep shows no
cookie or URL value in any `logger.` call in `mac_browser.py`.

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
  import.
- Each loop-side function takes the `_State`.
- The run key for `CLOUD` stays `key_for(run_id)`; any other upstream uses
  `key_for(upstream + "\x00" + run_id)`.
- `_KEYS: dict[key, upstream]` routes `_serve_client`.
- `MAX_CASTS` applies per upstream.
- `drop` refuses `CLOUD` (`ValueError`).

**Tests.**

- Every existing test passes **unmodified**.
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

### Task 5: Bridge + plugin — `use_browser`, the Mac target, sign-in sync over the door

**Goal.** `lightbulb_desktop use_browser` points the run's `browser_*` tools at the Mac.
`browser.state` checkpoints work against a door URL with Mac vetting. The skill text tells agents
how.

**Files.**

- `apps/infra/hermes/bridge/browser_control.py`
- `apps/infra/hermes/bridge/browser_state.py`
- `apps/infra/hermes/bridge/turn.py`: the two `begin_turn` call sites pass `family="room"` /
  `family="paperclip"`
- `apps/infra/hermes/plugins/lightbulb/desktop.py`: new actions only; the `browser` action stays
  until task 8
- `apps/infra/hermes/skills-src/lightbulb/references/surfaces.md`: the rows "Paired Mac tools",
  "Local browser setup", and the "Browser on a paired Mac" paragraph
- `apps/infra/hermes/skills-src/lightbulb/references/platform-primitives.md`: the
  `lightbulb_desktop` paragraph
- `apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md`: anchors
- Tests: `tests/test_browser_control.py`, `tests/test_browser_state.py`,
  `tests/test_plugin_desktop.py`

**Interfaces (`browser_control.py`).**

```python
def begin_turn(run_id: str, agent_id: str, frame_sink=None, family: str = "room") -> dict
def select_mac(device_id: str, generation: str) -> str | None   # None = ready; else the message to show
def select_cloud() -> None
def _mac_upstream(state: dict) -> str                           # f"mac:{deviceId}:{runId}"
```

**Behavior.**

- **Turn state.** `state` gains `"family"`, `"mac": None | {"deviceId", "generation"}` and
  `"error"`. `_post` returns `(status, payload)` so an error's `detail.message` reaches the agent.
- **`select_mac`**
  - Validates the ids (`[A-Za-z0-9_-]{1,128}`).
  - Ends the run's cloud clients: `cdp_mux.close_run(run, CLOUD)` and `_disconnect(task)` for its
    Hermes tasks.
  - Sets `state["mac"]`, clears the snapshot generation, then `_refresh(state)`.
  - On failure it resets `state["mac"] = None` and returns the backend message (or
    "The Mac's browser is unavailable.").
- **`select_cloud`.** Releases the Mac (below) and sets `state["mac"] = None`.
- **`cdp_url()`.** Returns `cdp_mux.url_for(run, _mac_upstream(state))` while `state["mac"]`.
- **`_refresh`, Mac branch**
  - POSTs `mac/acquire` with `{runId, agentId, family, deviceId, generation}` (same 202 polling),
    then `cdp_mux.set_upstream(url, generation, upstream=_mac_upstream(state))`.
  - Never touches `_LEASE` / `RELEASE_GRACE_S`, which are the cloud's.
  - `humanInputAt` is always 0.
  - Casting uses `cdp_mux.cast(..., upstream=_mac_upstream(state))`.
- **`pre_tool_call`.**
  - `computer_use` while `state["mac"]` → block: "computer_use works on the cloud browser's screen
    only; on a Mac use the browser_* tools."
  - Any other failure blocks with `state["error"] or` the existing message.
- **`_end_run` / `abort` / `release`, Mac.** `cdp_mux.close_run(run, name)`, then
  `cdp_mux.drop(name)`, then a daemon thread POSTs `mac/release {runId, deviceId, generation}`
  (190 s timeout).
- **`dispatch_input`.** Unchanged, which makes it cloud-only by task 4.

**Interfaces (`browser_state.py`).**

```python
_DOOR_PATH = re.compile(r"^/api/v1/internal/mac-cdp/[A-Za-z0-9_=-]{16,4096}$")
def _valid_cdp_url(value: object) -> str
    # AgentCore wss as today, OR ws:// + an IPv4 literal in 10/8, 172.16/12 or 192.168/16, port 8000,
    # path _DOOR_PATH, no userinfo/query/fragment.
def _vet(state: dict) -> tuple[dict, set[str]]
    # Mac export -> (vetted state, domains to keep from the workspace). Rules = spec §3, ported from
    # desktop_browser_state._vetted/_good_cookie/_good_storage.
def _merge_three(base, current, previous, open_origins=frozenset(), *, keep=frozenset(), hold_back=False) -> dict
    # keep: domains taken from `previous`; hold_back: if more than max(5, len(base domains) // 2)
    # base domains vanish from `current`, those domains are taken from `previous`.
```

`checkpoint(params)`:

- accepts an optional `"source" in {"cloud", "mac"}` (default `"cloud"`);
- with `"mac"`, applies `_vet` to every Mac-side export (the save's `current`, and the load's
  post-load export used as the next base) and passes `keep` / `hold_back=True` to the save merge;
- leaves cloud behavior byte-identical.

**Interfaces (plugin `desktop.py`).**

- `ACTION_FIELDS` gains `"use_browser": ({**SELECTION}, [*SELECTION])` and
  `"use_cloud_browser": ({}, [])`.
- `handle`, after the existing ctx checks:
  - `use_browser` → `from bridge import browser_control`, then `msg = browser_control.select_mac(...)`,
    then return `{"browser":"mac","deviceId":...}` or `{"error": msg}`;
  - `use_cloud_browser` → `select_cloud()`, then `{"browser":"cloud"}`.
- `SCHEMA["description"]` adds one sentence:
  - `use_browser` switches this run's `browser_*` tools to the selected Mac's agent Chrome (the
    device whose commands list `browser.cdp.v1`);
  - `use_cloud_browser` switches back;
  - `computer_use` stays cloud-only.

**Skill text.**

- The Mac browser uses the same `browser_*` tools after `use_browser`.
- It is the agent's own Chrome on that Mac, never the person's everyday one.
- It has the workspace sign-ins, which sync automatically.
- It has no `file://` or `chrome://` pages, and no uploads of Mac files.
- Downloads land in the Mac's `~/Downloads/Lightbulb agent`.
- Watchers see it view-only.
- Relay each error message verbatim, especially "Update the Lightbulb desktop app …".
- Keep `SKILL.md` untouched (thin); details go in `references/`.

**Tests to add.**

- `test_browser_control.py`:
  - `test_select_mac_acquires_and_points_cdp_url_at_mac_upstream`
  - `test_select_mac_surfaces_backend_message`
  - `test_select_mac_disconnects_cloud_clients`
  - `test_mac_refresh_sets_upstream_generation`
  - `test_mac_run_end_drops_upstream_and_releases`
  - `test_select_cloud_releases_mac`
  - `test_computer_use_blocked_on_mac`
  - `test_mac_frames_cast_on_mac_upstream`
  - `test_mac_never_yields_to_cloud_human_input`
- `test_browser_state.py`:
  - `test_door_url_accepted`
  - `test_door_url_public_or_hostname_refused`
  - `test_door_url_wrong_port_or_path_refused`
  - `test_mac_source_bad_cookie_keeps_workspace_domain`
  - `test_mac_source_drops_bad_origin`
  - `test_mac_source_holds_back_mass_signout`
  - `test_mac_source_vets_load_base`
  - `test_cloud_source_unchanged`
- `test_plugin_desktop.py`:
  - `test_use_browser_selects_mac`
  - `test_use_browser_returns_backend_error_verbatim`
  - `test_use_cloud_browser`
  - `test_use_browser_rejects_bad_ids`

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
// "use_browser" or "browser" ("browser" is dropped in task 8).
export function macBrowserDevice(run): string | undefined
// The deviceId of that same last call, when it selects a Mac.
```

**Tests to add.**

- `it("treats a run whose last selection is use_browser as a Mac run")`
- `it("a run that switched back with use_cloud_browser is not a Mac run")`
- `it("reads the device from the last use_browser call")`
- Keep the existing `browser`-action cases.

**Run.** `cd apps/frontend && npx vitest run src/components/messaging/__tests__/BrowserRunPreview.test.tsx src/components/messaging/__tests__/BrowserRunPreview.frames.test.tsx`

**Done when.** The tests pass. `RoomPanel` and `BrowserDockPanel` need no change: they call these
two functions. The Mac view stays view-only.

---

### Task 7: Desktop — the agent Chrome, the CDP channel, the filter, the wipe

**Goal.** A new signed desktop build serves `browser.cdp.v1` and `browser.wipe.v1` on the native
socket. It launches and pipes its own Chrome, filters methods, and wipes on unpair and Browser-off.
It leaves the OpenClaw browser path untouched (task 10 removes it).

**Files (all under `apps/desktop/src-tauri/src/`).**

- New `agent_chrome.rs`.
- `node_client.rs`:
  - add `"browser.cdp.v1"` and `"browser.wipe.v1"` to the connect `commands` list;
  - route the events `browser.cdp.open|frame|close` to `NodeEvent::Cdp { event: String, payload: Value }`;
  - add `NodeClient::event_sender() -> EventSender`, a clone of `write_tx` with an async `send(event, payload)`;
  - raise the `write_tx` capacity 16 → 1024.
- `node_invoke.rs`:
  - `"browser.cdp.v1"`: if Browser is off → `BROWSER_OFF`; else `agent_chrome::ensure_running` →
    `{"profileId"}` or its failure code;
  - `"browser.wipe.v1"` → `agent_chrome::wipe` → `{"wiped"}`, no switch check;
  - `run()` owns an `agent_chrome::Channels` and feeds it `NodeEvent::Cdp`, calling `close_all()`
    on `Disconnected`.
- `lib.rs`: `mod agent_chrome;`. `forget_pairing` and `revoke_agent_browser` also call
  `agent_chrome::wipe(&app_data)`, after closing channels, logging failures.
- `pair_state.rs`: `STORE_VERSION` 5 → 6, so every updated Mac re-pairs and its grant gains the
  new commands.
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
pub fn seed_preferences(dir: &Path, downloads: &Path) -> std::io::Result<()>; // only when Default/Preferences is absent
pub fn parse_active_port(text: &str) -> Option<(u16, String)>;        // "port\n/devtools/browser/<id>\n"
pub async fn ensure_running(app_data: &Path) -> Result<Endpoint, Failure>;
pub fn wipe(app_data: &Path) -> Result<bool, String>;                 // symlink-safe like agent_browser::wipe_target
pub enum Filtered { Pass(String), Refuse(String /* CDP error reply */) }
pub fn filter(raw: &str, downloads: &Path) -> Filtered;
pub struct Channels { /* channel id -> (tx to Chrome task, JoinHandle) */ }
impl Channels {
    pub fn new(app: tauri::AppHandle, out: EventSender) -> Self;
    pub async fn on_event(&mut self, event: &str, payload: &Value);   // open / frame / close
    pub fn close_all(&mut self);
}
```

**`ensure_running`.**

1. Create the dir (0700) with `seed_preferences` if it is new, and read `profile_id`.
2. If `DevToolsActivePort` parses and `GET http://127.0.0.1:<port>/json/version` answers within
   1 s, return.
3. Otherwise delete a stale `DevToolsActivePort` and run
   `open -nga "Google Chrome" --args --user-data-dir=<dir> --remote-debugging-port=0 --no-first-run --no-default-browser-check`.
   A non-zero exit whose stderr names the app → `CHROME_MISSING`.
4. Poll `DevToolsActivePort` for up to 15 s, else `CHROME_START_FAILED`.
5. Never follow a symlinked dir.

**`Channels::on_event("browser.cdp.open")`.**

1. Browser switch (`desktop_permissions::load(app).browser`) off → close `BROWSER_OFF`.
2. At 4 channels → close `CAPACITY`.
3. `ensure_running`; failure → close with its code.
4. `payload.profileId != endpoint.profile_id` → close `PROFILE_CHANGED`.
5. `connect_async("ws://127.0.0.1:<port><path>")`.
6. Spawn the pump:
   - Chrome text → `browser.cdp.frame`; over 16 MiB → close `TOO_LARGE`;
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
- Rewrite `Browser.setDownloadBehavior` and `Page.setDownloadBehavior` so `downloadPath` is the
  pinned dir, and leave `behavior` as sent.
- A refusal reply is `{"id": <same>, ["sessionId": <same>,] "error": {"code": -32000, "message": "Refused by Lightbulb on this Mac: <Method>"}}`.
- Unparseable JSON is refused with id 0.
- Everything else passes byte-identical.

**Tests to add (Rust `#[cfg(test)]`).**

- `agent_chrome::tests`:
  - `filter_refuses_file_input_and_drag`
  - `filter_refuses_non_web_navigation`: `file:`, `chrome:`, `chrome-extension:`, `devtools:`,
    `javascript:`, and unparseable
  - `filter_allows_http_https_and_about_blank`
  - `filter_rewrites_download_path`
  - `filter_refuses_denied_domains_and_methods`
  - `filter_refusal_echoes_id_and_session`
  - `filter_passes_other_methods_byte_identical`
  - `parse_active_port_reads_port_and_path`
  - `profile_id_is_stable_until_wipe`
  - `wipe_refuses_symlinks_and_parent_components`
  - `seed_preferences_pins_downloads_only_on_a_fresh_profile`
- `node_client` tests:
  - `signed_connect_declares_cdp_and_wipe`
  - `handle_message_routes_cdp_events`
- `pair_state` tests: `load_version_5_is_none`, which forces a re-pair.

**Run.** CI (`desktop-build.yml`, `cargo test --lib`). Locally, only with more than 10 GB free:
`cd apps/desktop/src-tauri && TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --lib agent_chrome:: node_client:: pair_state::`

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
- `apps/infra/hermes/bridge/turn.py`: drop any `browser.proxy` reference.
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
  - move `MAX_UPLOAD_REQUEST` and `CHUNK_BYTES` in as module constants;
  - drop `"browser.proxy"` from `PUBLIC_COMMANDS`, the `browser.proxy` branches in
    `validate_request` / `_execute` / `_execute_before_deadline` / `_dispatch` (`browser_media`),
    and the `BrowserScopes` / `browser_state` constructor argument.
- `core/services/node_broker.py`: drop `browser_media` and `browser.proxy.upload.v1`.
- `core/services/desktop_pair.py`:
  - remove `DESKTOP_CLI_BROWSER_COMMANDS` and `browser.state.v1`, and `"browser"` from
    `cli_caps`;
  - remove `browser.proxy.upload.v1` from `NODE_COMMANDS_DECLARED_UNCONDITIONALLY` only if nothing
    reads it (grep `desktop_pair_live`);
  - `granted_commands` becomes `set(entry.get("commands") or [])`.
- `routers/desktop_execution.py`: delete `/internal/browser-clear` and `/internal/browser-state`,
  and the `browser.proxy` upload size carve-out in `_body`.
- `routers/desktop_pair.py`: drop the old `clear_device` call (keep `mac_browser.clear_device`).
- `routers/webhooks.py`: import `revoke_member_soon` from `core.services.mac_browser`. Move
  `revoke_member_soon` there verbatim, calling only `mac_browser.clear_member`, together with its
  test.
- `core/services/browser_control.py`: fix the comment naming `desktop_browser_state`.
- The `__macbrowser__#` rows stay in place. They are harmless reserved keys that every scan skips,
  and nothing reads them any more. Say so in the PR.
- Frontend: delete `apps/frontend/src/components/desktop/LocalBrowserSetup.tsx` and its test, and
  its use in `LocalSessions.tsx`. That component calls desktop commands task 10 removes, so it must
  ship first; this backend dispatch carries the frontend.
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
- `test_desktop_pair.py`: `test_browser_grant_is_cdp_only`
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
- `scripts/vendor-sidecars.sh`: remove `vendor_agent_browser` and `AGENT_BROWSER_VERSION`.
- `tauri.conf.json`: remove `bin/agent-browser-*`.
- `.github/workflows/desktop-build.yml`: remove the agent-browser find, case and signing
  assertions (lines ~166-236). Leave every other binary's hardened-runtime and entitlement check
  intact.
- `CLAUDE.md`, Desktop App: drop `agent-browser` from the sidecar list.

**Run.** CI `cargo test --lib`. Locally only with more than 10 GB free:
`cargo test --lib local_mcp:: agent_chrome::`

**Done when.**

- CI is green.
- `git grep -n "agent-browser\|browser_state::\|browser\.state\.v1\|local_browser_" apps/desktop .github`
  is empty.
- The signed prod build is installed by the founder, and checklist items 1 and 5 pass again.

---

## Prod verification checklist (founder, prod; after tasks 1-7 are live; again after 10)

Record each result on the PR, with the Mac's `~/Library/Logs/…` desktop log lines and backend
CloudWatch `mac_cdp.door` / `mac_browser.sync` metrics.

1. **Founder Mac, cold start.**
   - Quit the agent Chrome, and delete `<app_data>/agent-chrome`.
   - In a room: "@CEO use my Mac's browser to open news.ycombinator.com and tell me the top story".
   - Expect: `use_browser` → one `starting` → a Chrome window opens on the Mac (it does not take
     focus) → the answer.
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
   - `mac_browser.sync{kind=save,result=ok}`.
5. **Browser-off wipe.**
   - Switch Browser off in the desktop app's Settings.
   - Expect: the agent Chrome quits, `<app_data>/agent-chrome` is gone, and an open run's next
     browser call answers "Browser is switched off for that Mac …".
   - Switch it back on and ask again: expect a fresh `load` (new profile id), with the workspace
     sign-ins present.
6. **Old app message.** On a Mac still on the previous build (or before task 7 ships), `use_browser`
   answers exactly "Update the Lightbulb desktop app on that Mac to let agents use its browser."
7. **Unpair / member removal.** Unpair Aden's Mac from Settings → Devices. Expect
   `mac_browser.clear{result=ok}`, and Aden's `agent-chrome` dir is gone if his Mac was online.
8. **Filter.**
   - Ask an agent on the Mac to `browser_navigate` to `file:///etc/hosts` and to
     `chrome://settings`: both are refused with "Refused by Lightbulb on this Mac".
   - A form upload is refused.
   - A download lands in `~/Downloads/Lightbulb agent`.
