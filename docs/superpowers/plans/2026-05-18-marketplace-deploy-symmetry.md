# Marketplace deploy symmetry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the marketplace deploy path into symmetry with `/chat`, `/teams`, and `paperclip_provisioning`: strip provider plugins from the slice so buyers' providers aren't overwritten, mint the agent_id via Paperclip first so OpenClaw and Paperclip share IDs, and rewrite MCP-server `args` paths so seller-keyed absolute paths get repointed at the buyer's new workspace dir.

**Architecture:** Two pre-process steps at publish time (`extract_agent_slice` drops provider plugins; `agent_export` rewrites MCP args to a `__AGENT_DIR__` sentinel), one accept-and-substitute step at deploy time (`catalog_service.deploy_from_artifact` takes a caller-supplied `new_agent_id` and substitutes the sentinel before deep-merge), and one new orchestration step (`routers/marketplace_listings.deploy_listing` mints the Paperclip agent first, threads the UUID into the catalog deploy, rolls back the Paperclip record on failure).

**Tech Stack:** Python 3.12, FastAPI, pytest, AWS (DDB, S3, ECS), Paperclip BFF (Aurora-backed; called via internal admin client).

**Spec reference:** `docs/superpowers/plans/2026-05-18-marketplace-deploy-symmetry-design.md`

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `apps/backend/core/services/catalog_slice.py` | Slice extractor — agent + plugins + tools + (optional) mcp/skills. Provider-plugin denylist lives here. | Modify |
| `apps/backend/core/services/agent_export.py` | Snapshot a seller's agent into a CatalogPackage. Owns the `__AGENT_DIR__` sentinel + applies the sentinel rewrite at export. | Modify |
| `apps/backend/core/services/catalog_service.py` | Buyer-side deploy. Accepts external `new_agent_id`; substitutes the sentinel back to the new agent_id before applying the slice. | Modify |
| `apps/backend/core/services/paperclip_agent_lifecycle.py` | Paperclip-first agent minting from non-WS callers. Has `create_paperclip_for_chat` already; we add `create_paperclip_for_marketplace` peer + an orphan-cleanup helper. | Modify |
| `apps/backend/routers/marketplace_listings.py` | `deploy_listing` endpoint orchestration: Paperclip mint → catalog deploy → rollback on failure. | Modify |
| `apps/backend/tests/unit/services/test_catalog_slice.py` | Pin provider-plugin strip + non-provider preserve. | Modify |
| `apps/backend/tests/unit/services/test_agent_export.py` | Pin MCP-args sentinel rewrite. | Modify |
| `apps/backend/tests/unit/services/test_catalog_service.py` | Pin external new_agent_id + sentinel substitution. | Modify (create if absent) |
| `apps/backend/tests/unit/routers/test_marketplace_listings.py` | Pin Paperclip-first orchestration + rollback + 503-on-Paperclip-failure. | Modify |

---

## Task 1: Provider-plugin denylist in `catalog_slice.py`

**Files:**
- Modify: `apps/backend/core/services/catalog_slice.py`
- Test: `apps/backend/tests/unit/services/test_catalog_slice.py`

- [ ] **Step 1: Locate the test file (or create it)**

Run: `ls apps/backend/tests/unit/services/test_catalog_slice.py 2>&1`

If the file exists, read it to learn the existing test idioms.
If it doesn't exist, create a stub with the standard header:

```python
"""Tests for catalog_slice."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services import catalog_slice  # noqa: E402
```

- [ ] **Step 2: Write the failing test for provider-strip**

Add to `apps/backend/tests/unit/services/test_catalog_slice.py`:

```python
def test_extract_agent_slice_strips_provider_plugins():
    """Provider plugins are user-config (the buyer picked a provider at
    signup), not agent-config. Shipping them in the slice would deep-merge
    over the buyer's provider setup and silently switch them off the path
    they chose. Strip at slice time."""
    openclaw_json = {
        "agents": {"list": [{"id": "agt_1", "name": "Demo"}]},
        "plugins": {
            "entries": {
                "amazon-bedrock": {"enabled": True, "config": {"region": "us-east-1"}},
                "chatgpt-oauth": {"enabled": False},
                "anthropic": {"enabled": True},
                "openai": {"enabled": True},
                "browser": {"enabled": True},
                "memory": {"enabled": True},
            }
        },
        "tools": {"profile": "default"},
    }
    slice_ = catalog_slice.extract_agent_slice(openclaw_json, "agt_1")
    entries = slice_["plugins"]["entries"]
    # Provider plugins gone.
    assert "amazon-bedrock" not in entries
    assert "chatgpt-oauth" not in entries
    assert "anthropic" not in entries
    assert "openai" not in entries
    # Non-provider plugins (genuinely agent-scoped) preserved.
    assert entries["browser"] == {"enabled": True}
    assert entries["memory"] == {"enabled": True}


def test_extract_agent_slice_handles_missing_plugins_block():
    """openclaw_json without a plugins block (legacy / fresh container)
    must still produce a valid slice — empty plugins dict, no crash."""
    openclaw_json = {"agents": {"list": [{"id": "agt_1", "name": "Demo"}]}}
    slice_ = catalog_slice.extract_agent_slice(openclaw_json, "agt_1")
    assert slice_["plugins"] == {}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_catalog_slice.py::test_extract_agent_slice_strips_provider_plugins -x --no-cov -q`
