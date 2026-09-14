# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Lightbulb (`flickmeon.ai`) is an AI agent platform powered by [OpenClaw](https://github.com/openclaw/openclaw). Users subscribe, get a per-user ECS Fargate container running the OpenClaw Docker image, and work with them in a **Slack-like messaging workspace** — rooms, channels and DMs shared by AI employees and humans, driven by Clerk-authenticated HTTP commands with AWS AppSync Events for live delivery and DynamoDB as the canonical ordered store (see "Messaging"). The platform uses a Next.js 16 frontend (Vercel), FastAPI backend (ECS Fargate), Clerk for authentication, **DynamoDB** for metadata (users, containers, billing, usage counters, pending updates, channel links, admin actions, oauth tokens/state, integrations, webhook dedup, metronome fast-gate, bedrock AIP owners, agent-email, company slugs, paperclip-companies, marketplace listings/purchases/payouts/takedowns/sellers, provisioning-workflows — see `database-stack.ts` for the full set; BYO LLM keys live in **Secrets Manager**, not DynamoDB), AWS Bedrock + ChatGPT OAuth + BYO API key for LLM inference, Stripe for new subscription and marketplace purchases, and a configured Apple server-notification/verifier path retained only for historical Apple subscription lifecycle reconciliation. EFS provides per-user agent workspaces, and a per-tenant Paperclip BFF (Aurora Serverless v2 Postgres) powers the Teams Inbox. Infrastructure is managed via **AWS CDK** in `apps/infra/` (single source of IaC truth). The desktop app is **Tauri** (not Electron) — see `project_desktop_app_tauri` memory. There is also an **Expo iOS app** at `apps/mobile/` — see the "Mobile App" section.

Every freshly provisioned workspace gets a six-agent Paperclip starter roster: **CEO**, **Engineering**, **Finance**, **Growth**, **Analytics**, and **Customer Operations**. `paperclip_provisioning.provision_org` records their role-keyed UUIDs in `starter_agent_ids`; CEO is the initial root and the specialists report to it. Names are user-editable, so policy follows those UUIDs and roles rather than a display name. This applies only to fresh workspaces — existing/founder rosters are not migrated. **Friday** and **Atlas** are portable curated marketplace packages, not automatic seeds. The roster is Lightbulb-native: it has no GStack or ACP dependency.

The OpenClaw container image we run is an **extended image** built from our **OpenClaw fork** (`ghcr.io/thelightbulbcompany/agent`, a **Debian**-based image — NOT Alpine) with additional Linux skill binaries and baked plugins: the externalized upstream Slack/Discord/Bedrock plugins plus four in-repo plugins (`apps/infra/openclaw/plugins/{aws-exec-profile,lightbulb-channel,lightbulb-marketplace,paperclip-exec-creds}` — `lightbulb-channel` is the messaging ingress/outbound seam, see "Messaging"). Its Dockerfile lives at `apps/infra/openclaw/` and is published to ECR `lightbulb/openclaw-extended` by `.github/workflows/build-openclaw-image.yml`. The pinned base + per-env extended tags are tracked in `openclaw-version.json` at the repo root, where the fields mean distinct things:
- `upstream` — the fork base the Dockerfile `FROM`s (e.g. `ghcr.io/thelightbulbcompany/agent:2026.7.2-isol8.4`). A **private** GHCR package, pulled only by the authenticated build workflow. Keep it in lockstep with the Dockerfile `FROM` line.
- `image`/`tag`/`full` — the **legacy public** upstream image (`ghcr.io/openclaw/openclaw`), kept only as the bootstrap/unknown-env fallback in `container-stack.ts` (pulled by per-user ECS tasks with **no** registry credentials, so it must stay anonymously pullable). `tag` also feeds the `OPENCLAW_UPSTREAM_VERSION` env fallback.
- `extendedImage` + per-env `dev.tag`/`prod.tag` — the ECR extended image actually deployed; `dev.tag`/`prod.tag` are the per-environment promotion gate (flip dev, validate, then prod). They drive `meta.lastTouchedVersion` via `deployed_upstream_version()`.

