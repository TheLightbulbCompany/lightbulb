# Landing-page onboarding — design

**Status:** proposal · **Date:** 2026-08-12 · **Visual reference:**
`docs/landing-rehaul/` (see also the interactive previews described in §11)

Move the first-run funnel from behind auth onto the public landing page, run
Friday's plan generation on the answer alone, capture an email to send the full
plan, and only then ask for an account.

**The hard invariant: container and Paperclip provisioning mechanics do not
change.** Not one line of `ensure_user_ready`, `provision_org`,
`background_provision`, `reconcile_pending_brief`, or the funnel claim. Everything
in §5 is additive surface that sits *in front* of the existing machine and hands
off to it untouched.

---

## 1. Why

Today the funnel is gated: a visitor must create a Clerk account before they are
asked a single question, and before isol8 demonstrates anything. The funnel then
renders *during* provisioning to fill dead time (`routers/onboarding.py`, module
docstring).

That ordering spends the account-creation ask before any value has been shown.
Inverting it — answer, watch Friday work, get a plan, then decide — puts the
demonstration before the commitment, and turns the funnel answer into a lead
even when the visitor never signs up.

## 2. Target flow

| # | Step | Auth? | Backend |
|---|------|-------|---------|
| 1 | "Let's get started." → *Start something new* / *Grow my business* | no | none |
| 2 | "What's your idea?" / "What's your company's website?" (+ *I don't know yet — you pick*) | no | none |
| 3 | **Generation runs.** Friday's log streams, then the first moves render with the tail withheld | no | `POST /api/v1/leads/brief` (new) |
| 4 | "Where should Friday send the rest?" → email | no | `POST /api/v1/leads/email` (new) |
| 5 | "It's on its way." → **Create my account** | Clerk | existing signup, then replay (§6) |

Steps 1–2 keep the exact copy and branch semantics that ship today in
`FirstRunFunnel.tsx`. Step 3 is new. Step 4 is new and purely additive — it gates
nothing except the remainder of the emailed plan. Step 5 is the existing signup.

## 3. What exists today (verified)

### Intake

`POST /api/v1/onboarding/brief` (`routers/onboarding.py`) is intake-only. It
records the funnel answer and returns; it depends on nothing but auth.

`BriefBody` is `path: Literal["new","grow"]`, `input: str | None` (max 2000),
`detail: str | None` (max 500), with a validator that:

- requires `input` on the `grow` path,
- normalises blank-but-present `input` to `None` on the `new` path — **an absent
  idea is the "you pick" signal**, not an error,
- rejects `input` shorter than 3 characters.

It writes via `PaperclipRepo.put_pending_brief(owner_id, path=, user_input=,
detail=, brief_text=, submitted_by=)`, which is **put-if-absent**: returns `True`
iff this answer landed, `False` when one is already pending. `brief_text` is
precomputed at intake so the phrasing the user submitted is what Friday opens
with, even if the brief-builder changes later. A `failed` item is deliberately
*not* protected by that conditional.

Everything is keyed by `resolve_owner_id(auth)` — `org_id` for an org, else
`user_id`. Onboarding truth is company-scoped: one answer per company, whichever
member's tab submits it.

### Fulfilment

`core/services/onboarding_brief.reconcile_pending_brief` is a server-owned
reconciler driven off the `GET /container/status` poll. It takes the
generation-fenced `funnel_state: not_started → submitted` claim, seeds the company
goal + project, stages `BRIEF.md` into Friday's pre-boot workspace, and dispatches
her kickoff `chat.send`. `funnel_state == "complete"` is set later by
`core/services/bootstrap_dispatcher` once that kickoff actually delivers
(`chat.final`), and `GET /container/status` reads it off the owner row.

Background: `docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md` §2.

### Provisioning doors

`ensure_user_ready` is dispatched from `GET /container/status`
(`routers/container.py:317,339,526`) plus three self-heal re-dispatches
(`routers/paperclip_proxy.py:1101,1360`, `routers/teams/agents.py:404`,
`routers/teams/deps.py`). `provision_org` short-circuits on an active row
(`core/services/paperclip_provisioning.py:340`). Clerk webhooks are pure
record-keeping and do not trigger provisioning (`routers/webhooks.py:336`).

**None of these are touched by this design.**

### The blocker this design has to work around

Both halves of onboarding key off `resolve_owner_id(auth)`. **Before signup there
is no owner id**, no owner row, no `funnel_state` to claim, and no container. So
the pre-account path cannot reuse `/onboarding/brief`, and the pre-account plan
cannot be Friday's kickoff.

## 4. Non-goals

- No change to container or Paperclip provisioning, in any form.
- No change to `/onboarding/brief`, `put_pending_brief`, `reconcile_pending_brief`,
  the funnel claim, or `funnel_state` semantics.
- No account, container, Paperclip company, or Stripe object created for a lead.
- Not a waitlist. Signup stays open; the email is for delivering the plan.

## 5. New surface (all additive)

### 5.1 `marketplace`-style DDB table: `isol8-<env>-leads`

Defined in `apps/infra/lib/stacks/database-stack.ts` following the existing table
pattern.

| attr | type | notes |
|------|------|-------|
| `lead_id` | S (PK) | server-generated `lead_<uuid4>` |
| `path` | S | `"new"` \| `"grow"` |
| `user_input` | S? | absent = the "you pick" signal, same as intake |
| `detail` | S? | optional audience note, `new` path only |
| `email` | S? | absent until step 4 |
| `email_lower` | S | GSI PK, for dedupe/lookup |
| `plan_json` | S? | generated plan |
| `plan_state` | S | `pending` \| `generating` \| `ready` \| `failed` |
| `email_state` | S | `none` \| `queued` \| `sent` \| `failed` |
| `claimed_by` | S? | Clerk `user_id`/`org_id` once the lead converts |
| `created_at` / `updated_at` | S | ISO 8601 |
| `ttl` | N | expire unconverted leads (suggest 90d) |

GSI `email_lower-index` so a returning visitor with the same email updates their
lead rather than accumulating rows.

### 5.2 `POST /api/v1/leads/brief` — unauthenticated

Request mirrors `BriefBody` exactly, **including its validator**, so the two
front doors cannot drift:

```jsonc
{ "path": "new", "input": "A ceramics studio that ships nationwide",
  "detail": "Interior designers and small hotels" }
```

Response `201`: `{ "lead_id": "lead_…", "plan_state": "generating" }`

Creates the row, sets `plan_state="generating"`, dispatches generation off the
request path. Returns immediately — the client streams progress via 5.4.

Extract `BriefBody` into a shared schema module and import it in both routers.
Do not copy the validator.

### 5.3 `POST /api/v1/leads/email` — unauthenticated

`{ "lead_id": "lead_…", "email": "you@company.com" }` → `202`

Sets `email`, `email_lower`, `email_state="queued"`. When `plan_state` reaches
`ready`, send. If the plan is already `ready`, send immediately.

Always `202`, even for an unknown `lead_id` — do not let this endpoint confirm
whether a lead exists.

### 5.4 `GET /api/v1/leads/{lead_id}/plan` — unauthenticated

Returns `{ plan_state, plan }` where `plan` is present once `ready`. Polled by
the client during step 3. Returns `404` for unknown ids.

The preview shows a synthetic log while this polls. Either drive the log from
real generation stages, or keep it decorative — but if decorative, it must not
claim work that is not happening.

### 5.5 Lead plan generation

A new service, `core/services/lead_plan.py`. Given `path` + `input` + `detail`,
produce a short structured plan: 5 moves, each a title plus one line.

This is **not** Friday. It has no container, no workspace, no roster, no tools.
It is a single model call against a fixed prompt, writing `plan_json`. Keep it
cheap and bounded — it runs for unauthenticated traffic.

The first two moves are returned in full; moves 3–5 return titles only until
`email_state == "sent"`. The withholding is server-side. Do not ship the full
plan to the client and blur it in CSS.

### 5.6 Plan email

Reuse the existing sender: `core/services/ses_email.send_ses_email`, with
`core/services/welcome_email.py` as the structural pattern (`_render_*_html` /
`_render_*_text` + a `send_*` entry point). Add `core/services/lead_plan_email.py`
in the same shape. No new email dependency — `boto3`/SES is already in place.

## 6. The handoff at signup

This is the whole reason provisioning stays untouched.

1. Client persists `lead_id` alongside the existing draft
   (`DRAFT_STORAGE_KEY = "isol8_first_run_draft_v1"` in `FirstRunFunnel.tsx`)
   plus a cookie, so it survives Clerk's multi-step signup — the same transport
   problem `writeSignupIntentCookie` already solves for team intent.
2. After Clerk auth exists, the client calls the **existing**
   `POST /api/v1/onboarding/brief` with the same `{path, input, detail}`.
3. Everything downstream runs exactly as it does today: `put_pending_brief` →
   `reconcile_pending_brief` off the status poll → funnel claim → company goal →
   `BRIEF.md` → Friday's kickoff → `funnel_state: complete`.
4. Separately, `POST /api/v1/leads/{lead_id}/claim` (authed) stamps `claimed_by`
   for attribution. Analytics only; nothing branches on it.

Replay is safe by construction: `put_pending_brief` is put-if-absent and the
funnel claim only advances `not_started → submitted` once. A user who signs up
twice, or whose tab double-posts, converges.

**Diff to existing provisioning code: none.**

## 7. Frontend changes

- Extract the funnel from `/onboarding` into a component the landing page renders.
  Its `Path` (`"new" | "grow"`) and `Mode` (`"idea" | "noIdea"`) state, draft
  persistence, and copy carry over unchanged.
- Add steps 3–5.
- Gate the landing hero on auth state: funnel when signed out, resume when signed
  in (§8).
- `/onboarding` keeps working for anyone who lands there mid-provision. Do not
  delete it — see the open question in §10.

### 7.1 Friday's orb — reproduce exactly

Friday is represented by one asset across every screen: a soft radial orb that
breathes. It is pure CSS, no canvas and no library. On the funnel it is
**centred above the question at 280px**, in a 190px-tall box so the label sits
under it without pushing the headline down.

```css
.orb-wrap{position:relative;height:190px;display:grid;place-items:center}
.orb{
  width:280px;aspect-ratio:1;border-radius:50%;pointer-events:none;filter:blur(6px);
  background:radial-gradient(circle at 44% 42%,
    rgba(6,64,43,.20) 0%, rgba(74,158,116,.16) 34%, rgba(6,64,43,.07) 58%,
    rgba(6,64,43,.02) 72%, transparent 80%);
  animation:orb-breathe 11s ease-in-out infinite;
}
@keyframes orb-breathe{
  0%,100%{transform:scale(1) translate(0,0);opacity:.9}
  50%    {transform:scale(1.075) translate(1.2%,-1.6%);opacity:1}
}
```

Three things that matter and are easy to lose in a port:

- The gradient is **off-centre** (`at 44% 42%`) with a `forest-soft` mid-stop.
  A centred, single-hue gradient reads as a grey smudge on cream.
- The centre alpha is `.20`. Earlier passes used `.085`, which is invisible once
  the 3.5% grain layer sits over it.
- Do not place it under a fixed translucent nav. The nav's
  `rgba(250,247,242,.84)` + `backdrop-filter` paints over the orb's crown and
  leaves a visible straight seam at the nav's bottom edge.

Everything else on these screens is type. There are no images, icons, or
illustrations to produce.

## 8. Signed-in landing page

Once authenticated, the landing hero becomes a resume surface: the thread they
last opened, an activity feed of what the roster did while they were away, and
any pending approvals.

All of this data already exists in the Teams BFF (`routers/teams/`) — threads,
messages, agents. No new backend beyond a "most recently active thread for this
owner" read, which the existing thread list can serve if it can sort by activity.

## 9. Abuse and privacy

The three new endpoints are the **first unauthenticated write surface** on the
API. Treat them accordingly:

- Rate-limit per IP on `leads/brief` and `leads/email`. Generation costs a model
  call; without a limit this is a billable amplification vector.
- Cap `input` at 2000 and `detail` at 500 — inherited from `BriefBody`.
- `leads/email` returns `202` regardless, so it cannot enumerate leads.
- Never return `email` from any read endpoint.
- Do not send to an address until that request came from the session holding the
  `lead_id`; a `lead_id` is a bearer token for one email send, so generate it
  with a CSPRNG and keep it out of URLs that get logged where possible.
- Consider a light confirmation step if this gets used to mail arbitrary
  addresses. Not required for v1, but it is the obvious abuse.
- `ttl` expiry on unconverted leads keeps this from becoming an unbounded store
  of personal data.

## 10. Open questions — need a product decision

**(a) Two generators.** The pre-account plan cannot be Friday's kickoff, so the
lead sees one artifact and the post-signup user gets another, better one from
Friday. Either accept the duplication, or frame the email explicitly as a preview
that Friday redoes properly once she has a container. Pick one — the copy in step
5 depends on the answer.

**(b) The dead-time filler.** `routers/onboarding.py` states outright that the
funnel renders during provisioning to fill it, because the answer usually arrives
~30s before there is an owner row to claim against. Move the funnel to the landing
page and post-signup users get provisioning with nothing to look at. The plan they
already received is the obvious replacement, but that screen needs designing.

**(c) Does the funnel still show post-signup?** If a user signs up *without*
going through the landing funnel (direct `/sign-up` link, invite), they still need
to answer. Keeping `/onboarding` as a fallback means two entry points to maintain.
Recommend: keep it, render it only when no brief is pending.

## 11. Test plan

Backend:

- `BriefBody` validator is shared — assert `leads/brief` and `onboarding/brief`
  reject and normalise identically (`grow` requires input; blank `new` input →
  `None`; `<3` chars rejected).
- `leads/brief` creates exactly one row and does not touch `PaperclipRepo`,
  `funnel_state`, or dispatch `ensure_user_ready`. Assert with mocks that those
  are never called — this is the regression test for the §0 invariant.
- `leads/email` on unknown `lead_id` returns `202` and writes nothing.
- Plan withholding: `GET …/plan` returns 2 full moves + 3 titles before send,
  all 5 after.
- Replay idempotency: post the same brief twice post-signup, assert one pending
  item and one funnel claim.

Frontend:

- Full flow: choose → input → generation → email → signup CTA.
- The "you pick" path submits with `input` absent.
- Refresh mid-flow restores draft **and** `lead_id`.
- Back navigation from every step.
- `prefers-reduced-motion` skips the generation animation and shows the plan.

## 12. Rollout

Feature-flag the landing hero swap. Ship the `leads/*` endpoints and the table
first — they are inert until the frontend calls them. Then flip the hero for a
percentage of unauthenticated traffic, watching signup conversion against the
current gated funnel. `/onboarding` stays live throughout, so rollback is a flag
flip with no data migration.

## 13. Prior art

- `docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md` — the
  intake/fulfilment split this design sits on top of.
- `docs/superpowers/specs/2026-07-12-onboarding-aha-first-run-design.md`
- `apps/frontend/src/components/onboarding/FirstRunFunnel.tsx` — the funnel being
  relocated. Copy and branch semantics are authoritative there, not here.
- `docs/landing-rehaul/` — the visual language these screens are built in.
