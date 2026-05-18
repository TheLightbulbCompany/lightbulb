# Marketplace deploy: RPC-pair agent entry, file-merge the rest

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the chokidar race that strands marketplace-deployed agents in a "file-written but container-not-yet-reloaded" limbo by routing the agent entry through OpenClaw's `agents.create` + `agents.update` RPCs (live in-memory state update) while keeping plugins/mcp/skills/cron on the file-merge path.

**Architecture:** Extend the existing `pair_with_openclaw` helper with an optional `extra_update_fields` dict so it threads the full slice-derived agent entry (identity, skills, reasoningDefault, etc.) into the `agents.update` RPC. Reorder `catalog_service.deploy_from_artifact` so workspace extract → RPC pair runs *before* the file-merge writes for plugins/mcp/skills/cron. Drop the `agent_entry` branch from `apply_deploy_mutation` entirely (catalog_service is the sole caller). Router resolves container IP + token via `ecs_manager.resolve_running_container(owner_id)` and threads them through.

**Tech Stack:** Python (FastAPI, pytest-asyncio), boto3 (DynamoDB + ECS lookups already wired), existing `pair_with_openclaw` over the gateway pool.

---

## Why this matters (background for the implementer)

After a buyer hires a listing, the existing flow:

1. Router mints a Paperclip agent (`create_paperclip_for_marketplace`) → gets a fresh UUID.
2. Router calls `catalog_service.deploy_from_artifact(new_agent_id=<uuid>)`.
3. Catalog extracts `workspace.tar.gz` into EFS at `workspaces/{uuid}/`.
4. Catalog calls `apply_deploy_mutation` which writes the agent_entry into `openclaw.json["agents"]["list"]` AND deep-merges plugins/mcp/skills into their respective subtrees, all under a single `fcntl.lockf`.
5. Catalog writes the `.template` sidecar.
6. Catalog appends cron jobs into `cron/jobs.json`.

OpenClaw runs chokidar with `CHOKIDAR_USEPOLLING=true` on EFS — there's a 1–30s lag between the file write at step 4 and the container's in-memory state actually reloading. The frontend's `useAgents().refresh()` fires immediately after the deploy returns, calling the gateway's `agents.list` RPC — which returns stale data (no Ray) because chokidar hasn't reloaded yet. SWR caches the stale snapshot. User sees the chat sidebar with no Ray, or sees a UUID where the friendly name should be.

The fix: stop relying on chokidar to register the agent. Use the gateway RPC instead — `agents.create` + `agents.update` apply immediately to the container's in-memory state (chokidar still picks up the file when it gets around to it, and the two paths converge to the same final state). Plugins/mcp/skills/cron stay on the file-merge path because they don't need to be live for an `agents.list` response to show the new agent.

The `/chat` flow already does exactly this — `_intercept_agents_create` in `routers/websocket_chat.py` mints Paperclip then calls `pair_with_openclaw`. We're mirroring that approach into the marketplace deploy.

---

## File Structure

**Modified files:**

- `apps/backend/core/services/paperclip_openclaw_pairing.py` — add `extra_update_fields: dict | None = None` param to `pair_with_openclaw`. When set, those fields ride along on the `agents.update` RPC after the friendly name.
- `apps/backend/core/services/catalog_service.py` — reorder `deploy_from_artifact`: workspace-extract → pair-with-openclaw (RPC, threading `extra_update_fields=agent_entry`) → file-merge plugins/mcp/skills → sidecar → cron. Stops calling `apply_deploy_mutation`; calls a renamed/simplified `apply_deploy_aux_mutations` instead. New `container_ip` + `container_token` kwargs.
- `apps/backend/core/services/config_patcher.py` — rename `apply_deploy_mutation` → `apply_deploy_aux_mutations`, drop the `agent_entry` positional arg and the entire `agents.list` mutation branch (legacy flat-list migration code goes with it). Plugins/mcp/skills branches stay verbatim.
- `apps/backend/routers/marketplace_listings.py` — in `deploy_listing`, after Paperclip mint succeeds, resolve container via `ecs_manager.resolve_running_container(owner_id)` and pass `container_ip` + `container_token` into `catalog.deploy_from_artifact`. Add a 503 short-circuit if no container is reachable (rolls back Paperclip first, same pattern as the existing catch-all in this function).

**Modified test files:**

- `apps/backend/tests/unit/services/test_paperclip_openclaw_pairing.py` — add tests for `extra_update_fields` (None preserves existing behavior; populated dict gets merged into update params).
- `apps/backend/tests/unit/services/test_catalog_service.py` — rewrite the three existing `apply_deploy` tests to use the new ordering. Add: RPC-pair called before file-merge; RPC-pair failure rolls back workspace dir and does NOT touch file-merge; container-unreachable raises early. The three existing tests that touch `agent_entry` on `apply_deploy_mutation` become tests that the `agent_entry` is passed to `pair_with_openclaw` (via the gateway-pool mock).
- `apps/backend/tests/unit/services/test_config_patcher_apply_deploy.py` (if it exists; otherwise inline in an existing config_patcher test file) — update existing tests for `apply_deploy_mutation` to call the renamed `apply_deploy_aux_mutations` without an `agent_entry` arg; drop any test that exercised the agent_entry append path.
- `apps/backend/tests/unit/routers/test_marketplace_listings_deploy.py` (or wherever the existing deploy router tests live; grep first) — add: 503 when container unreachable; Paperclip rolled back if RPC pair fails.

---

## Task 1: Extend `pair_with_openclaw` with `extra_update_fields`

**Files:**
- Modify: `apps/backend/core/services/paperclip_openclaw_pairing.py:125-194`
- Test: `apps/backend/tests/unit/services/test_paperclip_openclaw_pairing.py`

The helper currently calls `agents.update` with `{"agentId": pc_uuid, "name": friendly_name}`. We're adding an optional `extra_update_fields` dict that callers can use to thread additional config (identity, skills list, reasoningDefault, workspace path override, etc.) through the same RPC. The friendly `name` always wins — `extra_update_fields["name"]` is overridden by the explicit `friendly_name` param. The `agentId` field is always overridden by `pc_uuid` so callers can't accidentally point at the wrong agent.

When `extra_update_fields is None`, behavior is identical to today (existing `_intercept_agents_create` caller passes None and is unaffected).

