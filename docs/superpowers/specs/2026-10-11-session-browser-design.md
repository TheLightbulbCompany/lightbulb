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
  - Spike first: getting the peer pid of a loopback TCP connection on macOS (see Spikes).
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
  the load/save steps are driven by the app rather than by the bridge. Spike: where the
  load/save engine runs (see Spikes).
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

## Spikes before the plan

1. **Peer pid of a loopback TCP connection on macOS.** Options: `proc_pidfdinfo` scan, or
   `lsof -i` by port pair. Fallback: a per-session token in the MCP entry's environment. Measure
   cost per connect.
2. **chrome-devtools-mcp against a proxy.** Does it attach through our port to an existing
   Chrome without launching one? Do its tools work through the `agent_chrome.rs` filter (for
   example `Target.createTarget` forced to the background)?
3. **Sign-in load/save without a bridge run on the Mac path.** Can the app run the
   `browser_state.py` steps itself, or must the backend ask the runtime to run them over a
   cloud-side pipe as today? Pick the one with no second engine.
4. **Cloud reverse door.** The bridge dials back to the backend on demand. Confirm that a woken
   runtime can be told to dial within the 90 s invoke budget, and measure the added latency per
   CDP command.

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
