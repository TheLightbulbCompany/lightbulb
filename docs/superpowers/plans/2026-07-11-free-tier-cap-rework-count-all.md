# Free-Tier Cap Rework — Count Every Run + Per-Entrance Stop

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> Extends the cap already on branch `feat/free-tier-daily-cap` (PR #1219). Steps
> use `- [ ]`. Grounded on workflow `wpskxfqrq` (2026-07-11).

**Why:** The `/code-review` pass found the cap only enforces at the webchat
`gate_chat` chokepoint, so Meta spend from **Teams/Paperclip, steer, and
concurrent same-session runs** escapes the counter. Decision (owner): the free
tier includes **interactive** Teams (must be capped), but **autonomous**
cron/channels/routines are switched off (sub-spec 2, separate). So:
**measure every run at the one layer that sees all of them; stop at each entrance.**

**Architecture:**
- **Measure:** move counting into `_fetch_and_record_usage` (fires for every
  run). Webchat keeps reserve→reconcile; every *non-webchat* meta run has its
  actual cost ADDed to `meta_daily_spend`. Gate on `is_meta_model(model)` +
  session **source** (not "reserve absent").
- **Stop:** webchat at `gate_chat` (unchanged); interactive Teams via a
  read-only over-cap check in the Teams BFF; steer via a cap check.
- **Gate on the real meta signal:** stamp the provisioned provider on the
  container row so the pre-send block doesn't key on the mutable
  `subscription_status` (fixes the upgrade-window leak).

## Global Constraints

- Unit **microcents**; `CAP_MICROCENTS = 3_000_000`; UTC date; only meta runs
  (`is_meta_model`) count; `_reserves` key is `(owner_id, session_key.lower())`.
- **Count each run exactly once.** Webchat = reserve→reconcile; everything else
  = ADD, gated on session source via `_parse_session_key` — NOT on "reserve
  absent" (a duplicate `lifecycle/end` must not double-add).
- `record_usage` stays analytics-only; it must never touch `meta_daily_spend`.
- Same branch `feat/free-tier-daily-cap`; TDD; no push without approval.

---

## Task A — Count every meta run (`meta_cap.settle`)

**Files:** `core/services/meta_cap.py`, `core/gateway/connection_pool.py`;
Tests `tests/unit/services/test_meta_cap.py`,
`tests/unit/gateway/test_gate_chat_meta_cap.py`

**Grounding:** `_fetch_and_record_usage` (`connection_pool.py:978`) runs for all
sources; `model` captured at `:1040` (hoist before the `try` like
`raw_cost_microcents` at `:993`, else the `finally` NameErrors); the reconcile
call is at `:1124`; `_parse_session_key(session_key)` classifies source
(`is_channel_session`/`is_cron_session`/`is_isolated_session`(=paperclip) vs
webchat).

- [ ] **Test (meta_cap):** `settle(owner, key, actual, is_meta, source)`:
  - webchat source WITH a reserve → reconciles (reserve→actual), as today.
  - non-webchat source + `is_meta` + `actual>0` → ADDs `actual` to today's
    counter (no reserve involved).
  - non-webchat + non-meta → no-op. webchat + duplicate call (reserve already
    popped) → no-op (does NOT re-ADD).
- [ ] Implement `meta_cap.settle(owner_id, session_key, actual_microcents, *, is_meta, is_webchat)`:
  reuse the lowercase key + stored-date discipline; webchat branch = current
  `reconcile` logic; else-branch = `if is_meta and actual>0: add_meta_daily_spend(owner_id, utc_today(), actual)`.
  Keep `reconcile`/`release` intact (webchat still uses them via `settle`).
- [ ] **Wire** in `_fetch_and_record_usage`: hoist `model`; replace the
  `meta_cap.reconcile(...)` call with `meta_cap.settle(self.user_id, session_key, raw_cost_microcents or 0, is_meta=is_meta_model(model), is_webchat=<source is webchat>)`.
  Derive `is_webchat` from `_parse_session_key(session_key)` (webchat = not
  channel/cron/isolated). Import `is_meta_model`.
- [ ] Run both test files → pass. **Commit.**

> This closes the Teams/Paperclip/cron **counting** gap and the concurrent
> same-session **under-count** (#3/#7): every meta run adds its own actual cost.

## Task B — Interactive-Teams stop (read-only over-cap check)

**Files:** `core/services/meta_cap.py`, `routers/teams/issues.py`,
`routers/teams/interactions.py`; Tests for each router + `test_meta_cap.py`

**Grounding:** `add_issue_comment` (`teams/issues.py:248`) and
`respond_interaction` (`interactions.py:89`) are the user-post front doors;
`ctx.owner_id` (deps.py:140) is in scope; they proxy to Paperclip which triggers
Friday. A READ-ONLY check here stops the run before it starts. `get_meta_daily_spend`
(`billing_repo.py:449`) already exists (this resolves the #10 dead-code flag).

- [ ] **Test (meta_cap):** `is_over_daily_cap(owner_id)` → True when the owner
  is `trial_local` AND `get_meta_daily_spend(owner_id, utc_today()) >= CAP_MICROCENTS`;
  False for paid owners and under-cap trial owners.
- [ ] Implement `async def is_over_daily_cap(owner_id) -> bool` in `meta_cap.py`
  (fetch `billing_repo.get_by_owner_id`; gate on `subscription_status=="trial_local"`;
  read-only `get_meta_daily_spend`). **Do NOT reserve here** — a reserve placed
  in an HTTP handler is never reconciled (reconcile only runs on the ws usage
  path) and would leak.
- [ ] **Test (teams routers):** an over-cap `trial_local` owner posting a
  comment / responding to an interaction gets **402** with the structured
  `meta_daily_cap` payload; an under-cap / paid owner proceeds.
- [ ] Guard `add_issue_comment` + `respond_interaction` at the top (before the
  Paperclip call): `if await meta_cap.is_over_daily_cap(ctx.owner_id): raise HTTPException(402, detail={"blocked":True,"code":"meta_daily_cap","message":"You've used today's free allowance — resets tomorrow. Upgrade to keep going."})`.
- [ ] Run → pass. **Commit.**

## Task C — Gate the pre-send block on the provisioned provider (fix #4)

**Files:** `core/containers/ecs_manager.py`, `core/repositories/container_repo.py`,
`core/gateway/connection_pool.py`; Tests for each

**Grounding:** the pre-send reserve keys on the mutable `subscription_status`
(`connection_pool.py:1960`), which flips to `active` (sync Stripe webhook,
`stripe_webhook_handlers.py:233`) BEFORE the container is reprovisioned off meta
(fire-and-forget `background_provision`, `billing_service.py:773`). The container
row has no provider field today (`ecs_manager.py:614` upsert_fields). Do NOT use
`resolve_provider_choice` — it derives from the same mutable status.

- [ ] **Test:** `provision_user_container(provider_choice="meta", …)` writes a
  provider marker onto the container row; a bedrock/byo provision writes its own.
- [ ] Stamp the provider onto the container row at provision time: add
  `provider_choice` (or an `is_meta` bool) to the `upsert_fields` in
  `ecs_manager.py:614` (the method already has `provider_choice`).
- [ ] **Test (gate_chat):** the trial cap reserve fires when the CONTAINER row
  says meta even if `subscription_status` has flipped to `active` (upgrade
  window); it does NOT fire once the container is reprovisioned off meta.
- [ ] Change `gate_chat`'s cap predicate (`connection_pool.py:1959-1960`) to gate
  on the container-row provider marker (via `container_repo.get_by_owner_id`)
  being meta, rather than `subscription_status=="trial_local"`. (The post-turn
  count in Task A already keys on `is_meta_model(model)`, the authoritative
  backstop, so an uncapped meta run is caught at reserve or at settle.)
- [ ] Run → pass. **Commit.**

## Task D — Cap the steer path (fix #2)

**Files:** `routers/websocket_chat.py`; Test `tests/unit/gateway/` or a
websocket test

**Grounding:** `_process_agent_steer_background` (`websocket_chat.py:2249`)
dispatches `chat.send` with no gate; a steer can run a fresh Meta turn uncapped.

- [ ] **Test:** an over-cap `trial_local` owner's steer is blocked (no dispatch),
  same structured error; under-cap proceeds.
- [ ] Add a cap check before the steer dispatch — call `pool.gate_chat(...)`
  (with the steer's `session_key`) OR a read-only `meta_cap.is_over_daily_cap`
  check if a reserve on the steer path would double-count with the main run's
  reserve. **Prefer the read-only over-cap check** here (steer shares the run's
  session/lifecycle; a second reserve on the same session_key would collide) —
  block if over cap, else let it through and let Task A's settle count it.
- [ ] Run → pass. **Commit.**

## Task E — Fix the over-eager timeout refund (fix #5)

**Files:** `routers/websocket_chat.py`, possibly `core/services/meta_cap.py`;
Tests

**Grounding:** the error tail (`websocket_chat.py:2067`) calls `meta_cap.release()`
on ANY `chat.send` failure, assuming $0. But an ambiguous `asyncio.TimeoutError`
(ACK delayed past `_RPC_TIMEOUT`) does not prove the container never ran — the
turn may have run, and releasing then drops the reconcile so real cost never
counts.

- [ ] **Test:** on a definite non-delivery (e.g. `ConnectionError` before send)
  → reserve released (refunded). On an ambiguous `TimeoutError` → reserve is
  KEPT (not released), so if the run actually happened its cost is reconciled by
  the later `lifecycle/end`, else the worst-case reserve stands until midnight
  (fail-closed).
- [ ] Narrow the `release()` call to the error branches that prove the send
  never reached the container; on ambiguous timeout, do NOT release (keep the
  reserve). Confirm the four `except` branches (`websocket_chat.py:2017-2032`)
  and gate `release` accordingly.
- [ ] Run → pass. **Commit.**

---

## Sequencing & notes
- Order: **A** (count-all — highest value, fixes #1-count + #3/#7) → **B** (Teams
  stop, resolves #10) → **C** (provider gate, #4) → **D** (steer, #2) → **E**
  (timeout, #5).
- **Deferred as fail-closed minors** (over-restrict, never overspend), noted for
  the final review: **#8** (reserve orphaned when a container crashes before
  `lifecycle/end` → wrong lockout — needs a reserve-TTL, which conflicts with
  the no-sweeper stance) and **#9** (reserve stranded if the over-cap rollback
  ADD fails). Both only *shrink* a free allowance; revisit if they bite.
- **Out of scope (sub-spec 2):** switching autonomous cron/channels/routines off
  for free owners + the exec/EFS-config tamper lockdown (#6).
- After all tasks: final whole-branch review + a second `/code-review` pass
  before merge.