- [ ] **Step 1: Write the failing test for `extra_update_fields=None`**

In `apps/backend/tests/unit/services/test_paperclip_openclaw_pairing.py`, add after the existing tests:

```python
@pytest.mark.asyncio
async def test_pair_with_extra_update_fields_none_preserves_existing_behavior():
    """extra_update_fields=None (default) -> agents.update RPC params
    contain only agentId + name, identical to today. Backwards
    compatibility for the chat-side caller (_intercept_agents_create)
    that doesn't supply extras."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    pool = MagicMock()
    pool.send_rpc = AsyncMock(return_value={"ok": True, "agentId": pc_uuid})

    await pair_with_openclaw(
        owner_id="user_abc",
        paperclip_agent={"id": pc_uuid, "name": "Code Reviewer", "role": "engineer"},
        gateway_pool=pool,
        ip="10.0.0.5",
        token="gw_token",
        # Note: NOT passing extra_update_fields — exercises default arg.
    )

    update_call = pool.send_rpc.await_args_list[1]
    assert update_call.kwargs["params"] == {
        "agentId": pc_uuid,
        "name": "Code Reviewer",
    }
```

- [ ] **Step 2: Write the failing test for `extra_update_fields` populated**

```python
@pytest.mark.asyncio
async def test_pair_with_extra_update_fields_threads_into_update_rpc():
    """extra_update_fields is merged into the agents.update RPC params
    so marketplace deploys can carry the full slice-derived agent entry
    (identity, skills, reasoningDefault, etc.) through the same call."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    pool = MagicMock()
    pool.send_rpc = AsyncMock(return_value={"ok": True, "agentId": pc_uuid})

    await pair_with_openclaw(
        owner_id="user_abc",
        paperclip_agent={"id": pc_uuid, "name": "Ray", "role": "general"},
        gateway_pool=pool,
        ip="10.0.0.5",
        token="gw_token",
        extra_update_fields={
            "identity": {"emoji": "🍃", "vibe": "calm"},
            "skills": ["resy"],
            "reasoningDefault": "balanced",
            # These should be overridden by the helper:
            "agentId": "ATTEMPTED_OVERRIDE",
            "name": "ATTEMPTED_OVERRIDE",
        },
    )

    update_call = pool.send_rpc.await_args_list[1]
    params = update_call.kwargs["params"]
    # Caller-supplied agentId/name are clobbered — the helper is the
    # single source of truth for those.
    assert params["agentId"] == pc_uuid
    assert params["name"] == "Ray"
    # extras are threaded through.
    assert params["identity"] == {"emoji": "🍃", "vibe": "calm"}
    assert params["skills"] == ["resy"]
    assert params["reasoningDefault"] == "balanced"
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd apps/backend
uv run pytest tests/unit/services/test_paperclip_openclaw_pairing.py::test_pair_with_extra_update_fields_none_preserves_existing_behavior tests/unit/services/test_paperclip_openclaw_pairing.py::test_pair_with_extra_update_fields_threads_into_update_rpc -v
```

Expected: FAIL with `TypeError: pair_with_openclaw() got an unexpected keyword argument 'extra_update_fields'`.

- [ ] **Step 4: Implement `extra_update_fields` support**

Edit `apps/backend/core/services/paperclip_openclaw_pairing.py:125-194`. Replace the existing `pair_with_openclaw` function:

```python
async def pair_with_openclaw(
    *,
    owner_id: str,
    paperclip_agent: dict[str, Any],
    gateway_pool: Any,
    ip: str,
    token: str,
    extra_update_fields: dict[str, Any] | None = None,
) -> None:
    """Pair one Paperclip agent with the owner's OpenClaw container.

    Calls OpenClaw's ``agents.create`` RPC with ``name=<paperclip_uuid>``
    (so OpenClaw derives ``agentId`` from the UUID via
    ``normalizeAgentId``), then ``agents.update`` to set the human-
    friendly display name plus any caller-supplied extras.

    ``extra_update_fields`` — optional dict folded into the
    ``agents.update`` params. Used by marketplace deploys to thread the
    full slice-derived agent entry (identity, skills, reasoningDefault,
    workspace path, etc.) through the same RPC instead of file-merging
    it into ``openclaw.json`` and waiting for chokidar to reload. The
    helper always overrides ``agentId`` (to ``pc_uuid``) and ``name``
    (to ``friendly_name``), so callers can include them in the dict and
    they'll be ignored — the helper is the source of truth for both.

    Idempotent: an "agent already exists" error from agents.create is
    swallowed (the agent was paired by a previous run or by the
    provision-time openclaw.json write).

    Other errors propagate so the caller (an HTTP handler or RPC
    interceptor) can return them to the user. Container reachability
    is the caller's responsibility — this helper assumes the gateway
    pool can reach the owner's container.

    Note: ``gateway_pool.send_rpc`` takes a ``user_id`` kwarg by name,
    but the gateway pool is keyed by container owner. Always pass the
    owner_id there.
    """
    pc_uuid = paperclip_agent["id"]
    friendly_name = paperclip_agent.get("name") or pc_uuid

    create_params = {
        "name": pc_uuid,
        "workspace": f"/home/node/.openclaw/workspaces/{pc_uuid}",
    }
    if paperclip_agent.get("model"):
        create_params["model"] = paperclip_agent["model"]

    try:
        await gateway_pool.send_rpc(
            user_id=owner_id,  # gateway pool is keyed by owner_id
            req_id=f"pair-create-{secrets.token_hex(8)}",
            method="agents.create",
            params=create_params,
            ip=ip,
            token=token,
        )
    except RuntimeError as e:
        # OpenClaw returns INVALID_REQUEST with "already exists" when the
        # agent is registered. Treat as idempotent success.
        if "already exists" in str(e):
            logger.info(
                "pair_with_openclaw: agent %s already exists in container for owner %s — skipping update",
                pc_uuid,
                owner_id,
            )
            return
        raise

    # Build update params: start from caller extras (if any), then
    # overlay the helper-owned fields (agentId, name) so they always
    # win. Caller cannot accidentally re-point the update at a
    # different agent or rename it back to the UUID.
    update_params: dict[str, Any] = dict(extra_update_fields) if extra_update_fields else {}
    update_params["agentId"] = pc_uuid
    update_params["name"] = friendly_name

    await gateway_pool.send_rpc(
        user_id=owner_id,  # gateway pool is keyed by owner_id
        req_id=f"pair-update-{secrets.token_hex(8)}",
        method="agents.update",
        params=update_params,
        ip=ip,
        token=token,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd apps/backend
uv run pytest tests/unit/services/test_paperclip_openclaw_pairing.py -v
```

