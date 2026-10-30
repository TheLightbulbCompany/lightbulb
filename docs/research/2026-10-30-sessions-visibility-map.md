# Session visibility and reach map

Read-only, 30 Oct 2026. Everything below is read from origin/main:
- CLI = TheLightbulbCompany/cli @ `3215af2`, cited as `cli/src/...`.
- Monorepo = TheLightbulbCompany/lightbulb @ `f47cc26c8`, cited from the repo root (`apps/...`, `packages/...`).

Docs are quoted only where they disagree with the code.

## The core finding

Lightbulb has **three separate session systems**. They share no store and no id, and none of them reads another:

| # | System | Id shape | Producer | Store / transport |
|---|---|---|---|---|
| 1 | **Network presence** (CLI engine, P2P) | `<member12>/claude:<conv>`, `<member12>/friday` | the CLI daemon on each computer (hooks + scanning `~/.claude/sessions/*.json`) | iroh P2P `Presence` frames, held in memory and in the local sqlite `peers_presence` table |
| 2 | **Shared live sessions** (desktop "session sharing", relay) | UUID | the Mac desktop app's relay heartbeats (`apps/desktop/src-tauri/src/local_sessions/*`) | backend DynamoDB local-session rows, served by `/api/v1/messaging/sessions` |
| 3 | **Uploaded sessions** (capture upload) | `<harness>:<id>` | the CLI daemon's `upload.rs` loop (opt-in) | `SESSIONS#<user>` / `WSESSIONS#<ws>` rows (+ S3 for `full`), `/api/v1/sessions` |

The desktop app bundles the CLI engine (`apps/desktop/src-tauri/src/engine.rs:1-5`). Its sidebar still never asks the engine's `directory`. The CLI never posts to `/messaging/sessions`. So whether a session appears in "the app" or in `find_sessions` depends on which system saw it.

---

## A. Surfaces: what each one shows

