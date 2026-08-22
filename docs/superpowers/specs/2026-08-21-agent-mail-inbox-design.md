# Agent mail: inbox, allowlisted wake, free send — design

Status: approved in chat 2026-08-21 (brainstorm + ponytail pass). Disposable — delete when shipped.

## Goal

Any agent (not just Friday) can use its `{agent}@{slug}.isol8.co` address as a real
account identity: sign up for SaaS by reading the verification mail itself, be
reachable by people the owner allows, and email anyone the owner lets it — on a
per-owner SES tenant so one bad owner never pauses the account.

Non-goals (v1): owner-facing inbox reader, phone/SMS, payment cards, custom MAIL
FROM, dedicated IPs, egress-IP work for CAPTCHA walls (separate track).

## What exists (read before changing)

- `core/services/agent_email.py` — mint is generic; the ONLY caller
  (`agent_mail_owner_send.send_to_owner`) hard-codes Friday.
- `routers/ses_inbound.py` — gates (secret, dedup, spam/DMARC, auto-submitted, trial
  cap) then wakes the agent via `agent_mail_delivery.deliver_to_agent`; drops any
  sender not in `verified_senders` (`dropped_unverified_sender`). `public` flag exists
  on the row but nothing ever sets it.
- Outbound doors: `routers/agent_mail_relay.py` (`POST /agent-mail/reply`, Fernet
  reply-token from `core/services/agent_mail_token.py`) and `routers/agent_mail_send.py`
  (`POST /agent-mail/send-to-owner/u/{owner_id}`, first-contact state machine +
  `DAILY_SEND_CAP=10` in `core/services/agent_mail_owner_send.py`). Skill scripts
  `isol8-mail.sh` (reply) and `isol8-owner-email.sh`.
- SES: `isol8.co` identity verified, Easy DKIM, wildcard MX/SPF for `*.isol8.co`,
  DMARC `p=none` at the org domain, production access granted, 50k/day quota. NO
  configuration set, NO event destination, NO tenants. Backend has
  `AGENT_EMAIL_ATTACHMENTS_BUCKET` env but no IAM on it.
- Frontend: agent email appears only on admin pages. Per-agent control UI lives in
  `control/panels/AgentChannelsSection.tsx`, rendered by `AgentsPanel.tsx:246`.

## Design

### 1. Rows (`agent-email-addresses`, existing table)

- `verified_senders: list[str]` — unchanged name; entries are exact addresses **or**
  `@domain` (matched on the sender's domain). Still seeded with the owner's email.
- `send_to_anyone: bool` (default False) — owner opt-in for free-recipient send.
- `outbound_disabled: str | None` — reason string when a complaint disabled outbound.
- `public` — deleted (dead).

Inbox messages live in a new per-env table `agent-mail-inbox` (pk `address`, sk
`received_key = {received_at}#{message_id}`, 30-day TTL). (The brainstorm said "S3
prefix on the attachments bucket"; that bucket is prod-only and the backend has no
IAM on it, so a per-env table is the smaller change — see the plan.)

### 2. Inbound (`ses_inbound`)

After the existing gates (secret → dedup → known recipient → spam/virus → DMARC/SPF →
auto-submitted):

1. **Store** the parsed payload (from/subject/text/threading headers/attachments +
   `received_at`) as a row in `agent-mail-inbox`. Bodies are bounded (truncate `text`
   at 64 KB). Attachment `download_url`s are the Lambda's presigned URLs and expire —
   acceptable (`# ponytail`).
2. **Wake** only if the sender matches the owner or `verified_senders` (exact or
   `@domain`). Everything else returns `stored_not_woken` instead of
   `dropped_unverified_sender`.
3. The delivered chat message carries the S3 key (`Reply-Key: inbox/...`) instead of
   a Fernet reply token. `agent_mail_token.py` is deleted.
4. Trial-cap autoreply stays owner-only (unchanged).

### 3. Outbound — one door

`POST /api/v1/agent-mail/u/{owner_id}/send` — hook-token auth (same timing-safe
compare the two deleted routes used), per-owner in-process per-minute limiter.

Body: `{agent_id, to?, subject, body_text, body_html?, reply_key?}`. Exactly one of
`to` / `reply_key` / (neither ⇒ owner).

Policy, one function, evaluated server-side:

| recipient | allowed when | side effects |
|---|---|---|
| owner (no `to`, no `reply_key`) | always | first-contact state machine as today |
| `reply_key` under `inbox/{one of this owner's addresses}/` | `outbound_disabled` is None | `To` = that message's sender; `In-Reply-To`/`References` from its headers |
| arbitrary `to` | `send_to_anyone` && `outbound_disabled` is None | — |

All paths: `DAILY_SEND_CAP` (raised 10 → 50, per owner, the existing durable counter).

First use for an agent: mint `{agent}@{slug}.isol8.co` for **that** `agent_id`
(agent name looked up from the owner's Paperclip roster; today's mint un-hardcoded),
then idempotent `CreateTenant(owner-{owner_id})` + resource associations (identity
`isol8.co`, the config set). Every send stamps `ConfigurationSetName` and
`TenantName`. Tenant name is deterministic — nothing stored.

Deleted: `routers/agent_mail_relay.py`, `routers/agent_mail_send.py`,
`core/services/agent_mail_token.py`, `scripts/isol8-owner-email.sh`. The owner-send
service (`agent_mail_owner_send.py`) becomes the owner branch of the one send service.
Reply tokens in chat messages delivered before this ships stop working — accepted.

### 4. Read — one route

`GET /api/v1/agent-mail/u/{owner_id}/inbox?agent_id=&since=&limit=` — hook-token
auth. Queries the agent's address partition newest-first (`received_key > since`),
returns `{address, messages: [{key, from, subject, received_at, text}]}` with `text`
bounded at 8 KB; `key` is the reply key `"{address}/{received_key}"`. A sibling
`POST /api/v1/agent-mail/u/{owner_id}/address {agent_id}` mints-or-returns the
agent's address (the CLI's `address` subcommand needs it BEFORE any send — that is
the address the agent types into a signup form).

