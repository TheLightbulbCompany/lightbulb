# Mac browser over raw CDP — design

Status: owner-approved direction ("ship this instead"), 2026-09-28. Plan:
`docs/superpowers/plans/2026-09-28-mac-browser-raw-cdp.md`. Delete both docs when the train ships.

## 1. What changes

Today an agent drives a paired Mac's browser through a typed, per-action proxy:
`lightbulb_desktop` `browser` action → `core/services/desktop_browser.py` → `browser.proxy` on the
Mac's CLI node (the OpenClaw sidecar's `lightbulb` profile) → one round trip per click. Frames come
from `desktop_frames.py` polling screenshots at ~1 fps, and sign-ins sync through
`desktop_browser_state.py` + the Mac app's `browser.state.v1` (`browser_state.rs`, a bundled
agent-browser).

After this train the Mac's Chrome is **a second upstream for the bridge's `cdp_mux`**. Hermes's own
`browser_*` tools drive it exactly as they drive the AgentCore cloud browser: same snapshots, same
refs, same `browser_cdp`, same screencast frames, same sign-in save/load code
(`bridge/browser_state.py`). The only Mac-specific code left is a byte pipe and a method filter.

```
Hermes browser_* ──> cdp_mux (loopback :9222, one upstream per (Mac, run))
                        │ ws://<backend task IP>:8000/api/v1/internal/mac-cdp/<ticket>
                        ▼
                 backend task holding the Mac's node socket (door: routers/mac_cdp.py)
                        │ node socket events browser.cdp.open / .frame / .close
                        ▼
                 Tauri (agent_chrome.rs): method filter ──> ws://127.0.0.1:<port>/devtools/browser/<id>
                                                             Chrome --user-data-dir=<appdata>/agent-chrome
```

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Tauri launches its own Chrome: `open -nga "Google Chrome" --args --user-data-dir=<app_data>/agent-chrome --remote-debugging-port=0 --no-first-run --no-default-browser-check`, and reads `<dir>/DevToolsActivePort`. | A separate profile dir keeps the person's everyday cookies out of reach, and Chrome 136+ refuses remote debugging on the default dir anyway. `-g` keeps the agent window from stealing focus. |
| D2 | The new dir is **not** the OpenClaw `lightbulb` profile dir (`<app_data>/openclaw-state/browser/lightbulb/user-data`). | The old path keeps working on a new app until its deletion task, and the two Chromes never contend for one `SingletonLock`. The deletion task wipes the legacy dir once. |
| D3 | CDP rides the **existing native node socket** (`clientId=openclaw-macos`, `node_client.rs`) as three events. There is no second socket and no new pairing. | That socket is already authenticated, paired, and terminated in-process by the backend task that holds it. |
| D4 | The door is a WebSocket route on the **same port 8000** of the task that holds the socket: `ws://<taskAddress host>:8000/api/v1/internal/mac-cdp/<ticket>`. `desktop_execution` already resolves that address (`taskAddress` on the ready row). Infra adds service-SG ingress on 8000 from `ContainerSecurityGroup`, which is the SG on every AgentCore runtime ENI. | The runtime can already reach every backend route through the internal ALB (ALB SG admits the whole VPC CIDR on 80/443), so this adds addressability, not new routes. A second listener would need a second uvicorn server fighting over signals. |
| D5 | **One `cdp_mux` upstream per (Mac device, run)**, next to the cloud upstream. | Each connection carries exactly one run's authority, so it closes when that run ends. Two members' runs in one microVM (founder + Aden) never share a pipe. Chrome admits many CDP clients. |
| D6 | The agent picks the Mac explicitly: `lightbulb_desktop` `use_browser {deviceId, generation}`, and `use_cloud_browser` to go back. Every later `browser_*` call in that run goes to the Mac. | This keeps the ruling that selected-Mac execution is explicit. The cloud stays the default. |
| D7 | Sign-in sync reuses `bridge/browser_state.py` (agent-browser over CDP, three-way merge), pointed at a backend-minted **state ticket**. `_valid_url` widens to accept the door URL. Mac-supplied exports are **vetted in the bridge** (`source: "mac"`). | This is one merge implementation. `_merge_three` was already written for "a paired Mac may have written meanwhile". |
| D8 | First owner wins per Mac: the row `__macchrome__#<nativeDeviceId>` names the workspace whose sign-ins the agent Chrome holds. It keeps the `load_id` fence (a conditional write on every claim, base write and fence). | This keeps #3377 / #3395. |
| D9 | The Mac reports a **profile id**, a random id written into the agent-chrome dir when it is created. A wipe from any cause (Browser off, unpair, the person deleting it) changes it. The backend treats a changed id as a fresh profile. | The backend never trusts a stale "loaded" record. Without this, the row would say loaded after #3384's Browser-off wipe. |
| D10 | Clearing a Mac is a **wipe**: `browser.wipe.v1` kills the agent Chrome (pid checked against its `--user-data-dir`) and deletes the dir. It replaces per-cookie clears. | A wipe is complete by construction: every origin, IndexedDB and extensions. |
| D11 | Frames reuse `cdp_mux.cast` → the run's frame sink → `room.browser.frame`, unchanged. Mac casts are **view-only**: `cdp_mux.dispatch` looks only at the cloud upstream. | Owner ruling: room members see frames read-only. |
| D12 | Room **and** Paperclip runs. A Paperclip acquire first runs the existing `desktop.operation.approve.v1` consent step (`_paperclip_consent` machinery), which the Mac answers from its stored switches (Paperclip actions + Browser) with no prompt. Tickets carry `family` and `companyId` so the door's re-check rebuilds the full auth. | Parity with the old path; reuses the consent path, nothing new on the Mac (ruling 2026-09-28). |
| D13 | No Chrome policy file (per-profile policy needs root/MDM on macOS). The new profile's `Default/Preferences` is **seeded** to pin downloads (`~/Downloads/Lightbulb agent`, no prompt), and the Tauri **method filter** enforces the rest (§4.3). | Ruling 2026-09-28. |
| D14 | No compat flags. Old and new paths coexist only between the new path going live and the deletion tasks, and every coexisting piece is deleted in the same train. | Owner ruling: "no compat, no flags". |

