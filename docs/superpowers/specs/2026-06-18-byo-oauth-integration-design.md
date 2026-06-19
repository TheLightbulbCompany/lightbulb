# Bring-Your-Own OAuth App (Integrations) — Design

Date: 2026-06-18
Status: Draft (verified against code via the `verify-byo-oauth-design` workflow, 2026-06-18)

## Goal

Let an owner connect an integration using **their own OAuth application** (their
own `client_id` / `client_secret`) instead of Isol8's shared app. This unblocks
the one integration that is otherwise unreachable for most users: **Google
Workspace (gog)**.

Today gog is gated on two axes (`providers.yaml`):
- `internal: true` — visible only to `INTERNAL_INTEGRATION_OWNERS`.
- `requires_bedrock: true` — connectable only by owners on Bedrock-managed
  inference, because Google's Limited Use policy forbids Gmail/Calendar/Drive
  content being used to train a model, and Bedrock is the only provider path
  where Isol8 guarantees no-training.

So a **ChatGPT-OAuth owner** (non-Bedrock) cannot use Google at all. The honest
escape hatch — the same one direct-OpenClaw users take — is for that owner to
**bring their own Google OAuth app**: then the data relationship and Limited-Use
compliance are between the *user and Google under the user's app*, not Isol8's,
so Isol8's no-training promise is not implicated.

## Non-goals (explicit)

- **No marketplace coupling.** This change must not alter marketplace deploy or
  hire behavior. (Verified — see "Verification".) This is a hard invariant; any
  movement in the marketplace test suites stops the work.
- **Not changing the OAuth dance shape.** Still 3-legged `authorization_code` +
  PKCE; the only change is *which app's credentials* it uses.
- **Not a per-owner app for every provider now.** The mechanism is generic
  (any `direct_oauth` provider can opt in via a registry flag), but **only
  `google` is enabled.** Others keep the managed Isol8 app for everyone.
- **Escape-hatch only, for now.** BYO is offered to owners who can't otherwise
  connect the provider (gated). A power-user "use my own app instead of the
  managed one" toggle for already-connectable providers is a later policy
  choice, not in scope.
- **Not building a new credential rail or secret store.** Extends the existing
  registry, dance, secret store, and connect flow.

## Architecture (data flow)

```
Owner (integrations tab) ──"Use your own Google app"──▶ paste client_id/secret
                                                          + acknowledge Limited-Use
                                          │
                          PUT /connections/google/byo-app  (store app config)
                                          │
                          isol8/<env>/integrations/<owner>/oauth-app/google   (Secrets Manager, RW-granted prefix)
                                          │
Owner clicks Connect ──▶ /connections/google/authorize
        │                     └─ oauth_dance.start(owner_id, "google")
        │                          └─ _oauth_client(owner_id,"google") → owner's app if stored, else Isol8's
        ▼
   Google consent (owner's app) ──code──▶ /connections/google/callback
                                              └─ oauth_dance.exchange(...) → tokens (minted by owner's app)
                                                   └─ apply_connection → gog_cli materializer
                                                        └─ seeds in-container gog keyring on EFS
                                                             (client_id/secret/refresh_token — gog self-refreshes; refresh_owner: actor)
```

The backend retains **no** Google token (`_retains_secret == False` for gog).
The two durable copies are:
1. the **gog keyring blob on EFS** (the refresh token; gog self-refreshes), and
2. the **BYO app config** (`client_id`/`client_secret`) in Secrets Manager,
   which `_oauth_client` re-reads on every connect/reconnect.

## Components

### 1. Registry — `core/services/credentials/providers.yaml` + `registry.py`

Two new optional `Provider` fields (backward-compatible defaults → existing
entries unaffected, and the marketplace classifier ignores unknown fields):

- `allow_byo_app: bool = False` — set `true` on `google`.
- `byo_setup_url: str | None` — link to the Google Cloud setup guide shown in
  the wizard.

### 2. Per-owner BYO app secret store

The user's app `client_id`/`client_secret` live in Secrets Manager.

**Path (IAM-correct):** nest under the already-granted `integrations/` prefix —
`isol8/<env>/integrations/<owner>/oauth-app/<provider>`. The backend task role's
**RW** grant is scoped to `isol8/<env>/integrations/*` (`service-stack.ts:453-455`);
the broader `isol8/<env>/*` grant is **read-only** (`service-stack.ts:379-390`),
so a separate `oauth-apps/*` path would `AccessDenied` on write. Nesting under
`integrations/` needs **no infra change.**

`integration_secret_store._secret_name` currently hardcodes
`isol8/{env}/integrations/{owner}/{provider}` (`integration_secret_store.py:28-33`).
Add a small parameterized variant (e.g. a `kind="oauth-app"` segment) so the BYO
app config is a distinct secret from any retained integration credential.

This secret holds ONLY the app config, never a token:
```json
{ "client_id": "...", "client_secret": "...", "acknowledged_at": "ISO8601" }
```

### 3. OAuth dance — `core/services/credentials/oauth_dance.py`

The single hinge. `_oauth_client` currently takes only `provider_key`
(`oauth_dance.py:27`). Thread `owner_id` (already in scope at `start()`/`exchange()`,
just unused for the lookup):

```python
async def _oauth_client(owner_id: str, provider_key: str) -> tuple[str, str]:
    byo = await oauth_app_store.get(owner_id, provider_key)   # the new BYO app secret
    if byo:
        return byo["client_id"], byo["client_secret"]
    return await _isol8_oauth_client(provider_key)            # today's Secrets Manager path
```

This flows through `start` (`:57`) and `exchange` (`:76`) automatically. The
credential dict keys are unchanged, so `apply_connection` → `gog_cli` consume it
identically (verified BYO-generic).

