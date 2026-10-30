---
title: "Memory on your computers and in your workspace"
description: "What the free network remembers and where it keeps it, what changes when a team adds the cloud workspace, and how to approve, forget and wipe."
order: 8.5
section: "Agent network"
---

**On the free network, memory lives on your computers and moves only between the members of your network. When a team adds the cloud workspace, team entries also go to the workspace, and the facts the workspace learns come back down to its members' computers. Your personal entries never leave your own computers.**

## On the free network

Agents save a fact with the `remember` tool and read it back with `recall`. Each fact is an entry signed by its author. It records the agent, repository and session it came from, and when.

- **Where it is kept.** In `state.db` in `~/.lightbulb/connect/` on each computer, a folder only your user can read. The search index is built on the computer and never leaves it.
- **Who gets it.** A `team` entry syncs to the computers on your network. A `me` entry syncs only to your own computers, signed in as you. `lightbulb memory team off` stops this computer writing team entries.
- **Secrets.** A fact that matches a secret pattern (API keys, tokens, private-key blocks, credentials in URLs) is refused with the reason, both when an agent writes it and when it arrives from another computer.
- **Your sessions.** The background service also records your coding sessions on this computer: each session, each tool call with a 200-character summary, and your prompts and the agents' replies, each cut to 4 KB. `recall` searches them too. Prompts and replies are never uploaded and are deleted after 90 days. `lightbulb sessions turns off` stops recording them and stops `recall` using the ones already stored.
- **What leaves the computer.** With uploads off, which is the default, memory makes one request of its own: setup downloads the memory model (about 31 MB) once. `lightbulb setup --no-model` skips it, and search is then keyword-only.

`lightbulb memory list` shows what is remembered, and `lightbulb memory last` shows the memory block the last session was handed at its start.

## When your team adds the workspace

A workspace admin adds the workspace's agents to the network:

```sh
lightbulb login            # sign this computer in to the workspace
lightbulb invite --agents  # the workspace joins this network
```

The workspace joins as one always-on member. For memory, three things change.

- **Team entries go up.** Every `team` entry on your computers syncs to the workspace member, which mirrors it into the workspace memory so the workspace's agents can recall it.
- **Personal entries never do.** A `me` entry goes only to a computer signed in as you, and the workspace member is not one.
- **What the workspace learns comes down.** The workspace memory extracts facts from the conversations in its rooms. Each new or changed fact becomes a `team` entry on the workspace member and syncs to the computers of the workspace's members. A computer on your network whose person is not a member of the workspace does not receive them.

Workspace memory is processed and stored in AWS (Bedrock AgentCore) in our AWS account, under a namespace that belongs to that workspace alone. Before a room turn is stored, secret-shaped text is scrubbed out of it.

### The first sync

The first time the workspace syncs, every fact it has already extracted becomes a team entry at once, and each member's computer receives all of them on its next sync. A workspace that has been running for a while can hold thousands of facts, so expect thousands of new team entries on each computer. After that, only new and changed facts come down. `lightbulb memory list --team` shows them.

## Uploads and lessons

Nothing from a session is uploaded unless you choose it. `lightbulb login` asks once, and `lightbulb sessions upload off|metadata|full` changes it, from your own terminal only (an agent's shell is refused).

- `metadata` sends, for each finished session, the agent, repository, branch, times and token counts, and for each tool call its tool, time, duration and whether it worked.
- `full` also sends each tool call's summary.
- Neither sends your prompts, the agents' replies, file contents, diffs or command output.

Uploads are signed by your computer, sent only while it is signed in, and only for sessions that finish after you turn them on. We keep them for 30 days on a free account and a year in a workspace, in our AWS account.

Full uploads are also read by the evaluator, which runs in AWS (Bedrock AgentCore) in our account and looks for patterns across your own sessions, never anyone else's. On a free account it reads up to 50 sessions a month. Its suggestions are drafts. Nothing is remembered until you approve one:

```sh
lightbulb memory drafts               # the suggested lessons
lightbulb memory approve <ID>         # keep one for yourself
lightbulb memory approve <ID> --team  # share it with the team
lightbulb memory dismiss <ID>         # throw one away
```

Signed in on your own, `approve` makes a `me` entry on this computer, and `--team` makes a `team` entry. In a workspace, `approve` keeps the lesson in your own part of the workspace memory, which agents read in your direct messages with them, and `--team` shares it with the whole workspace. A lesson that matches a secret pattern is refused.

## Forgetting, leaving and wiping

- `lightbulb memory forget <ID>` deletes an entry on every computer that holds it, as each comes online, and in the workspace memory. A computer that stays offline forever keeps what it held before the forget.
- `lightbulb sessions forget <ID>` deletes a captured session, with its prompts and replies, from this computer.
- `lightbulb leave` takes this computer out of the network. The entries already on it stay until you forget them.
- When a person leaves the workspace, their own part of the workspace memory is deleted at once, with the sessions they uploaded and the drafts they were given in that workspace. Team memory belongs to the workspace and stays.
- A workspace wipe deletes the workspace memory (team and every member's own part), its uploaded sessions and the evaluator's drafts. Team entries already synced to members' computers stay there until they are forgotten.
- Deleting your Lightbulb account deletes your uploaded sessions, your drafts and what the evaluator drew from your sessions.

What protects the network itself is on [security and privacy](/docs/agent-network-security). What we do with your data is on [what happens to your data](/security).
