# AGENTS.md

> Codex-facing mirror of `CLAUDE.md`, regenerated from it on 2026-11-10.
> `CLAUDE.md` is the maintained original — if the two disagree, `CLAUDE.md` wins;
> regenerate this file from it rather than editing here.

This file provides guidance to Codex and other coding agents when working with code in this repository.

## Project Overview

Lightbulb (`thelightbulb.company`) is **a voice assistant for each business**, sold as the receptionist. It has
three surfaces:

1. **Phone.** A caller rings the business's number (Twilio). The assistant answers, takes messages, books,
   moves and cancels appointments, and hands off to a person in opening hours.
2. **Google Meet.** A member invites the workspace's agent address to a meeting. The agent joins, answers when
   it is named, and starts the follow-up work it was clearly given.
3. **The orb.** A member talks to the assistant in the web app and the Mac app (`/voice/lightbulb`). The orb
   does the general agent work the old product did.

Typed messages (web, Mac, mobile) still exist and take the same path as speech.

**The engine** is the same for every surface. **GPT-Live** (OpenAI, through LiveKit) is the voice. It holds no
business data and has no tools: it hands every business request to the workspace's **Router**, a Temporal
workflow that only decides (start a task, reply, ask, note, ignore, and a few surface actions). Real work runs
as **Workers**: Temporal task workflows that run turns of **Hermes** (`nousresearch/hermes-agent`) on the
**shared Hermes pool**, one AgentCore runtime per tier with one session per owner or per task. DynamoDB is the
record; S3 holds each agent's Hermes profile. Approvals are authenticated card taps, never a spoken "yes".

**Where the decisions and the status live:**

- **Standing founder decisions F1 to F38**, with reasons: Part 1 of
  `docs/superpowers/plans/2026-11-09-voice-to-founder-test.md`. A short list is in "Standing decisions" below.
  Do not reopen them in code.
- **The board**, `docs/voice-status.md`: every promise, and whether it is Built, on Prod, Machine-tested and
  Founder-walked. Status lives there, not in this file. A PR that changes a capability edits its row.
- The design spec and master plan: `docs/superpowers/specs/2026-11-07-voice-agents-design.md` and
  `docs/superpowers/plans/2026-11-07-voice-agents-master-plan.md`. Where they differ from Part 1, Part 1 wins.

The stack: FastAPI on ARM Fargate (ECS), Temporal Cloud, DynamoDB and S3, Bedrock AgentCore, OpenAI GPT-Live via
LiveKit Cloud, Twilio, Recall.ai (Meet bots), Next.js 16 on Vercel, a Tauri Mac app, an Expo iOS app, Clerk,
Stripe and Metronome. Infrastructure is AWS CDK in `apps/infra/` (the single source of IaC truth).

## Standing decisions

From plan Part 1 (F-numbers). Each constrains code; do not reopen them.

- **F38 One architecture for every use case.** Phone, Meet, the orb and typed messages all go voice or text, then
  the Router, then a Worker. A new use case adds tools and permissions, never a new shape.
- **F2/F3 The Router decides only;** facts it cannot see go to a Worker thread.
- **F4/F5 GPT-Live is the voice on all three surfaces, with no business data and no tools, and the model owns the
  conversation.** Code enforces permissions, settings, effect safety, identity and duplicate prevention. No keyword
  rules, canned lines, mute gates or one-question prompt patches.
