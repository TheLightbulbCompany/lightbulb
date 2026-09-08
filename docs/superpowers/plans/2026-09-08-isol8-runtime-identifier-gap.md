# Isol8 Runtime-Identifier Gap — Audit and Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the genuinely-safe part of the `isol8` runtime-identifier gap left by the
2026-08-31 rename, and record — with evidence — exactly why the rest must not be swept.

**Architecture:** This is an audit first and a code change second. Every identifier below was
individually traced to its producers and consumers. The output is a three-way split:
**fix now** (no cross-process contract), **fix later with a shim** (a live contract, recipe
included), and **never** (deliberate residue, or a name that correctly describes residue).

**Tech Stack:** Python/FastAPI backend, AWS CDK (TypeScript), the extended OpenClaw Docker
image, the Paperclip fork image, Next.js frontend.

**Spec:** none — this document is both the audit and the plan. The originating brief is the
`CLAUDE.md` section *"RUNTIME IDENTIFIERS — RETIRED 2026-08-31; do not reintroduce"*, which
lists nine renames as complete and omits everything below.

## Global Constraints

- **A naive `s/isol8/lightbulb/` sweep breaks the fleet.** `CLAUDE.md` says so explicitly.
  Every change here is individually justified or it is not made.
- **Deliberate residue is off-limits**, each for a stated reason in `CLAUDE.md`: the
  `isol8.awsapps.com` SSO portal URL and the `isol8-admin` CLI profile; the Expo slug
  `isol8-mobile`; `-isol8.N` image tags and `isol8/*` fork branches; historical issue/PR
  references; `BOOTSTRAP_EMAILS`; `isol8` reserved in the slug/handle allowlists; and the EFS
  lock sidecars `.isol8-guard` / `.isol8-protocol`.
- **The `isol8.co` mail domain is human-gated** and stays untouched.
- Backend checks: `cd apps/backend && uv run pytest <paths> -q` and `uv run ruff check .`
- Infra checks: `cd apps/infra && npm test`. Any change under `apps/infra/lib/` must run
  `npx cdk synth` and commit a regenerated `.github/workflows/deploy.yml` if it changed.
- **Never merge, push to main, or dispatch a deploy** from this work.

---

## Part 1 — Audit

### 1.1 Correcting the brief

Three claims in the originating brief did not survive verification:

| brief claim | verified reality |
|---|---|
| `isol8.action` — part of a ~37-occurrence event-string group | **Does not exist.** `git grep 'isol8\.action'` returns nothing. The real counts are `isol8.hire_suggestion` 19 and `isol8.notifications` 11 — 30 total, not 37. |
| `X-Isol8-Public-Host` spans 4 source files | Correct, **and the code is internally consistent** — `api-stack.ts` sets exactly the name `main.py` reads. Nothing is broken. What *is* wrong is `CLAUDE.md` + `AGENTS.md`, which document a header named `X-Lightbulb-Public-Host` that **exists nowhere in the codebase**. |
| the test fixture `isol8-dev-openclaw-replicas` is "possibly easy" | It is easy but **worthless** — see §1.5. The same stale bucket name in two *runbooks* and one *code comment* is a real defect and is fixed here. |

### 1.2 Identifiers the brief missed entirely

The gap is larger than fifteen. All of these are the same class and appear in neither
`CLAUDE.md` table:

| identifier | where | class |
|---|---|---|
| `_SEVERITY_RE = r"^isol8-[^-]+-(P\|W)\d+-"` | `core/services/ops/alarms.py:15` | **live bug** — see §1.3 |
| `/usr/local/bin/isol8-local-sqlite-boot.sh` | `Dockerfile:916`, `container-stack.ts:850,915` | baked path |
| `/usr/local/bin/isol8-local-sqlite-app.sh` | `Dockerfile:917` | baked path |
| `/etc/isol8-litestream.yml` | `Dockerfile:918` | baked path |
| `/usr/local/bin/isol8-warp-boot.sh` | `Dockerfile:487`, `container-stack.ts:817` | baked path |
| `/opt/isol8/chrome-extensions/` | `Dockerfile:839,841` | baked path |
| `PAPERCLIP_ISOL8_NOTIFY_URL` / `_TOKEN` | `paperclip-stack.ts:474,535` | Paperclip fork image env |
| `.isol8-seed-fingerprint` | `atlas_seed/__init__.py:160` | EFS-resident sidecar |
| `.openclaw/isol8-setup-seed.json` | `friday_identity.py:79` | EFS-resident sidecar |
| `ISOL8_SYSTEM_SELLER_ID = "__isol8__"` | `scripts/ingest_companies_registry.py:38` | **stored DynamoDB value** |
| `"isol8 top-up …"` / `"isol8 checkout top-up …"` | `core/billing/metronome_funding.py:93,185` | **money-path string, see §1.4** |
| `ISOL8_THEME` | `frontend/…/WorkspaceShell.tsx:65` | local const, no contract |
| `ISOL8_REPO` | `scripts/switch-ci-runner.sh:27` | local shell override |
| `_ISOL8_PATCH_RE` | `core/openclaw_version.py:163` | parses protected residue |
| `ISOL8_CURATOR_EMAIL = "admin@isol8.co"` | `core/constants.py:13` | human-gated mail |

### 1.3 The one live bug

`core/services/ops/alarms.py` classifies operator-strip alarm severity by matching the alarm
name against:

```python
_SEVERITY_RE = re.compile(r"^isol8-[^-]+-(P|W)\d+-")
```

Every CloudWatch alarm in both environments is named by `resourcePrefix`, which is
`` `lightbulb-${env}-` `` (`apps/infra/lib/table-names.ts:36`). All 14 `alarmName` sites in
`observability-stack.ts` go through it; there is no second naming site. So the regex matches
**nothing in production**, `_severity()` returns `"unknown"` for every alarm, and the operator
attention strip has silently shown no critical/warning distinction since the resource rename.

`tests/unit/core/services/ops/test_alarms.py` is green — because every fixture name in it is
an `isol8-*` name. The test encodes the retired convention and therefore *masks* the defect.
This is the reason the bug outlived the rename.

**This is the only identifier in the whole audit that is both broken and safe to fix
unilaterally:** the alarm name is read from the CloudWatch API at request time, there is no
second process to keep in step, and no stored value changes.

### 1.4 The best illustration of why a sweep is dangerous

`metronome_funding.py` builds a Metronome commit description:

```python
description = f"isol8 top-up owner={owner_id} amount_cents={amount_cents}"
```

It reads as pure branding. It is not. The comment directly above it explains:

> The description carries NO timestamp: Metronome's idempotent replay requires the retried
> body to be identical (same key + different body → 409).

The description is part of the **request body under an idempotency key**. Rename it, and any
top-up retry whose first attempt was made by the pre-rename build comes back **409** — a
real customer failing to get real credits, on the money path, only under retry, only across
the deploy boundary. A `sed` would have changed it without a second thought.

`ISOL8_SYSTEM_SELLER_ID = "__isol8__"` is the same trap in a different shape: the constant
*name* is free to change, but the *value* is written to marketplace listing rows in DynamoDB.
Renaming the value is a data migration, and renaming only the name makes the identifier lie
about the data.

Two further names are best left exactly as they are, because they **correctly describe
protected residue**:

- `_ISOL8_PATCH_RE` parses the `-isol8.N` image-tag suffix, which `CLAUDE.md` protects as a
  rollback ref. The pattern must keep matching `-isol8.`; the name accurately says so.
- `ISOL8_CURATOR_EMAIL` holds `admin@isol8.co`, on the human-gated mail retirement.

Renaming either would make the code *less* honest. This is the concrete form of the rule that
a sweep destroys meaning, not just correctness.

### 1.5 Verified harmless — deliberately not changed

`isol8-dev-openclaw-replicas` appears in four backend test files as a `monkeypatch` value for
`settings.LITESTREAM_BUCKET`. It is an arbitrary string; the tests assert only that it is
threaded through unchanged. Renaming it is ~12 lines of churn that changes no behaviour and
buys no clarity that the surrounding code doesn't already give. **Left alone on purpose.**