### 4. Connect flow + gate carve-out — `routers/connections.py`

**Four** enforcement sites must carve out together when the owner has a BYO app
configured + acknowledged for the provider (the verification corrected an
initial "two gates" undercount — missing the LIST filter would have left the
feature invisible):

| Site | What it does | Carve |
|------|--------------|-------|
| `_require_grantable_provider` (`:88`) | 404s a non-allowlisted owner on an `internal` provider | skip `internal` when BYO configured |
| **LIST/catalog filter (`:209`)** | omits `internal` providers from `GET /connections` for non-allowlisted owners → **no row, no Connect button** | include when BYO configured; **keep the `connected == "connected"` survivor clause** so an already-connected BYO Google never disappears |
| `_require_bedrock_for` (`:129/131`) | 403 + "Switch to Bedrock" upsell unless on Bedrock | skip `requires_bedrock` when BYO configured |
| **callback re-check (`:382`)** | post-exchange bedrock re-check → `?error=requires_bedrock` redirect | skip in lockstep, else a BYO owner is bounced *after* a valid connection |

Carve condition (single predicate, reused at all four sites):
`allow_byo_app && owner has a stored+acknowledged BYO app for this provider`.

**Without** a BYO app, every gate and the Switch-to-Bedrock upsell behave
exactly as today (managed-flow path untouched).

New endpoints:
- `PUT /connections/{provider}/byo-app` — store `client_id`/`client_secret` +
  set `acknowledged_at` (requires the Limited-Use acknowledgment).
- `DELETE /connections/{provider}/byo-app` — clear it (does not disconnect an
  active connection; mirrors the existing un-gated disconnect posture).
- `GET` exposure: the connections payload surfaces whether a provider is
  BYO-capable and whether the owner has a BYO app configured, so the UI can
  render the right affordance. (`internal` itself stays filter-only, never on
  the wire — `useConnections.ts:19-32`.)

### 5. Materializer — unchanged

`gog_cli` bakes in **no** Isol8 `client_id`/`secret`; the only Isol8 secret it
uses is `GOG_KEYRING_PASSWORD` (`gog_cli.py:85`), orthogonal to OAuth identity.
A BYO-minted token materializes identically. No change.

### 6. Frontend — `/settings/connections` (integrations tab)

The tab is mature and server-driven (per-row state machine in `ConnectButton.tsx`,
billing-aware locks, OAuth-return banners, tests). BYO is **additive + small**:

- A new `ConnectButton` branch for a BYO-capable, currently-locked provider:
  "Use your own Google app".
- A paste affordance for `client_id`/`client_secret` (the inline-paste UI at
  `ConnectButton.tsx:175-216` is a ready template).
- The setup steps (create Google Cloud project, enable Gmail/Calendar/Drive
  APIs, create an OAuth client, **register the exact redirect URI we display**,
  paste credentials) — driven by `byo_setup_url`.
- The Limited-Use **acknowledgment** checkbox.
- Extend the `Connection` type with the BYO-capable / BYO-configured flags.

The provider only reaches the UI **after** the backend `:209` carve-out — the
load-bearing work is backend.

## Error handling

- BYO app store/get failures → surface a clear 4xx; never fall back to Isol8's
  app silently for an owner who *intended* BYO (avoids confusing token-mint
  mismatches).
- A bad `client_id`/`secret` surfaces at Google's consent screen / token
  exchange; map the exchange error to a connections-tab error banner.
- `DELETE byo-app` while connected: keep the live connection (gog keyring on EFS
  is independent); the owner can re-auth later.

## Testing

- **Dance:** `_oauth_client` returns the owner's app when stored, Isol8's when
  not; `start`/`exchange` use the resolved app.
- **Carve-out:** all four sites (`:88`, `:131`, `:209`, `:382`) lift only when
  BYO configured + acknowledged; with no BYO app, each still gates + the
  Switch-to-Bedrock upsell still fires. Explicitly test the **LIST visibility**
  carve (the dead-code trap).
- **Survivor clause:** an already-`connected` BYO Google stays listed after the
  BYO app is deleted.
- **No-marketplace-coupling guard:** run `test_catalog_service` +
  `test_marketplace_listings` unchanged; a BYO connect only adds a `connected`
  row.
- **Secret store:** the parameterized `_secret_name` writes under the
  RW-granted `integrations/` prefix (no AccessDenied).

## Verification (workflow `verify-byo-oauth-design`, 2026-06-18)

- **Refresh works; the feared failure mode cannot occur.** Google is
  `refresh_owner: actor` — no backend refresh path; gog self-refreshes from the
  BYO-seeded keyring (`gog_cli.py:69-77`). `_oauth_client` is the only OAuth-app
  resolution point.
- **No marketplace coupling — confirmed.** `assert_credentials_satisfied`
  (`catalog_service.py:121-129`) keys on `integrations_repo` `connected` rows;
  reads neither `internal` nor `requires_bedrock`; never imports the connect
  flow. BYO connect is purely additive.
- **Corrections folded in:** four carve-sites (not two); BYO secret nested under
  the RW-granted `integrations/` path (the `oauth-apps/*` path was IAM-blocked);
  `owner_id` threaded into `_oauth_client`.

## Open decision (founder)

The exact **Limited-Use acknowledgment wording** and whether it is a hard
checkbox. Placeholder until provided. (Substance: the owner affirms that, using
their own Google app with non-Bedrock inference, they accept responsibility for
their app's compliance with Google's Limited Use policy.)

## Build size

Backend-weighted and modest: registry fields, a parameterized secret name, the
`owner_id` thread, four small carve-outs + two endpoints. Frontend is one
`ConnectButton` branch + a paste/setup affordance. No infra change required (the
secret nests under an already-granted prefix).