## 3. Security model

**What a ticket grants.** A ticket grants a CDP byte pipe to **one** Mac's agent Chrome. It holds
for one run of one workspace, and only while all of these hold, re-checked every 30 s on the
holder:

- the run is active (`authorize_run(..., active=True)` returns the same auth). For a room run that
  means its trigger's human author is the member who paired this Mac.
- the pairing is present, not `revoking`, and the native socket generation matches.
- the Browser grant is present: the ready row declares `browser.cdp.v1` and the paired entry grants it.
- `DESKTOP_EXECUTION_ENABLED` and the owner allowlist admit the owner.
- the Mac row's `loaded_owner` is this owner and its `profile_id` matches the ticket's.

**Where each check lives.**

| Layer | Enforces |
|---|---|
| Backend acquire (`mac_browser.acquire`) | Run authority, member = pairer, flag, grant, first-owner-wins, profile id, sync before first use |
| Door (`routers/mac_cdp.py`) | Ticket decrypt, TTL, single use, `address == own`, the same re-checks every 30 s, frame size, channel cap |
| Mac (`agent_chrome.rs`) | Browser switch on (local consent, checked at every open), connects **only** to its own agent Chrome's `DevToolsActivePort` endpoint, the method filter, frame size |
| Bridge (`browser_control` / `cdp_mux`) | View-only Mac casts, per-run upstream, no `computer_use` on a Mac |
| Bridge (`browser_state`, `source: "mac"`) | Vetting of Mac-supplied exports; mass-signout hold-back |

