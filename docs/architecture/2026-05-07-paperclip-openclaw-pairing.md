# Paperclip ↔ OpenClaw Agent Pairing

**Date:** 2026-05-07
**Status:** Implemented (PR landing on branch `feat/paperclip-openclaw-agent-pairing`)
**Implementation plan:** `docs/superpowers/plans/2026-05-07-paperclip-openclaw-agent-pairing.md`

---

## Problem statement

Before this change, OpenClaw agents and Paperclip agents were two disjoint
populations of records that happened to share a vocabulary:

- The `/chat` UI managed OpenClaw agents — `agents.list[]` in the user's
  `openclaw.json`, with workspace files at `<EFS>/users/{owner_id}/agents/<openclaw_uuid>/`.
- The `/teams` Inbox managed Paperclip agents — rows in Aurora keyed by a
  Paperclip-generated UUID, with no awareness of any OpenClaw container.

When a `/teams` issue was assigned to a Paperclip agent, Paperclip's
`openclaw_gateway` adapter sent a `req agent` frame keyed by the Paperclip
UUID. OpenClaw didn't recognize that UUID, fell through to its hardcoded
literal `"main"` agent, and the run landed in `agents/main/agent/`. From
the user's point of view: "Friday took a ticket, but her workspace is
empty and someone else's transcripts keep showing up." Issue runs and
chat runs lived in the same `main` directory, so contexts bled.

The fix has two pieces:

1. Make Paperclip's UUID the OpenClaw agentId, so issue routing lands in
   the right workspace.
2. Switch Paperclip's adapter `sessionKeyStrategy` from `"fixed"` to
   `"issue"` so each issue gets its own OpenClaw session.

---

## Why Paperclip is the ID authority

Paperclip's `createAgentSchema` (upstream
`paperclip/packages/api/src/schemas/agents.ts`) does **not** accept a
client-supplied `id` field. The server generates the UUID on insert. We
explored the alternative — making OpenClaw mint the ID and pushing it
into Paperclip — and rejected it because:

- It would require an upstream Paperclip API change we don't own.
- Paperclip already has the durable record (Aurora row, FK constraints
  to channels/threads/issues). OpenClaw's `agents.list[]` is a config
  artefact derived from that record, not the other way around.

So the policy is: Paperclip mints the UUID, then we forward it to
OpenClaw as the `name` parameter on `agents.create`. OpenClaw's
`normalizeAgentId` (upstream `openclaw/src/agents/agent-paths.ts`)
derives `agentId` from `name` — slugifying it but preserving the UUID
shape. We confirmed in tests that a Paperclip UUID (lowercase,
`8-4-4-4-12` hex with hyphens) survives normalization unchanged.

---

## The seven consistency surfaces

The Paperclip UUID appears literally — same string — in seven places:

1. **`agents.list[].id` in `openclaw.json`** — written by
   `_build_agents_list` in `apps/backend/core/containers/config.py:438`,
   one entry per Paperclip agent.
2. **Workspace dir on EFS:** `<EFS>/users/{owner_id}/workspaces/<paperclip_uuid>/` —
   created lazily by the OpenClaw container on first agent run.
3. **Agent dir on EFS:** `<EFS>/users/{owner_id}/agents/<paperclip_uuid>/` —
   houses the agent's `agent/` subdirectory (config, memory, transcripts).
4. **Session transcripts:** `agents/<paperclip_uuid>/agent/sessions/<sessionKey>.json`
   — OpenClaw scopes session files under the agent's `agentId`.
5. **Identity file:** `agents/<paperclip_uuid>/agent/identity.json` —
   OpenClaw runtime-managed (we deep-merge around it via the
   config-patcher).
6. **Auth-profiles.json (container-wide fallback):** lives at
   `agents/main/agent/auth-profiles.json` and intentionally does **not**
   match the paired UUID. See "Why we left `agents/main/` alone" below.
7. **Session-key prefix:** `agent:<paperclip_uuid>:<member_user_id>` for
   `/chat` runs (`apps/backend/routers/websocket_chat.py:881`) and
   `agent:<paperclip_uuid>:paperclip:issue:<issue_id>` for `/teams`
   issue runs (via `synthesize_openclaw_adapter`'s
   `sessionKeyStrategy="issue"`).

If any one of these uses a different string than the others, runs route
to the wrong directory or session, and inheritance breaks. Future
changes that touch agent identity must keep all seven aligned.

---

## Runtime flow

### Org sign-up (Paperclip seed)

