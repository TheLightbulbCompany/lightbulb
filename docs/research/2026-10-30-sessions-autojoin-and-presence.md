# Auto-join on desktop sign-in, and ended-session presence: code trace

Read from `origin/main` on 30 Oct 2026. Engine = `lightbulb-network.nosync` (E:), monorepo = `lightbulb.nosync` (M:). Every line number is from `git show origin/main:<path>`.

## 0. Corrections to the brief

- **Invite tickets do not expire in 10 minutes.** A once link lasts **24 h** and a reusable link lasts **7 days** (E: src/ticket.rs:16-18, 62). The 10 minutes is the *four-word code* that can stand in for a ticket (E: src/code.rs:1-5, 27). `invite --agents` asks for no code (E: src/cli.rs:966-970; src/daemon/ipc.rs:419-425).
- **No invite waits for an Allow any more.** Every link is signed in mode `open` (E: src/daemon/net_ops.rs:23-25), and "An invite is the approval: nobody is asked" (E: src/admission.rs:212-221). The bridge and backend docstrings still say the workspace's join "waits for that admin's Allow" (M: apps/infra/hermes/bridge/server.py:484-487; apps/backend/core/services/network_member.py:181-183). Those docstrings are out of date.
- **The shipped model reverses the 23 Oct spec.** The spec has the workspace found the team and mint each person's line (spec §2 lines 45-46, §4 lines 241-245, E8 lines 381-385). What shipped is different: "A workspace never invites: it joins one network when an admin runs `lightbulb invite --agents`" (M: network_member.py:10-11), and the engine refuses `team.invite` from an instance (E: net_ops.rs:108-111).

## 1. Engine: tickets, admission, join, login

### Minting
- `team.invite {mode}` (E: net_ops.rs:595-631) reprints this member's live reusable link, or mints a new one through `new_link` (net_ops.rs:660-680). `new_link` writes a signed roster `link` entry and a `team_link` row.
- `ticket::mint` (E: ticket.rs:53-74):
  - the secret is 32 random bytes;
  - `ticket_id` is `<inviter short>.<ulid>`;
  - a once link stores only `sha256(secret)` in the roster (ticket.rs:44-47, 114-117).
- `sign` (ticket.rs:78-111) lists the members a joiner may dial: the inviter first, then the founder, at most 6 (`MAX_MEMBERS`, ticket.rs:19, 88-94). It also carries `relay_hint` and the inviter's direct `addrs`.
- The ticket has an optional `wake` field, which the signature covers (ticket.rs:37-40, 262-281). **No production code sets it.** A grep for `.wake =` / `wake: Some` finds only the test at ticket.rs:270.
- **What a ticket binds:** team_id, link id, secret, mode, inviter, expiry. It does **not** bind a person, a name or a role:
  - every admission writes `role: "member"` (admission.rs:320-321);
  - replay accepts only `member` (roster.rs:403-413);
  - the founder is just whoever wrote the genesis (roster.rs:76-82, 352-358).
- **Agent callers:** when the caller of `team.invite` is an agent, the link is forced to `Once` (net_ops.rs:98-107), and the admitting computer shows a Keep/Remove notice afterwards (admission.rs:223-229).

### Admission (on the member being dialed)
1. `serve` drops any request whose `candidate` is not the QUIC peer or that the peer did not sign (admission.rs:147-170).
2. A peer that is already a member gets the roster again (admission.rs:176-183).
3. `check` (admission.rs:265-297) then requires all of:
   - `ticket::verify`: the inviter's signature, the same team, and a live link from that signer with the same mode and secret (ticket.rs:153-170);
   - the link has not expired (admission.rs:275);
   - the secret proof is `sha256(secret‖candidate)`, and the link is not already used (admission.rs:280-284);
   - the name fits, and neither the name (case-insensitive) nor the 12-character key prefix collides (roster.rs:216-227);
   - the admitting computer is itself a member.
4. Rate limits are 20 open requests per team, 5 per inviter and 1 per candidate (admission.rs:62, 206-211).
5. It then writes the `admit` entry (admission.rs:310-337) and sends a signed `join_decision` followed by the whole roster (admission.rs:348-375).

