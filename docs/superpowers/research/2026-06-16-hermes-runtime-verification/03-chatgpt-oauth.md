# Hermes runtime verification — #03 ChatGPT/Codex OAuth schema + refresh ownership

**Date:** 2026-06-16
**Scope:** Audit open questions #3 (refresh ownership / write-back) & #4 (exact `auth.json` schema).
**Source:** Hermes v0.14.0 (`pyproject.toml:` `version = "0.14.0"`) at `/Users/.../hermes-agent`.
**Verdict:** The prior audit (`docs/audit-2026-05-17-hermes-vs-openclaw.md` §5.4) is **materially wrong on the two-file dance for v0.14.0.** Hermes no longer auto-imports or writes back `~/.codex/auth.json` at runtime. There is exactly ONE runtime store: `~/.hermes/auth.json`. The hosted refresh-ownership problem is therefore **structurally identical to what Isol8 already runs for OpenClaw today** — and Isol8 already accepts it (DDB = seed, on-disk = authoritative, no write-back).

---

## ⚠️ CONTRADICTION with the prior audit (highest-value finding)

The audit (`audit §5.4`, lines 766–781) claims, quoting `credential_sources.py:294-298`:

> "Codex tokens live in TWO places: our auth store AND `~/.codex/auth.json`. `refresh_codex_oauth_pure()` writes both every time… `_seed_from_singletons()` would re-import from `~/.codex/auth.json` on the next `load_pool()` call."

**This is false in v0.14.0.** Two independent code reads contradict it:

1. **No auto-import at pool load.** `agent/credential_pool.py:1512-1543` (`_seed_from_singletons`, `provider == "openai-codex"` branch) reads tokens *only* from the Hermes auth store via `_load_provider_state(auth_store, "openai-codex")` (line 1520). The inline comment is explicit (`credential_pool.py:1522-1527`):
   > "Hermes owns its own Codex auth state — we do NOT auto-import from `~/.codex/auth.json` at pool-load time. OAuth refresh tokens are single-use, so sharing them with Codex CLI / VS Code causes refresh_token_reused race failures."

2. **`refresh_codex_oauth_pure()` writes nothing** (it is "pure" — `hermes_cli/auth.py:2702-2803`). It does the HTTP refresh and *returns* a dict; it never touches disk. The only writer is `_save_codex_tokens()` (`auth.py:2688-2699`), which writes **only** `~/.hermes/auth.json` (via `_save_provider_state` + `_save_auth_store`). `~/.codex/auth.json` is never written by Hermes — confirmed by `_import_codex_cli_tokens` docstring (`auth.py:2828-2832`): *"Does NOT write to the shared file."*

The `credential_sources.py:293-322` docstring the audit quoted describes the **`hermes auth remove`** suppression machinery and reflects an older model; the file header even still says it (`credential_sources.py:8`: "device_code — auth.json providers.<provider>"). But the live seed path (credential_pool.py) was rewritten so the resurrection it warns about no longer happens at runtime. `~/.codex/auth.json` is now a **one-time, interactive, opt-in import source only** (`auth.py:5193-5211`: prompts `"Import these credentials? (a separate login is recommended) [y/N]"`).

**Net:** the "two-file dance" the audit treated as a migration cost does not exist for v0.14.0. One file. Simpler than the audit assumed.

---

## 1. Exact on-disk schema

### 1a. `~/.hermes/auth.json` (the sole runtime store)

Path resolution: `get_hermes_home() / "auth.json"` (`auth.py:780`, `_auth_file_path`). Written atomically `0o600`, parent dir forced `0o700` (`auth.py:1003-1030`).

Top-level shape (`_load_auth_store` `auth.py:963-1000`, `_save_auth_store` `auth.py:1012-1013`):

```jsonc
{
  "version": 1,                        // AUTH_STORE_VERSION (auth.py:63), int
  "updated_at": "2026-06-16T...Z",     // ISO8601, set on every save
  "active_provider": "openai-codex",   // optional str
  "providers": {                       // dict<provider_id, provider_state>
    "openai-codex": { ...codex state... },
    "xai-oauth":    { ... },
    "nous":         { ... },
    "minimax-oauth":{ ... }
  }
}
```

**Codex provider state** `providers["openai-codex"]` (written by `_save_codex_tokens` `auth.py:2688-2699`; read/validated by `_read_codex_tokens` `auth.py:2639-2685`):

