# Map 11 — Drift + Versioning Pressure Test (Hermes runtime option)

Basis under test: `/Users/prasiddhaparthsarthy/Desktop/hermes-agent` @ `4c46c35ed` (branch `basis-v0.14.0-spec`).
`upstream/main` @ `47131` (fetched), **3171 commits ahead** of HEAD.
All claims below were read directly from this tree / git at the cited refs.

---

## 1. The version scheme — "v0.14.0" reconciled

Hermes runs a **dual version scheme** and the spec quotes the *marketing* one:

- `pyproject.toml:7` → `version = "0.14.0"`. This is the human-facing semver, bumped per release-note doc.
- `git describe --tags` → `v2026.5.16-201-g4c46c35ed`. The git tags are **CalVer** (`v2026.5.16`, `v2026.5.28`, …). HEAD is **201 commits past tag `v2026.5.16`** — i.e. HEAD is NOT the v0.14.0 release commit, it's 201 commits *after* it.
- `RELEASE_v0.14.0.md:1` confirms the mapping explicitly: `# Hermes Agent v0.14.0 (v2026.5.16)` — "Release Date: May 16, 2026", "Since v0.13.0: 808 commits". So `v0.14.0` ≡ CalVer tag `v2026.5.16`.

**Verdict on §1:** "v0.14.0" is a real, locatable release (= tag `v2026.5.16`), but **the spec's basis commit `4c46c35e` is +201 commits ahead of that tagged release** (HEAD commit date 2026-05-17, one day after the v0.14.0 release date). So even the *basis itself* is not clean v0.14.0; line numbers cited "for v0.14.0" are really for a post-release snapshot. Anyone re-pinning to the actual `v2026.5.16` tag will see different line offsets than the spec.

---

## 2–5. Drift on the load-bearing files (HEAD → upstream/main)

All four spec-cited files still exist at upstream (no renames):
`gateway/platforms/api_server.py`, `agent/agent_init.py`, `hermes_cli/auth.py`, `hermes_cli/config.py` — all `cat-file -e` OK at both refs.

Commit counts on each since basis: api_server **15**, agent_init **28**, auth **64**, config **84**.

### MATERIAL — auth model changed (breaks a likely load-bearing spec assumption)
`api_server.py` grew **+849 / −51 lines** (diffstat). The decisive commit is `1a9ef8314 fix(security): require API_SERVER_KEY before dispatching API server work` (May 25, 2026 — *after* basis).
- **Basis (`api_server.py:3434`, what the spec reads):** `if is_network_accessible(self._host) and not self._api_key: return False` — the server **starts on loopback with NO key and accepts ALL requests** (`_check_auth:750-751` returns `None` "allow all"; warning logged at 3475-3479). A spec that provisions Hermes behind the Isol8 in-VPC ALB/loopback and relies on "no key needed for local-only" is reading this exact behavior.
- **Upstream:** the guard is now `if not self._api_key: return False` for **every** bind, loopback included — the API server **refuses to boot without `API_SERVER_KEY`**, and the "no key = accept all" warning branch is deleted. `_check_auth`'s no-key path is demoted to "tests / unsupported manual wiring only."
- **Impact:** if the spec assumes Isol8 can talk to a keyless loopback Hermes (mirroring how Isol8 today reaches the OpenClaw gateway), that assumption is **invalid against latest upstream** — Isol8 would have to generate, store (Secrets Manager), and inject `API_SERVER_KEY` per container and send `Authorization: Bearer`. This is net-new provisioning plumbing the spec's eng-week estimate may not include.

### ADDITIVE — run schema / SSE framing (does NOT break the spec)
- Run-create handler `_handle_runs` (basis `api_server.py:2862`) is structurally intact upstream (same auth → session-key → concurrency-gate flow).
- SSE framing unchanged in shape: `text/event-stream`, `data: …\n\n`, `data: [DONE]`, custom `event: hermes.tool.progress` (basis 1357-1459). Upstream only **adds** fields: `run.completed` now carries an authoritative `messages` per-turn transcript (`1cb850b67`, refs #34703) — explicitly documented as "purely additive: clients that ignore the field are unaffected." A basis-targeting SSE consumer keeps working.
- Other api_server commits are health-version reporting (`30c791361`), cron-prompt scanning, response-cache hardening — non-breaking.

### MEDIUM — config schema migrated 23 → 29
`hermes_cli/config.py` grew +84 commits. `_config_version` is **23** in basis (`config.py:1692`) vs **29** upstream (`config.py:2572`) — **6 schema migrations**. A forward migration runner exists (`check_config_version`, `current/latest` compare), so a basis-shaped `config.yaml` is auto-upgraded on boot rather than rejected. **But:** if the spec hardcodes/generates a pinned Hermes `config.yaml` against the v23 shape (the Isol8 pattern — Isol8 hand-writes `openclaw.json`), it's writing to a moving target; e.g. `887295ba5` migrates custom-provider model maps v11→v12. Lower risk than OpenClaw's strict-zod brick (Hermes migrates rather than crash-loops), but the "write a fixed config" assumption is fragile.

### LOW — agent model + auth-provider plumbing (no shape break)
- `agent/agent_init.py` +300/−49, but the 28 commits are feature/fix accretion (memory-update notices, compression-trigger tuning, custom-provider headers, context-engine host contract) — **no rewrite of the agent run model signature**. Spec claims about the agent-init surface survive in spirit.
- `hermes_cli/auth.py` +64 commits are almost entirely OAuth-credential plumbing (xAI/Codex/OpenRouter/Qwen/Gemini pool entries, token self-heal). Relevant to any "reuse Hermes OAuth like OpenClaw OAuth" claim — the pool/profile model is actively churning — but no removal of the auth surface the spec depends on.

---

## Verdict — would pinning to LATEST upstream invalidate a load-bearing spec claim?

**YES — one hard invalidation, plus two soft risks.**

- **HARD:** upstream `1a9ef8314` makes `API_SERVER_KEY` **mandatory to even start the API server** (loopback included). Any spec claim that Isol8 can run/reach a keyless Hermes the way it reaches the keyless OpenClaw loopback gateway is **false against latest upstream** and adds per-container key generation + Secrets-Manager + Bearer-injection work the estimate likely omits.
- **SOFT 1:** config schema moved 23→29 (6 migrations); a hand-pinned `config.yaml` is auto-migrated (won't brick) but is shape-unstable.
- **SOFT 2:** auth-provider/OAuth pooling churned 64 commits; OAuth-reuse claims rest on a moving surface.
- **NOT invalidated:** run create flow + SSE framing (additive only), agent model signature (accretion only), and all four files still exist with no renames.

Bonus correction the spec gets wrong on its face: the basis it pins (`4c46c35e`) is **+201 commits past the actual v0.14.0 / `v2026.5.16` tag** — so "line numbers match v0.14.0" is already off by 201 commits of churn even before considering upstream.
