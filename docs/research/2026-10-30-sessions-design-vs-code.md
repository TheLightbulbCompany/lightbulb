# Sessions, presence and visibility: what was designed vs what the code does

Read-only, 30 Oct 2026. Designs are read from monorepo `origin/main` (`git show origin/main:<path>`) unless another source is named. The memory v5 spec exists only on `origin/docs/planning-docs-to-main` @ `1262b3293` and was never merged to main. The v6 amendment exists only on `origin/docs/memory-v2`. Code facts come from `session-visibility-map.md` (the map; CLI @ `3215af2`, monorepo @ `f47cc26c8`), plus a few lookups I made, each marked "checked".

## The short answer

**Yes, this was designed.** The 22 Oct network design answered "which sessions exist" with one source: the engine's `directory`. The app was to be "UI over the engine": one sessions component for both the free path and the workspace, with the hub (the backend session relay) deleted at cutover. The 23 Oct Plan C made it concrete. It mounted a `NetworkSessions` component in the workspace sidebar and kept the shared-sessions product as a renamed `workspace_sessions` model, fed from each Mac's engine stream.

**None of the app half was built.** Three things overtook it, and none of them went into a spec:
- The stateless-member redesign (23–24 Oct) moved the workspace into the AgentCore runtime and said "nothing new in the desktop app".
- The "trusted network" ruling (25–26 Oct) deleted holds.
- A commit note in #4381 (26 Oct) says: "Session sharing stays on the backend."

