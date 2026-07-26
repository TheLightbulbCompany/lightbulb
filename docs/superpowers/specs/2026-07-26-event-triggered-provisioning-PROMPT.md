# IMPLEMENTATION PROMPT — Event-Triggered Provisioning

You are implementing a change to the Isol8 backend (`apps/backend/`, FastAPI +
DynamoDB, repo `Isol8AI/isol8`). Read this whole file before touching code.

Companion reference (read it, it has the reasoning and the source citations):
`docs/superpowers/specs/2026-07-26-event-triggered-provisioning-design.md`.

All line numbers below were verified on 2026-07-26. If a line has drifted, grep
for the quoted symbol — do not guess.

---

## 1. The problem

`GET /container/status` does not read state; it **causes** provisioning. It
dispatches `ensure_user_ready`, which evaluates the provision gate, adds
Paperclip memberships, and dispatches the container executor.

The frontend polls that endpoint every ~3s while pre-ready
(`apps/frontend/src/components/chat/ProvisioningStepper.tsx:355-366`). The poll
lands on a **load-balanced backend with no shared in-flight state**, and fires
from **every open tab**. That is the root of the duplicate-provisioning race
family (#1422 duplicate Better-Auth users + duplicate companies; #1442 duplicate
company on the very fix meant to close it).

**Your job:** move the *trigger* to server-side events, and keep the poll as an
idempotent fallback.

## 2. Why this is cheap now (do not re-litigate)

`apps/backend/core/repositories/container_repo.py:143` —
`try_claim_provision(owner_id, claim_id=..., execution_name=...)` is a
**DynamoDB conditional write** that elects exactly one winner per owner across
all backend tasks. Claimable: no row / `error` / `stopped` / stale
`provisioning` lease. Not claimable: `running`, or a fresh lease.

`apps/backend/core/services/provision_dispatch.py:97` — `dispatch_provision`
stamps the execution name in that same atomic write, then calls Step Functions
`StartExecution` (idempotent for STANDARD workflows on identical name+input).
It never raises.

`apps/backend/scripts/provision_runner.py:114` — `_seed_owner_paperclip` runs
the Paperclip owner seed (`provision_org`) as **step 0 inside the runner**, i.e.
under the same single-flight. That is what closed #1442.

⇒ **Multiple triggers are already safe.** Whoever claims first wins; everyone
else no-ops. You are adding triggers, not adding coordination.

---

## 3. The trigger taxonomy — this is the core decision, get it right

The trigger is **not** "a user was created". It is **"the owner became eligible
to provision"**, i.e. `core/services/provision_gate.evaluate_provision_gate`
transitioning from returning a `Gate` to returning `None`. That moment differs
by tenancy.

### 3.1 PERSONAL — trigger: Clerk `user.created`

- `ensure_personal_trial_default` writes a card-less `trial_local` billing row
  (`core/services/user_provision.py:59`).
- `trial_local` is in `_ACTIVE_STATUSES` and the gate has an explicit
  `trial_local` short-circuit that **skips the provider-required layer**
  (`core/services/provision_gate.py:589-596`).
- ⇒ the gate clears the instant the trial row exists. `user.created` is a real
  eligibility event.

**Dispatches:** create the trial row, then dispatch the provisioning executor.

### 3.2 ORG OWNER — trigger: Stripe `customer.subscription.created` / `.updated`

- **There is NO org free tier.** `ensure_personal_trial_default` returns
  immediately for org context (`core/services/user_provision.py:84-85`), and the
  gate returns `team_billing_required` for an org with no billing row
  (`core/services/provision_gate.py:476-482`).
- `create_team_checkout` writes the customer row **and** `provider_choice`
  synchronously (`apps/backend/routers/billing.py:647` and `:672`) *before* the
  Stripe session, so when the subscription goes live, gate layers 1 and 2 are
  both already satisfied.
- ⇒ **`organization.created` is NOT the trigger.** Clerk org creation carries no
  eligibility information at all. The Stripe subscription event is the trigger.

**Dispatches:** the provisioning executor for `owner_id == org_id`.

### 3.3 ORG MEMBER — trigger: Clerk `organizationMembership.created`

