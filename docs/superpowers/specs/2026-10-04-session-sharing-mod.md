# Session sharing inside Claude Code (a Claude Code mod)

Status: draft, 2026-10-04. Builds on `2026-10-02-managed-session-sharing.md` (revision 5).
Where the two differ, that spec wins until this one ships.

## 1. Problem

Session sharing works on the Lightbulb side and is invisible on the Claude Code side:

- The terminal never says the session is shared, how (live terminal or transcript), who is
  watching, or who can type.
- A join request reaches the owner in Lightbulb only. The person is working in the terminal.
- A teammate message shows as raw text: `[Lightbulb teammate contribution from @aden …]` and
  Claude's long peer warning. An owner's phone prompt looks almost the same.
- The authority rules are skill text. The skill says "Sharing is not enforcement."

Claude Code v2.1.287 added mods: a plugin of JavaScript/TypeScript hooks that run inside the
Claude Code process. A mod can draw a band above the prompt, panes and toasts, redraw
transcript rows, hold or refuse a tool call, read every stored row and every streamed model
step, and call HTTP over a Unix socket (`$.http.fetch(url, { socketPath })`).

## 2. Decisions

1. **One plugin, `lightbulb-sessions`**, shipped inside the desktop app and installed by
   enrollment when Claude sharing is on. It adds a mod. It adds no MCP server (the `lightbulb`
   name is taken, and a clash broke Codex's config), no settings hooks, and nothing that reads
   `CLAUDE_PLUGIN_ROOT`. Discovery keeps the existing hooks, wake hook, skill and MCP entry.
2. **The mod talks to the desktop app only**, through a new uid-checked socket, `mod.sock`,
   beside `tool.sock`. The app maps the caller's pid to one live mirrored session, the same way
   `native_tools.rs` does (the mod runs in the Claude process itself, so the peer pid is the
   Claude pid, not its parent). The mod holds no token and makes no network call.
3. **Sharing decisions can be made in the terminal** (owner, 2026-10-04): Allow or Decline a
   join request, Pause, Unpause.
4. **Only the owner's machine can press them.** A live terminal carries an admitted teammate's
   keys and mouse on the same pty as the owner's, and the mod cannot tell them apart. There is
   no paused concept (owner ruling 2026-10-05): people have read or write access, and remote
   input is never frozen while a request waits. Instead, when the mod sends a decision that
   widens access (`allow`, `unpause`) and any current viewer of that session has `canType`
   (a remote writer is connected, per the stored presence for that generation), the app shows a
   native macOS dialog on the owner's Mac ("Let Mira write to this session?" / "Resume sharing
   fix-login?", Allow/Cancel) before it forwards the decision. The app process draws it, never
   the pty, so a remote writer's keys cannot answer it. Cancel replies `409 {"error":"cancelled",
   "message":"Cancelled on this Mac."}`. With no remote writer connected the decision goes out
   with no dialog. `decline` and `pause` narrow access and never ask.
5. **A Mac may write these decisions for its owner.** Today a device token reaches read routes
   only (`routers/messaging.py`, `device_human`). This adds one write door, device-authed like
   `/messaging/native-session-tools`: `POST /messaging/native-session-decisions`
   `{sessionId, generation, decision: allow|decline|pause|unpause, userId?, requestId?}`. The
   backend checks the row is the device's own, current generation, connection live, and calls
   the existing `decide_join` / `set_sharing` with the owner's `AuthContext`. Same audit rows,
   same notifications. Approved by the owner, 2026-10-04.
6. **Teammate rows are redrawn for the person only.** The mod hooks `ui.render` on
   `UserMessage` with `origin.kind` `peer` and draws "Aden Kim · via Lightbulb", "Friday ·
   agent" or "You · from Lightbulb". The model still reads the labelled text, unchanged; the skill
   depends on it.
7. **The rules become code.** While a turn started by a teammate message runs, the mod's
   `tool.call` hook refuses writes to `.claude/settings*.json`, `~/.claude/settings.json`,
   hooks, `CLAUDE.md`, `AGENTS.md` and `.codex/` config, with the reason in the result. The
   skill stays as the explanation.
8. **Not in this work: a live picture for plain sessions** (the mod streams reply text, tool
   and status to the app). Parked by the owner, 2026-10-04. The shell switch stays opt-in.
9. **Presence reaches the Mac** (§4.2): who watches, who may type, who is typing, pending join
   requests, paused.

## 3. What the person sees

The band above the prompt, one line, only while sharing is on:

| State | Band |
|---|---|
| Live, nobody watching | `● Live in Lightbulb` `[Pause]` |
| Live, watched | `● Live · Aden, Mira watching · Aden typing` `[Pause]` |
| Transcript (plain session) | `● Shared as a transcript` `[Pause]` |
| Paused | `○ Sharing paused` `[Resume sharing]` |
| Join request | `Aden Kim asked to join` `[Allow]` `[Decline]` |
| Remote approval pending | `Waiting on you here or in Lightbulb` |

On surfaces that do not draw (VS Code chat, `claude -p`) the hooks still run; nothing shows.
Below v2.1.287 the plugin does not load and today's behaviour stays.

## 4. Contract

### 4.1 Mod ↔ desktop app: `mod.sock`

`<hook dir>/mod.sock` (beside `tool.sock`), uid-checked. HTTP/1.1, JSON, `Connection: close`.
The app resolves the caller: the peer pid must be the `claude` process of exactly one live,
unrestricted, mirrored session (`native_tools.rs::resolve_source`, minus the parent step).
Otherwise `404 {"error":"not_shared"}`; the mod shows nothing.

- `GET /v1/state` →
  ```json
  {"sessionId":"…","paused":false,
   "terminal":"held"|"attach"|null,
   "viewers":[{"userId":"…","name":"Aden Kim","canType":true,"typing":false}],
   "joinRequests":[{"userId":"…","name":"Mira Shah","requestId":"…"}],
   "approvalWaiting":false}
  ```
  `paused` is the row's `unshared`. `approvalWaiting`: approval.rs holds this session's prompt
  for a remote answer. The mod polls every 2 s.
- `POST /v1/decide {"decision":"allow"|"decline"|"pause"|"unpause","userId"?,"requestId"?}` →
  `200 <state>` or `409 {"error":"…","message":"…"}`. The app forwards to §4.3 with a short-lived device JWT, as `native_tools.rs` does.

### 4.2 Backend → Mac: presence

- New FEATURES entry `sessionPresence`. Only a Mac that sees it and advertises
  `sessionPresence` gets the frame below (an older desktop drops its connection over an unknown
  frame).
- The device-heartbeat ack carries `presence: [{sessionId, generation, paused, viewers,
  joinRequests}]` for this Mac's live sessions that have any viewer, pending request or pause
  (the backstop; shapes as in §4.1; `viewers` from `VIEWER#` rows, `canType` from
  `allowedSenders`/team control, `typing` from the viewer row's `typedAt` (set on each admitted keystroke batch) in the last 10 s; names from
  `human_display_name`). Trimmed to `MAX_ENVELOPE_BYTES` like `watching`.