1. Clerk fires `organization.created` → backend webhook.
2. `provision_org` in `apps/backend/core/services/paperclip_provisioning.py:120`
   creates the Paperclip company and seeds a single agent named
   "Friday" with `role="ceo"`. Paperclip mints the UUID.
3. The org's owner record in DDB now has a Paperclip company, but no
   OpenClaw container yet. That's fine — the container is provisioned
   on first `/chat` open.

### First `/chat` open (container provision)

1. User loads `/chat` → frontend triggers REST provision.
2. `ecs_manager.write_user_configs`
   (`apps/backend/core/containers/ecs_manager.py:1502`) calls
   `fetch_paperclip_agents_for_owner` from
   `apps/backend/core/services/paperclip_openclaw_pairing.py:64`.
3. The helper resolves the Paperclip company for the owner, calls
   `paperclip_admin_client.list_agents`, returns the live agent list.
4. `build_openclaw_config_dict`
   (`apps/backend/core/containers/config.py:481`) takes that list and
   writes one entry per Paperclip agent into `agents.list`. The CEO
   (Friday, post-seed) gets `default: true`. The literal `"main"` entry
   is gone from `agents.list`.
5. Container boots. OpenClaw reads `agents.list` and registers each
   Paperclip UUID as a known agentId.

### Issue assignment in `/teams`

1. User assigns an issue to Friday in `/teams`.
2. Paperclip's `openclaw_gateway` adapter (configured per-agent at
   create time by `synthesize_openclaw_adapter` in
   `apps/backend/core/services/paperclip_adapter_config.py`) sends a
   `req agent` frame to the user's container.
3. The frame's session key is
   `agent:<friday_uuid>:paperclip:issue:<issue_id>` because
   `sessionKeyStrategy="issue"`.
4. OpenClaw matches `<friday_uuid>` against `agents.list`, finds Friday,
   and the run lands in `agents/<friday_uuid>/agent/sessions/<key>.json`.
5. A second issue assigned to Friday gets a different session key →
   different transcript. Memory plugin (MEMORY.md) bridges across
   sessions for long-term knowledge.

### `/chat` agent create (forward-going pairing)

1. User clicks "New agent" in the Agents panel → frontend sends
   `agents.create` RPC over the WS.
2. `_intercept_agents_create`
   (`apps/backend/routers/websocket_chat.py:550`) catches the RPC
   before it reaches the container.
3. The interception calls Paperclip first, mints a UUID, then issues
   `agents.create` (with `name=<paperclip_uuid>`) and `agents.update`
   (with `displayName=<friendly name>`) to the gateway via
   `pair_with_openclaw`.
4. The original RPC's response carries the Paperclip UUID back to the
   client.

### `/teams` agent create

1. POST to `/api/v1/teams/agents` (`apps/backend/routers/teams/agents.py:330`).
2. `_admin().create_agent(...)` mints the Paperclip row + UUID.
3. If the owner's container is reachable, `pair_with_openclaw` issues
   `agents.create` + `agents.update` against it. If not, the next
   container provision picks the agent up via the live-list fetch.

---

## Multi-tenant model

The container is keyed by `owner_id`:

- For personal users, `owner_id == user_id`.
- For orgs, `owner_id == clerk_org_id`. All members of an org share one
  container, one EFS root, and one Paperclip company.

Member-level chat isolation lives in the session-key suffix:
`agent:<agentId>:<member_user_id>`. Two members chatting with Friday at
the same time are reading and writing different session files
(`agents/<friday_uuid>/agent/sessions/agent:<friday_uuid>:user_alice.json`
vs `...:user_bob.json`). The same agent's memory plugin (MEMORY.md) is
shared across them — that's intentional; it's the org-wide knowledge
store.

Issue-driven runs sidestep member identity entirely. The session key is
`paperclip:issue:<issue_id>`, scoped to the issue, not to whoever
clicked "Assign" — Paperclip's notion of "who runs this" lives at the
issue/thread level, not in OpenClaw's session key.

---

## Session-key strategy: why `"issue"`

Pre-PR, `synthesize_openclaw_adapter` set
`sessionKeyStrategy="fixed"` — every Paperclip-driven run for a given
agent collapsed into one session, regardless of which issue triggered
it. Issues bled into each other; the user saw transcripts about Issue A
when reading Issue B's thread.

Post-PR, the strategy is `"issue"`. Paperclip's
`openclaw_gateway` adapter substitutes `paperclip:issue:<issue_id>` into
the session key on every wake. Different issues never share a
transcript.

