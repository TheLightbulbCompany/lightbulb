# One session list (design)

Status: founder rulings in §8, build contract in §10. Step 1 built 30 Oct 2026. Builder-checked once (see §9).

Research this rests on, with file:line for every claim (read these before planning):
- `docs/research/2026-10-30-sessions-visibility-map.md`: the three session systems as shipped.
- `docs/research/2026-10-30-sessions-design-vs-code.md`: what the 22 Oct design decided and what was built.
- `docs/research/2026-10-30-sessions-desktop-pipeline.md`: how the desktop app discovers, keeps and ends a session.
- `docs/research/2026-10-30-sessions-harness-surfaces.md`: what Claude Code, the Claude desktop app, Codex, Cursor, OpenCode and Grok let a third party see. Each claim marked confirmed or unconfirmed.
- `docs/research/2026-10-30-sessions-sidebar-coupling.md`: every sidebar field and action, its backend source, and the engine's equivalent.
- `docs/research/2026-10-30-sessions-autojoin-and-presence.md`: tickets, admission, login, the cloud member, and how presence frames evolve safely.

Words: the **engine** is the `lightbulb` CLI daemon (TheLightbulbCompany/cli), one per computer, bundled in the desktop app. The **network** is its peer-to-peer team. The **backend list** is `/api/v1/messaging/sessions` fed by the desktop app's relay. **Friday** stands for every workspace agent. A **row** is one session as a person sees it.

---

## 1. Intent and why

On 30 Oct the founder's desktop sidebar showed four of Aden's Claude sessions as Idle; the network showed two. The sidebar also listed about twenty "Stopped" sessions the network had dropped, and six sessions of Leonardo's, who has the app but was never on the network. Friday, the sidebar and the CLI each answer "which sessions exist and what are they doing" differently.

The cause is not a bug in one place. Lightbulb has three session systems that share no store, no id and no rules (visibility map §core). The 22 Oct architecture decided the engine's directory is the only list and the app renders it; the app half was never built (design-vs-code §A1). Meanwhile the desktop app grew its own liveness rules that can show a session as Idle with no live process behind it (desktop pipeline §2, §5).

Founder ruling, 30 Oct 2026: build the one honest list, from the engine, in three independently shippable steps, and confirm every third-party assumption before building. Network workers (teammates starting or resuming sessions on another computer) stay in the back pocket. "Who's who and which harness is good for what" is memory plus one skill rule, later, and is not in this document.

**Honest** means: one place decides each session's state, the owner's engine, from the harness's own records; every viewer shows that same state; a state is never frozen, guessed, or kept alive by a heartbeat.

---

## 2. Goals, non-goals, anti-goals

Goals
- One session id and one state for a session, wherever it is shown: `lightbulb who`, `find_sessions`, the desktop sidebar, web, phone, Friday's tools.
- The engine lists every session Claude itself lists, including Claude desktop-app sessions and background sessions, with Claude's own busy / waiting / idle words.
- Recently ended sessions stay visible for seven days everywhere, as ended, with a resume path for their owner.
- Offline is shown as offline, with when the computer was last seen, never as "idle, gets messages instantly".
- A workspace member who signs in to the desktop app is on the workspace's network without running an invite, and is told so.
- Shared sessions ("Live in Lightbulb": watch, ask to join, pick up, type with the owner's OK, resume, end) keep working unchanged.
- No field or message in the product contradicts the 26 Oct ruling that everyone on a network is trusted and nothing is held.

Non-goals
- Teammates resuming or starting sessions on another computer (network workers).
- Changing Friday's two tools (`lightbulb_local_sessions`, the network directory). After this work they agree; their shape stays.
- Codex, Cursor, OpenCode and Grok parity with the Claude discovery below. §4.1 says what each gets now; full parity is a follow-up with its own verification (harness surfaces §3-4).
- Deciding the sidebar's new layout here. The founder ruled on 30 Oct that the sidebar needs a redesign (a five-row cap is not good enough) with design research first; that is Step 4 (§5) with its own design document. This document fixes what a row means and where it comes from; Step 4 decides how rows are shown.
- Uploaded sessions (the capture upload timeline) and the web inspector.

Anti-goals (a failure even if it works)
- A sidebar row that is "Idle" while no process holds that conversation.
- A session that disappears from one surface while another still shows it live.
- A wire change that blanks an older engine's view of a computer (presence frames are validated whole, autojoin §6).
- Exposing a conversation id, a file path or a join line to anyone who should not have it.
- A setup or test run that replaces the person's real background service (this happened on the founder's Mac on 30 Oct).

---

## 3. Constraints

