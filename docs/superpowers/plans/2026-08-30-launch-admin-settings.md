# Launch Competition — admin-managed settings — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every human input the Launch Competition needs — the two dates, the pot constants, the comp slot count, the free-month coupon — is edited on `/admin/launch` and takes effect within 30 seconds, with no PR and no deploy. The Stripe coupon + promotion code can be created from the same card.

**Architecture:** One DynamoDB row per environment (`lightbulb-{env}-launch-settings`, item `id="settings"`) replaces `launch-schedule.json` + the `LAUNCH_SCHEDULE_JSON` env + `STRIPE_FREE_MONTH_COUPON_ID`. `launch_phase` reads the row through a 30-second in-process cache (it sits on the container-status poll path). Admin routes (`GET/PUT /admin/launch/settings`, `POST /admin/launch/settings/stripe-coupon`) are platform-admin-gated, validated exactly like the retired synth validator, audited via `@audit_admin_action`, and invalidate the cache + the public response caches on write. The admin sheet gains a **Launch settings** card.

**Tech Stack:** FastAPI + DynamoDB (moto in tests), stripe-python (top-level `stripe.Coupon`/`stripe.PromotionCode`), CDK (`database-stack.ts`), Next.js admin console (`app/admin/launch`), vitest/pytest.

**Spec:** the owner-approved design in chat (2026-08-30): settings card fields = `enabled`, `drop_at`, `competition_start_at` (ET pickers → ISO with offset), `comp_slots`, `competition_days`, `pot_front_cents`, `pot_per_entrant_cents`, `stripe_coupon_id`, `stripe_promotion_code` (display only), plus "Create Stripe coupon + promo code". Base spec for everything else: `docs/superpowers/specs/2026-08-28-launch-competition-design.md` (update its Config/Runbook in Task 5).

## Global Constraints

