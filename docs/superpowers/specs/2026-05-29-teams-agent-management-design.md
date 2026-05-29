# Teams Agent Management — Design Spec

**Status:** Spec
**Roadmap:** sub-project **B** in [2026-05-04-teams-ui-parity-roadmap.md](./2026-05-04-teams-ui-parity-roadmap.md)
**Date:** 2026-05-29

## Background

Upstream Paperclip lets you fully manage agents (edit role/title/capabilities/icon, set reporting lines, pause/resume, delete). Isol8's `/teams` currently can't: `AgentDetailPanel` shows a raw JSON Overview + a *disabled* Config tab, the org chart is read-only, and the agents list only creates (name + role). This spec closes that gap — but it must be designed around the Paperclip↔OpenClaw split, because agent identity and metadata are **shared** between the two systems.

## Foundation: the Paperclip ↔ OpenClaw overlap

This is the load-bearing model for the whole design.

- **OpenClaw** (the per-user **container**) is the agent **runtime**: executes agents (LLM, tools, skills, MCP, workspace, sessions). Config in `openclaw.json` (`agents.list[]`). Reached via the **Isol8 WS gateway** (`useGateway` / gateway RPCs).
- **Paperclip** (per-tenant Aurora service) is the **management/org layer**: the company, agents-as-team-members, issues, approvals, routines, goals, projects, costs, org hierarchy, status/heartbeats. Reached via the **Teams BFF** (REST).
- **Identity:** one logical agent = a **Paperclip record (master, owns the UUID)** + an **OpenClaw entry (runtime, id derived from that UUID**, workspace `workspaces/{uuid}`). `pair_with_openclaw` registers it via `agents.create` + `agents.update` RPCs. The pairing module states the policy outright: *"The container is the runtime; Paperclip is the master record."*
- **Agent meta** (`role`, `title`, `capabilities`, `icon`) lives in **both**: `openclaw.json` `agents.list[].params.paperclip` (the durable, EFS-persisted copy — see `core/services/paperclip_meta.py`) **and** the Paperclip record. They are kept in sync:
  - **container → Paperclip:** editing meta (control panel gateway RPC) writes `params.paperclip`; the `websocket_chat` interceptors (`routers/websocket_chat.py:620,729`) read it and mirror to Paperclip via `PaperclipAdminClient.update_agent`.
  - **Paperclip → container:** at provision, `fetch_paperclip_agents_for_owner` → `write_openclaw_config` rebuilds `agents.list`.
- **Adapter:** the Paperclip agent's `openclaw_gateway` adapter (Isol8-synthesized, carries the service token + gateway URL) binds it to the container. **Isol8-managed, redacted on every BFF read (`routers/teams/agents.py:53-69`), never user-edited.**

**Durability:** `openclaw.json` persists on **EFS**, and config updates **deep-merge** (preserving runtime additions incl. `params.paperclip`). So agent meta survives normal re-provisions / config patches. The only loss path is a **full EFS wipe** (deliberate clean-slate), because `_build_agents_list` (`core/containers/config.py:406`) rebuilds `agents.list` without re-seeding `params.paperclip` from the master. This spec hardens that (see Backend §H).

### Consequence for the design

Because meta lives in both places and is kept in sync, a naive `/teams` edit that only PATCHes Paperclip would **diverge from the container's `params.paperclip`** and then be overwritten the next time the container meta is mirrored (a control-panel save / agent-create interceptor) — and, with the §H hardening, a full re-provision rebuilds the container from the master, so the two must not be allowed to drift. Editing via the container path keeps both consistent in one write. Therefore:

| Field / action | Source of truth | Transport from `/teams` | Why |
|---|---|---|---|
| role, title, capabilities, icon | `params.paperclip` (container, EFS) | **gateway RPC** (writes container) → interceptor mirrors to Paperclip | avoid clobber; single source of truth |
| delete | both | **gateway `agents.delete` RPC** → interceptor cascades to Paperclip delete + trashes workspace | one call handles both sides |
| reports_to | Paperclip only | **BFF PATCH** `/teams/agents/{id}` | not in `params.paperclip`; Paperclip-owned org hierarchy |
| pause / resume | Paperclip only | **BFF** (new routes) | Paperclip runtime/management state |
| adapter config/type | Isol8-managed | — (never editable) | binds container; redacted |
| budget | Paperclip only | — (omit; see Open Items) | Isol8 bills via owner-level Bedrock credits; per-agent budget is likely cosmetic |

