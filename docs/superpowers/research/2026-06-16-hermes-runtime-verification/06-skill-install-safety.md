# Hermes runtime verification — #06: Skill install + safety in-container

**Date:** 2026-06-16
**Open question:** Audit #7 — how do Hermes skills get installed into a running
container, do they need outbound network, is there safety scanning/sandboxing,
and what would Isol8 need to add vs. its existing marketplace safety model?
**Source tree:** `hermes-agent/` (v0.14.0) + `apps/backend/`. Every claim below
is a file:line I read this pass. Where the prior audit drifted, I call it out.

---

## CORRECTION to the prior audit's citation

The 2026-05-17 audit's open-question #7 cited **`hermes_bootstrap.py ~288`** and
`~/.hermes/skills/` as the skill-install location. **That citation is wrong.**

- `hermes-agent/hermes_bootstrap.py` is a **130-line Windows UTF-8 stdio shim**
  (`hermes_bootstrap.py:1-129`, `apply_windows_utf8_bootstrap`). It has zero
  lines about skills and is only 130 lines long — there is no line 288. It is
  imported at the top of entry points purely to fix `cp1252` on Windows.
- The real skill-install engine is **`tools/skills_hub.py`** (2830 lines), with a
  thin human CLI wrapper at **`hermes_cli/skills_hub.py`** and an
  **agent-runtime** tool at **`tools/skill_manager_tool.py`**.
- The `~/.hermes/skills/` directory *is* correct: `tools/skills_hub.py:48-49`
  defines `HERMES_HOME = get_hermes_home()` (default `~/.hermes`, override via
  `HERMES_HOME` env — `hermes_constants.py:14-30`) and
  `SKILLS_DIR = HERMES_HOME / "skills"`.

---

## 1. How a skill gets installed into a running container

There are **three** install paths, and they have **different security postures**.

### 1a. Human CLI / TUI install (`hermes skills install <id>`)

Router `hermes_cli/skills_hub.py:1317` (`skills_command`) → `do_install`
(`hermes_cli/skills_hub.py:408`). Full pipeline (verified `do_install`,
`hermes_cli/skills_hub.py:408-611`):

1. **Resolve + fetch** the bundle from a registry adapter (`SkillSource.fetch`,
   `tools/skills_hub.py:303`). Adapters: `official` (bundled), `github`,
   `clawhub`, `claude-marketplace`, `lobehub`, raw-URL
   (`tools/skills_hub.py:73`, trust-level field at `:75`).
2. **Quarantine** — write bundle into `~/.hermes/skills/.hub/quarantine/<name>/`
   (`quarantine_bundle`, `tools/skills_hub.py:2726-2748`). Path-traversal-safe:
   every bundle-controlled path passes `_normalize_bundle_path` which rejects
   absolute paths, `..`, and drive letters (`tools/skills_hub.py:93-115`).
3. **Scan** — `scan_skill(q_path, source=...)` (`tools/skills_guard.py:599`),
   called at `hermes_cli/skills_hub.py:548`.
4. **Policy gate** — `should_allow_install(result, force)` decides
   allow/block/ask (`tools/skills_guard.py:646-680`), checked at
   `hermes_cli/skills_hub.py:552`. Block → `shutil.rmtree(q_path)` + audit-log
   `BLOCKED`, install aborts (`:553-561`).
5. **Human confirm** — interactive `y/N` prompt, with a different warning panel
   for official vs third-party skills (`:570-599`). `--force` or `skip_confirm`
   (TUI) bypasses the prompt.
6. **Promote** — `install_from_quarantine` moves quarantine → `SKILLS_DIR`,
   records provenance in `lock.json` (source, identifier, trust_level,
   `scan_verdict`, content hash, file list — `tools/skills_hub.py:2751-2813`),
   and appends an `INSTALL` line to `audit.log` (`append_audit_log`,
   `:2693-2706`).

### 1b. Agent-runtime install (the LLM installs/writes a skill mid-run)

This is the path that matters for "into a *running* container." The agent calls
the skill-manager tool (`tools/skill_manager_tool.py`). After writing the skill
dir it calls `_security_scan_skill` (`tools/skill_manager_tool.py:410,450,546,651`).
**But that scan is a no-op by default** — see §2b.

### 1c. Bundled / "official optional" skills — fully offline

`hermes-agent/skills/` ships **25** category dirs in-image (`apple`, `devops`,
`github`, `red-teaming`, `mcp`, … — `ls skills/`). The `official` adapter's
`trust_level_for` returns `builtin`/`trusted` and installs are copies from the
image, requiring **no network**. `optional-skills/` is a second in-image tree of
opt-in skills. Installing these is a local file copy through the same
quarantine→scan→promote pipeline.

