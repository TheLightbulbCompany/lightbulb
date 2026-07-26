# Budget-exhaustion UX — design

**Date:** 2026-07-26
**Status:** approved, pending implementation plan

## Problem

A free-tier (`trial_local`) owner who exhausts their daily allowance is shown
**"Provider rate limit reached — wait for your quota to reset, upgrade your plan
with the provider, or switch this workspace to Isol8's Bedrock-managed
credits."** Every clause is wrong: the provider has no quota problem, there is
no provider plan to upgrade, and the owner is *already* on Bedrock-managed
credits. There is no upgrade CTA anywhere in the flow.

Observed in prod 2026-07-26, owner `user_3H1NQ7qf9BGed0PA0eE1WRWHH5i`:

```
billing_accounts/user_3H1NQ7qf9BGed0PA0eE1WRWHH5i
  subscription_status              = trial_local
  trial_daily_spend_microcents     = { "2026-07-26": 2758514 }   # $2.7585
  reserved_microcents              = { "2026-07-26": 0 }

containers/user_3H1NQ7qf9BGed0PA0eE1WRWHH5i
  status          = running
  bedrock_revoked = <absent>          # backstop kill never fired
```

`Isol8/BedrockGate → gate.trial_daily_cap` recorded 11 denials in 24h, all this
owner.

## Root cause

Two components disagree about where the cap is.

| | denies when | effective threshold |
|---|---|---|
| bedrock-gate, trial (`ledger.mjs:60`) | `spent + reserved + estimate > cap` | **$2.70** |
| backend `gate_chat` L2 (`connection_pool.py:2506`) | `spent >= cap` | **$3.00** |
| bedrock-gate, paid (`ledger.mjs:179`) | `remaining - reserved - estimate <= 0` | **$0.30 left** |
| backend `_row_blocked` (`metronome_gate.py`) | `remaining <= 0` | **$0 left** |

`CAP_MICROCENTS = 3_000_000`, `RESERVE_ESTIMATE_MICROCENTS = 300_000`
(`bedrock-gate-stack.ts:170,173`).

This opens a **$0.30 dead zone on both plans** where the backend forwards the
turn, the gate refuses it, and every UI surface reports the owner is fine.

The failure chain:

1. `gate_chat` reads $2.7585 < $3.00 → not blocked → forwards to OpenClaw.
2. Gate returns `429 ThrottlingException`, `"isol8: daily free limit reached"`,
   header `x-isol8-gate-code: trial_daily_cap` (`errors.mjs:41`).
3. OpenClaw retries across model candidates; all 429. With ≥2 attempts
   `model-fallback.ts:741` builds a `FallbackSummaryError` that **discards the
   upstream message**, yielding `errorKind: "rate_limit"` and *"All models are
   temporarily rate-limited."* (`agent-runner-failure-reply.ts:71`).
4. Backend forwards `code=rate_limit` verbatim (`connection_pool.py:1824`).
5. `ChatErrorCard` renders the provider-rate-limit card.

**The dead zone is a permanent trap, not a transient one.** A refused request
generates no tokens, so nothing settles, so `spent` never advances past
$2.7585 — it can never reach $3.00, so `gate_chat` never fires and the correct
`trial_daily_cap` card is unreachable for the rest of the UTC day.

### Why not fix it upstream

- The gate's `x-isol8-gate-code` header is dead weight: OpenClaw does not read
  response headers into `errorKind`. Propagating it requires a fork patch.
  **Rejected** — no fork change if avoidable.
- Pattern-matching the gate's message text works at one attempt
  (`model-fallback.ts:741` rethrows the original) and silently fails at two or
  more — i.e. it would pass in dev and break in exactly the observed case.
  **Rejected** as fragile and coupled to upstream internals.

## Scope

Three pieces (A, B, C). The Paperclip routine auto-pause after budget refusals
is **explicitly out of scope** and filed separately — see "Deferred".

---

## A — one admission-threshold derivation

The gate is the only component that actually decides admission. Everything else
must predict *its* answer rather than re-derive a looser one.

Backend adopts the gate's inequality:

- `trial_cap.reason_for_account` → `DAILY_CAP` when
  `spent + RESERVE_ESTIMATE_MICROCENTS > CAP_MICROCENTS`
- `metronome_gate._row_blocked` → blocked when
  `remaining <= RESERVE_ESTIMATE_MICROCENTS`
- `OutOfCreditsBanner` → renders when `balance_microcents <= RESERVE_ESTIMATE_MICROCENTS`
  (today: `> 0` returns null). Without this the banner keeps the paid dead zone
  open on the UI side even after the backend predicate is fixed: an owner with
  $0.20 left would be refused by the gate while the banner stays hidden. The
  indeterminate case (`null` balance — Metronome unavailable) keeps its existing
  never-show-a-false-banner behaviour.

