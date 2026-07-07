# AWS integration: on-demand pull credentials via `credential_process` — Design

**Date:** 2026-07-06 (reworked same day: push → pull after mechanism review)
**Status:** Draft (pending user review)
**Scope owner:** platform / infra
**Supersedes:** the credential-delivery mechanism (`credential_source=EcsContainer`)
of `2026-07-05-aws-integration-scope-tiers-design.md`. The scope tiers, ViewOnly
ARN fix, trust-pin gate concept, and connect UX from that spec are shipped and
stay. Only how credentials reach the agent changes.

## Summary

Live validation of the aws_sts rail (spec §4 of 2026-07-05, executed 2026-07-06)
failed at the first `aws` call from the agent with:

> `No credentials found in credential_source referenced in profile isol8-readonly`

Root cause (adversarially verified, see Evidence): OpenClaw's host-env sanitizer
strips `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` from every agent exec child, so
`credential_source = EcsContainer` can never resolve its base credentials from an
agent exec. This is upstream, deliberate, and good: it is the wall that keeps the
agent away from the ECS task role. The shipped design required the agent to reach
the task role in order to bootstrap the AssumeRole — it fought the wall and lost.
It was never an IAM problem: the error is raised locally by botocore before any
network call, and the live IAM chain was verified intact.

**The redesign: on-demand pull.** The materialized profile uses
`credential_process` — a first-class AWS mechanism where the CLI executes a
program to obtain credentials. The program is a small helper the backend
materializes onto the owner's EFS; it authenticates to a new backend endpoint
with a per-owner bearer token, the backend performs the `sts:AssumeRole`
(server-side, with the ExternalId it already stores), and the helper caches the
short-lived credentials locally. Rotation happens **in the container, exactly
when needed** — no scheduled infrastructure, credentials exist on disk only
around actual use, every fetch is an audited backend request, and disconnect
revokes access instantly.

## The credential model (the point of the design)

Two identities, hard-walled by OpenClaw's sanitizer — which becomes the
enforcement boundary rather than the obstacle:

| | Identity | Delivery | Consumers |
|---|---|---|---|
| Node process | ECS task role | metadata endpoint (`AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`) | Bedrock inference, EFS, logs, ECS Exec — infra only |
| Agent exec | connector role (`isol8-connector`) | `credential_process` helper → backend → STS | the agent's `aws` CLI — the only AWS identity the agent has |