**Consent.** Consent is given once, at pairing (the Browser switch). The app never shows a
per-operation prompt. Browser off means the Mac refuses `browser.cdp.open`, closes every open
channel, and wipes the profile (#3384). The re-pair that follows drops the server grant, and the
door's re-check closes any pipe left open on another task within 30 s.

**Everyday profile.** The agent never reaches the person's everyday profile. The Mac pipes only to
the endpoint named in `<app_data>/agent-chrome/DevToolsActivePort`, and it validates that the port
answers `GET /json/version` before it connects. No CDP URL, port or path ever comes from the
backend or the model.

**Revocation.** Unpair (`DELETE /desktop/devices/{id}`) and member removal
(`revoke_member_soon`) fence the row and send `browser.wipe.v1`. The fence is conditional on the
`load_id` they read, and happens only if the holder is this owner or nobody. The wipe command is
`allow_revoking` + `allow_ungranted`, and it is in the base Mac command set, so it works after
Browser off. The Mac also wipes locally on unpair and on Browser off. An offline Mac is covered by
D9: its next acquire sees either no pairing or a new profile id.

**Mac-supplied state is untrusted.** On every `source: "mac"` checkpoint the bridge vets the
export before it touches the merge:

- cookies are reduced to known keys, strings capped at 4 KiB, `expires` finite, bools typed,
  `sameSite ∈ {Strict, Lax, None}`, at most 5000 cookies;
- a cookie that fails vetting drops its whole domain from this read, so that domain keeps the
  workspace copy;
- an origin must equal `scheme://netloc` (http/https), with items ≤ 1 MiB each and ≤ 10 MiB per
  origin; one that fails is left unread;
- a save that would sign the workspace out of more than `max(5, half)` of the base's cookie domains
  at once holds those domains back.

These rules are the same as `desktop_browser_state._vetted`, moved.

**What the method filter blocks.** The filter refuses the command and answers it with a CDP error;
the command never reaches Chrome:

- `DOM.setFileInputFiles`, `Input.dispatchDragEvent`, `Page.handleFileChooser` (no Mac file leaves
  through a form);
- navigation to any URL that is not `http:`, `https:` or exactly `about:blank`, in `Page.navigate`,
  `Target.createTarget` and `Network.loadNetworkResource` (no `file:`, `chrome:`,
  `chrome-extension:` or `devtools:`);
- `Browser.grantPermissions`, `Browser.setPermission`, `Browser.executeBrowserCommand`,
  `Browser.close`, `Browser.crash*`, `Target.openDevTools`, `Target.exposeDevToolsProtocol`;
- the `Tethering.*` (opens ports on the Mac), `Extensions.*`, `PWA.*`, `Cast.*`, `DeviceAccess.*`
  and `SystemInfo.*` domains.

`Browser.setDownloadBehavior` and `Page.setDownloadBehavior` get their `downloadPath` rewritten to
the pinned folder. Everything else passes, matching the cloud's "agents may send any method"
(owner ruling 2026-09-27), minus what reaches the person's machine.

**Residual risks (accepted, stated).**

- An extension the agent installs from the Web Store can read pages in the agent profile. It
  cannot get file access without a `chrome://extensions` toggle, which the filter makes
  unreachable.
- The ticket appears once in the backend access log. It is single-use, lives 60 s, and is bound to
  the task address and socket generation.
- Vetting runs in the runtime, where the agent's shell also runs. That shell can already write
  workspace sign-ins through the cloud browser, so this gives nothing new.

## 4. Protocol

### 4.1 Grants

- `browser.cdp.v1` joins `DESKTOP_MAC_COMMANDS` and is granted under **Browser**
  (`mac_commands()` for new pairs).
- `browser.wipe.v1` joins `DESKTOP_MAC_COMMANDS` and `DESKTOP_MAC_BASE_COMMANDS`, so new pairs
  always get it.
- **No re-pair, no `pair_state` bump** (ruling: consent once). Existing pairs keep their stored
  `commands`, so `desktop_pair.granted_commands` **derives** both for an `openclaw-macos` entry:
  `browser.wipe.v1` always, and `browser.cdp.v1` when the pair's Browser grant holds (the
  reciprocal `node-host` peer's stored commands contain `browser.proxy`, exactly like today's
  `browser.state.v1` derivation from #3395). The derivation stays after the old path is deleted,
  keyed on that stored string.
- The new desktop app declares both unconditionally (like `browser.state.v1` today) and refuses
  `browser.cdp.v1` locally when Browser is off.
- **New-app marker.** The ready row's `commands` are `declared ∩ granted` at connect, so a new app
  on a Browser-off pair also lacks `browser.cdp.v1` there. `browser.wipe.v1` (declared only by the
  new app, always granted) is the marker: ready row lacks `browser.wipe.v1` → old app; has it but
  lacks `browser.cdp.v1` → Browser off.
- `browser.cdp.v1` is in `PUBLIC_COMMANDS`, so `lightbulb_desktop devices` shows which Mac can
  browse. Submitting it as an operation is still refused (`unsupported_command`).

### 4.2 Node-socket events (native socket only; JSON text frames; no acks)

| Direction | Frame |
|---|---|
| backend → Mac | `{"type":"event","event":"browser.cdp.open","payload":{"channel":"<uuid4>","nodeId":"<device>","profileId":"<id>"}}` |
| Mac → backend | `{"type":"event","event":"browser.cdp.opened","payload":{"channel":"<uuid4>"}}` |
| both | `{"type":"event","event":"browser.cdp.frame","payload":{"channel":"<uuid4>","data":"<one CDP JSON message as text>"}}` |
| both | `{"type":"event","event":"browser.cdp.close","payload":{"channel":"<uuid4>","code":"<CODE>"}}` |

**Opening a channel.** The Mac answers `opened` or `close`. `close` carries one of these codes:
`BROWSER_OFF`, `CHROME_MISSING`, `CHROME_START_FAILED`, `PROFILE_CHANGED` (the dir's id no longer
equals `profileId`), `CAPACITY`, `CLOSED`, `TOO_LARGE` (the backend adds `OVERFLOW` and `TIMEOUT` locally). The backend waits 15 s for the answer.

**Limits.** At most 4 open channels per socket, and 16 MiB per `data`; the Mac also bounds the
**encoded envelope** at 24 MiB (the relay drops the whole socket with 1009 above its 25 MiB
`MAX_PAYLOAD`), closing that channel `TOO_LARGE` instead. The backend holds a 256-frame inbound
queue per channel; overflow closes the channel with `OVERFLOW` without blocking (the close marker
is delivered by the channel's own closed flag, never by a `put` into the full queue). The Mac's outbound
socket writer has 1024 slots and awaits (backpressure) rather than dropping.

**Teardown.** A socket disconnect closes every channel on both sides. The backend's
`NodeSession.send` queue grows from 16 to 1024 so CDP traffic never starves invokes. Invoke
admission still fails fast with `NodeCapacity` when the queue is full.

**Node invokes (native socket).** Standard `node.invoke.request` / `node.invoke.result`:

- `browser.cdp.v1` params `{"action":"ensure"}`: Browser must be on. It launches Chrome if it is
  not running and answers `{"profileId":"<id>"}`. Error codes are `BROWSER_OFF`, `CHROME_MISSING`
  and `CHROME_START_FAILED`.
- `browser.wipe.v1` params `{}`: closes channels, stops the agent Chrome and deletes the dir. It
  answers `{"wiped": true|false}` and runs whatever the Browser switch says.

### 4.3 Mac-side pipe (`agent_chrome.rs`)

On `browser.cdp.open`, the Mac:

1. checks the Browser switch;
2. runs `ensure_running()`, which launches Chrome if needed and polls `DevToolsActivePort` for up to
   15 s;
3. compares the profile id;
4. connects `tokio_tungstenite` to `ws://127.0.0.1:<port><path>`;
5. replies `opened`.

Each backend→Chrome `data` passes through `filter(&str, downloads) -> Filtered::{Pass(String)
/* byte-identical or rewritten */, Refuse(String) /* CDP error reply */}`. Chrome→backend is not filtered. Size is checked in both directions.

## 5. Ticket

**Format.** A Fernet token. The key is `sha256("mac-cdp-ticket-v1:" + WS_INTERNAL_SECRET)`,
urlsafe-b64. The claims, all required, are:

```json
{"purpose":"mac-cdp-v1","use":"agent","family":"room"|"paperclip","ownerId","memberId",
 "companyId","deviceId","generation","address","profileId","runId","agentId","roomId",
 "nonce","iat","exp"}
```

- `deviceId` is the **native** device.
- `address` is the holder's `own_address()`.
- The run fields rebuild exactly the auth `authorize_run` returns: room auth uses
  `memberId`/`roomId` (`companyId` = `""`), Paperclip auth uses `companyId` (`memberId`/`roomId`
  = `""`).
- There is one ticket kind (`use` is always `"agent"`): sign-in load and checkpoint run in the
  bridge over the run's own pipe, like a cloud session ("one sync engine" ruling), so no separate
  state ticket exists. The holder re-check needs the row's `loaded_owner`/`profile_id` to match.

**Lifetime.**

- `exp = iat + 60`. The ticket must be **redeemed** (WebSocket upgrade) within 60 s.
- It is **single use**: the holder keeps redeemed nonces until `exp + 5`.
- After redemption the connection lives while the §3 checks hold: re-checked every 30 s and
  capped at 9 h. Lost authority closes at once; a transient re-check error (Clerk, DynamoDB)
  retries at the next tick and closes (1011) only after 3 in a row.
- Bridges never cache a ticket. `cdp_mux` reconnects with the URL from the latest acquire, and
  every browser tool call re-acquires.

**Door close codes.**

| Code | Meaning |
|---|---|
| 4401 | Bad, expired or replayed ticket |
| 4409 | Wrong task, or socket generation gone |
| 4403 | Authority gone (run ended, unpaired, revoking, Browser grant gone, held elsewhere, profile changed, disabled) |
| 4502 | The Mac refused to open or the channel ended abnormally (`OVERFLOW`, `TOO_LARGE`, `TIMEOUT`, a Mac code); the reason is that code |
| 4503 | Node socket at capacity (`CAPACITY`) |
| 1011 | Internal error |
| 1000 | Normal close |

The bridge treats any close as upstream loss. The next tool call's acquire returns the precise
reason (§7).

## 6. Lifecycles

### 6.1 Acquire

`POST /api/v1/internal/browser/{owner}/mac/acquire {runId, agentId, family, deviceId, generation}`
authenticates with the hook token. It is forwarded to the holder task if needed, then:

1. Run authority: `authorize_run(owner, runId, agentId, family, active=True)` builds `auth`
   (room or Paperclip). The hook token alone never authorizes a runId.
2. Resolve the native target with `browser.cdp.v1`. The error mapping is in §7. The
   update-required check comes first: the ready row lacks `browser.wipe.v1`.
   Paperclip: run the `desktop.operation.approve.v1` consent step once for this acquire.
3. Invoke `browser.cdp.v1 ensure` → `profileId`.
4. Read the row and decide:
   - **Held by another still-paired workspace**, or its unexpired sync claim → 409
     `browser_profile_held_by_another_workspace`, or 202 starting when it is this owner's own claim.
   - **Deferred**: this owner's last load failed less than 60 s ago (row `base_key` None and
     `deferred_until` in the future) → ready without a sync. Checked before Fresh.
   - **Fresh**: `loaded_owner != owner`, or `profile_id != profileId`, or no `base_key`. Claim
     (conditional on `load_id`), then run a background sync. It wipes first if the row names a
     different (stale) owner for this same `profileId`, re-ensures, then loads (or `base` when the
     workspace has no save; the bridge's `base` writes its export to `stateUrl`, so the new Mac
     base key is passed as `stateUrl` there). Answer 202 `{"status":"starting"}`.
   - **Stale**: `synced_key != workspace saved_key` and no other run's lease on this device is
     live. Claim, then background save (three-way) then load. Answer 202.
   - Otherwise **ready**.
5. When ready:
   - record the lease `leases[runId] = now + 300`;
   - stamp `mac_browsing_at` on the owner's browser row (frames);
   - mint an `agent` ticket;
   - answer `{"generation": <native generation>, "cdpUrl": <door URL>, "humanInputAt": 0, "framesAt": <human_frames_at>}`.

### 6.2 Release

`POST /api/v1/internal/browser/{owner}/mac/release {runId, deviceId, generation}` runs when the run
ends or leaves the Mac. It drops the lease, and once no other lease on that device is live it
schedules a background **save** on the holder. With no `base_key` on the row the save is
**skipped** (the bridge would treat a missing base as empty and let every stale Mac domain win):

1. The bridge checkpoint uses `action: "save"`, `source: "mac"`, the state ticket as `cdpUrl`,
   `previousStateUrl` = the workspace save, `baseStateUrl` = the row's `base_key`, and
   `stateUrl`/`baseOutUrl` = fresh keys.
2. The backend runs `_still_loaded(load_id)`.
3. It CAS-publishes the new `saved_key` onto the `__browser__#<owner>` row: only if `saved_key` is
   unchanged, never in `transition`, carrying the extension-storage snapshot over as `_commit` did.
   Up to 3 tries; `Busy` retries on the next release or acquire.
4. It writes the row `base_key`/`synced_key` conditionally on `load_id` and deletes the replaced
   objects.

If the Mac is offline, the save is skipped. The Mac keeps its changes, and the next release saves
them.

### 6.3 Revocation

`mac_browser.clear_device(owner, device)` (unpair, member removal):

1. Resolve the native device of the pair.
2. If the row holder is another workspace, do nothing.
3. Otherwise, fence the row (owner `None`, new `load_id`, conditional on the one read) and delete
   its `base_key`.
4. Dispatch `browser.wipe.v1` (best effort, 15 s).
5. Log and meter it, never with values.

## 7. Failure modes and user-visible errors

The acquire answers `4xx {"detail":{"code","message"}}`. The bridge's pre_tool_call blocks with
`message`, and `use_browser` returns it as `{"error": message}`.

| Condition | code (HTTP) | Message the agent relays |
|---|---|---|
| `DESKTOP_EXECUTION_*` excludes the owner | `desktop_disabled` (503) | Agents can't use a Mac's browser in this workspace yet. |
| Paperclip run whose Mac refuses consent (Paperclip actions or Browser switch off) | `desktop_consent_denied` (403) | That Mac doesn't allow Paperclip actions. Its owner can turn them on in the Lightbulb desktop app's Settings. |
| Trigger's author is not the Mac's pairer / not a member | `desktop_forbidden` (403) | Only the Mac of the person who asked can be used for this. |
| Mac offline, reconnected, or wrong generation | `desktop_generation_lost` (409) | That Mac is offline or reconnected. List devices again and pick it. |
| **Old desktop app** (ready row lacks `browser.wipe.v1`) | `desktop_update_required` (409) | Update the Lightbulb desktop app on that Mac to let agents use its browser. |
| Browser switched off (ready row has `browser.wipe.v1` but not `browser.cdp.v1`, or Mac says `BROWSER_OFF`) | `browser_off` (409) | Browser is switched off for that Mac. Its owner can turn it on in the Lightbulb desktop app's Settings. |
| Chrome not installed | `chrome_missing` (409) | Google Chrome isn't installed on that Mac. Install it there, or use the cloud browser. |
| Chrome won't start | `chrome_start_failed` (409) | Chrome on that Mac didn't start. Try again in a moment. |
| Profile held by another workspace | `browser_profile_held_by_another_workspace` (409) | This Mac's agent browser holds another workspace's sign-ins. Unpair it from that workspace first, or use the cloud browser. |
| Sync in progress | 202 `starting` | The Mac's browser is loading the workspace's sign-ins; retry this browser action in a few seconds. |
| Door closes mid-run (any 44xx/4502) | next acquire | Whichever row above now applies. |
| `computer_use` while on a Mac | bridge block | computer_use works on the cloud browser's screen only; on a Mac use the browser_* tools. |
| Watcher input on a Mac frame | `NOT_CASTING` | Unchanged cloud message; the Mac is view-only. |

**Sync failure.** A load failure still lets the run browse. The row keeps `loaded_owner = owner`
with no `base_key`, so nothing is saved back and the next acquire (after a 60 s defer) retries the
load. The failure is metered as `mac_browser.sync{kind=load,result=error}` and logged by class name
only, never with values. A save failure keeps the Mac's changes on the Mac.

## 8. Rollout (deploy units and order)

| Order | Unit | Safe alone because |
|---|---|---|
| 1-3 | Backend (prod `workflow_dispatch`) | Nothing calls the new routes yet; old path untouched. |
| 4-5 | Hermes image (build → `hermes-version.json` pin flip → `roll_hermes_runtimes.py`) | `use_browser` against any current Mac answers **"Update the Lightbulb desktop app"**; old `browser` action still works. |
| 6 | Frontend (rides the backend dispatch) | Detects both actions until task 8. |
| 7 | Desktop (`desktop-build.yml`, signed) | New dir, new events; old `browser.proxy` path still served by the sidecar. |
| 8-10 | Deletions (Hermes, backend, desktop) | After prod verification (§ plan) passes. |

## 9. Rulings (2026-09-28, formerly open questions)

1. No Chrome policy file: seeded `Preferences` + the CDP method filter (D13).
2. Paperclip runs may use a Mac's browser through the existing consent step (D12).
3. The legacy OpenClaw profile folder is wiped once by the task-10 desktop build (D2).
4. No `pair_state` bump; grants are derived server-side (§4.1).
