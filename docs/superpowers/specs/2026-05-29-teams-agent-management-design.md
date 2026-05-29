# Teams Agent Management — Design Spec

**Status:** Spec
**Roadmap:** sub-project **B** in [2026-05-04-teams-ui-parity-roadmap.md](./2026-05-04-teams-ui-parity-roadmap.md)
**Date:** 2026-05-29

## Background

Upstream Paperclip manages agents from its agent-detail page (edit role/title/capabilities/icon, set reporting lines, pause/resume, delete). Isol8's `/teams` can't: `AgentDetailPanel` shows a raw JSON Overview + a *disabled* Config tab, the org chart is read-only, and the agents list only creates (name + role). This spec closes the gap — designed around the Paperclip↔OpenClaw split, with a deliberate **single-editor** rule that prevents the two systems from diverging.

## Foundation: the Paperclip ↔ OpenClaw overlap

The load-bearing model for the whole design.

- **OpenClaw** (the per-user **container**) is the agent **runtime**: executes agents (LLM, tools, skills, MCP, workspace, sessions). Config in `openclaw.json` (`agents.list[]`). Reached via the **Isol8 WS gateway**.
- **Paperclip** (per-tenant Aurora service) is the **management/org layer**: company, agents-as-team-members, issues, approvals, routines, goals, projects, costs, org hierarchy, status. Reached via the **Teams BFF** (REST).
- **Identity:** one logical agent = a **Paperclip record (master, owns the UUID)** + an **OpenClaw entry (runtime, id from the UUID**, workspace `workspaces/{uuid}`). Pairing module: *"The container is the runtime; Paperclip is the master record."*
- **Agent meta** (`role`, `title`, `capabilities`, `icon`) lives in **both**: `openclaw.json` `agents.list[].params.paperclip` (durable, EFS-persisted — `core/services/paperclip_meta.py`) **and** the Paperclip record, kept in sync (container→Paperclip via the `websocket_chat` mirror; Paperclip→container at provision via `_build_agents_list`).
- **Adapter:** the `openclaw_gateway` adapter (Isol8-synthesized, carries the service token) binds the agent to the container. **Isol8-managed, redacted on every BFF read (`routers/teams/agents.py:53-69`), never editable.**

### The single-editor rule (core design decision)

Because meta lives in two places, two editors would diverge. So **each piece of agent data has exactly one editor, located where that data conceptually lives:**

| Data | Owner | The one editor | In `/teams` |
|---|---|---|---|
| role, title, capabilities, icon | OpenClaw `params.paperclip` (mirrored to Paperclip) | **`/chat` control panel** (`AgentOverviewTab`, today) | **read-only display** + "manage in control panel" pointer |
| reports_to | Paperclip only (no container copy) | **`/teams`** (new) | editable (ReportsToPicker → BFF) |
| pause / resume | Paperclip only | **`/teams`** (new) | editable (BFF routes) |
| delete | both (cascade) | gateway `agents.delete` (already in `/chat`; add to `/teams`) | action (guarded) |
| adapter config/type | Isol8-managed | — (never editable) | hidden |
| budget | Paperclip only | — (omitted; Isol8 bills owner-level Bedrock credits) | not shown |

This is a **deliberate divergence from upstream**: upstream edits meta in agent-detail; Isol8 centralizes meta editing in the `/chat` control panel and keeps `/teams` read-only for those fields. The benefit: no second writer to `params.paperclip`, so **no divergence, no clobber, and no reconciliation/hardening needed**.

## Scope

`/teams` agent management adds, in `AgentDetailPanel` (the hub) + quick actions on the org chart / agents list:
- **reports_to** editing (the org-chart hierarchy) — BFF.
- **pause / resume** — BFF.
- **delete** — gateway cascade; **blocked for seeded agents** (Friday/Atlas).
- **read-only** display of role/title/capabilities/icon, with a pointer to edit them in the agent's `/chat` control panel.

**Explicitly out of scope:** editing role/title/capabilities/icon in `/teams` (single-editor rule → stays in the control panel); adapter editing; per-agent budget; the `_build_agents_list` re-seed hardening (a separate, pre-existing full-EFS-wipe latent fix — tracked as its own roadmap row, not bundled here); agent runtime internals (sessions/keys/config-revisions — stay in `/chat`); creating agents (already exists).

## Architecture

```
/teams AgentDetailPanel (hub)
  ├─ Meta (role/title/capabilities/icon)  → READ-ONLY + "manage in control panel"
  ├─ reports_to (ReportsToPicker)         → BFF PATCH /teams/agents/{id}            → Paperclip
  ├─ pause / resume                       → BFF POST /teams/agents/{id}/pause|resume → Paperclip
  ├─ delete†                              → gateway agents.delete → container + Paperclip cascade + workspace trash
  └─ Adapter: read-only "managed by Isol8" note (never the secret)

† delete hidden for Friday/Atlas
Org chart card  → opens AgentDetailPanel; live status dot via /teams/agents invalidation
Agents list row → quick pause/resume/delete (guarded) + opens detail
```