### Network requirement

- **NOT git/`git clone`.** Despite a `git` mental model, Hermes fetches skills
  over **HTTP via `httpx`** — GitHub *Contents API* (`api.github.com/repos/.../contents/...`,
  `tools/skills_hub.py:469,520,622,682`), raw-URL fetch (`_fetch_text`,
  `:963`), and registry index JSON (`_guarded_http_get`, `:126`). `subprocess`
  is used only for `gh auth token` to mint a GitHub PAT (`:235`), never to clone.
- **Bundled / optional-skills installs need no network** (local copy, §1c).
- **Remote installs (github/clawhub/lobehub/url) require outbound HTTPS.** In our
  per-user ECS container that means egress to `api.github.com` / arbitrary hosts
  — subject to the SG/NAT egress posture. Hermes does gate egress with
  `is_safe_url` (SSRF block) + `check_website_access` rule engine on every fetch
  and every redirect hop (`_guarded_http_get`, `tools/skills_hub.py:126-160`;
  `from tools.url_safety import is_safe_url`, `:38`).

---

## 2. Safety scanning / sandboxing

### 2a. There IS a real built-in scanner — `tools/skills_guard.py` (932 lines)

Module docstring: *"Every skill downloaded from a registry passes through this
scanner before installation. Regex-based static analysis to detect known-bad
patterns…"* (`tools/skills_guard.py:1-23`). It is materially more than Isol8's:

- **THREAT_PATTERNS** (`:83`+): exfiltration (curl/wget/fetch/requests
  interpolating `$SECRET` envs — `:87-102`), reading credential stores
  (`~/.aws`, `.env`, `.netrc`, `.pgpass`, `.npmrc`, Hermes secrets file —
  `:104-128`), `os.getenv`/Ruby `ENV[]` secret reads (`:138-145`),
  **prompt-injection** (HTML-comment override directives `:194`, jailbreak
  patterns `:458`), hardcoded secrets / embedded private keys / leaked
  GitHub/OpenAI/Anthropic/AWS keys (`:438-455`), persistence (writes to agent
  config files `:430`), and **invisible-unicode** detection (`scan_file`,
  `:586`).
- **Structural checks** — file count, total size, binary files, symlinks
  (`_check_structure`, called in `scan_skill` `:622`).
- **Verdict** — `safe | caution | dangerous` (`_determine_verdict`, `:632`).
- **Trust-aware policy matrix** (`INSTALL_POLICY`, `:41-51`):
  | trust | safe | caution | dangerous |
  |---|---|---|---|
  | builtin | allow | allow | allow (never scanned, `:12`) |
  | trusted (`openai/skills`, `anthropics/skills`, `huggingface/skills` — `:39`) | allow | allow | **block** |
  | community (everything else) | allow | **block** | **block** |
  | agent-created | allow | allow | ask |
  `--force` overrides any block (`should_allow_install(force=True)`, `:664-668`).

### 2b. THE GAP: agent-runtime skills are NOT scanned by default

This is the highest-value finding. `tools/skill_manager_tool.py:78-102`
(`_security_scan_skill`) is a **no-op unless `skills.guard_agent_created` is
enabled, which defaults to `False`** (`_guard_agent_created_enabled`,
`:59-75`). The code comment states the rationale explicitly
(`:62-65`):

> "Off by default because the agent can already execute the same code paths via
> `terminal()` with no gate, so the scan adds friction without meaningful
> security."

So in the running container, **an agent installing/authoring a skill bypasses the
scanner entirely by default.** And even when the human CLI path *does* scan, the
agent (or a curl-piped install) can pass `--force` to override a `block`
(`tools/skills_guard.py:664`). The scanner is opt-in / overridable, not a hard
gate.

### 2c. No sandboxing of the skill itself

A "skill" is `SKILL.md` + arbitrary scripts/files copied into `SKILLS_DIR`
(`SkillBundle.files`, `tools/skills_hub.py:82-90`; promote = `shutil.move`,
`:2791`). When the agent uses a skill it runs those scripts via the same
`terminal()`/exec tooling — there is **no per-skill sandbox, seccomp, or
network jail**. The only isolation boundary is the container itself (the same as
OpenClaw). The scanner is *static pre-install analysis*, not runtime
confinement.

### 2d. MCP config — trusted-by-default, no scan

`hermes_cli/mcp_config.py` manages MCP servers as `{command, args, env}` (stdio)
or `{url}` (sse/http) entries with env-var key management
(`_env_key_for_server` `:107`, `_parse_env_assignments` `:112`,
`cmd_mcp_add` `:226`). There is **no safety scan, allowlist, or sandbox** on MCP
servers — adding one runs an arbitrary command or connects to an arbitrary URL.
This is the same posture as OpenClaw (MCP global-only, trusted-by-default — see
memory `project_openclaw_mcp_global_only_nested_inert`).

