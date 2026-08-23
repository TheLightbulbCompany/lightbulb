# Workspace rehaul — full remaining scope

Companion to `2026-08-23-workspace-shell-rehaul.md`, which holds the why, the
settled decisions, and what already shipped (#1979, #1980, #1981). This file is
the complete remaining build, at PR granularity, with the backend dependency
called out per item.

**Explicitly out of scope:** chat streaming polish. Tracked separately.

---

## A. UI only — no backend

Ten PRs. Ordered lowest risk first; each is independently shippable and
revertible.

| # | PR | Touches | Risk |
|---|---|---|---|
| A1 | Agent page: **Files** tab | `AgentInspectorPanel` (tab shell exists) | Low |
| A2 | **Routines** → own rail destination | `CompanyNav`, `panelRegistry` | Low |
| A3 | **Activity cleanup** | Port `agentMap` / `entityNameMap`; reskin `ActivityRow` to the handoff shape | Low |
| A4 | Work: **scope switch** (Goals / Projects / Tasks) | New chrome over three existing panels | Med |
| A5 | Work: **group by** agent and project | `IssuesPanel` regroup | Med |
| A6 | Work: **create flow** | Scope declares the type, so no type picker. Status glyph + inferred chips. | Med |
| A7 | Rail: **one state glyph** | Drops status dot + published dot | Med |
| A8 | Rail: **four row actions → one overflow** | `ChatLayout.css` is dual-owned with mobile; 13 test files assert on `.agent-item`. Own PR, mobile regression-tested. | **High** |
| A9 | **Layout presets** in the header | Verify dockview maximize/split API first | Med |
| A10 | **Completion beats**, remaining moments | Task done, routine completes, connector reconnects, agent hired | Low |

---

## B. Calendar

Purpose, per the product call: **organization.** When tasks completed, when they
are due. Not external sync.

The data splits three ways, and only one third needs backend.

### B1 — Ships with what exists (no backend)

- **Routine schedules.** `RoutineTriggerSummary` carries `cronExpression`,
  `timezone`, `nextRunAt`. This is the "Every day" strip and every future
  occurrence.
- **Routine run history.** `RoutineRun.completedAt` and `failureReason` — a
  failed 06:00 run renders in place, in amber.
- **Agent run history.** `HeartbeatRun.startedAt` / `completedAt`.

### B2 — Derivable today, needs care

- **Task completions.** `Issue` has **no** `completedAt`. `updatedAt` is a bad
  proxy: any edit bumps it, so a task edited last week would land on the wrong
  day. The honest source is the **activity feed**, which already records an
  `action` per event with a timestamp — a status change to done IS the
  completion, dated correctly. Depends on A3 landing first, since the same
  enrichment is needed to name the actor.

### B3 — Needs backend

- **Task due dates.** `Issue` has no `dueDate` field at all. This is a new field
  end to end: Paperclip schema → BFF projection → `Issue` type → the Work create
  and detail surfaces → Calendar. Nothing in the UI can fake it, and a calendar
  that shows completions but not deadlines only does half the stated job.

**Sequencing:** B1 ships as a real calendar on routines alone. B2 adds the
completion history. B3 is the one that needs a backend ticket, and it should be
opened now because it gates the half of the feature the product call actually
asked for first.

---

## C. Backend first

| Item | What is missing | Blocks |
|---|---|---|
| Dashboard → **Today** | A digest read model. "Needs you / working / finished" is not a rearrangement of `teams-dashboard`. | The default landing surface |
| **Company brain** | Folder creation + upload endpoints. EFS file-viewer and `teams-artifacts` cover part of it. | The brain, and A1's Files tab beyond read-only |
| **Task due dates** | See B3 | Half of Calendar |

**Dashboard → Ledger** is likely neither: `UsagePanel` already computes billed
spend with a per-model split. Confirm before scoping; if it holds, it moves to
section A as a reskin.

---

## D. Cross-cutting, currently unowned

These are not features, which is exactly why they get skipped. Each needs an
owner or it ships broken.

### D1 — Empty states

Every surface. A workspace with **no** agents, **no** tasks, **no** files and
**no** routines is the first thing a new customer sees, and none of that copy
exists. The rehaul makes this worse, not better: a dashboard built around "what
needs you" is emptier on day one than a task list was. Needs one pass, all
surfaces, written together so they share a voice.

### D2 — Provisioning

Bind the stepper to **real** phases rather than a timer, and make it survive a
step failing. Two states, not one: first provision runs minutes and earns a
narrative; waking a scaled-to-zero container runs seconds and must not pretend
otherwise. See `P-provisioning.html`.

### D3 — Mobile

Every decision so far is desktop-only. `ChatLayout` is the mobile shell and it
shares `ChatLayout.css` with the dock — which is precisely why A7 and A8 are
rated High. Mobile needs its own pass, not a media query.

### D4 — Tauri shell

The desktop app points at `/chat`, so it inherits the rehaul for free. What it
does **not** inherit: window chrome, the tray, and `DesktopUpdater`'s footer
placement once the rail is restructured. Verify after A8.

### D5 — Marketplace

Reskin only, per `MAP.html`. Framed as hiring rather than shopping, uniform
grid, no featured agent. Independent of everything above.

---

## Dependency order

```
A1 A2 ─────────────────────────► independent, start here
A3 ──────────────► B2 ─────────► Activity enrichment gates completion history
A4 ► A5 ► A6 ──────────────────► Work, in order
A7 ► A8 ───────► D3 ► D4 ──────► rail changes gate the mobile + Tauri check
B1 ────────────────────────────► independent
B3 (backend) ──► Calendar full
C  (backend) ──► Today, Company brain
D1 ────────────────────────────► last, once every surface exists
D5 ────────────────────────────► independent
```

## Done means, per PR

`vitest run` green, `eslint` clean on touched files, `tsc --noEmit` adding no new
errors, and every control in `MAP.html`'s button inventory either wired or listed
in the PR body as deliberately deferred.