**No sign-in proof is checked at admission.** A ticket is a bearer credential: anyone holding it gets in.

### `join` on the candidate (E: admission.rs:419-573)
- **A computer joins only while it is solo** (`is_solo`: it is the only member of its roster, roster.rs:209-212). Otherwise the answer is `refused / already_in_team` with the team's name (admission.rs:439-446). The CLI then says "You are in "X". Leave it first: lightbulb leave" (cli.rs:1076). **So one team per computer, and you must leave first.**
- **A computer that founded its own one-member network counts as solo, so it can join directly.**
- **A founder whose network has other members must leave.** Nobody can remove a founder; it can only leave (roster.rs:501-512; net_ops.rs:642-645). After it leaves, the remaining members keep the team with no founder.
- Dialing:
  - it dials the ticket's members in order (admission.rs:502-551);
  - it keeps dialing for `DIAL_WAIT` = 30 s (E: src/net/mod.rs:57);
  - if the ticket carries a `wake`, it rings the doorbell once and keeps dialing for `WAKE_WAIT` = 150 s (net/mod.rs:56; admission.rs:493-497, 552-558). The CLI prints "Waking your workspace…" (cli.rs:1036-1046).
- **What is kept:** `take_roster` swaps in the new roster only (`store.clear_roster`, then accept every entry, inside a transaction; admission.rs:652-674). Mail, sessions and memory in `state.db` are untouched.
- `leave` (net_ops.rs:270-321):
  - it writes a `leave` entry, tells online peers, waits up to 5 s, then starts a fresh solo genesis stamped after the old root;
  - the CLI says "Messages already stored here stay" (cli.rs:1268);
  - links minted in the old network stop working.
- **Who may call `team.join`:**
  - from a socket, any caller that resolves to an actor, meaning a person (`local`) or a registered agent session (ipc.rs:339-342, 166-174);
  - an instance (the bridge) may always call it.

### `lightbulb login` (E: src/login.rs, src/proof.rs)
- It is a device flow (login.rs:284-327):
  1. challenge;
  2. the computer signs `{purpose: lightbulb-login, challenge, key}` with its member key (login.rs:75-85, 258-265);
  3. the person confirms the code in the browser;
  4. the computer receives a proof, an EdDSA JWS `{iat, exp, sub: user_…, computer: <key>, org?}` that lives at most 30 days (proof.rs:1-5, 56-65, 69-104).
- The proof is saved in `login.json` with mode 0600 (login.rs:48-63, 337-364). It is renewed in its last week (login.rs:404-431).
- **Does a signed-in computer prove its Clerk identity to peers? Yes, but only as an optional claim.**
  - It sends the proof in every `hello` (net/mod.rs:1431-1447; frames.rs:105-108), and peers store it as-is (net/mod.rs:1100-1106).
  - Only the **cloud member** acts on it: it answers mail only from computers whose proof names one of the workspace's `members` and whose key is not revoked (proof.rs:113-126; net/mod.rs:1276-1285). Memory sync also uses it to decide what is shared as "me" (memsync.rs:25-30, 55).
  - **On a Mac, proofs cannot be verified.** `ISSUER_KEYS` is empty "until the key ceremony" (proof.rs:25-27). Only `LIGHTBULB_ISSUER_KEY` verifies a proof, and the backend gives that key only to the Hermes runtime (M: hermes_provisioning.py:162). `lightbulb signin` refuses with "Verified names are not available yet" (cli.rs:1096-1100).
- **The desktop app never signs the engine in.** Nothing in `apps/desktop` writes `login.json` or calls `/network/device/*`. `engine.rs` only runs `version` and `setup` (M: apps/desktop/src-tauri/src/engine.rs:1-75). `headless.rs join` is the older enrollment pairing (headless.rs:298, 400), not the network.

## 2. The workspace's cloud member

