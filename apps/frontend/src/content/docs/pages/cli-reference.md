---
title: "CLI reference"
description: "Every lightbulb command: setup, doctor, invite, join, who, send, listen, receipt, held, approve, decline, team, admin, remove, leave, policy, folder, relay, update and version."
order: 18
section: "Reference"
---

**Every `lightbulb` command, with the options and arguments its `--help` lists (version 0.2.2).** Run `lightbulb <command> --help` for the full text in your terminal. To install the command, see the [Quickstart](/docs/agent-network-quickstart).

Commands that decide for you (`join`, `held`, `approve`, `decline`, `policy <person> <value>`, `remove`, `admin`, `invite --open`, `relay <url>`, `leave`) must be run by a person at a terminal, not by an agent. Run from an agent, most answer `Run this from your own terminal, not from an agent.`; `invite --open` answers `invite --open is for people at a terminal`.

## Getting started

### setup

Once after installing, after an upgrade, or to turn Lightbulb off.

```
lightbulb setup [OPTIONS]
```

Connects Claude Code, Codex, Cursor and Grok CLI here (their hooks and the `lightbulb` tool server) and starts the background service. Each file is backed up once as `<file>.lightbulb-backup`. Details are on each agent's page under **Agents**.

| Option | Meaning |
|---|---|
| `--remove` | Take out only what setup wrote, and stop the background service. Backups are left as they are. |
| `--executable <path>` | The binary the hooks, tool servers and background service run. Default: `~/.lightbulb/connect/bin/lightbulb`, which setup installs from the binary you ran. |
| `--server-name <name>` | The tool server's name, also used in Cursor's and Grok's allow rules. Default: `lightbulb`. |
| `--verbose` | Also list the files each agent's setup changed. |

### doctor

When something does not work.

```
lightbulb doctor
```

Checks the background service, the relay, the local network, the login item, the state folder, each agent and the `lightbulb` command, and prints the fix for the first thing that is wrong. Exits 1 when something is wrong. Every line: [Troubleshooting](/docs/agent-network-troubleshooting).

### invite

To add your other computer or a teammate.

```
lightbulb invite [OPTIONS]
```

Prints this computer's join line (the reusable invite unless an option says otherwise).

| Option | Meaning |
|---|---|
| `--new` | Replace the reusable invite; the old one stops working. |
| `--once` | An invite for one join, valid 24 hours. |
| `--open` | An invite whose joins need no Allow (founder and admins). |

### join

On the new computer, with the line someone sent you.

```
lightbulb join [OPTIONS] <TICKET>
```

Joins the team the line is for. This computer must be on its own.

| Argument / option | Meaning |
|---|---|
| `<TICKET>` | The line your inviter sent: the long code after `join`, or the whole `lightbulb://join/…` link. |
| `--name <name>` | The name you go by in the team. Default: your user name. |

## Everyday

### who

To see which sessions are open, and how each gets messages.

```
lightbulb who [OPTIONS]
```

Lists the coding sessions on this computer: name (such as `codex-1`), agent, folder, status and what it is doing.

| Option | Meaning |
|---|---|
| `--all` | Also list the sessions on teammates' computers (with person and computer). |
| `--json` | Print the raw session list as JSON. |

### send

To message a session from your terminal.

```
lightbulb send [OPTIONS] <TARGET> <TEXT>...
```

