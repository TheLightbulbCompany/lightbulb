# Vercel CLI Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a one-step, GitHub-parity Vercel **CLI** integration: paste a Vercel Account Token → a bundled `vercel` skill is enabled and the token is injected as `VERCEL_TOKEN`, so agents run `npx vercel …` (read + write) with no image/boot/RPC change.

**Architecture:** A new `vercel_cli` paste-token provider on the existing credential rail. A new `skill_cli` materializer writes the token to `skills.entries.<skill>.apiKey` + `enabled:true` in the owner's `openclaw.json` (hot-reloaded, no restart) and seeds the skill's `SKILL.md` onto EFS. OpenClaw's native `primaryEnv` injection puts the token in the gateway-host exec env; the skill instructs `npx --yes vercel@<exact-ver>` (extract→/tmp, storm-safe). The token is scrubbed from marketplace artifacts because `skills.entries[*]` is already scrubbed; we additionally harden `catalog_slice` to scrub the `tools` block.

**Tech Stack:** Python 3.12 / FastAPI backend, `pytest`; Pydantic registry; YAML providers catalog; Next.js/React frontend (`vitest`).

## Global Constraints

- Exec runs on the **gateway** host — verified because we set no `agents.defaults.sandbox` and `tools.exec.host` is unset (`auto`→`gateway`). The token injection depends on this; a regression guard enforces it.
- The token is a Vercel **Account Token** (dashboard-minted), **finite-lived** — `refresh_owner: none`. Validate by API call (`GET https://api.vercel.com/v2/user`), **never** by token prefix (`vca_`/`vcp_` overlap).
- The token must live **only** in `integration_secret_store` (encrypted) and `skills.entries.vercel.apiKey` (scrubbed from marketplace). **Never** in any `tools.*` field.
- Pin an **exact** npx version (`vercel@<x.y.z>`), never `@latest` — exact is the only form that reuses a warm `_npx/<hash>` with no per-call registry round-trip.
- Materializer file writes use `atomic_write` (0600, uid/gid 1000) from `core.services.credentials.materializers.base`.
- No image change, no boot-script change, no new gateway RPC, no `tools.exec.host` pin (would break the desktop `node` override).
- **Pre-warm at connect is NOT in v1** — the backend cannot exec inside the container. The first `vercel` invocation is a one-time ~68s cold fetch; cached on EFS thereafter. (Documented tradeoff; revisit if/when an in-container trigger exists.)

---

## File Structure

- **Create** `apps/backend/core/services/credentials/skills/vercel/SKILL.md` — the bundled skill (knowledge + `npx vercel` recipes + `primaryEnv` frontmatter).
- **Create** `apps/backend/core/services/credentials/materializers/skill_cli.py` — `write`/`remove`: seed `skills.entries.<skill>` + SKILL.md; remove on disconnect.
- **Modify** `apps/backend/core/services/credentials/registry.py` — add an optional `SkillSpec` block (`key`, `primaryEnv`, `package`) on `Provider`.
- **Modify** `apps/backend/core/services/credentials/providers.yaml` — add the `vercel_cli` provider.
- **Modify** `apps/backend/core/services/credentials/materializers/__init__.py` — register `skill_cli`.
- **Modify** `apps/backend/core/services/catalog_slice.py` — scrub the top-level `tools` block (hardening).
- **Modify** `apps/frontend/src/components/settings/integration-receipts.ts` — add the `vercel_cli` receipt.
- **Test** `apps/backend/tests/credentials/test_registry.py`, `tests/credentials/test_skill_cli.py`, `tests/unit/services/test_catalog_slice_scrub.py`, `tests/unit/containers/test_exec_host_guard.py`.

---

### Task 1: Registry — add a `SkillSpec` block to `Provider`

**Files:**
- Modify: `apps/backend/core/services/credentials/registry.py`
- Test: `apps/backend/tests/credentials/test_registry.py`