Expected: FAIL — current `extract_agent_slice` copies the full plugins block, so `amazon-bedrock` etc. are still in `entries`.

- [ ] **Step 4: Implement the denylist + filter**

In `apps/backend/core/services/catalog_slice.py`, near the top of the file (after the imports), add:

```python
# Provider plugins are user-config (the buyer picked a provider at signup),
# not agent-config. Shipping them in the slice would deep-merge over the
# buyer's provider setup and silently switch them off the path they chose
# (Bedrock / ChatGPT OAuth / BYOK Anthropic / BYOK OpenAI). Strip at slice
# time so the deploy can never override the buyer's provider plugin.
_PROVIDER_PLUGIN_NAMES = frozenset(
    {
        "amazon-bedrock",
        "chatgpt",
        "chatgpt-oauth",
        "anthropic",
        "openai",
        "google",
        "gemini",
        "ollama",
        "mistral",
    }
)


def _strip_provider_plugins(plugins: dict[str, Any]) -> dict[str, Any]:
    """Drop provider entries from a plugins block, leaving structural keys
    (``slots``, anything non-``entries``) and non-provider entries intact."""
    out = copy.deepcopy(plugins) if plugins else {}
    entries = out.get("entries") if isinstance(out, dict) else None
    if isinstance(entries, dict):
        out["entries"] = {k: v for k, v in entries.items() if k not in _PROVIDER_PLUGIN_NAMES}
    return out
```

Then in `extract_agent_slice`, change the slice initializer:

```python
slice_: dict[str, Any] = {
    "agent": strip_user_specific_fields(agent_entry),
    "plugins": _strip_provider_plugins(openclaw_json.get("plugins") or {}),
    "tools": copy.deepcopy(openclaw_json.get("tools") or {}),
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_catalog_slice.py -x --no-cov -q`
Expected: both new tests PASS; all existing tests in the file still PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/catalog_slice.py apps/backend/tests/unit/services/test_catalog_slice.py
git commit -m "feat(marketplace): strip provider plugins from agent slice

Provider plugins (amazon-bedrock, chatgpt-oauth, anthropic, openai, etc.)
are user-config — the buyer chose a provider at signup. Shipping them in
the slice deep-merges over the buyer's provider setup and silently moves
them off the path they picked. Strip the denylist at slice time so the
deploy can't override provider choices."
```

---

## Task 2: MCP `args` sentinel rewrite at export time

**Files:**
- Modify: `apps/backend/core/services/agent_export.py`
- Test: `apps/backend/tests/unit/services/test_agent_export.py`

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/services/test_agent_export.py`:

```python
def test_export_replaces_seller_agent_id_in_mcp_args_with_sentinel(fake_efs, tmp_path: Path):
    """Regression for marketplace-deploy-symmetry (spec 2026-05-18): the
    slice's mcp.servers.X.args paths reference the seller's workspace,
    e.g. ``/home/node/.openclaw/workspaces/{seller_agent_id}/mcp-resy/index.js``.
    On deploy that path doesn't exist in the buyer's container. Replace
    the seller-keyed segment with __AGENT_DIR__ at export so deploy can
    substitute the buyer's new agent_id cleanly.
    """
    seller_id, agent_id, agent_dir = fake_efs
    # Seller's openclaw.json already has an mcp servers entry — make it
    # carry an args path that points at the seller's workspaces dir.
    cfg_path = tmp_path / "users" / seller_id / "openclaw.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["mcp"] = {
        "servers": {
            "resy": {
                "command": "node",
                "args": [f"/home/node/.openclaw/workspaces/{agent_id}/mcp-resy/index.js"],
                "env": {"RESY_EMAIL": "", "RESY_PASSWORD": ""},
            }
        }
    }
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    pkg = agent_export.export_agent_from_efs(
        seller_id=seller_id,
        agent_id=agent_id,
        selected_mcps=["resy"],
    )

    args = pkg.openclaw_slice["mcp"]["servers"]["resy"]["args"]
    # Seller's agent_id MUST NOT appear in the slice — it's
    # buyer-non-portable.
    assert agent_id not in args[0], (
        f"seller agent_id leaked into slice mcp args: {args}"
    )
    # Sentinel must be substituted in.
    assert "__AGENT_DIR__" in args[0], (
        f"__AGENT_DIR__ sentinel missing from slice mcp args: {args}"
    )
    # Rest of the path (mcp-resy/index.js) preserved.
    assert args[0].endswith("/mcp-resy/index.js"), args[0]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export.py::test_export_replaces_seller_agent_id_in_mcp_args_with_sentinel -x --no-cov -q`
