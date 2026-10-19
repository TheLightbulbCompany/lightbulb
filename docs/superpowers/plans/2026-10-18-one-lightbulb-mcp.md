# One Lightbulb MCP, and joining without the app

Owner ask, 2026-10-18: one MCP implementation instead of two, the local
cleanup, and a way to join from a machine with no desktop app, scripted.

## Where we are

| Server | Code | Acts as |
|---|---|---|
| `lightbulb` (Mac) | `native_mcp.rs` (schemas, validation) + `native_tools.rs` (routing) | the calling session, proven by the Mac's device key |
| `lightbulb` hosted | `core/services/hosted_mcp.py` (FastMCP) | a person, by Clerk OAuth |

The same 9 tools are written twice: descriptions, argument checks and
routes. The backend services under both are already shared. Only the MCP
surface is duplicated.

## The design

1. The backend's `/mcp` is the only tool implementation.
2. `/mcp` takes two credentials: Clerk OAuth (a person) or the Mac's device
   JWT (a session). The session is a signed `source` claim inside that JWT,
   so the device key vouches for it; the backend's native path then checks it
   is this Mac's current session. Owner approved this auth on 2026-10-18.
3. The Mac's `session-mcp` becomes a pipe. It finds the calling session
   locally, as today, mints the device token, and forwards `tools/list` and
   `tools/call` to `/mcp`.
4. A tool reads `caller.session` when it needs it. `send_message` sends as
   the session. `use_browser` defaults to the caller's own session.

Kept on the Mac, because only the Mac can know it: which session is calling
(process tree), the Codex turn check, the first-turn warm-up retry.

## Track: product (Forge builds, in order)

- [x] P1. Backend: `Caller` type (person or session) in `hosted_mcp.py`. Put
  device-token verification (`machine_auth`) on `/mcp`, with the session
  claim checked against the `local-sessions` row (owner, device, generation).
- [x] P2. Backend: move the native-only behaviour into the shared tools:
  prepare-send for a session caller, `isThisSession` in `find_sessions`, and
  `use_browser` with no `session_id`.
- [x] P3. Backend tests: `test_hosted_mcp_session_caller.py`.
- [x] P4. Mac: `session-mcp` forwards `tools/list` and `tools/call` to `/mcp`,
  with the `source` claim in the device JWT. Delete the tool list,
  `internal()` and `read()`. Keep `PRIVATE_TOOLS` and `canonical_hook` (they
  key on tool names, which do not change). Unknown-backend fallback: if `/mcp` is
  unreachable or older, report the error; do not keep a second copy.
- [ ] P5. Desktop release. Then delete the old device routes
  (`messaging/native-session-tools*`, `local-sessions/device/{rooms,tasks,…}`)
  once no app older than that release reports in (check `stats`).

## Track: joining without the app (after P1-P6)

Reason Network's model, adopted where it fits us. The server stays the hub.

- [ ] J1. Split a headless `lightbulb` binary out of `src-tauri`: the
  local-sessions daemon, holder, `session-mcp`, hooks. No Tauri, no webview.
  Key in Keychain on macOS, a 0600 file on Linux.
- [ ] J2. Join ticket: a member makes a single-use, expiring ticket in
  Settings (or by API with their own Clerk token). `lightbulb join <ticket>`
  generates an Ed25519 key and posts the public key with the ticket. The
  backend pairs that device to that person, as `/desktop/pair` does today.
- [ ] J3. `lightbulb setup`: wire Claude Code and Codex (MCP, hooks, skill)
  with no prompts. Install a launchd or systemd user service.
- [ ] J4. Script use: `lightbulb send <to> --from <session>` and
  `lightbulb listen --as <name>`.
- [ ] J5. The desktop app runs the same binary as its daemon (one code path).

## Track: cleanup

- [x] C1. Removed the hand-added `lightbulb-hosted` entry from the owner's
  `~/.claude.json` (2026-10-18).
- [ ] C2. Owner's app is 0.4.853 and latest is 0.4.868. Updating it rewrites
  `lightbulb-browser` from `npx …@latest` to the bundled pinned copy (#4083).

## Later

- Signed approvals (Reason's `ask_user(about=)`), signed by the approver's
  device key, kept on the receiving machine. Phone and web have no device
  key: a passkey assertion is the candidate there.
- Delivery tier per session (push, turn, pull) in `find_sessions` and in
  each send's result.
- A LAN fast path (iroh) for live terminal frames between two machines.
  Not before the server path costs us something measurable.

## Not adopting

- Peer-to-peer messaging with no server: we need the ordered log, history
  and revocation.
- Machine keys as the trust root: Clerk membership stays the root.

## Rollout order (P1-P4 are one PR)

1. Prod-deploy the backend before any desktop build that carries P4: a P4 Mac
   against an older backend gets 401 from `/mcp` and lists no tools.
2. Verify on dev with a locally built app (`LIGHTBULB_API_BASE` = dev): every
   tool from Claude Code and Codex, a send's receipt, a refused send's reason
   hidden in the shared transcript, `use_browser`, Codex first-turn send.
3. Then dispatch `desktop-build.yml`.