Third-party surfaces (harness surfaces §1-4; **C** confirmed, **U** unconfirmed)
- **C** `claude agents --json` is Anthropic's documented, supported way to read session state from outside Claude Code (2.1.145+; `--all`, `id`, `state` 2.1.169+). It lists interactive terminals, Claude desktop-app sessions (`entrypoint: claude-desktop`) and background sessions; `status` is `busy | waiting | idle`, `waitingFor` names the prompt; background rows carry `state: working | blocked | done | failed | stopped`. Claude maps its own `shell` state (turn over, a Bash task still running) to `busy`. A terminal parked into the agent view is listed only as its background job. About 0.25 s per call.
- **C** `~/.claude/sessions/<pid>.json` is undocumented and has changed shape across 2.1.2xx-3xx. It is still the only source of `messagingSocketPath` (the push socket) and the fallback below 2.1.145.
- **C** There is no "session ended" record anywhere in Claude; the pid file disappears and the listing drops the row. Titles come from the transcript (`custom-title`, then `ai-title`). `claude --resume <sessionId>` resumes; `claude attach <id>` attaches a background job.
- **C** Claude desktop-app sessions write the same pid file, appear in the listing, run the hooks in `~/.claude/settings.json` (the engine already has a hook-registered row for one on this Mac), and keep their transcript in the normal place. **U** Resuming one from a terminal was not run (it would be a second writer while the app's process lives).
- **C** Codex: hooks (`SessionStart` … `SessionEnd`, `PermissionRequest`) are documented and on by default; the shared app-server daemon's `thread/loaded/list` and `thread/status/changed` give busy / waiting-on-approval / waiting-on-input, and both are labelled experimental by OpenAI. `SessionEnd` can fire up to 30 minutes after the last activity. **U** Whether hooks fire inside the Codex app in ChatGPT: the docs are silent and no app thread has run since the engine started recording Codex rows.
- **C** Cursor, OpenCode and Grok have no machine-wide list of running sessions; each has a stored-session command and a resume flag.

Engine wire compatibility (autojoin §6)
- An extra optional field on `Remote` or `Presence` is safe: serde ignores unknown fields, and new peers read old frames with `#[serde(default)]`.
- A new value in `status` or `tier` is not safe: an older peer validates every session and drops the whole presence frame when one fails. A new frame variant stops old peers reading. The protocol version must not be bumped.
- The cloud member is pinned in the Hermes image (`apps/infra/hermes/Dockerfile`) and only moves on a tag flip plus a runtime roll. It will be an "older peer" of every Mac between an engine release and that roll.

Product and trust
- Owner ruling 2026-10-26: everyone in a network is trusted; nothing holds a message; an invite is the approval. The binding architecture doc still describes holds and must be amended (design-vs-code §C).
- A ticket is a bearer credential: admission checks no sign-in proof (autojoin §1). A join line is shown once and never logged.
- Macs cannot verify sign-in proofs (`ISSUER_KEYS` is empty); only the cloud member can. Person identity between Macs stays "not verified" in this work.
- Memory rule: teammates see a session's name, folder, repo, branch and status, never its files or conversation (CLI `docs/security.md`). Nothing here widens that.
- No external users yet; the founder, Aden, a Windows WSL computer, a cua container and the cloud member are the network.

Operational
- Build on clean `origin/main` in fresh worktrees, one per task; never on another agent's branch; never `git stash`.
- Engine changes ship as a signed release, then the Hermes pin flips (`#4492`/`#4499` pattern), then `roll_hermes_runtimes`; a deploy alone does not move existing runtimes.
- Cost: no paid API runs are needed for this work.

---

## 4. The design

### 4.1 The engine's session model

One session has one id: `<member12>/<provider>:<conversation>` (unchanged). The engine keeps a row per session it has ever seen on this computer, with these states:

| State | Meaning | Source for Claude |
|---|---|---|
| `busy` | the agent is working, or a tool it started is still running | listing `status: busy`, background `state: working` |
| `waiting` | the agent is waiting for its person: a permission prompt, a question, a sandbox or worker request | listing `status: waiting` with `waiting_for` = Claude's `waitingFor`; background `state: blocked` |
| `idle` | the agent is at its prompt, process alive. A background job whose `state` is `done`, `failed` or `stopped` but whose process Claude still keeps (about an hour, attachable with `claude attach`) is `idle` with `job.state` set | listing `status: idle`; background `state: done | failed | stopped` with a `pid` |
| `ended` | no process holds the conversation; the row keeps `ended_at` and a `reason` | the row left the listing (`reason: process-exited`), moved on under a new id (`moved-on`, from the transcript's `continued-in`), or a background job's process is gone while the listing still shows it finished (`job-done`) |

Own rows (this computer's, never on the wire) also carry what the desktop app's capture needs: `pid`, `birth`, `kind` (`interactive | background`), `entrypoint`, `job` (`{id, state}` for a background job), and the raw `waiting_for`. A parked cloud member is not "offline": its records carry `wakes: true` (from the roster's `meta.wake`) and read "parked, wakes when messaged".

Rules
- **Claude discovery reads `claude agents --json --all`** (the documented surface), on every hook event and at most every 10 s otherwise, with a 2 s floor. The pid-file scan stays only to find the push socket for a listed session and as the whole source when Claude is older than 2.1.145. Rows the listing drops are ended. `sdk-*` entrypoints (`claude -p`, the Agent SDK) are not sessions; daemon spares are not sessions.
- **The engine is the only status writer, and it is instant.** The engine's Claude hooks gain `Notification` and `PermissionRequest` (non-blocking: they record `waiting` with `waiting_for` the moment Claude asks, then let Claude continue), beside today's `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`, `SessionEnd`. The desktop app's own hooks keep one job, relaying a permission decision from the phone or web back to Claude (its `PermissionRequest` handler holds the prompt until a decision arrives); they stop writing status. One writer, no lag.
- **Codex** gains the `PermissionRequest` hook, which sets `waiting` with `waiting_for: permission prompt`, and keeps ended rows from `SessionEnd`. Threads on the shared daemon still share one pid, so a Codex row can end up to 30 minutes late; `who` says nothing more precise than the engine knows. The daemon's thread list and the Codex app are the follow-up (§7).
- **Cursor, OpenCode, Grok**: unchanged discovery; their rows gain `ended` when their end hook or process exit is seen. The Cursor IDE's refused hook caller is a follow-up (§7).
- **Ended rows are kept seven days** (one constant replaces the engine's 1-day `DAY` for ended rows and for an offline member's presence; the sidebar's separate 3-day offline and 7-day ended rules go away in §4.4). An ended row keeps: id, agent, title, folder (basename), repo, branch, `ended_at`, `reason`, and on the owner's computer only, the resume command (`claude --resume <id>`, `claude attach <id>` for a background job, `codex resume <id>`, `cursor-agent --resume <id>`, `opencode -s <id>`, `grok -r <id>`).
- **Resume is the owner's.** The engine never runs a resume; it prints the command (`lightbulb who`) and returns it (`find_sessions`, `directory`) for this computer's own rows only. The wire never carries it. A teammate sees the ended row with no resume. A message to an ended session is queued as today (outbox retention unchanged) and says so in its receipt.
- **Offline** is a property of the computer, not the session. A member's rows keep their last state and gain `online: false` and `seen_at` (unix seconds, last connection close). After seven days offline the presence is dropped.
- **Titles**: Claude's `custom-title` then `ai-title` from the transcript, else Claude's `name`; other harnesses as today. The desktop app stops inventing its own ("New chat"), see §4.4.
- **Hidden folders** (`lightbulb folder <path> off`) hide live and ended rows alike from teammates, as today for live rows.
- **Who you are**: each directory record gains `user_id`, the `sub` of the computer's sign-in proof when it has one (its own from `login.json`, a peer's from the proof in its `hello`). It is a claim, unverified on Macs, and is marked so; the sidebar uses it only to group a network row under a workspace member.

### 4.2 Wire and compatibility

- `Remote` gains optional `waiting_for: Option<String>`; `status` on the wire stays `busy | idle`, with a `waiting` session sent as `busy` plus `waiting_for`. New engines show `waiting`; old engines show `busy`, which is Claude's own fallback word.
- `Presence` gains optional `ended: Vec<Ended>` (`session_id, agent, folder, title?, repo?, branch?, ended_at, reason`), newest first, at most 50. Old engines ignore it. `kept` stays as it is for old peers' mail routing.
- `Hello` is unchanged. No capability bit is needed because nothing new is a frame variant.
- The `directory` op output gains `waiting_for`, `seen_at`, `wakes`, `ended_at`, `reason`, `resume` (own rows only), `user_id`, `folder` (for every row; it is in `Remote` today but not output), and on own rows the local-only fields of §4.1 (`pid`, `birth`, `kind`, `entrypoint`, `job`). It loses `needs_owner_ok` and `can_send` (§4.6). All of this ships in the one Step 1 release so Step 3 needs no second one.
- `find_sessions` (MCP) returns the same; its description says ended rows carry `ended_at` and, for this computer, `resume`. The hosted MCP's vendored tool spec is re-vendored at the engine release.

### 4.3 What each surface shows

| Surface | Source | Shows |
|---|---|---|
| `lightbulb who`, `who --all` | engine directory | live rows with `busy` / `waiting for you: permission prompt` / `idle` (a finished background job: `idle, done, attach: claude attach <id>`); offline computers as `offline, last seen 2 h ago, messages queued`; a parked workspace as `parked, wakes when messaged`; a `Recent` section of ended rows with `ended 3 h ago` and, for own rows, the resume command. The current bug that prints `idle` for every row (`cli.rs:271` compares against `live`) is fixed. |
| `find_sessions`, Friday's network directory tool | engine directory | the same records; agents read `status`, `waiting_for`, `online`, `ended_at`. |
| Desktop sidebar | backend list joined to the engine (§4.4) | the rows it shows today, with state and existence from the engine; plus network-only rows. |
| Web, phone, Friday's `lightbulb_local_sessions`, room context, Activity, notifications | backend list | unchanged shape; for every person who runs the desktop app its rows now mirror the engine (§4.4). |

### 4.4 The desktop app reads the engine (Step 3)

The backend list stays, as the 22 Oct O1 ruling allowed, but it becomes a copy fed by each Mac's engine. Nothing in the app decides whether a session exists or what state it is in.

Discovery and liveness
- The app's Claude discovery (`claude agents --json` polling, `Holders`, `still_registered`, the liveness tick's `runs()` / `held()`, the fork-ends-origin rule) is deleted. In its place the app opens the engine's socket (`~/.lightbulb/connect/sock`, newline JSON), calls `directory`, and `subscribe`s for changes. This computer's Claude rows drive mirror creation, state and end. The app's existing per-session capture (version verification, transcript, generation, push socket, restrictions, uploads) runs for each live row the engine lists, as today.
- A mirror's status is the engine's: `busy → busy`, `waiting → waiting-approval` with `waitingFor`, `idle → idle` (with `done` and `terminal: attach` when `job.state` is finished), `ended → ended` with `endedAt` and `endedReason` from the engine's `reason`. Nothing in the app freezes a status or forces `idle`; the "listing stopped showing it, keep it as idle" rule (`transport.rs:511-527`) goes, and so does every `Transition::Status` the app's hooks wrote (`lifecycle.rs:409-446`, `transport.rs:952-970`). The app's hooks stay installed only to relay permission decisions (§4.1).
- The owner's own sessions that are not shared in Lightbulb registered nothing on the backend until now and were absent from the sidebar. They now appear as network-only rows under "You", dimmed, with one action: Share (the same switch the holder band offers). (Founder ruling 30 Oct: yes, T4.)
- Codex, Cursor and Grok discovery in the app is unchanged in this step (non-goal). The Claude pipeline is the one that produced the 30 Oct mismatch.
- If the engine's socket is unreachable the sidebar footer, where "Mac connected" lives, says "Lightbulb's background service is off" with a Fix button that runs the bundled `setup`; no Claude sessions are listed until it answers. The app already installs and runs the engine at every launch (`engine.rs`), so this is a fault state, not a mode.

The join between the two lists
- The desktop registers each mirror with a new public field `networkSessionId` (`<member12>/<provider>:<conversation>`), taken from the engine's own row. The backend stores it and returns it on `LocalSession`. The conversation id and profile fingerprint stay private (`_public` allowlist unchanged).
- The sidebar (desktop only; the web app loads the same code, so this is gated to Tauri) merges: every backend row, joined by `networkSessionId` to its engine row when one exists; then every engine row with no backend match as a **network-only row**.
- A network-only row shows person, agent, title, folder, state, and "on the network" as its second line when the folder is unknown. It has no Open, Join, Resume or End; it is information, the same as `who --all`. Rows of workspace agents (`Friday`, `Forge`, …) are network-only rows under the workspace's name. Hidden-folder sessions never arrive, so they never show. (Founder ruling 30 Oct: yes to these rows, T3.) Until Step 4 lands they sit in today's layout under its cap and "+N more"; Step 4 replaces that layout.
- Ended rows: the backend row stays the source of Recent / Stopped for shared sessions (resume through the holder, as today). Network-only ended rows show in Recent with "ended" and no action.
- Retention: the backend keeps its 7-day ended rule; the client's 3-day offline rule goes, and offline follows the engine's seven days through `online: false` on the joined row.

What stays on the backend, unchanged
- The whole relay: register, heartbeat and lease, screen and input streaming, commands, approvals, join and pick-up, team control, link sharing, resume, end and remove, chat rooms, notifications and push, Activity, transcripts and search, uploads.
- The list endpoints for web, phone, Friday and room context.
- "Only me" (`shared: false`) is a sharing switch on the backend; hidden folders are a presence switch on the engine. They stay separate: a session can be on the network and not shared in Lightbulb, and the row says so ("on the network").

### 4.5 Joining the workspace's network from the app (Step 2)

The rule: **signed in to a workspace means on its network.** The desktop app and `lightbulb login` both apply it.

Flow (desktop)
1. After Clerk sign-in, once the engine answers on its socket, the app asks the engine `team.get` (it already returns `team_id`) and the backend `GET /api/v1/network/team` for the workspace's `teamId` (stored, no wake; see below). If they match: done, silently. No workspace team id yet means the member has never joined or minted; treat as step 2.
2. If this computer is alone in its own one-computer network: the app calls `POST /api/v1/network/join-line` (Clerk auth) in the background and shows "Connecting your computer to <Workspace>…" in the sidebar footer. The route checks the caller is a member of the active workspace, wakes the cloud member if parked (about 55 s cold start, inside the 90 s ingress limit), asks it over the existing RPC path for a once link, and returns it with `no_store`, never logging it.
3. The app runs the engine's `team.join` with that line and the person's name (first name, then `<Name> (<computer>)` on `name_taken`). The ticket carries the member's doorbell (`wake`), so the joiner rings it and keeps dialing if the member parks again.
4. On success the app shows a notice, once: "Your computer joined <Workspace>'s network. Teammates on it see the names, folders and status of your sessions, never your files or conversations. Hide a folder: `lightbulb folder <path> off`." with a Leave button.
5. If this computer is in a different multi-member network, the app does not leave it. It asks: "Your computer is on <Team>. Join <Workspace>'s network instead?" with Join / Not now. Join runs `leave` then step 3.
6. "Not now", Leave, a `removed.json` left by a teammate's Remove, or a `lightbulb leave` by the person all set a remembered opt-out in the app; the app stops asking until the person signs out and in again or presses Connect in the footer.
7. The app also signs the engine in: it runs `lightbulb login` without a browser, reads the device code, and approves it through `POST /network/device/approve` with the person's Clerk session. Without `login.json` the cloud member refuses this computer's mail to Friday. A proof is renewed by the engine itself.

Flow (CLI): `lightbulb login` does steps 1 to 3 after the proof arrives, prints the notice text, and explains `already_in_team` the same way. `lightbulb invite --agents` keeps working for a workspace whose cloud member is still alone; when the member is already in a network it says "Your workspace's agents are already on a network. Sign in with `lightbulb login` to join it."

Who founds (see §8, T2)
- The workspace's cloud member anchors the workspace's network. For a workspace whose member already joined an admin's network through `invite --agents` (the founder's today), nothing changes: the member mints once links in that network and new members join it.
- For a fresh workspace, the first sign-in makes the member's own network the workspace's network, which is what the 23 Oct design intended.

Engine changes for this
- Allow `team.invite` from an instance caller (today refused, `net_ops.rs:108-111`), forced to a once link, with the instance's doorbell written into the ticket before signing, and the `not_a_person` check skipped for an instance.

Backend and bridge
- Bridge RPCs `network.invite` → engine `team.invite {mode: once, code: false}` (returns the line; logs nothing) and `network.team` → engine `team.get` (returns `team_id` and member count).
- The backend stores the workspace's `teamId` in `<owner>/network/sessions.json` whenever it learns it: after `network.join` is admitted, after every `network.invite`, and once by a backfill that calls `network.team` for each workspace with `joined: true` (the founder's workspace is one). `GET /api/v1/network/team` returns it from the store, so a sign-in costs no wake when the computer is already in.
- Route `POST /api/v1/network/join-line`: Clerk user, active workspace, membership check, `sessions_exist(owner)` else 409 "no workspace agents yet", RPC with the 90 s timeout, 503 on `UNAVAILABLE` with `Retry-After`.
- The stale `joined: true` flag is not used to decide anything; the stored `teamId` and the member's `network.team` are.

Free users (no workspace) never see any of this; they invite each other as today.

### 4.6 Small fixes that belong to "honest"

- Remove the trust leftovers: `needs_owner_ok` and `can_send` from directory and `find_sessions` records; the "held" receipt texts and `HELD_S` in `mail.rs` that can no longer be produced (`State::Held` stays parseable for older peers' acks). Amend the binding architecture doc (`2026-10-22-lightbulb-network-architecture.md` §6, §9, §11) with the 26 Oct ruling.
- `who --all` prints the real state (busy, waiting, idle, offline), not `idle` for every row.
- `setup` and `setup --refresh` never touch launchd unless `HOME` is the account's real home directory (from the passwd entry) and `LIGHTBULB_CONNECT_HOME` is unset; otherwise they print the "would run" line. This closes the hole a test run used to replace the founder's real background service with one pointing at a temp folder (the job's fixed label is what makes the replacement possible). `doctor`'s "Starts at login" line names the fix (`lightbulb setup`).

### 4.7 What Friday sees

Unchanged tools. After Step 3, `lightbulb_local_sessions` (the backend copy) and the network directory agree for every member who runs the desktop app, and the directory alone covers members who only run the CLI. A Friday turn started by a network message sees what it sees today (that question is outside this document).

---

## 5. Order of work

Each step ships on its own and is useful alone. The order limits risk and gets the founder's own confusion fixed first.

1. **Step 1, engine** (CLI repo, one release): §4.1, §4.2, §4.6. Then: tag, Hermes pin flip, `roll_hermes_runtimes`, re-vendor the hosted tool spec. Acceptance A1-A4.
2. **Step 3, desktop and backend** (monorepo): §4.4 in today's layout. Depends on the Step 1 release being installed by the app (bump the bundled engine). Acceptance A5-A7, A10.
3. **Step 2, auto-join** (CLI + monorepo): §4.5. Depends on Step 1's instance invite. Acceptance A8-A9.
4. **Step 4, the sidebar redesign** (monorepo): design research first (prior art for lists of many live agents and sessions, our own sidebar's history and the five-row cap), then a design document of its own through the interface-design skills, then the build. It consumes the rows §4.4 produces and changes nothing about their meaning. The research starts now, in parallel with Step 1.

Step 2 is after Step 3 because it is the only step that touches admission.

---

## 6. Acceptance

- A1. On the founder's Mac, `claude agents --json --all`, `lightbulb who`, and the desktop sidebar's own rows list the same conversations with the same state, including the Claude desktop-app session and a backgrounded one; a `waiting` session reads "waiting for you: permission prompt" in `who`.
- A2. Closing a terminal moves its row to `Recent` within 15 s on this Mac and within a minute on Aden's `who --all`; it stays seven days; `who` prints `claude --resume <id>` for it on this Mac and nothing on Aden's.
- A3. Closing Aden's laptop lid turns every row of that computer to `offline, last seen …` on the founder's `who --all` within the engine's reconnect window; sending to one says "queued".
- A4. The cloud member on the pre-release engine still shows the founder's computer's sessions (no blanked presence) while the founder runs the new engine. Verified with the pinned image before the pin flips.
- A5. No sidebar row is Idle without a live process: for every own row marked Idle, `claude agents --json` has that `sessionId` with `status: idle`. The 30 Oct case (four Idle vs two live) cannot recur, because the app no longer has a liveness rule.
- A6. Shared sessions unchanged: watch, ask to join, pick up, type with the owner's OK, resume, end, rename, and the PR badge all work on a row whose state came from the engine; the web app and phone list the same rows.
- A7. The sidebar shows the Windows WSL computer's OpenCode session and Friday as network-only rows, dimmed, with no Open.
- A8. Leonardo signs in to the desktop app and, with no invite from anyone, appears in the founder's `who --all` and `lightbulb team` as `Leonardo · <computer>`; his sessions appear on the network; a message from his Claude to Friday is answered (the engine is signed in).
- A9. A Mac already in another multi-member network is asked, not moved; "Not now" is remembered across launches.
- A10. With "Share every new session" off, a terminal the person starts appears in their own sidebar dimmed under "You" with Share as its only action, and in no teammate's sidebar until shared.

---

## 7. Left to the builder, and open verifications

Builder's calls
- The engine's internal row and store shape for ended rows (a column set on the existing sessions table or a new table), the poll scheduling, and how `subscribe` pushes directory changes.
- The Tauri command and event names for the engine socket; whether polling or a subscription feeds the sidebar.
- Exact `endedReason` words the backend accepts for the engine's `reason` values.
- Name suffix rule on `name_taken`.

Verify before relying on it (each becomes a plan task with a recorded result)
- V1. `claude --resume <id>` of an ended Claude desktop-app session from a terminal works. Test on an ended session only.
- V2. Codex hooks fire for threads inside the Codex app in ChatGPT (`SessionStart` row appears in the engine store). If not, Codex app threads are listed by the app's existing lsof method only, and the engine says so in `docs/usage.md`.
- V3. Aden's two extra Idle rows: capture `claude agents --json`, the pid files and `daemon/roster.json` on Aden's Mac before Step 3 ships and record which app rule produced them (desktop pipeline §5 lists the candidates).
- V4. **Confirmed 30 Oct (docs).** `claude agents --json --all` lists finished background jobs until the person deletes them in the agent view or transcript cleanup (`cleanupPeriodDays`) removes the saved conversation; Claude never prunes them on its own and the row carries no end time. So the engine stamps `ended_at` when it first sees `done | failed | stopped`, and after the seven days it keeps only the id as a tombstone while the listing still shows the job, so a job is never re-born as "ended just now".
- V5. **Confirmed 30 Oct (code).** `lightbulb login` prints "Type this code there: <code>" and polls; with `LIGHTBULB_CONNECT_NO_BROWSER` set it opens nothing (`login.rs:267-327`). `POST /network/device/approve` takes `{userCode}` under the normal Clerk user auth (`routers/network.py`), which the hosted web code in the Tauri window already has.

Follow-ups (not in this document)
- Codex parity: the shared daemon's `thread/loaded/list` and status events; the Codex app if V2 fails.
- Cursor IDE hook caller (`Cursor.app`), OpenCode waiting state, Grok `active_sessions.json`.
- Network workers: teammate resume and start, approvals.
- Verified names between Macs (the issuer key ceremony).

---

## 8. Decisions for the founder

Collected after the builder check (§9). Most important first. **Ruled 30 Oct 2026:** T1 join then notify; T2 the cloud member anchors the workspace's network; T3 and T4 shared plus network rows, and the sidebar gets a redesign with design research first (Step 4), because a five-row cap is not good enough.

- T1. **Auto-join silently with a notice, or offer a Join button first?** This document says: join silently when the computer is alone in its own network (a notice after), ask only when it is already on another network. The founder's words were "automatically" and "the option to connect automatically". The alternative, always ask, adds one click for every member and avoids any surprise.
- T2. **The workspace's cloud member anchors the network.** For a fresh workspace the first sign-in makes the member's own network the workspace's; `invite --agents` is then a legacy path. For the founder's workspace nothing changes. The alternative is to keep "an admin's Mac founds, the workspace joins it", which cannot support auto-join without an admin online.
- T3. **Network-only rows in the sidebar** (Friday, the Windows computer, CLI-only teammates), dimmed and informational. The 22 Oct design said yes. The alternative is to show only shared sessions and leave the rest to `who --all`.
- T4. **Your own unshared sessions in your sidebar.** Today they are not there at all. This document shows them dimmed under "You" with a Share action, which is what makes the sidebar equal to `claude agents --json` (A1) and makes sharing discoverable. With "Share every new session" off that is most of a person's terminals, so the 5-row cap and "+N more" fill up with them. The alternative keeps them out and the sidebar stays a list of shared sessions only.

Calls made in this document, for the founder to check while reading: seven days for both ended rows and offline presence; resume is the owner's only and never on the wire; `waiting` travels as `busy` plus `waiting_for` for old peers; the backend list stays as a copy rather than being deleted; the app requires the engine (no fallback discovery); Codex, Cursor, Grok parity deferred; `networkSessionId` is public, the conversation id stays private; hidden folders hide ended rows too; the engine is the only status writer and gains `Notification` and `PermissionRequest` hooks; a finished background job with a live process is idle, not ended; a parked workspace reads "parked", not "offline"; the workspace's team id is stored on the backend so a sign-in needs no wake.

---

## 9. Builder check

Run 30 Oct 2026 on the first draft. Six questions.
- Answered in the document: (1) who writes status after Step 3 (§4.1 "only status writer", §4.4); (3) a finished background job with a live process (§4.1 table); (4) where the workspace's team id comes from (§4.5 backend); (5) the local-only fields own rows carry so Step 3 needs no second engine release (§4.1, §4.2); (6) the parked cloud member (§4.1, §4.3).
- Theirs: (2) the owner's own unshared sessions in the sidebar, now T4.

---

## 10. Build contract for Steps 2, 3 and 4 (30 Oct 2026)

Founder ruling, 30 Oct: finish the remaining steps with subagent-driven development, in separate worktrees, against one agreed architecture, consolidating at the end. This section is that agreement. A step changes it only by a ruling recorded in its ledger and repeated in its PR.

### 10.1 Who owns what

| Piece | Owner | Others |
|---|---|---|
| Foundation PR (below) | merged first, to main | every step branches from main after it |
| `apps/desktop/src-tauri/src/local_sessions/*` discovery and status | Step 3 | Step 2 does not touch them |
| Backend `networkSessionId` and the `LocalSession` model | Step 3 | |
| `useSessionRows`'s body and `sidebarSessionList`'s data | Step 3 | Step 4 reads the hook only |
| `RoomDirectory`'s Sessions section, `SessionListRow`, `AllSessionsView` layout | Step 4 | Step 3 keeps them compiling and changes only their data source |
| `network.invite`, `network.team`, `/network/join-line`, `/network/team`, sign-in and join in the app, the sidebar footer's connect state and notice | Step 2 | |
| Engine change for instance invites | Step 2, on a fresh CLI branch from main after 0.8.0 merges | |
| Hermes engine pin, plugin default filter, `skills-src/lightbulb/references/surfaces.md`, re-vendored `network_tools.json` | Engine release step, before Step 3 | |

### 10.2 The foundation PR

Small, merged before the three steps start. Nothing user-visible changes.
- `apps/desktop/src-tauri/src/engine_client.rs`: connects to `~/.lightbulb/connect/sock`, one newline-JSON request per call with a timeout, and an optional subscription. One Tauri command, `engine_ask(op, body)`, read-only from the page: `directory`, `sessions`, `team.get`, with `pid`, `birth_micros`, `entrypoint`, `kind` and `cwd` stripped. Native Rust callers use `engine_client::ask` unfiltered. **Security ruling, 30 Oct:** the window loads a hosted page, so script on it must never be able to join or leave a network. Step 2 joins in native code: Rust fetches the join line itself with the person's session, never a ticket handed over by the page. Registered in `lib.rs`.
- `packages/shared/src/messaging/sessionRow.ts`: the `SessionRow` type below and a pure `mergeSessionRows(backendRows, engineRows, viewer)` with unit tests.
- `apps/frontend/src/components/messaging/useSessionRows.ts`: returns `SessionRow[]`. In the foundation it maps the backend list only, so behaviour is unchanged. `RoomDirectory` and `AllSessionsView` read it.

### 10.3 `SessionRow`

```ts
type SessionState = 'busy' | 'waiting' | 'idle' | 'done' | 'offline' | 'parked' | 'ended';
interface SessionRow {
  id: string;                  // networkSessionId when known, else the backend sessionId
  backend?: LocalSession;      // the joined backend row; every action and detail reads it
  engine?: EngineRecord;       // the joined directory record
  source: 'shared' | 'network';// 'network' = no backend row: info only, plus Share on own rows
  person: { name: string; userId?: string; isViewer: boolean };
  computer?: string;
  agent: string;
  title: string;
  folder?: string;
  repo?: string;
  branch?: string;
  state: SessionState;
  waitingFor?: string;
  lastSeenAt?: number;         // offline
  endedAt?: number;
  endedReason?: string;
  resume?: string;             // own ended rows only
}
```

State comes from `engine` when joined, else from `backend`. Actions come only from `backend`. A row with no `backend` has no Open, Join, Resume or End.

### 10.4 Compatibility rules

- **The window loads the hosted web app, and desktop builds lag it.** The frontend calls `engine_ask` only when the command exists. Without it (an older app, web, phone), `useSessionRows` returns the backend list exactly as today. Web and phone never read an engine.
- **The backend list stays,** written by the Mac from the engine's state after Step 3. Older Macs keep writing it their own way. `networkSessionId` is optional on the model.
- **Engine floor:** the app bundles engine ≥ 0.8.0 from Step 3 on, and `engine_ask` failing is shown in the footer (§4.4), never as an empty list.
- **Rollout order:** engine 0.8.0, then Hermes pin and roll, then foundation, then Steps 3 and 2 (either order, separate PRs), then Step 4, then a desktop release carrying all of it.

### 10.5 Step 4's design decisions (controller's defaults; founder reviews screenshots before merge)

The research is `docs/research/2026-10-30-sessions-sidebar-design-research.md`. Its six questions are answered so the work can proceed; each is a two-way door.
1. **Grouping:** a "Needs you" group on top (only `waiting` rows and failed ended rows of the viewer's own), then per person, viewer first. This honours the 2026-10-13 ruling against ranking by status, since only `waiting` moves. A persisted switch offers person, state, and repo.
2. **Counts:** the Dock badge counts the viewer's own `waiting` rows only, as today.
3. **Network-only rows and workspace agents:** their own collapsible group, "On the network", below people. A workspace member's network-only rows sit under that person instead (founder, 31 Oct). The viewer's own unshared rows stay under the viewer, dimmed, with Share.
4. **Retention:** seven days everywhere, from the engine. Pins and archives stay per browser for now.
5. **Row:** one line: avatar, harness mark, title, state. Second line only when it carries news: waiting reason, or folder and PR. Computer and repo in the hover and in All sessions.
6. **Overflow:** no global cap. Each group folds with "… N more" and a count in its header, and keeps waiting and pinned rows visible. Search and filters in the sidebar. All sessions stays the full table.

### 10.6 Consolidation

Each step opens its own PR from its own worktree, off main. Merge order per §10.4. After the last merge, one consolidation pass on main: run the three steps' acceptance rows (A5-A10) together on a local desktop build, the full suites in CI, and record the results on the final PR.
