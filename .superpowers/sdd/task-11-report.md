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

---

# Task 11 (follow-up): Fix the two ESLint errors on this branch

## Status: DONE

## Diagnosis

The two lint errors flagged by the gate were real and introduced by this
branch:

1. `useGetStarted.ts:71` — `react-hooks/refs` ("Cannot access refs during
   render"): `doneRef.current = done;` was a render-time ref write (the ref
   kept `done` in sync so the stable `onStep` subscription could read the
   latest value). Writing a ref during render is the violation.

2. `InboxPage.tsx:145` — `react-hooks/set-state-in-effect`: the `?newIssue=1`
   auto-open effect called `setNewIssueOpen(true)` synchronously inside a
   `useEffect`.

A third, latent error surfaced only after fixing #1: `useGetStarted.ts:59`
(`react-hooks/set-state-in-effect` on the Clerk-metadata hydration effect's
`setDone(...)`). It was masked because the react-hooks plugin halts analysis of
a component at the first hard `react-hooks/refs` violation. Since the whole file
is new on this branch, it too is "introduced by this branch" and had to be
cleared to reach 0 errors.

## Fix 1 — useGetStarted.ts (refs during render)

Moved the ref sync into an effect, exactly the minimal fix:
```ts
const doneRef = useRef(done);
useEffect(() => { doneRef.current = done; }, [done]);
```
The `onStep` handler runs after commit, so it still reads the latest committed
`done`. Behavior (event completion + persist, idempotent append) preserved.

Also cleared the unmasked hydration-effect error using the established repo
convention for external-system→React sync (same pattern as
`settings/connections/page.tsx:84` and `marketplace/ListingDetailView.tsx:43`):
wrapped the hydration `setX(...)` block in a scoped
`/* eslint-disable react-hooks/set-state-in-effect */` … `/* eslint-enable */`
pair with a comment explaining it's the sanctioned async-load hydration (must
run when `isLoaded` flips; also fires `capture()`/`persist()` side effects, so
it cannot move to render-time). Guarded once via `hydratedRef`.

## Fix 2 — InboxPage.tsx (setState in effect)

Replaced the effect-driven open with lazy-init + a during-render derived
transition (no effect-setState), keeping a strip-only effect:
```ts
const newIssueParam = searchParams.get("newIssue");
const [newIssueOpen, setNewIssueOpen] = useState(() => newIssueParam === "1");
const [prevNewIssueParam, setPrevNewIssueParam] = useState(newIssueParam);
if (newIssueParam !== prevNewIssueParam) {
  setPrevNewIssueParam(newIssueParam);
  if (newIssueParam === "1") setNewIssueOpen(true);
}
useEffect(() => {
  if (newIssueParam !== "1") return;
  const params = new URLSearchParams(searchParams.toString());
  params.delete("newIssue");
  router.replace(`?${params.toString()}`, { scroll: false });
}, [newIssueParam, searchParams, router]);
```
- Lazy-init opens on first render reaching the page with `?newIssue=1`.
- The during-render previous-param compare (React's "adjust state when a prop
  changes" pattern — the same one the selection clamp below already uses, and
  NOT setState-in-effect) reopens it when the user re-navigates with the param
  while the inbox panel is already mounted. This case is real: ChatLayout.tsx:691
  `router.push("?panel=...inbox&newIssue=1")` only changes the URL when the panel
  is already mounted.
- The effect now only calls `router.replace` (allowed — not setState) to strip
  the param. Trace confirms open-once + strip with no reopen loop.

## Verification (targeted only)

1. `pnpm exec eslint src/components/getstarted/useGetStarted.ts src/components/teams/inbox/InboxPage.tsx` → **exit 0, 0 errors**.
2. `pnpm exec vitest run src/components/getstarted` → **8 files, 15 tests passed** (derived signals, event completion+persist, first-run auto-open, returning-user re-hydration all green).
3. `pnpm exec vitest run src/__tests__/components/teams/inbox/InboxPage.test.tsx` → **1 file, 15 tests passed**. (NOTE: the spec'd path `src/components/teams/inbox` has no co-located tests — "No test files found, exit 1"; the actual InboxPage suite lives under `src/__tests__/components/teams/inbox/` and passes.)
4. `pnpm exec vitest run src/components/chat/__tests__/ChatInput.focus.test.tsx` → **1 file, 1 test passed**.
5. `pnpm exec tsc --noEmit` → **exit 0**.

---

# Final code-review fixes (2026-06-25)

Applied three review findings to the Get Started guide, all in
`apps/frontend/src/components/getstarted/`.

## Fix 1 — IMPORTANT: "Dismiss" permanently bricked the re-openable launcher
`GetStarted.tsx` early-returns a launcher-only render (`onClick={gs.open}`)
when `gs.dismissed`, but `open()` never cleared `dismissed` (persisted to
Clerk `unsafeMetadata`), so the launcher became a permanent dead button
across sessions/devices.

- `useGetStarted.ts` `open()` now also `setDismissed(false)` +
  `persist({ dismissed: false })`. `dismissed` is treated purely as
  "don't auto-open"; `seen` already prevents auto-open, so clearing
  `dismissed` on explicit reopen is correct. The next render then sees
  `dismissed=false` + `isOpen=true`, so the card is reachable again.
- `GetStarted.tsx` unchanged — its early-return still renders the launcher,
  so the launcher is always visible.
- Regression test added to `__tests__/useGetStarted.test.tsx`:
  dismiss() → `isOpen=false` + `dismissed=true` (and persisted), then
  open() → `isOpen=true` + `dismissed=false` (and persisted false).

## Fix 2 — MINOR: `get_started_completed` re-fired on every load
`completedFiredRef` was mount-scoped, so a fully-onboarded returning user
emitted `get_started_completed` on every `/chat` load.

- Added `completedFired?: boolean` to `GetStartedMeta`.
- `completedFiredRef` is now seeded from `stored.completedFired` at the
  ref declaration, and re-seeded in the hydration effect
  (`completedFiredRef.current = !!s.completedFired;`) for the hard-reload
  null-at-mount case.
- The firing effect now also `persist({ completedFired: true })` (and the
  effect deps gained `persist`). Net: fires at most once ever per user.
  Uses the existing merge-don't-clobber persist pattern.

## Fix 3 — MINOR: document the single-emit assumption
Added a one-line WHY comment at the `onStep` handler noting that the
read-then-append of `doneRef.current` is safe only because the producers
(chat send / task create / hire install) are distinct one-shot user
actions that never co-fire in the same synchronous tick.

(Did NOT touch focus-from-panel behavior or the persist-prev read — review
accepted those.)

## Verification (targeted only — never the full suite)

1. `pnpm exec vitest run src/components/getstarted` → **8 files, 16 tests
   passed** (was 15; +1 new dismiss→reopen regression test, all prior tests
   still green).
2. `pnpm exec tsc --noEmit` → **exit 0**.
3. `pnpm exec eslint src/components/getstarted/useGetStarted.ts
   src/components/getstarted/GetStarted.tsx` → **exit 0, 0 errors**.
   (Also linted the modified test file → exit 0.)
