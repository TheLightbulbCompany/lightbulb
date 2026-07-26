# Agent outbound email — owner-directed sends

- **Date:** 2026-07-25
- **Status:** Design, approved in conversation, pre-implementation
- **Supersedes:** the "Friday's first email" portion of #1365, deleted in `be2ffea52`

## Problem

An agent should be able to email its owner. Two use cases, one now and one
imminent:

1. **First contact** — once Friday's first run is done, she emails the owner
   in her own voice, referencing what she learned. It is a defining product
   moment: the first time the agent reaches *out* rather than waiting to be
   addressed.
2. **Routines** — once an owner pays, heartbeats and routines turn on, and a
   seeded nightly routine has Friday do a task and email the result.

Neither is possible today. **No agent-initiated email path exists at all.**

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
   └─ mint_agent_email_address("Friday")       ← address minted here, frozen
    ↓
background_provision → ECS + EFS + write_openclaw_config
   └─ write_owner_facts() → isol8-facts.json gains email_address
    ↓
user submits funnel → POST /onboarding/brief → stages BRIEF.md, claims funnel
    ↓
maybe_dispatch_bootstrap()   ← from submit_brief AND the /status poll
   ├─ claims bootstrap_state: not_started → dispatched
   ├─ chat.send( build_first_run_message(brief) )
   └─ register_run_completion_callback → chat.final → mark_bootstrap_delivered
    ↓
Friday researches, writes up, files three backlog tasks
    ↓
NOTHING FURTHER HAPPENS
```

Friday is never told to name the company, and never told to email anyone.

### The two layers, and which one is missing

This distinction drove the design and is worth stating plainly:

- **The transport** — `ses_email.send_ses_reply` is a plain function with no
  latch and no limit. It already works and can be called any number of times.
  It has simply **never been called in production** (SES send statistics are
  empty since the stack was created).
- **The triggers** — there is exactly one: "reply to a message that arrived,"
  which requires a `reply_token`. `encode_reply_token` is minted in exactly
  one place, `routers/ses_inbound.py`, which runs only when mail *arrives*.

So outbound is not limited or one-shot. It is simply unreachable unless
somebody emails the agent first. What is missing is an **agent-initiated
trigger**, not a send capability.

### Why the previous attempt never fired

#1365 shipped one: `agent_mail_token.mint_first_contact_token`, a
one-recipient token, instructed via **BOOTSTRAP.md**.

`be2ffea52` deleted both. Its commit message records why: BOOTSTRAP.md was
**inert** — OpenClaw's `reconcileWorkspaceBootstrapCompletionState` never
surfaces a seeded BOOTSTRAP.md into Friday's context once `setupCompletedAt`
is pre-marked. She never saw the instruction, so the token minting was dead
code and was removed as cleanup.

The transport was fine. The delivery vehicle for the *instruction* was not.
This design does not reuse it.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **One general primitive**: "agent emails its owner", callable repeatedly | First contact and nightly routines are two callers of one capability. A one-shot endpoint would have blocked routines on night two. |
| 2 | **Once-only is per-purpose policy**, not a property of the endpoint | `first_contact` is once; `routine` is not. The guard belongs to the caller that needs it. |
| 3 | **Mint after Friday names the company**, not at provisioning | The slug becomes an address other people are given, so freezing it is right — freezing an auto-generated placeholder the user never chose is not. Prod has one owner and zero external users, so moving this is nearly free now. |
| 4 | **Friday writes the body**, tailored | Preserves the product moment. Reuses `isol8-mail.sh`'s shape. |
| 5 | **Friday invokes it via a tool call**, not a `chat.final` observer | She decides when her work is done. Collapses mint + send into one request and removes the need for a token entirely. |
| 6 | **Inbound wakes a scaled-to-zero container** (free tier only) | Otherwise "just reply to this email" is a promise the product breaks for every new user. See below. |
| 7 | **Retry, then alarm** | The previous version failed silently for its entire life. Nothing here may disappear quietly. |

## Architecture

### The primitive

```
POST /api/v1/agent-mail/send-to-owner/u/{owner_id}
Authorization: Bearer {hook_token}
{ "purpose": "first_contact" | "routine", "subject": "...",
  "body_text": "...", "body_html": null }

  1. auth        container_repo.get_by_owner_id + timing-safe compare
  2. rate limit  60/min per owner (own bucket) AND a per-owner DAILY cap
  3. policy      once-only purposes check their own state; others pass
  4. address     read Paperclip company name  ← whatever Friday set
                 allocate_company_slug()      ← idempotent per owner
                 mint_agent_email_address()   ← idempotent per owner
  5. facts       write_owner_facts()          ← only when newly minted
  6. send        SES: From = minted address, To = owner_email (server-derived)
  7. settle      per-purpose state + metric