**`reserved` is deliberately omitted.** Holds are transient and released on
settle, so folding them into the backend predicate would over-block an owner
with two runs legitimately in flight. `spent` is the durable floor, and it is
what makes the dead zone permanent — which is the bug being fixed. Omitting
`reserved` is never *under*-blocking in the stuck case
($2.7585 + $0.30 > $3.00 → blocked).

`RESERVE_ESTIMATE_MICROCENTS` becomes a backend constant in documented lockstep
with `bedrock-gate-stack.ts`, matching the existing three-copy `CAP_MICROCENTS`
discipline. Reading it from SSM the way `bedrock_gate_url` does was considered
and rejected as overkill for a constant that effectively never changes.

Because both plans share one reserve estimate, this single change closes the
free-tier *and* paid-credit dead zones.

### Reset behaviour (verified, unchanged by this work)

`trial_daily_spend_microcents` and `reserved_microcents` are **date-keyed maps**.
A new UTC day is a new key, so both read 0 by pure derivation — no worker, no
sweeper, no user action. At 00:00 UTC the gate's next check reads
`0 + 0 + $0.30 <= $3.00` → admit. `gate_chat` L2 and
`ecs_manager.start_user_service` both read through `utc_today()`, so every door
clears in lockstep.

`bedrock_revoked` + the IAM policy + the gate-token deny flag do *not* self-clear
— they are cleared only by `maybe_restore_trial_bedrock` on the
`/container/status` poll (`container.py:280`). That path is unreachable in this
failure mode: the revoke fires only when the counter crosses the cap, which the
dead zone prevents. Confirmed in prod (container `running`, no `bedrock_revoked`).

**Therefore the "resets at midnight UTC" promise in the C copy is true as
written.**

---

## B — mid-run reclassification

A turn admitted under the threshold can still cross it mid-loop. The gate 429s,
OpenClaw reports `rate_limit`, and the user gets the wrong card. A pre-send
check cannot prevent this; the classification must be corrected on the way out.

In `connection_pool.py`'s `state == "error"` branch, where `errorKind` is
already read and forwarded:

```
err_kind == "rate_limit"
  └─ is the owner's inference Isol8-funded?      (trial_local, or paid bedrock_claude)
       ├─ no  (ChatGPT OAuth / BYOK) → leave untouched
       └─ yes → re-read budget state
                  (trial_cap.reason_for_account / metronome_gate.is_owner_blocked)
                ├─ money reason found → swap `code`, substitute our copy
                └─ none → leave untouched (genuine Bedrock throttle)
```

The substituted `code` and `message` are **the exact values `gate_chat` already
returns** for that reason (`connection_pool.py:2508-2523, 2535-2539`) — not a
second copy. A turn refused before it starts and a turn refused mid-loop are the
same event to the user and must render the identical card.

**Provider-awareness is load-bearing.** OAuth and BYOK owners consume no Isol8
budget; a 429 on those paths is a real third-party throttle, and relabelling it
would route the user to a billing page for someone else's quota. This mirrors
`gate_chat`'s existing provider gate — the same reasoning that made the
wake-admission gate reuse `gate_chat` rather than a trial-only predicate.

Cost: one DDB read, on error frames only.

**Masking guard.** The upstream message rides along under a separate key so
logs and support retain the true cause, and the `chat.error` metric gains a
`reclassified` dimension. If we ever begin mislabelling real throttles it
surfaces as a visible spike rather than a silent lie.

The reclassification lives inline in the gateway error branch, calling the
existing shared predicates. No new policy module — `trial_cap` and
`provision_gate` already own that policy.

---

## C — the wall, done well

No ambient usage meter, no always-on cost counter. Fix the wall.

### `ChatErrorCard`

Rewrite `trial_daily_cap` / `trial_expired`:

```
⚠ Daily free limit reached
  You've used today's free usage.
  Resets at midnight UTC — in about 4 hours.
  [Upgrade to keep going]
```

"in about N hours" is **computed client-side from the next UTC midnight**, not
hardcoded and not server-supplied — the same local-clock pattern `TrialBanner`
already uses via `useSyncExternalStore`. Round to hours; below one hour say
"in under an hour". `trial_expired` omits the reset line entirely — expiry is
permanent, and `trial_cap._is_expired` never self-clears.