**Interfaces:**
- Produces: `registry.SkillSpec` (Pydantic) with `key: str`, `primary_env: str` (alias `primaryEnv`), `package: str`; and `Provider.skill: SkillSpec | None` (default `None`).

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/credentials/test_registry.py  (append)
def test_vercel_cli_is_skill_paste_provider():
    p = registry.get_provider("vercel_cli")
    assert p.acquisition == "paste_token"
    assert p.actor == "skill"
    assert p.refresh_owner == "none"
    assert p.wiring.materializer == "skill_cli"
    assert p.wiring.mcp_server is None and p.wiring.openclaw_paths is None
    # Skill block carries what the materializer needs.
    assert p.skill is not None
    assert p.skill.key == "vercel"
    assert p.skill.primary_env == "VERCEL_TOKEN"
    assert p.skill.package.startswith("vercel@")  # exact-pinned
    # Validate by API call, never by token prefix.
    assert p.validation is not None
    assert p.validation.url == "https://api.vercel.com/v2/user"
    assert p.validation.token_prefix is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/credentials/test_registry.py::test_vercel_cli_is_skill_paste_provider -v`
Expected: FAIL — `KeyError: unknown integration provider: 'vercel_cli'` (provider not added until Task 3) and/or `AttributeError: 'Provider' object has no attribute 'skill'`.

- [ ] **Step 3: Add the `SkillSpec` model + `Provider.skill` field**

```python
# registry.py — add near McpRemote
class SkillSpec(BaseModel):
    """A bundled skill delivered by a paste-token CLI integration. The
    materializer enables ``skills.entries.<key>`` and injects the credential as
    the env var ``primary_env`` (OpenClaw's native skill env injection). ``package``
    is the EXACT-pinned npm spec the skill's SKILL.md tells the agent to ``npx``."""

    key: str  # skills.entries.<key> this integration owns
    primary_env: str = Field(alias="primaryEnv")
    package: str  # e.g. "vercel@<x.y.z>" — exact version, never a range/tag

    model_config = {"populate_by_name": True}
```

```python
# registry.py — add field on Provider (next to mcp_remote)
    # Bundled skill delivered by a paste-token CLI integration (see SkillSpec).
    skill: SkillSpec | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/credentials/test_registry.py::test_vercel_cli_is_skill_paste_provider -v`
Expected: PASS (after Task 3 adds the YAML entry). If running before Task 3, it still fails on the missing provider — run Steps 3 and Task 3 Step 3, then re-run. (Order note: this test goes green at the end of Task 3.)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/credentials/registry.py apps/backend/tests/credentials/test_registry.py
git commit -m "feat(integrations): add SkillSpec block to the credential registry"
```

---

### Task 2: The bundled `vercel` SKILL.md

**Files:**
- Create: `apps/backend/core/services/credentials/skills/vercel/SKILL.md`
- Test: `apps/backend/tests/credentials/test_skill_cli.py` (asserts the file exists + frontmatter, added in Task 4)

**Interfaces:**
- Produces: a SKILL.md resource the `skill_cli` materializer reads and copies to EFS.

- [ ] **Step 1: Create the skill file**

```markdown
---
name: vercel
description: Deploy, inspect, and manage Vercel projects via the Vercel CLI.
primaryEnv: VERCEL_TOKEN
---

# Vercel CLI

You can use the Vercel CLI to manage the user's Vercel account. Authentication is
already configured — the `VERCEL_TOKEN` environment variable is set for you.

**Always invoke the CLI via npx with the pinned version:**

```bash
npx --yes vercel@<x.y.z> <command>
```

(The first call downloads + extracts to local scratch and may take ~1 minute; later
calls are fast.)

## Common commands

- Who am I / sanity check: `npx --yes vercel@<x.y.z> whoami`
- List projects: `npx --yes vercel@<x.y.z> project ls`
- List recent deployments: `npx --yes vercel@<x.y.z> ls`
- Read a deployment's build/runtime logs: `npx --yes vercel@<x.y.z> logs <deployment-url>`
- Inspect a deployment: `npx --yes vercel@<x.y.z> inspect <deployment-url>`

## Deploying

Deploy/rollback/env commands are project-scoped. If `VERCEL_ORG_ID` and
`VERCEL_PROJECT_ID` are set in the environment, deploys target that project. Otherwise,
from the project's working directory run once: `npx --yes vercel@<x.y.z> link --yes`,
then:

- Deploy a preview: `npx --yes vercel@<x.y.z> deploy`
- Promote to production: `npx --yes vercel@<x.y.z> deploy --prod`
- Roll back: `npx --yes vercel@<x.y.z> rollback <deployment-url>`

## On auth errors

If a command fails with a 401 / "not authorized" / "token expired", the user's Vercel
token has expired or been revoked. Tell the user to reconnect Vercel in Settings →
Integrations; do not retry.
```