`/teams` already mounts the `GatewayProvider` (`app/teams/layout.tsx`), so the delete action can issue the gateway `agents.delete` RPC; all editable fields use the BFF.

## Backend changes

**A. Pause/resume.** Add `POST /teams/agents/{id}/pause` + `/resume` (`routers/teams/agents.py`) backed by new `PaperclipAdminClient.pause_agent` / `resume_agent` (upstream server exposes `POST /api/agents/:id/pause|resume`). Follow the existing admin-client `_post` + `PaperclipApiError` pattern.

**B. reports_to.** Already accepted on `PatchAgentBody` (`routers/teams/schemas.py:55`) and forwarded by `patch_agent`. Add a regression test only.

**C. Delete guard (seeded agents).** In the `agents.delete` cascade (`paperclip_agent_lifecycle.delete_paperclip_for_chat` and its `websocket_chat` call site), reject deletion of a seeded agent with a clear error. Identification (no new provisioning code): **Friday = the `role == "ceo"` agent** (robust against rename — role ≠ name); **Atlas = `name == "Atlas"`** (documented fragility; a durable `params.paperclip.seeded` marker is noted as future hardening but NOT added here, to keep this sub-project off the provisioning/container path). The `/teams` and `/chat` UIs hide/disable delete for seeded agents; the guard is the backstop.

No other backend changes: no gateway meta-write from `/teams`, no role-lock-in-meta, no `_build_agents_list` change.

## Frontend changes

- **`AgentPropertiesPanel`** in `AgentDetailPanel` (replaces the disabled Config tab + JSON Overview):
  - **Read-only** meta section: role, title, capabilities, icon, with a link/button "Edit in the agent's control panel" (deep-link to `/chat?...` agent control).
  - **`reports_to`** editor via **`ReportsToPicker`** (BFF).
  - **Actions:** pause/resume; delete (hidden/disabled for seeded).
  - **Adapter:** a small read-only "managed by Isol8" note (no secret).
- **`ReportsToPicker`** (port from upstream `paperclip/ui/src/components/ReportsToPicker.tsx`): searchable agent dropdown; excludes self + would-be cycles; "No manager" option. On select → BFF PATCH `reports_to` → revalidate `/teams/agents` (org chart re-renders).
- **Quick actions** on agents-list rows + org-chart card menus: pause/resume, delete — disabled/hidden for seeded agents (mirror the server guard).
- **`useAgentMutations`** hook: `setReportsTo` / `pause` / `resume` (BFF) + `delete` (gateway), each invalidating the `/teams/agents` SWR key so org chart + list refresh live (status dots already update via `TeamsEventsProvider`).

No `useAgentMetaSave`, no `AgentIconPicker` — meta editing isn't added to `/teams`.

## Types

Extend the slim `CompanyAgent` (`components/teams/shared/types.ts`) to carry `role`, `title`, `status`, `reportsTo`, `capabilities`, `icon` (all already forwarded by the BFF list minus the redacted adapter fields) for read-only display + reports_to editing + the delete guard. Adapter fields stay absent from the type so the UI can't reference them.

## Data flow & error handling

- reports_to / pause / resume: call → revalidate `/teams/agents`. Optimistic where safe.
- delete: gateway `agents.delete` → cascade → revalidate.
- Guardrail violations (delete seeded) return a clear error surfaced as a toast; the UI proactively disables guarded controls so the error path is rare.
- Adapter binding and `params.paperclip` are never written by any `/teams` path. Meta stays single-editor in `/chat`.

## Testing

- **BFF:** unit tests for pause/resume routes + admin-client methods; the delete-seeded guard (Friday-by-ceo, Atlas-by-name); reports_to PATCH.
- **Frontend:** `ReportsToPicker` (self/cycle exclusion, no-manager); `AgentPropertiesPanel` (meta read-only, reports_to save, actions); quick actions disabled for seeded agents; `useAgentMutations` SWR invalidation.

## Open items (resolved; verify during implementation)

- **Seeded identification:** Friday-by-`ceo`-role, Atlas-by-name for the delete guard now; durable `params.paperclip.seeded` marker is a future hardening (out of scope here).
- **Icon display:** opaque `AGENT_ICON_NAMES` string rendered read-only; no picker in `/teams`.
- **Budget:** omitted (Isol8 bills owner-level Bedrock credits; per-agent Paperclip budget is presumed cosmetic — revisit only if it drives real enforcement).
- **"Manage in control panel" deep-link:** confirm the exact `/chat` route/param that opens a given agent's control panel for the meta-edit pointer.
