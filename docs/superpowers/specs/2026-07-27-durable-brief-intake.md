# Durable onboarding-brief intake

**Status:** planned · **Branch:** `fix/durable-onboarding-brief-intake`

## The bug, from prod

Org `org_3H3viG…`, 2026-07-27:

```
05:25:37.995  POST /onboarding/brief → 202     ← funnel rendered, user typed "isol8.co"
05:25:41.120  POST → 202
05:25:44.269  POST → 202
05:25:47.428  POST → 202
05:25:50.565  POST → 202
05:25:53.707  POST → 202                        ← MAX_RETRIES exhausted; red error painted
05:26:10.369  paperclip-companies row created (status=active)
05:26:14.120  POST → 200                        ← user clicked Retry; brief landed
```

The 202 is `_ctx` (`routers/teams/deps.py:127`) reporting "workspace still
provisioning" — a correct keep-polling signal. `FirstRunFunnel.submit()` has no
notion of it: anything that isn't `staged`/`already_submitted` burns one of
`MAX_RETRIES = 5` at `RETRY_MS = 3000`. The client's patience is ~16s; the
Paperclip org seed took 33s. Every first-run user who types faster than
provisioning gets a red error on a signup that is working.

Raising the constant was rejected: it treats a design flaw as a tuning problem.

## The design flaw

`/onboarding/brief` is the only onboarding write that is not a reconciler.
Every neighbour — `ensure_user_ready`, `maybe_dispatch_bootstrap` — is
server-owned, idempotent, claim-fenced, and driven by the `/container/status`
poll that is already running. The brief endpoint instead performs **intake and
fulfilment in one request**:

| step | needs |
|---|---|
| record what the user said | auth |
| resolve owner row + claim funnel | paperclip-companies row, `onboarding_generation` |
| `list_agents` → find Friday | Paperclip active, seed landed |
| seed goal + project | Paperclip active |
| stage BRIEF.md | EFS workspace |

so it inherits the readiness of all four, and the **browser** becomes the retry
engine for the whole chain. The visible cost is the red error. The real cost:
the brief exists nowhere but that tab, so a user who closes it — or who never
clicks Retry — loses it silently, and nothing on the server knows they answered.

## The fix

Split intake from fulfilment. Intake becomes a durable write that depends on
nothing but auth; fulfilment becomes a reconciler on the hook that already
exists.

| | now | after |
|---|---|---|
| `POST /onboarding/brief` | `Depends(_ctx)` + Paperclip + Friday + EFS | auth only; records the answer; returns `staged` |
| seed + stage | inline in the request | `reconcile_pending_brief(owner_id)` |
| trigger | client retry loop | post-intake fast path + every `/container/status` poll |
| a failure means | user sees an error, brief is lost | pending item survives, next poll retries |

### Where a pre-provision brief lands

A `__pending_brief__:{owner_id}` item in `paperclip-companies` — the same
sentinel-PK idiom as the `__org_create_claim__:{org_id}` row already in that
table (`paperclip_repo.py:152`). Onboarding truth stays in one table.

```
user_id      __pending_brief__:{owner_id}     (PK, conditional put)
kind         "pending_brief"
owner_id     org_… | user_…
path         "new" | "grow"
input        str | absent          ("you pick" submits none)
detail       str | absent
brief_text   build_onboarding_brief_text(...)   — precomputed at intake
submitted_by clerk user_id of the actual submitter
submitted_at iso8601
```

`attribute_not_exists(user_id)` on the put: the first submitter wins, a
concurrent second org member gets `already_submitted`. One code path — intake
**never** claims inline even when everything is already up; the reconciler is
the only claimer, and the post-intake fast path makes that indistinguishable
from inline when Paperclip is warm.

### The funnel dismisses on the pending item

`ProvisioningStepper.tsx:717` branches on `container.funnel.state`, composed by
`readiness_state.compute_status` from `onboarding["funnel_state"]`. So
`get_onboarding_state` reports `funnel_state: "submitted"` when a pending item
exists and the row is absent/`not_started`. The funnel then dismisses on
**durable server state** rather than optimistic client metadata, and stays
dismissed across a reload taken before the drain runs.

Cost: one extra `GetItem`, only when the row is missing or the funnel has not
been submitted — i.e. the pre-onboarding window. Onboarded owners (the fleet)
short-circuit and pay nothing.

### The reconciler

