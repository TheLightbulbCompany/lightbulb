# Attack 03 — Auth & Trust + Credential Foraging (Hermes runtime option)

Basis re-verified independently against `/Users/prasiddhaparthsarthy/Desktop/hermes-agent` @ commit `4c46c35e`
(`docs(messaging): clarify admin/user split … #27623`). Isol8 backend in the worktree.
Every line cited below was opened and read in this pass — not echoed from the maps.

Verdict shorthand: **CONFIRM** = map/spec claim holds; **CRACK** = map/spec claim is wrong;
**NUANCE** = claim holds but is materially incomplete or mis-cited.

---

## (a) §3 single-bearer `API_SERVER_KEY`, no operator/device model — **CONFIRM (hard)**

- `_check_auth` is the *entire* gate (`gateway/platforms/api_server.py:742-762`). It does exactly one
  thing: `hmac.compare_digest(token, self._api_key)` against the single process-wide key
  (`:756`). The key source is `extra["key"]` falling back to `os.getenv("API_SERVER_KEY","")`
  (`:647`). There is **no** caller identity, no per-method scope, no device key, no signature check.
- **Open-by-default backdoor**: if `self._api_key` is falsy, `_check_auth` returns `None` (allow-all)
  at `:750-751` — "No key configured — allow all (local-only use)". For a *loopback* Hermes this is
  fine; for the Isol8 port it means a misconfigured/empty `API_SERVER_KEY` env on the task def yields
  a wide-open container. The bind guard (below) only saves you when the bind is non-loopback.
- Bind guard is genuinely solid and *is* the only thing preventing a keyless network-exposed
  container: `is_network_accessible(self._host) and not self._api_key → return False`
  (`:3433-3440`), and a placeholder key < 8 chars is rejected via `has_usable_secret(min_length=8)`
  (`:3442-3457`, ported from openclaw#64586). So a non-loopback Hermes is guaranteed to carry a real
  key — spec line 177 correct.
- **Contrast with what Isol8 gives up**: Isol8's OpenClaw path has a real device-attestation layer —
  `apps/backend/core/crypto/operator_device.py` mints an Ed25519 keypair, `device_id = SHA-256(pubkey)`,
  signs challenges (`:11-142`). Hermes has **zero** analog. Map04 §C4 and Map09 §3-4 are correct;
  re-verified at the cited lines.

## (b) "one file / no write-back" — TRUE only for `providers.openai-codex`; the pool FORAGES + WRITES BACK — **CONFIRM the foraging/write-back; CRACK one mis-cited sub-claim**

- **Codex refresh path is genuinely clean.** `_save_codex_tokens` writes ONLY `~/.hermes/auth.json`
  (`hermes_cli/auth.py:2688-2699`); `refresh_codex_oauth_pure` is side-effect-free by contract
  (`:2702-2708` "without mutating Hermes auth state"); POSTs to the OpenAI token endpoint with the
  public Codex client_id `app_EMoamEEZ73f0CkXaXp7hrann` (`:90`, `:2720-2727`); `refresh_token_reused`
  → relogin (`:2756-2763`). `_import_codex_cli_tokens` reads `~/.codex/auth.json` but
  **"Does NOT write to the shared file"** (`:2827-2858`, docstring `:2831`) and is interactive-only
  (called from the `hermes auth` flow ~`:5195-5197`). So codex = single-source, no egress, no
  write-back, no runtime forage. Spec line 26/308 correct **for codex**.
- **CRACK — Map04 mis-read the codex pool branch.** Map04 §A2 lines 44-47 claim "the *credential-pool*
  seeder reads `~/.codex/auth.json` at **pool-load time** (`credential_pool.py:1523` comment + branch)".
  The actual branch at `agent/credential_pool.py:1512-1543` does the **opposite**: it seeds the codex
  pool entry only from the Hermes auth store (`_load_provider_state(... "openai-codex")`, `:1520`),
  and the comment at `:1522-1527` reads verbatim "Hermes owns its own Codex auth state — we do NOT
  auto-import from `~/.codex/auth.json` at pool-load time." So codex does NOT forage at runtime via
  the pool either. (The stale `credential_sources.py:294-308` docstring asserting
  "refresh_codex_oauth_pure() writes both every time" is **also wrong** — that function writes nothing;
  the pool refresh path `:780-790` only mutates the in-memory entry.) Net: codex is *cleaner* than
  Map04 claims, which actually strengthens the "use codex as the model" argument.
- **CONFIRM — non-codex providers forage host files.** `agent/credential_sources.py:4-13` enumerates
  the sources: `env:<VAR>` (os.environ / `~/.hermes/.env`), `claude_code` (`~/.claude/.credentials.json`),
  `hermes_pkce` (`~/.hermes/.anthropic_oauth.json`), `qwen-cli` (`~/.qwen/oauth_creds.json`),
  `gh_cli` (`gh auth token`), config/model. The live readers are in
  `agent/credential_pool.py::_seed_from_singletons` (`:1317-1574`): anthropic reads Claude Code +
  Hermes PKCE (`:1342-1364`), copilot resolves `gh auth token` (`:1412-1430`), qwen/xai/nous/minimax
  each read their own store. **Only anthropic is consent-gated** (`is_provider_explicitly_configured`,
  `:1335-1340`); copilot/qwen/gh have no equivalent explicit-config gate (they self-gate only on the
  per-source suppression set). In a single-user CLI this is the headline feature; in a hosted
  per-tenant container it is a credential-bleed surface that the image MUST neutralize by simply not
  shipping `~/.claude`, `~/.codex`, `~/.qwen`, `gh`, and by scoping env.
- **CONFIRM — write-BACK to host files is real.** On anthropic `claude_code` refresh the pool writes
  the rotated token back to `~/.claude/.credentials.json` via `_write_claude_code_credentials`
  (`agent/credential_pool.py:767-779`, again on the retry path `:872-880`). The writer targets
  `Path.home()/".claude"/".credentials.json"` and merges into `claudeAiOauth`
  (`agent/anthropic_adapter.py:871-905`). So any Hermes container that ever sees a host `~/.claude`
  will *mutate* it. The spec's blanket "no write-back; DDB = seed" is FALSE for the pool path —
  true only for `providers.openai-codex`. Map04 §C2 correct.
- **CONFIRM — global-root `auth.json` read-through fallback.** `read_credential_pool` merges a
  global-root `auth.json` per-provider whenever the profile has zero entries
  (`hermes_cli/auth.py:1099-1143`), backed by `_load_global_auth_store` (`:832-865`, which itself has
  only a *pytest* seat-belt, not a runtime tenancy guard). In a shared HOME / baked base layer this is
  a cross-tenant bleed primitive. The image must guarantee no global `~/.hermes/auth.json` exists and
  HOME is per-task. Map04 §A1/§C3 correct.

## (c) "desktop / exec-approval can't carry over" — **CONFIRM**

- Exec-approval (`POST /v1/runs/{run_id}/approval`) is gated by the **same** `_check_auth` single
  bearer (`gateway/platforms/api_server.py:3223-3227`), then resolves against an in-process
  `_run_approval_sessions[run_id]` map (`:3255-3263`) via `resolve_gateway_approval` (`:3270-3272`).
  Whoever cleared the one token can answer any run's approval with `once|session|always|deny`
  (`:3242-3253`). There is **no** "only the paired operator device may approve" primitive — Hermes
  cannot distinguish callers at all. Map09 §4 correct; spec line 189 correct.
- The WS/TUI surfaces don't help: `tui_gateway`/dashboard approvals ride the same flat, unscoped
  dispatch behind a per-boot ephemeral token (Map09 §2-3) — not provisionable, not device-bound.
- **Consequence for the port**: Isol8's trusted-device exec-approval (the Tauri desktop path +
  `operator_device.py` Ed25519 model) has **no Hermes equivalent**. If Isol8 wants device-gated
  approval on a Hermes container, the **backend** must own the policy (decide which approvals to
  forward / which device may answer) — Hermes provides no building block. This is unbudgeted work the
  spec's "runtime is orthogonal" framing hides.