```

**No token.** The deleted `mint_first_contact_token` existed only to bake a
recipient into a value Friday could hold. An endpoint that already knows who
she is derives the recipient from `owner_id` server-side — nothing to mint,
expire, or expose to a prompt where it could be pasted into a visible body.

**The daily cap is a real fence, not ceremony.** An agent in a loop could
otherwise email its owner hundreds of times in an hour. 10/day per owner,
rejected with `429` and a metric above it.

### Callers

| Purpose | Guard | Triggered by |
|---|---|---|
| `first_contact` | Once per `onboarding_generation` (`first_contact_state`) | Friday, instructed by the first-run kickoff prompt |
| `routine` | None — rate + daily cap only | A seeded nightly routine, once the owner pays |

Adding a caller means adding a purpose and (optionally) a guard. It does not
mean touching auth, minting, or the SES path.

### Security model

Reuses `hooks_proxy` verbatim: `/u/{owner_id}` names the owner, and the
`Authorization: Bearer` value is compared timing-safely against that owner's
`containers.hook_token`. Only that owner's own container process holds that
token, so a container can only ever send for its own owner.

The recipient is **never** client-supplied. It is the owner's own email,
resolved server-side. There is no parameter that can redirect the message.

### Wake on inbound (decision 6)

`agent_mail_delivery.deliver_to_agent` currently returns
`"container_not_running"` without POSTing, and its docstring calls the drop
intentional. That is defensible for a general hook, and wrong for email.

Scope, verified: the idle reaper is `_maybe_stop_trial_container`, gated on
`subscription_status == "trial_local"`. **Paid containers stay up, so this is
a free-tier-only problem.** But onboarding happens on the free tier:

| | Free tier | Paid |
|---|---|---|
| Friday's first email | works — sent during her run, container awake | works |
| Owner's reply, an hour later | **silently dropped** | works |
| Nightly routine email | n/a | works |

So the single flow it breaks is the one first contact creates: a brand-new
user is told "just reply and it reaches me," replies, and the message
vanishes. No bounce — SES already accepted it.

**A wake cannot be synchronous.** Fargate cold start is tens of seconds and
the ingest Lambda would time out. So:

1. `deliver_to_agent` finds no running container.
2. Evaluate the trial cap (`trial_block_reason`). Over cap → existing
   `dropped_over_cap`, no wake. Cost is fenced exactly as chat is.
3. Under cap → start the service and persist a **pending-delivery** marker
   (the raw MIME is already in S3 for 7 days, so nothing is re-fetched from
   the network or held in memory).
4. Deliver when the gateway connection for that owner comes up — event-driven
   off the existing connection lifecycle, not a polling worker.

Abuse is bounded by construction: `verified_senders` means only the owner can
send to a private address, and they are cap-gated. A stranger cannot wake
anyone's container.

### Components

| Component | Change |
|---|---|
| `routers/agent_mail_send.py` | **New.** Mirrors `agent_mail_relay.py` minus the reply-token; carries `purpose`. |
| `core/services/agent_mail_owner_send.py` | **New.** Policy → address → facts → send → settle. |
| `apps/infra/openclaw/skills/isol8/scripts/isol8-owner-email.sh` | **New.** Sibling of `isol8-mail.sh`; body on stdin, `--subject`, `--purpose`. |
| `apps/infra/openclaw/skills/isol8/SKILL.md` | Document the helper and when each purpose applies. |
| `core/services/onboarding_brief.build_first_run_message` | Add the naming step and the send step. |
| `core/services/paperclip_provisioning.provision_org` | **Remove** `allocate_company_slug` + `mint_agent_email_address`. |
| `core/services/owner_facts.build_owner_facts` | Add `owner_id` so the script can build its URL. |
| `core/services/agent_mail_delivery` | Wake + pending-delivery instead of a silent drop. |
| `core/repositories/paperclip_repo` | `first_contact_state` claim/settle/re-arm. |
| `core/services/bootstrap_dispatcher._on_terminal` | Also inspect `first_contact_state` and dispatch a bounded nudge. Composes no mail. |

## State machine (first_contact only)

Lives on the paperclip-companies owner row beside `bootstrap_state`, scoped to
`onboarding_generation` (consistent with `238f452d8`).

```
not_started ──claim──> sending ──ok──> sent        (terminal)
     ▲                    │
     │                    └──fail──> failed        (terminal, alarms)
     │                                   │
     └────────── re-arm (bounded ×3) ────┘