```jsonc
{
  "auth_mode": "chatgpt",              // str literal, set on save (auth.py:2697)
  "last_refresh": "2026-06-16T...Z",   // ISO8601 str (auth.py:2690-2691)
  "tokens": {
    "access_token":  "eyJ...",         // str, REQUIRED (validated auth.py:2666-2674)
    "refresh_token": "...",            // str, REQUIRED (validated auth.py:2675-2681)
    "id_token":      "eyJ..."          // str, OPTIONAL — present from device-code login
                                       //   (auth.py:3155,3179-3180); not required by reader
  }
}
```

**No `expires_at` / `expires` field is stored.** Expiry is derived on the fly from the JWT `exp` claim of the access token: `_codex_access_token_is_expiring` (`auth.py:1552-1557`) → `_decode_jwt_claims(access_token)["exp"]`, refreshed when `exp <= now + 120s` (`CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120`, `auth.py:92`). This differs from OpenClaw, which stores an explicit `expires` ms-epoch (see §4).

### 1b. `~/.codex/auth.json` (Codex CLI shared file — import-only, NOT used at runtime)

Path: `${CODEX_HOME:-~/.codex}/auth.json` (`auth.py:2833-2836`). Parsed by `_import_codex_cli_tokens` (`auth.py:2827-2858`):

```jsonc
{
  "tokens": {
    "access_token":  "...",   // str, required for a valid import
    "refresh_token": "..."    // str, required for a valid import
  }
  // (Codex CLI also writes OPENAI_API_KEY + account_id siblings; Hermes ignores them,
  //  reading only payload["tokens"] — auth.py:2841)
}
```

Import is rejected if the access token is already expired (`auth.py:2848-2855`). Hermes copies `tokens` verbatim into its own store via `_save_codex_tokens(cli_tokens)` (`auth.py:5204`) only on explicit `y` at the interactive prompt.

---

## 2. Who refreshes, and where the refreshed token lands

**Refresh owner: the Hermes container, in-process, at runtime resolution.**