The sanitizer strips every env credential channel from the exec child
(`AWS_CONTAINER_CREDENTIALS_*`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN`) while allowlisting `AWS_CONFIG_FILE`. File-mediated
delivery is the only channel that survives — and `credential_process` is a
config-file setting, invisible to env sanitization.

Honesty note on the wall: exec children run as the same UID as the node
process, so `/proc/1/environ` is technically readable — the sanitizer is
defense-in-depth against ambient use, not a hard boundary against a determined
agent. This argues for keeping the task role *boring*: after this spec's §5
carve-out revert, the task role cannot assume anywhere in our account even if
its credentials were scraped.

## Evidence (verified 2026-07-06; four claims survived adversarial review, plus pull-mechanism checks)

- **Sanitizer, at the deployed tag** (`v2026.6.10-isol8.2`, = prod + dev image
  base): `AWS_CONTAINER_CREDENTIALS_{RELATIVE,FULL}_URI` are in
  `blockedOverrideOnlyKeys` and absent from `allowedInheritedOverrideOnlyKeys`
  (`host-env-security-policy.json:249-250,268-272`), so they land in
  `blockedInheritedKeys` and are dropped from the exec child
  (`bash-tools.exec.ts:1572-1584`). Plugin hooks cannot re-inject them —
  `filterPluginExecEnv` silently drops blocked keys (`bash-tools.exec.ts:124-143`).
  No config/env escape hatch exists at that tag; the fork diff touches zero
  sanitizer files. This also rules out delivering creds via plugin env
  injection — the helper cannot live "in the plugin."
- **The error is pre-IAM**: botocore raises `"No credentials found in
  credential_source referenced in profile …"` locally when the container
  provider finds no URI env var (`botocore/credentials.py:1857-1870,2068-2088`)
  — before any `AssumeRole` network call. The live IAM chain was verified
  intact; had base creds resolved, the assume would have succeeded.
- **`credential_process` works in a stripped env** (empirically tested):
  with `env -i`, no HOME, no credentials file, a config profile whose
  `credential_process` prints `{Version:1, AccessKeyId, SecretAccessKey,
  SessionToken, Expiration}` JSON authenticated successfully. The helper is
  executed **once per CLI invocation** (counted 2 runs for 2 commands) — the
  CLI does not cache process-sourced creds on disk, so the helper does its own
  caching.
- **The plugin needs zero changes**: `resolve.js` matches only the
  line-anchored `[profile isol8-readonly]` header — never the body — and
  re-reads the file on every call. A `credential_process` body still triggers
  `AWS_PROFILE=isol8-readonly` injection.
- **The container can reach the backend**: per-user container security group
  is `allowAllOutbound: true` (`container-stack.ts:143`); containers already
  make outbound calls (npm, MCP servers). The image is node-based (Debian) —
  the helper needs no new runtime.
- **Backend can already call AssumeRole**: the backend task role has
  `sts:AssumeRole` on `*` (sid `StsAccess`, `service-stack.ts:742-745`, since
  the original ServiceStack), no permissions boundary, no Deny anywhere, and
  SCPs cannot apply (we are the org management account). The **only** blocker
  is the connector role's trust policy, which currently pins the per-owner
  container role.
- **Sessions are capped at 1 hour, hard**: backend task-role creds are
  themselves role-session creds, so backend `AssumeRole` is role chaining (ECS
  is absent from AWS's exemption list, which names only user credentials and
  EC2 instance profiles); chaining caps sessions at 3600s regardless of
  `MaxSessionDuration`. Every deployed connector stack also pins
  `MaxSessionDuration: 3600`.
- **Expiry is a clean failure** (empirically tested with genuinely expired
  creds): `An error occurred (ExpiredToken) … The security token included in
  the request is expired`, exit non-zero. (Wording varies per service — never
  string-match it.)
- **Blast radius of the trust migration is one stack**: exactly one aws_sts
  connection exists fleet-wide (the founder's self-account connector,
  PowerUser tier). No customer stacks to migrate.

## Design

### 1. The pull flow

```
agent runs `aws …`
  → plugin injects AWS_PROFILE=isol8-connector           (naming-only plugin change, image rebuild)
  → CLI reads AWS_CONFIG_FILE profile                     (unchanged env wiring)
      [profile isol8-connector]
      region = {region}
      credential_process = /usr/local/bin/node /home/node/.openclaw/.aws/isol8-creds.mjs
  → helper: cache valid (> 10 min left)? print it
            else POST {api}/api/v1/connections/aws/credentials
                 Authorization: Bearer <per-owner token>
  → backend: validate token (hash compare, constant-time)
             → read {role_arn, external_id, region} from Secrets Manager
             → AssumeRole with the backend task role (§3), DurationSeconds=3600
             → return credential_process JSON {Version:1, AccessKeyId,
               SecretAccessKey, SessionToken, Expiration}
  → helper: write cache (0600) + print → CLI proceeds
```

The ExternalId never leaves the backend. Credentials appear on EFS only as the
helper's cache, only after actual use, and expire ≤1h later.

### 2. Materialized files (all on the owner's EFS `.aws/`, by the backend)

`materializers/aws_cli.py` writes, at connect (and rewrites on any repair):

| File | Mode | Content |
|---|---|---|
| `config` | 0600 | `[profile isol8-connector]` + `region` + `credential_process` line (absolute node + helper paths) |
| `isol8-creds.mjs` | 0700 | ~40-line node helper: read token, check cache, fetch, cache, print. No dependencies beyond node stdlib. API base URL baked in at materialization (env-specific). 10s fetch timeout; on failure, fall back to cache if still valid, else exit non-zero with a clear one-line error naming the Isol8 credential service. |
| `token` | 0600 | per-owner bearer token (below) |
| `cache.json` | 0600 | written by the helper at runtime, not the materializer |

Disconnect removes all four (existing remover path) — plus the server-side
revocation below, which is what actually cuts access.

- **EFS-materialized, not image-baked — deliberate.** The helper and the
  endpoint are two halves of one contract; materialization ships them in the
  same backend deploy, atomically, to every container including existing ones.
  Image-baking (e.g. in the plugin's directory) reintroduces image-release
  coupling and fleet version skew — the two slowest parts of v1's rollout.
  The helper being agent-writable is acceptable: the agent can already run
  arbitrary code as itself; the security boundary is the token + backend
  validation, not helper integrity.
- The profile stays **named** (never `[default]`) so the node process's
  Bedrock SDK default chain is untouched — and is **renamed to
  `isol8-connector`** (with the template file renamed `aws-connector.yaml`,
  prose de-"readonly"-ed everywhere): the naming cleanup rides this change
  since the plugin gets a routine image rebuild anyway. The CFN logical ID
  `Isol8ReadOnlyRole` alone stays (renaming a logical ID replaces the role in
  deployed stacks).
- **Tamper analysis (the files are agent-writable — acceptable):** editing
  `credential_process` gains nothing (the agent already has arbitrary exec as
  itself); reading the token grants exactly the access the agent is entitled
  to (cross-tenant impossible — each container's EFS access point roots at
  `/users/{owner}`, other owners' files are not mounted); corrupting a file is
  self-harm, repaired by deterministic re-materialization. The materializer is
  the **single writer** (verified: config patcher touches only `openclaw.json`,
  the reconciler skips materializer files, reprovision preserves EFS). The
  config file also doubles as the opt-in signal: profile present ⇔ connected
  (the plugin injects `AWS_PROFILE` only when the header exists). A read-only
  mount split (RO config/helper + RW cache) was considered and rejected: extra
  access-point plumbing to prevent tampering that escalates nothing.

### 3. Trust — account-root principal + ExternalId (the standard pattern)

The connector role's trust policy names our **account root**:

```json
{"Effect": "Allow",
 "Principal": {"AWS": "arn:aws:iam::877352799272:root"},
 "Action": "sts:AssumeRole",
 "Condition": {"StringEquals": {"sts:ExternalId": "<owner's ExternalId>"}}}
```

This is AWS's own documented cross-account SaaS pattern (the confused-deputy
docs; how Datadog-class integrations work): root delegates to our account's
IAM, and the per-owner ExternalId — held server-side in Secrets Manager, never
on customer or container disk — is the per-owner lock. The root ARN can never
drift, so nothing in any connected account ever needs updating again.

**Why the account-level pin is safe here:** the principals in our account that
could exercise it reduce to the backend and human admins. Per-owner container
roles lose their `sts:AssumeRole` grant entirely (`owner-aws-sts` retired, §5)
AND the restored unconditional boundary deny blocks assume-into-our-account;
other service roles lack the ExternalId (SM access is scoped); admins can read
SM anyway. The backend calls `AssumeRole` directly with its task role
(single hop, `DurationSeconds=3600` — still role chaining, still the 1h cap).

**Rejected variants:** (a) pin the backend task role ARN — CDK-generated
suffix (`…TaskRole30FC0FBB-…`); role replacement would silently break every
connected account's trust, fixable only by each customer; (b) a dedicated
stable-named `connector-source` middle role — solves (a) but is invented
infrastructure and an extra STS hop for a problem root-trust solves more
simply.

Sites that change in lockstep (all reference the same helper):
- `aws_cfn.py`: `owner_task_role_arn(owner_id)` → `platform_trust_principal_arn()`
  returning the root ARN (account id from `bedrock_aip.OUR_ACCOUNT_ID`, no
  literals; no owner parameter — per-owner isolation lives in the ExternalId
  condition, which stays).
- `connections.py:282` — quick-create link + `aws-setup` response inject the new
  principal.
- `connections.py:114` — the self-account trust-pin gate expects the root
  principal + the owner's ExternalId (same fail-closed logic).
- `aws-readonly-connector.yaml` — parameter *description* text only (the
  principal is a pure `!Ref`); README drift fix (it still claims two tiers).

### 4. The token rail (the one genuinely new security surface)

**Why an endpoint exists at all:** `sts:AssumeRole` must be SigV4-signed with
existing AWS credentials, and the agent's exec env has none — by design (the
sanitizer; v1's root cause). The backend is the only component holding both an
AWS identity (its task role) and the ExternalId, so it is the
identity bridge. The only unsigned STS call is `AssumeRoleWithWebIdentity` —
i.e. removing the endpoint is exactly the OIDC endgame in Rejected
alternatives, deferred deliberately.

- **Mint at connect** (`aws-complete`): 32 random bytes, urlsafe. Store only a
  SHA-256 hash, added to the existing Secrets Manager credential JSON
  (`isol8/{env}/integrations/{owner}/aws` gains `"token_hash"`). Plaintext goes
  to the owner's EFS token file and nowhere else.
- **Validate per fetch**: request carries `owner_id` + bearer token; backend
  reads that owner's SM secret, `hmac.compare_digest` on the hash. Any
  mismatch, missing connection, or `status != connected` → 401. No global
  token index needed.
- **Rate limit**: per-owner in-process limiter (e.g. 10/min — legitimate use
  is ~1 fetch per 50 min). Excess → 429 + metric.
- **Audit**: every fetch logged (owner, result, latency) + CloudWatch metric —
  the credential path becomes observable per-use, which neither push nor
  EcsContainer offered.
- **Revoke**: disconnect deletes the SM secret (hash gone → all future fetches
  401) and the EFS files. Outstanding cached creds expire ≤1h — same tail as
  every design; instant cut of new issuance is strictly better than push's
  ≤15-min window.
- Precedent for the pattern in-repo: desktop device tokens
  (`crypto/operator_device.py`), service JWTs (`service_token.py`).

### 5. What gets retired (security posture strictly improves)

- `bedrock_aip.put_owner_aws_sts_policy` / `delete_owner_aws_sts_policy` (the
  per-owner `owner-aws-sts` inline grant + its self-account-deny-drop logic) —
  retired. The per-owner container role loses its AWS-rail job entirely.
- The #1155 self-account carve-outs revert: `BoundaryDenyAssumeIntoOurAccount`
  returns to **unconditional** (drop the `aws:PrincipalArn` exemption in
  `container-stack.ts`), and the inline-deny drop logic goes. No per-owner
  container role can assume into our account, ever, founder included.
- The trust-pin gate **stays** (repointed at the account-root principal):
  still what makes self-account connection safe — only a role whose trust pins
  our assume principal + the owner's ExternalId can be connected; infra roles
  (which trust service principals, not root+ExternalId) remain unconnectable.
- **No scheduled worker pass.** Nothing to enumerate, no refresh cadence, no
  runway math, no multi-task overlap analysis, no `list_by_provider` repo
  addition. The update-service worker is untouched.

### 6. Failure modes

- **Backend down at fetch time**: helper uses cache if >0 min validity remains
  (typical tolerance: up to ~1h after the last successful fetch), else exits
  with a clear one-line error. Self-heals on the next `aws` call once the
  backend returns. Same availability class as chat itself.
- **A long-running CLI command near session end** dies with `ExpiredToken` —
  creds are resolved once per process. Documented, accepted.
- **Helper latency**: ~0 on cache hit; one HTTPS round-trip + 1 STS call
  (~0.4s) on miss, roughly once per 50 min of active use.
- **Reprovision** preserves EFS → files survive; nothing to rehydrate.
- **Rolling deploys / task death / multi-backend**: stateless per-request —
  no coordination of any kind.

### 7. Migration

- **Our account (the only live connection):** one CloudFormation parameter
  update on the existing `isol8-connector` stack — `TrustedPrincipalArn` →
  `arn:aws:iam::877352799272:root`, `UsePreviousValue` for ExternalId,
  re-acknowledge `CAPABILITY_NAMED_IAM`. No reconnect, no container change.
- **Existing materialized file**: the founder re-runs nothing — `aws-complete`
  is not re-invoked; instead the backend rewrites the owner's `.aws/` files
  (config/helper/token) via a one-shot repair path on deploy (or the founder
  disconnects/reconnects — 2 minutes; decide in the plan).
- **Ordering:** CDK (boundary revert) and backend ship together → CFN
  parameter update → materialize/repair → validate.

## What does NOT change

- The connect-flow CFN stack in the target account — still required; it *is*
  the connector role. One-time per account, quick-create link, same UX.
- The `aws-exec-profile` plugin's hook wiring and resolver LOGIC, and the
  OpenClaw fork — zero changes. (The plugin's header-only profile check was
  verified compatible; plugin env injection of credentials is
  sanitizer-blocked, so the helper could never live there anyway.) The plugin
  does get a naming-only rename (profile constant + descriptions) and the
  extended image rebuilds to carry it — a routine, mechanical rebuild.
- `container-stack.ts` env (`AWS_CONFIG_FILE` already deployed) — no delivery
  change (only the §5 boundary revert touches this stack).
- Scope tiers, picker, ViewOnly ARN fix, ExternalId minting — all stay.
- Bedrock / node-process credential chain — untouched.

## Rejected alternatives

- **Backend-push static creds on a 15-min schedule** — the previous draft of
  this spec; fully verified and viable, kept as the documented fallback (all
  IAM/trust work is shared, so switching later is cheap). Rejected because
  pull dominates it operationally: no scheduled infra, creds on disk only
  around use, per-fetch audit, instant revocation, and rotation lives in the
  container as desired.
- **OIDC web identity federation** (`AWS_WEB_IDENTITY_TOKEN_FILE` *is*
  sanitizer-allowlisted; CLI would self-refresh via
  `AssumeRoleWithWebIdentity`, no chaining cap, sessions to 12h) — the
  customer-scale endgame if we ever want the backend out of the credential
  path entirely. Rejected now: it makes Isol8 a public OIDC IdP (JWKS hosting,
  signing-key rotation) and adds an `AWS::IAM::OIDCProvider` + federated trust
  to every customer template — disproportionate for one connection, and the
  12h sessions lengthen the revocation tail.
- **Task-role revival** — fork-allowlisting the credential URI (or
  `/proc/1/environ` scraping) re-exposes the task role to every agent
  fleet-wide (the sanitizer policy is global), and forces back the per-owner
  trust + both #1155 carve-outs. Strictly worse, fork change or not.
- **IAM Roles Anywhere** — golden path for non-AWS workloads, but X.509 +
  signing-helper binary in the image + trust anchors per account + ACM PCA
  cost. Disproportionate.
- **Helper baked into the image / plugin directory** — see §2; version-skew
  and image-release coupling for no security gain.

## Acceptance criteria

From a gateway agent exec (chat path, where the plugin fires):

1. `aws sts get-caller-identity` returns the **connector role** ARN
   (`…assumed-role/isol8-connector/…`), not the task role.
2. Tier enforcement spot-check unchanged from the 07-05 spec (write denied on
   read tiers, permitted on `power_user`).
3. **Cache behavior:** two consecutive calls → exactly one backend fetch
   (audit log shows one entry); after cache deletion, the next call fetches
   again.
4. **Revocation:** disconnect → next fetch 401s, helper reports the clear
   error once the cache expires; reconnect restores.
5. **Backend-down degradation:** with the endpoint unreachable and a valid
   cache, calls still succeed; with no cache, the helper's error names the
   credential service (not a generic CLI stack trace).
6. Node process unaffected: no `AWS_PROFILE` in process env; Bedrock inference
   still works.

## Rollout

1. **CDK:** boundary-deny revert only (container-stack — restore the
   unconditional `BoundaryDenyAssumeIntoOurAccount`). No new IAM resources.
   One deploy.yml ride.
2. **Backend:** credentials endpoint + token rail + materializer rewrite
   (config/helper/token) + `aws_cfn.py` helper + `connections.py`
   principal/gate updates + retire `owner-aws-sts` machinery. Same deploy
   (backend image ships as a CDK asset in deploy.yml).
3. **Ops:** the one-parameter CFN stack update on `isol8-connector` (manual,
   ~2 min) + re-materialize the founder's `.aws/` files.
4. **Live validation:** the acceptance list — every link above was verified
   individually before this end-to-end attempt; the only never-run link is the
   backend's real AssumeRole with the real ExternalId.
