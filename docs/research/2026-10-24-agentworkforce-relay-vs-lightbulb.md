# AgentWorkforce Relay vs the `lightbulb` network (2026-10-24)

Clean-room read of `github.com/AgentWorkforce/relay` at `c3a92d9` (2026-10-20, `main`, shallow
clone, nothing run or built). Compared with `lightbulb-network.nosync` `main` (`7a7d145`, v0.2.4)
and the Track C design (`lb-planc-design.nosync/.../2026-10-23-lightbulb-network-stateless-cloud-member-design.md`,
sections 1–4 and 10). Their file references are `path:line` in their repo. No code was copied.

## Summary

**What it is.** A hosted, centralised "Slack for coding agents". A per-machine Rust **broker**
(`crates/broker`) connects over WebSocket to **Relaycast**, a messaging server. By default that
server is their hosted `https://cast.agentrelay.com`, which runs on Cloudflare (Durable Objects
and D1). You can self-host it as one Node + SQLite process. The broker **spawns** coding agents in
PTYs (or wraps them), types incoming messages into their terminals, and gives them an MCP server.
Other layers sit on top: workflows ("flows"), integrations exposed as files ("relayfile"), shared
session history, remote spawn on other machines ("fleet"), and cloud sandboxes (Daytona).

**The three biggest differences**
1. **The server sees and decides everything.** Message bodies, membership and agent identity all
   live on and are authenticated by the Relaycast server, using bearer keys. There is no end-to-end
   encryption and nothing is signed. Ours is peer to peer, end-to-end encrypted, with a signed
   roster and no server of ours.
2. **Remote spawn with permissions bypassed.** The server can tell any node to spawn an agent, and
   every spawned Claude/Codex/Gemini/Grok gets its permission bypass flag added automatically. A
   compromised server, or anyone holding the workspace key, can run code on every node. Ours never
   starts agents and never changes their permissions. It also holds first messages from strangers
   until a person at that Mac clicks Allow.
3. **Breadth and maturity.** They support about 10 harnesses on macOS, Linux and Windows. They
   have channels, threads, files, search and workflows, offline delivery held on the server, 926
   stars, v14 and roughly 2,100 Rust tests. We support 4 agents, on Apple-silicon macOS only, at
   v0.2.4.

| # | Axis | Theirs (one line) | Ours (one line) | Verdict (for us) |
|---|---|---|---|---|
| 1 | Architecture | Hosted hub server + per-machine broker that spawns agents | P2P daemon per computer, no server of ours | different goal |
| 2 | Transport / encryption | TLS to the server; server reads bodies; no E2E | QUIC E2E between member keys; relays see metadata only | **ours better** |
| 3 | Identity / trust / join | Bearer workspace key `rk_live_…` + server-issued agent tokens | Ed25519 key per computer, signed roster, signed invites | **ours better** |
| 4 | Harness coverage / delivery | ~10 harnesses; PTY injection, MCP, some hooks, "native" sessions | 4 agents; hooks + push into existing sessions + MCP | **theirs better** |
| 5 | Offline behaviour | Server stores messages (forever by default); neither side needs to be online together | Sender's outbox, 24 h, both must be online at some moment | **theirs better** |
| 6 | Receipts | Proven "harness acceptance"; durable in-doubt receipt before write | queued/held/stored/delivered/read/refused/expired | same (both rigorous) |
| 7 | Holds for strangers | None; any workspace member's message is delivered | First message from a new person held; native Allow/Decline | **ours better** |
| 8 | Loop / abuse limits | Self-echo filter; policy rate/canMessage not wired in this repo | Hop cap 4, 12 msgs / 10 min per sender per session, enforced by the receiver | **ours better** |
| 9 | Agent permissions | Auto-injects `--dangerously-skip-permissions` etc. | Never touches agent permissions; person-only ops refused from agent shells | **ours better** |
| 10 | State stored where | All messages, channels, files and presence on the server | `~/.lightbulb/connect/state.db` on each computer | **ours better** |
| 11 | Compromised server | Read all, impersonate, inject prompts, spawn bypassed agents, watch terminals | A relay can see who/when/how much and can drop traffic | **ours better** |
| 12 | Telemetry | On by default (PostHog), hashed machine id, account id when signed in | None | **ours better** |
| 13 | Cost / hosting | Hosted cloud "free to start", no public prices; self-host the server | Free, no servers; iroh public relays or the team's own | different goal |
| 14 | Install / platforms | curl\|bash or npm; macOS x64/arm64, Linux x64/arm64, Windows x64 | curl\|sh, brew, npm; Apple-silicon macOS 13+ only | **theirs better** |
| 15 | Feature scope | Channels, threads, DMs, files, search, flows, integrations, shared sessions, fleet | Session-to-session messages, directory, holds | **theirs better** (different scope) |
| 16 | Maturity / activity | 926★, 84 forks, v14.0.0, daily releases, ~1,850 Rust + 255 TS test files, 245 open issues | v0.2.4, 0★, 410 tests, written threat and verification map | **theirs better** |
| 17 | Language | TypeScript CLI/SDK + Rust broker + Swift SDK | Rust, one binary | different goal |
| 18 | License | Apache-2.0 | Apache-2.0 | same |

