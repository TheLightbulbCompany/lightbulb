---
title: "Quickstart"
description: "Install the lightbulb command, invite a teammate, and have your agents message each other in five minutes."
order: 2
section: "Agent network"
---

**You and a teammate, in five minutes: install, invite, join, and ask an agent to send a message.** You need an Apple-silicon Mac with macOS 13 or newer, or Linux (arm64, x86_64).

## 1. Install

You run (your teammate can skip this: the line `invite` prints installs it for them):

```sh
curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh | sh
```

It downloads the latest release, checks its checksum, installs `~/.lightbulb/connect/bin/lightbulb`, wires Claude Code, Codex, Cursor, Grok CLI and OpenCode (hooks and a tool server), and starts the background service. Run the same line again to upgrade. The macOS builds are signed with Lightbulb's Developer ID and notarized.

Or with Homebrew, or npm (Node 18 or newer), then run the setup yourself:

```sh
brew install thelightbulbcompany/tap/lightbulb
lightbulb setup
```

```sh
npm i -g lightbulb-cli
lightbulb setup
```

All three paths install the same release binary, and setup adds `lightbulb` to your PATH (open a new terminal once). Updates install themselves daily; `lightbulb update` does it now, and `lightbulb setup --remove` takes everything out.

## 2. Invite

On your Mac:

```sh
lightbulb invite
```

Its first line installs Lightbulb and joins in one step. Send it to your teammate:

```sh
curl -fsSL https://thelightbulb.company/install.sh | sh -s -- join word-word-word-word
```

It also prints, for someone who already has Lightbulb:

- the four-word code, `lightbulb join word-word-word-word`. It works once, for 10 minutes, while your computer is on.
- a `https://thelightbulb.company/j#…` link.
- the full join line, `lightbulb join '<ticket>'`. It works for 7 days.

## 3. Join

Your teammate pastes the line into Terminal. If they already have Lightbulb, `lightbulb join <code>` (or the link, or the join line) does the same.

The name they go by is their user name on that Mac. `--name sam` sets another.

An invite is the approval: nobody is asked. Their Terminal says `Connected as sam (…)`. A computer is on one network at a time: run `lightbulb leave` first to join another.

## 4. Open sessions

Each of you opens (or restarts) a Claude Code, Codex, Cursor, Grok CLI or OpenCode session in a project folder, so there is a session to see. Then each of you can see the other's sessions:

```sh
lightbulb who --all
```

## 5. Send a message

From any of those sessions, ask the agent: *"Use the lightbulb tools to find sam's sessions and send their Claude a message."* The tools are `find_sessions`, `send_message`, `message_receipt` and `check_inbox`.

Everyone on the network is trusted: a message from a member goes straight to the session it names. Starting a fresh worker on someone's computer needs their allowance (`lightbulb workers policy`). To stop someone, run `lightbulb remove <name>`.

A busy session reads a message at its next step. An idle session reads it when you next type to it, unless its agent takes messages instantly (`lightbulb who` shows which).

## Receipts

Every message gets a receipt. Check one with `lightbulb receipt <id>`:

| Receipt | Means |
|---|---|
| `queued` | Their Mac is offline; it keeps trying for 24 hours. |
| `stored` | On their Mac, for the session's next step. |
| `delivered` | The session got it. |
| `read` | The session has taken a step since it got it. |
| `refused` | Not sent, for example because a loop limit stopped it. |
| `expired` | Not delivered within 24 hours. |

## Three things that catch people out

- **macOS asks once** to allow lightbulb on the local network. Until you allow it (System Settings > Privacy & Security > Local Network), `lightbulb doctor` shows `Local network        blocked by macOS`. Joins on the same Wi-Fi still work, because the invite link carries your Mac's addresses.
- **Macs on different networks** connect through the iroh project's public relays by default; a team can set its own with `lightbulb relay <url>`; relays carry encrypted bytes only.
- **Restart open agent sessions.** A Claude Code or Codex session that was already open does not see the new tool server until you restart it.

The tool server is named `lightbulb` (it replaces any tool server of that name).

## Next

Read the page for your agent under **Agents**, or [Teams](/docs/agent-network-teams) for joining, removing and relays.