Expected: all tests pass — the two new ones plus the existing ones (which use the default `extra_update_fields=None` path).

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/paperclip_openclaw_pairing.py \
        apps/backend/tests/unit/services/test_paperclip_openclaw_pairing.py
git commit -m "feat(pairing): extra_update_fields on pair_with_openclaw"
```

---

## Task 2: Drop `agent_entry` branch from `apply_deploy_mutation`; rename to `apply_deploy_aux_mutations`

**Files:**
- Modify: `apps/backend/core/services/config_patcher.py:305-412`
- Test: `apps/backend/tests/unit/services/test_config_patcher.py` (find existing tests with `grep -ln apply_deploy_mutation apps/backend/tests/`)

The current `apply_deploy_mutation(owner_id, agent_entry, plugins_patch, *, mcp_patch, skills_patch)` is invoked only by `catalog_service.deploy_from_artifact`. After Task 3 we're going to route agent_entry through the RPC path instead, so this function's first positional arg + the agents.list mutation branch become dead. Drop them. Rename the function to `apply_deploy_aux_mutations` to signal that it's the AUX (plugins/mcp/skills) merge, not the whole deploy mutation anymore.

The legacy `agents.list` flat-list migration code goes with it — that code only made sense to run alongside an agent_entry append, and the migration was a self-heal hook for the same call. Future deploys can no longer trigger it because they don't touch `agents.list` at all. If a legacy flat-list config still exists in the wild, a normal openclaw.json read by the container will surface as a schema-validation error and the user re-provisions; we don't keep undead self-heal code around for a theoretical case.

The `tools.allowed` self-heal stays — it's unrelated to agent_entry handling and still has a real cleanup job to do for any pre-fix configs.

- [ ] **Step 1: Find and read existing `apply_deploy_mutation` tests**

```bash
grep -rln "apply_deploy_mutation" apps/backend/tests/
```

Expected: at least one test file using `apply_deploy_mutation`. Read the matching test(s) to understand the existing contract you'll be rewriting. Pay attention to any tests that assert agent_entry append behavior — those become DELETED in Step 4.

- [ ] **Step 2: Write the failing test for the renamed function with no agent_entry**

In the test file from Step 1 (or create `apps/backend/tests/unit/services/test_config_patcher_aux.py` if there isn't a logical home), replace the old `apply_deploy_mutation` tests with:

```python
@pytest.mark.asyncio
async def test_apply_deploy_aux_mutations_merges_plugins_mcp_skills_only(tmp_path, monkeypatch):
    """The aux mutation deep-merges plugins/mcp/skills into openclaw.json.
    It does NOT touch agents.list — that path is owned by the RPC pair
    now."""
    from core.services import config_patcher

    config_dir = tmp_path / "user_abc"
    config_dir.mkdir()
    config_path = config_dir / "openclaw.json"
    config_path.write_text('{"agents": {"list": [{"id": "agent_pre_existing"}]}, "plugins": {"entries": {}}}')

    monkeypatch.setattr(config_patcher, "_efs_mount_path", str(tmp_path))

    await config_patcher.apply_deploy_aux_mutations(
        "user_abc",
        plugins_patch={"entries": {"resy": {"enabled": True}}},
        mcp_patch={"servers": {"resy": {"command": "node"}}},
        skills_patch={"entries": {"haiku": {"enabled": True}}, "allowBundled": ["foo"]},
    )

    import json
    written = json.loads(config_path.read_text())
    # Plugins/mcp/skills merged.
    assert written["plugins"]["entries"]["resy"] == {"enabled": True}
    assert written["mcp"]["servers"]["resy"] == {"command": "node"}
    assert written["skills"]["entries"]["haiku"] == {"enabled": True}
    assert written["skills"]["allowBundled"] == ["foo"]
    # agents.list untouched — pre-existing agent still there, no new one appended.
    assert written["agents"]["list"] == [{"id": "agent_pre_existing"}]


@pytest.mark.asyncio
async def test_apply_deploy_aux_mutations_no_agent_entry_kwarg():
    """The renamed function no longer accepts an agent_entry arg. This
    test exists to lock in the signature so the agent_entry branch
    cannot accidentally come back without a corresponding test change."""
    import inspect
    from core.services import config_patcher

    sig = inspect.signature(config_patcher.apply_deploy_aux_mutations)
    assert "agent_entry" not in sig.parameters
    assert list(sig.parameters.keys())[0] == "owner_id"
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd apps/backend
uv run pytest tests/unit/services/test_config_patcher_aux.py -v
# (or wherever you placed the new tests)
```

Expected: FAIL — `apply_deploy_aux_mutations` doesn't exist yet.

- [ ] **Step 4: Implement the rename + drop the agent_entry branch**

Edit `apps/backend/core/services/config_patcher.py:305-412`. Replace the entire `apply_deploy_mutation` function with:

```python
async def apply_deploy_aux_mutations(
    owner_id: str,
    *,
    plugins_patch: dict,
    mcp_patch: dict | None = None,
    skills_patch: dict | None = None,
) -> None:
    """Atomically apply the auxiliary subtree mutations for a catalog
    deploy (plugins, mcp, skills) to openclaw.json.

    The agent entry itself is NOT written here — it goes through the
    OpenClaw gateway's ``agents.create`` + ``agents.update`` RPCs in
    ``catalog_service.deploy_from_artifact`` so the container's in-memory
    state updates immediately (instead of waiting for chokidar to pick
    up the file write, which on EFS with poll-mode can lag 1-30s).

    Performed inside a single exclusive lock so concurrent deploys
    cannot race on the read-modify-write of the plugins / mcp / skills
    subtrees:
      1. Deep-merge ``plugins_patch`` into ``config["plugins"]``.
      2. (Optional) Deep-merge ``mcp_patch`` (e.g. ``{"servers": {…}}``)
         into ``config["mcp"]``.
      3. (Optional) Deep-merge ``skills_patch.entries`` and union
         ``skills_patch.allowBundled`` into ``config["skills"]``.

    NOTE: top-level ``tools`` is intentionally left alone — see history
    in the prior ``apply_deploy_mutation`` for the ``tools.allowed``
    self-heal rationale (still in place below).
    """

    def _mutate(current: dict) -> bool:
        if plugins_patch:
            existing_plugins = current.get("plugins")
            if not isinstance(existing_plugins, dict):
                existing_plugins = {}
            current["plugins"] = _deep_merge(existing_plugins, plugins_patch)

        if mcp_patch:
            existing_mcp = current.get("mcp")
            if not isinstance(existing_mcp, dict):
                existing_mcp = {}
            current["mcp"] = _deep_merge(existing_mcp, mcp_patch)

        if skills_patch:
            existing_skills = current.get("skills")
            if not isinstance(existing_skills, dict):
                existing_skills = {}

            patch_bundled = skills_patch.get("allowBundled")
            patch_minus_bundled = {k: v for k, v in skills_patch.items() if k != "allowBundled"}
            if patch_minus_bundled:
                existing_skills = _deep_merge(existing_skills, patch_minus_bundled)
            if isinstance(patch_bundled, list):
                existing_bundled = existing_skills.get("allowBundled")
                if not isinstance(existing_bundled, list):
                    existing_bundled = []
                merged_bundled = list(existing_bundled)
                for name in patch_bundled:
                    if name not in merged_bundled:
                        merged_bundled.append(name)
                existing_skills["allowBundled"] = merged_bundled
            current["skills"] = existing_skills

        # Self-heal: strip the invalid ``tools.allowed`` key if present
        # (left over from pre-fix deploys). Cleans up the bad on-disk state
        # so OpenClaw's strict-schema validator accepts the file again.
        tools = current.get("tools")
        if isinstance(tools, dict) and "allowed" in tools:
            del tools["allowed"]

        return True

    await _locked_rmw(
        owner_id,
        _mutate,
        f"deploy aux merge plugins={bool(plugins_patch)} mcp={bool(mcp_patch)} skills={bool(skills_patch)}",
    )
