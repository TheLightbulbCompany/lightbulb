# Skills

<!--
  MAINTENANCE (Lightbulb engineers): anchored to code.
  - CLI + refs ......... fork src/cli/skills-cli.ts (install command: <skill-ref>, --agent, --global, --as)
  - Install target ..... fork src/skills/lifecycle/archive-install.ts (<workspaceDir>/skills)
  - Sources ............ fork src/config/types.skills.ts (maxSkillsLoadedPerSource: bundled/managed/workspace/extra)
  - Per-agent scoping .. fork src/config/types.agents.ts (AgentEntry.skills) + src/skills/discovery/agent-filter.ts
  - RPC tiers .......... fork src/gateway/methods/core-descriptors.ts (skills.install / skills.upload.* = operator.admin)
  - Owner UI ........... apps/frontend/src/components/control/panels/SkillsTab.tsx (skills.status / skills.update)
  - The Paperclip gap .. paperclip fork server/src/routes/agents.ts (buildUnsupportedSkillSnapshot; adapter.listSkills
                         exists for claude/codex/cursor/gemini/grok/opencode/pi — NOT openclaw_gateway)
-->

## Skills are OpenClaw's, and they live in this container

A skill is a folder with a `SKILL.md`, on disk here. Nothing fetches them at
answer time. OpenClaw loads them from four sources:

| Source | Where | Who writes it |
|---|---|---|
| **bundled** | `/opt/hermes-skills/` (`lightbulb`, `paperclip`, …) | baked into the image — you cannot change these |
| **managed** | `~/.openclaw/skills/` | shared across every agent in this container |
| **workspace** | `<agent workspace>/skills/` | per-agent — this is the one you install into |
| **extra** | `skills.load.extraDirs` | rarely used |

## DO NOT use Paperclip's company-skills library for an agent here

`/opt/hermes-skills/paperclip/references/company-skills.md` documents a company skill
library with `skills install` / `skills agent sync`. **That path does not work
on Lightbulb.** Paperclip syncs skills into a harness directory for adapters
that implement it — claude, codex, cursor, gemini, grok, opencode, pi. Every
Lightbulb agent uses the `openclaw_gateway` adapter, which does not, so the
sync endpoint answers:

```json
{"adapterType":"openclaw_gateway","supported":false,"mode":"unsupported",
 "warnings":["This adapter does not implement skill sync yet."]}
```

That is not a bug to report or retry, and it is not a reason to tell the owner
their request is impossible. It means you were on the wrong plane: Paperclip's
library exists to push skills into OTHER harnesses. Yours are already in the
same container you run in. Use the CLI below.

## Installing a skill

```bash
openclaw skills install <skill-ref> --agent <agent-id>
```

`<skill-ref>` is one of:

| Form | Example |
|---|---|
| ClawHub | `@owner/weather` |
| skills.sh | `skills-sh:owner/repo/weather` |
| Git repo | `git:https://github.com/owner/repo` |
| Local directory | `/path/to/skill-dir` |

Useful flags: `--agent <id>` targets that agent's workspace (omit and it infers
from cwd, then the default agent); `--global` installs into the shared managed
directory for every agent instead; `--as <slug>` names a git/local skill;
`--version <v>` pins one.

A repo of many skills is many installs — one per skill folder. Prefer a
focused subset over everything a repo contains: every loaded skill costs
prompt space, and `maxSkillsInPrompt` / `maxSkillsPromptChars` will silently
drop the overflow.

## Scoping a skill to one agent

Installing into an agent's workspace already scopes it. To restrict which
skills an agent loads at all, `agents.entries.<agentId>.skills` is an
allowlist — an explicit list REPLACES `agents.defaults.skills` rather than
merging with it, so a list of one means that agent loads exactly one skill.

## What you cannot do

- **`skills.install` and `skills.upload.*` are gateway RPCs at
  `operator.admin`.** You are not an operator; those are the owner's Skills
  tab, not your door. There is no install-from-URL RPC. Your door is the CLI
  above, through `terminal`.
- **You cannot install into `/opt/hermes-skills/`** — it is baked into the image.
  Changing a bundled skill takes an image rebuild by Lightbulb engineers.

## Telling the owner

The owner's own view is the **Skills tab** (Control Panel, flickmeon.ai/chat):
it lists what is installed, what each skill needs, and lets them enable or
disable one. Skills you install appear there. Skill *Proposals* — the ones you
author yourself from repeated corrections — are a different tab; see
`references/marketplace.md`.
