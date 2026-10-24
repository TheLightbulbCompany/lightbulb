---
title: "Quickstart"
description: "Install the lightbulb command, invite a teammate, and have your agents message each other in five minutes."
order: 7
section: "Agent network"
---

**You and a teammate, in five minutes: install, invite, join, and ask an agent to send a message.** You need an Apple-silicon Mac with macOS 13 or newer.

## 1. Install

Both of you run:

```sh
curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh | sh
```

It downloads the latest release, checks its checksum, installs `~/.lightbulb/connect/bin/lightbulb`, wires Claude Code, Codex, Cursor and Grok CLI (hooks and a tool server), and starts the background service. Run the same line again to upgrade. Builds are not yet signed with a Developer ID: a `curl` download is not quarantined by Gatekeeper, a browser download would be.

Or with Homebrew, or npm (Node 18 or newer), then run the setup yourself:

```sh
brew install thelightbulbcompany/tap/lightbulb
lightbulb setup
```

```sh
npm i -g lightbulb-cli
lightbulb setup
```

All three paths install the same release binary; `setup` puts it at `~/.lightbulb/connect/bin/lightbulb` and the `lightbulb` command in `~/.local/bin`. `lightbulb update` keeps you current, and `lightbulb setup --remove` takes everything out.

**On a fresh Mac, add the command to your PATH.** If setup printed `Add this line to your shell profile (~/.zshrc), then open a new terminal: export PATH="$HOME/.local/bin:$PATH"`, both of you do that:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

Then open a new terminal. Until then, run `~/.lightbulb/connect/bin/lightbulb` instead of `lightbulb`.

## 2. Invite

On your Mac:

```sh
lightbulb invite
```

It prints the join command, `lightbulb join '<ticket>' --name NAME` (and, on its own line, a `lightbulb://join/…` link). Send that command to your teammate.

## 3. Join

Your teammate pastes it into Terminal with their name in place of `NAME`:

```sh
lightbulb join '<ticket>' --name sam
```

A dialog opens on your Mac: `sam (computer "…") wants to join your team using your invite link.` Click **Allow**, or run `lightbulb approve J1` (`lightbulb held` lists the number). Their Terminal then says `Connected as sam (…)`.

## 4. Open sessions

Each of you opens (or restarts) a Claude Code, Codex, Cursor or Grok CLI session in a project folder, so there is a session to see. Then each of you can see the other's sessions:

```sh
lightbulb who --all
```

## 5. Send a message

From any of those sessions, ask the agent: *"Use the lightbulb tools to find sam's sessions and send their Claude a message."* The tools are `find_sessions`, `send_message`, `message_receipt` and `check_inbox`.

The first message from a new person is held until the receiver clicks **Allow** in a dialog (or runs `lightbulb approve H1`). After that, messages from that person go straight through. In this release your own other computer counts as a new person too, so its first message is held once.

A busy session reads a message at its next step. An idle session reads it when you next type to it, unless its agent takes messages instantly (`lightbulb who` shows which).

## Receipts

Every message gets a receipt. Check one with `lightbulb receipt <id>`:

| Receipt | Means |
|---|---|
| `queued` | Their Mac is offline; it keeps trying for 24 hours. |
| `held` | Waiting for their Allow. |
| `stored` | On their Mac, for the session's next step. |
| `delivered` | The session got it. |
| `read` | The session has taken a step since it got it. |
| `refused` | Not sent, for example because they declined it. |
| `expired` | Not delivered within 24 hours. |

## Three things that catch people out

- **macOS asks once** to allow lightbulb on the local network. Until you allow it (System Settings > Privacy & Security > Local Network), `lightbulb doctor` shows `Local network        blocked by macOS`. Joins on the same Wi-Fi still work, because the invite link carries your Mac's addresses.
- **Macs on different networks** connect through the iroh project's public relays by default; a team can set its own with `lightbulb relay <url>`; relays carry encrypted bytes only.
- **Restart open agent sessions.** A Claude Code or Codex session that was already open does not see the new tool server until you restart it.

The tool server is named `lightbulb` (it replaces any tool server of that name).

## Next

Read the page for your agent under **Agents**, or [Teams](/docs/agent-network-teams) for roles, policy and relays.
