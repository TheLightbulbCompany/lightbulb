# Marketplace deploy: provider isolation, Paperclip sync, MCP path rewrite

**Date:** 2026-05-18
**Scope:** backend (catalog_slice, catalog_service, agent_export, marketplace_listings, paperclip) + a small frontend follow-up
**Discovered while:** testing Ray the Resy Bot's full publish → deploy cycle on dev (PR #710–#712 series)

## Problem

The marketplace deploy path (`POST /api/v1/marketplace/listings/{slug}/deploy` → `catalog_service.deploy_from_artifact`) was built as a stand-alone install pipeline. It diverges from the three other agent-creation paths (`/chat agents.create`, `/teams create_agent`, `paperclip_provisioning.provision_org`) in three concrete ways, each surfacing as a real defect:

1. **Provider overwrite.** `catalog_slice.extract_agent_slice` copies the seller's *entire* `plugins.entries` block. On deploy, that deep-merges into the buyer's `openclaw.json` and silently overrides whatever provider (Bedrock / ChatGPT OAuth / BYOK Anthropic / BYOK OpenAI) the buyer chose at signup. The seller's provider stack is forced onto the buyer.
2. **No Paperclip sync.** The three other paths use Paperclip-first agent-id minting (Paperclip's UUID becomes the OpenClaw agent_id). The marketplace deploy mints its own UUID inside `catalog_service.deploy_from_artifact:118` and never calls Paperclip. Result: a buyer's deployed agent shows up in OpenClaw (chat sidebar) but is invisible in Teams Inbox.
3. **Hardcoded seller paths in MCP `args`.** When the seller's MCP server is bundled, the slice carries `mcp.servers.X.args` like `/home/node/.openclaw/workspaces/{seller_agent_id}/mcp-resy/index.js`. On deploy, the new agent_id is different, so the path points at a directory that doesn't exist in the buyer's container. The MCP fails to start.

All three are independently confirmed against Ray on dev (PRs #710/#711 verified the export side; the three issues here all sit on the deploy side).

## Non-goals

- The "already-installed listing" UI affordance (gating Hire on the buyer's `.template` sidecars) is a separate UX follow-up. Out of scope.
- Hire-twice de-duplication / cleanup of orphan installs. Out of scope.
- Refactoring `apply_deploy_mutation`'s deep-merge semantics. The semantics are correct for non-provider plugins — only the *input* to the merge needs filtering.
- Storefront-side "you already have this" warning. Out of scope.

## Architecture

### Existing flow (broken)

```
buyer hits Hire
  └─ POST /marketplace/listings/{slug}/deploy
      └─ catalog_service.deploy_from_artifact(slug, manifest, slice_, tar_bytes)
          ├─ new_agent_id = "agent_" + uuid.hex[:12]              # local UUID
          ├─ untar workspace.tar.gz → workspaces/{new_agent_id}/
          └─ apply_deploy_mutation(owner_id, agent_entry, plugins_patch, mcp_patch, skills_patch)
              ├─ deep-merge plugins_patch (INCLUDING providers — bug #1)
              └─ deep-merge mcp_patch (INCLUDING seller's hardcoded paths — bug #3)

[no call to Paperclip — bug #2]
```

### Target flow

```
buyer hits Hire
  └─ POST /marketplace/listings/{slug}/deploy
      ├─ Paperclip-first agent mint:
      │   └─ admin_client.create_agent(company_id, name, role, adapter_type, adapter_config)
      │      └─ paperclip_agent["id"] = <Paperclip-minted UUID>
      ├─ catalog_service.deploy_from_artifact(..., new_agent_id=paperclip_agent["id"])
      │   ├─ untar workspace.tar.gz → workspaces/{paperclip_agent.id}/
      │   ├─ rewrite mcp_patch.servers.*.args: __AGENT_DIR__ → paperclip_agent.id
      │   └─ apply_deploy_mutation(..., plugins_patch=<providers stripped>)
      └─ on catalog failure: best-effort admin_client.delete_agent(paperclip_agent.id)

[slice was already pre-processed at publish time:
  - agent_export replaces seller's agent_id in MCP args with __AGENT_DIR__ sentinel
  - extract_agent_slice strips provider plugins from plugins.entries]
```

## Component changes

### 1. `catalog_slice.py` — provider-plugin filter

Define a denylist and apply it at slice extraction:

```python
# Provider plugins are user-config, not agent-config. Shipping them in the
# slice would deep-merge into the buyer's openclaw.json and silently
# override the provider they chose at signup (Bedrock / ChatGPT OAuth /
# BYOK). Strip at slice time so the deploy can never put a buyer on a
# provider they didn't pick.
_PROVIDER_PLUGIN_NAMES = frozenset({
    "amazon-bedrock",
    "chatgpt",
    "chatgpt-oauth",
    "anthropic",
    "openai",
    "google",
    "gemini",
    "ollama",
    "mistral",
})

def _strip_provider_plugins(plugins: dict) -> dict:
    out = copy.deepcopy(plugins)
    entries = out.get("entries") if isinstance(out, dict) else None
    if isinstance(entries, dict):
        out["entries"] = {k: v for k, v in entries.items() if k not in _PROVIDER_PLUGIN_NAMES}
    return out
```

Wire into `extract_agent_slice`:

```python
slice_: dict[str, Any] = {
    "agent": strip_user_specific_fields(agent_entry),
    "plugins": _strip_provider_plugins(openclaw_json.get("plugins") or {}),
    "tools": copy.deepcopy(openclaw_json.get("tools") or {}),
}
```

`tools` block is **not** filtered. Tools config is genuinely agent-scoped (the seller's deny-list / profile reflects what the *agent* should and shouldn't do, not which provider the buyer uses).

### 2. `agent_export.py` — MCP args sentinel replacement at export time

Before serializing the slice to S3, walk `slice_["mcp"]["servers"]` and rewrite any `args` entry that contains a path including the seller's `agent_id`:

```python
_AGENT_DIR_SENTINEL = "__AGENT_DIR__"

def _rewrite_mcp_args_paths(slice_: dict, seller_agent_id: str) -> None:
    """In-place rewrite of seller-absolute paths to the buyer-portable sentinel.

    Slices carry `mcp.servers.X.args` with paths like
    `/home/node/.openclaw/workspaces/{seller_agent_id}/mcp-resy/index.js`.
    On deploy, the new agent_id is different. Replace the seller-keyed
    segment with __AGENT_DIR__ at export time so deploy can substitute it
    cleanly without parsing UUIDs out of args.
    """
    servers = ((slice_.get("mcp") or {}).get("servers")) or {}
    for entry in servers.values():
        if not isinstance(entry, dict):
            continue
        args = entry.get("args")
        if isinstance(args, list):
            entry["args"] = [
                a.replace(f"workspaces/{seller_agent_id}", f"workspaces/{_AGENT_DIR_SENTINEL}")
                if isinstance(a, str) else a
                for a in args
            ]
        # env / command don't normally carry paths, but check defensively.
```

Called from `export_agent_from_efs` after `extract_agent_slice` returns. Sentinel is defined in `core/services/agent_export.py` and re-exported / re-defined in `catalog_service.py` (or moved to a shared `catalog_constants.py`). Constant duplication is acceptable here — the alternative (shared module) is one more file for two lines.

### 3. `catalog_service.deploy_from_artifact` — accept external `new_agent_id` + substitute sentinel

```python
async def deploy_from_artifact(
    self,
    *,
    owner_id: str,
    slug: str,
    manifest: dict,
    slice_: dict,
    tar_bytes: bytes,
    new_agent_id: str | None = None,  # NEW
) -> dict:
    # Caller can supply a Paperclip-minted UUID. When None (e.g. tests, or
    # a future direct-deploy path), fall back to the existing generator.
    if new_agent_id is None:
        new_agent_id = f"agent_{uuid.uuid4().hex[:12]}"

    # ... existing untar + agent_entry build ...

    # Sentinel substitution on the MCP slice before merge.
    if mcp_patch:
        servers = (mcp_patch.get("servers") or {})
        for entry in servers.values():
            if not isinstance(entry, dict):
                continue
            args = entry.get("args")
            if isinstance(args, list):
                entry["args"] = [
                    a.replace(_AGENT_DIR_SENTINEL, new_agent_id)
                    if isinstance(a, str) else a
                    for a in args
                ]
```

### 4. `marketplace_listings.deploy_listing` — Paperclip-first orchestration

New helper in `core/services/paperclip_agent_lifecycle.py` (peer to `create_paperclip_for_chat`):

```python
async def create_paperclip_for_marketplace(
    *,
    buyer_user_id: str,
    owner_id: str,
    name: str,
    role: str | None,
) -> dict:
    """Mint a Paperclip agent for a marketplace install.

    Mirrors create_paperclip_for_chat but takes its name/role from the
    slice's agent entry instead of a wizard payload. Same adapter shape,
    same owner-scoped service token, same admin session cookie.
    """
    # ... same body as create_paperclip_for_chat, role default "general" ...
```

Then in `routers/marketplace_listings.py:deploy_listing`, after the entitlement check and *before* fetching tarball:

```python
agent_name = (slice_.get("agent") or {}).get("name") or listing.get("name", slug)
agent_role = (slice_.get("agent") or {}).get("role") or "general"

try:
    paperclip_agent = await create_paperclip_for_marketplace(
        buyer_user_id=auth.user_id,
        owner_id=owner_id,
        name=agent_name,
        role=agent_role,
    )
except Exception:
    logger.exception(...)
    raise HTTPException(503, "paperclip unavailable — try again")

try:
    result = await catalog.deploy_from_artifact(
        owner_id=owner_id,
        slug=slug,
        manifest=manifest,
        slice_=slice_,
        tar_bytes=tar_bytes,
        new_agent_id=paperclip_agent["id"],
    )
except Exception:
    # Roll back the orphan Paperclip agent so a re-hire doesn't accumulate
    # phantom Paperclip records the buyer never asked for.
    await _delete_paperclip_orphan_best_effort(paperclip_agent["id"], owner_id)
    raise
```

The orphan-cleanup helper wraps `admin_client.delete_agent` + swallows errors. Same idempotency guarantees as `delete_paperclip_for_chat`.

### 5. Frontend changes

Frontend already has `seller_id` bypass in `useHire.ts` (shipped in PR #712). The marketplace deploy will now correctly produce a Paperclip-registered agent — the chat sidebar and Teams Inbox should both surface it without further frontend changes.

Optional future affordance (out of scope): a "you already have this listing" gate on the storefront listing detail page, using `useCatalog().installedSlugs`.

## Data flow

```
PUBLISH (seller, /listings/{id}/artifact-from-agent):
  agent_export.export_agent_from_efs(seller_id, agent_id, ...)
    ├─ build tarball from workspaces/{agent_id}/
    ├─ extract_agent_slice(openclaw_json, agent_id, ...)
    │   └─ plugins: provider-stripped at slice time          ◄── change #1
    ├─ _rewrite_mcp_args_paths(slice, seller_agent_id)
    │   └─ "/workspaces/abc-123/mcp-resy/" → "/workspaces/__AGENT_DIR__/mcp-resy/"  ◄── change #2
    └─ upload tarball + slice JSON to S3

DEPLOY (buyer, /listings/{slug}/deploy):
  ├─ entitlement check (already-deployed seller bypass from PR #712)
  ├─ create_paperclip_for_marketplace(buyer, owner_id, name, role) → paperclip_agent  ◄── change #4
  └─ catalog_service.deploy_from_artifact(..., new_agent_id=paperclip_agent["id"])
      ├─ untar → workspaces/{paperclip_agent.id}/
      ├─ rewrite mcp_patch args: __AGENT_DIR__ → paperclip_agent.id     ◄── change #3
      ├─ apply_deploy_mutation:
      │   ├─ plugins deep-merge (provider keys already absent)
      │   └─ mcp deep-merge (paths now point at new agent dir)
      └─ on any failure: admin_client.delete_agent(paperclip_agent.id)  ◄── change #4 (rollback)
```

## Error handling

| Failure point | Behavior |
|---|---|
| Paperclip `create_agent` fails | 503 to client; no OpenClaw side-effect; safe to retry. |
| Catalog deploy fails after Paperclip create | Roll back Paperclip create (delete_agent); 500 to client; safe to retry. Idempotent because Paperclip's delete is 404-tolerant. |
| MCP args sentinel substitution misses a path | The MCP fails to start on buyer's side, surfaced as runtime error — not a deploy-time failure. Tests pin substitution against realistic seller paths. |
| Provider plugin slips through denylist | Plugin name added in a future OpenClaw release and not yet in the denylist. Mitigation: denylist update + content alert in Codex review. Low blast radius (each new provider needs an explicit allowlist on the buyer's container to actually load). |
| `paperclip-companies` row missing for owner | RuntimeError surfaced as 500 with "owner has no Paperclip company — re-run provisioning". Same behavior as `create_paperclip_for_chat`. |

## Testing

**Unit**

- `test_extract_agent_slice_strips_provider_plugins` — input plugins.entries with bedrock + browser + memory → output has only browser + memory.
- `test_extract_agent_slice_keeps_non_provider_plugins` — pins specific plugins (browser, memory) survive the filter.
- `test_agent_export_replaces_seller_agent_id_in_mcp_args` — seller's path becomes `__AGENT_DIR__` in slice JSON.
- `test_deploy_from_artifact_uses_supplied_agent_id` — when new_agent_id passed, that ID is used for workspaces dir + agent entry.
- `test_deploy_from_artifact_substitutes_sentinel_in_mcp_args` — `__AGENT_DIR__` in mcp_patch becomes the new agent_id.
- `test_marketplace_deploy_creates_paperclip_agent_first` — mock chain, assert Paperclip create called before catalog deploy.
- `test_marketplace_deploy_uses_paperclip_minted_uuid` — assert catalog_service receives new_agent_id matching Paperclip's response.
- `test_marketplace_deploy_rolls_back_paperclip_on_catalog_failure` — catalog raises, Paperclip delete_agent called with the right id.
- `test_marketplace_deploy_503_when_paperclip_unavailable` — paperclip create raises → 503 + no catalog call.

**Integration / manual smoke**

- Resubmit Ray, approve, hire from a fresh dev Clerk account; assert:
  - Ray shows in chat sidebar (OpenClaw side)
  - Ray shows in Teams Inbox (Paperclip side)
  - Buyer's provider config (whichever they picked at signup) unchanged
  - Resy MCP starts successfully (args points at new agent's workspace dir)

## Migration / rollback

- Existing published listings have un-rewritten MCP args (seller's path baked in). After this change, those listings will still deploy with broken MCPs for new buyers. Sellers must re-publish to pick up the sentinel rewrite. Not strictly a migration — just expected behavior for new uploads.
- Existing buyer installs are unaffected; no DB schema changes.
- Rollback: revert the four files. No data shape changes to undo.

## Open questions

None requiring resolution before implementation. The denylist coverage (provider plugin names) is the only place where we're picking a closed set without external authority — added new providers will need to be appended to `_PROVIDER_PLUGIN_NAMES` when they're integrated, same as other provider-name lists in the codebase.

## File-by-file change summary

| File | Change |
|---|---|
| `apps/backend/core/services/catalog_slice.py` | Add `_PROVIDER_PLUGIN_NAMES` + `_strip_provider_plugins`, wire into `extract_agent_slice`. |
| `apps/backend/core/services/agent_export.py` | Add `_AGENT_DIR_SENTINEL`, `_rewrite_mcp_args_paths`, call after slice extraction. |
| `apps/backend/core/services/catalog_service.py` | Accept `new_agent_id` kwarg in `deploy_from_artifact`; substitute sentinel in mcp_patch args. |
| `apps/backend/core/services/paperclip_agent_lifecycle.py` | Add `create_paperclip_for_marketplace`. |
| `apps/backend/routers/marketplace_listings.py` | Orchestrate Paperclip-first mint + catalog deploy + rollback in `deploy_listing`. |
| `apps/backend/tests/unit/services/test_catalog_slice.py` | Add provider-strip tests. |
| `apps/backend/tests/unit/services/test_agent_export.py` | Add MCP-args-sentinel test. |
| `apps/backend/tests/unit/services/test_catalog_service.py` | Add `new_agent_id` + sentinel substitution tests. |
| `apps/backend/tests/unit/routers/test_marketplace_listings.py` | Add Paperclip-first orchestration tests + rollback test. |