The same string in a *comment* and in two *runbooks* is a different matter — those are read as
instructions, and they are wrong. See Tasks 2 and 3.

### 1.6 The easy/hard split

**Fix now — no cross-process contract, no shim, no deploy ordering (Tasks 1-5):**

| identifier | why it is safe |
|---|---|
| `alarms.py` `_SEVERITY_RE` | backend-only read of a live API value; **fixes a live bug** |
| `service-stack.ts:342` comment | a comment that contradicts the ARN 8 lines below it |
| `s3://isol8-{env}-openclaw-replicas` in 2 runbooks | operator commands against a bucket that does not exist |
| `X-Lightbulb-Public-Host` in `CLAUDE.md`/`AGENTS.md` | documents a header that exists nowhere |
| `ISOL8_THEME` | module-local `const`; its *value* already says `lightbulb` |

**Fix later, needs a shim or coordinated deploy (Part 3 — not implemented here):**

| identifier | what breaks on a naive rename | shim needed |
|---|---|---|
| `X-Isol8-Public-Host` | `api-stack.ts` and the backend are separate stacks in one pipeline run, and the backend rolls at `maxHealthyPercent: 200`. For minutes, one side sends the new name and the other reads the old → every `company[-env]` request misses the proxy rewrite. | **Yes** — accept-both, exact precedent in §3.1 |
| the 12+ `ISOL8_*` container env vars and 5 baked paths | Three-sided: the image bakes them, `ecs_manager` injects them, **and per-owner Atlas seed files already on EFS read them**. `container-stack.ts:850` is a hard `exit 1` guard on the boot-script path, so shipping CDK before the image **fails every container boot**. | Yes, plus a fleet reroll |
| `isol8_paperclip` cookie | logs out every live session | Yes — verifier accepts both for a window |
| `isol8.hire_suggestion` | emitted by the baked `lightbulb-marketplace` plugin, consumed by `parseHireSuggestion`, which returns `null` on mismatch → HireCard silently degrades to a plain tool pill fleet-wide during the reroll | Yes — frontend accepts both |
| `isol8.notifications`, `PAPERCLIP_ISOL8_NOTIFY_*` | lives in the **Paperclip fork**; needs a fork change, a new image digest, a pin bump, and a per-tenant redeploy | Yes |
| `.isol8-seed-fingerprint`, `isol8-setup-seed.json` | EFS-resident per owner; same once-only-migration shape as the protected `.isol8-guard` | Yes |