> The literal `<x.y.z>` is replaced with the exact pinned version at seed time by the materializer (Task 4), sourced from `provider.skill.package`.

- [ ] **Step 2: Commit**

```bash
git add apps/backend/core/services/credentials/skills/vercel/SKILL.md
git commit -m "feat(integrations): add bundled Vercel CLI skill"
```

---

### Task 3: providers.yaml — add the `vercel_cli` provider

**Files:**
- Modify: `apps/backend/core/services/credentials/providers.yaml`
- Test: re-run Task 1's test (goes green here)

**Interfaces:**
- Consumes: `registry.SkillSpec`, the `skill_cli` materializer name (Task 5 registers it; the registry only needs the string).

- [ ] **Step 1: Add the provider entry** (append after `vercel:` / `posthog:`)

```yaml
# Bucket B variant — paste-token CLI delivered as a bundled SKILL (GitHub gh_cli
# model). The owner pastes a Vercel Account Token; the skill_cli materializer enables
# skills.entries.vercel + seeds the SKILL.md onto EFS, and OpenClaw injects the token as
# VERCEL_TOKEN (primaryEnv) into the gateway-host exec env. The agent runs `npx vercel`
# (extract→/tmp, storm-safe). Distinct key from the `vercel` MCP-OAuth integration; the
# two surfaces use different credentials and cannot share one. Validate by API call —
# Account-token prefixes overlap, so token_prefix is unusable.
vercel_cli:
  display_name: Vercel (CLI)
  acquisition: paste_token
  actor: skill
  refresh_owner: none
  validate: { method: GET, url: "https://api.vercel.com/v2/user", auth_header: Authorization }
  skill:
    key: vercel
    primaryEnv: VERCEL_TOKEN
    package: "vercel@<PINNED>"   # replace <PINNED> with the current exact version, e.g. vercel@39.1.3
  wiring:
    materializer: skill_cli
```

> Set `<PINNED>` to a real current exact version when implementing (check `npm view vercel version`).

- [ ] **Step 2: Run the registry test**

Run: `cd apps/backend && uv run pytest tests/credentials/test_registry.py -v`
Expected: PASS (including `test_vercel_cli_is_skill_paste_provider` and the existing self-consistency loop `test_registry_materializers_and_oauth_are_self_consistent` — which requires the `skill_cli` materializer to be registered; if it fails on "unknown materializer", do Task 5 first or together).

- [ ] **Step 3: Commit**

```bash
git add apps/backend/core/services/credentials/providers.yaml
git commit -m "feat(integrations): register vercel_cli paste-token provider"
```

---

### Task 4: The `skill_cli` materializer

**Files:**
- Create: `apps/backend/core/services/credentials/materializers/skill_cli.py`
- Test: `apps/backend/tests/credentials/test_skill_cli.py`

**Interfaces:**
- Consumes: `base.atomic_write`, `base.owner_efs_root`, `registry.get_provider`, `config_patcher.set_openclaw_config_path` / `delete_openclaw_config_path`.
- Produces: `write(owner_id, provider_key, credential)` and `remove(owner_id, provider_key, credential)`.

**Design notes:** `write` does two things — (a) patch `skills.entries.<key>` = `{enabled: true, apiKey: <token>}` via the config patcher (hot-reload), and (b) seed the SKILL.md to the owner's EFS skills dir with `<x.y.z>` replaced by the pinned version. `remove` deletes the config entry (disable) and the seeded skill dir, and the token file. The skills dir on EFS is `owner_efs_root/skills/<key>/SKILL.md` (config dir = `/home/node/.openclaw`, EFS-mapped). The config patcher is async; the materializer rail invokes materializers via `run_in_thread` (see `connections.py` disconnect), so drive async calls with `asyncio.run` (mirror `mcp_oauth.py:_run_sync`).

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/credentials/test_skill_cli.py
import json
from pathlib import Path
import pytest
from core.services.credentials.materializers import skill_cli