```

The claim is a **conditional write**, so two concurrent calls cannot both
send; the loser gets `already_sent`. `routine` sends carry no such state.

### Retry

**The `/container/status` poll cannot drive this.** It is off by the time
first contact matters — `useProvisioningState` documents the cadence as
"0/off once ready", and first contact happens after ready.

Instead the safety net reuses the run-completion callback
`bootstrap_dispatcher` **already registers** (`register_run_completion_callback`
→ `_on_terminal`, which today flips `bootstrap_state` to `delivered`). At that
same terminal moment it reads `first_contact_state`; if still `not_started`,
it dispatches a short nudge run, bounded by `first_contact_attempts` (3).
After the third, state is `failed` and the alarm fires.

This does not contradict decision 5. The **send** remains Friday's own tool
call; the observer never composes or sends mail. It only notices she hasn't
and reminds her.

We retry a **nudge**, not a send. A send that reached SES is never retried.

## Error handling

| Failure | Behaviour |
|---|---|
| `AGENT_EMAIL_DOMAIN` unset | `{"status": "disabled"}`, no state change. Non-prod stays inert. |
| Bad/missing hook_token | `401`. No state change, no leak about the owner. |
| Rate limit / daily cap exceeded | `429` + metric. |
| Unknown `purpose` | `400`. Fail closed rather than defaulting to unguarded. |
| Slug allocation fails | State `failed`, alarm. Nothing partial is written. |
| Mint loses a conditional-put race | Treated as a failed mint, state `failed`, alarm. |
| `write_owner_facts` fails | **Send proceeds.** Facts are advisory; the address row is the source of truth. Logged. |
| SES send fails | State `failed`, alarm. Not auto-retried — the message may have partially transited. |
| `first_contact` already `sent` | `{"status": "already_sent"}`, no SES call. |
| Inbound, container asleep, over cap | `dropped_over_cap` (existing), no wake. |

Metrics: `agent_email.owner_send{purpose,result}` and
`agent_email.first_contact{company_named}` so "how often does Friday skip
naming" is a number rather than a guess.

## Testing

**Unit — endpoint**
- wrong hook_token → 401; token for owner A cannot send for owner B
- `first_contact` twice → second is `already_sent`, SES **not** called
- `routine` twice → **both** send (the regression that motivated this revision)
- concurrent `first_contact` claim: one `sending`, one `already_sent`
- unknown purpose → 400
- daily cap exceeded → 429
- `AGENT_EMAIL_DOMAIN` empty → `disabled`, no mint, no send

**Unit — service**
- no prior address → minted from the **current** Paperclip company name, not
  the provisioning placeholder
- prior address exists → reused, not re-minted
- `write_owner_facts` called after a new mint, and the written dict carries a
  non-null `email_address` (guards the quiet-failure path where `SKILL.md`
  would tell Friday she has no address)
- `write_owner_facts` raising does not block the send
- SES returning None → state `failed`, metric `result=failed`

**Unit — inbound wake**
- container running → delivered directly, no wake
- container at zero, under cap → service started, pending marker persisted
- container at zero, over cap → `dropped_over_cap`, **no** wake
- pending delivery flushes when the gateway connection comes up
- a second inbound while already pending does not start a second service

**Unit — prompt / nudge**
- `build_first_run_message` contains the naming and send steps, brief and
  no-brief paths
- `_on_terminal` with `not_started` nudges and increments attempts; with
  `sent` does nothing; the third exhausted attempt sets `failed` and alarms

**Manual, prod**
- signup → first run → email received → reply → Friday receives it *after*
  her container has scaled to zero (the case that is broken today)

## Migration

Prod holds exactly one owner (the founder, `trial_local`) with
`friday@admin-2.isol8.co` already minted from the placeholder — `admin` is in
`RESERVED_SLUGS`, hence the `-2`.

`company_slug_repo` has **no delete path** and reservations are permanent by
design, so the new flow will not re-derive this address. With zero external
users, the clean option is a one-time manual delete of the `company-slugs`
and `agent-email-addresses` rows for that owner. A deliberate operator
action, not code.

No other environment is affected: `AGENT_EMAIL_DOMAIN` is empty everywhere
but prod, so every guard short-circuits.

## Risks

1. **Friday may not follow the instruction.** Mitigated by the bounded
   re-nudge plus the alarm — and unlike BOOTSTRAP.md, the kickoff prompt is
   server-dispatched and verified delivered. The metric makes compliance
   observable instead of assumed.
2. **She may email before naming the company**, freezing the slug on the
   placeholder. The prompt orders the steps; `company_named` quantifies it.
3. **`write_owner_facts` must run after the mint.** The single most likely
   silent failure: `isol8-facts.json` would carry `email_address: null`, and
   `SKILL.md` explicitly instructs Friday not to claim an address in that
   case — she would decline to send, exactly as quietly as the BOOTSTRAP.md
   version did. Covered by a dedicated test.
4. **Waking on inbound costs money.** Fenced by the cap check and by
   `verified_senders` — only the owner can trigger it, and only under cap.

## Out of scope

- Addresses for Atlas or custom/marketplace agents. The scheme
  (`{agent}@{slug}.{domain}`) already supports them and SES receiving is
  catch-all, so only a mint call is missing — a separate change.
- Making addresses public (`public: true` already skips the sender allowlist;
  no UI exposes it).
- Slug rename/alias support.
- Authoring the nightly routine itself. This spec makes `purpose="routine"`
  possible; seeding the routine is separate work.