Expected: FAIL — current code doesn't rewrite args; the assertion `agent_id not in args[0]` fires.

- [ ] **Step 3: Add the sentinel constant + rewrite helper**

In `apps/backend/core/services/agent_export.py`, near the top of the file (after the existing constants):

```python
# Sentinel that replaces the seller's agent_id in MCP server args at
# export time. ``catalog_service.deploy_from_artifact`` substitutes this
# back to the buyer's freshly-minted agent_id before deep-merging the
# slice into their openclaw.json. Without this rewrite, the slice's args
# would still reference the seller's workspace path — which on the
# buyer's container doesn't exist (different agent_id).
_AGENT_DIR_SENTINEL = "__AGENT_DIR__"


def _rewrite_mcp_args_paths(slice_: dict, seller_agent_id: str) -> None:
    """In-place: replace ``workspaces/{seller_agent_id}`` segments in MCP
    server args with ``workspaces/{_AGENT_DIR_SENTINEL}``.

    Only ``args`` is rewritten; ``command`` and ``env`` don't carry
    workspace-relative paths in any seller config we've seen, but the
    function is defensive — adding more fields is a one-line change.
    """
    servers = ((slice_.get("mcp") or {}).get("servers")) or {}
    for entry in servers.values():
        if not isinstance(entry, dict):
            continue
        args = entry.get("args")
        if isinstance(args, list):
            entry["args"] = [
                a.replace(f"workspaces/{seller_agent_id}", f"workspaces/{_AGENT_DIR_SENTINEL}")
                if isinstance(a, str)
                else a
                for a in args
            ]
```

Then in `export_agent_from_efs`, after the `slice_ = extract_agent_slice(...)` line (around line 303-313 today), before the `slice_["cron_jobs"] = ...` line:

```python
    _rewrite_mcp_args_paths(slice_, seller_agent_id=agent_id)
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export.py::test_export_replaces_seller_agent_id_in_mcp_args_with_sentinel -x --no-cov -q`
Expected: PASS.

- [ ] **Step 5: Run the full file to confirm no regressions**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export.py -x --no-cov -q`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/agent_export.py apps/backend/tests/unit/services/test_agent_export.py
git commit -m "feat(marketplace): rewrite seller paths in mcp args to __AGENT_DIR__ sentinel

The slice's mcp.servers.X.args paths reference the seller's workspace
(e.g. workspaces/abc-123/mcp-resy/index.js). On deploy that path doesn't
exist in the buyer's container — different agent_id. Replace the
seller-keyed segment with the __AGENT_DIR__ sentinel at export time so
catalog_service.deploy_from_artifact can substitute the buyer's new
agent_id cleanly during merge."
```

---

## Task 3: `catalog_service.deploy_from_artifact` accepts external `new_agent_id` + substitutes the sentinel

**Files:**
- Modify: `apps/backend/core/services/catalog_service.py`
- Test: `apps/backend/tests/unit/services/test_catalog_service.py`

- [ ] **Step 1: Locate or create the test file**

Run: `ls apps/backend/tests/unit/services/test_catalog_service.py 2>&1`

If absent, scaffold:

```python
"""Tests for catalog_service."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import pytest  # noqa: E402

from core.services.catalog_service import CatalogService  # noqa: E402
```

- [ ] **Step 2: Write the failing tests**

Add to `apps/backend/tests/unit/services/test_catalog_service.py`:

```python
def _make_service():
    """Construct a CatalogService with a fully mocked workspace + deploy
    mutation, so tests focus on the orchestration logic in
    ``deploy_from_artifact`` and not the EFS / config-patcher side effects.
    """
    workspace = MagicMock()
    workspace.extract_tarball_to_workspace = MagicMock(return_value=None)
    workspace.write_template_sidecar = MagicMock(return_value=None)
    workspace.read_cron_jobs = MagicMock(return_value=[])
    workspace.cleanup_agent_dirs = MagicMock(return_value=None)
    apply_deploy = AsyncMock(return_value=None)
    svc = CatalogService(workspace=workspace, apply_deploy_mutation=apply_deploy)
    return svc, workspace, apply_deploy


@pytest.mark.asyncio
async def test_deploy_from_artifact_uses_supplied_new_agent_id():
    """When the caller supplies new_agent_id, that id MUST be used for the
    workspace dir + the agent entry deep-merged into openclaw.json. The
    Paperclip-first agent-id flow depends on this — Paperclip mints the
    UUID; catalog_service must respect it instead of generating its own.
    """
    svc, workspace, apply_deploy = _make_service()
    result = await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_={"agent": {"name": "Demo"}, "plugins": {}, "tools": {}, "cron_jobs": []},
        tar_bytes=b"fake-tar",
        new_agent_id="paperclip-uuid-1234",
    )
    assert result["agent_id"] == "paperclip-uuid-1234"
    # extract_tarball_to_workspace was called with the supplied id.
    workspace.extract_tarball_to_workspace.assert_called_once()
    extract_kwargs = workspace.extract_tarball_to_workspace.call_args.kwargs or {}
    extract_args = workspace.extract_tarball_to_workspace.call_args.args or ()
    # The implementation may pass via args or kwargs — accept either.
    assert "paperclip-uuid-1234" in (extract_kwargs.get("agent_id"), *extract_args)
    # apply_deploy_mutation called with the supplied id in the agent_entry.
    apply_deploy.assert_awaited_once()
    agent_entry = apply_deploy.await_args.args[1]
    assert agent_entry["id"] == "paperclip-uuid-1234"


@pytest.mark.asyncio
async def test_deploy_from_artifact_substitutes_agent_dir_sentinel_in_mcp_args():
    """Spec change #3: at export time, agent_export replaces the seller's
    agent_id in mcp.servers.X.args with __AGENT_DIR__. At deploy time,
    catalog_service must substitute it back to the buyer's new_agent_id
    before applying the slice. Otherwise the MCP starts up pointing at
    a workspace that doesn't exist on the buyer's container.
    """
    svc, _workspace, apply_deploy = _make_service()
    slice_ = {
        "agent": {"name": "Demo"},
        "plugins": {},
        "tools": {},
        "cron_jobs": [],
        "mcp": {
            "servers": {
                "resy": {
                    "command": "node",
                    "args": [
                        "/home/node/.openclaw/workspaces/__AGENT_DIR__/mcp-resy/index.js"
                    ],
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
    )
    # apply_deploy_mutation receives mcp_patch with the sentinel substituted.
    apply_deploy.assert_awaited_once()
    mcp_patch = apply_deploy.await_args.kwargs.get("mcp_patch")
    assert mcp_patch is not None
    args = mcp_patch["servers"]["resy"]["args"]
    assert "__AGENT_DIR__" not in args[0], args[0]
    assert "agent_xyz" in args[0], args[0]
    assert args[0].endswith("/mcp-resy/index.js"), args[0]


@pytest.mark.asyncio
async def test_deploy_from_artifact_generates_uuid_when_new_agent_id_omitted():
    """Backwards-compat: callers that don't pass new_agent_id still get a
    locally-generated UUID. Direct catalog deploys (tests, future paths)
    should keep working.
    """
    svc, _workspace, apply_deploy = _make_service()
    result = await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_={"agent": {"name": "Demo"}, "plugins": {}, "tools": {}, "cron_jobs": []},
        tar_bytes=b"fake-tar",
    )
    assert result["agent_id"].startswith("agent_")
    assert len(result["agent_id"]) > len("agent_")
```

- [ ] **Step 3: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_catalog_service.py -x --no-cov -q`
Expected: FAIL — `deploy_from_artifact` doesn't accept a `new_agent_id` kwarg, doesn't substitute the sentinel.

- [ ] **Step 4: Read the current `deploy_from_artifact` body**

Read: `apps/backend/core/services/catalog_service.py` — focus on the `deploy_from_artifact` function (lines ~100-170 today). Understand the existing flow:
- `new_agent_id = f"agent_{uuid.uuid4().hex[:12]}"` — local mint
- `agent_entry["id"] = new_agent_id` etc.
- `mcp_patch = copy.deepcopy(slice_.get("mcp"))` — passed to `_apply_deploy`

- [ ] **Step 5: Add the sentinel constant + the kwarg + substitution**

Near the top of `apps/backend/core/services/catalog_service.py` (after imports):

```python
# Mirrors agent_export._AGENT_DIR_SENTINEL. Duplicated rather than
# imported because catalog_service is on the deploy side and shouldn't
# need to depend on agent_export's symbol surface. If a third caller
# emerges, move to a shared constants module.
_AGENT_DIR_SENTINEL = "__AGENT_DIR__"


def _substitute_agent_dir_sentinel(mcp_patch: dict | None, new_agent_id: str) -> dict | None:
    """In-place substitute ``__AGENT_DIR__`` → ``new_agent_id`` in any
    ``args`` entry of every mcp.servers.X dict. Returns the patch
    unchanged if it's None or non-dict.
    """
    if not isinstance(mcp_patch, dict):
        return mcp_patch
    servers = mcp_patch.get("servers")
    if not isinstance(servers, dict):
        return mcp_patch
    for entry in servers.values():
        if not isinstance(entry, dict):
            continue
        args = entry.get("args")
        if isinstance(args, list):
            entry["args"] = [
                a.replace(_AGENT_DIR_SENTINEL, new_agent_id) if isinstance(a, str) else a
                for a in args
            ]
    return mcp_patch