- A `local.session.presence {sessionId, generation, …same}` frame is sent, best effort, when a
  join request is made or answered (so the band shows it at once). Everything else rides
  the ack.
- Every entry carries `at`, the server milliseconds taken before its row was read, and the ack
  carries `presenceAt`, the same for the whole ack. The ack and the frame are independent posts,
  so the Mac keeps per session only the entry with the greatest `at` (equal replaces; no `at`
  counts as 0), and removes a session the ack leaves out only when what it holds has
  `at <= presenceAt`. Reads also check the caller's generation.

### 4.3 Mac → backend: decisions

`POST /messaging/native-session-decisions` (device auth: `native_session_device`)
`{sessionId, generation, decision, userId?, requestId?}`. Refused unless the row's
`humanUserId`/`deviceId` are the device's, `generation` is current, the session has not ended,
and the row's connection is this device's. Then `decide_join` (allow/decline, `requestId`
required) or `set_sharing` (pause/unpause) as `AuthContext(device.user_id, device.org_id)`.
Returns the public session. Audit and notifications as the web path.

### 4.4 Install

The plugin ships in the app bundle as a folder marketplace
(`Contents/Resources/mods/.claude-plugin/marketplace.json`, plugin `lightbulb-sessions`,
`"source": "./lightbulb-sessions"`). Turning Claude sharing on runs
`claude plugin marketplace add <folder>` and `claude plugin install lightbulb-sessions@lightbulb`
with the provider binary `provider_program` finds; turning it off uninstalls it. A failure is
logged and never blocks sharing (the mod is an addition).

## 5. Pieces

| Piece | Where |
|---|---|
| Mod | `apps/desktop/src-tauri/mods/lightbulb-sessions/` |
| `mod.sock`, decide, confirmation dialog, install | `local_sessions/mod_socket.rs`, `enrollment.rs`, `transport.rs` |
| Presence, decision door | `core/services/session_presence.py` (new), `session_screen.py` ack, `routers/messaging.py` |

Not touched: `cli.rs`, `session_start.rs`, status wording (owned by the native-session
session), `ExplicitSharingNotice.tsx` (#3716).

Desktop changes are verified on prod after a release (no dev desktop builds).

## 6. Open

- The live picture for plain sessions (decision 8).
- Later: `/handoff`, an in-terminal room pane.
