# Teams entry point + onboarding cleanup — design

**Date:** 2026-07-10
**Status:** Approved design, ready for implementation plan
**Scope:** Project A of a two-part effort. Project B (per-seat Teams billing) is captured
here as out-of-scope follow-on but ships immediately after.

## Problem

Right after login, `/onboarding` asks every user "How would you like to use Isol8?" —
**Personal** vs **Organization**. This is the wrong place for that decision:

- It interrupts every single signup, including the ~all of them that are personal.
- "Organization" isn't a real commercial tier today — picking it just mounts Clerk's
  `<CreateOrganization>` and sets the workspace `owner_id` to the `org_id` instead of
  `user_id`. Billing is the *identical* flat $50/mo shared pool. So the fork asks a
  loaded question ("are you a team?") with no pricing or product consequence attached.

The account type should be **implied by intent from the landing page**, not asked as an
interstitial. Teams should become a real, self-serve, per-seat tier surfaced in marketing —
not a free branch buried in onboarding.

## Decisions (locked)

- **Teams is a self-serve, per-seat tier.** Personal keeps the flat $50/mo price.
- **Seat economics: access-only, shared pool.** Per-seat price buys workspace access; all
  members still draw from one shared credit pool the admin funds (unchanged from today).
  Per-seat billing is purely the subscription *quantity* = member count.
- **No trial for Teams.** Personal keeps its 14-day Stripe trial; Teams pays from day one.
- **Per-seat dollar amount is a config/env value**, set when the Stripe price is created (Project B).
- **Project A first, B immediately after.** The A→B gap is a non-issue; A is designed toward
  the final per-seat end-state, not a throwaway interim.

## Current state (as of this design)

- **Onboarding fork:** `apps/frontend/src/app/onboarding/page.tsx`, mode state machine
  `"choose" | "personal" | "org" | "invitations"` (lines ~36-39). Choose-screen at
  lines ~234-269. Personal = `handlePersonal()` (writes `unsafeMetadata.onboarded=true`,
  `POST /users/sync`, → `/chat`). Org = Clerk `<CreateOrganization>` → `InviteTeammatesStep`
  (`POST /orgs/{orgId}/invitations`). Users with a pending org invite are force-routed to
  `"invitations"` mode.
- **Owner model:** `resolve_owner_id` (`apps/backend/core/auth.py:97-99`) → `owner_id = org_id`
  in org context else `user_id`. `is_org_owner_id` distinguishes by `org_`/`user_` prefix.
- **Billing:** identical flat $50 for personal and org owners
  (`apps/backend/routers/billing.py` `create_trial_checkout`, org callers must be admins).
  No enterprise tier exists (old free/starter/pro/enterprise ladder was deleted).
- **Landing pricing:** `apps/frontend/src/components/landing/PricingThreeCard.tsx` — flat
  `$50/mo`, "One price. Three ways to power it." Three *inference-provider* cards from
  `apps/frontend/src/lib/providerCards.ts`, each linking to `/sign-up?provider=<id>`.
  No team/org card.
- **Param threading is fragile:** `?provider=` rides the URL only. The onboarding gate
  (`apps/frontend/src/hooks/useOnboardingGate.ts:96`, `router.replace("/onboarding")`) and
  every onboarding exit navigate with bare paths, **dropping the query string**. Result: a
  fresh signup that passes through onboarding *loses* `provider` and the provider step is
  silently skipped. We must not repeat this for `intent`.

## Design — Project A

### A1. Simplify the onboarding state machine

Delete the choose-screen. New precedence in `onboarding/page.tsx`:

1. **Pending Clerk org invitation** → `invitations` mode. *Unchanged* — preserves the
   invariant that a user can't hold both personal tenancy and a pending org invite.
2. **`signupIntent === "team"`** → `org` mode (Clerk `<CreateOrganization>` +
   `InviteTeammatesStep`). *Internals unchanged.*
3. **Otherwise** → auto-run the personal path (`handlePersonal()`) behind a spinner.

