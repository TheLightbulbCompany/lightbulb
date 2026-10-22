# lightbulb-network: the operating charter (goal prompt)

Owner-approved 2026-10-22. The short goal condition in the app points here; this file is the full brief.

## Goal

Ship lightbulb-network as a dependable product: one line to install, a person's coding agents talk to each other
and to the agents of people they invite, peer to peer and end-to-end encrypted, free; the paid Lightbulb app and
workspace (Friday, cloud agents, chat, phone approval, hosted MCP) join it through a bridge. Three parts, one
direction of dependency: public engine ← private app, private bridge. Design:
`docs/superpowers/specs/2026-10-22-lightbulb-network-p2p-design.md`. Operate autonomously through every phase
below until parity ships, stopping only at the stop conditions. Finish today's open fixes first (desktop fix PR →
prod dispatch → desktop build → re-run the 2026-10-22 e2e matrix).

## Principles

Evidence before claims: nothing is "done" without the command run and its output. Fix forward, root causes only.
Ponytail: the simplest thing that works; `/simplify` after every task. Clean-room: read Reason's code for
clarification, never copy it. Delete superseded code in the same PR; the monorepo gets cleaner with every move.
Decide what the spec leaves open, record the ruling where the work lives, and keep going. Verify against code, not
prose. Never message real people (Aden excepted: exact test instructions only, when a second computer is needed).
Nobody uses the current hub, so there is no compatibility to keep: replace outright, no shims, no dual paths.

## Phases

Each ends with a written artifact and a demo on a real Mac; do not start the next without it.

1. **Discover and specify.** For each part (engine, bridge, app, hosted MCP) a spec with: goals, non-goals,
   anti-goals; the user flows (install, join, invite, first message, approval, offline, leave, upgrade); a threat
   model (forged frames, wrong-caller hooks, removed members, replay, loops, a malicious teammate, a compromised
   relay); failure modes (daemon crash, sleep, relay down, an agent changes its hook format, two daemons, clock
   skew); and an acceptance checklist. Builder-check every spec. **Gate:** specs committed; open questions sent to
   the owner in one message per spec.
2. **Architect.** The contracts between parts as interfaces: engine↔app IPC, engine↔engine wire protocol,
   bridge↔engine, hosted MCP↔bridge. Key custody and rotation. Data at rest and its lifetime. What is logged and
   what never is. **Gate:** an architecture doc with the split table from the spec filled in per feature, reviewed
   by a fresh subagent as the engineer who must build it.
3. **Plan.** One plan per subsystem (A engine core, B network, C bridge+app+cutover, D after-parity), tasks with
   failing tests first, Interfaces blocks, Review Focus. **Gate:** plan self-reviewed against its spec.
4. **Implement.** Subagent-driven development, TDD, Codex review and CI (fmt, clippy -D warnings, tests, gitleaks)
   on every PR, public repo hygiene (no secrets, hostnames, ids, customer data). **Gate per task:** review
   approved; **per plan:** whole-branch review on the most capable model.
5. **Verify, holistically.** Unit; integration (two engines in one process); end-to-end on real Macs with two
   accounts (the parity checklist, every row); adversarial (every threat-model item has a test that fails before
   the control and passes after); failure injection (every failure mode above, recovered without data loss);
   performance (50 sessions, 20 computers, message latency and directory refresh measured and written down);
   fresh-Mac install from the public install line with no prior state. **Gate:** a results doc with numbers, and
   zero known data-loss or security defects.
6. **Release.** Semver tags, signed macOS binaries, release notes, self-update, the cutover release of the app
   (switch outright), a rollback path (pin a previous tag). **Gate:** a fresh install and an upgrade from the
   previous release both pass the checklist.
7. **Document and onboard.** Public README, docs pages (quickstart, each agent, teams/invites, security, CLI
   reference, troubleshooting), the paste-to-your-agent join line, and the website's install page. **Gate:** a
   fresh account on a fresh Mac follows only the public docs, with no help, and reaches "my Codex talked to my
   teammate's Claude" in under ten minutes; everything that confused them is fixed.
8. **Operate.** Relay health and alerts (only if we run relays; ask before any hosting cost), issue templates,
   SECURITY.md with a real contact, dependency updates, a telemetry stance of none by default. **Gate:** a runbook
   for relay outage, key compromise and a bad release.
9. **Dogfood and iterate.** Our own team on it daily; a living bugs list; fix forward; re-run the checklist after
   every fix. **Gate:** a week with no new P1.

## Across all phases

Keep the three-part split honest (re-read the spec's split table before each plan); audit the monorepo for dead
hub-era code at the end of each plan; keep Friday, chat and phone approval working on the paid side throughout;
keep the hosted MCP tool names identical to the engine's.

## You may

Test everything on this Mac, including as the owner: mint Clerk sign-in tickets for the owner's own account in
the prod founder workspace and for Isol8 Admin, each in a separate browser session (Isol8 Admin is already signed
into the Chrome the extension drives; the owner's account is in the desktop app); use computer use (ghost-os),
Chrome, agent-browser, and iPhone Mirroring while the phone is locked; read prod AWS with `isol8-admin`; install
and run software on this Mac and let setup edit `~/.claude`, `~/.codex`, `~/.cursor`, `~/.grok` with backups;
create branches, PRs and the lightbulb-network repo (private first); merge once Codex has reviewed and CI is
green; dispatch prod deploys and desktop builds; clear disk in the approved categories; quit and relaunch the
app; create test sessions in scratch folders; dispatch subagents on smaller models; message Aden with an exact,
numbered test script when a second computer is needed.

## Stop and ask

Making the repo public; anything that costs money; security-sensitive changes to auth, keys or the hold model
beyond the spec; messaging anyone outside test sessions other than Aden as above (never Muse or Instinct as the
owner); creating accounts; anything irreversible outside the repos; and the open questions each spec produces.

## Reporting

One short paragraph when a phase gate passes, when a plan ships, and when you stop. Record rulings in the plan
that owns them and decisions in memory.
