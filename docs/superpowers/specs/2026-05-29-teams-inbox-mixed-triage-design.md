# Teams Inbox Mixed Triage — Design

**Roadmap:** sub-project **E** of the [Teams UI Parity Roadmap](./2026-05-04-teams-ui-parity-roadmap.md). Extends the inbox deep-port (#3) and depends on the agent-management run-detail route shipped in #754 (sub-project B).

**Status:** Spec

## Goal

Surface pending **approvals** and **failed agent runs** inside `/teams/inbox` as an at-a-glance "Needs attention" triage band with inline actions, and retire the crude standalone Approvals panel — without disturbing the issue-centric inbox (tabs, kanban, keyboard nav, archive).

## Background — current state

A gap audit (2026-05-29 upstream-parity sweep) flagged that Isol8's inbox shows **issues only**, while triage items live elsewhere or nowhere:

- **Inbox** (`InboxPage`): issue feed with tabs Mine / Recent / Unread / All and a list⇄board toggle. The `approvals` / `runs` / `joins` tab *values* exist in the `InboxTab` union + URL, but they are not rendered as tabs and `useInboxData` fetches only the three issue lists — the data hook explicitly notes approvals/runs are "not yet ported."
- **Approvals**: a separate `ApprovalsPanel` (its own sidebar entry, `/teams/approvals`). Rejection uses a browser `prompt()`.
- **Failed runs**: the BFF already exposes `GET /teams/inbox/runs` (`list_company_heartbeat_runs(status="failed")`) and `GET /teams/inbox/live-runs`, with query keys defined — but **no frontend consumes them**.
- **Joins**: no list endpoint, no UI. Isol8 auto-creates+accepts+approves member joins server-side in `paperclip_provisioning.provision_member`, so there is no pending-joins queue. **Out of scope** (a joins row would always be empty).
- **Realtime**: `TeamsEventsProvider` already invalidates `/teams/inbox` on `teams.heartbeat.run.*` and `teams.activity.logged`.

So "mixed triage" for Isol8 concretely means: **approvals + failed runs, surfaced in the inbox with inline actions**, and a decision on the standalone Approvals panel.

## Decisions

1. **Item types:** approvals + failed runs. Joins excluded (vestigial in Isol8).
2. **Layout:** a **"Needs attention" band** pinned above the inbox toolbar — a triage *layer*, not a tab. Chosen over a unified interleaved feed (mixes transient alerts with ongoing issue work; the issue-specific kanban/keyboard/archive UX doesn't apply to alert rows) and over wiring the per-tab stubs (segregated; defeats triage-at-a-glance).
3. **Approvals panel:** fully retired; its content + actions move into the band. (The sidebar nav has no per-item badge today; the pending-approvals **count** continues to show on the Dashboard tile, unchanged. The band's own count header becomes the inbox-side at-a-glance signal.)
4. **Failed-run dismiss:** **local per-user acknowledge** (localStorage). There is no server retry/rerun, and a failed run is a shared company-level fact, so we hide it per-user rather than mutate shared state or invent a backend action.

## UX & behavior

### The "Needs attention" band

- Rendered by `InboxPage` above `InboxToolbar`, visible on **every** issue tab and in **both** list and board view.
- **Collapsible**; collapse state persisted per-company in localStorage (same pattern as the list⇄board `viewMode`). Header shows the total count.
- **Renders nothing when empty** (zero pending approvals + zero un-acknowledged failed runs) so "inbox zero" stays clean.
- **Ordering:** approval rows first (blocking/actionable), then failed-run rows; each group newest-first.

### Approval row

- Displays the approval `title` (falls back to "Approval requested") + relative age.
- Actions:
  - **Approve** — optional note; `POST /teams/approvals/{id}/approve { note? }`.
  - **Reject** — required reason via an **inline expanding text field** (replaces the browser `prompt()`); the Reject confirm button is disabled until the reason is non-empty; `POST /teams/approvals/{id}/reject { reason }`.
- On success: optimistic removal from the band + revalidate `/teams/approvals` and `/teams/dashboard` (sidebar badge).
- On error: surface a non-blocking inline error on the row; the row stays so the user can retry.

### Failed-run row

- Displays the agent name (resolved from the `agents` prop via `agentId`, falling back to the id) + "heartbeat run failed" + relative age + a one-line `failureReason` when present.
- Actions:
  - **View** — links to the existing run-detail route `/teams/agents/{agentId}/runs/{id}` (shipped in #754). Hidden when `agentId` is null (run payload allows null) — in that case the row is informational only.
  - **Dismiss** — local per-user acknowledge: adds the run id to a per-company localStorage set; the row disappears immediately and stays hidden across reloads for this user.

## Architecture

### Data flow

```
InboxPage
  └─ NeedsAttentionBand(companyId, agents)
       ├─ useInboxTriage()
       │    ├─ read("/approvals")        -> { approvals: Approval[] }  (status "pending")
       │    ├─ read("/inbox/runs")       -> HeartbeatRun[] (status "failed")
       │    └─ useAcknowledgedRuns()     -> filters out locally-acked run ids
       │         => { approvals, failedRuns, count, isLoading, mutateApprovals, mutateRuns }
       ├─ ApprovalRow[]   (useApprovalMutations: approve/reject)
       └─ FailedRunRow[]  (View link + Dismiss via useAcknowledgedRuns.ack)
```

### New frontend files (each small, single-purpose)

| File | Responsibility |
|------|----------------|
| `inbox/NeedsAttentionBand.tsx` | Collapsible container; count header; hide-when-empty; renders approval then failed-run rows. |
| `inbox/ApprovalRow.tsx` | One approval; Approve (optional note) / Reject (inline required reason); optimistic. |
| `inbox/FailedRunRow.tsx` | One failed run; View link (hidden when no `agentId`) + Dismiss. |
| `inbox/hooks/useInboxTriage.ts` | Fetch `/approvals` + `/inbox/runs`; filter acked runs; expose merged result + count + mutators. |
| `inbox/hooks/useApprovalMutations.ts` | `approve(id, note?)` / `reject(id, reason)`; revalidate `/teams/approvals` + `/teams/dashboard`. |
| `inbox/hooks/useAcknowledgedRuns.ts` | Per-company local ack set; `ack(id)`, `isAcked(id)`. |

### Edits

| File | Change |
|------|--------|
| `inbox/InboxPage.tsx` | Mount `<NeedsAttentionBand companyId={companyId} agents={agents} />` above `<InboxToolbar>`. |
| `shared/lib/inboxStorage.ts` | Add `load/saveAckRuns(companyId)` + `load/saveBandCollapsed(companyId)`, mirroring the existing read-items helpers (per-company key namespace). |
| `TeamsEventsProvider.tsx` | Add `/teams/approvals` to the `teams.activity.logged` key list; add `/teams/inbox/runs` to `teams.heartbeat.run.queued` and `teams.heartbeat.run.status`. |
| `TeamsSidebar.tsx` | Remove the `approvals` nav entry (`{ key: "approvals", ... }`). The sidebar nav renders no per-item badge, so nothing else to change; the pending-approvals count stays on the Dashboard tile. |
| `TeamsPanelRouter.tsx` | Remove the `approvals` mapping from `PANELS`, and redirect the now-unmapped `approvals` key to `/teams/inbox` via a client-side `router.replace` (today an unmapped key renders a literal "Unknown panel: {panel}", so the redirect must be explicit). |

### Deletions

- `panels/ApprovalsPanel.tsx` and its test (if any).

### BFF

**No new endpoints.** `/teams/approvals`, `/teams/approvals/{id}/approve`, `/teams/approvals/{id}/reject`, and `/teams/inbox/runs` already exist and return the needed shapes (`Approval`, `HeartbeatRun`). The realtime change is entirely frontend (key map). The `joins` tab value and `_TABS_NOT_ON_ISSUES` handling in `routers/teams/inbox.py` are left untouched (out of scope; harmless).

## Types

Both already exist in `shared/types.ts`:

- `Approval { id; issueId?; title?; status: "pending"|"approved"|"rejected"; createdAt?; decidedAt? }`
- `HeartbeatRun { id; agentId?; status: "queued"|"running"|"succeeded"|"failed"|"cancelled"; startedAt?; completedAt?; failureReason? }`

No new types required.

## Error handling

- **Load failure** of `/approvals` or `/inbox/runs`: the band degrades to whichever source loaded; a failed source contributes no rows (the band never blocks the issue inbox below it). If both fail, the band renders nothing (same as empty) — the issue inbox is unaffected.
- **Approve/reject failure**: inline error on the row, row retained for retry; caches not optimistically cleared on failure.
- **Dismiss** is local-only and cannot fail.
- **Null `agentId`** on a failed run: View hidden; row remains informational.

## Testing

- **NeedsAttentionBand**: renders nothing when empty; shows count; collapse toggle persists to localStorage; renders approval rows before failed-run rows.
- **ApprovalRow**: Approve posts `{ note }`; Reject requires a non-empty reason (confirm disabled until typed) and posts `{ reason }`; optimistic removal; error path keeps the row.
- **FailedRunRow**: View href equals `/teams/agents/{agentId}/runs/{id}`; View hidden when `agentId` is null; Dismiss calls `ack(id)` and the row disappears.
- **useInboxTriage**: merges the two sources; filters out acknowledged run ids; computes count.
- **useAcknowledgedRuns / inboxStorage**: ack round-trips through localStorage; per-company isolation (one company's acks don't leak to another).
- **Realtime**: `EVENT_KEY_MAP` includes `/teams/approvals` under `teams.activity.logged` and `/teams/inbox/runs` under the heartbeat-run events.
- **Retirement**: `TeamsPanelRouter` no longer maps `approvals`; `/teams/approvals` redirects to `/teams/inbox`.

## Out of scope

- **Joins** triage (auto-handled server-side; no queue).
- **Run retry/rerun** (no backend capability; not adding one here).
- **Server-side run dismiss** (no endpoint; acknowledge is local).
- **Live-runs ("Live" badge)** — `/inbox/live-runs` exists but is a separate dashboard concern, not triage.
- **Interleaving triage rows *into* the issue list / kanban** — explicitly rejected in favor of the band.

## Convention

Plan filename: `docs/superpowers/plans/2026-05-29-teams-inbox-mixed-triage.md`. Update the roadmap row E (status + links) when this enters Plan / In-progress / Done.
