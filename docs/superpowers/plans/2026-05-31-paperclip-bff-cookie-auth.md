# Paperclip BFF Auth — Unified Client-Side Per-User Session Cookie

> Status: DRAFT for review. Not started. Replaces the per-request Paperclip
> sign-in ("the storm") with a single browser-held `.isol8.co` session cookie
> reused across both user-facing surfaces. **No Paperclip-server fork.**

## Problem

Both user-facing Paperclip surfaces sign the user into Paperclip **on every
request**:

- **Native Teams BFF** (`/api/v1/teams/*`): `resolve_teams_context` →
  `session_factory` → `get_user_session_cookie` runs `POST /api/auth/sign-in/email`
  per request (`routers/teams/deps.py:87`, `core/services/paperclip_user_session.py:35-62`).
  Its own docstring flags this as a stopgap ("V1: per-request sign-in … V2:
  cache").
- **Board proxy** (`company.isol8.co`): every proxied request mints a fresh
  session via `sign_in_user` (`routers/paperclip_proxy.py:1138`; module docstring
  "v1, no session caching").

Under burst load (e.g. bulk board actions + panel auto-refetch + browser
retries) this becomes a re-auth storm: `httpx.ReadTimeout` → 500s → browser
retries → self-sustaining. Verified live 2026-05-31 (~82 panel 500s + ~90
sign-ins per 3 min).

## Why this design (decision record)

Exhaustively evaluated (see session research): per-user board API keys (no mint
endpoint exists), Better Auth JWT/bearer (plugins off; would require forking
`auth.ts` to verify a foreign JWT), the upstream cloud-tenant header path
(REST-only, forces `isInstanceAdmin`, scopes by `hash(stackId)` — unsafe on a
shared server without a fork), and a server-side cookie cache (SM/in-process).
**Chosen: client-side cookie reuse**, because:

- The Paperclip session is a **user-owned** credential; the browser is where
  session cookies belong (admin credentials stay system-owned in Secrets
  Manager — that distinction is the whole point).
- **Zero new infra** (no SM per-user secret, no DDB session table — aligns with
  the secrets-out-of-DDB credential redesign), **zero Paperclip fork**, **fully
  stateless BFF** (scales across N tasks).
- The proxy **already** mints a `.isol8.co`-scoped HttpOnly Paperclip cookie on
  the browser (`_rewrite_set_cookie_domain`, `paperclip_proxy.py:407-440`) and
  then throws it away — half the work exists.

The cookie stays **HttpOnly** (browser auto-sends, JS cannot read → no XSS
exfiltration). "Client-side" here means the browser cookie jar, not a
JS-readable token.

## Current state (grounded)

### Two surfaces, different transports
- **Native Teams BFF**: browser → `api.isol8.co/api/v1/teams/*` with a **Clerk
  Bearer JWT** header (`core/auth.py:169-187`); `credentials` is never set on the
  fetch (`apps/frontend/src/lib/api.ts:154-157`) → **the browser sends no cookies**.
  The Paperclip cookie stays backend-only.
- **Board proxy**: the entire `company.isol8.co` host is reverse-proxied
  (`routers/paperclip_proxy.py`); Clerk bypassed; reached by top-level navigation
  (not an iframe). The browser holds `isol8_paperclip` (host-scoped identity JWT,
  8h) **and** the upstream Paperclip session cookie rewritten to `Domain=.isol8.co`.

### Cookie mechanics (load-bearing)
- Name: `__Secure-paperclip-default.session_token` (Better Auth, instance id
  defaults to `default`; `paperclip/server/src/auth/better-auth.ts`). HttpOnly,
  Secure, SameSite=Lax, Path=/. The `__Secure-` prefix permits `Domain=` (only
  `__Host-` forbids it), so the `.isol8.co` rewrite is valid.
- **Origin requirement (#1 failure mode):** any forwarded cookie must be replayed
  with `Origin: <PAPERCLIP_PUBLIC_URL>` (`https://[dev.]company.isol8.co`) or
  Better Auth's `getSession` looks for the wrong (bare vs `__Secure-`) cookie name
  and returns anonymous → 401 (`core/services/paperclip_admin_client.py:217-218`,
  rationale `:194-204`).
- Session life: **7-day** expiry, **1-day** sliding `updateAge` (library
  defaults; no `session` block in Paperclip's config). Reused within a day → no
  rotation; touched once/day → effectively indefinite; untouched → dies at 7d. On
  rotation Better Auth emits a fresh `Set-Cookie` that must be relayed back.
- **No invalidate-on-401 in the user path** today (only the admin path has it:
  `core/services/paperclip_admin_session.py:151-159`).

### Domains — all same-site under `.isol8.co`
frontend `isol8.co`/`dev.isol8.co`, api `api.isol8.co`/`api-dev.isol8.co`, board
`company.isol8.co`/`dev.company.isol8.co`. A `Domain=.isol8.co` cookie is
eligible cross-subdomain; `SameSite=Lax` permits it on same-site
navigations/fetches. The only blocker on the native BFF is that its fetch never
sets `credentials: 'include'` and CORS doesn't allow credentials.

### Provisioning interaction (prerequisite — do not break)
- **`/teams` does NOT provision.** The only canonical trigger is
  `GET /container/status` → `ensure_user_ready` (`core/services/user_provision.py`),
  polled by `/chat`'s `ProvisioningStepper`. Webhooks are record-keeping only.
- `resolve_teams_context` is a read-gate: `None`→409, `provisioning`→202,
  `failed`→503, `active`→OK (`routers/teams/deps.py:78-85`). The cookie mint runs
  **only after** the `active` check (`:87`), so it is already correctly fenced
  behind provisioning.
- Rows go `absent → active`/`failed` directly; `status="provisioning"` is
  effectively never written. The frontend auto-polls only on **202**, so a user
  landing **directly on `/teams`** with no row gets a **409 terminal error and
  never provisions** (existing gap — see Open Decision D1).
- The per-user cookie mint depends on the `paperclip-companies` row being
  `active` with a valid Fernet password — the same password provisioning
  generates/stores (`paperclip_provisioning.py` `provision_org`/`provision_member`).

## Goal / non-goals

**Goal:** one browser-held `.isol8.co` Paperclip session cookie serves both
surfaces; the BFF forwards it (with Origin) and signs in only on cold-start /
expiry / 401. Storm eliminated on both surfaces. Stateless, no fork, no new infra.

**Non-goals (explicitly out of scope):**
- WS fan-out HA / `desiredCount=1` (`teams_event_broker` in-process; #518
  unwired) — separate workstream.
- Moving the Paperclip sign-in **password** out of DDB — belongs to the
  credential-layer redesign track.
- The Paperclip live-events WS (optional, droppable; cookie covers it anyway).

## Design

### The cookie contract (shared by both surfaces)
A single per-user credential: the Better Auth `__Secure-paperclip-default.session_token`,
set on the browser with `Domain=.isol8.co`, HttpOnly, Secure, SameSite=Lax,
Path=/. Minted by a server-side sign-in only on miss/expiry/401; otherwise read
from the request and forwarded upstream with the `Origin` stamp.

### Shared helper: `resolve_or_mint_session_cookie(request, response, user_id)`
One module (e.g. `core/services/paperclip_session_cookie.py`) used by both
surfaces:
1. Read `__*.session_token` from `request.cookies` (match on `.session_token`
   suffix, not a hardcoded prefix).
2. If present → return it (caller forwards upstream with Origin). 
3. If absent → acquire a **per-user singleflight lock** (see cold-start), sign in
   via the existing `get_user_session_cookie` path, set the cookie on `response`
   (domain-rewritten to `.isol8.co`, HttpOnly), return it.
4. `invalidate(user_id)` / re-mint on a downstream 401 (mirror
   `paperclip_admin_session.invalidate_admin_session`).
Reuse `_rewrite_set_cookie_domain` (`paperclip_proxy.py:407-440`) and
`_extract_session_cookie` (`paperclip_admin_client.py:99-132`).

### Surface 1 — board proxy (the easy half)
- Stop stripping the Paperclip `*.session_token` cookie inbound
  (`_filter_request_headers`, `paperclip_proxy.py:347` currently drops `cookie`);
  read it from `request.cookies`.
- If present → forward as upstream `Cookie:` (with Origin), **skip** the
  unconditional `sign_in_user` (`:1138`). If absent → sign in (existing path);
  the existing `.isol8.co` Set-Cookie rewrite (`:1259-1289`) seeds the browser.
- On upstream 401 → evict + sign in once + reseed.
- Keep `isol8_paperclip` (host-scoped identity) as-is.
- (Perf, optional) hoist the per-request `httpx.AsyncClient` (`:1132`) to a shared
  client.

### Surface 2 — native Teams BFF (the work)
- **Frontend** (`apps/frontend/src/lib/api.ts` / `useApi`): add
  `credentials: 'include'` on Teams calls so the browser sends the `.isol8.co`
  cookie to `api.isol8.co`. Keep the Clerk Bearer header (still identifies the
  user). 
- **CORS** (`apps/backend/main.py` CORSMiddleware): `allow_credentials=True` with
  the **specific** origin(s) `https://isol8.co` / `https://dev.isol8.co` (never
  `*`). Confirm/adjust current config (`CORS_ORIGINS` = `isol8.co` per
  `service-stack.ts:688-693`).
- **Routes** (`routers/teams/deps.py` + the `_ctx` helper in
  `routers/teams/agents.py:279-307`): thread `Request`/`Response`. In
  `resolve_teams_context`, after the `status=="active"` gate, call the shared
  helper instead of `session_factory` directly: read the inbound cookie → use it;
  on miss/401 → sign in + set cookie on the response. Relay Paperclip rotation
  Set-Cookie back to the browser.

### Cold-start singleflight (the one real subtlety)
The Teams page fires ~6–8 panel requests in parallel. On the **first** load (no
cookie yet) they would all race to sign in before any `Set-Cookie` lands — a
mini-storm. The shared helper holds a **per-user `asyncio.Lock`** around the cold
mint (mirroring `_AdminSessionState`, `paperclip_admin_session.py:67`): the first
request signs in, the rest await and reuse the result. Bounds cold-start to **one**
sign-in per user. This is a coalescer for the cold mint only — **not** a
persistent cache (the browser is the cache).

### Security
- **Origin stamp** on every forwarded request (else 401). Non-negotiable.
- **Confused-deputy binding:** a request authenticates identity via Clerk bearer
  (`auth.user_id`) but forwards a Paperclip cookie. Ensure a request can only use
  *its own* user's cookie. HttpOnly + SameSite already make cross-user injection
  hard; additionally bind the Paperclip cookie to the Clerk `sub` (e.g. set an
  HMAC-signed companion cookie like the existing `isol8_paperclip`,
  `paperclip_proxy.py:506-521`, and reject if it doesn't match
  `auth.user_id`). On mismatch → evict + re-mint as that user. Finalize the exact
  binding in implementation.
- Cookie remains HttpOnly (no JS read). Per the agreed posture, the
  cookie-stays-backend rule applied only to the **admin** cookie; the user cookie
  in the browser is by-design acceptable.

## Provisioning interaction & Open Decision D1
The cookie mint stays fenced behind `status=="active"` (no change to
provisioning correctness). But this work touches the cold-start path, so we
should decide whether to also close the existing "direct-to-`/teams` 409
terminal" gap:
- **D1a (recommended):** make the `/teams` cold path trigger provisioning
  on 409 (dispatch `ensure_user_ready` like `/container/status` does) and have
  the frontend poll on 409 as well as 202, so a user who lands directly on
  `/teams` actually onboards (matches the expectation that "/teams provisions").
- **D1b:** leave provisioning to `/chat` only; document that `/teams` requires
  prior `/chat` onboarding. Smaller scope; preserves today's behavior.

## Tests
- Warm path: cookie present → **zero** sign-ins, forwarded with Origin.
- Cold start: no cookie → one sign-in, cookie set on response (`.isol8.co`,
  HttpOnly).
- Parallel cold-start (N concurrent) → **exactly one** sign-in (singleflight).
- 401 from upstream → evict + re-mint once.
- Cookie shared across both surfaces (board + native BFF).
- Confused-deputy: a cookie not bound to the Clerk `sub` is rejected/re-minted.
- Provisioning gate unaffected: not-active → 409/202/503 before any mint.

## Rollout
Backend cookie read/forward + frontend `credentials: 'include'` + CORS
`allow_credentials` must ship **together** (the browser won't send the cookie
until both sides are ready) → one coordinated PR (backend + frontend), on a fresh
worktree branch per the workflow. First concrete steps: confirm/adjust CORS, lock
the confused-deputy binding, then the shared helper + both surfaces. Decommission
note: after this, `get_user_session_cookie` runs only on cold/401, not per
request.

## Open decisions
- **D1** — direct-to-`/teams` provisioning (D1a recommended) vs leave to `/chat`.
- **D2** — `credentials: 'include'` scope: Teams calls only, or all `api.ts`
  calls (harmless but sends the cookie on every `.isol8.co` API call).
- **D3** — confused-deputy binding mechanism (signed companion cookie vs accept
  HttpOnly+SameSite trust).
