# Onboarding First-Task Pre-fill — Design

**Date:** 2026-06-26
**Status:** Approved design, pre-implementation
**Scope:** Frontend only. No backend, no DynamoDB, no new deps.

## Problem

A new owner finishes provisioning and the Get Started guide's **"Create your
first task"** step hands them off to a **blank** New Task dialog. With nothing
filled in, the user faces an empty form and doesn't know what to write — the
"it took me to a create-a-task page and I didn't know what to put there"
moment reported on prod.

Paperclip upstream avoids this deliberately: its design goal is *"end onboarding
with a real first task, not a blank dashboard."* Their web onboarding wizard
(`OnboardingWizardClassic.tsx` → `handleLaunch()`) ends by creating a real,
**editable**, pre-filled task **assigned to the CEO agent**, then navigates to
it. (Note: the CLI `paperclipai onboard --yes` creates **no** task — it only
writes config, runs doctor, and boots the server/UI. All task creation happens
in the browser wizard.)

We already auto-seed the equivalent of Paperclip's wizard steps 1–2 server-side
in `provision_org` (the company + **Friday** the CEO + **Atlas** the recruiter).
The only missing piece is the equivalent of their final step: a pre-filled,
editable first task assigned to the CEO.

## Goal

Make the Get Started **"task"** step open a **pre-filled, editable** New Task
dialog **assigned to Friday (CEO)**. The user edits the scaffold to their real
goal, hits Create, and lands on a populated Tasks board — mirroring how
Paperclip assigns its onboarding task to the CEO agent.

Non-goal: changing the plain toolbar "New task" button, which must stay blank.

## Current state (as-merged on `main`)

The Get Started guide is fully shipped:

- `apps/frontend/src/components/getstarted/getStartedConfig.ts:18` — the `task`
  step: `{ id: "task", title: "Create your first task", ctaLabel: "New task", … }`.
- `apps/frontend/src/components/chat/ChatLayout.tsx:787-803` — the `task` action
  builds a URL with `panel=…inbox`, `view=control`, and **`newIssue=1`**, then
  `router.push("/chat?…")`.
- `apps/frontend/src/components/teams/inbox/InboxPage.tsx:149-162` — reads
  `?newIssue` (`=== "1"`) to lazily open `newIssueOpen`, then strips the param.
- `apps/frontend/src/components/teams/inbox/InboxPage.tsx:372-381` — renders
  `<NewIssueDialog>` with `agents`, `projects`, `onCreated` (no default values).
- `apps/frontend/src/components/teams/issues/NewIssueDialog.tsx` — supports only
  `defaultProjectId`; `title`/`description`/`assigneeAgentId` start as `""`
  (`useState("")` at lines 81, 82, 89) and reset to empty in `reset()`.

So the wiring already routes the onboarding click to the dialog; the dialog just
opens empty.

## Design

Three small, in-pattern frontend changes.

### 1. `NewIssueDialog` accepts seedable defaults

Add three optional props alongside the existing `defaultProjectId`:

- `defaultTitle?: string`
- `defaultDescription?: string`
- `defaultAssigneeAgentId?: string`

Each seeds **both** the `useState` initial value **and** the `reset()` baseline,
exactly as `defaultProjectId` already does (`useState(defaultProjectId ?? "")`
+ `setProjectId(defaultProjectId ?? "")` in `reset()`). This keeps the
"reopening the dialog returns to its baseline" behavior consistent and means the
plain blank composer is unchanged when the new props are omitted.

The submit payload already reads `title` / `description` / `assigneeAgentId`
state, so no submit-path change is needed.

### 2. Shared first-task constant + Friday resolver

New module `apps/frontend/src/components/getstarted/getStartedFirstTask.ts`:

- `ONBOARDING_FIRST_TASK_TITLE` and `ONBOARDING_FIRST_TASK_DESCRIPTION` — the
  copy below, in one place so it's tunable.
- `resolveCeoAgentId(agents: CompanyAgent[]): string | undefined` — returns the
  seeded CEO's id. Resolution order, robust to user-created look-alikes:
  1. `a.seeded === true && a.role === "ceo"` (the canonical Friday),
  2. else `a.seeded === true && a.name.toLowerCase() === "friday"`,
  3. else `undefined` (dialog opens unassigned rather than guessing).

  `seeded` is server-stamped from the persisted seeded-agent IDs (never from
  role/name), so this never mis-assigns to a user agent named "Friday" or using
  role "ceo".