- **What it is:** the engine as a sidecar (`lightbulb member`) in the AgentCore microVM, an embedded instance on a token-gated Unix socket (E: src/member.rs:1-9, 40-111, 170-204). Every op runs as `Caller::Instance` (ipc.rs:146-153).
- **Which network it is in:** it starts on its own solo network, named for the workspace, with itself as founder (`solo_genesis`, net_ops.rs:364-370; `leave` recreates it, net_ops.rs:304-312). It moves to another network only when an admin runs `invite --agents` (§3). From then on it is in **that admin's computer's network**.
- **Can it mint invites? No.**
  - net_ops.rs:108-111: `if op == "team.invite" && caller == Caller::Instance { return UNSUPPORTED }`, with the comment "A workspace member never invites".
  - ipc.rs:177 says the same.
  - The bridge's RPC table has no invite method. It registers `network.join`, `network.sessions`, `network.hold`, `network.leave`, `network.wake` and the memory ops (M: bridge/server.py:522-525, 531-540). There is no `team.get` either, so the backend cannot ask which team the member is in.
  - No backend route asks it to mint.
- **How the backend reaches it:**
  1. `container_rpc.call(owner, method, params)` sends an `InvokeAgentRuntime` envelope `{method, params, timeoutMs}` through `runtime_transport` (M: container_rpc.py:47-83; runtime_transport.py:41 (90 s ingress timeout), 273).
  2. The bridge's `_rpc` looks the method up in `RPC_METHODS` (server.py:543-557).
  3. `_net` starts the sidecar in the foreground, then sends the engine op over the socket (server.py:464-476; bridge/network.py:248-276).
- **Doorbell:** `POST /api/v1/network/wake` with `Authorization: Bearer <owner>.<secret>` (M: routers/network.py:133-139) leads to `network_member.ring` and a queued wake (network_member.py:451-528). That ends in the bridge's `network.wake`, which only runs `ensure_started` (server.py:517-519). After a join, the instance publishes its doorbell into the team as a `meta.wake` (admission.rs:543-546; roster.rs:455-464).
- **Cold start is about 55 s.** That number comes from comments (network_member.py:140, 538). `call_op` never wakes a parked runtime (network_member.py:536-541). `join_network` does wake it, and for a first-time workspace it also mints the member key and restarts the runtime (network_member.py:186-195).

## 3. `lightbulb invite --agents` end to end

1. **On the admin's Mac** (E: cli.rs:966-976):
   - `team.invite {mode: once, code: false}` mints a 24 h once link from this computer;
   - `login::invite_agents(ticket)` (login.rs:433-443) runs the device flow with purpose `agents`;
   - the signature covers the ticket (login.rs:72-85), and the body carries the computer's stored proof (login.rs:291-296). Without a proof it fails with "Sign this computer in first".
2. **Backend `/network/device/start`** (M: routers/network.py:52-58 → services/network_login.py:131-162):
   - checks the signature and consumes the nonce;
   - checks that the proof is valid for this key and not revoked, else 409;
   - stores a device row with `{purpose: agents, computerKey, ticket, computerUser}`.
3. **Browser confirm** at `/network/device/approve` (network_login.py:175-208):
   - `require_org_admin`;
   - `owner_id = resolve_owner_id(ctx)` (the active workspace);
   - `computerUser` must equal the person confirming;
   - then `network_member.join_network(owner_id, ticket)`.
4. **`join_network`** (network_member.py:180-210):
   - provisions the member if needed;
   - calls `container_rpc.call(owner, "network.join", {ticket})`;
   - then sets `_mark_joined(owner, True)`;
   - `already_in_team` becomes `NETWORK_ALREADY_JOINED` "already on another network".
5. **Bridge `_network_join`** (server.py:483-500):
   - refuses if `team.get` shows more than one member ("the first network wins");
   - otherwise answers `{state: "joining"}` at once;
   - `join_in_background` then calls engine `team.join` with the workspace name and waits up to 660 s (bridge/network.py:300-336).
6. **Who runs it:** a workspace admin on their own signed-in computer.
7. **What it records:** only `"joined": true` in the S3 owner object `<owner>/network/sessions.json` (network_member.py:36-37, 117-118, 213-222). Next to it is the engine snapshot `<owner>/network/state.db` (network_member.py:190).
   - **No table or row stores the network or team id, or which admin's network it was.** The device-login row with the ticket is transient (network_login.py:149-162, 192, 207).
   - `joined` is set when the bridge answers `joining`, **before the engine is actually admitted** (network_member.py:197-198 vs server.py:497-498). A join that later fails still leaves `joined: true`.