| Surface | What it lists | Source and transport | Freshness / retention | Who can see it | Never shows | file:line |
|---|---|---|---|---|---|---|
| **`lightbulb who`** | Only this computer's live sessions (Claude, Codex, Cursor, Grok, OpenCode), by handle. Includes ones hidden by `folder off`, marked "hidden from teammates". | Daemon `sessions` op, built from `live_sessions` (registered rows + Claude process scan). | Live; `Gone` is dropped. | The local user. | Teammates' sessions; ended sessions. | `cli/src/cli.rs:214-219`, `cli/src/daemon/ipc.rs:289-290`, `cli/src/sessions.rs:174-237` |
| **`lightbulb who --all`** | Local sessions, then every roster member's last presence: Person, Computer, Agent, Folder (a teammate's shows its repo), Status, Doing. | Daemon `directory` op (same data as `find_sessions`). | Same as `find_sessions`. **The Status column always reads "idle"**: it tests `status == "live"`, but directory rows carry `busy`/`idle`. No online column. | Any member of this computer's network roster. | `online` and `needs_owner_ok`; ended sessions; desktop-relay or uploaded sessions. | `cli/src/cli.rs:229-296` (bug at `:271`), `cli/src/presence.rs:102` |
| **MCP `find_sessions`** (local `lightbulb` server) | `{session_id, person, person_id, person_verified, computer, agent, cwd (local only), repo, branch, status busy/idle, doing, title, tier push/next_step/pull, online, can_send:true, needs_owner_ok, is_this_session, name}`. Default limit 20; `total` is also returned. | Daemon `directory`. Local rows come from `live_sessions` + hosted apps (`link::apps`). Remote rows are each member's latest `Presence` frame, received P2P after `hello`, re-sent on change, every 5 s check and every 60 s regardless. | A teammate's presence lists the sessions that were **live when that computer last sent**. An offline member's list stays for **one day** after its last connection (`seen_at + DAY`), then is dropped. Ended sessions are never listed (`kept`/`known` addresses are carried for routing only). | Every computer in the same P2P network roster. No workspace sign-in is needed to see. | Sessions under a `folder off` path; ended sessions; anything from systems 2 or 3; the full cwd of a remote session (folder name only unless `share_paths`). | `cli/src/mcp.rs:96-106,696-721`; `cli/src/presence.rs:18-31,143-211,385-420,425-525`; `cli/src/mcp/tools.json:2-13` |
| **Hosted MCP `find_sessions`** (`api.thelightbulb.company/mcp`, OAuth) | The same `directory` rows, reshaped. `is_this_session` is the app's own address. | Backend → the person's pinned computer's engine (`_through_member`, "directory"). | As above. | The signed-in person; works without a workspace. | Same as above. | `apps/backend/core/services/hosted_mcp.py:179-182,213,370-394` |
| **Desktop app sidebar "Sessions"** (the desktop window is the hosted web app) | Workspace sessions. Pinned first, then live (idle/busy/waiting-approval/unknown), then offline and ended. 5 rows before "+N more". | `GET /api/v1/messaging/sessions?includeEnded=true` → `list_workspace_sessions`, fed by the **desktop relay heartbeats** (system 2). | Server: live and offline rows with **no age bound**, plus ended rows from the last **7 days** (caps: 100 live, 20 ended). Client: drops offline rows last heard ≥ **3 days** ago; ended rows stay until the viewer archives them. Refreshed by live events plus a 30 s poll. | Any workspace member. Teammates' sessions appear only while shared; your own always appear. "Membership is not consulted" (no room check). | CLI-network-only sessions (Cursor/Grok/OpenCode/Codex not mirrored; any session on a Mac without desktop sharing); uploaded sessions. | `apps/desktop/src-tauri/tauri.conf.json:9-10,24`; `apps/backend/core/services/local_sessions.py:1299-1378`; `packages/shared/src/messaging/localSessionLabels.ts:167-216`; `RoomDirectory.tsx:392-438` |
| **"All sessions" view** | The same list, grouped today / yesterday / week / earlier. | Same as the sidebar. | Same as the sidebar. | Same as the sidebar. | Same as the sidebar. | `localSessionLabels.ts:218-221`, `AllSessionsView.tsx:31-33` |
| **Settings › "Session sharing" card** (`LocalSessions.tsx`) | Not a list: on/off for this Mac's sharing. | Tauri invoke → `enrollment.json`. | n/a | The local user. | Sessions. | `apps/frontend/src/components/desktop/LocalSessions.tsx:249-305`; `apps/desktop/src-tauri/src/local_sessions/enrollment.rs:585-588` |
| **Agent inspector › "Sessions" tab** (`SessionsTimeline`) | Uploaded sessions grouped by `computer`: harness, repo, branch, times, tokens. A Share button on every row. | `GET /api/v1/sessions` (system 3). | Only sessions that **ended** after uploads were turned on. The upload loop runs every 300 s. Rows expire after 30 days (no workspace) or 365 days (in one). | Whole workspace (`WSESSIONS#`), with no owner name per row. | Live sessions; prompts and turns (never uploaded); anything from systems 1 or 2. | `cli/src/upload.rs:15-48,75-123`; `apps/backend/core/services/session_store.py:29,104-112`; `SessionsTimeline.tsx:29-83` |
| **Memory tab** (`AgentMemoryTab`) | Memory facts and drafts. It does **not** list sessions; it reads `/sessions` only to link evidence ids. | `GET /memory`, `/memory/drafts`, `/sessions` | n/a | Person auth. | Sessions. | `AgentMemoryTab.tsx:59-76,174-178` |
| **Share by link** `/shared/<token>` | One uploaded session's metadata, plus tool calls if it was a `full` upload. | `POST /sessions/{id}/share` (owner only) → HMAC token. | Valid 7 days. | Anyone with the link, no auth. | User and workspace ids. | `apps/backend/routers/sessions.py:27,122-135`; `session_store.py:153-184` |
| **Session-sharing live view** (watch, type, join, pick up) | One shared session's terminal or conversation, viewers, join requests. | Backend relay ↔ the Mac (`transport.rs`, `remote_input.rs`, `presence.rs`). | Live. | Any workspace member can READ a shared session. Commanding it is owner-only, unless "Let teammates control" is on or the person was allowed in after asking to join. | Unshared sessions ("reads as gone"). | `local_sessions.py:1136-1180,988-1001`; `apps/desktop/src-tauri/src/local_sessions/remote_input.rs:1-12` |
| **Cloud agents (Friday etc.): `lightbulb_network directory`** | The same P2P directory as `find_sessions`, from the workspace's embedded CLI member. | Local sidecar socket → CLI `directory`. | As `find_sessions`. | The agent. | Systems 2 and 3. | `apps/infra/hermes/plugins/lightbulb/__init__.py:370-450`; `cli/src/presence.rs:451-457` |
| **Cloud agents: `lightbulb_local_sessions resources/read`** | Every **shared** relay session in the workspace, with its events. | `/api/v1/messaging/local-session-agent/*` → `_agent_sessions`. | Live and offline, plus ended in the last 7 days. | The agent, under a room grant. A roomless (Paperclip) run sees only people who allowed background work. **Not available in a turn started by network mail** (no grant). | Unshared sessions; uploaded sessions; P2P sessions. | `__init__.py:318-366`; `apps/backend/routers/messaging.py:324-342`; `local_sessions.py:1282-1296`; `apps/infra/hermes/bridge/network.py:436` |
| **Cloud agents: room ingress summary** | Up to 24 live shared sessions of the room's humans, attached to an agent's room turn. | `live_sessions_for_room` | Live only. | Agents in that room. Blanked if any room member is from another org. | Offline and ended sessions. | `local_sessions.py:1417-1443` |
| **Cloud agents: `lightbulb_workspace search_sessions`** | Text search over the requester's own sessions plus everyone's shared relay sessions. | `searchable_sessions` | No 7-day window. | Run as the requesting person; refused without one. | Uploaded sessions. | `apps/backend/core/services/message_search.py:227-290`; `local_sessions.py:1381-1396`; `messaging.py:250-285` |
| **Cloud agents: memory** | Team scope, plus the speaker's private scope in a 1:1 DM only. | `agentcore` → `/api/v1/internal/memory/recall`. | n/a | The agent. | Upload text (only approved drafts become memory). | `apps/infra/hermes/plugins/memory/agentcore/__init__.py:244,269-289`; `apps/backend/routers/memory.py:150-200` |

