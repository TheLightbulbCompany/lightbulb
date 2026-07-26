# Agent first-contact email — design

- **Date:** 2026-07-25
- **Status:** Design, approved in conversation, pre-implementation
- **Supersedes:** the "Friday's first email" portion of #1365, deleted in `be2ffea52`

## Problem

Every new owner should receive an email from Friday once her first run is
done — in her voice, referencing what she learned. It is a defining product
moment: the first time the agent reaches *out* rather than waiting to be
addressed.

This does not happen today, and never has. Not once, in any environment.

## What exists today (verified 2026-07-25)

```
sign-up → Clerk user.created  (or /container/status poll fallback)
    ↓
evaluate_provision_gate → trial_local row → gate clears
    ↓
provision_org  (sync)
   ├─ Paperclip signup + company create
   │    name = generate_company_name() → PLACEHOLDER
   │      orgs: Clerk org name | personal: prettified email local part
   ├─ allocate_company_slug()                  ← slug fixed here
   ├─ seed Friday (CEO), seed Atlas
   ├─ purge builtin agents
   └─ mint_agent_email_address("Friday")       ← address minted here, frozen
    ↓
background_provision → ECS + EFS + write_openclaw_config
   └─ write_owner_facts() → isol8-facts.json gains email_address
    ↓
user submits funnel → POST /onboarding/brief
   ├─ persists onboarding_brief_text, claims funnel submission
   └─ stages BRIEF.md into Friday's workspace
    ↓
maybe_dispatch_bootstrap()   ← from submit_brief AND the /status poll
   ├─ guards: funnel submitted, bootstrap_state not_started, runtime reachable
   ├─ claims bootstrap_state: not_started → dispatched
   ├─ chat.send( build_first_run_message(brief) )
   └─ register_run_completion_callback → chat.final → mark_bootstrap_delivered
    ↓
Friday researches, writes up, files three backlog tasks
    ↓
NOTHING FURTHER HAPPENS
```

Friday is never told to name the company, and never told to email anyone.
**No cold-send path exists in the codebase.** `encode_reply_token` is minted
in exactly one place — `routers/ses_inbound.py` — which only runs when mail
*arrives*. The address minted during provisioning can therefore only ever
receive, and only from the owner's own address (`verified_senders`).

### Why the previous attempt never fired

#1365 did ship a cold-send path: `agent_mail_token.mint_first_contact_token`,
a one-recipient token with the owner's address baked in. Friday was told to
use it by a `## Send your first email` section rendered into **BOOTSTRAP.md**.

`be2ffea52` deleted both. Its commit message records the reason: BOOTSTRAP.md
was **inert** — OpenClaw's `reconcileWorkspaceBootstrapCompletionState` never
surfaces a seeded BOOTSTRAP.md into Friday's context once `setupCompletedAt`
is pre-marked. She never saw the instruction, so the token minting was dead
code and was removed as cleanup.

The transport was fine. The delivery vehicle for the *instruction* was not.
This design must not reuse it.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **Mint after Friday names the company**, not at provisioning | The slug becomes an email address other people are given, so freezing it is right — but freezing an auto-generated placeholder the user never chose is not. Prod has one owner and zero external users, so moving this is nearly free now and expensive later. |
| 2 | **Friday writes the email**, tailored | Preserves the product moment. Reuses the relay and `isol8-mail.sh` unchanged. |
| 3 | **Friday invokes it via a tool call**, not a `chat.final` observer | She decides when her work is done. Collapses mint + send into one request and removes the need for a token entirely. |
| 4 | **Retry, then alarm** | The previous version failed silently for its entire life. Nothing here may disappear quietly. |

## Architecture

```
bootstrap_dispatcher dispatches Friday's first run
  (kickoff prompt gains: name the company, then send the first email)
              ↓
     Friday works … then SHE decides she is done
              ↓
  printf '%s' "<body>" | isol8-first-email.sh --subject "…"
              ↓
POST /api/v1/agent-mail/first-contact/u/{owner_id}
Authorization: Bearer {hook_token}
  1. auth        container_repo.get_by_owner_id + timing-safe compare
  2. rate limit  60/min per owner (own bucket)
  3. claim       first_contact_state: not_started → sending (conditional)
  4. address     read Paperclip company name  ← whatever she just set
                 allocate_company_slug()      ← idempotent per owner
                 mint_agent_email_address()   ← idempotent per owner
  5. facts       write_owner_facts()          ← email_address now populated
  6. send        SES: From = minted address, To = owner_email (server-derived)
  7. settle      first_contact_state = sent | failed, emit metric
```