## Scope

Full upstream agent-management parity, **with Isol8 guardrails**:
- Edit `role` (locked to `ceo` for Friday), `title`, `capabilities`, `icon`, `reports_to`.
- `pause` / `resume`.
- `delete` (blocked for seeded agents Friday/Atlas).
- Read-only display of adapter status (managed-by-Isol8 note), never the adapter secret.

**Out of scope:** editing adapter config/type; per-agent budget controls (pending confirmation it's not a no-op); agent runtime internals (sessions, keys, config-revisions) — those stay in the `/chat` control panel.

## Architecture

**Edit surface = the hub pattern.** `AgentDetailPanel` becomes the canonical editor; the org chart and agents list link to it and offer quick actions. `/teams` already mounts the `GatewayProvider` (`app/teams/layout.tsx`), so the panel can issue gateway RPCs exactly like the control panel.

```
/teams AgentDetailPanel (hub)
  ├─ Properties form
  │   ├─ name, title, capabilities, icon, role*  ──(gateway RPC: params.paperclip)──> container ──(interceptor mirror)──> Paperclip
  │   └─ reports_to (ReportsToPicker)            ──(BFF PATCH /teams/agents/{id})───────────────> Paperclip
  ├─ Actions
  │   ├─ pause / resume                          ──(BFF POST /teams/agents/{id}/pause|resume)───> Paperclip
  │   └─ delete†                                 ──(gateway agents.delete RPC)──> container ──(interceptor cascade)──> Paperclip + workspace trash
  └─ Adapter: read-only "managed by Isol8" note (never the secret)

* role locked to ceo for Friday   † delete hidden for Friday/Atlas
Org chart card  → opens AgentDetailPanel; status dot live via /agents invalidation
Agents list row → quick pause/resume/delete (guarded) + opens detail
```

**Shared meta-save logic.** The control panel's meta-save (`AgentOverviewTab` → `params.paperclip` via gateway RPC, with `metaDirty` clobber-prevention) is extracted into a reusable hook (`useAgentMetaSave`) consumed by both the control panel and the new `/teams` properties form, so the two never drift.

## Backend changes

**A. BFF pause/resume.** Add `POST /teams/agents/{id}/pause` and `/resume` (`routers/teams/agents.py`) backed by new `PaperclipAdminClient.pause_agent` / `resume_agent` (upstream server exposes `POST /api/agents/:id/pause|resume`). Mirror the existing admin-client `_patch`/`_post` error-handling pattern.

**B. reports_to.** Already accepted on `PatchAgentBody` (`routers/teams/schemas.py:55`) and forwarded by `patch_agent`. No change beyond a test.

**C. Seeded-agent marker (robust guardrails).** At provision time (`paperclip_provisioning.provision_org` Friday/Atlas seeds) write `params.paperclip.seeded = true` for Friday and Atlas, via `paperclip_meta.write_to_agent_entry` (extend `PaperclipMeta` with an optional `seeded: bool`). Guardrails key off this marker, not fragile name/role matching. Back-compat: agents without the marker fall back to "Friday = the `ceo`-role agent; Atlas = name == 'Atlas'".

**D. Delete guard.** In the `agents.delete` interceptor path (`paperclip_agent_lifecycle.delete_paperclip_for_chat` / its websocket_chat call site), reject deletion of a seeded agent with a clear error. The `/teams` and `/chat` UIs both hide/disable delete for seeded agents, so the error is the backstop.

**E. Friday role lock.** In the meta-write/mirror path (the gateway `agents.update` interceptor that calls `update_agent`, and `write_to_agent_entry`), reject a `role` change away from `ceo` for the seeded CEO (Friday). Surfaced as a friendly error; UI disables the control.

**H. `_build_agents_list` re-seed hardening.** When rebuilding `agents.list` from the Paperclip master (`core/containers/config.py:406`), write each agent's `params.paperclip` from its Paperclip record (`role/title/capabilities/icon` + `seeded`). Makes Paperclip a fully authoritative master so even a full EFS wipe reconstructs the meta. Uses `paperclip_meta.write_to_agent_entry` for a single shape source.

## Frontend changes

- **`AgentPropertiesForm`** in `AgentDetailPanel` (replaces the disabled Config tab + JSON Overview): name, title, capabilities, icon, role (disabled for Friday) via the shared meta-save hook; `reports_to` via `ReportsToPicker` (BFF). Save shows pending/success/error; `metaDirty` guard prevents clobbering in-flight edits (mirrors `AgentOverviewTab`).
- **`ReportsToPicker`** (port from upstream `paperclip/ui/src/components/ReportsToPicker.tsx`): searchable agent dropdown; excludes self and would-be cycles; "No manager" option. On select → BFF PATCH `reports_to`, then revalidate `/teams/agents` (org chart re-renders).
- **`AgentIconPicker`**: reuse/extract the control panel's icon control (icon value space = Paperclip's `AGENT_ICON_NAMES`; treated as an opaque string per `paperclip_meta.py`).
- **Quick actions** on agents-list rows and org-chart card menus: pause/resume, delete — **disabled/hidden for seeded agents** (mirror the server guard).
- **`useAgentMutations`** hook: `reportsTo`, `pause`, `resume` (BFF) + `delete` (gateway), each invalidating the `/teams/agents` SWR key so the org chart and list refresh live (status dots already update via `TeamsEventsProvider`).
- **`useAgentMetaSave`** shared hook (extracted from `AgentOverviewTab`): the gateway-RPC `params.paperclip` write + Paperclip mirror, consumed by both control panel and `/teams`.

