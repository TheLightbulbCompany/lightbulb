# lightbulb-network: the bridge (spec)

Status: draft for owner review, 2026-10-22. Part of the design in
`2026-10-22-lightbulb-network-p2p-design.md`; its "Where each thing lives" table binds this spec, and `2026-10-22-lightbulb-network-architecture.md` is binding over both where they differ.

Words used here. **Engine**: the public daemon, CLI, MCP server and hooks on a computer. **Bridge**: private
backend code that makes a Lightbulb workspace a member of a person's network. **Hosted caller**: anything that reaches
us over `api.thelightbulb.company/mcp` and has no computer of its own (Muse, ChatGPT, Claude apps, Grok Bot, a
script with an `lbk_` key). **Network**: the set of computers a person has linked, plus teammates they invited.

## Intent and why

Today the backend sits in the middle of every session message. It stores it, queues it, and pushes it down a
socket to a Mac in plaintext. That is the "hub". The new engine removes the hub: computers talk directly,
encrypted. But the workspace product still needs to reach the network. Friday must message a person's Claude Code
session. A person on their phone must tap Allow. Muse or ChatGPT must send to a session and read the reply.

The bridge does that, and nothing more. It is a **member computer that lives in the cloud**. It has its own
key, like any other computer. It joins a person's network the same way a laptop does. It reads what is addressed
to it. It reads nothing else. This is the same trust level as a teammate's laptop, not a server in the middle.

The bridge is also how the workspace product stays honest about the free one. If the bridge were deleted, every free
feature would still work. If the engine were deleted, the bridge would have nothing to join.

## Goals

1. Friday and the cloud agents can send to a linked person's sessions and receive replies, with the same labels,
   holds, receipts and loop limits a computer-to-computer send has.
2. Hosted callers use the hosted MCP with the **same tool names and arguments as the engine's MCP**. A skill
   written for one works on the other.
3. Hosted callers are pull-only. They send, and they read replies with a tool. Nothing is pushed into them.
4. A person can Allow, Decline or Always allow a held message from the web app and the phone. The answer is
   signed and reaches the holding computer.
5. Signing in with Lightbulb issues a proof that verifies the person's name on the network and links the person
   to their workspace. The engine checks the proof offline.
6. Webhook agents (Grok Bot and others) keep working: one webhook POST per admitted message, replies through
   the hosted MCP.
7. The bridge reads only what is addressed to it. This is enforced by the encryption, not by our promise.
8. The hub-era code is deleted in the same cutover release that lands the bridge. The list is in "Interfaces owned".
9. Using it needs a Lightbulb workspace; a free trial counts (every org gets one). The copy says "a Lightbulb workspace", never "paid".

## Non-goals

- Being in the path between two people's computers. Direct sends never touch the bridge.
- Running an engine for people with no workspace. The free path never needs us.
- Pushing into hosted callers. Muse and ChatGPT cannot be woken by us; they pull.
- Storing message history for people. The bridge keeps a short mailbox for pull callers and nothing else.
- Moving rooms, chat, tasks, Paperclip or billing into the network. Those stay workspace-only.
- Changing the engine's wire format. The bridge speaks it as a client.
- File attachments for hosted callers (later, if they need them).

## Anti-goals

- The bridge becoming a plaintext relay: a place where mail between two computers can be read or logged.
- A free user ever being told to buy a workspace to message their own sessions.
- A Lightbulb sign-in, or our servers being up, being required to join or send on the free path.
- A second set of rules for hosted senders. They get the same hold, loop limit, framing and defang.
- Any hub-era path kept "just in case". No dual paths, no compatibility shims.
- A secret, hostname, account id or customer datum landing in the public repo through the bridge's shared types.

## User flows

### 1. A Friday message to a person's session

1. The person asks Friday in `#general`: "tell my Claude session in `api-repo` to run the tests."
2. Friday calls the Hermes plugin tool. It is the same tool name and args as today's `lightbulb_local_sessions`
   in spirit, but now it targets the network: `find_sessions`, then `send_message(request_id, text, session_id)`.
   Friday is a session of the bridge's cloud member (`<bridge_key_short>/friday`), so two people's Fridays never
   collide and replies come back to that session key.