- A member joining an already-subscribed org needs **no container** — they ride
  the shared org box. They need a Paperclip membership row, or every Teams
  surface 401s on them.
- `_ensure_paperclip_for_member` (`core/services/user_provision.py:653`) is
  join-only by construction (it never creates a company).

**Dispatches:** the Paperclip membership add. **NEVER a container provision.**

---

## 4. Verified environment facts you would otherwise get wrong

1. **Clerk has ZERO webhook endpoints configured in BOTH prod and dev.**
   Owner-verified in the dashboard, corroborated by 30 days of prod logs. Every
   handler in `apps/backend/routers/webhooks.py` is code that **has never
   executed**. Do not assume the handlers are battle-tested — they have only
   ever seen payloads we invented in `tests/unit/routers/test_webhooks*.py`.
2. **Stripe webhooks DO work.** Subscription rows, `subscription_item_id`, and
   status transitions all come from `core/services/stripe_webhook_handlers.py`.
   The Stripe side needs no new infrastructure.
3. **`CLAUDE.md` currently states the opposite of what you are building** —
   line 499: *"Clerk webhooks are pure record-keeping; they do not trigger
   provisioning."* This change intentionally reverses that. Updating CLAUDE.md
   is **part of the task**, not a follow-up. See §8.
4. **`CLERK_WEBHOOK_SECRET` is not wired in CDK at all.** It is read at
   `apps/backend/core/config.py:25` and appears nowhere in
   `apps/infra/lib/stacks/auth-stack.ts` or `service-stack.ts`. Because
   `_verify_svix_signature` returns immediately when the secret is empty
   (`routers/webhooks.py:66-68`), **`POST /api/v1/webhooks/clerk` currently
   accepts any unsigned body from anyone on the internet** and will cancel org
   Stripe subscriptions (`organization.deleted`) and disable Paperclip access
   (`user.deleted`). **Fix this FIRST (Step 1) — it is a prerequisite, and it is
   worth shipping on its own.**
5. **Clerk delivers via Svix**: at-least-once, **no ordering guarantee**, no
   documented rate limit (so handlers can be hit **concurrently**), 8 retries
   over ~27.6h, **2xx within 15 seconds** required, and **the endpoint is
   auto-disabled after 5 days of sustained failure**. `svix-id` is stable across
   retries and is the correct dedup key. Full citations in the design doc §5.3.

---

## 5. Non-negotiables

- **`GET /container/status` KEEPS its `ensure_user_ready` dispatch.** A missing,
  misconfigured, failed, or Svix-auto-disabled webhook must degrade to today's
  behaviour, never to a signup outage. In this change `routers/container.py` is
  **unchanged**. Do not "clean it up".
- **`try_claim_provision` is the fence.** Do not add locks, in-process
  single-flight, or a second dedup layer. If you find yourself writing
  coordination code, you have misread §2.
- **Personal and org eligibility differ.** Orgs have no free tier. Never trigger
  an org container provision off a Clerk org/membership event.
- **Gate policy lives in ONE place.** The webhook path and the poll path must
  call the same helper. Do not copy gate logic into a webhook handler.
- **Behind a flag, default OFF.** This is the core signup path.

---

## 6. The work

### Step 1 — Secure the Clerk webhook endpoint (prerequisite, ship-able alone)

1. `apps/infra/lib/stacks/auth-stack.ts:78-79` — add
   `clerkWebhookSecret: createSecret("ClerkWebhookSecret", "clerk_webhook_secret")`
   to the `this.secrets` object, following the existing `createSecret` pattern
   (CDK generates a placeholder; the operator overrides with the real `whsec_…`
   from the Clerk dashboard via `aws secretsmanager update-secret`). Thread it
   through the `AuthSecrets` interface and the stage wiring.
2. `apps/infra/lib/stacks/service-stack.ts:1393-1402` — add
   `CLERK_WEBHOOK_SECRET: ecs.Secret.fromSecretsManager(...)` to
   `backendContainerSecrets`, using `secretsmanager.Secret.fromSecretNameV2`
   **by name** (not a cross-stack `ISecret`) — see the comment at line 1394 for
   why (KMS auto-grant cycle).
