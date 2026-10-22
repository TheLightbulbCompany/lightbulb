# lightbulb-network: peer-to-peer agent network (design)

Status: draft for owner approval, 2026-10-22.

## Why

People's coding agents (Claude Code, Codex, Cursor, Grok CLI and more) should talk to each other, and to the
agents of people they invite, without a Lightbulb server reading anything. Today every message goes through our
backend in plaintext. Reason Network shows the free, peer-to-peer version works and is what this audience trusts.

Lightbulb's public face becomes this network: simple, free, open source, developed in the open. Friday and the
cloud agents, chat, phone approval and the rest stay in the paid website and app, which join the network as a
bridge.

## Goals

1. Free, open-source engine in a new public repo, **`TheLightbulbCompany/lightbulb-network`**. It is created
   private, and the owner makes it public after reviewing the first PR (charter, 2026-10-22); from then on every
   commit, PR, review and CI run is visible, and the history before the flip stays visible too.
2. Computers talk directly to each other, end-to-end encrypted. A relay is a fallback only and sees ciphertext only.
3. Feature parity with Reason Network's public feature set (list below), then improve on it.
4. Onboarding in one line (`curl … | sh` or one line pasted to an agent), focused only on "your bots talking to each
   other, and to the bots of people you invite".
5. The paid Lightbulb workspace connects in as a bridge: Friday and cloud agents, hosted MCP clients (Muse,
   ChatGPT, Claude apps, Grok Bot), webhook agents, agent keys, phone and web approval.
6. When parity passes, everyone moves to the new engine automatically. There are no existing users to migrate.

## Non-goals

- Using Reason's code. Reading their open-source code for inspiration or clarification is allowed (owner,
  2026-10-22); copying, porting or transcribing it is not. Think through how each piece works and what it is for,
  then build our own. Our existing `apps/desktop/src-tauri/src/local_sessions/hooked.rs` says its hook formats were
  ported from reason-network; it never enters the public repo. The engine's Cursor and Grok support is written
  fresh from those agents' own docs and captured payloads (Plan A, Task 6 ruling, 2026-10-22).
- Cloud agents, chat, tasks, Paperclip, billing or anything workspace-shaped in the public repo.
- A Lightbulb server in the message path between people's computers.
- Windows and Linux at first: macOS first, Linux next (core split step 8), Windows later.

## Anti-goals (a failure even if it works)

- Anything on the free path that asks for a card, a plan, or a workspace.
- The CLI or public docs drifting into Friday/CEO/workspace features.
- A secret, internal hostname, account id or customer datum landing in the public repo.

## Feature parity list (the acceptance bar)

From Reason Network's public README, network page and design notes:

1. **Sign in and you have a network**; your other computers join by signing in with the same account; friends join
   from an invite line pasted to their agent or run in a terminal.
2. **Team membership** that every computer holds and checks itself (signed entries); only members can connect;
   any member can invite or remove; removal also replaces the invite link.
