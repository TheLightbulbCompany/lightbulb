# lightbulb-network engine: product and engineering spec

Status: draft for owner review, 2026-10-22. Part 1 of 4 (engine; bridge, app and hosted MCP have their own specs).
Binding over this spec where they differ: `2026-10-22-lightbulb-network-architecture.md`. Binding inputs: the approved design (`2026-10-22-lightbulb-network-p2p-design.md`, esp. "Where each thing lives"),
Plan A, and the operating charter. Where this spec makes a call the design left open, it says **Call:**.

## Intent and why

A person runs several coding agents (Claude Code, Codex, Cursor, Grok CLI). Today they cannot talk to each other,
or to a teammate's agents, without a Lightbulb server reading every message. The engine is the free, open-source
program on each computer that fixes this: it finds the agents, lets them message each other, and connects computers
directly and encrypted. It is one binary: a daemon, a CLI, an MCP server and the agent hooks.

The engine is the public face of Lightbulb. It must be good with no account, no card, no app and no workspace.
It knows nothing about workspaces, plans, rooms or Paperclip. The app and the bridge depend on it. It
depends on neither.

## Goals

1. One line installs it. Another line joins a network. Two minutes from nothing to "my Codex messaged my Claude".
2. Computers talk directly. Everything on the wire is end-to-end encrypted. A relay forwards ciphertext only.
3. Membership is held and checked by every computer (signed log). No server decides who is in. A join needs a person's Allow (an admin's, or the inviter's) unless the link was minted `--open`.
   One network per computer.
4. Any agent with MCP or hooks can send, receive and look up sessions. Senders are told how fast the message will be seen.
5. Nothing is lost silently. Every message ends in a receipt, one of seven forward-only states: `queued`, `stored`, `delivered`, `read`, `held`, `refused`, `expired`.
6. A stranger's message never reaches an agent without the person's OK on their own computer.
7. Parity with the 13-item list in the design, then the improvements named there.
8. Survives sleep, crash, restart and an agent's own upgrade without the person doing anything.
9. Works with no Lightbulb server reachable. Our servers (relay, sign-in) are conveniences, never requirements.

## Non-goals

- Cloud agents, chat, rooms, tasks, billing, Friday. (Bridge.)
- A UI. The engine has CLI output and OS dialogs only. (App.)
- Shared terminals, session browser, resume. (App.)
- Windows and Linux in v1. Nothing in state, wire or crypto is macOS-only; Linux is a port.
- Hiding who talks to whom from a relay. Hiding message size and timing.
- Protecting against malware running as the same user (it can read the key file).
- Group chat history, search, or message editing. Mail is point to point and then gone.
- Anything a hosted assistant needs. They enter through the bridge.

## Anti-goals

- A step on the free path that asks for a card, plan, workspace or Lightbulb account.
- The CLI, docs or help text mentioning Friday, CEO, workspaces or plans.
- A message body, key, token or teammate's file path in a log, crash report or telemetry. (Telemetry: none.)
- A secret, internal hostname, account id or customer datum in the repo.
- A teammate's agent acting in my session without my OK on my computer.
- Two code paths for the same job (daemon vs app, CLI vs MCP). Every client talks to the daemon the same way.
- Silent drops. If we cannot deliver, the sender is told.

## User flows

