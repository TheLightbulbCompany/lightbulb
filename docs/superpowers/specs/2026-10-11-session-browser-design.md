# The Lightbulb browser for Claude Code and Codex sessions

Status: design approved in chat by the owner on 2026-10-11. It replaces parts A and B of
`2026-10-07-coding-agent-browser-design.md` (the file is in git history at 5edba54d4). Facts cite
`origin/main` at 6919f3f73.

## What the owner approved

1. **Shared browser.** A Claude Code or Codex session that is shared to Lightbulb browses in a
   Lightbulb browser, so it has the workspace's sign-ins. It never uses a throwaway Chrome or the
   person's everyday Chrome.
2. **Which browser.**
   - Claude Code and Codex use **the Mac's Lightbulb agent Chrome**.
   - Friday and the other Hermes agents use **the workspace's cloud browser**. This is unchanged.
   - Either kind of agent can be told to switch.
   - If browser access is off for that Mac, the session uses the cloud browser.
3. **Watch and take over.** People watching the session see a **Browser** tab next to the
   terminal with the live page. People who may type into the session may also click and type
   in the browser. Nothing is recorded, and frames flow only while someone watches.
4. **Limits.**
   - A session that is not shared is refused with "Share this session to use the Lightbulb
     browser".
   - Codex behaves the same as Claude Code.
   - The everyday Chrome is never touched.

Later, not in this version: an agent continues in the exact tab another agent left (a
"where the work is" record plus tab ownership).

## How it works

### 1. Connecting a session

- **Local port.** The desktop app serves one fixed loopback browser port (`127.0.0.1:<port>`).
  It answers `/json/version` and accepts the browser WebSocket, so standard CDP clients connect
  to it as they would to a Chrome.
- **MCP entry.** At pairing, the app registers one user-scope MCP entry, `lightbulb-browser`.
  This is the same mechanism `native_mcp.rs` uses for `lightbulb`
  (`claude mcp add --scope user …`), and the Codex config gets the equivalent entry. The entry
  runs chrome-devtools-mcp with `--browserUrl http://127.0.0.1:<port>`. We write no browser
  tools of our own.
- **Identifying the session.** The app identifies which session a connection comes from by the
  peer process: the TCP peer pid, walked up to the session process, matched against the local
  sessions list.
  - A connection that is not from a shared session is refused, and the refusal reaches Claude
    Code as an error message.
  - The method is libproc (spike 1).
- **Each session is a run.** A session is a "run" with id `session:<sessionId>`, so it uses the
  existing lease and sign-in machinery.
  - The lease is renewed while CDP traffic flows.
  - It is released 300 s after the last command, or when the session ends.
  - Release saves sign-ins.

### 2. Routing

The session's place is `mac` (the default) or `cloud`.

- **`mac`.** The connection becomes one more channel into `agent_chrome.rs` (`Channels`, up to
  `MAX_CHANNELS` = 4). Its existing filter applies unchanged:
  - no focus stealing
  - background tabs
  - downloads to `~/Downloads/Lightbulb agent`
  - no capability restrictions

  On the backend, `mac_browser.py` gets a lease for `session:<id>`. The first lease loads
  sign-ins and the last lease out saves them, exactly as for Hermes runs. The difference is that
  the load/save steps are driven by the app rather than by the bridge. The runtime runs
  the load/save steps (spike 3).
- **`cloud`.** The app dials the backend's machine relay with a session-scoped request.
  - The backend wakes the runtime and hands the bridge a single-use ticket.
  - The bridge dials back to a backend door, mirroring `mac_cdp.py` in reverse.
  - The backend joins the two sockets.
  - Inside the runtime, the session is one more run on `cdp_mux`, in its own tabs. AWS allows
    one CDP connection, and the bridge keeps holding it.
  - The runtime stays awake while the session browses.
- **Fallback.** When the Mac's browser permission is off (`desktop_permissions.browser`), a
  `mac` session is routed to `cloud`.
- **Switching.**
  - The `lightbulb` MCP gets one new tool, `use_browser {where: "mac" | "cloud"}`. It sets the
    session's place, which persists for that session, and the next CDP connection follows it.
  - Friday's existing `lightbulb_desktop use_browser` / `use_cloud_browser` are unchanged.

### 3. Watching and taking over

- **Browser tab.** The session view gets a **Browser** tab beside the terminal. It shows only
  while the session has an open browser connection.
- **Frames on `mac`.** The existing `agent_cast.rs` casts the channel's current tab. Today the
  cast is keyed to a room run (`mac_live.py` requires a room claim). It gains a session key:
  frames go to the session's watchers on the existing session screen rail as an image kind, and
  only while someone watches. That is the same "viewers keep it alive" heartbeat, with the 15 s
  cast TTL.