## Types

Extend the slim `CompanyAgent` (`components/teams/shared/types.ts`) to carry `role`, `title`, `status`, `reportsTo`, `capabilities`, `icon`, `seeded` (all already forwarded by the BFF list minus the redacted adapter fields). Keep adapter fields absent from the type so the UI can't reference them.

## Data flow & error handling

- Meta edits: optimistic where safe; the gateway RPC writes `params.paperclip`, the interceptor mirrors to Paperclip, then `/teams/agents` revalidates. On failure, surface the gateway error and keep the form dirty.
- reports_to / pause / resume / delete: call → revalidate `/teams/agents`. Guardrail violations (delete seeded, Friday role change) return a clear BFF/gateway error surfaced as a toast; the UI proactively disables guarded controls so the error path is rare.
- Adapter binding is never touched by any path.

## Testing

- **BFF:** unit tests for pause/resume routes (+ admin-client methods), the delete-seeded guard, and the Friday-role-lock rejection. reports_to PATCH test.
- **Provision:** `paperclip_meta` round-trip incl. `seeded`; `_build_agents_list` re-seeds `params.paperclip` from master; provision writes the seeded marker for Friday/Atlas.
- **Frontend:** `AgentPropertiesForm` (save, dirty-guard, role locked for Friday), `ReportsToPicker` (self/cycle exclusion), quick actions disabled for seeded agents, `useAgentMutations` SWR invalidation.

## Open items (resolved with caveats; verify during implementation)

- **Budget:** confirm whether Paperclip per-agent budget maps to any Isol8 control. Expectation: it does not (Isol8 bills owner-level Bedrock credits) → omit the control. If it turns out to drive Paperclip-side enforcement the user cares about, add it as a Paperclip-only BFF field in a follow-up.
- **Seeded identification:** primary = `params.paperclip.seeded` marker (new); fallback for pre-marker agents = Friday-by-`ceo`-role, Atlas-by-name. Document the fallback's fragility.
- **Icon value space:** opaque `AGENT_ICON_NAMES` string; if the upstream list is large, ship a searchable picker, else a simple select.

## Out of scope (explicit)

Adapter config/type editing; agent runtime internals (sessions/keys/config-revisions — stay in `/chat`); per-agent budget (pending); creating agents (already exists via the create dialog / chat path — this spec is management of existing agents).
