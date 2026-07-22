# Provisioning: Honest Failure State (PR 1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make provisioning failures honest and non-wedging: a Paperclip-seed failure must not block the container, and every provisioning failure must be represented truthfully in the status the client polls — never masked as `provisioning` — so clients render an error the moment it exists, with no failure-timers.

**Architecture:** Provisioning is poll-driven and runs as fire-and-forget `asyncio` tasks inside the FastAPI process. Today two failure paths are invisible: (a) a synchronous Paperclip seed inside `ensure_user_ready` raises *before* the container is dispatched, aborting provisioning with a swallowed exception and never creating a container row; (b) a container that reaches `status="error"` is overwritten back to `"provisioning"` on every poll. This PR separates "seed Teams" from "provision container," records a **cause** on every failure, and exposes failure honestly through `/container/status`. Exactly one time-based mechanism is retained — the stale-lease liveness check for a dead provisioner process (a SIGKILL'd task cannot send a synchronous "I died" signal; a heartbeat is the only honest detector). All other time-based failure inference is removed.

**Tech Stack:** Python 3 / FastAPI / DynamoDB (repositories, no ORM). Tests: `cd apps/backend && uv run pytest tests/ -v`.

## Global Constraints

- **The 422 must NOT be swallowed as success.** Treating a duplicate-email 422 as "seed succeeded" yields a container with a broken/absent Teams workspace and no error surfaced (verifier NEW-5). A seed failure must let the container provision **while recording + surfacing the Teams failure**.
- **No attempt counters, no failure-timeouts.** Failure is stated by the operation, not inferred from retry-count or wall-clock. The ONLY surviving time-based mechanism is the existing stale-lease liveness check (`container.py:315` `is_provision_claim_stale` + `not has_live_service`) — keep it, do not add others.
- **`error` ≠ `stopped`.** `stopped` = healthy scaled-to-zero (silent auto-wake is correct). `error` = failed (must surface, never masked as `provisioning`). Do not lump them.
- **Preserve transient self-heal.** A transient container failure (Spot reclaim / 5xx / ENI blip) should still auto-retry — but reported honestly as `error_retrying`, not `provisioning`. A deterministic failure (bad config, 4xx, Paperclip 422) must surface immediately and NOT be retried in a loop.
- **No new backend dependencies. Zero changes to `apps/frontend` or `apps/mobile` in this PR** — those consume the new status contract in PR 2/PR 3. This PR MAY add fields to the `/container/status` response; it must not rename or remove existing ones.
- Backend test scope is `tests/`, not `tests/unit`. Write tests per task; run the full backend suite at the end.

---

### Task 1: Decouple the Paperclip seed from container dispatch (D1)

**Files:**
- Modify: `apps/backend/core/services/user_provision.py:175-211` (`ensure_user_ready`)
- Test: `apps/backend/tests/unit/services/test_user_provision.py` (create if absent; else add cases)

**Interfaces:**
- Consumes: `_ensure_paperclip_for_org` / `_ensure_paperclip_for_owner` (unchanged signatures); `background_provision`; `put_metric`.
- Produces: after this task, a seed exception NEVER prevents reaching the `background_provision` dispatch at line 210-211.

**Design:** The seed await at 175-196 currently sits *before* the dispatch at 210-211 with no guard, so any raise exits the function first. Mirror the EXISTING precedent in this same function — `ensure_metronome_customer` at lines 165-173 is already wrapped in `try/except` + `put_metric` + `logger.exception` + "will retry on next poll". Wrap the seed the same way. The container config's Paperclip fetch is already best-effort (`ecs_manager` returns `[]` and falls back to the legacy `main` agent when no company exists), so a container that provisions without a seeded company still boots and serves chat.

- [ ] **Step 1: Write the failing test** — `ensure_user_ready` dispatches the container even when the seed raises.

```python
async def test_seed_failure_does_not_block_container_dispatch(monkeypatch):
    # org admin path: _ensure_paperclip_for_org raises, background_provision must still be dispatched
    dispatched = {}
    async def fake_seed(**kw): raise PaperclipApiError("POST /api/auth/sign-up/email -> 422", status_code=422)
    async def fake_bg(owner_id, user_id): dispatched["owner"] = owner_id
    monkeypatch.setattr(user_provision, "_ensure_paperclip_for_org", fake_seed)
    monkeypatch.setattr(user_provision, "background_provision", fake_bg)
    # ... construct an org-admin AuthContext, stub gate/metronome ...
    await ensure_user_ready(auth)
    assert dispatched["owner"] == auth.org_id     # container dispatched despite the 422
```

- [ ] **Step 2: Run it, confirm it FAILS** (today the raise aborts before dispatch): `uv run pytest tests/unit/services/test_user_provision.py::test_seed_failure_does_not_block_container_dispatch -v`

- [ ] **Step 3: Wrap the seed** — around the `if auth.is_org_context: ... else: ...` seed block (175-196), add try/except that records + continues:

```python
    try:
        if auth.is_org_context:
            await _ensure_paperclip_for_org(org_id=auth.org_id, user_id=auth.user_id, email=auth.email or "", is_admin=auth.is_org_admin)
        else:
            await _ensure_paperclip_for_owner(owner_id=owner_id, owner_user_id=auth.user_id, owner_email=auth.email or "", org_id=None)
    except Exception:
        # Teams seed is a SEPARATE concern from the container (see below). A seed
        # failure must NOT abort provisioning — the box still boots and serves
        # chat (ecs_manager falls back to the legacy `main` agent when no company
        # exists). The failure is recorded on the paperclip-companies row by
        # _mark_failed and surfaced via /container/status (Task 5). Mirrors the
        # ensure_metronome_customer guard above.
        put_metric("paperclip.seed.error")
        logger.exception("Paperclip seed failed for owner %s — provisioning continues; Teams degraded", owner_id)
```

- [ ] **Step 4: Run the test, confirm PASS.**
- [ ] **Step 5: Commit** — `fix(provision): seed Teams best-effort so a Paperclip failure can't wedge the container`

---

### Task 2: Make the detached provisioning tasks observable (D1 observability)

**Files:**
- Modify: `apps/backend/routers/container.py:227, 237, 393` (the `asyncio.create_task(ensure_user_ready(...))` sites)
- Test: `apps/backend/tests/unit/routers/test_container_status.py`

**Design:** These three `create_task` sites have no done-callback, and there is no global `loop.set_exception_handler`, so a raised exception is a bare uncorrelated "Task exception was never retrieved" that no alarm sees. Add a shared done-callback that, on `task.exception()`, emits a provision-error metric and a correlated `logger.exception`. (Compare `background_provision`, which is already self-contained; these sites are not.) Do NOT make `/container/status` await the task.

- [ ] **Step 1:** Write a test asserting that when `ensure_user_ready` raises, the done-callback emits the metric (patch `put_metric`, dispatch, let the loop run the callback).
- [ ] **Step 2:** Run, confirm FAIL.
- [ ] **Step 3:** Add a module-level helper `_watch_provision_task(task)` that logs+metrics on `task.exception()`, and attach it via `.add_done_callback(...)` at all three sites.
- [ ] **Step 4:** Run, confirm PASS.
- [ ] **Step 5: Commit** — `fix(provision): surface swallowed ensure_user_ready task exceptions to metrics`

---

### Task 3: Record a CAUSE on container-provision failure (D3, part 1)

**Files:**
- Modify: `apps/backend/core/services/container_provision.py:116-135` (the `except Exception` block)
- Modify: `apps/backend/core/repositories/container_repo.py:277-308` (`release_provision_to_error`)
- Test: `apps/backend/tests/unit/repositories/test_container_repo.py`, `tests/unit/services/test_container_provision.py`

**Design:** `release_provision_to_error` today writes `status="error"`/`substatus="provision_failed"` with no cause, so the status endpoint cannot tell a transient failure (retry) from a deterministic one (surface). Classify the caught exception and persist it: add `last_error: str` and `retryable: bool` to the error transition. Classification: `ProvisionSuperseded` is already handled separately; a 4xx `PaperclipApiError`/config/validation error → `retryable=False`; ECS Spot-reclaim / throttling / 5xx / ENI errors → `retryable=True`. Keep the write token-conditioned as today.

- [ ] **Step 1:** Failing test — `release_provision_to_error(owner_id, claim_id, last_error="...", retryable=False)` persists both fields on the row.
- [ ] **Step 2:** Run, confirm FAIL.
- [ ] **Step 3:** Extend `release_provision_to_error` signature + UpdateExpression to write `last_error` + `retryable`; in `container_provision.py:116-135` classify the exception and pass them.
- [ ] **Step 4:** Run, confirm PASS.
- [ ] **Step 5: Commit** — `feat(provision): record failure cause (last_error + retryable) on the error transition`

---

### Task 4: Honest status for a failed container — stop masking `error` (D3, part 2)

**Files:**
- Modify: `apps/backend/routers/container.py:304-379` (the retryable-states branch) and `_status_response`
- Test: `apps/backend/tests/unit/routers/test_container_status.py`

**Design:** Today (line 318) `error` and `stopped` share the retry path, and lines 378-379 overwrite the response to `status="provisioning"`/`substatus="auto_retry"` for BOTH. Change so:
- `stopped` (healthy scaled-to-zero) keeps the silent auto-wake exactly as today.
- `error` with `retryable=True`: still auto-dispatch, but report **honestly** — `status="error"`, `substatus="error_retrying"` (NOT `provisioning`), plus `last_error`. The client renders "we hit a problem, retrying" and can escalate on its own; no server counter.
- `error` with `retryable=False`: do **not** re-dispatch; return `status="error"` + `last_error` so the client's error card renders immediately.
- The stale-lease/dead-worker path (`stale_provision`, line 315) is UNCHANGED — it remains the one legitimate time-based recovery.

- [ ] **Step 1:** Failing tests — (a) `error`+`retryable=False` returns `status="error"` and does NOT dispatch; (b) `error`+`retryable=True` returns `substatus="error_retrying"` (never `"provisioning"`) and dispatches; (c) `stopped` still auto-wakes and reports `provisioning`; (d) a stale `provisioning` lease with no live service still re-claims (liveness path intact).
- [ ] **Step 2:** Run, confirm FAIL.
- [ ] **Step 3:** Split the branch on `status`+`retryable`; remove the unconditional overwrite for `error`.
- [ ] **Step 4:** Run, confirm PASS.
- [ ] **Step 5: Commit** — `fix(provision): report container error honestly instead of masking it as provisioning`

---

### Task 5: Surface the Paperclip-seed failure through /container/status (D2)

**Files:**
- Modify: `apps/backend/routers/container.py` (`_status_response` and/or the no-row + running-container branches)
- Read: `apps/backend/core/repositories/paperclip_repo.py` (`get` / `find_org_owner`) and `paperclip_provisioning.py:_mark_failed`
- Test: `apps/backend/tests/unit/routers/test_container_status.py`

**Design:** `_mark_failed` records the seed failure as a `paperclip-companies` row with `status="failed"`, a table `/container/status` never reads. Add a read of the owner's paperclip-companies status and expose a **non-blocking** `teams_status` field on the response: `"failed"` when the seed failed, else present/absent as today. This is DEGRADED, not a hard block — the container still serves chat; the client shows a "Teams needs attention / retry" affordance (PR 2/PR 3), never a full "Setup failed" that hides chat. Add the field additively; do not change existing fields.

- [ ] **Step 1:** Failing test — with a `paperclip-companies` row `status="failed"` and a running container, `/container/status` returns `teams_status="failed"` and still `phase="ready"`/normal (chat not blocked).
- [ ] **Step 2:** Run, confirm FAIL.
- [ ] **Step 3:** Read the paperclip row in the status handler; add `teams_status` to `_status_response`. Keep it best-effort (a paperclip-read failure must not break the status endpoint).
- [ ] **Step 4:** Run, confirm PASS.
- [ ] **Step 5: Commit** — `feat(provision): expose teams_status so a failed Teams seed is visible to the client`

---

### Task 6: Fix 422 handling, the empty-id placeholder, and the no-CEO company (D4 + NEW-2)

**Files:**
- Modify: `apps/backend/core/services/user_provision.py:477-483, 574-581` (the 409-only guards)
- Modify: `apps/backend/core/services/paperclip_provisioning.py` — `_mark_failed` (~1170-1184, the `paperclip_user_id=""` placeholder), the `provision_org` unconditional `status="active"` (~531-544), and the false "409" comments (~275, 302, 639)
- Test: `apps/backend/tests/unit/services/test_paperclip_provisioning.py`

**Design:**
- The guards treat only `409` as idempotent-success and re-raise 422. With Task 1 in place, the re-raise no longer aborts the container — but the seed still hard-fails every poll. Correct the classification: a duplicate-email **422** is the "user already exists" signal, same category as 409. It must NOT be swallowed as success (Global Constraint) — route it to the recorded-failure path (`_mark_failed` + surfaced via Task 5), and fix the false "would 409" comments to say 422.
- `_mark_failed` writing `paperclip_user_id=""` guarantees the next poll can never adopt and re-signs-up → re-422 forever. Stop writing an empty id: leave the field absent/unchanged so a future adopt path (or the wipe-tooling fix) can work, rather than poisoning it.
- `provision_org` marks the company `status="active"` even when Friday/Atlas seeding threw (NEW-2) → an active company with no CEO, and idempotency then blocks re-seed. Only mark `active` when the required seed agents actually landed; otherwise record a degraded/failed status that a later poll can reconcile.

> NOTE: full orphan RECOVERY (adopt/reset a Better-Auth user whose password was wiped) is out of scope for PR 1 — it needs a Paperclip user-delete/reset the platform doesn't expose, and belongs with the wipe-tooling fix. PR 1's job is: don't wedge, don't lie, don't mark-active-with-no-CEO.

- [ ] **Step 1:** Failing tests — (a) a 422 from the owner seed is recorded (not raised as success, not swallowed as success); (b) `_mark_failed` does not write `paperclip_user_id=""`; (c) `provision_org` with a throwing Friday seed does NOT persist `status="active"`.
- [ ] **Step 2:** Run, confirm FAIL.
- [ ] **Step 3:** Apply the three changes + comment corrections.
- [ ] **Step 4:** Run, confirm PASS.
- [ ] **Step 5: Commit** — `fix(paperclip): handle 422 duplicates honestly; never mark a CEO-less company active`

---

## Self-Review Notes (already reconciled)

- Member path (verifier NEW-1) shares the 409-only guard at 574-581 — Task 6 fixes both owner and member guards.
- The `stale_provision` liveness path is deliberately untouched (Task 4) — it is the one retained time-based mechanism.
- Follow-ups explicitly NOT in this PR: the durable/restartable provisioning-job rearchitecture (the smell that necessitates the liveness check); the full 422-orphan RECOVERY + wipe-tooling Better-Auth-user delete; all client changes (PR 2 web, PR 3 mobile).