---

## B. Reach: who can send what to whom, and the gate each passes

| Path | Transport | Gates (in order) | Is `needs_owner_ok` enforced? | Holds? | file:line |
|---|---|---|---|---|---|
| **Session → session, same computer** (`send_message session_id`, `lightbulb send`) | Local daemon mailbox. Pushed when the tier is `push` (Claude messaging socket / Codex app-server), otherwise delivered at the next hook. | Address grammar; group `*` refused for agents; loop limits (12 per 10 min, 20 hops). | Not computed (same person). | No. | `cli/src/mcp.rs:725-760`; `cli/src/daemon/ipc.rs:1476-1560`; `cli/src/cli.rs:332-336` |
| **Session → teammate's session** (P2P) | Sender outbox → envelope → receiver `mail::receive`. Retried with backoff for 1 day (`TTL_S`), then `expired`. Offline: `queued`. | Sender: the target prefix must be a roster member (`not_member`). Receiver: `from` within the peer's prefix; `to` on this computer; resend/id-reuse checks; hops/rate; this computer still in its own roster. **No sign-in proof is required between two laptops.** | **No. Advisory only.** `needs_owner_ok = person_id != me` is only a field in the directory row; `receive` stores straight to the mailbox: "No one holds a member's message: membership is the gate." | **No message hold is ever created.** `holds.rs` only files permission holds (`file_permission`). The receipt text "held: waiting for X to approve" / "has to allow it first" is unreachable for messages. | `cli/src/presence.rs:442`; `cli/src/mail.rs:15-21,196,222,729-866`; `cli/src/holds.rs:1-4,94`; `cli/src/outbox.rs:1-17` |
| **Session → workspace agent** (e.g. Friday) | P2P to the workspace's embedded CLI member (Hermes microVM). If it is parked, the sender's daemon rings the doorbell (`waking`). | Everything above, plus: `to` must be one of the declared agents (`policy`), plus **`members` gate**: the sender computer's latest `hello` must carry an EdDSA sign-in proof for that computer, in force, with `sub` in the workspace's member list and the key not revoked. Otherwise `not_a_member`: refused, not held. | No (same advisory field). | No. | `cli/src/net/mod.rs:1250-1290,925-955`; `cli/src/mail.rs:812-817`; `cli/src/proof.rs:69-126`; `apps/backend/core/services/network_member.py:80-118` |
| **Workspace agent → session** | The bridge sends as its own declared session only (`not_declared` otherwise). | Same receiver checks. **Laptops have no members gate**, so any roster member, cloud included, can deliver. | No. | No. | `cli/src/daemon/ipc.rs:1516-1519`; `__init__.py:427` |
| **Session → room / DM / person** (`send_message room_id|person_id`) | Backend `/messaging/rooms...`, signed with the computer's sign-in proof. | The workspace tools exist only while signed in (`signed_in()`); then backend room membership. | n/a | No. | `cli/src/mcp.rs:90-94,762-800` |
| **Person in app → own shared session** ("Sent from Lightbulb by you") | Backend command → relay → Mac `remote_input` / inbox. | `authorize_session(owner_only)`, `_require_control` (live, shared, capabilities, generation, turn). The header is `[Sent from Lightbulb by you]` plus the verified-sender line. | n/a | No. | `local_sessions.py:1136-1180,1611-1652,1669+`; `apps/desktop/src-tauri/src/local_sessions.rs:100-107` |
| **Teammate (person) → someone's shared session** (session-sharing injection) | Same as above. | Read: any workspace member while shared. Send/approve: owner, or anyone while `teamControl` is on, or an `allowedSenders` person (join request → owner decides). Interrupt: owner or team control. End: owner only. Remote keystrokes from teammates lose ctrl+x and mouse input. The header is `[Lightbulb teammate contribution from @aden (Aden Kim) {...}]`. | n/a (a real gate: join request + owner decision) | Join and pick-up requests wait for the owner. | `local_sessions.py:988-1001,1600-1609,2285-2390`; `remote_input.rs:1-12` |
| **Agent → shared session via the backend** | — | `submit_command` refuses agents ("Only a person can command a session"). Agents are told to use `lightbulb_network`, i.e. the P2P path above, which needs the session's **network** address. No mapping from the UUID exists. | — | — | `local_sessions.py:1680-1681`; `__init__.py:321-322` |

