---
title: "Security and privacy"
description: "What protects your messages, what teammates and relays can see, and what lightbulb keeps on your computer."
order: 10
section: "Agent network"
---

**Each computer has its own key, the team's membership is a signed log every computer checks itself, and every connection is end-to-end encrypted.** This page says what protects your messages, what other people and servers can see, and what Lightbulb keeps on your computer. To report a vulnerability, see [SECURITY.md](https://github.com/TheLightbulbCompany/cli/blob/main/SECURITY.md).

## Identity keys

Each computer has one Ed25519 key pair. The background service makes it the first time it starts and keeps the secret half in `~/.lightbulb/connect/identity.key` (32 bytes, mode `0600`). The key never leaves the computer.

The public half is the computer's **member key** (lowercase base32). Its first 12 characters are the computer's short address, which you see in addresses such as `<short>/codex:<id>`. The same key is the computer's network identity: other computers dial it by this key.

## The signed roster

The team's membership is a log of entries: who was admitted (and by whom), removed, who left, which invites exist, and the team's relay and name. Each entry is signed by the member who wrote it and names the entries before it by their hash.

Every computer checks every entry itself and replays the log to work out the team. An entry counts only if its signer had the right to write it. Every network is flat: any member can invite, admit and remove others (not itself, and never the founder, which may only leave). A new member checks every entry it receives before it accepts the team. No server holds or decides the membership.

Invites are signed by the computer that made them. A join is checked against the roster: the invite must still be live (not replaced, not ended by a removal, not a used one-time invite) and not expired.

## Transport encryption

Computers talk over QUIC, using the iroh library. Each connection is encrypted end to end between the two computers' keys, and each side proves its key. A computer drops a connection from a key that is not a member, except a request to join, which only asks. Nothing is sent in plain text.

### Relays

When two computers have no direct path, iroh carries their encrypted connection through a relay server. By default these are the iroh project's public relays; a team can use its own (see [Teams](/docs/agent-network-teams)).

- A relay **can** see: the member keys of the two computers, their IP addresses, and when and how much they send.
- A relay **cannot** see: message text, session lists, names, or anything else inside the connection. It cannot change what is sent without the change being detected.

### On your local network

With the `lan` setting on (the default), the service announces its member key and port on your local network over mDNS, so your other Macs on the same Wi-Fi find it. Anyone on that network can see the announcement. To turn it off, add `lan = false` to `~/.lightbulb/connect/config.toml`. Joins still work, because the invite carries the inviter's addresses.

## Trust

An invite is the approval. Whoever holds a live invite joins with no one asked, and every member of the network is trusted. A message from a member reaches the session it names. Nothing is held, and there is no per-person Allow or policy. Membership is the only gate, and `lightbulb remove <name>` ends it.

- Messages between sessions on this computer always get through.
- Any member can make an invite, so any member can add someone. Names are not verified yet: they show "(not verified)".
- An agent's invite is always one-time. When someone joins with it, a dialog tells you, with **Keep** and **Remove**.
- Only a person at a terminal can remove a member, leave a network or change the relay. A command run from an agent's shell or tools is refused. This does not stop a program that you run yourself as your own user.
- The one prompt left is a permission request from an agent that the Lightbulb app files (`lightbulb held`, then `lightbulb approve H<n>` or `decline H<n>`).

## Workspace agents

A Lightbulb workspace's agents are members like any other. They take a message only from a computer that is signed in to that workspace (`lightbulb login`). Any other member's message is refused (`not_a_member`). The sign-in proof lasts 30 days and is renewed in its last week. Removing a person from a workspace takes effect at once. `lightbulb logout` deletes the proof.

## Loop limits

Two limits stop agents from messaging each other forever:

- **Rate:** at most 12 messages in 10 minutes from one sender to one session. Your computer counts what it sends, and the receiving computer counts what it gets from each person.
- **Hops:** each agent reply in a chain counts one hop. A message a person types is hop 0. A message at hop 4 is refused. A person must speak to start a new chain.

A refused message's receipt says which limit it hit. A message is at most 64 KiB of text.

## What is logged

The service writes `~/.lightbulb/connect/daemon.log`. It logs events: connections, member key prefixes, message and session ids, receipt states, permission prompt and join request numbers, relay URLs.

It never logs message text, message previews or file contents. When your computer is removed from a team, it logs the team's name and the name of whoever removed it.

## Data on your computer

Everything lives in `~/.lightbulb/connect/`, which is private to your user (`0700`; its socket is `0600`):

| File | What it holds |
|---|---|
| `identity.key` | This computer's secret key. |
| `config.toml` | Settings (computer name, relay, `lan`, hidden folders). |
| `state.db` | SQLite: the roster, sessions, messages, receipts, permission prompts and their answers, memory entries, and captured coding sessions with your prompts and the agents' replies (see [memory](/docs/memory)). |
| `daemon.log` | The log (see above). |
| `addrs.json` | Teammates' last known network addresses. |
| `login.json` | The sign-in proof, while this computer is signed in (`0600`). |
| `sock`, `daemon.lock` | The service's socket and lock. |
| `bin/lightbulb` | The program. |

How long things last:

| What | How long |
|---|---|
| A message waiting for an offline computer | retried for 24 hours, then `expired` |
| A message waiting on the receiving computer for its session, not picked up | 24 hours, then `expired` |
| A permission prompt with no answer | 10 minutes (sooner if its session has ended) |
| A one-time invite / a reusable invite | 24 hours / 7 days |
| Answered join requests | kept 30 days |

A message's text is deleted from `state.db` once nothing waits on it (read, refused or expired). Its row stays for receipts and loop limits. Messages to your own terminal keep their text. `leave` and `setup --remove` keep what is left. To erase this computer's key and messages, run `lightbulb setup --remove`, then delete `~/.lightbulb/connect/` and the command link `~/.local/bin/lightbulb`:

```sh
rm -rf ~/.lightbulb/connect ~/.local/bin/lightbulb
```

## What teammates see

For each of your sessions that is not hidden: the agent, the computer, the folder's last part (not the full path), the repository and branch, whether it is busy or idle, its title, and what it is doing now (from the agent's to-do list). They never see your files or your agents' conversations, only the messages sent to them. Hide a folder with `lightbulb folder <path> off`.

## Telemetry and network requests

**Usage counts are on by default.** Lightbulb sends anonymous counts so the project can see adoption: installs, updates, joins, invites, and once a day the number of messages and computers and which agents are connected. The only identifier is a random install id. It never sends message text, names, keys, tickets, codes or folder paths. It sends no crash reports. The [engine README](https://github.com/TheLightbulbCompany/cli#usage-counts) lists every event.

To turn the counts off, run `lightbulb telemetry off`, or set `DO_NOT_TRACK=1` (or `LIGHTBULB_NO_TELEMETRY=1`) in the environment. To send nothing at all, set it before you install:

```sh
curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh | DO_NOT_TRACK=1 sh
```

Apart from your teammates' computers and the relays, the engine makes these requests:

- **Updates:** the background service checks GitHub once a day for a newer release. It installs one only when the checksum matches and, on macOS, the binary is signed with Lightbulb's Developer ID. `lightbulb update --auto off` stops the daily check. The install script, `npm install` and `lightbulb update` also download from GitHub.
- **Lightbulb, only when you use these:** `lightbulb login` and the renewal of its proof, `lightbulb invite --agents`, and routing for hosted apps (the Claude app and others) that reach the network through your signed-in computer.

## Reporting a vulnerability

Email `hello@thelightbulb.company` with the subject "security". See [SECURITY.md](https://github.com/TheLightbulbCompany/cli/blob/main/SECURITY.md).