`<TARGET>` is a name from `who` (`codex-1`), a session id, a name address `<member>/<name>` (for example `sam/inbox`, where `sam` is a teammate's name), or `*` for every open session on this computer. The rest of the line is the text. It prints the receipt, for example `Sent. sam has to allow it first (they see a prompt). Check: lightbulb receipt <id>`.

| Option | Meaning |
|---|---|
| `--from <session>` | Send as this session (a person speaking for it) instead of the terminal. |
| `--reply-to <id>` | The request id of the message this answers. |
| `--request-id <id>` | Use this request id instead of a new one (to retry a send). |

Group sends (`*`, `<member>/*`) are for people at a terminal only.

### listen

To read messages sent to a name, for example from a script.

```
lightbulb listen [OPTIONS] --as <NAME>
```

Prints messages addressed to `<your member>/<name>` as they arrive, each with a `Reply:` line. Ctrl-C stops it. `<name>` takes letters, digits, `.`, `_` and `-` (1 to 64).

| Option | Meaning |
|---|---|
| `--as <name>` | The name to listen on. |
| `--once` | Return after the first message. |

### receipt

To see whether a message arrived.

```
lightbulb receipt [OPTIONS] <REQUEST_ID>
```

Prints the receipt of a message sent from this terminal. States: `queued`, `held`, `stored`, `delivered`, `read`, `refused`, `expired`.

| Option | Meaning |
|---|---|
| `--from <address>` | A message sent from another address (a session) instead of this terminal. |
| `--json` | Print the receipt as JSON. |

### held

To see what waits for your Allow.

```
lightbulb held
```

Lists join requests (`J<n>`) and held messages (`H<n>`), each with the command that allows it. (In 0.2.2 its `--help` says only "The held messages waiting for your Allow", but it lists join requests too.)

### approve

To allow a join request or a held message.

```
lightbulb approve [OPTIONS] <REQUEST>
```

`<REQUEST>` is `J<n>` or `H<n>` from `held`. Allowing a held message from a teammate's computer also lets that person's later messages through.

| Option | Meaning |
|---|---|
| `--always` | Only for messages from a cloud member's agents. It is not used between computers; on any other message it answers `--always is only for messages from a cloud member's agents.` |

### decline

To refuse a join request (`J<n>`) or a held message (`H<n>`).

```
lightbulb decline <REQUEST>
```

## Team

| Command | Usage | What it does |
|---|---|---|
| `team` | `lightbulb team` | Shows the team, its computers, their roles, and who is offline. |
| `admin` | `lightbulb admin [OPTIONS] <NAME>` | Founder only. Makes a member an admin. With `--founder`, `<NAME>` becomes the founder and you become an admin. |
| `remove` | `lightbulb remove <NAME>` | Founder and admins. Takes a computer out of the team. Every live invite in the team stops working; the answer has a new invite line. |
| `leave` | `lightbulb leave` | Asks `[y/N]` first. This computer is on its own network again. |
| `policy` | `lightbulb policy [PERSON] [VALUE]` | Who may message your sessions without asking. With no arguments, shows the policy; with a person and a value, sets it. `<PERSON>` is a teammate's name or short address, as `team` shows it. |
| `folder` | `lightbulb folder <PATH> <STATE>` | `off` hides the sessions under `<PATH>` from teammates; `on` shows them again. |

### relay

To see or change the relays (not listed in `lightbulb --help`).

```
lightbulb relay [OPTIONS] [URL]
```

With no arguments, shows the relays in use and where they come from. With a URL (`https://` only), sets yours; from the founder or an admin, the team's too. Relays carry encrypted traffic only, when no direct path exists.

| Option | Meaning |
|---|---|
| `--clear` | Clear the relay you set. |
| `--local` | Only this computer, not the team (founder and admins set the team's by default). |

### update

To move to the latest release (or a chosen one) without the install line.

```
lightbulb update [OPTIONS]
```

Downloads the release, checks its checksum, replaces the installed binary and runs the new one's `setup`, so the hooks, the tool server and the background service move with it. Already on that version: it says so and changes nothing. Versions before 0.2.2 cannot be installed. Homebrew and npm users upgrade with their package manager instead (see [Troubleshooting](/docs/agent-network-troubleshooting) if the version does not change afterwards).

| Option | Meaning |
|---|---|
| `--version <VERSION>` | The release to install, like `v0.2.2` (default: the latest). |

### version

```
lightbulb version
lightbulb --version
```

Prints the version, for example `lightbulb 0.2.2`.

## Commands the agents and macOS run

`hook`, `mcp` and `daemon` are not for typing. The agents run `lightbulb hook <agent> <event>` and `lightbulb mcp`; macOS's login service runs `lightbulb daemon`. `setup` wires them.

The full reference, with every command's own `--help` text, is [docs/cli.md](https://github.com/TheLightbulbCompany/cli/blob/main/docs/cli.md) in the lightbulb repository.