- Backend: Python 3.12, ruff line-length 120, `asyncio_mode = "auto"`; every DynamoDB call via `core.dynamodb.run_in_thread`; tables via `core.dynamodb.get_table("launch-settings")` (prefix applied by the helper). Never log a Stripe secret; the coupon/promo ids are not secrets.
- **Settings row shape** (item `id="settings"`): `enabled: bool` (default False), `drop_at: str|None`, `competition_start_at: str|None` (ISO-8601 WITH offset — `parse_iso` contract unchanged), `comp_slots: int` (220), `competition_days: int` (30), `pot_front_cents: int` (100_000), `pot_per_entrant_cents: int` (1_000), `stripe_coupon_id: str` (""), `stripe_promotion_code_id: str` (""), `stripe_promotion_code: str` (""), `updated_at`, `updated_by`.
- **Feature-off semantics unchanged:** `launch_phase.schedule()` returns `None` when `enabled` is False OR either date is null; `require_launch` 404s; the hold never fires; the webhook join returns first thing.
- **Validation (server, `launch_settings.validate(payload)`)**: dates must match `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$` AND parse via `parse_iso` (Python raises on `24:00`/Feb 29 — no separate calendar check needed); `competition_start_at > drop_at`; ints ≥ 0 (`comp_slots ≥ 1`, `competition_days ≥ 1`); `enabled=True` requires both dates. Errors → 422 with field-level messages.
- **Cache:** `launch_phase._CACHE` (row + fetched_at), TTL 30 s; `launch_phase.invalidate()` clears it. `Schedule` dataclass field order unchanged: `(drop_at, competition_start_at, competition_end_at, comp_slots, activation_window_hours, competition_days)`. `constants()` returns `{competition_days, comp_slots, pot_front_cents, pot_per_entrant_cents}` from the row (defaults when no row). New `coupon_id() -> str`.
- **Delete, do not keep for compatibility:** `launch-schedule.json`, `launch_phase.load_schedule_file/_FILE/env_key`, the `LAUNCH_SCHEDULE_JSON` env + `validateLaunchSchedule` in `service-stack.ts` (+ its tests), `apps/frontend/src/lib/launchSchedule.ts` (+ test), `settings.STRIPE_FREE_MONTH_COUPON_ID` (+ CDK env + test assertion). Grep gates at the end of each task.
- **Admin gate**: every new route `Depends(require_platform_admin)`; writes decorated `@audit_admin_action("launch.settings.update", target_user_id_override="__launch__", capture_params=["body"])` / `"launch.settings.stripe_coupon"`. Reads of the settings never audit.
- **Stripe coupon creation** (`launch_settings.create_stripe_coupon(auth)`): refuses (409 `already_configured`) when `stripe_coupon_id` is set, unless `body.force`; requires `enabled` dates set (needs `competition_start_at` for `expires_at`); `stripe.Coupon.create(percent_off=100, duration="once", name="Lightbulb launch free month", idempotency_key=f"launch-coupon:{env}:{competition_start_at}")`, then `stripe.PromotionCode.create(coupon=coupon.id, code=<generated "LIGHTBULB-XXXX">, max_redemptions=comp_slots, expires_at=int(competition_start_at.timestamp()), idempotency_key=...)`; store ids + code on the row; return them. Map `stripe.StripeError` → 502 `stripe_error` (message logged, not echoed).
- **Response-cache invalidation on write**: `PUT` and the coupon route call `launch_phase.invalidate()`, `routers.launch.clear_response_cache()` (lazy import) and `core.services.ops.launch.clear_cache()`.
- Frontend: brand tokens only (`node scripts/brand-tokens-scan.mjs src` prints nothing); product name "Lightbulb"; admin data via server actions (`adminGet/adminPost` in `app/admin/_actions/_shared.ts`); typed confirm via `ConfirmActionDialog` (`confirmText`); inputs via `@/components/ui/input`.
- **ET handling (client)**: pickers are `<input type="datetime-local">` labelled "Eastern time"; on save convert wall time → ISO with the America/New_York offset for THAT date via `Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", timeZoneName: "longOffset" })` (yields `GMT-04:00`/`GMT-05:00`); on load convert the stored ISO back to ET wall time for display. Unit-test both directions incl. a DST-boundary date.
- Verification commands (run at the end of every task, scoped): backend `uv run pytest <files> -q --no-cov` + `uv run ruff check .`; frontend `pnpm exec tsc --noEmit -p tsconfig.json`, `pnpm exec vitest run <dirs>`, eslint on touched files, brand scan; infra `npx vitest run test/database-stack.test.ts test/service-stack.test.ts`, `npx cdk synth`, `git status --short .github/workflows/deploy.yml` EMPTY. No full suites; never `sleep`/`tail`.

---

### Task 1: Settings row + `launch_phase` reads it (backend)

**Files:**
- Create: `apps/backend/core/repositories/launch_settings_repo.py`, `apps/backend/core/services/launch_settings.py` (validation + defaults + `to_public()`), `apps/backend/tests/unit/repositories/test_launch_settings_repo.py`, `apps/backend/tests/unit/services/test_launch_settings.py`
- Modify: `apps/backend/core/services/launch_phase.py` (rewrite the loader), `apps/backend/tests/conftest.py` (`launch_schedule` fixture now patches `launch_phase._CACHE`/the repo `get`), `apps/backend/tests/unit/services/test_launch_phase.py`, `apps/backend/core/services/launch_referral.py` (`coupon = launch_phase.coupon_id()`), `apps/backend/core/config.py` (delete `STRIPE_FREE_MONTH_COUPON_ID`), every test that patches `settings.STRIPE_FREE_MONTH_COUPON_ID` (grep) → patch `launch_phase.coupon_id`.
- Delete: `launch-schedule.json` (repo root).

