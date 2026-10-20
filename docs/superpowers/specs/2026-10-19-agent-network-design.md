# Agent network: any session messages any session

Status: design, approved for write-up by the owner 2026-10-19. Not built.

## Why

Lightbulb's launch pitch includes "the team's own local agents". Today a Claude Code or Codex
session joins a workspace only through the Mac desktop app, and only those two agents. The owner
wants any person's coding agent, or their Grok Bot, to find and message any other person's agent
in the workspace, the way Reason Network does it between machines, but with Lightbulb's server
as the hub.

## What a person sees

- Every live agent of every workspace member is a row in the left sidebar, next to today's
  shared sessions: owner, agent kind, busy/idle and for how long, repo@branch, a one-line "doing",
  and how fast it hears you ("wakes now", "next step", "by webhook").
- Any of those agents can message any other one by name.
- A message from someone else's agent is held until the receiving agent's owner OKs it, or goes
  straight in if that sender is already allowed. This is today's rule, unchanged.

## Scope, v1

| Agent | Where it runs | Sends in | Hears a message |
|---|---|---|---|
| Claude Code | a machine | `lightbulb` MCP | at once: wakes an idle session (messaging socket) |
| Codex | a machine | `lightbulb` MCP | at once (`codex queue` / app-server `turn/start`) |
| Cursor | a machine | `lightbulb` MCP | next step or turn end (hooks); an idle session waits |
| Grok CLI | a machine | `lightbulb` MCP | next step (hooks) |
| Grok Bot | xAI's cloud | hosted MCP | a routine started by its webhook (URL + key) |

Out of v1: Muse, Instinct, ChatGPT, Claude.ai, Gemini, Copilot, opencode, Amp. Email and
scheduled polling as delivery paths.

## The parts

### 1. The hub (decided)

The Lightbulb server is the hub: directory, message store, permission check, delivery. No
peer-to-peer. Every delivery is event-driven: a message is written, the server pushes it down the
target's open connection or fires its webhook. Nothing polls.

### 2. `lightbulb join`: a machine without the app (decided)

- One `lightbulb` binary, split out of the desktop app's local-sessions code: the background
  service, the stdio MCP server, the hooks, and the CLI. No Tauri, no window. **macOS only in v1**
  (owner, 2026-10-19); Linux later.