**Default copy (approved option C, CEO-framed, assigned to Friday):**

- Title: `Build out my team for [your goal]`
- Description:
  ```
  You are my CEO. Help me stand up a team to deliver [your goal].

  - Work out what roles and skills this goal needs.
  - Break it into concrete tasks and start delegating.
  - If a needed skill isn't on the team yet, bring it in.
  - Report back with the plan and the first tasks you've kicked off.
  ```

`[your goal]` is an obvious replace-me token in the editable fields.

### 3. Deep-link contract: `newIssue=onboarding`

Distinguish the onboarding pre-fill from the blank toolbar composer via the
existing `newIssue` query param:

- `ChatLayout.tsx` task action: change `params.set("newIssue", "1")` to
  `params.set("newIssue", "onboarding")`. Nothing else in that action changes.
- `InboxPage.tsx`:
  - Treat **both** `"1"` and `"onboarding"` as "open the dialog" (extend the
    existing `=== "1"` checks to `!== null` / a small set), and keep stripping
    the param afterward.
  - Track whether the open was the onboarding variant (e.g. a
    `newIssuePrefill` boolean derived from the param at the same point the open
    state is seeded).
  - When onboarding: pass `defaultTitle={ONBOARDING_FIRST_TASK_TITLE}`,
    `defaultDescription={ONBOARDING_FIRST_TASK_DESCRIPTION}`, and
    `defaultAssigneeAgentId={resolveCeoAgentId(agents)}` to `<NewIssueDialog>`.
    Otherwise pass nothing (blank, as today).

`InboxPage` already has `agents` in scope (it passes them to the dialog), so the
CEO resolution needs no new data fetch.

## Behavior

1. User clicks "New task" on the Get Started `task` step.
2. One navigation to `/chat?panel=…inbox&view=control&newIssue=onboarding`.
3. `InboxPage` opens `NewIssueDialog` pre-filled with the title + description,
   assignee = Friday, status `todo` (the dialog's existing default), and strips
   the param so a refresh doesn't reopen it.
4. User edits `[your goal]` (and anything else), hits **Create**.
5. The dialog's existing `onCreated` closes it and opens the new task; the
   existing `task` completion event still fires (Get Started step checks off).

## To verify during implementation

- **Run trigger:** does creating a `todo` task assigned to Friday auto-trigger a
  Friday run in our Paperclip wiring? Upstream treats issue-assignment as a
  trigger. If ours does too, that's *more* Paperclip-like and acceptable; if it
  doesn't, the task simply waits — also fine. Confirm and note the actual
  behavior; do not block on it.
- **`task` completion event:** confirm the existing `emitStep("task")` producer
  on create-success still fires for this path (it should — same dialog).
- **`CompanyAgent` fields:** `role` and `seeded` are confirmed present
  (`shared/types.ts:467-487`); confirm `agents` is populated at the moment the
  onboarding dialog opens (first-run timing) — if the roster hasn't loaded,
  `resolveCeoAgentId` returns `undefined` and the dialog opens unassigned, which
  the user can fix. Acceptable degradation.

## Testing

Targeted vitest only (never the full suite — ~17.7 GB RAM). CI runs
`tsc --noEmit` over test files, so annotate any mock-builder return types.

- `getStartedFirstTask.test.ts` — `resolveCeoAgentId` picks the seeded CEO,
  ignores a user agent named "Friday"/role "ceo", returns `undefined` when no
  seeded CEO exists; copy constants are non-empty and contain the `[your goal]`
  token.
- `NewIssueDialog` test — when `defaultTitle`/`defaultDescription`/
  `defaultAssigneeAgentId` are provided, the fields render seeded; when omitted,
  they render blank (no regression to the toolbar path).
- `InboxPage` test — `?newIssue=onboarding` opens the dialog with the seeded
  props; `?newIssue=1` opens it blank.

## Out of scope

- The plain toolbar "New task" composer (stays blank).
- Server-side task seeding in `provision_org` (rejected in favor of the
  editable step).
- Auto-running Friday on the task (only verified, not forced).
- Any change to the other Get Started steps.

## Files

- Modify: `apps/frontend/src/components/teams/issues/NewIssueDialog.tsx`
- Modify: `apps/frontend/src/components/teams/inbox/InboxPage.tsx`
- Modify: `apps/frontend/src/components/chat/ChatLayout.tsx`
- Create: `apps/frontend/src/components/getstarted/getStartedFirstTask.ts`
- Create tests as listed above.
