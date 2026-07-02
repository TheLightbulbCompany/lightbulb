# Map 04 — Hermes Auth / Credentials / Providers (pressure test)

Basis: `/Users/prasiddhaparthsarthy/Desktop/hermes-agent` @ `basis-v0.14.0-spec` (commit 4c46c35e).
All line numbers verified against that tree. Compared against the design spec
`docs/superpowers/specs/2026-06-16-hermes-runtime-option-design.md`.

Verdict up front: the spec's **auth-hop topology is largely correct and well-grounded** (it
already cites `_check_auth`, the bind guard, the codex refresh path, the Nous-only proxy, and the
single-bearer model). But it **systematically understates the credential surface**: it treats Hermes
as a clean Bedrock/ChatGPT-OAuth/BYOK provider that maps 1:1 onto OpenClaw. The code shows a much
larger, *host-ambient*, multi-source credential pool that actively reads (and writes back to)
machine-global files outside `~/.hermes`. That surface is the real multi-tenant hazard and the spec
never mentions it.

---

## A. The full provider-auth picture (what actually resolves a bearer)

### A1. The auth store is ONE file, but with TWO sub-stores + a global fallback
- `~/.hermes/auth.json` (path resolved with pytest seat-belt, `auth.py:780-805`). Written `0o600`,
  parent forced `0o700`, created via `O_EXCL` to close the umask TOCTOU window
  (`_save_auth_store`, `auth.py:1003-1051`; perms at `:1009`, `:1024`). Spec Q4 cites this correctly.
- The store has **two parallel shapes** the spec's Q4 JSON omits:
  - `providers.<id>` — per-provider OAuth state (codex, nous, xai-oauth, …) (`auth.py:1054-1068`).
  - `credential_pool.<provider>` — a **list** of pooled credentials with rotation/labels
    (`read_credential_pool`/`write_credential_pool`, `auth.py:1099-1156`). Spec Q4 shows only the
    `providers` block; the pool is a whole second mechanism it never models.
- **Global fallback (spec never mentions this):** `read_credential_pool` and `_load_global_auth_store`
  merge a *global-root* `auth.json` as a read-through fallback when a profile has zero entries for a
  provider (`auth.py:1099-1143`, `832-865`). In a multi-tenant image this is a cross-tenant bleed
  primitive if any global `~/.hermes/auth.json` ever exists in the base layer or a shared HOME.

### A2. ChatGPT / Codex OAuth — spec is CORRECT, and it's identical to OpenClaw today
- Provider id is `openai-codex`, `auth_mode="chatgpt"` (`auth.py:2697`). Reader hard-requires
  `tokens.access_token` + `tokens.refresh_token` only (`_read_codex_tokens`, `auth.py:2658-2681`).
- Refresh: container-owns, in-place write-back to `~/.hermes/auth.json`, **no egress hook**
  (`resolve_codex_runtime_credentials` → `_refresh_codex_auth_tokens` → `_save_codex_tokens`,
  `auth.py:2861-2903`, `2806-2824`, `2688-2699`). POSTs to `https://auth.openai.com/oauth/token`
  with the Codex public client_id `app_EMoamEEZ73f0CkXaXp7hrann` (`auth.py:90-91`, `2721-2727`).
  Refresh-token rotation honored (`refresh_codex_oauth_pure`, `auth.py:2800-2802`); `refresh_token_reused`
  raises relogin (`:2756-2763`) — same hazard class as OpenClaw's OAuth (memory `project_openclaw_oauth_auth_model`).
