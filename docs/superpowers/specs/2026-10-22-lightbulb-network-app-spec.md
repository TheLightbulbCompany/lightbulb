# lightbulb-connect: the app (spec)

Status: draft for owner review, 2026-10-22. Part of `2026-10-22-lightbulb-network-p2p-design.md`; read its "Where
each thing lives" table first. `2026-10-22-lightbulb-network-architecture.md` is binding over this spec where
they differ. Ops are listed by plan in "Interfaces consumed" (architecture §9), with snake_case fields. The app
never calls `register`: that op is for hook and MCP paths, which identify their caller.

## Intent and why

The engine is the product. It runs on every computer, free, with no app. The app is the Lightbulb desktop
program that sits on top of it and makes it pleasant: a sessions list, notifications you can click, a switch,
and the things that must stay private (the app owns the features of sharing a live terminal, the session browser, resume; the engine only carries the bytes).

The app renders and asks. It carries no transport, no delivery, no membership and no policy logic. If a feature
needs one of those, it is an engine feature and the app calls it.

Today the app is the opposite. `local_sessions/transport.rs` (7,817 lines) holds a socket to our backend and
mirrors every session through it. That is the hub. This spec removes it from the app. After cutover the app
talks to one thing: the engine daemon on the same computer, as a socket client. The engine reaches the Mac as a bundled, signed binary that the app installs at the canonical path; it is never a crate linked into the app at runtime.

## Goals

1. One engine per computer (and one network per computer). The app attaches to the running daemon. It starts
   one only if none runs. It never starts a second.
2. A sessions sidebar that shows the engine's directory: own sessions and teammates' sessions, with person,
   agent, folder, busy or idle, what it is doing, and delivery tier.
3. Held messages are easy to answer: a Mac notification reading "<Agent> via <Person> wants to…" with the text;
   a click opens the session with Allow and Decline; "Always allow" is one step away.
4. The "agents send without asking" switch (`agents_send_without_asking`, for the person's own linked cloud
   member), and the "Share every new session" setting.
5. Live-terminal sharing, the session browser and resume work as they do today for the person's own sessions;
   sharing to others rides engine `stream`s under a person-signed `share_grant`.
6. A person with no account and no workspace gets a complete, working app for the network. Nothing nags for a
   card.
7. A workspace member gets the same, plus the workspace shell (chat, Friday, teams), unchanged.
8. The hub-era code leaves the app in the one cutover release, together with the backend's and the bridge's
   half (architecture §11). The list is exact (see "What goes, and what stays"). There is no intermediate build.

## Non-goals