After that, the hub sidebar kept getting new features (#4416–#4454, 26–27 Oct). So the three-systems state in the map is what the shipped code drifted into. It is not what was designed. No later spec revisits the sidebar or the free→workspace transition for sessions.

---

## 0. The documents, in order

| Date | Doc | Role for this question |
|---|---|---|
| 09-09 | `docs/superpowers/specs/2026-09-09-native-multiplayer-messaging-architecture.md` | Rooms, OpenClaw and Paperclip only. It does not cover coding sessions. |
| 10-02 → 10-04 | `2026-10-02-managed-session-sharing.md` (rev 5) | System 2 (desktop relay sharing): who sees and types into a shared session. |
| 10-04, 10-05, 10-05 | `…session-sharing-mod.md`, `…ask-to-pick-up.md`, `…public-session-links.md` | System 2 additions: terminal band, pick-up asks, public links. |
| 10-07 | `2026-10-07-agent-session-approval.md` | System 2: agents ask before messaging a person's session. |
| 10-19 | `2026-10-19-agent-network-design.md` | A hub-based agent network: every member's agents in the sidebar. **Superseded** by the 22 Oct P2P design. |
| 10-22 | `…lightbulb-network-p2p-design.md`, `…-architecture.md` (binding), `…-app-spec.md`, `…-bridge-spec.md`, `…-engine-spec.md`, `…-onboarding-and-hosted-mcp-spec.md`, plan `…-goal.md` | **The unified design.** The engine owns presence. The app renders the engine. The hub is deleted. |
| 10-23 | `docs/superpowers/plans/2026-10-22-lightbulb-network-plan-c-bridge-app-cutover.md` | The most concrete version (Tasks 15–21): `NetworkSessions`, `workspace_sessions` fed by the engine, deletion of the hub. |
| 10-23/24 | `2026-10-23-lightbulb-network-stateless-cloud-member-design.md` (rev 3.1) and `plans/2026-10-24-…-plan-c-phase-1.md` | Replaces Plan C's bridge with an engine sidecar in AgentCore. Messages only, "nothing new in the desktop app". The app tasks were never re-planned. |
| 10-24 | `docs/research/2026-10-24-network-roles-reconciliation.md` | Flat networks. The hybrid rulings: a workspace joins a free network, and its agents answer members only. |
| 10-25 | `2026-10-25-hosted-apps-free-design.md` | Hosted apps route through the person's own computer. The workspace joins "via `lightbulb login` + `invite --agents`". |
| 10-25 / 10-26 | CLI PR #66 (`244eebe`), monorepo PR #4381 (`c11297d93`) | **Rulings recorded only in PR bodies:** no holds, everyone on the network is trusted, and "Session sharing stays on the backend". |
| 10-27 / 10-28 | memory v5 (`1262b3293`), v6 amendment (`docs/memory-v2`) | What teammates see vs what the workspace sees, for captured and uploaded sessions. |

I found no spec or plan dated 25–30 Oct that addresses the sidebar, presence, or the free→workspace upgrade for sessions. I grepped for "invite --agents", "presence", "sidebar", "one list" and "source of truth". The hits are user docs and the network login page, not designs.

---

## 1–2. What was decided, and its status today

Status key:
- **BUILT**: the code does it.
- **PARTLY**: some of it is built.
- **NOT BUILT**: nothing of it is in the code.
- **SUPERSEDED**: a later doc, PR or ruling replaced it (which one is named).

### A. The source of truth for "which sessions exist"

**A1. The engine's directory is the one list. The app reads it and keeps no state of its own.**
> "A sessions sidebar that shows the engine's directory: own sessions and teammates' sessions, with person, agent, folder, busy or idle, what it is doing, and delivery tier." (app spec, Goals 2)
> Anti-goal: "Two daemons on one Mac, or the app and the daemon holding different state about the same session." (app spec, Anti-goals)
> `directory`: "All known sessions across the team … Own and teammate sessions in one list, flagged `own`." (app spec, Interfaces consumed › Sessions and directory)
> Split table: "Session discovery, presence, 'doing' | [engine] owns it | sessions sidebar reads the engine | `find_sessions` through the bridge" (p2p design, "Where each thing lives (the split; owner: do not forget it)")

**Status: NOT BUILT.**
- The desktop app bundles the engine (`apps/desktop/src-tauri/src/engine.rs:1-5`), but "its sidebar still never asks the engine's `directory`" (map, core finding).
- The sidebar reads `GET /api/v1/messaging/sessions` (`local_sessions.py:1299-1378`, `localSessionLabels.ts:167-216`).
- Checked: no `NetworkSessions`, `network_sessions` or `network_hold_request` exists in `apps/frontend` or `apps/desktop` at `f47cc26c8`. The only matches are in `hosted_mcp.py` and the Hermes bridge.

**A2. One component for free people and workspace members.**
> "A second copy of the sessions list for free people and another for members. One component serves both." (app spec, Non-goals)
> "Workspace member: the same component mounts in the workspace shell's sidebar (`WorkspaceSidebar`) above the rooms." (app spec, One component, three surfaces)
> Plan C Task 16 creates `apps/frontend/src/components/network/NetworkSessions.tsx` and modifies `WorkspaceSidebar.tsx`.

**Status: NOT BUILT.** The free path has no app surface at all, only CLI `who` and MCP `find_sessions`.

**A3. The hub is deleted at cutover.**
> "deleted — desktop `transport.rs`, … `presence.rs`, `remote_input.rs`, `snapshot.rs`; backend `local_sessions.py`, `local_session_relay.py`, `local_session_grants.py`, `local_sessions_repo.py`, the `/local-sessions/device/*` routes" (architecture §11)

**Status: NOT BUILT, and SUPERSEDED in practice. No spec records the reversal.**
- Checked at `f47cc26c8`: `local_sessions.py`, `local_session_relay.py`, `local_session_grants.py` and `transport.rs` are all present.
- The #4381 body (26 Oct) says: "Session sharing stays on the backend; the desktop band and notifications still post here."
- The hub sidebar then gained features: #4420 (one sidebar state per session), #4426 (keep ended sessions until archived), #4416, #4453, #4454.
- What #4381 did delete: the hub's *agent* send paths (agent asks, held sends, `agent_send_without_asking`, webhook agents).

**A4. Plan C's middle path (owner ruling O1).**
> "The monorepo stops being a hub, and the shared-sessions product keeps working the same (owner, O1)." (Plan C, Goal)
> "The backend keeps the workspace's session model, renamed `workspace_sessions`. Only its writer changes: the bridge writes it from each Mac's control stream, in place of the hub socket." (Plan C, Architecture)
> "Only sessions the person shares are sent." (Task 18). Test `workspace_sees_members_shared_sessions_only` (Task 19).

**Status: NOT BUILT. SUPERSEDED by the stateless design for the bridge half; the app half was orphaned.**
- The stateless design: "It replaces the stateful 'bridge' in Plan C … nothing new in the desktop app" (§1, §4.1). "Phases: (2) session streams to the web and phone, through the member, to AppSync" (§10).
- Phase 1 plan: "Phase 1 is messages only. Session streams to the web and phone are phase 2."
- Phase 2 was never planned. `local_sessions.py` keeps its name and its own writer (the desktop relay).

### B. Who sees what

**B1. A teammate on the network (free or not).**
> "Live directory: every session on every teammate's computer with person, computer, agent, repo, branch, busy or idle, and what it is doing" (p2p design, parity item 3)
> "For each of your sessions that is not hidden: the agent, the computer, the folder's last part …, the repository and branch, whether it is busy or idle, its title, and what it is doing now … They never see your files or your agents' conversations, only the messages sent to them." (CLI `docs/security.md:235-240`, checked)
> "Teammates still see presence, not sessions" (memory v5 §6, "Upload, by choice")

**Status: BUILT for system 1.** See map rows `find_sessions` / `who --all` (`cli/src/presence.rs:18-31,143-211,425-525`).

The rule is not honoured across systems. A workspace teammate also sees the full live terminal and transcript of every session on a Mac with desktop sharing on (B3). That happens even if the owner ran `folder off` (map #7).

**B2. A workspace member (app).**
> "The existing shell, unchanged. The sessions list now also shows hosted agents' presence from the bridge as members of the team (a workspace appears as a computer named for the workspace)." (app spec, What a workspace member additionally sees)
> Acceptance 13: "Sessions sidebar shows the same engine list." (app spec)

**Status: NOT BUILT.**
- The sidebar shows only system 2 (map row "Desktop app sidebar").
- The workspace's agents appear only in P2P presence, as person "The Lightbulb Company" (map C.4, `net_ops.rs:718-728`, `presence.rs:428`).

**B3. Shared desktop sessions (system 2).**
> "Every Claude Code and Codex session on a Mac with sharing on is tracked and shared with the workspace, whether or not Lightbulb started it." (managed session sharing, rev 5, owner rulings 2026-10-04; this reverses §2.1 "Plain `claude`/`codex` is private")
> Read by any member (ruling 2026-09-24, cited in agent-session-approval §2 decision 2). Typing only by the owner, people in `allowedSenders`, or anyone under team control. "Agents never get the screen or input." (managed session sharing §2.9, §4)

**Status: BUILT.** Map rows "Desktop app sidebar", "Session-sharing live view"; `local_sessions.py:988-1001,1136-1180`; `remote_input.rs:1-12`.

**B4. The workspace (its cloud member) sees the whole directory, and is told so.**
> "`presence` … every member sees the whole directory (linking a workspace means the workspace sees it too; the link flow says so)" (architecture §6)

**Status: PARTLY.**
- The cloud member sees the full P2P directory, including free non-members on the same network (map row "Cloud agents: `lightbulb_network directory`").
- The link flow does not say so. Checked: `apps/frontend/src/app/auth/network/page.tsx:146` says only "Signed-in members' computers on that network can then message your agents."

**B5. Cloud agents and shared sessions.**
> "`resources` lists every shared session in the workspace for a room grant … For a Paperclip grant, only the shared sessions of people who opted in" (agent-session-approval §2, decision 2)
> "T11 two sessions tools: … one tool beside `lightbulb_local_sessions`, named so the model cannot confuse them" (stateless design, Index; Plan C phase 1, Task 11)

**Status: BUILT.** Map rows "Cloud agents: `lightbulb_local_sessions`" and "`lightbulb_network directory`". Having two tools was a deliberate phase-1 choice. That choice is the source of map #19 and #21.

**B6. Captured and uploaded sessions.**
> Capture: "Every session your agents run, recorded on this computer … Nothing leaves." Signed in: "One timeline of your agents across computers." Workspace: "Timeline of every agent and machine in the workspace … a session shared by link" (memory v5 §2 table)
> "in a workspace the timeline shows sessions to the workspace's members because the workspace pays for and owns that record" (memory v5 §6). Founder decision 4 (§12): "Capture is local by default; the upload choice is the person's; workspace members see the workspace's timeline."

**Status: PARTLY.**
- Built: workspace-wide rows (`WSESSIONS#`), 30/365-day retention (`session_store.py:29`), 7-day links.
- Not designed: where the timeline lives. It is under an agent's inspector (map #22). No owner name per row (#22). A Share button on others' rows (#23). Evidence links point at system 2 (#24).

### C. Approval and holds

**C1. The 22 Oct hold model.**
> "Hold policy … Defaults: the person's own computer sessions `accept`; the person's own cloud member follows `agents_send_without_asking` (default `false`, so their own Friday is held until they flip it …); everyone else `hold`." (architecture §7)
> Session record carries `can_send, needs_owner_ok` (architecture §9).
> App spec flows: "A held message arrives", "Allow, Decline, Always allow". p2p acceptance 2–4 and 8.
> Earlier versions of the same idea:
> - 10-19: "A message from someone else's agent is held until the receiving agent's owner OKs it … Allowing a person allows all of that person's agents (owner, 2026-10-19)" (agent network design §6)
> - 10-07: "Otherwise `send` and `interrupt` ask, unless the session owner turned on `agentSendWithoutAsking`" (agent-session-approval §2.4); owner ruling "Friday should need my approval to message my Claude Code session".
> - 10-24 (stateless design, job 4): "On each Mac, Friday's first message is held once in the native dialog".

**Status: SUPERSEDED.**
- CLI #66 (`244eebe`, 25 Oct): "An invite is the approval; everyone on the network is trusted … delete message holds and per-person policy … membership is the only gate for messages; State::Held stays parseable for older peers' acks."
- Monorepo #4381 (`c11297d93`): "**Owner ruling 2026-10-26: everyone in a network is trusted, so nothing holds a message.** … agent_send_without_asking removed".
- User docs say the same: `agent-network-teams.md:48` "Everyone on the network is trusted … nothing is held and there is no Allow", plus CLI `docs/usage.md:124`, `docs/security.md:31`.
- The ruling lives only in PR bodies and user docs. The binding architecture doc still describes holds.
- Leftovers in code: `needs_owner_ok` (`presence.rs:442`), `can_send: true` (`:441`), receipt texts that can never be produced (`mail.rs:196,222`), and `HELD_S` "nothing holds in Plan A" (`mail.rs:20`). These are map #8, #9, #10.

**C2. Admission (joining a network).**
> "Admission needs a person's yes … A ticket minted `invite --open` admits without the prompt" (architecture §4). Roles: "only the founder and founder-marked admins remove".

**Status: SUPERSEDED.**
- 10-24 roles reconciliation: "Every network is flat, like Reason".
- CLI #66: "joins admit with no Allow".

**C3. Session-sharing gates (system 2): read, type, join request, pick-up.**
These come from managed session sharing §2/§4, the sharing mod, and ask-to-pick-up.

**Status: BUILT, and still in force for system 2.** Map Reach row "Teammate (person) → someone's shared session". These are the only real person-approval gates left anywhere.

### D. The workspace joining the network (what changes at `invite --agents`)

**D1. The hybrid rulings (owner, 10-24).**
> "1. A workspace joins an existing free network as one more member … 2. Every network is flat … 3. The workspace's agents answer workspace members only. Free people on the same network talk to each other's sessions as normal; a message from a non-member to Friday or another workspace agent is refused, not held." (roles reconciliation, "Hybrid rulings")
> "Membership for agent access lives on the cloud member: the backend writes the workspace's member person ids … into `sessions.json` … One list, one check, no new roles." (same)
> "The workspace is not an app route. It stays what it is: its agents join a network via `lightbulb login` + `invite --agents`, behind the member gate." (hosted-apps-free design, Routes)

**Status: BUILT.**
- `invite --agents`: CLI `src/cli.rs:966-973`, `src/login.rs:342` (checked).
- The members gate: `cli/src/mail.rs:815-817`, `proof.rs:113-126`, `network_member.py:80-118` (map B, C.3).
- A deviation: the proof carries no workspace claim (map #12).

**D2. What `invite --agents` was designed to change.** Collected from the docs above, the designed effects are:
- (a) The workspace's engine member joins the person's existing network.
- (b) Friday and the other agents become that member's declared sessions (stateless design job 5: `cloud.sessions.set`).
- (c) Only signed-in workspace members can message them.
- (d) Memory `team` entries sync with the cloud member (memory v5 §4.2; `apps/frontend/src/content/docs/pages/memory.md:28`).
- (e) The workspace sees the whole directory (architecture §6, B4).

**Status: (a)–(d) BUILT, (e) built but not disclosed.**

Nothing designed says what changes **in the app** at `invite --agents`. The 22 Oct answer was "nothing changes, because the app already shows the engine's list". That answer depended on A1, which was not built. So today:
- Members' sidebars are unchanged.
- Network-only sessions (Cursor, Grok, OpenCode, any Mac without desktop sharing) never appear in the app.
- Friday appears in teammates' `find_sessions` as a person named after the company.

**D3. Labels for the workspace's agents.** Four answers, one per doc:
- Architecture §5: `<App> via <name>` for a bridge-role member.
- Plan C, Global Constraints: "Peers see `<Agent> · <workspace name>` (e.g. `Friday · The Lightbulb Company`) … The `<App> via <person>` form is retired."
- App spec: "a workspace appears as a computer named for the workspace".
- Stateless design §4.2: "So the workspace founds as an ordinary computer."

**Status: PARTLY.** The code follows the stateless design. The person is the Clerk org name and the label is "The Lightbulb Company's Friday (not verified)" (map C.4, #14; `label.rs:27-50,77-79`).

### E. Row shape and offline

**E1. Status and online state.**
> Session record `status: busy|idle|gone` (architecture §9). `directory` rows carry `last_seen` (app spec). "Directory stale (teammate offline) | Rows dim with 'last seen'; messages show `stored` and where they wait." (app spec, Failure modes). Plan C Task 16: "Offline rows are dimmed and show `last seen`."

**Status: PARTLY.**
- The code has `status busy|idle` plus `online: bool`. There is no `gone`, and `last_seen` is not exposed.
- `who --all` hides `online` and always prints "idle" (map #3, #4, #5; `cli.rs:271`).

**E2. Delivery tier and "where it waits".**
> "The sender is told the tier." (architecture §7). The receipt extension returns `waits_on`, `expires_at` (app spec).

**Status: PARTLY.** The tier is frozen from the last presence (map #6).

**E3. Hiding a folder.**
- Parity item 13: "folder on/off" (p2p design).
- Plan C Task 16 puts `folders_off` and `share_paths` in the app's `NetworkSettings`, beside "Share every new session".
- Memory v5: "Folders hidden with `lightbulb folder off` are not captured."
- Managed sharing keeps "The per-Mac switch and pause" (rev 5).

**Status: PARTLY.** `folder off` works for P2P and capture. Desktop sharing never reads it (map #7). Plan C's single settings surface was not built.

### F. Agents and sessions

**F1. Agents message sessions only through the engine.**
> Plan C Task 14 ("Friday's tool on the network"); #4381: "Agents message only through the engine … find_sessions is network-only; no hub-session or agent_id sends".

**Status: BUILT.** `local_sessions.py:1680-1681`; `hosted_mcp.py:179-182,370-394`.

What was never designed: how an agent addresses a session it found as a relay UUID (map #19).

**F2. Inbound network messages to Friday become local turns, not room turns.**
> "Inbound network messages to Friday run as local turns (job 2). They are not posted into a room. … Rooms and the network stay disconnected." (stateless design §3)

**Status: BUILT** (`bridge/network.py:436`). This decision has a consequence nobody designed for: those turns have no grant, so they cannot read rooms or sessions (map #20).

---

## 3. Where the designs conflict or leave the question open

1. **Delete the hub vs keep it (the core conflict).**
   - Architecture §11 and the app spec delete `local_sessions.py` and its relay, and make the engine directory the only list.
   - Plan C (owner O1) keeps `workspace_sessions` for the shared-sessions product. So even Plan C has two lists in the same `WorkspaceSidebar`: `NetworkSessions` (all live network sessions) and the shared-session model (rows fed from the same Mac's engine stream). Plan C says nothing about how a session that is in both is shown once, or which id it uses.
   - #4381 then says "Session sharing stays on the backend" and leaves the desktop relay as its writer.
   - Three positions. Only the first is written as a design.

2. **"Teammates see presence, not conversations" vs "every session is shared with the workspace".**
   - CLI `docs/security.md:235-240` and memory v5 §6 say a teammate sees metadata only.
   - Managed session sharing rev 5 says every Claude/Codex session on a sharing Mac is readable by every workspace member, terminal and transcript included.
   - Each is true in its own system. No doc says which rule wins for a person who is in both a network and a workspace.

3. **Holds.**
   - The architecture (binding), app spec, p2p acceptance, 10-19, 10-07 and the stateless design (job 4, "Friday's first message is held once") all design holds.
   - CLI #66 and #4381 delete them.
   - The architecture doc, the `held` receipt state and `needs_owner_ok` were never amended. This is why the code still carries the vocabulary.

4. **The workspace's identity on the network.** Bridge-role cloud member (architecture §4–5) vs `kind: cloud`, `person_id = ws:<owner>`, label `Friday · <workspace>` (Plan C) vs an ordinary `computer` founder (stateless design §4.2). The code took the last one, which gives map #14 and #15.

5. **Privacy switch.**
   - `folder off` is the engine's (parity 13; CLI security doc "Hide a folder with `lightbulb folder <path> off`").
   - The desktop's per-Mac sharing switch and pause are separate (managed sharing rev 5).
   - Plan C would have put both on one settings page, but still as two switches. One switch was never designed.

6. **Agent approval went through four versions, then was dropped.** The versions were 10-07 (ask, `agentSendWithoutAsking`), 10-19 (allowing a person allows their agents), 10-22 (`cloud_of`, `agents_send_without_asking`, own Friday held) and 10-26 (trusted, nothing held). Only the last is in code. It is recorded in a PR body, not a spec.

7. **Left open in every doc:**
   - **Retention.** P2P presence is 1 day (`presence.rs:22`). Sidebar ended rows are 7 days or until archived. Uploads are 30/365 days. Links are 7 days. No doc sets one retention rule for "a session".
   - **What a free non-member on a workspace-joined network sees of Friday.** The hybrid ruling covers only sending ("refused, not held"). Visibility was never decided, so today they see Friday in the directory and get `not_a_member` if they message it.
   - **What a network-mail turn may read.** The stateless design keeps rooms and the network apart and never says what context such a turn gets.
   - **How an agent goes from a shared-session UUID to a network address** (map #19).
   - **Status vocabulary.** Architecture §9 says `busy|idle|gone`. The backend uses `idle|busy|waiting-approval|unknown|offline|ended`. No doc reconciles them (map #3).

---

## 4. The gap

**Solved on paper, never built:**
- **#1, two or three lists with no shared id.** Architecture §1/§9/§11, the app spec (Goals 2, Anti-goals, "One component, three surfaces") and Plan C Tasks 16/19 decided on one engine directory, read by one `NetworkSessions` component in both the free page and `WorkspaceSidebar`, with the hub deleted. None of it was built, and #4381 reversed the hub deletion in a commit note.
- **#3, #5: offline vocabulary.** The app spec and Plan C Task 16 decided: offline rows dimmed with `last seen`, `status: gone`, "never an empty list as if it were true". Not built.
- **#6: stale tier.** The app spec's receipt extension (`waits_on`, `expires_at`, "where mail waits") is partly built.
- **#7: `folder off` vs desktop sharing.** Plan C Task 16 put `folders_off` beside "Share every new session" in one settings page, over the engine's list. Not built.
- **#14, #15: agents looking like a company-named person.** Plan C set the label `Friday · <workspace name>`, and the app spec "a computer named for the workspace". The stateless redesign dropped both without saying so.
- **#26: one Mac with two names.** Once the app attached to the engine there was one computer name. Not built.
- **B4: telling the person the workspace sees the whole directory.** Required by architecture §6 ("the link flow says so"). Not built.

**Decided, then superseded, with leftovers:**
- **#9, #10, part of #8**: `needs_owner_ok`, the receipt texts promising "held" that no longer happen, and `can_send` all belong to the dropped hold model (C1).
- **#11**: the gate asymmetry is intended (hybrid ruling 3). Its error text was never designed.

**Never designed:**
- **#2**: one retention rule.
- **#19**: UUID ↔ network address.
- **#20**: what a network-mail turn can read.
- **#21**: agents seeing different session sets by path. The stateless design chose two tools on purpose; nobody designed how they agree.
- **#22–#25**: where the uploaded-session timeline lives, owner names on its rows, Share on others' rows, evidence links.
- **#4**: the `who --all` "idle" bug.
- **#12, #13**: the gate's docs claim, and a stale members list on a parked VM.
- **#16**: verified names. Explicitly deferred to phase 3 (stateless design §10).
- **#17**: two meanings of one header. The architecture §5 copied the frame verbatim on purpose, so this collision is a decided choice.
- **#18, #27**: doc and comment drift.

**What actually happened.** The free network shipped as designed. The workspace joined it as designed (`invite --agents`, members gate). The step that would have made the app one list on top of it was the app half of Plan C (Tasks 15–21). The stateless rewrite orphaned that half, and two later PR-level rulings (trust; "session sharing stays") undercut it. No document since 24 Oct says what the app should show.