3. `apps/backend/routers/webhooks.py:57-99` — replace the inline
   `_verify_svix_signature` body with the shared, fail-closed
   `core.crypto.svix.verify_svix_signature`. Behaviour changes required:
   - secret set but **malformed** → reject (today line 75 logs and *skips* —
     fail-open; the shared module deliberately fail-closes).
   - add a **`svix-timestamp` tolerance check: reject anything more than 5
     minutes from now** (past or future). Neither verifier does this today, so a
     captured payload replays indefinitely. 5 minutes matches Svix's own
     libraries.
   - secret **unset** → keep skipping (local dev only), but log at WARNING.
4. Deploy. **No behavioural change** — no webhooks arrive yet.

### Step 2 — Extract an auth-free, owner-scoped entry point

In `apps/backend/core/services/user_provision.py`:

```python
class ProvisionTriggerResult(str, Enum):
    DISPATCHED = "dispatched"          # execution started (or idempotently re-affirmed)
    GATED = "gated"                    # gate up — normal, not an error
    NOT_CLAIMABLE = "not_claimable"    # running / fresh lease — someone else owns it

async def ensure_owner_provisioned(owner_id: str, *, trigger: str) -> ProvisionTriggerResult:
    ...
```

Requirements:

- Evaluate `evaluate_provision_gate(owner_id=owner_id, is_admin=True)`.
  `is_admin=True` is correct **by construction** — the caller is owner-scoped,
  never member-scoped.
- Gate up → return `GATED`, no side effects.
- Gate clear → `await dispatch_provision(owner_id)` (import from
  `core.services.provision_dispatch`; it never raises) → return `DISPATCHED`.
- Never raises. Never takes an `AuthContext` (webhooks have no JWT — that is the
  entire reason this helper exists).
- Emit `put_metric("provision.trigger", dimensions={"source": trigger, "result": result})`.

Then refactor so there is one policy site:

- `_provision_or_reconcile` (`user_provision.py:256`) — replace the
  `asyncio.create_task(background_provision(owner_id, auth.user_id))` at line
  299 with the new helper. **Preserve the surrounding logic exactly**: the
  `wake_policy.is_parked_trial` suppression and the `status != "running"` branch
  are load-bearing (see that function's docstring).
- `ensure_personal_trial_default` (`user_provision.py:59`) — add an auth-free
  form `ensure_personal_trial_default_for_user(user_id: str)`. It currently uses
  `AuthContext` for exactly two things: `auth.is_org_context` (a personal-only
  predicate — an org `owner_id` starts with `org_`, so use `is_org_owner_id`)
  and `auth.user_id` (the pending-invitation lookup). **Do not duplicate the
  pending-invitation refusal** — it is a tenancy invariant (Gate B). Keep the
  existing `AuthContext` signature as a thin wrapper.

Add the flag to `apps/backend/core/config.py` (follow the existing convention,
e.g. line 190 `TRIAL_WAKE_GATE_ENABLED`):

```python
PROVISION_ON_WEBHOOK: bool = os.getenv("PROVISION_ON_WEBHOOK", "false").lower() == "true"
```

OFF must be byte-identical to today's behaviour.

### Step 3 — Wire the three triggers

**A. Clerk `user.created`** — `apps/backend/routers/webhooks.py:639-676`.
The branch already persists `user_id` + primary email to `user_repo` and sends
the welcome email. **Append**, guarded by the flag:

```
ensure_personal_trial_default_for_user(user_id)
ensure_owner_provisioned(user_id, trigger="clerk.user_created")
```

⚠️ **Guard on primary-email verification.** The payload carries
`data.email_addresses[].verification.status`. Trigger only when the primary
email (the one whose `id` matches `data.primary_email_address_id` — see
`_extract_primary_email` at `routers/webhooks.py:203`) has
`verification.status == "verified"`. Otherwise log and skip; the poll picks the
owner up later. **Reason:** `ensure_personal_trial_default` and the gate refuse
a personal trial when the user has a pending org invitation
(`provision_gate.py:483-511`), but **Clerk matches invitations to VERIFIED
emails only**. Firing at the earliest possible instant would otherwise hand an
about-to-be-invited user a junk personal container + billing row + throwaway
Paperclip company. This is the single riskiest part of the change.

**B. Stripe subscription** — `apps/backend/core/services/stripe_webhook_handlers.py`,
inside `handle_subscription_state` (starts line 622). The predicate you need
already exists: `entering_live` at line 723-725 ("this row just acquired a live
subscription"), and `resolve_owner_account` has already mapped the event to a
billing row.

Add the dispatch **after** the existing `trial_local` conversion block
(line 808-876) and **before** `write_owner_facts` (line 900), guarded by the
flag and by `entering_live`:

```
ensure_owner_provisioned(account["owner_id"], trigger="stripe.subscription_live")
```

- Do **not** fire it when the trial→paid conversion branch claimed the entry —
  that branch owns the container row and runs its own drain+reprovision
  (`run_conversion_reprovision`). Dispatching underneath it lands a second task
  on a draining one; that is the documented EFS-WAL corruption mode.
- Firing for **personal** paid signups as well as orgs is fine and correct — the
  gate short-circuits or the claim refuses.

**C. Clerk `organizationMembership.created`** —
`apps/backend/routers/webhooks.py:285-357` (`_handle_organization_membership_created`).
The handler currently only logs + probes tenancy + syncs seats. Add, guarded by
the flag:

```
_ensure_paperclip_for_member(org_id=..., member_user_id=..., member_email=...)
```

Payload paths (already extracted by the existing helpers, all confirmed against
Clerk docs): `data.organization.id`, `data.public_user_data.user_id`,
`data.public_user_data.identifier` (email, via `_extract_org_member_email` at
line 184). **NO container dispatch on this path.**

`_ensure_paperclip_for_member` already returns gracefully when the org's company
does not exist yet (it emits `paperclip.member_add{outcome=no_company}`) — that
is the correct out-of-order behaviour. **Do not add a retry queue.**

### Step 4 — Observability

- The `provision.trigger{source,result}` metric from Step 2 is how you prove the
  change works. `source=status_poll, result=dispatched` should fall to ~0 for
  fresh signups once the flag is on.
- Add a **CloudWatch absence alarm** in
  `apps/infra/lib/stacks/observability-stack.ts`: **zero
  `webhook.clerk.received` datapoints in 24h**. This is the only detector for
  (a) today's "no endpoint configured" state and (b) Svix silently disabling the
  endpoint after 5 days of failure. Model it on the existing alarms; note W20
  (`webhook.clerk.sig_fail`, line 829-833) already exists but can never fire
  today because verification never runs.

---

## 7. Verification — what "done" looks like

Do these in dev, in order. Steps 1-2 are dashboard/config work, not code.

1. **Configure the Clerk DEV endpoint**: `https://api-dev.isol8.co/api/v1/webhooks/clerk`,
   subscribed to `user.created`, `user.updated`, `user.deleted`,
   `organizationMembership.created`, `organizationMembership.deleted`,
   `organization.deleted`. **Do NOT subscribe `organization.created`** — its
   handler is being deleted (§8). Put the endpoint's `whsec_…` into the dev
   secret. Endpoints and signing secrets are **per Clerk instance** — dev and
   prod are configured separately.
2. **Prove delivery before writing any assertions**: Clerk Dashboard → Webhooks
   → your endpoint → **Testing** tab → **Send Example** for `user.created`.
   Expect a 200 and a `webhook.clerk.received{event_type=user.created}`
   datapoint. If this fails, nothing downstream matters.
   For local work: `clerk webhooks listen --forward-to http://localhost:8000/api/v1/webhooks/clerk`
   (first-party CLI relay, shipped 2026-07-09) — but the relay URL must still be
   registered as a Dashboard endpoint or nothing flows. `clerk webhooks verify`
   validates a captured signature offline.
3. **Flip `PROVISION_ON_WEBHOOK=true` in dev.**
4. **Golden path (personal):** sign up a fresh Clerk account. Assert **exactly
   one** Step Functions execution for that owner, and that the containers row's
   `provision_claimed_at` **precedes** the first `/container/status` log line for
   that user. *That ordering is the proof the event is the trigger* — not merely
   that provisioning happened.
5. **Race:** re-run the #1442 acceptance test (15 concurrent `/container/status`)
   with a `user.created` webhook interleaved → exactly ONE company, ONE
   Better-Auth user, ONE execution.
6. **Org path:** complete team checkout with **no browser tab polling** → the
   Stripe event alone must drive the org to `running`.
7. **Member path:** invite + accept → Paperclip membership lands from the
   webhook; assert **no** container dispatch fired.
8. **Flag OFF regression:** full backend suite green, behaviour byte-identical
   to today.

**Done =** all of the above, plus §8's deletions, plus the CLAUDE.md update,
plus the real-payload fixture test in §9.

Prod rollout (after a clean dev soak): wire the prod secret, configure the prod
endpoint, flip the flag, watch `provision.trigger{source}` for a week.

---

## 8. DELETE these — do not accumulate dead code

1. **`apps/backend/core/services/user_provision.py:1-17` (module docstring).**
   Three of its claims become false: *"called from exactly one site"*, *"No
   webhook, REST endpoint, or service-layer call dispatches it from anywhere
   else"*, *"Clerk webhooks … do NOT call this helper."* **Rewrite it**, don't
   amend it — describe the trigger taxonomy and the fallback.
2. **`CLAUDE.md`**, four edits:
   - line 499 — *"`GET /container/status` is the only caller of
     `ensure_user_ready`… Clerk webhooks are pure record-keeping; they do not
     trigger provisioning."* → the taxonomy + "the poll remains a non-negotiable
     idempotent fallback".
   - line 347 — the services blurb saying `ensure_user_ready` is "called only
     from `GET /container/status`".
   - line 479 — the Provisioning Flow diagram; add the event entry points.
   - line 639 — the env-var table: `CLERK_WEBHOOK_SECRET` is no longer
     "Optional".
3. **`_handle_organization_created`** (`routers/webhooks.py:264-283`) and its
   dispatch branch at line 735-736. After this change it has **no job at all** —
   org creation carries no eligibility information, the trigger is the Stripe
   event, and `provision_runner._seed_owner_paperclip` resolves the org creator
   from the Clerk Backend API (`clerk_admin.get_organization().created_by`), not
   from this payload. Also drop `organization.created` from the subscribed set.
4. **`_org_provision_locks`** (`user_provision.py:56`) — an in-process
   `asyncio.Lock` per org whose own docstring concedes it is single-instance
   scoped and reopens across tasks. `try_acquire_org_create_claim` is the real
   cross-instance fence and the executor owns CREATE. Assess and delete.
5. **Reassess `_enqueue_paperclip_retry`** (`routers/webhooks.py:116`) +
   `PAPERCLIP_RETRY_KIND` + its consumer at
   `core/services/update_service.py:550`. Built to re-drive Paperclip
   provisioning from webhooks; **has never executed a single row**. Its only
   remaining live call site is `archive_member` (`routers/webhooks.py:429`) — a
   one-op queue with a scheduled worker behind it. Decide: keep for that one op,
   or fold the retry into the handler and delete the queue.
6. **`docs/architecture/2026-05-07-paperclip-openclaw-pairing.md:93`** — *"Clerk
   fires `organization.created` → backend webhook"* documents a flow that never
   ran and is being deleted.
7. **`core/services/idempotency.py`** — NOT deleted, but **must not acquire a
   webhook caller.** It is an in-process 60s TTL cache keyed on
   `(handler, Idempotency-Key)` for admin double-click protection. It is
   instance-local and provides nothing against Svix's parallel delivery landing
   on two backend tasks. Use `core/services/webhook_dedup.py` (already wired for
   Clerk at `routers/webhooks.py:608-628`) — it is the right tool and needs no
   change.

Deletion of the poll's dispatch in `routers/container.py` is **explicitly out of
scope** — see §5.

---

## 9. Traps — things that will silently break

- **Do NOT remove or weaken the `/container/status` dispatch.** Svix disables an
  endpoint after **5 days** of sustained failure, silently. If the poll is not
  there, signup stops working and nothing tells you.
- **Do NOT dispatch off `organization.created` or `organizationMembership.created`
  for the container.** Orgs have no free tier; those events fire long before the
  org is eligible, so every dispatch would be a wasted gate evaluation, and it
  encodes the wrong mental model (org bolted onto a personal-shaped flow).
- **Do NOT fire the Stripe dispatch during a trial→paid conversion.** See §6-B.
- **Do NOT trust `data.created_by` / `data.organization.created_by`.** Those two
  paths are used in the current handlers but could **not** be confirmed against
  Clerk's documentation. They are logging-only today and are not needed by this
  change. Verify against a real payload before relying on either.
- **Handlers must ACK within 15 seconds** or Svix treats it as a failure. Today
  everything runs inline: `organization.deleted` cancels a Stripe subscription
  *and* paginates a GSI disabling every member row;
  `organizationMembership.created` makes a Stripe seat-sync call. Your new
  dispatch is cheap (one conditional write + `StartExecution`), but do not add
  more inline work — and note the pre-existing slow handlers as a follow-up
  risk, not something to fix here.
- **Raw Clerk webhook JSON is snake_case.** The `@clerk/backend` SDK type docs
  render the same objects in camelCase. Python handlers read the raw JSON — use
  `public_user_data.user_id`, not `publicUserData.userId`.
- **Never assume `email_addresses[0]` is primary.** Match
  `primary_email_address_id`. `_extract_primary_email`
  (`routers/webhooks.py:203`) already does this correctly — reuse it.
- **`webhook_dedup` writes the dedup row BEFORE handling**, and the Clerk
  handler does not use the `delete_event` rollback. A crash after the dedup
  write suppresses Svix's retry. Do **not** add a blanket rollback (it reopens
  double-handling for every other branch) — the provisioning branch is covered
  by the poll fallback, which is fine precisely because the trigger is
  idempotent. Add a comment saying so.
- **There is no `clerk-*` header alias.** Only `svix-*` ships (the sole Svix
  alias is a paid-tier `webhook-*` white-label). Do not add alias handling.
- **Clerk events are unordered, and can arrive concurrently** (no documented
  rate limit + parallel delivery). Never write a handler that assumes event A
  precedes event B.

---

## 10. Tests to write

- Unit per trigger: eligible owner → `dispatch_provision` called once with the
  right `owner_id`; gated owner → not called, `GATED` returned; org membership →
  `_ensure_paperclip_for_member` called and `dispatch_provision` **not** called.
- Unverified primary email on `user.created` → no trigger.
- Same `svix-id` twice → one dispatch.
- Signature: valid → 200; invalid → 400 + `webhook.clerk.sig_fail`; missing
  headers → 400; **secret set but malformed → reject**; timestamp >5 min old →
  reject.
- Flag OFF → byte-identical to today; existing suite untouched and green.
- **A contract test against a REAL captured Clerk payload.** Every existing
  Clerk handler test uses a payload we invented, because no handler has ever
  seen a real one. Capture a genuine delivery from the dev Dashboard's **Message
  Attempts** log, commit it as a fixture, replay it through the handler. **This
  is the highest-value test in the change** — it is the only one that catches a
  wrong assumption about payload shape.

Run scope: `cd apps/backend && uv run pytest tests/ -v` (CI scope is `tests/`,
not `tests/unit/`).

---

## 11. Open questions to resolve during implementation

1. Does `user.created` fire **before or after** primary-email verification, per
   sign-up flow (email code vs OAuth)? Determines how often the §6-A guard
   defers to the poll. One dev signup answers it.
2. Confirm `data.organization.created_by` / `data.created_by` against the
   captured fixture (see §9).
3. Can an org become eligible with **no** Stripe event? `routers/billing.py:135`
   references *"a flat/comped/grandfathered org sub"* — such an org would stay
   poll-only. Believed rare and correctly handled by the fallback; confirm
   whether any exists in prod.

---

## 12. Ground rules for this repo

- Branch, never commit to `main`; open a PR.
- Do not push without explicit approval.
- Never modify production servers directly — everything goes through CDK /
  GitHub Actions.
- Never use `sleep` or `tail` in bash. Use `gh run watch` for CI.
- Planning docs under `docs/superpowers/` are disposable — delete this prompt
  and the companion design doc in the PR that ships the feature (the PR
  description is the durable record).
