# Browsers beside a coding session (priorities 3 and 4, research level)

Status: research only, 2026-10-07. These are the last two items of the 2026-10-07 brief; the
owner confirmed they come after the terminal (priority 1) and session chat (priority 2). Nothing
here is scheduled before those ship. Facts cite `main` at `dfce908f8`.

## A. A view of the browser connected to a person's coding agent (priority 3)

### A.1 What exists
- The Mac already casts a tab a Mac agent run drives: `agent_cast.rs` opens its own CDP
  connection to the channel's Chrome, attaches to the tab the agent last drove, and streams
  screencast frames (≤10 fps, ≤200 KB base64, adaptive quality) plus a 480 px thumb every
  500 ms as `browser.cast.frame`; `browser.cast.set {channel, view, thumb}` from the backend
  starts and stops it (`agent_cast.rs:1-30`). The web renders them with `BrowserFrameView.tsx`
  on the room's frames sibling channel and sends clicks/keys to `POST /browser/mac/input`
  (`BrowserFrameView.tsx:1-12`, `routers/container.py:389`). That rail is for Hermes agents'
  "Lightbulb agent" Chrome (`agent_chrome.rs`), not for a person's coding session.
- On the founder's Mac, coding sessions reach browsers three ways (observed 2026-10-07):
  `chrome-devtools-mcp` and `@playwright/mcp` servers spawned by Claude Code (they start a
  Chrome with a remote-debugging port), the Claude-in-Chrome extension (native messaging host
  `com.anthropic.claude_code_browser_extension.json`, the person's everyday Chrome, no CDP port),
  and Codex's extension (`com.openai.codexextension.json`). `agent-browser` is not installed on
  the Mac; it is baked into the Hermes image only.

### A.2 Proposed shape (not scheduled)
- **Discovery, per session, on the Mac:** walk the session pid's children for MCP browser
  servers; find the Chrome they launched (child pid, `--remote-debugging-port` / `--user-data-dir`
  on its command line, listening port via `lsof`); keep only Chromes whose profile is a
  dedicated automation profile (never the person's everyday profile). The Claude-in-Chrome
  extension drives the everyday Chrome with no CDP door; it is out of scope for a live view
  (no window capture, owner ruling 2026-10-07).
- **Association:** the browser belongs to exactly one session (its process ancestor). The
  session row carries `browsers: [{id, kind: "cdp"|"extension", title, url}]`; no browser is ever
  attached to a session that did not spawn it.
- **View:** a "Browser" tab beside the terminal (same column as session chat), frames on the
  session screen rail (`kind: "image"`, with its own budget; a session browser is a real CDP screencast of a dedicated automation Chrome, not a window capture), only
  while someone watches. Only the tab the agent drives is cast (CDP `Target.getTargets` of that
  Chrome, the agent's current target); other tabs of that Chrome are never shown.
- **Control:** read-only for everyone; clicks and keys only for people who may type into the
  session (`may_control`); owner-only: disconnect / stop casting. Input goes through the
  existing Mac input route shape (`/browser/mac/input` batched events) re-keyed by session.
- **Not exposed:** cookies, storage, downloads, other tabs, other sessions' browsers, the
  everyday Chrome. Nothing is recorded.
- **Reconnect:** cast set TTL 15 s (`agent_cast.rs:23`) already stops orphaned casts; a new
  watcher restarts it.

### A.3 Spike before any build
Can a Chrome started by `chrome-devtools-mcp` / `@playwright/mcp` be attached by a second CDP
client without disturbing the MCP server (Chrome admits many CDP clients; the Mac rail already
does this for its own Chrome)? Measure CPU of a screencast beside a terminal relay.

## B. AgentCore browser control through the Lightbulb CLI/MCP (priority 4, absolute last)

### B.1 What exists
- The workspace's cloud browser (AgentCore `aws.browser.v1`) is owned by the backend
  (`browser_control.py`): one session per workspace, started on demand, saved automatically,
  stopped after 30 min idle; AWS admits ONE automation (CDP) connection per session and one
  live-view viewer. The bridge in the runtime holds that one connection and multiplexes every
  run through `cdp_mux.py`; the backend signs CDP and live-view addresses
  (`browser_sessions.py:197-225`, 300 s presign cap) and hands the CDP address to the bridge at
  `POST /internal/browser/<owner>/acquire` with a per-run lease (`acquire_agent`,
  `browser_control.py:991`, `LEASE_SECONDS` 300, `ACQUIRE_WAIT_SECONDS` 10).
- People use it through the panel (`/browser/live-view`, `/viewing`, `/input`,
  `routers/container.py:218-437`). The `lightbulb` MCP (hosted and the Mac bridge) exposes five
  messaging tools only (`hosted_mcp.py:239-322`, `native_mcp.rs:245-263`). Hermes agents have
  `browser_*`, `browser_cdp` and `computer_use` tools in the runtime.
- The reverse door already exists for a Mac's Chrome: a runtime dials a single-use ticketed
  WebSocket on the backend task (`mac_cdp.py`, `TICKET_SECONDS` 60, re-checked every 30 s).

### B.2 Proposed commands (not scheduled)
On the `lightbulb` MCP (both servers) and as `lightbulb browser …` in the CLI:
- `browser_status` → `{target: "cloud"|"mac:<device>", state, leasedBy?, liveViewUrl?}`.
- `browser_lease {target, seconds≤300}` → a lease as a "human session run" (`run_id = session:<sid>`),
  refused `409 leased_by_<agent>` while an agent holds it (the bridge's wait is 10 s; a CLI
  waits the same), `404` when the workspace has no browser, `403` for non-members.
- `browser_cdp {target, method, params}` → one CDP command, proxied by the backend to the
  bridge's multiplexer through the runtime envelope while a runtime is up, or sent directly by
  the backend on the one connection when no run holds it. Never a raw CDP URL to the CLI: the
  single-connection rule and the lease live in one place.
- `browser_release`.
- **Never switch targets silently:** `target` is required; the default the CLI offers is the
  session's own connected browser (part A) when one exists, else it refuses with the choice.
- Receipts: every lease and command writes an audit row keyed by session and person; errors are
  typed (`no_browser`, `leased`, `not_member`, `target_required`, `cloud_off`).
- Permissions: the caller must be a human session token (the Mac bridge attests the session;
  the hosted server's OAuth client identity is refused for control), a workspace member, and the
  session's own human; admins can stop.

### B.3 Tests to write before any build
Wrong-workspace token refused; a member of workspace A cannot lease workspace B's browser; an
agent run's lease blocks the CLI (409) and vice versa; a CLI that names no target is refused; the
cloud browser stays off when `cloud_off`.
