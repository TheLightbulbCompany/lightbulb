# Friday's first run, made light — design

**Status:** agreed with the owner 2026-09-02 (in-chat). Disposable per CLAUDE.md:
delete with the PR that ships it.

## Problem (measured, not reported)

Two prod first runs traced end to end on 2026-09-01:

| | Leo (real brief) | Aden ("Hello") |
|---|---|---|
| kickoff run | 28 tool calls / 2m23s (26 `exec`) | 55 tool calls / 5m24s (40 `exec`, 3 `web_search`, 6 `skill_workshop`) |
| Bedrock invocations | 71 | 47 |
| spend | $2.90 → **capped, `bootstrap_state=failed`** | $1.62, delivered |

The $3.00/day free-tier cap admits ~74 calls at today's ~34.5k prompt tokens per
call. 1 in 5 active trial-days caps out (unchanged since July). The owner sees a
market write-up, an issues table, an integrations table, a "skip for now" list,
"three tabs are open in your Browser panel", and the tool trail.

## Why: three instruction sources that disagree

1. **The kickoff** (`onboarding_brief.build_first_run_message` +
   `_INTEGRATIONS_STEP` + `_CLOSING_STEPS`): research the market and write it up;
   rename the company; file three backlog issues; curl the integrations catalog;
   analyse flags; mint OAuth links; open browser tabs; email the owner.
2. **`BRIEF.md`** (`_BRIEF_TEMPLATE`, staged on disk): "Research the person as
   well as the business."
3. **Friday's seeded `AGENTS.md`** (`friday_identity._AGENTS_TEMPLATE`):
   "Delegate specialist execution — do not perform it yourself" and "Confirm
   before creating any issues — wait for confirmation."

The expensive part is not the decisions (which moves, which integrations — one
model call each). It is Friday doing **plumbing through the LLM loop**: fetching a
catalog the backend already has, parsing flags, minting links, opening tabs,
filing issues, and working around failures.

## Two invariants that make the cut safe

- `bootstrap_state=delivered` (the launch-competition gate) is set by
  `_kickoff_observer` on run state `final` only. No job has to complete.
- First contact is satisfied by **any** `--owner` email while pending
  (`agent_mail_owner_send.py`). Without one, `_maybe_nudge_first_contact`
  re-prompts Friday up to 3 times, each a new run. So the kickoff keeps a short
  owner email.

## Design

**Deterministic, backend-owned (no model):**
- At brief fulfilment, append an **"Integrations"** section to `BRIEF.md` rendered
  from the credentials registry — for every provider: display name, key, and one of
  `one-click` / `needs you (bring your own app)` / `by request`, from the same
  two flags `/platform-info` exposes (`byo_required`, `requestable`). Staged at
  provision time from the live registry, so it is as current as the curl was for
  the first run; later turns keep reading the live catalog per the skill. Nothing is
  baked into the image.
- A **Stripe line** rendered from the billing row, because the company's own
  Stripe account is the eligibility gate for the whole Projects rail and is
  move-independent:
  - `company_stripe_account_id` set → *connected*.
  - paid (`subscription_status` in `active`/`trialing`) → *required; a setup card
    is already in your Inbox (#2254 files it when the subscription goes live) —
    don't file another.*
  - otherwise (trial) → *required for payments and for provisioning hosting and
    analytics; it is set up when you subscribe — say so, don't request it.*
  Friday never calls the Stripe request door on first run.

**Bounded, model-owned (the kickoff):**
- Read `BRIEF.md`. Rename the company if it is a placeholder (kept: it mints the
  task-key prefix and the email subdomain).
- **Two web searches at most**, then a **five-bullet** market brief.
- **Three moves, one line each — do not file them.** Filing happens after the
  owner says go (the playbook's own Confirm step).
- From the Integrations section, name the **two or three** that serve those
  moves, one line each: "connect them from the Integrations tab". The tab runs the
  OAuth in the owner's own browser (`useConnections.ts` → `/connections/{p}/authorize`);
  Friday opening tabs in the container's Chrome was never needed for OAuth.
- A **three-line** owner email via `lightbulb-mail.sh send --owner` (first
  contact).
- End with **one question**: which move first.
- Dropped from the kickoff: the catalog curl, the flag analysis, link minting,
  browser tabs, issue creation, the report-length email.
- `BRIEF.md` drops "research the person" on the with-brief path; the "you pick"
  block keeps it (there it is job zero).

**Out of scope (separate work):** collapsing the tool trail in the chat UI
(frontend); a cheap-tier (Haiku) inference profile for trial owners; a
`first-run.lobster` pipeline. Any of these is phase 2 if the measurement below
says the bounded prompt is still too heavy.

## Constraints

- Backend only. Do **not** touch `apps/infra/openclaw/**` (a `SKILL.md` change
  triggers an image build and needs a fleet reroll). Keep the constant name
  `_INTEGRATIONS_STEP` so the skill's maintenance anchor stays valid.
- The kickoff must never contain `Authorization`, `OPENCLAW_HOOK_TOKEN`, `bearer`
  (any case), or a `platform-info` curl (`test_kickoff_never_inlines_an_authorization_header`
  already pins the first three).
- Keep the naming sentence before the moves (`test_kickoff_names_the_company_before_the_board`).
- Keep `lightbulb-mail.sh send --owner` in both kickoff paths.
- No new env vars, no new tables, no new dependencies.

## Success measure

Reprovision the dev E2E trial (`user_3IeOW2y8Y1o84mB2dnxZ54Ok9Xp`) after the dev
deploy and re-run the three CloudWatch queries used for the baseline (tool calls by
name, Bedrock `Invocations` on its AIP, `trial_daily_spend_microcents`). Target:
≤ 15 tool calls, ≤ 20 invocations, ≤ $0.60 on a real brief. Record before/after in
the PR body.