- **Surprise the spec underplays:** `~/.codex/auth.json` is NOT merely "interactive import-only."
  It is imported at `hermes auth` interactive time (`_import_codex_cli_tokens`, `auth.py:2827-2858`,
  called at `:5195`) AND the *credential-pool* seeder reads `~/.codex/auth.json` at **pool-load time**
  (`credential_pool.py:1523` comment + branch). So the "one runtime store" claim (spec line 26) is true
  for the `providers.openai-codex` refresh path but FALSE for the pool path, which still reaches into
  `~/.codex/`. Minor for a container (we won't ship `~/.codex`), but the spec's blanket "never read at
  runtime" is too strong.

### A3. Bedrock — ambient AWS chain, no per-tenant scoping (spec correct, hazard understated)
- `agent/bedrock_adapter.py` uses the **default boto3 credential chain** (env → profile → instance
  role), zero key management (`_get_bedrock_runtime_client`, `bedrock_adapter.py:74-84`; module
  docstring `:9-15`). Detection via `has_aws_credentials` (`auth.py:1479-1486`, `4724-4727`).
- This is exactly Isol8's container-task-role model — but Hermes has **one process-wide AWS identity**.
  There is no notion of "this request's tenant"; whatever IAM the task assumes is fully available to
  any caller who passes `_check_auth`. Fine because Isol8 already isolates one container per owner —
  but it means the runtime contributes **zero** tenant isolation; isolation is 100% the ECS/IAM boundary.

### A4. BYOK — `~/.hermes/.env` + os.environ, much wider than "Anthropic/OpenAI"
- `resolve_api_key_provider_credentials` (`auth.py:4734-4776`) resolves any `auth_type=="api_key"`
  provider from `PROVIDER_REGISTRY`. The registry is large (bedrock, kimi-coding, zai, lmstudio,
  minimax, qwen, gemini, xai, …) — far beyond the spec's 3-path framing.
- Env seeding prefers `~/.hermes/.env` over `os.environ` (`env_loader.py:150-151`,
  `credential_pool._seed_from_env` `:1579-1641`). **Consequence the spec misses:** any provider API
  key injected as a task-def env var becomes a live credential automatically. That's convenient, but
  it also means the BYOK "write to `~/.hermes/.env`" plan (spec line 307-308) competes with os.environ
  precedence — if both are set the file wins, which can silently shadow a rotated env-injected key.

### A5. Other OAuth providers (entirely absent from the spec's mental model)
- xAI Grok OAuth (`resolve_xai_oauth_runtime_credentials`, `auth.py:3194`; tokens in
  `providers."xai-oauth"`), Gemini OAuth (`:1762`), Qwen (`:1702`), MiniMax (`:5972`),
  Codex-runtime-switch, Copilot via `gh auth token`. Each has its own refresh + store branch. None
  matter for the *initial* Isol8 port (we seed only codex/bedrock/byok), but they mean the
  `write_auth` adapter facet (spec §5) is hiding a much larger schema-tracking burden than "translate
  one DDB row." Every Hermes bump can move any of these shapes; spec line 114 only commits to tracking
  `AUTH_STORE_VERSION`, which does **not** version the per-provider `tokens` sub-shapes.

---

## B. The OAuth Provider Proxy — spec is CORRECT and the contradiction is real
- `hermes proxy` is loopback (`DEFAULT_HOST="127.0.0.1"`, `proxy/server.py:53`; port 8645 `:52`),
  **accepts any inbound bearer and discards it** (`proxy/__init__.py:7-10`; server replaces
  `Authorization` at `server.py:150-151`, no inbound auth check anywhere in `handle_proxy`
  `:119-202`), and the adapter registry has **exactly one entry** `{"nous": NousPortalAdapter}`
  (`proxy/adapters/__init__.py:14-17`). It is a credential-attaching forwarder by design
  (`server.py:1-10`).
- Release notes + prior audit claim it wraps "Claude Pro / ChatGPT / SuperGrok" — **false in code.**
  Spec line 130 flags this loudly and correctly. Confirmed: dead for our purposes; the only reusable
  artifact is the `UpstreamAdapter` ABC (`proxy/adapters/base.py:38-91`).
- One nuance: `NousPortalAdapter._save_state` also calls `_write_shared_nous_state`
  (`nous_portal.py:130`), which writes refresh tokens to a **machine-global shared store** outside the
  profile (`auth.py:3520-3579`). Not proxy-specific — see §C.

---

## C. Multi-tenant-hostile behaviors (the part the spec omits entirely)

This is the highest-value gap. The spec's §3 trust model is about the *backend↔container hop* (single
bearer, no device attestation, no scopes — all correct). It says nothing about **where Hermes sources
credentials from on the host filesystem**, which is the actual hosted-multi-tenant risk:

1. **Ambient host-file credential seeding.** `agent/credential_pool._seed_from_singletons`
   (`:1317-1421`) reads, at pool-load time: `~/.claude/.credentials.json` (Claude Code),
   `~/.hermes/.anthropic_oauth.json` (Hermes PKCE), `~/.qwen/oauth_creds.json`, `~/.codex/auth.json`,
   `gh auth token` (Copilot), and `os.environ`. Enumerated in `agent/credential_sources.py:4-13`.
   In a single-user CLI this is the headline feature; in a shared/host-mounted container it's a
   credential-bleed surface. There IS a consent gate for anthropic
   (`is_provider_explicitly_configured`, `credential_pool.py:1336-1338`) — but env/gh_cli/qwen/codex
   branches are not all gated the same way.

2. **Write-BACK to host files.** The pool doesn't just read `~/.claude/.credentials.json` — on refresh
   it **writes the rotated token back** to it (`_write_claude_code_credentials`,
   `credential_pool.py:767-773`, `848-874`). So a Hermes container that ever sees a host `~/.claude`
   will mutate it. The spec's clean "no write-back; DDB=seed" claim (line 308) is true **only** for the
   `providers.openai-codex` path, not the pool path.

3. **Machine-global shared Nous store.** `_write_shared_nous_state` persists long-lived OAuth tokens
   to a profile-independent path (`auth.py:3520-3579`) explicitly "to be cross-profile useful"
   (`:3527-3528`). Same cross-tenant-bleed shape as the global `auth.json` fallback (§A1).

4. **Single `API_SERVER_KEY` == full access.** `_check_auth` is the *only* gate; if the key matches
   (or is unset) the caller gets everything (`api_server.py:742-762`, `750-751`). No per-call scope, no
   per-agent restriction, capabilities self-describe auth as just `{"type":"bearer","required":bool}`
   (`api_server.py:979-982`). Exec-approval (`POST /v1/runs/{id}/approval`, `api_server.py:3223-3299`)
   is gated by the **same** bearer — so the "only the paired operator device may approve" policy that
   OpenClaw's Ed25519 device model gives us is simply **gone**; if we want it, the Isol8 backend must
   enforce it. Spec §3 line 189 acknowledges this; good.

Bind guard is genuinely solid: network-accessible bind refuses to start without a key
(`api_server.py:3433-3440`) and rejects placeholder keys < 8 chars via `has_usable_secret`
(`:3444-3454`). So a non-loopback Hermes container is guaranteed to carry a real key — spec line 177
is correct.

---

## D. Bottom line for the founder's scope worry
- The spec's **auth-hop and provider-gate claims hold** — runtime really is orthogonal to the
  Bedrock/OAuth/BYOK provider axis, and the codex refresh model is identical to what we run today.
- What the ~10-15 eng-week estimate **does not price in**: (a) the credential surface is *much* wider
  than 3 providers (a full `PROVIDER_REGISTRY` + a second `credential_pool` mechanism + per-provider
  token sub-schemas that `AUTH_STORE_VERSION` does not version); (b) Hermes' default behavior is to
  forage host-ambient credentials and write back to host files — the port must **actively disable**
  these paths (env scoping, no `~/.claude`/`~/.codex`/`~/.qwen` in the image, no global `auth.json`,
  suppress shared-Nous-store) or risk cross-tenant leakage; (c) every Hermes bump is a multi-shape
  auth-schema audit, not a single-version check. None of these are blockers, but they are unbudgeted
  hardening work the "translate one DDB row" framing hides.