3. **Live directory**: every session on every teammate's computer with person, computer, agent, repo, branch,
   busy or idle, and what it is doing (from the agent's own title and todo list, never self-reported).
4. **Direct, encrypted transport**: same-network discovery, hole punching across the internet, relay fallback; a team
   can run its own relay.
5. **Delivery tiers per agent**: push (live, wakes idle), next step (hooks), pull (unread note on every tool result,
   plus a check-inbox tool that can wait). Senders are told which.
6. **Offline**: the sender's computer keeps an outbox and retries; mail for a closed session waits on the recipient's
   computer for a day; the sender is told where it waits.
7. **Receipts**: stored, delivered, held, refused; later delivered and read.
8. **Small text file attachments** from the agent's own folder only, saved inert on the receiver.
9. **Approval**: per-computer inbound policy (accept, hold, refuse; per person); a pop-up on the person's own
   computer for approve or deny, answered only there, signed by that computer.
10. **Safety**: peer text framed as peer input, loop limit per sender and per thread, group sends only from a
    person at a terminal.
11. **Names that stay** with a conversation across resume; a menu-bar view of who is online.
12. **Hosted assistants** reach the network through a member's computer (Reason: via its server). For us this is
    the paid workspace's bridge (goal 5).
13. **CLI**: who, send (with notify and files), listen, link, setup, leave, folder on/off.

### Pinned behaviour for the vague items

- *What it is doing* (3): the session's own title (Claude Code's transcript title, Codex's thread name, Cursor's
  and Grok's where they keep one) and its current todo item. Agents never report it.
- *Pull that can wait* (5): the check-inbox tool takes an optional wait in seconds and returns on the first unread
  message or at the deadline.
- *One-day hold* (6): mail for a session that is not live waits on the recipient's computer; after 24 hours with
  that session never back, it is dropped and the sender's receipt reads `expired`. The person's own terminal
  mailbox never expires.
- *Receipts* (7): `stored` (on the recipient's computer), `delivered` (handed to the agent), `read` (the agent's
  next turn consumed it), `held` (waiting for the person), `refused`, `expired`. A sender may ask for a receipt
  at any time by request id.

## Acceptance checklist (from the 2026-10-22 e2e on v0.4.905)

Run on real sessions on a Mac, with a second account as the teammate:

1. Own ring: Claude → Codex → Cursor → Grok → Claude, each delivered and acted on, labelled "Your <agent>".
2. Teammate's first message to a session: held, receipt `held`; a Mac notification names the sender; clicking
   it opens the session with Allow and Decline.
3. Allow delivers it labelled "<Agent> via <Person>" (or "<Person>'s <Agent>"), withdraws the notification, and
   the same person's next message (from any of their agents) goes straight in.
4. Decline: receipt `refused`, nothing delivered.
5. A held message to a Cursor or Grok session arrives at its next step after Allow.
6. Loop limit: the 13th message from one sender to one session inside 10 minutes is refused.
7. A body that forges the owner header is neutralised.
8. Friday (through the paid bridge) → a session: delivered with the agent label; held unless the person's
   "agents send without asking" is on.
9. Sender offline: a message queued while the recipient's Mac is asleep arrives after wake, receipt `stored` →
   `delivered`.
10. `join` on a fresh Mac with no app installed ends with "Connected as <you>"; `leave` stops the service.

## Improvements we already know of

- Receipts and labels as tested on 2026-10-22 ("Your Codex", "Aden's Codex", "X via Person").
- One Allow per person covering all their agents, and Always allow for agents.
- The bridge: Friday, cloud agents and phone or web approval for people who pay.
- Codex, Claude Code push and the session registry/holder work already in `apps/desktop/core`.

## What carries over from today's work

- Delivery into each agent (Claude push, Codex queue, Cursor/Grok hooks, pull note), session discovery,
  `lightbulb setup`, the installer, Homebrew tap and CLI commands, the docs (rewritten for the new install).
- Today's e2e checklist becomes the parity acceptance test, run on real sessions on a Mac.
- Today's server-hub network stays live until parity, then the hub becomes the bridge only. The two open fix PRs
  still ship.

## Decided

- Build it ourselves; no Reason code (owner).
- New public repo `lightbulb-network`, public from the first commit, Lightbulb name kept (owner).
- The private monorepo depends on the public repo pinned by release tag; never the reverse (my call).
- Fresh history; the existing `lightbulb-core` commits are not exported (my call: they sit beside private code).
- Free, no card (owner). Licence Apache-2.0 for the engine (approved earlier for core).
- Move everyone over automatically at parity (owner).
- gitleaks on every PR from day one (my call; draft config in `docs/research/open-source-client/`).
- **The free path needs no Lightbulb app** (my call): the engine is one self-contained binary — a headless daemon
  installed as a launchd service plus the CLI. When the desktop app is installed it runs that same engine (one
  instance per computer; the app attaches to the running daemon rather than starting a second). Approval pop-ups
  on the free path come from the daemon through the OS's own dialog; the app's richer notifications are a bonus.
- **Relays** (my call): Lightbulb runs the default relays and rendezvous, as a convenience, seeing ciphertext and
  which computers talk only; a team can point at its own relay and then contacts nothing of ours. Cost is a small
  VPS per region.
- **Keys** (my call): one key per computer in a 0600 file under the state folder, moved into the OS keychain where
  one exists; nothing macOS-only in the crypto or the wire format, so Linux (split step 8) is a port, not a redesign.
- **Group sends** (my call): the MCP `send_message` tool refuses `*`, `person/*` and repo-wide targets outright;
  the CLI allows them only when run by a person (no session identity on the invocation: not from an agent's shell,
  not with `--from <session>`).
- **No coexistence period** (owner, 2026-10-22: nobody is on the current hub; it was just built and tested). The
  hub's session-messaging path is replaced outright as the engine lands, with nothing kept for compatibility; the
  hub keeps only the bridge role. The first desktop release carrying the engine is the cutover. The hooks code
  moves into the public repo as it is (parity needs it) and is rewritten there after parity. The monorepo pins the
  public crates by release tag.

- **Identity** (owner, 2026-10-22): **an account is optional.** A computer joins and sends with its own key and an
  unverified name. Signing in with Lightbulb issues a proof that verifies the name and links the person to their
  paid workspace, which is what lets Friday reach their sessions. Our sign-in server is never required to join or
  send; proofs verify offline against keys compiled into the engine.
- **Membership** (owner, 2026-10-22): any member can invite; **only the founder, and members the founder marks
  admin, can remove.** Removal blocks new connections and future mail and replaces the invite link; mail already on
  the removed computer stays there.

## Architecture: three parts, one direction of dependency

```
public   lightbulb-network   engine daemon + CLI + MCP server + hooks   (knows nothing of workspaces or billing)
            ▲                          ▲
private  Lightbulb app (Tauri)     bridge (backend)                       app and bridge depend on the engine,
         embeds/attaches the engine  a workspace "computer" on the        pinned by release tag; the engine never
         for UI, notifications,      network: Friday, cloud agents,       depends on either
         Share, sessions view        hosted MCP, webhooks, phone approval
```

- The **engine** owns identity, membership, transport, delivery into agents, receipts, holds and the CLI. It has
  no notion of a workspace, a plan, a room or a Paperclip agent.
- The **bridge** is the only code that speaks both languages: it is a member of a person's network on the
  workspace's behalf and translates workspace events (a Friday run, a web Allow) into network messages, and back.
- The **app** is UI over the engine: it starts or attaches to the daemon, shows sessions, and renders the
  approvals the daemon raises. It carries no transport or delivery logic of its own.
- **The monorepo gets cleaner, not fatter.** As each piece moves into the engine, the hub-era code it replaces is
  deleted from `apps/backend` and `apps/desktop` in the same PR, with no dual paths left behind (owner: "our main
  repo is clean as well").

## Where each thing lives (the split; owner: do not forget it)

The rule: what a computer's agent needs goes in the engine; what a hosted or phone agent needs goes through the
hosted MCP and the bridge; the app only renders and asks. The hosted MCP keeps the same tool names and arguments
as the engine's MCP, so an agent's skill works on either.

| Capability | Engine + CLI (free, public) | App (private, free UI) | Hosted MCP + bridge (paid workspace) |
|---|---|---|---|
| Identity, keys, name proofs | owns keys; verifies proofs offline | shows sign-in | issues proofs on Lightbulb sign-in |
| Membership, invites, removal | the whole model; `link`, `invite`, `remove` | buttons over the same | — |
| Transport, relays, encryption | owns it | — | the bridge is a member computer in the cloud |
| Session discovery, presence, "doing" | owns it | sessions sidebar reads the engine | `find_sessions` through the bridge |
| Delivery into local agents (push, hooks, pull) | owns it | — | hosted agents are pull-only: unread note + check-inbox |
| Holds and approval | per-computer policy; OS dialog; `approve` in the CLI | notification, Allow / Decline / Always allow | Allow from web and phone |
| Receipts | owns them | shows them | `message_receipt` |
| Small file attachments | owns it | — | later, if hosted agents need it |
| Loop limit, peer framing, defang | owns it | — | bridge applies the same to hosted senders |
| `setup` for installed agents, self-update | owns it | runs it on launch | — |
| Share a live terminal, session browser, resume | — | owns it | — |
| Friday and cloud agents, rooms, chat, tasks | — | UI | owns it (`find_rooms`, `read_messages`, tasks) |
| Agent keys, webhook agents, DCR clients | — | settings UI | owns it |
| Group sends (`*`, `person/*`, `repo:`) | a person at the CLI only; the MCP tool refuses | — | refused |

## Process (owner, 2026-10-22)

- Ponytail: the simplest working thing at every step; no abstraction without a second user.
- `/simplify` after every task, before review.
- Every PR to the public repo passes gitleaks, Codex review and public CI.

## Left to the builder

- Transport library and wire format (Rust; QUIC with key-based dialing is the obvious fit), relay hosting.
- How sign-in proofs are issued (Clerk-backed issuer is the default guess) and verified offline.
- How the paid bridge joins: a workspace "member computer" in the cloud versus routing through a person's computer.
- How much of `apps/desktop/core` moves into the new repo versus is rewritten.
- Release and auto-update mechanics for the engine binary.
