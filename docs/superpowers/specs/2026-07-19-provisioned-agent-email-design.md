# Provisioned Agent Email — Design

- **Date:** 2026-07-19
- **Status:** Approved design (pre-implementation)
- **Author:** (brainstorm session)

## 1. Summary

Give every owner's agent (Friday, in v1) a real email address — `<handle>@agents.isol8.co` —
that the owner can **email to talk to their agent** and that the agent **replies from**.
It is a bidirectional, threaded email conversation between a user and their agent,
provisioned automatically at onboarding with zero setup, à la Polsia's per-company
`name@polsia.app`.

The primary job on the free tier is **conversion**: a low-friction "email your agent and
it answers" hook. Real *building* stays behind the paywall — but the email pipeline itself
is **identical across tiers**; the only differences are the levers that already tier the
platform (container on/off, spend cap, capability gates).

## 2. Goals / Non-goals

**Goals (v1)**
- One provisioned address per owner, routed to Friday.
- Inbound: owner emails the address → the agent receives it as a conversation turn.
- Outbound: the agent replies by email, threaded into the user's client.
- Same pipeline for free and paid; tier = gated features + container lifecycle.
- Automatic provisioning at onboarding; the agent knows its own address.
- Data model built so **per-agent addresses** drop in later as a gated feature.

**Non-goals (v1 — explicit deferrals)**
- **Proactive / scheduled email** (morning digest, nightly-CEO report). That needs the
  always-on scheduler (paid-only) and is a clean fast-follow.
- **Public inbox** ("agent as front desk" — anyone can email it). Schema is forward-
  compatible (`public` flag) but the behavior is off in v1.
- **Per-agent / multiple addresses per owner.** Schema supports it; UI + gating come later.
- **Bring-your-own domain** for a user's company (later; v1 is an isol8 subdomain).
- **Paperclip Inbox as an email surface** (see §4, superseded decision).

## 3. Key decisions (and why)

| Decision | Choice | Rationale |
|---|---|---|
| Routing target | One address per owner → Friday | Simplest; mirrors Polsia; schema keyed per-`agent_id` so per-agent is "more rows, gated" later. |
| Delivery surface | **Direct agent conversation** (not Paperclip) | See §4. Paperclip won't hold a trigger for a sleeping container and adds a circuit-breaker footgun; direct keeps the backend a dumb router. |
| Sender policy | **Owner-verified senders only** | A private line. Prevents spoofed strangers triggering billable runs. Forward-compatible to public via a per-address `public` flag. |
| Outbound scope | **Reactive reply loop only** | Proactive digest pulls in the always-on scheduler (paid-only) — deferred. |
| Vendor | **Resend, both directions** | Inbound is GA (custom domain, parsed webhooks, signature verification, durable storage, Attachments API); outbound already runs through our `email_service` seam. One vendor honors "one system for both." Backend-mediated, so the vendor is purely a backend concern. |
| Tiering | **One pipeline, gated** | Free vs paid is not two designs — it's container on/off + spend cap + capability gates, all existing levers. |

## 4. Why direct conversation, not Paperclip (superseded decision)

An earlier step chose "Paperclip Inbox item" as the delivery surface. Verification of the
deployed Paperclip fork (`~/Desktop/paperclip-fork.nosync`) overturned that:

- A triggered run (issue comment / assignment / routine) is dispatched **immediately** and
  dials a **fresh WebSocket** to the agent's runtime. If the container is scaled to zero the
  connect fails within seconds and the run is marked terminal `failed`.
- There is **no "pending until runtime up" queue**; waking the container a minute later does
  **not** replay the trigger. The backoff-retry ladder (`2m/10m/30m/2h`) is reserved for
  `transient_upstream` (Claude/Codex 5xx) errors — a dead gateway does not qualify.
- Repeated blind fires at a dead container **trip the routine circuit breaker**
  (auto-pause after 3 consecutive failures).

So Paperclip delivery would force the backend into elaborate wake-first orchestration on the
free tier — exactly the statefulness we want to avoid. **Direct delivery** (below) sidesteps
all of it. Paperclip remains where the *paid-capable* agent does real work — but it's a tool
the agent *uses mid-conversation*, not a surface the email lands on.