8. **Undo:** `POST /network/leave` (admin) → `network.leave` → `team.leave` → `joined` removed (routers/network.py:77-85; network_member.py:225-237).

## 4. Several members, each with their own network

- Every Mac founds its own solo network at setup.
- If admins A and B both run `invite --agents`:
  - whichever join is admitted first wins;
  - the second gets `already_in_team` from the bridge's member count (server.py:495-496) or from the engine's solo check (admission.rs:444-446). The backend shows it as `NETWORK_ALREADY_JOINED` (network_member.py:206-209).
- **Race:** if B arrives while A's join is still in flight, the member count is still 1, so the bridge spawns a second background join. The engine allows one `team.join` at a time (`joining.try_lock`, so the second gets `BAD_REQUEST`, admission.rs:424-426), or the second refuses once the first has finished (admission.rs:657-659). B's browser still shows "Confirmed. … is joining" (login.rs:441), and the backend wrote `joined: true` both times.
- **"The workspace's network" is whatever roster the cloud member holds:** A's network, or the member's own solo network if no admin ever ran it. The backend cannot tell which, because no team id is stored and no `team.get` RPC exists.
- A non-admin member's Mac stays on its own solo network until someone sends it a line.

## 5. Simplest buildable auto-join path

**Flow:** desktop app sign-in (Clerk) → `POST /api/v1/network/join-line` (Clerk auth) → backend checks membership and wakes the member → RPC `network.invite {name}` → engine `team.invite` mints a once link, adds `wake` and signs → line returned → app runs `lightbulb join '<ticket>' --name <name>` (or calls the `team.join` IPC op) → app shows a notice ("Your computer joined <Workspace>'s network").

### What exists
| Piece | Where |
|---|---|
| Once-link minting, signed into the roster, 24 h | E: net_ops.rs:595-631, 660-680; ticket.rs:53-74 |
| Admission with no Allow | E: net_ops.rs:23-25; admission.rs:212-231 |
| Joiner handling of a workspace ticket's `wake` (ring once, keep dialing 150 s, CLI message) | E: admission.rs:489-497, 552-558; net/mod.rs:56; cli.rs:1036-1046, 1081 |
| Ticket field `wake`, covered by the signature | E: ticket.rs:37-40 |
| The member's own doorbell, held in memory | E: daemon/mod.rs:121-122 (`Cloud.wake`) |
| RPC path to the member, plus the bridge `_net` helper | M: container_rpc.py:47-83; server.py:464-476 |
| Clerk auth, active workspace, admin helpers | M: core/auth.py:98 (`resolve_owner_id`), 125 (`require_org_admin`) |
| The workspace's people list (same one as the sidecar gate) | M: network_member.py:82-83, 114 (`_humans_to_ask`) |
| Proof-signed computer auth (if the engine calls instead of the app) | M: network_login.py:399-475 (`computer_person`; `free_ok` returns `org_id=None` for a person with no workspace, 467-468) |
| Join result states and reasons for the app to show | E: admission.rs:24-38; cli.rs:1057-1090 |

### What must be built
1. **Engine:**
   - admit `team.invite` for `Caller::Instance` by removing net_ops.rs:108-111;
   - force `Once` for an instance, the way agent calls already are (net_ops.rs:98-107);
   - put the instance's `cloud.wake` into the ticket before signing. `sign` sets `wake: None` (ticket.rs:106), so it needs a parameter or a re-sign as in the test at ticket.rs:276-277.
   - `actor` is `None` for an instance (ipc.rs:166-169), and net_ops.rs:118-120 refuses `not_a_person` when the actor is `None`, so that check also needs an instance exception.
   - Optionally, bind the ticket to the person's name. Nothing binds it today.
