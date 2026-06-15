# Support Pipeline + Welcome Email — Design

- **Date:** 2026-06-14
- **Status:** Approved design, pre-implementation
- **Author:** brainstormed with the founder (prez)
- **Scope:** One combined spec covering three coupled pieces on a shared transactional-email foundation.

## Goal

Make it easy for users to get support — especially when they hit an error — by
capturing a support ticket enriched with the debug context we already know
(identity, container state, request IDs, PostHog + CloudWatch deep-links) and
delivering it to the `support@isol8.co` inbox. Give the founder's own Isol8
OpenClaw agent read access to that inbox so it can triage and draft replies
semi-autonomously. Separately, send a welcome email to new users on signup.

## The three pieces (on one shared foundation)

| Piece | What | Shipped where |
|---|---|---|
| **Foundation** | Transactional email via Resend + verified `isol8.co` sender | Platform code (backend) |
| **A. Support capture** | In-app "report a problem" surfaces → enriched ticket → `support@` | Platform code (frontend + backend) |
| **B. Agent triage** | Founder's Support agent reads `support@`, triages, drafts replies | Config on the founders' container (+ one small credential-rail addition) |
| **C. Welcome email** | One transactional welcome on signup | Platform code (backend) |

Pieces A and B are coupled by the **transport decision**: the ticket lands as an
email in the Google mailbox the agent reads (email-as-queue). C is nearly
independent — it only shares the Resend foundation.

## Why Resend AND Google (division of labor)

- **Resend = outbound, app-generated mail.** Send-only API. Used for the welcome
  email and for *delivering* the support ticket into the `support@` inbox.
  Dedicated authenticated sending infra (DKIM/SPF/DMARC on `isol8.co`),
  delivery tracking, templates. Keeps automated mail off the human mailbox's
  sending reputation.
- **Google Workspace `support@isol8.co` = the inbox + the conversation.**
  Receives tickets and direct customer email. The agent reads it via the
  existing `gog` CLI rail. Replies go out as `support@isol8.co` so threads stay
  coherent and land back in the same inbox.

---

## Shared Foundation — transactional email (Resend)

**New code:**
- `core/services/email_service.py` — a single seam: `send_email(to, subject,
  html, *, reply_to=None, from_addr=..., tags=...)`. Everything else (welcome,
  ticket delivery) calls through it. Thin wrapper over the `resend` Python SDK.
- `RESEND_API_KEY` added to `core/config.py` settings, stored in Secrets Manager
  via `auth-stack.ts`, surfaced to the backend service like other secrets
  (`service-stack.ts`). Empty key disables sending (no-op + warn), mirroring how
  Stripe/PostHog optional keys behave.

**Domain / sender setup (ops, documented in the plan):**
- Verify **`isol8.co` in Resend** using a **subdomain Return-Path** (MAIL FROM)
  so Resend's SPF does not collide with the Google Workspace MX on the root
  domain. The visible `From` stays on the root domain.
- Sender identities (no extra mailboxes required — any address on the verified
  domain can send):
  - Welcome: **`From: hello@isol8.co`**, **`Reply-To: support@isol8.co`**.
  - Ticket delivery: **`From: tickets@isol8.co`**, **`To: support@isol8.co`**,
    **`Reply-To: <reporting user's email>`**.

**Why `tickets@` (not `support@`) as the ticket From:**
1. Avoids a `From == To` self-send (Gmail threads/filters self-addressed mail
   oddly).
2. Cleaner DMARC posture — Resend sends as a dedicated automated identity, not
   the primary human mailbox.
3. Inbox separation — tickets (`From: tickets@`) are filterable apart from
   direct customer email, which the agent uses for triage. `Reply-To` is the
   user, so replies are unaffected by the `From`.

---

## Piece A — Support capture (platform code, all users)

### Backend

- **`POST /api/v1/support/tickets`** (authenticated) in a new
  `routers/support_tickets.py`, registered in `main.py` alongside the other
  routers.
- **Request body (client-supplied, low-trust):** the user's typed message; a
  `category` (reuse the existing dropdown values from `app/support/page.tsx`);
  light client context — current route/URL, browser/OS/user-agent, app build
  SHA; and, when launched from the ErrorBoundary, the captured error payload
  (message + stack + component stack) plus the action that failed.