@pytest.fixture
def efs(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    return tmp_path


def test_write_seeds_skill_and_patches_config(efs, monkeypatch):
    written = {}

    async def fake_set(owner_id, path, value):
        written[tuple(path)] = value

    monkeypatch.setattr(
        "core.services.config_patcher.set_openclaw_config_path", fake_set
    )
    skill_cli.write("owner_1", "vercel_cli", {"token": "vcp_secret"})

    # (a) config entry enabled + token under apiKey
    entry = written[("skills", "entries", "vercel")]
    assert entry == {"enabled": True, "apiKey": "vcp_secret"}
    # (b) SKILL.md seeded onto EFS with the pinned version substituted
    md = (efs / "owner_1" / "skills" / "vercel" / "SKILL.md").read_text()
    assert "primaryEnv: VERCEL_TOKEN" in md
    assert "<x.y.z>" not in md  # placeholder replaced
    assert "vercel@" in md      # pinned package present


def test_remove_deletes_entry_and_skill(efs, monkeypatch):
    deleted = []

    async def fake_del(owner_id, path):
        deleted.append(tuple(path))

    monkeypatch.setattr(
        "core.services.config_patcher.delete_openclaw_config_path", fake_del
    )
    # seed something to remove
    skill_dir = efs / "owner_1" / "skills" / "vercel"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("x")

    skill_cli.remove("owner_1", "vercel_cli", {})

    assert ("skills", "entries", "vercel") in deleted
    assert not skill_dir.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/credentials/test_skill_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: ... skill_cli`.

- [ ] **Step 3: Implement the materializer**

```python
# apps/backend/core/services/credentials/materializers/skill_cli.py
"""Materializer for a CLI delivered as a bundled SKILL (e.g. Vercel).

`write` enables ``skills.entries.<key>`` with the pasted token under ``apiKey``
(OpenClaw injects it as the skill's ``primaryEnv`` into the gateway-host exec env)
and seeds the skill's SKILL.md onto the owner's EFS. `remove` reverses both.

The token lives ONLY in ``skills.entries.<key>.apiKey`` (scrubbed from the marketplace
slice) — never in any ``tools.*`` field."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from core.services import config_patcher
from core.services.credentials import registry
from core.services.credentials.materializers.base import atomic_write, owner_efs_root

_SKILL_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "skills"


def _run_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("skill_cli materializer must run off the request loop (use run_in_thread)")


def _skill_dir(owner_id: str, key: str) -> Path:
    return owner_efs_root(owner_id) / "skills" / key


def write(owner_id: str, provider_key: str, credential: dict) -> None:
    prov = registry.get_provider(provider_key)
    spec = prov.skill
    if spec is None:
        raise ValueError(f"{provider_key}: skill_cli requires a `skill` block")
    token = credential["token"]

    # (b) seed SKILL.md, substituting the pinned package version
    template = (_SKILL_TEMPLATE_ROOT / spec.key / "SKILL.md").read_text()
    body = template.replace("<x.y.z>", spec.package)
    atomic_write(_skill_dir(owner_id, spec.key) / "SKILL.md", body.encode())

    # (a) enable the skill + carry the token as apiKey (scrubbed from marketplace)
    _run_sync(
        config_patcher.set_openclaw_config_path(
            owner_id, ["skills", "entries", spec.key], {"enabled": True, "apiKey": token}
        )
    )


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    prov = registry.get_provider(provider_key)
    spec = prov.skill
    if spec is None:
        return
    _run_sync(
        config_patcher.delete_openclaw_config_path(owner_id, ["skills", "entries", spec.key])
    )
    shutil.rmtree(_skill_dir(owner_id, spec.key), ignore_errors=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/credentials/test_skill_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/credentials/materializers/skill_cli.py apps/backend/tests/credentials/test_skill_cli.py
git commit -m "feat(integrations): skill_cli materializer (seed skill + token on connect)"
```

---

### Task 5: Register the `skill_cli` materializer

**Files:**
- Modify: `apps/backend/core/services/credentials/materializers/__init__.py`
- Test: re-run `test_registry.py` (self-consistency loop)

- [ ] **Step 1: Add the imports + registry entries**

```python
# __init__.py
from core.services.credentials.materializers.skill_cli import remove as _skill_cli_remove
from core.services.credentials.materializers.skill_cli import write as _skill_cli

# in MATERIALIZERS:
    "skill_cli": _skill_cli,
# in MATERIALIZER_REMOVERS:
    "skill_cli": _skill_cli_remove,
```

- [ ] **Step 2: Run the self-consistency test**

Run: `cd apps/backend && uv run pytest tests/credentials/test_registry.py -v`
Expected: PASS — `test_registry_materializers_and_oauth_are_self_consistent` confirms `skill_cli` is registered with a matching remover.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/core/services/credentials/materializers/__init__.py
git commit -m "feat(integrations): register skill_cli materializer"
```

---

### Task 6: Harden `catalog_slice` to scrub the `tools` block

**Files:**
- Modify: `apps/backend/core/services/catalog_slice.py` (the `tools` deep-copy ~line 344; reuse `_scrub_secret_fields`)
- Test: `apps/backend/tests/unit/services/test_catalog_slice_scrub.py`

**Why:** defense in depth — even though our token lives in `skills.entries[*]` (already scrubbed), a future mistake placing a secret under `tools.exec.env` must not leak into published artifacts.

- [ ] **Step 1: Write the failing test**

```python
# apps/backend/tests/unit/services/test_catalog_slice_scrub.py
from core.services import catalog_slice


def test_tools_exec_env_is_scrubbed_in_slice():
    openclaw_json = {
        "tools": {"exec": {"mode": "full", "env": {"SECRET": "leak_me"}}},
        "skills": {"entries": {"vercel": {"enabled": True, "apiKey": "vcp_x"}}},
    }
    slice_ = catalog_slice.extract_agent_slice(openclaw_json)  # adjust to real signature
    env = slice_["tools"]["exec"]["env"]
    assert env == {"SECRET": ""}  # key preserved, value scrubbed
```

> Adjust `extract_agent_slice(...)` to the function's real signature/return; the assertion is what matters — a value under `tools.*` is blanked.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_catalog_slice_scrub.py -v`
Expected: FAIL — `SECRET` value is `"leak_me"` (tools copied unscrubbed).

- [ ] **Step 3: Apply the scrub to the `tools` copy**

At the `"tools": copy.deepcopy(openclaw_json.get("tools") or {})` site (~line 344), recursively blank secret-shaped values. Add a small recursive scrubber that walks the `tools` dict and blanks any key in `_SECRET_FIELD_NAMES` (and dict-typed `env`/`headers` to `{k: ""}`), reusing the existing `_scrub_secret_fields` semantics:

```python
def _scrub_tools_block(tools: dict[str, Any]) -> dict[str, Any]:
    """Recursively blank secret-shaped values anywhere in the tools block."""
    out = copy.deepcopy(tools)

    def _walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if k in _SECRET_FIELD_NAMES:
                    node[k] = {kk: "" for kk in v} if isinstance(v, dict) else ("" if not isinstance(v, list) else [])
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(out)
    return out
```

Replace the unscrubbed copy with `"tools": _scrub_tools_block(openclaw_json.get("tools") or {})`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_catalog_slice_scrub.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/catalog_slice.py apps/backend/tests/unit/services/test_catalog_slice_scrub.py
git commit -m "fix(marketplace): scrub secret-shaped values in the tools block of published slices"
```

---

### Task 7: Exec-host regression guard

**Files:**
- Test: `apps/backend/tests/unit/containers/test_exec_host_guard.py`
- (Reads `core.containers.config` — the openclaw.json builder.)

**Why:** the token injection only works on the `gateway` host, which holds only because we configure no sandbox and don't pin `host`. This tripwire fails if a future change adds `agents.defaults.sandbox` or pins `tools.exec.host` to something else, before it silently breaks every CLI token.

- [ ] **Step 1: Write the test**

```python
# apps/backend/tests/unit/containers/test_exec_host_guard.py
from core.containers import config

def test_emitted_config_keeps_exec_on_gateway_host():
    # Build the openclaw.json the way write_user_configs does; adjust to the real builder.
    cfg = config.build_openclaw_config_for_test()  # use the real builder entry point

    agents_defaults = (cfg.get("agents") or {}).get("defaults") or {}
    assert "sandbox" not in agents_defaults, (
        "agents.defaults.sandbox is set — exec would resolve to the sandbox host and "
        "drop skill-injected env vars (VERCEL_TOKEN). Keep it unset or fix CLI token delivery."
    )
    exec_cfg = (cfg.get("tools") or {}).get("exec") or {}
    assert exec_cfg.get("host") in (None, "gateway", "auto"), (
        f"tools.exec.host={exec_cfg.get('host')!r} — must stay gateway/auto-without-sandbox."
    )
```

> Wire `build_openclaw_config_for_test()` to the actual config builder used by `write_openclaw_config` (e.g. construct the dict the same way `config.py` does). If no pure builder exists, assert against `_build_exec_policy()` (no `host` key) **and** grep that `config.py` never emits `agents.defaults.sandbox`.

- [ ] **Step 2: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/containers/test_exec_host_guard.py -v`
Expected: PASS today (no sandbox configured, no host pin).

- [ ] **Step 3: Commit**

```bash
git add apps/backend/tests/unit/containers/test_exec_host_guard.py
git commit -m "test(containers): guard exec resolves to gateway host (skill env injection)"
```

---

### Task 8: Frontend — receipt + reconnect copy

**Files:**
- Modify: `apps/frontend/src/components/settings/integration-receipts.ts`
- Test: none required (typed data); ensure `tsc --noEmit` passes.

**Interfaces:**
- Consumes: `INTEGRATION_RECEIPTS` (keyed by provider id).

- [ ] **Step 1: Add the receipt**

```ts
  vercel_cli: { delivers: "Vercel CLI — deploy, logs, project & env tools for your account" },
```

- [ ] **Step 2: Typecheck**

Run: `cd apps/frontend && pnpm exec tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/components/settings/integration-receipts.ts
git commit -m "feat(integrations): vercel_cli connection receipt copy"
```

> **Note on the connect UI:** `ConnectionsList` + `ConnectButton` already render paste-token providers generically (the row appears automatically once `vercel_cli` is in the server-driven list). No new component is required for the basic paste flow. The 401→reconnect UX is conveyed by the skill text (Task 2) instructing the agent to tell the user to reconnect; a dedicated "expired" badge is a follow-up (see Task 9), not required for v1.

---

### Task 9 (optional, deferred): org/project capture + reconnect badge

**Decision #2 (capture org/project at connect)** and an explicit "token expired → reconnect" badge are **follow-ups**, not blockers: `vercel link --yes` (instructed in the SKILL.md) already makes `deploy` work without backend org/project capture, and the skill already routes 401s to "reconnect." Implement when desired:
- Extend `PasteBody` (`routers/connections.py`) with optional `vercel_org_id` / `vercel_project_id`; when present, the `skill_cli` materializer writes them under `skills.entries.vercel.env` (also scrubbed).
- Add an `expired` status to the integrations row + a backend signal when a `vercel` API call 401s.

(Left explicitly out of v1 scope to keep the first ship one-step and small.)

---

## Verification (end of plan)

- [ ] `cd apps/backend && uv run pytest tests/credentials/ tests/unit/services/test_catalog_slice_scrub.py tests/unit/containers/test_exec_host_guard.py -v` → all pass.
- [ ] `cd apps/frontend && pnpm exec tsc --noEmit` → clean.
- [ ] Manual dev check (one-time): connect `vercel_cli` on a dev account with a real Vercel Account Token → confirm `skills.entries.vercel` appears in the dev container's `openclaw.json` and a message asking the agent to run `npx vercel whoami` returns the account (first call ~1 min cold, then fast). Disconnect → confirm the entry + skill dir + token are gone.

## Out of scope (tracked, not built here)

- Pre-warm at connect (needs in-container exec we don't have).
- The `binary`/`kind:download` kind for non-npm CLIs (gh/gog-class) — separate plan, with mandatory sha256 verification.
- Folding MCP + CLI into a single "Vercel" entry.
- The unrelated provider-auth re-warm event-loop bug surfaced during verification.
