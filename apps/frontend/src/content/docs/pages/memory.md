---
title: "Memory on your computers"
description: "What the network remembers and where it keeps it, how to share and forget."
order: 4
section: "Agent network"
---

**On the free network, memory lives on your computers and moves only between the members of your network. Your personal entries never leave your own computers.**

## On the free network

Agents save a fact with the `remember` tool and read it back with `recall`. Each fact is an entry signed by its author. It records the agent, repository and session it came from, and when.

- **Where it is kept.** In `state.db` in `~/.lightbulb/connect/` on each computer, a folder only your user can read. The search index is built on the computer and never leaves it.
- **Who gets it.** A `team` entry syncs to the computers on your network. A `me` entry stays on the computer that made it; `lightbulb memory share <ID>` turns one into a team entry. `lightbulb memory team off` stops this computer writing team entries.
- **Secrets.** A fact that matches a secret pattern (API keys, tokens, private-key blocks, credentials in URLs) is refused with the reason, both when an agent writes it and when it arrives from another computer.
- **Your sessions.** The background service also records your coding sessions on this computer: each session, each tool call with a 200-character summary, and your prompts and the agents' replies, each cut to 4 KB. `recall` searches them too. Prompts and replies are never uploaded and are deleted after 90 days. `lightbulb sessions turns off` stops recording them and stops `recall` using the ones already stored.
- **What leaves the computer.** Nothing is uploaded. Memory makes one request of its own: setup downloads the memory model (about 31 MB) once. `lightbulb setup --no-model` skips it, and search is then keyword-only.

`lightbulb memory list` shows what is remembered, and `lightbulb memory last` shows the memory block the last session was handed at its start.

## Forgetting, leaving and wiping

- `lightbulb memory forget <ID>` deletes an entry on every computer that holds it, as each comes online. A computer that stays offline forever keeps what it held before the forget.
- `lightbulb sessions forget <ID>` deletes a captured session, with its prompts and replies, from this computer.
- `lightbulb leave` takes this computer out of the network. The entries already on it stay until you forget them.

What protects the network itself is on [security and privacy](/docs/agent-network-security).