**No dollar figure at the wall.** After A the wall fires at $2.70 of usable
spend while Settings advertises "$3/day of included usage". Printing an exact
number at the wall would contradict that; omitting it keeps the $0.30 an
invisible safety margin and leaves the Settings headline true. Raising
`CAP_MICROCENTS` to $3.30, and advertising the real $2.70, were both considered
and rejected — the first raises the real per-owner daily ceiling 10%, the second
puts an odd number in front of prospects.

Add the two missing cases. Both are emitted by `gate_chat` today and both fall
through to `default` → "Failed to generate", which is what a null-pointer would
render:

- `out_of_credits` → top-up copy + CTA into the credits flow
- `subscription_inactive` → reactivate copy + CTA

**Suppress Retry on all money codes.** Retry against a budget wall is
guaranteed to fail and reads as product flakiness rather than an exhausted
allowance.

### Fix the cross-route CTA (acceptance criterion)

`ChatErrorCard.tsx:142` renders its action as a bare `<a>` wired to
`handleShellLinkClick`, which unconditionally does:

```ts
e.preventDefault();
window.history.pushState(null, "", url);
```

That helper exists for **in-shell** navigation — `?panel`, `?agent`, `?detailId`
on the route the shell is already on. All 19 other call sites pass a `/chat?…`
URL built by `teamsPanelUrl` / `teamsDetailUrl` / `agentInspectorUrl`.
`ChatErrorCard` is the only call site passing a **cross-route** URL
(`/settings?panel=billing`) — and it is used by exactly the two cards this work
makes load-bearing. `pushState` to a different pathname updates the URL bar and
`usePathname` but does not render the `/settings` route.

`TrialBanner` and `OutOfCreditsBanner` target the same destinations with a real
Next `<Link>` and navigate correctly; the error card is the odd one out. The
existing `ChatErrorCard.shellNav.test.tsx` only covers the in-shell `rate_limit`
→ `/chat?panel=llm` case, so nothing caught it.

**Fix:** cross-route actions render as `<Link>`; `handleShellLinkClick` is
retained for in-shell targets only. To be confirmed live in the browser, not
only by test — this is the user's stated acceptance criterion.

The rest of the subscribe chain is verified sound: Settings' free-tier card
renders **"Subscribe — $50/mo"** for `trial_local` including the expired state →
`startTrialCheckout` → `POST /billing/trial-checkout` with
`provider_choice: bedrock_claude` → Stripe Checkout URL → redirect.
`create_trial_checkout` creates the Stripe customer when the row has none, which
is the `trial_local` case.

### Sidebar chip

`WorkspaceSidebar.tsx:304` and `ChatLayout.tsx:466` render `{subscriptionStatus}`
unmapped, so CSS uppercases the raw enum to **`TRIAL_LOCAL`**. Settings already
has the correct mapping (`statusChipLabel` → "Free tier" / "Free tier ended").
Extract it into a shared helper so one mapping serves all three call sites.

---

## Testing

**Backend**
- `trial_cap.reason_for_account` boundary: 2,699,999 / 2,700,000 / 2,700,001
  microcents (the gate uses `>`, so 2,700,000 must still admit).
- `_row_blocked` boundary at `remaining` = 299,999 / 300,000 / 300,001.
- Reclassification: `rate_limit` + capped `trial_local` → `trial_daily_cap`;
  `rate_limit` + capped paid Bedrock → `out_of_credits`; `rate_limit` + **OAuth
  owner** → unchanged (the load-bearing negative case); `rate_limit` + funded
  owner → unchanged.

**Frontend**
- `ChatErrorCard` renders correct title/copy/CTA for all four money codes.
- Retry is absent on money codes, present on `timeout` / `unknown`.
- Cross-route action uses `Link` (navigates), in-shell action still uses
  `pushState`.
- Shared status-chip helper maps `trial_local` → "Free tier" in both shells.

**Live**
- Browser check: wall card → Upgrade → Stripe Checkout, on desktop
  `WorkspaceShell`.

---

## Deferred

**Paperclip routine auto-pause after budget refusals.** A budget-refused wake
parks the run, but the routine circuit breaker still counts it toward
`consecutiveFailureCount`, so enough refusals set
`autoPauseReason="consecutive_failures"` and the routine stays paused after the
cap resets, needing manual resume. Flagged in #1426's closing note as needing
its own issue; it is a distinct mechanism and cannot change the wall card
designed here. To be filed with the evidence gathered above.

## Prior art

- **#1426** (closed) — onboarding kickoff exhausts the trial cap. Shipped fix
  directions 1, 2, 4 (burn prevention: #1454, #1445, #1451, Paperclip
  retry-parking). **Direction 3 — "surface the reason: the gate's own message
  should reach the inbox item / limit card" — was never shipped.** This design
  is that direction.
- **#1427** (closed) — `trial_local` billing page showed no subscribe CTA.