---

## C. The observed facts, explained from code

**1. The founder's Mac lists Aden's two Claude sessions with online=false, idle, next_step/push, needs_owner_ok=true.**
- When Aden's computer disconnects, `presence::seen` stamps `seen_at` (`cli/src/presence.rs:386-392`).
- `stored()` keeps a member's last presence while `seen_at + DAY >= now` (`:394-407`).
- `directory` emits each of those sessions with `online = d.peers.is_online(key)`, which is false (`:519-521`; `cli/src/net/mod.rs:430-434`).
- `status` is whatever Aden's Mac last published: Claude's own `status: busy` → `Live` → wire `busy`, anything else → `idle` (`cli/src/sessions.rs:225-229`, `presence.rs:102`, `net/frames.rs:44`). So "idle" means "Claude was waiting for input when the laptop last spoke", not "offline".
- `tier` is likewise frozen from Aden's Mac. `push` means Claude's messaging socket existed there (`sessions.rs:219-226`), `next_step` means it did not. Neither says anything about delivery while offline: a send is `queued` in the founder's outbox for up to 1 day (`cli/src/outbox.rs:14`).
- The branch comes from Aden's hook. The title "CLI e2e tests from Prasiddha" is Claude's session `name` (`sessions.rs:212`).
- `needs_owner_ok=true` because Aden is a different person (`presence.rs:442`). Nothing enforces it.
- These rows vanish 24 h after Aden's last connection, even if the sessions are still open on the closed laptop.