2. **Bridge:** a `network.invite` RPC method → `_net("network.invite", "team.invite", {mode: "once", code: false})`, registered at server.py:522. It should return the `ticket`/`line` and log neither.
3. **Backend:** the `POST /network/join-line` route:
   - Clerk `get_current_user`, then `resolve_owner_id`, then check the caller is in `_humans_to_ask(owner)`;
   - require `sessions_exist(owner)`, or decide whether to provision;
   - `container_rpc.call(owner, "network.invite", …)` with the 90 s ingress timeout;
   - set `no_store` on the response and never log the line;
   - handle UNAVAILABLE / RUNTIME_UNAVAILABLE as a retryable 503, as `join_network` does (network_member.py:202-205).
   - Optionally add a `team.get` RPC and store the team id, so the backend knows which network it is.
4. **Desktop app** (nothing exists today):
   - after Clerk sign-in, call the route and run the join;
   - show the notice and map `already_in_team`, `name_taken` and `unreachable` to app copy;
   - remember an opt-out;
   - **also sign the engine in.** Without `login.json`, the cloud member's `members` gate refuses this Mac's mail to Friday (net/mod.rs:1276-1285; proof.rs:113-126), so joining alone does not let the person talk to the workspace agents. There is no "sign in with a Clerk token" engine path today. Two options:
     - build one;
     - have the app run `lightbulb login` and approve the device code itself through `POST /network/device/approve`, which already takes Clerk auth (routers/network.py:67-74).

### Risks
- **Cold start.** The first call to a parked member takes about 55 s (network_member.py:140), inside a 90 s ingress limit (runtime_transport.py:41). A first-time workspace also needs key minting and a restart (network_member.py:186-195). Run the route in the background with a "Connecting…" state. Once the member has just minted, it is warm, so the joiner's dial should reach it. If it parks again before the dial, the ticket's `wake` covers it, provided item 1 sets it.
- **Solo workspace member.** If no admin has run `invite --agents`, the member is solo, and the first auto-join makes the member's own network the workspace network. After that, any admin's `invite --agents` fails with `already_in_team` (server.py:495-496). Decide whether that is the intended model (it matches the 23 Oct spec), and if so, retire or reword `invite --agents`.
- **Mac already in another multi-member network.** `join` refuses with `already_in_team` (admission.rs:444-446). Auto-leaving would silently drop the person's teammates, so ask the person instead. A Mac that only holds its own solo network can join directly, and its mail, sessions and memory stay (admission.rs:663).
- **Mac already in the workspace's network.** It also gets `already_in_team`. The app can compare `team.get.team_id` with the minted ticket's `team_id` (ticket.rs:25) and treat a match as done. Better, check before minting, so no stray once links pile up in the roster.
- **Name collisions.** Names are unique per network, ignoring case (roster.rs:216-227), and every computer is its own person (roster.rs:90-92, 909-928). A person's second Mac with the same name gets `name_taken`. Use `<Name> (<computer>)` or retry with a suffix (cli.rs:1077-1079).
- **Bearer credential.** Admission checks no proof, so the line must reach only that person, shown once and never logged. A once link used by someone else is spent for good.
- **Free users.** A person with no workspace has no cloud member: `computer_person(free_ok)` returns `org_id=None` (network_login.py:467-468). Skip auto-join for them. Also decide whether a workspace with no provisioned runtime should provision one from a sign-in (costly) or skip.
- **Leaving.**
  - A person's `lightbulb leave` drops the Mac to solo (net_ops.rs:270-321). The next app launch would re-join it, so the app needs a remembered opt-out.
  - A computer removed by a teammate has `removed.json` (net_ops.rs:455-456), which the app should respect.
  - A workspace `network.leave` sends the member back to its own solo network (server.py:507-510). Every auto-joined Mac is then left in a network without the workspace.
- **Stale `joined`.** It is set before admission (§3), and no team id is stored. Do not use it to decide whether to mint.

## 6. Ended sessions in presence, compatibility, offline representation