```
reconcile_pending_brief(owner_id):
  pending = get_pending_brief(owner_id)            # absent → return
  row     = resolve_owner_row(owner_id, consistent_read=True)
  if row is None or not row.onboarding_generation or row.status != "active":
      return                                       # next poll retries
  claim = claim_funnel_submission(row.user_id, gen, submitted_by=…, brief_text=…)
  if STALE_GENERATION:    return                   # next poll retries
  if ALREADY_SUBMITTED:                            # a peer/earlier drain won
      delete_pending() only if the row carries onboarding_goal_id; else return
  try:
      list_agents → Friday → seed_goal_and_project → record_onboarding_ids
      → stage_first_run_files
  except:
      existing rollback (delete project, delete goal, revert claim); leave
      pending in place  → next poll retries
  delete_pending()
  maybe_dispatch_bootstrap(owner_id)
```

The `ALREADY_SUBMITTED` asymmetry is load-bearing: a drain that loses the CAS
must **not** delete the pending item, because the winner may still fail and
revert the claim — deleting would lose the brief for good. Only evidence that
fulfilment actually happened (`onboarding_goal_id` on the row) retires it.

The whole rollback block moves across unchanged; re-draining after a crash is a
no-op.

## Steps

1. **repo** (`core/repositories/paperclip_repo.py`) — `put_pending_brief`
   (conditional), `get_pending_brief`, `delete_pending_brief`.
2. **repo** — `get_onboarding_state` reports `funnel_state: "submitted"` off a
   pending item when the row is absent or `not_started`.
3. **service** (`core/services/onboarding_brief.py`) —
   `reconcile_pending_brief(owner_id)`: the router's current body, moved, with
   an off-request Paperclip session (`paperclip_user_session.get_user_session_cookie`)
   instead of `_ctx`.
4. **router** (`routers/onboarding.py`) — intake only. Drops `Depends(_ctx)`;
   validates, writes pending, fires `reconcile_pending_brief` as a background
   task, returns `{"status": "staged"}`.
5. **status hook** (`routers/container.py`) — `_maybe_drain_pending_brief`
   beside the existing `maybe_dispatch_bootstrap` dispatch, gated the same
   cheap way (skip unless a drain could plausibly do something). **On BOTH
   return paths**, not just the has-container one: `ensure_user_ready` brings
   Paperclip up *before* it dispatches the container, and the drain only needs
   Paperclip — so a brief must not sit unfulfilled behind a container that is
   slow or failing to appear. (Caught by a test that asserted the dispatch on
   the no-container path and didn't get it.)
6. ~~**reprovision** — delete the pending item when a new
   `onboarding_generation` is minted.~~ **Dropped — it was wrong.** A fresh
   mint is the same code path for a first-ever provision as for a
   reprovision, so deleting there would delete the very brief the mechanism
   exists to carry across provisioning. And carrying one into a reprovisioned
   company is correct anyway: an item can only still exist if it was never
   fulfilled, so the owner answered the funnel and got nothing for it. No TTL
   either (the table has none configured; adding one for this corner would be
   infra for nothing) — the bound is the drain, which every poll runs.
7. **clients** — `FirstRunFunnel.submit()` becomes one POST, no loop, on web
   and mobile. Delete `RETRY_MS`, `MAX_RETRIES`, the "unexpected response body"
   branch. The error branch stays for genuine failures (network, 5xx) with its
   existing streak → persistent-error UI.

`{"status": "staged"}` is deliberately kept as the fresh-intake wire word: it
already means "the server has your brief", so a frontend/backend deploy skew is
a non-event. The clients also KEEP their response-body check (`staged` /
`already_submitted`) even though the new backend can no longer 202 — skew runs
in both directions, and a new client that dismissed the funnel on an old
backend's 202 would discard the answer. Only the retry loop goes.

## Tests

- intake writes the pending item and returns `staged` with **no** Paperclip
  dependency reachable; a second submit returns `already_submitted`
- reconciler: no-ops when the row is absent / not active / stale generation
- reconciler: drains → claim, seed, record, stage, delete pending, dispatch
- reconciler: a seed failure leaves the pending item and reverts the claim
- reconciler: `ALREADY_SUBMITTED` deletes the pending item only when
  `onboarding_goal_id` is present
- `get_onboarding_state` reports `submitted` from a pending item
- web + mobile: one POST per submit, no retry loop; error branch intact

## Out of scope

- Deleting the optimistic `firstRun.briefSubmitted` metadata paint (now
  redundant, but a separate cleanup).
- The `bootstrap_state: not_started` row observed on `org_3H3viG…` after a
  successful submit — different failure, tracked separately.