---

## 3. Comparison to Isol8's marketplace safety model

Isol8's scanner is **`apps/backend/core/services/marketplace_safety.py`** (531
lines), invoked from **`agent_export.py`** at publish time (note: CLAUDE.md says
`scan_tarball`, but the real function is `_scan_tarball_for_safety_flags` +
`marketplace_safety.scan` — `agent_export.py:349-389,470,587`; minor doc drift).

| Dimension | Hermes `skills_guard` | Isol8 `marketplace_safety` |
|---|---|---|
| **Where it runs** | **In-container, at install time** (defender = the consumer's own runtime) | **Server-side, at publish time** (defender = the platform, before S3 upload) |
| **What it checks** | exfil, cred-store reads, prompt-injection, hardcoded secrets, invisible unicode, structural (`tools/skills_guard.py:83-596`) | exfil (`curl\|bash`, `eval`, `subprocess`), hardcoded secrets, AWS keys, env writes, suspicious fetch hosts, + **PII** (email/phone) for persona files (`marketplace_safety.py:44-145`) |
| **Format gating** | trust-level by source repo | `format=` "skillmd" / "openclaw" / "persona" (`marketplace_safety.scan(...)`, `agent_export.py:383-387`) |
| **Enforcement** | trust-matrix block, but **opt-in for agents + `--force` override** (`tools/skills_guard.py:41-51,664`; `skill_manager_tool.py:85`) | **flags are advisory** — admin makes final call (`marketplace_safety.py:4-9`) **EXCEPT** credential leaks in the published slice **hard-block** publish: `has_blocking_credential_findings` → `raise CredentialLeakError` (`agent_export.py:462-466`) |
| **Provenance** | `lock.json` (source/hash/verdict) + `audit.log` (`skills_hub.py:2793-2811`) | manifest `safety_flags` list on the S3 artifact (`agent_export.py:470-480`) |

**The key model difference:** Isol8 scans **once, centrally, before an artifact
is ever published** — a buyer deploying it trusts the platform already vetted +
scrubbed it. Hermes scans **at the moment of install inside each container**, and
that scan is **bypassable by the very actor (the agent) we'd most want to gate.**

### What Isol8 would need to ADD for a Hermes runtime

1. **A publish-time scanner for Hermes skill bundles.** Isol8's
   `marketplace_safety.scan` is regex-on-bytes and format-agnostic at its core;
   point a new `format="hermes-skill"` at `SKILL.md` + scripts. We could
   *reuse Hermes's own `tools/skills_guard.THREAT_PATTERNS`* server-side rather
   than reimplement — it's a strict superset of our patterns (adds prompt-
   injection + invisible-unicode + cred-store-read patterns we lack). Port them
   into `marketplace_safety._PATTERNS`.
2. **Disable/neutralize Hermes's bypassable in-container path for marketplace
   deploys.** Marketplace deploys must land as pre-scanned artifacts, not via the
   agent calling `hermes skills install --force`. Either (a) deploy skills by
   un-tarring our scrubbed artifact directly into `SKILLS_DIR` (mirroring
   `catalog_service.deploy_from_artifact`), skipping Hermes's installer; or
   (b) if we use Hermes's installer, force `skills.guard_agent_created=true` in
   container config AND strip `--force` from any agent-reachable surface.
3. **Egress policy decision for remote skill fetch.** If we *allow* agents to
   `hermes skills install github/...` at runtime, the per-user ECS container
   needs controlled egress; Hermes's `is_safe_url` + `check_website_access`
   (`tools/skills_hub.py:131-142`) gives an SSRF/allowlist hook we can configure,
   but we'd still want NAT/SG-level egress limits. If we *disallow* it, ship only
   bundled + marketplace-deployed skills (offline) and don't wire registry
   adapters.
4. **A slice-scrub equivalent for MCP/skill secrets.** Isol8 hard-blocks
   credential leaks in the published `openclaw-slice.json`
   (`assert_slice_scrubbed`, `agent_export.py:462`). Hermes skills can carry
   `env`/scripts with secrets; the export pipeline needs the same scrub-then-
   re-inspect-then-block gate for any Hermes-equivalent secret-bearing fields
   (MCP `env`, skill scripts).
5. **MCP parity is a wash — no new gate needed beyond what we already lack.**
   Hermes MCP config is trusted-by-default exactly like OpenClaw; our existing
   curated-integration posture (memory
   `project_integration_mcp_and_reprovision`) carries over unchanged.