**No token.** The deleted `mint_first_contact_token` existed only to bake a
recipient into a value Friday could hold. An endpoint that already knows who
she is derives the recipient from `owner_id` server-side — nothing to mint,
nothing to expire, and no routing credential sitting in a prompt where it
could be pasted into a visible body. This is both simpler and tighter than
the design it replaces.

**No orchestration.** Minting is a lazy get-or-create inside the one endpoint
that needs an address. `allocate_company_slug` already returns an owner's
existing reservation and `mint_agent_email_address` already returns the
existing row on an ownership match, so "mint late" costs one call on a path
that runs anyway.

### Security model

Reuses `hooks_proxy` verbatim: `/u/{owner_id}` names the owner, and the
`Authorization: Bearer` value is compared timing-safely against that owner's
`containers.hook_token`. Only that owner's own container process holds that
token, so a container can only ever trigger first contact for its own owner.

The recipient is **never** client-supplied. It is the owner's own email,
resolved server-side. There is no parameter that can redirect the message.

### Components

| Component | Change |
|---|---|
| `routers/agent_mail_first_contact.py` | **New.** Mirrors `agent_mail_relay.py` minus the reply-token. |
| `core/services/first_contact.py` | **New.** Claim → address → facts → send → settle. |
| `apps/infra/openclaw/skills/isol8/scripts/isol8-first-email.sh` | **New.** Sibling of `isol8-mail.sh`; body on stdin, `--subject` arg. |
| `apps/infra/openclaw/skills/isol8/SKILL.md` | Document the first-contact helper and when to use it. |
| `core/services/onboarding_brief.build_first_run_message` | Add the naming step and the send step. |
| `core/services/paperclip_provisioning.provision_org` | **Remove** `allocate_company_slug` + `mint_agent_email_address`. |
| `core/services/owner_facts.build_owner_facts` | Add `owner_id` so the script can build its URL. |
| `core/repositories/paperclip_repo` | `first_contact_state` claim/settle/re-arm methods. |
| `core/services/bootstrap_dispatcher._on_terminal` | Also inspect `first_contact_state` and dispatch a bounded nudge. Composes no mail. |

## State machine

`first_contact_state` lives on the paperclip-companies owner row beside
`bootstrap_state`, scoped to `onboarding_generation` (consistent with
`238f452d8`, which co-located onboarding state there).

```
not_started ──claim──> sending ──ok──> sent        (terminal)
     ▲                    │
     │                    └──fail──> failed        (terminal, alarms)
     │                                   │
     └────────── re-arm (bounded) ───────┘
```

- The claim is a **conditional write**, so two concurrent calls cannot both
  send. The loser gets `already_sent`.
- `sent` is terminal. A repeat call returns `{"status": "already_sent"}` and
  performs no SES call — the owner never receives a duplicate.
- `first_contact_attempts` bounds re-arming at **3**.

### Retry

If Friday never calls the endpoint the state simply stays `not_started`, so
something must notice.

**The `/container/status` poll cannot be that something.** It is off by the
time this matters — `useProvisioningState` documents the cadence as "0/off
once ready", and first contact happens *after* the owner is ready. Any retry
hung off the poll would never fire.

Instead the safety net reuses the run-completion callback
`bootstrap_dispatcher` **already registers** for every first run
(`register_run_completion_callback` → `_on_terminal`, which today flips
`bootstrap_state` to `delivered`). At that same terminal moment it also reads
`first_contact_state`; if still `not_started`, it dispatches a short nudge
run — *"you haven't sent {owner} their first email yet"* — bounded by
`first_contact_attempts` (3). After the third, state is `failed` and the
alarm fires.

This does not contradict decision 3. The **send** remains Friday's own tool
call; the observer never composes or sends mail. It only notices she hasn't
and reminds her. No new machinery is introduced — the callback is already
registered and already fires.