## 5. Architecture overview

```
                    ┌──────────────────────────────── Resend (agents.isol8.co) ───────────┐
  user's inbox  ◄───┤  outbound send (In-Reply-To/References)   inbound receive (MX)       │
                    └───────▲───────────────────────────────────────────┬──────────────────┘
                            │ relay (from = owner's addr, rate-limited)  │ email.received webhook
                            │                                            ▼
                    ┌───────┴────────────────────────────── Isol8 backend (FastAPI) ───────┐
                    │  /webhooks/resend-inbound        POST /internal/agent-mail/send        │
                    │   • verify signature              • authed by container token          │
                    │   • addr → (owner, agent)         • enforce from = owner's addr        │
                    │   • sender allowlist              • rate-limit + loop guard             │
                    │   • trial-cap gate                • Resend send                         │
                    │   • container running? ───────────────────────┐                        │
                    └───────────────────────────────────────────────┼────────────────────────┘
                              deliver (fire-and-forget)               ▲ isol8-mail tool call
                                      │                               │
                                      ▼                               │
                    ┌──────────── per-user OpenClaw container ────────┴────────┐
                    │  native /hooks/agent wakes the agent with                 │
                    │  the email → agent converses → calls `isol8-mail reply`   │
                    │  (no mail credentials in the container)                   │
                    └───────────────────────────────────────────────────────────┘
```

## 6. Components

### 6.1 Mail vendor — Resend, dedicated receiving domain
- Verify a **dedicated subdomain `agents.isol8.co`** in Resend (its own SPF/DKIM/DMARC).
  **Never the `isol8.co` apex** — apex MX is already used for `support@`/`tickets@` receiving.
- Register an `email.received` webhook → backend. Resend parses MIME and delivers JSON
  (from/to/subject/text/html + attachment metadata) and **stores the message durably** even
  if our webhook is down (replayable — we lean on this instead of building a queue).
- Outbound continues through the existing seam `core/services/email_service.py`
  (`send_email`, gated on a real `re_…` key). Agent replies set the `from` to the owner's
  `@agents.isol8.co` address and include `In-Reply-To`/`References` for threading.
- **Reputation isolation (optional knob):** agent mail lives on its own receiving domain
  already; if desired it can use a separate Resend API key so agent deliverability can never
  affect company transactional mail.

### 6.2 `agent-email-addresses` DynamoDB table (new)
- **PK** = full address, lowercased (e.g. `friday-a1b2c3@agents.isol8.co`).
- Attributes: `owner_id`, `agent_id`, `verified_senders: [str]`, `public: bool (=false)`,
  `status`, `created_at`.
- **GSI** `by-owner` on `owner_id` (list an owner's addresses).
- Repository `core/repositories/agent_email_repo.py`, modeled on the existing
  `channel_link_repo.py` (plain-dict items, no ORM).
- Provisioned by CDK `database-stack.ts` as `isol8-{env}-agent-email-addresses` and addressed
  by the **prefix convention** (`core.dynamodb.get_table("agent-email-addresses")`), exactly
  like `channel-links` — **no table-name env var**. Grants are explicit per-table
  (`grantReadWriteData` in `service-stack.ts`); there is no wildcard DDB policy.
- The feature's kill switch is **`AGENT_EMAIL_DOMAIN`** instead: empty ⇒ no address is ever
  minted, so the whole machinery can ship inert and be switched on per environment.

### 6.3 Address minting (in provisioning)
- Mint the address row inside `paperclip_provisioning.provision_org` — the same place Friday
  and Atlas are seeded — so every newly provisioned owner gets it with zero extra calls.
  Idempotent (short-circuit if a row already exists), matching `provision_org`'s contract.
- **Local part is derived from the owner's company slug** (see §6.3a — this is a hard
  prerequisite). The address is a conversion asset, so it must look like a real company
  address, not a hash:
  - **Primary agent (Friday):** `{slug}@agents.isol8.co` — e.g. `acme@agents.isol8.co`.
    The company's main address is its CEO's, which is the Polsia shape.
  - **Additional agents (later, gated):** `{slug}-{agent}@agents.isol8.co` — e.g.
    `acme-atlas@agents.isol8.co`.