---

## Six bullets (return value)

1. **(a) CONFIRM, hard.** Single-bearer `API_SERVER_KEY` is the only gate — `_check_auth` does one
   `hmac.compare_digest` (`api_server.py:742-762`), no scopes/device/identity; and it **allows-all when
   the key is empty** (`:750-751`). Bind guard (`:3433-3457`) only protects non-loopback binds, so an
   empty/placeholder key on the task def is a real open-container footgun for the port.
2. **(b) CONFIRM foraging + write-back.** Non-codex providers forage host files at pool-load
   (`credential_sources.py:4-13`; `credential_pool.py:1317-1574`) and **write rotated anthropic tokens
   back to `~/.claude/.credentials.json`** (`credential_pool.py:767-779,872-880` →
   `anthropic_adapter.py:871-905`), plus a global-root `auth.json` read-through fallback
   (`auth.py:1099-1143,832-865`). These are cross-tenant-bleed primitives the image must disable
   (no `~/.claude`/`~/.codex`/`~/.qwen`/`gh`, no global `auth.json`, per-task HOME, scoped env).
3. **(b) CRACK — codex is *cleaner* than Map04 says.** Map04 §A2:44-47 claims the credential pool
   reads `~/.codex/auth.json` at pool-load; the branch at `credential_pool.py:1512-1543` (comment
   `:1522-1527`) explicitly does **NOT** auto-import codex at runtime. Codex genuinely is
   single-source / no-forage / no-write-back in *both* the providers path (`auth.py:2688-2699,2702-2708`)
   and the pool path. The `credential_sources.py:294-308` "writes both every time" docstring is stale.
4. **(b) NUANCE — "no write-back" is provider-specific, not global.** It is TRUE for
   `providers.openai-codex` only; FALSE for the anthropic pool path. The spec's blanket "DDB = seed,
   no write-back" must be scoped to codex, or the port must hard-disable the anthropic foraging/write
   branches before relying on it.
5. **(c) CONFIRM.** Exec-approval (`api_server.py:3223-3227`) is gated by the *same* single bearer and
   resolves against an in-process run-map — no device binding, no caller distinction. Isol8's Ed25519
   operator-device model (`core/crypto/operator_device.py:11-142`) has no Hermes analog; trusted-device
   approval would have to be re-implemented entirely in the Isol8 backend.
6. **Scope verdict.** The spec's auth-*hop* topology holds, but the credential *surface* is the
   unbudgeted risk: a full `PROVIDER_REGISTRY` + a second `credential_pool` mechanism + ambient
   host-file forage/write-back + global-fallback bleed, none of which "translate one DDB row" prices in.
   Recommend the port standardize on the codex/bedrock/BYOK-via-scoped-`.env` paths and **affirmatively
   strip** every foraging source rather than assume the container won't have those host files.