### What is published (E: src/presence.rs)
- **On a computer,** `build()` (presence.rs:147-211) builds three lists:
  - **`sessions`:** the live sessions, newest first, at most 100 (`MAX`, presence.rs:19, 183-185), plus the hosted apps over the route link (202-207). `status` is `busy` or `idle` (`STATUSES`, frames.rs:43-44; presence.rs:102). `tier` is the delivery tier: `push`, `next_step` or `pull` (sessions.rs:69-85).
  - **`known`:** the addresses of non-live sessions that still hold waiting mail (presence.rs:192-196).
  - **`kept`:** the addresses of non-live sessions, either ended or no longer running, whose last-seen or ended time is within `DAY` = 86 400 s (presence.rs:20-22, 197-201).
  - `ended` rows come from `store.ended_since(now - DAY)`, which also **deletes** rows that ended earlier (E: store.rs:460-469; the `ended_at` column, store.rs:69-70).
  - Folders turned off with `folders_off` are filtered out (presence.rs:156, 174).
- **Ended sessions go out only as bare addresses** in `kept` (and `known`), with no agent, title, repo or end time.
- **On an instance,** the presence is only its declared sessions, with `known` and `kept` empty (presence.rs:148-150, 269-277).
- **On the receiving side:**
  - `put` stores `seq`, `seen_at`, `sessions` and `kept`, and drops `known` (presence.rs:356-371). The raw frame is saved to `peers_sessions` and re-read at start (presence.rs:55-78).
  - **Nothing reads `kept`.** `directory` lists only `l.sessions` (presence.rs:518-523), and `receipt` reads only `sessions`, for `tier` (ipc.rs:1628-1635).

### Frames (E: src/net/frames.rs)
- `Remote` has the fields `{session_id, agent, repo?, branch?, folder, status, doing?, tier, title?}` (frames.rs:30-41).
- `Presence` has the fields `{member, seq (unix ms), sessions, known, kept}` (frames.rs:123-130).
- `Frame` is `#[serde(tag = "t")]` with **no `deny_unknown_fields`** (frames.rs:95-96).

### How older peers would treat new fields
- **An extra optional field on `Remote` or `Presence` is safe.** serde ignores unknown fields by default, so old peers drop it silently. New peers must declare it `#[serde(default, skip_serializing_if = …)]` to read old frames. `Hello.addrs`, `Hello.proof`, `Hello.memory` and `Ticket.addrs`/`wake` all follow this pattern (frames.rs:101-111; ticket.rs:31-40). The 23 Oct spec states the same rule: "No protocol bump: … `#[serde(default)]`" (spec line 387).
- **A new value in an existing field is not safe.** An old peer validates every `Remote` (`valid`: `status` must be in `STATUSES`, `tier` in `Tier::ALL`; frames.rs:64-83), and **drops the whole presence frame if any one session fails** (frames.rs:302-317; net/mod.rs:1089-1091). So publishing ended sessions as `status: "ended"` inside `sessions` would blank that computer's whole presence for old peers. Publish them in a new optional list (for example `ended: Vec<Remote>`), or as extra fields on `kept`, instead.
- **A new frame variant is not safe either.** "this enum has no unknown-variant tolerance, so an old build would stop reading" (frames.rs:115-116). Gate it behind a `hello` capability bit, the way `memory` is gated (frames.rs:109-111; net/mod.rs:1121-1125).
- **Do not bump the protocol version.** `PROTOCOL = 1` must match exactly (net/mod.rs:44, 1112-1116), so a bump marks every old peer `Outdated`.
- **Size.** Each list is capped at 100 entries (frames.rs:311-313), and a frame at 1 MiB (frames.rs:16).

### Offline representation in `directory` (presence.rs:395-525)
- `stored()` keeps a member's latest presence while that member is online or was last seen within `DAY`. Older presences are dropped from memory and from the store (presence.rs:395-420).
- `seen()` refreshes `seen_at` when a connection closes (presence.rs:385-392).
- Each record has `online = d.peers.is_online(member)`, while the local computer is always `true` (presence.rs:423-444, 519).
- **The record has no `seen_at` or "offline since".** An offline member's sessions are listed with their last `status` (`busy`/`idle`) and `tier` unchanged, with only `online: false` to tell them apart.
- `receipt` adds `online`, plus `waking` when the member has a doorbell and is offline (ipc.rs:1624-1627).
