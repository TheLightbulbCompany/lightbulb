# Event-Triggered Provisioning — Design

**Status:** DESIGN ONLY — not implemented, no code changed by this doc.
**Date:** 2026-07-26
**Parent:** `2026-07-24-onboarding-architecture-remediation-design.md` — **Property A**
("an unorchestrated distributed saga driven by a client poll").
**Builds on:** `2026-07-25-seed-leg-executor-design.md` (shipped as #1456).

---

## 1. What this changes, in one paragraph

Today a browser poll (`GET /container/status`, every ~3s) is what *causes*
provisioning. This design moves the **trigger** to server-side events — a Clerk
webhook for personal signup and org membership, a Stripe webhook for org
subscription — while keeping the poll as an **idempotent fallback that is never
removed**. The poll stops being the thing that starts provisioning and becomes
what it always should have been: a read, plus a safety net.

The design is deliberately *additive*. Its correctness does not depend on the
webhooks working. If every webhook is misconfigured, undelivered, or disabled,
behaviour degrades exactly to today's.

---

## 2. Why now: #1456 changed the calculus

The historical objection to a second provisioning trigger was that concurrent
triggers double-provision. That objection is dead:

- `container_repo.try_claim_provision` is a **DynamoDB conditional write** that
  elects exactly one winner per owner across *all* backend tasks (this matters
  even at `desiredCount=1` — rolling deploys run at `maximumPercent=200`).
  Claimable states: no row / `error` / `stopped` / stale `provisioning` lease.
  **Not** claimable: `running`, or a fresh lease.
- `core/services/provision_dispatch.dispatch_provision` stamps the execution
  name in that same atomic write and calls `StartExecution`, which is idempotent
  for STANDARD workflows on identical name+input.
- The Paperclip owner seed (`provision_org`: Better-Auth signup + company +
  Friday/Atlas) now runs as **step 0 inside the runner**
  (`scripts/provision_runner.py::_seed_owner_paperclip`), i.e. under the same
  single-flight. This is what closed #1442's duplicate-company window.

So the question is no longer *"is a second trigger safe?"* (it is — whoever
claims first wins, everyone else no-ops) but *"why is a client poll the
trigger at all?"*. The remaining costs of poll-as-trigger:

1. **Latency.** Provisioning starts when a browser tab decides to ask, not when
   the owner becomes eligible. A user who signs up and closes the tab gets
   nothing until they come back.
2. **Load.** Every fresh signup fans a gate evaluation (billing read + possibly
   a Clerk invitation lookup + a Metronome fast-gate read) across every open tab
   every 3s.
3. **Diagnosability.** "Why did this owner never provision?" currently has the
   answer "nobody polled", which is invisible server-side.
4. **It keeps the causal edge in the client**, which is the structural property
   (#A) the remediation doc says keeps generating new race findings as new legs
   are added.

---

## 3. The key insight: the trigger is "owner became eligible", not "user created"

`core/services/provision_gate.evaluate_provision_gate` is the *definition* of
eligibility. The correct trigger for provisioning is therefore **the transition
of that gate from up → down**, not any particular identity event. Getting this
right is what lets personal and org onboarding both work correctly, instead of
org being bolted onto a personal-shaped flow.

The transition happens at different moments per tenancy:

### 3.1 Personal owner

- `ensure_personal_trial_default` writes a card-less `trial_local` row for a
  brand-new personal owner (`user_provision.py:59-111`).
- `trial_local` is in `_ACTIVE_STATUSES`, and the gate has an explicit
  `trial_local` short-circuit that **skips the provider-required layer**
  (`provision_gate.py:589-596`) — a trial owner is provision-eligible with no
  stored provider.
- ⇒ **The gate clears the instant the trial row exists.** Nothing else is
  needed. So `user.created` genuinely *is* the eligibility event for a personal
  owner.

### 3.2 Org owner

- **There is no org free tier.** `ensure_personal_trial_default` returns
  immediately for org context (`user_provision.py:84-85`), and the gate returns
  `team_billing_required` for an org with no billing row
  (`provision_gate.py:476-482`).
- An org is gated until a per-seat subscription is live. `create_team_checkout`
  writes the customer row **and** `provider_choice` synchronously
  (`routers/billing.py:647,672`) *before* the Stripe session, so at the moment
  the subscription goes live the gate's Layer 1 and Layer 2 are both satisfied.
- ⇒ **The natural trigger is a Stripe event, not `organization.created`.**
  Clerk org creation carries no eligibility information at all.

### 3.3 Org member

- A member joining an already-subscribed org needs **no container** — they ride
  the shared org box. What they need is a Paperclip membership row, or every
  Teams surface 401s on them (the silent orphan-member incident).
- `_ensure_paperclip_for_member` is join-only by construction (it never creates
  a company), which is precisely why `ensure_user_ready` is already allowed to
  call it on the **gate-up** branch (`user_provision.py:165-180`).
- ⇒ **Trigger: `organizationMembership.created`. Dispatches a membership add,
  never a provision.**

### 3.4 The generalization (named here, deferred to a follow-up)

Layers 2 and 3 of the gate mean a Stripe subscription is *necessary but not
sufficient* for an org: an org that picked ChatGPT OAuth still has
`oauth_required` up until the device-code flow completes. The complete set of
"gate goes down" transitions is:

| Transition | Owner scope | Currently triggered by |
|---|---|---|
| `trial_local` row created | personal | poll |
| Stripe subscription enters `active`/`trialing` | personal + org | poll |
| ChatGPT OAuth device flow completes | personal + org | poll |
| BYO key + `byo_provider` saved | personal + org | poll |
| Metronome credit top-up clears `credits_required` | personal + org | poll |
| Trial daily cap resets / Bedrock restored | personal | poll |

The last four are **in-process backend code paths** (`routers/oauth.py`,
`routers/settings_keys.py`, `routers/metronome_webhook.py`,
`provision_gate.maybe_restore_trial_bedrock`) — none of them currently dispatch
anything; all of them wait for the next poll. Once §4's entry point exists,
wiring each is a one-line call with no webhook involved. **Explicitly out of
scope for this change** (they are latency wins, not race fixes) but the entry
point is designed so they cost one line each.

---

## 4. Architecture

### 4.1 The one new thing: an auth-free, owner-scoped entry point

`ensure_user_ready(auth)` cannot be called from a webhook: it takes an
`AuthContext` (a JWT-derived dataclass) and a webhook has no JWT. Rather than
synthesize fake auth contexts at three call sites, extract the owner-scoped
half:

```python
# core/services/user_provision.py

class ProvisionTriggerResult(str, Enum):
    DISPATCHED = "dispatched"     # execution started (or idempotently re-affirmed)
    GATED = "gated"               # gate up — nothing to do, not an error
    NOT_CLAIMABLE = "not_claimable"  # running / fresh lease — someone else owns it

async def ensure_owner_provisioned(owner_id: str, *, trigger: str) -> ProvisionTriggerResult:
    """Evaluate the provision gate for OWNER_ID and, if it is clear, dispatch
    the provisioning executor. No AuthContext, no request, no side effects
    beyond the dispatch. Safe to call from any trigger, any number of times.
    """
```

Semantics:

- `is_admin=True` is correct by construction — the caller is owner-scoped, not
  member-scoped. (Personal owners are admins of themselves; an org `owner_id`
  reached from a Stripe subscription is the org's own billing row.)
- On a clear gate it calls `dispatch_provision(owner_id)` — the same funnel the
  poll uses, so there is exactly one provisioning path in the system.
- It never raises. A gate being up is a normal return, not an error.
- It emits `provision.trigger{source, result}` (see §8).

`ensure_user_ready`'s owner half (`_provision_or_reconcile`) calls the same
helper, so gate policy lives in exactly one place and the poll and the webhook
can never disagree — the same invariant that made `ensure_personal_trial_default`
a shared helper between `ensure_user_ready` and `/container/status`.

`ensure_personal_trial_default` also needs an auth-free form. It currently takes
`AuthContext` for exactly two things: `auth.is_org_context` (a personal-only
predicate) and `auth.user_id` (the pending-invitation lookup). Refactor to
`ensure_personal_trial_default_for_user(user_id: str)` with the existing
`AuthContext` signature kept as a thin wrapper — **do not duplicate the
pending-invitation refusal**, it is a tenancy invariant (Gate B).

### 4.2 Trigger wiring

```
Clerk  user.created                     → ensure_personal_trial_default_for_user(user_id)
                                        → ensure_owner_provisioned(user_id, trigger="clerk.user_created")

Stripe customer.subscription.created    → (existing handle_subscription_state, after the
       customer.subscription.updated       `entering_live` block)
                                        → ensure_owner_provisioned(account["owner_id"],
                                                                   trigger="stripe.subscription_live")

Clerk  organizationMembership.created   → _ensure_paperclip_for_member(org_id, user_id, email)
                                          [NO container dispatch]

GET /container/status                    → UNCHANGED. Still dispatches ensure_user_ready.
```

Notes on each:

- **`user.created`** already has a handler that persists `user_id` + primary
  email to `user_repo` and sends the welcome email. Both are correct code that
  **has never executed** (see §5). Adding the trigger here also finally makes
  `lookup_owner_email`'s fast path (`user_repo.email`) real instead of always
  falling through to the Clerk Backend API.
- **Stripe** is the right org hook because `handle_subscription_state` already
  computes `entering_live` (`stripe_webhook_handlers.py:723-725`) — the exact
  predicate "this row just acquired a live subscription" — and already resolves
  the owner via `resolve_owner_account`. The hook point exists; only the call is
  missing. It fires for personal paid signups too, which is harmless and
  correct (the gate short-circuits or the claim refuses).
- **`organizationMembership.created`** carries everything needed in the payload:
  `data.organization.id`, `data.public_user_data.user_id`,
  `data.public_user_data.identifier` (the member's email). The existing
  `_extract_org_member_email` helper already reads exactly those paths.
- **`organization.created` is NOT a trigger** and its handler becomes dead —
  see §10.

### 4.3 What is deliberately NOT moved

- The **bootstrap leg** (`maybe_dispatch_bootstrap`) stays poll-driven. It gates
  on runtime reachability ("container is running *right now*"), which is not an
  event any external provider can tell us about. Out of scope.
- The **funnel-brief submit** path is unchanged.
- The **wake** path (`container_wake`, `wake_policy`) is unchanged — an explicit
  wake is a user-intent door, not an eligibility transition.

---

## 5. Verified facts this design rests on

### 5.1 Clerk webhooks are entirely unconfigured (owner-verified, 2026-07-25)

**Clerk has ZERO webhook endpoints configured in BOTH the prod and dev
instances.** Corroborated by 30 days of prod logs showing no inbound Clerk
webhook. Every handler in `routers/webhooks.py` is code that has never run.
The owner has accepted responsibility for the endpoint configuration existing.

Consequences that are live today, independent of this design:

| Handler | What has silently never happened |
|---|---|
| `user.created` → `user_repo.put(user_id, email=...)` | the users table's `email` column is populated only opportunistically; `lookup_owner_email` always pays the Clerk API fallback |
| `user.created` → `send_welcome_email` | **no welcome email has ever been sent** (this is its only call site) |
| `user.deleted` → `channel_link_repo.sweep_by_member` | orphan channel-link rows accumulate for deleted users |
| `user.deleted` / `organization.deleted` → `provisioning.disable` | deleted Clerk users/orgs keep `status="active"` Paperclip rows, and `resolve_teams_context` only inspects DDB — a deleted user's Teams access is not revoked |
| `organization.deleted` → `cancel_subscription_for_owner` | **deleting a Clerk org leaves its per-seat Stripe subscription billing forever** |
| `organizationMembership.*` → `sync_team_seat_quantity` | covered by the opportunistic reconcile on `/container/status` (`routers/container.py:184-187`) — the only one of these with a working fallback |

These are *not* fixed by this design, but they are all fixed by the same
prerequisite (configure the endpoint), so they belong in the same rollout.

### 5.2 The Clerk webhook endpoint is currently UNAUTHENTICATED — must fix first

`CLERK_WEBHOOK_SECRET` is read from the environment (`core/config.py:25`) and is
**not wired anywhere in CDK** — it has no secret in `auth-stack.ts` and no entry
in `service-stack.ts`'s `backendContainerSecrets` (verified by repo-wide grep;
the only other hits are the code that reads it and a CLAUDE.md table row marking
it "Optional"). `_verify_svix_signature` returns immediately when the secret is
empty (`routers/webhooks.py:66-68`), so today
`POST /api/v1/webhooks/clerk` accepts **any** unsigned body from anyone on the
internet and will:

- cancel an org's Stripe subscription (`organization.deleted`),
- disable any user's Paperclip access (`user.deleted`, `organizationMembership.deleted`),
- sweep channel links.

It has not been exploited only because the route is `include_in_schema=False`
and nothing advertises it. This is a **prerequisite fix, not a nice-to-have**:
enabling webhook-triggered provisioning without it would additionally hand an
unauthenticated caller a provisioning trigger (i.e. a cost-amplification
primitive). The W20 alarm on `webhook.clerk.sig_fail` exists but can never fire,
because verification never runs.

Two related gaps in the verifier itself:

- **No timestamp tolerance.** Neither the inline verifier nor the shared
  `core/crypto/svix.verify_svix_signature` compares `svix-timestamp` to now.
  Svix's own libraries reject anything more than **5 minutes** off
  ([docs.svix.com/receiving/verifying-payloads/why](https://docs.svix.com/receiving/verifying-payloads/why)).
  Without it a captured valid payload replays indefinitely; the only defence is
  the 30-day `svix-id` dedup row.
- **Fail-open on a malformed secret** (`routers/webhooks.py:72-76` logs and
  skips). The shared module deliberately fail-closes and says not to "fix" it
  back; the router should adopt the shared module's posture.

### 5.3 Clerk / Svix delivery semantics (researched 2026-07-25)

Clerk delivers webhooks via **Svix**
([clerk.com/docs/guides/development/webhooks/overview](https://clerk.com/docs/guides/development/webhooks/overview)).

| Property | Value | Source |
|---|---|---|
| Delivery | **At-least-once** — *"a message can sometimes be processed twice by the webhook receiver"* | [docs.svix.com/idempotency](https://docs.svix.com/idempotency) |
| Ordering | **None.** Regular endpoints deliver *"independently and in parallel"*; order is *"on a best effort basis."* FIFO endpoints are an opt-in Svix feature with **no Clerk surface** | [docs.svix.com/advanced-endpoints/fifo-endpoints](https://docs.svix.com/advanced-endpoints/fifo-endpoints) |
| Concurrency | Clerk documents **no webhook rate limit** — *"Clerk will send as many Webhooks events as your server can handle."* Combined with parallel delivery: **handlers can be hit concurrently** | [clerk.com/blog/webhooks-v-bapi](https://clerk.com/blog/webhooks-v-bapi) |
| Retries | 8 attempts: immediately, 5s, 5min, 30min, 2h, 5h, 10h, 10h (≈27.6h total) | [docs.svix.com/retries](https://docs.svix.com/retries) |
| Success | `2xx` within **15s**. *"Any other status code, including 3xx redirects, are treated as failures."* | [docs.svix.com/retries](https://docs.svix.com/retries) |
| Exhaustion | Message marked `Failed`. **After 5 days of total failure the endpoint is auto-disabled.** | [docs.svix.com/retries](https://docs.svix.com/retries) |
| Recovery | Dashboard → endpoint → **Message Attempts**: resend one message, "Recover Failed" over a date range, or replay all missing since the first failure | [clerk.com/docs/.../webhooks/overview](https://clerk.com/docs/guides/development/webhooks/overview) |
| Headers | `svix-id`, `svix-timestamp`, `svix-signature` (`v1,<base64>`). **No `clerk-*` aliases exist** — the only alias Svix supports is a paid-tier `webhook-*` white-label | [docs.svix.com/receiving/verifying-payloads/how-manual](https://docs.svix.com/receiving/verifying-payloads/how-manual) |
| Dedup key | `svix-id` is *"unique per message but is reused across retries of the same message"* | [docs.svix.com/idempotency](https://docs.svix.com/idempotency) |
| Signing scheme | HMAC-SHA256 over `{svix-id}.{svix-timestamp}.{raw body}`, key = base64-decoded secret after the `whsec_` prefix | same |
| Python library | Clerk's first-party `verifyWebhook()` is **JS/TS only**; for Python the documented option is the `svix` pip package | [docs.svix.com/receiving/verifying-payloads/how](https://docs.svix.com/receiving/verifying-payloads/how) |

Two operational consequences worth designing around:

- **15s deadline.** Our handlers do everything inline. `organization.deleted`
  cancels a Stripe subscription *and* paginates a GSI disabling every member
  row; `organizationMembership.created` makes a Stripe seat-sync call. Under
  load these can plausibly exceed 15s → retry → duplicate work → and after 5
  days of that, a **silently disabled endpoint**. Handlers should ACK fast and
  do slow work off-request. (Not a blocker for the *trigger* — a dispatch is a
  single conditional write plus `StartExecution` — but it is a real hazard for
  the pre-existing handlers.)
- **Auto-disable after 5 days** is the most insidious failure mode: it turns
  "webhooks work" into "webhooks silently stopped" with no signal on our side.
  It is the single strongest argument for §7's non-negotiable fallback and §8's
  absence alarm.

### 5.4 Clerk event names and payload shapes

Exact strings confirmed (Clerk's own first-party skills repo,
[github.com/clerk/skills](https://github.com/clerk/skills/blob/main/skills/features/clerk-webhooks/SKILL.md);
Clerk's docs deliberately point at the Dashboard's Event Catalog tab rather than
publishing an exhaustive list): `user.created|updated|deleted`,
`session.created|ended|removed|revoked`, `organization.created|updated|deleted`,
`organizationMembership.created|updated|deleted`,
`organizationInvitation.accepted|created|revoked`.

`organizationInvitation.accepted` **exists and is distinct** from
`organizationMembership.created`; their relative ordering is **not documented**,
so do not depend on it. Creating an org fires **both** `organization.created`
and `organizationMembership.created`, order unspecified.

Payload paths our handlers already use, and whether research confirms them:

| Path used in `routers/webhooks.py` | Confirmed? |
|---|---|
| `data.id` (user id) | ✅ |
| `data.email_addresses[].{id,email_address}` + `data.primary_email_address_id` | ✅ — and Clerk explicitly says to match the pointer, *not* to assume index 0. `_extract_primary_email` does this correctly |
| `data.email_addresses[].verification.status` | ✅ present in the published `user.created` example (`"verified"`) |
| `data.organization.id` | ✅ |
| `data.public_user_data.user_id` | ✅ |
| `data.public_user_data.identifier` (member email) | ✅ |
| `data.role` = `org:admin` \| `org:member` | ✅ |
| `data.created_by` (on `organization.created`) | ⚠️ **not confirmed** by the sources found |
| `data.organization.created_by` (nested, on membership events) | ⚠️ **not confirmed** by the sources found |

Both unconfirmed paths are used **only for logging** today, and neither is used
by this design (`_handle_organization_created` is deleted; the runner resolves
the org creator from the Clerk Backend API via
`clerk_admin.get_organization().created_by`, not from a webhook payload). Still
worth checking against a real captured payload — see §11.

Raw webhook JSON is **snake_case**; the `@clerk/backend` SDK type reference
renders the same objects in camelCase. A Python handler reading raw JSON must
use snake_case, which ours does.

### 5.5 Dev/local testing story

- **Dashboard "Testing" tab → "Send Example"** fires a synthetic event of a
  chosen type at the endpoint. Confirmed
  ([clerk.com/docs/guides/development/webhooks/syncing](https://clerk.com/docs/guides/development/webhooks/syncing)).
- **Delivery log exists**: Dashboard → endpoint → **Message Attempts**, showing
  the payload and HTTP response code per attempt, with manual resend/replay.
- **`clerk webhooks listen`** — a *first-party CLI relay* shipped 2026-07-09
  ([clerk.com/changelog/2026-07-09-clerk-cli-webhooks-and-impersonate](https://clerk.com/changelog/2026-07-09-clerk-cli-webhooks-and-impersonate)):
  `clerk webhooks listen --forward-to http://localhost:8000/api/v1/webhooks/clerk`.
  Also `clerk webhooks token` (stable reusable relay URL) and
  `clerk webhooks verify` (offline signature check against a saved delivery —
  useful for the contract test in §9). **Caveat:** the relay URL must still be
  registered as an endpoint in the Dashboard; the CLI replaces ngrok, not the
  endpoint config. There is no `clerk webhooks trigger` — synthetic events come
  from the Dashboard only.
- **Development instances support webhooks**, and endpoints + signing secrets
  are **per-instance** — dev and prod are configured and secreted separately.
  Note Isol8's dev Clerk instance is shared with prod *previews*.

---

## 6. Idempotency — why no new machinery is needed

At-least-once delivery plus parallel delivery plus a still-live poll means the
same owner can be triggered many times, concurrently, from different sources.
Four independent layers already collapse that to one provision:

1. **`svix-id` dedup** — `webhook_dedup.record_event_or_skip(svix_id, source="clerk")`
   is already wired into `handle_clerk_webhook` (`routers/webhooks.py:608-628`),
   backed by a DDB conditional PutItem with a 30-day TTL. `svix-id` is stable
   across retries, so this is the correct key. Stripe uses the same table keyed
   on `evt_*` (`routers/billing.py:1102`).
2. **`try_claim_provision`** — *the real fence*. A strongly-consistent DDB
   conditional write; refuses a `running` row or a fresh lease. This is what
   makes a webhook that races N polls across M backend tasks resolve to one
   execution.
3. **`StartExecution` idempotence** on identical name+input for STANDARD
   workflows, with the name stamped in the same atomic write as the claim.
4. **Business-key idempotence inside the work** — `provision_org` short-circuits
   on an existing active row; `try_acquire_seed_claim` and
   `try_acquire_org_create_claim` remain as defence-in-depth; `provision_member`
   treats 409 as success.

**Assessment of the two existing helpers:**

- `core/services/webhook_dedup.py` — **correct tool, already used, no change
  needed.** One sharp edge to be aware of rather than fix: the dedup row is
  written *before* handling and the `delete_event` rollback helper is **not**
  used by the Clerk handler. If a handler crashes after the dedup write, Svix's
  retry is suppressed. Adding a blanket rollback would re-open double-handling
  for every other branch; the right posture is to **rely on the poll fallback
  for the provisioning branch specifically** and say so in the handler, which
  costs nothing precisely because the trigger is idempotent anyway.
- `core/services/idempotency.py` — **wrong tool; must not grow a webhook
  caller.** It is an in-process TTL cache keyed on `(handler, Idempotency-Key)`
  with a 60s default, built for admin double-click protection. It is
  instance-local, so it provides nothing against Svix's parallel delivery
  landing on two backend tasks. Leave it where it is.

**One dedup gap worth stating:** a Dashboard *manual replay* days later may or
may not reuse the original `svix-id` (**not documented**), and the dedup row's
TTL is 30 days. Either way layer 2 makes a replay a no-op, which is exactly why
we don't need to know the answer.

---

## 7. Ordering, and races between the webhook and the poll

Neither Clerk/Svix nor Stripe guarantees ordering. Enumerated:

| Scenario | Outcome |
|---|---|
| **Webhook beats the first poll** (the intended case) | Provision starts seconds after signup instead of after the browser lands and polls. The poll then finds a `provisioning` row and no-ops. **Net win.** |
| **Poll beats the webhook** | The poll's dispatch wins the claim; the webhook's `ensure_owner_provisioned` gets `NOT_CLAIMABLE` and returns. Today's behaviour exactly. |
| **Duplicate `user.created` delivery** | `svix-id` dedup drops it; if it somehow got through, the claim refuses it. |
| **`organizationMembership.created` arrives before the org's company exists** | `_ensure_paperclip_for_member` already returns gracefully on "no active company" and logs + emits `paperclip.member_add{outcome=no_company}`. The member's own poll retries. **Correct as-is — do not add a retry queue.** |
| **`organization.created` and `organizationMembership.created` for the org creator, in either order** | Neither is a provisioning trigger. Irrelevant. |
| **Stripe `.created(incomplete)` after `.updated(active)`** | Already guarded — `handle_subscription_state` refuses to write `incomplete*` over a live status, citing Stripe's documented lack of ordering (`stripe_webhook_handlers.py:659-689`). |
| **`user.created` for someone about to accept an org invite** | ⚠️ **The one genuinely new hazard.** See below. |

### 7.1 The pending-invitation hazard (riskiest part of this design)

`ensure_personal_trial_default` refuses to create a `trial_local` row when the
user has a pending org invitation, and the gate has a matching first-touch
refusal (`provision_gate.py:483-511`). Both rely on
`clerk_admin.list_pending_invitations_for_user`, and **Clerk matches invitations
to VERIFIED emails only** — a limitation the gate's own comment already
acknowledges.

Today the poll fires *after* the frontend has routed the user (and after any
email verification step), so this refusal usually works. A `user.created`
webhook fires at the **earliest possible instant** — plausibly before the
primary email is verified — which would widen the window in which an
invited user gets a personal `trial_local` row and a personal container that
is instant junk (container + billing row + a throwaway Paperclip company).

**Mitigation (recommended):** gate the `user.created` trigger on the primary
email being verified — the payload carries
`data.email_addresses[].verification.status`, and the sources confirm the field
exists. When it is not `"verified"`, skip the trigger and log; the poll picks
the owner up later, exactly as today. This is strictly a "when to trigger
early" decision — the fallback makes the conservative choice free.

**Rejected alternative:** triggering on `session.created` instead. It fires only
after a real sign-in (so the email is verified and the frontend is about to poll
anyway), which removes almost all of the latency benefit while adding a
high-volume event (`session.created` fires on every sign-in, forever) that we
would have to filter down to "first ever session" with a state read.

**Open:** whether Clerk emits `user.created` before or after primary-email
verification in each of our sign-up flows (email code, OAuth). Resolvable in
one dev signup once webhooks are on — see §11.

---

## 8. What `/container/status` becomes

**Non-negotiable: `/container/status` keeps its `ensure_user_ready` dispatch.**
A missing, misconfigured, failed, or Svix-auto-disabled webhook must degrade to
today's behaviour, never to a signup outage. This is stated as a constraint, not
a preference, and §5.3's 5-day silent auto-disable is the concrete reason.

Staged:

- **Phase 1 (this design).** `/container/status` is **completely unchanged**.
  The webhooks are purely additive. Every existing behaviour — the composed
  read, the trial-cap gate and Bedrock restore, wake, the error auto-retry, the
  stale-lease self-heal, the conversion resume, the member-join reconcile, the
  bootstrap dispatch — stays put.
- **Phase 2 (only after §9 shows the webhook actually fires).** The poll's
  dispatch becomes explicitly *fallback-shaped*: it fires only when the owner
  has no container row and no in-flight execution **and** the account is older
  than a small grace window (say 30s, comfortably inside Svix's first two retry
  intervals). Below the window, the poll returns "provisioning" and lets the
  event own it. This removes the steady-state duplicate gate evaluation without
  removing the safety net.
- **Never:** removing the dispatch, or making readiness depend on a webhook
  having arrived.

Conceptually the endpoint's contract becomes *"read the composed state, and heal
it if nobody else is going to"* — the causal edge moves to the server; the poll
keeps the repair edge.

---

## 9. Failure modes

| Failure | Behaviour | Detection |
|---|---|---|
| Webhook endpoint never configured (**today's state**) | Identical to today — poll drives everything | `provision.trigger{source=webhook}` count is zero; §8 absence alarm |
| Endpoint configured but the signing secret is missing | With the §10 rollout: the handler **fail-closes** and 400s → Svix retries → alarm fires. (Today it fail-opens, which is the vulnerability in §5.2) | `webhook.clerk.sig_fail` (W20 alarm already exists) |
| Individual delivery fails (backend 5xx, deploy, timeout) | Svix retries 8× over ~27.6h; the poll covers the gap immediately anyway | `webhook.clerk.received` gap |
| **Sustained failure ≥5 days → endpoint auto-disabled** | Silently reverts to poll-only, permanently, with no error anywhere | **Only** the absence alarm catches this |
| Webhook fires for an ineligible owner (gate up) | `ensure_owner_provisioned` returns `GATED`. One billing read, no side effects | `provision.trigger{result=gated}` |
| Webhook fires for an owner already running / mid-provision | `try_claim_provision` refuses; `NOT_CLAIMABLE` | `provision.trigger{result=not_claimable}` |
| Duplicate/replayed delivery | `svix-id` dedup, then the claim | `webhook.dedup{outcome=already_seen}` |
| Handler exceeds Svix's 15s deadline | Treated as failure → retry → duplicate work → 5-day disable risk. Affects the **pre-existing** slow handlers (`organization.deleted`, seat sync), not the dispatch | p99 latency on the route |
| Forged/replayed payload | Signature check (once wired) + `svix-id` dedup. **No timestamp tolerance** — see §5.2 | `webhook.clerk.sig_fail` |
| Stripe event resolves to no billing row | `resolve_owner_account` already returns None and emits `stripe.webhook.owner_resolve{path=unresolved}`; the trigger is skipped | existing metric |

**Observability added by this design** — one metric with two dimensions:

```
provision.trigger{source=clerk_user_created|stripe_subscription_live|status_poll,
                  result=dispatched|gated|not_claimable}
```

`source=status_poll, result=dispatched` falling to ~0 for fresh signups is the
single number that proves the event trigger is live. And a **new absence alarm**
— "zero `webhook.clerk.received` in 24h" — is the detector that would have
caught today's state, and is the only thing that catches a Svix auto-disable.

---

## 10. Migration / rollout

Ordered so that every step is independently safe and reversible.

1. **Wire the secret + fail closed.** Add a `clerk_webhook_secret` secret in
   `auth-stack.ts` (same `createSecret` pattern as Clerk/Stripe) and
   `CLERK_WEBHOOK_SECRET` to `service-stack.ts`'s `backendContainerSecrets`.
   Change the router to use the shared, fail-closed
   `core/crypto/svix.verify_svix_signature`, add a `svix-timestamp` tolerance
   check (5 minutes, matching Svix's own libraries), and make a *set-but-invalid*
   secret reject rather than skip. **No behavioural change** — no webhooks
   arrive yet. This step alone closes the §5.2 vulnerability and should ship
   whether or not the rest of this design does.
2. **Configure the Clerk DEV endpoint**: `https://api-dev.isol8.co/api/v1/webhooks/clerk`,
   subscribed to `user.created`, `user.updated`, `user.deleted`,
   `organizationMembership.created`, `organizationMembership.deleted`,
   `organization.deleted`. (**Not** `organization.created` — see §11.) Fire a
   synthetic `user.created` from the Dashboard's Testing tab and confirm a 200
   plus a `webhook.clerk.received{event_type=user.created}` datapoint.
3. **Land the trigger code behind a setting** — `PROVISION_ON_WEBHOOK`, default
   **OFF**. OFF = handlers stay record-keeping (today's behaviour, byte for
   byte). This is the core signup path; the flag is mandatory, mirroring how
   `SEED_IN_EXECUTOR` de-risked #1456.
4. **Dev validation.** Flip ON in dev. Then:
   - Fresh Clerk signup → assert **exactly one** SFN execution for the owner,
     and that `provision_claimed_at` precedes the first `/container/status` log
     line for that user. That ordering *is* the proof the event is the trigger.
   - Re-run the #1442 concurrency acceptance test (15 concurrent polls) **with**
     a `user.created` webhook interleaved → still exactly one company, one user,
     one execution.
   - Org path: complete team checkout → assert the Stripe event dispatches, and
     that a poll-free org (no browser open) reaches `running`.
   - Member path: invite + accept → assert the Paperclip membership lands from
     the webhook, with no container dispatch.
5. **Prod.** Wire the prod secret, configure the prod endpoint (separate
   instance, separate signing secret), flip the flag, watch
   `provision.trigger{source}` for a week.
6. **Phase 2** (§8's fallback-shaping) only after that week is clean.

**Existing users are unaffected at every step.** Nothing in the design touches
an owner who already has a container; the events only fire for new
users/subscriptions/memberships, and every path is claim-guarded.

---

## 11. What gets DELETED

Dead or wrong the moment this lands — remove in the same PR, don't accumulate:

1. **`user_provision.py`'s module docstring** — its first three paragraphs are
   now false: *"Single canonical helper called from exactly one site"*, *"No
   webhook, REST endpoint, or service-layer call dispatches it from anywhere
   else"*, *"Clerk webhooks … do NOT call this helper."* Rewrite (not amend) to
   describe the trigger taxonomy and the fallback.
2. **CLAUDE.md**:
   - Line 499: *"`GET /container/status` is the only caller of
     `ensure_user_ready`… Clerk webhooks are pure record-keeping; they do not
     trigger provisioning"* → replace with the taxonomy + the
     fallback-is-non-negotiable statement.
   - Line 347's services blurb (`ensure_user_ready` "called only from
     `GET /container/status`").
   - The Provisioning Flow diagram at line 479 (add the event entry points).
   - Line 639's env-var table: `CLERK_WEBHOOK_SECRET` is no longer "Optional".
3. **`_handle_organization_created`** (`routers/webhooks.py:264-283`) — after
   this design it has **no job at all**: org creation carries no eligibility
   information, the trigger is the Stripe event, and the runner resolves the org
   creator from the Clerk Backend API rather than this payload. Delete the
   handler, its dispatch branch, and drop `organization.created` from the
   subscribed event set. (This also retires the one unconfirmed payload path
   `data.created_by`.)
4. **`_org_provision_locks`** (`user_provision.py:56`) — an in-process
   `asyncio.Lock` per org whose own docstring concedes it is single-instance
   scoped and reopens across tasks. With the executor owning CREATE and
   `try_acquire_org_create_claim` being the real cross-instance fence, it is
   vestigial. Assess and delete.
5. **Reassess `_enqueue_paperclip_retry` + `PAPERCLIP_RETRY_KIND` + its consumer
   in `update_service.py:550`.** This queue was built to re-drive Paperclip
   provisioning from webhooks and **has never executed a single row**. Its only
   remaining live op is `archive_member` (`routers/webhooks.py:429`) — a
   one-op queue with a scheduled worker behind it. Once membership-deleted
   handling is exercised for real, decide: keep for that one op, or fold the
   retry into the handler and delete the queue.
6. **`docs/architecture/2026-05-07-paperclip-openclaw-pairing.md:93`** — *"Clerk
   fires `organization.created` → backend webhook"* describes a flow that both
   never ran and is being deleted.
7. **Phase 2 only:** the unconditional dispatch in `/container/status`'s
   no-container branch (`routers/container.py:247,269`) narrows to the
   fallback-shaped form.

Not deleted, but flagged: `core/services/idempotency.py` stays where it is and
must not acquire a webhook caller (§6).

---

## 12. Testing

- **Unit, per trigger:** eligible owner → `dispatch_provision` called once with
  the right `owner_id`; gated owner → not called, `GATED` returned; org
  membership → `_ensure_paperclip_for_member` called, `dispatch_provision`
  **not** called.
- **Flag OFF regression:** behaviour byte-identical to today; the existing suite
  stays green untouched.
- **Idempotency:** same `svix-id` twice → one dispatch. Webhook + 15 concurrent
  polls → exactly one execution, one company, one Better-Auth user (the #1442
  acceptance test, extended).
- **Ordering:** membership event before the company exists → graceful no-op plus
  the `paperclip.member_add{outcome=no_company}` metric, not an error.
- **Signature:** valid → 200; invalid → 400 + `webhook.clerk.sig_fail`; missing
  headers → 400; **secret set but malformed → reject** (the fail-open fix);
  timestamp older than 5 minutes → reject.
- **Contract test against a REAL payload.** Every Clerk handler has only ever
  been tested against payloads we invented (`tests/unit/routers/test_webhooks*.py`),
  because none has ever seen a real one. Capture a genuine delivery from the dev
  Dashboard's Message Attempts log, commit it as a fixture, and replay it
  through the handler. `clerk webhooks verify` can validate the captured
  signature offline. **This is the single highest-value test in the change** —
  it is the only one that can catch a wrong assumption about payload shape.

---

## 13. Open questions

1. **Does `user.created` fire before or after primary-email verification**, per
   sign-up flow (email code vs OAuth)? Determines how often §7.1's mitigation
   defers to the poll. Resolvable with one dev signup once step 2 is done.
2. **`data.organization.created_by`** on membership payloads and `data.created_by`
   on `organization.created` are **unconfirmed** by the sources found (§5.4).
   Both are logging-only today and neither is used by this design, but confirm
   against the captured fixture.
3. **Can an org become eligible with no Stripe event?** `routers/billing.py:135`
   references *"a flat/comped/grandfathered org sub"* — an org whose row was
   written by hand would never receive `customer.subscription.*` and would stay
   poll-only. Believed rare and correctly handled by the fallback; confirm
   whether any such org exists in prod.
4. **Does a Dashboard manual replay reuse the original `svix-id`?** Not
   documented. Moot for correctness (layer 2 makes a replay a no-op) but affects
   how `webhook.dedup` metrics read during a recovery.
5. **Clerk/Svix payload retention window** — bounds how far back "Recover Failed"
   can replay after an incident. Not found in the docs.
6. **The 15s deadline vs the existing slow handlers** (§5.3). Not a blocker for
   this design, but `organization.deleted`'s Stripe cancel + full GSI member
   sweep should probably become a queued/background job before that handler ever
   runs against a large org. Worth its own follow-up.
7. **Should `organizationInvitation.accepted` be subscribed** as a second signal
   for the member path? It exists and is distinct, but its ordering against
   `organizationMembership.created` is undocumented, and the membership event is
   the one that carries the fields we need. Recommendation: no — one trigger per
   outcome.

---

*Design only. Nothing here is implemented. The recommended first move is step 1
of §10 (wire the secret and fail closed), which is worth shipping on its own
merits regardless of whether the rest of this design proceeds.*
