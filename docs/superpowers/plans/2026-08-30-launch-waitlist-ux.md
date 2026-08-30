# Launch waitlist — conventional UX — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The waitlist behaves the way people expect from a waitlist product: the landing page shows it while it's open (pot + how many are on the list + "see your spot"), you get an immediate "You're #N" with your share link (in the app and by email), and every referral moves you up the queue.

**Architecture:** No new tables. One new settings field (`referral_jump`, spots per referral). Position stays the immutable sign-up order; an **effective position** = `max(1, position − referral_jump × referrals_signed_up)` is derived wherever a position is shown or used for the top-N slots. A small client component on the landing hero reads the public `GET /launch` and renders a pill linking to `/launch` when the competition is enabled. The Clerk `user.created` webhook sends a "You're #N on the list" email (via the existing `email_service`) instead of the generic welcome email while the competition is in its waitlist phase.

**Tech Stack:** FastAPI + DynamoDB, existing `email_service.send_email`, Next.js landing (`components/landing/HeroFunnel.tsx`, `app/landing.css`), the `/launch` components, the admin settings card.

**Spec:** `docs/superpowers/specs/2026-08-28-launch-competition-design.md` (update Decisions/Frontend in Task 4). Owner decision 2026-08-30: "I want the conventional UX", ship to prod when done.

## Global Constraints

- Backend: Python 3.12, ruff 120, `asyncio_mode="auto"`; DynamoDB via `run_in_thread`; never log secrets. Product name "Lightbulb". Frontend: brand tokens only (`node scripts/brand-tokens-scan.mjs src` prints nothing) — the landing exemption applies to `components/landing/**` and `app/landing.css` only.
- **`referral_jump`**: new int field on the settings row (`launch_settings_repo.DEFAULTS["referral_jump"] = 10`, in `_INT_FIELDS`, validated `≥ 0`, in `to_public`, in `LaunchSettingsUpdate` (required like the other seven → the PUT body is now eight fields), in `launch_phase.constants()`, and a "Spots per referral" input on the admin card).
- **Effective position** (single helper `launch_service.effective_position(row: dict, referral_jump: int) -> int` = `max(1, int(row["position"]) − referral_jump × int(row.get("referrals_signed_up", 0)))`). `GET /launch/me.waitlist` gains `effective_position`, `moved_up` (= position − effective_position), `referral_jump`; `in_comp_window` uses the EFFECTIVE position. The admin sheet's top-slot logic (`ops/launch.py`: `resolvable`, `top`, `top_slots_claimed/activated`) uses the effective position; the CSV gains an `effective_position` column right after `position`; `waitlist_rows` sorted by effective position.
- **Landing pill** (`components/launch/LaunchHeroPill.tsx`, client): `fetch(BACKEND_URL + "/launch")`; 404/error → render nothing; else `<a href="/launch" className="ld-launch-pill">` with copy by phase — waitlist: `Launch competition · {pot} pot · {signups} on the list → see your spot`; activation: `Launch competition · {pot} pot · doors are open → join`; live: `Launch competition is on · {competing} competing → the board`; ended: nothing. Mounted in `HeroFunnel.tsx` directly under the eyebrow `<p className="ld-mono ld-funnel-eyebrow">`; styled by a new `.ld-launch-pill` rule in `app/landing.css` using the landing palette (looks like a quiet mono pill; hover underline). Hydration-safe: render nothing until the fetch resolves (no SSR mismatch). Tests: renders nothing on 404; renders the waitlist copy with the number formatted; links to `/launch`.
- **Status copy** (`LaunchStatusLines.tsx`, used by the held card and `/launch`): `You're #{effective_position} on the list.` then, when `moved_up > 0`: `Up {moved_up} spots from {referrals_signed_up} referrals.` Always: `Each referral moves you up {referral_jump} spots.` `/launch` rules gain the same sentence. Copy must never say "first 220" as a promise — keep "the first {comp_slots} on the list get the free month".
- **Waitlist email** (`core/services/launch_waitlist_email.py`, `send_waitlist_email(*, to_email, first_name, effective_position, referral_link, drop_at, referral_jump, comp_slots)` → `email_service.send_email(kind="launch_waitlist", from_addr=settings.EMAIL_FROM_WELCOME, …)`, HTML + text, subject `You're #{n} on the Lightbulb list`; body: position, doors-open date in ET, "each referral moves you up {j} spots", the share link, "the first {comp_slots} on the list get the free month"). Wired in `routers/webhooks.py` `user.created`: when `launch_phase.schedule()` is set AND the phase is `waitlist` AND the join returned a row → send the waitlist email and SKIP `send_welcome_email` (one email, the right one); any other case → existing welcome email unchanged. Best-effort (never fails the webhook). Never raises.
- **Feature-off invariants unchanged**: nothing above renders/sends/computes when `schedule()` is `None`.
- Verification per task (scoped): backend `uv run pytest <files> -q --no-cov` + ruff; frontend tsc + `vitest run <dirs>` + eslint + brand scan; no full suites; never `sleep`/`tail`.