```

Modify `deploy_from_artifact` signature + body. Find the `new_agent_id = ...` line and the `mcp_patch = ...` line. Change:

```python
async def deploy_from_artifact(
    self,
    *,
    owner_id: str,
    slug: str,
    manifest: dict,
    slice_: dict,
    tar_bytes: bytes,
    new_agent_id: str | None = None,
) -> dict:
    """[existing docstring]

    Args:
        ...
        new_agent_id: When supplied, the buyer-side OpenClaw agent_id is
            pinned to this value. Marketplace deploys pass Paperclip's
            freshly-minted UUID so the two systems share the same id.
            Defaults to a locally-generated ``agent_<hex>`` UUID for
            backwards-compatible direct callers.
    """
    if new_agent_id is None:
        new_agent_id = f"agent_{uuid.uuid4().hex[:12]}"
    # ... existing code that uses new_agent_id ...
```

And where `mcp_patch` is computed (look for the line containing `mcp_patch = copy.deepcopy(slice_.get("mcp"))` or similar), substitute the sentinel:

```python
mcp_patch = copy.deepcopy(slice_.get("mcp")) or None
mcp_patch = _substitute_agent_dir_sentinel(mcp_patch, new_agent_id)
```

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_catalog_service.py -x --no-cov -q`
Expected: 3 PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/catalog_service.py apps/backend/tests/unit/services/test_catalog_service.py
git commit -m "feat(marketplace): accept external new_agent_id + substitute __AGENT_DIR__ sentinel

Two changes that unlock Paperclip-first agent minting and unbreak MCP
deploys for non-self-hire buyers:

1. deploy_from_artifact accepts a caller-supplied new_agent_id. The
   marketplace router will pass Paperclip's freshly-minted UUID so
   Paperclip and OpenClaw share the same agent id. Callers that omit
   it keep getting a locally-generated agent_<hex> uuid (no regression
   on the direct-deploy path or tests).
2. Substitute the __AGENT_DIR__ sentinel in mcp_patch.servers.X.args
   before deep-merging. Pairs with agent_export's rewrite at publish
   time — the seller's workspace path becomes the buyer's new agent
   dir on every install."
```

---

## Task 4: `create_paperclip_for_marketplace` helper in `paperclip_agent_lifecycle.py`

**Files:**
- Modify: `apps/backend/core/services/paperclip_agent_lifecycle.py`
- Test: covered by the router tests in Task 5 (the helper is a thin wrapper around already-tested `PaperclipAdminClient.create_agent`; adding unit tests here would mostly re-test the admin client)

- [ ] **Step 1: Read the existing `create_paperclip_for_chat` for the template**

Read: `apps/backend/core/services/paperclip_agent_lifecycle.py` — lines ~36-100. Understand the structure:
- Resolve `company_id` via PaperclipRepo
- Open httpx.AsyncClient against Paperclip
- Get admin session cookie
- Mint service token for `owner_id`
- Synthesize adapter config (gateway URL, service token, user_id)
- Call `admin_client.create_agent`
- Return the agent dict

- [ ] **Step 2: Add `create_paperclip_for_marketplace`**

Append to `apps/backend/core/services/paperclip_agent_lifecycle.py`:

```python
async def create_paperclip_for_marketplace(
    *,
    buyer_user_id: str,
    owner_id: str,
    name: str,
    role: str | None,
) -> dict:
    """Create a Paperclip agent for a marketplace install.

    Mirror of ``create_paperclip_for_chat`` for the storefront
    ``/marketplace/listings/{slug}/deploy`` flow. Returns the Paperclip
    agent dict (caller threads ``["id"]`` into ``catalog_service.
    deploy_from_artifact`` as ``new_agent_id`` so Paperclip and
    OpenClaw share the same UUID).

    ``buyer_user_id``: the authenticated buyer triggering Hire (used as
        adapterConfig.sessionKey, the manual-wake fallback).
    ``owner_id``: container owner (org_id for orgs, user_id for personal);
        used to look up the shared Paperclip company AND mint the service
        token. Same owner-vs-member split as the chat path.
    ``name``: agent display name — taken from the slice's agent entry,
        falls back to the listing's slug.
    ``role``: Paperclip role string (e.g. "general"). Defaults to "general"
        when None.
    """
    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await _resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        raise RuntimeError(
            f"owner {owner_id} has no Paperclip company — marketplace deploy blocked. "
            "Re-run org provisioning."
        )

    http = httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=15.0,
    )
    try:
        admin_client = PaperclipAdminClient(http_client=http)
        admin_cookie = await get_admin_session_cookie(http)
        adapter_config = synthesize_openclaw_adapter(
            gateway_url=_ws_gateway_url(settings.ENVIRONMENT or ""),
            service_token=service_token.mint(owner_id),
            user_id=buyer_user_id,
        )
        return await admin_client.create_agent(
            company_id=company_id,
            name=name,
            role=role or "general",
            adapter_type=OPENCLAW_GATEWAY_TYPE,
            adapter_config=adapter_config,
            session_cookie=admin_cookie,
        )
    finally:
        await http.aclose()