Net effect: personal users never see an interstitial; onboarding is a silent pass-through.
The team path still exists, reached by intent rather than a question.

### A2. Thread `intent=team` via a one-hop URL param (not metadata)

Mechanism decision: the codebase already uses `unsafeMetadata` for the durable `onboarded`
flag, but for the team signal a one-hop URL param is the *simpler and safer* choice — it is
read synchronously (no async write/read race against the personal auto-complete path) and
needs no cleanup. Metadata is reserved for values that must survive *multiple* hops (see A3).

- Landing "Create a team" CTA → `/sign-up?intent=team`. Clerk carries the query string onto
  its post-signup redirect, landing the user on `/chat?intent=team`.
- `useOnboardingGate` (which runs on `/chat` and today redirects fresh signups to
  `/onboarding` with a **bare** path, dropping the query) is changed to **forward
  `intent`**: `router.replace("/onboarding?intent=team")`.
- `onboarding/page.tsx` reads `searchParams.get("intent")` synchronously on first render and
  selects the `org` branch, then lifts it into React state (`explicitMode = "org"`). That
  state survives Clerk's `CreateOrganization` same-route soft navigation
  (`afterCreateOrganizationUrl="/onboarding?intent=team"` as belt-and-suspenders), so the
  post-create render still mounts `InviteTeammatesStep` instead of falling through to the
  redirect branch. Nothing to clear — the signal is consumed into component state.

### A3. Deferred fast-follow — stop dropping `provider`

The same gate redirect also drops the `?provider=` param, so fresh signups routed through
onboarding lose their provider choice and the provider-specific ProvisioningStepper step is
silently skipped (a real funnel bug — see memory `project_onboarding_funnel_posthog_blindspot`).
The correct fix is **durable via `unsafeMetadata.providerChoice`** (this value must survive
multiple hops all the way to `/chat`, unlike `intent`), read as a fallback in
`ProvisioningStepper` and preserved through the merge-preserving onboarding-completion writes.
This is **intentionally deferred** out of Project A to keep the plan and its tests tight; the
Project A completion writes are already merge-preserving so the fix drops in cleanly.

### A4. Landing page Teams entry point

Add a **separate slim Teams strip** below the three provider cards in the landing pricing
section (Teams is a different axis from provider choice, so not a 4th provider card):

- Copy: "Isol8 for Teams — shared agents, one workspace, per-seat pricing."
- Per-seat price rendered from a single frontend constant/config (placeholder until the
  Stripe price is set in B).
- CTA "Create a team" → `/sign-up?intent=team`.

## Out of scope — Project B (per-seat Teams billing), ships immediately after

Captured so context isn't lost; **not** part of this spec's implementation:

- New Stripe **per-seat** price (quantity-based subscription); personal keeps the flat price.
- `create_trial_checkout` branches by owner type: personal → flat + 14-day trial; team →
  per-seat, quantity = current member count, **no trial**.
- Seat-count sync: Clerk org member join/leave (webhook) → update Stripe subscription quantity.
- Provision gate: conceptually unchanged (access-only shared pool); verify the team
  subscription-active / seat check.
- Config/env for the new price ID + per-seat amount; wire the landing price to it.

## Testing

- **Onboarding routing (frontend unit/component):** personal default runs `handlePersonal`
  with no choose-screen; `signupIntent=team` routes to the org branch; pending invitation
  still forces `invitations` mode and beats `signupIntent`.
- **Intent threading:** `intent=team` on the sign-up URL survives the onboarding-gate
  redirect via `unsafeMetadata`; `provider` likewise no longer drops.
- **Landing:** Teams strip renders, CTA points to `/sign-up?intent=team`.
- Run `tsc --noEmit` after adding tests (repo CI typechecks test files). Single-file vitest
  runs only — never the full frontend suite.

## Risks / notes

- Removing the fork removes the *only* current entry to org creation. Acceptable: the
  landing CTA replaces it and B follows immediately.
- `unsafeMetadata` is user-writable; it only steers UI routing here (never authorization),
  so that's fine. Backend owner/admin checks remain the source of truth.
