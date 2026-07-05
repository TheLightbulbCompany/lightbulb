# AWS integration: scope tiers + self-account support — Design

**Date:** 2026-07-05
**Status:** Approved (design), pending implementation plan
**Scope owner:** platform / infra

## Summary

The `aws_sts` integration lets an owner connect an AWS account so their
OpenClaw agent can make scoped AWS API calls (via `aws` CLI in the agent's
exec environment, using short-lived credentials from a cross-account
`AssumeRole`). Today it ships with only two read presets, a **broken default
policy ARN**, and a hard block on connecting Isol8's own account — and the
entire runtime path has **never been exercised end-to-end** (all tests are
mocked).

This spec makes the integration (1) offer a real scope choice, (2) work on its
default path, and (3) be connectable against our own account by a single
allowlisted owner — and proves the whole rail live for the first time by having
the founder connect our own account through the real flow.

## Non-goals (explicitly out of scope)

- **How we steer the ops agent ("Jarvis").** What the agent does with whatever
  AWS access exists — its instructions, soft guardrails, ops workflows,
  secret-restraint — is a *separate concern* handled through the agent's
  prompt/soul, and gets its own downstream spec. This spec designs the
  **integration** as a general, agent-agnostic capability. There are **no
  per-agent IAM overlays** here; the chosen tier *is* the entire capability.
- The customer-facing productization of tiers beyond exposing the picker
  (marketing, tier docs, billing) — not part of this spec.

## Current state (verified, file:line)

- **Credential rail (works as designed, never run live):** the baked AWS CLI v2
  (`apps/infra/openclaw/Dockerfile`) reads `AWS_CONFIG_FILE` (set in
  `container-stack.ts` to `/home/node/.openclaw/.aws/config`). The baked
  `aws-exec-profile` plugin's `resolve_exec_env` hook injects
  `AWS_PROFILE=isol8-readonly` into the **agent exec env only** (never the node
  process), so in-process Bedrock stays on the ECS task-role default chain. The
  materialized `[profile isol8-readonly]` (`materializers/aws_cli.py`) uses
  `credential_source=EcsContainer` → `AssumeRole` into the connector role with
  `external_id`.
- **The plugin is native, not a hack:** `resolve_exec_env` is a first-class
  upstream OpenClaw plugin-SDK hook (vanilla `openclaw/openclaw`, added
  2026-06-02); our plugin registers through the public `definePluginEntry` SDK
  entry point with **zero fork changes**. No config-only alternative exists —
  the config-level env fields (`env.vars`, skills `primaryEnv`) mutate
  `process.env` globally and would break Bedrock. **No change to the plugin.**
- **Tiers today:** only `ViewOnlyAccess` / `ReadOnlyAccess`, gated by CFN
  `AllowedValues` (`aws-readonly-connector.yaml`), mirrored in
  `registry.py` (`AwsStsSpec`), `providers.yaml`, and the `aws-setup` response
  (`connections.py`). Frontend default is `view_only` (`AwsConnectCard.tsx`).
- **Self-account block (3 layers):** `connections.py:305` returns HTTP 400 if
  the pasted role ARN is in `877352799272`; `bedrock_aip.py`
  `DenyAssumeIntoOurAccount` inline-denies `sts:AssumeRole` into
  `arn:aws:iam::877352799272:role/*`; `container-stack.ts`
  `BoundaryDenyAssumeIntoOurAccount` denies the same at the permission boundary.

### Known defects found during design research

1. **Broken ViewOnly ARN (ship-blocker).**
   `arn:aws:iam::aws:policy/ViewOnlyAccess` returns `NoSuchEntity` from IAM;
   the correct ARN is `arn:aws:iam::aws:policy/job-function/ViewOnlyAccess`.
   Because ViewOnly is the **default** scope, the default connect path fails
   role creation today. Wrong in three sites: `providers.yaml:443`,
   `registry.py:151`, and the CFN `AllowedValues`. The test only asserts
   `.endswith('ViewOnlyAccess')`, so it passes on the broken value.
   (`ReadOnlyAccess` is correct as-is — it is not a job-function policy.)
2. **Never validated live.** Every existing test is unit-level/mocked; the
   runtime `AssumeRole` path has never booted on a live container.

### Corrected facts (were wrong in earlier discussion)

