# Launch Competition — Addendum Implementation Plan (product decisions of 2026-08-28/29)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rework the shipped-but-unmerged Launch Competition (PR #2133, branch `feat/launch-competition`) to the owner's clarified product: sign-ups stay open the whole time; the waitlist IS a Clerk account + the stashed idea; accounts are held (no container, no trial clock) until the drop; joining the competition requires the activation signal + a subscription; Stripe is an optional "get on the board" step; two pots (public/projected vs funded); one schedule file read by every surface; plus the CI brand-token failure and CodeRabbit's valid findings.

**Architecture:** `launch-schedule.json` at the repo root becomes the single source of dates and pot constants (per-env keys, like `openclaw-version.json`), read by the backend at runtime and importable by the frontend at build time; the `LAUNCH_*` settings/CDK env vars are deleted. A `launch_pending` provision gate holds pre-drop accounts before the free-trial row is stamped. The Clerk `user.created` webhook writes the waitlist row (position = sign-up order). `POST /launch/enter` splits into `POST /launch/join` (activation + subscription → entry, "competing") and `POST /launch/connect-stripe` (key → board). Public pot = front + per × sign-ups; funded pot (admin) = front + per × first-invoice-counted rows. The email-only waitlist form, the hero swap and `useLaunchPhase` are removed.

**Tech Stack:** as the base plan (FastAPI + DynamoDB/moto, stripe-python 14, CDK/vitest, Next.js 16 + vitest).

