# Agent mail: answering every inbound the owner sends — design

- **Date:** 2026-07-26
- **Status:** Design, approved in conversation, pre-implementation
- **Supersedes:** the "Wake on inbound" section of
  `2026-07-25-agent-first-contact-email-design.md` (#1473). That work is folded
  in here rather than left as two overlapping designs.

## Problem

Agent outbound email shipped 2026-07-26 (#1479/#1485/#1488). An agent can now
email its owner, and the owner can reply.

Except on the free tier, where a reply can vanish. Two silent drops sit on the
inbound path, and between them they cover the most likely outcome for a new
user:

1. **The container is asleep.** `agent_mail_delivery.deliver_to_agent` returns
   `"container_not_running"` without POSTing. Its docstring calls the drop
   intentional.
2. **The owner is over the free-tier cap.** `routers/ses_inbound.py` returns
   `"dropped_over_cap"`.

In both cases SES has already accepted the message, so there is no bounce. The
owner replies to their own agent and nothing happens, forever, with no
explanation. That is the same failure family as the BOOTSTRAP.md original this
whole feature has been climbing out of: the system behaves correctly and the
user cannot tell.

It is also a wasted moment. An owner replying to their agent is the most
engaged they will ever be.

## Who is actually on the other end

Load-bearing, because it decides what is safe to send.

`routers/ses_inbound.py` enforces a sender allowlist: a private address only
accepts mail from `verified_senders`, which is `[owner_email]`. `public` is
`false` on every row and no surface sets it.

So an inbound that reaches the cap check or the delivery step is **the owner**,
authenticated by that allowlist. Not a prospect, not their customer. The
conversion in play is free → paid for someone who has already signed up.

That is why steps 3 and 5 below may safely answer, and steps 1 and 2 may not.

## The four states

```
inbound reply
├─ awake, under cap ......... deliver; agent replies          (nothing added)
├─ asleep, under cap ........ wake, deliver; agent replies    (free-tier footer)
├─ DAILY_CAP ................ no run possible TODAY           (auto-reply: resets)
└─ TRIAL_EXPIRED ............ no run possible at all          (auto-reply: upgrade)
```

`trial_cap.trial_block_reason` already distinguishes the last two, and the
distinction is not cosmetic:

| Reason | Servable? | Must they pay? |
|---|---|---|
| `DAILY_CAP` | Not today — resets at 00:00 UTC | **No.** Waiting is enough. |
| `TRIAL_EXPIRED` | No | **Yes.** |

**Only `TRIAL_EXPIRED` may present payment as REQUIRED.** Telling a
`DAILY_CAP` owner they must upgrade is simply false — their allowance returns
in hours, and that is the kind of false urgency that is trivially caught and
expensive to be caught at.

`DAILY_CAP` copy may still *offer* upgrading, but only after the free path and
never as the way out: "send this again after the reset, **or** upgrade to
remove the limit." Waiting is stated first because waiting genuinely works.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **Wake and deliver** rather than auto-replying with an upgrade prompt | The agent's email invites a reply. Answering it with a paywall breaks that invitation at the first opportunity, and reads as broken software to someone who does not know the tiers. Conversion should come from the product working, not from a wall. |
| 2 | **Surface the limit via a deterministic footer**, not the agent's own words | An LLM may or may not mention it; a footer always does, and is testable. It rides the email the owner is already reading. |
| 3 | **Auto-replies are server-templated, no agent run** | SES sending is not gated by the trial cap (the cap fences Bedrock inference), so we can always answer even when we cannot run the agent. Nothing is reserved or spent. |
| 4 | **A capped message is dropped, not queued** | The owner is told to resend after reset. Queueing needs a pending store plus something to fire at 00:00 UTC, and a day-old message arrives without context. Not worth it for a state that resolves itself. |
| 5 | **Unknown-recipient and unverified-sender stay silent** | Those senders are unauthenticated and could be anyone. Replying confirms the address exists and turns the endpoint into a reflector. |

## Components

| Component | Change |
|---|---|
| `core/services/agent_mail_autoreply.py` | **New.** Server-templated sends from the agent's own address; two bodies, no agent run. |
| `routers/ses_inbound.py` | The `dropped_over_cap` branch calls the auto-reply and returns `capped_notified`. |
| `core/services/agent_mail_delivery.py` | Cap-check → start the service → deliver when the gateway comes up, instead of dropping. Stamps `email_cold_wake_at`. |
| `core/services/agent_mail_reply.py` | Appends the free-tier footer when the reply follows a cold wake. |
| `core/repositories/container_repo.py` | `email_cold_wake_at` stamp + clear. |

## Wake on inbound

`deliver_to_agent` finds no running container:

1. Evaluate `trial_block_reason`. Over cap → the auto-reply path above; **no
   wake**. Cost stays fenced exactly as chat is.
2. Under cap → start the service and persist a pending-delivery marker. The raw
   MIME is already in S3 for 7 days, so nothing is re-fetched or held in memory.
3. Deliver when the gateway connection for that owner comes up — event-driven
   off the existing connection lifecycle, not a polling worker.

A wake cannot be synchronous: Fargate cold start is tens of seconds and the
ingest Lambda would time out.

Scope, verified: the idle reaper is `_maybe_stop_trial_container`, gated on
`subscription_status == "trial_local"`. **Paid containers stay up, so this is
free-tier-only** — but onboarding is free tier, so it is exactly the path a new
user takes.

Abuse is bounded by construction: only the owner can send to a private address,
and they are cap-gated. A stranger cannot wake anyone's container.

## The footer, and the one piece of state it needs

The footer must only appear when *this* reply followed a cold wake — otherwise
every free-tier reply carries it and it becomes noise.

The wake and the reply happen in **different requests**: the wake is inbound,
the reply comes later when the agent calls `/agent-mail/reply`. So the fact has
to be carried:

- `deliver_to_agent` stamps `email_cold_wake_at` on the container row when it
  wakes.
- `send_reply` appends the footer when that stamp is recent (≤ 1 hour) **and**
  the owner is `trial_local`, then clears it.

~15 lines and one extra DDB read on an already-cold path. The alternative — a
separate "I was asleep, waking up" email — needs no state but sends two emails
per reply, which is worse for the same information.

Footer text (appended below the agent's own words, visually separated):

> I was asleep when this arrived — on the free plan I sleep when idle, so
> replies wait for me to wake. isol8.co/settings

## Auto-reply copy

`DAILY_CAP`:

> I've used today's free allowance, so I can't pick this up right now. It
> resets at 00:00 UTC — send this again after that, or upgrade to remove the
> limit: isol8.co/settings

`TRIAL_EXPIRED`:

> Your free plan has ended, so I can't run right now. Upgrade and I'll pick
> this straight back up: isol8.co/settings

Both send from the agent's own address so they thread with the conversation the
owner is already in.

## Error handling

| Failure | Behaviour |
|---|---|
| `AGENT_EMAIL_DOMAIN` unset | Whole path inert, as today. |
| Auto-reply SES send fails | Log + metric; the inbound is still dropped. Never raises — the ingest webhook must stay terminal. |
| Wake fails to start the service | Log + metric; drop. No retry storm off an inbound. |
| Gateway never comes up | The pending marker expires with the S3 object (7 days). No delivery, no crash. |
| Container row missing `email_cold_wake_at` | No footer. Absence is the common case. |
| Stamp older than 1 hour | No footer, and the stamp is cleared. Guards a stale stamp attaching to an unrelated reply. |

Metrics: `agent_email.inbound{result=delivered|woken|capped_notified|dropped}`
and `agent_email.autoreply{reason}` — so "how often does a free-tier reply hit
a sleeping agent" becomes a number rather than a guess.

## Testing

**Auto-reply**
- `DAILY_CAP` → auto-reply sent, body mentions the reset, **does not** ask for payment
- `TRIAL_EXPIRED` → auto-reply sent, body **does** ask for payment
- both → the inbound is not delivered, and no agent run is dispatched
- SES failure on the auto-reply → logged, inbound still terminal, no raise

**Silence where it belongs**
- unknown recipient → **no** auto-reply (would confirm the address exists)
- unverified sender → **no** auto-reply (would make this a reflector)

**Wake**
- running container → delivered directly, no wake, no stamp
- asleep + under cap → service started, pending marker persisted, stamp written
- asleep + over cap → **no** wake, auto-reply instead
- a second inbound while already pending → does not start a second service

**Footer**
- `trial_local` + recent stamp → footer appended exactly once, stamp cleared
- `trial_local` + no stamp → no footer
- paid owner + recent stamp → no footer
- stamp older than 1 hour → no footer, stamp cleared
- the footer never appears in the agent's own body, only below it

## Out of scope

- Queueing a capped message until reset (decision 4).
- Making addresses public — `public: true` already skips the allowlist, but no
  surface sets it, and opening it would change who can reach these paths.
- Any in-app upgrade prompt. This spec covers email only.