async def delete_paperclip_for_marketplace(
    *,
    owner_id: str,
    agent_id: str,
) -> None:
    """Cleanup partner to ``create_paperclip_for_marketplace`` for the
    case where Paperclip create succeeded but the catalog deploy failed
    afterwards. Idempotent — Paperclip's delete_agent swallows 404, so
    a missing or already-cleaned agent is a no-op.
    """
    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await _resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        logger.info(
            "delete_paperclip_for_marketplace: owner %s has no Paperclip company — skip",
            owner_id,
        )
        return
    http = httpx.AsyncClient(
        base_url=settings.PAPERCLIP_INTERNAL_URL,
        timeout=15.0,
    )
    try:
        admin_client = PaperclipAdminClient(http_client=http)
        admin_cookie = await get_admin_session_cookie(http)
        await admin_client.delete_agent(
            session_cookie=admin_cookie,
            agent_id=agent_id,
        )
    finally:
        await http.aclose()
```

- [ ] **Step 3: Smoke test that the file imports cleanly**

Run: `cd apps/backend && uv run python -c "from core.services.paperclip_agent_lifecycle import create_paperclip_for_marketplace, delete_paperclip_for_marketplace; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add apps/backend/core/services/paperclip_agent_lifecycle.py
git commit -m "feat(paperclip): add create/delete helpers for marketplace deploy

Peers of create_paperclip_for_chat for the storefront's
/marketplace/listings/{slug}/deploy flow. create_paperclip_for_marketplace
mints a Paperclip agent the deploy router can thread into
catalog_service.deploy_from_artifact as new_agent_id;
delete_paperclip_for_marketplace is the rollback path for the case
where catalog deploy fails after Paperclip create succeeded
(404-tolerant, same idempotency posture as the chat-side delete)."
```

---

## Task 5: Orchestrate Paperclip-first deploy in `marketplace_listings.deploy_listing`

**Files:**
- Modify: `apps/backend/routers/marketplace_listings.py`
- Test: `apps/backend/tests/unit/routers/test_marketplace_listings.py`

- [ ] **Step 1: Write the failing tests**

Add to `apps/backend/tests/unit/routers/test_marketplace_listings.py`:

```python
@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_deploy_mints_paperclip_agent_first_and_threads_uuid_to_catalog(
    mock_get_by_slug,
    mock_get_catalog,
    mock_boto,
    mock_create_pc,
    mock_delete_pc,
    client,
):
    """Spec change #4: marketplace deploy must call Paperclip's
    create_agent BEFORE invoking the catalog service, and pass Paperclip's
    UUID as ``new_agent_id`` so OpenClaw and Paperclip share the agent id.
    """
    mock_get_by_slug.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "paperclip-uuid-1234", "name": "Demo"}
    real_slice = {
        "agent": {"name": "Demo", "role": "general"},
        "plugins": {},
        "tools": {},
        "cron_jobs": [],
    }
    mock_boto.return_value = _stub_s3_get_object(_slice=real_slice)
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(
        return_value={
            "slug": "demo-agent",
            "version": 1,
            "agent_id": "paperclip-uuid-1234",
            "name": "Demo",
            "skills_added": [],
            "plugins_enabled": [],
            "cron_jobs_added": 0,
            "config_registered": True,
        }
    )
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/demo-agent/deploy")
    assert resp.status_code == 200, resp.text

    # Paperclip mint was called first with the slice's agent name + role.
    mock_create_pc.assert_awaited_once()
    pc_kwargs = mock_create_pc.await_args.kwargs
    assert pc_kwargs["name"] == "Demo"
    assert pc_kwargs["role"] == "general"

    # Catalog deploy received the Paperclip-minted UUID.
    catalog.deploy_from_artifact.assert_awaited_once()
    deploy_kwargs = catalog.deploy_from_artifact.await_args.kwargs
    assert deploy_kwargs["new_agent_id"] == "paperclip-uuid-1234"

    # Rollback NOT called on success.
    mock_delete_pc.assert_not_awaited()


@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_deploy_rolls_back_paperclip_on_catalog_failure(
    mock_get_by_slug,
    mock_get_catalog,
    mock_boto,
    mock_create_pc,
    mock_delete_pc,
    client,
):
    """If Paperclip create succeeds but catalog deploy fails (e.g.
    apply_deploy_mutation raises mid-merge), delete the orphan Paperclip
    agent so a retry doesn't leave a phantom Paperclip record.
    """
    mock_get_by_slug.return_value = _published_listing(price_cents=0)
    mock_create_pc.return_value = {"id": "paperclip-uuid-1234", "name": "Demo"}
    mock_boto.return_value = _stub_s3_get_object(
        _slice={"agent": {"name": "Demo"}, "plugins": {}, "tools": {}, "cron_jobs": []}
    )
    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=RuntimeError("apply_deploy boom"))
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/demo-agent/deploy")
    assert resp.status_code == 500, resp.text
    # Rollback was called with the orphan id.
    mock_delete_pc.assert_awaited_once()
    delete_kwargs = mock_delete_pc.await_args.kwargs
    assert delete_kwargs["agent_id"] == "paperclip-uuid-1234"