- Address uniqueness follows from **slug** uniqueness, which is reserved atomically (§6.3a).
  The address does **not** need to be secret — the sender allowlist is the security boundary,
  not obscurity.
- **Alias on rename.** `status` is `"active"` (the current primary) or `"alias"`. Renaming the
  company mints a new `active` address and demotes the old row to `alias`; **aliases keep
  routing forever.** A rename must never break a live email thread or orphan mail in flight.
  Many addresses → one agent is already natural given the address-keyed PK + `by-owner` GSI.
- Seed `verified_senders` with the owner's Clerk primary email.
- Backfill for existing owners: a one-shot script (archive after running, per repo policy).

### 6.3a Company identity (prerequisite — separate plan)

The email slug has no source today: `provision_org` currently names the Paperclip company
`name=owner_email` (`paperclip_provisioning.py:327,338`), i.e. literally `user@gmail.com`, and
our backend exposes **no rename path**. A sensible address therefore depends on first giving
the company a real, editable name:

- **Auto-generate at provisioning, then editable** — no extra step in the signup funnel.
- **Slug reservation** must be atomic (conditional write) so two "Acme" owners can't collide;
  collisions take a numeric suffix.
- **Rename** updates the name, re-reserves the slug, and triggers the alias-mint above.

This is scoped as its own plan and sequenced **before** `AGENT_EMAIL_DOMAIN` is ever switched
on — which is safe precisely because the email feature ships inert (§8/Plan 1), so no
hash-shaped or email-shaped address is ever minted in the interim.

### 6.4 Inbound webhook + router
- `routers/agent_mail.py` → `POST /api/v1/webhooks/resend-inbound` (public, no Clerk; like
  the Stripe/Clerk webhook routers). Steps, all synchronous and stateless:
  1. **Verify Resend webhook signature** (reject otherwise).
  2. Resolve `to` address → `(owner_id, agent_id)` via `agent_email_repo`. Unknown → drop.
  3. Check `from` ∈ `verified_senders` (unless `public`). Not allowed → drop (optionally a
     one-time bounce; see §9). This is the anti-spoof / anti-billing-abuse gate.
  4. **Loop guard**: drop if `Auto-Submitted: auto-*` / `Precedence: bulk`, or if this thread
     has exceeded a max-replies-per-thread counter.
  5. **Trial-cap gate**: reuse the existing spend gate (each inbound = one agent run). Over
     cap → drop (or a single "you're at your limit" bounce).
  6. **Deliver** (§6.5) if the container is running; if asleep, do nothing (§7).

### 6.5 Delivery into the agent — DECIDED: OpenClaw native `/hooks/agent`
Deliver the email to the running container as a **fire-and-forget** event; the agent replies
independently via the `isol8-mail` tool, so **the backend never captures a response**.

- **Decision: OpenClaw native `/hooks/agent` webhook.** Credential-free, purpose-built for
  "an external event wakes the agent," fire-and-forget (no RPC to hold open), and semantically
  an inbound event rather than a live chat. Requires `hooks.token` in `openclaw.json` (written
  at provision time) and in-VPC HTTP from the backend to the container's gateway port.
  - Pass a **reply-correlation id** (thread id + original `Message-ID`) in the hook payload so
    the agent's reply tool threads correctly, and a **thread hint** so multi-mail threads land
    in the same agent session/context.
- **Documented fallback (only if hook wiring blocks):** `chat.send` gateway RPC
  (`connection_pool.send_rpc(method="chat.send", …)`, the exact path `websocket_chat.py` uses).
  Reuses proven plumbing and gives natural session threading via a thread-derived `sessionKey`;
  the backend must not block on run completion. Not the chosen path — recorded for contingency.

### 6.6 Outbound relay + `isol8-mail` tool
- **Relay endpoint** `POST /api/v1/internal/agent-mail/send`, authenticated by the container's
  existing token (gateway/service token — the same trust the container already holds). Payload:
  `{ correlation_id, subject?, body_text, body_html? }`.
  - Backend **enforces `from` = the calling container's own provisioned address** (looked up by
    owner, not taken from the request) → a container can only send as itself.
  - Rate-limit per owner + per thread; loop-guard counter increment.
  - Send via `email_service.send_email` with `In-Reply-To`/`References` from the correlation id.