**Spec:** `docs/superpowers/specs/2026-08-28-launch-competition-design.md` — the base spec; Task 7 reconciles it. Where this addendum and the spec disagree, THIS ADDENDUM WINS (it encodes the owner's later decisions).

## Global Constraints

- Everything in the base plan's Global Constraints still applies (ruff 120, `run_in_thread`, no key in logs, `ld-*` on the public page, no Tailwind palette classes ANYWHERE now — see below, commit per task, never push without the owner's go — the controller pushes to the PR branch at the end).
- **Brand tokens are enforced repo-wide** by `apps/frontend/src/__tests__/brandTokens.test.ts` (`scripts/brand-tokens-scan.mjs`): no colour literals, no `zinc-*`-style palette classes, no retired font vars outside `components/landing/` and `app/landing.css`. Sanctioned font vars: `--font-sans`, `--font-serif`, `--font-mono`. Admin surfaces use the semantic classes the console now uses: `text-foreground`, `text-muted-foreground`, `hover:bg-accent`, `hover:text-foreground`, `border-border`, `bg-card`, `text-err`. Run `node scripts/brand-tokens-scan.mjs src` from `apps/frontend` — it must print nothing.
- **Dates**: `launch-schedule.json` holds ISO instants WITH offset (owner: drop = Thursday midnight Eastern = `…T00:00:00-04:00` Friday; competition start = Sunday midnight Eastern = Monday `00:00:00-04:00`). `null` = feature off. No `LAUNCH_*` env vars remain anywhere (settings, CDK, tests, docs).
- **Hold semantics**: pre-drop AND no container row ⇒ `launch_pending` gate, evaluated BEFORE `ensure_personal_trial_default` in every door, so no trial row exists until the first poll after the drop. Existing owners (container row present) are never held.
- **Pots**: public `pot_cents = pot_front_cents + pot_per_entrant_cents × signups` (waitlist rows); funded `funded_pot_cents = pot_front_cents + pot_per_entrant_cents × rows with pot_counted_at` (set once from the owner's first `invoice.paid`, any amount — redemption counts).
- **Competing** = entry rows (activation + live subscription required to create one). **On the board** = entries with a stored key. `rank`/`refresh_all` consider only entries with `has_key`.
- Backend verification scope (owner directive: never the full 20-min suite locally): `cd apps/backend && uv run ruff check . && uv run ruff format --check . && uv run pytest tests/unit -q -x -k "launch or stripe_webhook or lifespan or ops or provision_gate or container_status or webhooks or user_provision"`.
- Frontend verification: `cd apps/frontend && pnpm tsc --noEmit -p tsconfig.json && pnpm lint && node scripts/brand-tokens-scan.mjs src && NODE_OPTIONS='--max-old-space-size=8192' pnpm vitest run src/components/launch src/app/admin/launch src/lib/__tests__ src/components/landing/__tests__/HeroFunnel.test.tsx src/components/chat/__tests__ src/__tests__/brandTokens.test.ts`.
- Infra verification: `cd apps/infra && npx vitest run && npx cdk synth > /dev/null && git -C ../.. status --short .github/workflows/deploy.yml` (no drift expected).

## File Structure

- Create: `launch-schedule.json` (repo root); `apps/backend/core/services/launch_hold.py`; `apps/frontend/src/lib/launchSchedule.ts` (+ test); `apps/frontend/src/components/chat/LaunchHeldCard.tsx` (+ test); `apps/frontend/src/components/launch/JoinForm.tsx`, `ConnectStripeForm.tsx` (rename of `EnterForm`).
- Modify: `core/config.py` (delete `LAUNCH_*`), `core/services/launch_phase.py` (file-backed schedule), `core/services/provision_gate.py` + `core/services/user_provision.py` + `routers/container.py` (hold), `routers/webhooks.py` (join), `routers/launch.py` + `schemas/launch.py` + `core/services/launch_service.py` + `core/repositories/launch_entries_repo.py` + `launch_waitlist_repo.py` + `launch_revenue.py` + `launch_referral.py` + `ops/launch.py` + `routers/ops.py`; CDK `service-stack.ts` + tests; frontend `lib/launch.ts`, `components/launch/*`, `components/chat/ProvisioningStepper.tsx`, `components/landing/HeroFunnel.tsx` (+test, revert to main), `app/admin/launch/*`, `app/admin/_lib/api.ts`; docs (spec, base plan constraints, skill anchor).
- Delete: `components/launch/WaitlistForm.tsx`, `useLaunchPhase.ts`, their tests; `POST /launch/waitlist` + `POST /launch/enter` + their tests.

---

### Task 1: One schedule file — `launch-schedule.json`, backend loader, CDK/env removal, frontend import

**Files:**
- Create: `launch-schedule.json` (repo root), `apps/frontend/src/lib/launchSchedule.ts`, `apps/frontend/src/lib/__tests__/launchSchedule.test.ts`
- Modify: `apps/backend/core/services/launch_phase.py`, `apps/backend/core/config.py`, `apps/infra/lib/stacks/service-stack.ts`, `apps/infra/test/service-stack.test.ts`, every backend test that patches `launch_phase.settings.LAUNCH_*` (grep: `tests/unit/services/test_launch_phase.py`, `tests/unit/routers/test_launch_public.py`, `test_launch_me.py`, `test_launch_enter.py`, `tests/unit/services/test_launch_revenue_sweep.py`, `test_ops_launch.py`, `test_launch_referral.py`)
- Test: `apps/backend/tests/unit/services/test_launch_phase.py` (rewrite)

**Interfaces:**
- Produces (backend `launch_phase`): `load_schedule_file() -> dict` (searches upward from the module for `launch-schedule.json`, like `core/openclaw_version.py` does for `openclaw-version.json`; cached in a module global `_FILE`; tests replace `_FILE`); `env_key() -> "dev" | "prod"` (`settings.ENVIRONMENT == "prod"` → `prod`, else `dev`); `schedule() -> Schedule | None` — `None` when the env's `drop_at` or `competition_start_at` is `null`; `Schedule` gains nothing new but is now built as `Schedule(drop_at, competition_start_at, competition_end_at=start + competition_days, comp_slots, activation_window_hours=<int((start-drop).total_seconds()//3600)>, competition_days)` so every existing consumer keeps working; `pot_cents(n)` reads `pot_front_cents`/`pot_per_entrant_cents` from the file; new `constants() -> dict` returning `{"comp_slots", "competition_days", "pot_front_cents", "pot_per_entrant_cents"}`.
- Produces (frontend `lib/launchSchedule.ts`): `export const LAUNCH_SCHEDULE = schedule as LaunchScheduleFile;` typed import of the root JSON, plus `export function launchDatesFor(env: "dev" | "prod")`.
- Removed: `settings.LAUNCH_DROP_AT`, `LAUNCH_COMP_SLOTS`, `LAUNCH_ACTIVATION_WINDOW_HOURS`, `LAUNCH_COMPETITION_DAYS`, `LAUNCH_PRIZE_FRONT_CENTS`, `LAUNCH_POT_PER_ENTRANT_CENTS`; the matching CDK env entries and their test. `STRIPE_FREE_MONTH_COUPON_ID` STAYS (it is a per-env secret-ish id, not a date).

- [ ] **Step 1: Write the schedule file**

```json
{
  "notes": [
    "THE ONE PLACE the Launch Competition's dates and pot constants live. Read by the backend at runtime (core/services/launch_phase.py) and by the frontend at build time (apps/frontend/src/lib/launchSchedule.ts). Change a date = one commit; merge deploys dev, the prod promotion carries it.",
    "Instants carry their offset. Owner: the drop is Thursday night at midnight Eastern (so Friday 00:00 -04:00); the competition starts Sunday night at midnight Eastern (Monday 00:00 -04:00). null = feature off (every /launch* route 404s, nothing is held, nothing refreshes).",
    "pot_per_entrant_cents is the owner's pledge per person. The PUBLIC pot counts sign-ups; the FUNDED pot (admin) counts people whose first invoice has landed."
  ],
  "competition_days": 30,
  "comp_slots": 220,
  "pot_front_cents": 100000,
  "pot_per_entrant_cents": 1000,
  "dev": { "drop_at": null, "competition_start_at": null },
  "prod": { "drop_at": null, "competition_start_at": null }
}
```

- [ ] **Step 2: Rewrite `test_launch_phase.py` against the file**

```python
"""Phase math for the Launch Competition — pure functions over launch-schedule.json."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.services import launch_phase

DROP = datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)  # Thu Sep 17 midnight Eastern = Fri 00:00 -04:00
START = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)  # Sun Sep 20 midnight Eastern = Mon 00:00 -04:00


def _file(dev=None, prod=None, **over):
    base = {"competition_days": 30, "comp_slots": 220, "pot_front_cents": 100_000, "pot_per_entrant_cents": 1_000,
            "dev": dev or {"drop_at": None, "competition_start_at": None},
            "prod": prod or {"drop_at": None, "competition_start_at": None}}
    base.update(over)
    return base


CONFIGURED = _file(dev={"drop_at": "2026-09-18T00:00:00-04:00", "competition_start_at": "2026-09-21T00:00:00-04:00"})


@pytest.fixture
def configured():
    with patch.object(launch_phase, "_FILE", CONFIGURED), patch.object(launch_phase.settings, "ENVIRONMENT", "dev"):
        yield


def test_schedule_is_none_when_env_dates_are_null():
    with patch.object(launch_phase, "_FILE", _file()), patch.object(launch_phase.settings, "ENVIRONMENT", "dev"):
        assert launch_phase.schedule() is None


def test_schedule_selects_the_env_key():
    prod_only = _file(prod={"drop_at": "2026-09-18T00:00:00-04:00", "competition_start_at": "2026-09-21T00:00:00-04:00"})
    with patch.object(launch_phase, "_FILE", prod_only):
        with patch.object(launch_phase.settings, "ENVIRONMENT", "dev"):
            assert launch_phase.schedule() is None
        with patch.object(launch_phase.settings, "ENVIRONMENT", "prod"):
            assert launch_phase.schedule().drop_at == DROP


def test_schedule_derives_end_and_window(configured):
    s = launch_phase.schedule()
    assert s.drop_at == DROP and s.competition_start_at == START
    assert s.competition_end_at == START + timedelta(days=30)
    assert s.activation_window_hours == 72 and s.comp_slots == 220


def test_schedule_rejects_start_before_drop():
    bad = _file(dev={"drop_at": "2026-09-21T00:00:00-04:00", "competition_start_at": "2026-09-18T00:00:00-04:00"})
    with patch.object(launch_phase, "_FILE", bad), patch.object(launch_phase.settings, "ENVIRONMENT", "dev"):
        with pytest.raises(ValueError):
            launch_phase.schedule()


@pytest.mark.parametrize(
    "now,expected",
    [
        (DROP - timedelta(days=21), "waitlist"),
        (DROP - timedelta(seconds=1), "waitlist"),
        (DROP, "activation"),
        (START - timedelta(seconds=1), "activation"),
        (START, "live"),
        (START + timedelta(days=29), "live"),
        (START + timedelta(days=30), "ended"),
    ],
)
def test_phase_boundaries(configured, now, expected):
    assert launch_phase.phase(launch_phase.schedule(), now) == expected


def test_pot_reads_the_file(configured):
    assert launch_phase.pot_cents(0) == 100_000
    assert launch_phase.pot_cents(500) == 600_000
    assert launch_phase.constants() == {"comp_slots": 220, "competition_days": 30, "pot_front_cents": 100_000, "pot_per_entrant_cents": 1_000}


def test_real_file_parses_and_is_off_by_default():
    f = launch_phase.load_schedule_file()
    for env in ("dev", "prod"):
        assert set(f[env]) == {"drop_at", "competition_start_at"}
        if f[env]["drop_at"] is not None:
            assert launch_phase.parse_iso(f[env]["competition_start_at"]) > launch_phase.parse_iso(f[env]["drop_at"])
```

- [ ] **Step 3: Rewrite `launch_phase.py`**

```python
"""Launch Competition schedule — pure functions over launch-schedule.json, no I/O after load.

launch-schedule.json at the repo root is THE source of the dates and pot
constants for every surface (backend runtime here; frontend build time via
apps/frontend/src/lib/launchSchedule.ts). Keyed per environment like
openclaw-version.json. null dates = feature off.

Phase ladder: waitlist (before the drop) → activation (drop .. competition
start) → live → ended.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from core.config import settings

Phase = Literal["waitlist", "activation", "live", "ended"]
FILENAME = "launch-schedule.json"
_FILE: dict | None = None


@dataclass(frozen=True)
class Schedule:
    drop_at: datetime
    competition_start_at: datetime
    competition_end_at: datetime
    comp_slots: int
    activation_window_hours: int
    competition_days: int


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_schedule_file() -> dict:
    """Read launch-schedule.json by searching upward from this file (same
    posture as core/openclaw_version.py — the backend image copies the file
    into its build context; a hardcoded parents[N] would break in the container)."""
    global _FILE
    if _FILE is not None:
        return _FILE
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        candidate = ancestor / FILENAME
        if candidate.is_file():
            _FILE = json.loads(candidate.read_text())
            return _FILE
    raise FileNotFoundError(FILENAME)


def env_key() -> str:
    return "prod" if settings.ENVIRONMENT == "prod" else "dev"


def constants() -> dict:
    f = load_schedule_file()
    return {
        "comp_slots": int(f["comp_slots"]),
        "competition_days": int(f["competition_days"]),
        "pot_front_cents": int(f["pot_front_cents"]),
        "pot_per_entrant_cents": int(f["pot_per_entrant_cents"]),
    }


def schedule() -> Schedule | None:
    f = load_schedule_file()
    env = f[env_key()]
    if not env.get("drop_at") or not env.get("competition_start_at"):
        return None
    drop = parse_iso(env["drop_at"])
    start = parse_iso(env["competition_start_at"])
    if start <= drop:
        raise ValueError("launch-schedule.json: competition_start_at must be after drop_at")
    c = constants()
    return Schedule(
        drop_at=drop,
        competition_start_at=start,
        competition_end_at=start + timedelta(days=c["competition_days"]),
        comp_slots=c["comp_slots"],
        activation_window_hours=int((start - drop).total_seconds() // 3600),
        competition_days=c["competition_days"],
    )


def phase(s: Schedule, now: datetime | None = None) -> Phase:
    now = now or datetime.now(timezone.utc)
    if now < s.drop_at:
        return "waitlist"
    if now < s.competition_start_at:
        return "activation"
    if now < s.competition_end_at:
        return "live"
    return "ended"


def pot_cents(entrants: int) -> int:
    c = constants()
    return c["pot_front_cents"] + c["pot_per_entrant_cents"] * max(entrants, 0)
```

- [ ] **Step 4: Delete the settings, the CDK env, and fix every test that patched them**

- `core/config.py`: delete the six `LAUNCH_*` settings (keep `STRIPE_FREE_MONTH_COUPON_ID`).
- `service-stack.ts`: delete the `LAUNCH_*` env entries (keep `STRIPE_FREE_MONTH_COUPON_ID: ""`); `service-stack.test.ts`: the launch env test now asserts ONLY `STRIPE_FREE_MONTH_COUPON_ID` is present and that no `LAUNCH_` name appears (`Match.not(Match.arrayWith([Match.objectLike({ Name: Match.stringLikeRegexp("^LAUNCH_") })]))`).
- Every backend test that did `patch("core.services.launch_phase.settings.LAUNCH_DROP_AT", …)` or `patch.object(rev.launch_phase, "schedule", …)`: replace the settings patch with a fixture that patches `launch_phase._FILE` to a configured/unconfigured file dict AND `launch_phase.settings.ENVIRONMENT` to `"dev"`. Add to `tests/conftest.py`:

```python
@pytest.fixture
def launch_schedule():
    """Patch launch-schedule.json in memory. Call with (drop_at, start_at) datetimes or None to turn the feature off."""
    from contextlib import contextmanager
    from datetime import timedelta
    from unittest.mock import patch

    from core.services import launch_phase

    @contextmanager
    def _set(drop_at=None, start_at=None, *, comp_slots=220, competition_days=30):
        env = {"drop_at": drop_at.isoformat() if drop_at else None,
               "competition_start_at": (start_at or (drop_at + timedelta(hours=72) if drop_at else None)).isoformat() if drop_at else None}
        f = {"competition_days": competition_days, "comp_slots": comp_slots, "pot_front_cents": 100_000,
             "pot_per_entrant_cents": 1_000, "dev": env, "prod": {"drop_at": None, "competition_start_at": None}}
        with patch.object(launch_phase, "_FILE", f), patch.object(launch_phase.settings, "ENVIRONMENT", "dev"):
            yield

    return _set
```

and rewrite each test's `configured` fixture as `with launch_schedule(DROP): yield` (past drop = `datetime.now(timezone.utc) - timedelta(days=1)`; future = `+10 days`). Tests that built `Schedule(...)` positionally keep working (the dataclass is unchanged).

- [ ] **Step 5: Frontend import + test**

```ts
// apps/frontend/src/lib/launchSchedule.ts
import schedule from "../../../../launch-schedule.json";

export interface LaunchScheduleEnv { drop_at: string | null; competition_start_at: string | null }
export interface LaunchScheduleFile {
  competition_days: number;
  comp_slots: number;
  pot_front_cents: number;
  pot_per_entrant_cents: number;
  dev: LaunchScheduleEnv;
  prod: LaunchScheduleEnv;
}

/** The one schedule file, importable at build time for static copy (dates, pot constants). Live phase still comes from GET /launch. */
export const LAUNCH_SCHEDULE = schedule as unknown as LaunchScheduleFile;

export function launchDatesFor(env: "dev" | "prod"): LaunchScheduleEnv {
  return LAUNCH_SCHEDULE[env];
}
```

```ts
// apps/frontend/src/lib/__tests__/launchSchedule.test.ts
import { describe, expect, it } from "vitest";
import { LAUNCH_SCHEDULE, launchDatesFor } from "../launchSchedule";

describe("launch-schedule.json", () => {
  it("has the pot constants and both env keys", () => {
    expect(LAUNCH_SCHEDULE.pot_front_cents).toBe(100000);
    expect(LAUNCH_SCHEDULE.pot_per_entrant_cents).toBe(1000);
    expect(Object.keys(launchDatesFor("dev"))).toEqual(["drop_at", "competition_start_at"]);
    expect(Object.keys(launchDatesFor("prod"))).toEqual(["drop_at", "competition_start_at"]);
  });
  it("orders competition_start_at after drop_at wherever dates are set", () => {
    for (const env of ["dev", "prod"] as const) {
      const { drop_at, competition_start_at } = launchDatesFor(env);
      if (drop_at && competition_start_at) expect(Date.parse(competition_start_at)).toBeGreaterThan(Date.parse(drop_at));
    }
  });
});
```

If `tsc` rejects the JSON import, add `"resolveJsonModule": true` is already on in Next's tsconfig; if the relative path is refused by Turbopack at `pnpm build` time, move the import into `next.config.ts`-free territory by re-exporting from `packages/shared` instead — try the direct import first and note the outcome.

- [ ] **Step 6: Verify + commit**

Run the backend scope (`-k "launch or stripe_webhook or lifespan or ops"`), the frontend `launchSchedule` test + tsc, and the infra vitest + synth; `git status --short .github/workflows/deploy.yml` must be empty.

```bash
git add launch-schedule.json apps/backend apps/infra apps/frontend/src/lib/launchSchedule.ts apps/frontend/src/lib/__tests__/launchSchedule.test.ts
git commit -m "feat(launch): one schedule file — launch-schedule.json replaces every LAUNCH_* env var"
```

---

### Task 2: Pre-drop hold — `launch_pending` gate before the trial clock

**Files:**
- Create: `apps/backend/core/services/launch_hold.py`
- Modify: `apps/backend/core/services/provision_gate.py` (`_evaluate_provision_gate_inner` first check), `core/services/user_provision.py` (`ensure_user_ready` first statement), `routers/container.py` (the "no container row" branch, before `ensure_personal_trial_default`)
- Test: `apps/backend/tests/unit/services/test_launch_hold.py`, extend `tests/unit/routers/test_container_status.py` (one class)

**Interfaces:**
- Produces: `launch_hold.pre_drop_gate(owner_id: str, *, owner_role: str = "admin", now=None) -> Gate | None` — returns a `Gate(code="launch_pending", title="You're on the list", message="Doors open <Mon Sep 18, 12:00 AM ET-style date>. Sign back in then and Friday runs your idea.", action_label="See your spot", action_href="/launch", action_admin_only=False, owner_role=owner_role)` when `launch_phase.schedule()` exists, `now < drop_at`, and `container_repo.get_by_owner_id(owner_id)` is None; else `None`. Never raises (a repo error ⇒ `None`, logged).
- `provision_gate._evaluate_provision_gate_inner`: FIRST statement `if (g := await launch_hold.pre_drop_gate(owner_id, owner_role=…)) is not None: return g`.
- `user_provision.ensure_user_ready`: FIRST statement (before `ensure_personal_trial_default`): `if await launch_hold.pre_drop_gate(owner_id) is not None: return`.
- `routers/container.py` no-container branch: call `launch_hold.pre_drop_gate(owner_id, owner_role=…)` BEFORE `ensure_personal_trial_default`; if a gate → `raise HTTPException(402, gate.to_payload())`.

- [ ] **Step 1: Tests**

```python
# tests/unit/services/test_launch_hold.py
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from core.services import launch_hold

NOW = datetime.now(timezone.utc)


async def test_holds_new_owner_before_drop(launch_schedule):
    with launch_schedule(NOW + timedelta(days=10)), patch.object(launch_hold.container_repo, "get_by_owner_id", AsyncMock(return_value=None)):
        g = await launch_hold.pre_drop_gate("user_1")
    assert g is not None and g.code == "launch_pending" and g.action_href == "/launch" and g.owner_role == "admin"


async def test_never_holds_an_owner_with_a_container(launch_schedule):
    with launch_schedule(NOW + timedelta(days=10)), patch.object(launch_hold.container_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "user_1"})):
        assert await launch_hold.pre_drop_gate("user_1") is None


async def test_lifts_at_the_drop(launch_schedule):
    with launch_schedule(NOW - timedelta(seconds=1)), patch.object(launch_hold.container_repo, "get_by_owner_id", AsyncMock(return_value=None)):
        assert await launch_hold.pre_drop_gate("user_1") is None


async def test_off_when_unconfigured_and_on_repo_error(launch_schedule):
    with launch_schedule(None), patch.object(launch_hold.container_repo, "get_by_owner_id", AsyncMock(return_value=None)):
        assert await launch_hold.pre_drop_gate("user_1") is None
    with launch_schedule(NOW + timedelta(days=10)), patch.object(launch_hold.container_repo, "get_by_owner_id", AsyncMock(side_effect=RuntimeError("ddb"))):
        assert await launch_hold.pre_drop_gate("user_1") is None
```

Add to `tests/unit/routers/test_container_status.py` (reuse its existing fixtures for a personal owner with no container row and no billing row):

```python
class TestLaunchPendingHold:
    async def test_pre_drop_owner_is_402_launch_pending_and_no_trial_row_is_created(self, async_client, launch_schedule):
        from datetime import datetime, timedelta, timezone
        with launch_schedule(datetime.now(timezone.utc) + timedelta(days=10)), \
             patch("routers.container.container_repo.get_by_owner_id", AsyncMock(return_value=None)), \
             patch("core.services.launch_hold.container_repo.get_by_owner_id", AsyncMock(return_value=None)), \
             patch("routers.container.ensure_personal_trial_default", AsyncMock()) as trial:
            r = await async_client.get("/api/v1/container/status")
        assert r.status_code == 402 and r.json()["detail"]["blocked"]["code"] == "launch_pending"
        trial.assert_not_awaited()
```

(adapt the patch targets to the names `routers/container.py` actually imports — read the file; the load-bearing assertions are the 402 code and that the trial default was never awaited.) Also add to `tests/unit/services/` a test that `ensure_user_ready` returns before `ensure_personal_trial_default` when held (patch `core.services.user_provision.launch_hold.pre_drop_gate` to return a Gate and `ensure_personal_trial_default` to an AsyncMock; assert not awaited).

- [ ] **Step 2: Implement**

```python
# core/services/launch_hold.py
"""Pre-drop hold: a signed-up account is kept container-less (and trial-clock-less)
until the Launch Competition's drop instant. Existing owners (container row present)
are never held. Evaluated BEFORE ensure_personal_trial_default in every door so the
3-day free trial starts on the first sign-in after the drop, not at sign-up."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.repositories import container_repo
from core.services import launch_phase
from core.services.provision_gate import Gate

logger = logging.getLogger(__name__)
CODE = "launch_pending"
_ET = ZoneInfo("America/New_York")


def _doors_open_copy(drop_at: datetime) -> str:
    local = drop_at.astimezone(_ET)
    return f"Doors open {local.strftime('%A, %B %-d')} at {local.strftime('%-I:%M %p')} ET. Sign back in then and Friday runs your idea."


async def pre_drop_gate(owner_id: str, *, owner_role: str = "admin", now: datetime | None = None) -> Gate | None:
    s = launch_phase.schedule()
    if s is None:
        return None
    now = now or datetime.now(timezone.utc)
    if now >= s.drop_at:
        return None
    try:
        if await container_repo.get_by_owner_id(owner_id) is not None:
            return None
    except Exception:  # noqa: BLE001 — a read failure must not block provisioning
        logger.warning("launch_hold: container read failed for %s; not holding", owner_id, exc_info=True)
        return None
    return Gate(
        code=CODE,
        title="You're on the list",
        message=_doors_open_copy(s.drop_at),
        action_label="See your spot",
        action_href="/launch",
        action_admin_only=False,
        owner_role=owner_role,
    )
```

`provision_gate.py` imports `launch_hold` lazily inside `_evaluate_provision_gate_inner` (to avoid the circular import: `launch_hold` imports `Gate` from `provision_gate`). Same lazy import in `user_provision.py` and `routers/container.py`.

- [ ] **Step 3: Verify + commit** — backend scope incl. `provision_gate or container_status or user_provision`.

```bash
git commit -am "feat(launch): pre-drop hold — held accounts have no container and no trial clock until the drop"
```

---

### Task 3: Sign-up is the waitlist — webhook join, `POST /launch/waitlist` removed, public counters

**Files:**
- Modify: `routers/webhooks.py` (`user.created`), `routers/launch.py` (remove `join_waitlist`; reshape `launch_info`), `schemas/launch.py` (remove `WaitlistJoinRequest/Response`; `LaunchInfo` fields), `core/repositories/launch_waitlist_repo.py` (`mark_pot_counted`, `count_pot_counted`)
- Test: `tests/unit/routers/test_webhooks*.py` (add one test beside the existing `user.created` tests), `tests/unit/routers/test_launch_public.py` (rewrite), `tests/unit/repositories/test_launch_waitlist_repo.py` (two tests)

**Interfaces:**
- `user.created` handler: after `user_repo.put`, `if launch_phase.schedule() is not None: await launch_waitlist_repo.join(email, referred_by=None, source="signup")` inside its own try/except (best-effort; never fails the webhook; skipped when no email).
- `launch_waitlist_repo.mark_pot_counted(email) -> bool` (CAS on `pot_counted_at`), `count_pot_counted() -> int` (paginated scan with `FilterExpression=attribute_exists(pot_counted_at)`, `Select=COUNT`).
- `GET /launch` → `{phase, drop_at, competition_start_at, competition_end_at, signups, competing, on_board, pot_cents, comp_slots, pot_per_entrant_cents}` where `signups = launch_waitlist_repo.count()`, `competing = launch_entries_repo.count()`, `on_board = launch_entries_repo.count_with_key()` (Task 4 adds it — until then use `count()` and let Task 4 switch), `pot_cents = launch_phase.pot_cents(signups)`.
- `LaunchInfo` schema updated accordingly; the 20s response cache stays.

- [ ] Steps: tests first (webhook join called with the extracted email and `source="signup"`; not called when schedule is None; `GET /launch` shape with `signups`/`competing`/`on_board`; `POST /launch/waitlist` now 404/405), then implement, then commit `feat(launch): sign-up is the waitlist — webhook join, public counters, email form endpoint removed`.

---

### Task 4: Join vs Connect Stripe; funded pot

**Files:**
- Modify: `core/repositories/launch_entries_repo.py` (`join`, `set_key_connected`, `count_with_key`; `upsert` removed), `core/services/launch_service.py` (`join`, `connect_stripe`, `can_join` renamed from `can_enter`, `rank` filters `has_key`), `core/services/launch_revenue.py` (`refresh_all` skips entries without `has_key`), `routers/launch.py` (`POST /join`, `POST /connect-stripe`; `POST /enter` removed), `schemas/launch.py` (`JoinRequest{display_name, public}`, `ConnectStripeRequest{stripe_key}`), `core/services/launch_referral.py` (`on_invoice_paid` also calls `launch_waitlist_repo.mark_pot_counted(payer_email)` — first invoice of ANY amount — before the amount gate), `core/services/ops/launch.py` (`funded_pot_cents`, `public_pot_cents`, `competing`, `on_board`, `signups`), `routers/ops.py` (the DELETE stays)
- Tests: rename/rewrite `test_launch_service_enter.py` → `test_launch_service_join.py`, `test_launch_enter.py` → `test_launch_join.py`; extend entries repo, referral, revenue-sweep, ops tests.

**Interfaces:**
- `launch_entries_repo.join(owner_id, *, display_name, public, waitlist_position) -> dict` (put-if-absent; `has_key=False`, `baseline_cents=0`, `window_cents=0`, `delta_cents=0`, `entered_at`); `set_key_connected(owner_id, *, currency, baseline_cents, baseline_at) -> None` (sets `has_key=True`); `count_with_key() -> int`.
- `launch_service.can_join(*, s, billing, onboarding, now=None) -> (bool, reason)` — same matrix as `can_enter`; `join(auth, s, *, display_name, public) -> entry`; `connect_stripe(auth, s, *, stripe_key) -> entry` — requires an existing entry (`no_entry` reason otherwise), validates the key, computes the baseline over `[start−30d, start)` (finalization in the refresh path unchanged), stores the key, `set_key_connected`.
- `rank(entries)` considers `has_key` only; `leaderboard()` returns `{"competing": n_all, "on_board": n_key, "rows": …}`.
- `refresh_all` skips `not entry.get("has_key")`.
- Routes: `POST /launch/join {display_name, public}` (auth; org-admin-only in org context), `POST /launch/connect-stripe {stripe_key}` (auth; rate-limited; org-admin-only), `GET /launch/leaderboard` unchanged path.
- Funded pot: in `launch_referral.on_invoice_paid`, right after resolving `payer_email` and BEFORE the `amount_paid > 0` gate, `await launch_waitlist_repo.mark_pot_counted(payer_email)` (first invoice of any amount — redemption counts; CAS makes duplicates inert). `ops/launch.compute` returns `public_pot_cents = pot_cents(signups)`, `funded_pot_cents = pot_cents(count_pot_counted())`, `signups`, `competing`, `on_board`; the economics `paid`/`comp_eligible` split now runs over ENTRIES (competing), unchanged formula.
- `REASON_COPY` on the frontend (Task 5) gains `no_entry`.

- [ ] Steps: tests first (repo: join put-if-absent, second join returns the existing row untouched, set_key_connected flips has_key, count_with_key; service: can_join matrix, join creates without touching Stripe, connect_stripe refuses without an entry, connect stores key only after baseline; referral: `mark_pot_counted` awaited even for a $0 invoice and before the amount gate; sweep: entries without has_key skipped; ops: two pots + three counts), then implement, then commit `feat(launch): join (activation + subscription) vs connect Stripe (board); funded pot from the first invoice`.

---

### Task 5: Frontend — hero back to the idea funnel, held card in the app, Join/Connect on `/launch`

**Files:**
- Revert to `origin/main`: `apps/frontend/src/components/landing/HeroFunnel.tsx`, `apps/frontend/src/components/landing/__tests__/HeroFunnel.test.tsx` (`git checkout origin/main -- <paths>`).
- Delete: `components/launch/WaitlistForm.tsx`, `components/launch/useLaunchPhase.ts`, `components/launch/__tests__/WaitlistForm.test.tsx`.
- Rename: `components/launch/EnterForm.tsx` → `ConnectStripeForm.tsx` (+ test); create `components/launch/JoinForm.tsx` (+ test), `components/chat/LaunchHeldCard.tsx` (+ test).
- Modify: `lib/launch.ts` (types: `LaunchInfo{signups, competing, on_board, pot_cents, comp_slots, pot_per_entrant_cents, …}`, `LaunchMe{…, joined: boolean, has_key: boolean, can_join, reason, idea: string | null}`; `fetchLeaderboard` shape `{competing, on_board, rows}`; drop `joinWaitlist`), `components/launch/LaunchPage.tsx`, `LaunchHero.tsx`, `MyLaunchCard.tsx`, `Leaderboard.tsx`, `components/chat/ProvisioningStepper.tsx` (render `<LaunchHeldCard blocked={blocked} />` when `blocked.code === "launch_pending"`, beside the existing `team_billing_required` / trial special-cases), `lib/analytics.ts` (`launch_joined`, `launch_stripe_connected` replace `launch_entered`; keep `launch_referral_copied`; delete `launch_waitlist_joined`).
- Backend touch: `launch_service.me()` adds `idea` (from `paperclip_repo.get_pending_brief(owner_id)` → its `input`/text field, first 140 chars, or `None`), `joined`, `has_key`, `can_join`, `reason`; keep `waitlist{position, referral_code, in_comp_window, referral_link, referrals_signed_up, referrals_paid, free_months_banked}`, `activated`, `activated_at`, `phase`. (Read `paperclip_repo.get_pending_brief` to get the field name; add one test.)

**Behaviour:**
- `LaunchHero` stats: public pot (`formatMoney(info.pot_cents)`), `signups` ("signed up") during waitlist; once past the drop add `competing` ("competing") and `on_board` ("on the board"); countdown to the drop, then to the start, then time left.
- `LaunchHeldCard` (in-app, pre-drop): "You're #N on the list" (from `GET /launch/me`), countdown to `drop_at` (from `GET /launch`), "Your idea: …" (`me.idea`, or "Add your idea on drop day" when null), the referral link (`ReferralLink`), and the fine print "Doors open <date>. Sign back in then and Friday runs your idea." Uses `useApi` for `/launch/me` and plain fetch for `/launch`. Rendered by `ProvisioningStepper` for `blocked.code === "launch_pending"`; nothing else on the stepper changes.
- `MyLaunchCard` (on `/launch`): pre-drop → position + countdown + idea + referral link (same content as the held card, reuse a shared `LaunchStatusLines` piece or duplicate the 10 lines — prefer a shared piece in `components/launch/LaunchStatusLines.tsx`); post-drop, not activated → "Finish onboarding — Friday's first run has to complete."; activated + not joined → `<JoinForm />` when `can_join`, else the `BLOCKED_COPY` reason (`no_subscription` copy: "Redeem your code or subscribe to join — the free month is the competition month."); joined + no key → "You're in. Connect a read-only Stripe key to get on the board." + `<ConnectStripeForm />`; joined + key → the lift line (existing); `refresh_error` → the error + `<ConnectStripeForm />` again. Referral attribution: keep the once-guard, but remove `localStorage[LAUNCH_REF_KEY]` ONLY on a resolved response (`attributed` true OR false) — never on a thrown error (CodeRabbit).
- `LaunchPage`: waitlist phase → hero + "Sign up" CTA (`/sign-up`) + "Already signed up? Sign in" + rules; activation → sign-up CTA + rules; live/ended → leaderboard. `RULES` render `info.comp_slots` and `formatMoney(info.pot_per_entrant_cents)` instead of literals (CodeRabbit).
- `Leaderboard` header shows "N on the board · M competing".
- Palette + fonts: `launch.css` uses `var(--font-serif)` / `var(--font-mono)`; no palette classes anywhere in `components/launch`, `components/chat/LaunchHeldCard.tsx`, `app/launch`.

- [ ] Steps: tests first (LaunchPage per phase incl. sign-up CTA in waitlist; MyLaunchCard states: pre-drop / not activated / can_join → JoinForm / joined-no-key → ConnectStripeForm / on board; JoinForm posts `{display_name, public}` to `/launch/join`; ConnectStripeForm client-rejects `sk_`, never leaks the key to analytics, maps `no_entry`; LaunchHeldCard renders position/idea/referral link; ProvisioningStepper renders the held card on `launch_pending` — add one test in `components/chat/__tests__/` mirroring how the existing trial-cap card is tested), implement, `node scripts/brand-tokens-scan.mjs src` prints nothing, frontend verification, commit `feat(launch): hero back to the idea funnel; held card; join vs connect on /launch`.

---

### Task 6: Admin on the brand tokens + two pots; CodeRabbit's valid findings

**Files:**
- Modify: `app/admin/launch/page.tsx`, `app/admin/launch/LaunchAdminClient.tsx`, `app/admin/_lib/api.ts` (`LaunchOpsData{…, signups, competing, on_board, public_pot_cents, funded_pot_cents, pot_counted}`), its test; `core/services/launch_revenue.py`, `routers/launch.py`, `routers/ops.py`, `core/services/ops/launch.py`, `tests/unit/services/test_launch_service_join.py`, `docs/superpowers/specs/2026-08-28-launch-competition-design.md` (one regex pipe).

**Changes (each with a covering test):**
1. **Brand tokens (CI failure):** replace every `zinc-*` class in `app/admin/launch/*` with the console's semantic classes (`text-foreground`, `text-muted-foreground`, `hover:bg-accent`, `hover:text-foreground`, `border-border`, `bg-card`); `MetricStat` tones already map to tokens. `node scripts/brand-tokens-scan.mjs src` must print nothing.
2. **Two pots on the sheet:** tiles "public pot" (`public_pot_cents`, hint "N signed up × $10 + $1,000") and "funded pot" (`funded_pot_cents`, hint "N first invoices landed"); counts "signed up / competing / on the board"; economics unchanged. CSV export unchanged in columns.
3. **CSV formula neutralization (CodeRabbit):** in `csvEscape`, prefix values starting with `=`, `+`, `-`, `@` with `'` before quoting; tests for each prefix.
4. **Currency mismatch (CodeRabbit):** in `launch_revenue.refresh_entry`, when the (finalized) baseline currency differs from the window currency → `_record(set_refresh_error(owner_id, "mixed_currency"))` and return before `set_revenue`; regression test EUR baseline / USD window.
5. **Cache invalidation after refresh (CodeRabbit):** `routers/launch.py` exposes `clear_response_cache()`; `launch_revenue.maybe_revalidate`'s `_run` calls it (lazy import) after `refresh_all` completes; `ops/launch.py` exposes `clear_cache()` and `routers/ops.py` uses it instead of touching `_cache` (nitpick). Test: after a completed `_run`, the next `GET /launch/leaderboard` recomputes.
6. **Delete the key before the row (CodeRabbit):** `DELETE /ops/launch/entries/{owner_id}` calls `launch_revenue.delete_key` first, then `launch_entries_repo.delete`; test order via a shared call log.
7. **Nitpicks:** `test_launch_service_join.py` mixed-currency refusal asserts `store` not awaited; `ops/launch.py` billing fan-out under the same `Semaphore(8)`; spec line 113 escapes `\|`.

- [ ] Steps: tests first, implement, frontend + backend verification, commit `fix(launch): admin on the brand tokens, two pots, CodeRabbit findings`.

---

### Task 7: Docs, verification, push to the PR

**Files:**
- Modify: `docs/superpowers/specs/2026-08-28-launch-competition-design.md` (decisions table: sign-ups OPEN throughout — no Clerk Restricted; waitlist = Clerk account + pending brief + row from the webhook; hold; join vs connect; two pots; dates in `launch-schedule.json`, drop Thu midnight ET / start Sun midnight ET; runbook: week-0 step becomes "edit `launch-schedule.json` (dev first), merge, verify on dev, promote"; remove `LAUNCH_*` mentions), `docs/superpowers/plans/2026-08-28-launch-competition.md` (Global Constraints: point at this addendum for the superseded items), `apps/infra/openclaw/skills/lightbulb/SKILL.md` (the block: waitlist = sign up on the site with your idea; you are held until the drop; join after Friday's first run; Stripe optional for the board; referral links `/?ref=`).

- [ ] **Step 1:** docs edits, commit `docs(launch): reconcile spec, base plan and skill anchor with the addendum`.
- [ ] **Step 2:** the three verification commands from Global Constraints, full; paste tails into the report. The backend's ONE known pre-existing failure on `main` (`test_cloudwatch_url`) is outside the `-k` scope and not ours.
- [ ] **Step 3 (controller only):** rebase on `origin/main`, push to `origin/feat/launch-competition` (the PR branch — the owner authorized the PR and asked for CodeRabbit's review; pushing follow-ups to it is within that), update the PR body's "What" section for the new model, watch checks by name, relay CodeRabbit's re-review.

## Self-review

- Coverage: schedule file (T1) ↔ owner's "one var" ask; hold (T2) ↔ "held until drop, no trial burn"; webhook join (T3) ↔ "emails in Clerk, position = sign-up order"; join/connect (T4) ↔ "activation first, then join; Stripe optional"; two pots (T3/T4/T6) ↔ "public grows at sign-up, funded on first invoice"; hero revert + held card + `/launch` states (T5); CI brand tokens + CodeRabbit (T6); docs + push (T7).
- Names used across tasks: `launch_phase.schedule/constants/pot_cents/load_schedule_file/_FILE/env_key`; `launch_hold.pre_drop_gate/CODE`; `launch_waitlist_repo.join/mark_pot_counted/count_pot_counted`; `launch_entries_repo.join/set_key_connected/count_with_key`; `launch_service.can_join/join/connect_stripe/rank/leaderboard/me`; routes `/launch`, `/launch/me`, `/launch/referral`, `/launch/join`, `/launch/connect-stripe`, `/launch/leaderboard`; frontend `LaunchInfo{signups, competing, on_board, pot_cents, comp_slots, pot_per_entrant_cents}`, `LaunchMe{joined, has_key, can_join, reason, idea}`, `JoinForm`, `ConnectStripeForm`, `LaunchHeldCard`, `LaunchStatusLines`; analytics `launch_joined`, `launch_stripe_connected`, `launch_referral_copied`.