3. The tool calls the bridge (`/bridge/send`), which acts as the workspace's member computer. The bridge looks up the person's
   network, finds the session in the directory (the bridge is a member, so it sees the directory), and sends an
   encrypted message to the computer that owns the session. It is addressed to that computer. The bridge
   encrypts it; only that computer decrypts.
4. The receiving computer applies its own policy. Friday is the person's own linked cloud member, so if the
   person set `agents_send_without_asking` on, it goes in (labelled "Your Friday"; a teammate's computer shows
   "Friday via <Person>"). If not, it is **held**. The receipt returns `held`.
5. Friday is told the receipt state (`queued`, `stored`, `delivered`, `read`, `held`, `refused`, `expired`) and says so in the room.
   It asks once, never re-asks under a new request id, and treats `refused` as final for the run.
6. When the session replies it calls `send_message(..., reply_to=<requestId shown in the delivered frame>)`, addressed
   to Friday's session key on the bridge. The daemon sets the envelope's `thread` from `reply_to` (fallback: the most
   recent inbound message from that target session, else a new thread). The bridge decrypts it and posts it to the
   backend at `/internal/bridge/inbound`; the backend routes it by `thread` to the run, hosted mailbox or webhook that
   sent the original, so two Friday messages answered in turn each reach their own run.

Rule: the bridge is the sender of record for Friday. The person is shown as the accountable human, as today.

### 2. A person allowing it from the phone

1. The holding computer raises its normal OS dialog. If the person is linked and the bridge is reachable, the
   computer also sends a small **hold notice** to its person's bridge member only: who, which session, a request
   id, and a preview of the first 160 characters only when `config.toml: preview_to_workspace = true` (default
   false; the person opted in by linking, see "what the bridge may see"). It is addressed to the bridge, so the
   bridge may read it.
2. The bridge writes an Activity item and sends a phone push: "Friday wants to message your Claude session."
3. The person taps Allow, Decline or Always allow on the phone or web. The backend authenticates them with
   Clerk, checks that they own the holding computer's link, and calls `/bridge/decide` to build a `decision`
   frame: `{hold_id, holding_member, verdict, scope: once|person|own_cloud, nonce, decided_at}`.
4. The bridge **sends that decision to the holding computer as an encrypted network frame**, addressed to
   that computer. The computer accepts it only if all hold: the bridge's roster entry carries a `kind=bridge`
   proof whose `sub` is the hold target's `person_id`; the scope is `once`, `person` or `own_cloud` (`own_cloud`, the phone/web Always allow for the person's own cloud member, is accepted only when the held message's sender is that bridge's own session, and the holding computer applies it by setting `agents_send_without_asking = true`; `cloud_of` for other people's agents is never relayed); the nonce is fresh and `hold_id` +
   `holding_member` match. Then it applies it and signs the decision record with its own key, as if the person
   had answered the local dialog. Residual trust, stated: the held message may come from the bridge's own sessions (a held Friday message allowed from the phone is the
   main case), so a compromised backend can send as the person's own cloud agent and allow it once, or allow one
   held third-party message once; a phone-held person key that co-signs is Plan D.
5. First answer wins. If the local dialog was answered first, the bridge's late decision is dropped and the
   phone item resolves to "Answered on your Mac."

If the computer is asleep, the decision waits in the bridge's outbox and is delivered on wake (see failure modes). A hold lasts at most 7 days; the phone item has the same lifetime.

### 3. A Muse, ChatGPT or Claude-app user sending through the hosted MCP, receiving by pull

1. The person adds `https://api.thelightbulb.company/mcp` in their app. Clerk runs OAuth (or the client
   registers by DCR, or the person pastes an `lbk_` key into a script).
