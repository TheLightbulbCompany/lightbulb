# Integration MCP Setup + Reprovision Resilience — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> This plan was grounded + adversarially verified against the codebase on 2026-05-31 (workflow `wf_a0e61965`). The findings are baked into the Background, Decisions, and Risks. Read those before the tasks.

**Headline use case:** *You hire a marketplace agent that needs Stripe. The hire flow makes Stripe work* — installs the Stripe MCP **server** (command + your key) into your container so the agent can actually call Stripe. The same mechanism powers a standalone connect and survives a reprovision.

**Goal:** ONE curated integration (**Stripe**) wired end-to-end so that connecting installs a working MCP **server** (full `{command, args, env}` block, not a bare env var), the connection survives a container reprovision, and the integration **owns** `mcp.servers.stripe` (overwriting any value there, leaving the user's other servers alone). The same path serves standalone connect, reprovision, and **marketplace hire**.

**Architecture:** Add a new `mcp_server` wiring rail to the curated provider registry (`{key, command, args, env}`, where `env` maps each env-var name to a credential *source* like `input.token`, resolved via the existing `_resolve_source` helper). A pure builder turns `(provider, credential) → {command, args, env}`. A small reconcile derives the connected integrations' server blocks from `(connected integrations × registry spec × Secrets-Manager secrets)` and writes each as a full block into `mcp.servers.<key>` — overwriting integration keys, never touching user keys. On provision the blocks are merged into the freshly-built `openclaw.json` (one write, no reload window); on the live paths (connect / marketplace hire / manual panel) they're patched in per-server via the file-locked `config_patcher`.

**Tech Stack:** Python 3.12 / FastAPI / Pydantic v2; `core/services/config_patcher.py` (fcntl-locked atomic openclaw.json RMW); `core/services/credentials/` (registry + apply); `core/containers/config.py` (`write_openclaw_config`) + `ecs_manager.py` (`write_user_configs`); pytest.

---

## Background (grounded 2026-05-31 — read before starting)

**The curated registry + its rails** (`core/services/credentials/`):
- `registry.py` — `Provider` + `Wiring`. `Wiring` today has exactly two fields, `openclaw_paths: list[OpenclawPath] | None` and `materializer: str | None`, with an `_exactly_one` validator: `if bool(self.openclaw_paths) == bool(self.materializer): raise`. **This is a 2-way XOR and must become 3-way** to admit `mcp_server` (Task 1). `get_provider`/`all_providers` load `providers.yaml` once via `@lru_cache`.
- **Live providers today (only 3):** `linear` + `stripe` (both `paste_token`, `actor=mcp`, `refresh_owner=none`, `openclaw_paths` writing `mcp.servers.<p>.env.<KEY>` — **env leaf only, NO command**); `google` (`direct_oauth`, `actor=cli`, `refresh_owner=actor`, `materializer=gog_cli`).
- **Two delivery rails exist; a third is being added:**
  - `openclaw_paths` (`apply_connection.py:28-32`) → `config_patcher.set_openclaw_config_path(owner, path.split("."), value)`. Writes a literal into a dotted path in `openclaw.json`.
  - `materializer` (`apply_connection.py:33-37`) → runs `MATERIALIZERS[name]` in a worker thread; writes the CLI's own credential **file** onto the owner's EFS (e.g. `gog_cli` writes the `gog` keyring under `.config/gogcli`). `toml_cli` exists but **no live provider uses it (dead code)**.
  - `mcp_server` (**NEW, this plan**) → writes the full `mcp.servers.<key>` block.
- **All delivery is backend-side EFS writes** — the backend mounts every owner's per-user EFS; there is **zero ECS-exec** into the running container. So apply/reconcile works whether the container is up or down.
- **Custody / retention** (`connections.py` `_retains_secret` = `refresh_owner != "actor"`): `linear`/`stripe` retain the credential dict in Secrets Manager at `isol8/<env>/integrations/<owner>/<provider>`; `google`/`gog` retains **nothing** in SM (the EFS keyring is the sole custodian — deliberate M6 non-retention). `integrations_repo` is a secret-free status index (`{owner_id, provider, status}`).

**Two consumer types, two persistence models** (the framing this plan commits to):
- **MCP-server consumer** (Stripe): reads its key from `mcp.servers.<p>.env`. Durable source = the **SM secret**. Reconcile rebuilds it from `(registry spec × SM secret)`. Fully reprovision-resilient.
- **CLI consumer** (gog/Google): reads its credential from a **file on EFS** (the keyring). Persistence = the **EFS keyring itself**. Survives a config-rewrite reprovision; a full EFS-wipe needs re-connect (no SM secret to rebuild from — `refresh_owner=actor`). Reconcile **skips** it (SM `get` returns null) — correct, not a gap.

**The provision write path** (`core/containers/`):
- `ecs_manager.write_user_configs` (`~1706`) resolves per-owner secrets (Paperclip key minted from SM at `~1818`, company id) and passes them into `write_openclaw_config`. **This is the established "fetch per-owner secrets at config-write time" pattern** the new code follows.
- `write_openclaw_config` (`config.py:929`) builds the dict and does `config_path.write_text(json.dumps(config, indent=2))` — a **full overwrite**, no lock. It sets `config["mcp"] = {"servers": {"paperclip": {...}}}` (`~905-924`) **only** the paperclip server. So a reprovision drops every non-paperclip server.
- Fresh provision creates the ECS service at `desiredCount=0`, runs `write_user_configs` while the **container is DOWN**, then scales to 1 → **no chokidar reload window**. `PATCH /debug/provision` runs `write_user_configs` on a **LIVE** container → chokidar (`CHOKIDAR_USEPOLLING`) reloads on each EFS write.
- `config_patcher` (`set_openclaw_config_path` / `delete_openclaw_config_path`) is a **locked atomic RMW**; requires the file to **pre-exist** (raises `ConfigPatchError` on `FileNotFoundError`); same path/format/chown as `write_openclaw_config`. `set_openclaw_config_path(["mcp","servers","stripe"], block)` replaces **only** that subtree (preserves `paperclip` + user keys); it **no-ops** when the value is unchanged.

**The marketplace deploy** (`catalog_service.py`):
- `deploy_from_artifact` deep-merges the seller's slice (whose `mcp.servers.<p>` has `env` scrubbed to `""` — the merge **blanks** any token the buyer had), then `_fill_buyer_credential_slots` (`~158-241`) re-fills the env leaf via `apply_connection`, guarded by `_slice_exposes_canonical_key` (`~129-155`) which skips a bundle whose secret sits under a non-canonical key.

**The cache crash-loop is fixed (PR #785).** Both npm + uv caches persist on EFS via `$HOME`-relative symlinks (`container-stack.ts:423,425`); verified against upstream `mcp-stdio-transport.ts:52-53` (MCP children inherit `HOME`). `npx -y @stripe/mcp` is safe. Caveat: the fix lives in the CDK base task def, so a per-user container only gets it after being **reprovisioned** past the #785 deploy.

---

## Decisions (resolved 2026-05-31)

- **D1 — Stripe is the only curated integration; Linear is REMOVED, not deferred-in-place.** Linear has no official local (API-key) MCP server, so it can't carry a `command`. Today it writes a **command-less, non-functional** server. Leaving it on the `openclaw_paths` rail is a latent container-brick (a leaf env-write onto a server with no command is an invalid MCP shape — see Risks). So Task 2 **deletes the `linear` block from `providers.yaml`**. It returns only when a real Linear MCP package + a clear use case exist, and then on the `mcp_server` rail. We **control** the catalog; we don't ship a half-integration.
- **D2 — MCP providers use the `mcp_server` rail; never `openclaw_paths` into `mcp.servers`.** The `openclaw_paths`-leaf-into-`mcp.servers` pattern is the root of both the "connect ≠ usable" gap and the brick. After this plan, no live provider uses it; `openclaw_paths` remains in the code only for genuine non-MCP config locations (none live).
- **D3 — CLI (materializer) integrations stay first-class, with EFS-keyring persistence.** gog/Google is unchanged. Reconcile **skips** it (no SM secret); it persists via its EFS keyring. A *future* paste-key (retained) CLI would extend reconcile with a re-materialize step — **not built now** (no such provider; YAGNI).
- **D4 — user-added servers are the user's responsibility (Task 9 = OUT OF SCOPE).** Reconcile only touches registry-provider keys. A user's own MCP servers / downloaded CLIs are theirs; not reconciled, not made wipe-resilient.
- **D5 — npx cache is mitigated.** No special handling needed; `@stripe/mcp` rides the EFS-symlinked npm cache. Smoke must still confirm the target container is post-#785.
- **D6 — `mcp_server.env` is a source-map, for forward-compatibility.** `env` maps each var name to a credential source (`input.token`, `credentials.access_token`, etc.), resolved by the same `_resolve_source` the `openclaw_paths` rail uses. This makes the *common* future case — adding another paste-key MCP (GitHub/Notion/Slack), an OAuth MCP, or a multi-env-var MCP — a **pure `providers.yaml` block with zero code**. The registry is the deliberate extension point; everything (connect, reconcile, reprovision, marketplace gate + hire, disconnect, the connect UI) iterates it generically.

---

## Ownership model (the single invariant)

Keys in `mcp.servers` whose name equals a **registry provider key** (today: `stripe`) are **integration-owned** — connect writes them, disconnect removes them, reconcile re-asserts them, and they win over any user value for that key. The reserved `paperclip` key is owned by `write_openclaw_config`. **Every other key is user-owned** and is never touched by integration code. The whole plan's correctness rests on this.

---

## File structure

| File | Change | Responsibility |
|------|--------|----------------|
| `core/services/credentials/registry.py` | Modify | Add `McpServer` model; add `mcp_server` to `Wiring`; **replace the 2-way `_exactly_one` validator with a 3-way XOR**. |
| `core/services/credentials/providers.yaml` | Modify | `stripe` → `mcp_server`; **delete the `linear` block**. `google` untouched. |
| `core/services/credentials/mcp_server.py` | **Create** | `build_mcp_server_block(provider, credential)` (pure), `build_connected_integration_servers(owner)` (provision map), `reconcile_integrations(owner)` (live per-server patch). |
| `core/services/credentials/apply_connection.py` | Modify | Add the `mcp_server` dispatch branch (full-block write) before the trailing `RuntimeError`. |
| `routers/connections.py` | Modify | `disconnect` removes `mcp.servers.<key>` for `mcp_server` providers. |
| `core/containers/config.py` | Modify | `write_openclaw_config` / its dict-builder accept + merge `integration_mcp_servers` into `config["mcp"]["servers"]` (preserving `paperclip`). |
| `core/containers/ecs_manager.py` | Modify | `write_user_configs` builds the connected-integration servers (best-effort) and passes them to `write_openclaw_config`. |
| `core/services/catalog_service.py` | Modify | `deploy_from_artifact` calls `reconcile_integrations` after the slice merge; **delete** `_fill_buyer_credential_slots` + `_slice_exposes_canonical_key` (safe — no `openclaw_paths`-MCP provider remains). |
| `routers/integrations.py` | Modify | After the manual-panel PUT/PATCH, call `reconcile_integrations` to re-assert integration-owned keys. |
| `tests/credentials/test_mcp_server.py` | **Create** | Unit tests for builder + reconcile + provision map. |

---

## Task 1: Registry — add the `mcp_server` rail + 3-way validator

**Files:**
- Modify: `apps/backend/core/services/credentials/registry.py` (`Wiring` + its validator)
- Test: `apps/backend/tests/credentials/test_registry.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/credentials/test_registry.py — add
def test_mcp_server_wiring_parses():
    from core.services.credentials.registry import Provider
    p = Provider(key="stripe", display_name="Stripe", acquisition="paste_token", actor="mcp",
                 refresh_owner="none",
                 wiring={"mcp_server": {"key": "stripe", "command": "npx",
                                       "args": ["-y", "@stripe/mcp", "--tools=all"],
                                       "env": {"STRIPE_SECRET_KEY": "input.token"}}})
    assert p.wiring.mcp_server.command == "npx"
    assert p.wiring.mcp_server.env == {"STRIPE_SECRET_KEY": "input.token"}
    assert p.wiring.openclaw_paths is None and p.wiring.materializer is None

def test_wiring_accepts_exactly_one_of_three():
    import pytest
    from pydantic import ValidationError
    from core.services.credentials.registry import Wiring, McpServer
    # zero set → reject
    with pytest.raises(ValidationError):
        Wiring()
    # two set → reject (this is the case the OLD 2-way validator could not catch)
    with pytest.raises(ValidationError):
        Wiring(materializer="gog_cli",
               mcp_server=McpServer(key="x", command="npx", args=[], env={"K": "input.token"}))
```

- [ ] **Step 2: Run** — `cd apps/backend && uv run pytest tests/credentials/test_registry.py -k "mcp_server or exactly_one" -v` → FAIL (`mcp_server` not a field; old validator only compares two fields).

- [ ] **Step 3: Implement** — add the model and rewrite the validator to a true 3-way exclusivity check (replace the existing `bool(openclaw_paths) == bool(materializer)` body):

```python
class McpServer(BaseModel):
    key: str            # the mcp.servers.<key> this integration owns
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)  # env var name -> credential source ("input.token", "credentials.access_token")


class Wiring(BaseModel):
    openclaw_paths: list[OpenclawPath] | None = None
    materializer: str | None = None
    mcp_server: McpServer | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "Wiring":
        if sum(bool(x) for x in (self.openclaw_paths, self.materializer, self.mcp_server)) != 1:
            raise ValueError("wiring must set exactly one of openclaw_paths | materializer | mcp_server")
        return self
```

- [ ] **Step 4: Run** — `uv run pytest tests/credentials/test_registry.py -v` → PASS.
- [ ] **Step 5: Commit** — `git add apps/backend/core/services/credentials/registry.py apps/backend/tests/credentials/test_registry.py && git commit -m "feat(credentials): add mcp_server wiring rail + 3-way exclusivity validator"`

---

## Task 2: providers.yaml — Stripe on `mcp_server`; remove Linear

**Files:**
- Modify: `apps/backend/core/services/credentials/providers.yaml`
- Test: `apps/backend/tests/credentials/test_registry.py` (+ update any test that asserted `linear` exists)

- [ ] **Step 1: Write the failing test**

```python
def test_stripe_is_mcp_server_and_linear_is_gone():
    from core.services.credentials.registry import get_provider, all_providers
    w = get_provider("stripe").wiring
    assert w.mcp_server is not None and w.mcp_server.key == "stripe"
    assert w.mcp_server.env == {"STRIPE_SECRET_KEY": "input.token"}
    assert "linear" not in all_providers()   # Linear removed (D1)
```

- [ ] **Step 2: Run** — FAIL (stripe still `openclaw_paths`; `linear` still present).
- [ ] **Step 3: Implement** — set the `stripe` block to its official server and **delete the entire `linear:` block**. Leave `google:` untouched. Preserve `stripe`'s existing `validate:` line verbatim:

```yaml
stripe:
  display_name: Stripe
  acquisition: paste_token
  actor: mcp
  refresh_owner: none
  validate: { method: GET, url: "https://api.stripe.com/v1/account", auth_header: Authorization }
  wiring:
    mcp_server: { key: stripe, command: npx, args: ["-y", "@stripe/mcp", "--tools=all"], env: { STRIPE_SECRET_KEY: input.token } }
```

> Removing `linear` drops it from the connect surface. Any pre-existing `linear` status rows / `mcp.servers.linear` env leaves in dev are harmless orphans (the server had no command, so it never ran) and can be ignored or cleaned. No placeholder package name ships.

- [ ] **Step 4: Run** — `uv run pytest tests/credentials/test_registry.py -v` → PASS. Grep for stale references: `grep -rn '"linear"\|linear:' apps/backend/core apps/backend/tests` — update/remove any test that asserted Linear connectivity.
- [ ] **Step 5: Commit** — `git commit -am "feat(credentials): Stripe → mcp_server rail; remove non-functional Linear provider"`

---

## Task 3: The MCP-server builder + reconcile

**Files:**
- Create: `apps/backend/core/services/credentials/mcp_server.py`
- Test: `apps/backend/tests/credentials/test_mcp_server.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/credentials/test_mcp_server.py
import pytest
from unittest.mock import AsyncMock
from core.services.credentials.mcp_server import (
    build_mcp_server_block, build_connected_integration_servers, reconcile_integrations,
)

def test_build_block_from_paste_credential():
    assert build_mcp_server_block("stripe", {"token": "sk_live_X"}) == {
        "command": "npx", "args": ["-y", "@stripe/mcp", "--tools=all"],
        "env": {"STRIPE_SECRET_KEY": "sk_live_X"}}

def test_build_block_none_for_non_mcp_provider():
    assert build_mcp_server_block("google", {"access_token": "a"}) is None  # materializer, not mcp_server

@pytest.mark.asyncio
async def test_build_connected_map_skips_actor_and_unretained(monkeypatch):
    from core.services.credentials import mcp_server as m
    monkeypatch.setattr(m.integrations_repo, "list_for_owner",
        AsyncMock(return_value=[{"provider": "stripe", "status": "connected"},
                                {"provider": "google", "status": "connected"}]))
    monkeypatch.setattr(m.integration_secret_store, "get",
        AsyncMock(side_effect=lambda o, p: {"token": "sk_X"} if p == "stripe" else None))
    servers = await m.build_connected_integration_servers("org_1")
    assert servers == {"stripe": {"command": "npx", "args": ["-y", "@stripe/mcp", "--tools=all"],
                                  "env": {"STRIPE_SECRET_KEY": "sk_X"}}}

@pytest.mark.asyncio
async def test_reconcile_writes_each_block_per_server(monkeypatch):
    from core.services.credentials import mcp_server as m
    monkeypatch.setattr(m, "build_connected_integration_servers",
        AsyncMock(return_value={"stripe": {"command": "npx", "args": [], "env": {"STRIPE_SECRET_KEY": "sk_X"}}}))
    writes = []
    async def _set(owner, path, value): writes.append((path, value))
    monkeypatch.setattr(m.config_patcher, "set_openclaw_config_path", _set)
    await m.reconcile_integrations("org_1")
    # per-server REPLACE of the stripe subtree only — preserves paperclip + user keys
    assert writes == [(["mcp", "servers", "stripe"],
                       {"command": "npx", "args": [], "env": {"STRIPE_SECRET_KEY": "sk_X"}})]

@pytest.mark.asyncio
async def test_reconcile_noops_when_config_missing(monkeypatch):
    from core.services.credentials import mcp_server as m
    monkeypatch.setattr(m, "build_connected_integration_servers",
        AsyncMock(return_value={"stripe": {"command": "npx", "args": [], "env": {}}}))
    async def _raise(owner, path, value): raise m.config_patcher.ConfigPatchError("no file")
    monkeypatch.setattr(m.config_patcher, "set_openclaw_config_path", _raise)
    await m.reconcile_integrations("org_1")  # must not raise
```

- [ ] **Step 2: Run** — FAIL (module missing).
- [ ] **Step 3: Implement** `mcp_server.py`:

```python
"""Build + reconcile the per-owner MCP servers an integration owns.

A curated provider with `mcp_server` wiring owns `mcp.servers.<key>` in the
owner's openclaw.json. Connect installs the full {command,args,env} block;
disconnect removes it; reconcile re-asserts it from the durable sources
(registry spec x Secrets-Manager secret). Actor-refresh providers (gog) retain
no SM secret and persist via their EFS keyring — reconcile skips them.
"""

from __future__ import annotations

from typing import Any

from core.repositories import integrations_repo
from core.services import config_patcher
from core.services.credentials import integration_secret_store, registry


def build_mcp_server_block(provider_key: str, credential: dict[str, Any]) -> dict | None:
    """The {command, args, env} block for a provider's MCP server, or None if the
    provider has no mcp_server wiring. Each env var's value is resolved from the
    credential via the registry's source string (e.g. "input.token"), reusing the
    same _resolve_source the openclaw_paths rail uses — so a paste-key, OAuth, or
    multi-env provider is pure registry data."""
    spec = registry.get_provider(provider_key).wiring.mcp_server
    if spec is None:
        return None
    # Lazy import: apply_connection lazily imports THIS module's builder, so keep
    # the dependency one-directional at module-load time.
    from core.services.credentials.apply_connection import _resolve_source
    return {"command": spec.command, "args": list(spec.args),
            "env": {var: _resolve_source(src, credential, credential) for var, src in spec.env.items()}}


async def build_connected_integration_servers(owner_id: str) -> dict[str, dict]:
    """{provider_key: full_block} for every connected mcp_server provider whose
    credential is retained in Secrets Manager. The single source of truth used by
    both the provision-time merge and the live reconcile. Skips actor-refresh
    providers (SM get -> None) and non-mcp_server rails."""
    servers: dict[str, dict] = {}
    for row in await integrations_repo.list_for_owner(owner_id):
        provider = row.get("provider")
        if not provider or row.get("status", "connected") != "connected":
            continue
        spec = registry.get_provider(provider).wiring.mcp_server
        if spec is None:
            continue
        cred = await integration_secret_store.get(owner_id, provider)
        if not cred:
            continue
        block = build_mcp_server_block(provider, cred)
        if block is not None:
            servers[spec.key] = block
    return servers


async def reconcile_integrations(owner_id: str) -> None:
    """Re-assert every connected integration's MCP server into the LIVE
    openclaw.json. Per-server REPLACE of mcp.servers.<key> (preserves the
    paperclip block + user-owned keys); idempotent (set no-ops when unchanged).
    No-op if the container has no config yet (ConfigPatchError swallowed — a
    provision will inject the blocks instead)."""
    for key, block in (await build_connected_integration_servers(owner_id)).items():
        try:
            await config_patcher.set_openclaw_config_path(owner_id, ["mcp", "servers", key], block)
        except config_patcher.ConfigPatchError:
            return
```

- [ ] **Step 4: Run** — `uv run pytest tests/credentials/test_mcp_server.py -v` → PASS.
- [ ] **Step 5: Commit** — `git add apps/backend/core/services/credentials/mcp_server.py apps/backend/tests/credentials/test_mcp_server.py && git commit -m "feat(credentials): mcp_server builder + reconcile"`

---

## Task 4: apply_connection installs the full server on connect

**Files:**
- Modify: `apps/backend/core/services/credentials/apply_connection.py` (add a branch before the trailing `RuntimeError`)
- Test: `apps/backend/tests/credentials/test_apply_connection.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_apply_mcp_server_writes_full_block(monkeypatch):
    from core.services.credentials import apply_connection as ac
    calls = []
    async def _set(owner, path, value): calls.append((owner, path, value))
    monkeypatch.setattr(ac.config_patcher, "set_openclaw_config_path", _set)
    await ac.apply_connection("org_1", "stripe", {"token": "sk_live_X"})
    assert calls == [("org_1", ["mcp", "servers", "stripe"],
                      {"command": "npx", "args": ["-y", "@stripe/mcp", "--tools=all"],
                       "env": {"STRIPE_SECRET_KEY": "sk_live_X"}})]
```

- [ ] **Step 2: Run** — FAIL (no mcp_server branch; falls through to `RuntimeError`).
- [ ] **Step 3: Implement** — add the branch (a provider has exactly one rail per Task 1, so order is safe):

```python
    if w.mcp_server:
        from core.services.credentials.mcp_server import build_mcp_server_block
        block = build_mcp_server_block(provider_key, credential)
        # Replace the whole server subtree — the integration OWNS mcp.servers.<key>,
        # so a stale/scrubbed/user value for this key is overwritten.
        await config_patcher.set_openclaw_config_path(
            owner_id, ["mcp", "servers", w.mcp_server.key], block)
        return
```

- [ ] **Step 4: Run** — `uv run pytest tests/credentials/test_apply_connection.py -v` → PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(credentials): connect installs the full mcp_server block"`

---

## Task 5: disconnect removes the integration's server block

**Files:**
- Modify: `apps/backend/routers/connections.py` (`disconnect`, after `_get_provider_or_404`)
- Test: `apps/backend/tests/unit/routers/test_connections.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
@patch("routers.connections.config_patcher")
@patch("routers.connections.integration_secret_store")
@patch("routers.connections.integrations_repo")
async def test_disconnect_removes_mcp_server_block(self, mock_repo, mock_store, mock_patcher, async_client):
    mock_repo.delete = AsyncMock(); mock_store.delete = AsyncMock()
    mock_patcher.delete_openclaw_config_path = AsyncMock()
    r = await async_client.delete("/api/v1/connections/stripe")
    assert r.status_code == 200
    mock_patcher.delete_openclaw_config_path.assert_awaited_once_with(
        "user_test_123", ["mcp", "servers", "stripe"])
```

- [ ] **Step 2: Run** — FAIL (disconnect has no mcp_server branch — it would leave the live-token server block on EFS).
- [ ] **Step 3: Implement** — add an `mcp_server` branch alongside the existing materializer/openclaw_paths handling (keep those):

```python
    if p.wiring.mcp_server:
        await config_patcher.delete_openclaw_config_path(
            owner_id, ["mcp", "servers", p.wiring.mcp_server.key])
```

- [ ] **Step 4: Run** — `uv run pytest tests/unit/routers/test_connections.py -v` → PASS.
- [ ] **Step 5: Commit** — `git commit -am "fix(credentials): disconnect removes the mcp_server block (no credential residue)"`

---

## Task 6: Re-inject integration servers at provision (reprovision resilience)

Merge the connected integrations' blocks INTO the freshly-built `openclaw.json` — a single write, no reload window (container is down on fresh provision), and no possibility of a command-less server (we write the full block). This is preferred over a post-write reconcile loop for the provision path.

**Files:**
- Modify: `apps/backend/core/containers/config.py` (the dict-builder's paperclip-MCP block `~905-924` + `write_openclaw_config` signature `~929`)
- Modify: `apps/backend/core/containers/ecs_manager.py` (`write_user_configs`, after the per-owner secret fetches, before `write_openclaw_config`)
- Test: `apps/backend/tests/unit/containers/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
def test_build_merges_integration_mcp_servers_and_keeps_paperclip():
    from core.containers.config import build_openclaw_config_dict
    cfg = build_openclaw_config_dict(
        # ...the existing required args used by the neighbouring config tests...,
        integration_mcp_servers={"stripe": {"command": "npx", "args": ["-y", "@stripe/mcp"],
                                            "env": {"STRIPE_SECRET_KEY": "sk_X"}}},
    )
    assert cfg["mcp"]["servers"]["stripe"]["command"] == "npx"
    # paperclip (when present in the test fixture) is preserved alongside stripe
```

- [ ] **Step 2: Run** — FAIL (`integration_mcp_servers` not a param).
- [ ] **Step 3: Implement**
  - In `config.py`, give the dict-builder and `write_openclaw_config` an `integration_mcp_servers: dict | None = None` param. After the paperclip block runs, merge (integration keys win; create `mcp`/`servers` if paperclip was absent):

```python
    if integration_mcp_servers:
        servers = config.setdefault("mcp", {}).setdefault("servers", {})
        servers.update(integration_mcp_servers)  # full blocks; integration keys win, paperclip preserved
```

  - In `ecs_manager.write_user_configs`, build the map best-effort (a Secrets-Manager hiccup must not fail the whole provision — mirror the `pre_stage_friday_identity` best-effort style) and pass it:

```python
        try:
            from core.services.credentials.mcp_server import build_connected_integration_servers
            integration_mcp_servers = await build_connected_integration_servers(owner_id)
        except Exception:
            logger.exception("build_connected_integration_servers failed for %s; provisioning without them", owner_id)
            integration_mcp_servers = {}
        # ... existing call, with the new kwarg:
        await write_openclaw_config(..., integration_mcp_servers=integration_mcp_servers)
```

- [ ] **Step 4: Run** — `uv run pytest tests/unit/containers/ -k "config or provision" -v` → PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(credentials): merge connected integration MCP servers into openclaw.json on provision"`

---

## Task 7: Marketplace hire installs the integration server (headline use case)

`deploy_from_artifact` deep-merges the seller's slice (blanking the buyer's Stripe token), then re-asserts the buyer's real, full block via `reconcile_integrations`. The old per-slice slot-fill + canonical-key guard are deleted — safe **because** no `openclaw_paths`-MCP provider remains (Stripe's full-block overwrite makes a non-canonical key impossible; Linear is gone).

**Files:**
- Modify: `apps/backend/core/services/catalog_service.py` (replace the call at `~512`; delete `_fill_buyer_credential_slots` `~158-241` + `_slice_exposes_canonical_key` `~129-155`)
- Test: `apps/backend/tests/unit/services/test_catalog_service.py` + migrate/remove `tests/credentials/test_marketplace_requires_credentials.py::test_slot_fill_skips_bundle_with_non_canonical_key`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_deploy_reconciles_integration_servers(monkeypatch):
    import core.services.catalog_service as cs
    called = {}
    async def _reconcile(owner_id): called["owner"] = owner_id
    monkeypatch.setattr(cs, "reconcile_integrations", _reconcile)
    assert not hasattr(cs, "_fill_buyer_credential_slots")
    assert not hasattr(cs, "_slice_exposes_canonical_key")
    # ...drive deploy_from_artifact with a stripe-bundling slice via the existing harness...
    assert called["owner"] == "org_1"
```

- [ ] **Step 2: Run** — FAIL (`reconcile_integrations` not imported; old helpers still present).
- [ ] **Step 3: Implement**
  - Import at module top: `from core.services.credentials.mcp_server import reconcile_integrations`.
  - Replace `await _fill_buyer_credential_slots(owner_id, slice_)` (`~512`) with:

```python
            # The deep-merge above lands the slice's registry-provider servers with
            # scrubbed-blank credentials. Re-assert the buyer's own connected
            # integrations from the durable sources (registry spec x Secrets Manager),
            # overwriting those keys with the canonical server + the buyer's token.
            await reconcile_integrations(owner_id)
```

  - Delete `_fill_buyer_credential_slots` and `_slice_exposes_canonical_key`. Keep `assert_credentials_satisfied` (the gate) unchanged.
  - Migrate the canonical-key test to assert the full-block overwrite (the buyer's `mcp.servers.stripe` ends as the registry block with the real token), or remove it if redundant with Task 3's reconcile test.

- [ ] **Step 4: Run** — `uv run pytest tests/unit/services/test_catalog_service.py tests/credentials/test_marketplace_requires_credentials.py -v` → PASS. Confirm clean: `grep -rn "_fill_buyer_credential_slots\|_slice_exposes_canonical_key" apps/backend` → empty.
- [ ] **Step 5: Commit** — `git commit -am "feat(marketplace): hire installs integration MCP servers via reconcile (drops slot-fill + canonical-key guard)"`

---

## Task 8: Manual panel re-asserts integration servers (ownership)

**Files:**
- Modify: `apps/backend/routers/integrations.py` (`replace_mcp_servers` PUT + `upsert_mcp_server` PATCH)
- Test: `apps/backend/tests/unit/routers/test_integrations_mcp.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_put_servers_reasserts_connected_integration(monkeypatch, async_client):
    import routers.integrations as ri
    monkeypatch.setattr(ri, "reconcile_integrations", AsyncMock())
    monkeypatch.setattr(ri, "set_openclaw_config_path", AsyncMock())
    r = await async_client.put("/api/v1/integrations/mcp/servers",
        json={"servers": {"my-custom": {"command": "node", "args": ["x.js"], "env": {}}}})
    assert r.status_code == 200
    ri.reconcile_integrations.assert_awaited_once_with("user_test_123")
```

- [ ] **Step 2: Run** — FAIL (PUT doesn't reconcile; the whole-dict replace would drop `mcp.servers.stripe`).
- [ ] **Step 3: Implement** — after the existing `set_openclaw_config_path(...)` write in both handlers:

```python
    from core.services.credentials.mcp_server import reconcile_integrations
    await reconcile_integrations(owner_id)  # re-assert integration-owned keys over the user's input
```

- [ ] **Step 4: Run** — `uv run pytest tests/unit/routers/ -k integrations_mcp -v` → PASS.
- [ ] **Step 5: Commit** — `git commit -am "feat(integrations): manual MCP panel re-asserts integration-owned servers"`

---

## Task 9 (OUT OF SCOPE this pass — D4): preserve user-added servers across a full EFS-wipe

> Documented for completeness; **not built now**. Reconcile rebuilds integration servers from durable sources; user-added servers have no durable copy. Build only if the "user MCPs are the user's responsibility" stance changes. Implementation sketch: in `write_user_configs`, read the pre-overwrite `mcp.servers`, carry forward keys that are neither registry-provider keys nor `paperclip`, and fold them into `integration_mcp_servers`.

---

## Final verification

- [ ] `cd apps/backend && uv run pytest tests/credentials tests/unit/routers/test_connections.py tests/unit/routers/test_integrations_mcp.py tests/unit/containers tests/unit/services/test_catalog_service.py -v`
- [ ] **Pre-flight:** confirm the target dev container is post-#785 (has the EFS npm/uv cache symlinks) — otherwise reprovision it first so `npx -y @stripe/mcp` warms onto EFS instead of crash-looping.
- [ ] **Connect smoke:** connect Stripe at `/settings/connections` → `mcp.servers.stripe` in `openclaw.json` has `command: npx` (full block, not just env) → `/chat` "list my recent Stripe charges" returns real data with **no** separate manual MCP step.
- [ ] **Hire smoke (headline):** with Stripe connected, hire a marketplace agent requiring Stripe → the deploy lands `mcp.servers.stripe` as the canonical server + the real token (not the scrubbed bundle) → the agent calls Stripe on first run.
- [ ] **Reprovision smoke:** with Stripe connected, `PATCH /api/v1/debug/provision` → `mcp.servers.stripe` is still present (full block re-injected) and `mcp.servers.paperclip` survives.
- [ ] **Ownership smoke:** add a `my-custom` server via the manual panel → survives a Stripe connect/disconnect; set a `stripe`-keyed server via the panel → reconcile overwrites it with the integration's.

---

## Risks / notes (grounded)

- **The `openclaw_paths`-into-`mcp.servers` anti-pattern is the load-bearing lesson.** Writing only an env leaf under `mcp.servers.<p>` (no `command`) yields an invalid MCP server that bricks the container on chokidar reload (`reference_openclaw_config_brick`). This plan eliminates it: MCP providers use the full-block `mcp_server` rail; Linear (which can't carry a command) is removed. Never reintroduce an MCP provider on the `openclaw_paths` rail.
- **Verify `@stripe/mcp` runs in the container** — `npx -y @stripe/mcp --tools=all` reachable, `STRIPE_SECRET_KEY` is the env it reads. The npx/uv crash-loop is fixed (PR #785 EFS cache symlinks, npm + uv), so this is safe on any post-#785 container — but a pre-#785 container must be reprovisioned first to pick up the symlinks.
- **CLI integrations are first-class but have a different persistence model.** gog/Google (materializer/`refresh_owner=actor`) persists via its EFS keyring, not SM — reconcile correctly skips it. It survives a config-rewrite reprovision; a **full EFS-wipe** needs re-connect (no SM secret to rebuild from — deliberate M6 non-retention). This is `refresh_owner`-determined, not "CLI vs MCP": a future paste-key CLI would be re-materializable.
- **Reconcile must patch per-server, never the whole `mcp.servers` subtree** — `set_openclaw_config_path(["mcp","servers","stripe"], block)` preserves the `paperclip` block + user keys. (The manual-panel PUT intentionally replaces the whole dict; Task 8's reconcile-after restores the integration keys.)
- **`write_openclaw_config` is an unlocked `write_text`; reconcile/config_patcher is fcntl-locked.** Within `write_user_configs` they run strictly sequentially (overwrite fully completes before any patch), so no intra-call race. A concurrent manual-panel write during a reprovision could in theory interleave (pre-existing property, reprovision is rare/gated) — keep reconcile sequential, never `gather` it with the overwrite.
- **Reload amplification (future, not v1):** on a LIVE container, a multi-provider live reconcile fires one chokidar reload per provider. With only Stripe today that's one write. If a second `mcp_server` provider ships, batch the per-server writes into a single `_locked_rmw` (a new `config_patcher` helper) to collapse reloads.
- **Reconcile is best-effort at provision** — a Secrets-Manager hiccup must not fail the whole reprovision (Task 6 wraps the build; the standalone reconcile swallows `ConfigPatchError`).
- **No new cross-stack infra.** Pure backend + config; ships independently of the deferred Pass-2 table-drop cleanup.