**Counts:** ours better 8, theirs better 5, same 2, different goal 3.

**Two ideas to adopt (ideas only, not code):**
1. **Receipts that prove the harness accepted the message, plus an "in doubt" state.** They mark a
   message received only when the harness visibly took it (composer cleared, activity began). They
   write a durable receipt *before* the write, and after that a retry may only look up the receipt;
   it never writes the message again. Our push paths (the Claude socket, `codex queue`) would gain
   an honest `in_doubt` state instead of guessing.
2. **A mailbox that does not need sender and receiver online at the same time.** They get this from
   a server that reads the messages. We can get it without giving that up: a message sealed to the
   recipient's key that waits at an opt-in mailbox (for example the workspace's cloud member, or a
   relay-side store that sees only ciphertext). Today a closed laptop lid on the sender's side
   stalls delivery.

---

## 1. Architecture: what runs where

- **Hosted hub.** Every broker talks to Relaycast. The default origin is hardcoded as
  `https://cast.agentrelay.com` (`crates/broker/src/relaycast/auth.rs:1167`,
  `crates/broker/src/node_control.rs:68`; `docs/self-hosting/README.md:53`). The server runs on
  Cloudflare: the broker has retry logic for "D1 pressure" (`node_control.rs:59`) and comments
  about Durable Object hibernatable WebSockets (`crates/broker/src/terminal_control.rs:28`).
- **Self-host is possible for the messaging engine only.** `@relaycast/engine` is "a single
  Node + SQLite process", "single-tenant and single-process" (`docs/self-hosting/README.md:14,19`).
  Cloud features (sandboxes, dashboards, shared sessions) "still require a Cloud account"
  (`docs/self-hosting/README.md:65`).
- **Per-machine broker** (`agent-relay node up`). It holds a node WebSocket with a 12 s heartbeat
  (`node_control.rs:30`), registers the node and its agents, and runs the server's actions.
- **The broker owns the agents.** It spawns them in a PTY (`crates/relay-pty`,
  `crates/broker/src/pty_worker.rs`, `spawner.rs`) or wraps a CLI (`wrap.rs`), and injects messages
  as terminal input (`docs/harnesses/injection.md`).
- **Fleet.** Server-to-node frames are `deliver`, `action.invoke`, `ping`,
  `terminal.reconnect_requested`, `reply` and `error` (`crates/broker/src/fleet_wire.rs:854-868`).
  `action.invoke` with `spawn:<harness>` or `release` starts or stops agents on that node
  (`crates/broker/src/runtime/fleet.rs:1708-1735`).

Ours: one daemon per computer, peers dial each other by key, and the iroh relays are only a
fallback path (`docs/security.md` "Transport encryption", "Relays"). We never start agents. We
attach to sessions the person already runs (`docs/agents.md`). **Different goal**: they orchestrate
fleets of agents; we connect the agents people already run.

