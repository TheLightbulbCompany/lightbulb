# AGENTS.md

Guidance for Codex and other coding agents. This is a copy of `CLAUDE.md`; edit both together. If they disagree, `CLAUDE.md` wins.

## What this repo is

**Spark is the only product** (founder, 25 Nov 2026). Spark is a voice assistant for the Mac. The Mac app itself is
built in a separate repo, `TheLightbulbCompany/lightbulb-os`. This repo holds what Spark needs on the server side:

- **The website** (`apps/frontend`, Next.js 16 on Vercel): Spark's page, invites, the download, billing pages, the
  admin console, legal pages, and the pages for the network CLI.
- **The backend** (`apps/backend`, FastAPI on ECS Fargate): Spark's account, referral, billing and voice routes.
- **The infrastructure** (`apps/infra`, AWS CDK): the single source of IaC truth.

The phone receptionist, the Meet bot and the old agent-messaging platform (Hermes on AgentCore, Paperclip, rooms,
AppSync, the marketplace, agent mail, the openrouter-gate, the Tauri and iPhone apps) are deleted from the backend,
infra and CI (#5396). Git history is the record. Do not rebuild on them. Old names in the code are leftovers, not a map: read the code.

The network CLI (`TheLightbulbCompany/cli`) is kept. This repo only serves its pages and installer.

## Critical rules

- **This repo is PUBLIC.** No secrets, customer names or data, or phone numbers in a commit, PR, doc or test fixture.
  `secret-scan.yml` runs on every PR and every push to `main`.
- **Never change production by hand.** Every change to code, infrastructure or environment variables goes through
  git and a pipeline (CDK, GitHub secrets, Secrets Manager).
- **Deleted paths 404. Nothing redirects** (founder, 25 Nov 2026). When you delete a page or route, do not add a
  redirect for it.
- **No migrations.** No backfills or compatibility layers: fix forward.
- **If tests fail, your change is likely the cause**, even if the error is in a file you did not touch.
- **Never use `sleep` or `tail` in bash.** Use `gh run watch` and the Read tool.
- **Always watch your runs after you push.**
- **Delete over rewrite.** Plans and one-shot scripts are deleted once they have done their job; git history
  keeps them.

## Monorepo

Three independent apps: `apps/frontend` (pnpm, from the repo root), `apps/infra` (npm, its own
`package-lock.json`), `apps/backend` (uv).

```bash
pnpm install                                    # website deps, from the repo root
cd apps/infra && npm ci && npx cdk synth        # CDK
cd apps/backend && uv sync && uv run uvicorn main:app --reload --port 8000
cd apps/frontend && pnpm run dev                # localhost:3000
cd apps/backend && uv run pytest tests/ -v
cd apps/frontend && pnpm test
```

## The website (`apps/frontend`)

- **`/` and `/os`** are Spark's page (`src/app/os/`). `/os` stays because invite links `/os?ref=CODE` point there
  (`OsInvite.tsx`). The price line comes from the backend's public `GET /os/config`; never hardcode a price.
- **Download:** `next.config.ts` points `/download/spark` and the update feed `/spark/appcast.xml` at the `spark`
  GitHub release of this repo (Windows builds: the `spark-windows` release).
- **`/os/billing`** is the page Spark's billing links come back to.
- **Admin console:** on an admin host (`admin.`, `admin.dev.`) the admin lives at the root, and `/admin*` paths
  404 on every host (`src/middleware.ts`). The Spark tab is `src/app/admin/spark/`. The host gate is cosmetic: the
  backend's `require_platform_admin` admits only the Clerk user ids in `PLATFORM_ADMIN_USER_IDS`.
- **The network CLI's pages:** `/network`, `/j`, `/docs`, and `/install.sh`, which redirects to the CLI repo's
  latest installer.
- Legal pages (`/terms`, `/privacy` and the rest), `/support` (a mailto link to `support@thelightbulb.company`; no form, and the backend sends no mail), `/sign-in`, `/sign-up`.
- Marketing facts for Spark: `.agents/product-marketing.md`. Banned copy words:
  `src/content/__tests__/bannedCopy.ts`.
- Colour: read `src/app/globals.css` before you describe a colour; no colour literals outside the exemptions in
  `src/__tests__/brandTokens.test.ts`. `DESIGN.md` holds the design tokens.
- The browser calls the API same-origin at `/api/v1/*`; `next.config.ts` rewrites it to `API_ORIGIN` (server-side,
  no path): production `https://api.thelightbulb.company`, preview and the `dev` environment
  `https://api-dev.thelightbulb.company`. Server code uses `apiOrigin()` from `src/lib/apiOrigin.ts`.

## The backend (`apps/backend`)

Spark's routes are `routers/os.py` and `routers/os_billing.py`, mounted at `/api/v1/os`:

- `GET /config` (public: price and trial), `GET` and `DELETE /account`, `POST /referral`, `POST /event`,
  `POST /log` (typed lines and the work log, into the Spark store), `POST /channel`.
- `POST /voice-session`, `GET` and `DELETE /voice-session/{id}`: GPT-Live WebRTC sessions
  (`core/voice/os_voice.py`). The Mac's audio goes straight to OpenAI. The backend holds a sideband that meters and
  ends the session and, when `SPARK_TRANSCRIPTS_ENABLED` is set, saves the text transcript (never audio), kept 90
  days by TTL.
- `GET /checkout` and `GET /portal`: Spark's billing links. They take no Clerk session; they carry a short-lived
  token (`core/services/os_billing.py`) and answer 303 to Stripe.

A Spark account is a few attributes on the person's `users` row (see the `os.py` docstring). It is keyed on the
person, never on an organisation. Auth is Clerk JWT (`core/auth.py`). DynamoDB items are plain dicts; there is no
ORM. `core/config.py` holds the settings.

Besides those, the backend has only: the Stripe webhook (`POST /api/v1/billing/webhooks/stripe`, Spark
subscriptions), the Clerk webhook (`POST /api/v1/webhooks/clerk`: `user.created` saves the person; `user.deleted`
cancels their subscription and deletes their voice data), the admin routes (`GET /api/v1/admin/me`, `GET /api/v1/ops/spark`), `/health`, and the voice-session
sweeper (`os_voice_sweeper`, started in `main.py`'s lifespan).

`PAPERCLIP_SERVICE_TOKEN_KEY` is a leftover name, but it is live: it signs Spark's checkout and portal links.

Re-runnable ops tools in `apps/backend/scripts/`:

- `spark_store_check.py`: write, read back and delete a throwaway person's objects on dev (run by a person with dev credentials, not CI).
- `spark_transcripts.py`: print recent saved transcripts, with each person shown as a hash. Read-only.

## The infrastructure (`apps/infra`)

`apps/infra/lib/app.ts` is the entry; `lightbulb-stage.ts` is the per-environment stage. There are two
environments, dev and prod, and no staging. `apps/infra/lib/domains.ts` is the truth for hostnames; the product
domain is `thelightbulb.company` in both environments.

The stacks (`apps/infra/lib/stacks/`): network (VPC, ALB; no NAT), dns (hosted zone, wildcard cert), auth (Secrets
Manager and the KMS key), container (the ECS cluster and its task-event
trail), service (the backend on ECS Fargate), api (the `api[-dev]` HTTP API on API Gateway, VPC Link V2
to the ALB), observability (the backend's alarms), database, and **spark-store** (`spark-store-stack.ts`: one S3
bucket, with a lifecycle rule that expires `data/` after 90 days).

The Spark store is storage only (`core/observability/spark_store.py`): gzip JSON-lines objects at
`data/<table>/<user_id>/<YYYY-MM-DD>/<name>.json.gz` (tables `turns` and `task_steps`), organized by person so that
deleting one person's data is a single listing per table. Voice turns are written after the `_save_turn`
transaction succeeds (which checks the session and `os_analytics_off`); `POST /os/log` first does a consistent read
of the person's `users` row (no row or `os_analytics_off`: nothing is written). A write whose check is over 60
seconds old is dropped. Deleting a person's data lists and deletes their objects: for an account deletion (Clerk
`user.deleted` or `DELETE /os/account`) the first sweep is awaited and a failure fails the request, with a second
sweep two minutes later in the background; for usage data turned off both run in the background, skipped while one is
running (its second sweep covers a later opt-out; the app's next `analytics:false` retries a failure). The store uses its
own two-thread pool, so S3 never shares threads with DynamoDB. One alarm, W91, fires on `spark_store.forget_failed`.
No job, queue, Firehose, Athena or Glue; analysis is not built yet. Events stay in PostHog only; an account deletion also deletes the person and their events there (`posthog_admin.delete_person`, best effort, needs `person:write` on the personal key; a failure counts `posthog.person_delete_failed`). The usage-data switch writes `os_analytics_at` (request arrival, ns) and ignores an older write, so the person's last choice wins; a tie goes to off.

The database stack holds four DynamoDB tables, named `lightbulb-{env}-{suffix}`: `users` (the Spark account),
`containers` (voice-session rows, found through `status-index`), `spark-transcripts` and `webhook-event-dedup` (Stripe
and Clerk).

Stacks pass secret names and KMS key ARNs, not objects, to avoid cross-stack KMS grant cycles. Environment
variables are set by CDK; do not set them by hand.

| AWS setting | Value |
|---|---|
| Account | `877352799272`, alias `thelightbulb` |
| Region | `us-east-1` |
| SSO start URL | `https://isol8.awsapps.com/start` (cannot be renamed) |
| CLI profile | `isol8-admin` |

## Shipping

### Code review and merging

**Codex is the only reviewer**, through the `chatgpt-codex-connector` GitHub App. A bot cannot ask Codex for a
review: `@codex review` from `github-actions[bot]` gets "create a Codex account", which means the wrong commenter,
not a bad PR. Codex publishes no status check, so read its review before you merge. It answers on one of four
surfaces; check all four before you decide it was silent:

```bash
gh pr view <n> --repo TheLightbulbCompany/lightbulb --json reviews  # formal review
gh api repos/TheLightbulbCompany/lightbulb/pulls/<n>/comments       # inline findings
gh api repos/TheLightbulbCompany/lightbulb/issues/<n>/comments      # "Didn't find any major issues."
gh api repos/TheLightbulbCompany/lightbulb/issues/<n>/reactions     # a bare 👍 is a complete approval
```

The local `codex` CLI is the fallback when the connector does not pick a PR up; paste its result into the PR.

### Deploy model

- **Backend and AWS: `deploy.yml`** (hand-written). It runs `cdk deploy 'dev/*'` when a merge touches
  `apps/backend/**` or `apps/infra/**`. **Prod deploys only when a person runs it**:
  `gh workflow run deploy.yml --ref main` (deploys dev first, then `cdk deploy 'prod/*'`). The prod gate is
  guarded by `apps/infra/test/deploy-workflow.test.ts`.
- **Website: Vercel's Git integration** (project `frontend`, root `apps/frontend`, production branch `main`). Every
  PR gets a preview deployment (Preview env vars: the dev API and the dev Clerk key). **A merge to `main` that
  touches the website deploys production automatically**; merges that don't touch it are skipped
  (affected-projects deployments). There is no website workflow in `.github/workflows`.
- **For the backend, `main` is ahead of prod by default.** Never call it live because it merged. Read the
  running version from AWS (`aws ecs describe-task-definition`).
- `deploy.yml` has one concurrency group, `lightbulb-deploy`, with `queue: max`: runs queue in order. Before you dispatch, this
  should read 0:

```bash
gh run list --repo TheLightbulbCompany/lightbulb --workflow deploy.yml --limit 10 \
  --json status --jq '[.[]|select(.status!="completed")]|length'
```

- A run's `status` lags its jobs; count jobs for liveness.
- Every session shares one GitHub identity. Pass `--interval 60` to `gh run watch`, poll no faster than once a
  minute, and on a 403 or 429 wait for `x-ratelimit-reset` / `retry-after`.

```bash
gh run watch <run-id> --repo TheLightbulbCompany/lightbulb --exit-status --interval 60
```

## Names that stay

- The legal entity is **"Isol8 Inc., doing business as Lightbulb AI"**. Write no other legal name.
- The SSO portal `isol8.awsapps.com` and the `isol8-admin` profile cannot be renamed.
- GitHub OIDC subs are id-qualified (`repo:TheLightbulbCompany@253404738/lightbulb@1182791203:…`); keep that form
  in the deploy-role trust policies.