- Neither `ViewOnlyAccess` nor `ReadOnlyAccess` grants
  `secretsmanager:GetSecretValue` — **neither read preset can read secret
  values.** The real difference is data-plane reads: `ReadOnlyAccess` reads S3
  object bodies + DynamoDB items; `ViewOnlyAccess` is metadata/list only — so
  ViewOnly is the genuinely safer default. `GetSecretValue` is only present in
  the broad write presets (PowerUser/Admin), which this spec does not add a
  deny to (that would be an agent-steering concern, out of scope).
- An IAM role can attach 10 (default) / 25 (max) managed policies — composition
  is possible but unused here; each connector role attaches exactly one.

## Design

### 0. Configuration — nothing hardcoded (hard requirement)

**No owner ids, account ids, or role ARNs appear as literals in source.** All
identity-specific values are configuration- or context-derived:

- The self-account allowlist owner id is a new per-env setting
  `PLATFORM_SELF_ACCOUNT_OWNER_ID`, wired exactly like `PLATFORM_OPS_OWNER_ID`:
  `PLATFORM_SELF_ACCOUNT_OWNER_ID_DEV`/`_PROD` GitHub secrets → `app.ts` synth
  env → `service-stack.ts` task env → `config.py` → backend. **Empty ⇒ no owner
  may connect our own account (fail-closed).** For the CDK boundary carve
  (`container-stack.ts`), the same value flows from the synth env — the founder
  org id never appears in committed infra code.
- The platform account id uses existing derived sources — `this.account` in
  CDK, and the pre-existing `bedrock_aip.OUR_ACCOUNT_ID` in the backend (not a
  new literal). No new account-id literals are introduced.
- The connector role ARN the allowlisted owner may assume is *composed* from the
  configured owner id + account + the fixed connector role name, not written as
  a literal.

This mirrors the repo's established no-Clerk-ids-in-git posture. The concrete
founder-org id (`org_3DBS…`) named elsewhere in this spec is illustrative; it
lives only in the deploy secret, never in code.

### 1. Scope tiers (uniform, pure AWS managed-policy presets)

Expand the menu to a three-rung ladder, identical for every owner:

| Tier | Managed policy ARN | Notes |
|------|--------------------|-------|
| `view_only` (default) | `arn:aws:iam::aws:policy/job-function/ViewOnlyAccess` | metadata/list; no data-plane read |
| `read_only` | `arn:aws:iam::aws:policy/ReadOnlyAccess` | broad read incl. S3/DDB data |
| `power_user` | `arn:aws:iam::aws:policy/PowerUserAccess` | read + write, excludes IAM/Org management |

`AdministratorAccess` is deliberately **not** offered (account-root). No
composed or overlay policies — pure presets keep the tiers uniform and
CloudFormation `AllowedValues`-validated.

**Role name:** the connector role is hardcoded `isol8-readonly-connector` in
the CFN template. Under `power_user` this name is a mild misnomer, but
**keep it** — existing connections and the trust/grant plumbing reference it by
name, so renaming is a breaking change for no functional gain. (If a rename is
ever wanted, it is a separate migration, not this spec.)

**Sites to change (coordinated):**
- `apps/infra/aws/aws-readonly-connector.yaml` — add the PowerUser ARN to
  `ManagedPolicyArn.AllowedValues` (and fix the ViewOnly ARN, §2).
- `apps/backend/core/services/credentials/registry.py` — `AwsStsSpec` gains the
  power-user policy ARN alongside view/read.
- `apps/backend/core/services/credentials/providers.yaml` — aws_sts block lists
  all three.
- `apps/backend/routers/connections.py` — `aws-setup` returns all three with
  their quick-create URLs.
- `apps/frontend/src/components/settings/AwsConnectCard.tsx` — picker offers
  three tiers, `view_only` default, with a one-line risk label per tier.

### 2. Fix the broken ViewOnly ARN (lands first)

Correct the ARN to `arn:aws:iam::aws:policy/job-function/ViewOnlyAccess` in all
three sites, and change the connect test from `.endswith(...)` to an exact-ARN
assertion so the malformed value can never pass again. This is a live-breaking
default-path bug; it is the first commit in the stack (and could be
cherry-picked as a standalone hotfix if we want it out before the rest).

### 3. Self-account carve-out (narrow, exact-match)

Allow exactly one owner — the founder org `org_3DBS8G83oPrMm49yN2b70EjoFpN` — to
connect account `877352799272`, and only to assume *its own* connector role.
Every other owner remains fully blocked. The allowlist is the configured
`PLATFORM_SELF_ACCOUNT_OWNER_ID` (§0 — per-env config, never a source literal,
fail-closed when unset). It gates all three layers:

- **`connections.py:305`** — the 400-gate skips only when
  `owner_id == PLATFORM_SELF_ACCOUNT_OWNER_ID` (and the pasted role account ==
  our account). Still validates the ARN shape and that it is the expected
  connector role.
- **`bedrock_aip.py` inline deny** — for the allowlisted owner, the
  `DenyAssumeIntoOurAccount` statement is narrowed so it does **not** cover the
  one exact connector role ARN that owner is permitted to assume; the broad
  deny into every other role in our account stays.
- **`container-stack.ts` boundary deny** — same exact-ARN carve on the
  permission boundary (requires a CDK deploy). This is the hardest floor; an
  explicit `Deny` always wins, so the carve must be an exact-ARN exception, not
  a blanket removal.

The result: the founder org's per-owner task role may `AssumeRole` into exactly
`arn:aws:iam::877352799272:role/isol8-readonly-connector` (its own connector),
nothing else; all customer isolation is preserved.

### 4. Approach A — connecting our own account is the live validation

The founder connects account `877352799272` through the **real** connect UI:
pick a tier → deploy the CFN quick-create stack in our account → paste the role
ARN back. This exercises the entire rail end-to-end for the first time and
doubles as the acceptance test (§6). No separate synthetic test harness needed
for the happy path.

### 5. The plugin — unchanged

`aws-exec-profile` is the correct, native mechanism. No code change; we only
confirm it fires on the live path during validation.

## Acceptance criteria (the never-tested invariant)

From a **gateway agent exec** (the chat/agent exec path, not a root ECS-exec
shell — the plugin only fires on `resolve_exec_env`):

1. `aws sts get-caller-identity` returns the **assumed connector role** ARN
   (`.../isol8-readonly-connector/...`), not the per-owner task role.
2. A write call (e.g. `aws s3 mb …`) is **denied** under `view_only`/`read_only`
   and **permitted** under `power_user` (spot-check on the self-account
   connection).
3. The **node process has no `AWS_PROFILE`** and Bedrock inference is
   unaffected (in-process SDK still on the task role).
4. The default connect path (`view_only`) **creates the role successfully**
   (ARN fix verified against live IAM).

## Open questions to resolve during implementation

Flagged by design research; must be answered before/while building:

1. **Per-owner task role for the founder org.** The CFN trust pins the owner's
   per-owner ECS task role (`isol8-{env}-owner-bedrock-{owner}`). Confirm the
   founder org actually has a per-owner role (was minted for all provider
   choices per Plan 1, but verify for `org_3DBS…` specifically) — without it
   there is no principal for the trust and connect breaks.
2. **Reprovision rehydration.** `~/.aws/config` is written on connect; confirm
   whether a container replacement re-materializes it (like the mcp_oauth
   rehydration) or silently drops the profile until the owner reconnects. If no
   rehydration path exists, add one.
3. **Session refresh.** Connector role `MaxSessionDuration` is 3600s; confirm
   `credential_source=EcsContainer` auto-refresh handles the hourly rollover
   cleanly during long agent exec runs.

## Risks & rollback

- **Blast radius of the carve-out:** exactly one allowlisted owner + one exact
  connector-role ARN. No other owner, and no other role in our account, becomes
  assumable. Customer isolation is untouched.
- **Rollback:** disconnect kills the grant (drops the inline grant + secret +
  `~/.aws/config` profile). Full revert = unset the `PLATFORM_SELF_ACCOUNT_OWNER_ID`
  secret + redeploy (backend + one CDK deploy for the boundary) — because the
  allowlist is config, not code, this is a secret change, no code revert needed.
  The tier expansion + ARN fix are independently revertible.
- **Deploy dependency:** the boundary-deny carve requires a CDK deploy; the tier
  + ARN changes are backend/frontend. Sequence so the ARN fix can ship first.

## Rollout sequence

1. **ViewOnly ARN fix** (+ test hardening) — smallest, unblocks the live default
   path. Ships first (own commit; cherry-pickable).
2. **Tier expansion** to the three-rung ladder (4 backend sites + picker).
3. **Self-account carve-out** (backend 2 sites + CDK boundary) behind the
   allowlist constant.
4. **Live validation** — founder connects `877352799272`, runs the §6
   acceptance checks; resolve the §7 open questions.