- `lightbulb join` signs the person in by browser handoff: it prints a link and a short code (and
  opens the browser when there is one), the person signs up or signs in on the normal Lightbulb
  page and approves "Connect this machine", and the terminal picks it up. A person with no account
  creates one this way. An invite link from a teammate (today's workspace invite) puts them in that
  workspace; with no invite, the normal sign-up creates their own workspace. On a machine with no
  browser, they open the link on another device. *(builder's call)*
- The service makes the machine's key (Keychain on macOS, a 0600 file on Linux), pairs it the way
  `/desktop/pair` does today, installs itself as a launchd or systemd user service, and keeps one
  open connection to the server.
- `lightbulb setup` wires every installed v1 agent with no prompts: MCP entry, hooks, the
  session-sharing skill. It backs up each file it edits.
- The desktop app runs the same service, so there is one code path.

### 3. Finding sessions

- The service discovers live sessions itself: Claude Code's `~/.claude/sessions/<pid>.json`,
  Codex's session index, and each session's own `lightbulb` MCP server and hooks registering it.
- "doing" comes from what the agent already records (its title, its todo list), never from the
  agent reporting it.
- A restarted session in the same folder with the same agent keeps its name and its unread
  messages. Two live sessions in one folder with the same agent get `-2`, `-3` names. *(my call)*
- An agent is live while its machine's service connection is up and the session runs; it drops
  off a couple of minutes after the connection goes quiet. *(my call)*

### 4. Delivery into a session

- Push, where the agent allows it: Claude Code through its messaging socket, Codex through its
  queue. The desktop app's holder already does this for sessions it starts; the service does it for
  any session.
- Next step, otherwise: Cursor and Grok CLI hooks inject the message at the next tool call or turn
  end.
- Every delivery says which tier it got, back to the sender: "delivered, it is reading it now" /
  "queued, it sees this at its next step".
- Incoming text is framed as from another agent, never as the user, using today's
  `[Lightbulb teammate contribution …]` header with the sending agent named.
- A message to an offline or idle agent is stored on the server and delivered when it can be; the
  sender gets a "queued" receipt, not an error. A held message nobody OKs expires after 7 days.
  *(my call)*
- Loops: agent-to-agent chains use today's session hop cap (2 replies, then pause until a human
  speaks), plus a per-sender rate limit. *(my call)*

### 5. Grok Bot

- Sending: Grok Bot adds the hosted MCP (`https://api.thelightbulb.company/mcp`).
- Receiving: its owner asks Grok Bot for a routine that runs on a webhook, then pastes the
  webhook URL and key into Lightbulb (Settings, on that agent's row). Lightbulb POSTs each
  admitted message to it; the routine wakes the bot with the message and it replies through MCP.
- The URL and key are a credential: stored in Secrets Manager, never shown again after entry.
- xAI's own docs do not describe the webhook trigger yet (our teardown and third-party guides
  do). Verify it before building this part. If it does not exist, Grok Bot ships send-only (no
  polling, no email). *(my call)*
- A Grok Bot belongs to one person; its row is created when its owner registers it in Settings.
  If its hosted-MCP sign-in fails (Clerk has no dynamic registration), add a per-agent key the
  owner creates and revokes in Settings. *(my call)*

### 6. Permission (decided: today's rule)

- Every agent row has an owner. A message from someone else's agent is held until that owner
  OKs it, or goes straight in if the sender is allowed, exactly as shared sessions work today
  (`allowedSenders`, team control, Ask to join).
- **Allowing a person allows all of that person's agents** (owner, 2026-10-19): if Aden is
  allowed into my session, Aden's Claude Code, Codex, Cursor, Grok CLI and Grok Bot all reach it
  without a hold. The allow list stays keyed on people.
- A person's own agents reach each other instantly, as the owner controls their own sessions
  today. Your Grok Bot messaging your own Claude session counts as you. *(my call)*
- An agent is still named as itself: the message, the hold card and the session header say
  "Aden's Grok Bot", not just "Aden", so you can see a bot wrote it. Authority is Aden's;
  the label is the agent's.
- Reuse the existing agent-ask rail (`agent_may_command`, `awaiting_approval`, the Needs-you
  card) rather than a new one.

## Non-goals

- Peer-to-peer transport (iroh) or end-to-end encryption.
- Agents outside the v1 table.
- A new approval system: this reuses today's.

## Anti-goals (failure even if it works)

- A teammate's agent acting in my session without my OK when I have not allowed them.
- Anything polling on a timer.
- A second code path for the desktop app and the CLI.

## Constraints

- Our sign-in server (Clerk) has no dynamic client registration. Claude Code, Codex and the
  `lightbulb` service sign in fine; the service's local MCP needs no OAuth at all (it uses the
  machine's key). Grok Bot's hosted-MCP sign-in is untested; an agent key is the fallback.
- No backward compatibility (owner, 2026-10-19: there are no older apps to keep working; fix
  forward). Rollout: server first, then the desktop app and CLI release. A session started before
  an update resumes after a restart.
- A desktop or CLI release reaches every user (auto-update); there is no test channel.

## Build order *(my call)*

1. The `lightbulb` service and `join`, with Claude Code and Codex (they already work in the app).
2. Cursor and Grok CLI delivery through hooks.
3. Grok Bot: verify the webhook, then send + receive.

## Left to the builder

- Wire protocol between the service and the server (extend today's local-sessions transport or
  replace it).
- How the CLI and the desktop app share the crate.
- Linux key storage beyond a 0600 file, and the systemd unit details.
- The exact header wording for an agent sender.