```

- [ ] **Step 5: Sweep callers of the old name**

```bash
grep -rn "apply_deploy_mutation" apps/backend/
```

Expected: hits only in `catalog_service.py` (the import + the call site — both to be updated in Task 3) and in any old test files. Do NOT update `catalog_service.py` in this task — Task 3 will. Update only test files in this sweep (rename to `apply_deploy_aux_mutations`, drop `agent_entry` positional arg, switch `plugins_patch` to a kwarg). Delete any test cases that explicitly asserted on the agent_entry append behavior — they cover dead code now and Task 3's tests cover the replacement RPC path.

- [ ] **Step 6: Run targeted tests to verify the renamed function passes**

```bash
cd apps/backend
uv run pytest tests/unit/services/test_config_patcher_aux.py -v
# Plus any pre-existing test files you updated in Step 5:
uv run pytest tests/unit/services/test_config_patcher.py -v
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/config_patcher.py \
        apps/backend/tests/unit/services/test_config_patcher*.py
git commit -m "refactor(config_patcher): drop agent_entry; rename to apply_deploy_aux_mutations"
```

---

## Task 3: Reorder `catalog_service.deploy_from_artifact`: extract → RPC pair → file-merge → sidecar → cron

**Files:**
- Modify: `apps/backend/core/services/catalog_service.py:113-296` (the whole `deploy_from_artifact` method + the singleton wire-up at the bottom)
- Test: `apps/backend/tests/unit/services/test_catalog_service.py`

`deploy_from_artifact` gains two new kwargs:
- `container_ip: str` — the running container's task IP, resolved by the router via `ecs_manager.resolve_running_container(owner_id)`.
- `container_token: str` — the gateway token from the same container row.

Both are passed straight to `pair_with_openclaw`. They have NO defaults — a caller that can't supply them shouldn't be calling deploy at all, because the deploy now requires a reachable container (file-only deploys with no live RPC are not a supported mode anymore).

The injected `apply_deploy_mutation` collaborator is renamed to `apply_deploy_aux_mutations`. The singleton constructor at the bottom of the file wires it from `config_patcher.apply_deploy_aux_mutations`. Test injection follows the new name.

A new collaborator is injected: `pair_fn` — defaults to `core.services.paperclip_openclaw_pairing.pair_with_openclaw`, but overridable so tests can swap a `MagicMock` in without monkeypatching the module-level import.

Order of operations inside the try-block:
1. Workspace extract (already happens before the try; keep it there).
2. (NEW) `pair_with_openclaw(..., extra_update_fields=agent_entry_minus_id_and_workspace)` — this REPLACES the `apply_deploy_mutation` call for the agent_entry side. Skipped iff `slice_has_agent` is False (legacy listings; same gate as today). The `id` and `workspace` fields are not included in `extra_update_fields` — the helper writes those from `pc_uuid` and the create-RPC's `workspace` param.
3. (Renamed) `apply_deploy_aux_mutations(...)` — only when plugins/mcp/skills are non-empty AND `slice_has_agent`. Skipped when the slice has no agent (legacy listing — there's nothing to register MCPs against).
4. (Existing) write template sidecar.
5. (Existing) append cron jobs.

Rollback (except clause):
- Existing `cleanup_agent_dirs` — keep verbatim.
- The existing P2 `remove_from_openclaw_config_list(..., agents.list, ...)` rollback is now DEAD CODE in the success-then-fail-later case (the file never gained an agent_entry, so there's nothing to remove). REMOVE the entire P2 fallback block — it was solving for the old failure mode.
- Optional new rollback: best-effort `agents.delete` RPC. Skipped for v0 — if the pair RPC succeeded, the container has the agent in memory, and a later file-merge failure (e.g. sidecar write) leaves the agent functional in-memory but missing its template sidecar. The buyer still has a working agent; the only loss is the "installed-templates" sidebar entry. Acceptable tradeoff for v0. Document this in the function docstring.

The `container_ip` / `container_token` come from the router via `ecs_manager.resolve_running_container` — see Task 4.

- [ ] **Step 1: Write the failing test — RPC pair called BEFORE file-merge**

Replace the existing `_make_service` helper and tests in `apps/backend/tests/unit/services/test_catalog_service.py`:

```python
"""Tests for catalog_service."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, call  # noqa: E402

import pytest  # noqa: E402

from core.services.catalog_service import CatalogService  # noqa: E402


def _make_service():
    """Construct a CatalogService with fully mocked workspace, aux
    mutation, AND pair_with_openclaw, so tests focus on the
    orchestration logic in ``deploy_from_artifact`` and not the EFS /
    config-patcher / gateway side effects.
    """
    workspace = MagicMock()
    workspace.extract_tarball_to_workspace = MagicMock(return_value=None)
    workspace.write_template_sidecar = MagicMock(return_value=None)
    workspace.read_cron_jobs = MagicMock(return_value=[])
    workspace.cleanup_agent_dirs = MagicMock(return_value=None)
    apply_aux = AsyncMock(return_value=None)
    pair_fn = AsyncMock(return_value=None)
    svc = CatalogService(
        workspace=workspace,
        apply_deploy_aux_mutations=apply_aux,
        pair_fn=pair_fn,
    )
    return svc, workspace, apply_aux, pair_fn


@pytest.mark.asyncio
async def test_deploy_calls_pair_before_aux_merge():
    """Order matters: pair_with_openclaw runs FIRST. If it fails, we
    haven't yet polluted openclaw.json with orphan plugin entries.
    """
    svc, workspace, apply_aux, pair_fn = _make_service()
    call_order = []
    pair_fn.side_effect = lambda *a, **kw: call_order.append("pair") or None
    apply_aux.side_effect = lambda *a, **kw: call_order.append("aux") or None
    workspace.extract_tarball_to_workspace.side_effect = lambda *a, **kw: call_order.append("extract")
    workspace.write_template_sidecar.side_effect = lambda *a, **kw: call_order.append("sidecar")

    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_={"agent": {"name": "Demo"}, "plugins": {"entries": {"p": {}}}, "tools": {}, "cron_jobs": []},
        tar_bytes=b"fake-tar",
        new_agent_id="agent_xyz",
        container_ip="10.0.0.5",
        container_token="gw_tok",
    )

    assert call_order == ["extract", "pair", "aux", "sidecar"]


@pytest.mark.asyncio
async def test_deploy_threads_agent_entry_through_pair_extra_update_fields():
    """The slice's agent dict (minus id/workspace) lands in
    pair_with_openclaw's extra_update_fields. The id and workspace are
    intentionally left out — the helper owns those (pc_uuid + the
    workspace path it builds from pc_uuid)."""
    svc, _ws, _aux, pair_fn = _make_service()
    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_={
            "agent": {
                "name": "Demo",
                "identity": {"emoji": "🍃"},
                "skills": ["resy"],
                "reasoningDefault": "balanced",
            },
            "plugins": {},
            "tools": {},
            "cron_jobs": [],
        },
        tar_bytes=b"fake-tar",
        new_agent_id="agent_xyz",
        container_ip="10.0.0.5",
        container_token="gw_tok",
    )

    pair_fn.assert_awaited_once()
    kwargs = pair_fn.await_args.kwargs
    assert kwargs["owner_id"] == "user_buyer"
    assert kwargs["paperclip_agent"]["id"] == "agent_xyz"
    assert kwargs["paperclip_agent"]["name"] == "Demo"
    assert kwargs["ip"] == "10.0.0.5"
    assert kwargs["token"] == "gw_tok"
    extras = kwargs["extra_update_fields"]
    assert extras["identity"] == {"emoji": "🍃"}
    assert extras["skills"] == ["resy"]
    assert extras["reasoningDefault"] == "balanced"
    # id/workspace stripped — helper owns those.
    assert "id" not in extras
    assert "workspace" not in extras


@pytest.mark.asyncio
async def test_deploy_pair_failure_skips_aux_and_rolls_back_workspace():
    """If pair_with_openclaw raises, apply_deploy_aux_mutations is NOT
    called, and cleanup_agent_dirs runs. No orphan plugin/mcp entries
    in openclaw.json; just an extracted workspace dir to GC."""
    svc, workspace, apply_aux, pair_fn = _make_service()
    pair_fn.side_effect = RuntimeError("container unreachable")

    with pytest.raises(RuntimeError, match="container unreachable"):
        await svc.deploy_from_artifact(
            owner_id="user_buyer",
            slug="demo",
            manifest={"name": "Demo", "version": 1},
            slice_={"agent": {"name": "Demo"}, "plugins": {"entries": {"p": {}}}, "tools": {}, "cron_jobs": []},
            tar_bytes=b"fake-tar",
            new_agent_id="agent_xyz",
            container_ip="10.0.0.5",
            container_token="gw_tok",
        )

    apply_aux.assert_not_awaited()
    workspace.cleanup_agent_dirs.assert_called_once_with("user_buyer", "agent_xyz")


@pytest.mark.asyncio
async def test_deploy_legacy_slice_with_no_agent_skips_pair_and_aux():
    """Listings published before slice-synthesis shipped have slice_={}
    (or slice_.agent absent). Both pair_with_openclaw and
    apply_deploy_aux_mutations are skipped — we still extract the
    workspace + write the sidecar, but no RPC + no config merge.
    """
    svc, workspace, apply_aux, pair_fn = _make_service()
    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="legacy",
        manifest={"name": "Legacy", "version": 1},
        slice_={},
        tar_bytes=b"fake-tar",
        new_agent_id="agent_xyz",
        container_ip="10.0.0.5",
        container_token="gw_tok",
    )

    pair_fn.assert_not_awaited()
    apply_aux.assert_not_awaited()
    workspace.write_template_sidecar.assert_called_once()


@pytest.mark.asyncio
async def test_deploy_uses_supplied_new_agent_id_in_sentinel_substitution():
    """The MCP-args sentinel substitution (Task 2 of #713) still runs;
    the buyer's agent_id is substituted into ``__AGENT_DIR__`` slots
    before the aux merge.
    """
    svc, _ws, apply_aux, _pair = _make_service()
    slice_ = {
        "agent": {"name": "Demo"},
        "plugins": {},
        "tools": {},
        "cron_jobs": [],
        "mcp": {
            "servers": {
                "resy": {
                    "command": "node",
                    "args": ["/home/node/.openclaw/workspaces/__AGENT_DIR__/mcp-resy/index.js"],
                    "env": {"RESY_EMAIL": ""},
                }
            }
        },
    }
    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=slice_,
        tar_bytes=b"fake-tar",
        new_agent_id="agent_xyz",
        container_ip="10.0.0.5",
        container_token="gw_tok",
    )

    apply_aux.assert_awaited_once()
    mcp_patch = apply_aux.await_args.kwargs["mcp_patch"]
    args = mcp_patch["servers"]["resy"]["args"]
    assert "__AGENT_DIR__" not in args[0]
    assert "agent_xyz" in args[0]


@pytest.mark.asyncio
async def test_deploy_generates_uuid_when_new_agent_id_omitted():
    """Backwards-compat: direct callers (tests, future paths) that
    don't pass new_agent_id still get a locally-generated UUID.
    """
    svc, _ws, _aux, _pair = _make_service()
    result = await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_={"agent": {"name": "Demo"}, "plugins": {}, "tools": {}, "cron_jobs": []},
        tar_bytes=b"fake-tar",
        container_ip="10.0.0.5",
        container_token="gw_tok",
    )
    assert result["agent_id"].startswith("agent_")


@pytest.mark.asyncio
async def test_deploy_coerces_decimal_version_in_sidecar():
    """Regression for the dev-tested marketplace deploy 500: boto3
    returns DDB Numbers as Decimal. Sidecar write must coerce to int.
    """
    from decimal import Decimal

    svc, workspace, _aux, _pair = _make_service()
    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": Decimal("1")},
        slice_={"agent": {"name": "Demo"}, "plugins": {}, "tools": {}, "cron_jobs": []},
        tar_bytes=b"fake-tar",
        new_agent_id="agent_test",
        container_ip="10.0.0.5",
        container_token="gw_tok",
    )
    workspace.write_template_sidecar.assert_called_once()
    content = workspace.write_template_sidecar.call_args.kwargs["content"]
    assert isinstance(content["template_version"], int)
    assert content["template_version"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd apps/backend
uv run pytest tests/unit/services/test_catalog_service.py -v
```

Expected: FAIL — CatalogService doesn't yet accept `apply_deploy_aux_mutations`/`pair_fn`/`container_ip`/`container_token`.

- [ ] **Step 3: Rewrite `CatalogService` + `deploy_from_artifact`**

Edit `apps/backend/core/services/catalog_service.py`. Replace the whole class body (everything between `class CatalogService:` and the closing of `list_deployed_for_user`) and the singleton wire-up at the bottom:

```python
class CatalogService:
    def __init__(
        self,
        *,
        workspace,
        apply_deploy_aux_mutations: Callable[..., Awaitable[None]],
        pair_fn: Callable[..., Awaitable[None]],
    ):
        self._workspace = workspace
        self._apply_aux = apply_deploy_aux_mutations
        self._pair_fn = pair_fn

    async def deploy_from_artifact(
        self,
        *,
        owner_id: str,
        slug: str,
        manifest: dict,
        slice_: dict,
        tar_bytes: bytes,
        container_ip: str,
        container_token: str,
        new_agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Deploy a pre-fetched artifact (tarball + slice + manifest) into the
        owner's container.

        Steps:
          1. Extract ``tar_bytes`` into EFS at ``workspaces/{new_agent_id}/``.
          2. If the slice carries an agent, register it LIVE via
             ``pair_with_openclaw`` over the gateway — bypassing the
             chokidar reload lag that plagued the file-merge path. The
             slice's agent dict (minus ``id`` / ``workspace``) rides
             along on the ``agents.update`` RPC as ``extra_update_fields``.
          3. Deep-merge plugins/mcp/skills into the owner's openclaw.json
             via ``apply_deploy_aux_mutations`` (no agent_entry write —
             the agent is already in memory from step 2).
          4. Write the ``.template`` sidecar so the buyer's
             installed-templates sidebar surfaces this deploy.
          5. Append the cron jobs carried by the slice.

        Args:
            container_ip / container_token: resolved by the caller via
                ``ecs_manager.resolve_running_container(owner_id)``. No
                defaults — a deploy without a reachable container is
                not a supported mode.
            new_agent_id: pinned buyer-side OpenClaw agent_id. Marketplace
                deploys pass Paperclip's freshly-minted UUID so the two
                systems share the same id. Defaults to a locally-generated
                ``agent_<hex>`` UUID for direct callers.

        Rollback posture:
            - Workspace dir cleanup is best-effort + idempotent (always
              runs on failure).
            - A successful pair followed by a failed aux merge / sidecar
              write leaves the agent FUNCTIONAL in the container's
              in-memory state (chokidar will eventually pick up the
              partially-written file). The buyer can chat. The only
              loss is the installed-templates sidebar entry (sidecar
              missing) or some plugins/mcps not registered. Accepted
              tradeoff for v0 — the inverse "delete the in-memory agent"
              RPC isn't wired today and the partial-write recovery is
              chokidar's job anyway.
        """
        if new_agent_id is None:
            new_agent_id = f"agent_{uuid.uuid4().hex[:12]}"

        self._workspace.extract_tarball_to_workspace(
            user_id=owner_id,
            agent_id=new_agent_id,
            tar_bytes=tar_bytes,
        )

        slice_ = slice_ or {}
        slice_has_agent = bool(slice_.get("agent"))
        agent_entry: dict[str, Any] = {}

        try:
            if slice_has_agent:
                # Deep-copy so we don't mutate caller state (matters if
                # S3 returned a cached/shared dict).
                agent_entry = copy.deepcopy(slice_.get("agent") or {})
                # Strip id/workspace from the update payload — the
                # pairing helper owns those. Keep agent_entry locally
                # with id/workspace set so the return-value summary
                # below still describes the deployed agent.
                agent_entry["id"] = new_agent_id
                agent_entry["workspace"] = f".openclaw/workspaces/{new_agent_id}"
                extra_update_fields = {
                    k: v
                    for k, v in agent_entry.items()
                    if k not in ("id", "workspace")
                }

                # Synthetic Paperclip-shaped dict for the helper — only
                # ``id`` and ``name`` (and optional ``model``) are read
                # off it; the rest comes from extra_update_fields.
                paperclip_agent = {
                    "id": new_agent_id,
                    "name": agent_entry.get("name") or new_agent_id,
                }
                if agent_entry.get("model"):
                    paperclip_agent["model"] = agent_entry["model"]

                await self._pair_fn(
                    owner_id=owner_id,
                    paperclip_agent=paperclip_agent,
                    gateway_pool=_get_gateway_pool_for_pair(),
                    ip=container_ip,
                    token=container_token,
                    extra_update_fields=extra_update_fields,
                )

                # Aux merge — plugins/mcp/skills. agent_entry is NOT in here.
                plugins_patch = copy.deepcopy(slice_.get("plugins") or {})
                mcp_patch = copy.deepcopy(slice_.get("mcp")) or None
                mcp_patch = _substitute_agent_dir_sentinel(mcp_patch, new_agent_id)
                skills_patch = copy.deepcopy(slice_.get("skills")) or None

                await self._apply_aux(
                    owner_id,
                    plugins_patch=plugins_patch,
                    mcp_patch=mcp_patch,
                    skills_patch=skills_patch,
                )

            raw_version = manifest.get("version", 1)
            try:
                template_version = int(raw_version) if raw_version is not None else 1
            except (TypeError, ValueError):
                template_version = 1
            self._workspace.write_template_sidecar(
                user_id=owner_id,
                agent_id=new_agent_id,
                content={
                    "template_slug": slug,
                    "template_version": template_version,
                    "deployed_at": datetime.now(timezone.utc).isoformat(),
                },
            )

            template_cron_jobs = slice_.get("cron_jobs") or []
            if template_cron_jobs and slice_has_agent:
                from core.services.config_patcher import append_cron_jobs

                await append_cron_jobs(
                    owner_id,
                    _remap_cron_jobs_for_deploy(
                        template_cron_jobs,
                        new_agent_id=new_agent_id,
                        owner_id=owner_id,
                    ),
                )
        except Exception:
            # Workspace extracted but pair / aux / sidecar / cron failed.
            # cleanup_agent_dirs is idempotent + best-effort.
            self._workspace.cleanup_agent_dirs(owner_id, new_agent_id)
            # We deliberately do NOT issue an agents.delete RPC rollback
            # if the pair already succeeded — see method docstring,
            # "Rollback posture". v0 tradeoff.
            raise

        return {
            "slug": slug,
            "version": manifest.get("version", 1),
            "agent_id": new_agent_id,
            "name": manifest.get("name", slug),
            "skills_added": list(agent_entry.get("skills") or []),
            "plugins_enabled": list(((slice_.get("plugins") or {}).get("entries") or {}).keys()),
            "cron_jobs_added": (len(slice_.get("cron_jobs") or []) if slice_has_agent else 0),
            "config_registered": slice_has_agent,
        }

    # ---- deployed ----

    def list_deployed_for_user(self, owner_id: str) -> list[dict[str, Any]]:
        # (unchanged — keep the existing implementation verbatim)
        ...


def _get_gateway_pool_for_pair():
    """Indirection so tests can monkeypatch the gateway pool without
    importing the heavy ``core.containers`` module at decorate time.
    """
    from core.containers import get_gateway_pool

    return get_gateway_pool()


_catalog_service: CatalogService | None = None


def get_catalog_service() -> CatalogService:
    """Returns the singleton CatalogService used by marketplace deploys."""
    global _catalog_service
    if _catalog_service is not None:
        return _catalog_service

    from core.containers import get_workspace
    from core.services.config_patcher import apply_deploy_aux_mutations
    from core.services.paperclip_openclaw_pairing import pair_with_openclaw

    _catalog_service = CatalogService(
        workspace=get_workspace(),
        apply_deploy_aux_mutations=apply_deploy_aux_mutations,
        pair_fn=pair_with_openclaw,
    )
    return _catalog_service
```

(Don't paste a `...` literal — copy the existing `list_deployed_for_user` body verbatim from the current file.)

- [ ] **Step 4: Run catalog_service tests**

```bash
cd apps/backend
uv run pytest tests/unit/services/test_catalog_service.py -v
```

Expected: all pass.

- [ ] **Step 5: Reset singleton between tests if needed**

If any pre-existing test relies on `get_catalog_service()` returning a fresh instance, add an autouse fixture that clears `_catalog_service` between tests:

```python
@pytest.fixture(autouse=True)
def _reset_catalog_singleton():
    import core.services.catalog_service as cs
    cs._catalog_service = None
    yield
    cs._catalog_service = None
```

Grep `grep -rn "get_catalog_service" apps/backend/tests/` to see whether this is needed.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/catalog_service.py \
        apps/backend/tests/unit/services/test_catalog_service.py
git commit -m "feat(catalog): RPC-pair agent entry, aux-merge plugins/mcp/skills"
```

---

## Task 4: Router — resolve container, thread ip + token; 503 when unreachable

**Files:**
- Modify: `apps/backend/routers/marketplace_listings.py:293-472` (specifically the `deploy_listing` handler)
- Test: `apps/backend/tests/unit/routers/test_marketplace_listings_deploy.py` (grep first: `grep -rln "deploy_listing\|/listings/.*/deploy" apps/backend/tests/`)

Between the Paperclip mint and `catalog.deploy_from_artifact`, resolve the container:

```python
from core.containers import get_ecs_manager

ecs = get_ecs_manager()
container, container_ip = await ecs.resolve_running_container(owner_id)
if container is None or container_ip is None:
    # Roll back the freshly-minted Paperclip agent before bailing.
    await delete_paperclip_for_marketplace(owner_id=owner_id, agent_id=new_agent_id)
    raise HTTPException(
        status_code=503,
        detail="container starting — retry in a moment",
    )
container_token = container["gateway_token"]
```

Then add `container_ip=container_ip, container_token=container_token` to the existing `catalog.deploy_from_artifact(...)` call.

The existing catch-all that rolls back Paperclip on `catalog.deploy_from_artifact` failure stays — that's now covering "RPC pair raised" and "aux merge raised" and "sidecar write raised" alike. No structural change to that block.

- [ ] **Step 1: Locate existing deploy_listing test file**

```bash
grep -rln "/listings/.*/deploy\|deploy_listing" apps/backend/tests/routers/ apps/backend/tests/unit/routers/
```

If a test file exists, read it to understand the fixtures already in place. If none exists, create `apps/backend/tests/unit/routers/test_marketplace_listings_deploy.py` patterned after a sibling file like `test_marketplace_listings.py` (FastAPI `TestClient`, mocked `auth.get_current_user`, monkeypatched boto3 / S3 / marketplace_service helpers).

- [ ] **Step 2: Write failing test — 503 when container is None**

```python
@pytest.mark.asyncio
async def test_deploy_returns_503_when_container_not_running(monkeypatch):
    """When ecs_manager.resolve_running_container returns (None, None)
    — buyer's container is stopped, never provisioned, or unhealthy —
    we 503 and roll back the freshly-minted Paperclip agent."""
    # Mock listing fetch, S3 fetch, Paperclip mint+delete, ecs_manager,
    # so the only path exercised is the container resolution branch.
    # ... (use the existing test file's fixtures as a template)
    # Assert: response.status_code == 503
    # Assert: delete_paperclip_for_marketplace was awaited with the
    #         agent_id returned by create_paperclip_for_marketplace.
    # Assert: catalog.deploy_from_artifact was NOT awaited.
```

(The implementer fills the fixture wiring once they've read the existing test file. Skeleton above states the assertions.)

- [ ] **Step 3: Run test to verify it fails**

```bash
cd apps/backend
uv run pytest tests/unit/routers/test_marketplace_listings_deploy.py -v
```

Expected: FAIL — current `deploy_listing` doesn't resolve the container.

- [ ] **Step 4: Implement the container resolution + 503 short-circuit**

In `apps/backend/routers/marketplace_listings.py`, just after the Paperclip mint try-block (around line 437, after `new_agent_id = paperclip_agent["id"]`), insert:

```python
from core.containers import get_ecs_manager

ecs = get_ecs_manager()
container, container_ip = await ecs.resolve_running_container(owner_id)
if container is None or container_ip is None:
    logger.info(
        "Marketplace deploy: container not reachable for owner=%s — rolling back paperclip agent %s",
        owner_id,
        new_agent_id,
    )
    try:
        await delete_paperclip_for_marketplace(owner_id=owner_id, agent_id=new_agent_id)
    except Exception:
        logger.exception(
            "Marketplace deploy: rollback delete_paperclip_for_marketplace failed for agent %s",
            new_agent_id,
        )
    raise HTTPException(
        status_code=503,
        detail="container starting — retry in a moment",
    )
container_token = container["gateway_token"]
```

Then extend the existing `catalog.deploy_from_artifact(...)` call to thread the two new kwargs:

```python
result = await catalog.deploy_from_artifact(
    owner_id=owner_id,
    slug=slug,
    manifest=manifest,
    slice_=slice_,
    tar_bytes=tar_bytes,
    new_agent_id=new_agent_id,
    container_ip=container_ip,
    container_token=container_token,
)
```

Move the `from core.containers import get_ecs_manager` to the top of the file with the other imports (don't leave it as an inline import).

- [ ] **Step 5: Run router test to verify it passes**

```bash
cd apps/backend
uv run pytest tests/unit/routers/test_marketplace_listings_deploy.py -v
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/routers/marketplace_listings.py \
        apps/backend/tests/unit/routers/test_marketplace_listings_deploy.py
git commit -m "feat(marketplace): router resolves container, threads ip+token to deploy"
```

---

## Task 5: End-to-end verification + push

- [ ] **Step 1: Run the full backend test suite**

```bash
cd apps/backend
uv run pytest tests/ -v
```

Expected: all pass. Pay attention to any test that was implicitly relying on `apply_deploy_mutation` being callable with an `agent_entry` positional arg — those must already be updated by Task 2 Step 5 or this run surfaces them.

- [ ] **Step 2: Run linters**

```bash
cd apps/backend
uv run ruff format --check .
uv run ruff check .
```

Expected: both clean. If `ruff format --check` complains, run `uv run ruff format .` and re-stage.

- [ ] **Step 3: Re-read all modified files with fresh eyes**

Open each file end-to-end:
- `apps/backend/core/services/paperclip_openclaw_pairing.py`
- `apps/backend/core/services/config_patcher.py`
- `apps/backend/core/services/catalog_service.py`
- `apps/backend/routers/marketplace_listings.py`

Check for:
- Stale comments / docstrings still referencing `apply_deploy_mutation` or the agent_entry append path.
- The "P2" Codex-rollback comment block in `catalog_service` — must be gone, since the rollback it described is no longer applicable.
- Any `from core.services.config_patcher import apply_deploy_mutation` left over.

- [ ] **Step 4: Open the PR**

```bash
git push -u origin worktree-marketplace-deploy-rpc-pair
gh pr create --title "fix(marketplace): RPC-pair agent entry to avoid chokidar reload race" \
  --body "$(cat <<'EOF'
## Summary

- Eliminates the chokidar reload race that left buyers with a stale chat sidebar (no Ray) or a UUID-shaped display name after hiring a marketplace listing.
- Routes the agent entry through OpenClaw's `agents.create` + `agents.update` RPCs so the container's in-memory state updates immediately, instead of waiting 1-30s for chokidar to pick up the openclaw.json file write on EFS.
- Plugins/mcp/skills/cron stay on the file-merge path (renamed to `apply_deploy_aux_mutations`) — they don't need to be live in-memory for the sidebar fix.
- Drops the now-dead `agent_entry` branch from `apply_deploy_mutation` (catalog_service was the sole caller; legacy flat-list migration code goes with it).

## Test plan

- [ ] `uv run pytest tests/` clean
- [ ] `uv run ruff format --check . && uv run ruff check .` clean
- [ ] Manually hire a listing in dev; verify the chat sidebar shows the friendly name immediately on deploy (no UUID, no missing row).
- [ ] Verify the buyer's openclaw.json on EFS gets the plugins/mcp/skills merged (file-merge path still wired).
- [ ] Stop the buyer's container, hit `/deploy` — expect 503 + Paperclip rollback (no orphan Paperclip agent on retry).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review check

Before reporting DONE, the implementer should verify:

- [ ] `pair_with_openclaw` test for `extra_update_fields=None` still passes — the chat-side caller in `_intercept_agents_create` is unchanged and must keep working.
- [ ] No file in the repo still imports `apply_deploy_mutation` — `grep -rn "apply_deploy_mutation" apps/backend/` returns zero hits (only `apply_deploy_aux_mutations`).
- [ ] `deploy_from_artifact`'s rollback no longer references `remove_from_openclaw_config_list` for the agents.list path.
- [ ] The order assertion test in `test_catalog_service.py` actually fails if you swap the `await self._pair_fn(...)` and `await self._apply_aux(...)` lines — proving the test is meaningful.