**2. The app shows a session of Aden's "from today" that the CLI list does not.**
The sidebar is system 2 (desktop relay → `/messaging/sessions`). `find_sessions` is system 1 (P2P presence). Any of these makes a session appear in one and not the other:
- **It ended.** The sidebar keeps ended sessions 7 days server-side and until archived client-side (`local_sessions.py:1347,1369-1370`; `localSessionLabels.ts:200-214`). The CLI never lists an ended remote session: only live ones are in `sessions`; ended ones go to `kept`, which `directory` ignores (`presence.rs:197-209,520`).
- **It was not live when Aden's Mac sent its last presence**, e.g. it started and stopped between presences, or Aden's network daemon was off or on another network while the desktop relay was on. The two are independent connections.
- **It runs inside the Claude/Codex app, or is a held session started from the app.** The desktop mirrors those (the skill says "One that runs inside the Claude or Codex app has no terminal, so members read its conversation"). The CLI only knows Claude CLI processes with `~/.claude/sessions/<pid>.json`, plus sessions whose hooks registered (`sessions.rs:174-237`).
- **Aden ran `lightbulb folder <path> off`.** That hides it from P2P presence only (`presence.rs:156,174,183`); the monorepo never reads `folders_off`.

The converse also happens. The two sessions in fact 1 appear in the sidebar only if Aden's desktop sharing mirrored them (shared, and enrolled on that Mac).

**3. A teammate computer in the founder's network, not signed in to the workspace, was refused by Friday with `not_a_member`; the founder's signed-in Mac was not.**
- Friday lives on the workspace's embedded CLI member, so `d.cloud` is `Some`.
- Its `members` list is pushed by the backend from `_humans_to_ask(owner, None)` (`network_member.py:80-118`, `net_ops.rs:739-784`).
- On each envelope, `net::receive` computes `proof::admitted`: the online peers whose last `hello` carried a proof verifying for that key, with `sub` in `members`, not revoked (`cli/src/net/mod.rs:1275-1285`, `cli/src/proof.rs:113-126`).
- A computer that never ran `lightbulb login` sends no proof (`net/mod.rs:1100-1106`), so `mail::receive` refuses it with `not_a_member` (`cli/src/mail.rs:815-817`, text at `:15-17`).
- Network-roster membership alone is enough between laptops, but not for a workspace member. That is the only proof gate on the P2P path.
- Doc disagreement: `agent-network-security.md:51` says "only from a computer that is signed in to that workspace". The proof carries only `{sub, computer}` with no workspace claim (`proof.rs:56-65`): any signed-in computer of a listed person passes.

**4. Workspace agents appear as person "The Lightbulb Company", online=false, needs_owner_ok=true, tier pull.**
- **Person.** The bridge starts `lightbulb member --name <sessions.workspace>`, the Clerk org name (`apps/infra/hermes/bridge/network.py:186-191`; `network_member.py:109`). The embedded member's solo genesis uses that name as its person name (`cli/src/daemon/net_ops.rs:302-310,366-369`). The row prints `person: m.name` (`presence.rs:428`).
- **Status and tier.** Every declared cloud session is published with hardcoded `status: "idle"`, `tier: pull`, no repo or branch (`net_ops.rs:718-728`).
- **online=false.** The microVM is parked; a send rings the doorbell and the receipt says `waking` (`net/mod.rs:925-955`, `ipc.rs:1627`).
- **needs_owner_ok=true.** The member's `person_id` is its own key, so it is never "me" (`presence.rs:442`).
- **Sender label.** Messages from Friday are labelled "The Lightbulb Company's Friday (not verified)" (`cli/src/label.rs:27-50,77-79`).

---

## D. Inconsistencies and confusing seams

Each item has file:line, the user-visible confusion, and a one-line possible direction.

### Two lists of "my team's sessions"
1. **Two (really three) session lists with no shared id.**
   - Where: `cli/src/presence.rs:449` vs `local_sessions.py:1351` vs `session_store.py:104`.
   - Confusion: the app sidebar, `find_sessions`, and the inspector "Sessions" tab disagree about which of Aden's sessions exist.
   - Direction: pick one source of truth for "sessions on my team".
