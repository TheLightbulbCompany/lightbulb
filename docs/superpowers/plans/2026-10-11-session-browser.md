# Session Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A shared Claude Code or Codex session browses with the workspace's sign-ins in a Lightbulb browser (its Mac's "Lightbulb agent" Chrome, or the workspace cloud browser), and the people watching the session see and drive that browser from a Browser tab beside its terminal.

**Architecture:** The desktop app serves one fixed loopback CDP port. A connection is identified by its peer process (libproc), walked up to one shared session, and offered to the backend over the node socket (`browser.cdp.offer`). The backend (`core/services/session_browser.py`, on the task holding that Mac's socket) treats the session as run `session-<sessionId>`: on the Mac it takes a `mac_browser` lease and has the runtime run the sign-in sync over the existing ticketed `mac_cdp` door, then tells the Mac to open an `agent_chrome` channel whose Chrome side is the session's own client; on the cloud it mints a `use: "session"` door ticket and the bridge (`browser.attach`) pumps that door into its own `cdp_mux` as one more run. Frames go to the session's existing frames channel as a new `image` kind while someone watches; people who may type into the session drive it through `/browser/mac/input`'s delivery.

**Tech Stack:** Rust (Tauri 2 desktop: tokio, tokio-tungstenite, libc), Python 3 / FastAPI / DynamoDB (backend), Python bridge in the Hermes image (websockets, cdp_mux), Next.js 16 + React 19 + vitest (frontend), `@lightbulb/shared` TypeScript.

**Spec:** `docs/superpowers/specs/2026-10-11-session-browser-design.md` (approved 2026-10-11, with spike results). Read it before any task.

## Global Constraints

- Never the person's everyday Chrome. The Mac path only ever drives `agent_chrome` (`<app_data>/agent-chrome`, window "Lightbulb agent").
- Only shared sessions. Anything else is refused with exactly: `Share this session to use the Lightbulb browser`.
- Codex behaves exactly as Claude Code: same port, same `lightbulb-browser` MCP entry (via `codex mcp add`), same rules.
- Nothing is recorded. Frames flow only while someone has the Browser tab open (viewer heartbeat + the Mac's 15 s cast TTL); they are published once and stored nowhere.
- No capability restrictions in the agent Chrome (owner ruling 2026-09-29). Only plumbing/UX filters: no focus stealing, background tabs/windows, downloads to `~/Downloads/Lightbulb agent`.
- Error codes, verbatim: `not_shared`, `browser_profile_held_by_another_workspace`, `mac_browser_busy`, `cloud_unavailable`.
- `MAX_CHANNELS` = 4 per Mac (desktop `agent_chrome::MAX_CHANNELS` and backend `node_broker.MAX_CDP_CHANNELS`).
- Lease: renewed while the session's browser connection is open; released 300 s after its last command or when the session ends; release saves sign-ins.
- Run id is `session-<sessionId>`; the bridge's Mac upstream is `mac:<deviceId>:session-<sessionId>`.
- Never log a ticket, a door URL, a cookie, a frame or page content: owner ids and exception class names only.
- Ponytail: reuse the existing machinery (mac_browser, mac_cdp, cdp_mux, agent_chrome, agent_cast, session-frames rail, mac_live input delivery). No new event bus, no feature flags, no compatibility shims.
- One worktree per milestone, under `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.claude/worktrees/`, branched from a fresh `origin/main`. Never stack on another branch.
- Each milestone is one PR. Codex is the only reviewer; merge once Codex has reviewed (CLAUDE.md "Code Review").
- CI is the test gate: locally run only the test files a task names.
- The desktop app has no dev build: it is tested on prod, as the founder, after `desktop-build.yml` (CLAUDE.md "Desktop App").
- Bridge changes ship only through the Hermes image: build, flip `hermes-version.json` dev then prod, then `roll_hermes_runtimes.py` (CLAUDE.md "Runtime rollout").
- Planning docs are disposable: the last PR (M4) deletes this plan and the spec.

## Review Focus

1. **chrome-devtools-mcp after its socket closes** (the 300 s idle close, `use_browser`, a backend refusal on renewal): a person expects the next browser tool call to reconnect and work. The reconnect itself is chrome-devtools-mcp's (it re-runs `puppeteer.connect` when its browser is disconnected), so it is pinned by the M1 acceptance check 4 and the M4 acceptance check 2; Task 5's `ending_a_session_channel_tells_the_backend_once` pins that a closed connection leaves no channel or lease behind for the next one to trip on.
2. **A loopback client that is not a shared session** (a web page, another local program, an Only-me session): a person expects a refusal that says why, never a browser. Pinned by Task 6 (`peer_pid`/ancestors), Task 7 (`no_browser` refuses an `Origin`, `refuse` answers in CDP error form).
3. **Two shared Codex conversations served by one Codex process** (the Codex app, or one Codex service for several conversations): the peer process cannot tell them apart; a person expects a clear refusal, never the wrong session's browser. Pinned by Task 6's ambiguity test.
4. **The session ends or stops being shared while its browser is open:** a person expects the connection to close and the sign-ins made so far to be kept. Pinned by Task 3 (an ended session's run still has authority, so its release save runs) and Task 4's unshare test.
5. **Another program holds the port** (spike: an automation Chrome held 9333): a person expects a loud notice and never a session driving a foreign browser. Pinned by Task 7 (`serve` reports `EADDRINUSE` and serves nothing).

## Decisions taken in the plan

These could not be read off the code or the spec. Each is a choice, with its reason.

1. **Run id `session-<sessionId>`, not `session:<sessionId>`.** `desktop_execution._ID`, the bridge's `MacRun`/`_MAC_ID` pattern and `browser_control._ID` all exclude `:`, and `_ID` guards owner, member, agent and device ids too. Widening it widens every one of them; a `-` form fits all of them as they are. The bridge upstream becomes `mac:<device>:session-<sid>`.
2. **Port 39222.** Not 9222 (Chrome's default), not 9333 (seen held in the spike). Fixed, fails loudly.
3. **The MCP entry is written when session sharing is enabled** (`native_mcp::configure`, which already runs on enable/disable), not literally "at pairing": only shared sessions can use it, and `configure(false)` removes it again.
4. **`npx -y chrome-devtools-mcp@latest --browserUrl http://127.0.0.1:39222`**, Google's documented install line; the spike did not record a version to pin.
5. **Ancestor walk via `proc_pidinfo(PROC_PIDTBSDINFO).pbi_ppid`** (the existing `holder::identity::read`), not `sysctl KERN_PROC_PID`: the same kernel fact, already in the codebase, own-user processes only.
6. **`socket_fdinfo` read at fixed byte offsets.** `libc` has `proc_pidfdinfo` but not the struct; the `libproc` crate needs bindgen at build time. The offsets (`soi_kind` 256, `insi_fport` 264, `insi_lport` 268, size 792) are pinned by a live self-test on a real loopback socket.
7. **The Mac closes a session's connection 300 s after its last command**; chrome-devtools-mcp reconnects on its next tool call (Review Focus 1). The backend cannot see Mac-local CDP traffic, so "renewed while traffic flows" is "renewed while the channel is open", which the idle close bounds.
8. **The backend drives the Mac sync, the runtime runs it, the app only proxies** (spike 3): `container_rpc` `browser.state` with a `doorUrl` reuses `browser_state.checkpoint` over a named `cdp_mux` upstream. The bridge already runs Mac syncs this way for its own runs.
9. **A session-family run keeps authority after its session ends** (only unshared, removed or a departed member are refused), so the release's save still passes the door's re-check.
10. **Frames: `workspace.session.screen` with `kind: "image"`, the thumb tier only** (480 px JPEG, at most 2 a second, ~10 KB), budgeted per session under `<sid>#browser`. No desktop capability flag: the only Macs that receive a session channel's `browser.cast.set` are those that sent `browser.cdp.offer`, and old web/iOS clients already drop an unknown kind (`parseLiveEvent` rejects it).
11. **Watch demand is `browserWatchedAt` on the session META row**, stamped by the Browser tab every 20 s; frames are wanted while it is younger than `mac_live.WANTED_S` (45 s), re-set every `SET_EVERY_S` (10 s), as for room runs.
12. **Mac-path input reuses `/browser/mac/input`'s delivery** (`mac_live._live`, `deliver_mac_input`, the peer route); a session entry has no room and re-checks `may_control` where the socket is. Cloud input maps the same event shape onto the cloud cast's `click`/`wheel`/`key`/`text` (`browser_control.send_input`). The human-input tag: on the cloud a watcher's input counts as a person's input, as today; on the Mac the per-channel `Lightbulb.humanInput` is not sent to a session channel (its client, chrome-devtools-mcp, has no yield to make), while the Mac's own watch hook keeps telling agent runs on that Chrome.
13. **`browserOpen {where, until}` on the META row drives the Browser tab** (`LocalSession.browser`), renewed with the lease; it and `browserPlace` survive re-registration.
14. **Cloud STARTING is retried by the backend, not the app:** each `browser.attach` gets a fresh single-use ticket (tickets live 60 s); the Mac waits up to 150 s for an answer.
15. **The runtime is kept awake by a `health` invoke every 120 s** while a session browses the cloud (AgentCore parks a microVM with no invokes; the door is outbound and does not count).
16. **The hosted `use_browser` takes a `session_id`:** the hosted MCP is account-scoped and cannot know which session calls it; only people who may control that session can move it. The Mac's native `use_browser` derives the session and also closes its open connection so the next one follows at once.
17. **The desktop-execution gate applies to both paths** (`DESKTOP_EXECUTION_ENABLED` + allowlist): `mac_browser.acquire` and the `mac_cdp` door already check it. Error `desktop_disabled`.
18. **Web and desktop only.** The iPhone app gets no Browser tab in this version; "Anyone with the link" viewers get no browser frames.
19. **Browser off routes to the cloud** using the offer's `macBrowser` flag (the Mac's own `desktop_permissions.browser`) and the node's declared `browser.cdp.v1`; M1, before the cloud path exists, refuses it with `browser_off`.

## File Structure

| File | Responsibility | Milestone |
|---|---|---|
| `apps/desktop/src-tauri/src/session_browser.rs` (new) | Loopback port, peer pid + ancestors, offers, decisions, relay | M1, M3, M4 |
| `apps/desktop/src-tauri/src/agent_chrome.rs` | Local (session) channels: `open_local`, `end_local`, `filter_for`, no human-input signal for them | M1 |
| `apps/desktop/src-tauri/src/node_invoke.rs` | Route backend CDP events to `session_browser::on_event` first | M1 |
| `apps/desktop/src-tauri/src/lib.rs` | `mod session_browser;` and start `serve` at setup | M1 |
| `apps/desktop/src-tauri/src/local_sessions/native_tools.rs` | `live_mirrors`, `session_of_ancestors`, `use_browser` route | M1, M4 |
| `apps/desktop/src-tauri/src/local_sessions.rs` | `shared_session_for`, `session_still_shared` | M1 |
| `apps/desktop/src-tauri/src/local_sessions/enrollment.rs` | `session_scope` | M1 |
| `apps/desktop/src-tauri/src/local_sessions/native_mcp.rs` | `lightbulb-browser` entry; `use_browser` tool | M1, M4 |
| `apps/backend/core/services/session_browser.py` (new) | Offer → lease → sync → open → hold → release; frames; input; cloud; place | M1–M4 |
| `apps/backend/core/services/node_broker.py` | `browser.cdp.offer`, `cdp_accept`, local channels, close message | M1 |
| `apps/backend/core/services/desktop_execution.py` | `session` run family authority | M1 |
| `apps/backend/core/services/mac_cdp.py` | `session` family; `use: "session"` door | M1, M3 |
| `apps/backend/core/services/mac_browser.py` | `acquire` answers `profileId` | M1 |
| `apps/backend/core/services/local_sessions.py` | `browser` projection, `browser_open`, kept keys | M1 |
| `apps/backend/routers/gateway_relay.py` | Wire `on_offer` | M1 |
| `apps/backend/core/services/mac_live.py` | `live()` registration; session input branch | M2 |
| `apps/backend/routers/messaging.py` | Browser viewing/input routes; device `browser-place` route | M2, M3, M4 |
| `apps/backend/core/services/hosted_mcp.py` | `use_browser` tool | M4 |
| `apps/infra/hermes/bridge/browser_control.py` | `mac_checkpoint`; `attach_session`, door pump, session sink, `session_frames` | M1, M3 |
| `apps/infra/hermes/bridge/server.py` | `browser.state` door branch; `browser.attach`; `browser.frames` run | M1, M3 |
| `packages/shared/src/live/types.ts`, `packages/shared/src/messaging/types.ts`, `packages/shared/src/index.ts` | `image` kind; `LocalSession.browser` | M2 |
| `apps/frontend/src/components/workspace/panels/BrowserFrameView.tsx` | `session` mode | M2 |
| `apps/frontend/src/components/messaging/SessionBrowserTab.tsx` (new) | The Browser tab body | M2 |
| `apps/frontend/src/components/messaging/LocalSessionPanel.tsx`, `SessionTerminalView.tsx` | Terminal/Browser tabs; ignore image frames | M2 |
| `packages/claude-plugin/lightbulb/skills/lightbulb-session-sharing/SKILL.md` | What a session's agent is told | M1, M3, M4 |
| `apps/infra/hermes/skills-src/lightbulb/references/surfaces.md`, `MAINTENANCE.md` | What Hermes agents tell people | M1–M4 |

---

# Milestone M1 — the Mac path

Branch: `feat/session-browser-mac` in `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.claude/worktrees/session-browser-mac`, from `origin/main`:

```bash
cd /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync
git fetch origin main
git worktree add .claude/worktrees/session-browser-mac -b feat/session-browser-mac origin/main
cd .claude/worktrees/session-browser-mac
```

### Task 1: Bridge — `browser.state` with a Mac door

**Files:**
- Modify: `apps/infra/hermes/bridge/browser_control.py` (constants near `_ID` at line 99; `_run_mac_sync` at lines 311-344; new `_mac_steps`, `mac_checkpoint` after it)
- Modify: `apps/infra/hermes/bridge/server.py:404-410` (`_browser_state`)
- Test: `apps/infra/hermes/tests/test_browser_control.py`, `apps/infra/hermes/tests/test_server_routes.py`

**Interfaces:**
- Consumes: `cdp_mux.set_upstream(url, generation, upstream=)`, `cdp_mux.url_for(run, upstream=)`, `cdp_mux.close_run`, `cdp_mux.drop`, `browser_state.checkpoint(params, budget_s=)`.
- Produces: `browser_control.mac_checkpoint(params: dict) -> {"ok": bool}`; RPC `browser.state` with `{"doorUrl", "generation", "upstream", "steps"}` answers `{"ok": true, "payload": {"ok": bool}}`. Used by Task 4 (`session_browser._sync`).

- [ ] **Step 1: Write the failing tests**

Append to `apps/infra/hermes/tests/test_browser_control.py` (it already has the `clean` fixture and `_checkpoints`):

```python
DOOR = "ws://10.0.3.7:8000/api/v1/internal/mac-cdp/gAAAAB-tick_et="
SESSION_UP = "mac:mac-1:session-11111111-1111-4111-8111-111111111111"


def test_mac_checkpoint_runs_the_steps_on_the_door_then_forgets_it(monkeypatch, clean):
    ran = _checkpoints(monkeypatch)
    params = {"doorUrl": DOOR, "generation": "g1", "upstream": SESSION_UP,
              "steps": [{"action": "save"}, {"action": "load"}]}
    assert browser_control.mac_checkpoint(params) == {"ok": True}
    assert clean["upstream"] == [(DOOR, "g1", SESSION_UP)]
    assert [p["cdpUrl"] for p in ran] == [f"ws://mux/{SESSION_UP}/sync:{SESSION_UP}"] * 2
    assert [(p["action"], p["source"]) for p in ran] == [("save", "mac"), ("load", "mac")]
    assert (f"sync:{SESSION_UP}", SESSION_UP) in clean["closed"] and clean["dropped"] == [SESSION_UP]


def test_mac_checkpoint_stops_at_a_failed_step_and_still_forgets_the_door(monkeypatch, clean):
    ran = _checkpoints(monkeypatch, fail="save")
    params = {"doorUrl": DOOR, "generation": "g1", "upstream": SESSION_UP,
              "steps": [{"action": "save"}, {"action": "load"}]}
    assert browser_control.mac_checkpoint(params) == {"ok": False}
    assert [p["action"] for p in ran] == ["save"]
    assert clean["dropped"] == [SESSION_UP]


@pytest.mark.parametrize("bad", [{"doorUrl": "wss://evil.example/x"}, {"doorUrl": "ws://a/api/v1/other/t"},
                                 {"upstream": "cloud"}, {"steps": []}])
def test_mac_checkpoint_refuses_anything_but_a_backend_door(clean, bad):
    params = {"doorUrl": DOOR, "generation": "g1", "upstream": SESSION_UP, "steps": [{"action": "load"}]} | bad
    with pytest.raises(ValueError):
        browser_control.mac_checkpoint(params)
    assert clean["upstream"] == []
```

Append to `apps/infra/hermes/tests/test_server_routes.py`:

```python
def test_browser_state_with_a_door_runs_the_mac_checkpoint(monkeypatch):
    seen = []
    monkeypatch.setattr(server.browser_control, "mac_checkpoint", lambda params: seen.append(params) or {"ok": True})
    params = {"doorUrl": "ws://x/api/v1/internal/mac-cdp/t", "steps": [{"action": "load"}]}
    code, obj = server.handle_plugin("/plugins/lightbulb-channel/rpc", "POST", AUTH,
                                     {"method": "browser.state", "params": params})
    assert (code, obj) == (200, {"ok": True, "payload": {"ok": True}}) and seen == [params]
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/infra/hermes && uv run --no-project --with pytest --with botocore --with pyyaml --with websockets python -m pytest tests/test_browser_control.py tests/test_server_routes.py -q -k "mac_checkpoint or door"`
Expected: FAIL with `AttributeError: module 'bridge.browser_control' has no attribute 'mac_checkpoint'`.

- [ ] **Step 3: Implement**

In `browser_control.py`, after `_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")` (line 99):

```python
#: A backend ticketed Mac door (mac_cdp.door_url): ws://<task>/api/v1/internal/mac-cdp/<ticket>.
_DOOR = re.compile(r"ws://[A-Za-z0-9.:-]+/api/v1/internal/mac-cdp/[A-Za-z0-9_=-]+")
#: A named Mac upstream, ``mac:<device>:<run>``.
_MAC_UPSTREAM = re.compile(r"mac:[A-Za-z0-9_-]{1,128}:[A-Za-z0-9_-]{1,128}")
```

Replace the body of `_run_mac_sync` (lines 311-344) so the step loop is shared:

```python
def _mac_steps(url: str, steps: list, deadline: float) -> tuple[bool, str | None]:
    """Each step through ``browser_state.checkpoint`` on ``url`` (a loopback client on a Mac pipe), in
    order, stopping at the first failure: (all ran, the last step's action). Never logs a URL or a step."""
    kind = None
    for step in steps:
        kind = step.get("action") if isinstance(step, dict) else None
        left = deadline - time.monotonic()
        if kind is None or left <= 0:
            return False, kind
        try:
            browser_state.checkpoint({**step, "source": "mac", "cdpUrl": url}, budget_s=left)
        except Exception as exc:  # noqa: BLE001 -- the class only
            log.warning("mac browser sync failed: %s", type(exc).__name__)
            return False, kind
    return True, kind


def _run_mac_sync(state: dict, sync: dict, deadline: float | None = None) -> bool:
    """The one sync engine: each step through ``browser_state.checkpoint`` over a loopback client
    on the run's own Mac pipe, in order, stopping at the first failure; then ``mac/synced``.
    Never logs a URL, a step or an exception message."""
    deadline = time.monotonic() + MAC_SYNC_BUDGET_S if deadline is None else deadline
    mac = state.get("mac")
    name, key = _mac_upstream(state, mac), f"sync:{state['runId']}"
    if name is None:  # deselected before this thread ran: the claim lapses on the backend
        return False
    device = mac["deviceId"]
    try:
        ok, kind = _mac_steps(cdp_mux.url_for(key, upstream=name), sync.get("steps") or [], deadline)
    except Exception as exc:  # noqa: BLE001 -- the mux did not start
        ok, kind = False, None
        log.warning("mac browser sync failed: %s", type(exc).__name__)
    finally:
        with contextlib.suppress(Exception):
            cdp_mux.close_run(key, name)
    body = {"runId": state["runId"], "agentId": state["agentId"], "family": state["family"],
            "deviceId": device, "syncId": str(sync.get("syncId") or ""), "ok": ok}
    if not ok and kind:  # a failed save gets a muted note in the room (the backend posts it)
        body["kind"] = kind
        if state.get("roomId"):
            body["roomId"] = state["roomId"]
    _post_mac("mac/synced", body, timeout=max(MAC_SYNCED_MIN_S, min(20.0, deadline - time.monotonic())))
    return ok


def mac_checkpoint(params: dict) -> dict:
    """``browser.state`` with a ``doorUrl`` (spec 2026-10-11, spike 3): the backend's own sign-in sync
    for a shared Claude Code or Codex session's Mac browser. Points the named upstream at the ticketed
    door, runs the steps over one loopback client on it, then forgets the upstream. Same engine as a
    run's own sync; raises ValueError for anything but a backend door."""
    door, upstream, steps = params.get("doorUrl"), params.get("upstream"), params.get("steps")
    if (not (isinstance(door, str) and _DOOR.fullmatch(door))
            or not (isinstance(upstream, str) and _MAC_UPSTREAM.fullmatch(upstream))
            or not isinstance(steps, list) or not steps):
        raise ValueError("invalid Mac checkpoint")
    key = f"sync:{upstream}"
    try:
        cdp_mux.set_upstream(door, str(params.get("generation") or ""), upstream=upstream)
        ok, _ = _mac_steps(cdp_mux.url_for(key, upstream=upstream), steps, time.monotonic() + MAC_SYNC_BUDGET_S)
    finally:
        with contextlib.suppress(Exception):
            cdp_mux.close_run(key, upstream)
        with contextlib.suppress(Exception):
            cdp_mux.drop(upstream)
    return {"ok": ok}
```

In `server.py`, replace `_browser_state` (lines 404-410):

```python
def _browser_state(params: dict, **_kwargs) -> dict:
    """Checkpoint through the isolated state helper; never return its URL or exception text. A
    ``doorUrl`` is the backend's own Mac sync for a shared session (browser_control.mac_checkpoint)."""
    try:
        if (params or {}).get("doorUrl"):
            return {"ok": True, "payload": browser_control.mac_checkpoint(params)}
        from bridge import browser_state
        return {"ok": True, "payload": browser_state.checkpoint(params)}
    except Exception:
        return {"ok": False, "error": {"code": "UNAVAILABLE", "message": "browser checkpoint failed"}}
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/infra/hermes && uv run --no-project --with pytest --with botocore --with pyyaml --with websockets python -m pytest tests/test_browser_control.py tests/test_server_routes.py -q`
Expected: PASS (the existing Mac sync tests too: `_run_mac_sync` behaves as before).

- [ ] **Step 5: Commit**

```bash
git add apps/infra/hermes/bridge/browser_control.py apps/infra/hermes/bridge/server.py apps/infra/hermes/tests/test_browser_control.py apps/infra/hermes/tests/test_server_routes.py
git commit -m "feat(bridge): browser.state runs a backend Mac door's sign-in sync"
```

### Task 2: Backend — `browser.cdp.offer` in the node protocol

**Files:**
- Modify: `apps/backend/core/services/node_broker.py` (`CDP_EVENTS` line 42; `CdpChannel.__init__` lines 195-204; `NodeSession.__init__` lines 256-268; `cdp_open` lines 397-421; `cdp_close` lines 434-441; `_cdp_event` lines 502-528)
- Test: `apps/backend/tests/unit/services/test_node_broker.py`

**Interfaces:**
- Produces: `node_broker.BUSY_MESSAGE: str`; `CdpChannel(channel_id: str | None = None)` with `.local: bool`; `NodeSession.on_offer: Callable[[CdpChannel, str, bool], None] | None`; `async NodeSession.cdp_accept(channel, payload: dict) -> None` (raises `CdpClosed`, `NodeCapacity`); `NodeSession.cdp_close(channel, code="CLOSED", message: str | None = None)`. Used by Tasks 4, 11, 15.

- [ ] **Step 1: Write the failing tests**

Append to `apps/backend/tests/unit/services/test_node_broker.py`:

```python
SID = "11111111-1111-4111-8111-111111111111"
OFFER = "a" * 32


def _offer(session, cid=OFFER, sid=SID, mac=True):
    session.resolve({"type": "event", "event": "browser.cdp.offer",
                     "payload": {"channel": cid, "sessionId": sid, "macBrowser": mac}})


def _out(session):
    frames = []
    while not session.cdp_out.empty():
        frames.append(session.cdp_out.get_nowait())
    return frames


async def test_an_offer_registers_the_macs_channel_and_hands_it_to_the_relay():
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    seen = []
    session.on_offer = lambda channel, sid, mac: seen.append((channel.id, sid, mac))
    _offer(session)
    _offer(session)  # a replay changes nothing
    assert seen == [(OFFER, SID, True)] and session.cdp[OFFER].id == OFFER


@pytest.mark.parametrize(("cid", "sid"), [("../x", SID), (OFFER, "not-a-session"), (OFFER.upper(), SID)])
async def test_a_malformed_offer_is_ignored(cid, sid):
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    session.on_offer = lambda *args: pytest.fail("offered")
    _offer(session, cid=cid, sid=sid)
    assert session.cdp == {} and _out(session) == []


async def test_an_offer_past_the_channel_cap_is_refused_busy():
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    session.on_offer = lambda *args: None
    for i in range(node_broker.MAX_CDP_CHANNELS):
        _offer(session, cid=f"{i:032x}")
    _offer(session, cid="f" * 32)
    assert _out(session) == [{"type": "event", "event": "browser.cdp.close", "payload": {
        "channel": "f" * 32, "code": "mac_browser_busy", "message": node_broker.BUSY_MESSAGE}}]
    assert "f" * 32 not in session.cdp


async def test_accept_opens_the_offered_channel_and_waits_for_opened():
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    session.on_offer = lambda *args: None
    _offer(session)
    accepted = asyncio.create_task(session.cdp_accept(session.cdp[OFFER], {"profileId": "p1"}))
    await asyncio.sleep(0)
    assert _out(session) == [{"type": "event", "event": "browser.cdp.open",
                              "payload": {"channel": OFFER, "nodeId": "mac1", "profileId": "p1"}}]
    session.resolve({"type": "event", "event": "browser.cdp.opened", "payload": {"channel": OFFER}})
    await asyncio.wait_for(accepted, 1)


async def test_a_local_channels_cdp_frames_are_never_queued():
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    session.on_offer = lambda *args: None
    _offer(session)
    channel = session.cdp[OFFER]
    channel.local = True
    session.resolve({"type": "event", "event": "browser.cdp.frame", "payload": {"channel": OFFER, "data": "{}"}})
    assert channel.inbound.empty() and OFFER in session.cdp


async def test_a_close_can_carry_the_message_a_session_is_shown():
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    session.on_offer = lambda *args: None
    _offer(session)
    session.cdp_close(session.cdp[OFFER], "not_shared", "Share this session to use the Lightbulb browser.")
    assert _out(session)[-1]["payload"] == {"channel": OFFER, "code": "not_shared",
                                            "message": "Share this session to use the Lightbulb browser."}
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_node_broker.py -v -k "offer or accept or local_channel or carry"`
Expected: FAIL (`AttributeError: ... has no attribute 'on_offer'` / `BUSY_MESSAGE`).

- [ ] **Step 3: Implement**

`node_broker.py` — constants (after line 44):

```python
CDP_EVENTS = (
    "browser.cdp.opened", "browser.cdp.frame", "browser.cdp.close", "browser.cast.frame", "browser.cdp.offer",
)  # fmt: skip
#: A full Mac's refusal, as a session's CDP client reads it (mac_browser.MESSAGES uses the same text).
BUSY_MESSAGE = "This Mac's browser is busy; try again shortly."
_OFFER_ID = re.compile(r"[0-9a-f]{32}")
_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
```

(remove the old one-line `CDP_EVENTS` at line 42).

`CdpChannel.__init__`:

```python
    def __init__(self, channel_id: str | None = None) -> None:
        self.id = channel_id or str(uuid.uuid4())
        self.inbound: _ByteQueue = _ByteQueue(CDP_INBOUND_QUEUE)
        self.closed = asyncio.Event()
        self.opened: asyncio.Future = asyncio.get_running_loop().create_future()
        self.close_code: str | None = None
        self.cast: dict[str, dict] = {}  # latest frame per tier ("view"/"thumb") — a slot, not a queue
        # Consumer contract: clear this event, THEN pop both `cast` slots — clearing first means a
        # frame that lands mid-drain re-sets it, so the next wait wakes again instead of losing it.
        self.cast_ready = asyncio.Event()
        #: A shared session's Mac-path channel (session_browser): the Mac serves its CDP to the
        #: session's own client, so none of its CDP frames is ever ours to queue.
        self.local = False
```

`NodeSession.__init__` — add after `self.catalog: list[dict] = []`:

```python
        #: (channel, sessionId, macBrowser) for a `browser.cdp.offer`; the relay wires it to
        #: session_browser.start. None: offers are ignored (the Mac gives up after its wait).
        self.on_offer = None
```

Replace `cdp_open` and add `cdp_accept`:

```python
    async def cdp_open(self, profile_id: str, notice: str | None = None) -> CdpChannel:
        """`notice`: plain text the Mac shows its owner once for this channel (a Paperclip run)."""
        if CDP_COMMAND not in self.commands:
            raise CommandNotAllowed(CDP_COMMAND)
        if len(self.cdp) >= MAX_CDP_CHANNELS or self.cdp_out.full():
            raise NodeCapacity()
        channel = CdpChannel()
        self.cdp[channel.id] = channel
        await self.cdp_accept(channel, {"profileId": profile_id} | ({"notice": notice[:200]} if notice else {}))
        return channel

    async def cdp_accept(self, channel: CdpChannel, payload: dict) -> None:
        """Open `channel` on the Mac (`browser.cdp.open` carrying `payload`) and wait for `opened`."""
        if self.cdp_out.full():
            self.cdp_close(channel, "CAPACITY")
            raise NodeCapacity()
        self.cdp_out.put_nowait(
            {"type": "event", "event": "browser.cdp.open", "payload": {"channel": channel.id, "nodeId": self.node_id, **payload}}
        )
        try:
            await asyncio.wait_for(asyncio.shield(channel.opened), timeout=CDP_OPEN_TIMEOUT_S)
        except asyncio.TimeoutError:
            self.cdp_close(channel, "TIMEOUT")
            raise CdpClosed("TIMEOUT") from None
        except asyncio.CancelledError:
            self.cdp_close(channel)
            raise
```

Replace `cdp_close`:

```python
    def cdp_close(self, channel: CdpChannel, code: str = "CLOSED", message: str | None = None) -> None:
        """Idempotent. Never touches `inbound`: this runs inside the relay reader, which must not raise.
        `message`: what a shared session's own CDP client is shown (its refusal)."""
        if self.cdp.pop(channel.id, None) is channel and not self.cdp_out.full():
            # A full lane drops the close; the Mac closes its side when the socket or Chrome ends.
            payload = {"channel": channel.id, "code": code} | ({"message": message[:300]} if message else {})
            self.cdp_out.put_nowait({"type": "event", "event": "browser.cdp.close", "payload": payload})
        channel._end(code)
```

In `_cdp_event`, handle the offer before the channel lookup, and drop a local channel's frames:

```python
    def _cdp_event(self, frame: dict) -> None:
        payload = frame.get("payload")
        if frame["event"] == "browser.cdp.offer":
            if isinstance(payload, dict):
                self._offer(payload)
            return
        channel = self.cdp.get(str(payload.get("channel") or "")) if isinstance(payload, dict) else None
        if channel is None:
            return
        event = frame["event"]
        if event == "browser.cdp.opened":
            if not channel.opened.done():
                channel.opened.set_result(None)
        elif event == "browser.cdp.frame":
            if channel.local:
                return  # the Mac serves this channel's CDP itself
            data = payload.get("data")
            ...  # unchanged from here
```

Add `_offer` after `_cdp_event`:

```python
    def _offer(self, payload: dict) -> None:
        """A shared Claude Code or Codex session on this Mac asks for a browser (spec 2026-10-11)."""
        cid, sid = payload.get("channel"), payload.get("sessionId")
        if (
            not (isinstance(cid, str) and _OFFER_ID.fullmatch(cid))
            or not (isinstance(sid, str) and _SESSION_ID.fullmatch(sid))
            or cid in self.cdp
            or self.on_offer is None
        ):
            return
        channel = self.cdp[cid] = CdpChannel(cid)
        if len(self.cdp) > MAX_CDP_CHANNELS:
            self.cdp_close(channel, "mac_browser_busy", BUSY_MESSAGE)
            return
        self.on_offer(channel, sid, payload.get("macBrowser") is True)
```

`_offer` runs inside `resolve`, so it needs a running loop for `CdpChannel` (it has one: the relay reader).

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_node_broker.py tests/unit/services/test_mac_cdp.py -v`
Expected: PASS (existing `cdp_open` tests unchanged: the open payload is the same dict).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/node_broker.py apps/backend/tests/unit/services/test_node_broker.py
git commit -m "feat(node-broker): a Mac can offer a shared session's CDP channel"
```

### Task 3: Backend — the `session` run family

**Files:**
- Create: `apps/backend/core/services/session_browser.py` (ids only; Task 4 grows it)
- Modify: `apps/backend/core/services/desktop_execution.py:792-845` (`authorize_run`, new branch before `if family == "room":`)
- Modify: `apps/backend/core/services/mac_cdp.py:93-106` (`_well_formed`)
- Modify: `apps/backend/core/services/mac_browser.py:61-97` (`MESSAGES`), `:750-757` (`acquire`'s answer)
- Test: `apps/backend/tests/unit/services/test_desktop_execution.py`, `test_mac_cdp.py`, `test_mac_browser.py`

**Interfaces:**
- Produces: `session_browser.RUN_PREFIX = "session-"`, `AGENT_ID = "session"`, `run_id(session_id) -> str`, `session_of(run: str) -> str | None`, `auth_for(owner, member, session_id) -> dict` (`{"ownerId","memberId","runId","agentId","family":"session","roomId":""}`); `authorize_run(..., family="session")` returns that dict; `mac_browser.acquire` answers `"profileId"`.

- [ ] **Step 1: Write the failing tests**

Append to `apps/backend/tests/unit/services/test_desktop_execution.py`:

```python
SESSION_SID = "11111111-1111-4111-8111-111111111111"
SESSION_RUN = f"session-{SESSION_SID}"


@pytest.fixture
def session_row(monkeypatch):
    from core.repositories import local_sessions_repo
    from core.services import clerk_admin

    row = {"ownerId": "org_1", "sessionId": SESSION_SID, "humanUserId": "user_1", "deviceId": "mac1"}

    async def get_session(owner, sid):
        return dict(row) if (owner, sid) == ("org_1", SESSION_SID) else None

    async def members(org):
        return [{"user_id": "user_1"}]

    monkeypatch.setattr(local_sessions_repo, "get_session", get_session)
    monkeypatch.setattr(clerk_admin, "list_organization_members", members)
    return row


async def test_a_shared_session_is_its_own_persons_run(session_row):
    auth = await execution.authorize_run("org_1", SESSION_RUN, "session", "session", active=True)
    assert auth == {"ownerId": "org_1", "memberId": "user_1", "runId": SESSION_RUN, "agentId": "session",
                    "family": "session", "roomId": ""}
    assert execution.valid_auth(auth)


async def test_an_ended_session_keeps_its_authority_so_its_save_runs(session_row):
    session_row["endedAt"] = 1
    assert (await execution.authorize_run("org_1", SESSION_RUN, "session", "session", active=True))["memberId"] == "user_1"


@pytest.mark.parametrize("change", [{"unshared": True}, {"removedAt": 1}, {"ownerId": "org_2"}])
async def test_an_unshared_or_removed_session_has_no_run(session_row, change):
    session_row.update(change)
    with pytest.raises(execution.DesktopError) as err:
        await execution.authorize_run("org_1", SESSION_RUN, "session", "session", active=True)
    assert err.value.code == "run_forbidden"


async def test_a_session_run_needs_its_own_agent_id_and_a_member(session_row, monkeypatch):
    from core.services import clerk_admin

    with pytest.raises(execution.DesktopError):
        await execution.authorize_run("org_1", SESSION_RUN, "a1", "session", active=True)
    with pytest.raises(execution.DesktopError):
        await execution.authorize_run("org_1", "session-not-a-uuid", "session", "session", active=True)

    async def nobody(org):
        return []

    monkeypatch.setattr(clerk_admin, "list_organization_members", nobody)
    with pytest.raises(execution.DesktopError):
        await execution.authorize_run("org_1", SESSION_RUN, "session", "session", active=True)
```

(The file imports the module as `execution`.)

Append to `apps/backend/tests/unit/services/test_mac_cdp.py`:

```python
SESSION = {"ownerId": "org_1", "memberId": "user_1", "runId": "session-11111111-1111-4111-8111-111111111111",
           "agentId": "session", "family": "session", "roomId": ""}


def test_a_session_runs_ticket_round_trips():
    claims = mac_cdp.redeem(mac_cdp.mint(target=TARGET, profile_id="p1", auth=SESSION))
    assert claims["family"] == "session" and claims["use"] == "agent"
    assert mac_cdp.auth_from_claims(claims) == SESSION
```

Append to `apps/backend/tests/unit/services/test_mac_browser.py`:

```python
SESSION = {"ownerId": OWNER, "memberId": MEMBER, "runId": "session-11111111-1111-4111-8111-111111111111",
           "agentId": "session", "family": "session", "roomId": ""}


async def test_a_session_acquires_its_requesters_mac_and_learns_the_profile_id(h):
    out = await mac_browser.acquire(SESSION, None)
    assert out["deviceId"] == NATIVE and out["profileId"] == "prof1"
    assert h.row["leases"] == {SESSION["runId"]: int(NOW + mac_browser.LEASE_SECONDS)}
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_desktop_execution.py tests/unit/services/test_mac_cdp.py tests/unit/services/test_mac_browser.py -v -k "session"`
Expected: FAIL (`unsupported_run_family`, ticket `invalid`, `KeyError: 'profileId'`).

- [ ] **Step 3: Implement**

Create `apps/backend/core/services/session_browser.py`:

```python
"""A shared Claude Code or Codex session's browser (spec docs/superpowers/specs/2026-10-11-session-browser-design.md).

The session is a run, `session-<sessionId>`, so it uses the Mac browser lease and the workspace
sign-in sync exactly as an agent run does. Its person is the run's member. Never log a ticket, a
URL or a frame: owner ids and exception class names only.
"""

from __future__ import annotations

import re

RUN_PREFIX = "session-"
AGENT_ID = "session"
_SID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def run_id(session_id: str) -> str:
    return RUN_PREFIX + session_id


def session_of(run: object) -> str | None:
    """The session id of a `session-<id>` run, else None."""
    sid = run[len(RUN_PREFIX) :] if isinstance(run, str) and run.startswith(RUN_PREFIX) else ""
    return sid if _SID.fullmatch(sid) else None


def auth_for(owner: str, member: str, session_id: str) -> dict:
    """What `desktop_execution.authorize_run` answers for this session's run."""
    return {
        "ownerId": owner,
        "memberId": member,
        "runId": run_id(session_id),
        "agentId": AGENT_ID,
        "family": "session",
        "roomId": "",
    }
```

`desktop_execution.authorize_run` — insert before `if family == "room":` (line 833):

```python
    if family == "session":
        # A shared Claude Code or Codex session's browser (spec 2026-10-11): its own person, read
        # off its row. An ended session keeps it, so the release's save passes the door's re-check.
        from core.repositories import local_sessions_repo
        from core.services.session_browser import AGENT_ID, session_of

        sid = session_of(run_id)
        row = await local_sessions_repo.get_session(owner_id, sid) if sid else None
        member_id = (row or {}).get("humanUserId")
        if (
            agent_id != AGENT_ID
            or not row
            or row.get("ownerId") != owner_id
            or row.get("unshared")
            or row.get("removedAt") is not None
            or not isinstance(member_id, str)
            or not _ID.fullmatch(member_id)
        ):
            raise DesktopError("run_forbidden", 403)
        if owner_id != member_id:
            from core.services.clerk_admin import list_organization_members

            if not owner_id.startswith("org_") or not any(
                m.get("user_id") == member_id for m in await list_organization_members(owner_id)
            ):
                raise DesktopError("run_forbidden", 403)
        return {
            "ownerId": owner_id,
            "memberId": member_id,
            "runId": run_id,
            "agentId": agent_id,
            "family": family,
            "roomId": "",
        }
```

`mac_cdp._well_formed` — the family set:

```python
    return (
        claims["use"] == "agent"
        and claims["family"] in {"room", "paperclip", "call", "session"}
        and valid_auth(auth_from_claims(claims))
    )
```

`mac_browser.py` — import `BUSY_MESSAGE` beside `CAST_COMMAND` (`from core.services.node_broker import BUSY_MESSAGE, CAST_COMMAND`), set `"mac_browser_busy": BUSY_MESSAGE,` in `MESSAGES`, and add the profile id to `acquire`'s answer:

```python
    return {
        "deviceId": native,
        "generation": generation,
        "cdpUrl": mac_cdp.door_url(target, mac_cdp.mint(target=target, profile_id=profile_id, auth=auth)),
        "deviceName": await _device_name(owner, native),
        "humanInputAt": 0,
        # The profile this lease is for: a shared session's channel opens on it (session_browser).
        "profileId": profile_id,
        "sync": decided["sync"],
    }
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_desktop_execution.py tests/unit/services/test_mac_cdp.py tests/unit/services/test_mac_browser.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/session_browser.py apps/backend/core/services/desktop_execution.py apps/backend/core/services/mac_cdp.py apps/backend/core/services/mac_browser.py apps/backend/tests/unit/services/test_desktop_execution.py apps/backend/tests/unit/services/test_mac_cdp.py apps/backend/tests/unit/services/test_mac_browser.py
git commit -m "feat(sessions): a shared session's browser is a run of its own person"
```

### Task 4: Backend — offer → lease → sync → open → hold → release

**Files:**
- Modify: `apps/backend/core/services/session_browser.py`
- Modify: `apps/backend/core/services/local_sessions.py` (`LocalSession` fields near line 285; new `browser_open` after `screen_available` at line 689; `_session_public` near line 792; `register_session`'s kept keys at lines 998-1006)
- Modify: `apps/backend/routers/gateway_relay.py:266` (wire `on_offer`)
- Test: `apps/backend/tests/unit/services/test_session_browser.py` (new), `apps/backend/tests/unit/services/test_local_session_relay.py`

**Interfaces:**
- Consumes: Task 2 (`cdp_accept`, `cdp_close(..., message)`, `on_offer`, `.local`), Task 3 (`auth_for`, `session_of`, `acquire`'s `profileId`), Task 1 (RPC `browser.state` with a door).
- Produces: `session_browser.NOT_SHARED`, `MESSAGES`, `start(owner, user, device, node, channel, session_id, mac_browser_on)`, `async serve(...)`, `async shared_row(owner, user, device, session_id) -> dict`, `_mark_open(owner, human, session_id, where, *, announce)`, `_serve_mac`, `_hold_mac`, `_release`, `_sync`; `local_sessions.browser_open(row, now=None) -> "mac" | "cloud" | None`; `LocalSession.browser`.

- [ ] **Step 1: Write the failing tests**

Create `apps/backend/tests/unit/services/test_session_browser.py`:

```python
"""A shared session's browser (spec 2026-10-11): offer -> lease -> sync -> open -> hold -> release."""

import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

from core.services import mac_browser, node_broker, session_browser
from core.services.desktop_execution import DesktopError

OWNER, MEMBER, DEVICE = "org_1", "user_1", "mac1"
SID = "11111111-1111-4111-8111-111111111111"
RUN = f"session-{SID}"
UPSTREAM = f"mac:{DEVICE}:{RUN}"
CHANNEL = "c" * 32
DOOR = "ws://10.0.3.7:8000/api/v1/internal/mac-cdp/T1"


def _answer(sync=None):
    return {"deviceId": DEVICE, "generation": "g1", "cdpUrl": DOOR, "deviceName": None,
            "humanInputAt": 0, "profileId": "prof1", "sync": sync}


class Env:
    def __init__(self):
        self.row = {"ownerId": OWNER, "sessionId": SID, "humanUserId": MEMBER, "deviceId": DEVICE}
        self.acquired: list | Exception = [_answer({"syncId": "L1", "steps": [{"action": "load"}]})]
        self.released = {"sync": {"syncId": "L2", "steps": [{"action": "save"}]}, "cdpUrl": DOOR, "generation": "g1"}
        self.calls = []


@pytest.fixture
def env(monkeypatch):
    env = Env()
    monkeypatch.setattr(session_browser, "RENEW_SECONDS", 0.05)
    monkeypatch.setattr(session_browser, "POLL_S", 0)

    async def get_session(owner, sid):
        return copy.deepcopy(env.row) if (owner, sid) == (OWNER, SID) and env.row else None

    async def acquire(auth, device, *, budget_s=25):
        env.calls.append(("acquire", auth["runId"], device))
        if isinstance(env.acquired, Exception):
            raise env.acquired
        return env.acquired.pop(0) if env.acquired else _answer()

    async def release(auth, device, generation):
        env.calls.append(("release", generation))
        return env.released

    async def synced(auth, device, sync_id, ok):
        env.calls.append(("synced", sync_id, ok))

    async def rpc(owner, method, params=None, *, timeout_s=30.0):
        env.calls.append(("rpc", method, params["upstream"], [s["action"] for s in params["steps"]]))
        return {"ok": True}

    async def mark(owner, human, sid, where, *, announce):
        env.calls.append(("open", where))

    monkeypatch.setattr(session_browser.repo, "get_session", get_session)
    monkeypatch.setattr(session_browser.local_sessions, "_enrolled", AsyncMock(return_value={"enabled": True}))
    monkeypatch.setattr(mac_browser, "acquire", acquire)
    monkeypatch.setattr(mac_browser, "release", release)
    monkeypatch.setattr(mac_browser, "synced", synced)
    monkeypatch.setattr(session_browser.container_rpc, "call", rpc)
    monkeypatch.setattr(session_browser, "_mark_open", mark)
    return env


def _offered(commands=("browser.cdp.v1", "browser.cast.v1")):
    node = node_broker.NodeSession(DEVICE, set(commands))
    node.on_offer = lambda *args: None
    node.resolve({"type": "event", "event": "browser.cdp.offer",
                  "payload": {"channel": CHANNEL, "sessionId": SID, "macBrowser": True}})
    return node, node.cdp[CHANNEL]


def _out(node):
    frames = []
    while not node.cdp_out.empty():
        frames.append(node.cdp_out.get_nowait())
    return frames


async def _play_mac(node):
    """The Mac's half: answer the open with opened; returns what the backend sent."""
    for _ in range(200):
        frames = _out(node)
        if frames:
            for frame in frames:
                if frame["event"] == "browser.cdp.open":
                    node.resolve({"type": "event", "event": "browser.cdp.opened",
                                  "payload": {"channel": frame["payload"]["channel"]}})
            return frames
        await asyncio.sleep(0.01)
    return []


def _closed_by_mac(node):
    node.resolve({"type": "event", "event": "browser.cdp.close", "payload": {"channel": CHANNEL, "code": "CLOSED"}})


async def test_a_shared_session_gets_the_mac_with_the_workspace_sign_ins_then_saves_on_close(env):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    assert await _play_mac(node) == [{"type": "event", "event": "browser.cdp.open",
                                      "payload": {"channel": CHANNEL, "nodeId": DEVICE, "profileId": "prof1"}}]
    await asyncio.sleep(0.12)  # past two renewals
    assert env.calls[:4] == [("acquire", RUN, DEVICE), ("rpc", "browser.state", UPSTREAM, ["load"]),
                             ("synced", "L1", True), ("open", "mac")]
    assert channel.local, "its CDP stays on the Mac"
    assert env.calls.count(("acquire", RUN, DEVICE)) >= 2, "renewed while open"
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)
    assert env.calls[-4:] == [("open", None), ("release", "g1"), ("rpc", "browser.state", UPSTREAM, ["save"]),
                              ("synced", "L2", True)]


async def test_an_unshared_session_is_refused_with_not_shared(env):
    env.row["unshared"] = True
    node, channel = _offered()
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True)
    assert _out(node) == [{"type": "event", "event": "browser.cdp.close", "payload": {
        "channel": CHANNEL, "code": "not_shared", "message": session_browser.NOT_SHARED}}]
    assert env.calls == []


@pytest.mark.parametrize("change", [{"humanUserId": "user_2"}, {"deviceId": "mac2"}, {"endedAt": 1}, {"removedAt": 1}])
async def test_a_session_that_is_not_this_macs_running_shared_one_is_not_shared(env, change):
    env.row.update(change)
    node, channel = _offered()
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True)
    assert _out(node)[-1]["payload"]["code"] == "not_shared"


async def test_sharing_switched_off_for_the_mac_is_not_shared(env, monkeypatch):
    from core.services.messaging import ApiProblem

    monkeypatch.setattr(session_browser.local_sessions, "_enrolled",
                        AsyncMock(side_effect=ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session sharing is disabled.")))
    node, channel = _offered()
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True)
    assert _out(node)[-1]["payload"]["code"] == "not_shared"


async def test_another_workspaces_profile_is_refused_with_its_message(env):
    held = "This Mac's agent browser holds the sign-ins of the Acme workspace. Unpair it from Acme first, or use the cloud browser."
    env.acquired = DesktopError("browser_profile_held_by_another_workspace", 409, held)
    node, channel = _offered()
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True)
    assert _out(node) == [{"type": "event", "event": "browser.cdp.close", "payload": {
        "channel": CHANNEL, "code": "browser_profile_held_by_another_workspace", "message": held}}]
    assert not any(call[0] == "release" for call in env.calls), "nothing was leased"


async def test_browser_off_is_refused(env):
    node, channel = _offered()
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False)
    assert _out(node)[-1]["payload"]["code"] == "browser_off"


async def test_unsharing_while_open_closes_the_channel_and_still_releases(env):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    await _play_mac(node)
    env.row["unshared"] = True
    await asyncio.wait_for(served, 2)
    assert {"type": "event", "event": "browser.cdp.close", "payload": {
        "channel": CHANNEL, "code": "not_shared", "message": session_browser.NOT_SHARED}} in _out(node)
    assert ("release", "g1") in env.calls


async def test_a_starting_mac_is_polled_inside_the_offer_budget(env, monkeypatch):
    env.acquired = [mac_browser.STARTING, mac_browser.STARTING, _answer()]
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    assert (await _play_mac(node))[0]["event"] == "browser.cdp.open"
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)
    assert env.calls[:3] == [("acquire", RUN, DEVICE)] * 3
```

Append to `apps/backend/tests/unit/services/test_local_session_relay.py`:

```python
@pytest.mark.asyncio
async def test_an_open_session_browser_shows_until_its_lease_and_survives_reregistration(transport):
    sid = await start(transport)
    table = messaging_repo._table()
    table.update_item(Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
                      UpdateExpression="SET browserOpen = :b, browserPlace = :p",
                      ExpressionAttributeValues={":b": {"where": "mac", "until": int(time.time()) + 60}, ":p": "cloud"})
    row = await repo.get_session(OWNER, sid)
    assert service._session_public(row, ALICE.user_id).browser == "mac"
    await relay(transport).handle_message("conn-1", registration())
    row = await repo.get_session(OWNER, sid)
    assert row["browserOpen"]["where"] == "mac" and row["browserPlace"] == "cloud"
    assert service.browser_open(row, now=int(time.time()) + 61) is None
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_browser.py tests/unit/services/test_local_session_relay.py -v -k "session_browser or browser"`
Expected: FAIL (`AttributeError: module 'core.services.session_browser' has no attribute 'serve'`).

- [ ] **Step 3: Implement**

`local_sessions.py`:

In `class LocalSession`, after `shared: bool = True`:

```python
    # Where this running session's browser connection is open now (spec 2026-10-11): the Browser
    # tab beside its terminal shows while it is set. None: no open connection.
    browser: Literal["mac", "cloud"] | None = None
```

After `screen_available` (line 689):

```python
def browser_open(row: dict, now: int | None = None) -> Literal["mac", "cloud"] | None:
    """Where the session's browser connection is open, while session_browser keeps renewing it."""
    opened = row.get("browserOpen") or {}
    if int(opened.get("until") or 0) <= (int(time.time()) if now is None else now):
        return None
    return opened.get("where") if opened.get("where") in ("mac", "cloud") else None
```

In `_session_public`, right after `live = public["status"] not in ("ended", "offline")` (line 792):

```python
    public["browser"] = browser_open(row) if live else None
```

In `register_session`, add the two keys to the kept tuple (lines 998-1006):

```python
                for key in (
                    "workLink",
                    "workLinkRevision",
                    "unshared",
                    "pickupAllowed",
                    "pickupSnapshot",
                    "pickupEpoch",
                    "pickedUpFrom",
                    "displayTitle",
                    # The session's open browser connection and its chosen place (session_browser).
                    "browserOpen",
                    "browserPlace",
                )
```

`session_browser.py` — replace the module with the ids from Task 3 plus:

```python
"""A shared Claude Code or Codex session's browser (spec docs/superpowers/specs/2026-10-11-session-browser-design.md).

The Mac app identifies a CDP connection on its loopback port as one shared session and offers it
(`browser.cdp.offer`, node_broker). This runs on the task holding that Mac's socket. The session
is a run, `session-<sessionId>`, of its own person: it takes a `mac_browser` lease (first owner
wins; the first lease in loads the workspace sign-ins, the last one out saves them), the runtime
runs those sync steps over the ticketed Mac door (`browser.state` with a door, spike 3), and then
the Mac opens the channel on its own agent Chrome for the session's client. The lease is renewed
while the channel is open; the Mac closes it 300 s after the session's last command, or when the
session ends or stops being shared. Never log a ticket, a URL or a frame: owner ids and exception
class names only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time

from botocore.exceptions import ClientError

from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import container_rpc, local_sessions, mac_browser
from core.services.desktop_execution import DesktopError
from core.services.messaging import ApiProblem
from core.services.node_broker import CDP_COMMAND, CdpClosed, NodeCapacity

logger = logging.getLogger(__name__)

RUN_PREFIX = "session-"
AGENT_ID = "session"
NOT_SHARED = "Share this session to use the Lightbulb browser."
MESSAGES = {"not_shared": NOT_SHARED}
OFFER_BUDGET_S = 60  # the Mac waits 150 s for the answer
POLL_S = 2
RENEW_SECONDS = 120  # < mac_browser.LEASE_SECONDS
OPEN_SECONDS = 2 * RENEW_SECONDS
SYNC_TIMEOUT_S = 150  # the bridge caps one Mac sync at 120 s (MAC_SYNC_BUDGET_S)
_SID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_clock = time.time
_tasks: set[asyncio.Task] = set()


# run_id, session_of, auth_for: unchanged from Task 3


def _err(code: str, status: int = 409) -> DesktopError:
    return DesktopError(code, status, MESSAGES.get(code) or mac_browser.MESSAGES.get(code, mac_browser.GENERIC_MESSAGE))


async def shared_row(owner: str, user: str, device: str, session_id: str) -> dict:
    """The session's row while it is this Mac's, its person's, running and shared; else not_shared."""
    row = await repo.get_session(owner, session_id)
    if (
        row is None
        or row.get("humanUserId") != user
        or row.get("deviceId") != device
        or row.get("unshared")
        or row.get("endedAt") is not None
        or row.get("removedAt") is not None
    ):
        raise _err("not_shared", 403)
    try:
        await local_sessions._enrolled(owner, user, device)  # sharing still on for this Mac
    except ApiProblem:
        raise _err("not_shared", 403) from None
    return row


async def _mark_open(owner: str, human: str, session_id: str, where: str | None, *, announce: bool) -> None:
    """`browserOpen` on the session row (local_sessions.browser_open): the Browser tab shows while it is live."""
    key = {"pk": repo.session_pk(owner, session_id), "sk": "META"}
    if where:
        update = {
            "UpdateExpression": "SET browserOpen = :b",
            "ExpressionAttributeValues": {":b": {"where": where, "until": int(_clock()) + OPEN_SECONDS}},
        }
    else:
        update = {"UpdateExpression": "REMOVE browserOpen"}
    try:
        await store.run_in_thread(
            store._table().update_item, Key=key, ConditionExpression="attribute_exists(pk)", **update
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return
    if announce:
        from core.services import local_session_relay  # it imports local_sessions

        await local_session_relay.publish_resources_changed(owner, human)


async def _sync(auth: dict, device: str, answer: dict) -> None:
    """Run a claimed sign-in sync in the runtime over the door `answer` carries, then report it.
    Never raises: a failed sync is reported failed (mac_browser defers a load, keeps a save's base)."""
    sync = answer["sync"]
    ok = False
    if answer.get("cdpUrl"):
        try:
            result = await container_rpc.call(
                auth["ownerId"],
                "browser.state",
                {
                    "doorUrl": answer["cdpUrl"],
                    "generation": answer["generation"],
                    "upstream": f"mac:{device}:{auth['runId']}",
                    "steps": sync["steps"],
                },
                timeout_s=SYNC_TIMEOUT_S,
            )
            ok = isinstance(result, dict) and result.get("ok") is True
        except Exception as exc:  # noqa: BLE001 -- the class only
            logger.warning("session browser sync failed owner=%s error=%s", auth["ownerId"], type(exc).__name__)
    with contextlib.suppress(Exception):
        await mac_browser.synced(auth, device, str(sync["syncId"]), ok)


async def _acquire(auth: dict, device: str, deadline: float) -> dict:
    """mac_browser.acquire until it stops answering STARTING (a cold Chrome, another bridge's sync)."""
    while True:
        budget = max(1.0, min(mac_browser.ACQUIRE_BUDGET_S, deadline - _clock()))
        answer = await mac_browser.acquire(auth, device, budget_s=budget)
        if answer != mac_browser.STARTING:
            return answer
        if _clock() + POLL_S >= deadline:
            raise _err("starting")
        await asyncio.sleep(POLL_S)


async def _release(auth: dict, device: str, generation: str) -> None:
    """Drop the lease; the last one out saves the Mac's sign-ins. Never raises."""
    try:
        answer = await mac_browser.release(auth, device, generation)
        if answer.get("sync"):
            await _sync(auth, device, answer)
    except Exception as exc:  # noqa: BLE001
        logger.warning("session browser release failed owner=%s error=%s", auth["ownerId"], type(exc).__name__)


async def _hold_mac(auth: dict, device: str, node, channel) -> None:
    """While the Mac has the channel open: every RENEW_SECONDS re-check the session is shared, renew
    the lease (running any sync it hands back) and the `browserOpen` mark. DesktopError ends it."""
    owner, user, sid = auth["ownerId"], auth["memberId"], session_of(auth["runId"])
    while True:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(channel.closed.wait(), RENEW_SECONDS)
        if channel.closed.is_set():
            return
        await shared_row(owner, user, device, sid)
        answer = await mac_browser.acquire(auth, device)
        if isinstance(answer, dict) and answer.get("sync"):
            await _sync(auth, device, answer)
        await _mark_open(owner, user, sid, "mac", announce=False)


async def _serve_mac(auth: dict, device: str, node, channel) -> None:
    owner, user, sid = auth["ownerId"], auth["memberId"], session_of(auth["runId"])
    answer = await _acquire(auth, device, _clock() + OFFER_BUDGET_S)
    try:
        if answer.get("sync"):
            await _sync(auth, device, answer)
        channel.local = True  # set before the open: none of its CDP frames is ours
        await node.cdp_accept(channel, {"profileId": answer["profileId"]})
        await _mark_open(owner, user, sid, "mac", announce=True)
        try:
            await _hold_mac(auth, device, node, channel)
        finally:
            await _mark_open(owner, user, sid, None, announce=True)
    finally:
        await _release(auth, device, answer["generation"])


async def serve(owner: str, user: str, device: str, node, channel, session_id: str, mac_browser_on: bool) -> None:
    """One offered channel, from the offer to its close. Never raises; a refusal reaches the
    session's own CDP client as the close's message."""
    auth = auth_for(owner, user, session_id)
    try:
        await shared_row(owner, user, device, session_id)
        if not mac_browser_on or CDP_COMMAND not in node.commands:
            raise _err("browser_off")
        await _serve_mac(auth, device, node, channel)
    except DesktopError as exc:
        exc = mac_browser._with_message(exc)
        node.cdp_close(channel, exc.code, exc.message)
    except (CdpClosed, NodeCapacity):
        node.cdp_close(channel)
    except Exception as exc:  # noqa: BLE001
        logger.warning("session browser failed owner=%s error=%s", owner, type(exc).__name__)
        node.cdp_close(channel)


def start(owner: str, user: str, device: str, node, channel, session_id: str, mac_browser_on: bool) -> None:
    """node_broker's `on_offer`: serve it in the background (the relay reader never waits)."""
    task = asyncio.create_task(serve(owner, user, device, node, channel, session_id, mac_browser_on))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
```

`routers/gateway_relay.py` — after `session = node_broker.NodeSession(device_id, allowed)` (line 266):

```python
    # A shared Claude Code or Codex session on this Mac asks for a browser (spec 2026-10-11).
    session.on_offer = functools.partial(session_browser.start, identity.owner_id, identity.user_id, device_id, session)
```

and add `import functools` and `from core.services import session_browser` to its imports.

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_browser.py tests/unit/services/test_local_session_relay.py tests/unit/services/test_local_sessions.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/session_browser.py apps/backend/core/services/local_sessions.py apps/backend/routers/gateway_relay.py apps/backend/tests/unit/services/test_session_browser.py apps/backend/tests/unit/services/test_local_session_relay.py
git commit -m "feat(sessions): a shared session's offered channel leases the Mac browser with the workspace sign-ins"
```

### Task 5: Desktop — session channels in `agent_chrome`

**Files:**
- Modify: `apps/desktop/src-tauri/src/agent_chrome.rs` (`filter` lines 405-460; `Entry` lines 577-594; `open` lines 727-754; `send_close` line 693; `human_input_events` lines 966-979; `human_input_now` lines 998-1012; `pump` lines 1021-1069; `route_backend` lines 929-962; tests module `fake` line 1575)
- Test: same file, `mod tests`

**Interfaces:**
- Produces: `pub fn filter_for(raw: &str, downloads: &Path, session: bool) -> (Filtered, Option<String>, Option<String>)`; `Channels::open_local(&self, id: &str, profile_id: &str, to_client: mpsc::Sender<String>) -> Result<mpsc::Sender<String>, &'static str>`; `Channels::end_local(&self, id: &str) -> bool`; `Channels::out(&self) -> EventSender`; `pub(crate) fn send_close`; `#[cfg(test)] pub(crate) fn test_channels() -> (Channels, mpsc::Receiver<Message>)`. Used by Tasks 7, 17.

- [ ] **Step 1: Write the failing tests**

In `agent_chrome.rs` `mod tests`, make `fake` reachable from the module (`pub(super) fn fake`) and add:

```rust
    fn local(ch: &Channels, id: &str) -> (Result<mpsc::Sender<String>, &'static str>, mpsc::Receiver<String>) {
        let (tx, rx) = mpsc::channel(8);
        (ch.open_local(id, "p", tx), rx)
    }

    #[test]
    fn a_session_channel_opens_new_tabs_in_a_background_window() {
        let raw = call("Target.createTarget", json!({"url": "https://a.com"}));
        let Filtered::Pass(out) = filter_for(&raw, &dl(), true).0 else { panic!() };
        let v: Value = serde_json::from_str(&out).unwrap();
        assert_eq!((v["params"]["background"].clone(), v["params"]["newWindow"].clone()), (json!(true), json!(true)));
        // chrome-devtools-mcp resize_page: answered, never run (already in SWALLOWED).
        assert!(matches!(filter_for(&call("Browser.setContentsSize", json!({})), &dl(), true).0, Filtered::Answer(_)));
    }

    #[tokio::test]
    async fn a_fifth_channel_local_or_not_is_capacity() {
        let (ch, _, _rx) = fake(Duration::from_secs(600));
        for id in ["a", "b", "c"] {
            open(&ch, id);
        }
        let (first, _c1) = local(&ch, "s1");
        assert!(first.is_ok());
        let (fifth, _c2) = local(&ch, "s2");
        assert_eq!(fifth.err(), Some("CAPACITY"));
    }

    #[tokio::test]
    async fn a_session_channel_never_gets_the_runs_human_input_signal() {
        let (ch, _, mut rx) = fake(Duration::from_secs(600));
        open(&ch, "a");
        let (_tx, _client) = local(&ch, "s1");
        let events = ch.human_input_events(Instant::now(), 1);
        assert_eq!(events.len(), 1, "only the run's channel");
        assert!(events[0].contains("\"channel\":\"a\""));
        ch.human_input_now("s1");
        assert!(next_frame(&mut rx).await.is_none());
    }

    #[tokio::test]
    async fn chromes_side_of_a_session_channel_goes_to_its_client_not_the_backend() {
        let (ch, _, mut rx) = fake(Duration::from_secs(600));
        let (client_tx, mut client) = mpsc::channel(8);
        ch.deliver("s1", Some(&client_tx), "{\"id\":1,\"result\":{}}".into()).await.unwrap();
        assert_eq!(client.recv().await.as_deref(), Some("{\"id\":1,\"result\":{}}"));
        assert!(next_frame(&mut rx).await.is_none(), "nothing up the node socket");
        ch.deliver("r1", None, "{}".into()).await.unwrap();
        assert_eq!(next_frame(&mut rx).await.unwrap()["event"], "browser.cdp.frame");
    }

    #[tokio::test]
    async fn ending_a_session_channel_tells_the_backend_once() {
        let (ch, _, mut rx) = fake(Duration::from_secs(600));
        let (_tx, _client) = local(&ch, "s1");
        assert!(ch.end_local("s1"));
        let v = next_frame(&mut rx).await.expect("a close");
        assert_eq!((v["event"].as_str(), v["payload"]["channel"].as_str(), v["payload"]["code"].as_str()),
            (Some("browser.cdp.close"), Some("s1"), Some("CLOSED")));
        assert!(!ch.end_local("s1"));
        assert!(next_frame(&mut rx).await.is_none());
    }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/desktop/src-tauri && cargo test agent_chrome::tests -- --nocapture`
Expected: FAIL to compile (`no function or associated item named open_local`, `filter_for`, `deliver`, `end_local`).

- [ ] **Step 3: Implement**

`filter` becomes a wrapper; the body moves to `filter_for` with one changed line:

```rust
pub fn filter(raw: &str, downloads: &Path) -> (Filtered, Option<String>, Option<String>) {
    filter_for(raw, downloads, false)
}

/// `session`: a shared Claude Code or Codex session's channel (session_browser). Its new tabs open in
/// a new background window, not a background tab: in a background tab requestAnimationFrame never
/// fires, so Playwright's click times out; a background window stays "visible" and the front app is
/// unchanged (spike 2026-10-11). Runs keep today's background tab.
pub fn filter_for(raw: &str, downloads: &Path, session: bool) -> (Filtered, Option<String>, Option<String>) {
    // ... the existing body of `filter`, with the createTarget branch now:
        } else if method == "Target.createTarget" && msg.get("params").is_some_and(Value::is_object) {
            // A background tab (a run) or a background window (a session): never in front of the person.
            msg["params"]["background"] = json!(true);
            msg["params"]["newWindow"] = json!(session);
            Filtered::Pass(msg.to_string())
        }
    // ...
}
```

`Entry` gets the sink and a constructor:

```rust
struct Entry {
    // ... existing fields ...
    /// A shared session's channel: Chrome's side goes to this sender (the session's own CDP client
    /// on this Mac's loopback port), never up the node socket.
    local: Option<mpsc::Sender<String>>,
}

impl Entry {
    fn new(tx: mpsc::Sender<String>, task: tokio::task::JoinHandle<()>, local: Option<mpsc::Sender<String>>) -> Self {
        Self { tx, task: Some(task), sessions: HashMap::new(), current: None, url: None,
            hidden_pending: HashSet::new(), hidden: HashSet::new(), local }
    }
}
```

In `open`, replace the struct literal with `Entry::new(tx, task, None)`.

Make `send_close` `pub(crate)` and add, after `open`:

```rust
    /// A shared Claude Code or Codex session's channel (session_browser): the same Chrome, filter and
    /// casts as a run's, but Chrome's side goes to `to_client`. Err(BROWSER_OFF | CAPACITY) at once.
    pub(crate) fn open_local(&self, id: &str, profile_id: &str, to_client: mpsc::Sender<String>) -> Result<mpsc::Sender<String>, &'static str> {
        if !(self.hooks.browser_on)() {
            return Err("BROWSER_OFF");
        }
        let mut inner = self.lock();
        if inner.channels.contains_key(id) || inner.channels.len() >= MAX_CHANNELS {
            return Err("CAPACITY");
        }
        let (tx, rx) = mpsc::channel(1024);
        if inner.channels.is_empty() {
            inner.awake = (self.hooks.stay_awake)();
        }
        self.hooks.epoch.fetch_add(1, Ordering::SeqCst);
        let task = tokio::spawn(self.clone().run(id.to_string(), profile_id.to_string(), rx));
        inner.channels.insert(id.to_string(), Entry::new(tx.clone(), task, Some(to_client)));
        Ok(tx)
    }

    /// The session's connection ended here: close its channel and tell the backend, which releases.
    pub(crate) fn end_local(&self, id: &str) -> bool {
        let open = self.remove(id);
        if open {
            self.send_close(id, "CLOSED");
        }
        open
    }

    pub(crate) fn out(&self) -> EventSender {
        self.out.clone()
    }
```

`human_input_events` — only run channels:

```rust
    fn human_input_events(&self, now: Instant, at: u64) -> Vec<String> {
        let mut inner = self.lock();
        let recent = |t: Option<Instant>| t.is_some_and(|t| now.saturating_duration_since(t) < AGENT_INPUT_WINDOW);
        // A session's client is not an agent run: it has nothing to yield.
        let ids: Vec<String> = inner.channels.iter().filter(|(_, e)| e.local.is_none()).map(|(id, _)| id.clone()).collect();
        if recent(inner.last_agent_input) || recent(inner.last_human_sent) || ids.is_empty() {
            return Vec::new();
        }
        inner.last_human_sent = Some(now);
        let data = json!({"method": "Lightbulb.humanInput", "params": {"at": at}}).to_string();
        ids.into_iter().map(|id| event_text("browser.cdp.frame", json!({"channel": id, "data": data}))).collect()
    }
```

`human_input_now` — the early return becomes:

```rust
            if recent || !inner.channels.get(channel).is_some_and(|e| e.local.is_none()) {
                return;
            }
```

`route_backend` — pick the filter by the channel's kind:

```rust
        let session = self.lock().channels.get(channel).is_some_and(|e| e.local.is_some());
        let (filtered, method, session_id) = filter_for(data, &self.hooks.downloads, session);
```

`pump` — read the sink once and send through `deliver`:

```rust
    async fn pump(&self, id: &str, profile_id: &str, rx: &mut mpsc::Receiver<String>) -> &'static str {
        let local = self.lock().channels.get(id).and_then(|e| e.local.clone());
        // ... unchanged until the select loop, which becomes:
        loop {
            tokio::select! {
                msg = from_chrome.next() => match msg {
                    Some(Ok(Message::Text(text))) => {
                        self.note_target_event(id, text.as_str());
                        if let Err(code) = self.deliver(id, local.as_ref(), text.to_string()).await { return code }
                    }
                    Some(Ok(_)) => {}
                    _ => return "CLOSED",
                },
                data = rx.recv() => match data {
                    None => return "CLOSED",
                    Some(data) => match self.route_backend(id, &data) {
                        Route::Chrome(text) => if to_chrome.send(Message::Text(text.into())).await.is_err() { return "CLOSED" },
                        Route::Backend(text) => if let Err(code) = self.deliver(id, local.as_ref(), text).await { return code },
                    },
                },
            }
        }
    }

    /// Chrome's text (or an answer the filter gave) to whoever drives the channel: a session's own
    /// client, else the backend as a `browser.cdp.frame`.
    async fn deliver(&self, id: &str, local: Option<&mpsc::Sender<String>>, text: String) -> Result<(), &'static str> {
        match local {
            Some(client) => client.send(text).await.map_err(|_| "CLOSED"),
            None => {
                let frame = envelope(id, &text)?;
                self.out.send_text(frame).await.map_err(|_| "CLOSED")
            }
        }
    }
```

At the end of the file (outside `mod tests`):

```rust
#[cfg(test)]
pub(crate) fn test_channels() -> (Channels, mpsc::Receiver<Message>) {
    let (channels, _, rx) = tests::fake(Duration::from_secs(600));
    (channels, rx)
}
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/desktop/src-tauri && cargo test agent_chrome`
Expected: PASS (all existing `agent_chrome` tests too).

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src/agent_chrome.rs
git commit -m "feat(desktop): agent Chrome channels a shared session drives from this Mac"
```

### Task 6: Desktop — which shared session is asking

**Files:**
- Create: `apps/desktop/src-tauri/src/session_browser.rs` (peer pid and ancestors only; Task 7 adds the server)
- Modify: `apps/desktop/src-tauri/src/lib.rs` (`mod session_browser;` beside `mod session_start;` at line 24)
- Modify: `apps/desktop/src-tauri/src/local_sessions/native_tools.rs:108-172` (`live_mirrors`, `session_of_ancestors`, `mirror_live`)
- Modify: `apps/desktop/src-tauri/src/local_sessions.rs` (two `pub(crate)` wrappers)
- Modify: `apps/desktop/src-tauri/src/local_sessions/enrollment.rs:494-500` (`session_scope`)
- Test: `session_browser.rs` and `native_tools.rs` `mod tests`

**Interfaces:**
- Produces: `session_browser::NOT_SHARED: &str`, `session_browser::peer_pid(server_port: u16, client_port: u16) -> Option<u32>`, `session_browser::ancestors(pid: u32) -> Vec<u32>`; `local_sessions::shared_session_for(scope: &Path, ancestors: &[u32]) -> Result<String>`; `local_sessions::session_still_shared(scope: &Path, session_id: &str) -> bool`; `local_sessions::enrollment::session_scope(app) -> Result<PathBuf>`. Used by Task 7.

- [ ] **Step 1: Write the failing tests**

Create `apps/desktop/src-tauri/src/session_browser.rs` with only its test module for now:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_os = "macos")]
    #[test]
    fn peer_pid_finds_this_processs_own_client() {
        // Pins the socket_fdinfo offsets on a real loopback socket: this process is both ends.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let server = listener.local_addr().unwrap().port();
        let client = std::net::TcpStream::connect(("127.0.0.1", server)).unwrap();
        let _accepted = listener.accept().unwrap();
        let port = client.local_addr().unwrap().port();
        assert_eq!(peer_pid(server, port), Some(std::process::id()));
        assert_eq!(peer_pid(server, 1), None, "nobody's client port");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn ancestors_walk_up_from_a_child_to_this_process() {
        let mut child = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let chain = ancestors(child.id());
        child.kill().unwrap();
        child.wait().unwrap();
        assert_eq!(chain[0], child.id());
        assert_eq!(chain[1], std::process::id());
        assert!(chain.len() <= 32 && !chain.contains(&1));
    }
}
```

In `native_tools.rs` `mod tests`:

```rust
    fn persisted(scope: &Path, key: &str, sid: &str, shared: bool) {
        let mut mirror = super::super::transport::tests::test_mirror("22222222-2222-4222-8222-222222222222", "gen-1");
        mirror.native.provider = "claude".into();
        mirror.session_id = Some(sid.into());
        mirror.shared = shared;
        mirror.persist(&scope.join(format!("{key}.json"))).unwrap();
    }

    #[test]
    fn a_browser_connection_finds_its_shared_session_among_its_ancestors() {
        use std::os::unix::fs::PermissionsExt;
        let scope = tempfile::tempdir().unwrap();
        fs::set_permissions(scope.path(), fs::Permissions::from_mode(0o700)).unwrap();
        // This test process is the session (test_mirror's process); a child stands in for the CDP client.
        let key = "b0b0feed".repeat(8); // its own: pending asks are process-wide
        persisted(scope.path(), &key, "session-b", true);
        let mut client = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let me = std::process::id();
        let found = session_of_ancestors(scope.path(), &[client.id(), me]);
        let alone = session_of_ancestors(scope.path(), &[client.id()]);
        let still = mirror_live(scope.path(), "session-b");
        persisted(scope.path(), &key, "session-b", false);
        let unshared = session_of_ancestors(scope.path(), &[client.id(), me]);
        let gone = mirror_live(scope.path(), "session-b");
        client.kill().unwrap();
        client.wait().unwrap();
        assert_eq!(found.unwrap(), "session-b");
        assert_eq!(alone.unwrap_err(), crate::session_browser::NOT_SHARED);
        assert_eq!(unshared.unwrap_err(), crate::session_browser::NOT_SHARED);
        assert!(still && !gone);
    }

    #[test]
    fn two_shared_sessions_in_one_process_are_refused_as_ambiguous() {
        use std::os::unix::fs::PermissionsExt;
        let scope = tempfile::tempdir().unwrap();
        fs::set_permissions(scope.path(), fs::Permissions::from_mode(0o700)).unwrap();
        persisted(scope.path(), &"c0c0feed".repeat(8), "session-c", true);
        persisted(scope.path(), &"d0d0feed".repeat(8), "session-d", true);
        let refused = session_of_ancestors(scope.path(), &[std::process::id()]).unwrap_err();
        assert!(refused.contains("cannot tell which"), "{refused}");
    }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/desktop/src-tauri && cargo test session_browser native_tools`
Expected: FAIL to compile (`cannot find function peer_pid`, `session_of_ancestors`, `mirror_live`).

- [ ] **Step 3: Implement**

`session_browser.rs`, above the tests:

```rust
//! The Lightbulb browser for shared Claude Code and Codex sessions (spec 2026-10-11).

/// What a session's CDP client reads when the Lightbulb browser refuses it.
pub(crate) const NOT_SHARED: &str = "Share this session to use the Lightbulb browser";

/// `struct socket_fdinfo` (<sys/proc_info.h>): libc has the call but not the struct. These offsets
/// are pinned by `peer_pid_finds_this_processs_own_client`.
const SOCKET_FDINFO_SIZE: usize = 792;
const PROC_PIDFDSOCKETINFO: i32 = 3;
const SOCKINFO_TCP: i32 = 2;
const SOI_KIND: usize = 256;
const TCP_FPORT: usize = 264;
const TCP_LPORT: usize = 268;

/// The pid that owns the client end of a loopback TCP connection to `server_port` from
/// `client_port` (spike 2026-10-11: libproc socket matching, ~0.3 ms median, no root). Looks at
/// this user's processes only; None when no process does (deny).
#[cfg(target_os = "macos")]
pub(crate) fn peer_pid(server_port: u16, client_port: u16) -> Option<u32> {
    let n = unsafe { libc::proc_listallpids(std::ptr::null_mut(), 0) };
    let mut pids = vec![0 as libc::pid_t; n.max(0) as usize + 64];
    let bytes = (pids.len() * std::mem::size_of::<libc::pid_t>()) as i32;
    let n = unsafe { libc::proc_listallpids(pids.as_mut_ptr().cast(), bytes) };
    pids.truncate(n.max(0) as usize);
    pids.into_iter()
        .filter(|pid| *pid > 1)
        .find(|&pid| crate::holder::identity::read(pid as u32).is_some() && owns_client(pid, server_port, client_port))
        .map(|pid| pid as u32)
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn peer_pid(_server_port: u16, _client_port: u16) -> Option<u32> {
    None
}

#[cfg(target_os = "macos")]
fn owns_client(pid: libc::pid_t, server_port: u16, client_port: u16) -> bool {
    let size = std::mem::size_of::<libc::proc_fdinfo>();
    let bytes = unsafe { libc::proc_pidinfo(pid, libc::PROC_PIDLISTFDS, 0, std::ptr::null_mut(), 0) };
    if bytes <= 0 {
        return false;
    }
    let mut fds = vec![unsafe { std::mem::zeroed::<libc::proc_fdinfo>() }; bytes as usize / size + 16];
    let bytes = unsafe { libc::proc_pidinfo(pid, libc::PROC_PIDLISTFDS, 0, fds.as_mut_ptr().cast(), (fds.len() * size) as i32) };
    fds.truncate(bytes.max(0) as usize / size);
    fds.iter().filter(|fd| fd.proc_fdtype == libc::PROX_FDTYPE_SOCKET as u32).any(|fd| {
        let mut info = [0u8; SOCKET_FDINFO_SIZE];
        let got = unsafe {
            libc::proc_pidfdinfo(pid, fd.proc_fd, PROC_PIDFDSOCKETINFO, info.as_mut_ptr().cast(), SOCKET_FDINFO_SIZE as i32)
        };
        let int = |at: usize| i32::from_ne_bytes(info[at..at + 4].try_into().unwrap_or([0; 4]));
        // insi_lport/insi_fport hold the port in network byte order in their low 16 bits.
        got == SOCKET_FDINFO_SIZE as i32
            && int(SOI_KIND) == SOCKINFO_TCP
            && u16::from_be(int(TCP_LPORT) as u16) == client_port
            && u16::from_be(int(TCP_FPORT) as u16) == server_port
    })
}

/// `pid` and its parents, closest first, this user's processes only, at most 32: the session's own
/// process is somewhere up there (npx -> node -> claude).
pub(crate) fn ancestors(pid: u32) -> Vec<u32> {
    let mut chain = Vec::new();
    let mut next = Some(pid);
    while let Some(pid) = next.filter(|p| *p > 1 && chain.len() < 32) {
        chain.push(pid);
        next = crate::holder::identity::read(pid).map(|identity| identity.parent);
    }
    chain
}
```

`lib.rs`: add `mod session_browser;` after `mod session_start;`.

`native_tools.rs` — split the mirror scan out of `resolve_source` (lines 117-139) and use it twice:

```rust
/// Every mirror of a running, shared, unrestricted session whose own process is still that process.
fn live_mirrors(scope: &Path) -> Result<Vec<Mirror>> {
    let mut live = Vec::new();
    for entry in fs::read_dir(scope).map_err(|_| "local session state unavailable")? {
        let path = entry.map_err(|_| "local session state unavailable")?.path();
        let Some(key) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        if path.extension().is_none_or(|e| e != "json") || key.len() != 64 || !key.bytes().all(|b| b.is_ascii_hexdigit()) {
            continue;
        }
        let Ok(mirror) = private_read::<Mirror>(&path) else {
            continue;
        };
        // (A withheld session is no source: it reads and sends nothing through its tools.)
        if mirror.ended_at.is_some()
            || super::transport::stopped(key, &mirror)
            || mirror.native.restriction.is_some()
            || process(mirror.native.process.pid).ok().as_ref() != Some(&mirror.native.process)
        {
            continue;
        }
        live.push(mirror);
    }
    Ok(live)
}
```

`resolve_source` keeps its first lines (`parent`, `thread`, `codex_host`) and loops `for mirror in live_mirrors(scope)? { let matches = ...; ... }` with the existing match and push unchanged.

```rust
const BROWSER_AMBIGUOUS: &str = "More than one shared session runs in this process (the Codex app, or one Codex \
    service for several conversations), so the Lightbulb browser cannot tell which one is asking";

/// The one shared session among a browser connection's peer process and its ancestors.
pub(super) fn session_of_ancestors(scope: &Path, ancestors: &[u32]) -> Result<String> {
    let mut found: Vec<String> = live_mirrors(scope)?
        .into_iter()
        .filter(|m| ancestors.contains(&m.native.process.pid))
        .filter_map(|m| m.session_id)
        .collect();
    found.sort();
    found.dedup();
    match found.len() {
        1 => Ok(found.remove(0)),
        0 => Err(crate::session_browser::NOT_SHARED.into()),
        _ => Err(BROWSER_AMBIGUOUS.into()),
    }
}

/// The session still runs and is shared (a browser connection's 30 s check).
pub(super) fn mirror_live(scope: &Path, session_id: &str) -> bool {
    live_mirrors(scope).is_ok_and(|live| live.iter().any(|m| m.session_id.as_deref() == Some(session_id)))
}
```

`local_sessions.rs` (beside `pub use native_mcp::serve_mcp;`):

```rust
/// The shared session a browser connection comes from (session_browser), or why not.
pub(crate) fn shared_session_for(scope: &Path, ancestors: &[u32]) -> Result<String> {
    native_tools::session_of_ancestors(scope, ancestors)
}

pub(crate) fn session_still_shared(scope: &Path, session_id: &str) -> bool {
    native_tools::mirror_live(scope, session_id)
}
```

`enrollment.rs`, after `launch_scope`:

```rust
/// The dir of this Mac's shared-session mirrors (native_tools reads it), while sharing is on.
pub(crate) fn session_scope(app: &tauri::AppHandle) -> Result<PathBuf> {
    Ok(state_dir(app)?.join(launch_scope(app)?))
}
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/desktop/src-tauri && cargo test session_browser native_tools`
Expected: PASS (including the existing `a_plain_sessions_own_mcp_call_resolves_it_as_the_source`).

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src/session_browser.rs apps/desktop/src-tauri/src/lib.rs apps/desktop/src-tauri/src/local_sessions.rs apps/desktop/src-tauri/src/local_sessions/native_tools.rs apps/desktop/src-tauri/src/local_sessions/enrollment.rs
git commit -m "feat(desktop): name the shared session behind a loopback browser connection"
```

### Task 7: Desktop — the loopback browser port

**Files:**
- Modify: `apps/desktop/src-tauri/src/session_browser.rs` (server, offers, `on_event`)
- Modify: `apps/desktop/src-tauri/src/node_invoke.rs:89-93` (route CDP events through `session_browser::on_event`)
- Modify: `apps/desktop/src-tauri/src/lib.rs` (setup, after the uploads cleanup near line 1983: start `serve`)
- Test: `session_browser.rs` `mod tests`

**Interfaces:**
- Consumes: Task 5 (`open_local`, `end_local`, `out`, `send_close`, `test_channels`), Task 6 (`peer_pid`, `ancestors`, `NOT_SHARED`, `shared_session_for`, `session_still_shared`, `session_scope`), `crate::with_agent_chrome_channels`.
- Produces: `session_browser::PORT: u16 = 39222`, `IDLE_CLOSE`, `serve(app)`, `on_event(&Channels, &str, &Value) -> bool`, `http_response(path) -> (u16, String)`; node event `browser.cdp.offer {channel, sessionId, macBrowser}`. Used by Tasks 8, 17, 20.

- [ ] **Step 1: Write the failing tests**

Add to `session_browser.rs` `mod tests`:

```rust
    use futures_util::{SinkExt, StreamExt};
    use serde_json::{json, Value};
    use tokio_tungstenite::tungstenite::Message;

    #[test]
    fn the_http_surface_is_what_cdp_clients_read_first() {
        let (status, body) = http_response("/json/version");
        let v: Value = serde_json::from_str(&body).unwrap();
        assert_eq!((status, v["webSocketDebuggerUrl"].as_str()), (200, Some("ws://127.0.0.1:39222/devtools/browser/lightbulb")));
        assert_eq!(http_response("/json/version/"), (status, body), "with a trailing slash too");
        assert_eq!(http_response("/json/list"), (200, "[]".into()));
        assert_eq!(http_response("/json/new?about:blank").0, 404);
    }

    #[test]
    fn a_web_pages_handshake_is_refused() {
        use tokio_tungstenite::tungstenite::handshake::server::{Request, Response};
        let page = Request::builder().uri("/devtools/browser/lightbulb").header("origin", "https://evil.example").body(()).unwrap();
        assert_eq!(no_browser(&page, Response::new(())).unwrap_err().status(), 403);
        let tool = Request::builder().uri("/devtools/browser/lightbulb").body(()).unwrap();
        assert!(no_browser(&tool, Response::new(())).is_ok());
    }

    #[tokio::test]
    async fn a_refused_client_reads_why_in_cdp_error_form() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("ws://{}{WS_PATH}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let ws = tokio_tungstenite::accept_async(stream).await.unwrap();
            refuse(ws, vec![r#"{"id":1,"method":"Target.getBrowserContexts"}"#.into()], NOT_SHARED).await;
        });
        let (mut client, _) = tokio_tungstenite::connect_async(url).await.unwrap();
        client.send(Message::Text(r#"{"id":2,"method":"Target.setDiscoverTargets","params":{"discover":true}}"#.into())).await.unwrap();
        let mut answers = Vec::new();
        while let Some(Ok(Message::Text(text))) = client.next().await {
            answers.push(serde_json::from_str::<Value>(&text).unwrap());
        }
        server.await.unwrap();
        assert_eq!(answers, [json!({"id": 1, "error": {"code": -32000, "message": NOT_SHARED}}),
            json!({"id": 2, "error": {"code": -32000, "message": NOT_SHARED}})]);
    }

    #[tokio::test]
    async fn the_backends_answer_opens_the_chrome_channel_or_refuses_with_its_message() {
        let (channels, _rx) = crate::agent_chrome::test_channels();
        let (to_client, _client) = tokio::sync::mpsc::channel(8);
        let (decide, decided) = tokio::sync::oneshot::channel();
        lock(&PENDING).insert("o1".into(), Pending { to_client, decide });
        assert!(on_event(&channels, "browser.cdp.open", &json!({"channel": "o1", "profileId": "p"})));
        assert!(matches!(decided.await, Ok(Decision::Chrome(_))));
        let (to_client, _client) = tokio::sync::mpsc::channel(8);
        let (decide, decided) = tokio::sync::oneshot::channel();
        lock(&PENDING).insert("o2".into(), Pending { to_client, decide });
        let message = "Share this session to use the Lightbulb browser.\u{7}";
        assert!(on_event(&channels, "browser.cdp.close", &json!({"channel": "o2", "code": "not_shared", "message": message})));
        let Ok(Decision::Refused(shown)) = decided.await else { panic!("refused") };
        assert_eq!(shown, "Share this session to use the Lightbulb browser.");
        assert!(!on_event(&channels, "browser.cdp.frame", &json!({"channel": "o1", "data": "{}"})), "Chrome frames are Channels' own");
        assert!(!on_event(&channels, "browser.cdp.open", &json!({"channel": "run-1"})), "a run's channel");
    }

    #[tokio::test]
    async fn a_full_mac_refuses_the_session_and_tells_the_backend() {
        let (channels, mut rx) = crate::agent_chrome::test_channels();
        for id in ["a", "b", "c", "d"] {
            channels.on_event("browser.cdp.open", &json!({"channel": id, "profileId": "p"}));
        }
        let (to_client, _client) = tokio::sync::mpsc::channel(8);
        let (decide, decided) = tokio::sync::oneshot::channel();
        lock(&PENDING).insert("o3".into(), Pending { to_client, decide });
        assert!(on_event(&channels, "browser.cdp.open", &json!({"channel": "o3", "profileId": "p"})));
        let Ok(Decision::Refused(shown)) = decided.await else { panic!("refused") };
        assert!(shown.contains("busy"));
        let Some(Message::Text(text)) = tokio::time::timeout(std::time::Duration::from_secs(1), rx.recv()).await.unwrap() else { panic!() };
        let v: Value = serde_json::from_str(&text).unwrap();
        assert_eq!((v["payload"]["channel"].as_str(), v["payload"]["code"].as_str()), (Some("o3"), Some("CAPACITY")));
    }

    #[tokio::test]
    async fn a_taken_port_is_reported_and_nothing_is_served() {
        let taken = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = taken.local_addr().unwrap().port();
        let reported = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let sink = reported.clone();
        assert!(bind_or_report(port, move |text| sink.lock().unwrap().push(text)).await.is_none());
        assert!(reported.lock().unwrap()[0].contains(&port.to_string()));
    }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/desktop/src-tauri && cargo test session_browser`
Expected: FAIL to compile (`http_response`, `no_browser`, `refuse`, `on_event`, `bind_or_report` not found).

- [ ] **Step 3: Implement**

Replace the module doc and add, in `session_browser.rs` (above the peer-pid code from Task 6):

```rust
//! The Lightbulb browser for shared Claude Code and Codex sessions (spec 2026-10-11).
//!
//! A fixed loopback port speaks the slice of Chrome's DevTools surface chrome-devtools-mcp and
//! Playwright MCP use: `GET /json/version` (with or without a trailing slash), `/json/list`, and the
//! browser WebSocket. A connection is identified by its peer process, walked up to one shared
//! session (anything else is refused with `NOT_SHARED`), and offered to the backend
//! (`browser.cdp.offer`). The backend answers `browser.cdp.open` (an `agent_chrome` channel whose
//! Chrome side is this connection) or `browser.cdp.close` with the message the client is shown.
//! A connection closes 300 s after its last command, when its session ends or stops being shared,
//! and when Browser is switched off. The person's everyday Chrome is never involved.

use crate::agent_chrome::Channels;
use crate::node_client::EventSender;
use futures_util::stream::{SplitSink, SplitStream};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Arc, LazyLock, Mutex};
use std::time::Duration;
use tauri::Manager;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, oneshot, Notify};
use tokio_tungstenite::tungstenite::handshake::server::{ErrorResponse, Request, Response};
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::Message;

pub(crate) const PORT: u16 = 39222;
pub(crate) const IDLE_CLOSE: Duration = Duration::from_secs(300);
/// The backend's answer: it may wake the runtime (~55 s cold) and run a sign-in sync first.
const OFFER_WAIT: Duration = Duration::from_secs(150);
/// Commands a client sends before the answer, held (and answered with the refusal if refused).
const MAX_EARLY: usize = 256;
const LIVE_CHECK: Duration = Duration::from_secs(30);
const WS_PATH: &str = "/devtools/browser/lightbulb";
const NOT_CONNECTED: &str = "Lightbulb on this Mac is not connected to its workspace yet";

type Ws = tokio_tungstenite::WebSocketStream<TcpStream>;

pub(crate) enum Decision {
    Chrome(mpsc::Sender<String>),
    Refused(String),
}

struct Pending {
    to_client: mpsc::Sender<String>,
    decide: oneshot::Sender<Decision>,
}

/// Offers awaiting the backend's answer, by channel.
static PENDING: LazyLock<Mutex<HashMap<String, Pending>>> = LazyLock::new(Default::default);
/// Open connections: channel -> (session, its stop).
static OPEN: LazyLock<Mutex<HashMap<String, (String, Arc<Notify>)>>> = LazyLock::new(Default::default);

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn log(msg: &str) {
    crate::log(&format!("[session-browser] {msg}"));
}

pub(crate) fn http_response(path: &str) -> (u16, String) {
    match path.trim_end_matches('/') {
        "/json/version" => (
            200,
            json!({"Browser": "Lightbulb/1", "Protocol-Version": "1.3",
                "webSocketDebuggerUrl": format!("ws://127.0.0.1:{PORT}{WS_PATH}")})
            .to_string(),
        ),
        "/json/list" | "/json" => (200, "[]".into()),
        _ => (404, "{}".into()),
    }
}

/// A web page can reach loopback too, and a browser always sends Origin on a WebSocket handshake;
/// a CDP client (Node `ws`, Playwright) never does. Same rule as `proxy.rs`.
fn no_browser(req: &Request, resp: Response) -> Result<Response, ErrorResponse> {
    if req.headers().contains_key("origin") {
        return Err(tokio_tungstenite::tungstenite::http::Response::builder().status(403).body(None).unwrap());
    }
    Ok(resp)
}

/// Bind the port, or say loudly why not: another program there would be the browser a session drives.
async fn bind_or_report(port: u16, report: impl FnOnce(String)) -> Option<TcpListener> {
    match TcpListener::bind(("127.0.0.1", port)).await {
        Ok(listener) => Some(listener),
        Err(e) => {
            log(&format!("port {port} unavailable: {e}"));
            report(format!(
                "Port {port} is in use by another program, so Claude Code and Codex sessions cannot use the \
                 Lightbulb browser. Quit that program, then reopen Lightbulb."
            ));
            None
        }
    }
}

pub async fn serve(app: tauri::AppHandle) {
    let Some(listener) = bind_or_report(PORT, |text| {
        tauri::async_runtime::spawn(async move {
            crate::notify::native_notify("Lightbulb".into(), Some(text), None, None, None).await;
        });
    })
    .await
    else {
        return;
    };
    loop {
        match listener.accept().await {
            Ok((stream, peer)) => {
                tokio::spawn(connection(app.clone(), stream, peer.port()));
            }
            Err(e) => log(&format!("accept failed: {e}")),
        }
    }
}

async fn connection(app: tauri::AppHandle, mut stream: TcpStream, client_port: u16) {
    let mut head = [0u8; 4096];
    let Ok(n) = stream.peek(&mut head).await else { return };
    let path = String::from_utf8_lossy(&head[..n]).split_whitespace().nth(1).unwrap_or("").to_string();
    if !path.starts_with("/devtools/browser/") {
        let (status, body) = http_response(&path);
        let _ = stream.read(&mut head).await; // read the request, so closing never resets the answer
        let reply = format!(
            "HTTP/1.1 {status} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            if status == 200 { "OK" } else { "Not Found" },
            body.len()
        );
        let _ = stream.write_all(reply.as_bytes()).await;
        let _ = stream.shutdown().await;
        return;
    }
    let source = {
        let app = app.clone();
        tokio::task::spawn_blocking(move || source_of(&app, client_port))
            .await
            .unwrap_or_else(|_| Err(NOT_SHARED.into()))
    };
    let Ok(ws) = tokio_tungstenite::accept_hdr_async(stream, no_browser).await else { return };
    match source {
        Ok(session) => run(app, ws, session).await,
        Err(message) => refuse(ws, Vec::new(), &message).await,
    }
}

fn source_of(app: &tauri::AppHandle, client_port: u16) -> Result<String, String> {
    let pid = peer_pid(PORT, client_port).ok_or_else(|| NOT_SHARED.to_string())?;
    let scope = crate::local_sessions::enrollment::session_scope(app).map_err(|_| NOT_SHARED.to_string())?;
    crate::local_sessions::shared_session_for(&scope, &ancestors(pid))
}

async fn still_shared(app: &tauri::AppHandle, session: &str) -> bool {
    let (app, session) = (app.clone(), session.to_string());
    tokio::task::spawn_blocking(move || {
        crate::local_sessions::enrollment::session_scope(&app)
            .is_ok_and(|scope| crate::local_sessions::session_still_shared(&scope, &session))
    })
    .await
    .unwrap_or(false)
}

fn current(app: &tauri::AppHandle) -> Option<(Channels, EventSender)> {
    let mut found = None;
    crate::with_agent_chrome_channels(app, |c| found = Some((c.clone(), c.out())));
    found
}

async fn run(app: tauri::AppHandle, ws: Ws, session: String) {
    let Some((channels, out)) = current(&app) else {
        return refuse(ws, Vec::new(), NOT_CONNECTED).await;
    };
    let id = uuid::Uuid::new_v4().simple().to_string();
    let browser_on = app.path().app_data_dir().is_ok_and(|d| crate::desktop_permissions::load_from(&d).browser);
    let (to_client, mut from_upstream) = mpsc::channel::<String>(1024);
    let (decide, decided) = oneshot::channel();
    lock(&PENDING).insert(id.clone(), Pending { to_client, decide });
    let offer = json!({"channel": id, "sessionId": session, "macBrowser": browser_on});
    if out.send("browser.cdp.offer", offer).await.is_err() {
        lock(&PENDING).remove(&id);
        return refuse(ws, Vec::new(), NOT_CONNECTED).await;
    }
    let (mut sink, mut stream) = ws.split();
    let mut early = Vec::new();
    let decided = tokio::time::timeout(OFFER_WAIT, decided);
    tokio::pin!(decided);
    let upstream = loop {
        tokio::select! {
            answer = &mut decided => match answer {
                Ok(Ok(Decision::Chrome(tx))) => break tx,
                Ok(Ok(Decision::Refused(message))) => return refuse_split(sink, stream, early, &message).await,
                _ => {
                    lock(&PENDING).remove(&id);
                    let _ = out.send("browser.cdp.close", json!({"channel": id, "code": "TIMEOUT"})).await;
                    return refuse_split(sink, stream, early, "The Lightbulb browser did not answer; try again").await;
                }
            },
            msg = stream.next() => match msg {
                Some(Ok(Message::Text(text))) => {
                    if early.len() < MAX_EARLY {
                        early.push(text.to_string());
                    }
                }
                Some(Ok(_)) => {}
                _ => {
                    // The client left before the answer: whatever the backend took, it releases.
                    lock(&PENDING).remove(&id);
                    if !channels.end_local(&id) {
                        let _ = out.send("browser.cdp.close", json!({"channel": id, "code": "CLOSED"})).await;
                    }
                    return;
                }
            },
        }
    };
    let stop = Arc::new(Notify::new());
    lock(&OPEN).insert(id.clone(), (session.clone(), stop.clone()));
    let mut open = true;
    for text in early {
        open = open && upstream.send(text).await.is_ok();
    }
    let idle = tokio::time::sleep(IDLE_CLOSE);
    tokio::pin!(idle);
    let mut check = tokio::time::interval(LIVE_CHECK);
    check.tick().await;
    while open {
        tokio::select! {
            msg = stream.next() => match msg {
                Some(Ok(Message::Text(text))) => {
                    idle.as_mut().reset(tokio::time::Instant::now() + IDLE_CLOSE);
                    open = upstream.send(text.to_string()).await.is_ok();
                }
                Some(Ok(Message::Close(_))) | Some(Err(_)) | None => open = false,
                Some(Ok(_)) => {}
            },
            text = from_upstream.recv() => match text {
                Some(text) => open = sink.send(Message::Text(text.into())).await.is_ok(),
                None => open = false, // the channel ended: Chrome quit, Browser off, or the backend closed it
            },
            _ = &mut idle => open = false,
            _ = check.tick() => open = still_shared(&app, &session).await,
            _ = stop.notified() => open = false,
        }
    }
    lock(&OPEN).remove(&id);
    channels.end_local(&id);
    let _ = sink.close().await;
}

async fn refuse(ws: Ws, early: Vec<String>, message: &str) {
    let (sink, stream) = ws.split();
    refuse_split(sink, stream, early, message).await
}

/// Tell a refused client why, in CDP's own error shape (puppeteer and Playwright surface the
/// message), for the commands it already sent and those of the next second; then close.
async fn refuse_split(mut sink: SplitSink<Ws, Message>, mut stream: SplitStream<Ws>, early: Vec<String>, message: &str) {
    let answer = |text: &str| {
        serde_json::from_str::<Value>(text)
            .ok()
            .and_then(|v| v.get("id").cloned())
            .map(|id| json!({"id": id, "error": {"code": -32000, "message": message}}).to_string())
    };
    for reply in early.iter().filter_map(|text| answer(text)) {
        let _ = sink.send(Message::Text(reply.into())).await;
    }
    let _ = tokio::time::timeout(Duration::from_secs(1), async {
        while let Some(Ok(Message::Text(text))) = stream.next().await {
            if let Some(reply) = answer(&text) {
                let _ = sink.send(Message::Text(reply.into())).await;
            }
        }
    })
    .await;
    // A close reason is at most 123 bytes.
    let reason: String = message.char_indices().take_while(|(i, c)| i + c.len_utf8() <= 120).map(|(_, c)| c).collect();
    let _ = sink.send(Message::Close(Some(CloseFrame { code: CloseCode::Policy, reason: reason.into() }))).await;
}

fn clean(text: &str) -> String {
    text.chars().filter(|c| !c.is_control()).take(300).collect()
}

fn refusal(code: &str) -> &'static str {
    match code {
        "CAPACITY" => "This Mac's browser is busy; try again shortly.",
        "BROWSER_OFF" => "Browser access on this Mac is off in Lightbulb's Settings.",
        _ => "The Lightbulb browser is not available right now",
    }
}

/// Backend events for this module's channels. node_invoke calls it before `Channels::on_event`:
/// true when handled here.
pub(crate) fn on_event(channels: &Channels, event: &str, payload: &Value) -> bool {
    let Some(id) = payload.get("channel").and_then(Value::as_str) else { return false };
    match event {
        "browser.cdp.open" => {
            let Some(pending) = lock(&PENDING).remove(id) else { return false };
            let profile = payload.get("profileId").and_then(Value::as_str).unwrap_or("");
            let decision = match channels.open_local(id, profile, pending.to_client) {
                Ok(tx) => Decision::Chrome(tx),
                Err(code) => {
                    channels.send_close(id, code);
                    Decision::Refused(refusal(code).into())
                }
            };
            let _ = pending.decide.send(decision);
            true
        }
        "browser.cdp.close" => {
            let Some(pending) = lock(&PENDING).remove(id) else { return false };
            let message = payload.get("message").and_then(Value::as_str).map(clean).filter(|m| !m.is_empty());
            let _ = pending.decide.send(Decision::Refused(message.unwrap_or_else(|| refusal("").into())));
            true
        }
        _ => false,
    }
}
```

`node_invoke.rs` — the CDP task (lines 89-93):

```rust
    let cdp_task = tokio::spawn(async move {
        while let Some((event, payload)) = cdp_events.recv().await {
            // A shared session's offered channel first (session_browser), then the runs' own.
            if !crate::session_browser::on_event(&cdp_chrome, &event, &payload) {
                cdp_chrome.on_event(&event, &payload);
            }
        }
    });
```

`lib.rs` setup — after `let _ = std::fs::remove_dir_all(agent_chrome::downloads_dir().join(agent_chrome::UPLOADS));`:

```rust
            // The Lightbulb browser port for shared Claude Code and Codex sessions (spec 2026-10-11).
            tauri::async_runtime::spawn(session_browser::serve(app.handle().clone()));
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/desktop/src-tauri && cargo test session_browser agent_chrome node_invoke`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src/session_browser.rs apps/desktop/src-tauri/src/node_invoke.rs apps/desktop/src-tauri/src/lib.rs
git commit -m "feat(desktop): a loopback CDP port that offers a shared session's connection to the workspace"
```

### Task 8: Desktop — the `lightbulb-browser` MCP entry

**Files:**
- Modify: `apps/desktop/src-tauri/src/local_sessions/native_mcp.rs` (`configured` lines 34-72 → `entry` + `configured`; `change` lines 74-90; `configure` lines 182-228)
- Test: same file, `mod tests`

**Interfaces:**
- Consumes: `crate::session_browser::PORT`.
- Produces: `native_mcp::BROWSER_SERVER_NAME = "lightbulb-browser"`, `browser_command() -> Vec<String>`; `configure(home, executable, enabled)` also adds/removes the browser entry for Claude Code and Codex.

- [ ] **Step 1: Write the failing test**

In `native_mcp.rs` `mod tests`:

```rust
    #[cfg(unix)]
    #[test]
    fn sharing_on_adds_the_browser_entry_and_off_removes_only_ours() {
        use std::os::unix::fs::PermissionsExt;
        let home = tempfile::tempdir().unwrap();
        let h = home.path();
        let bin = h.join(".local/bin");
        std::fs::create_dir_all(&bin).unwrap();
        // A fake `claude` CLI that edits ~/.claude.json the way `claude mcp` does.
        let fake = bin.join("claude");
        std::fs::write(&fake, r#"#!/usr/bin/env python3
import json, os, sys
p = os.path.join(os.environ["HOME"], ".claude.json")
doc = json.load(open(p)) if os.path.exists(p) else {}
servers = doc.setdefault("mcpServers", {})
a = sys.argv[1:]
if a[:2] == ["mcp", "remove"]: servers.pop(a[-1], None)
elif a[:2] == ["mcp", "add"]: servers[a[4]] = {"command": a[6], "args": a[7:]}
json.dump(doc, open(p, "w"))
"#).unwrap();
        std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755)).unwrap();
        if super::super::provider_program(h, "claude") != std::fs::canonicalize(&fake).ok() {
            return; // provider discovery does not look in this temp home here
        }
        let exe = Path::new("/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        let servers = || serde_json::from_str::<Value>(&std::fs::read_to_string(h.join(".claude.json")).unwrap()).unwrap()["mcpServers"].clone();
        configure(h, exe, true);
        assert_eq!(servers()[BROWSER_SERVER_NAME], json!({"command": "npx",
            "args": ["-y", "chrome-devtools-mcp@latest", "--browserUrl", "http://127.0.0.1:39222"]}));
        configure(h, exe, false);
        assert!(servers()[BROWSER_SERVER_NAME].is_null());
        std::fs::write(h.join(".claude.json"), json!({"mcpServers": {BROWSER_SERVER_NAME: {"command": "/bin/other", "args": []}}}).to_string()).unwrap();
        let notes = configure(h, exe, true);
        assert!(notes.iter().any(|n| n.contains("not Lightbulb's")), "{notes:?}");
        assert_eq!(servers()[BROWSER_SERVER_NAME]["command"], "/bin/other");
        configure(h, exe, false);
        assert_eq!(servers()[BROWSER_SERVER_NAME]["command"], "/bin/other", "someone else's is never removed");
    }
```

- [ ] **Step 2: Run it to see it fail**

Run: `cd apps/desktop/src-tauri && cargo test native_mcp`
Expected: FAIL to compile (`cannot find value BROWSER_SERVER_NAME`).

- [ ] **Step 3: Implement**

```rust
/// The session browser's MCP entry (spec 2026-10-11): Google's chrome-devtools-mcp attached to this
/// Mac's session-browser port. We write no browser tools of our own.
pub(super) const BROWSER_SERVER_NAME: &str = "lightbulb-browser";

pub(super) fn browser_command() -> Vec<String> {
    vec!["npx".into(), "-y".into(), "chrome-devtools-mcp@latest".into(), "--browserUrl".into(),
        format!("http://127.0.0.1:{}", crate::session_browser::PORT)]
}

/// The MCP server entry `name` as the provider stores it (`{command, args}`); Null when absent.
fn entry(home: &Path, cli: &Path, provider: &str, name: &str) -> io::Result<Value> {
    if provider == "claude" {
        let doc = match std::fs::read(home.join(".claude.json")) {
            Ok(bytes) => serde_json::from_slice::<Value>(&bytes)
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Claude MCP config invalid"))?,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Value::Null),
            Err(e) => return Err(e),
        };
        return Ok(doc["mcpServers"][name].clone());
    }
    let mut command = Command::new(cli);
    cli_home(&mut command, home);
    let output = command.args(["mcp", "get", name, "--json"]).output()?;
    if !output.status.success() {
        return Ok(Value::Null);
    }
    let doc = serde_json::from_slice::<Value>(&output.stdout)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Codex MCP config invalid"))?;
    Ok(doc["transport"].clone())
}

fn configured(home: &Path, cli: &Path, provider: &str, name: &str) -> io::Result<Option<String>> {
    let entry = entry(home, cli, provider, name)?;
    if entry.is_null() {
        return Ok(None);
    }
    if entry["args"] != json!(["session-mcp"]) {
        return Err(io::Error::new(io::ErrorKind::AlreadyExists, "MCP server name is already used"));
    }
    entry["command"]
        .as_str()
        .map(|v| Some(v.to_owned()))
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "MCP server command invalid"))
}

fn change(home: &Path, cli: &Path, provider: &str, verb: &str, name: &str, line: &[&std::ffi::OsStr]) -> io::Result<()> {
    let mut command = Command::new(cli);
    cli_home(&mut command, home);
    if provider == "claude" {
        command.args(["mcp", verb, "--scope", "user", name]);
    } else {
        command.args(["mcp", verb, name]);
    }
    if verb == "add" {
        command.arg("--").args(line);
    }
    if command.output()?.status.success() {
        Ok(())
    } else {
        Err(io::Error::other("native MCP config change failed"))
    }
}

/// `lightbulb-browser`: added while sharing is on, removed (only if it is ours) when it goes off.
/// A server of that name that is not ours is left alone and reported.
fn configure_browser(home: &Path, cli: &Path, provider: &str, enabled: bool, notes: &mut Vec<String>) {
    let ours = browser_command();
    let Ok(current) = entry(home, cli, provider, BROWSER_SERVER_NAME) else {
        return notes.push(format!("{provider} MCP settings are unavailable"));
    };
    let is_ours = current["command"].as_str() == Some(ours[0].as_str()) && current["args"] == json!(&ours[1..]);
    if !enabled {
        if is_ours && change(home, cli, provider, "remove", BROWSER_SERVER_NAME, &[]).is_err() {
            notes.push(format!("{provider} {BROWSER_SERVER_NAME} could not be removed"));
        }
        return;
    }
    if is_ours {
        return;
    }
    if !current.is_null() {
        return notes.push(format!("{provider} already has a {BROWSER_SERVER_NAME} server that is not Lightbulb's; it was left as it is"));
    }
    let line: Vec<&std::ffi::OsStr> = ours.iter().map(std::ffi::OsStr::new).collect();
    if change(home, cli, provider, "add", BROWSER_SERVER_NAME, &line).is_err() {
        notes.push(format!("{provider} {BROWSER_SERVER_NAME} could not be installed"));
    }
}
```

In `configure`, call it first inside the loop, and pass a command line to the existing `change` calls:

```rust
    for provider in ["claude", "codex"] {
        let Some(cli) = super::provider_program(home, provider) else {
            continue;
        };
        configure_browser(home, &cli, provider, enabled, &mut notes);
        let ours_line = [executable.as_os_str(), std::ffi::OsStr::new("session-mcp")];
```

and the four existing `change` calls in that loop become:

```rust
            let _ = change(home, &cli, provider, "remove", LEGACY_SERVER_NAME, &[]);
            // ...
            if ours && change(home, &cli, provider, "remove", SERVER_NAME, &[]).is_err() {
            // ...
        if (stale || foreign) && change(home, &cli, provider, "remove", SERVER_NAME, &[]).is_err() {
            // ...
        if !ours && change(home, &cli, provider, "add", SERVER_NAME, &ours_line).is_err() {
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/desktop/src-tauri && cargo test native_mcp`
Expected: PASS (including `enable_replaces_a_hosted_lightbulb_entry_and_says_so`).

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src/local_sessions/native_mcp.rs
git commit -m "feat(desktop): register lightbulb-browser for Claude Code and Codex while sharing is on"
```

### Task 9: Docs, PR and prod acceptance for M1

**Files:**
- Modify: `packages/claude-plugin/lightbulb/skills/lightbulb-session-sharing/SKILL.md` (new section before "## When your user asks to share this session")
- Modify: `apps/infra/hermes/skills-src/lightbulb/references/surfaces.md` (new paragraph after the "**Which sessions are shared**" paragraph, line ~105)
- Modify: `apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md` (new anchor after the "Open in a terminal" entry, line ~106)

- [ ] **Step 1: Write the session-sharing skill section**

```markdown
## Browsing

A shared session browses with the `lightbulb-browser` MCP tools (Google's
chrome-devtools-mcp, which the Lightbulb Mac app installs). They drive the Mac's
"Lightbulb agent" Chrome window, which has the workspace's sign-ins; never your user's
everyday Chrome. New pages open in a background window: nothing comes to the front.
If a tool fails with "Share this session to use the Lightbulb browser", this session is
not shared; tell your user, and do not look for another browser. "This Mac's browser is
busy" means four connections are already open; try again shortly. After five minutes
without a browser command the connection closes; the next tool call opens it again.
```

- [ ] **Step 2: Write the surfaces paragraph**

```markdown
**Session browser** (desktop app on the session's Mac, sharing on): a shared Claude Code or
Codex session browses with its `lightbulb-browser` tools in that Mac's "Lightbulb agent"
Chrome, with the workspace's sign-ins, exactly as a run does after `lightbulb_desktop
use_browser`. A session that is not shared is refused ("Share this session to use the
Lightbulb browser"). Its sign-ins load when it starts browsing and save when it stops (five
minutes after its last command, or when the session ends). It shares the Mac's four browser
connections with agent runs.
```

- [ ] **Step 3: Add the MAINTENANCE anchor**

```text
  - Session browser (shared Claude/Codex sessions browse in the Lightbulb browser) ... references/surfaces.md ("Session browser" paragraph)
                              <- apps/desktop/src-tauri/src/session_browser.rs (port 39222, peer process, offers);
                                 apps/desktop/src-tauri/src/local_sessions/native_mcp.rs (lightbulb-browser entry);
                                 apps/backend/core/services/session_browser.py (lease, sync, refusals);
                                 packages/claude-plugin/lightbulb/skills/lightbulb-session-sharing/SKILL.md ("Browsing")
```

- [ ] **Step 4: Build the skills and commit**

Run: `cd apps/infra/hermes && ./build-skills.sh` (it must succeed; `skills/` is generated and gitignored, never commit it).

```bash
git add packages/claude-plugin/lightbulb/skills/lightbulb-session-sharing/SKILL.md apps/infra/hermes/skills-src/lightbulb/references/surfaces.md apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md
git commit -m "docs(sessions): the session browser in the session-sharing skill and the lightbulb skill"
```

- [ ] **Step 5: Open the PR and watch it**

```bash
git push -u origin feat/session-browser-mac
gh pr create --repo TheLightbulbCompany/lightbulb --base main --title "feat(sessions): shared Claude Code and Codex sessions browse in the Mac's Lightbulb browser (M1)" --body-file /dev/stdin <<'BODY'
Milestone M1 of docs/superpowers/plans/2026-10-11-session-browser.md (spec docs/superpowers/specs/2026-10-11-session-browser-design.md).

- Desktop: loopback CDP port 39222; peer process -> shared session; `browser.cdp.offer`; session channels in agent_chrome (new tabs in a background window); `lightbulb-browser` MCP entry for Claude Code and Codex.
- Backend: the `session` run family; offer -> mac_browser lease -> runtime sign-in sync over the Mac door -> open -> renew -> release (save).
- Bridge: `browser.state` with a Mac door.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01N9nLe5RG6ZAEHy5ihTq7gM
BODY
gh run watch "$(gh run list --repo TheLightbulbCompany/lightbulb --branch feat/session-browser-mac --limit 1 --json databaseId --jq '.[0].databaseId')" --repo TheLightbulbCompany/lightbulb --exit-status --interval 60
```

Expected: CI green. Wait for Codex's review on all four surfaces (CLAUDE.md "Code Review"), address findings, merge.

- [ ] **Step 6: Ship the bridge, the backend and the desktop app**

1. `build-hermes-image.yml` builds a candidate `h-<date>-<sha7>` on the merge; land a PR flipping `hermes-version.json` `dev.tag`, deploy, then `prod.tag`; run `apps/backend/scripts/roll_hermes_runtimes.py` (CLAUDE.md "Runtime rollout"; for prod, a one-off Fargate run-task, not ECS exec).
2. Promote the backend: `gh workflow run deploy.yml --ref main --repo TheLightbulbCompany/lightbulb` once the "Before dispatching" check reads 0.
3. Build the desktop app: `gh workflow run desktop-build.yml --ref main --repo TheLightbulbCompany/lightbulb`; install the update on the founder's Mac.

- [ ] **Step 7: Prod acceptance (founder's Mac, sharing on)**

1. `curl -s http://127.0.0.1:39222/json/version` → JSON with `webSocketDebuggerUrl` `ws://127.0.0.1:39222/devtools/browser/lightbulb`.
2. `claude mcp get lightbulb-browser` and `codex mcp get lightbulb-browser` → the npx command line.
3. In a shared Claude Code session: "Use lightbulb-browser to open https://example.com and take a snapshot." The page opens in the "Lightbulb agent" window in the background; the front app stays in front. A site the workspace is signed into in the cloud browser shows signed in.
4. Leave it idle for 5 minutes; ask for another snapshot: it reconnects and works (Review Focus 1).
5. Set the session to Only me; ask again: the tool error contains "Share this session to use the Lightbulb browser".
6. Same as 3 in a shared Codex CLI session.
7. End the session; within a minute the backend log shows `mac_browser.sync` `save` `ok` for the owner.

Record the results on the PR.

---

# Milestone M2 — the Browser tab

Branch: `feat/session-browser-view` in `.claude/worktrees/session-browser-view`, from `origin/main` after M1 merged (same commands as M1 with the new name).

### Task 10: Shared — the `image` kind and `LocalSession.browser`

**Files:**
- Modify: `packages/shared/src/live/types.ts` (new `SessionBrowserFrame` after `SessionScreen` at line 233; the envelope at line 325; the validator case at line 432)
- Modify: `packages/shared/src/index.ts:56` (export `SessionBrowserFrame`)
- Modify: `packages/shared/src/messaging/types.ts` (`LocalSession`, beside `screenAvailable?: boolean;` at line 394)
- Modify: `apps/frontend/src/components/messaging/SessionTerminalView.tsx:371` (ignore image frames)
- Test: `packages/shared/src/live/sessionScreen.test.ts`

**Interfaces:**
- Produces: `type SessionBrowserFrame = { sessionId: string; kind: "image"; data: string; w: number; h: number; seq: number }`; `workspace.session.screen` payload is `SessionScreen | SessionBrowserFrame`; `LocalSession.browser?: "mac" | "cloud" | null`. Used by Task 12.

- [ ] **Step 1: Write the failing test**

Append to `packages/shared/src/live/sessionScreen.test.ts`:

```ts
test("a session browser frame is an image kind on the same channel, and only a well-formed one passes", () => {
  const event = { schemaVersion: 1, eventId: "img-1", type: "workspace.session.screen", occurredAt: "2026-10-11T00:00:00Z",
    payload: { sessionId: "s1", kind: "image", data: "/9j/4AAQ", w: 1280, h: 800, seq: 1760000000000 } };
  expect(parseLiveEvent(event)).toEqual(event);
  for (const bad of [{ data: "" }, { w: "1280" }, { h: Number.NaN }, { seq: -1 }, { sessionId: "" }]) {
    expect(parseLiveEvent({ ...event, payload: { ...event.payload, ...bad } })).toBeNull();
  }
});
```

- [ ] **Step 2: Run it to see it fail**

Run: `cd packages/shared && pnpm vitest run src/live/sessionScreen.test.ts`
Expected: FAIL (`parseLiveEvent` answers null for the image frame).

- [ ] **Step 3: Implement**

`types.ts`, after the `SessionScreen` type:

```ts
/** A shared session's browser tab as a 480 px JPEG (spec 2026-10-11 §3), on the session's frames
 *  channel: `data` is base64, `w`x`h` the tab's CSS size (viewer input is in those pixels), `seq`
 *  orders frames. Frames flow only while someone has the Browser tab open; nothing stores them. */
export type SessionBrowserFrame = { sessionId: string; kind: "image"; data: string; w: number; h: number; seq: number };
```

The envelope line becomes:

```ts
  | LiveEventEnvelope<"workspace.session.screen", SessionScreen | SessionBrowserFrame>
```

The validator case begins:

```ts
    case "workspace.session.screen":
      if (payload.kind === "image") {
        return str(payload.sessionId) && str(payload.data) && num(payload.w) && num(payload.h)
          && Number.isSafeInteger(payload.seq) && (payload.seq as number) >= 0;
      }
      return str(payload.sessionId) && str(payload.generation)
      // ... the existing terminal-frame checks, unchanged
```

`index.ts:56` adds `SessionBrowserFrame` to the `./live/types` export list.

`messaging/types.ts`, in `LocalSession` after `screenAvailable?: boolean;`:

```ts
  /** Where this running session's browser connection is open (spec 2026-10-11); absent or null: none. */
  browser?: "mac" | "cloud" | null;
```

`SessionTerminalView.tsx:371`:

```ts
      if (event.type !== "workspace.session.screen" || event.payload.kind === "image") return; // the Browser tab's
```

- [ ] **Step 4: Run the test and the type check**

Run: `cd packages/shared && pnpm vitest run src/live/sessionScreen.test.ts && cd ../../apps/frontend && pnpm exec tsc --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 5: Commit**

```bash
git add packages/shared/src/live/types.ts packages/shared/src/index.ts packages/shared/src/messaging/types.ts packages/shared/src/live/sessionScreen.test.ts apps/frontend/src/components/messaging/SessionTerminalView.tsx
git commit -m "feat(shared): a session's browser frames are an image kind on its frames channel"
```

### Task 11: Backend — frames while watched, and watcher input

**Files:**
- Modify: `apps/backend/core/services/session_browser.py` (`watch`, `_watch_mac`, `_publish`, `input_target`, `may_type`; `_hold_mac` runs the first two)
- Modify: `apps/backend/core/services/mac_live.py` (new `live` context manager; `run` lines 131-150 uses it; `input` lines 153-169 gets the session branch)
- Modify: `apps/backend/routers/messaging.py` (two routes after `type_into_local_session` at line 375; import `mac_live, session_browser`)
- Test: `apps/backend/tests/unit/services/test_session_browser.py`, `apps/backend/tests/unit/services/test_mac_live.py`, `apps/backend/tests/unit/routers/test_session_browser_routes.py` (new)

**Interfaces:**
- Consumes: Task 4 (`_hold_mac`), `node_broker.NodeSession.cast_set(channel, view, thumb)`, `CdpChannel.cast`/`cast_ready`, `session_screen._spend`, `session_screen._still_a_member`, `routers.container.deliver_mac_input`, `MAC_INPUT_PATH`, `mac_browser._forward_mac`.
- Produces: `session_browser.watch(auth, session_id)`, `input_target(auth, session_id) -> dict`, `may_type(owner, user, run) -> bool`; `mac_live.live(owner, run_id, session, channel, room_id=None)`; routes `POST /api/v1/messaging/sessions/{session_id}/browser/viewing` (204) and `POST /api/v1/messaging/sessions/{session_id}/browser/input` (204, body `{events}` in `/browser/mac/input`'s event shape). Used by Tasks 12, 16.

- [ ] **Step 1: Write the failing tests**

In the `env` fixture of `apps/backend/tests/unit/services/test_session_browser.py`, add (the M1 tests now run `_watch_mac` and `_publish` beside the hold):

```python
    monkeypatch.setattr(session_browser.store, "ensure_workspace_id", AsyncMock(return_value="ws-1"))
    monkeypatch.setattr(session_browser.live_events, "get_live_event_publisher", lambda: None)
```

Then append:

```python
class FakeNode:
    def __init__(self):
        self.sets = []

    def cast_set(self, channel, view, thumb):
        self.sets.append((view, thumb))


class FakeCastChannel:
    def __init__(self):
        self.cast, self.cast_ready = {}, asyncio.Event()


async def test_frames_flow_only_while_someone_watches(env, monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(session_browser, "_clock", lambda: now["t"])
    monkeypatch.setattr(session_browser.mac_live, "FRAME_POLL_S", 0.01)
    node = FakeNode()
    watching = asyncio.create_task(session_browser._watch_mac(OWNER, SID, node, object()))
    await asyncio.sleep(0.05)
    assert node.sets == [], "nobody watches: no cast"
    env.row["browserWatchedAt"] = now["t"]
    await asyncio.sleep(0.05)
    assert node.sets == [(False, True)], "the thumb tier only"
    now["t"] += session_browser.mac_live.WANTED_S
    await asyncio.sleep(0.05)
    assert node.sets[-1] == (False, False)
    watching.cancel()


async def test_a_cast_frame_goes_to_the_session_frames_channel_as_an_image(monkeypatch):
    published = []

    class Publisher:
        async def publish_session_frames(self, workspace, event):
            published.append((workspace, event))

    monkeypatch.setattr(session_browser.live_events, "get_live_event_publisher", lambda: Publisher())
    monkeypatch.setattr(session_browser.store, "ensure_workspace_id", AsyncMock(return_value="ws-1"))
    session_browser.session_screen._spent.clear()
    channel = FakeCastChannel()
    publishing = asyncio.create_task(session_browser._publish(OWNER, SID, channel))
    channel.cast["thumb"] = {"jpegBase64": "/9j/4AAQ", "w": 1280, "h": 800, "seq": 7}
    channel.cast_ready.set()
    await asyncio.sleep(0.01)
    publishing.cancel()
    ((workspace, event),) = published
    assert workspace == "ws-1" and event["type"] == "workspace.session.screen"
    assert event["payload"] == {"sessionId": SID, "kind": "image", "data": "/9j/4AAQ", "w": 1280, "h": 800, "seq": 7}


async def test_only_people_who_may_type_into_the_session_may_drive_its_browser(env, monkeypatch):
    monkeypatch.setattr(session_browser.session_screen, "_still_a_member", AsyncMock())
    assert await session_browser.may_type(OWNER, MEMBER, RUN), "its own person"
    assert not await session_browser.may_type(OWNER, "user_2", RUN), "a watcher"
    env.row["allowedSenders"] = ["user_2"]
    assert await session_browser.may_type(OWNER, "user_2", RUN), "let in by its person"
    env.row["unshared"] = True
    assert not await session_browser.may_type(OWNER, MEMBER, RUN)
    assert not await session_browser.may_type(OWNER, MEMBER, "r1"), "not a session run"
```

Append to `apps/backend/tests/unit/services/test_mac_live.py`:

```python
async def test_a_session_entry_delivers_only_to_people_who_may_type(monkeypatch):
    from core.services import session_browser

    session, channel = FakeSession(), FakeChannel()
    allowed = {"user_1"}

    async def may_type(owner, user, run):
        return user in allowed

    monkeypatch.setattr(session_browser, "may_type", may_type)
    move = [{"type": "move", "x": 1.0, "y": 1.0}]
    with mac_live.live(OWNER, "session-x", session, channel):
        await mac_live.input(OWNER, "user_1", "session-x", move)
        with pytest.raises(PermissionError):
            await mac_live.input(OWNER, "user_2", "session-x", move)
    assert session.inputs == [move]
    with pytest.raises(LookupError):
        await mac_live.input(OWNER, "user_1", "session-x", move)
```

Create `apps/backend/tests/unit/routers/test_session_browser_routes.py`:

```python
"""The Browser tab's routes: who may watch and drive a shared session's browser."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.auth import AuthContext, get_human_session
from core.services import task_address
from core.services.connection_service import ConnectionService
from core.services.messaging import ApiProblem
from routers import container
from routers import messaging as router

SID = "11111111-1111-4111-8111-111111111111"
ROW = {"ownerId": "org_1", "sessionId": SID, "humanUserId": "user_1", "deviceId": "mac1",
       "browserOpen": {"where": "mac", "until": 2**40}}
CLICK = {"type": "down", "x": 10, "y": 20, "button": "left", "clicks": 1}
HERE = "http://10.0.0.1:8000"


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(router.router, prefix="/api/v1")
    app.add_exception_handler(ApiProblem, lambda _r, exc: JSONResponse(exc.body(), exc.status_code))
    app.dependency_overrides[get_human_session] = lambda: AuthContext(user_id="user_2", org_id="org_1")

    async def desktop_task_address(self, owner, device):
        return HERE

    monkeypatch.setattr(ConnectionService, "desktop_task_address", desktop_task_address)
    monkeypatch.setattr(task_address, "own_address", lambda: HERE)
    monkeypatch.setattr(router.session_browser.session_screen, "_still_a_member", AsyncMock())
    return TestClient(app)


def test_a_person_who_may_not_type_cannot_drive_the_browser(client, monkeypatch):
    monkeypatch.setattr(router.local_sessions, "authorize_session",
                        AsyncMock(side_effect=ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can do that.")))
    delivered = AsyncMock()
    monkeypatch.setattr(container, "deliver_mac_input", delivered)
    response = client.post(f"/api/v1/messaging/sessions/{SID}/browser/input", json={"events": [CLICK]})
    assert response.status_code == 403
    delivered.assert_not_awaited()
    assert router.local_sessions.authorize_session.await_args.kwargs == {"owner_only": True}


def test_input_reaches_the_session_run_on_the_task_holding_the_mac(client, monkeypatch):
    monkeypatch.setattr(router.local_sessions, "authorize_session", AsyncMock(return_value=(dict(ROW), {})))
    delivered = AsyncMock()
    monkeypatch.setattr(container, "deliver_mac_input", delivered)
    assert client.post(f"/api/v1/messaging/sessions/{SID}/browser/input", json={"events": [CLICK]}).status_code == 204
    delivered.assert_awaited_once_with("org_1", "user_2", f"session-{SID}",
                                       [{"type": "down", "x": 10.0, "y": 20.0, "button": "left", "clicks": 1}])


def test_a_closed_browser_takes_no_input_and_no_watch(client, monkeypatch):
    monkeypatch.setattr(router.local_sessions, "authorize_session",
                        AsyncMock(return_value=({**ROW, "browserOpen": {"where": "mac", "until": 1}}, {})))
    assert client.post(f"/api/v1/messaging/sessions/{SID}/browser/input", json={"events": [CLICK]}).status_code == 409
    assert client.post(f"/api/v1/messaging/sessions/{SID}/browser/viewing").status_code == 409
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_browser.py tests/unit/services/test_mac_live.py tests/unit/routers/test_session_browser_routes.py -v`
Expected: FAIL (`_watch_mac`, `mac_live.live`, the routes do not exist).

- [ ] **Step 3: Implement**

`mac_live.py` — add `import contextlib` and, before `run`:

```python
@contextlib.contextmanager
def live(owner: str, run_id: str, session, channel, room_id: str | None = None):
    """`run_id`'s live channel on this task while the block runs: where `input` delivers. A room run's
    (`room_id`), or a shared session's browser (None: session_browser.may_type decides who types)."""
    key, entry = (owner, run_id), (session, channel, room_id)
    _live[key] = entry
    try:
        yield
    finally:
        if _live.get(key) is entry:
            del _live[key]
```

`run` becomes:

```python
async def run(claims: dict, session, channel) -> None:
    """Until the channel closes; room runs on a cast-capable Mac only."""
    if claims.get("family") != "room" or not claims.get("roomId") or not session.can_cast:
        return
    tasks = {
        asyncio.create_task(_watch(claims["ownerId"], claims["runId"], session, channel)),
        asyncio.create_task(_publish(claims, channel)),
        asyncio.create_task(channel.closed.wait()),
    }
    with live(claims["ownerId"], claims["runId"], session, channel, claims["roomId"]):  # the door's room, never a caller's
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:  # not awaited, as in mac_cdp.serve; the callback retrieves any exception
                task.cancel()
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
```

`input` becomes:

```python
async def input(owner: str, user: str, run_id: str, events: list[dict], *, viewer_owner: str | None = None) -> None:  # noqa: A001 — the spec's name
    """Only a current human member of the run's room (who can see its frames) may drive it; for a
    shared session's browser (an entry with no room), only a person who may type into the session.
    A guest's membership is keyed under their own workspace, while the channel belongs to the room
    owner's workspace.

    Raises LookupError when the run has no live channel on this task, PermissionError when `user`
    may not drive it, otherwise whatever `cast_input` raises (CommandNotAllowed / CdpClosed /
    NodeCapacity)."""
    entry = _live.get((owner, run_id))
    if entry is None:
        raise LookupError(run_id)
    session, channel, room_id = entry
    if room_id is None:
        from core.services import session_browser  # it imports this module

        if not await session_browser.may_type(owner, user, run_id):
            raise PermissionError(run_id)
    else:
        try:
            await require_room_member(viewer_owner or owner, user, room_id)
        except ApiProblem:
            raise PermissionError(run_id) from None
    session.cast_input(channel, events)
```

`session_browser.py` — imports grow to:

```python
from core.services import container_rpc, live_events, local_sessions, mac_browser, mac_live, session_screen
from core.services.messaging import ApiProblem, make_live_event
from core.services.node_broker import CDP_COMMAND, CdpClosed, CommandNotAllowed, NodeCapacity
```

and add:

```python
async def watch(auth, session_id: str) -> None:
    """The Browser tab's ~20 s beat: this session's browser frames for the next mac_live.WANTED_S.
    Any member who may read the session; frames are stored nowhere."""
    row, _ = await local_sessions.authorize_session(auth, None, session_id)
    await session_screen._still_a_member(row, auth.user_id)
    if local_sessions.browser_open(row) is None:
        raise ApiProblem("LOCAL_SESSION_OFFLINE", "This session's browser is not open.")
    await store.run_in_thread(
        store._table().update_item,
        Key={"pk": repo.session_pk(row["ownerId"], row["sessionId"]), "sk": "META"},
        UpdateExpression="SET browserWatchedAt = :now",
        ConditionExpression="attribute_exists(pk)",
        ExpressionAttributeValues={":now": int(_clock())},
    )


async def input_target(auth, session_id: str) -> dict:
    """The session a person drives the browser of: they may type into it (`may_control`), are still
    in the workspace, and its browser is open."""
    row, _ = await local_sessions.authorize_session(auth, None, session_id, owner_only=True)
    await session_screen._still_a_member(row, auth.user_id)
    if local_sessions.browser_open(row) is None:
        raise ApiProblem("LOCAL_SESSION_OFFLINE", "This session's browser is not open.")
    return row


async def may_type(owner: str, user: str, run: str) -> bool:
    """Re-checked where the Mac's socket is (mac_live.input): this person may still type into it."""
    sid = session_of(run)
    row = await repo.get_session(owner, sid) if sid else None
    if not row or row.get("unshared") or not local_sessions.may_control(row, user):
        return False
    try:
        await session_screen._still_a_member(row, user)
    except ApiProblem:
        return False
    return True


async def _watch_mac(owner: str, session_id: str, node, channel) -> None:
    """The thumb-tier cast while someone has the Browser tab open, re-set every SET_EVERY_S so the
    Mac's 15 s TTL keeps it alive; off otherwise (mac_live's rhythm). Never the view tier: a 480 px
    frame fits the session frames budget."""
    sent, sent_at = False, 0.0
    while True:
        try:
            row = await repo.get_session(owner, session_id)
        except Exception as exc:  # noqa: BLE001 -- a DynamoDB blip: keep the last state
            logger.warning("session browser watch read failed owner=%s error=%s", owner, type(exc).__name__)
        else:
            now = _clock()
            want = bool(row) and now - float((row or {}).get("browserWatchedAt") or 0) < mac_live.WANTED_S
            if want != sent or (want and now - sent_at >= mac_live.SET_EVERY_S):
                try:
                    node.cast_set(channel, False, want)
                    sent, sent_at = want, now
                except NodeCapacity:
                    pass  # the node socket is backed up: next tick
                except (CdpClosed, CommandNotAllowed):
                    return
        await asyncio.sleep(mac_live.FRAME_POLL_S)


async def _publish(owner: str, session_id: str, channel) -> None:
    """Each cast frame to the session's frames channel, `workspace.session.screen` kind "image",
    within the session frames budget (its own key). Stored nowhere."""
    publisher = live_events.get_live_event_publisher()
    workspace = await store.ensure_workspace_id(owner)
    while True:
        await channel.cast_ready.wait()
        channel.cast_ready.clear()  # clear, THEN pop (node_broker's consumer contract)
        frame = channel.cast.pop("thumb", None)
        channel.cast.pop("view", None)
        if frame is None or not session_screen._spend(f"{session_id}#browser", len(frame["jpegBase64"]) * 3 // 4, _clock()):
            continue
        payload = {"sessionId": session_id, "kind": "image", "data": frame["jpegBase64"],
                   "w": frame["w"], "h": frame["h"], "seq": frame["seq"]}
        try:
            await publisher.publish_session_frames(workspace, make_live_event("workspace.session.screen", payload))
        except Exception as exc:  # noqa: BLE001 -- skip it; the next frame follows
            logger.info("session browser frame dropped owner=%s error=%s", owner, type(exc).__name__)
```

`_hold_mac` runs them beside its renewals and registers the channel for input:

```python
async def _hold_mac(auth: dict, device: str, node, channel) -> None:
    """While the Mac has the channel open: frames while watched, watcher input, and every
    RENEW_SECONDS the shared re-check and the lease renewal. DesktopError ends it."""
    owner, user, sid = auth["ownerId"], auth["memberId"], session_of(auth["runId"])
    side = (
        [asyncio.create_task(_watch_mac(owner, sid, node, channel)), asyncio.create_task(_publish(owner, sid, channel))]
        if node.can_cast
        else []
    )
    try:
        with mac_live.live(owner, auth["runId"], node, channel):
            while True:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(channel.closed.wait(), RENEW_SECONDS)
                if channel.closed.is_set():
                    return
                await shared_row(owner, user, device, sid)
                answer = await mac_browser.acquire(auth, device)
                if isinstance(answer, dict) and answer.get("sync"):
                    await _sync(auth, device, answer)
                await _mark_open(owner, user, sid, "mac", announce=False)
    finally:
        for task in side:
            task.cancel()
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
```

`routers/messaging.py` — add `mac_live` and `session_browser` to the `from core.services import (...)` block, and after `type_into_local_session`:

```python
class SessionBrowserInput(ContractModel):
    events: mac_live.MacInputEvents


@router.post("/messaging/sessions/{session_id}/browser/viewing", status_code=204)
async def watch_session_browser(session_id: str, auth: AuthContext = Depends(get_human_session)):
    """The Browser tab beats every 20 s while it is open: the session's browser frames keep coming."""
    await session_browser.watch(auth, session_id)


@router.post("/messaging/sessions/{session_id}/browser/input", status_code=204)
async def session_browser_input(
    session_id: str, body: SessionBrowserInput, auth: AuthContext = Depends(get_human_session)
):
    """Clicks and keys on a shared session's browser, from a person who may type into the session,
    in /browser/mac/input's shape, delivered where its Mac's socket is."""
    from core.services import mac_browser, task_address
    from core.services.connection_service import ConnectionService
    from core.services.desktop_execution import DesktopError
    from routers import container

    row = await session_browser.input_target(auth, session_id)
    owner, run = row["ownerId"], session_browser.run_id(row["sessionId"])
    events = mac_live.dump_events(body.events)
    address = await ConnectionService().desktop_task_address(owner, row["deviceId"])
    if address is None:
        raise ApiProblem("LOCAL_SESSION_OFFLINE", "The Mac is offline.", retryable=True)
    if address == task_address.own_address():
        await container.deliver_mac_input(owner, auth.user_id, run, events)
        return
    try:
        await mac_browser._forward_mac(
            address, {"ownerId": owner, "userId": auth.user_id, "runId": run, "events": events}, container.MAC_INPUT_PATH, timeout=5
        )
    except DesktopError as exc:
        status = exc.status if exc.status in (403, 409, 429) else 502
        raise HTTPException(status, detail={"code": exc.code, "message": exc.message}) from None
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_browser.py tests/unit/services/test_mac_live.py tests/unit/routers/test_session_browser_routes.py tests/unit/routers/test_browser_mac_input.py -v`
Expected: PASS (the room-run Mac input tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/session_browser.py apps/backend/core/services/mac_live.py apps/backend/routers/messaging.py apps/backend/tests/unit/services/test_session_browser.py apps/backend/tests/unit/services/test_mac_live.py apps/backend/tests/unit/routers/test_session_browser_routes.py
git commit -m "feat(sessions): a shared session's browser streams to watchers and takes their input"
```

### Task 12: Frontend — the Browser tab

**Files:**
- Modify: `apps/frontend/src/components/workspace/panels/BrowserFrameView.tsx` (`useMacInput` lines 69-105; `BrowserFrameView` props and body lines 132-250)
- Create: `apps/frontend/src/components/messaging/SessionBrowserTab.tsx`
- Modify: `apps/frontend/src/components/messaging/LocalSessionPanel.tsx` (state near line 136; JSX around line 337)
- Modify: `apps/frontend/src/components/messaging/messaging.css` (after `.session-terminal-slot` at line 697)
- Test: `apps/frontend/src/components/workspace/panels/__tests__/BrowserFrameView.test.tsx`, `apps/frontend/src/components/messaging/__tests__/SessionBrowserTab.test.tsx` (new)

**Interfaces:**
- Consumes: Task 10 types; Task 11 routes.
- Produces: `useSessionBrowserFrame(sessionId?: string): RoomBrowserFrame | null`; `BrowserFrameView` prop `session?: { sessionId: string; canType: boolean }`; `SessionBrowserTab({ sessionId, frames, canType })`; `showsBrowserTab(session, watchingRemotely): boolean`.

- [ ] **Step 1: Write the failing tests**

Append to `BrowserFrameView.test.tsx`:

```tsx
describe("BrowserFrameView on a shared session's browser", () => {
  function image(sessionId: string, seq: number, data = `jpeg${seq}`): void {
    const event = { schemaVersion: 1, eventId: `${sessionId}-${seq}`, type: "workspace.session.screen", occurredAt: "now",
      payload: { sessionId, kind: "image", data, w: 1000, h: 500, seq } } as LiveEvent;
    act(() => handlers.forEach((handler) => handler(event)));
  }

  it("beats the session, shows its newest image only, and takes no input without canType", () => {
    render(<BrowserFrameView session={{ sessionId: "s1", canType: false }} />);
    expect(post).toHaveBeenCalledWith("/messaging/sessions/s1/browser/viewing", {});
    expect(claimFrames).not.toHaveBeenCalled();
    image("s2", 9);
    frame("s1", 3); // a room frame never shows here
    expect(screen.queryByRole("img")).toBeNull();
    image("s1", 5);
    image("s1", 4);
    expect(picture().src).toContain("jpeg5");
    fireEvent.pointerDown(picture(), { button: 0, clientX: 1, clientY: 1, pointerId: 1 });
    expect(post).not.toHaveBeenCalledWith(expect.stringContaining("/browser/input"), expect.anything());
  });

  it("someone who may type sends Mac-shaped input to the session's route", async () => {
    render(<BrowserFrameView session={{ sessionId: "s1", canType: true }} />);
    image("s1", 1);
    fireEvent.pointerDown(picture(), { button: 0, clientX: 1, clientY: 1, pointerId: 1 });
    await act(async () => { await Promise.resolve(); });
    expect(post).toHaveBeenCalledWith("/messaging/sessions/s1/browser/input",
      { events: [expect.objectContaining({ type: "down", button: "left", clicks: 1 })] });
  });
});
```

Create `apps/frontend/src/components/messaging/__tests__/SessionBrowserTab.test.tsx`:

```tsx
// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({ useApi: () => ({ post: vi.fn() }) }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ onEvent: () => () => undefined, claimFrames: () => () => undefined }) }));

import { showsBrowserTab } from "../SessionBrowserTab";

describe("showsBrowserTab", () => {
  it("shows only while a running session watched from elsewhere has a browser connection open", () => {
    expect(showsBrowserTab({ browser: "mac", status: "busy" }, true)).toBe(true);
    expect(showsBrowserTab({ browser: "cloud", status: "waiting-approval" }, true)).toBe(true);
    expect(showsBrowserTab({ browser: null, status: "busy" }, true)).toBe(false);
    expect(showsBrowserTab({ browser: "mac", status: "ended" }, true)).toBe(false);
    expect(showsBrowserTab({ browser: "mac", status: "offline" }, true)).toBe(false);
    expect(showsBrowserTab({ browser: "mac", status: "busy" }, false)).toBe(false);
  });
});
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/frontend && pnpm vitest run src/components/workspace/panels/__tests__/BrowserFrameView.test.tsx src/components/messaging/__tests__/SessionBrowserTab.test.tsx`
Expected: FAIL (`session` prop ignored; `SessionBrowserTab` module missing).

- [ ] **Step 3: Implement**

`BrowserFrameView.tsx` — after `useBrowserFrame`:

```tsx
/** The latest frame of a shared session's browser (`workspace.session.screen`, kind "image", on the
 * session's frames channel), in the room frame's shape; newest seq wins. */
export function useSessionBrowserFrame(sessionId?: string): RoomBrowserFrame | null {
  const { onEvent } = useLiveEvents();
  const [held, setHeld] = useState<{ sessionId?: string; frame: RoomBrowserFrame | null }>({ sessionId, frame: null });
  useEffect(() => {
    if (!sessionId) return undefined;
    return onEvent((event) => {
      if (event.type !== "workspace.session.screen" || event.payload.kind !== "image" || event.payload.sessionId !== sessionId) return;
      const { data, w, h, seq } = event.payload;
      setHeld((current) => {
        const kept = current.sessionId === sessionId ? current.frame : null;
        return kept && kept.seq >= seq ? current
          : { sessionId, frame: { roomId: "", agentId: "", runId: sessionId, seq, jpegBase64: data, w, h } };
      });
    });
  }, [onEvent, sessionId]);
  return held.sessionId === sessionId ? held.frame : null;
}
```

`useMacInput` takes the session as a fifth argument and posts there when set:

```tsx
function useMacInput(runId: string | undefined, deviceId: string | undefined, roomId: string | undefined,
  onResult: (error: string | null) => void, sessionId?: string) {
  const api = useApi();
  const state = useRef<{ queue: MacEvent[]; busy: boolean; runId?: string; deviceId?: string; roomId?: string; sessionId?: string; gen: number }>(
    { queue: [], busy: false, gen: 0 },
  );
  useEffect(() => {
    const current = state.current;
    current.queue.length = 0;
    current.runId = runId;
    current.deviceId = deviceId;
    current.roomId = roomId;
    current.sessionId = sessionId;
    current.gen += 1;
  }, [runId, deviceId, roomId, sessionId]);
  const pump = useCallback(() => {
    const next = () => {
      const current = state.current;
      const { runId: run, deviceId: device, roomId: room, sessionId: session, gen } = current;
      if (current.busy || !current.queue.length || (!session && (!run || !device))) return;
      const events = current.queue.splice(0, MAX_INPUT_EVENTS);
      current.busy = true;
      // A shared session's browser takes the same events on its own route (spec 2026-10-11).
      const sent = session
        ? api.post(`/messaging/sessions/${encodeURIComponent(session)}/browser/input`, { events })
        : api.post("/browser/mac/input", { runId: run, deviceId: device, ...(room && { roomId: room }), events });
      void (sent as Promise<unknown>).then(
        // ... the existing then/finally, unchanged
```

`BrowserFrameView` — the new prop and its wiring:

```tsx
export function BrowserFrameView({
  runId, interactive = false, mac, session, prefer, onTakeControl, empty, heartbeat = "/browser/viewing", heartbeatBody,
}: {
  // ... existing props
  /** A shared Claude Code or Codex session's browser (spec 2026-10-11): frames from the session's
   * frames channel (the caller holds that claim), the beat to the session, and with `canType` the
   * Mac's input shape to the session's own input route. */
  session?: { sessionId: string; canType: boolean };
}): React.ReactElement {
  const api = useApi();
  const { claimFrames } = useLiveEvents();
  // A session never shows a room's frames: no run has this id.
  const roomFrame = useBrowserFrame(session ? `session:${session.sessionId}` : runId, prefer);
  const sessionFrame = useSessionBrowserFrame(session?.sessionId);
  const frame = session ? sessionFrame : roomFrame;
  const [error, setError] = useState<string | null>(null);
  const isMac = !!mac;
  const macLike = isMac || !!session?.canType;
  const live = interactive || macLike;
  const macSend = useMacInput(runId, mac?.deviceId, mac?.roomId, setError, session?.sessionId);
```

and in the body:

- the reset effect's dependency list becomes `[runId, deviceId, mac?.roomId, session?.sessionId]`;
- `const beatPath = session ? `/messaging/sessions/${encodeURIComponent(session.sessionId)}/browser/viewing` : isMac ? "/browser/frames" : heartbeat;`
- `const beatBody = JSON.stringify(session ? {} : isMac ? { runId, deviceId, tier: "view", roomId: mac?.roomId } : heartbeatBody ?? { frames: true });`
- in `send`: `if (macLike) { macSend(event as MacEvent); return; }` with `macLike` in its dependency list instead of `isMac`;
- in `flush`: `if (batch.text && macLike)` and `[send, macLike]`;
- `const input = !frame ? {} : macLike ? macInput : interactive ? { ... } : {};`
- the image's class: `${macLike ? "touch-none " : ""}`.

Create `apps/frontend/src/components/messaging/SessionBrowserTab.tsx`:

```tsx
"use client";

import { useEffect } from "react";
import type { LocalSession } from "@lightbulb/shared";
import { BrowserFrameView } from "@/components/workspace/panels/BrowserFrameView";
import type { SessionFrames } from "./SessionTerminalView";

/** The Browser tab shows while a running session has an open browser connection, for people watching
 *  it from elsewhere (its owner at that Mac sees the real window). */
export function showsBrowserTab(session: Pick<LocalSession, "browser" | "status">, watchingRemotely: boolean): boolean {
  return watchingRemotely && !!session.browser && session.status !== "ended" && session.status !== "offline";
}

/** A shared session's browser (spec 2026-10-11 §3): the page the session drives, as 480 px frames on
 *  the session's frames channel while this tab is open; people who may type into the session may click
 *  and type in it. Nothing is recorded. */
export function SessionBrowserTab({ sessionId, frames, canType }: { sessionId: string; frames?: SessionFrames; canType: boolean }) {
  useEffect(() => (frames ? frames.claim(frames.channel, () => undefined) : undefined), [frames]);
  return (
    <div className="relative min-h-[320px] flex-1" role="region" aria-label="Session browser">
      <BrowserFrameView session={{ sessionId, canType }}
        empty={<p role="status" className="text-sm text-[var(--fg-mute)]">Waiting for the session&apos;s page…</p>} />
    </div>
  );
}
```

`LocalSessionPanel.tsx` — import `{ SessionBrowserTab, showsBrowserTab } from "./SessionBrowserTab"`; after `const watchRemote = ...` (line 136):

```tsx
  // Terminal | Browser while the session has a browser connection open (spec 2026-10-11).
  const [view, setView] = useState<"terminal" | "browser">("terminal");
  const browserTab = showsBrowserTab(session, local === "none" && !!onEvent);
  const showBrowser = browserTab && view === "browser";
```

and in the JSX, immediately before `{watchRemote && <div className="session-terminal-slot" ...`:

```tsx
      {browserTab && <div className="session-view-tabs" role="tablist" aria-label="Session view">
        <button type="button" role="tab" aria-selected={!showBrowser} onClick={() => setView("terminal")}>Terminal</button>
        <button type="button" role="tab" aria-selected={showBrowser} onClick={() => setView("browser")}>Browser</button>
      </div>}
      {showBrowser && <SessionBrowserTab sessionId={session.sessionId} frames={frames} canType={control && running && session.shared !== false} />}
```

and the terminal slot keeps its terminal mounted (no reconnect, no history burst) while hidden: `<div className="session-terminal-slot" hidden={showBrowser} ...`.

`messaging.css`, after `.session-terminal-slot { display: contents; }`:

```css
/* `display: contents` beats the hidden attribute's UA rule: say it again for the slot. */
.session-terminal-slot[hidden] { display: none; }
/* Terminal | Browser above a shared session's view while it browses (spec 2026-10-11). */
.session-view-tabs { display: flex; flex: none; gap: 4px; padding: 4px 12px; border-bottom: 1px solid var(--border); }
.session-view-tabs button { padding: 2px 8px; border-radius: 6px; font-size: 13px; color: var(--fg-soft); }
.session-view-tabs button[aria-selected="true"] { background: var(--plane-raised); color: var(--fg); }
```

- [ ] **Step 4: Run the tests and the brand scan**

Run: `cd apps/frontend && pnpm vitest run src/components/workspace/panels/__tests__/BrowserFrameView.test.tsx src/components/messaging/__tests__/SessionBrowserTab.test.tsx src/__tests__/brandTokens.test.ts && pnpm exec tsc --noEmit`
Expected: PASS (all the existing room and Mac `BrowserFrameView` tests too), no type errors.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/workspace/panels/BrowserFrameView.tsx apps/frontend/src/components/messaging/SessionBrowserTab.tsx apps/frontend/src/components/messaging/LocalSessionPanel.tsx apps/frontend/src/components/messaging/messaging.css apps/frontend/src/components/workspace/panels/__tests__/BrowserFrameView.test.tsx apps/frontend/src/components/messaging/__tests__/SessionBrowserTab.test.tsx
git commit -m "feat(sessions): a Browser tab beside a shared session's terminal while it browses"
```

### Task 13: Docs, PR and prod acceptance for M2

**Files:**
- Modify: `apps/infra/hermes/skills-src/lightbulb/references/surfaces.md` (the "Session browser" paragraph from Task 9)
- Modify: `apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md` (the "Session browser" anchor)

- [ ] **Step 1: Extend the surfaces paragraph** — append to "**Session browser**":

```markdown
People watching the session see a **Browser** tab beside its terminal while it browses; those
who may type into the session may also click and type there. Frames flow only while someone has
that tab open, and nothing is recorded.
```

- [ ] **Step 2: Extend the anchor** — append to the "Session browser" entry:

```text
                                 apps/frontend/src/components/messaging/SessionBrowserTab.tsx + LocalSessionPanel.tsx (the tab);
                                 BrowserFrameView.tsx (session mode); routes /messaging/sessions/{id}/browser/{viewing,input}
```

- [ ] **Step 3: Build skills, commit, PR**

Run: `cd apps/infra/hermes && ./build-skills.sh`

```bash
git add apps/infra/hermes/skills-src/lightbulb/references/surfaces.md apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md
git commit -m "docs(sessions): the session Browser tab"
git push -u origin feat/session-browser-view
gh pr create --repo TheLightbulbCompany/lightbulb --base main --title "feat(sessions): watch and drive a shared session's browser (M2)" --body-file /dev/stdin <<'BODY'
Milestone M2 of docs/superpowers/plans/2026-10-11-session-browser.md.

- A Browser tab beside a shared session's terminal while it browses; frames (480 px, `workspace.session.screen` kind `image`) only while the tab is open.
- People who may type into the session click and type through /browser/mac/input's delivery.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01N9nLe5RG6ZAEHy5ihTq7gM
BODY
```

Watch CI with `gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status --interval 60`, wait for Codex, merge, promote (`gh workflow run deploy.yml --ref main`). No Hermes image or desktop build is needed for M2.

- [ ] **Step 4: Prod acceptance** (founder's Mac browsing from a shared session, a teammate account on the web)

1. While the session browses, the teammate's session view shows Terminal | Browser; the Browser tab shows the page within a few seconds.
2. With the tab closed for 15 s, the Mac's log shows `[agent-cast] channel …: cast stopped (ttl)`.
3. A teammate the owner let in (Ask to join) clicks a link in the Browser tab: the page follows. A teammate who was not let in gets no response (the route answers 403).
4. When the session's connection closes (5 minutes idle), the Browser tab disappears.

---

# Milestone M3 — the cloud path

Branch: `feat/session-browser-cloud` in `.claude/worktrees/session-browser-cloud`, from `origin/main` after M2 merged.

### Task 14: Bridge — `browser.attach`

**Files:**
- Modify: `apps/infra/hermes/bridge/browser_control.py` (constants after `_MAC_UPSTREAM`; `_SESSION_SINKS` beside `_SINKS` at line 105; new `attach_session`, `_start_pump`, `_pump_door`, `_session_sink`, `session_frames` after `dispatch_input` at line 500)
- Modify: `apps/infra/hermes/bridge/server.py:416-433` (`_browser_frames`; new `_browser_attach`)
- Test: `apps/infra/hermes/tests/test_browser_control.py` (the `clean` fixture resets `_SESSION_SINKS` too), `apps/infra/hermes/tests/test_server_routes.py`

**Interfaces:**
- Consumes: `_refresh`, `begin_turn`, `_TURN`, `_BROWSING`, `_end_run`, `_post`, `LEASE_ID`, `cdp_mux.url_for`, `cdp_mux.cast`, `turn.publish_frame`, `turn.now`.
- Produces: RPC `browser.attach {runId, doorUrl, frameEndpoint, frameChannel}` → `{"status": "attached" | "starting" | "unavailable"}`; RPC `browser.frames {runId, ttl}` casts only that session's tab. Used by Task 16.

- [ ] **Step 1: Write the failing tests**

In the `clean` fixture of `test_browser_control.py`, add `monkeypatch.setattr(browser_control, "_SESSION_SINKS", {})`. Then append:

```python
SESSION_ID = "11111111-1111-4111-8111-111111111111"
SESSION_RUN = f"session-{SESSION_ID}"
FRAMES = {"frameEndpoint": "https://x.appsync-api.us-east-1.amazonaws.com/event",
          "frameChannel": f"/ws/ws-1/session-frames/{SESSION_ID}"}


def test_attach_joins_the_session_to_the_bridges_lease_and_pumps_its_door(monkeypatch, clean):
    posted = []
    monkeypatch.setattr(browser_control, "_post", lambda path, body, timeout=20.0: posted.append(body) or
                        {"generation": "g1", "cdpUrl": URL})
    pumped = []
    monkeypatch.setattr(browser_control, "_start_pump", lambda run, door: pumped.append((run, door)))
    assert browser_control.attach_session({"runId": SESSION_RUN, "doorUrl": DOOR, **FRAMES}) == {"status": "attached"}
    assert posted == [{"runId": "bridge", "agentId": "session"}], "the bridge's one lease, not a per-session one"
    assert SESSION_RUN in browser_control._BROWSING and SESSION_RUN in browser_control._SESSION_SINKS
    assert pumped == [(SESSION_RUN, DOOR)]
    assert browser_control._TURN.get() is None


def test_attach_answers_starting_while_the_cloud_browser_starts(monkeypatch, clean):
    monkeypatch.setattr(browser_control, "HOOK_BUDGET_S", 0)
    monkeypatch.setattr(browser_control, "_post", lambda path, body, timeout=20.0: {"status": "starting"})
    monkeypatch.setattr(browser_control, "_start_pump", lambda run, door: pytest.fail("pumped"))
    assert browser_control.attach_session({"runId": SESSION_RUN, "doorUrl": DOOR, **FRAMES}) == {"status": "starting"}
    assert SESSION_RUN not in browser_control._BROWSING


@pytest.mark.parametrize("bad", [{"runId": "run-1"}, {"doorUrl": "wss://evil.example/x"}])
def test_attach_refuses_anything_but_a_session_run_and_a_backend_door(clean, bad):
    with pytest.raises(ValueError):
        browser_control.attach_session({"runId": SESSION_RUN, "doorUrl": DOOR, **FRAMES} | bad)


def test_a_door_that_will_not_connect_ends_the_session_run(monkeypatch, clean):
    import websockets.sync.client as client

    def refuse(*args, **kwargs):
        raise OSError("refused")

    monkeypatch.setattr(client, "connect", refuse)
    browser_control._BROWSING[SESSION_RUN] = set()
    browser_control._SESSION_SINKS[SESSION_RUN] = print
    browser_control._pump_door(SESSION_RUN, DOOR)
    assert SESSION_RUN not in browser_control._BROWSING and SESSION_RUN not in browser_control._SESSION_SINKS
    assert SESSION_RUN in clean["closed"]


def test_a_browser_tab_beat_casts_only_that_sessions_tab(clean):
    sink = lambda frame: None  # noqa: E731
    browser_control._SESSION_SINKS[SESSION_RUN] = sink
    assert browser_control.session_frames(SESSION_RUN, 45) == 1
    assert clean["casts"] == [(SESSION_RUN, sink, 45, True)]
    assert browser_control.session_frames("session-other", 45) == 0


def test_the_session_sink_publishes_an_image_on_the_sessions_own_channel(monkeypatch):
    from bridge import turn

    sent = []
    monkeypatch.setattr(turn, "publish_frame", lambda endpoint, channel, *events: sent.append((endpoint, channel, events)) or 200)
    sink = browser_control._session_sink(SESSION_RUN, FRAMES["frameEndpoint"], FRAMES["frameChannel"])
    sink({"seq": 3, "jpegBase64": "/9j/", "w": 480, "h": 300})
    ((endpoint, channel, (event,)),) = sent
    assert (endpoint, channel) == (FRAMES["frameEndpoint"], FRAMES["frameChannel"])
    assert event["type"] == "workspace.session.screen"
    assert {**event["payload"], "seq": 0} == {"sessionId": SESSION_ID, "kind": "image", "data": "/9j/", "w": 480, "h": 300, "seq": 0}
    assert event["payload"]["seq"] > 1_700_000_000_000, "epoch ms: a restarted cast never goes backwards"
    assert browser_control._session_sink(SESSION_RUN, FRAMES["frameEndpoint"], "/ws/ws-1/session-frames/other") is None
```

Append to `test_server_routes.py`:

```python
def test_browser_attach_and_a_sessions_frames_beat(monkeypatch):
    monkeypatch.setattr(server.browser_control, "attach_session", lambda params: {"status": "attached"})
    monkeypatch.setattr(server.browser_control, "session_frames", lambda run, ttl: 1)
    rpc = "/plugins/lightbulb-channel/rpc"
    assert server.handle_plugin(rpc, "POST", AUTH, {"method": "browser.attach", "params": {"runId": "x"}}) == \
        (200, {"ok": True, "payload": {"status": "attached"}})
    assert server.handle_plugin(rpc, "POST", AUTH, {"method": "browser.frames", "params": {"runId": "session-x", "ttl": 45}}) == \
        (200, {"ok": True, "payload": {"runs": 1}})
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/infra/hermes && uv run --no-project --with pytest --with botocore --with pyyaml --with websockets python -m pytest tests/test_browser_control.py tests/test_server_routes.py -q`
Expected: FAIL (`attach_session` does not exist).

- [ ] **Step 3: Implement**

`browser_control.py` constants (after `_MAC_UPSTREAM`):

```python
#: A shared Claude Code or Codex session's run (backend session_browser.run_id).
_SESSION_RUN = re.compile(r"session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: Who holds the bridge's lease on a session's behalf (the panel names no agent for it).
AGENT_SESSION = "session"
#: Renew the bridge's lease this often while a session's commands flow (the backend's lease is 300 s).
SESSION_RENEW_S = 120.0
```

beside `_SINKS`:

```python
_SESSION_SINKS: dict = {}            # session run id -> publish(frame) to its session frames channel
```

after `dispatch_input`:

```python
def attach_session(params: dict) -> dict:
    """``browser.attach`` (spec 2026-10-11, cloud): a shared Claude Code or Codex session browses the
    workspace's cloud browser as one more run, in its own tabs. The backend hands a single-use
    ticketed door carrying the session's CDP from its Mac; this dials it and pumps it into the run's
    own loopback endpoint until either side closes. "starting" while the cloud browser starts: the
    backend asks again with a fresh ticket."""
    run, door = params.get("runId"), params.get("doorUrl")
    if not (isinstance(run, str) and _SESSION_RUN.fullmatch(run)) or not (isinstance(door, str) and _DOOR.fullmatch(door)):
        raise ValueError("invalid browser attach")
    state = begin_turn(run, AGENT_SESSION, family="session")
    try:
        ready = _refresh(state)
    finally:
        _TURN.set(None)
    if not ready:
        return {"status": "starting" if state.get("starting") else "unavailable"}
    sink = _session_sink(run, params.get("frameEndpoint"), params.get("frameChannel"))
    with _LOCK:
        _BROWSING.setdefault(run, set())  # the bridge's lease and grace timer cover it
        if sink is not None:
            _SESSION_SINKS[run] = sink
    _start_pump(run, door)
    return {"status": "attached"}


def _start_pump(run: str, door: str) -> None:
    threading.Thread(target=_pump_door, args=(run, door), name="session-door", daemon=True).start()


def _pump_door(run: str, door: str) -> None:
    """The door <-> the run's loopback endpoint, both ways, until either side closes; the bridge's
    lease is renewed every SESSION_RENEW_S while commands flow. Then the run ends: its tabs close and
    the lease's grace timer saves the browser once nothing else browses."""
    from websockets.sync.client import connect

    renewed = [time.monotonic()]

    def copy(source, target, renew: bool) -> None:
        try:
            for message in source:
                target.send(message)
                if renew and time.monotonic() - renewed[0] >= SESSION_RENEW_S:
                    renewed[0] = time.monotonic()
                    threading.Thread(target=_post, args=("acquire", {"runId": LEASE_ID, "agentId": AGENT_SESSION}),
                                     daemon=True).start()
        except Exception:  # noqa: BLE001 -- either side closing ends the pump
            pass
        finally:
            for ws in (source, target):
                with contextlib.suppress(Exception):
                    ws.close()

    try:
        with connect(door, max_size=None, open_timeout=10) as outer, \
                connect(cdp_mux.url_for(run), max_size=None, open_timeout=10) as inner:
            back = threading.Thread(target=copy, args=(inner, outer, False), name="session-door-back", daemon=True)
            back.start()
            copy(outer, inner, True)
            back.join(5)
    except Exception as exc:  # noqa: BLE001 -- the class only: the door URL is a capability
        log.info("session door ended: %s", type(exc).__name__)
    finally:
        with _LOCK:
            _SESSION_SINKS.pop(run, None)
        _end_run(run)


def _session_sink(run: str, endpoint, channel):
    """publish(frame) of the session's tab to its own frames channel, as `workspace.session.screen`
    kind "image" (the Mac path's shape; seq in epoch ms like a Mac cast, so a restarted cast never goes
    backwards). None unless `channel` is that session's frames channel."""
    sid = run.removeprefix("session-")
    if not (isinstance(endpoint, str) and endpoint.startswith("https://") and isinstance(channel, str)
            and channel.startswith("/ws/") and channel.endswith(f"/session-frames/{sid}")):
        return None

    def publish(frame: dict) -> None:
        from bridge import turn  # it imports this module

        turn.publish_frame(endpoint, channel, {
            "schemaVersion": 1, "eventId": str(uuid.uuid4()), "type": "workspace.session.screen", "occurredAt": turn.now(),
            "payload": {"sessionId": sid, "kind": "image", "data": frame["jpegBase64"], "w": frame["w"], "h": frame["h"],
                        "seq": time.time_ns() // 1_000_000}})

    return publish


def session_frames(run_id: str, ttl: float) -> int:
    """A Browser tab's beat for a session browsing the cloud: cast its tab for ``ttl`` more seconds
    and resend the latest frame. 0 when it is not browsing here. Room runs' casts are untouched."""
    with _LOCK:
        sink = _SESSION_SINKS.get(run_id)
    if sink is None:
        return 0
    cdp_mux.cast(run_id, sink, min(max(ttl, 0.0), 120.0), True)
    return 1
```

`server.py`:

```python
def _browser_frames(params: dict, **_kwargs) -> dict:
    """A live-frame watcher's heartbeat (backend, while an agent browses); with a session's `runId`,
    that session's Browser tab only."""
    params = params or {}
    ttl = float(params.get("ttl") or browser_control.FRAMES_TTL_S)
    if params.get("runId"):
        return {"ok": True, "payload": {"runs": browser_control.session_frames(str(params["runId"]), ttl)}}
    return {"ok": True, "payload": {"runs": browser_control.frames_wanted(ttl)}}


def _browser_attach(params: dict, **_kwargs) -> dict:
    """A shared session's CDP onto the cloud browser (browser_control.attach_session)."""
    try:
        return {"ok": True, "payload": browser_control.attach_session(params or {})}
    except ValueError:
        return {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "invalid browser attach"}}


RPC_METHODS["browser.frames"] = _browser_frames
RPC_METHODS["browser.input"] = _browser_input
RPC_METHODS["browser.attach"] = _browser_attach
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/infra/hermes && uv run --no-project --with pytest --with botocore --with pyyaml --with websockets python -m pytest tests/test_browser_control.py tests/test_server_routes.py tests/test_cdp_mux.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/hermes/bridge/browser_control.py apps/infra/hermes/bridge/server.py apps/infra/hermes/tests/test_browser_control.py apps/infra/hermes/tests/test_server_routes.py
git commit -m "feat(bridge): browser.attach puts a shared session on the cloud browser as one more run"
```

### Task 15: Backend — the `use: "session"` door

**Files:**
- Modify: `apps/backend/core/services/mac_cdp.py` (`mint` lines 67-86; `_well_formed` lines 93-106; `check` lines 142-166; `serve` lines 255-272)
- Test: `apps/backend/tests/unit/services/test_mac_cdp.py`, `apps/backend/tests/unit/routers/test_mac_cdp_door.py`

**Interfaces:**
- Consumes: Task 2 (`cdp_accept`), Task 16's `session_browser.relay(claims)` (a stub returning None is enough until Task 16; the test patches it).
- Produces: `mac_cdp.mint(..., use: str = "agent")`; a `use: "session"` ticket redeems only for the session family, needs no browser grant or Mac profile, and relays the offered channel (`browser.cdp.open {relay: true}`).

- [ ] **Step 1: Write the failing tests**

Append to `test_mac_cdp.py` (it has `SESSION` from Task 3):

```python
def test_a_relay_ticket_is_for_the_session_family_only():
    claims = mac_cdp.redeem(mac_cdp.mint(target=TARGET, profile_id="cloud", auth=SESSION, use="session"))
    assert claims["use"] == "session" and mac_cdp.auth_from_claims(claims) == SESSION
    with pytest.raises(mac_cdp.TicketError):
        mac_cdp.redeem(mac_cdp.mint(target=TARGET, profile_id="cloud", auth=ROOM, use="session"))


async def test_a_relay_needs_no_browser_grant_and_no_mac_profile(monkeypatch):
    seen = {}

    async def resolve(owner, member, device, generation, command):
        seen["command"] = command
        return TARGET

    monkeypatch.setattr(mac_cdp.connection_service, "resolve_desktop_target", resolve)
    monkeypatch.setattr(mac_cdp.desktop_execution, "submissions_enabled", lambda owner: True)
    monkeypatch.setattr(mac_cdp.desktop_execution, "_reauthorize", AsyncMock())
    from core.services import mac_browser

    monkeypatch.setattr(mac_browser, "check_holder", AsyncMock(side_effect=AssertionError("no Mac profile is involved")))
    claims = mac_cdp.redeem(mac_cdp.mint(target=TARGET, profile_id="cloud", auth=SESSION, use="session"))
    assert await mac_cdp.check(claims) == TARGET and seen["command"] is None
```

Append to `test_mac_cdp_door.py`:

```python
SESSION = {"ownerId": "org_1", "memberId": "user_1", "runId": "session-11111111-1111-4111-8111-111111111111",
           "agentId": "session", "family": "session", "roomId": ""}


def test_a_session_ticket_relays_the_offered_channel_to_the_runtime(mac, monkeypatch):
    from core.services import session_browser

    accepted, made = [], {}

    async def cdp_accept(channel, payload):
        accepted.append(payload)

    mac.cdp_accept = cdp_accept
    monkeypatch.setattr(session_browser, "relay",
                        lambda claims: made.setdefault("channel", FakeChannel()) if claims["runId"] == SESSION["runId"] else None)
    url = "/api/v1/internal/mac-cdp/" + mac_cdp.mint(target=TARGET, profile_id="cloud", auth=SESSION, use="session")
    with _client().websocket_connect(url) as ws:
        ws.send_text('{"id":1,"method":"Target.getTargets"}')
        assert ws.receive_text() == 'echo:{"id":1,"method":"Target.getTargets"}'
    assert accepted == [{"relay": True}] and mac.opened_with == [], "the Mac's Chrome is never opened"


def test_a_session_ticket_with_no_offered_channel_closes_4404(mac, monkeypatch):
    from core.services import session_browser

    monkeypatch.setattr(session_browser, "relay", lambda claims: None)
    url = "/api/v1/internal/mac-cdp/" + mac_cdp.mint(target=TARGET, profile_id="cloud", auth=SESSION, use="session")
    assert _closed_with(url)[0] == 4404
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_mac_cdp.py tests/unit/routers/test_mac_cdp_door.py -v`
Expected: FAIL (`mint() got an unexpected keyword argument 'use'`).

- [ ] **Step 3: Implement**

`mint` takes the use and puts it in the claims:

```python
def mint(*, target: dict, profile_id: str, auth: dict, use: str = "agent") -> str:
    """`target` is the NATIVE device's resolve_desktop_target() dict; `auth` is exactly what
    authorize_run returned. Room `memberId` is the run's human (== the pairer); Paperclip's is ""
    and is re-derived from the pairing on every check. `use` "session": a shared session's relay
    to the cloud browser (session_browser): the door joins the runtime to the channel its Mac offered."""
    if not valid_auth(auth) or auth["ownerId"] != target["ownerId"]:
        raise ValueError("mac-cdp ticket auth invalid")
    now = int(_clock())
    claims = {
        "purpose": PURPOSE,
        "use": use,
        "ownerId": target["ownerId"],
        "deviceId": target["deviceId"],
        "generation": target["generation"],
        "address": target["taskAddress"],
        "profileId": profile_id,
        "nonce": uuid.uuid4().hex,
        "iat": now,
        "exp": now + TICKET_SECONDS,
    } | {field: auth.get(field, "") for field in _RUN_FIELDS}
    return _cipher().encrypt(json.dumps(claims, separators=(",", ":")).encode()).decode()
```

`_well_formed`:

```python
    use, family = claims["use"], claims["family"]
    return (
        ((use == "agent" and family in {"room", "paperclip", "call", "session"}) or (use == "session" and family == "session"))
        and valid_auth(auth_from_claims(claims))
    )
```

`check`:

```python
    relay = claims["use"] == "session"
    try:
        # A relay to the cloud browser drives nothing on the Mac: no browser grant, no profile.
        target = await connection_service.resolve_desktop_target(
            owner, member, claims["deviceId"], claims["generation"], None if relay else CDP_COMMAND
        )
    except connection_service.DesktopTargetError as exc:
        raise DesktopError(exc.code, 409 if exc.code == "desktop_generation_stale" else 403) from None
    if target["taskAddress"] != claims["address"]:
        raise DesktopError("desktop_task_moved", 409)
    await desktop_execution._reauthorize(auth)
    if relay:
        return target
    from core.services import mac_browser  # it imports this module

    await mac_browser.check_holder(owner, claims["deviceId"], claims["profileId"])
    return target
```

In `serve`, the channel comes from the offer for a relay:

```python
        from core.services import mac_browser

        try:
            if claims["use"] == "session":
                from core.services import session_browser  # it imports this module

                channel = session_browser.relay(claims)
                if channel is None:
                    _meter("lost")
                    return await _close(websocket, 4404)
                await session.cdp_accept(channel, {"relay": True})
            else:
                channel = await session.cdp_open(claims["profileId"], await mac_browser.notice(claims))
        except CdpClosed as exc:
            _meter("refused")
            return await _close(websocket, 4502, exc.code)
        except NodeCapacity:
            _meter("refused")
            return await _close(websocket, 4503, "CAPACITY")
        except CommandNotAllowed:
            _meter("forbidden")
            return await _close(websocket, 4403)
```

(`mac_live.run` returns at once for the session family; the pumps and re-check run unchanged; `finally` closes the relay channel, which ends the session's connection on its Mac.)

Add to `session_browser.py` now (Task 16 fills it):

```python
_relays: dict[tuple[str, str], object] = {}  # (owner, run) -> the offered channel its door relays


def relay(claims: dict):
    """The door's half (mac_cdp, `use: "session"`): the channel this ticket's session offered, keyed by
    its own workspace and run, so another workspace's ticket never finds it."""
    return _relays.get((claims["ownerId"], claims["runId"]))
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_mac_cdp.py tests/unit/routers/test_mac_cdp_door.py tests/unit/services/test_mac_browser.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/mac_cdp.py apps/backend/core/services/session_browser.py apps/backend/tests/unit/services/test_mac_cdp.py apps/backend/tests/unit/routers/test_mac_cdp_door.py
git commit -m "feat(mac-cdp): a session door relays a shared session's offered channel to the runtime"
```

### Task 16: Backend — routing a session to the cloud browser

**Files:**
- Modify: `apps/backend/core/services/session_browser.py` (`serve`; new `_native_target`, `_attach`, `_hold_cloud`, `_watch_cloud`, `_serve_cloud`, `cloud_events`, `cloud_input`; `MESSAGES`)
- Modify: `apps/backend/routers/messaging.py` (`session_browser_input`: a cloud branch)
- Test: `apps/backend/tests/unit/services/test_session_browser.py`, `apps/backend/tests/unit/routers/test_session_browser_routes.py`

**Interfaces:**
- Consumes: Task 14 RPCs, Task 15 (`mint(use="session")`, `relay`), `connection_service.resolve_desktop_target`, backend `browser_control.send_input(owner, event)`, `settings.APPSYNC_EVENTS_HTTP_ENDPOINT`.
- Produces: the place rule — Mac when the Mac's Browser is on, the pairing declares `browser.cdp.v1` and `browserPlace` is not `"cloud"`; else cloud. `cloud_unavailable` with `CLOUD_MESSAGE`. Used by Task 19 (`browserPlace`).

- [ ] **Step 1: Write the failing tests**

In `test_session_browser.py`, delete `test_browser_off_is_refused` (Browser off now goes to the cloud) and append:

```python
@pytest.fixture
def cloud(env, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "WS_INTERNAL_SECRET", "s3cret")
    monkeypatch.setattr(settings, "APPSYNC_EVENTS_HTTP_ENDPOINT", "https://x.appsync-api.us-east-1.amazonaws.com/event")
    target = {"taskAddress": "http://10.0.3.7:8000", "ownerId": OWNER, "memberId": MEMBER, "deviceId": DEVICE,
              "generation": "g1", "commands": []}
    monkeypatch.setattr(session_browser, "_native_target", AsyncMock(return_value=target))
    monkeypatch.setattr(session_browser.store, "ensure_workspace_id", AsyncMock(return_value="ws-1"))
    answers = [{"status": "starting"}, {"status": "attached"}]
    seen = []

    async def rpc(owner, method, params=None, *, timeout_s=30.0):
        seen.append((method, params))
        return answers.pop(0) if method == "browser.attach" else {"ok": True}

    monkeypatch.setattr(session_browser.container_rpc, "call", rpc)
    return seen


async def _relayed(env):
    for _ in range(200):
        if session_browser.relay({"ownerId": OWNER, "runId": RUN}) is not None and ("open", "cloud") in env.calls:
            return
        await asyncio.sleep(0.01)


async def test_browser_off_routes_the_session_to_the_cloud_browser(env, cloud):
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False))
    await _relayed(env)
    assert session_browser.relay({"ownerId": OWNER, "runId": RUN}) is channel
    attaches = [params for method, params in cloud if method == "browser.attach"]
    assert len(attaches) == 2 and attaches[0]["doorUrl"] != attaches[1]["doorUrl"], "a fresh ticket each time"
    assert attaches[1]["doorUrl"].startswith("ws://10.0.3.7:8000/api/v1/internal/mac-cdp/")
    assert (attaches[1]["runId"], attaches[1]["frameChannel"]) == (RUN, f"/ws/ws-1/session-frames/{SID}")
    assert not any(call[0] in ("acquire", "release") for call in env.calls), "the Mac browser is never leased"
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)
    assert session_browser.relay({"ownerId": OWNER, "runId": RUN}) is None
    assert env.calls[-1] == ("open", None)


async def test_a_session_set_to_cloud_goes_there_with_browser_on(env, cloud):
    env.row["browserPlace"] = "cloud"
    node, channel = _offered()
    served = asyncio.create_task(session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, True))
    await _relayed(env)
    assert any(method == "browser.attach" for method, _ in cloud)
    _closed_by_mac(node)
    await asyncio.wait_for(served, 2)


async def test_a_cloud_that_does_not_start_is_cloud_unavailable(env, cloud, monkeypatch):
    monkeypatch.setattr(session_browser, "OFFER_BUDGET_S", 0.05)
    cloud_answers = iter(lambda: {"status": "starting"}, None)

    async def rpc(owner, method, params=None, *, timeout_s=30.0):
        return next(cloud_answers)

    monkeypatch.setattr(session_browser.container_rpc, "call", rpc)
    node, channel = _offered()
    await session_browser.serve(OWNER, MEMBER, DEVICE, node, channel, SID, False)
    assert _out(node)[-1]["payload"] == {"channel": CHANNEL, "code": "cloud_unavailable", "message": session_browser.CLOUD_MESSAGE}
    assert session_browser.relay({"ownerId": OWNER, "runId": RUN}) is None


def test_another_workspaces_ticket_never_finds_the_relay():
    channel = object()
    session_browser._relays[(OWNER, RUN)] = channel
    try:
        assert session_browser.relay({"ownerId": OWNER, "runId": RUN}) is channel
        assert session_browser.relay({"ownerId": "org_2", "runId": RUN}) is None
    finally:
        session_browser._relays.clear()


def test_cloud_input_is_the_cloud_casts_own_shape():
    events = [{"type": "down", "x": 1.0, "y": 2.0, "button": "left", "clicks": 1},
              {"type": "up", "x": 1.0, "y": 2.0, "button": "left", "clicks": 1},
              {"type": "move", "x": 3.0, "y": 4.0}, {"type": "wheel", "x": 1.0, "y": 1.0, "deltaY": -120.0},
              {"type": "key", "key": "Enter", "modifiers": 0}, {"type": "paste", "text": "hi"}]
    assert session_browser.cloud_events(events) == [
        {"type": "click", "x": 1.0, "y": 2.0, "button": "left"},
        {"type": "wheel", "x": 1.0, "y": 1.0, "deltaY": -120.0},
        {"type": "key", "key": "Enter"},
        {"type": "text", "text": "hi"},
    ]
```

Append to `test_session_browser_routes.py`:

```python
def test_input_on_a_cloud_browser_goes_to_the_cloud_cast(client, monkeypatch):
    monkeypatch.setattr(router.local_sessions, "authorize_session",
                        AsyncMock(return_value=({**ROW, "browserOpen": {"where": "cloud", "until": 2**40}}, {})))
    sent = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(router.session_browser.browser_control, "send_input", sent)
    up = {"type": "up", "x": 10, "y": 20, "button": "left", "clicks": 1}
    assert client.post(f"/api/v1/messaging/sessions/{SID}/browser/input", json={"events": [CLICK, up]}).status_code == 204
    sent.assert_awaited_once_with("org_1", {"runId": f"session-{SID}", "type": "click", "x": 10.0, "y": 20.0, "button": "left"})
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_browser.py tests/unit/routers/test_session_browser_routes.py -v`
Expected: FAIL (`_native_target`, `CLOUD_MESSAGE`, `cloud_events` missing; Browser off still refused).

- [ ] **Step 3: Implement**

`session_browser.py` — imports add `from core.config import settings` and `browser_control, mac_cdp` to the `core.services` import; constants:

```python
CLOUD_MESSAGE = "The workspace's cloud browser isn't available right now. Try again in a minute."
MESSAGES = {"not_shared": NOT_SHARED, "cloud_unavailable": CLOUD_MESSAGE}
```

`serve` picks the place:

```python
    try:
        row = await shared_row(owner, user, device, session_id)
        on_mac = mac_browser_on and CDP_COMMAND in node.commands and row.get("browserPlace") != "cloud"
        await (_serve_mac if on_mac else _serve_cloud)(auth, device, node, channel)
    except DesktopError as exc:
        # ... unchanged
```

and add:

```python
async def _native_target(auth: dict, device: str, node) -> dict:
    """The Mac's live socket here, with no browser grant needed: a relay drives nothing on the Mac."""
    from core.services import connection_service

    try:
        return await connection_service.resolve_desktop_target(auth["ownerId"], auth["memberId"], device, node.generation, None)
    except connection_service.DesktopTargetError:
        raise _err("cloud_unavailable") from None


async def _attach(auth: dict, target: dict) -> None:
    """`browser.attach` until the runtime dials the door: a fresh single-use ticket each time (one lives
    60 s), polling while the cloud browser starts. cloud_unavailable past OFFER_BUDGET_S, or on any
    other answer."""
    owner, sid = auth["ownerId"], session_of(auth["runId"])
    workspace = await store.ensure_workspace_id(owner)
    deadline = _clock() + OFFER_BUDGET_S
    while True:
        ticket = mac_cdp.mint(target=target, profile_id="cloud", auth=auth, use="session")
        try:
            answer = await container_rpc.call(
                owner,
                "browser.attach",
                {
                    "runId": auth["runId"],
                    "doorUrl": mac_cdp.door_url(target, ticket),
                    "frameEndpoint": settings.APPSYNC_EVENTS_HTTP_ENDPOINT,
                    "frameChannel": f"/ws/{workspace}/session-frames/{sid}",
                },
                timeout_s=30,
            )
        except Exception as exc:  # noqa: BLE001 -- the runtime did not answer
            logger.warning("session browser attach failed owner=%s error=%s", owner, type(exc).__name__)
            raise _err("cloud_unavailable") from None
        status = (answer or {}).get("status") if isinstance(answer, dict) else None
        if status == "attached":
            return
        if status != "starting" or _clock() + POLL_S >= deadline:
            raise _err("cloud_unavailable")
        await asyncio.sleep(POLL_S)


async def _watch_cloud(owner: str, session_id: str, run: str) -> None:
    """While someone has the Browser tab open, ask the runtime to cast the session's tab every
    SET_EVERY_S (`browser.frames` with its run); the cast stops by itself after."""
    while True:
        try:
            row = await repo.get_session(owner, session_id)
            if row and _clock() - float(row.get("browserWatchedAt") or 0) < mac_live.WANTED_S:
                await container_rpc.call(owner, "browser.frames", {"runId": run, "ttl": mac_live.WANTED_S}, timeout_s=10)
        except Exception as exc:  # noqa: BLE001 -- the next beat tries again
            logger.info("session browser frames ask failed owner=%s error=%s", owner, type(exc).__name__)
        await asyncio.sleep(mac_live.SET_EVERY_S)


async def _hold_cloud(auth: dict, device: str, channel) -> None:
    """While the door relays: every RENEW_SECONDS re-check the session is shared, keep the runtime
    awake (AgentCore parks a microVM no invoke reaches; the door is outbound and does not count) and
    renew the `browserOpen` mark. The bridge renews its own lease while commands flow."""
    owner, user, sid = auth["ownerId"], auth["memberId"], session_of(auth["runId"])
    watcher = asyncio.create_task(_watch_cloud(owner, sid, auth["runId"]))
    try:
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(channel.closed.wait(), RENEW_SECONDS)
            if channel.closed.is_set():
                return
            await shared_row(owner, user, device, sid)
            with contextlib.suppress(Exception):
                await container_rpc.call(owner, "health", {}, timeout_s=10)
            await _mark_open(owner, user, sid, "cloud", announce=False)
    finally:
        watcher.cancel()


async def _serve_cloud(auth: dict, device: str, node, channel) -> None:
    owner, user, sid = auth["ownerId"], auth["memberId"], session_of(auth["runId"])
    target = await _native_target(auth, device, node)
    key = (owner, auth["runId"])
    _relays[key] = channel
    try:
        await _attach(auth, target)
        await _mark_open(owner, user, sid, "cloud", announce=True)
        try:
            await _hold_cloud(auth, device, channel)
        finally:
            await _mark_open(owner, user, sid, None, announce=True)
    finally:
        if _relays.get(key) is channel:
            del _relays[key]


def cloud_events(events: list[dict]) -> list[dict]:
    """The Mac's input shape -> the cloud cast's (bridge cdp_mux._dispatch): a press and release is one
    click at the release; moves are dropped. ponytail: no cloud drags; add a pointer API there if wanted."""
    out = []
    for event in events:
        kind = event["type"]
        if kind == "up":
            out.append({"type": "click", "x": event["x"], "y": event["y"], "button": event.get("button", "left")})
        elif kind == "wheel":
            out.append({"type": "wheel", "x": event["x"], "y": event["y"], "deltaY": event["deltaY"]})
        elif kind == "key":
            out.append({"type": "key", "key": event["key"]})
        elif kind in ("text", "paste"):
            out.append({"type": "text", "text": event["text"]})
    return out


async def cloud_input(row: dict, events: list[dict]) -> None:
    """A person's input on a session browsing the cloud: into its cast tab, as the cloud's own watcher
    input (it counts as a person's input there too)."""
    for event in cloud_events(events):
        await browser_control.send_input(row["ownerId"], {"runId": run_id(row["sessionId"]), **event})
```

`routers/messaging.py` `session_browser_input` — right after `events = mac_live.dump_events(body.events)`:

```python
    if local_sessions.browser_open(row) == "cloud":
        from core.services.browser_control import BrowserError

        try:
            await session_browser.cloud_input(row, events)
        except BrowserError as exc:
            raise HTTPException(409, detail={"code": "browser_not_casting", "message": str(exc)}) from None
        return
```

(`browser_control` is the backend module `core.services.browser_control`; `BrowserError(message)` is how it refuses.)

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_browser.py tests/unit/routers/test_session_browser_routes.py tests/unit/services/test_mac_cdp.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/session_browser.py apps/backend/routers/messaging.py apps/backend/tests/unit/services/test_session_browser.py apps/backend/tests/unit/routers/test_session_browser_routes.py
git commit -m "feat(sessions): a shared session browses the cloud browser when its Mac's is off or it chose the cloud"
```

### Task 17: Desktop — relaying a session to the cloud

**Files:**
- Modify: `apps/desktop/src-tauri/src/session_browser.rs` (`Decision::Relay`, `RELAYS`, `Upstream`, `on_event`, `run`)
- Test: same file

**Interfaces:**
- Consumes: Task 7; `agent_chrome::envelope(channel, data)`.
- Produces: `browser.cdp.open {channel, relay: true}` makes the connection a relay: the client's commands go up as `browser.cdp.frame`, the backend's frames for that channel come down to it, and a backend close ends it.

- [ ] **Step 1: Write the failing tests**

```rust
    #[tokio::test]
    async fn a_cloud_answer_relays_the_session_through_the_node_socket() {
        let (channels, _rx) = crate::agent_chrome::test_channels();
        let (to_client, mut client) = mpsc::channel(8);
        let (decide, decided) = oneshot::channel();
        lock(&PENDING).insert("r1".into(), Pending { to_client, decide });
        assert!(on_event(&channels, "browser.cdp.open", &json!({"channel": "r1", "relay": true})));
        assert!(matches!(decided.await, Ok(Decision::Relay)));
        assert!(!channels.end_local("r1"), "no Chrome channel was opened");
        assert!(on_event(&channels, "browser.cdp.frame", &json!({"channel": "r1", "data": "{\"id\":1,\"result\":{}}"})));
        assert_eq!(client.recv().await.as_deref(), Some("{\"id\":1,\"result\":{}}"));
        assert!(on_event(&channels, "browser.cdp.close", &json!({"channel": "r1", "code": "CLOSED"})));
        assert!(client.recv().await.is_none(), "the backend closed it: the connection ends");
    }

    #[tokio::test]
    async fn a_relayed_command_goes_up_as_a_cdp_frame() {
        let (channels, mut rx) = crate::agent_chrome::test_channels();
        Upstream::Relay(channels.out()).send("r2", "{\"id\":1}".into()).await.unwrap();
        let Some(Message::Text(text)) = rx.recv().await else { panic!("a frame") };
        let v: Value = serde_json::from_str(&text).unwrap();
        assert_eq!((v["event"].as_str(), v["payload"]["channel"].as_str(), v["payload"]["data"].as_str()),
            (Some("browser.cdp.frame"), Some("r2"), Some("{\"id\":1}")));
    }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/desktop/src-tauri && cargo test session_browser`
Expected: FAIL to compile (`no variant Relay`, `Upstream` not found).

- [ ] **Step 3: Implement**

```rust
pub(crate) enum Decision {
    Chrome(mpsc::Sender<String>),
    /// The workspace's cloud browser: the backend joins this channel to the runtime's door.
    Relay,
    Refused(String),
}

/// Relayed connections: channel -> its client (the backend's frames go there).
static RELAYS: LazyLock<Mutex<HashMap<String, mpsc::Sender<String>>>> = LazyLock::new(Default::default);

/// Where a connection's commands go: this Mac's agent Chrome, or up the node socket to the cloud.
enum Upstream {
    Chrome(mpsc::Sender<String>),
    Relay(EventSender),
}

impl Upstream {
    async fn send(&self, id: &str, text: String) -> Result<(), ()> {
        match self {
            Upstream::Chrome(tx) => tx.send(text).await.map_err(|_| ()),
            Upstream::Relay(out) => {
                let frame = crate::agent_chrome::envelope(id, &text).map_err(|_| ())?;
                out.send_text(frame).await.map_err(|_| ())
            }
        }
    }
}
```

In `on_event`, `"browser.cdp.open"` checks for a relay before opening Chrome:

```rust
        "browser.cdp.open" => {
            let Some(pending) = lock(&PENDING).remove(id) else { return false };
            if payload.get("relay").and_then(Value::as_bool) == Some(true) {
                lock(&RELAYS).insert(id.to_string(), pending.to_client);
                let _ = pending.decide.send(Decision::Relay);
                return true;
            }
            // ... the Chrome branch, unchanged
        }
        "browser.cdp.frame" => {
            let client = lock(&RELAYS).get(id).cloned();
            let Some(client) = client else { return false };
            let data = payload.get("data").and_then(Value::as_str).unwrap_or("");
            if client.try_send(data.to_string()).is_err() {
                lock(&RELAYS).remove(id); // a stalled client: its connection ends
            }
            true
        }
        "browser.cdp.close" => {
            if let Some(pending) = lock(&PENDING).remove(id) {
                // ... the refusal, unchanged
                return true;
            }
            lock(&RELAYS).remove(id).is_some()
        }
```

In `run`, the decision becomes an `Upstream`, and the end of the connection is the upstream's:

```rust
    let upstream = loop {
        tokio::select! {
            answer = &mut decided => match answer {
                Ok(Ok(Decision::Chrome(tx))) => break Upstream::Chrome(tx),
                Ok(Ok(Decision::Relay)) => break Upstream::Relay(out.clone()),
                // ... Refused and the timeout, unchanged
            },
            // ... the early-command branch, unchanged
        }
    };
    // ...
    for text in early {
        open = open && upstream.send(&id, text).await.is_ok();
    }
    // ... in the loop: `open = upstream.send(&id, text.to_string()).await.is_ok();`
    // after the loop:
    lock(&OPEN).remove(&id);
    match upstream {
        Upstream::Chrome(_) => {
            channels.end_local(&id);
        }
        Upstream::Relay(out) => {
            if lock(&RELAYS).remove(&id).is_some() {
                let _ = out.send("browser.cdp.close", json!({"channel": id, "code": "CLOSED"})).await;
            }
        }
    }
    let _ = sink.close().await;
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/desktop/src-tauri && cargo test session_browser`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src/session_browser.rs
git commit -m "feat(desktop): relay a shared session's CDP to the workspace cloud browser"
```

### Task 18: Docs, PR and prod acceptance for M3

**Files:**
- Modify: `packages/claude-plugin/lightbulb/skills/lightbulb-session-sharing/SKILL.md` ("## Browsing")
- Modify: `apps/infra/hermes/skills-src/lightbulb/references/surfaces.md` ("Session browser"), `MAINTENANCE.md` (anchor)

- [ ] **Step 1: Docs** — append to the skill's "## Browsing":

```markdown
When browser access is off on the Mac, the same tools drive the workspace's cloud browser
instead, in a tab of their own; "The workspace's cloud browser isn't available right now"
means it could not start, so try again in a minute.
```

and to the surfaces paragraph:

```markdown
When Browser is switched off on that Mac, the session browses the workspace's cloud browser
instead (its own tab, the same sign-ins; saved when the shared browser saves).
```

and to the anchor: `apps/infra/hermes/bridge/browser_control.py (attach_session: the cloud path); apps/backend/core/services/mac_cdp.py (use "session" door)`.

- [ ] **Step 2: Build skills, commit, PR, ship**

Run: `cd apps/infra/hermes && ./build-skills.sh`

```bash
git add packages/claude-plugin/lightbulb/skills/lightbulb-session-sharing/SKILL.md apps/infra/hermes/skills-src/lightbulb/references/surfaces.md apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md
git commit -m "docs(sessions): a session browses the cloud when its Mac's browser is off"
git push -u origin feat/session-browser-cloud
gh pr create --repo TheLightbulbCompany/lightbulb --base main --title "feat(sessions): shared sessions fall back to the workspace cloud browser (M3)" --body-file /dev/stdin <<'BODY'
Milestone M3 of docs/superpowers/plans/2026-10-11-session-browser.md.

- Browser off (or a session set to the cloud): the backend mints a `use: "session"` door ticket; `browser.attach` makes the bridge dial it and pump it into its cdp_mux as run `session-<id>`.
- Frames from the bridge's cast to the session's frames channel; input through the cloud cast.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01N9nLe5RG6ZAEHy5ihTq7gM
BODY
```

After CI and Codex: merge; ship the Hermes image (dev pin, prod pin, `roll_hermes_runtimes.py`), promote the backend, build the desktop app (as in Task 9 Step 6).

- [ ] **Step 3: Prod acceptance**

1. Switch Browser off in the founder's Mac Settings. In a shared Claude Code session: "Use lightbulb-browser to open https://example.com and take a snapshot." It works; the workspace's Browser panel shows a new tab with example.com; the Mac's agent Chrome is not opened.
2. The Browser tab in a teammate's session view shows the cloud page; a click from a let-in teammate lands.
3. After 5 minutes idle the connection closes; ~30 s later the backend log shows the cloud browser's save for the owner.
4. Switch Browser back on: the next connection opens on the Mac again.

---

# Milestone M4 — `use_browser`

Branch: `feat/session-browser-switch` in `.claude/worktrees/session-browser-switch`, from `origin/main` after M3 merged.

### Task 19: Backend — the session's place, from the Mac and from the hosted MCP

**Files:**
- Modify: `apps/backend/core/services/session_browser.py` (`PLACES`, `set_place`, `place_from_mac`)
- Modify: `apps/backend/routers/messaging.py` (device route after `device_update_task` at line 179)
- Modify: `apps/backend/core/services/hosted_mcp.py` (`use_browser` tool after `update_task` at line 278; imports)
- Test: `apps/backend/tests/unit/services/test_session_browser.py`, `apps/backend/tests/unit/routers/test_local_session_device_reads.py`, `apps/backend/tests/unit/services/test_hosted_mcp_auth.py`

**Interfaces:**
- Consumes: Task 4 (`shared_row`), Task 16 (the place rule reads `browserPlace`), `local_sessions.authorize_session(..., owner_only=True)`.
- Produces: `session_browser.set_place(owner, session_id, where) -> {"sessionId", "browser"}`; `place_from_mac(owner, user, device, session_id, where)`; `POST /api/v1/local-sessions/device/sessions/{session_id}/browser-place {where}` (device token); hosted MCP tool `use_browser(where, session_id)`. Used by Task 20.

- [ ] **Step 1: Write the failing tests**

Append to `test_session_browser.py`:

```python
async def test_use_browser_sets_where_the_next_connection_goes(monkeypatch):
    from core.services.messaging import ApiProblem

    updates = []

    async def run_in_thread(fn, **kwargs):
        updates.append(kwargs)

    monkeypatch.setattr(session_browser.store, "run_in_thread", run_in_thread)
    assert await session_browser.set_place(OWNER, SID, "cloud") == {"sessionId": SID, "browser": "cloud"}
    assert updates[0]["UpdateExpression"] == "SET browserPlace = :w"
    assert updates[0]["ExpressionAttributeValues"] == {":w": "cloud"}
    with pytest.raises(ApiProblem):
        await session_browser.set_place(OWNER, SID, "elsewhere")
```

Append to `test_local_session_device_reads.py`:

```python
SESSION = "11111111-1111-4111-8111-111111111111"


def test_the_macs_use_browser_moves_its_own_shared_session(client, monkeypatch):
    from core.services.desktop_execution import DesktopError

    shared = AsyncMock(return_value={})
    monkeypatch.setattr(router.session_browser, "shared_row", shared)
    monkeypatch.setattr(router.session_browser, "set_place", AsyncMock(return_value={"sessionId": SESSION, "browser": "cloud"}))
    path = f"/api/v1/local-sessions/device/sessions/{SESSION}/browser-place"
    response = client.post(path, json={"where": "cloud"}, headers={"Authorization": "Bearer device"})
    assert response.status_code == 200 and response.json() == {"sessionId": SESSION, "browser": "cloud"}
    shared.assert_awaited_once_with("org_1", "user_mac", "dev_1", SESSION)
    shared.side_effect = DesktopError("not_shared", 403, "Share this session to use the Lightbulb browser.")
    assert client.post(path, json={"where": "mac"}, headers={"Authorization": "Bearer device"}).status_code == 403
    assert client.post(path, json={"where": "laptop"}, headers={"Authorization": "Bearer device"}).status_code == 422
```

Append to `test_hosted_mcp_auth.py`:

```python
async def test_use_browser_moves_a_session_its_caller_may_control(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    sid = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(hosted_mcp, "_caller", lambda *_scope: AuthContext(user_id="user_sender", org_id="org_test"))
    authorize = AsyncMock(return_value=({"ownerId": "org_test", "sessionId": sid}, {}))
    monkeypatch.setattr(hosted_mcp.local_sessions, "authorize_session", authorize)
    monkeypatch.setattr(hosted_mcp.session_browser, "set_place", AsyncMock(return_value={"sessionId": sid, "browser": "mac"}))
    assert await hosted_mcp.use_browser("mac", sid) == {"sessionId": sid, "browser": "mac"}
    assert authorize.await_args.kwargs == {"owner_only": True}
    authorize.side_effect = ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can do that.")
    with pytest.raises(ToolError, match="LOCAL_SESSION_FORBIDDEN"):
        await hosted_mcp.use_browser("cloud", sid)
```

(`test_hosted_mcp_auth.py` already imports `AuthContext`, `ApiProblem`, `AsyncMock`, `pytest` and `hosted_mcp`; add any it lacks.)

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_browser.py tests/unit/routers/test_local_session_device_reads.py tests/unit/services/test_hosted_mcp_auth.py -v -k "use_browser"`
Expected: FAIL (`set_place`, the route and the tool do not exist).

- [ ] **Step 3: Implement**

`session_browser.py`:

```python
PLACES = ("mac", "cloud")


async def set_place(owner: str, session_id: str, where: str) -> dict:
    """`use_browser`: where this session's NEXT browser connection goes; an open one stays where it is.
    Kept on the row (it survives re-registration) for the session's life."""
    if where not in PLACES:
        raise ApiProblem("INVALID_REQUEST", "where must be mac or cloud.")
    try:
        await store.run_in_thread(
            store._table().update_item,
            Key={"pk": repo.session_pk(owner, session_id), "sk": "META"},
            UpdateExpression="SET browserPlace = :w",
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":w": where},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.") from None
        raise
    return {"sessionId": session_id, "browser": where}


async def place_from_mac(owner: str, user: str, device: str, session_id: str, where: str) -> dict:
    """The Mac's own `lightbulb` MCP: the session the Mac proved is calling, shared and running."""
    try:
        await shared_row(owner, user, device, session_id)
    except DesktopError as exc:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", exc.message or NOT_SHARED) from None
    return await set_place(owner, session_id, where)
```

`routers/messaging.py`, after `device_update_task`:

```python
class DeviceBrowserPlace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    where: Literal["mac", "cloud"]


@router.post("/local-sessions/device/sessions/{session_id}/browser-place")
async def device_use_browser(session_id: str, body: DeviceBrowserPlace, device=Depends(native_session_device)):
    """`use_browser` from the Mac's `lightbulb` MCP: the calling session (the Mac proved which) moves
    its next browser connection."""
    return await session_browser.place_from_mac(device.owner_id, device.user_id, device.device_id, session_id, body.where)
```

`hosted_mcp.py` — add `from typing import Literal`, add `session_browser` to the `from core.services import ...` line, and after `update_task`:

```python
@mcp.tool()
async def use_browser(where: Literal["mac", "cloud"], session_id: str) -> dict:
    """Choose where a shared Claude Code or Codex session browses with its lightbulb-browser tools: "mac" (its Mac's Lightbulb agent Chrome, the default) or "cloud" (the workspace's cloud browser). Its next browser connection goes there; one already open stays where it is. session_id comes from find_sessions; only people who may control that session can move it."""
    auth = _caller(WRITE_SCOPE)
    try:
        sid = messaging.validate_uuid(session_id, label="session ID")
        row, _ = await local_sessions.authorize_session(auth, None, sid, owner_only=True)
        return await session_browser.set_place(row["ownerId"], row["sessionId"], where)
    except messaging.ApiProblem as exc:
        # FastMCP hides any other exception's text, so the client would never learn why.
        raise ToolError(f"{exc.error.code}: {exc.error.message}") from exc
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_browser.py tests/unit/routers/test_local_session_device_reads.py tests/unit/services/test_hosted_mcp_auth.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/session_browser.py apps/backend/routers/messaging.py apps/backend/core/services/hosted_mcp.py apps/backend/tests/unit/services/test_session_browser.py apps/backend/tests/unit/routers/test_local_session_device_reads.py apps/backend/tests/unit/services/test_hosted_mcp_auth.py
git commit -m "feat(sessions): use_browser sets where a shared session browses next"
```

### Task 20: Desktop — `use_browser` on the Mac's `lightbulb` MCP

**Files:**
- Modify: `apps/desktop/src-tauri/src/local_sessions/native_mcp.rs` (`tools` lines 245-279; `internal` lines 309-351; the eight-tools test lines 515-527)
- Modify: `apps/desktop/src-tauri/src/local_sessions/native_tools.rs` (`answer`, after the `read` dispatch at line 266)
- Modify: `apps/desktop/src-tauri/src/session_browser.rs` (`close_session`)
- Test: `native_mcp.rs` and `session_browser.rs` `mod tests`

**Interfaces:**
- Consumes: Task 19's device route; Task 7's `OPEN`.
- Produces: MCP tool `use_browser {where: "mac" | "cloud"}`; `session_browser::close_session(session_id)`.

- [ ] **Step 1: Write the failing tests**

In `native_mcp.rs`, replace `exactly_the_eight_tools_and_only_send_is_destructive` with:

```rust
    #[test]
    fn exactly_the_nine_tools_and_only_send_is_destructive() {
        let listed = tools();
        let names: Vec<_> = listed["tools"].as_array().unwrap().iter().map(|t| t["name"].as_str().unwrap()).collect();
        assert_eq!(names, ["find_rooms", "find_sessions", "read_messages", "send_message", "message_receipt",
            "find_tasks", "read_task", "update_task", "use_browser"]);
        for tool in listed["tools"].as_array().unwrap() {
            let writes = matches!(tool["name"].as_str(), Some("send_message" | "update_task" | "use_browser"));
            assert_eq!(tool["annotations"]["readOnlyHint"], !writes);
            assert_eq!(tool["annotations"]["destructiveHint"], tool["name"] == "send_message");
            assert_eq!(tool["annotations"]["openWorldHint"], false);
        }
    }

    #[test]
    fn use_browser_takes_only_mac_or_cloud_and_never_a_session() {
        assert_eq!(internal("use_browser", &json!({"where": "cloud"})), Some(("use_browser", json!({"where": "cloud"}))));
        assert!(internal("use_browser", &json!({"where": "laptop"})).is_none());
        assert!(internal("use_browser", &json!({"where": "mac", "session_id": "x"})).is_none(), "the session is the Mac's to prove");
    }
```

In `session_browser.rs` `mod tests`:

```rust
    #[tokio::test]
    async fn use_browser_ends_only_that_sessions_connections() {
        let (a, b) = (Arc::new(Notify::new()), Arc::new(Notify::new()));
        lock(&OPEN).insert("x1".into(), ("session-a".into(), a.clone()));
        lock(&OPEN).insert("x2".into(), ("session-b".into(), b.clone()));
        close_session("session-a");
        assert!(tokio::time::timeout(Duration::from_millis(50), a.notified()).await.is_ok());
        assert!(tokio::time::timeout(Duration::from_millis(50), b.notified()).await.is_err());
        lock(&OPEN).remove("x1");
        lock(&OPEN).remove("x2");
    }
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd apps/desktop/src-tauri && cargo test native_mcp session_browser`
Expected: FAIL (nine tools expected; `close_session` not found).

- [ ] **Step 3: Implement**

`native_mcp.rs` `tools()` — append to the list:

```rust
        {"name":"use_browser","description":"Choose where this session's lightbulb-browser tools browse: \"mac\" (this Mac's Lightbulb agent Chrome, the default) or \"cloud\" (the workspace's cloud browser). The next browser tool call reconnects there.",
         "inputSchema":{"type":"object","properties":{"where":{"type":"string","enum":["mac","cloud"]}},"required":["where"],"additionalProperties":false},
         "annotations":{"readOnlyHint":false,"destructiveHint":false,"openWorldHint":false}}
```

`internal()` — before `_ => None`:

```rust
        "use_browser" if only(&["where"]) && args.get("where").and_then(Value::as_str).is_some_and(|w| w == "mac" || w == "cloud") => {
            Some(("use_browser", Value::Object(args.clone())))
        }
```

`native_tools.rs` `answer` — after the `read` dispatch:

```rust
        if tool == "use_browser" {
            let response = client
                .post(format!(
                    "{}/api/v1/local-sessions/device/sessions/{}/browser-place",
                    crate::LIGHTBULB_API_BASE.trim_end_matches('/'),
                    source.session_id,
                ))
                .bearer_auth(&token)
                .json(&request["arguments"])
                .send()
                .await
                .map_err(|_| "Lightbulb unavailable")?;
            let ok = response.status().is_success();
            let data: Value = response.json().await.map_err(|_| "invalid Lightbulb response")?;
            if ok {
                // The next connection follows the new place: end the open one now.
                crate::session_browser::close_session(&source.session_id);
            }
            return Ok(json!({"ok": ok, "data": data}));
        }
```

`session_browser.rs`:

```rust
/// `use_browser` moved this session's browser: end its open connections, so the next one follows.
pub(crate) fn close_session(session_id: &str) {
    for (session, stop) in lock(&OPEN).values() {
        if session == session_id {
            stop.notify_one();
        }
    }
}
```

- [ ] **Step 4: Run the tests**

Run: `cd apps/desktop/src-tauri && cargo test native_mcp session_browser native_tools`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src/local_sessions/native_mcp.rs apps/desktop/src-tauri/src/local_sessions/native_tools.rs apps/desktop/src-tauri/src/session_browser.rs
git commit -m "feat(desktop): use_browser on the Mac's lightbulb MCP moves a session's browser"
```

### Task 21: Docs, delete the planning docs, PR and prod acceptance for M4

**Files:**
- Modify: `packages/claude-plugin/lightbulb/skills/lightbulb-session-sharing/SKILL.md` ("## Browsing")
- Modify: `apps/infra/hermes/skills-src/lightbulb/references/surfaces.md` ("Session browser"), `MAINTENANCE.md` (anchor)
- Delete: `docs/superpowers/specs/2026-10-11-session-browser-design.md`, `docs/superpowers/plans/2026-10-11-session-browser.md`

- [ ] **Step 1: Docs** — append to the skill's "## Browsing":

```markdown
`use_browser` (the `lightbulb` MCP) chooses where: `"mac"` (the default) or `"cloud"`. Your
next browser tool call reconnects there. Use it when your user asks, or when a site needs
the other browser's sign-in.
```

to the surfaces paragraph:

```markdown
A session moves between the two with its `lightbulb` MCP's `use_browser` (`mac` or `cloud`); a
person who may control the session can do the same through the hosted MCP's `use_browser`
with its `session_id`.
```

and to the anchor: `native_mcp.rs + native_tools.rs (use_browser); apps/backend/core/services/hosted_mcp.py (use_browser); routers/messaging.py (/local-sessions/device/sessions/{id}/browser-place)`.

- [ ] **Step 2: Delete the planning docs** (CLAUDE.md "Planning docs are disposable"; the PR descriptions are the durable record)

```bash
git rm docs/superpowers/specs/2026-10-11-session-browser-design.md docs/superpowers/plans/2026-10-11-session-browser.md
```

- [ ] **Step 3: Build skills, commit, PR, ship**

Run: `cd apps/infra/hermes && ./build-skills.sh`

```bash
git add packages/claude-plugin/lightbulb/skills/lightbulb-session-sharing/SKILL.md apps/infra/hermes/skills-src/lightbulb/references/surfaces.md apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md
git commit -m "docs(sessions): use_browser; delete the shipped session-browser spec and plan"
git push -u origin feat/session-browser-switch
gh pr create --repo TheLightbulbCompany/lightbulb --base main --title "feat(sessions): use_browser switches a shared session between the Mac and the cloud browser (M4)" --body-file /dev/stdin <<'BODY'
Milestone M4, the last of the session browser (spec and plan deleted here; this PR and #M1-#M3 are the record).

- `use_browser {where: "mac" | "cloud"}` on the Mac's `lightbulb` MCP (the session the Mac proves) and on the hosted MCP (`session_id`, people who may control it). Kept per session; the next connection follows; the Mac ends the open one at once.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01N9nLe5RG6ZAEHy5ihTq7gM
BODY
```

After CI and Codex: merge; promote the backend; build the desktop app.

- [ ] **Step 4: Prod acceptance**

1. In a shared Claude Code session with Browser on: "Use use_browser cloud, then open https://example.com with lightbulb-browser." The page opens in the workspace cloud browser, not the Mac's.
2. "Use use_browser mac, then take a snapshot." It reconnects on the Mac's agent Chrome.
3. Same for a shared Codex CLI session.
4. From another Claude Code with the hosted MCP signed in as a teammate the owner let in: `use_browser` with that session's id switches it; as a teammate not let in, it answers `LOCAL_SESSION_FORBIDDEN`.