- **`isol8-mail` tool**: a small binary/tool baked into the extended image
  (`apps/infra/openclaw/`) that POSTs to the relay. **No mail credentials in the container** —
  the shared `agents.isol8.co` reputation makes an in-container key a fleet-wide spam risk, and
  a free-tier `exec` agent can read its own env. The relay holds the only key.

### 6.7 Agent-facing surface (reuses baked infra)
- `core/services/owner_facts.py` writes `email_address` (and a capability hint: talk-only vs
  build-capable) into `~/.openclaw/isol8-facts.json`.
- The baked **isol8 skill** (`apps/infra/openclaw/skills/isol8`) gains a short section: *"You
  have an email address (see isol8-facts). When you receive an email event, reply using the
  `isol8-mail` tool. [tier-appropriate behavior]."* Update its maintenance-header anchors.

## 7. Free-tier "asleep" behavior

Free-tier containers scale to zero; paid stay always-on. When mail arrives for a **sleeping**
container there is nowhere to deliver, so the backend **does nothing** — the message stays
durably in Resend, unanswered. **No backend wake machinery is built.** This is an accepted,
intentional limitation: on the free tier you can talk to your agent when it's up; real always-
on behavior is a paid feature.

- **Optional, low-cost enhancement (recommended, not required):** when the user next reopens
  the app and the container wakes **on its own** (existing `/container/status` poll →
  `start_user_service`), drain any inbound Resend received while it was down (query Resend for
  recent unprocessed messages for that address, run each through §6.4 step 6). This rescues the
  conversion UX for near-zero cost and still adds **no** backend-initiated wake.

## 8. Tier = gates (mapped to existing machinery)

| Concern | Free | Paid | Existing lever |
|---|---|---|---|
| Container | scale-to-zero → silent when asleep | always-on → always answers | container lifecycle (`ecs_manager`) |
| Spend | $3 cap; each inbound = one run vs cap | metered | bedrock-gate / trial-cap gate |
| Capability | converse | converse **+ build/execute** (tools, Paperclip work, longer runs) | config-policy + owner-facts |
| Addresses | 1 (Friday) | multiple / per-agent *(later)* | `agent-email-addresses` + gate |

There is **no email-specific tier code** — the pipeline is identical and rides what already
tiers the platform.

## 9. Attachments (store-and-link)

Inbound attachments are **stored and linked**, not inlined:
- On `email.received`, call Resend's **Attachments API** to fetch metadata + `download_url`.
- Copy each attachment into an S3 bucket (`AGENT_EMAIL_ATTACHMENTS_BUCKET`) under
  `inbound/{owner_id}/{message_id}/{filename}`. Note the bucket is created in
  **`service-stack.ts`**, not `database-stack.ts` — that's where the marketplace artifacts
  bucket lives, and buckets follow that precedent.
- Deliver to the agent a **manifest** (filenames, sizes, content-types, S3 keys / signed URLs)
  rather than raw bytes, so the agent can reference or fetch them as needed.
- Enforce a per-message size/count cap; scan/verdict per Resend before storing.
- Outbound attachments are **out of scope for v1** (reply is text/html only).

## 10. Security & abuse

- **Subdomain only** — never `isol8.co` apex (MX collision with existing receiving).
- **Sender allowlist** — spoofed strangers can't trigger billable runs; enforced via the
  `verified_senders` list (see "Inbound auth model" below for why this, not SPF/DKIM/DMARC,
  is the primary boundary).
- **No mail credentials in the container** — outbound is a backend relay; the relay pins the
  `from` address, so one compromised/prompt-injected container can only ever send as itself.
- **Loop / auto-reply guard** — `Auto-Submitted`/`Precedence` headers + max-replies-per-thread,
  so an out-of-office autoresponder can't ping-pong the agent into unbounded spend.
- **Spend gating** — inbound reuses the existing trial-cap gate; each inbound is a run.
- **Rate limits** — per owner and per thread on the relay.
- **Webhook signature verification** on the inbound endpoint.