- Any network, crypto, membership, hold, receipt or delivery code in the app. The engine owns them.
- Remote typing by teammates, teammate-requested resume and session pick-up (taking over a teammate's session).
  These rode the hub. They are out at cutover and return as envelope kinds `input`, `resume`, `pickup` under the
  same hold rules in Plan D, unless Plan C has room. Phone-answered Claude permission prompts are different:
  they ride the `decision` path and stay (Plan C).
- Windows and Linux. macOS only.
- A second copy of the sessions list for free people and another for members. One component serves both.
- Using the app as the only way to answer a hold. The OS dialog and the CLI work without it.

## Anti-goals (a failure even if it works)

- The app needs to be installed or signed in for the engine to work.
- The app asks a free person for a plan, a card, or a workspace.
- Two daemons on one Mac, or the app and the daemon holding different state about the same session.
- The app answers a hold on its own, from an agent's request, or from page script.
- The app stores message text or keys that the engine already stores.
- Notification text leaving the Mac (no push relay for the free path).

## User flows

### First launch, engine already installed

1. App starts. It calls `ping` on `~/.lightbulb/connect/sock`.
2. Daemon answers with `version` and `protocol`. If the protocol is in the app's supported range, the app
   attaches: `subscribe`, `ui.claim`, then `sessions` and `holds` for the first paint.
3. The sidebar appears within one second. No dialog. The app does not rerun `setup`; it runs the engine's
   `doctor` command and, if an agent is unwired, shows one line "Cursor is not connected. Connect" that runs the
   engine's `setup` for that agent.
4. If the engine is older than the bundled one but in range, the app leaves it alone. See "Upgrade".

### First launch on a bare Mac

1. `ping` fails (no socket, no lock). The app shows one screen: "Lightbulb will connect these agents:" and lists
   the agents it found (Claude Code, Codex, Cursor, Grok) and the files it will edit, each with a backup note.
   One button: Connect. Consent is given once here, as with Desktop pairing.
2. Connect copies the bundled, signed engine binary to the canonical path
   `~/.lightbulb/connect/bin/lightbulb-connect`, writes `app-install.json` (self-update is then off and the app
   ships and updates the engine), runs `lightbulb-connect setup`, then `lightbulb-connect daemon --install` (the
   launchd job `company.thelightbulb.connect`; it also links `~/.local/bin/lightbulb` to the binary). The app
   runs the engine's own commands. It writes none of the agent files itself.
3. App waits up to 5 s for `ping`, then attaches as above. Failure: see "Daemon not running".
4. The app asks nothing else. Sign-in is a menu item ("Sign in to verify your name"), never a gate.

### A held message arrives

The engine raises the hold. The app only shows it.

| State | What happens |
|---|---|
| App open, window focused | In-app banner at the top of the sidebar with Allow / Decline. No Mac notification. |
| App open, window not focused | Mac notification "<Agent> via <Person> wants to…" plus the first 140 characters. Dock badge counts holds. |
| App running, window closed (menu bar only) | Same Mac notification. |
| App not running | The daemon shows the OS dialog (the engine's fallback), and the person's linked workspace can relay a web or phone Allow. The app plays no part. |
| Mac asleep | Nothing shows. The hold waits in the daemon. On wake, the app calls `holds` and posts a notification for every hold not yet notified. The sender's receipt reads `held` throughout. |

Rule: while an app holds the claim (`ui.claim`, a 20 s lease with a 7 s heartbeat), the daemon does not show its
OS dialog. When the claim lapses the daemon shows the dialog for every open hold. A person never loses a hold
because the app crashed. A duplicate surface in the race is tolerated; both resolve the same hold.

**Who answers a hold.** The person on their computer (the app, the CLI `approve`/`decline`, or the OS dialog), or a `decision` frame relayed by the person's own `bridge`-role member (web and phone Allow). The holding computer accepts a relayed decision only if the signer's `kind=bridge` proof `sub` is the hold target's `person_id`, the held message's sender is not one of that bridge's own sessions, the scope is `once` or `person` (policy switches are never relayed), and the nonce and `hold_id`/`holding_member` match; it applies it and signs the record with its own key. First answer wins; a second gets `not_open`.

### Allow, Decline, Always allow

- Notification buttons: **Allow** and **Decline**. Clicking the body opens the session with the same two buttons
  and the full text.
- **Allow** delivers this message and writes `people[person_id] = accept`. Their next message, from any of
  their computer sessions, goes straight in (checklist 3). `person_id` is sticky: signing in later, or a proof
  expiring, never flips an Allow.
- **Decline** refuses this message (receipt `refused`). The next message holds again.
- **Always allow** appears in the session view and the notification's action menu. On a hold from *that
  person's* cloud member (their Friday, their Muse) it writes `cloud_of[person_id] = accept`. The "agents send
  without asking" switch is `agents_send_without_asking`, for the person's **own** linked cloud member.
  Defaults: own sessions `accept`, everything else `hold`.
- Every answer is one `hold.answer` call. The engine signs it with the computer key. The app shows the result
  from the engine's `hold.resolved` event, not from its own click.

### A teammate's session appears

1. The engine's `directory.changed` event arrives. The app calls `directory`.
2. The session appears under the teammate's name, grouped by person then computer. Row: agent icon, folder, dot
   for busy or idle, one line of "doing", and a small tier tag (push, next step, pull).
3. No notification. A new teammate session is not an event worth interrupting for.

### Opening a teammate's session

1. Click the row. The right pane opens the session view: header (person, agent, computer, folder), the "doing"
   line, the delivery tier, and a message box.
2. The message box calls `send` with `to` set to the session key. It shows the receipt as it changes
   (`queued`, `stored`, `delivered`, `read`, `held`, `refused`, `expired`) and where mail waits if the session is not live.
3. If the teammate shares a live terminal and you are allowed, the pane shows it (next flow). Otherwise it shows
   the conversation summary the engine has, or the existing "screen is not shared" notice. Never a blank pane.

### Sharing a live terminal

1. The person turns sharing on for a session (the button, `Share every new session`, or the share skill's
   `POST /v1/share` on the app's local mod socket, unchanged).
2. The app attaches to the session's holder as a `watch` client and reads terminal frames, as `screen.rs` does.
3. The person's Share action mints a person-signed `share_grant` (`share.grant`: session, viewer member, input
   false, expiry). The app hands the frames to the engine's stream channel (`stream.open`, `stream.send`). The
   engine moves them, encrypted, only to the viewers a grant names, and accepts `stream.open` from the same
   `person_id` or a granted member. The app owns the feature; the engine carries the bytes. The app never
   opens a socket to another computer.
4. A watcher's app calls `stream.subscribe` and renders the frames with the terminal view it already has.
5. Sharing off, session end or app quit calls `stream.close`. The frames stop that moment.
6. Browser and phone viewers reach the stream through the bridge, which reads it only because the person granted it to their bridge member ("Anyone with the link" viewing is a bridge feature over such a grant).

### Upgrade of app vs engine

When the app manages the engine (`app-install.json`), the app ships and updates it with its own Tauri updater; otherwise the engine updates itself from signed releases. They
move on their own clocks. The contract is the `protocol` integer from `ping`.

| Case | Behaviour |
|---|---|
| Engine protocol inside the app's range | Attach. Versions may differ. |
| Engine older, below the app's minimum | The app asks to replace it: "Update the Lightbulb engine". On yes: install the bundled binary, `shutdown`, relaunch the daemon. Holds survive in the database. |
| Engine newer, above the app's maximum | The app does not render sessions or answer holds. Banner: "Update Lightbulb to keep using this app." The claim is not taken, so the OS dialog covers holds. |
| App upgrades while sessions share a terminal | Streams close on quit and reopen on relaunch. No message is lost; messages never ride the stream. |

## Threat model

### A notification click delivering to the wrong session

- A notification carries `hold_id`, the `session_id`, and the session's `birth_micros`, all from the hold. It
  carries no text the app acts on.
- On click the app calls `hold.get {hold_id}`. It proceeds only if the daemon returns that hold still open and
  the same `session_id` and `birth_micros`. A reused pid or resumed session fails the birth check.
- Otherwise the app shows "That request is no longer open." It never falls back to a nearby session.
- Allow and Decline carry only `hold_id`. The daemon applies them to the hold, not to "the session now at the
  top". A stale button answers a resolved hold with `not_open` and does nothing.
- Test: raise two holds from two senders, resolve the first, click the first notification, assert no effect on
  the second.

### An Allow forged by a web page

The app's webview loads remote pages for workspace members (`https://thelightbulb.company/chat`); the free path loads no remote origin. Any script on a remote page, or in an iframe, could call a Tauri command.

- `hold.answer` is not exposed to the webview as a bare command. The webview may call `network_hold_request`,
  which only asks the Rust side to show a native confirm sheet (AppKit alert) with sender, agent and text. The
  Rust side calls `hold.answer` only after the person presses the sheet's button. A click inside the webview
  alone does nothing.
- Notification buttons are answered in Rust from the notification's own `userInfo`, as `held_notice.rs` does
  today. They never pass through the webview.
- Tauri capabilities for network commands are bound to the main window and to the app's own origins only. A
  navigation to any other origin loses them.
- The daemon refuses `hold.answer` when any ancestor of the caller is a known agent process (Plan A's
  `caller::identify`). An agent's shell cannot answer, with or without the CLI.
- Test: from the devtools console of a remote page, call every network command. Assert `hold.answer` is never
  reached without a sheet press, and the sheet text is the engine's, not the page's.

### The app answering for a session it does not own

- The app only lists and answers holds the daemon returns from `holds`. That list holds only holds on this
  computer's own sessions. A teammate's session never appears in it.
- The daemon checks that the hold's target belongs to this computer before signing. The app is not trusted to
  do this check.
- The app holds no key. Signing happens in the daemon. A compromised webview cannot sign anything.
- Workspace-side Allow (web, phone) goes through the bridge as a `decision` frame to the engine, under the acceptance rules above. The app has no code path to it.
- Another app instance, or a stale app, answers the same hold: the second answer gets `not_open`.
- The socket is 0600 and same-user only. A same-user process can still call it. That is the engine's trust
  boundary, and the ancestor check above is its only guard. We do not claim more.

## Failure modes

| Failure | What the app does |
|---|---|
| Daemon not running at launch | Start it (`daemon --install` if the launchd job is missing, else `launchctl kickstart`). Wait 5 s. Still down: sidebar shows "The engine is not running. Start it" with a button and the last `doctor` output. The app never shows an empty list as if it were true. |
| Daemon dies while the app runs | Events stop. After 3 s without a heartbeat, the status chip turns red and the list greys out with an "as of 12:41" stamp. The app retries every 2 s, then `launchctl kickstart`. On return it re-reads everything (`sessions`, `holds`, `directory`). |
| Engine newer than app | See Upgrade. Read-only banner; no sessions; no answers. |
| Engine older than the app's minimum | See Upgrade. Offer replace; do nothing silently. |
| Two apps (a second user session, or a dev build) | Each takes `ui.claim`. The daemon keeps one claimant by user session and rejects the second with `claimed_by_other`. The second app shows the list read-only and no banner. A hold is never answered by both: the daemon answers once. |
| Notification permission denied | The app does not ask again in a loop. The in-app banner, the Dock badge and the menu-bar count carry holds. Settings shows "Notifications are off in System Settings" with a button that opens the pane. If the app is closed the daemon's OS dialog still appears (it does not need the app's permission). `native_notify` already answers false when macOS permission is not granted; keep that check. |
| App open but window closed and the tray hidden | Notification still posts. If that fails, the claim is released so the OS dialog shows. |
| Directory stale (teammate offline) | Rows dim with "last seen"; messages show `stored` and where they wait. |
| Terminal watch refused (someone else already watches) | Existing behaviour: "Being watched elsewhere". Not retried in a loop. |
| Codex pane blank (known bug being fixed) | A session with no terminal frames shows its notice (`sessionScreenNotice`) or "Waiting for the first screen", never an empty box. Asserted in acceptance. |
| Resume refuses a session that is not usable (known bug being fixed) | The resume guard asks the daemon (`sessions`, status plus birth-time check), not the app's own mirror map. "Already running" is shown only for a Live session. Asserted in acceptance. |

## Data

What the app stores that the engine does not. Everything else is read from the engine every time.

| Item | Where | Why the engine does not hold it |
|---|---|---|
| Window, pane width, last selected session, collapsed groups | webview local storage (existing) | Pure view state. |
| Notified hold ids (ring of the last 200) | `~/Library/Application Support/company.thelightbulb.desktop/notified.json` | Stops a duplicate banner on reconnect. Cleared when `hold.resolved` arrives. |
| Share every new session (boolean) | `desktop_permissions.json` (existing store) | Sharing a terminal is app-owned. |
| Per-session share flag and the holder id of each shared terminal | existing `local_sessions` mirror state, shrunk to this | The holder is the app's. |
| Notification permission state and "asked once" flag | existing `notify.rs` state | OS permission is per app. |
| Session browser index (ended sessions that can resume) | existing `session_browser.rs` cache, rebuilt from the agents' own transcripts | Not a network concern. |
| Workspace sign-in (Clerk) and desktop pairing | existing keychain entries | Optional; the engine never needs it. |
| Engine install record (path, version installed by the app) | `~/.lightbulb/connect/app-install.json` | Lets the app tell its own copy from a Homebrew one. |

The app never writes keys, membership, policy, receipts, messages or outbox. It never reads another
computer's message text except what `send` returns to it as a receipt.

## Interfaces consumed

Transport: the engine's Unix socket, newline-delimited JSON, as Plan A Task 2. The app is one more client.
Ops by plan (architecture §9). **A:** `ping`, `status`, `subscribe`, `register` (hook and MCP paths only; the app
never registers), `sessions`, `end`, `send`, `receipt`, `inbox`, `hook`, `tool`, `shutdown`. **B:** `identity.*`,
`team.*` (incl. `join_requests`, `admit`), `directory`, `holds`, `hold.get`, `hold.answer`, `policy.get/set`,
`ui.claim`, `share.grant`, `stream.*`, `reload`. **C** adds none (the bridge uses the engine as a crate). NEW
below means Plan B. Fields are snake_case; errors are `{ok:false, error:"<code>"}` from a closed list per op. The
app uses these ops only and never reads `state.db`.

### Core

| Op | Body in | Body out | Notes |
|---|---|---|---|
| `ping` | none | `{ version, protocol, min_app_protocol, instance_id, started_at }` | Extended. `protocol` is an integer. `instance_id` changes on each daemon start; the app drops cached state when it changes. |
| `status` | none | `{ identity: {name, verified, proof_kind?}, team: {id, name, role}?, computer: {name}, relay: {state}, notifier_claimed: bool, outbox: n }` | One call for the header and settings. No workspace fields. |
| `subscribe` | `{ topics: ["sessions","directory","holds","receipts","status"] }` | stream of events | Replaces polling. The connection stays open and receives `{event, ...}` frames. |
| `ui.claim` NEW | `{ kind: "notifier", app_instance, ttl_s: 20 }` | `{ ok, claimed_by? }` | Heartbeat every 7 s. Lapse means the daemon shows the OS dialog. |
| `shutdown` | none | `{ ok }` | Used for the engine replace flow. Refuses while a hold is mid-answer. |
| `doctor` (CLI) | none | per-agent wired or not | Not a socket op: the app runs the engine's `doctor` command (Plan A Task 10). |
| `setup` (CLI) | `{ agent?, remove? }` | the `Report` of Plan A Task 9 | Not a socket op: the app runs the engine's `setup`; it does not edit agent files. |

### Sessions and directory

| Op | Body out |
|---|---|
| `sessions` (Plan A) | This computer's `[Session]` with `session_id, provider, conversation, pid, birth_micros, cwd, title, doing, status, tier`. |
| `directory` NEW | All known sessions across the team: the same shape plus `person {id, name, verified}`, `computer {id, name}`, `repo`, `branch`, `last_seen`. Own and teammate sessions in one list, flagged `own`. |
| `send` (Plan A) | The app's message box. Body `{ from: "person", to, text, request_id }`. |
| `receipt` (Plan A) | By `request_id`. |
| `receipt` extension | `{ request_id }` returns `{ state, reason?, waits_on?: "recipient computer", expires_at? }` for the "where it waits" line; there is no separate `where` op. |

### Holds and policy

| Op | Body in | Body out |
|---|---|---|
| `holds` NEW | none | `[Hold { hold_id, from: {person, agent, label}, to_session_id, to_birth_micros, text, text_truncated, raised_at }]` of open holds on this computer. |
| `hold.get` NEW | `{ hold_id }` | The hold or `not_open`. |
| `hold.answer` NEW | `{ hold_id, decision: "allow"\|"decline"\|"always" }` | `{ ok, receipt_state }`. Refuses callers that descend from an agent process. Signs with the computer key. |
| `policy.get` NEW | none | `{ agents_send_without_asking: bool, people: {person_id: "accept"\|"hold"\|"refuse"}, cloud_of: {person_id: "accept"\|"hold"} }` |
| `policy.set` NEW | `{ agents_send_without_asking?: bool, person?: {person_id, mode}, cloud_of?: {person_id, mode} }` | `{ ok }` |

### Team and sign-in (thin)

| Op | Notes |
|---|---|
| `team.*` NEW (invite, members, remove, `join_requests`, `admit`) | The app shows buttons over the same ops the CLI `invite`, `join`, `remove` use. A join request raises a hold-shaped prompt ("<name> wants to join your network"); a link minted `--open` skips it. |
| `identity.*` NEW (`attach_proof`) | `{ proof }`. After Lightbulb sign-in the app obtains a proof and hands it over. The engine verifies offline. The app never interprets it. |

### Stream (live terminal)

| Op | Notes |
|---|---|
| `share.grant` NEW | `{ session_id, viewer_member, input: false, expires_at }` mints the person-signed `share_grant`. |
| `stream.open` NEW | `{ session_id, grant_id? }` returns `{ stream_id }`; accepted for the same `person_id` or a granted viewer. |
| `stream.send` NEW | `{ stream_id, seq, bytes }` (≤64 KB). Opaque to the engine. |
| `stream.subscribe` NEW | `{ session_id, from_member }` returns frames as events. |
| `stream.close` NEW | `{ stream_id }`. |

### Events (engine to app)

`session.changed {session_id}`, `directory.changed`, `hold.raised {hold}`, `hold.resolved {hold_id, decision, by}` (`by` is `local` or `bridge`),
`receipt.changed {request_id, state}`, `status.changed`, `daemon.stopping`.

Every event is a hint. After a reconnect the app re-reads full state with the request ops and ignores any event
older than that read. Events carry no state the app needs to keep.

## Interfaces owned

Tauri commands the webview may call. All are bound to the main window and the app's own origins.

| Command | Does |
|---|---|
| `network_status()` | `status` plus engine/app protocol check result: `ok`, `engine_too_old`, `engine_too_new`, `down`. |
| `network_sessions()` | `directory` (and `sessions` when the team is empty). |
| `network_send({to, text, requestId})` | `send` as the signed-in person; returns the first receipt. |
| `network_receipt(requestId)` | `receipt` and `where`. |
| `network_holds()` | `holds`. |
| `network_hold_request({holdId, decision})` | Shows the native confirm sheet. Calls `hold.answer` only after a button press. Returns the resolved state. This is the only way for the webview to answer. |
| `network_policy_get()` / `network_policy_set(patch)` | Reads and writes policy, including the switch. |
| `network_team_*` | Invite link, members, remove. |
| `network_connect_agents({agent?})` | Runs the engine's `setup` after the consent screen. |
| `network_install_engine()` | Bare-Mac flow. Copies the bundled binary, runs `setup` and `daemon --install`. |
| `network_replace_engine()` | The upgrade flow. |
| `share_set({sessionKey, on})`, `share_every_new_session_set(bool)` | Terminal sharing (unchanged by this spec). |
| `session_resume({conversation})` | Resume an ended session. Guard asks the daemon. |
| `session_browser_list()` | Existing. |

Events to the webview: `network:status`, `network:sessions`, `network:holds`, `network:receipt`. They are
re-emits of engine events, debounced to 100 ms.

The local mod socket (`mod_socket.rs`, `POST /v1/share`) stays as is.

### One component, three surfaces

One React component `NetworkSessions` (in the frontend package, no `next/*` imports) renders the sidebar,
the session view and the hold banner. It takes its data from the commands above through an injected adapter.

- Free person, or signed out: the bundled free `/network` page, shipped inside the app and loaded from the app's
  own origin. There is no remote origin on the free path, so it works offline and needs no Clerk session; it
  also carries the status chip and the hold buttons.
- Workspace member: the same component mounts in the workspace shell's sidebar (`WorkspaceSidebar`) above the
  rooms.
- Web and phone never mount it for engine data. They get the bridge's `find_sessions`.

### What a signed-out or workspace-less person sees

- Sessions (own and teammates), holds, the switch, Share every new session, invite and team, "Sign in to verify
  your name" in the menu.
- Not shown: Friday, rooms, chat, tasks, Teams, billing, pricing, marketplace. No upsell card. A single quiet
  line in Settings, "Add a Lightbulb workspace to bring Friday and cloud agents in", with no button that starts
  checkout.
- The "agents send without asking" switch is shown, but with no linked workspace there is no cloud member, so
  its help text says "Applies to agents from your Lightbulb workspace" and it is off and disabled. A workspace
  needs a Lightbulb workspace; a trial counts.

### What a workspace member additionally sees

The existing shell, unchanged. The sessions list now also shows hosted agents' presence from the bridge as
members of the team (a workspace appears as a computer named for the workspace). The member's phone and web
Allow works through the bridge; the app reflects the resolved hold from `hold.resolved`.

## What goes, and what stays

Reviewed from the file headers on 2026-10-22. Files not read in full are marked and must be audited in the
plan before deletion.

### Deleted at cutover (hub-era)

| Path | Why |
|---|---|
| `local_sessions/transport.rs` (7,817 lines), the whole file | The socket to the backend: registration, heartbeats, acks, clock anchor, relay of screens and approvals. It goes entirely; live terminals move onto engine `stream`s (a share grant), the rest is cut or moves to the engine. |
| `local_sessions/snapshot.rs` | Session pick-up author side: uploads transcripts and code to the backend. Hub only. |
| `local_sessions/approval.rs` | Remote answers to Claude permission prompts through the backend. Hub only. The feature returns over the `decision` path in Plan C. |
| `local_sessions/held_notice.rs` | Notification for a hub-held send; answers through a hub decision. Replaced by a new notice module fed by `holds` and `hold.answer`. Reuse `notify.rs`. |
| `local_sessions/presence.rs` | Viewers, typing and join requests as the backend said them. |
| `local_sessions/remote_input.rs` | A teammate's keystrokes forwarded by the backend (`local.session.input`). |
| `local_sessions/session_states.rs`'s relay flags (`READS_*`) and the `Mirror` calls into `transport` | Negotiation with the relay's version. The ended-screen keeping itself stays if still wanted. |
| `session_pickup.rs`, `session_continue.rs` | Taking over a teammate's session through the hub. Audit: delete if only `snapshot.rs` and the web use them. Pick-up returns as the envelope kind `pickup` in Plan D. |
| Frontend `useSessionPickup.ts`, `SessionCommitPr.tsx` hub-specific paths, and `LocalSessionPanel` code that reads hub-mirrored sessions | Replaced by `NetworkSessions`. |
| Backend `local_sessions.py`, `local_session_relay.py`, `local_session_grants.py` | Plan C deletes these. Listed so the app PR and the backend PR land together. |
| `/desktop/pair`'s session-relay URL (`relayUrl` for sessions) | The app has no relay to dial for sessions. The node relay for Mac execution (`node_client.rs`) is a different thing and stays. |

### The delivery code the engine owns, deleted in the same release

`claude.rs`, `codex.rs`, `hooked.rs` (never moved; the engine's Cursor and Grok code is written fresh),
`enrollment.rs`, `lifecycle.rs`, `native_mcp.rs`, `native_tools.rs`, `plugin.rs`. Plan A is engine-only and
touches none of these; the monorepo copies stay until this cutover release, which also removes the backend files
(`local_sessions.py`, `local_session_relay.py`, `local_session_grants.py`, `local_sessions_repo.py`, the
`/local-sessions/device/*` routes and the `lightbulb-local-sessions` websocket client).

### Stays (private, app-owned)

| Path | Role after cutover |
|---|---|
| `local_sessions/screen.rs` | Watches the holder; frames go to `stream.send` under a share grant, not the backend. The relay framing changes; the diffing stays. |
| `local_sessions/resume.rs` | Start an ended session again in a holder. Now triggered only by the person in the app. The backend `resume` list input is deleted. |
| `local_sessions/mod_socket.rs`, `start_links.rs`, `workspace.rs`, `stats.rs` | Not read in full. Default: stay if their input is local (a Claude mod, a provider hook, local files). Delete any part whose input is a backend frame. Audit in the plan. |
| `holder/`, `session_browser.rs`, `shell_integration.rs`, `session_start.rs`, `agent_chrome.rs`, `agent_cast.rs`, `desktop_permissions.rs`, `file_access.rs`, `node_client.rs`, `node_invoke.rs`, `notify.rs`, `tray.rs`, `local_mcp.rs` | Unrelated to the hub mirror, or the notification and permission machinery this spec reuses. |
| `apps/desktop/core` (holder, keychain, pair state) | Stays. The pair state is for Mac execution and workspace, not the network. |

## Acceptance checklist

On a real Mac, app installed from the cutover build, a second account on a second computer as the teammate.
Each line is run and its output recorded.

1. **Bare Mac.** Fresh user, no `~/.lightbulb`, app installed. Launch. One consent screen. Press Connect. The
   sidebar shows within 10 s; `lightbulb doctor` is green for each installed agent, and `which lightbulb` resolves to the canonical path.
2. **Engine present.** Install the engine first with the public line, then launch the app. The app attaches;
   `pgrep -f "lightbulb-connect daemon"` shows one process before and after.
3. **No app needed.** Quit the app. A teammate's first message produces the OS dialog. Allow in the dialog.
   Delivered, receipt `delivered`.
4. **Hold with app open and focused.** Banner with Allow and Decline; no Mac notification; the dialog does not
   also appear.
5. **Hold with app unfocused.** Notification reads "<Agent> via <Person> wants to…" with the text. Click it. The
   right session opens with Allow and Decline. Allow. The notification is withdrawn. The teammate's next message
   from a different agent goes straight in.
6. **Wrong-click test.** Two holds; resolve the first by CLI; click its notification. Nothing happens to the
   second; the app says the request is closed.
7. **Decline.** Receipt `refused`; nothing delivered; the next message holds again.
8. **Mac asleep.** Close the lid; the teammate sends; open the lid. One notification, one hold, receipt `held`
   then `delivered` after Allow. No duplicate on the second wake.
9. **App crash.** `kill -9` the app while a hold is open. Within 25 s the OS dialog appears.
10. **Always allow.** On a hold from a person's cloud member (their Friday), choose Always allow. Its next message goes straight in; it
    shows as `cloud_of[person] = accept` in Settings and can be removed there.
11. **Switch.** Turn "agents send without asking" on with a linked workspace. A message from your own Friday is delivered with no
    hold. Turn it off; it holds again.
12. **Free person.** Fresh Mac, not signed in, no workspace. Sessions, holds, invite, switch (disabled), and Share
    every new session work. No Friday, rooms or checkout appears anywhere. Airplane mode: the bundled fallback
    page still shows holds.
13. **Member.** Sign in with a workspace. Chat and Teams work. Sessions sidebar shows the same engine list. A web
    Allow resolves the hold and the app's banner clears from `hold.resolved`.
14. **Teammate session.** A teammate starts Codex. Its row appears within 5 s with person, agent, folder,
    busy or idle, and "doing" from the agent's own title. Open it; send; the receipt walks `stored`, `delivered`,
    `read`.
15. **Live terminal.** Share a session to the teammate (a share grant). The teammate's app shows the terminal within 3 s. Turn sharing off; the
    frames stop within 1 s. The Codex pane is never blank (it shows a notice or "Waiting for the first screen").
16. **Resume.** End a Claude session. In the session browser, press Resume. It starts. Press Resume on a session
    that is running: the refusal says "already running" only for that one. A session whose process is gone is
    resumable (the 2026-10-22 bug).
17. **Daemon down.** `launchctl bootout` the job. Within 5 s the app shows the red chip and restarts it; the list
    returns with no stale rows.
18. **Engine newer.** Install an engine with a higher `protocol`. The app shows the update banner, shows no
    sessions, takes no claim; the OS dialog covers a hold.
19. **Engine older.** Install a lower `protocol`. The app offers Replace; after yes, holds raised before the
    replace are still open.
20. **Two apps.** Run a second app instance as another user session or a dev build. The second is read-only; a
    hold is answered once.
21. **Notifications denied.** Deny in System Settings. The banner, Dock badge and menu-bar count carry holds;
    the app does not re-prompt.
22. **Forgery.** From devtools on a remote page, call every `network_*` command. `hold.answer` is never reached
    without a native sheet press; from an agent's shell the CLI `approve` is refused.
23. **Cleanliness.** `rg "transport::|local_session_relay|/local-sessions" apps/desktop` returns nothing.
    `cargo test` and the frontend tests pass. The deleted-files table above matches `git diff --stat`.

## Rulings

Rulings (architecture 2026-10-22), folded in from the earlier open questions:

1. **Always allow.** Allow writes `people[person_id] = accept` (all that person's computer sessions). Always allow on a hold from that person's cloud member writes `cloud_of[person_id] = accept`. The switch is `agents_send_without_asking` for the person's own linked cloud member.
2. **Hub-era features at cutover.** Teammate typing, teammate-requested resume and pick-up become envelope kinds `input`, `resume`, `pickup` in Plan D unless Plan C has room. Phone-answered Claude permission prompts stay, over the `decision` path (Plan C).
3. **Stream channel.** The engine has `stream.*`, opened only for the same `person_id` or a member named in a person-signed `share_grant`. The app owns the feature; the engine carries the bytes. No separate app-to-bridge connection.

### My calls, made

- One component serves free and member surfaces; the free surface is the bundled `/network` page (no remote origin).
- Webview answers only through a native confirm sheet; notification buttons answer in Rust.
- The notifier claim has a 20 s lease; lapse means the OS dialog.
- App refuses to answer or render when the engine's protocol is newer than it knows.
- The app runs the engine's own `setup`; it never edits agent files.
- No mobile push, no email, no telemetry from the app about holds.