---

### Task 1: `referral_jump` + effective position (backend)

**Files:**
- Modify: `core/repositories/launch_settings_repo.py` (DEFAULTS/_INT_FIELDS), `core/services/launch_settings.py` (validate ≥ 0; `to_public`), `schemas/launch.py` (`LaunchSettingsUpdate.referral_jump`), `core/services/launch_phase.py` (`constants()`), `core/services/launch_service.py` (`effective_position`, `me()` fields, `join()` stores the effective position as `waitlist_position`), `core/services/ops/launch.py` (top-slot logic + CSV rows + sort), tests: `tests/unit/services/test_launch_settings.py`, `test_launch_service_me.py`, `test_ops_launch.py`, `tests/unit/routers/test_launch_me.py`, `test_ops_launch_routes.py` (PUT body now eight fields).

- [ ] Steps: tests first (validate rejects −1; `effective_position` floors at 1; `me().waitlist` fields incl. `in_comp_window` on the effective value — position 230 with 2 referrals × 10 → 210 → in window; ops `top` uses effective; CSV column present), implement, commit `feat(launch): referrals move you up the list — referral_jump setting + effective position everywhere a position is shown`.

---

### Task 2: "You're #N on the list" email (backend)

**Files:**
- Create: `core/services/launch_waitlist_email.py`, `tests/unit/services/test_launch_waitlist_email.py`.
- Modify: `routers/webhooks.py` (`user.created` branch: waitlist email instead of welcome when in the waitlist phase), `tests/unit/routers/test_webhooks_welcome.py` (both branches).

- [ ] Steps: tests first (renders position/link/date/jump; `send_email` called with `kind="launch_waitlist"`; webhook: in waitlist phase → waitlist email sent, welcome NOT sent; feature off → welcome sent as before; email failure never fails the webhook), implement, commit `feat(launch): the sign-up email is the waitlist confirmation while the list is open`.

---

### Task 3: Landing pill, queue-jump copy, admin field (frontend)

**Files:**
- Create: `components/launch/LaunchHeroPill.tsx` (+ test), `.ld-launch-pill` in `app/landing.css`.
- Modify: `components/landing/HeroFunnel.tsx` (mount the pill under the eyebrow — the ONLY change to that file; its existing tests must still pass — mock the pill in `HeroFunnel.test.tsx`), `components/launch/LaunchStatusLines.tsx` (+ test), `components/launch/LaunchPage.tsx` rules sentence, `lib/launch.ts` (`LaunchMe.waitlist` gains `effective_position`, `moved_up`, `referral_jump`), `app/admin/launch/LaunchSettingsCard.tsx` (+ "Spots per referral" input, in the PUT body; test), `app/admin/_lib/api.ts` (`LaunchSettings.referral_jump`).

- [ ] Steps: tests first, implement, `pnpm exec vitest run src/components/launch src/components/landing src/app/admin/launch src/app/launch`, tsc, eslint, brand scan, commit `feat(launch): the landing page shows the open waitlist; every referral moves you up; admin sets the jump`.

---

### Task 4: Docs + verification

- [ ] Spec: Decisions table — Waitlist row mentions queue-jumping (`referral_jump`, default 10) and the landing pill; Frontend section — pill + status copy; Config table — `referral_jump`. Commit `docs(launch): conventional waitlist UX`.
- [ ] Verification: backend `uv run pytest tests/unit -q --no-cov -k "launch or webhook or ops"` + ruff; frontend tsc + `vitest run src/components/launch src/components/landing src/app/admin src/app/launch src/lib src/__tests__/brandTokens.test.ts` + eslint + brand scan; infra untouched (no synth needed). Controller: push, PR, CodeRabbit, merge, dev verify with Claude-in-Chrome (enable on dev → landing pill visible; held card shows jump copy), then `gh workflow run deploy.yml --ref main` (owner: "I want this in prod"), verify prod, then disable dev.