### 5. CLI + skill (`apps/infra/openclaw/skills/isol8/`)

One script `isol8-mail.sh` replaces both:

```
isol8-mail.sh address                       # mints if needed, prints the address
isol8-mail.sh inbox [--since ISO] [--limit N]
isol8-mail.sh send --owner  --subject S      # body on stdin
isol8-mail.sh send --to ADDR --subject S
isol8-mail.sh send --reply KEY
```

`owner_id` from `isol8-facts.json` (as today). `agent_id` from the exec cwd
(`workspace-{agent_id}`); `--as <agent_id>` overrides. (Plan: verify the exec tool's
default cwd is the agent workspace in the fork; if not, the skill tells the agent to
pass `--as`.) SKILL.md: one "Your email" section (address, inbox, who can wake you,
send rules) + "Getting a verification code" (sign up with your address → `inbox` →
read the code). `owner_facts` unchanged.

### 6. SES / CDK (`ses-inbound-stack.ts`, `service-stack.ts`)

- `CfnConfigurationSet isol8-{env}-agent-mail` + `CfnConfigurationSetEventDestination`
  with `matchingEventTypes: [COMPLAINT]` → EventBridge (typed in aws-cdk-lib 2.190).
- `events.Rule` (source `aws.ses`, complaint detail-type) → `events.ApiDestination`
  (`events.Connection` with API-key auth = the existing SES ingest shared secret, header
  `X-Isol8-SES-Secret`) → `POST https://api-{env}.isol8.co/api/v1/webhooks/ses-events`.
- Backend handler: secret check → `detail.mail.source` → address row → set
  `outbound_disabled="complaint"` on all of that owner's rows → email the owner through
  the send door ("Outbound email paused — a recipient marked a message as spam. Re-enable
  in Control → Agents → Email.").
- The config set + rule + API destination live in a small per-env stack
  (`agent-mail-outbound-stack.ts`) — `ses-inbound-stack.ts` is prod-only (receipt
  rule sets are account-scoped) and dev needs its own config set.
- IAM (task role): `grantReadWriteData` on the inbox table; `ses:CreateTenant`,
  `ses:GetTenant`, `ses:CreateTenantResourceAssociation`,
  `ses:UpdateReputationEntityPolicy`, `ses:SendEmail` on `tenant/*`, the config set and
  the `isol8.co` identity ARNs. Tenants get the SES-managed `standard` reputation
  policy (`arn:aws:ses:{region}:aws:reputation-policy/standard`).
- Per-owner tenants are created by the backend (like per-owner Bedrock AIPs), not CDK.
  No `aws-cdk-lib` bump.

### 7. Owner UI

`AgentEmailSection({agentId})` as its own **Email** tab in `AgentsPanel.tsx` (the
Channels tab is subscription-gated; agent email exists on the free tier too):
address (or "minted on first use"), `verified_senders` editor (emails / `@domain`),
"Let this agent email anyone" switch, banner when `outbound_disabled` with a re-enable
action. Backed by `GET /api/v1/agent-mail/addresses` (owner's rows) and
`PATCH /api/v1/agent-mail/addresses/{agent_id}` (`verified_senders`, `send_to_anyone`,
`outbound_disabled: null` to re-enable). PATCH mints the row if missing.

### 8. Tests

Backend: policy function (owner / reply / free / disabled / cap), store-vs-wake split
(owner, exact, `@domain`, stranger), complaint handler, mint for a non-Friday agent,
`@domain` matching. Chaos: add the two `/u/{owner_id}` routes to
`NEGATIVE_PASS_EXCLUSIONS`, drop the `send-to-owner` entry. Frontend: render
self-review of the card (no vitest).

## Rollout

Dev first (`dev` mail domain already has wildcard MX). Existing rows lack the new
fields — readers treat absent as False/None. Delete this spec in the shipping PR.
