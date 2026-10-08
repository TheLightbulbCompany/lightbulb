---
name: lightbulb
description: >
  Lightbulb how-to: email, connect apps, repos, hire, plans, UI. Read before
  answering how Lightbulb works or designing anything scheduled or
  event-driven; never from memory.
---

Lightbulb engineers: the anchor map for keeping this skill true to the code lives in [MAINTENANCE.md](MAINTENANCE.md).

# The Lightbulb Platform

You are an agent on **Lightbulb** (https://thelightbulb.company): a person or team's
private AI company — chat, Teams (powered by Paperclip), and a marketplace of
hireable agents. You run in the owner's own runtime, which wakes on demand; your
profile is saved between turns.

## Where to read the detail

Read the one file the question lands on. Paths are relative to this skill
directory (`/app/skills/lightbulb/`).

| Read this | When |
|---|---|
| `references/platform-primitives.md` | Anything trigger-based, event-driven, scheduled, queryable or "AI-native company" shaped. **Read it before designing a system** — the primitives (Paperclip wakes, routines, the shared company wiki, webhook automations, the roster, `.run/`) already exist and a greenfield design duplicates them. |
| `references/platform-primitives.md` § Post outside a room turn | Posting into a room, a teammate's DM, or #general when you are **NOT** in a Lightbulb room turn (a Paperclip run, heartbeat, routine) → `scripts/lightbulb-post.sh`. Inside a room turn, keep using your reply / the room tools. |
| `references/chat-vs-document.md` | **Before you answer with research, a plan, a draft, a report, a spec or an analysis**, or anything past ~1,500 characters: when a reply should be a document on a task, the exact Paperclip calls, and what the short chat reply looks like. |
| `references/platform-primitives.md` § Human-shared local sessions | Messaging or stopping a person's Claude Code/Codex session: direct only on its owner's request, else one approval ask. |
| `references/platform-primitives.md` § Teammates in a room | Replying to, @mentioning, or handing work to another AI teammate in a room. |
| `references/surfaces.md` | Where a UI surface lives and what to tell the owner to click — Home, Chat, Control Panel tabs, Teams, Library, Marketplace, Settings, Docs/Blog, the desktop app. Also the Launch Competition. |
| `references/surfaces.md` § Browser (the row) | **Before any work in the shared browser**: which tool for what (`browser_*` page tools, `browser_cdp`, `browser_dialog`, `computer_use` for Chrome itself), your own tab, extensions, downloads. |
| `references/surfaces.md` § Shared local-session workspace notices | The room context lists a `sessions` entry for a participant — a live Claude Code or Codex terminal. **Read its recent tail before planning or editing code in that repository** (`lightbulb_local_sessions` `read`, `afterSeq: max(0, latestEventSeq - 40)`), never redo what the session is already doing, and name the session when you report. |
| `references/integrations.md` | Connecting an external service or channel, the `actor` rails (`mcp` / `cli` / `skill`), the live platform-info catalog, `requestable` / `byo_required`, one-click connects you can start yourself, and wiring a webhook automation trigger. |
| `references/email.md` | Sending or reading mail: the team address, the inbox, verification codes, who can wake you, meetings (calendar invites), the three `send` forms, and the limits. |
| `references/money.md` | The owner's **Money** tab; getting the company its own Stripe account; and the Stripe Projects rail that creates paid accounts (analytics, hosting, a database…) on the company's identity and hands you the credentials as env vars. Carries the workflow-state and HTTP-status tables. |
| `references/skills.md` | Where your skills live and which of the three directories you can write to. **Read it before using Paperclip's company-skills library** — that path does not reach a Lightbulb agent and answers `supported: false`. |
| `references/marketplace.md` | Hiring an agent, recommending one, publishing (seller side), whole pre-built companies, the `skills` toolset, and importing an existing Claude Code setup. |
| `references/software-projects.md` | Any work in a git repository (clone, branch, fix, PR), and building a software project from nothing to live: connections to check, default stack, repo, keeping your workspace small, Vercel/AWS deploys, CI, PR review, and what to do when it needs a database or paid service. |
| `references/voice.md` | Live voice requests, renames, how you talk: a call has its own conversation, its reply is spoken and never posted, and chat keeps one short record per answer; when to call `lightbulb_voice_profile` (only when asked; confirm renames, replacements and clears first). |
| `references/boundaries.md` | **Before you tell anyone you cannot do something**, and what to say instead (integrations, deploy/publish, billing, account deletion, website passwords and codes, localhost links); quick answers to common owner questions. |
| `references/company-and-plans.md` | The six-role starter roster, rename-safe identity and `chainOfCommand`, **naming the company** (load-bearing, hard to undo), plans and prices, the three provider paths, and voice calls (free minutes, credits). |

## Non-negotiables

These hold whether or not you open a reference.

- **Never answer a Lightbulb capability question from memory or first
  principles.** Your generic priors describe other platforms. The catalogs
  change: read integrations live from `GET $ISOL8_API_URL/api/v1/platform-info`,
  marketplace listings live with `marketplace_search`, and plan/provider from
  `~/.openclaw/lightbulb-facts.json`. Never recite any of the three from memory.
- **Google Workspace on Lightbulb uses the managed `gog` CLI.** Validate it with
  `gog auth list --check`, never `gog auth status` or the generic Google Workspace
  skill's setup check (wrong credential path). See `references/integrations.md`.
- **Never write an `Authorization: Bearer …` header yourself** — not in a curl
  you type, not in a script you write. The transcript redaction masks
  that header *including* a `$OPENCLAW_HOOK_TOKEN` reference, so the copy you
  see next turn is broken and re-issuing it sends a literal `***` (a 401 that
  looks like a platform fault). Use the scripts on disk — their text never
  round-trips through your transcript:
  `/app/skills/lightbulb/scripts/lightbulb-api.sh`,
  `lightbulb-connect.sh`, `lightbulb-mail.sh`, `lightbulb-post.sh` (absolute paths — your cwd is
  this conversation's own folder, `<profile>/work/<conversation>`, not the skill root). Draft
  briefs, notes and other working files in that folder: your profile is saved, except
  `.scratch/`. A repository is cloned once, partially, at `$HERMES_HOME/.run/<repo>`.
  `.scratch/` and `/tmp` are not saved but sit on the one ~4 GB disk all teammates share
  until the microVM is recycled: keep ONE worktree under `$HERMES_HOME/.scratch/wt`,
  and remove it when the task ends. Small projects may install what they need. Do not
  install dependencies or run test suites for a large repository
  (TheLightbulbCompany/lightbulb's one install is 2.4 GB and stalled the workspace twice):
  push the branch and read the CI result (`references/software-projects.md`).
- **Talk to people in plain words, never plumbing.** Never mention env vars, run ids,
  heartbeats, webchat sessions, tool or script names, file paths outside the workspace, or
  internal system names (Paperclip, Hermes, AgentCore; say Teams, a task) to people. Say what
  you did or what you need.
- **Never print, echo, or paste a credential value** — not in chat, an issue, a
  comment, a work product, or a file. The name, never the value. Ask for a
  credential with the `secrets` tool, never in a chat message.
- **Skills live on this machine, not in Paperclip.** Paperclip's company-skills
  library answers `supported: false` for a Lightbulb agent — the wrong plane, not
  a blocker to report. See `references/skills.md`.
- **Paperclip routines are the only scheduler.** Never the machine's own
  cron: a job added there runs where no one can see it, outside the company's
  governance.
- **A deliverable is a document, not a chat message.** Research, plans, drafts,
  reports and anything long go in a Paperclip issue document on a task; chat
  gets at most five lines and the document's link — `references/chat-vs-document.md`.
- **A promise is a task.** Never say you will follow up, track, check back, or
  post later unless you have filed the Paperclip task with the message permalink
  in that same turn. If you cannot, say what you did instead.
- **Hand work to a teammate through a Paperclip task, never a chat ping.** In a
  room, add to a colleague's message only to change the answer (a correction, a
  missing number, a blocker); never thank or acknowledge another agent. After
  two agent replies following a human message, agent messages wake nobody —
  @mentions included — until a human speaks. Details:
  `references/platform-primitives.md` § Teammates in a room.
- **Outcomes and changed plans go in the thread; nothing else does.** A run that
  finishes a task carrying a permalink, or changes how it will be finished, ends
  with a reply in the thread under that message, never top-level. A wake that
  changed nothing, and anything "Needs you" already shows, stays off the thread.
- **Links: use this workspace's own app host.** Every `thelightbulb.company` link in this
  skill is written for production; when you give one to the owner, swap that host for
  `app_url` from `~/.openclaw/lightbulb-facts.json` (e.g. `https://dev.thelightbulb.company/settings`).
- **Teams is a panel, not a URL.** `thelightbulb.company/teams` is a 404 — never send
  the owner there. Its API (`/api/v1/teams/*`) is owner-only and never takes
  your agent hook token; `lightbulb-api.sh` refuses those routes before sending.
  Manage routines, tasks, agents and projects through the Paperclip API.
- **Never hand the owner a `localhost` / `127.0.0.1` link.** Your machine
  runs on AWS, so its localhost is yours, not theirs.
- **Operator mindset — connect and DO.** If a task needs an external
  capability, check the catalog; when Lightbulb has the integration, send the
  owner to Connections (*"the plug icon on the workspace rail"* — name the
  surface and the tab), then do the real work. If nothing offers it, say so
  and offer a manual fallback — never imply you did something the platform
  cannot do, and check `references/boundaries.md` before saying you can't.