2. **Different retention per surface.**
   - Where: CLI presence 1 day (`presence.rs:22`); sidebar offline 3 days client-side but unbounded on the server (`localSessionLabels.ts:168`, `local_sessions.py:1339`); ended 7 days (`:1347`); uploads 30/365 days (`session_store.py:29`); share links 7 days (`sessions.py:27`).
   - Confusion: the same session disappears from each place at a different time.
   - Direction: one retention rule.
3. **Offline vocabulary differs.**
   - Where: CLI `status: busy|idle` + `online: bool` (`frames.rs:44`, `presence.rs:436-440`); backend `idle|busy|waiting-approval|unknown|offline|ended` (`local_sessions.py:28`); local `who` says `live` (`sessions.rs:59-64`).
   - Confusion: "idle" in the CLI can mean a closed laptop; in the app the same thing reads "offline".
   - Direction: fold `online` into status.
4. **`who --all` Status is always "idle".**
   - Where: `cli/src/cli.rs:271` tests `== "live"`, but directory rows say `busy` (`presence.rs:102`).
   - Confusion: busy teammates and busy local sessions show as idle.
   - Direction: compare against `busy`.
5. **`who --all` hides `online`.**
   - Where: `cli/src/cli.rs:229-296`.
   - Confusion: an offline teammate's session reads "idle, gets messages instantly".
   - Direction: show offline.
6. **The tier is a stale promise.**
   - Where: `presence.rs:104`, `cli.rs:276-280`, `mail.rs` receipts.
   - Confusion: `push` / "instantly" is shown for a session on a closed laptop; the message actually waits up to 24 h and then expires.
   - Direction: show "offline: queued until …".
7. **`folder off` hides only from the network.**
   - Where: `presence.rs:156,183`; no `folders_off` in the monorepo.
   - Confusion: a person who hid a folder still has its session in teammates' app sidebar if desktop sharing is on.
   - Direction: one privacy switch.
8. **`can_send: true` is a constant.**
   - Where: `presence.rs:441`.
   - Confusion: it reads like a capability check but is always true, even for offline, Friday-gated or ended targets.
   - Direction: drop it or compute it.

### Gates that look like gates but aren't
9. **`needs_owner_ok` reads like an enforced approval but is advisory.**
   - Where: `presence.rs:442` vs `mail.rs:739,834`.
   - Confusion: agents and people expect the owner to approve; the message is stored immediately.
   - Direction: rename it, or enforce it.
10. **Unreachable "held" receipt texts.**
    - Where: `mail.rs:196,222` ("has to allow it first (they see a prompt)"); `holds.rs:94` only files permission holds; `mail.rs:20` says "nothing holds in Plan A".
    - Confusion: copy promises an approval prompt that never appears.
    - Direction: remove it until holds exist.
11. **Network gate asymmetry.**
    - Where: laptops accept from any roster member (`mail.rs:833-836`); workspace agents demand a sign-in proof (`mail.rs:815-817`).
    - Confusion: the same teammate can message the founder's sessions but not Friday, and the error says "Ask an admin to add you" even when they are already a workspace member who just has not run `lightbulb login` on that computer (`mail.rs:15-17`).
    - Direction: say "sign in on this computer".
12. **Docs over-claim the gate.**
    - Where: `agent-network-security.md:51` says "signed in to that workspace"; the proof has no workspace claim (`proof.rs:56-65`).
    - Confusion: admins think the gate is per-workspace.
    - Direction: fix the doc or add the claim.
13. **The gate list's source.**
    - Where: `network_member.py:85` builds `members` from `website_logins._humans_to_ask`. A parked VM keeps the old list until restart (`network_member.py:135-142`; "absent keeps" at `net_ops.rs:769-783`).
    - Confusion: a just-added or just-removed person may be gated wrongly until the VM wakes.

### Identity and labels
14. **"Person: The Lightbulb Company" for agents.**
    - Where: `network_member.py:109` → `bridge/network.py:186-191` → `presence.rs:428`.
    - Confusion: agents look like a human teammate named after the company; `needs_owner_ok=true` implies an owner who doesn't exist.
    - Direction: a distinct `kind: agent`.
