# Task 11 Report: Analytics + final targeted verification

## Status: COMPLETE

## Capture sites added

All `capture(...)` calls import from `@/lib/analytics` (the single PostHog
touchpoint — `capture` is a typed, SSR-safe, init-gated no-op).

### `apps/frontend/src/components/getstarted/useGetStarted.ts`
- `import { capture } from "@/lib/analytics";`
- `capture("get_started_opened")` in `open()` (return-object handler).
- `capture("get_started_opened")` in the first-run auto-open path — the
  hydration effect's `if (!s.seen) { ... }` branch.
- `capture("get_started_dismissed")` in `dismiss()`.
- `capture("get_started_article_opened", { id })` at the top of `openArticle(id)`.
- `capture("get_started_completed")` — new `completedFiredRef` (`useRef(false)`) +
  effect on `[progress.done, progress.total]`; fires exactly once, only when
  `total > 0 && done === total`.

### `apps/frontend/src/components/getstarted/GetStarted.tsx`
- `import { capture } from "@/lib/analytics";`
- `capture("get_started_step_clicked", { step: id })` as the first line of
  `onAction` (before `gs.minimize()` / `actions[id]()`).

### `apps/frontend/src/lib/analytics.ts`
- Added a `// Get started guide` group of 5 entries to the strict
  `AnalyticsEvent` union (`get_started_article_opened`, `get_started_completed`,
  `get_started_dismissed`, `get_started_opened`, `get_started_step_clicked`).
  **Required** — the union is strict, so without this `tsc` would reject every
  new `capture(...)` call. Kept alphabetical within the group.

## Keeping existing tests green + pristine

Both tests now transitively invoke the analytics module. Following the existing
repo-wide convention (e.g. `chat/__tests__/ChatInput.test.tsx`), added
`vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));` to:
- `__tests__/useGetStarted.test.tsx` (exercises the real hook → real captures).
- `__tests__/GetStarted.test.tsx` (mocks the hook, but the `GetStarted`
  component's own `onAction` capture still runs).

This keeps the real PostHog SDK out of the test path and output pristine. No new
analytics tests added (brief: fire-and-forget, none required).

## Verification

### 1. `pnpm exec vitest run src/components/getstarted` — PASS
```
Test Files  8 passed (8)
     Tests  15 passed (15)
```
Output pristine (only the unrelated node `module.register()` DeprecationWarning).

### 2. `pnpm exec tsc --noEmit` — PASS (exit 0, no output)

### 3. `pnpm run lint` — exit 1, but NO new errors from this task
Full lint reports 2 errors + 24 warnings. Both errors are **pre-existing /
unrelated**, not introduced by Task 11:
- `useGetStarted.ts:71` `react-hooks/refs` (`doneRef.current = done`) — proven
  pre-existing: stashing my changes and linting the committed HEAD version still
  reports it (at line 70). I never touched that line (see `git diff HEAD`).
- `teams/inbox/InboxPage.tsx:145` `set-state-in-effect` — unrelated teams code.

Linting only the files I touched confirms zero new problems:
- `GetStarted.tsx`, `GetStarted.test.tsx`, `useGetStarted.test.tsx`,
  `analytics.ts` → eslint exit 0, clean.
- `useGetStarted.ts` → only the pre-existing `react-hooks/refs` error.

## Concerns
None. The lone lint error in a file I edited is pre-existing and on an untouched
line (proven by stash + lint of HEAD). Everything else is green.