- Entry point `resolve_codex_runtime_credentials` (`auth.py:2861-2903`) is called when the agent needs a live token. It checks expiry (`_codex_access_token_is_expiring`, 120s skew), and if expiring takes the cross-process `_auth_store_lock` (`auth.py:2877-2878`), re-reads under lock, and calls `_refresh_codex_auth_tokens`.
- `_refresh_codex_auth_tokens` (`auth.py:2806-2824`): calls the pure HTTP refresh (`refresh_codex_oauth_pure`) then **`_save_codex_tokens(updated_tokens)` — writes the new access AND (rotated) refresh token back into `~/.hermes/auth.json` IN PLACE** (`auth.py:2823`).
- **Hermes calls the token endpoint itself.** `refresh_codex_oauth_pure` POSTs to `CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"` (`auth.py:91`) with `grant_type=refresh_token`, `client_id = CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"` (`auth.py:90`, the Codex CLI's public client id). Response `refresh_token` (when rotated) replaces the old one (`auth.py:2800-2802`).

So: refresh happens **in-container, written back to the same EFS file in place, in-memory pool entry also updated** (`credential_pool.py:946-947` syncs refreshed pool tokens back to `auth.json` providers so the next `load_pool` sees fresh state). **It never reports out of the container.** `~/.codex/auth.json` is NOT touched on refresh.

---

## 3. CRITICAL for hosted multi-tenant: how would our backend learn the new refresh_token?

**It wouldn't — there is no hook, webhook, or event.** Hermes refreshes silently in-place on EFS (`~/.hermes/auth.json`). To learn the rotated refresh_token our backend would have to **poll/read the EFS file** (`<EFS>/users/{user_id}/hermes/auth.json` → `providers.openai-codex.tokens.refresh_token`) on a schedule, or accept that DDB drifts.

Searched for any callback surface: the refresh path (`auth.py:2806-2824`) and pool write-back (`credential_pool.py:946-947`, `_sync_*_back` at line 660) write only to local files. Hermes *does* have a webhook server (`gateway/platforms/webhook.py`, per audit §5.4 / open-q #3) but it is an *inbound* messaging-platform adapter, not a credential-rotation egress hook — nothing in the auth path emits to it.

### This is EXACTLY Isol8's current OpenClaw posture — and we already accept it

Isol8 today does the identical thing for OpenClaw ChatGPT OAuth (`apps/backend/core/services/oauth_service.py` + `apps/backend/core/containers/workspace.py`):

- **Backend → container is write-only.** `pre_stage_auth_profile_store` (`workspace.py:725-829`) writes the DDB-decrypted tokens to `<EFS>/users/{user_id}/credentials/oauth.json` at provision time, `chown 1000:1000` so the container can refresh in place (`workspace.py:809-815`). The only other backend touch is `delete_auth_profile_store` (`workspace.py:832+`) on disconnect.
- **No read-back exists.** Grep confirms the backend never reads `credentials/oauth.json` and there is no container→DDB token-update path. The docstring states the model outright (`workspace.py:752-754`): *"After first import the in-container OAuth manager refreshes the token in place."* DDB is the **seed**; the SQLite/file copy in the container is **authoritative** after first boot.
- Consequence already lived-with: after the container's first in-place refresh, the DDB `refresh_token` is stale. Re-OAuth (writing a fresh seed) is the recovery path; on an already-authed container it needs a reprovision (`workspace.py:749-753`).

**So question #3's "problem" is not new and not Hermes-specific.** Swapping to Hermes changes only the file path and shape — `<EFS>/users/{user_id}/credentials/oauth.json` (OpenClaw flat `{"openai":{access,refresh,expires}}`) becomes `<EFS>/users/{user_id}/hermes/auth.json` (Hermes `{"providers":{"openai-codex":{"tokens":{access_token,refresh_token},auth_mode,last_refresh}}}`). The refresh-ownership architecture (container-owns, no write-back, DDB-as-seed) is carried over unchanged.

---

## 4. Schema mapping for the writer the spike would build

A `core/runtime/hermes/auth_writer.py` (audit §appendix `pre_stage_chatgpt_oauth` for Hermes) would translate the **same** DDB row used today:

| DDB row (oauth_service) | OpenClaw `credentials/oauth.json` (today, `workspace.py:781-793`) | Hermes `~/.hermes/auth.json` (proposed) |
|---|---|---|
| top key | `"openai"` (`_OAUTH_PROVIDER_KEY`, `workspace.py:705`) | `providers."openai-codex"` |
| `access_token` | `.access` | `.tokens.access_token` |
| `refresh_token` | `.refresh` | `.tokens.refresh_token` |
| `expires_at` (ms) | `.expires` (int ms) | **omit** — Hermes derives expiry from JWT `exp`; no field |
| `account_id` | `.accountId` | omit (Hermes ignores it; reads only `tokens`) |
| `email` | `.email` | omit |
| (none) | (none) | `.auth_mode = "chatgpt"`, `.last_refresh = <ISO8601 now>` |
| envelope | `{provider: creds}` | `{"version":1,"providers":{"openai-codex":{...}}}` |

Note the writer must seed `auth_mode:"chatgpt"` and a `last_refresh` timestamp or the reader (`_read_codex_tokens` `auth.py:2658-2681`) still works (it only hard-requires `tokens.access_token` + `tokens.refresh_token`), but matching `_save_codex_tokens`'s shape exactly is safest. File must be `0o600`, owner UID 1000, parent `~/.hermes` `0o700` to match `_save_auth_store`'s own posture (`auth.py:1003-1030`). The `version: 1` constant must be tracked against `AUTH_STORE_VERSION` (`auth.py:63`) on Hermes bumps — same lockstep discipline the audit flagged for OpenClaw's `_AUTH_STORE_VERSION`.

---

## Residual open items for the spike

- **JWT `exp` cold-start:** OpenClaw treats a missing `expires` as already-expired and refreshes on first use (`workspace.py:785-787`). Hermes derives `exp` from the access-token JWT itself — so a seeded (possibly aged) DDB access token will be auto-refreshed by Hermes on first resolve as long as its refresh_token is still valid. Equivalent behavior; verify live that a seeded-but-expired access token triggers a clean refresh rather than a relogin prompt.
- **OAuth Provider Proxy (v0.14.0)** remains the strictly-better long-term option (audit open-q #4 / §5.4 "potentially game-changing") and would eliminate the EFS file entirely — not re-evaluated here; tracked separately.