2. The app calls `find_sessions` and sees the sessions on their network.
3. The app calls `send_message(request_id=..., text=..., session_id=...)`. The caller is a session of the
   bridge's cloud member (`<bridge_key_short>/muse-via-aden`), accountable to the person. The label on the
   receiving side reads "<App> via <name>": the receiver renders it from the roster for a `bridge`-role member
   whose proof `sub` equals `<name>`'s `person_id`; `<App>` comes from a short allowlist (Claude, ChatGPT, Muse,
   Grok Bot, Cursor, Codex, Friday and the workspace's agent names), and anything else renders as `App
   "<sanitised, ≤32 chars>"`.
4. The app has no way to be pushed to. So replies land in the bridge's **mailbox** for that caller. Every tool
   result carries an `unread` note when mail waits (as the hosted MCP does today). `check_inbox(wait_secs ≤
   600)` returns it, on the hosted MCP with the engine's name and arguments (`read_messages` stays for room
   reading). `message_receipt` tells the app where its own message is.
5. The mailbox keeps a reply for 7 days or until read plus 24 hours, whichever comes first.

### 4. A webhook agent

1. The person registers a webhook agent in Settings: name, URL, bearer key. The URL and key are one secret in
   Secrets Manager. They are never returned or logged.
2. Another session sends to it with `send_message(session_id=...)`: a webhook agent is a session of the
   bridge's cloud member (`<bridge_key_short>/grok-bot`). `agent_id` is a hosted-room concept only, never an
   engine target. Hold and loop rules apply as to any session.
3. An admitted message becomes ONE POST to the URL, with retries as today. The webhook agent replies through
   the hosted MCP's `send_message`, authenticated with an `lbk_` key.
4. Only the bridge ever holds the URL and key. A computer on the network never learns them.

### 5. Linking an account to a network

1. On a computer, the person runs `lightbulb signin` (or the app's sign-in button). The engine opens a browser to
   Lightbulb sign-in; it creates no org, trial or provisioning. A computer with no browser uses the short-code device login (`device_login.py`): the CLI
   shows a code, the person approves it in a browser.
2. Lightbulb issues a **name proof**: a signed statement "this computer key belongs to Clerk user U, display
   name N, workspace W", with a 24 hour expiry. `person_id` is sticky: it is assigned at admission and a
   later proof only writes a signed `merge`; proof expiry changes the verified badge, never `person_id`. The engine verifies it offline against issuer keys compiled into the
   engine and shows the name as verified.
3. The same step tells the backend "computer key K belongs to U". The backend stores the link. Now the bridge
   knows which network is U's, and the network's computers know the bridge's key is allowed to act for U.
4. The person's other computers do the same. All of them show as one person.
5. Unlinking: `lightbulb unlink` or the app. The backend deletes the link row and the bridge's member is
   removed by the person's `unlink` or by an admin.

### 6. What a free user without a workspace sees

- Install, join, invite, send, receive, approve at the terminal or the OS dialog. All work. No Lightbulb sign-in
  is needed. Their name is shown as "unverified".
- If they sign in but have no workspace, they get a verified name and nothing else. The bridge is not involved.
- In the app, the Friday, chat and phone approval entries are present but say plainly: "Needs a Lightbulb
  workspace." (a trial counts). One line, one link to plans. The CLI and public docs never mention it.
- Nothing on the free path asks for a card.

## Threat model

Trust level of the bridge: a member computer. It holds one key. It can read what is addressed to its key and
nothing else. A network member can remove it.

### A hosted caller impersonating a session

- Attack: a hosted caller (or a stolen token) sends as "Alice's Claude session" so a teammate trusts it.
- Control: the hosted MCP never lets a hosted caller choose its sender label. The bridge derives it from the
  authenticated principal: OAuth client id plus the person, or the agent key id plus the person. The label is
  `<App> via <name>` (the receiver renders it from the roster; `<App>` is allowlisted, anything else renders as
  `App "<sanitised>"`), never a session name. A session's own identity comes from its computer's signature,
  which the bridge cannot produce. A hosted caller has no signing key for any session.
- Test: send via OAuth with a body claiming `from: session ...`. The receiver shows "<App> via <Person>" and
  the claim is neutralised.

### A revoked agent key

- Attack: a leaked or deleted `lbk_` key keeps working.
- Control: only the SHA-256 is stored. Revoke deletes the lookup row and the list row in one write. The verifier
  reads the lookup row on every call (no cache longer than 30 seconds). Keys also expire in 90 days by default.
  A person's removal from the workspace revokes all their keys.
- Test: use a key, revoke it, call again. The next call is refused within 30 seconds. A revoked key's queued
  mailbox items are deleted with it.

### A removed member's bridge access

- Attack: a person is removed from the network (or leaves the workspace) but the bridge still sends to them.
- Control: the engine's membership log is signed and held by every computer. The bridge is a member and follows
  the log. When the bridge's own key is removed, it cannot connect or be addressed. When a **person** is
  removed from the network, the bridge also drops their sessions from `find_sessions` and refuses to send.
  When a person leaves the workspace, the backend unlinks their computers and deletes their mailbox and
  hosted-caller rows. Mail already on a removed computer stays there (the design's rule).
- Test: remove the person. `find_sessions` stops listing them and `send_message` returns `refused`, both within
  one membership sync.

### The bridge as a relay for plaintext, and the control

- Attack: the bridge forwards mail between two computers, and so could read or log it.
- Control, by design: **the bridge is never a hop between two computers.** A computer addresses a message to the
  destination computer's key. The bridge's key is not a recipient, so it cannot decrypt. If two computers
  cannot reach each other, they use the engine's relay, which sees ciphertext only. The bridge is not that relay.
- The bridge sees plaintext in exactly these cases: (a) a message addressed to the bridge (Friday, a webhook
  agent, a hosted caller's mailbox); (b) a hold notice the computer chose to send for phone approval; (c) the
  directory (who, which session, repo, branch, busy or idle, title), because it is a member and every member sees
  the whole directory; (d) a stream the person explicitly granted it with a `share_grant` (browser and phone
  viewing of a shared terminal), the fourth and last thing it can read. It sees nothing else.
- The bridge logs ids, sizes, states and timings. It **never logs message bodies**, hold previews, keys, or
  webhook URLs. A test greps the log output of a full e2e run for the test strings.
- The backend has no endpoint that returns the bodies of other-people mail. There is no admin read path.

### Other items (my call, listed so nothing is silent)

- Stolen proof: a proof names one computer key and expires in 24 hours (refreshed in the background). A stolen
  proof without the key proves nothing.
- Forged decision from the phone: the engine accepts a decision only under the acceptance rules in flow 2 (bridge
  role and `sub` match, `once|person` scope only, fresh nonce, matching `hold_id` and
  `holding_member`), only once; first answer wins and a second gets `not_open`.
- Loop between Friday and a session: the network caps agent-to-agent chains at 4 hops and 12 messages per 600 s
  per principal (the sending session, for cloud members); room turns keep their own cap of 2.

## Failure modes

| Failure | What the person sees | What happens |
|---|---|---|
| Bridge down | Direct sends between computers still work. Friday, hosted MCP and phone Allow are unavailable. | Computers keep holds locally and the OS dialog still works. Hold notices retry for up to an hour, then stop. Nothing is lost. |
| Workspace runtime cold | Friday takes about a minute to answer the first time. | The bridge is backend code, not the runtime, so it does not depend on the microVM. Only the Friday turn waits. The bridge accepts and stores the inbound reply, then the room turn wakes with the 90 s ingress budget. |
| Proof expired | Name shows as "unverified" on the other side. Sends still work. | The engine refreshes in the background. If the sign-in server is down, the old proof is trusted until its expiry plus 7 days of grace for display only. After that: unverified, not blocked. |
| Two workspaces for one person | The link names one workspace. | A computer links to one person and one workspace at a time. Linking again to a different workspace replaces the old link after a confirm. The old workspace's bridge leaves the network. |
| Bridge outbox backlog (computer asleep) | Sender's receipt reads `queued` (waiting in the bridge's outbox); `stored` only once the computer has it | Encrypted messages wait up to 24 hours, then the receipt reads `expired`. The bridge cannot read them; they were addressed to the computer. |
| Hold notice to a bridge that is not linked | Nothing on the phone. The OS dialog still works. | Computers send hold notices only if the person has linked and turned on phone approval. |
| Duplicate bridge request (retry) | Nothing. | `request_id` is the dedupe key everywhere (an opaque string matching `[A-Za-z0-9._-]{1,128}`, chosen by the sender). |
| Key rotation of the bridge | None. | A new bridge key is added by the founder-or-admin step, then the old is removed. Procedure in the architecture doc. |
| Clock skew | Proofs look expired or not yet valid. | Proof checks allow 5 minutes of skew; the engine shows "check your clock" instead of "unverified". |

## Data

What the backend stores about the network. All of it lives in the existing messaging table under keys already
used by `agent_keys` and `webhook_agents`, except where noted. No new tables. No message bodies for
computer-to-computer mail, ever.

| Item | Contents | Retention |
|---|---|---|
| Computer link | computer public key, Clerk user id, workspace id, display name, linked-at | until unlink or leaving the workspace |
| Name proof record | the last proof issued per computer key (for audit only) | 30 days after expiry |
| Bridge membership | the bridge's public key per workspace and the network id it joined | until unlink |
| Bridge private key | in Secrets Manager under the owner's prefix, never in DynamoDB | until unlink, rotated on demand |
| Agent keys (`lbk_`) | SHA-256, person, workspace, name, expiry | 90 days default, or revoke |
| DCR / OAuth clients | handled by Clerk; we store only the client label per grant | with the grant |
| Webhook agents | name, kind, policy; URL and key in one secret | until removed or workspace wipe |
| Hosted-caller mailbox | one row per reply: caller id, sender label, body, state | 7 days, or read + 24 hours |
| Hold notice | request id, sender label, a 160-character preview (only when `preview_to_workspace`), state | until resolved, max 7 days |
| Phone and web decisions | request id, verdict, scope, who, when | 30 days (audit) |
| Directory cache | session list as of the last sync | in memory, rebuilt on connect |

Wipes: the owner wipe and the account wipe remove every row above (they already remove agent keys and webhook
agents; the link, proof, bridge and mailbox rows are added to the same sweep). A test pins this.

What the bridge may and may not see of message content (decided):
- It reads what is addressed to it: Friday and cloud-agent traffic, webhook-agent traffic, hosted-caller replies,
  and hold notices.
- It does not read mail between two of the person's (or two people's) computers.
- It sees the directory, because every member does, the bridge included. The directory holds titles and todo
  items from the agent's own transcript. A person who does not want the workspace to see session titles does
  not link; the link flow says so.
- It reads a stream only by an explicit `share_grant` from the person.
- Hold previews are opt-in at link time with one plain line: "The phone can show who wants to message you and the
  first 160 characters." Off gives "Needs your Mac" on the phone with no preview.

## Interfaces owned

### Backend routes (all Clerk-authenticated unless said)

New, under `/api/v1/network/`:
- `POST /link/start`, `POST /link/finish` : issue a name proof, store the link. Reuses the device-login code flow
  for computers with no browser.
- `POST /proof/refresh` : the computer sends its key and its current proof, and gets a fresh one. Needs only the
  computer's signature, not a Clerk session, so a long-idle computer can refresh.
- `DELETE /link/{computerKey}` : unlink.
- `GET /bridge` : this workspace's bridge public key and network id (so the person can verify it).
- `POST /holds/{requestId}/decision` : Allow, Decline, Always allow from phone or web. Body: `{verdict, scope}`.
- `GET /holds` : pending holds for the signed-in person (for the Activity list).
- Internal HTTP, backend to bridge: `/bridge/send`, `/bridge/decide`, `/bridge/directory`.
- Internal (hook-token authenticated, from the bridge): `POST /internal/bridge/inbound` for decrypted traffic
  addressed to the bridge's sessions (Friday replies, webhook agent replies, hosted-caller mail), correlated by
  `thread`; the backend routes it to a room turn, a hosted mailbox (7 days, or 24 h after read) or a webhook.

Kept as is: `/mcp` (hosted MCP), agent key routes, webhook agent routes, device login routes.

### The bridge's engine-side identity

- One key per workspace-and-person link, generated in the backend, stored in Secrets Manager.
- It joins the person's network through the engine's normal invite path. Same-person admission applies: the
  bridge relays the request to one of the person's computers for a local confirmation and never admits alone.
- Its role is `bridge`, with these exact powers and no others: relay hold decisions and same-person join
  requests for its linked `person_id`. No invite, no remove, no promote. It is removed by the person's `unlink`
  or by an admin.
- Its display name is "Lightbulb (<Workspace>)". Its proof comes from the same issuer as a person's, with
  `kind=bridge`.
- **Where it runs:** `apps/bridge/` in the private monorepo, a Rust service with the engine as a crate (pinned
  by release tag) and one member identity per (person, workspace) link. One ECS task in v1 (shard by person
  hash when needed). It dials out through the relays, so it needs no inbound port; the backend reaches it over
  internal HTTP.

### Hosted MCP tools (same names and arguments as the engine's)

`find_sessions(query, limit=20)`, `send_message(request_id, text, session_id, reply_to?, files?, notify?)`, `message_receipt(request_id)` and `check_inbox(wait_secs ≤ 600)` are the engine's names and arguments. `read_messages`, `find_rooms`, `find_tasks`, `read_task`, `update_task` and `use_browser` stay hosted-only,
because they are workspace-shaped. Changes:
- `find_sessions` and `send_message(session_id=...)` now run through the bridge's network membership instead of
  the Mac socket. The arguments do not change.
- `send_message` keeps refusing `*`, `person/*` and repo-wide targets, and refuses `files`/`notify` with
  `unsupported`. Its network target is `session_id` only (every cloud agent, hosted caller and webhook agent is
  a session of the bridge's cloud member); the hosted MCP additionally accepts `room_id | person_id | agent_id`
  for workspace rooms, where `agent_id` is a hosted-room concept.
- `check_inbox` is added to the hosted MCP and is how a caller reads its mailbox. `read_messages` keeps reading
  rooms.
- The `unread` note on every tool result stays; it now counts mailbox items.

### Hermes plugin

`lightbulb_local_sessions` is replaced by the same tool pointing at the bridge. Its four actions stay
(`resources`, `read`, `send`, `receipt`) and its ask-once rules stay. The `localSessionToken` grant is replaced
by the existing hook token plus the turn's requester id.

### What is deleted

All of it lands in the one cutover release (architecture §11; no dual paths):
- Desktop: `transport.rs` goes entirely (not just its message-path half), with `claude.rs`, `codex.rs`,
  `hooked.rs`, `enrollment.rs`, `lifecycle.rs`, `native_mcp.rs`, `native_tools.rs`, `plugin.rs`, `approval.rs`,
  `held_notice.rs`, `presence.rs`, `remote_input.rs` and `snapshot.rs`. The engine comes back as the bundled
  binary.
- Backend: `core/services/local_sessions.py`, `local_session_relay.py`, `local_session_grants.py`,
  `core/repositories/local_sessions_repo.py`, the `/local-sessions/device/*` routes in `routers/messaging.py`,
  `routers/websocket_chat.py`'s local-session relay path, the `lightbulb-local-sessions` client id in the ws
  authorizer and `ws-message` Lambda path, and `native_session_tools.py`; plus their tests and the conftest
  stubs. The `defang` helper and the sender-label rule live in the engine and reach the bridge as a library
  call.
- Live terminals move onto engine `stream`s (the app owns the feature; the engine carries the bytes; the bridge
  reads a stream only by a share grant). Teammate typing, teammate-requested resume and session pick-up become
  envelope kinds `input`, `resume`, `pickup` in Plan D unless Plan C has room; resume is person-triggered only.

Stays (and what changes):
- `hosted_mcp.py`: tool bodies rewired to the bridge. Auth, scopes, DCR and the `unread` note stay.
- `agent_keys.py`: unchanged. The verifier gains the 30-second cache bound. No "trusted" agent keys.
- `webhook_agents.py`: unchanged except it sends and receives through the bridge instead of the session relay.
  Its own copy of `defang` is removed in favour of the engine's.
- `device_login.py`: stays; it becomes the proof flow and is also the link flow for browserless computers. It
  stops enrolling a hub socket and instead returns the name proof and the bridge's key.
- The holder, `screen.rs` (frames onto `stream`), `resume.rs` (person-triggered only), `notify.rs` and the mod
  socket.

## Acceptance checklist

Run on two real Macs with two accounts, A (Lightbulb workspace) and B (free, no workspace). Bridge in dev.

1. A links Mac 1 with `lightbulb signin`. The CLI ends "Connected as A (verified)". B's Mac shows A verified,
   offline, with the issuer's keys compiled in (turn off the network to the sign-in server and re-check).
2. A asks Friday to message the Claude session on Mac 1. With `agents_send_without_asking` off: held, receipt
   `held`, notification on Mac 1, phone push within 10 seconds.
3. A taps Allow on the phone. Within 10 seconds the message appears in the session labelled "Your Friday" ("Friday via A" on a teammate's Mac), the
   Mac notification is withdrawn, and the phone item reads "Answered".
4. A taps Decline on the web on a second message. Receipt `refused`; nothing delivered.
5. Always allow on the phone for Friday sends a `decision` with scope `own_cloud`; the holding computer sets `agents_send_without_asking = true`, and the next Friday message goes straight in. Send two Friday messages to one session, then answer them in turn with `send_message(reply_to=<that message's requestId>)`: each reply reaches its own run, not the other.
6. Answering the Mac dialog first, then tapping the stale phone item: the late decision is dropped.
7. Muse (or Claude app) connected by OAuth sends to Mac 1's Codex session; label "Muse via A". The reply is read
   by `check_inbox` and `unread` appears on the next tool result. Receipts move `queued`/`stored`, `delivered`, `read`.
8. An `lbk_` script key sends, is revoked, and the next call fails within 30 seconds. Its mailbox is gone.
9. A webhook agent receives one POST per admitted message. It replies through `send_message` with its key. The
   URL and key appear nowhere in logs or in any API response.
10. Forged sender: an OAuth call whose text claims to be a session shows "<App> via A" at the receiver.
11. B (free, no workspace) sends to A's session from the CLI with no sign-in. Works. B never sees "needs a
    workspace" on the CLI. In the app the Friday entry says "Needs a Lightbulb workspace".
12. Bridge down: stop the bridge task. Mac-to-Mac sends still work. Friday fails with a clear message. The OS
    dialog still approves holds. Restart; queued hold notices are delivered.
13. Mac 1 asleep: a Friday send waits, receipt `queued`; after wake, `stored` then `delivered`.
14. Remove A's Mac 1 from the network, then run `find_sessions` through the hosted MCP: Mac 1's sessions are gone
    and `send_message` returns `refused`.
15. Plaintext check: run a full run of items 2 to 9 with unique marker strings. Grep backend logs, DynamoDB rows
    and the relay logs. Markers appear only in the mailbox rows and the Friday room (as designed). They do not
    appear for any Mac-to-Mac send.
16. Wipe A's workspace. Every row in "Data" is gone, including the bridge secret.
17. After the cutover PRs: grep the monorepo for `local_session_relay`, `local_session_grants`,
    `lightbulb-local-sessions` and `local_sessions.py`. Zero hits. Backend and desktop CI pass.

## Rulings and open questions

Rulings (architecture 2026-10-22), folded in from the earlier open questions:

1. **Sharing, browser, resume and pickup.** `transport.rs` is deleted entirely at the cutover. Live terminals move onto engine `stream`s; resume stays person-triggered; teammate typing, teammate-requested resume and pick-up become envelope kinds in Plan D unless Plan C has room. There is no slimmer app-to-backend channel to build.
2. **Does the bridge read the directory?** Yes: every member sees the whole directory, the bridge included, and the link flow says so. The "own computers only" default and the "visible to my workspace" marking are dropped.

Still with the owner (sent 2026-10-22): **hold previews on the phone**, the opt-in 160-character preview (`preview_to_workspace`, default false) versus "Needs your Mac" only.

Decided by me (change them if you disagree): one bridge key per link; mailbox 7 days or read plus 24 hours; holds 7 days; proofs last 24 hours with 7 days of display grace; hosted callers cannot attach files; one workspace per computer link and one network per computer; the bridge is a Rust service in `apps/bridge/`, one ECS task; the `bridge` role can relay decisions and same-person join requests and nothing else.