- **Frames on `cloud`.** The bridge's `cdp_mux.cast` of the session's tab is sent to the same
  session channel.
- **Input.** Clicks and keys go through the same input shape as `/browser/mac/input`, keyed by
  session and allowed only for people who may type into the session (`may_control`). People's
  input is tagged as human, as today (`Lightbulb.humanInput`).

### 4. Errors (shown in Claude Code)

| Code | When |
|---|---|
| `not_shared` | The session is not shared ("Share this session to use the Lightbulb browser"). |
| `browser_profile_held_by_another_workspace` | The Mac profile belongs to another workspace (first-owner-wins, unchanged). |
| `mac_browser_busy` | All 4 Mac channels are in use. |
| `cloud_unavailable` | No cloud browser, or the runtime could not be woken. |

## Spike results (2026-10-11, throwaway scripts, not kept)

1. **Peer process of a loopback TCP connection.**
   - **Method:** libproc socket matching (`proc_listpids` → `PROC_PIDLISTFDS` →
     `PROC_PIDFDSOCKETINFO`, matching our port pair), then a parent-pid walk with
     `sysctl KERN_PROC_PID`.
   - **Results:**
     - correct in 20/20 tries
     - median 0.3 ms, about 40 ms worst case over all of the user's processes
     - no root needed
     - a grandchild client (npx → node) walks up to the `claude` process
   - **Do this:** look up once per accepted connection. If the process is not found, deny.
   - **Do not:** use `lsof`. It takes 63 ms and also returns the listener itself.
2. **chrome-devtools-mcp and Playwright MCP through a proxy.** Both attach without launching a
   Chrome of their own. They use only `GET /json/version` (with and without a trailing slash)
   plus the browser WebSocket, with flattened sessions. Two filter changes are needed for
   **session channels**:
   - **Open new tabs in a new window.** `Target.createTarget` must be forced to
     `{newWindow: true, background: true}`. In a background tab `requestAnimationFrame` never
     fires, so Playwright's click times out. A background window stays "visible" and the front
     app is unchanged (measured). Hermes channels keep today's behaviour unless a later test
     shows the same need.
   - **Also swallow `Browser.setContentsSize`.** chrome-devtools-mcp `resize_page` uses it.
   - **Fixed-port collision.** The port must fail loudly when another program already holds it:
     another automation Chrome on this Mac held port 9333.
3. **Sign-in load/save: one engine.**
   - **Where it runs:** the backend asks the runtime, over `container_rpc`, to run the existing
     `browser_state.checkpoint` steps against the Mac through the existing ticketed
     `mac_cdp` door. This extends the bridge's `browser.state` RPC with a Mac source (upstream
     `mac:<device>:session:<id>`).
   - **What the desktop app does:** it only proxies. It has no sync engine.
   - **Blockers to widen for a `session` family:**
     - `mac_cdp._well_formed`
     - `desktop_execution.AUTH_FIELDS` / `valid_auth` / `_reauthorize`
     - `desktop_execution._ID`, which has no `:`
   - **Opening the channel:** the Mac app opens the channel with a new node-socket event
     (`browser.cdp.offer {channel, sessionId}`). Today only the backend opens channels.
4. **Cloud path.**
   - **How the session connects:**
     1. The backend task that holds the Mac's node socket mints a `use: "session"` ticket.
     2. A new RPC, `browser.attach {doorUrl, runId}`, makes the bridge dial that door.
     3. The bridge pumps the door into its own loopback mux as run `session:<id>`, in its own
        tabs. This is about 30 lines in the bridge, and `cdp_mux` itself is unchanged.
   - **Lease:** the run joins `_BROWSING`, so the bridge's shared lease and grace timer cover it.
   - **Cold starts:** a cold start can outlast the 90 s invoke budget, so `browser.attach`
     answers STARTING and the app retries.
5. **Live frames.**
   - **Where they go:** an `image` kind is added to the session screen rail
     (`/ws/<ws>/session-frames/<sid>`). That rail already has session-keyed authorization,
     viewer heartbeats and a byte budget. A 480 px JPEG of about 10 KB fits one frame.
     `mac_live.py`'s room claim is left alone.
   - **Old desktops:** they drop the connection on an unknown frame kind (`_nudge`), so the new
     kind is gated on a desktop capability.

## Tests

- A session that is not shared is refused.
- A session of workspace A cannot reach workspace B's Mac profile or cloud browser.
- A `session:` lease loads sign-ins on the first lease and saves on the last release (Mac), and
  saves on release (cloud).
- With browser off, the session goes to cloud.
- `use_browser` switches the next connection.
- Watcher input is refused for people without `may_control`.
- No frames flow with no watcher.
- A fifth Mac channel gets `mac_browser_busy`.
