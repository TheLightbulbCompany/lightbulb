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

**Separation of responsibilities (revised after review):** the account-type decision lives in
`useOnboardingGate` (which already owns post-signup routing on `/chat`), NOT on the
`/onboarding` page. `/onboarding` is only the authenticated setup surface for the two flows
that need one: **team creation** and **invitation acceptance**. Personal signups never touch
it. This keeps each surface single-purpose and removes the invite-vs-personal races that arise
when personal completion shares a page with invitation acceptance.

### A1. `useOnboardingGate` owns personal completion; only team/invite redirect

The gate runs on `/chat` and, once Clerk state is fully loaded, routes a fresh signup:

1. **Pending invitation** → `router.replace("/onboarding")` (accept surface). *Tenancy
   invariant unchanged — pending invitations still beat the onboarded flag.*
2. **Team intent** (see A2) → `router.replace("/onboarding?intent=team")` (org creation).
3. **Otherwise (personal)** → completed **inline**: best-effort merge-preserving
   `unsafeMetadata.onboarded = true` write, and the gate returns `ready` so the user drops
   straight into `/chat`. No `/onboarding` detour, no chooser, no spinner page. The flag is
   non-critical for routing (personal always resolves to `ready`), so a failed write just
   self-heals on the next login.

The gate only decides once `clerkLoaded` is true, preserving the "don't act before org context
settles" guard that prevents the phantom personal-billing race.

### A2. `/onboarding` handles only team creation + invitation acceptance

`onboarding/page.tsx` has no personal path. Its effect only:

- **Path 1** — org already active → mark onboarded + redirect to `/chat`.
- **Path 2** — has memberships, no active org → `setActive` the first, then redirect.
- **Else** — no org/membership: if an invite accept is **in flight** (`acceptingId` set), HOLD
  on a spinner (Clerk has dropped the invitation but the membership hasn't propagated — Path 2
  fires when it lands; redirecting now would bounce the invitee through the gate during that
  gap and risk mis-classifying them as personal). Otherwise a stray visitor → `/chat`.

`intent=team` selects the `org` mode (Clerk `<CreateOrganization>` + `InviteTeammatesStep`),
lifted into React state so it survives the `afterCreateOrganizationUrl="/onboarding?intent=team"`
soft navigation.

### A2b. Team-intent transport: a signup cookie (not URL, not metadata)

**Clerk does NOT reliably carry an arbitrary `?intent=` query through its multi-step signup
flow** (email verification / SSO callback are distinct sub-routes that drop the original
query), and its post-signup redirect uses the configured `fallbackRedirectUrl`. So relying on
the URL is unsafe for the feature's core path. Instead:

- The landing "Create a team" CTA writes a short-lived cookie (`isol8_signup_intent=team`,
  ~30-min TTL) client-side, and links to `/sign-up?intent=team` (the query still helps
  single-step signups).
- `useOnboardingGate` computes team intent from the URL query **or** the cookie, and on the
  team redirect **consumes and clears** the cookie so a later personal signup in the same
  browser isn't mis-tagged.

The cookie survives every signup sub-route and the final redirect — the URL query and
`unsafeMetadata` do not (the latter can't be written by Clerk's prebuilt `<SignUp>`).

### A3. Deferred fast-follow — stop dropping `provider`

The gate historically dropped the `?provider=` param on its `/onboarding` redirect, so fresh
signups lost their provider choice and the provider-specific ProvisioningStepper step was
silently skipped (a real funnel bug — see memory `project_onboarding_funnel_posthog_blindspot`).
The correct fix is durable via `unsafeMetadata.providerChoice` (it must survive multiple hops
to `/chat`), read as a fallback in `ProvisioningStepper`. **Intentionally deferred** out of
Project A. (Note: with personal signups no longer redirected through `/onboarding`, the
provider param now reaches `/chat` directly for the common personal case; the metadata fix
remains the robust general solution.)

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

- **Gate routing (`useOnboardingGate`):** a fresh personal signup completes inline
  (`unsafeMetadata.onboarded` write) and returns `ready` with no `/onboarding` redirect;
  `intent=team` returns `redirecting` → `/onboarding?intent=team`; pending invitations still
  redirect to `/onboarding` and beat the onboarded flag.
- **Onboarding page:** `intent=team` renders `<CreateOrganization>`; pending invitations force
  the accept screen (no bounce); a stray visitor with neither is redirected to `/chat`.
- **Cookie transport (`signupIntent`):** `readSignupIntentCookie` parses the value among other
  cookies, url-decodes, and returns null when absent or for a name that merely contains the key.
- **Landing:** Teams strip renders, CTA points to `/sign-up?intent=team`.
- Run `tsc --noEmit` after adding tests (repo CI typechecks test files). Single-file vitest
  runs only — never the full frontend suite.

## Risks / notes

- Removing the fork removes the *only* current entry to org creation. Acceptable: the
  landing CTA replaces it and B follows immediately.
- `unsafeMetadata` is user-writable; it only steers UI routing here (never authorization),
  so that's fine. Backend owner/admin checks remain the source of truth.