### 10.1 Inbound auth model — Resend Agent Email Inbox Level 1

Resend's own guidance for an agent-facing inbox is a 5-level model. Level 1, **"Strict
Allowlist — only process emails from known senders,"** is what we implement as the v1
boundary: the `verified_senders` check in §10 above. Resend's documented model does not
recommend checking SPF/DKIM/DMARC for this use case, and Resend's receiving API surfaces no
authentication-verdict field — so the `Authentication-Results` header check in
`sender_authentication_verdict` (`core/services/agent_mail_inbound.py`) is opportunistic
defense-in-depth, not a control Resend asks for: it rejects mail that **explicitly fails**
SPF/DKIM/DMARC when a mail relay happens to stamp the header, and delivers everything else
(no verdict is the common case, per Resend). There is no strict-mode flag to also drop
no-verdict mail — Resend surfaces no verdict field at all, so such a flag could only ever
blackhole all inbound mail.

The From header is a filter, not a spoof-proof fence. The protections that actually hold
under a forged sender are structural: per-owner isolation (a forged sender can only reach
the one agent/owner the recipient address belongs to), the spend cap (bounds the blast
radius of any run that does get through), and replies going only to the genuine From address
captured at receipt time (never to an address the mail body claims).

Higher assurance than Level 1 is a known future direction, not v1 scope: Resend's Level 4
(sandboxed / restricted agent capabilities for processing mail) and Level 5 (human-in-the-loop
approval before an agent acts on inbound mail).

## 11. Error handling

- Unknown recipient / disallowed sender / over-cap / loop → **drop** (structured log + metric);
  optional single bounce for disallowed-sender and over-cap so the human isn't left confused.
- Delivery failure to a *running* container → log + metric; Resend still holds the message
  (replayable on next wake per §7 optional drain). No retry storm against the container.
- Relay send failure → surface a tool error to the agent (best-effort, matching
  `email_service`'s never-raise posture) + metric.
- All paths emit metrics (`agent_mail.inbound.{delivered,dropped_*}`, `agent_mail.outbound.*`)
  for observability.

## 12. Testing

- **Unit:** address resolution; sender-allowlist accept/reject; webhook signature verify;
  relay `from`-address enforcement (a container cannot send as another owner); trial-cap gate;
  loop guard; attachment manifest building.
- **Integration:** inbound webhook → delivery (container/gateway mocked); relay → Resend
  (mocked); asleep-container path (no delivery, no wake). No live email in CI.
- **Provisioning:** `provision_org` mints exactly one idempotent address row; owner-facts +
  isol8 skill carry the address.

## 13. Open questions / future

- **Disallowed-sender / over-cap bounce** — send a one-time courtesy bounce, or stay silent?
- **Per-agent addresses**, **public "front-desk" inbox**, **proactive digest**, **BYO domain**
  — all schema-forward-compatible, gated for later.

## 14. Relevant existing anchors (point-in-time; verify before editing)

- Channels are **container-side**; email is our **first backend-mediated inbound channel**
  (`routers/channels.py`, `core/repositories/channel_link_repo.py`, `core/containers/config.py`
  `CHANNEL_PROVIDER_IDS`).
- Container wake is **async, frontend-poll-only**; no backend wake path exists — we don't build
  one (`routers/container.py` `GET /container/status`, `core/containers/ecs_manager.py`
  `start_user_service`).
- Server-initiated agent delivery primitive: `connection_pool.send_rpc(method="chat.send", …)`
  keyed by `owner_id` + container IP + `gateway_token` (`core/gateway/connection_pool.py`,
  `routers/websocket_chat.py`).
- OpenClaw has **no native email send**; native inbound is Gmail-only (`gog` CLI + GCP Pub/Sub,
  `hooks.gmail.*`) with credentials **inside the container** → rejected. Generic `/hooks/agent`
  is the credential-free delivery lever (fork `~/Desktop/agent`).
- Outbound seam: `core/services/email_service.py` (Resend, 2 existing callers: welcome, support).
- Provisioning + agent facts: `paperclip_provisioning.provision_org`,
  `core/services/owner_facts.py` → `isol8-facts.json`, baked `apps/infra/openclaw/skills/isol8`.