The `sessionKey` field (still present in the adapter config) is the
fallback for wakes without an issue context — manual heartbeat,
sibling-agent wake, etc. We set it to the agent creator's `user_id` so
manual wakes route to a stable per-creator session.

---

## Why we left `agents/main/agent/auth-profiles.json` alone

It's tempting to "tidy up" by deleting `agents/main/` after this PR,
since no agent in `agents.list` uses that ID anymore. Don't.

OpenClaw's auth-profile resolver (`openclaw/src/agents/agent-paths.ts:6-13`,
`resolveOpenClawAgentDir`) returns
`<stateDir>/agents/<DEFAULT_AGENT_ID>/agent/` unconditionally, where
`DEFAULT_AGENT_ID = "main"` is hardcoded in
`openclaw/src/agents/routing/session-key.ts:21`. This is the
**container-wide auth-fallback location** — every agent without its own
`auth-profiles.json` reads from here.

The fallback test
`openclaw/src/agents/auth-profiles/oauth.fallback-to-main-agent.test.ts`
confirms the inheritance: secondary agents read `agents/main/agent/auth-profiles.json`
when their own is missing or refresh fails.

In Isol8's flat-fee model every container has one provider (Bedrock,
ChatGPT OAuth, or BYOK), so it makes sense to write the credential
once at the container-wide fallback path and let every paired agent
inherit. Relocating the file to Friday's directory would break the
inheritance chain and force per-paired-agent credential copies.

Two roles, one literal name:
- Pre-PR: `agents/main/` was BOTH the agent's runtime dir AND the auth
  fallback.
- Post-PR: `agents/main/` is PURELY the auth fallback. Friday and any
  other paired agent run out of `agents/<paperclip_uuid>/`. No agent
  runtime lives at `agents/main/` anymore — but the auth file does.

---

## Out of scope

Deferred to follow-up work:

- **Backfill of pre-existing OpenClaw-only agents.** Containers
  provisioned before this PR have `agents/main/` and possibly other
  pre-PR agent dirs that don't match any Paperclip UUID. Handled by
  the dev-reset wipe procedure (CLAUDE.md "Clean-Slate Dev Reset")
  rather than an in-place migration.
- **Deletion sync from Paperclip → OpenClaw.** When a Paperclip agent
  is deleted via `/teams`, we currently don't propagate
  `agents.delete` to the container. The reverse direction
  (`/chat`-initiated deletes) is handled by the existing interception
  at `apps/backend/routers/websocket_chat.py` `agents.delete` path.
- **Adapter config refresh on token rotation.** Each Paperclip agent's
  `adapterConfig` carries an Isol8-minted service token. When that
  token rotates, existing agents keep stale tokens until manually
  re-paired. Tracked separately.
- **Removing the legacy `"main"` agent fallback in OpenClaw upstream.**
  Deferred until every owner has at least one Paperclip agent
  (currently true for new owners; pre-PR owners need the wipe).

---

## Code map

The pairing logic lives in seven files:

- `apps/backend/core/services/paperclip_openclaw_pairing.py` —
  `fetch_paperclip_agents_for_owner`, `pair_with_openclaw`,
  `_resolve_company_id_for_owner`. Single source of truth for the
  pairing policy; consumed by all three call sites.
- `apps/backend/core/services/paperclip_adapter_config.py` —
  `synthesize_openclaw_adapter`. Returns
  `sessionKeyStrategy="issue"` and the rest of the adapter config
  written into each Paperclip agent.
- `apps/backend/core/services/paperclip_provisioning.py` —
  `provision_org`. Seeds the "Friday" agent with `role="ceo"` at org
  creation time.
- `apps/backend/core/containers/config.py` —
  `build_openclaw_config_dict` (`:481`) and `_build_agents_list`
  (`:438`). Threads `paperclip_agents` through and builds
  `agents.list`.
- `apps/backend/core/containers/ecs_manager.py` —
  `write_user_configs` (`:1502`). Fetches the live Paperclip list at
  provision and reprovision time.
- `apps/backend/routers/teams/agents.py` —
  `create_agent` (`:330`). `/teams` POST `/agents` route; calls
  Paperclip first, then `pair_with_openclaw`.
- `apps/backend/routers/websocket_chat.py` —
  `_intercept_agents_create` (`:550`). `/chat` `agents.create` RPC
  interception.

The session-key format (`agent:<agentId>:<user_id>`) lives at
`apps/backend/routers/websocket_chat.py:881`. The Paperclip-driven
session-key prefix (`paperclip:issue:<issue_id>`) is built by
Paperclip's `openclaw_gateway` adapter from `sessionKeyStrategy="issue"`.