**Interfaces:**
- `launch_settings_repo.get() -> dict|None`, `put(item: dict) -> dict` (full replace of item `id="settings"`, stamps `updated_at`), `DEFAULTS: dict`.
- `launch_settings.DEFAULTS`, `validate(payload: dict) -> dict` (returns the normalized row or raises `SettingsInvalid(errors: dict[str, str])`), `effective() -> dict` (DEFAULTS ⊕ row), `to_public(row) -> dict` (no `updated_by`).
- `launch_phase.schedule() -> Schedule|None` (None unless `enabled` and both dates), `constants() -> dict`, `coupon_id() -> str`, `invalidate() -> None`, `pot_cents(n)` unchanged, `_CACHE_TTL_S = 30`.
- Conftest: `launch_schedule(drop_at=None, start_at=None, *, comp_slots=220, competition_days=30, enabled=None)` → patches `launch_settings_repo.get` to return the synthesized row (`enabled` defaults to `drop_at is not None`) and calls `launch_phase.invalidate()` on enter/exit. Existing call sites keep working unchanged.

- [ ] Steps: tests first (repo put/get round-trip with moto; validate: bad grammar, naive timestamp, `24:00`, start ≤ drop, negative ints, enabled without dates → field errors; `schedule()` None when disabled / when a date is null; cache honours TTL and `invalidate()`; `coupon_id()` from the row; `launch_referral.grant_free_month` banks when `coupon_id()` is ""), implement, `git grep -n "STRIPE_FREE_MONTH_COUPON_ID\|LAUNCH_SCHEDULE_JSON\|load_schedule_file\|launch-schedule.json" -- apps/backend` → 0, commit `feat(launch): settings row replaces the schedule file — launch_phase reads DynamoDB through a 30s cache`.

---

### Task 2: Admin routes — read, update, create Stripe coupon (backend)

**Files:**
- Modify: `apps/backend/routers/ops.py` (add `GET /ops/launch/settings`, `PUT /ops/launch/settings`, `POST /ops/launch/settings/stripe-coupon`; the existing `GET /ops/launch` payload gains `settings: to_public(effective())` and `enabled`), `apps/backend/core/services/launch_settings.py` (`update(auth, payload)`, `create_stripe_coupon(auth, *, force=False)`), `apps/backend/schemas/launch.py` (`LaunchSettingsUpdate`, `StripeCouponCreateRequest{force: bool=False}`), `apps/backend/core/services/ops/launch.py` (`compute()` also returns `settings`), tests `tests/unit/routers/test_ops_launch_routes.py`, `tests/unit/services/test_launch_settings.py`.

**Interfaces:**
- `PUT /ops/launch/settings` body = the public row fields; 422 `{errors: {field: msg}}` on validation; 200 → the saved public row; audited; invalidates the three caches.
- `POST /ops/launch/settings/stripe-coupon {force}` → 200 `{stripe_coupon_id, stripe_promotion_code_id, stripe_promotion_code}`; 409 `already_configured`; 422 when dates missing; 502 `stripe_error`.
- `ops/launch.compute()` → adds `settings` (public row) and `enabled`; when disabled it returns `(data, "unavailable", …)` as today but WITH `settings` so the admin can enable.

- [ ] Steps: tests first (403 non-admin; PUT validation errors; PUT success writes the row, audit row created, caches cleared — assert `launch_phase.invalidate` + `clear_response_cache` + `clear_cache` called; coupon: 409 when set, `force` overrides, Stripe calls made with `max_redemptions=comp_slots` and `expires_at=start`, ids stored, `StripeError` → 502, no key/secret in any log line), implement, commit `feat(launch): admin reads/writes the launch settings and mints the Stripe coupon`.

---