## 2. Transport and encryption

- The broker's dependencies contain no message cryptography: no signing or AEAD crates, only
  `sha2`, `rustls` for TLS and `tokio-tungstenite` (`crates/broker/Cargo.toml:15-49`). A search of
  the `.rs`/`.ts`/`.md` sources for encrypt/ed25519/x25519/nacl finds only "credentials are now
  stored and encrypted" at the cloud (`packages/cloud/src/connect.ts:230`).
- Messages are posted to the server and read back from it. Example: the Gemini hook `POST`s
  `/v1/inbox/check` with a bearer token and gets the message bodies back
  (`plugins/gemini-relay-extension/hooks/after-tool-inbox.sh:6-30`). The server must read bodies
  for its search, threads and files features.

Ours: QUIC with TLS 1.3 and raw public keys, end to end between the two computers' keys. A relay
"cannot see message text" (`docs/security.md` "Transport encryption"; `docs/verification.md` §A
"Compromised relay"). **Ours better.**

## 3. Identity, trust and joining

- A workspace is created by an API call that returns a workspace key `rk_live_…`, "the admin
  credential for the workspace" (Relaycast `docs/self-hosting.md` §4). Agents register with that key
  and receive per-agent bearer tokens `at_live_…` (same section). The broker caches both
  (`relaycast/auth.rs:14-30`).
- **Joining means having the key.** `RELAY_WORKSPACE_KEY` in the environment is enough to start a
  node (`docs/self-hosting/README.md:25-45`). With no key set, "a new workspace is auto-created"
  (`plugins/codex-relay-skill/README.md:51`). Their own board lists "fresh `node up` silently mints
  a workspace" as an open issue (`CLAUDE.md`, standing board, #1378).
- Name squatting is decided by the server. The broker sends a stable identity key so that a
  restart is not mistaken for "a stranger" (`crates/broker/src/runtime/session.rs:332-339`).
- A message is authenticated only by the bearer token the server issued. No sender signature is
  carried.

Ours: an Ed25519 key per computer that never leaves it, a hash-linked roster in which each entry is
signed and checked by every member, signed invites with expiry, and admission decided by the
inviter's clock (`docs/security.md` "Identity keys", "The signed roster"; `docs/verification.md`
§A "Forged frames"). **Ours better.**

## 4. Delivery model and harness coverage

- **Harnesses.** The broker has spawn or config paths for Claude, Codex, Gemini, Grok
  (`crates/broker/src/worker.rs:1073-1090`), OpenCode (`snippets.rs:775`), Cursor
  (`snippets.rs:994`, writes `.cursor/mcp.json`), Droid (`snippets.rs:986`), Devin and Muse
  (`docs/harnesses/devin.md`, `muse.md`) and AI SDK agents (`packages/harnesses/src/ai-sdk`). The
  default spawnable set is claude, codex, gemini, opencode, muse, devin
  (`crates/broker/src/runtime/init.rs:991`). There is also a Swift SDK (`packages/sdk-swift`).
- **PTY injection is the main path.** The broker waits for a recognised composer, bracketed-pastes
  the message, then presses Enter. It strips control characters so a message body cannot submit,
  interrupt or edit the composer. Bodies are capped at 14,336 bytes for PTY agents
  (`docs/harnesses/injection.md:1-36`). Two modes exist: `Wait` (deliver at a turn boundary) and
  `Steer` (interrupt) (`fleet_wire.rs:54-57`).
- **Hooks and MCP.** The broker writes MCP config for each harness (`snippets.rs:403-1060`). The
  Codex plugin installs SessionStart, UserPromptSubmit and Stop hooks
  (`plugins/codex-relay-skill/README.md`). The Gemini extension uses `after-tool`/`before-model`
  inbox hooks (`plugins/gemini-relay-extension/hooks/`).
- **"Native existing session" delivery** into a harness session that is already running
  (`crates/broker/src/native_delivery.rs:1-5,22`), dispatched from the server
  (`runtime/fleet.rs:1718`).