- **F6 Phone uses the same Router and Workers under a caller principal.** No fast path for callers.
- **F7/F8 Approvals are card taps only** (a voice can be anyone's). In a meeting everyone acts with the inviting
  member's authority; spend, send and publish need that member's tap.
- **Caller boundary.** A caller or texter gets the business profile and their own bookings and messages, never owner
  credentials or team memory.
- **F9/F10 One workspace per business** (agent, phone, billing, permission boundary). The six-agent starter roster
  (CEO, Engineering, Finance, Growth, Analytics, Customer Operations) is kept; one agent is the receptionist
  (`frontAgentId` in the voice-agent settings). Names are editable, so policy follows role-keyed UUIDs.
- **F11 One shared Hermes pool per tier; a session per owner or per task.** Cost must follow use.
- **F12/F13 Paperclip, Aurora, the marketplace, Apps, the Stripe Projects rail and OpenSearch are deleted.** Do not
  rebuild on them, but see "Looks dead, is live".
- **F14 The OpenRouter gate stays** as the inference and billing gate for all text inference.
- **F17 A Meet session may sleep after five idle minutes and wake on its name.**
- **F20 Booking conflicts are the model's and the booking tool's;** the tool still reserves a slot atomically.
- **F22 Missed-caller and waitlist texts stay off** until a consent review.
- **F37 The business owns its opening** (greeting, AI notice, recording notice): an editable default, nothing
  enforced in code, except that the assistant never claims to be a person when a caller sincerely asks.
- **F32 One pool of credits covers voice and text.** Design pending (plan Part 2, Q2): build no second meter.
- **F33 Sign in with ChatGPT stays available to every workspace for now** (founder, 10 Nov). Customers default to
  managed credits.
- **No migrations.** No migration code, backfills or compatibility layers: fix forward. There are no real customers.
- **Merge rule (founder, 9 Nov).** Merge when CI is green and Codex's GitHub review is clean on the exact head.
  Prod goes only through the deploy workflows. F36: a review that finds an architecture issue goes to the founder.

## Product domain

The product runs on **`thelightbulb.company`** in both environments (`api.`/`api-dev.`, `ws.`/`ws-dev.`,
`bv.`/`bv-dev.`, `admin.`, `admin.dev.`, `dev.`, Clerk's `clerk.`/`accounts.`/`clkmail.`), the same domain that
carries mail. `flickmeon.ai` is retired: Vercel only, every path 308s to the product domain, no API hosts, no
mail. `apps/infra/lib/domains.ts` is the per-environment truth; read it rather than any doc when a hostname
matters. `ROOT_DOMAIN` and `PLATFORM_MAIL_DOMAIN` are the same value today but stay two parameters.

Product hosts and company slugs (`{agent}@{slug}.thelightbulb.company`) share one namespace, so **every new
product host must be added to `RESERVED_SLUGS`** (`core/services/company_identity.py`) before its DNS record
exists.

Kept on purpose: the `flickmeon://` deep-link scheme, the reserved `flickmeon` slug, and `LEGACY_ROOT_DOMAIN` /
`LAUNCH_DOMAIN` in `domains.ts`.

## Rebrand residue (Isol8 → Lightbulb)

The `isol8` brand is retired. A naive `s/isol8/lightbulb/` sweep breaks each of these:

| Residue | Why it stays |
|---|---|
| SSO portal `isol8.awsapps.com`, CLI profile `isol8-admin` | AWS allows one portal-URL customization per Identity Center instance, ever. |
| Expo slug `isol8-mobile` | Expo slugs cannot be renamed; `app.json` must match the server. |
| Mobile bundle id `co.isol8.mobile` | Bound to App Store record 6788944100, its build history, Push capability and APNs key. A rename (#2098) stranded the app. `APPLE_IAP_BUNDLE_ID` and the `apple-app-site-association` appID must match. The IAP product id `company.thelightbulb.mobile.plan.monthly` is new on purpose. |
| `-isol8.N` image tags | Rollback refs. |
| Historical refs (`Isol8AI/isol8#2053`) | Quote verbatim; GitHub web URLs redirect. |
| `isol8` reserved in slug and handle allowlists | Nobody can claim the retired brand. |
| `ISOL8_OWNER_ID`, `ISOL8_API_URL` | Read by the Hermes bridge and the bundled skills. A rename is a backend change plus an image rebuild plus a roll. |
| `isol8.hire_suggestion` | `HIRE_KIND` in the Hermes `lightbulb` plugin. Do not "finish" it. |
| `ISOL8_CURATOR_EMAIL` | Human-gated mail (`core/constants.py`, read by `curator.py`). |
| `"isol8 top-up …"` in `metronome_funding.py` | Part of an idempotent Metronome request body: changing it 409s a retried top-up on the money path. |
| CloudWatch namespaces `LightbulbAI*`, dimension `lightbulb-backend` | Renamed 2026-08-27; history before that is orphaned. Not related to the GitHub org name. |

The legal entity is **"Isol8 Inc., doing business as Lightbulb AI"**. Do not write any other legal name.

AWS names are `lightbulb-{env}-*` (DynamoDB), `lightbulb/{env}/*` (Secrets Manager), `/lightbulb/{env}/*` (SSM).

GitHub OIDC subs are id-qualified since the 2026-08-30 org rename
(`repo:TheLightbulbCompany@253404738/lightbulb@1182791203:…`). Plain `repo:ORG/REPO:*` patterns no longer
match. Keep the id-qualified form in the deploy-role trust policies.

## Looks dead, is live

Names from deleted systems that still carry load. Prove nothing reads a resource before you delete it.

- **`paperclip-companies` table**: the canonical workspace identity row.
- **`PAPERCLIP_SERVICE_TOKEN_KEY`**: signs session shares, digests and Needs-you links.
- **The bridge's `/internal/paperclip/v1/runs` route** (`apps/infra/hermes/bridge/server.py`): creates, polls
  and stops a real Hermes turn.
- **`provisioning-workflows` table**: still read by `owner_facts` and the wipes.
- **`ECS_SUBNETS`, `ECS_SECURITY_GROUP_ID`**: the AgentCore `networkModeConfig` that
  `hermes_provisioning` builds into a runtime spec. Removing them does not fail loudly: it hands a runtime an
  empty VPC config. `ContainerSecurityGroup` in `container-stack.ts` is the security group they name.
- **`OPENCLAW_WORK_QUEUE_URL`, `openclaw-work.fifo`, `core/services/openclaw_work.py`, the
  `/plugins/lightbulb-channel/*` bridge paths**: the live wake queue and ingress for typed messages. The prefix
  is a name, not a dependency.
- **`OWNER_STATE_BUCKET`**: its physical name is `lightbulb-{env}-openclaw-replicas`. One bucket, every
  per-owner file.
- **`core/billing/bedrock_aip.py`, `bedrock_gate_tokens.py`, the `bedrock-gate-tokens` table**: the per-owner
  role and the openrouter-gate bearer. Bedrock inference is gone; the names are scars. The table is owned by no
  stack (detached, not deleted).
- **`LitestreamReplicaLifecycle`** in `service-stack.ts`: carries the bucket-scoped `s3:ListBucket` every profile
  read, write and delete needs.
- **`components/messaging`, `workspace`, `teams` and `control`** in the web app: partly live. `/voice/lightbulb`
  mounts `workspace/RightPane` (which renders `teams` and `control` panels) and `messaging`; `/voice/settings`
  mounts `settings/SettingsPlace`, which pulls in `control` panels. Prove a file has no importer before you delete it.

## Critical Rules

**NEVER modify production servers directly.** Every change to backend, frontend, infrastructure or environment
variables is version controlled and deployed by a pipeline: AWS CDK, GitHub secrets and AWS Secrets Manager.

**Always watch your runs after you push.**

**NEVER overwrite any environment or docker files, or git repos, without permission.**

**If tests fail, your change is likely the culprit, even if the error is in a file you did not touch.**

**NEVER use `sleep` or `tail` in bash.** Use `gh run watch` or tool-based polling; use the Read tool instead of
`tail`.

**Never store customer data on this Mac, and never print a secret.**

**This repo is PUBLIC, by the founder's choice.** No secrets, customer names or data, or phone numbers in a commit,
PR, doc or test fixture. `secret-scan.yml` runs on every PR.

**Planning docs are disposable.** Plans and specs under `docs/superpowers/` are working documents. When the
feature ships or is abandoned, delete the doc; git history and the PR keep it. The plans that stay are the ones
this file or living code names: `2026-09-07-audit-report.md` (its §6 names load-bearing scar tissue that must
not be "fixed"), and the voice spec, master plan and founder-test plan above. One-shot backend scripts are
deleted once they have run; re-runnable ops tools stay in `apps/backend/scripts/`.

### Monorepo

Turborepo with **pnpm** for JS and **uv** for Python. Apps: `backend`, `frontend`, `desktop`, `mobile`,
`huddle-agent`, `infra`, `company`; plus `packages/shared` (see Frontend).

```bash
pnpm install                                   # all JS deps, from the repo root
cd apps/backend && uv sync                     # Python deps
cd apps/backend && uv run uvicorn main:app --reload --port 8000
cd apps/frontend && pnpm run dev               # localhost:3000
cd apps/backend && uv run pytest tests/ -v     # backend tests
cd apps/frontend && pnpm test                  # frontend unit tests
cd apps/huddle-agent && uv run pytest tests -q # voice worker tests
tla/check.sh                                   # TLA+ model checks, see tla/README.md
```

### Dev testing

There is no Playwright or E2E suite. Acceptance is an agent driving Chrome (web) and computer use (the installed
Mac app) against real environments, with receipts. UI is driven through Chrome and computer use, never DevTools
or CDP automation. For the voice product, a pass counts only on prod and only for the versions it ran on: see
the board's rules and plan Part 3. Real calls and meetings may be tested on prod (F26). Daily test spend caps:
Twilio $5, Recall 3 hours, OpenAI voice $5.

For a local frontend against dev endpoints, see `docs/runbooks/local-dev-browser-verification.md`.

**Signing Chrome in as an existing dev test account:** mint a Clerk sign-in token (`POST
https://api.clerk.com/v1/sign_in_tokens`, secret `lightbulb/dev/clerk_secret_key`, `+clerk_test` users) and redeem it
on a `dev.thelightbulb.company` page with `Clerk.client.signIn.create({strategy:'ticket', ticket})` then
`Clerk.setActive`. Password prompts and OTP entry are off-limits. Dev Clerk is shared with prod previews: never use
a production email.

`/api/v1/debug/provision` (`PATCH` rewrites every roster profile and stops the warm session; `DELETE` then
`POST` reprovisions) still exists and returns 403 in prod. The `dev-clean-slate-reset` and `prod-org-reprovision`
skills in `.agents/skills/` still describe Paperclip and Aurora wipes; those steps are stale.

## Shipping

### Code review and merging

**Codex is the only reviewer**, through the `chatgpt-codex-connector` GitHub App. A bot cannot ask Codex for a
review: `@codex review` from `github-actions[bot]` gets "create a Codex account", which means wrong commenter,
not bad PR. Codex publishes no status check, so read its review before merging. It answers on one of four
surfaces; check all four before you conclude it was silent:

```bash
gh pr view <n> --repo TheLightbulbCompany/lightbulb --json reviews  # formal review
gh api repos/TheLightbulbCompany/lightbulb/pulls/<n>/comments       # inline findings
gh api repos/TheLightbulbCompany/lightbulb/issues/<n>/comments      # "Didn't find any major issues."
gh api repos/TheLightbulbCompany/lightbulb/issues/<n>/reactions     # a bare 👍 is a complete approval
```

The local `codex` CLI is the fallback when the connector does not pick a PR up; paste its result into the PR.

### What CI runs on a pull request

CI was stripped on purpose (F23). A PR runs only:

- `secret-scan.yml` (every PR and every push to `main`).
- `backend-arm64.yml`: builds the ARM64 backend image and smoke-tests it. Path-filtered to `apps/backend/**`,
  `service-stack.ts` and `app.ts`. **It does not run the backend unit tests or lint.**
- `huddle-agent.yml`: the voice worker's tests, path-filtered to `apps/huddle-agent/**`.
- `desktop-build.yml` (Rust tests and clippy), path-filtered to `apps/desktop/**`.
- `roll-hermes-runtime.yml`, path-filtered to its own script and bootstrap files.

There is **no backend or frontend test or lint run on a PR**, and no infra CI. Run the tests you touched before
you push (`uv run pytest <files>`, `pnpm test`); check the exit code, never `pytest | tail`.

### Deploy model

**Dev deploys on merge; prod is promoted on demand.** `.github/workflows/deploy.yml` (the CDK pipeline, which
also holds the Vercel deploys `DeployVercelDev` and `prod-DeployVercelProd`) runs the dev stage on every push to
`main`. Every prod job carries `if: github.event_name == 'workflow_dispatch'`, so prod moves only when someone
runs `gh workflow run deploy.yml --ref main`. A dispatch deploys dev from that ref first; prod jobs `needs:` the
dev stage. Jobs run on `vars.DEPLOY_RUNNER`. The gate is set in `apps/infra/lib/app.ts`
(`addStageWithGitHubOptions` plus one `JsonPatch` for `prod-DeployVercelProd`, whose `if:` sits at the bottom of
its block) and guarded by `apps/infra/test/deploy-prod-promotion.test.ts`.

**`main` is ahead of prod by default.** Never infer prod's contents from `main`, and never call something live
because it merged. Three things deploy separately: backend and web (`deploy.yml` dispatch), the voice worker
(`huddle-agent.yml` dispatch), and the Hermes image (pin plus roll). A capability is on prod only when every
part it needs is. Read the running versions from AWS (`aws ecs describe-task-definition`,
`aws bedrock-agentcore-control get-agent-runtime`), LiveKit and Vercel.

**Concurrency:** one group, `lightbulb-deploy`, with `queue: max`: runs queue FIFO, so a merge cannot evict a pending
promotion and re-dispatching only adds a run behind it. (Without it, three promotions died that way on
2026-09-07.) Before you dispatch, this should read 0:

```bash
gh run list --repo TheLightbulbCompany/lightbulb --workflow deploy.yml --limit 10 \
  --json status --jq '[.[]|select(.status!="completed")]|length'
```

A run's `status` lags its jobs: for liveness count jobs
(`gh run view <id> --json jobs --jq '[.jobs[]|select(.status!="queued")]|length'`). A cancelled run with 0 jobs is
not self-evidently an eviction. `rerun --failed` on a deploy does not work; re-dispatch.

**`deploy.yml` is GENERATED** from `apps/infra/lib/app.ts` by `cdk-pipelines-github`. Its drift guard only throws
inside the deploy run, post-merge; locally `cdk synth` silently rewrites the file. So any change under
`apps/infra/lib/` must run `cd apps/infra && npx cdk synth` and commit the regenerated `deploy.yml` in the same
PR. No PR check catches this now that `infra-ci.yml` is deleted: drift blocks every deploy job after the merge.
`cdk synth` does not need Docker when node/npm and python3/pip3 are on the machine (the bundled Lambdas use
`local.tryBundle`).

The prod gate is the `workflow_dispatch` `if:`, not a GitHub Environment: reviewers on `prod` returned 422 when the
repo was private (Team plan); the repo is public now, so re-test before relying on either claim.
`workflow_dispatch` needs only write access, so the gate stops accidents, not a determined agent.

**Rate limits.** Every session shares one GitHub identity: 5,000 REST requests an hour plus secondary limits.
Always pass `--interval 60` to `gh run watch`, poll no faster than once a minute, and on a 403 or 429 back off
until `x-ratelimit-reset` / `retry-after`.

```bash
gh run watch <run-id> --repo TheLightbulbCompany/lightbulb --exit-status --interval 60
```

### Other release lanes

- **Voice worker:** `huddle-agent.yml` job `deploy-prod`, on `workflow_dispatch` only and only when
  `vars.LIVEKIT_PROD_ENABLED == 'true'`. See "Voice worker".
- **Hermes image:** `build-hermes-image.yml` on any `apps/infra/hermes/**` change pushes a candidate; a human flips
  the pin and rolls. See "Hermes image and rollout".
- **Phone trunks:** `phone-telephony.yml` (manual) reconciles the Twilio trunks and the production telephony
  secret. It buys no numbers and places no calls.
- **Desktop:** `desktop-build.yml` on a manual dispatch builds the signed prod DMG on the self-hosted Mac
  (`macos-desktop`) and publishes a `desktop-v0.4.*` release for the Tauri updater. There is no dev desktop build.
- **Mobile:** `mobile-ota.yml` (merge to `main` → `eas update --channel production`, Linux) and
  `mobile-release.yml` (`mobile-v*` tag → `eas build --local` + `eas submit`, self-hosted Mac). There is no mobile
  PR CI any more.
- The self-hosted Mac runs only the native lanes (`desktop-build.yml`, `mobile-release.yml`). Anything JS-only
  belongs on `vars.CI_RUNNER`.

## Architecture

### How a phone call flows

```
Caller → Twilio number → LiveKit SIP (dispatch rule, room "phone-*")
  → voice worker (apps/huddle-agent/src/huddle_agent, phone.py + live.py; GPT-Live with client delegation, no tools)
  → POST /api/v1/internal/voice/calls            (routers/voice_phone.py, worker secret): the call record
  → each delegated caller line: POST …/calls/{id}/turn
       → a message in the call's room, authored by the caller (not a member), plus a phone_contact trigger
       → Router (Temporal, router:{ws}) decides with the caller's scope and the business profile
       → Front desk Worker thread runs as the caller (booking tool → voice_booking.py)
  → the Router's and Worker's replies arrive as room messages (inReplyTo the line); the worker reads
    them from …/events and gives them to GPT-Live, which says them in its own words
```

- **Caller identity comes from the server, never the model:** the `PHONE#<e164>` / line row and the call record.
  A paused or not-live line only takes messages (`/message`); a caller cannot do what the owner unticked.
- **Bookings** (`core/services/voice_booking.py`): the assistant's own calendar first, Google Calendar optional,
  with pending-operation reconciliation so a slow or lost Google write never leaves a booking stuck. The slot is
  reserved atomically, so two bookings cannot take one slot.
- **Numbers** are bought at Go live (`VOICE_BUY_NUMBERS`, on in prod). "Keep my number" forwards to the bought
  number; porting is later.
- Hand-off to a person is the Router's `transfer_call`, only on a caller's batch.

### How a Meet works

A signed-in member invites the workspace's agent address (`agents@{slug}.{AGENT_EMAIL_DOMAIN}`) to a meeting.
`core/services/meeting_invites.py` turns a member's calendar invite into a meeting room; a stranger's invite is
ordinary team mail. At start time a Recall.ai bot joins the Meet (`meeting_bots.py`); `meet_bridge.py` turns each
person's Recall audio into their own LiveKit participant, so the worker (`meet.py`) knows who spoke. Everything
said that needs the business goes to the Router like a phone line. Everyone acts with the inviter's authority;
spend, send and publish need the inviter's card tap. The Router can post to Meet chat (`meeting_chat`). Notes
stay in the app, with no email (F15).

### How the orb works

Owner mode: the member's DM with the assistant, at `/voice/lightbulb`, with the voice worker's `owner.py`.
`routers/voice_owner.py` (worker secret plus the room token) turns each spoken line into the member's own message,
exactly like a typed one, and hands back the room's agent messages for the voice to say. The speaker is the
LiveKit identity the server took from the Clerk-minted join token and must be a human member seated in the DM.
Nothing on the voice side reads, approves or starts work. Today the orb is the only surface that debits credits.

### How a typed message flows

```
Client --HTTP (Clerk)--> routers/messaging.py --> lightbulb-{env}-messaging (DynamoDB, seq-ordered)
   one conditional transaction: bump META.lastSeq, put MSG, put IDEMP receipt, put WORK#ADMIT# outbox row
   DDB Stream ──> messaging-projector λ ──> AppSync Events ──> browser / Mac / mobile (subscribe-only)
              └─> EventBridge Pipe (WORK#) ──> openclaw-work.fifo ──> work Lambda ──> openclaw_work
                    ──> admission.admit_pointer ──> Router workflow (router:{ws})
                    ──> start_task | pass_through | reply | ask | note | ignore | meeting_chat | transfer_call …
                    ──> Worker task (TaskWorkflow) ──> Hermes turn on the shared pool
                    ──> result posted back to the room with inReplyTo
```

- The Router's tool list is in `core/temporal/router_prompt.py`; its workflows are in
  `core/temporal/workflows.py` (`RouterWorkflow`, `TaskWorkflow`), run by `scripts/temporal_worker.py` as its own
  ECS service on task queue `lightbulb`.
- Jev (`core/services/jev.py`) now only routes unaddressed messages (`route_for_router`).
- `messaging.AGENT_REPLY_CAP` (2) still caps agent-to-agent chains.
- The browser never supplies an owner id, author id, runtime address, fanout list or publish credential: the
  backend derives all of them. The durable `IDEMP` row, not DynamoDB's short-lived token, is the retry receipt.

**Messaging invariants:** no second event bus (SQS is the durable wake-up only, AppSync is fanout, DynamoDB is truth;
no Kafka, Redis or per-tenant queues). Every human membership removal rotates the room's `deliveryKey` in the same
transaction, because an AppSync `onSubscribe` decision cannot close an open subscription: this is a security
boundary. Clients are subscribe-only (Clerk JWT template `lightbulb-appsync`) and reconcile from HTTP
(`GET …/messages?afterSeq=`), since AppSync can drop or duplicate. The projector sends mobile push last (a raising
publish replays the whole record; Expo has no dedupe). Execution is at-least-once.

### Runtime transport (backend → Hermes)

There is **one shared Hermes pool per tier**: the CDK resource `lightbulb_{env}_hermes_pool` in
`service-stack.ts`, whose ARN reaches the backend as `HERMES_SHARED_RUNTIME_ARN`. It boots with no owner; the
bridge binds each session to the first invoke's tenant, and S3, memory and the browser come from that session's
scoped credentials (`HermesTenantRole`, narrowed per owner). The pool's own execution role has no tenant data
grants.

- **One way in:** `core/services/runtime_transport.py` sends `InvokeAgentRuntime` with a
  `{path, method, headers, body}` envelope the bridge unwraps, carrying the tenant in `X-Lightbulb-Tenant`.
  There is no backend-initiated socket and no address to resolve: AgentCore starts the microVM on the invoke.
- **One session id per owner** (`lb-<owner>-<hash>`), or one per task (`lb-<owner>-t-<hash>`). AgentCore makes a
  new microVM per distinct session id, and two live sessions for one owner are two writers on one S3 prefix,
  which corrupts the profile silently (reproduced 2026-09-16).
- **Single-flight cold start.** One id is not enough: N concurrent invokes on a cold session get N microVMs. A
  `__session__#<owner>` row in the containers table holds a `warm_until` estimate and a 120 s wake lease; one
  caller wakes the session and the rest wait. **Every stop must go through
  `hermes_provisioning.stop_runtime_session`**, which clears that row (a test enforces it). Detection query:
  `docs/runbooks/runtime-split-brain-detection.md`.
- Sessions idle out after **5 minutes** (`IDLE_TIMEOUT_S`) and live at most **8 hours** (`MAX_LIFETIME_S`,
  AWS's ceiling), in `core/services/agentcore_runtime.py`. AppSync connectivity proves nothing about compute.
- The bridge (`apps/infra/hermes/bridge/`, the image's `CMD`) restores the profile from S3 once per process and
  **saves it back at the end of each turn**, which is why a roll waits for an idle session: stopping mid-turn
  loses what that turn wrote.

**Provisioning** joins the pool; it creates nothing per business. The `lightbulb-{env}-provision` Step Functions
machine (`provision_dispatch.dispatch_provision`) runs `scripts/provision_runner.py` as a Lambda:
`prepare_hermes_workspace` (tokens and boot values), `_seed_owner_workspace` (the workspace row and the six-agent
roster in `agent_registry`, role-keyed uuid5 ids, so concurrent runs converge), then
`provision_hermes_runtime`, which writes every roster profile to S3 and records the pool ARN, with no
`CreateAgentRuntime`. Every step is idempotent; there is no lease. Every owner is a Clerk org; every provisioning
door refuses a personal (`user_`) owner. Per-owner runtimes are legacy (about 25 remain, being retired by exact
allowlist; their identity seeds are kept until network identity custody has a replacement).

### Hermes image and rollout

The image is `lightbulb/hermes-agent-arm64` in ECR, built from `apps/infra/hermes/Dockerfile`. It is
**linux/arm64 only** and `FROM`s `nousresearch/hermes-agent` **by digest**: upstream publishes no numbered tags.
It bakes the CLI-connector binaries, the `lightbulb` plugin (`apps/infra/hermes/plugins/`), the managed config
overlay (`managed/`, which wins at the leaf), the generated skills tree and the bridge.

- **Pins** are `hermes-version.json` at the repo root: `base` is the digest, `dev.tag` / `prod.tag` the promotion
  gate. Read its `notes` field before bumping. `build-hermes-image.yml` never writes the pins: it pushes one
  candidate tagged `h-<YYYY.M.D>-<sha7>` and a human lands a PR flipping the tag.
- **A pin flip, deployed by `deploy.yml`, updates the pool's image through CDK.** Then
  `apps/backend/scripts/roll_hermes_runtimes.py` idle-stops warm sessions that started on the old image
  (`ROLL_IDLE_WAIT_S`, 10 minutes, because of the end-of-turn save). `run_runtime_roll.py` with
  `roll-hermes-runtime.yml` runs one org's roll in CI. Verify by reading the pool's `containerUri`, never the pin
  file.
- **Config is not code.** An agent's config is its S3 profile under `<owner>/hermes/<agent>/`
  (`config.yaml`, `.env`, `SOUL.md`, `lightbulb-facts.json`), written by `hermes_profiles.write_agent_profile`.
  Writing it bumps `.revision`, and the bridge re-pulls before the next turn even in a warm microVM. A profile is
  a complete `$HERMES_HOME` merged over Hermes defaults alone; secrets never land in `config.yaml` (it holds
  `${VAR}` references; `.env` is the store).
- **Skills:** source is `apps/infra/hermes/skills-src/` (the `lightbulb` skill); `build-skills.sh` derives
  `/opt/hermes-skills`; `apps/infra/hermes/skills/` is generated and gitignored, never commit it. Keep `SKILL.md`
  thin (every agent pays for it on every load) and put detail in `references/*.md`; `MAINTENANCE.md` lists the
  anchors to update. `scripts/reapply_hermes_skills_dirs.py` re-projects skills into existing profiles.
- Per-owner dynamic state (plan, provider) is not in the skill: it is `$HERMES_HOME/lightbulb-facts.json`,
  written by `core/services/owner_facts.py`.

### Voice worker (`apps/huddle-agent`)

One LiveKit Agents worker, agent name **`lightbulb-huddle`**, serves every surface. `agent.entrypoint` routes a
room named `phone-*` to `phone.py`, a job with `source: "meet"` to `meet.py`, and `mode: "owner"` to `owner.py`;
`live.py` is the voice session they share. It runs GPT-Live with **client delegation and no tools**: the model
decides when a line needs the backend and fires a delegation, which `live.py` posts to the Router; results come
back as room messages and go to the model as commentary. Nothing in the worker speaks for the model or gates when
it talks (F5). Time caps are cost caps: `PHONE_MAX_S` (20 min) and `MEET_MAX_S` (90 min).

- **Deploy:** prod only, LiveKit Cloud agent `CA_HTFFgrTj24NH`, by `huddle-agent.yml` `deploy-prod` on
  `workflow_dispatch` with `LIVEKIT_PROD_ENABLED=true`. It copies the committed `livekit.prod.toml` to
  `livekit.toml`. The LiveKit plan allows one deployed agent.
- **There is no deployed dev worker.** Dev runs on a Mac (`lk agent start`, or
  `uv run python -m huddle_agent.agent dev`). Dev and prod share one LiveKit project.
- The `LIVEKIT_AGENT_NAME` in the Dockerfile and `agent_name=` in `agent.py` must stay equal: the backend
  dispatches by that name. The worker authenticates to the backend with `X-Lightbulb-Huddle-Secret`
  (`lightbulb/{env}/huddle_worker_secret`). Its secrets live in LiveKit (`lk agent secrets`), not in CDK.
- Phone evals: `tests/evals/sim.py` (simulated callers). Meet scenarios: `tests/replay/scenarios/`.

## Backend (FastAPI + DynamoDB)

`main.py` is the app. Its lifespan starts about ten in-process background loops, registered in
`system_health.BACKGROUND_TASKS`; the running-gauges loop is not optional, because the P12 backend-liveness canary expects its metric.
No ORM: DynamoDB items are plain dicts, accessed through `core/repositories/`; tables are `lightbulb-{env}-{suffix}`
(see `database-stack.ts`; the messaging and ws-connections tables live in `api-stack.ts`). `core/config.py` is the
source of truth for settings, but several modules read `os.getenv` directly. Run `ls` on the directory you touch;
this file keeps the why, not inventories.

- Auth is Clerk JWT (`core/auth.py`, `get_current_user` → `AuthContext`; JWKS cached 5 minutes, fails closed after
  15 minutes stale). `resolve_owner_id` returns 409 `workspace_required` for any caller with no org claim.
- `owner_lifecycle.py` is a DynamoDB conditional-write lease on the path that writes an owner's credentials. It
  fails closed.
- New Clerk calls go through `clerk_admin.py`; `routers/billing.py` and `routers/desktop_auth.py` still call
  `api.clerk.com` inline.
- Credentials for connectors are materialized into a profile only through `core/services/credentials/hermes_cli.py`
  `project`.
- `/admin` resolves only on admin hosts, but the host gate is cosmetic: `require_platform_admin` admits only the
  Clerk user ids in `PLATFORM_ADMIN_USER_IDS`.

**Machine doors.** No browser opens a WebSocket. Two machine doors remain: `wss://ws[-env]` (API Gateway WebSocket,
Lambda integrations, device-JWT-only authorizer, the desktop local-session relay through
`routers/websocket_chat.py`, gated by the `x-lightbulb-ws-internal` header), and `wss://bv[-env]` (CloudFront
VpcOrigin to an ALB listener path-capped to `/api/v1/gateway/*`, `routers/gateway_relay.py`, where the backend
terminates the desktop node protocol itself). The `BrowserView*` construct ids stay because a rename replaces the
distribution.

## Billing

The public contract is server-owned: `GET /billing/prices` and `GET /billing/account`. Client copy reads those
fields (`apps/frontend/src/lib/pricing.ts`); never bake a price into a client release.

- **The voice plan** (`core/billing/voice_plan*.py`, `voice_billing.py`): $250 a month, 1,500 minutes, $0.35 a minute
  after. It is a normal Stripe subscription (`STRIPE_VOICE_PLAN_PRICE_ID`, Checkout with promotion codes, #5208);
  Stripe's status is the access. Metronome (`METRONOME_VOICE_*`) bills only minutes past 1,500. The old Team plan
  ($50, five members, $20 a seat) is still in code and offered at sign-up; the founder ruled one plan (F27) and one
  credit pool (F32), both open on the board (6.1, 6.3, 6.7). Today only the orb debits credits; phone and Meet send
  minutes to the plan meter only. Build no second meter.
- **Payment is proven in the Stripe sandbox on dev** (F29), never by a real prod charge.
- **Free tier `trial_local`:** a workspace with no billing row gets one, bounded by OpenRouter's per-key daily
  limit (`OPENROUTER_TRIAL_KEY_DAILY_LIMIT_USD`) **and** a 30-day window (`billing_repo._TRIAL_LOCAL_DURATION_SECONDS`;
  a missing `trial_end` is unarmed, not expired). **There is no cap-only check primitive** — a cap-only check once
  woke expired trials — so every door calls the full predicate (`services/trial_cap.py` `trial_block_reason`, cap AND expiry).
- **The openrouter-gate** (`apps/infra/lambda/openrouter-gate/`, `openrouter-gate-stack.ts`) is the fence for all
  managed text inference (F14). The runtime authenticates with a per-owner gate bearer; the owner's own OpenRouter
  key is read by the gate alone. Capped owners (`trial_cap.is_managed_capped`, in lockstep with the gate's `capped`
  predicate in `core.mjs`) are valved by OpenRouter's per-key daily limit; managed paid owners by a reservation
  against the `metronome-fast-gate` row. The `openrouter-settlement` Lambda (5-minute schedule) is the Metronome
  writer for inference. Metronome is a ledger, not a valve.
- **Lockstep constants, never move one alone:** the markup in repo-root `openrouter-pricing.json` (read by the gate
  stack, settlement and the Metronome setup script) and `RESERVE_ESTIMATE_MICROCENTS` (`metronome_gate.py`).
- The backend gates (`gate_chat`, the provision gate) are UX: the limit card instead of a failed run. The real
  fence is the gate on the inference path.
- **Provider paths:** `openrouter_managed` (Lightbulb credits) for customers. `chatgpt_oauth` exists in code and
  is **internal only** (F33). Legacy `byo_key` rows keep resolving their Secrets Manager key
  (`lightbulb/{env}/user-keys/{owner}/{provider}`); new BYO keys are refused. `ENCRYPTION_KEY` (Fernet) protects
  ChatGPT OAuth tokens; rotating it orphans every stored token.
- **Apple IAP** (StoreKit 2) is built and live in dev; `APPLE_IAP_SELLING` is off in prod until the Paid Apps
  Agreement is active. `/billing/apple/link` and `/webhooks/apple-iap` stay live in prod so a charged purchase is
  always linkable. Sandbox transactions verify in prod by design (App Review), bounded by `apple_lapsed`. See
  `docs/runbooks/ios-iap-local-storekit-testing.md`.

## Frontend (Next.js 16)

React 19, Tailwind v4, Clerk, SWR. The app lives under `src/app/voice/`: Home (`/voice`), `/voice/lightbulb` (the
orb and the conversation), `/voice/agent`, `/voice/needs-you`, `/voice/settings`, `/voice/setup`, with components in
`components/voice/`. Sign-in, sign-up and the Mac app land on `/voice`. `/chat` redirects to `/voice/lightbulb`
(or `/voice/settings` for provider and settings links); the old workspace shell is deleted; `RightPane` survives inside `/voice/lightbulb`. `src/middleware.ts`
protects `/voice` and the other app routes and runs the admin-host gate (`/admin` 404s off admin hosts).

- **`packages/shared` (`@lightbulb/shared`) is load-bearing:** the API client, the AppSync live-events client and
  `useLiveEvents` hook, the messaging and runtime clients and shared hooks, used verbatim by web and mobile with
  platform specifics injected through a `PlatformAdapter`. No `next/*`, React Native or DOM globals in it.
- Clients do not derive a socket URL: `GET /messaging/live-config` returns the AppSync endpoints and the caller's
  opaque user-channel key.
- **Colour:** read `src/app/globals.css` (token ladder `--plane-*`, `--fg*`) before describing a colour; no colour
  literals outside the scanner's exemptions (`src/__tests__/brandTokens.test.ts`). Satoshi for UI, Inter for prose.
- **Vercel:** web deploys run inside `deploy.yml`. On 10 Nov 2026 the Vercel team had fallen back to Hobby and was
  paused for overage, so web deploys failed until renewal. Check the team's state before you trust a web deploy.

## Infrastructure (CDK)

`apps/infra/lib/app.ts` is the entry; `lightbulb-stage.ts` is the per-environment stage. There are dev and prod
only, no staging. Eleven stacks in `apps/infra/lib/stacks/`:

| Stack | Holds |
|---|---|
| `network-stack.ts` | VPC, ALB, security groups; egress through one t4g.micro NAT instance. |
| `auth-stack.ts` | Secrets Manager secrets and the KMS key. |
| `database-stack.ts` | The core DynamoDB tables. No relational database. |
| `container-stack.ts` | The platform ECS cluster, the owner-state bucket, the per-owner base policy and permissions boundary, Hermes ECR grants, `ContainerSecurityGroup`. No per-user container. |
| `service-stack.ts` | The ARM64 backend service, the Temporal worker service, the watchdog, the shared Hermes pool and its roles, the provision state machine and its Lambda. |
| `api-stack.ts` | The HTTP API (VPC Link V2 → ALB), the machine WebSocket API, the `bv-` CloudFront door, and the messaging plane (table and stream, AppSync Event API, projector Lambda, `openclaw-work.fifo` and its Pipe). |
| `openrouter-gate-stack.ts` | The openrouter-gate and the settlement Lambda. |
| `ses-inbound-stack.ts` | Inbound agent mail (SES → Lambda → backend); meeting invites arrive this way. |
| `agent-mail-outbound-stack.ts` | Outbound agent-mail configuration set and complaint events. |
| `dns-stack.ts` | The hosted zone lookup and one wildcard ACM cert (a cross-stack export: replace alongside, never in place). |
| `observability-stack.ts` | Dashboards, alarms, log retention. |

Stacks pass secret names and KMS key ARNs, not objects, to avoid cross-stack KMS auto-grant cycles. There is no
Paperclip stack (`apps/infra/test/no-paperclip-stack.test.ts` guards that).

| AWS setting | Value |
|---|---|
| Account | `877352799272`, alias `thelightbulb` |
| Region | `us-east-1` |
| SSO start URL | `https://isol8.awsapps.com/start` (unchangeable residue) |
| CLI profile | `isol8-admin` |

**Environment variables are set by CDK** (runtime, table names, queue URLs); do not hand-set them. Non-obvious
ones: `AGENTCORE_HERMES_IMAGE` (empty ⇒ provisioning refuses rather than guessing), `OWNER_STATE_BUCKET` (empty ⇒
writers refuse), `AGENT_EMAIL_DOMAIN` (derived from the mail domain, `dev.`-prefixed in dev; empty disables agent
mail), `HERMES_SHARED_RUNTIME_ARN`, `VOICE_BUY_NUMBERS`, `VOICE_INBOUND_TRUNK_ID` / `VOICE_OUTBOUND_TRUNK_ID`,
`TEMPORAL_*`, `LIVEKIT_*`, `TWILIO_*`, `RECALL_*`, `METRONOME_VOICE_*`. Frontend: `NEXT_PUBLIC_API_URL` includes
`/api/v1` (`http://localhost:8000/api/v1`, `https://api-dev.thelightbulb.company/api/v1`,
`https://api.thelightbulb.company/api/v1`).

## Desktop app (Tauri)

Tauri 2 at `apps/desktop/`, bundle id `company.thelightbulb.desktop`. It opens voice Home with the mic and Fn
hold-to-talk (F18). Prod is the default flavor (`tauri.conf.json`, `capabilities/default.json`); dev is opt-in
(`pnpm build:dev`, `tauri.dev.conf.json`), and each flavor grants only its own origin to local commands.
It is a voice shell only: the window, the 🌐 talk key (`voice_key.rs`), the tray, native notifications
(`notify.rs`), sign-in through Safari and the `flickmeon://auth` ticket, and the updater. It calls no backend
route itself (the callback page calls `/auth/desktop/sign-in-token`). There is no pairing, node, session sharing or
network engine; `legacy.rs` keeps the hooks, launch agent and shell block older releases installed from opening
a window, and removes the last two at launch. Never ship a script under `Contents/MacOS` (xattr signatures
break the updater's seal).

## Mobile app (Expo)

Expo iOS app at `apps/mobile/`, expo-router, four tabs: Home (`chats.tsx`), Agent (`teams.tsx`), Needs you
(`activity.tsx`), Settings. It shares the web protocol through `@lightbulb/shared`. Bundle id `co.isol8.mobile` and
slug `isol8-mobile` stay. `runtimeVersion.policy: "fingerprint"` blocks an OTA whose native fingerprint does not
match the binary. Metro inlines `EXPO_PUBLIC_*` at bundle time and `eas update` does not read `eas.json` env (a
no-env export once shipped an empty Clerk key). The paywall's web-pay link is allowed only because the app is sold
on the US App Store alone; nothing gates it on the storefront yet, so add that before widening availability.

## Knowledge graph (graphify)

`graphify-out/` (gitignored) holds a single-repo code graph. Ask it with `graphify query "<question>"`,
`graphify path "A" "B"` or `graphify explain "X"`; refresh with `/graphify . --update`. Keep it single-repo. The
`code-graph.yml` workflow that published it on every merge is deleted, so a fresh worktree builds its own. Do not
delete `graphify-out/cache/`: it holds the paid semantic extraction of the docs. The graph may predate the voice
pivot; confirm anything it says against the code.