We retry a **nudge**, not a send. A send that reached SES is never retried.

## Error handling

| Failure | Behaviour |
|---|---|
| `AGENT_EMAIL_DOMAIN` unset | `{"status": "disabled"}`, no state change. Every non-prod env stays inert. |
| Bad/missing hook_token | `401`. No state change, no information leak about the owner. |
| Rate limit exceeded | `429`. |
| Slug allocation fails | `first_contact_state = failed`, alarm. Owner has no address; nothing partial is written. |
| Mint loses a conditional-put race | Treated as failed mint (existing behaviour), state `failed`, alarm. |
| `write_owner_facts` fails | **Send proceeds.** The facts file is advisory; the address row is the source of truth. Logged. |
| SES send fails | `first_contact_state = failed`, alarm. Not auto-retried — the message may have partially transited. |
| Already `sent` | `{"status": "already_sent"}`, no SES call. |

Metric: `agent_email.first_contact{result=sent|already_sent|failed|disabled}`,
plus `company_named=true|false` so "how often does Friday skip naming" is a
number rather than a guess.

## Testing

**Unit — endpoint**
- wrong hook_token → 401; unknown owner → 401; token for owner A cannot send for owner B
- second call after `sent` → `already_sent`, and SES is **not** called
- concurrent claim: one `sending`, one `already_sent`
- `AGENT_EMAIL_DOMAIN` empty → `disabled`, no mint, no send
- rate limit → 429

**Unit — service**
- no prior address → slug allocated and address minted from the **current**
  Paperclip company name, not the provisioning placeholder
- prior address exists → reused, not re-minted
- `write_owner_facts` is called after a successful mint and the written dict
  carries a non-null `email_address` (guards the quiet-failure path where the
  `isol8` skill would tell Friday she has no address)
- `write_owner_facts` raising does not block the send
- SES returning None → state `failed`, metric `result=failed`

**Unit — prompt**
- `build_first_run_message` contains both the naming step and the send step,
  for the brief and no-brief paths

**Unit — nudge**
- `_on_terminal` with `first_contact_state = not_started` dispatches a nudge
  and increments `first_contact_attempts`
- `_on_terminal` with `first_contact_state = sent` dispatches nothing
- the third exhausted attempt sets `failed` and emits the alarm metric

**Manual, prod**
- full round trip: signup → first run → email received → reply to it → Friday
  receives the reply through the existing inbound path

## Migration

Prod currently holds exactly one owner (the founder, `trial_local`) with
`friday@admin-2.isol8.co` already minted from the placeholder — `admin` is in
`RESERVED_SLUGS`, hence the `-2`.

`company_slug_repo` has **no delete path** and reservations are permanent by
design, so the new flow will not re-derive this address on its own. With zero
external users, the clean option is a one-time manual delete of the
`company-slugs` and `agent-email-addresses` rows for that owner, letting the
new flow mint a real name. This is a deliberate operator action, not code.

No other environment is affected: `AGENT_EMAIL_DOMAIN` is empty everywhere
but prod, so every guard short-circuits.

## Risks

1. **Friday may not follow the instruction.** The mitigation is the bounded
   re-nudge plus the alarm — and unlike BOOTSTRAP.md, the kickoff prompt is
   server-dispatched and verified delivered. The metric makes compliance
   observable instead of assumed.
2. **She may email before naming the company**, freezing the slug on the
   placeholder. The prompt orders the steps; `company_named` in the metric
   quantifies how often it happens.
3. **`write_owner_facts` must run after the mint.** Missing this is the single
   most likely silent failure: `isol8-facts.json` would carry
   `email_address: null`, and `SKILL.md` explicitly instructs Friday not to
   claim an address in that case — she would decline to send, exactly as
   quietly as the BOOTSTRAP.md version did. Covered by a dedicated test.

## Out of scope

- Addresses for Atlas or custom/marketplace agents. The scheme
  (`{agent}@{slug}.{domain}`) already supports them and SES receiving is
  catch-all, so only a mint call is missing — but it is a separate change.
- Making addresses public (`public: true` already skips the sender allowlist;
  no UI exposes it).
- Slug rename/alias support.