Ours: Claude Code, Codex, Cursor and Grok CLI. Hooks at five moments, push into Claude Code's
messaging socket and Codex's `codex queue`, and four MCP tools (`docs/agents.md`). We deliver into
sessions people already opened; they mostly deliver into agents they spawned. **Theirs better** on
breadth and on platforms. Their steer/wait choice is worth noting.

## 5. Offline behaviour

- The server stores messages. Relaycast keeps history forever unless `RELAYCAST_MESSAGE_TTL_DAYS` is
  set (Relaycast `docs/self-hosting.md` §3). An offline agent picks up its inbox later
  (`after-tool-inbox.sh`). The sender does not need to stay online.
- Node liveness: an agent's presence lease on the server expires after 5 minutes without
  authenticated traffic, so the broker re-sends its inventory every 60 s (`node_control.rs:31-36`).

Ours: the message waits in the **sender's** outbox. It is retried every 30 s for 24 h and only
reaches the recipient when both computers are online at the same time (`docs/security.md` "How long
things last"; `docs/verification.md` §B "Sleep / wake"). **Theirs better.** This is the cost of
having no server, and idea 2 below addresses it.

## 6. Receipts

- PTY delivery counts only on `harness_acceptance`: the harness shows activity, clears its composer,
  or echoes the body. If the body stays in the composer, they press the submit key again a bounded
  number of times and never re-send the body. Failures are named (`docs/harnesses/injection.md:50-60`).
- Native delivery writes a durable receipt before writing to the worker. After that boundary every
  retry with the same `delivery_id` is a read-only lookup, with states `in_doubt` and `queued`
  (`native_delivery.rs:1-5,62-80`). The node acks deliveries to the server (`delivery.ack`,
  `fleet_wire.rs:831-850`).

Ours: seven sender-visible states; receipts only move forward and only the target member can move
them; and they survive `kill -9` (`README.md` receipts list; `docs/verification.md` §A "Replay",
§B "Daemon crash"). **Same.** Both are careful. Theirs proves acceptance at the harness, which is
better. Ours gives the sender a richer state to read.

## 7. Holds and approval

- Nothing gates a first message from an unknown sender. Anyone who holds the workspace key is in,
  and messages to an agent are delivered. Their only "hold" is `SetInteractiveHold`, which pauses
  injection while a human types into the PTY (`crates/broker/src/protocol.rs:602-610`). That is not
  a trust gate.
- The policy package has `canMessage` and `rateLimit` fields
  (`packages/policy/src/agent-policy.ts:51-66,416`). No other package in this repo depends on
  `@agent-relay/policy`, so its enforcement is not visible here. It may be enforced on the server
  (unverified).

Ours: the first message from a new person is held, and the person answers in a native dialog on the
recipient's Mac or with `approve`/`decline`. Allow, hold and refuse policy can be set per person.
Joins are held the same way. An agent's shell cannot answer a hold (`docs/security.md` "Holds and
policy"; `docs/verification.md` §A "Wrong-caller"). **Ours better.**

## 8. Loop and abuse limits

- Theirs: a self-echo filter in wrap mode (`crates/broker/src/wrap.rs:1480,2197`), retries on
  server registration rate limits (`relaycast/auth.rs:1135`), and a 16 KiB envelope ceiling for PTY
  delivery. There is no hop limit in the broker. The policy rate fields are as in section 7.
- Ours: `MAX_HOPS = 4`, with the floor taken from the receiver's own record of the thread, and 12
  messages per 600 s from one person to one session (`docs/security.md` "Loop limits";
  `docs/verification.md` §A "Loops"). **Ours better.**

## 9. Agent permissions (local safety)

- "Permission-bypass flags are auto-injected for all spawned agents. This means any actor who can
  trigger agent.add gets agents with no permission guardrails." Their own comment says so
  (`crates/broker/src/worker.rs:1070-1072`). The flags are `--dangerously-skip-permissions`
  (Claude), `--dangerously-bypass-approvals-and-sandbox` (Codex), `--yolo` (Gemini) and
  `--always-approve` (Grok) (`worker.rs:1073-1090`, repeated at `worker.rs:1334-1348`). Muse runs
  unattended with tool approvals off (`worker.rs:2430`).
- Ours: setup adds hooks and an MCP server only. We do not start agents or change their permission
  settings (`docs/agents.md` "What setup does"). **Ours better.**

## 10. State: what is stored and where

- Server: workspaces, agents, channels, threads, DMs, presence, files, message history (forever by
  default) and actions (Relaycast `docs/self-hosting.md` §1, §3). In the hosted service this is
  their Cloudflare tenancy.
- Local: a credential cache (`relaycast/auth.rs:14-45`), native delivery receipts
  (`native_delivery.rs`), and "trajectories" kept in the repo (`.agentworkforce/trajectories/`,
  per their `CLAUDE.md`).
- Ours: everything is on the computer, in `~/.lightbulb/connect/` (`docs/security.md` "Data on your
  computer"). **Ours better.**
- Side note on our docs: `docs/security.md:103` says message text is deleted once nothing waits on
  it, and `docs/security.md:119` says "Message text is kept in `state.db` after delivery; nothing
  deletes it." One of the two is wrong and should be fixed.

## 11. What a compromised server could do

For theirs (Relaycast, or anyone holding a workspace key, where the server allows it):
- read every message, file and thread in every workspace (section 2);
- send as any agent: it issues the bearer tokens and messages carry no signatures (section 3);
- inject text into agents' terminals through `deliver` (`fleet_wire.rs:856`). The control-character
  stripping stops early submits, not hostile content;
- **spawn agents with permissions bypassed on any node** that advertises `spawn:<harness>`, which is
  on by default for six harnesses (`runtime/init.rs:988-1012`, `runtime/fleet.rs:1727-1730`,
  `worker.rs:1070`). In practice that is remote code execution on every enrolled machine;
- watch terminal output over the terminal lane (`crates/broker/src/terminal_control.rs:1-5`).

For ours: a relay sees which keys talk, their IPs, timing and sizes, and it can drop or delay
traffic. It cannot read, change or impersonate (`docs/verification.md` §A "Compromised relay",
known gap 8). **Ours better.** This is the largest difference.

## 12. Telemetry

Theirs is on by default with opt-out. It records events, CLI types, commands and a hashed
`machine_id` on every event. When the user is signed in to Cloud it adds their user id, org,
email and name, and sends identity headers to the hosted gateway (`TELEMETRY.md:5,20-49`). Message
content is not collected (`TELEMETRY.md` "Message metadata"). Ours sends none
(`docs/security.md` "Telemetry"). **Ours better.**

## 13. Cost and hosting

The hosted cloud is "Free to start". agentrelay.com publishes no prices and `/pricing` returns 404
(fetched 2026-10-23). They run the servers. You can self-host Relaycast for free. Ours is free and
runs no servers (`README.md`). **Different goal**: their business is the hosted hub; ours puts
revenue in the paid workspace (Track C).

## 14. Install and platforms

`curl … install.sh | bash` or `npm install -g agent-relay` (`README.md` "Getting Started"). They
ship prebuilt broker packages for darwin arm64/x64, linux arm64/x64 and win32 x64 (`packages/broker-*`).
Ours: curl|sh, Homebrew and npm, Apple-silicon macOS 13+ only (`README.md` "Install"). **Theirs
better.**

## 15. Language and maturity

- They have about 7.3 MB of TypeScript, 4.9 MB of Rust (122,979 lines in `crates/`) and 0.4 MB of
  Swift. About 1,850 Rust `#[test]` functions and 255 `*.test.ts` files. 33 workflows, including
  `security.yml`, `stress-tests.yml`, `fleet-e2e.yml` and clean-room qualification.
- GitHub (2026-10-23): 926 stars, 84 forks, created 2025-12-17, pushed 2026-10-23. Latest release
  v14.0.0 (2026-10-13), with v13.2.0 through v14.0.0 released between 2026-10-08 and 2026-10-13.
  245 open issues, 88 open PRs. Top committers: khaliqgant 2,214 commits, willwashburn 1,032,
  claude 538. Their `CLAUDE.md` names a "resident" agent as the lead on the repo.
- Churn shows: their board lists open secret-leak fixes (#1379, keys in argv), 64 dependabot alerts
  with 1 critical (`CLAUDE.md`, standing board, dated 2026-07-29), and a broker that comments on
  its own fail-open paths.
- Ours: v0.2.4, 410 tests, and a threat model and failure modes mapped to tests
  (`docs/verification.md`). **Theirs better** on maturity and adoption. Ours is better documented
  for security.

## 16. License

Both are Apache-2.0 (their `LICENSE`, `README.md`; our `README.md`). **Same.** A clean-room reading
is still the right posture.

---

## Worth adopting (ideas only)

1. **Receipts that prove acceptance, with an `in_doubt` state.** Write the receipt before the push.
   After that point a retry may only look the receipt up. Mark `delivered` only when the agent
   shows it took the message. This fits our Claude-socket and `codex queue` pushes.
2. **A mailbox for non-overlapping online windows, without a server that reads bodies.** Seal the
   message to the recipient's member key and park it with an opt-in holder: the workspace cloud
   member, or a store at the relay that sees only ciphertext. The sender can then sleep.
3. Smaller ideas:
   - **wait vs steer** as an explicit choice per message (deliver at the next step, or interrupt).
   - **capability advertising** per computer (what this computer can run or offer), useful for a
     cloud member that offers "Friday" and hosted apps.
   - **a named, user-facing reason** for every refused or uncertain delivery. They do this well;
     we mostly do.

## Explicitly do not do

- Add permission-bypass flags to any agent, ever, or start agents on someone's computer from the
  network.
- Let any server or remote member trigger actions on a computer by default. Their default
  `spawn:<harness>` capacity is the anti-pattern.
- Use a bearer workspace key as membership, or silently create a team when no key is set.
- Put message bodies where a server can read them, for search or any other feature.
- Turn telemetry on by default or attach account identity to usage.
- Copy their breadth-first churn (v14 in 10 months, daily releases, open secret-leak items). Our
  verified, slower cadence is a selling point next to them.

## Relevance to Track C (hosted agents joining the network)

- **"Cloud is one more computer" is the same pattern they use.** Their cloud sandboxes (Daytona)
  run the same broker and enrol as a node in the workspace. The client waits for "the enrolled
  node to report ready" (`packages/cloud/src/fleet-sandbox.ts:17-28`). This supports Track C's
  shape (design §1, §2). The difference: theirs is a node of a hub they host, while ours is a peer
  holding its own key.
- **They do not handle parked compute.** Nodes hold a live WebSocket with a 12 s heartbeat and a
  5-minute presence lease (`node_control.rs:30-36`), and sandboxes stay up while provisioned (async
  preparation can take up to 35 min, `fleet-sandbox.ts:28`). Nothing in the repo matches our
  doorbell (design §4.6) for waking a parked AgentCore runtime. Their server hibernates (Durable
  Objects); their agents do not. Our wake-on-invoke member has no counterpart there.
- **Hosted assistants.** No ChatGPT, Claude.ai or Grok-app path was found. Their "hosted" agents
  are harnesses they drive (Devin, Muse, AI SDK). Our "<App> via <Person>" (design §2) has no
  counterpart.
- **Backend-off-bodies rule.** Their hub reads every body. Design §3 keeps network bodies, the
  roster and member keys away from our backend, and that is our clearest difference from them.
  Keep it. In phase 1 the backend mints `networkKey` once (design §4.3 ruling), which is a narrow,
  known exception and still far from their model.
- **Inbound to Friday while she is busy.** Their wait/steer split maps onto design §4.5 job 2.
  Inbound network messages becoming local turns should default to "wait" (the next turn boundary),
  matching our `HealthyBusy` handling.
- **At-least-once inbound with ack after the turn** (design §4.5 job 2) is the same discipline as
  their durable receipts. Consider their `in_doubt` idea for the case where the microVM dies
  between the turn and `inbox.ack`.