### Task 3: Infra — table in, env + validator out

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts` (`createTable("LaunchSettingsTable", "launch-settings", { partitionKey: { name: "id", type: STRING } })`), `apps/infra/lib/stacks/service-stack.ts` (delete the `LAUNCH_SCHEDULE_JSON` read/validate/inject block and `STRIPE_FREE_MONTH_COUPON_ID`), `apps/infra/test/database-stack.test.ts` (table asserted), `apps/infra/test/service-stack.test.ts` (delete the `validateLaunchSchedule` + `LAUNCH_SCHEDULE_JSON` + coupon-id tests; assert neither name appears in the backend container env).

- [ ] Steps: tests first, implement, `npx vitest run test/database-stack.test.ts test/service-stack.test.ts`, `npx cdk synth`, `git status --short .github/workflows/deploy.yml` EMPTY, `git grep -n "LAUNCH_SCHEDULE_JSON\|validateLaunchSchedule\|STRIPE_FREE_MONTH_COUPON_ID\|launch-schedule" -- apps/infra` → 0, commit `feat(infra): launch-settings table; the schedule env and validator retire`.

---

### Task 4: Admin console — Launch settings card

**Files:**
- Create: `apps/frontend/src/app/admin/launch/LaunchSettingsCard.tsx`, `apps/frontend/src/app/admin/launch/etTime.ts` (`toEasternLocal(iso) -> "YYYY-MM-DDTHH:mm"`, `fromEasternLocal(local) -> ISO with offset`), tests `__tests__/LaunchSettingsCard.test.tsx`, `__tests__/etTime.test.ts`.
- Modify: `app/admin/launch/LaunchAdminClient.tsx` (render the card first; when `enabled` is false show the card + a quiet "competition disabled" note instead of the `unavailable` banner), `app/admin/launch/page.tsx` (pass settings), `app/admin/_lib/api.ts` (`LaunchOpsData.settings`, `LaunchSettings` type), `app/admin/_actions/launch.ts` (`saveLaunchSettings(body)`, `createLaunchStripeCoupon(force)`), tests.
- Delete: `apps/frontend/src/lib/launchSchedule.ts`, `apps/frontend/src/lib/__tests__/launchSchedule.test.ts`.

**Behaviour:**
- Fields: Enabled switch; Drop (ET) + Competition start (ET) `datetime-local`; comp slots; competition days; pot front ($, whole dollars ↔ cents); pot per entrant ($); Stripe coupon id (text) + promotion code (read-only) + **Create Stripe coupon + promo code** button (disabled until dates are set; typed confirm with `create` when a coupon already exists → `force`).
- Save → `PUT`; field errors from 422 render under the inputs; success re-fetches (`router.refresh()`). If the save changes `enabled` or moves `drop_at` across "now" (client clock), open `ConfirmActionDialog` with `confirmText="launch"` first ("this changes what every signed-in user sees within 30 seconds").
- Copy: a one-line hint under the dates: "Doors open at drop; Join opens at competition start; the promo code expires at competition start."

- [ ] Steps: tests first (etTime both directions incl. a March DST date; card renders values; save posts the converted ISO; 422 errors render; confirm dialog appears on phase flip; coupon button disabled without dates), implement, `pnpm exec vitest run src/app/admin/launch src/lib`, tsc, eslint, brand scan, commit `feat(admin): launch settings card — dates, pot, slots, coupon, one click to mint the Stripe promo code`.

---

### Task 5: Docs + skill + PR body

**Files:**
- Modify: `docs/superpowers/specs/2026-08-28-launch-competition-design.md` (Config section → the settings row + admin card; Runbook step 1 → "open /admin/launch, set dates, Save; Create Stripe coupon"; remove `launch-schedule.json` / `STRIPE_FREE_MONTH_COUPON_ID` mentions — `git grep -c` → 0), `apps/infra/openclaw/skills/lightbulb/SKILL.md` only if it mentions the schedule file (it should not).
- [ ] Steps: edit, commit `docs(launch): settings live in the admin console`.

---

### Task 6: Verification + PR

- [ ] Backend `uv run pytest tests/ -q --no-cov -k "launch or ops or webhook or provision_gate or container_status or user_provision"` + ruff; frontend tsc + `vitest run src/app/admin src/components/launch src/components/chat src/lib src/__tests__/brandTokens.test.ts` + eslint + brand scan; infra tests + synth + drift; grep gates repo-wide: `git grep -n "launch-schedule\|LAUNCH_SCHEDULE_JSON\|STRIPE_FREE_MONTH_COUPON_ID\|validateLaunchSchedule\|load_schedule_file" -- apps packages docs` → 0 (except git history). Controller: push, PR, CodeRabbit, dev deploy, then a Claude-in-Chrome walkthrough on `admin.dev.flickmeon.ai/admin/launch`.