@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_deploy_503s_when_paperclip_unavailable(
    mock_get_by_slug,
    mock_get_catalog,
    mock_boto,
    mock_create_pc,
    client,
):
    """If Paperclip's admin API fails, surface 503 immediately and never
    touch the catalog. No orphan Paperclip record to clean up because
    no Paperclip agent was created.
    """
    mock_get_by_slug.return_value = _published_listing(price_cents=0)
    mock_boto.return_value = _stub_s3_get_object(
        _slice={"agent": {"name": "Demo"}, "plugins": {}, "tools": {}, "cron_jobs": []}
    )
    mock_create_pc.side_effect = RuntimeError("paperclip 502")
    catalog = MagicMock()
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/demo-agent/deploy")
    assert resp.status_code == 503, resp.text
    # Catalog never invoked.
    catalog.deploy_from_artifact.assert_not_called()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_marketplace_listings.py::test_deploy_mints_paperclip_agent_first_and_threads_uuid_to_catalog -x --no-cov -q`
Expected: FAIL — the router doesn't import `create_paperclip_for_marketplace`, the patch path doesn't resolve.

- [ ] **Step 3: Modify the router**

In `apps/backend/routers/marketplace_listings.py`, add the import near the top:

```python
from core.services.paperclip_agent_lifecycle import (
    create_paperclip_for_marketplace,
    delete_paperclip_for_marketplace,
)
```

Modify `deploy_listing`. Find the block where `slice_` is fetched from S3 and `catalog.deploy_from_artifact(...)` is called. Replace the `try: result = await catalog.deploy_from_artifact(...)` section with:

```python
    # Paperclip-first agent mint. Paperclip's create_agent returns an
    # agent UUID that we thread through to catalog_service as
    # new_agent_id, so OpenClaw and Paperclip share the same id. Mirror
    # of the /chat agents.create interception in websocket_chat.py and
    # the /teams create_agent route.
    agent_name = (slice_.get("agent") or {}).get("name") or listing.get("name") or slug
    agent_role = (slice_.get("agent") or {}).get("role") or "general"
    try:
        paperclip_agent = await create_paperclip_for_marketplace(
            buyer_user_id=auth.user_id,
            owner_id=owner_id,
            name=agent_name,
            role=agent_role,
        )
    except Exception as exc:
        logger.exception(
            "Marketplace deploy: Paperclip create failed for owner=%s listing=%s",
            owner_id,
            listing.get("listing_id"),
        )
        raise HTTPException(
            status_code=503,
            detail="paperclip unavailable — retry the install in a moment",
        ) from exc

    new_agent_id = paperclip_agent["id"]

    try:
        result = await catalog.deploy_from_artifact(
            owner_id=owner_id,
            slug=slug,
            manifest=manifest,
            slice_=slice_,
            tar_bytes=tar_bytes,
            new_agent_id=new_agent_id,
        )
    except Exception as exc:
        # Catalog failed after Paperclip create succeeded — roll back
        # the orphan Paperclip agent. Idempotent + 404-tolerant on the
        # Paperclip side, so a retry doesn't accumulate phantom records.
        logger.exception(
            "Marketplace deploy: catalog.deploy_from_artifact failed for owner=%s slug=%s — rolling back paperclip agent %s",
            owner_id,
            slug,
            new_agent_id,
        )
        try:
            await delete_paperclip_for_marketplace(
                owner_id=owner_id, agent_id=new_agent_id
            )
        except Exception:
            # Roll-back is best-effort; the original exception is what
            # the caller needs to see.
            logger.exception(
                "Marketplace deploy: rollback delete_paperclip_for_marketplace failed for agent %s",
                new_agent_id,
            )
        raise HTTPException(
            status_code=500,
            detail="deploy failed",
        ) from exc

    return {"agent_uuid": result["agent_id"], **result}
```

Remove the OLD `try: result = await catalog.deploy_from_artifact(...) except Exception as exc: ...` block that's now superseded.

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_marketplace_listings.py -x --no-cov -q`
Expected: 3 new tests PASS; all existing tests in the file PASS.

- [ ] **Step 5: Run the broader sweep**

Run: `cd apps/backend && uv run pytest tests/unit/services/ tests/unit/routers/test_marketplace_listings.py tests/unit/routers/test_marketplace_admin.py -x --no-cov -q`
Expected: all PASS (any prior breaks would have surfaced by now).

- [ ] **Step 6: Commit**