- **Server-side enrichment (authoritative, never client-trusted):**
  - **Core identity:** `user_id` (from JWT/`AuthContext`), email/name, `owner_id`,
    plan + provider path (Bedrock / ChatGPT OAuth / BYOK) from `billing_repo`,
    `env`, timestamp.
  - **Container runtime snapshot:** status/health, OpenClaw image version,
    service name, last provision time (from `container_repo` / `ecs_manager`).
  - **Agent debug deep-links:** PostHog person/replay URL (built from the
    `distinct_id`, which equals the Clerk `user_id`), CloudWatch Logs deep-links
    (user's container log group + backend request logs), admin user link.
  - **Recent request/trace IDs:** the last N backend `X-Request-ID`s for this
    user so the agent can grep exact log lines.
  - **Heavy client capture deliberately omitted** — reachable on demand via the
    PostHog person/replay link (session replay + forwarded exceptions already
    captured per `PostHogProvider.tsx`). Complementary durability: replays expire
    (~30d), request-ID → CloudWatch is durable for old tickets.
- **Delivery:** call `email_service.send_email` → Resend → `support@isol8.co`.
  Email body = a human-readable summary **+ a fenced JSON metadata block** so the
  agent parses the structured fields reliably rather than scraping prose.

### Frontend — four entry points (thin triggers into the one endpoint)

1. **Upgrade `/support` page** (`app/support/page.tsx`): replace the `mailto`
   form with a real authenticated form that POSTs via `useApi().post`. Show the
   simple `mailto` fallback only for signed-out visitors (see YAGNI note).
2. **ErrorBoundary "Report this error"** (`components/ErrorBoundary.tsx`): the
   fallback screen gets a one-click button that auto-attaches the error payload +
   client context and submits.
3. **Persistent Help button in the app shell** (chat + control header, e.g.
   `ChatLayout.tsx`): opens a lightweight report modal from anywhere.
4. **"Report" action on error toasts/banners:** when an API/gateway call surfaces
   an error toast, offer a "Report" action capturing that specific failure.

A shared `SupportTicketDialog` component (form + submit + the client-context
collector) backs surfaces 1, 3, and 4; the ErrorBoundary uses the same collector
with the error payload pre-filled.

### Signed-out visitors (YAGNI call)

The enriched endpoint is authenticated. Signed-out visitors on the landing
`/support` keep the simple `mailto` fallback for now — avoids building captcha /
anti-spam this round. Revisit if signed-out support volume warrants it.

---

## Piece B — Agent triage (config on the founders' container)

Runs on the founder's single prod Isol8 container — **not** a per-user feature
and **not** a `provision_org` change. One-time setup on the founders' org:

1. **Connect `support@isol8.co`** via the existing Google integration →
   materializes `gog` creds onto the container's EFS keyring (`gog_cli`
   materializer).
2. **Create a dedicated Support agent** on the founders' Paperclip company
   (Atlas-style second seeded agent) — focused debug playbook, keeps Friday's
   identity clean; Friday can delegate to it.
3. **Assign `support@isol8.co` to the Support agent** via the existing
   `/google/accounts/{email}/assign` seam → seeds its `IDENTITY.md` with "always
   act as `gog -a support@isol8.co`".
4. **Seed the Support agent's `AGENTS.md`** debug playbook: read the inbox via
   `gog` (filter `From: tickets@isol8.co` for app tickets), parse the fenced JSON
   metadata block, grep CloudWatch by request-ID, pull PostHog by `distinct_id`,
   write a triage summary + a **draft reply**.

### Autonomy

**Triage + draft, human sends.** The agent reads, investigates, and drafts a
reply, but a human approves and sends. No unsupervised customer email.

### Investigative reach (v1)

**CloudWatch + PostHog read**, read-only, **founders-container-only**:
- Scoped read-only creds materialized onto the founders' EFS via a **new small
  `providers.yaml` entry + materializer** (same shape as `gog_cli`) — this is the
  *only* platform code Piece B needs, and the grant only ever fires for the
  founders' org (not the shared task role, so no other container gets it).
- CloudWatch: query logs by request-ID / log group.
- PostHog: pull the user's recent events/session via the Persons API (existing
  `posthog_admin.py` path is the model).