15. **Agents are always "idle / pull / offline".**
    - Where: `net_ops.rs:725-727`.
    - Confusion: Friday runs a turn the moment mail arrives (`bridge/network.py:378-379`), and is reachable via the doorbell, but is listed as if it would only read mail when it checks.
16. **"(not verified)" on every teammate.**
    - Where: `label.rs:77-79` (`verified` = own key only); `person_verified:false` for all remote rows (`presence.rs:519`).
    - Confusion: even proof-checked, signed-in teammates read as unverified, while the same proof is trusted for `me` memory sync (`net/memsync.rs`) and the Friday gate.
17. **The same header prefix with two meanings.**
    - Where: CLI P2P mail is framed `[Lightbulb teammate contribution from Your Codex {requestId}]` even for your own sessions on your own computer (`cli/src/frame.rs:6,69-77`, `label.rs:33-46`). Desktop sharing uses `[Lightbulb teammate contribution from @aden (Aden Kim) {roomId,…}]` (`local_sessions.rs:100,2570`).
    - Confusion: the session-sharing skill's authority rules (`packages/claude-plugin/lightbulb/skills/lightbulb-session-sharing/SKILL.md` "What a teammate message looks like") describe only the `@handle` form, so an agent gets "teammate" rules for its own owner's other session.
    - Direction: separate prefixes.
18. **The skill vs the hosted MCP.**
    - Where: SKILL.md says the hosted `find_sessions` "marks no session isThisSession"; the code marks the app's own address (`hosted_mcp.py:392`).
    - Confusion: minor, but agents are told the wrong thing.

### Agents and the two session universes
19. **No bridge from a UUID to a network address.**
    - Where: `lightbulb_local_sessions` returns relay UUIDs and says "To message a session, use lightbulb_network" (`__init__.py:321-322`), but the network takes `<member>/claude:<conv>` and nothing maps one to the other. `submit_command` refuses agents (`local_sessions.py:1680-1681`).
    - Confusion: Friday can see a shared session it cannot address.
20. **Network-mail turns are blind.**
    - Where: `bridge/network.py:436` runs `run_turn(..., None, True)`, so there is no grant.
    - Confusion: when a teammate's session messages Friday over the network, Friday cannot read rooms, sessions or search for context, unlike when the same ask comes in a room.
21. **Agents see different session sets by path.**
    - In a room turn: the ingress summary has at most 24 live sessions (`local_sessions.py:1417-1443`).
    - Via `local_sessions resources`: all shared sessions, including offline and 7 days of ended (`:1282-1296`).
    - Via `lightbulb_network directory`: the P2P set.
    - Via the hosted/local MCP for people: P2P only.
    - Agents cannot see uploads at all (`apps/backend/routers/sessions.py:64-106`).

### Uploaded sessions (web inspector)
22. **The "Sessions" tab sits under an agent but shows workspace-wide uploads** with no owner name per row.
    - Where: `AgentInspectorPanel.tsx:223`, `SessionsTimeline.tsx:29-73`.
    - Confusion: whose sessions these are, and why they are under Friday.
23. **Share on a teammate's row fails.**
    - Where: the button is shown on every row (`SessionsTimeline.tsx:82`); the backend allows the owner only and returns 404 (`sessions.py:127-128`).
24. **Evidence links point at the wrong system.**
    - Where: Memory-draft links go to `/chat?panel=session&session=<id>` (`messageLinks.ts:11-12`), the live-session panel, but uploaded ids are `<harness>:<id>` (`sessions.py:29`).
    - Confusion: likely dead links.
25. **The timeline starts empty.** Uploads cover only sessions ended after opt-in (`upload.rs:33-42`). `metadata` uploads are never evaluated, although the opt-in copy implies the evaluator reads what you send (`cli.rs:2194` vs `sessions.py:80-90`).
26. **One Mac, two names.** The uploaded `computer` is the CLI's label (`upload.rs:77`) while the relay names the device separately.
27. **The sidebar comment contradicts the code.** `sessionLive` says "the only sessions the sidebar lists", but `sidebarSessionList` keeps ended and offline rows (`localSessionLabels.ts:174` vs `:200-214`).