Binary: the file `lightbulb-network`, at the one canonical path `~/.lightbulb/network/bin/lightbulb-network`. The command people type is `lightbulb`, a symlink `~/.local/bin/lightbulb` to that path; the examples below keep the long name. Subcommands the engine does not own (`claude`, `codex`, `share`, `continue`, the app's session launchers) are forwarded to the installed app's binary; without the app they print "needs the Lightbulb app: thelightbulb.company/download".
Names in angle brackets are filled in. Text in quotes is exactly what is printed.

### 1. Install

1. Person runs: `curl -fsSL https://thelightbulb.company/install | sh`
2. The script downloads the signed macOS archive for the person's CPU, checks its signature and hash, puts the binary
   at the canonical path, links `~/.local/bin/lightbulb` to it (adding that folder to the shell path file if needed), and runs `lightbulb-network setup`.
3. `setup` finds installed agents and wires each (hooks, MCP entry, permission allow-list), backing up every file it
   edits once as `<file>.lightbulb-backup`. It installs and starts the launchd job `company.thelightbulb.network`.
4. Person sees:
   ```
   Lightbulb Network 0.1.0 installed.
   Wired:  Claude Code, Codex, Cursor, Grok CLI      (not found: none)
   Backups: 6 files saved as *.lightbulb-backup
   Running in the background. Restart open agent sessions to load the tools.
   Next:   lightbulb-network init --name <your-name>     create a network
           lightbulb-network join '<link>'                join a friend's
   ```
5. With no agent installed it says `Wired: nothing yet. Install an agent, then run: lightbulb-network setup`.
6. The installer prints nothing about accounts and never opens a browser.

### 2. Join (create, or join a friend's)

Create, first person:
1. `lightbulb-network init --name prasiddha`
2. Creates this computer's key if none, writes the team log (founder entry, self-signed), starts connecting.
3. Prints:
   ```
   Network "prasiddha" created. You are the founder.
   This computer: prasiddha-mbp   (name not verified; run `lightbulb-network signin` to verify it)
   Invite line (paste to a friend's coding agent, or send as text):
     Join the Lightbulb network: https://thelightbulb.company/j/#<ticket>
   Show it again any time: lightbulb-network invite
   The link works for 7 days; `invite --new` renews it.
   ```
4. Team name defaults to the person name. `--team <name>` overrides.

Join, friend:
1. Friend pastes the invite line to their agent. The agent runs `lightbulb-network join '<link>' --name <name>`
   (the static `/j/` page shows the same command and the install line, and knows nothing of the team; the ticket is in the URL fragment, so it never reaches our servers). Or the friend runs it in a terminal.
2. The engine dials the inviting computer by key (any online member in the ticket works), proves it holds the link
   secret, and sends its public key and name.
3. The inviting computer or an online admin raises a hold-shaped prompt ("aden wants to join your network") and signs an admission only on Allow; a link minted with `invite --open` skips the prompt. `join` prints `Waiting for an admin to allow this...` meanwhile. The new computer receives the full team log and checks every
   signature itself.
4. Prints: `Connected as aden (aden-mbp). Team "prasiddha": 2 computers online.`
5. If the name is taken: `The name "aden" is already used in this network. Try: lightbulb-network join '<link>' --name aden-2`
6. If no member is reachable: `No one from this network is online right now. Ask them to open their laptop, then run the same command again. Your key is ready; nothing was lost.`
7. A second computer of the same person: `lightbulb-network signin` first on both, then `join`. The computer is admitted by one of the person's own computers after a local confirmation ("Your new Mac wants to join. Allow?"). **Ruling (architecture 2026-10-22):** names are roster-unique, so the second computer is `aden-2`; it is labelled "aden's" only when both carry a proof for the same account. An unverified second computer cannot share a name or a `person_id`.
8. `person_id` is sticky: assigned at admission (the Clerk id when the computer joined with a proof, else its own key); a later proof writes a signed `merge` entry; proof expiry changes only the verified badge.

### 3. Invite

1. Any member: `lightbulb-network invite` prints the invite line (the current link secret), which expires 7 days after it was minted.
2. One-time form: `lightbulb-network invite --once` prints a link valid for one join and 24 hours. `invite --open` mints a link whose joins need no Allow (the CLI says: "Anyone with this link joins without asking. Use it only with people you trust.").
3. Prints for `--once`: `One-time invite, expires <time>: Join the Lightbulb network: https://thelightbulb.company/j/#<ticket>`
4. Replace the reusable link: `lightbulb-network invite --new` prints `Old link stopped working. New link: ...`. The link is also replaced on every removal.
5. Make an admin (founder only): `lightbulb-network admin aden`, prints `aden can now remove members.`
6. Remove (founder or admin): `lightbulb-network remove aden-mbp`, prints
   `Removed aden-mbp. New connections and new mail from it are blocked. The invite link was replaced: <new line>`
7. A non-admin runs `remove`: `Only the founder and admins can remove computers. Ask prasiddha.`

### 4. First message (own ring, then a teammate)

1. Person's Claude Code session (installed and restarted after setup) calls the MCP tool `find_sessions`.
2. The agent sees one line per session: `k3f9a/codex:01a1  Your Codex  repo api  busy 2m  "fix token refresh"  tier push`.
3. Agent calls `send_message(request_id: "<ulid>", text: "...", session_id: "k3f9a/codex:01a1")`.
4. Tool result: `stored on this computer. It is reading now (push). Check with message_receipt("<ulid>").`
   For a Cursor/Grok target: `stored. k3f9a/cursor:77f is idle and next-step-only: it sees this at its next step.`
5. The receiving agent sees, as a user-role message:
   `[Lightbulb teammate contribution from Your Claude Code {"requestId":"<ulid>"}]` then the text.
6. Teammate case: `send_message(session_id: "b81c2/codex:9c2", ...)`. Result: `stored on aden-mbp. It may be held for aden to approve; you will see the receipt change.`
7. From a terminal: `lightbulb-network send aden/codex-api "Did the token format change?"` prints
   `01JB... stored on aden-mbp (tier push). Receipt: lightbulb-network receipt 01JB...`
8. Terminal sends are from `<you>/terminal`. An agent's shell that runs `send` is sent as that agent's session.

### 5. Approval

1. A teammate's first message to a session: receiver's receipt becomes `held`. The sender's `message_receipt` shows
   `held: waiting for prasiddha to approve`.
2. On the receiving computer the daemon shows a macOS dialog (osascript; the app may replace it with its own):
   ```
   Lightbulb Network
   aden's Codex wants to message your Claude Code in api.
   "Did the token format change? My tests started failing."
   [ Decline ]  [ Allow ]  [ Always allow ]
   ```
   Allow: deliver this message and write `people[aden] = accept`, so aden's other computer sessions are delivered from now on. Always allow: on a hold from aden's *cloud* member (their Friday, their Muse) it writes `cloud_of[aden] = accept`. Decline: refuse, receipt `refused` (reason `declined`). A hold lasts at most 7 days, then `expired`.
3. Terminal fallback, no dialog available (SSH, no desktop): `lightbulb-network held` lists `H1 aden's Codex -> api-claude "Did the token..."`;
   `lightbulb-network approve H1` or `lightbulb-network approve H1 --always`; `lightbulb-network decline H1`.
4. The answer is accepted from the local socket on the receiving computer, or from a `decision` frame sent by the person's own `bridge`-role member (web and phone Allow), under the acceptance rules in the Wire section; either way the receiving computer applies it and signs the record with its own key. First answer wins; a second gets `not_open`. Nobody else on the network can answer.
5. After Allow the message is delivered labelled `aden's Codex`, the notification is withdrawn, and the receipt reads `delivered`.
6. Undo: `lightbulb-network policy aden hold` (or `refuse`); prints `Messages from aden will be held.`
7. Admission works the same way: `lightbulb-network approve J1` / `decline J1` answers a join request.

### 6. Offline

1. Recipient computer asleep or offline. Sender's CLI prints `queued: aden-mbp is offline. Will retry until <time, +24h>.`
   Receipt `stored` appears only after the recipient computer stores it. Before that the receipt is `queued` (it lives in the sender's outbox; one of the seven states).
2. On wake the sender's daemon reconnects, resends from the outbox, receipt becomes `stored`, later `delivered`.
3. Recipient session closed: `stored on aden-mbp. aden's api-codex is not running; it gets this when it is back (up to 24 hours).`
4. After 24 hours without that session coming back: receipt `expired`, reason `session_gone`. After 24 hours
   of the recipient computer never being reachable: `expired`, reason `unreachable`. A message held for the person's yes expires after 7 days.
5. The person's own `listen` mailbox never expires.

### 7. Leave

1. `lightbulb-network leave` leaves the team (not the install) and asks: `Leave "prasiddha"? Your computer stops connecting. Mail already stored here stays. [y/N]`
2. On y: signs a departure entry, tells online members, deletes the team link secret and the team's roster cache
   (keeps the key), prints `Left "prasiddha". Background service stopped.`
3. `lightbulb-network leave --everything` also removes the launchd job, wired agent entries (`setup --remove`) and state; keeps backups. `lightbulb-network unlink` removes the person's workspace link (the bridge member).
4. The founder leaving: `You are the founder. Name a new founder first: lightbulb-network admin <name> --founder`.

### 8. Upgrade

1. Daemon checks for a signed release every 6 hours (`auto_update = true`; `lightbulb-network update` does it now).
2. It downloads, verifies the signature and hash, swaps the binary, and restarts through launchd between turns
   (waits until no push is mid-flight; at most 60 s).
3. Person sees nothing unless they ask: `lightbulb-network status` prints `0.1.1 (updated 2026-10-23 04:10). Previous: 0.1.0.`
4. `lightbulb-network update --pin 0.1.0` installs a given tag and sets `auto_update = false`. This is the rollback.
5. State is forward-compatible: `state.db` carries a version; a new binary migrates it; an older binary on a newer
   db refuses to start with `State is from a newer version (v3). Install 0.1.1 or later.` and does not touch the db.
6. The wire carries a protocol version. Two computers on adjacent versions talk; one that is too old is told
   `aden-mbp needs an update (protocol 2, yours 3)` and its mail waits in the outbox.

## Threat model

Assumed attacker capabilities listed per row. Out of scope everywhere: root or same-user malware on a person's computer.

| Threat | Attack | Control | Does NOT cover |
|---|---|---|---|
| Forged frames | A non-member or member sends an envelope claiming to be someone else; or a body contains `[Lightbulb ...]` or `[Sent from Lightbulb by you]` to look like a frame or the owner. | Transport authenticates the peer key and the gate drops non-members. Every envelope is signed by the sending computer; `from` person/computer must match that key's membership entry. The label ("Your X", "<Person>'s X") is built only from membership, never from message text. Body lines starting `[Lightbulb` or `[Sent from Lightbulb` get a zero-width space after `[`; lookalike brackets and Unicode direction overrides are stripped. | A member truthfully sending persuasive text (that is a malicious teammate, below). Lookalike glyphs not yet in the defang table. |
| Wrong-caller hooks | Cursor and Copilot load Claude Code's hooks from `~/.claude/settings.json`; a person or script runs `hook grok ...` from inside another agent's shell, registering a fake session or draining another's mail. | `caller::identify` walks process ancestors and acts only when the nearest agent ancestor is the named agent. A hook that is not under its agent registers nothing, prints nothing, exit 0. The socket checks peer uid equals the daemon's uid. An MCP call's source is derived from the OS peer and the agent's own metadata (Codex thread id); the model never names its own source. Ambiguous or unidentified source is refused. | Same-user software that runs a real agent binary and is therefore that agent. Agents that do not expose a thread id on a shared process (refused as ambiguous, not served). |
| Removed members | A removed member keeps trying: old link secret, old connections, or an offline admin admitting someone before hearing of the removal. | Gate checks active membership on every connection and every envelope, not just at connect. Removal is a signed entry from the founder or an admin; it replaces the link secret; the removed computer is sent the entry that says so and nothing else. Roster is a hash-chained log: an entry is valid only if its signer was active at its parent; on a fork the branch with the removal wins and later admissions by the removed signer are dropped with the computers only they admitted. | Mail already delivered to a removed computer, and anything it copied. A removed member's local files. A short window while computers have not yet heard of the removal (seconds when online; until reconnect when offline). |
| Replay | A captured frame or a duplicate envelope is delivered twice; a request id is reused with different text. | Transport is a fresh authenticated session per connection; a captured byte stream cannot be replayed into another. Envelope ids (ULID) are remembered 7 days per sender and a second copy is acked but not delivered again. `request_id` reused with a different recipient or text is `refused`, reason `request_id_reused`. Sender outbox gives up at 24 h, so a legitimate resend is never older than the remembered window. | A malicious member who resends the same text as a new message (that is a loop/abuse case). At-least-once delivery after a crash can show an agent one message twice; the id is in the frame. |
| Loops | Two agents answer each other forever; one sender floods one session. | Per (sender principal, recipient session): 12 messages in any 600 s, the 13th is `refused`, reason `loop`; the principal is the sending session for cloud members and the `person_id` otherwise. Per chain: the envelope carries `hops`; an agent send made in reaction to delivered mail has hops+1; at 4 without a human message it is refused with a muted line to the sender (`paused after 4 agent replies; a person must speak`). A human or terminal send resets hops to 0. The receiver counts hops itself, so a sender that lies about `hops` is still capped by the 12/600 s rule. (Room turns in the workspace keep their own cap of 2.) | Many senders each under the limit, to one recipient (inbound policy `hold` covers this). |
| Malicious teammate | A real member sends prompt-injection text, a huge payload, files, or tries to make my agent run commands. | Peer text is framed as peer input, with a fixed line telling the agent it is not the user. First contact is held until the person approves (default for teammates). Joining needs an Allow too, and a link expires after 7 days. Per-person policy `hold`/`refuse`. Group sends (`*`, `person/*`, `repo:`) are refused from agents and from teammates' computers. Files: text only, small, saved inert in a private folder, never opened, never executable. Body max 64 KB. Rate limits above. Folders marked `off` never register. | A teammate you have Allowed who is persuasive. An agent that does what peer text says despite framing. Text files that contain harmful instructions. |
| Compromised relay | The relay is hacked, subpoenaed or malicious. | Relay sees ciphertext and which key talks to which (and sizes/timing). It cannot read, forge or alter (keys are end to end, endpoints authenticate each other). It cannot add a member (membership is signed by members). The relay list is signed and accepted only whole. A team can run its own relay, then contacts nothing of ours. A dropped message is caught: no ack, the outbox retries; receipts come only from the receiver. | Metadata, traffic analysis, delay and denial of service. Direct-path leakage of IP addresses to peers. |

Added controls that cut across rows: keys never leave the computer; secrets never appear in logs; every IPC and
MCP argument is length-limited; the socket is 0600 inside a 0700 folder; setup writes only entries it can later remove.

## Failure modes

Rule for all rows: **never lose** (a) a message that was accepted by `send`, (b) the person's key and team log,
(c) a held message that the person has not decided on, (d) a receipt state once reached (states only move forward).

| Failure | What the person sees | How it recovers | What must never be lost |
|---|---|---|---|
| Daemon crash or kill | Next `who`/tool call starts it again (`ensure_running`); launchd also restarts it. Agent tool calls pause up to 5 s. | SQLite WAL, one transaction per state change. On start: reload outbox, mailbox, holds; re-register sessions from agents' own files; wait 2 minutes before expiring anything (live sessions need time to re-register). | Outbox, mailbox, holds, receipts, team log. A message mid-delivery is retried: at-least-once, may repeat, never vanishes. |
| Mac sleep / lid closed | Teammates see this computer offline with last-seen time; sender shows `queued: <name> is offline`. | On wake: reconnect (relay first, then direct), exchange roster heads and presence, outbox flushes, mailbox expires only after a 2-minute grace. Elapsed sleep time counts toward the 24 h clocks (wall time), so mail may expire on wake; the sender is told `expired`. | Outbox on the sleeper; mailbox on the receiver. |
| Relay down | `lightbulb-network status` shows `relay: unreachable; direct: 1 of 2`. Same-network and hole-punched peers keep working. | Try each relay in the list, then the team's own, retry with backoff capped at 60 s. Peers reachable only by relay show offline; their mail waits in the outbox. | Outbox. Nothing is sent in plaintext as a fallback. |
| An agent changes its hook format | `doctor` prints `cursor: hook output changed (unknown field "..."). Messages to Cursor sessions will wait.` Pull note and `check_inbox` still work. | Hooks parse tolerantly (unknown fields ignored). A hook that cannot parse prints nothing and exits 0 so the agent is never broken by us. Receipt stays `stored`, never `delivered`, while a message cannot be shown. Fixture tests per agent version run in CI; breakage ships as a patch release. | Mail stays in the mailbox until a hook can show it or it expires. Never mark `delivered` unless the agent's turn received it. |
| Two daemons | Second start prints `Another daemon is running (pid 4121).` and exits 0. Same key copied to a second computer: `This identity is already online from another computer. Run: lightbulb-network reset-identity` | `flock` on `daemon.lock` gives one daemon per state folder; stale socket (owner dead) is replaced. For a duplicate key, the older connection wins until the person resets identity (new key, new name entry, old key removed by an admin). | The first daemon's state is untouched by the second. |
| Clock skew | `doctor` warns when a peer's clock differs by over 5 minutes. Proofs may show unverified. | Membership order uses hash links, not time. Hold, outbox and dedupe expiries use only the local clock, from a durable first-seen timestamp on that computer. Invites expire on the inviter's clock. Proof expiry affects only the verified mark. Entries stamped over a day ahead are refused. | Membership and receipts are unaffected by skew. |
| Disk full or `state.db` locked/corrupt | `send` fails with `Could not save. Nothing was sent.` Corruption: `State is damaged. Backup at state.db.bad-<time>. Run: lightbulb-network doctor --repair` | Sending refuses before sending, so no message is accepted without being stored. On corruption the daemon moves the file aside, keeps `identity.key`, rebuilds roster from peers, and loses only local mail. | The key file is separate from the db and is written atomically (temp file + rename). |
| Setup file edited by the person or another tool | `doctor` shows `claude: lightbulb hook missing`. | `setup` is idempotent; it re-adds only our entries. | Their other settings, untouched. |

## Data

Folder `~/.lightbulb/network/` (mode 0700). Files 0600. Nothing here is synced to any server.

| Path | Holds | Lifetime |
|---|---|---|
| `identity.key` | This computer's ed25519 private key. Moved into the OS keychain when one exists; the file then holds only a pointer. | Until `reset-identity` or `leave --everything`. Never copied, never logged. |
| `identity.pub`, `proof.jws` | Public key; optional sign-in proof (compact signed token: account, name, expiry; under 4 KB). | Proof until it expires or is replaced; never required. |
| `config.toml` | `person`, `computer`, `issuer` (sign-in issuer URL, default `https://thelightbulb.company/network`), `preview_to_workspace` (default false), `share_paths`, `lan`, `tray`, `auto_update`, `relay`, `folders` (folders marked off). Inbound policy lives in the `policy` table, not here. | Until edited. |
| `app-install.json` | Present when the app manages the engine: self-update is then off and the app ships and updates the binary. | While the app manages it. |
| `sock` | Unix socket for the daemon, 0600. | While the daemon runs. |
| `daemon.lock` | flock + pid. | While the daemon runs. |
| `daemon.log` | Start/stop, connection up/down, counts, errors. Never bodies, never keys, never paths outside the state folder (a teammate's or yours), never names of non-members. Rotates at 5 MB, keeps 3. | Rotated. |
| `relays.json` | Last good signed relay list. | Replaced on update; kept after expiry until a newer one arrives. |
| `state.db` (SQLite, WAL) | Tables below. `PRAGMA user_version` is the schema version. | Per table below. |
| `inbox/<local-id>/` | Files attached to received messages, 0700 folder, 0600 files, never executable, macOS quarantine flag set. | Deleted 24 h after the message is read, or 7 days after arrival if unread, or on refusal/expiry. Cap 50 MB total, 10 MB per person, 10 MB per hour per person. |
| `*.lightbulb-backup` | Not here: beside each agent file that `setup` edited (`~/.claude/settings.json.lightbulb-backup` etc.). Written once, never overwritten. | Until the person deletes them. |

`state.db` tables:

| Table | Holds | Lifetime |
|---|---|---|
| `team` | Team id, name, relay, link secret (hash), invite records (hash, expiry, used-by). | Until leave. |
| `roster` | Hash-chained signed log: founder, admissions, roles (`member`/`admin`/`founder`, kind `computer`/`cloud`; the `bridge` role is a cloud member holding a `kind=bridge` proof), removals, departures. Per computer: key, person, computer name, current state. | Until leave. |
| `attestations` | Sign-in proofs for members; each checked locally and dropped if invalid. | While the member is active. |
| `sessions` | `key` (`<member_key_short>/<provider>:<conversation>`), provider, name, pid, birth time, cwd (local only), title, doing, status, tier, last seen. | Live: while running. Offline: name kept 24 h after last seen, then forgotten (not in the first 2 minutes after daemon start). |
| `peers_sessions` | Last presence from each online computer (agent, repo, branch, status, doing, tier). Folder is the last path part unless `share_paths`. | Replaced on each presence; dropped when the peer is offline for 24 h. |
| `messages` | id, request_id, from, to, kind, body (max 64 KB), thread, hops, origin, sent_at, direction (schema version 1 from Plan A; later plans only add tables). | Until 24 h after `read` (or `refused`/`expired`); the person's `listen` mailbox keeps unread mail until read. |
| `receipts` | request_id, state, reason, where, updated_at. States only move forward. | 7 days after reaching a final state. |
| `mailbox` | Messages waiting for a session, with `expires_at` = arrival + 24 h. | Until delivered or expired. Never expires for `cli:<name>` mailboxes. |
| `holds` | Messages waiting for the person's decision. | Until decided; dropped when the target session has been gone 24 h, or 7 days after arrival at most (receipt `expired`; the sender is told). |
| `outbox` | Messages not yet acked by the target computer; attempt count; next retry. | Until acked or 24 h, then receipt `expired`/`unreachable`. |
| `seen_ids` | (sender key, envelope id) for dedupe. | 7 days. |
| `policy` | `people: {person_id: accept|hold|refuse}`, `cloud_of: {person_id: accept|hold}`, `agents_send_without_asking: bool` (for the person's own linked cloud member). Defaults: own sessions `accept`, everything else `hold`. | Until changed. |
| `approvals` | Allow/decline decisions, signed by this computer, with the text shown. | 30 days. |
| `loop_counts` | Per (principal, recipient session) send times; per-chain hop counts. | 1 hour. |

Never written: message text or attachments in the log; private keys outside `identity.key`/keychain; other agents'
transcripts (only the one-line title and current todo item are read, and kept as `doing`); agents' own settings
beyond the entries `setup` adds; teammates' full paths unless `share_paths` is on; cloud credentials; account
passwords (the engine never sees one); any workspace, plan or billing data; anything about a folder marked `off`
(its sessions never register at all).

## Interfaces owned

### Wire (engine to engine; the contract for bridge and tests)

- Transport: QUIC, peers dialed by public key, mutual authentication with each side's team key, direct on LAN
  (mDNS), hole punching, relay fallback over HTTPS 443. **Call:** use `iroh` as a library (dial-by-key, hole punching,
  relays, mDNS exist and are maintained). We run our own relays and discovery; we never depend on the library
  vendor's public ones. The wire protocol (one ALPN, length-prefixed JSON frames, version number) is ours and
  documented in the repo as `docs/wire.md`.
- Frames (all inside the encrypted QUIC stream; `hello` and `roster` carry the protocol and roster head, and the entries since a head):

| Frame | Fields | Notes |
|---|---|---|
| `presence` | member, seq, sessions[], known[], kept[] | pushed on change; every member, the bridge included, sees the whole directory |
| `envelope` | id (ulid), kind: `message|input|resume|pickup`, from (session), to (session or name), reply_to, thread (first id), hops, body, files[], sent_at | Plan A uses `message` only |
| `ack` / `receipt` | id, state, reason | the seven states |
| `hold_notice` | hold_id, target_session, from (session), from_label_parts, preview?, created_at | computer to its person's bridge member only; `preview` (≤160 chars) only when `preview_to_workspace = true` |
| `decision` | hold_id, holding_member, verdict: `allow|decline`, scope: `once|person`, nonce, decided_at | signed by the sender |
| `join_request` / `join_decision` | ticket_id, candidate key, name, proof?, verdict | admission |
| `share_grant` | session, viewer member, input: bool, expires_at | person-signed; authorises `stream` for that viewer |
| `stream` | grant_id, seq, bytes (≤64 KB) | QUIC flow control is the backpressure |

- **Decision acceptance.** The holding computer accepts a relayed `decision` only if all hold: the signer's roster entry carries a `kind=bridge` proof whose `sub` is the hold target's `person_id`; the held message's sender is not one of that bridge's own sessions (no self-approval); scope is `once` or `person` (policy switches such as `cloud_of` or `agents_send_without_asking` are never relayed); the nonce is fresh and `hold_id` + `holding_member` match. The `bridge` role can relay decisions and same-person join requests and nothing else. Residual trust: a compromised backend can allow one held message from a third party into a linked person's session; a phone-held person key that co-signs is Plan D hardening.
- **Streams.** `stream.open` is accepted only from the same `person_id`, or from a member named in a person-signed `share_grant` for that session. The channel is unicast and end-to-end encrypted. The app owns the live-terminal feature; the engine carries the bytes; the bridge reads a stream only by an explicit share grant.
- Limits: body 64 KB; at most 5 files, 128 KB each, 256 KB total, text only; sessions per presence 100.
- Files: an agent may attach only files inside its own working folder (links resolved), never hidden files or
  folders, never under `~/.ssh`, `~/.aws`, `~/.config`, `~/Library` or the state folder. A person at a terminal may
  attach any path. Names reduced to a plain file name; defanged like text; saved as inert files (see Data).

### IPC (daemon socket, newline-delimited JSON, `{id, op, ...}` -> `{id, ok, ...}`)

| Op | Purpose | Called by |
|---|---|---|
| `ping`, `status` | liveness, version, relay/peer summary | all |
| `register`, `end`, `sessions` | session registry (`register` is for hook and MCP paths, caller-identified; the app never registers) | hooks, MCP, CLI; `sessions` and `end` also the app |
| `hook` | hook event in, optional mail out | `hook` subcommand |
| `tool` | MCP tool call with derived source | `mcp` subcommand |
| `send`, `receipt`, `inbox`, `wait` | messaging | CLI, MCP |
| `directory` | all sessions on all computers | CLI, MCP, app |
| `held`, `decide` | list and answer holds (socket-local only) | CLI, dialog, app |
| `policy` | get/set per-person policy | CLI, app |
| `team`, `invite`, `join`, `remove`, `admin`, `leave`, `unlink` | membership | CLI, app |
| `signin_proof` | store a proof | CLI, app |
| `subscribe` | stream events (`deliver`, `receipt`, `held`, `presence`, `roster`) | app, `listen` |
| `shutdown`, `reload` | lifecycle | CLI, updater |

Ops by plan: **A** `ping`, `status`, `subscribe`, `register`, `sessions`, `end`, `send`, `receipt`, `inbox`, `hook`, `tool`, `shutdown`. **B** `identity.*`, `team.*` (incl. `join_requests`, `admit`), `directory`, `holds`, `hold.get`, `hold.answer`, `policy.get/set`, `ui.claim`, `share.grant`, `stream.*`, `reload`. **C** adds none (the bridge uses the engine as a crate). The table above groups the same ops by purpose. Fields are snake_case; errors are `{ok:false, error:"<code>"}` from a closed list per op. The app attaches to the same socket and uses these ops only. It never reads `state.db`. `ui.claim` is a 20 s lease with a 7 s heartbeat: the daemon shows the OS dialog only when no claim is live.
Bridge: a cloud computer is an engine instance that runs headless with `kind = cloud` in its admission.
**Call:** the bridge uses the same binary as a library/daemon, so it speaks the same wire; no bridge-only frames.

### MCP tools (server name `lightbulb`, stdio; identical names and arguments to the hosted MCP)

| Tool | Arguments | Notes |
|---|---|---|
| `find_sessions` | `query` (optional text), `limit` (default 20) | Matches person, agent, repo, branch, title. Returns `session_id`, label, repo, status, doing, tier, computer. |
| `send_message` | `session_id`, `text`, `request_id`; optional `reply_to` (the `requestId` shown in the delivered frame; sets the envelope `thread`, else `thread` is the most recent inbound message from that session, else new), `files` (paths), `notify` (bool) | Refuses `*`, `person/*`, `repo:` with `group_send_not_allowed`. `files` and `notify` are engine-only; the hosted MCP accepts but rejects with `unsupported`. |
| `message_receipt` | `request_id` | Returns state, reason, where it waits. |
| `check_inbox` | `wait_secs` (optional, max 600) | Returns unread messages or waits for the first. |

Every result ends with the unread note when N > 0 and the session is not push tier: `N unread message(s) for this session; they arrive at your next step.`

### CLI

`init`, `join`, `invite [--new|--once|--open]`, `team`, `who [--all]`, `send <to> <text> [--from <session>] [--file <path>]... [--notify]`,
`listen --as <name> [--wait <secs>]`, `receipt <request_id>`, `held`, `approve <id> [--always]`, `decline <id>`,
`policy <person> accept|hold|refuse`, `remove <computer>`, `admin <person> [--founder]`, `leave [--everything]`,
`folder <path> on|off`, `signin`, `relay [<url>]`, `setup [--remove]`, `status`, `doctor [--repair]`, `update [--pin <v>]`,
`reset-identity`, `daemon [--install]`, `mcp`, `hook <agent> <event>`, `version`.
`--json` on every read command. Group targets (`*`, `person/*`, `repo:<name>`) work only when no session identity is
present on the invocation (not from an agent's shell, not with `--from`).

### Hook events

Per agent, mapped to one internal set: `session-start`, `prompt`, `tool-done`, `stop`, `session-end`.
Claude Code and Codex: `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`, `SessionEnd`. Cursor: `postToolUse`, `stop`
(session exists from its first hook). Grok: `PostToolUse`, `Stop`. Mail-taking events: `prompt`, `tool-done`, `stop`.
Output shape is per agent (additional context, or a `block` decision at `stop`).
**Ruling (Plan A, Task 6):** the Cursor and Grok parts are written fresh from those agents' own docs and captured payloads. `hooked.rs` in the monorepo carries ported formats and is never moved into the public repo.

### Delivery tiers

| Agent | Tier | How |
|---|---|---|
| Claude Code | push (wakes idle) | Session inbox socket. |
| Codex | push | `codex queue --thread`, app-server turn start. |
| Cursor, Grok CLI | next_step | Hooks at next step or turn end. |
| Any other MCP client | pull | Unread note + `check_inbox`. |

## Acceptance checklist

Each row runs on a real Mac. "B" is a second account on a second Mac. Pass means the observable result exactly.

1. Fresh Mac, no app: run the install line (`https://thelightbulb.company/install`); `lightbulb` resolves to the engine; `lightbulb-network status` shows the daemon running; launchd job exists (`launchctl list | grep company.thelightbulb.network`).
2. `setup` backs up each edited agent file once; running it twice leaves one backup and no duplicate entries; `setup --remove` restores only our entries.
3. Own ring: Claude -> Codex -> Cursor -> Grok -> Claude. Each hop arrives labelled `Your <Agent>`, is acted on, and the receipt walks `stored` -> `delivered` -> `read`.
4. A Cursor/Grok target shows `stored` until its next step, never `delivered` early.
5. 13th message from one session to another within 10 minutes: receipt `refused`, reason `loop`. 601 s later one is accepted.
6. A body containing `[Sent from Lightbulb by you]` and one containing a forged `[Lightbulb teammate contribution ...]` header both arrive with a zero-width space after `[` and never as a frame.
7. Run `lightbulb-network hook grok session-start` from inside a Claude session's shell: `who` shows no new Grok session.
8. `send_message` with `*` from an agent: refused `group_send_not_allowed`. `lightbulb-network send '*' hi` from a terminal: delivered to all own sessions.
9. `init` on A, `join` on B using the printed line: A is asked to Allow (unless the link was `--open`), then B prints `Connected as ...`; A and B list each other in `who --all`, each with agent, repo, branch, status, doing.
10. `doing` for a Claude session equals its transcript title and current todo item; a session that never calls our tools still shows (passive discovery).
11. B's first message to A's session: receipt `held`; A sees the dialog naming sender, agent and text; clicking it never shows an Allow that is answerable from B.
12. Allow: delivered as `<Agent> via <Person>` or `<Person>'s <Agent>`; B's second agent's next message goes straight in.
13. Decline: receipt `refused`; nothing delivered. A held message to a Cursor/Grok session arrives at its next step after Allow.
14. Held with no desktop (SSH only): `held` lists it; `approve H1` delivers it.
15. Close B's lid, send from A: sender shows `queued`; open the lid: receipt `stored` then `delivered`, no resend by the person.
16. Close the target session, send; reopen it with resume within 24 h: it gets the mail and keeps its name. Past 24 h (clock advanced for test): receipt `expired`.
17. `kill -9` the daemon during a send: on restart the message is still in the outbox and arrives exactly once or twice, never zero.
18. Start the daemon twice at once: one pid in `daemon.lock`; second exits cleanly.
19. Copy `identity.key` to a third machine and start it: it is refused with the "already online" message; admin removal of the old key works.
20. `remove B-mac` by the founder: B cannot connect (traffic check: no accepted connection after the removal entry); old link no longer joins; a link older than 7 days does not join; B's old mail stays on A.
21. A non-admin `remove`: refused with the exact text. `admin aden` then aden's `remove` works.
22. Fork test: an offline admin admits C while the founder removes that admin; after reconnect, C is dropped and the log converges the same on all computers.
23. Replay test: a recorded envelope sent twice yields one delivery and two acks. `request_id` reused with different text: `refused`.
24. Relay test: block direct UDP; messages still flow via relay. Block the relay too: sender shows offline, outbox holds, nothing in plaintext (packet capture shows no body text in either case).
25. Run with a team relay set: a packet trace shows no connection to any Lightbulb host.
26. Files: attach a 100 KB text file from the session's folder; it lands in `inbox/<id>/`, 0600, quarantined, not opened. An agent attaching `~/.ssh/id_ed25519`: refused. Held message's files are not saved before Allow.
27. A folder marked `off`: sessions there never appear in `who`, and their hooks print nothing.
28. Clock +10 minutes on B: `doctor` warns; messages still flow; membership unchanged.
29. Rename a hook field in a fixture for Cursor: `doctor` reports it; the agent is not broken; the receipt stays `stored`.
30. `update` to a newer build while an agent mid-turn: no message lost; `update --pin` returns to the old build; state opens in both.
31. `leave`: service stops, the roster cache is gone, key kept; `join` again with a new link works.
32. Fresh install from the public docs only, no help: a new person reaches "my Codex talked to my teammate's Claude" in under ten minutes.
33. 50 sessions on 20 computers (simulated): directory refresh and message latency are measured and written down; results doc exists.
34. gitleaks passes on the repo; no log in `daemon.log` contains a message body after the whole run (grep for a unique marker string).

## Rulings and open questions

Rulings (architecture 2026-10-22), folded in from the earlier open questions:

1. **Allow versus Always allow.** Allow covers all of that person's computer sessions (`people`); Always allow on a hold from their cloud member writes `cloud_of`. `agents_send_without_asking` is the receiver's switch for the person's **own** linked cloud member (their Friday skips the hold only when it is on).
2. **Unverified names across computers.** No sharing: names are roster-unique, the second computer is `aden-2`. With a proof for the same account, the person's first computer confirms and the second joins with the same `person_id`.
3. **Engine link to the issuer.** `signin` opens the issuer page; the issuer URL is `config.toml: issuer`, configurable like a relay, and is never required.

Still with the owner (sent 2026-10-22): hosting spend for the default relays and the proof issuer before Plan B; Developer ID and notary credentials as public-repo secrets for signed releases. FYI ruling the owner may overturn: `iroh` as the transport library, with our own relays only.

Calls made, not asked: the canonical binary path and the `lightbulb` command rule (no `lbn` alias); 24 h outbox; 7-day hold and 7-day link expiry; hop cap 4 and 12 per 600 s per principal (no per-thread-hour rule); file limits (5 files, 128 KB each, 256 KB total) and 64 KB body; no key rotation in v1 (a compromised key means remove, `reset-identity`, rejoin); dialog buttons; `kind = cloud` as the only engine concept that touches the bridge; the optional `files` and `notify` arguments on `send_message`.