```bash
git add apps/backend/routers/marketplace_listings.py apps/backend/tests/unit/routers/test_marketplace_listings.py
git commit -m "feat(marketplace): Paperclip-first agent mint on deploy

The marketplace deploy path now mints the agent UUID via Paperclip
BEFORE invoking the catalog service, threading the same UUID through
catalog_service.deploy_from_artifact's new_agent_id kwarg. Result:
Paperclip and OpenClaw share the agent id, the hired agent shows up
in Teams Inbox alongside the chat sidebar, and the deploy path is
symmetric with /chat agents.create + /teams create_agent +
paperclip_provisioning.provision_org.

If Paperclip create fails, surface 503 immediately (no catalog
side-effect). If catalog deploy fails after Paperclip create succeeds,
roll back the orphan Paperclip agent best-effort so a retry doesn't
accumulate phantom records."
```

---

## Task 6: Verify + open PR

**Files:** none

- [ ] **Step 1: Run the full backend test suite**

Run: `cd apps/backend && uv run pytest tests/unit/ -x --no-cov -q`
Expected: all PASS.

- [ ] **Step 2: Run ruff format + lint**

Run:
```bash
cd apps/backend && uv run ruff check . && uv run ruff format --check .
```
Expected: `All checks passed!` from both. If `format --check` reports drift, run `uv run ruff format .` and amend the most recent commit (or stage a follow-up).

- [ ] **Step 3: Sanity-grep that the seller_id bypass still works alongside the new flow**

Run:
```bash
grep -n "is_seller\|seller_id == auth.user_id" apps/backend/routers/marketplace_listings.py
```
Expected: the bypass added in PR #712 still present. The Paperclip mint should fire for seller self-hire too (sellers also belong to a Paperclip company; the agent record + adapter all make sense for self-hire).

- [ ] **Step 4: Push the branch + open PR**

```bash
git push -u origin worktree-marketplace-deploy-symmetry
gh pr create --title "feat(marketplace): provider strip + Paperclip-first agent ID + MCP path rewrite" --body "$(cat <<'EOF'
## Summary
Brings the marketplace deploy path into symmetry with the other three agent-creation paths (`/chat agents.create`, `/teams create_agent`, `paperclip_provisioning`). Three independent defects in one workflow, fixed together because the data flows through the same slice / deploy pair:

1. **Provider isolation.** `catalog_slice.extract_agent_slice` now strips provider plugins (`amazon-bedrock`, `chatgpt-oauth`, `anthropic`, `openai`, etc.) from the slice. The seller's provider stack can no longer override the buyer's.
2. **Paperclip-first agent ID.** `routers/marketplace_listings.deploy_listing` mints the Paperclip agent first; the resulting UUID is threaded through `catalog_service.deploy_from_artifact` as `new_agent_id` so OpenClaw and Paperclip share the agent id. Hired agents now appear in Teams Inbox alongside the chat sidebar.
3. **MCP path rewrite.** `agent_export` replaces the seller's workspace path in `mcp.servers.X.args` with a `__AGENT_DIR__` sentinel at publish time; `catalog_service.deploy_from_artifact` substitutes the sentinel back to the buyer's new agent_id on install. MCPs now point at a directory that actually exists in the buyer's container.

Failure paths covered: Paperclip create fails → 503, no catalog touch; catalog deploy fails post-Paperclip-create → orphan Paperclip agent rolled back best-effort.

Design + plan: `docs/superpowers/specs/2026-05-18-marketplace-deploy-symmetry-design.md`, `docs/superpowers/plans/2026-05-18-marketplace-deploy-symmetry.md`.

## Test plan
- [x] Unit suite green.
- [ ] Manual smoke on dev once deployed:
  - Republish Ray (picks up sentinel rewrite + provider-stripped slice).
  - Hire from a fresh dev Clerk account.
  - Confirm Ray in chat sidebar (OpenClaw) + Teams Inbox (Paperclip).
  - Confirm buyer's provider unchanged after install.
  - Confirm Resy MCP starts (path now points at buyer's workspace).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-review

**Spec coverage:**
- Provider denylist (spec §1) → Task 1 ✓
- MCP sentinel rewrite at export (spec §2) → Task 2 ✓
- External new_agent_id + sentinel substitution at deploy (spec §3) → Task 3 ✓
- `create_paperclip_for_marketplace` + cleanup helper (spec §4 prefix) → Task 4 ✓
- Router orchestration + rollback (spec §4 main) → Task 5 ✓
- Frontend changes (spec §5) → no changes needed; `useHire.ts` already has the seller bypass from PR #712 and consumes `agent_id` from the response, which now matches Paperclip's id.

**Placeholder scan:** none.

**Type consistency:** `new_agent_id` named consistently in Tasks 3 + 5. `_AGENT_DIR_SENTINEL` value `"__AGENT_DIR__"` defined identically in agent_export (Task 2) and catalog_service (Task 3). `create_paperclip_for_marketplace` / `delete_paperclip_for_marketplace` signatures used identically in Tasks 4 + 5.