Bundled agent-facing skills live at `apps/infra/openclaw/skills/` (`paperclip`, `lightbulb`). Both are **split**: a thin `SKILL.md` (routing table + non-negotiables) plus `references/*.md` the agent reads on demand — every agent pays for `SKILL.md` on every load, so new detail belongs in a reference, not inline. The `lightbulb` skill owns platform facts; the `paperclip` skill owns team coordination. When user-facing surfaces, marketplace/billing flows, or Paperclip seeding change, update the relevant skill (each skill's sibling `MAINTENANCE.md` lists every anchor **and the reference file that now carries it**; it is a sibling and not a header comment because an HTML comment inside `SKILL.md` is invisible when rendered but still costs the whole fleet tokens on every load). Per-owner dynamic state (plan/provider) is NOT in the skill; it lives in `~/.openclaw/lightbulb-facts.json`, written by `core/services/owner_facts.py`.

This repo does not vendor OpenClaw source. The base image is built from the **Isol8 fork `TheLightbulbCompany/agent`** (a GitHub fork of [github.com/openclaw/openclaw](https://github.com/openclaw/openclaw)). The fork's `main` tracks upstream — it was realigned to `openclaw/openclaw:main`, so `git pull upstream main` merges cleanly. Our patches do **not** live on `main`; they sit on `isol8/*` branches plus `v<version>-isol8.<n>` tags, which the fork's `docker-release.yml` builds and publishes to GHCR `ghcr.io/thelightbulbcompany/agent` (the carried patch set is enumerated in `openclaw-version.json`'s `notes`). The writable fork workspace is checked out at **`~/dev/agent-fork`** (origin = `TheLightbulbCompany/agent`, upstream = `openclaw/openclaw`). A stale second clone at `~/Desktop/agent` is pending delete — it sits on iCloud-synced Desktop and git operations hang there; do not use it. Treat fork checkouts as outside this repo — do not commit them into it.

Each user gets their own isolated OpenClaw container; the backend calls the container's baked lightbulb-channel plugin over HTTP for RPC and readiness, and the container reports run lifecycle and usage back over hook-token routes. Those events reach browsers through AppSync, never over a browser socket.

## Rebrand — Isol8 → Lightbulb (SHIPPED; residue documented below)

The `isol8` brand is retired. As of 2026-08-30 the migration is COMPLETE except
for the deliberate residue and pending items listed here. Current names:

| | value |
|---|---|
| Product name | **Lightbulb** |
| Product domain | **`flickmeon.ai`**, BOTH environments (`api.`/`api-dev.`, `ws.`/`ws-dev.`, `bv.`/`bv-dev.`, `admin.`, `admin.dev.`, `dev.`, `company.`, and Clerk's `clerk.`/`accounts.`/`clkmail.`) |
| Mail — humans, vendor logins, platform sender | **`thelightbulb.company`** (`hello@ support@ tickets@ admin@`) |
| Agent mail | **`{agent}@{slug}.thelightbulb.company`** (dev: `…@{slug}.dev.thelightbulb.company`) |
| GitHub | org **`TheLightbulbCompany`**, repo **`lightbulb`** (renamed 2026-08-30 from `Isol8AI/isol8`; forks `agent`, `paperclip`, `goosetown` moved with the org). GHCR is **`ghcr.io/thelightbulbcompany/*`** — package namespaces do NOT redirect. |
| Expo | org **`thelightbulbcompany`**, project display "Lightbulb Mobile" — same projectId; the slug stays `isol8-mobile` (see residue) |
| AWS account | name **"Lightbulb"**, alias **`thelightbulb`** (created 2026-08-30) |
| Legal entity | **"Isol8 Inc., doing business as Lightbulb AI"** — the entity rename is NOT yet effective. Do not write any other legal name. |

**`flickmeon.ai` is the product URL and carries NO mail** — no MX, no SES identity,
no mail records of any kind. Mail lives on its own domain, which is why the root
domain and the mail domain are two separate parameters
(`apps/infra/lib/domains.ts`: `ROOT_DOMAIN`, `PLATFORM_MAIL_DOMAIN`) threaded
through the stacks via `LightbulbStageProps`. `domains.ts` remains the per-environment
truth — read it rather than trusting any doc when a hostname matters.

**⚠️ GitHub OIDC subs are id-qualified since the rename.** After the 2026-08-30
org/repo rename, GitHub mints Actions OIDC tokens with subs shaped
`repo:TheLightbulbCompany@253404738/lightbulb@1182791203:…` (immutable ids, so the
form is rename-proof) — plain `repo:ORG/REPO:*` patterns no longer match. The
deploy-role trust policies and `container-stack.ts`'s builder-role trust use the
id-qualified form; keep it if you ever touch them.

**NOT renamed — deliberate residue; a naive `s/isol8/lightbulb/` sweep breaks
every one of these:**

- ~~**Data-bearing AWS resource names**~~ — **RENAMED; this bullet used to say
  "leave them," and that is now false.** Verified live against the AWS account
  2026-08-31: DynamoDB prefix is `lightbulb-{env}-*` (58 tables, e.g.
  `lightbulb-prod-billing-accounts`; 0 tables remain on `isol8-*`), Secrets
  Manager is `lightbulb/{env}/*` (271 secrets; 0 remain under `isol8/`), SSM is
  `/lightbulb/{env}/*`, ECR repos are `lightbulb/openclaw-extended` and
  `lightbulb/paperclip-extended` (no `isol8/*` repo exists), Cloud Map
  namespaces are `lightbulb-{env}.local`, and the deploy concurrency group is
  `lightbulb-deploy`. The rename WAS performed deliberately — see
  `apps/backend/scripts/migrate_secret_prefix.py` (`OLD_PREFIX = "isol8/"`;
  the spent script was deleted 2026-09-11, git history keeps it) —
  CLAUDE.md was simply never updated after it shipped. Do not trust any other
  doc or memory claiming these still live under `isol8-*`/`isol8/*`.
- **The SSO access portal URL `isol8.awsapps.com`** — AWS allows exactly ONE
  portal-URL customization per Identity Center instance, ever, and ours was spent
  on `isol8`. Unchangeable without deleting Identity Center (which wipes all
  users/groups/assignments). Accepted 2026-08-30. The `isol8-admin` CLI profile
  name stays with it.
- **The Expo project slug `isol8-mobile`** — Expo slugs cannot be renamed
  (dashboard and eas-cli have no rename; only display name changes).
  `apps/mobile/app.json` must keep `slug: "isol8-mobile"` to match the server.
- **The mobile bundle id `co.isol8.mobile`** — DECIDED 2026-09-07, after a rename
  to `company.thelightbulb.mobile` shipped in code (#2098) and stranded the app:
  ASC has exactly ONE record, **6788944100 on `co.isol8.mobile`**, and no App ID
  was ever registered for the new identifier, so nothing could be signed or
  uploaded. Keeping the old id preserves the record, build history through build
  18, the open Resolution Center thread on submission 05d24a7d, the App ID's Push
  capability + APNs key `W22KL7HTVW`, and the `co.isol8.signin` service id. A
  bundle id is invisible to users; a new record restarts review from zero.
  `APPLE_IAP_BUNDLE_ID` and the `apple-app-site-association` appID must match it.
  The IAP **product** id deliberately stays `company.thelightbulb.mobile.plan.monthly`
  — no product exists in ASC yet, so that one is free to carry the new brand.
- **`-isol8.N` image tags** and the `isol8/*` fork branches in
  `openclaw-version.json` / `paperclip-version.json` — they are rollback refs.
- **Historical issue/PR references** (`Isol8AI/isol8#2053`, `isol8 #2016`) — quote
  verbatim, always; GitHub web URLs redirect.
- **`BOOTSTRAP_EMAILS`** — renaming it silently disarms the guard that stops an
  admin wipe from deleting the Paperclip bootstrap admin, which is a fleet-wide
  provisioning outage.
- **`isol8` stays reserved** in the slug and handle allowlists, so nobody can claim
  the retired brand.
- **The EFS lock sidecars `.isol8-guard` / `.isol8-protocol`** — renaming them is a
  once-only migration across every EFS volume with a fatal failure mode and zero
  benefit (see "Config Patching Notes").
- **The internal dispatch header `X-Isol8-Public-Host`** — API Gateway parameter
  mapping (`api-stack.ts`) sets it from `$context.domainName`; `main.py`'s
  `HostDispatcherMiddleware` and `routers/paperclip_proxy.py` read it to route
  `company[-env].flickmeon.ai` traffic onto the Paperclip-proxy mount. Never
  renamed to `Lightbulb` branding — a rename means moving the
  `overwrite:header.*` parameter-mapping key and both readers together, not a
  docs edit. This file itself drifted to a `X-Lightbulb-Public-Host` spelling
  that appears in NO source file (fixed 2026-09-07): verify the header name
  against `api-stack.ts`/`main.py`/`paperclip_proxy.py`, never against prose.

**RUNTIME IDENTIFIERS — RETIRED 2026-08-31; do not reintroduce.** These used to
sit in the residue list above as "leave them, existing users/data would break".
The premise is gone (no external users; the one account holding a connector
stack is ours), so they were renamed outright, along with the transitional
accept-both shims that existed only to bridge them:

| was | now | lives in |
|---|---|---|
| `isol8-connector` | `lightbulb-connector` | `bedrock_aip.CONNECTOR_ROLE_NAME`, `aws-connector.yaml` `RoleName`, `service-stack.ts` iam:GetRole + sts:AssumeRole resources, the baked `aws-exec-profile` plugin |
| `isol8-creds.mjs` | `lightbulb-creds.mjs` | `aws_cli.HELPER_FILENAME` (on EFS at `.aws/`) |
| `isol8-facts.json` | `lightbulb-facts.json` | `owner_facts.FACTS_FILENAME`; read by the bundled `lightbulb` skill and the local-sqlite boot script |
| `isol8-email:` | `lightbulb-email:` | `agent_mail_thread._PREFIX` — **must stay covered by `hooks.allowedSessionKeyPrefixes`** |
| `isol8-agent-create:v1` | `lightbulb-agent-create:v1` | `paperclip_agent_idempotency._KEY_PREFIX` |
| `isol8-x` | `lightbulb-x` | `xurl_cli._XURL_APP_NAME` (app key inside the owner's `~/.xurl`) |
| `isol8-mcp-server` | `lightbulb-mcp-server` | the Paperclip agent API-key display name |
| `isol8-backend` (operator device) | `lightbulb-backend` | `operator_device.build_paired_operator_entry` `displayName` |
| `isol8-<owner>` STS session name | `lightbulb-<owner>` | `routers/connections.py` `RoleSessionName` |

**NOT yet renamed — the same class, still open (audited 2026-09-08).** These were
missed by the 2026-08-31 pass and are NOT residue; each needs a coordinated deploy
or an accept-both shim, so do NOT sweep them. Full audit + per-item recipes:
`docs/superpowers/plans/2026-09-08-isol8-runtime-identifier-gap.md`.

| identifier | why it can't be swept |
|---|---|
| `X-Isol8-Public-Host` | API GW (`api-stack.ts`) and the backend are separate stacks in one pipeline run, and the backend rolls at `maxHealthyPercent: 200`; a hard swap misroutes every `company[-env]` request during the roll. Precedent shim: `require_ws_internal_caller` in `routers/websocket_chat.py`. |
| the `ISOL8_*` container env vars + the baked `/usr/local/bin/isol8-*.sh`, `/etc/isol8-litestream.yml`, `/opt/isol8/` paths | three-sided (image, task def, AND per-owner Atlas seed files already on EFS). `container-stack.ts` hard-`exit 1`s when the boot-script path is missing, so a CDK deploy landing before the image fails EVERY container boot. |
| `isol8_paperclip` | live session cookie — a hard swap logs every active session out |
| `isol8.hire_suggestion` | emitted by the baked `lightbulb-marketplace` plugin, consumed by `parseHireSuggestion`, which returns null on mismatch → the HireCard silently degrades fleet-wide during a reroll |
| `isol8.notifications`, `PAPERCLIP_ISOL8_NOTIFY_*` | lives in the Paperclip FORK image, not this repo |
| `.isol8-seed-fingerprint`, `.openclaw/isol8-setup-seed.json` | EFS-resident per owner — same once-only-migration shape as the protected `.isol8-guard` |

**Never rename these** — the name correctly describes protected residue, or the
value is stored data: `_ISOL8_PATCH_RE` (parses the `-isol8.N` rollback tag),
`ISOL8_SYSTEM_SELLER_ID`'s value `"__isol8__"` (written to marketplace rows),
`ISOL8_CURATOR_EMAIL` (human-gated mail), and the `"isol8 top-up …"` descriptions in
`metronome_funding.py` — those are part of an idempotent Metronome request body, so
changing the string 409s a retried top-up on the money path.

Three of these have a state-on-EFS or state-in-someone-else's-account tail:
`lightbulb-connector` needs the quick-create stack relaunched **and** a rebuilt
extended image (the baked plugin injects the profile by name); the facts file's
old inode is deleted on every write so nothing can read frozen facts; the mail
prefix is inert until the container's `openclaw.json` allowlist is rewritten.

**CloudWatch metric namespaces** were renamed (owner decision 2026-08-27):
`Isol8*` → `LightbulbAI*`, and the metric `service` dimension `isol8-backend` →
`lightbulb-backend`. CloudWatch cannot backfill, so ~15 months of history is
orphaned (not deleted — just off every dashboard). Do NOT "fix" `LightbulbAI` in
metric namespaces to match the GitHub org name — they are unrelated identifiers.

**`ManagedBy` — migration COMPLETE (all 4 steps).** The tag gates every mutate
action on an owner's Bedrock application inference profile (`service-stack.ts` →
`aws:ResourceTag/ManagedBy`). It moved isol8-backend → lightbulb-backend via
add-before-remove, and the final narrowing shipped: `MANAGED_BY_ACCEPTED` lists
only `lightbulb-backend`, verified 2026-08-30 against all 64 profiles in both
envs (zero still tagged `isol8-backend`).
Do not confuse this tag with the
CloudWatch `service` dimension, the identical string in a different role.

**Still open (human-gated):** the `isol8.co` Google Workspace retirement + human
mailbox move to `thelightbulb.company` (including the AWS root-account email,
SNS/Budgets subscribers, and the three `google#…@isol8.co` integration rows);
the legal-entity paperwork and the Termly follow-ups.

---

## Critical Rules

**NEVER modify production servers directly.** All changes to backend, frontend, or infrastructure MUST:
1. Be version controlled in the appropriate repository
2. Go through CI/CD pipelines for deployment

This includes environment variables -- use AWS CDK, GitHub secrets, and AWS Secrets Manager.

**Always watch your runs after you make a push to a repository.**

**NEVER overwrite any environment or docker files, or git repos without permission.**

**If the tests are not passing, your change is likely the culprit, even if the error is not in a file you touched. These are all dependent systems.**

**NEVER use `sleep` or `tail` commands in bash.** Use proper tool-based approaches:
- Instead of `sleep`: use `gh run watch` for CI/CD, or tool-based polling
- Instead of `tail`: use the Read tool to read files, or run commands without piping through tail

**Planning docs are disposable.** Plans/specs under `docs/superpowers/` are working
documents for in-flight work. When the feature ships (or the approach is abandoned),
DELETE the doc in the same PR or shortly after — git history preserves it, and the
PR description is the durable record. The only plans/specs that stay are ones living
code or CLAUDE.md explicitly references. Never let shipped plans accumulate: a
2026-07 purge deleted 250 of 260 such files (196k lines) — don't rebuild that pile.
The same applies to one-shot backend scripts: once a backfill has run and the
data it wrote is produced at creation time, DELETE the script and its tests —
git history is the operational record (it carries the run date and PR context
an archive directory does not). Re-runnable ops tools stay in
`apps/backend/scripts/`.

One plan that stays under this rule: `docs/superpowers/plans/2026-09-07-audit-report.md`
— its §6 ("Deliberately NOT recommended") names load-bearing scar tissue that
must not be "fixed", and is referenced from
`apps/infra/lambda/bedrock-usage-meter/test_index.py`.

### Monorepo Setup

This is a **Turborepo monorepo** using **pnpm** for JS and **uv** for Python.

```bash
pnpm install                          # All JS deps (from repo root)
cd apps/backend && uv sync            # Python deps
```

### Individual Services

**Database:** DynamoDB tables provisioned by CDK (`apps/infra/lib/stacks/database-stack.ts`). No relational DB on the platform side — items are plain dicts, accessed via repositories in `apps/backend/core/repositories/`. (The per-tenant Paperclip BFF has its own Aurora Postgres, also provisioned in `database-stack.ts`.)

```bash
turbo run dev                              # everything, in parallel
turbo run dev --filter=@lightbulb/frontend     # or @lightbulb/backend
cd apps/backend  && uv run uvicorn main:app --reload --port 8000
cd apps/frontend && pnpm run dev           # localhost:3000; also `build`, `lint`
```

### Running Tests

```bash
turbo run test                        # All tests (parallel, cached)
turbo run test --filter=@lightbulb/backend    # Backend only
turbo run test --filter=@lightbulb/frontend   # Frontend only
turbo run lint                        # All linting
```

Backend: `cd apps/backend && uv run pytest tests/ -v`
Frontend unit: `cd apps/frontend && pnpm test`
Frontend E2E: `cd apps/frontend && pnpm run test:e2e`

### Container Re-provisioning (Dev)

When you change `openclaw.json` config, existing containers keep the old config on EFS. Re-provision:

```javascript
// Browser console (any Lightbulb page with Clerk loaded):
// PATCH -- wholesale-rewrites openclaw.json via write_user_configs + redeploys (preserves container).
// Seeds agents.list from the owner's LIVE Paperclip roster, so it CAN wire existing agent ids;
// it cannot CREATE the Paperclip company or agents (see Provisioning Flow).
await fetch('https://api-dev.flickmeon.ai/api/v1/debug/provision', {method:'PATCH', headers:{Authorization:'Bearer '+await Clerk.session.getToken()}}).then(r=>r.json())

// Full reprovision -- DELETE then POST
await fetch('https://api-dev.flickmeon.ai/api/v1/debug/provision', {method:'DELETE', headers:{Authorization:'Bearer '+await Clerk.session.getToken()}}).then(r=>r.json())
await fetch('https://api-dev.flickmeon.ai/api/v1/debug/provision', {method:'POST', headers:{Authorization:'Bearer '+await Clerk.session.getToken()}}).then(r=>r.json())
```

Debug endpoints return 403 in production.

### Clean-Slate Dev Reset

Full dev wipe (ECS, EFS, DynamoDB, Paperclip Aurora, Stripe, Clerk) moved to the
`dev-clean-slate-reset` skill (`.claude/skills/dev-clean-slate-reset/SKILL.md`).
Requires AWS SSO (`aws sso login --profile isol8-admin`).

### Prod Org Reprovision

Wipe an owner's container runtime + Paperclip side while preserving billing,
subscription, Clerk org, and marketplace rows — moved to the
`prod-org-reprovision` skill (`.claude/skills/prod-org-reprovision/SKILL.md`).

### Manual dev testing accounts

The E2E journey test no longer uses a fixed account — it mints a random
`isol8-e2e-<rand>@mailsac.com` per run and deletes it in `cleanupUser`
(`apps/frontend/tests/e2e/fixtures/user.ts`), so there is no reserved address
to avoid clobbering.

For manual dev testing, create your own Clerk account (or use an existing
personal email) at https://dev.flickmeon.ai/sign-up. The dev Clerk instance is
the same one used for prod previews, so test accounts created in dev are
real accounts -- don't use a production email.

**Signing a browser E2E in as an EXISTING dev test account without its
password** (Claude-in-Chrome / automation): mint a Clerk **sign-in token**
with the admin API and redeem it via the ticket strategy in the page — no
password/OTP typed, the same ticket mechanism `routers/desktop_auth.py`
uses. Get the dev Clerk secret from Secrets Manager
(`lightbulb/dev/clerk_secret_key`), find the user id via
`GET https://api.clerk.com/v1/users` (dev accounts are `+clerk_test`
addresses; `/users/sync` writes the JWT's email onto the users row and
heals rows that lack one, but rows untouched since 2026-09-03 may still
have no `email`, so Clerk stays the fallback truth),
then:
```bash
curl -sX POST https://api.clerk.com/v1/sign_in_tokens \
  -H "Authorization: Bearer $CLERK_SECRET_KEY" -H 'Content-Type: application/json' \
  -d '{"user_id":"user_XXX"}'   # -> {token, status:"pending"} (one-time, ~mins TTL)
```
In the browser (already on a dev.flickmeon.ai page, Clerk loaded):
```js
const si = await window.Clerk.client.signIn.create({strategy:'ticket', ticket:'<token>'});
await window.Clerk.setActive({session: si.createdSessionId});
```
Password prompts and OTP entry are off-limits; this admin-issued ticket is
the sanctioned path. NOTE the `+clerk_test` bootstrap-admin collision bug
(the bootstrap-admin address — `admin@isol8.co` today, `admin@thelightbulb.company`
after the mail move — as an owner can never seed Paperclip; issue #2003); pick
a different test account for import/Teams flows.

### Code Review

**CodeRabbit and Greptile are GONE** (subscriptions cancelled 2026-08-30). Do
not wait on them, and do not treat a stale `CodeRabbit` required check as
something to bypass — the fix is to drop the check, not to unprotect `main`.

| | who | surface | gates the merge? |
|---|---|---|---|
| **Gate** | `claude-code-review.yml` → `anthropics/claude-code-action` | sticky PR comment | **yes** — the job `claude-review` is the required status check on `main` |
| **Second opinion** | the `chatgpt-codex-connector` GitHub App | issue comment or 👍 reaction | no — it publishes no status check at all |

> **⚠️ KNOWN ISSUE, ACCEPTED BY THE OWNER (2026-09-14): both reviewers are down.**
> `claude-review` fails in ~1 s on every PR (`is_error:true`, zero model usage —
> the action's credentials, not the PR), and the Codex app answers `create a
> Codex account and connect to github` instead of reviewing. The owner knows and
> is fine with it: do not wait on either, do not treat the red `claude-review`
> as a blocker, and do not spend time fixing it. `main` currently has **no
> required checks** (`enforce_admins: false`), so merges are not blocked — the
> table above and "Admin bypass" below describe the intended setup, not today's.
> Until this note is removed, review the diff yourself (or with a subagent)
> before merging.

`claude-code-review.yml` runs on every `pull_request` and is deliberately **not**
path-filtered: a workflow that never starts reports no check, and a required
check that never reports blocks that PR forever. That is also why none of the
path-filtered gates (`backend.yml`, `frontend-ci.yml`, `infra-ci.yml`,
`company-ci.yml`, `mobile-ci.yml`) can be the required check. A job-level `if:`
is safe — a *skipped job* reports success to branch protection.

The same workflow posts a bare `@codex review` on open / ready-for-review /
reopen (not on every push). That comment is the trigger, and any trailing text
after `@codex review` stops it being picked up.

> **Installing the app is only half of it — it must be connected to a Codex
> account.** If Codex answers `To use Codex here, create a Codex account and
> connect to github`, nobody has linked it at
> <https://chatgpt.com/codex/cloud/settings/connectors>. It will keep replying
> with that one line and never review. That is a login problem, not a code one.

Codex then answers on one of four surfaces — check all four before concluding
it was silent:

```bash
gh pr view <n> --repo TheLightbulbCompany/lightbulb --json reviews  # formal review
gh api repos/TheLightbulbCompany/lightbulb/pulls/<n>/comments       # inline findings
gh api repos/TheLightbulbCompany/lightbulb/issues/<n>/comments      # "Review Result" / "Didn't find any major issues."
gh api repos/TheLightbulbCompany/lightbulb/issues/<n>/reactions     # a bare 👍 IS a complete approval
```

A clean pass is usually the issue **comment** or the 👍 alone, so a poll that
watches only the reviews endpoint hangs forever on a clean PR.

**Local `codex` CLI is the fallback** when the connector does not pick a PR up:
run it against the diff and paste the result into the PR thread. It cannot run
in Actions — it authenticates with a rotating ChatGPT OAuth token in
`~/.codex/auth.json` (there is no `OPENAI_API_KEY`), which is not a CI-safe
secret. That is why the GitHub App, not the CLI, does the automated pass.

**Admin bypass is not the normal path.** `main` runs with `enforce_admins: true`.
Merging past a missing or red required check means switching that off, and it
must go back on in the same sitting. If the reviewer itself is broken, fix it or
drop it from the required contexts — never leave the branch unprotected.

### CI/CD Monitoring

```bash
gh run watch <run-id> --repo TheLightbulbCompany/lightbulb --exit-status
gh run view <run-id> --repo TheLightbulbCompany/lightbulb --json status,jobs
```

**Deployment methods:**
- **Frontend:** dev auto-deploys from main. **PROD DOES NOT** — `prod-DeployVercelProd`
  carries the same `if: github.event_name == 'workflow_dispatch'` as every other prod job
  (its `if:` sits at the BOTTOM of the ~107-line block, so a `grep -A` from the job header
  misses it). Prod frontend and backend ship together on the dispatch; merging to main
  changes nothing a customer sees. Verify by curling the live site for a string from the
  merged diff, not by reading this table.
- **Backend:** `deploy.yml` (the CDK pipeline) builds the Docker image and deploys the ECS Fargate service. `backend.yml` is **PR lint + test only** — its header says so; it deploys nothing.
- **Infrastructure (CDK):** GitHub Actions (`deploy.yml`) → `cdk synth` → `cdk deploy` per environment

**DEV DEPLOYS ON MERGE; PROD IS PROMOTED ON DEMAND.** Every prod job in `deploy.yml`
carries `if: github.event_name == 'workflow_dispatch'`, so a push to `main` runs 52 of
66 jobs (dev + assets) and **skips all 14 prod jobs**. To ship to prod, a human runs the
workflow: Actions → deploy → Run workflow, or `gh workflow run deploy.yml --ref main`.
A dispatch run deploys dev from that ref first — prod jobs still `needs:` the full dev
stage, so a promotion cannot skip dev. The `lightbulb-deploy` concurrency group is unchanged,
so a promotion queues behind an in-flight dev deploy.

### `queue: max` — what it fixed, measured

**It works.** Natural experiment over 60 deploy runs (34 cancelled), classifying each
cancellation by what was happening at that instant, split on the `queue: max` merge
(7dbc6afb7, ~03:13Z on 2026-09-08):

```
BEFORE : 22 cancellations, 19 match supersede-on-arrival   (86%)
AFTER  : 12 cancellations,  0 match supersede-on-arrival   (0%)
```

*supersede-on-arrival* = cancelled within 5s of another run being **created**. That is the
failure that killed four consecutive prod promotions on 2026-09-07/08: you are pending,
somebody merges, you die silently. **It went to zero.**

So the practical question — *"can my merge to `main` kill a colleague's pending promotion?"* —
now has a measured answer: **no.** A merge freeze around a promotion is no longer buying that
protection.

**What is NOT established.** Of the 12 post-fix cancellations, 4 are a single cluster at
03:50:57-59 (a run completed, four newer pending runs cancelled in the same second, the oldest
pending started) and 8 are ambiguous or manual. So "oldest pending wins when the slot frees" is
**n=1** — possibly real, unproven. It is also *orthogonal to merging*: if it exists, it fires
when the slot frees regardless of what anyone merges.

> **⚠️ A cancelled run with 0 jobs is NOT self-evidently an eviction.** Three sessions
> independently misread the same run that way in one night, and one of those readings went into
> this file as established before being retracted. The cheap disambiguator: check whether any
> run actually **completed** in that second. Eviction cannot fire before the slot frees — if
> nothing completed, something else cancelled it (usually a human).

> **⚠️ A run's `status` field LAGS its jobs badly — do not use it for "has this started".**
> Measured live 2026-09-08 14:27Z: run 34236826570 reported `status: queued` while **16 of its
> jobs had completed** and 2 were in progress. An earlier run read `queued` with 18 jobs
> executing. So `queued` at the run level does not mean "waiting" and does not mean "past the
> concurrency gate" — it mostly means the field has not caught up.
>
> **For liveness, count jobs, not status:**
> ```bash
> gh run view <id> --repo TheLightbulbCompany/lightbulb --json jobs \
>   --jq '[.jobs[]|select(.status!="queued")]|length'
> ```
> This lag is the likeliest root of the whole "queued means X" confusion.

**Before dispatching**, the one precondition that is safe under every hypothesis above:
```bash
gh run list --repo TheLightbulbCompany/lightbulb --workflow deploy.yml --limit 10 \
  --json status --jq '[.[]|select(.status!="completed")]|length'
```
Dispatch when that reads **0**.

That group now carries **`queue: max`** (one line, patched in from `app.ts`). It exists
because `cancel-in-progress: false` protects only a run that is already `in_progress` — a
run still PENDING used to be evicted by the next arrival, since a group holds one pending
run by default. The pipeline is runner-bound, so a promotion sits pending for minutes and
any merge in that window silently cancelled it (three consecutive promotions on 2026-09-07,
each cancelled having run zero jobs — a cancelled run with **0 jobs** is the eviction
signature; one with jobs was cancelled by a human). `queue: max` queues up to 100 runs FIFO
instead. **A promotion no longer needs a merge freeze, and re-dispatching does not jump the
queue — it just adds another run behind the first.** The single group is deliberate: prod
jobs `needs:` the dev stage, so a per-event group would let a promotion and a push deploy the
same dev stacks concurrently. Note `queue: max` is a validation ERROR alongside
`cancel-in-progress: true`, and eviction was also coalescing merge bursts — a burst now
costs one dev deploy per merge.

**⚠️ THE CONSEQUENCE: `main` IS AHEAD OF PROD BY DEFAULT.** Merging no longer ships. Prod
sits at whatever was last promoted and drifts further behind with every merge, silently and
by design. So: never infer prod's contents from `main`, and never tell an owner something is
"live" because it merged. Check the running service — `aws ecs describe-task-definition` for
the image, or the deploy run list for the last `workflow_dispatch` event. This is the same
merged≠deployed trap that already burned this repo (see the version-pin promotes that were
silently cancelled by supersede on 2026-08-23), except now it is the normal state rather
than an accident.

**Verified in production, not just in tests** (run on `8427911`, the first push after the
gate landed): `completed/success`, **13 prod-named jobs `skipped`**, 52 non-prod `success`,
plus `prod-DeployVercelProd` skipped — that one does not match a `prod`-name filter, so a
naive grep undercounts. 14 gated jobs total.

Set in `apps/infra/lib/app.ts` via `addStageWithGitHubOptions(prodStage, { jobSettings: { if: … } })`
(the package's own public API) plus ONE `JsonPatch` for `prod-DeployVercelProd` — a
`GitHubActionStep` has no `if` prop and stage `jobSettings` never reach it. Guarded by
`apps/infra/test/deploy-prod-promotion.test.ts`, which fails by job name if any prod job
loses its gate.

⚠️ **Why not GitHub Environments** (the route `cdk-pipelines-github`'s README documents as
its "Manual Approval Step"): required reviewers and wait timers are **Enterprise-only for
private repos**. This org is on **Team**, and the API confirms it —
`PUT /repos/TheLightbulbCompany/lightbulb/environments/prod` with `reviewers` or `wait_timer` returns
**422 "Please ensure the billing plan supports…"**. The `dev`/`prod` environments exist
with `protection_rules: []`; pointing jobs at them gates nothing. Do not re-attempt this
without an Enterprise upgrade (isol8 #2016 tried it and was closed).

⚠️ **`workflow_dispatch` only needs WRITE access, and `Isol8Admin` (the Forge agent's own
GitHub identity) has repo ADMIN.** So the promotion gate stops accidents, not a determined
agent — Forge can dispatch its own prod deploy. Treat this as a permissions decision, not a
solved gate.

⚠️ **`.github/workflows/deploy.yml` is GENERATED** from `apps/infra/lib/app.ts` by
`cdk-pipelines-github`, and its own drift guard only throws when
`process.env.GITHUB_WORKFLOW` matches the pipeline's workflow name — i.e. **inside the
deploy run, post-merge**. Locally, `cdk synth` silently REWRITES the file instead of
complaining. So any change touching `apps/infra/lib/` must run `cd apps/infra && npx cdk synth`
and **commit the regenerated `deploy.yml` in the same PR**. `infra-ci.yml` now enforces this
on the PR (isol8 #2004) after four separate incidents where drift blocked every dev+prod
deploy job. `npx cdk synth` does **NOT** require Docker when a local toolchain is present
(verified 2026-08-25: `CDK_DOCKER=/usr/bin/false npx cdk synth` exits 0). The two bundled
Lambdas (`bedrock-gate`, ws-authorizer) run a host-side `local.tryBundle` (`npm ci` /
`pip3 install`) and only fall back to a Docker bundling image when node/npm or python3/pip3
is missing; the backend `DockerImageAsset` hashes its build context and defers `docker build`
to the publish step, not synth. So any machine — **or an OpenClaw container** (git/gh/node/npm/
python3-pip/uv are all baked into `apps/infra/openclaw/Dockerfile`, so an agent like Forge can
regenerate + commit `deploy.yml` for infra PRs) — synths with no daemon. **OrbStack**
(`open -a OrbStack`) is only the fallback if that local toolchain is absent, or if the
`api-stack.ts` `__pycache__` EISDIR footgun silently demotes the Python authorizer synth to
Docker. (The `infra-ci.yml` drift-guard comment still claims "Docker is required … no
supported way to skip bundling" — that is overstated; the repo's own `local.tryBundle` is the
skip.)
- **Extended OpenClaw image:** GitHub Actions (`build-openclaw-image.yml`) → build on Dockerfile/`openclaw-version.json` changes → push to ECR `lightbulb/openclaw-extended`. Promotion is MANUAL — the workflow never writes the version pins; a human lands a PR flipping `dev.tag`/`prod.tag`.
- **Desktop app:** GitHub Actions (`desktop-build.yml`) → signed DMG via Tauri. Standalone workflow (NOT part of `deploy.yml`), path-filtered on `apps/desktop/**`, runs on a **self-hosted macOS runner** (`runs-on: [self-hosted, macos-desktop]`). Version is `0.4.${{ github.run_number }}`.
- **Mobile app:** `mobile-ci.yml` (PR lint/typecheck/jest + `expo export` Metro gate, self-hosted mac), `mobile-release.yml` (`eas build --local` + `eas submit` to TestFlight, on `mobile-v*` tags), `mobile-ota.yml` (`eas update --channel production` on merge to main). See the "Mobile App" section.
- **E2E journey test:** GitHub Actions (`e2e-dev.yml`) runs the Playwright journey test against dev **on-demand only** (not on every deploy); keep regularly-needed coverage hermetic/per-PR

---

## Knowledge Graph (graphify)

`graphify-out/` (gitignored — `graph.json` is ~57MB and must never be committed) holds an
**single-repo** knowledge graph: ~29.5k nodes / 63k edges / 1,018 communities over `apps/`,
`packages/`, `docs/`, `.github/` (excludes `.playwright-cli/`, `.codex/`, `.agents/`, images).
Deliberately NOT cross-repo: a merged three-repo (this repo + OpenClaw + Paperclip) graph was built and deleted
2026-08-19 — `merge-graphs` produces disjoint per-repo islands (zero cross-repo edges), and
the fork bulk (5×) buries this repo in every ranking. Keep it single-repo.

- Ask it questions: `graphify query "<question>"` (also `graphify path "A" "B"`, `graphify explain "X"`).
- Refresh after changes: `/graphify . --update` — code-only changes re-extract via AST, no LLM cost.
- `graphify-out/cache/` (~35MB) holds the paid ~1.2M-token semantic extraction of the repo's
  docs. Don't delete it: with cache warm, a full rebuild costs ~0 tokens and ~2 minutes.
- Structural hubs by degree (verified 2026-08-19, orientation only): backend `AuthContext` /
  `get_current_user`, `run_in_thread()`, `PaperclipAdminClient`, `TeamsContext`,
  `GatewayConnection`, `put_metric()`; frontend `components/teams/shared/types.ts`,
  `useTeamsApi()`, `cn()`.

---

## Architecture

### High-Level Data Flow

```
Chat:      Client --> https://api-{env}.flickmeon.ai/api/v1/messaging/* (Clerk HTTP commands)
             --> FastAPI --> lightbulb-{env}-messaging (DynamoDB; canonical, `seq`-ordered)
             --> DDB Stream --> messaging-projector λ --> AppSync Events  --> browser/mobile (live)
                           \-> EventBridge Pipe --> openclaw-work.fifo --> openclaw_work
                                --> Cloud Map DiscoverInstances --> per-user OpenClaw container
                                    (lightbulb-channel plugin ingress; native replies POST back)

REST:      Client --> https://api-{env}.flickmeon.ai (API Gateway HTTP API) --> VPC Link V2 --> ALB --> FastAPI
Machines:  Tauri node hosts / local sessions / Paperclip operators --> wss://ws-{env}.flickmeon.ai (API GW v2 WS;
             machine-only authorizer, connectionId map in lightbulb-{env}-ws-connections)
Browser:   Client --> wss://bv-{env}.flickmeon.ai (CloudFront VpcOrigin — raw WS the HTTP API can't carry)
             --> ALB browser-view listener (path-capped /api/v1/browser-view/*) --> routers/browser_view.py
                                                    |
                                        EFS /mnt/efs/users/{user_id}/  (openclaw.json, workspace-{agent_id}/, skills, media, logs)
                                          +-- state/, agents/{agent_uuid}/  = task-local NVMe volumes nested in the EFS
                                              mount; SQLite (sessions, transcripts, memory) restored from S3 at boot,
                                              replicated by litestream. NOT on EFS -- see local-sqlite/ boot script.
```

There is **no NLB and no VPC Link V1** — that architecture was replaced; see "WebSocket Architecture".

### Production Infrastructure

Turborepo monorepo (`TheLightbulbCompany/lightbulb`): `apps/{frontend,backend,desktop,infra,mobile,company}`,
`packages/shared` (see the Frontend section — it is load-bearing). The non-obvious
entries at the root:

- `paperclip/` — **GITIGNORED**, a read-only clone of *vanilla* upstream
  (`paperclipai/paperclip`). Reference only. **NOT what we deploy** — see the
  fork note under AWS CDK Stacks.
- `openclaw-version.json` / `paperclip-version.json` — the image pins. Both
  carry a `notes` field explaining what each field means and what may not be
  changed without breaking the fleet. Read it before bumping.
- `stripe-app/` — the Lightbulb **Stripe App** (restricted-API-key auth), the
  guided way for an owner to mint the read-only key they paste into Settings →
  Connections. Deliberately OUTSIDE the pnpm workspace (own lockfile, own
  `pnpm install --ignore-workspace`): it is uploaded with the Stripe CLI, not
  built by our pipeline, and pulling it into `apps/*` re-resolved ~1.3k
  lockfile lines. See its README; `stripe apps upload` is owner-gated.

### AWS Configuration

| Setting | Value |
|---------|-------|
| Account ID | `877352799272` |
| Deploy Region | `us-east-1` |
| SSO Start URL | `https://isol8.awsapps.com/start` (unchangeable residue — see the Rebrand section) |
| Account alias | `thelightbulb` |
| CLI Profile | `isol8-admin` |

---

## Backend (FastAPI + DynamoDB)

**Entry point:** `main.py` -- FastAPI app. The lifespan handler's real startup work is `_resume_wake_resize_transitions`, a one-time trial-idle reconcile sweep, and the running-gauges loop, which emits `gateway.running.count` from the containers table (`startup_containers()` is a no-op — ECS services are always-on); shutdown is a `/health`-gated bounded ~20s graceful drain with `handoff_inflight_provisions` and no longer closes a pool. Middleware worth knowing: `HostDispatcherMiddleware` rewrites requests arriving on `company[-env].flickmeon.ai` (via the `X-Isol8-Public-Host` header set by API GW parameter mapping) onto the `/__paperclip_proxy__` sentinel mount, plus `ShutdownAware`, `SecurityHeaders`, `ProxyHeaders` (VPC-CIDR-scoped), and observability middleware. A global `PaperclipApiError` handler self-heals Teams-BFF 401s by re-minting the Paperclip session cookie. CORS middleware, custom OpenAPI schema with BearerAuth.

No SQLAlchemy, no ORM — DynamoDB items are plain dicts accessed via repositories.

> **File inventories are not kept here.** They rot faster than anyone updates
> them. Run `ls` on the directory you're touching. What this file documents is
> the *why* — invariants, single-writers, and things that look wrong until you
> know the reason.

### Core (`core/`)

Flat modules: `config.py` (Pydantic settings), `auth.py` (Clerk JWT via PyJWT;
`get_current_user` → `AuthContext`, JWKS cache w/ 5-minute TTL + 15-min
max-stale fail-closed), `dynamodb.py`, `encryption.py` (Fernet),
`constants.py` (`SYSTEM_ACTOR_ID = "__system__"`), `openclaw_version.py`,
`secrets_manager.py`.

Subpackages: `billing/` (Bedrock AIPs, pricing, gate URL — see the Billing
section), `containers/`, `gateway/`, `repositories/`, `services/`, `crypto/`,
`middleware/`, `observability/`.

### Repositories (`core/repositories/`)

Mostly one repo per DynamoDB table (`marketplace_repo.py` fronts five
marketplace tables); items are plain dicts. Table names follow
`lightbulb-{env}-{suffix}` (see `database-stack.ts`).

The **Stripe Projects provisioning rail** keeps its `integrations` rows and its
Secrets Manager blobs under **`projects/{provider}`** (`integrations_repo.projects_key`)
— never the bare provider, which the OAuth rail owns and four providers share
(stripe/posthog/sentry/cloudflare). A bare-key write makes the provider look
OAuth-connected to every reader and lets an OAuth Disconnect force-delete the
Projects credentials. Readers inject only the names `provider_resources[].env_keys`
declares.

The rail itself is **backend-owned** — the platform key never leaves the backend
and no container ever holds it. `core/services/stripe_provisioning.py` is the
only module that speaks `/v2/provisioning` (platform key + `Stripe-Context:
acct_…` + `Stripe-Version: unsafe-development`, a preview contract that can
change under us); `provisioning_workflow.py` is the state machine and a
`provisioning-workflows` row is the durable memory. Four invariants hold it up:

- **No webhooks**, so workflows advance one step on every READ — the CEO agent's
  status route, the owner's page loads and clicks — under a short per-row
  claim. There is no background driver and nothing sweeps the table.
- **No idempotency contract.** A state-changing POST that times out or 5xx's is
  **never repeated**: the row goes `requires_review` with its ids kept. Only
  reads retry.
- **The catalog partition is configuration**, `STRIPE_PROJECTS_CATALOG` —
  `testing` in dev, `prod` in prod. It is a catalog selector, **not** a safety
  net: a DEV/TESTING catalog can still create real infrastructure and real
  charges.
- **The API refuses legacy test mode** (`v2_api_not_supported_in_testmode`), so
  dev verification needs a Stripe **Sandbox** key, not the legacy test key.

The CEO agent only ever sees `/api/v1/company/provisioning/u/{owner}/…`; `remove` — the
one call that deprovisions — is reachable from the owner's typed-confirmation
route and nowhere else, and teardown never reaches it (spec §3.6, proved by
`tests/unit/services/test_wipe_never_deprovisions.py`).

**The rail is OFF by default** — `STRIPE_PROVISIONING_ENABLED` (CDK env, default
`false` in both envs) refuses `start()`/`on_approved()`, freezes `advance()`,
empties the CEO agent's catalog with `reason: provisioning_unavailable`, and hides the
Connect doors + the Domain & Site panel. It stays off until Stripe ships
delegated connected-account creation + KYC (isol8, Inc. is the Connect
platform and cannot connect to itself, and `POST /v2/provisioning/*` 500s for
us). Everything else a company-Stripe binding powers — the Money tabs, the
revenue rollup, card status, the CEO agent's "Stripe connected" awareness — is plain
v1 and runs on the **API-key path**: the owner pastes a restricted key
(`POST /company/stripe/api-key`, Read on Charges/Customers/Products/Prices/
Balance/Account; test-mode keys bind too and the card says "Test mode" —
Stripe's app reviewers may only use test accounts), it lives ONLY in Secrets Manager (`company_stripe` provider;
never `openclaw.json`, `env.vars`, or EFS), and the billing row carries
`source: "api_key"`. `company_stripe_reads._addressing` is the one resolver:
`connected/created` rows → platform key + `stripe_account` header; `api_key`
rows → their key, no header. The agent's own Stripe tools are a THIRD grant
(the OAuth MCP in the container) and must never be consolidated with the key.
`stripe-app/` (repo root) is the Stripe App (restricted-API-key auth) that mints
a key with exactly those six permissions for an owner to paste — see its README.

### Container Orchestration (`core/containers/`)

| File | Purpose |
|------|---------|
| `__init__.py` | Singletons: `get_ecs_manager()`, `get_workspace()`. No pool singleton and no startup/shutdown hooks — the backend holds no per-owner connection state to create or tear down. |
| `ecs_manager.py` | Per-user ECS Fargate lifecycle. Container IP discovery is **Cloud Map** `DiscoverInstances` (`discover_ip`), **not** `describe_tasks`: every per-user service registers into ONE shared Cloud Map service via `serviceRegistries`, and the lookup is scoped to a tenant by filtering the `ECS_SERVICE_NAME` attribute. `HealthStatus="HEALTHY_OR_ELSE_ALL"` because the shared service has no `HealthCheckCustomConfig`, so ECS-registered instances report `UNKNOWN`. **Never cache a container IP across reconnects** — the service is long-lived but the task IP is ephemeral (changes on every deploy / OOM / health-restart / reprovision); a cached dead IP caused a prod outage where the pool showed "connected" and every RPC timed out. Per-user EFS access points. Service naming: `openclaw-{user_id}-{hash}`. |
| `config.py` | `write_openclaw_config()`: generates `openclaw.json` with Bedrock provider and memory plugin. |
| `workspace.py` | EFS file I/O at `/mnt/efs/users/{user_id}/`. |

### Gateway (`core/gateway/`)

| File | Purpose |
|------|---------|
| `routers/gateway_relay.py` | Raw WebSocket relay on the `bv-` door for machine clients (desktop node hosts, Paperclip operators) — one backend task pipes both sockets for the connection's life; the OpenClaw handshake and the NOT_PAIRED self-heal are its only two frame hooks. Replaces the deleted `operator_rpc.py`/`node_connection.py`. |
| `core/services/machine_auth.py` | In-process port of the `websocket-authorizer` Lambda's device-JWT and Paperclip-service-token verifiers — the relay's upgrade auth. |
| `core/services/relay_hooks.py` | Per-connection side effects the relay handler calls: node deny-list edges, the operator wake/chat-gate check, the NOT_PAIRED self-heal, trial disarm. |
| `core/services/container_rpc.py` | One HTTP POST per RPC to the plugin's `/plugins/lightbulb-channel/rpc`, `wake=False` for read-only probes. Replaces the deleted `GatewayConnectionPool` as the backend's sole way to reach a container. |

### Services (`core/services/`)

`ls apps/backend/core/services/` for the current list. The ones whose behaviour
you cannot guess from the filename:

- `user_provision.py` — `ensure_user_ready` is the canonical readiness entry
  point. It is dispatched from `GET /container/status` polls **and** from the
  Teams reconcile path (`routers/teams/deps.py`) and the Paperclip-proxy
  handshake (`routers/paperclip_proxy.py`). Pre-gate steps:
  `ensure_personal_trial_default` (auto `trial_local` row),
  `ensure_metronome_customer`, `evaluate_provision_gate`, and
  `_maybe_arm_nightly_ceo` (the poll-path self-heal only — the nightly CEO loop is
  armed DETERMINISTICALLY by the provision executor's step 0 via `arm_nightly_ceo`).
  It is armed for EVERY live owner, not just paid ones: `should_arm_nightly_ceo`
  is deliberately broader than `has_always_on_entitlement` and returns True for
  paid (active/trialing) **or an unexpired `trial_local`** — the nightly run is a
  retention lever, and it stays safe on a trial because the wake path and the
  bedrock-gate still cap-gate it. Only an EXPIRED trial (and canceled/past_due/
  no-row) is left unarmed. On a FRESH provision the Paperclip seed runs
  **inside the provision executor** (step 0 of `scripts/provision_runner.py`,
  single-flight under `try_claim_provision` — kills the duplicate-company
  race); `ensure_user_ready` seeds on the poll thread only as reconcile for
  already-running rows.
- `provision_dispatch.py` — `dispatch_provision`, the single provisioning
  funnel. A fresh claim starts the SFN watchdog execution FIRST, then runs
  attempt 0 as a backend child process for speed. The machine watches the
  row (watch loop in `service-stack.ts`) and takes the ECS leg over on
  error or after its tick backstop — custody never depends on a client poll.
- `paperclip_provisioning.py` — owns `provision_org`, which creates the generic six-agent starter roster for fresh workspaces and preserves legacy/founder rosters.
- `config_patcher.py` — EFS-safe JSON deep-merge. `openclaw.json` writers take
  the persistent `.isol8-guard` and select the lock protocol recorded in
  `.isol8-protocol` (constants + `O_EXCL` acquire live in `efs_file_lock.py`);
  backend-only files use NFS-compatible `fcntl.lockf` (not `flock`).
- `clerk_admin.py` — Clerk REST wrapper for the admin surface. New Clerk calls
  **should** go through here, but two predate it and still hit `api.clerk.com`
  inline via httpx: `routers/billing.py` and `routers/desktop_auth.py`. A
  fleet-wide change to how we call Clerk has to touch all three.
- `key_service.py` — BYO LLM keys. Stored **solely in AWS Secrets Manager**
  (`lightbulb/{env}/user-keys/{user}/{provider}`), referenced by the ECS task def;
  no DynamoDB copy is kept.
- `catalog_service.deploy_from_artifact` — the consumption-side entry point for
  marketplace deploys.
- `agent_export.py` — seller-agent → tarball snapshotter. It tars
  `workspace-{id}/` (the seller's actual content) and **never** `agents/{id}/`
  (OpenClaw runtime state — `sessions/` logs, `agent/` auth tokens). Runtime
  state is excluded *by which root it points at*, not by a filter list; the
  filters only drop dotfiles and junk dirs. See `_resolve_agent_dir`'s docstring.

### Routers (`routers/`) — all mounted under `/api/v1`

~44 flat modules plus the `routers/teams/` subpackage (the Teams Inbox BFF —
agents, approvals, feed, feedback, inbox, interactions, issues, runs, search,
settings, tree_control, work; composes the `paperclip_*` services). `ls` both
for the current set.

Worth knowing: `paperclip_proxy.py` is the per-tenant Paperclip proxy and has a
route-filter security model — see `docs/audit-2026-05-02-paperclip-routes.md`
before touching it. `debug.py` returns 403 in prod.

### Models (`models/`) and schemas (`schemas/`)

`models/` is intentionally empty — DynamoDB items are plain dicts, no ORM.
`schemas/` holds the Pydantic request/response models for the HTTP surface.

---

## Frontend (Next.js 16 App Router)

React 19, Tailwind CSS v4, Clerk auth, SWR, lucide-react icons.

**Colours/fonts are the Brand Identity Manual's tokens**, defined once in
`src/app/globals.css` (`--midnight --cream --bulb --brass`, cream/midnight steps,
four state pairs; `.dark` reverses the ground and re-points the state tokens). No
colour literal anywhere else except the six deliberate exemptions the scanner
skips — `landing.css`, `landing-v2.css`, and `components/landing/**` (marketing,
its own palette), `lib/clerkAppearance.ts` (Clerk takes hex, not `var()`),
`*Brand.ts` vendor marks, and tests (`__tests__/`, `*.test.*`, which hold
literals as fixtures) —
`src/__tests__/brandTokens.test.ts` enforces it,
`node scripts/brand-tokens-scan.mjs` lists offenders. Fonts: Source Sans 3 (UI) /
Lora (prose) / mono for code only.

**`packages/shared` (`@lightbulb/shared`) is load-bearing:** the framework-neutral
API client (`createApiClient`), the dependency-free AppSync live-event client
(`AppSyncEventsClient` + the `useLiveEvents` hook — it implements AWS's Event API
wire protocol, owns the single socket/reconnect loop, and has no publish method),
the messaging and runtime command clients (`createMessagingClient`,
`createRuntimeClient`), shared data hooks (`useAgents`, `useOnboardingStatus`,
`useChatGPTOAuth`, …) and protocol types are consumed verbatim by web AND mobile,
with platform specifics injected via a `PlatformAdapter`. No `next/*`, no
react-native, no DOM globals in that package.

### Pages (`src/app/`)

`find apps/frontend/src/app -name page.tsx` for the current routes. The two
that will mislead you otherwise:

- **`/chat`** is the whole app. Both branches mount `LiveEventsProvider` and
  render the messaging workspace: desktop the **dockable workspace shell**
  (`components/workspace/` — chat, teams, control, file-viewer, and browser
  panels, where the `chat` panel is `panels/ChatPanel.tsx` → `MessagingConversation`);
  mobile `ChatLayout` via `MobileChatShell`, wrapping `MessagingWorkspace`. Teams
  and control are in-shell panels, **not routes** — the legacy `/teams/*` URL tree
  was removed and 404s. The **browser** panel is a singleton live view of the
  container's Chrome (one Xvfb display per container); its WS deliberately
  targets `bv[-env].flickmeon.ai` (CloudFront) because the API Gateway HTTP API
  cannot pass WebSocket upgrades (`workspace/panels/browserViewUrl.ts`).
- **`/admin`** only resolves on allowed admin hosts (`admin.flickmeon.ai`,
  `admin.dev.flickmeon.ai`, `admin.localhost:3000`; overridable via
  `NEXT_PUBLIC_ADMIN_HOSTS`) and 404s everywhere else. Allowlist lives in
  `apps/frontend/src/middleware.ts`.

### Source map (`src/`)

Per-file tables drift too fast to keep here — run `ls` on the directory you're
touching. The load-bearing structure:

- `components/workspace/` — the dockable desktop shell: `panelRegistry.ts` /
  `panelComponents.ts` map panel keys → components (`PanelKind = chat | teams |
  control | file-viewer | browser`); `workspaceController.ts` +
  `layoutStorage.ts` own layout state. Start here for anything "where does X
  render on desktop".
- `components/messaging/` — **the live chat surface**: `MessagingWorkspace`
  (+ `MessagingConversation`) over `RoomPanel` / `RoomComposer` / `RoomDirectory`
  / `RoomTimeline`, with `MessagingWorkspaceContext` holding selection and draft
  state. Mounted from `workspace/panels/ChatPanel.tsx` on desktop and from
  `MobileChatShell` on mobile. Start here for anything room-shaped.
- `components/chat/` — what the browser-gateway chat island left behind:
  `MessageList` (now a read-only transcript renderer, used by the Teams run
  detail), `ProvisioningStepper` and the
  provider-setup steps. `ChatLayout.tsx` is the **mobile-only** shell.
- `components/teams/` — Teams Inbox (panels routed by `TeamsPanelRouter.tsx`
  via `PANEL_LOADERS`/`DETAIL_LOADERS`; shared types in `shared/types.ts`;
  ported-from-upstream-Paperclip modules carry a header saying so).
- `components/control/` — control panels (`ControlPanelRouter.tsx` routes; the
  live nav sets are `settings-nav-config.tsx` + `CompanyNav`'s `COMPANY_SECTIONS`).
- the rest of `components/` (`admin`, `agents`, `blog`, `channels`, `desktop`,
  `getstarted`, `landing`, `legal`, `marketplace`, `notifications`,
  `onboarding`, `settings`, `support`, `ui`) — what their names say; `ui/` is
  shadcn/ui.
- `hooks/` — `useLiveEvents.tsx` (`LiveEventsProvider`, the WEB wrapper around
  `@lightbulb/shared`'s `useLiveEvents`: owns the messaging + runtime clients,
  room-subscription claims, SWR invalidation from `runtime.resource.invalidated`,
  and — because it is the one always-mounted provider — the Tauri node bridge
  (`node:status` listen + `send_auth_token` invoke)), `useRooms.ts` (room list,
  message pages, optimistic/uncertain send, live run state),
  `useRuntimeRpc.ts` (HTTP runtime RPC), `useTeamsApi.ts` (Teams BFF fetch),
  `useProvisioningState.ts` (stepper/blocked-state machine), plus ~30 more.
- `lib/api.ts` — `useApi()`: wraps `@lightbulb/shared`'s `createApiClient`
  (`get`, `post`, `put`, `patch`, `del`, `upload`, `uploadFiles`, `syncUser`).
  New data-fetching should go through this (or `useRuntimeRpc` for runtime RPC),
  not hand-rolled `fetch`.

Clients do **not** derive a chat socket URL. `GET /messaging/live-config` returns
this environment's AppSync HTTP + realtime endpoints, region, and the caller's
opaque user-channel key; `useLiveEvents` subscribes with a Clerk token minted
from the `lightbulb-appsync` JWT template. There is no `NEXT_PUBLIC_WS_URL` any
more: `wss://ws[-env].flickmeon.ai` is reached only by the desktop host
(`LIGHTBULB_WS_URL`), and `bv[-env]` stays the browser-view raw-WS door.

### Middleware

`src/middleware.ts` -- Clerk middleware protects `/chat(.*)`, `/onboarding`, `/settings(.*)`. An admin-host gate (`decideAdminHostRouting`) also runs on every request: `/admin` paths on non-admin hosts return 404, and admin hosts straying off `/admin` redirect back to `/admin` (with `/sign-in`/`/sign-up` exempted to avoid a redirect loop). A third gate: `PAPERCLIP_HOSTS` (`company.flickmeon.ai`, `dev.company.flickmeon.ai`) bypass Clerk entirely so the `next.config.ts` beforeFiles rewrite can proxy those hosts.

---

## Messaging

The product is a multiplayer messaging workspace: humans and AI employees share
rooms (`channel` / `dm` / `group-dm`). The browser/mobile chat WebSocket protocol
that used to carry this was removed — see
`docs/superpowers/specs/2026-09-09-native-multiplayer-messaging-architecture.md`
for the frozen contracts (it is the authority; this section is orientation).

```
Browser/mobile --HTTP (Clerk)--> routers/messaging.py --> lightbulb-{env}-messaging (DynamoDB)
                                                                     |  DDB Stream (NEW_IMAGE)
                          two INDEPENDENT consumers, neither blocking the other
                                   |                                       |
                       messaging-projector λ                   EventBridge Pipe (SK begins WORK#)
                                   |                                       |
                          AppSync Events                            openclaw-work.fifo
        /ws/<workspaceId>/rooms/<roomId>/<deliveryKey>                     |
        /ws/<workspaceId>/users/<opaqueUserKey>           core/services/openclaw_work.py
                                   |                                       |
                  browser/mobile (subscribe-only)      POST /plugins/lightbulb-channel/inbound/<agentId>
                                                        on the per-owner container (gateway auth)
                                                                           |
                                            native reply --> POST /api/v1/internal/messaging/
                                                             agent-messages/<ownerId> (hook token)
```

**Canonical store.** One DynamoDB table (`lightbulb-{env}-messaging`, created in
`api-stack.ts`, NOT `database-stack.ts`) holds rooms, members, immutable messages,
idempotency receipts, read cursors, per-agent delivery cursors, the private
user-stream keys, and the `WORK#`/`LIVE#` outbox rows (those two expire via TTL;
canonical data does not). `META.lastSeq` defines order — arrival time never does.
A message write is one conditional transaction: bump `lastSeq`, put the `MSG`,
put the `IDEMP` receipt, put one `WORK#MESSAGE` row per dispatch target.

**Command path.** `routers/messaging.py` under `/api/v1/messaging/*`, all Clerk
bearer tokens; `core/services/messaging.py` is the single policy seam over
`messaging_repo`. The browser never supplies an owner id, author id, OpenClaw
session key, container address, fanout list, or publish credential — the backend
derives all of them. Retries are safe because the durable `IDEMP` row, not
DynamoDB's short-lived transaction token, is the receipt. Public JSON is camelCase.

**Live delivery.** `core/services/live_events.py` (backend, IAM SigV4) and the
`messaging-projector` Lambda publish to AppSync Events on two channels:
`/ws/<workspaceId>/rooms/<roomId>/<deliveryKey>` for room members and
`/ws/<workspaceId>/users/<opaqueUserKey>` for exactly one signed-in human. Clients are **subscribe-only** (OIDC, Clerk JWT
template `lightbulb-appsync`) and reconcile from HTTP: AppSync stores no history,
can duplicate or drop, so a gap means `GET …/messages?afterSeq=`. Revocation is
the load-bearing part: an `onSubscribe` decision cannot close an already-open
subscription, so every human membership removal **rotates the room's
`deliveryKey`** in the same transaction and writes durable `LIVE` notices
(`room.subscription.rotated` to the rest, `room.access.removed` to the removed
member). The old channel goes permanently silent. This is a security boundary.

**Agent dispatch.** The stream fans out twice, independently: the projector for
live UI, and an EventBridge Pipe filtering `WORK#` inserts into
`openclaw-work.fifo`. The projector also fans mobile push out to Expo for every
member it notified, and it sends that push **last** — a raising publish replays
the whole stream record and Expo has no `eventId` dedupe, so an earlier send
would banner every member twice. That push is also the one place **mute is
enforced server-side**: mute is client-side on every other surface, so
`member.muted` is checked there and nowhere else in the fanout.

`core/services/openclaw_work.py` drains the work queue as a **wake
signal only** — it re-reads canonical messages through `throughSeq`, rechecks
membership, wakes the owner's container, and calls the baked `lightbulb-channel`
plugin's gateway-authenticated private ingress once per targeted message, keyed
by the canonical `messageId`. The FIFO `MessageGroupId` is per room+agent, so
agents run concurrently while one agent's room stays serialized. Native replies
come back over HTTP (`POST /api/v1/internal/messaging/agent-messages/{ownerId}`,
hook token) through the same canonical writer humans use. Execution is
at-least-once, not exactly-once.

**Room turns are asynchronous (spec `2026-09-12-async-turn-contract-design.md`).** The
consumer POSTs the ingress body with a `run` contract and gets `202 accepted` in
milliseconds; the container runs the turn, publishes the run frames itself to
`/ws/<workspaceId>/rooms/…` with its task role, and reports `run.started` /
`run.heartbeat` / `run.ended {outcome, usage}` to
`POST /api/v1/internal/messaging/runs/{ownerId}/events` (hook token). The ledger row
`OWNER#<owner>/RUN#<runId>` is the only run state; a one-minute sweeper closes runs that
never started (120 s) or stopped heartbeating (90 s) with an `error` + `run_ended` pair.
Abort and steer are HTTP calls to `/plugins/lightbulb-channel/runs/{runId}/{abort|steer}`.
Control RPCs, readiness (`health`) and abort/steer are HTTP calls into the plugin;
non-room usage arrives on `POST /internal/messaging/usage/{ownerId}`.

**Listen modes.** Every agent member carries `active` / `mentions` / `observe`
(humans carry none). `select_dispatch_targets` is the one place that reads them:
`observe` never runs, an `@mention` or **any DM** is a `user_request`, plain
traffic wakes only `active` members as a `room_event`, and the source agent is
always skipped. Only those targets get `WORK#` rows — everyone else just sees the
message as history on their next turn. A fresh workspace is seeded with a
`#general` channel holding the whole starter roster, CEO `active` and the
specialists `mentions`, plus one welcome message from the CEO.

**The rule that governs changes here: no second event bus.** SQS is the durable
wakeup and nothing else; AppSync is the fanout; DynamoDB is the truth. No Kafka,
Redis, per-tenant queue, room-commands queue, or room-fanout queue.

---

## WebSocket Architecture

**No browser or mobile client opens `wss://ws[-env].flickmeon.ai` any more.** Human
commands are HTTP and human delivery is AppSync (see "Messaging"). "Remove
WebSockets" never meant every protocol that happens to use one — these remain,
because events are not a substitute for them:

1. **Machine relay (local-session only):** API Gateway v2 WebSocket API; `$connect`/`$disconnect`/`$default` are **Lambda integrations** (`apps/infra/lambda/ws-*`). `ws-message` POSTs the payload to `http://{ALB}/api/v1/ws/message` inside the VPC, which `routers/websocket_chat.py` serves **for the desktop local-session relay only** (`clientId=lightbulb-local-sessions`, enrollment-gated). Tauri node hosts and Paperclip operators moved off this door onto the raw WS relay (item 5) and no longer traverse API Gateway at all.
2. The `websocket-authorizer` Lambda accepts exactly **one** credential kind — paired-device JWTs (EdDSA, `kind=device`) — and **deliberately refuses Clerk browser sessions**. Paperclip service tokens are no longer accepted here either: operators verify their own (still HS256) service tokens on the raw WS relay instead, via `core/services/machine_auth.py`. `ws-connect` narrows further, refusing any device token whose `clientId` isn't `lightbulb-local-sessions`. DynamoDB `lightbulb-{env}-ws-connections` maps connectionId → owner; no `chat`/`native_human` rows exist.
3. Backend `/ws/*` endpoints require the `x-lightbulb-ws-internal` shared-secret header, so forged callers arriving over the public HTTP API are rejected. Relay replies go out over the Management API (HTTPS POST to `@connections/{id}`) — the only traffic left on this path is the local-session relay.
4. **Backend → container:** `core/services/container_rpc.py`, one HTTP request per RPC to the container plugin — no backend-initiated socket remains; the operator-device pairing probe (`perform_signed_connect`) was deleted with `core/gateway/operator_rpc.py`.
5. **Raw WS door:** `wss://bv[-env].flickmeon.ai` is a CloudFront distribution with a **VpcOrigin** to the ALB's browser-view listener, forwarding two path-capped routes: the browser live-view panel's unbuffered WS (`/api/v1/browser-view/*`; the Clerk JWT rides the `Sec-WebSocket-Protocol` offer) and the **machine gateway relay** (`/api/v1/gateway/*`, `routers/gateway_relay.py`) — desktop node hosts and Paperclip operators dial this door directly with a device or service token as a `token`/`clientId` query pair; one backend task pipes both the client and upstream-container sockets for the connection's life, with only a TTL presence row (`ConnectionService`) surviving in DynamoDB.
6. REST rides the API Gateway HTTP API → VPC Link **V2** → ALB, including the `company[-env]` host dispatch (`X-Isol8-Public-Host` parameter mapping → `HostDispatcherMiddleware`). (The old VPC Link V1 → NLB chain is GONE.)

⚠️ AppSync has **no application disconnect callback**, and an open subscription is
not proof that compute is needed — so container park/wake is driven by durable
timestamps and the trial-idle sweep, never by UI connectivity.

---

## Provisioning Flow

A first-time user moves from "just signed up" to "ready to chat" through one canonical path. Understanding it matters because **Paperclip wiring is only set up at provision time** — there is no PATCH that retrofits a missing Paperclip company onto a pre-existing container.

**The path is workspace-only. No NEW personal workspace is ever provisioned.** Every owner is a Clerk org. `resolve_owner_id` raises 409 `workspace_required` for any caller with no org claim on every product API (since `96143cea9`), `ensure_workspace_trial_default` 403s a `user_`-prefixed id from any caller, and `ensure_user_ready` returns on its first statement for a non-org caller — so no door can mint a `trial_local` row, a container or a Paperclip company for someone who signed up alone. Legacy personal owners from before the 2026-09-10 cutover (three of them paying) are locked out until they are migrated onto a workspace (`docs/superpowers/plans/2026-09-14-legacy-personal-owner-migration.md`); the personal legs of `_reconcile_owner_seed`, `_ensure_paperclip_for_owner`, `arm_nightly_ceo` and `provision_runner` step 0 exist only because the executor's wake path still reconciles their boxes, and go when they do.

```
Clerk sign-up  ->  workspace (Clerk org) created or joined
   └─ frontend polls GET /container/status every ~3s
        └─ routers/container.py dispatches ensure_user_ready(auth)
             └─ core/services/user_provision.py
                  1. ensure_workspace_trial_default (org-only; 403 on a user_ id)
                     + ensure_metronome_customer
                  2. evaluate_provision_gate — subscription/credits/OAuth pre-checks
                  3. asyncio.create_task(dispatch_provision(owner_id))
                       └─ SFN watchdog started FIRST; attempt 0 = scripts/provision_runner.py
                            0. _seed_owner_paperclip (single-flight under try_claim_provision)
                                 └─ paperclip_provisioning.provision_org
                                      ├─ sign up Paperclip user (Better Auth)
                                      ├─ create Paperclip company; bootstrap-admin invite/accept/approve
                                      ├─ write PaperclipCompany DDB row (status="active")
                                      ├─ seed CEO, Engineering, Finance, Growth, Analytics,
                                      │  and Customer Operations with adapter_type="openclaw_gateway"
                                      └─ persist their stable role-keyed UUIDs in starter_agent_ids
                            1. ecs_manager.provision_user_container
                                 ├─ per-user EFS access point + ECS service
                                 └─ write_user_configs → write_openclaw_config(paperclip_agents=[…])
                                       └─ openclaw.json on EFS lists the live Paperclip roster in agents.list[]
                            0b. arm_nightly_ceo — seeds the CEO's "Nightly CEO progress" routine for
                                EVERY live owner: paid, OR an unexpired trial_local (see
                                should_arm_nightly_ceo — only an EXPIRED trial is left unarmed);
                                every path to provisioned runs this executor, so this is the one door
                  (on later polls of an already-running ORG row, ensure_user_ready reconciles the
                   Paperclip seed inline; _maybe_arm_nightly_ceo there is the self-heal wrapper)
```

Key invariants:

- **`ensure_user_ready` is the canonical readiness entry point** — dispatched from `GET /container/status` polls, plus the Teams reconcile path and the Paperclip-proxy handshake. Clerk webhooks never CREATE a company — that stays gated behind readiness — but `organizationMembership.created` performs the idempotent Paperclip JOIN inline (`_ensure_paperclip_for_member`), so a member who never polls is still added to Teams.
- **Paperclip URL is NOT in `openclaw.json`** — the Paperclip↔OpenClaw binding is on the Paperclip seed-agent's `adapterConfig.url`. The container connects to Paperclip via the agent's adapter at runtime; the container's own config only knows the agent IDs and workspace paths.
- **`provision_org` is idempotent** — short-circuits on existing active rows. Member adds and duplicate signups return 409, which the caller treats as success.
- **Fresh-provision Paperclip seeding is single-flight inside the executor** (`provision_runner.py` step 0) — deliberately, to kill the duplicate-company race a poll-thread seed allowed.
- **Fresh-workspace chat starts in one canonical `#general` room.** It is created only while the room is missing or empty, with the CEO in `active` mode and the five specialists in `mentions` mode. Reconciliation never restores a removed member or resets a user's choice. Room owners/admins can set each agent's per-room mode to `active`, `mentions`, or `observe`; ordinary members see the mode but cannot change it.
- **The first normal human message remains a normal dispatch.** Stored room modes choose its targets; a post-send capture then uses that first message once to create the initial goal/project and stage `BRIEF.md`. It does not force the CEO as the sole responder.

### Legacy containers (pre-Paperclip provisioning)

Owners whose container was provisioned **before** Paperclip integration shipped have a container row but no `PaperclipCompany` row, and an `openclaw.json` that lists no agents. They get 409s from `/teams` and have no Paperclip roster to chat with.

Two fixes:

1. **Full reprovision** — `DELETE /api/v1/debug/provision` then `POST /api/v1/debug/provision`. The new poll cycle invokes `ensure_user_ready` against an empty slate.
2. **Backfill script** — a one-shot `backfill_paperclip_for_legacy_users.py` (already run against prod, since deleted) paginated the `containers` table, found owners with no Paperclip row, and called `provision_org` for each. Recover it from git history only if a new legacy cohort appears.

**`PATCH /debug/provision` cannot CREATE Paperclip state.** It wholesale-rewrites `openclaw.json` via `write_user_configs`, seeding `agents.list` from the owner's LIVE Paperclip roster — so it can wire existing agent IDs into config, but it cannot create a Paperclip company, sign up a Paperclip user, or seed agents. Config repair ≠ provisioning.

---

## Billing System

### Plan model

The public billing contract is workspace-first: `GET /billing/account` exposes server-owned `workspace_price_usd` and `ultra_price_usd`, never membership counts or per-seat pricing. Public web/mobile copy must read those fields or use neutral/loading copy; do not bake a workspace price into a client release.

| plan | Stripe price | display amount | owner |
|---|---|---|---|
| **Workspace** | `STRIPE_WORKSPACE_PRICE_ID` | `WORKSPACE_PRICE_USD` (currently 50), echoed by `/billing/account` | one workspace, unlimited members |
| **Ultra** | `STRIPE_ULTRA_PRICE_ID` | `ULTRA_PRICE_USD` (currently 150), echoed by `/billing/account` | one workspace, unlimited members |

Workspace billing is seat-free: `STRIPE_WORKSPACE_PRICE_ID` and
`STRIPE_ULTRA_PRICE_ID` are the only recurring Stripe subscription prices.

**Apple IAP no longer accepts new purchases or restores.** The authenticated `POST /billing/apple/link` endpoint is a 410 tombstone that returns before verifier or repository work; there is no Apple/mobile checkout route. Keep `POST /webhooks/apple-iap`, StoreKit 2 JWS verification against pinned Apple root certificates, the configured Apple identifiers, `billing_repo` Apple fields, GSI `apple-original-transaction-index`, entitlement teardown, and cancellation/expiration mapping until App Store Connect proves there are zero active, grace-period, billing-retry, or refunded-but-unexpired subscriptions. The mobile app retains `expo-iap` only to open Apple's subscription-management UI for a hypothetical historical subscriber; it has no SKU, purchase, restore, or link action. `POST /billing/checkout` is the only subscription purchase API.

Containers default to a 1 vCPU / 4 GB box launched on **Fargate Spot** (~70% cheaper than on-demand; `ecs_manager` falls back to on-demand when Spot capacity is unavailable — at service create and via a zero-placement timeout in the provisioning poller). The old *plan*-tier size ladder is gone, but size is not uniform: `_size_for_owner` returns `ULTRA_CPU`/`ULTRA_MEMORY_MIB` (2 vCPU / 8 GB) for **either** an `ultra_pro` plan **or** a live agent count over `COMPANY_STANDARD_PLAN_MAX_AGENTS` (10) — the latter is a RELIABILITY floor, not a billing signal, added after an 11-agent prod owner crash-looped on boot 2026-08-14 (24 SQLite DBs restoring concurrently starved the single vCPU; `database is locked` is what SQLite reports when a starved process exhausts its busy timeout). Sizing is derived at every provision/reprovision, so a hand-set size not backed by plan-or-agent-count is silently undone. Paid containers stay always-on while the subscription is active. Checkout bills the first month immediately through `POST /billing/checkout` for a workspace plan; the 14-day Stripe trial was removed 2026-07-17 (evaluation happens on the no-card free tier below). Legacy `trialing` rows created before the cutover remain valid until they convert; for those, trial-clock state is owned by Stripe (do not compute trial-days-left from `trial_end` locally).

### Free tier (no card) — `trial_local`

A first-time **workspace** (Clerk org) with no billing row gets a `trial_local` row and a container **provisioned exactly like a paid Bedrock one, except with a SINGLE per-owner application-inference profile (the `sonnet` tier)**. The AIP is the attribution + pricing unit; model pinning is enforced by the **bedrock-gate** (a request may only name an AIP in the owner's own token row). `bedrock_trial` is a **config-path mode**, never a stored `provider_choice`. Personal (non-org) signups no longer reach this at all — `ensure_workspace_trial_default` 403s a `user_`-prefixed id, so only a workspace can start a trial; the three legacy paid personal owners keep their existing rows until migrated.

The trial is bounded on TWO axes: a **$3.00/day spend cap** and a **3-day trial window** (`trial_end` stamped locally at signup; expiry → permanent `TRIAL_EXPIRED`). Daily spend resets on UTC day rollover (`provision_gate.maybe_restore_trial_bedrock`). There is deliberately **no cap-only check primitive** — a cap-only check once woke EXPIRED trials, so every door must call the full predicate (`trial_block_reason` / `reason_for_account`, cap AND expiry).

**The enforcement model — read this before changing anything here.** Every Bedrock inference call flows through the **bedrock-gate** (`apps/infra/lambda/bedrock-gate/`, a streaming Lambda fronting bedrock-runtime). Containers authenticate to it with per-owner bearer tokens (`AWS_BEARER_TOKEN_BEDROCK`; hash-stored in `lightbulb-{env}-bedrock-gate-tokens`); **owner task roles carry NO `bedrock:Invoke`** (`build_owner_bedrock_policy` is a deny-only fence), so the `exec: full` shell path AccessDenies — the gate is the only door. The gate discovers its own URL via SSM (`/lightbulb/{env}/bedrock-gate-url`, read by `core/billing/bedrock_gate_url.py`; the literal value `direct` is the fleet break-glass mode, paired with `scripts/bedrock_gate_break_glass.py` which re-grants direct IAM — see `docs/runbooks/bedrock-gate-break-glass.md`).

- **The valve (primary)** is the gate's pre-flight **reservation**: admit only if `spent + holds + ~$0.30 estimate < $3.00`, place the hold, forward verbatim (byte-identical eventstream — load-bearing for Claude Code), settle Bedrock's own reported usage on completion. Over budget → 429 before a single token is generated. Overshoot is bounded by in-flight requests (cents), so the cap is a TRUE **$3.00/day** — no enforce-early margin. **Lockstep constants — NEVER move one alone:** `CAP_MICROCENTS` has three copies (`trial_cap.py`, meter `index.py`, gate stack env) and `RESERVE_ESTIMATE_MICROCENTS` (300_000) has two (`trial_cap.py`, gate stack env); every door predicting admission must subtract the reserve or a dead zone opens.
- **The auditor + backstop** is the invocation-log **meter Lambda** (`apps/infra/lambda/bedrock-usage-meter/`). It sees EVERY call from AWS's own records — including aborted/crashed streams the gate cannot settle and any break-glass direct traffic — and reconciles via the shared `requestId` dedup table (gate = cashier writes first; meter counts only what the gate missed, emitting `meter.reconciled_gateway_miss`). It remains the **sole Metronome writer** (paid billing never touches the gate). If the counter crosses the cap anyway it fires the backstop kill: deny the gate token + `desiredCount=0` (+ the now-vestigial IAM revoke, which still bites in break-glass direct mode).
- **The gates** (`gate_chat`, the Teams guards, `ecs_manager.start_user_service`) are read-only checks on that counter. **They are UX** — the limit card instead of a failed run. The historical "no pre-send reserve" rule applies to the BACKEND (off the inference path, where a reserve is unenforceable guesswork — one existed, caused a 15-defect review, and was deleted); the gate's reservation is different in kind: it lives on the only path in/out and settles against its own atomic counter.
- **The container config bounds** (`agents.defaults.timeoutSeconds`, `runRetries.max`, `subagents.maxConcurrent`, `tools.loopDetection`) shrink the blast radius of a runaway loop. They are **insurance, not the fence**. Note `timeoutSeconds` defaults to **48 HOURS** upstream and the agentic loop has no step/tool-call/token cap at all.

Trial containers scale to zero when idle and wake on the next `/container/status` poll (gated on the full trial predicate). The frontend stops that poll when a tab has been open/idle a while — a fresh user interaction (click) is what resumes polling and triggers the wake, so a long-open tab shows "Agents asleep" until someone clicks. Trial owners are never ingested to Metronome — they have no Stripe customer, and Metronome is a ledger, not a valve (its threshold alerts poll on a ≥3-minute tick and it never blocks usage); paid credit balances are gated per-request by the gate against the `metronome-fast-gate` row.

**Load-bearing constraints (verified 2026-07-15/16; do not re-litigate without new facts):** OpenClaw sends **no `maxTokens`** on the adaptive path — a small injected cap silently truncates thinking, so the gate clamps only ABOVE model-max (128K Sonnet/Opus, tamper ceiling). Bedrock invocation-log **text delivery cannot be disabled** without blinding the meter entirely (the modality flags gate whole records; bodies — which have leaked plaintext Paperclip keys/JWTs — are bounded by 3-day log retention + DLQ redaction instead). A streaming Lambda **cannot be a fence by itself** (not reliably notified of client aborts; can't guarantee its own final write) — which is why the meter backstop + deny-flag kill stay, per the staff-review + AWS-docs research in `docs/superpowers/specs/2026-07-15-bedrock-gate-design.md`.

### Paid provider paths

Three provider paths the user / org chooses between (stored on the per-owner `billing_accounts` record, not per-user):
1. **Bedrock managed** (`bedrock_claude`) — platform-billed Bedrock inference, draws from per-owner Bedrock credits.
2. **ChatGPT OAuth** (`chatgpt_oauth`) — available to personal owners AND org workspaces (see `routers/oauth.py` module docstring; the org admin who completes the device-code flow is the recorded `oauth_connector_user_id`, and only they can manage/re-select it — one ChatGPT account ↔ one container); user signs in to their own OpenAI account.
3. **BYO API key** (`byo_key`) — owner provides their own Anthropic / OpenAI key, stored **in AWS Secrets Manager** (`lightbulb/{env}/user-keys/{user}/{provider}`, referenced by the task def; no DynamoDB copy — `key_service.py`). Note: `ENCRYPTION_KEY`/Fernet no longer protects BYOK; it protects ChatGPT OAuth tokens + Paperclip credentials.

The provision gate (`core/services/provision_gate.py`) blocks chat when subscription is not active, OAuth tokens are missing, or Bedrock credits are exhausted. The frontend renders a structured "blocked" state instead of an indefinite spinner.

### Usage Flow

1. A room run's `run.ended` callback, or the plugin's `agent_end` hook for every other run, carries the session's token counts
2. `record_usage()` writes DynamoDB atomic counters (owner + per-member) — **analytics only**; billing flows through Metronome, whose sole writer is the meter Lambda
3. Chat gating is `gate_chat` (provision gate) + `trial_block_reason` (trial cap/expiry) + the Metronome fast-gate for paid credits — there is no `check_budget()` and no Stripe Meters usage reporting

### Container Update System

- **Track 1 (silent):** Config patches via EFS deep merge + OpenClaw file watcher (chokidar polling). Deep merge preserves OpenClaw's runtime additions (`meta`, `commands`, `identity`).
- **Track 2 (notification):** Image/resize updates queued in DynamoDB, user picks when via banner.
- **Admin endpoints:** `PATCH /container/config/{owner_id}`, `PATCH /container/config` (fleet)

### Config Patching Notes

- `CHOKIDAR_USEPOLLING=true` env var required for EFS NFS compatibility
- `openclaw.json.lock` starts as the backend's legacy persistent `fcntl`
  sidecar. Boot recovery takes the persistent `.isol8-guard`, publishes a
  resumable `.isol8-protocol` transition, removes the legacy inode, and then
  backend hot patches use OpenClaw's ephemeral `O_EXCL` protocol (constants
  live in `core/services/efs_file_lock.py`). Never unlink the sidecar outside
  boot recovery while holding that guard. After the transition, boot recovery
  also removes an ephemeral sidecar abandoned by a crashed backend or the
  stopped prior OpenClaw task.
- Backend-only EFS files use `fcntl.lockf()` (not `flock()` -- incompatible
  with NFS).
- `chown` to UID 1000 after write (OpenClaw runs as node)
- `env.vars` is re-asserted at every provision from Secrets Manager by
  `provisioning_landing.build_provisioned_env` — same class as the
  `integration_mcp_servers` merge. A reprovision overwrites openclaw.json
  wholesale, so anything not re-injected there is simply dropped. Channel bot
  accounts (`channels.*.accounts`) and their route bindings are the third
  carried-forward block (`ecs_manager._provision_channel_accounts`) — the
  platform Slack app relies on it.

---

## Marketplace

Sellers publish custom OpenClaw agents; buyers discover them and deploy a copy into their own container. Publishing and provisioning are **decoupled** — artifacts in S3 are immutable + versioned, so an old container deploying an old listing is safe.

### Status state machine

`draft → review → published → retired | taken_down`

- **draft** — seller's working row, artifact may or may not be uploaded yet.
- **review** — seller has submitted; awaits admin approval.
- **published** — buyer-visible. Publishing a new version atomically retires the previous published version (`marketplace_service.publish_new_version` via `marketplace_repo.transact_publish_flip`, a `TransactWriteItems`).
- **retired** — no longer searchable; previously bought entitlements still resolve.
- **taken_down** — no longer searchable, and the takedown cascade REVOKES every buyer's license (`takedown_service` → `set_license_revoked`); previously bought entitlements do NOT survive a takedown.

### Seller publish pipeline

```
POST /api/v1/marketplace/listings                       (create draft row in DDB)
POST /listings/{id}/artifact-from-agent                 (snapshot EFS agent → S3)
   └─ agent_export.export_agent_from_efs
        ├─ _resolve_agent_dir — resolves workspace-{id}/, NEVER agents/{id}/
        │                       (that root is OpenClaw runtime state, not seller content)
        ├─ _build_tarball   — filters .git, __pycache__, node_modules, .DS_Store, dotfiles
        ├─ _scan_tarball_for_safety_flags → marketplace_safety.scan (credential / PII patterns)
        ├─ extract_agent_slice              — agent + plugins + selected MCPs/skills + cron jobs,
        │                                     scrubbed of secrets
        └─ marketplace_service._upload_artifact_to_s3
              s3://$MARKETPLACE_ARTIFACTS_BUCKET/listings/{id}/v{n}/
                ├─ workspace.tar.gz    (deterministic; mtimes zeroed)
                ├─ manifest.json       (file count, safety flags, agent_id)
                └─ openclaw-slice.json (slice to be deep-merged into buyer's openclaw.json)
POST /listings/{id}/submit                              (draft → review)
```

`.projects/` and `.stripe` are named in `_SKIP_DIR_NAMES` as a **backstop for workspaces dirtied by the retired Stripe-CLI path**. The Stripe Projects rail is backend-owned and writes nothing to EFS except `env.vars` in `openclaw.json`, so there is nothing on it for an export to leak — the exclusion is structural, and the named entries only cover leftovers and hand-copied debris.

**Founder-seeded Friday is unpublishable** — the `FRIDAY_PUBLISH_ERROR_MSG` guard in `agent_export.py` blocks exporting an owner's Friday workspace, which would leak owner-keyed content. The portable Friday and Atlas marketplace packages instead live under `apps/backend/marketplace/curated-agents/` and are ingested by `scripts/ingest_curated_agents.py`.

### Admin surface

```
GET  /api/v1/admin/marketplace/listings         (list review-state rows)
POST /admin/marketplace/listings/{id}/approve   (review → published, with version transition)
POST /admin/marketplace/listings/{id}/reject
POST /admin/marketplace/listings/{id}/takedown  (+ GET /takedowns; cascade revokes licenses)
```

Plus purchase refunds (`marketplace_purchases.py`), a republish flow for
taken-down/retired listings, and seller profiles (`marketplace_sellers.py`
+ the `marketplace-sellers` table).

### Buyer flow

```
GET  /api/v1/marketplace/listings/search?q=…       (60s in-process cache, tokenized scoring)
GET  /marketplace/listings/{listing_id}            (DDB row + manifest; slugs are NOT unique)
POST /marketplace/listings/{listing_id}/deploy
   ├─ entitlement check (free listing or owns purchase row)
   ├─ fetch workspace.tar.gz + openclaw-slice.json from S3
   └─ catalog_service.deploy_from_artifact
        ├─ generate new agent_id (agent_<uuid>)
        ├─ untar into EFS at workspace-{new_agent_id}/
        ├─ deep-merge slice into buyer's openclaw.json
        ├─ remap cron job IDs + agent references
        └─ requirements detection → `needs_config_patch` in the deploy preview
```

**Agent-driven hires ride an approval rail:** `GET /marketplace/u/{owner_id}/recommend` (container-authed) files a pending `hire_agent` approval via `marketplace_hire.create_hire_approval`; the owner approving it in Teams triggers `marketplace_hire.deploy_for_approval`.

### Paid listings

`marketplace_purchases.py` handles Stripe Checkout for paid listings + the `/webhooks/stripe-marketplace` webhook that flips the purchase row to `entitled=true` (the webhook serves TWO Stripe registrations — platform + Connect — with two signing secrets). `marketplace_payouts.py` manages seller payout accounts.

---

## Environment Variables

### Backend (.env)

**`apps/backend/core/config.py` is the source of truth** — the `Settings` class
lists the variables and their defaults. (Beware: quite a few modules also read
`os.getenv` directly outside `Settings` — the `OPENCLAW_*` version overrides,
`workspace.py`, `billing_service.py`, `provision_dispatch.py`, and others.)
Only the ones with a non-obvious constraint are worth writing down here:

| Variable | Constraint |
|----------|-----------|
| `ENCRYPTION_KEY` | Fernet key protecting ChatGPT OAuth tokens + Paperclip credentials (NOT BYOK — those live in Secrets Manager). Rotating it orphans every stored token. |
| `CLOUD_MAP_SERVICE_ARN` | Registration target — the shared Cloud Map service every per-user ECS service attaches to via `serviceRegistries`. |
| `CLOUD_MAP_NAMESPACE_NAME` + `CLOUD_MAP_SERVICE_NAME` | Discovery side — `DiscoverInstances` takes *names*, not the ARN, so both are needed on top of the ARN. Unset ⇒ `discover_ip` returns None and every container looks unreachable. |
| `STRIPE_WORKSPACE_PRICE_ID` | One recurring Workspace Price. Its display amount is `WORKSPACE_PRICE_USD`, returned by `/billing/account`. |
| `STRIPE_ULTRA_PRICE_ID` | One recurring Ultra Price. Its display amount is `ULTRA_PRICE_USD`, returned by `/billing/account`. |
| `MARKETPLACE_LISTINGS_TABLE` | Must be set — there is NO disable gate; marketplace routers mount unconditionally and an empty value means runtime errors, not a designed off-switch. |
| `AGENT_EMAIL_DOMAIN` | Empty disables agent-email minting entirely. Derived per-env in `lightbulb-stage.ts` from `PLATFORM_MAIL_DOMAIN` — the **mail** domain, never the product domain: prod gets the apex, dev gets it `dev.`-prefixed. Agent addresses are `{agent}@{slug}.{AGENT_EMAIL_DOMAIN}`: `{agent}@{slug}.thelightbulb.company` in prod, `…@{slug}.dev.thelightbulb.company` in dev. `flickmeon.ai` carries no mail at all. |
| `CORS_ORIGINS` | Comma-separated. Note the API Gateway HTTP API handles CORS preflight at the gateway on the REST path — its allow/expose headers must match FastAPI's. |

Everything ECS/EFS/WS/table-name-shaped is set by CDK — don't hand-set them.

### Frontend (.env.local)

| Variable | Purpose |
|----------|---------|
| `NEXT_PUBLIC_API_URL` | Backend REST API URL **including `/api/v1`** (the chat WS URL is derived from it; `NEXT_PUBLIC_WS_URL` overrides) |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Clerk publishable key |
| `CLERK_SECRET_KEY` | Clerk secret key (server-side) |
| `GITHUB_RELEASES_TOKEN` | Fine-grained PAT (contents: read on TheLightbulbCompany/lightbulb; fine-grained PATs pin repos by id, so it survived the rename) for the desktop download + auto-updater routes — the repo is private |

**API URL by environment** (there is no staging environment — `app.ts` creates dev + prod only).
The root domain is a per-environment parameter mid-rebrand; `apps/infra/lib/domains.ts` is the truth:
- Local: `http://localhost:8000/api/v1`
- Dev: `https://api-dev.flickmeon.ai/api/v1`
- Production: `https://api.flickmeon.ai/api/v1`

---

## AWS CDK Stacks

Located in `apps/infra/lib/stacks/`. Entry point: `apps/infra/lib/app.ts`. Per-environment stage class: `lightbulb-stage.ts`. Stacks deliberately pass secret *names* (not `ISecret`) and KMS key *ARNs* (not objects) between stacks to avoid cross-stack KMS auto-grant cycles — see comments in `service-stack.ts`.

| Stack | Purpose |
|-------|---------|
| `network-stack.ts` | VPC (public/private subnets, NAT), ALB, security groups. (No NLB — the old WS chain is gone.) |
| `auth-stack.ts` | Secrets Manager secrets (Clerk, Stripe, encryption keys) + KMS key |
| `database-stack.ts` | The core DynamoDB tables (`lightbulb-{env}-{suffix}`) **and the Paperclip Aurora Serverless v2 cluster** (Postgres, 0–4 ACU, Data API). Four tables live elsewhere: `ws-connections` AND `messaging` (api-stack), `bedrock-gate-tokens` (gate stack), `bedrock-invocation-dedup` (meter stack). |
| `container-stack.ts` | ECS cluster for per-user OpenClaw containers, EFS filesystem, Cloud Map namespace + the one shared Cloud Map service all per-user services register into, container task-definition scaffolding |
| `service-stack.ts` | Backend FastAPI service on ECS Fargate: task definition (Docker image from ECR), service, target-group wiring, DynamoDB/secrets IAM, the SFN provision watchdog, and the marketplace artifacts S3 bucket. **Autoscaled 2-4 on CPU** (`desiredCount: 2`, `scaleOnCpuUtilization` targeting 60%) — sub-project D moved the machine WebSocket relays' remaining per-connection state (node/operator sockets, RPC waiters) out of process and onto DynamoDB (TTL presence rows, claim leases), so a second+ instance shares load instead of fighting an in-process map. Rolling deploys run at `maxHealthyPercent: 200`, so N is transiently one generation higher on every deploy — which is why the provisioning/bootstrap claim-leases exist. |
| `api-stack.ts` | Both front doors: the HTTP API (REST) with VPC Link **V2** → ALB + custom-domain host dispatch (`X-Isol8-Public-Host`) + Route53 records, AND the WebSocket API with **Lambda integrations** (`ws-connect`/`ws-disconnect`/`ws-message`), the machine-only Lambda authorizer, the `ws-connections` table, and the `bv-` CloudFront VpcOrigin distribution for raw WS. **Also the whole messaging plane** (it grew here, not in `database-stack.ts`): the `messaging` table + stream, the AppSync **Event API** with its `rooms`/`users` channel namespaces and OIDC-subscribe/IAM-publish split, the `messaging-projector` Lambda, and `openclaw-work.fifo` + the EventBridge Pipe that feeds it. |
| `bedrock-gate-stack.ts` | The streaming Lambda fronting bedrock-runtime — the enforcement fence for all Bedrock inference (+ the gate-tokens table). See the Billing section. |
| `bedrock-meter-stack.ts` | Invocation-log meter Lambda — the auditor + backstop kill, and the sole Metronome writer (+ the invocation-dedup table). |
| `paperclip-stack.ts` | Per-tenant Paperclip BFF — Fargate service, Cloud Map service discovery, ALB host route (the Aurora cluster comes from database-stack as a prop). Cross-stack KMS-cycle posture documented inline. See `apps/infra/paperclip/RUNBOOK.md`. |
| `ses-inbound-stack.ts` | Inbound agent email (SES → Lambda → backend). |
| `dns-stack.ts` | HostedZone lookup + ONE wildcard ACM cert for the environment's root domain (`*.flickmeon.ai`; it is a cross-stack CFN Export, so a replacement cert is created alongside and consumers migrated, never edited in place). Route53 records live with their consumers (mostly api-stack). |
| `observability-stack.ts` | CloudWatch dashboards, alarms, log retention |

`apps/infra/openclaw/` is a sibling directory (not a stack) containing the extended OpenClaw Dockerfile + README. Built by `.github/workflows/build-openclaw-image.yml` and pushed to ECR `lightbulb/openclaw-extended`. The tag we run is pinned in `openclaw-version.json`.

**Paperclip is a fork too — verify against the fork, not the in-repo clone.** Like OpenClaw, Paperclip has a vanilla-vs-fork split. The gitignored in-repo `paperclip/` clone tracks **vanilla upstream** (`paperclipai/paperclip`) and is a *read-only reference only*. What we actually deploy is the **Isol8 fork `TheLightbulbCompany/paperclip`**, checked out at **`~/dev/paperclip-fork`**. Releases are cut from **tags** (`v2026.813-isol8.<n>`, built from branch `isol8/2026.813-circuit-breaker`; current pin v2026.813-isol8.6); rollback refs are `isol8/2026.8-circuit-breaker` + tag `v2026.8-isol8.6`. The fork carries ~15 patches in **real source** (circuit breaker, agent-delete FK sweep, webhook auth hardening, self-healing gateway runs, `PAPERCLIP_SKIP_BUILTIN_AGENTS`, …) — `paperclip-version.json`'s `notes` field is the authoritative enumeration. Its own CI builds `ghcr.io/thelightbulbcompany/paperclip` (private GHCR, pinned by **digest** in `paperclip-version.json`), which `apps/infra/paperclip-extended/Dockerfile` re-tags into ECR `lightbulb/paperclip-extended` (via `.github/workflows/build-paperclip-image.yml`) so per-tenant ECS tasks can pull it with execution-role creds. **When you need to confirm what the deployed Paperclip BFF actually returns (route projections, run/liveness fields, watchdog logic), grep `~/dev/paperclip-fork`, not the vanilla `paperclip/` clone** — they can diverge. Treat both as outside this repo — do not commit either into it.

---

## Desktop App

Tauri 2 app at `apps/desktop/`. Bundle ID: **`company.thelightbulb.desktop`** (shipped 2026-08-30 — a fresh install relative to the old `co.isol8.desktop`; devices re-pair). Product name: **Lightbulb**; crate/binary `lightbulb-desktop`; keychain service and app-support dirs follow the bundle id; default docs folder `~/Lightbulb`; env vars `LIGHTBULB_API_BASE`/`LIGHTBULB_WS_URL`/`LIGHTBULB_CALLBACK_URL`. `LIGHTBULB_WS_URL` is compile-time-only (`option_env!` in `lib.rs`, set per env by `desktop-build.yml`) and now points at the `bv[-env]` **machine gateway relay** (`wss://bv[-env].flickmeon.ai/api/v1/gateway/ws`), not the old `ws[-env]` API Gateway door — it seeds the Mac node client's URL directly; the CLI node's loopback proxy instead reads the relay URL live from `/desktop/pair`'s `relayUrl` field (`gatewayUrl` there stays on `ws[-env]` for the local-session relay).

- Rust host at `apps/desktop/src-tauri/src/`: `lib.rs`, `main.rs`, `bootstrap.rs`, `device_token.rs`, `file_access.rs`, `keychain.rs`, `node_client.rs`, `node_invoke.rs`, `notify.rs` (native macOS notifications over `UNUserNotificationCenter` via `objc2-user-notifications` — NOT `tauri-plugin-notification`, whose `NSUserNotificationCenter` backend is deprecated and never registered the app; the webview calls the `native_notify` command and gets clicks back as `notification:clicked`), `pair_client.rs`, `pair_state.rs`, `proxy.rs`, `screen.rs`, `subprocess.rs`, `tray.rs`
- Embedded web view: dev flavor points at `https://dev.flickmeon.ai/chat`; the prod job **patches the URL to `https://flickmeon.ai/chat`** (and the updater endpoint) at build time in `desktop-build.yml`
- Sidecar binaries (`lightbulb-browser-service` externalBin, plus per-arch `node` and `openclaw-host`) are NOT checked in — `scripts/vendor-sidecars.sh` populates `src-tauri/bin/` at build (supports aarch64 + x86_64; CI builds aarch64 only)
- Protocol handler: `lightbulbai://` deep links (via `tauri-plugin-deep-link`); single-instance lock (`tauri-plugin-single-instance`)
- Signed DMG for macOS (arm64), built by the standalone `.github/workflows/desktop-build.yml` on a self-hosted macOS runner (label `macos-desktop`) — dev + prod jobs, auto-publishing `desktop-dev-v0.4.*` / `desktop-v0.4.*` GitHub Releases consumed by the Tauri auto-updater
- Auth: `POST /api/v1/auth/desktop/sign-in-token` (Clerk sign-in-token bridge, `routers/desktop_auth.py`) + the frontend's `/auth/desktop-callback` page; device pairing via `POST /api/v1/desktop/pair` / `DELETE /desktop/devices/{id}` (`routers/desktop_pair.py`). There is no device-code flow.
- Requirement driver: WebAuthn/passkey support (see `project_desktop_app_tauri` memory)

---

## Mobile App

Expo iOS app at `apps/mobile/` (`@lightbulb/mobile`): Expo SDK 57 / React Native 0.86, expo-router file-based routing. Scheme `flickmeon`, `applinks:flickmeon.ai`; the **bundle id is `co.isol8.mobile` and is staying** (decided 2026-09-07 — see the Rebrand section; it is bound to App Store record 6788944100 and a rename strands the app). Expo project: owner `thelightbulbcompany`, slug `isol8-mobile` (unrenameable — see the Rebrand section), projectId unchanged; per-device storage keys are `lightbulb.*`.

- **Reuses the web protocol via `@lightbulb/shared`** — the same `createMessagingClient`/`createRuntimeClient` HTTP clients and the same `AppSyncEventsClient`/`useLiveEvents` live transport (mounted by `src/providers/live-events.tsx`, the mobile twin of the web `LiveEventsProvider`), with platform specifics in `src/lib/adapter.ts` (`PlatformAdapter`). Env via `EXPO_PUBLIC_API_URL` / `EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY`, baked per `eas.json` profile (dev → api-dev.flickmeon.ai, production → api.flickmeon.ai).
- Routes: `(auth)/sign-in`; `(app)/chat`, `agent/[agentId]` (a room conversation over the messaging client, NOT a gateway socket), `settings`, `teams/*`, and a control tree mirroring the web panels (overview, agents, channels, integrations, llm, marketplace, sessions, skills, usage).
- Auth: Clerk Expo + `expo-apple-authentication`, token cache in `expo-secure-store`.
- Push: `PushRegistration.tsx` gets an Expo push token → `POST /devices {token, platform, deviceId}`; unregister `DELETE /devices/{deviceId}`.
- **Apple subscriptions:** no new StoreKit purchase/restore/link surface or SKU. Personal/no-workspace billing gates open `flickmeon.ai/onboarding` so the user creates or selects the billed workspace first; subscription purchase then uses the web app's sole `POST /billing/checkout` route. `expo-iap` remains only for `deepLinkToSubscriptions` so a hypothetical historical Apple subscriber can manage cancellation.
- CI/release: `mobile-ci.yml` (PR: shared+mobile typecheck/lint/jest + `expo export` as the Metro-resolution gate, self-hosted mac); `mobile-release.yml` (`mobile-v*` tag → `eas build --local` → `eas submit` to TestFlight); `mobile-ota.yml` (merge to main → `eas update --channel production`). `runtimeVersion.policy: "fingerprint"` guarantees an OTA whose native fingerprint doesn't match the installed binary is never delivered.
- **Gotcha:** Metro inlines `EXPO_PUBLIC_*` at bundle time and `eas update` does NOT read eas.json build env — a no-env export once shipped an OTA with an empty Clerk key (see the comment block in `mobile-ota.yml`).
