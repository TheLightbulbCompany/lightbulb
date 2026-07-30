# Gated integration requests + Meta Advanced Access

**Status:** design approved 2026-07-29
**Goal:** offer every integration to every owner with the least friction, including
the ones Isol8 cannot yet fulfil automatically.

## Problem

Instagram and Meta Ads are built, wired, and credentialed in both environments, but
no customer can use them. Two independent locks:

1. **Meta** — app `2080424579211211` is Unpublished (Development mode), so only
   accounts holding a role on the app can complete OAuth. Permissions sit at
   Standard Access; Advanced Access (App Review's output) is what permits other
   businesses' data.
2. **Isol8** — `providers.yaml` gates both providers with
   `environments: [local, dev]` + `owner_allowlist: [<founders' org>]`, and
   `routers/connections.py` *skips* gated providers entirely, so prod owners see
   nothing at all.

The second lock is ours and currently wastes demand: an owner who wants Instagram
has no way to say so. Meanwhile Meta's own tester model (Instagram Tester +
`owner_allowlist`) already lets us onboard named people today — it just needs a
front door.

## Load-bearing decision: gating means "requestable", not "hidden"

`_available_for` answers one question today — show or hide. It splits into two:
*can this owner connect now* and *should this owner see it at all*.

| Owner | can connect | affordance |
|---|---|---|
| On `owner_allowlist` (founders' prod org) | yes | **Connect** |
| Any other prod owner | no | **Request access** |
| Any dev/local owner | yes (env match) | **Connect** |

No `providers.yaml` change. The gate data is unchanged; only its interpretation
moves. This is deliberate: when App Review lands, deleting the two gate lines
turns every "Request access" into "Connect" with no other edit, and the request
affordance disappears on its own. The feature is designed to become dead code.

## Phase 1 — requestable flag, request affordance, skill guidance

### Backend

`routers/connections.py:285-286` stops `continue`-ing past gated providers.
Each listed provider gains:

- `requestable: bool` — true when `registry.available_for(p, owner_id)` is false
- `status: "unavailable"` for those (distinct from `disconnected`, which means
  connectable-but-not-connected)

`routers/platform_info.py` likewise returns gated providers with
`requestable: true` rather than filtering them out. This preserves the fix in
PR #1502 — agents still never claim a gated integration is merely a click away —
while letting them raise it as requestable. The public route can only evaluate the
env half of the gate (it has no owner id by design), so an allowlisted owner sees
one entry marked requestable that is in fact connectable for them. That asymmetry
is acceptable and must be noted in the skill copy: the agent defers to its own
toolset for ground truth.

### Frontend

`ConnectionsList` renders a **Request access** button where `requestable` is true,
in place of Connect. It calls the existing shared dialog:

```ts
openSupport({ category: "Integration request", message: `I'd like to use ${displayName}.` })
```

`OpenSupportOptions` gains an optional `message` field (it currently carries
`category`, `error`, `requestId`). `SupportTicketDialog` seeds its message state
from it, exactly as it already does for `category`.

**No new endpoint and no new email code.** `POST /api/v1/support/tickets` →
`core/services/support_ticket.submit_ticket()` already enriches with the owner's
account and diagnostics and delivers to `EMAIL_SUPPORT_ADDRESS`
(`support@isol8.co`). The enrichment is the point: the resulting email carries the
Clerk owner id, which is exactly what an operator needs to add them as an
Instagram Tester and to `owner_allowlist`.

**No request records.** Deliberately no DynamoDB table, no "Requested" state, no
dedupe, no admin queue. A duplicate email costs nothing; a table that outlives the
gate costs a migration. If gating turns out to be permanent this is the first thing
to revisit.

### Skill

`apps/infra/openclaw/skills/isol8/SKILL.md` gains a short block in the existing
"Integrations and channels" section defining three states:

- **tools present in my toolset** → use them; this is ground truth
- **`requestable: false` in platform-info** → tell the owner where to click
- **`requestable: true`** → tell the owner to hit *Request access* on that
  integration; a human sets it up. Do not promise a timeline.

Plus first-run guidance: when the owner's brief implies a gated capability, raise it
once, unprompted. Do not re-raise on later turns.

## Phase 2 — deauthorize + data-deletion webhooks

Meta blocks Instagram App Review until the app declares both, and they are
**callback endpoints Meta POSTs to**, not the human-readable page already set under
App settings → Basic (`https://isol8.co/support#data-deletion`). Currently both
fields are empty in Instagram Business login settings.

Two new routes:

- `POST /api/v1/webhooks/meta/deauthorize` — Meta signals the owner revoked the
  app. Tear down that owner's connection for the affected provider (the existing
  disconnect path), leaving the rest of the account intact.
- `POST /api/v1/webhooks/meta/data-deletion` — Meta requests deletion of that
  user's data. Must return the JSON confirmation shape Meta expects
  (`{url, confirmation_code}`).

Both verify Meta's signed request (`signed_request`, HMAC-SHA256 with the app
secret) and must fail closed on a bad signature — the same posture as
`CLERK_WEBHOOK_SECRET`. The app secret already lives in Secrets Manager at
`isol8/{env}/integrations/platform-oauth/meta-ads` and
`.../meta-instagram`; note these are **different secrets for different Meta apps**
and the verifying route must pick the right one.

Then register both URLs in Instagram Business login settings.

## Phase 3 — App Review submissions (runbook, not code)

Blocked on Phase 2 and on API traffic. Every permission currently reads **0 API
calls**; Meta will not accept a submission without traffic, and traffic requires a
real connect.

1. Connect Instagram from the founders' prod org (`/settings/connections`), then
   exercise publish, comments and DM tools so calls land on the counters. Meta's
   own note: testing data can take **up to 24 hours** to appear, and each test is
   valid **30 days**.
2. Record a screencast per permission showing it working inside Isol8:
   `instagram_business_basic`, `_content_publish`, `_manage_comments`,
   `_manage_messages`.
3. Submit those four for Advanced Access.
4. For ads: connect Meta Ads, generate traffic, then submit
   **`ads_mcp_management`** for Advanced Access — that is the permission gating the
   hosted ads MCP for other businesses' ad accounts.
5. **Publish** the app (self-service, no review) once Advanced Access lands.
6. Delete `environments` + `owner_allowlist` from both providers in
   `providers.yaml`. Every Request access becomes Connect.

Expect 1–4 weeks per review round; a rejection restarts the clock.

## Testing

- `connections.py`: a gated provider is listed with `requestable: true` for a
  non-allowlisted owner and `requestable: false` for an allowlisted one — the
  asymmetry is the whole feature, so pin both sides.
- `platform_info.py`: extend the existing
  `test_env_gated_provider_is_not_advertised` to assert the provider is now
  *present* with `requestable: true` rather than absent. That test currently
  encodes the opposite; updating it is part of the change, not incidental.
- Frontend: a `requestable` provider renders Request access and not Connect.
- Phase 2 routes: a bad signature is rejected; a valid deauthorize disconnects only
  the named provider for the named owner.

## Out of scope

- Request records, admin queue, "Requested" UI state (see Phase 1 rationale)
- WhatsApp — parked; OpenClaw's plugin is Baileys/WhatsApp-Web, unofficial, with
  number-ban risk if sold to customers
- Any change to `providers.yaml` gate data in Phases 1–2