- Admin-API reach is **out of scope** for v1 (higher PII surface; needs a
  security review).

### Durability

EFS is wiped on reprovision, so the Support agent's `AGENTS.md` doctrine + account
assignment must be seeded through a **reprovision-surviving seam** (the assign
seam already re-seeds `IDENTITY.md`; the doctrine must re-apply too) — otherwise
a future redeploy silently un-sets it.

---

## Piece C — Welcome email (platform code, all users)

- Hook the existing `user.created` handler in `routers/webhooks.py` (right after
  the current `user_repo.put`).
- Send one welcome via `email_service` → Resend, **`From: hello@isol8.co`**,
  **`Reply-To: support@isol8.co`** (so a plain reply lands in the agent-watched
  inbox).
- **Best-effort:** a send failure is logged and swallowed — it must never block
  signup / the webhook 200.
- Single transactional email, **no sequence**. Content: warm greeting, one-line
  what-to-do-next, link to `/chat`, link to support. Final copy reviewed at spec
  review.
- Trigger scope: **new owners on `user.created` only** — org members added later
  do not get one in v1.

---

## Cross-cutting

- **Trust boundary:** identity and account/container state are always
  server-derived from the JWT + repos, never taken from the client. The client
  only supplies the free-text message + benign context (route, browser, error
  payload).
- **PII:** ticket emails carry user data and go *only* to the founders' own
  `support@` inbox. PostHog masking is already configured (`maskInputOptions`,
  `[data-private]`). Admin-API agent reach deferred specifically to avoid exposing
  other users' data.
- **Failure isolation:** email sends are best-effort and never block the primary
  flow (signup webhook, ticket POST returns success even if the downstream email
  is retried/queued — decide retry posture in the plan).

## Out of scope (v1)

- DDB ticket store + admin queue UI (email-as-queue only).
- Auto-reply to customers (human-in-the-loop only).
- Admin-API agent reach.
- Onboarding / drip email sequences.
- Enriched tickets for signed-out visitors (mailto fallback).
- Network request/response body capture (PostHog network bodies stay off for PII).

## Testing approach

- **Backend:** unit-test the enrichment (mocked repos → asserted metadata block),
  the `email_service` seam (mocked Resend client), and the `support/tickets`
  route (auth required, enrichment present, delivery called). Welcome-email hook:
  assert it fires on `user.created` and that a send failure does not break the
  webhook.
- **Frontend:** targeted vitest on the `SupportTicketDialog` collector + the
  ErrorBoundary report path (per the repo's single-file-vitest constraint — no
  full `pnpm test`).
- **Piece B:** validated live on the founders' container (connect → assign →
  seed → read a test ticket → confirm a draft reply), since it's config not
  platform code.

## Suggested build sequence

1. Foundation (Resend client + secret + domain verification).
2. Piece C (welcome email) — smallest, proves the foundation end-to-end.
3. Piece A backend (endpoint + enrichment + delivery), then the four frontend
   surfaces.
4. Piece B — credential-rail materializer (platform code), then one-time
   container setup + agent doctrine.

## Key file touchpoints (from codebase exploration)

- `apps/backend/routers/webhooks.py` — `user.created` handler (welcome hook).
- `apps/backend/routers/support_tickets.py` — **new** ticket endpoint.
- `apps/backend/core/services/email_service.py` — **new** Resend seam.
- `apps/backend/core/config.py` — `RESEND_API_KEY` + sender config.
- `apps/backend/core/observability/middleware.py` — `X-Request-ID` source.
- `apps/backend/core/services/credentials/providers.yaml` + `materializers/` —
  the CloudWatch/PostHog read-creds rail (Piece B).
- `apps/backend/core/services/google_identity.py` + `routers/google_accounts.py`
  — account-assignment seam (Piece B).
- `apps/frontend/src/app/support/page.tsx` — upgrade form.
- `apps/frontend/src/components/ErrorBoundary.tsx` — report button.
- `apps/frontend/src/components/chat/ChatLayout.tsx` — persistent Help button.
- `apps/frontend/src/lib/api.ts` — `useApi().post` for the ticket call.
- `apps/infra/lib/stacks/auth-stack.ts` + `service-stack.ts` — `RESEND_API_KEY`
  secret wiring.