**Never (adds to `CLAUDE.md`'s residue list):** `_ISOL8_PATCH_RE`, `ISOL8_SYSTEM_SELLER_ID`'s
value `"__isol8__"`, `ISOL8_CURATOR_EMAIL`, the `metronome_funding` descriptions, and the test
fixtures of §1.5.

---

## Part 2 — Implementation (the easy subset)

> **STATUS: Tasks 1-5 are DONE** — shipped on `fix/isol8-runtime-identifier-gap`
> (PR #2388), five independent commits. Part 3 is deliberately not implemented.
> Steps are left checked below as the record of what was run.

### File Structure

| file | responsibility | change |
|---|---|---|
| `apps/backend/core/services/ops/alarms.py` | alarm severity classification | fix the regex to the live naming convention |
| `apps/backend/tests/unit/core/services/ops/test_alarms.py` | pins that convention | re-point fixtures at real alarm names; keep one `isol8-*` case asserting `unknown` |
| `apps/infra/lib/stacks/service-stack.ts` | backend task role + env | correct one stale comment |
| `.agents/skills/dev-clean-slate-reset/SKILL.md` | dev wipe runbook | correct the S3 bucket in 2 places |
| `.agents/skills/prod-org-reprovision/SKILL.md` | prod reprovision runbook | correct the S3 bucket |
| `CLAUDE.md`, `AGENTS.md` | repo truth | state the real header name; record the gap |
| `apps/frontend/src/components/workspace/WorkspaceShell.tsx` | dock theme | rename a local const |

---

### Task 1: Fix the alarm-severity regex (live bug)

**Files:**
- Modify: `apps/backend/core/services/ops/alarms.py:13-15`
- Test: `apps/backend/tests/unit/core/services/ops/test_alarms.py:8-24`

**Interfaces:**
- Consumes: `resourcePrefix(env) === ``lightbulb-${env}-``` from `apps/infra/lib/table-names.ts:36` — the single source of every `alarmName` in `observability-stack.ts`.
- Produces: `alarms._severity(name) -> "critical" | "warning" | "unknown"`, unchanged signature.

- [x] **Step 1: Write the failing test** — replace the `@pytest.mark.parametrize` block at
  `test_alarms.py:8-24` with the real naming convention. Keep an `isol8-*` case, now asserting
  `"unknown"`, so the retired prefix can never quietly come back:

```python
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("lightbulb-dev-P9-alb-5xx-rate", "critical"),
        ("lightbulb-prod-P12-backend-liveness", "critical"),
        ("lightbulb-dev-W28-alb-unhealthy-hosts", "warning"),
        ("lightbulb-prod-W5-gateway-connection-drop", "warning"),
        ("lightbulb-dev-composite-rollup", "unknown"),  # no P/W token
        ("lightbulb-dev-Perf1-not-a-priority", "unknown"),  # P must be P<digits>-
        ("TargetTracking-service/foo", "unknown"),  # not one of ours
        # The retired prefix is NOT the convention any more: every alarm is
        # named by resourcePrefix() = `lightbulb-{env}-`. Pinned so a revert
        # to the isol8-era regex fails here.
        ("isol8-dev-P9-alb-5xx-rate", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_severity_parses_alarm_name_convention(name, expected):
    assert alarms._severity(name) == expected
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/core/services/ops/test_alarms.py -q`
Expected: FAIL — the four `lightbulb-*` cases return `"unknown"`, and the `isol8-*` case
returns `"critical"`. That failure *is* the production bug.

- [x] **Step 3: Write the minimal implementation** — `alarms.py:13-15`:

```python
# Alarm-name convention from the observability stack (resourcePrefix() in
# apps/infra/lib/table-names.ts): lightbulb-<env>-P<n>-… = paging/critical,
# lightbulb-<env>-W<n>-… = warning.
_SEVERITY_RE = re.compile(r"^lightbulb-[^-]+-(P|W)\d+-")
```

- [x] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/core/services/ops/test_alarms.py -q && uv run ruff check core/services/ops/alarms.py`
Expected: all tests PASS, ruff clean.

- [x] **Step 5: Commit**

```bash
git add apps/backend/core/services/ops/alarms.py apps/backend/tests/unit/core/services/ops/test_alarms.py
git commit -m "fix(ops): classify alarm severity by the live lightbulb- name prefix"
```

---

### Task 2: Correct the stale bucket name in the service-stack comment

**Files:**
- Modify: `apps/infra/lib/stacks/service-stack.ts:342`

**Interfaces:**
- Consumes: nothing. Comment-only; the synthesized template must not change.
- Produces: nothing.

The comment says the backend prefix-deletes `s3://isol8-{env}-openclaw-replicas/{owner}/…`,
while the IAM resource eight lines below is `` arn:aws:s3:::lightbulb-${env}-openclaw-replicas ``
and `LITESTREAM_BUCKET` at line 1367 is `` `lightbulb-${env}-openclaw-replicas` ``. The comment
is simply wrong about a live resource.

- [x] **Step 1: Make the edit** — change the one path in the comment:

```
    // s3://lightbulb-{env}-openclaw-replicas/{owner}/..., or the next boot's
```

- [x] **Step 2: Verify the synthesized template is unchanged**

Run: `cd apps/infra && npx cdk synth >/dev/null && git status --short .github/workflows/deploy.yml`
Expected: synth exits 0 and `deploy.yml` is **not** listed as modified. A comment cannot change
the template; if `deploy.yml` did change, something else is drifting — stop and investigate
rather than committing it alongside this.

- [x] **Step 3: Run the infra suite**

Run: `cd apps/infra && npm test`
Expected: PASS.

- [x] **Step 4: Commit**

```bash
git add apps/infra/lib/stacks/service-stack.ts
git commit -m "docs(infra): the litestream replica bucket is lightbulb-, not isol8-"
```

---

### Task 3: Fix the two runbooks that name a bucket that does not exist

**Files:**
- Modify: `.agents/skills/dev-clean-slate-reset/SKILL.md:57,65`
- Modify: `.agents/skills/prod-org-reprovision/SKILL.md:55`

**Interfaces:**
- Consumes: the same `lightbulb-{env}-openclaw-replicas` fact as Task 2.
- Produces: nothing.

Both runbooks hand an operator a literal command against `isol8-{env}-openclaw-replicas`. That
bucket does not exist, so the wipe silently does not happen and the next boot's litestream
restore resurrects the state the operator believed they had deleted.

- [x] **Step 1: Make the edits** — three occurrences, `isol8-` → `lightbulb-`:

```
# dev-clean-slate-reset/SKILL.md:57
SQLite replicates to `s3://lightbulb-dev-openclaw-replicas/{owner_id}/…` and the

# dev-clean-slate-reset/SKILL.md:65
aws s3 rm "s3://lightbulb-dev-openclaw-replicas/<owner_id>/" --recursive --profile isol8-admin

# prod-org-reprovision/SKILL.md:55
aws s3 rm "s3://lightbulb-prod-openclaw-replicas/$OWNER/" --recursive --profile isol8-admin
```

Note `--profile isol8-admin` is **unchanged** — it is protected residue (`CLAUDE.md`: the SSO
portal customization was spent on `isol8` and cannot be changed).

- [x] **Step 2: Verify no `isol8-*-openclaw-replicas` remains outside the test fixtures**

Run: `git grep -n 'isol8-[a-z]*-openclaw-replicas' -- . ':!apps/backend/tests'`
Expected: no output.

- [x] **Step 3: Commit**

```bash
git add .agents/skills/dev-clean-slate-reset/SKILL.md .agents/skills/prod-org-reprovision/SKILL.md
git commit -m "fix(runbooks): point the replica wipe at the real lightbulb- bucket"
```

---

### Task 4: Make CLAUDE.md and AGENTS.md tell the truth about the dispatch header

**Files:**
- Modify: `CLAUDE.md` (the `HostDispatcherMiddleware` mention, the data-flow diagram, and the
  `api-stack.ts` row of the stacks table)
- Modify: `AGENTS.md` (the same three places — it is the Codex-facing mirror)

**Interfaces:**
- Consumes: the verified fact that `git grep 'Lightbulb-Public-Host'` matches **only**
  `CLAUDE.md` and `AGENTS.md` — no source file emits or reads it.
- Produces: nothing.

Both files name the header `X-Lightbulb-Public-Host`. `api-stack.ts:207` sets
`overwrite:header.X-Isol8-Public-Host` and `main.py:174` reads `b"x-isol8-public-host"`. An
agent trusting the docs greps for a string that does not exist and concludes the dispatch is
gone. Replace the name with the real one and mark it as a known-open rename so it is not
mistaken for an oversight.

- [x] **Step 1: Replace the three mentions in each file** with `X-Isol8-Public-Host`.

- [x] **Step 2: Add the identifier to the residue discussion** — immediately after the
  "RUNTIME IDENTIFIERS — RETIRED 2026-08-31" table in `CLAUDE.md` (and the mirror in
  `AGENTS.md`):

```markdown
**NOT yet renamed — the same class, still open.** These were missed by the
2026-08-31 pass and are NOT residue; each needs a coordinated deploy or an
accept-both shim, so do not sweep them. Full audit + recipes:
`docs/superpowers/plans/2026-09-08-isol8-runtime-identifier-gap.md`.

| identifier | why it can't be swept |
|---|---|
| `X-Isol8-Public-Host` | API GW (`api-stack.ts`) and the backend are separate stacks in one pipeline run; a hard swap misroutes every `company[-env]` request during the roll. Precedent shim: `require_ws_internal_caller` in `routers/websocket_chat.py`. |
| the `ISOL8_*` container env vars + the baked `/usr/local/bin/isol8-*.sh`, `/etc/isol8-litestream.yml`, `/opt/isol8/` paths | three-sided (image, task def, and per-owner Atlas seed files already on EFS); `container-stack.ts` hard-`exit 1`s if the boot-script path is missing, so CDK-before-image fails every boot |
| `isol8_paperclip` | live session cookie — a hard swap logs everyone out |
| `isol8.hire_suggestion` | emitted by the baked marketplace plugin, consumed by `parseHireSuggestion`; mismatch silently degrades the HireCard fleet-wide |
| `isol8.notifications`, `PAPERCLIP_ISOL8_NOTIFY_*` | lives in the Paperclip fork image, not this repo |
| `.isol8-seed-fingerprint`, `.openclaw/isol8-setup-seed.json` | EFS-resident per owner, same shape as the protected `.isol8-guard` |

**Never rename these** — the name correctly describes protected residue, or the
value is stored data: `_ISOL8_PATCH_RE` (parses the `-isol8.N` rollback tag),
`ISOL8_SYSTEM_SELLER_ID`'s value `"__isol8__"` (written to marketplace rows),
`ISOL8_CURATOR_EMAIL` (human-gated mail), and the `"isol8 top-up …"` descriptions
in `metronome_funding.py` (part of an idempotent Metronome body — changing the
string 409s a retried top-up).
```

- [x] **Step 3: Verify the docs no longer claim a header that does not exist**

Run: `git grep -n 'Lightbulb-Public-Host'`
Expected: no output.

- [x] **Step 4: Commit**

```bash
git add CLAUDE.md AGENTS.md
git commit -m "docs: the dispatch header is X-Isol8-Public-Host; record the unrenamed set"
```

---

### Task 5: Rename the dock-theme const

**Files:**
- Modify: `apps/frontend/src/components/workspace/WorkspaceShell.tsx:65,407`

**Interfaces:**
- Consumes: nothing — `ISOL8_THEME` is a module-local `const`, not exported, not serialized.
- Produces: nothing. Its *value* already reads `name: "lightbulb"` and
  `dockview-theme-lightbulb`; only the identifier is stale.

- [x] **Step 1: Rename both occurrences** `ISOL8_THEME` → `LIGHTBULB_THEME` (declaration at
  line 65, use at line 407). No other file references it.

- [x] **Step 2: Typecheck and test**

Run: `cd apps/frontend && npx tsc --noEmit && pnpm test -- WorkspaceShell`
Expected: PASS.

- [x] **Step 3: Commit**

```bash
git add apps/frontend/src/components/workspace/WorkspaceShell.tsx
git commit -m "refactor(workspace): rename the ISOL8_THEME const to LIGHTBULB_THEME"
```

---

## Part 3 — Deferred work (NOT implemented here)

### 3.1 `X-Isol8-Public-Host` — the shim already exists in this repo

Do not invent a mechanism. `routers/websocket_chat.py:449-491` renamed
`x-isol8-ws-internal` → `x-lightbulb-ws-internal` with an accept-both dependency whose
docstring states the exact reason, and it is the template:

```python
def require_ws_internal_caller(
    x_lightbulb_ws_internal: Optional[str] = Header(None, alias="x-lightbulb-ws-internal"),
    x_isol8_ws_internal: Optional[str] = Header(None, alias="x-isol8-ws-internal"),
) -> None:
    ...
    presented = x_lightbulb_ws_internal or x_isol8_ws_internal
```

**Ordering (three PRs, each fully deployed before the next):**

1. **Backend accepts both.** In `main.py`'s `HostDispatcherMiddleware.__call__`, match
   `b"x-lightbulb-public-host"` *or* `b"x-isol8-public-host"` off the ASGI scope; same in
   `paperclip_proxy.py:1549`. Ship and promote. Nothing emits the new name yet, so this is
   inert.
2. **Infra emits the new name.** Flip `api-stack.ts:207` to
   `"overwrite:header.X-Lightbulb-Public-Host"`. Because step 1 is already in prod, the
   stack-ordering window is harmless in both directions.
3. **Delete the old spelling** from the backend, and delete the transitional paragraph — the
   same follow-up `websocket_chat.py`'s docstring asks for and has not yet had.

Do not compress 1 and 2 into one PR: they are separate CloudFormation stacks in a single
pipeline run, and the backend rolls at `maxHealthyPercent: 200`.

### 3.2 The container env-var + baked-path contract

The hardest item, and strictly harder than the `lightbulb-connector` precedent, because it is
**three**-sided rather than two:

1. the extended image bakes both the consumers (`Dockerfile:487,642,839,916-918`) and the
   `lightbulb-marketplace` plugin's `dist/`, which throws on a missing `ISOL8_API_URL` /
   `ISOL8_OWNER_ID`;
2. `ecs_manager` injects the names into the task definition (`ecs_manager.py:3025,3033`) and
   `container-stack.ts` execs the baked paths (lines 817, 850, 915);
3. **per-owner Atlas seed files already written to EFS** read `process.env.ISOL8_AGENTS_DIR`
   (`atlas_seed/files/atlas-*.js`, `build.lobster`, `recap-and-cost.lobster`) — these are not
   redeployed by an image roll; they are rewritten only on reseed.

`container-stack.ts:850` is a hard `exit 1` if the boot script is absent, so a CDK deploy that
lands before the matching image **fails every container boot in the environment**.

**Ordering, if it is ever done:** make every consumer accept `LIGHTBULB_* or ISOL8_*` and keep
both baked paths (symlink the old name to the new) → build and pin the image → `dev.tag`,
validate → `prod.tag` → dispatch → reroll all ~34 containers → only then have `ecs_manager`
emit the new names → reseed Atlas → drop the old names. That is `CLAUDE.md`'s
image → dispatch → reroll → gate ordering, twice.

**Recommendation: do not do this.** It is a multi-day, fleet-wide, boot-fatal migration whose
entire benefit is cosmetic — the names are invisible to every user. Adding them to the
documented residue list (Task 4) captures the value at a fraction of the risk.

### 3.3 The remainder

- **`isol8_paperclip`** — `paperclip_proxy.py:727`. Accept both cookie names for one
  deprecation window, set only the new one, then drop the old. Cheap, but it is still a
  two-release dance on a live auth path, so it does not belong in an "easy" batch.
- **`isol8.hire_suggestion`** — needs `parseHireSuggestion` to accept both *before* the image
  emits the new kind, then an image build + fleet reroll. Frontend-first, same shape as §3.1.
- **`isol8.notifications` / `PAPERCLIP_ISOL8_NOTIFY_*`** — not in this repo. Requires a
  `TheLightbulbCompany/paperclip` fork change, a new GHCR digest, a `paperclip-version.json`
  bump, and a per-tenant redeploy.
- **`.isol8-seed-fingerprint` / `isol8-setup-seed.json`** — EFS-resident per owner. Same
  once-only-migration shape `CLAUDE.md` uses to justify leaving `.isol8-guard` alone; the same
  reasoning applies, so the honest recommendation is to add them to the residue list rather
  than migrate them.

---

## Self-Review

**Spec coverage.** Every identifier from the brief is dispositioned: the 12 `ISOL8_*` vars
(§3.2), `X-Isol8-Public-Host` (§3.1, Task 4), `isol8_paperclip` (§3.3), the event strings
(§3.3, with `isol8.action` corrected to non-existent in §1.1), the test fixtures (§1.5,
deliberately unchanged), and documentation drift (Tasks 2-4). Fifteen further identifiers the
brief missed are catalogued in §1.2.

**Placeholder scan.** No TBDs. Every code step carries the literal text to write; every
verification step carries the exact command and its expected output.

**Type consistency.** `_severity()` keeps its signature and its three return values. Task 2 is
comment-only and asserts the synthesized template is unchanged. Task 5 renames a
module-private const with no exported surface.

**Scope.** Tasks 1-5 are five independent, individually-revertible commits, none of which
crosses a process boundary. Everything that does is in Part 3 and is not implemented.
