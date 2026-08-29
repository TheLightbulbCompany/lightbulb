# P0b — The Provisioning Door Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Friday provision third-party provider accounts (PostHog, Vercel, Neon, Resend, …) **on the owner's own Stripe identity** through Stripe Projects — with Friday running the CLI in her own container, the backend filing the two approval cards the owner must click, landing the returned credentials in Secrets Manager + the container, and both wipe and marketplace export proven never to touch what the owner owns.

**Architecture:** **Friday drives the CLI; the backend never does.** Spec §3.2 puts the fallback in her hands ("Friday runs `next_step`") and §3.1's trigger split gives her the work "once the door is open". So there is no backend→container remote-exec path and no `ProvisioningDoor` abstraction in P0b. Instead the Stripe CLI is baked into the OpenClaw image, Friday runs `stripe login --non-interactive` / `stripe projects catalog|add|env` herself in `/home/node/.openclaw` (the EFS root, a sibling of every `workspace-{agent_id}/`), and three hook-token routes carry the platform's half: file the **pairing** card, file the **provider-consent** card, and accept the **landed** credentials. Landing writes each value to Secrets Manager at the existing integrations path, records pointers (`actor: "projects"`, `provider_resources`) on the `integrations` row, and injects the values as OpenClaw top-level `env.vars` — re-asserted through `build_openclaw_config_dict` so a reprovision cannot drop them. Owner-facing routes shrink to reading the resource list and revoking **our** copy.

**Tech Stack:** FastAPI, `stripe` CLI v1.50.6 (pinned + checksummed in the OpenClaw image), DynamoDB via boto3 + moto, pytest + `AsyncMock` + `TestClient`/`dependency_overrides`, Next.js/React + SWR + vitest/testing-library, Paperclip admin client for approvals.

**Spec:** `docs/superpowers/specs/2026-08-28-owner-data-plane-design.md` §2, §3.2, §3.4, §3.5, §3.6, §5.3 (row "Accounts"), §6 P0

**Stacked on:** `feat/company-stripe-spine` (P0a, PR #2157, **not yet merged**). This branch is `feat/provisioning-door` in worktree `.worktrees/provisioning-door`. Rebase on P0a before opening the PR.

## Global Constraints

Copied verbatim from the spec where the spec states them.

- **The one rule (§2):** "A page never renders from data only Lightbulb holds about the owner's business. Truth lives in accounts the owner owns (their Stripe, their PostHog, their provider accounts). We hold pointers, caches, and the records we originate." Pattern name: **owner-owned, agent-operated.**
- **No umbrella accounts (§9 non-goals):** "any Lightbulb umbrella account" is a non-goal. Every provider account is created on the OWNER's Stripe identity, by the owner's own paired CLI session. Never on a platform account.
- **Paid-only (§3.6):** "The accounts layer sits behind `require_always_on_entitlement` like automations. Trial owners see the card as the upgrade trigger." Every owner-facing mutating route calls `require_always_on_entitlement(owner_id)` (`apps/backend/routers/teams/_guards.py:20`, raises 402 `{blocked, code: "routines_require_subscription", message}`).
- **Org semantics (§3.6):** "The org admin who completes Stripe onboarding is the representative and the sole money-approver." In an org context every mutating route calls `require_org_admin(auth)` (`apps/backend/core/auth.py:118`). Owner id is always `resolve_owner_id(auth)` (`apps/backend/core/auth.py:97`) — `AuthContext` has no `owner_id` field.
- **Wipe never deprovisions (§3.6):** "`account_wipe`, `entitlement_teardown`, `owner_secret_sweep` revoke the Connect link and delete *our* credential copies. They **never** call Projects `remove` or touch owner-owned provider resources."
- **Marketplace export exclusions (§3.6):** "`agent_export.py` must exclude `.projects/`, Stripe CLI config and provider credentials before Projects ships. A hired agent arrives with no accounts."
- **Secrets Manager, never DynamoDB (§3.5):** "Backend copies of provider credentials live in Secrets Manager at `isol8/{env}/integrations/{owner}/{provider}` (existing store, `credentials/integration_secret_store.py`), never DynamoDB."
- **Credential injection class (§3.5):** static env is "acceptable for provider write tokens (PostHog project token, Vercel deploy token) scoped to the owner's own resources." The **proxy** pattern is reserved for "the connected-account Stripe key and anything with money authority" — **out of scope for P0b**, named as a follow-up.
- **Company- vs agent-level (§3.4):** "Company-level: Stripe account, domain, PostHog project, bundle.social team, formation, cards. Agent-level: mailbox, Google account binding, phone number. **State this per resource in the Accounts page.**" Everything P0b provisions is company-level; the Accounts row renders the level explicitly.
- **No `apps/infra/lib/` change in this PR**, so no `npx cdk synth` and no `deploy.yml` regeneration. (`apps/infra/openclaw/Dockerfile` is not under `lib/`.) If a later edit does touch `lib/`, it must synth and commit the regenerated workflow in the same PR.
- **Do NOT bump `openclaw-version.json`** here. Editing `apps/infra/openclaw/Dockerfile` triggers `.github/workflows/build-openclaw-image.yml` (path filter, lines 15-17) which publishes a new ECR tag; promoting it to `dev.tag` / `prod.tag` is a MANUAL, separate PR (CLAUDE.md, "Extended OpenClaw image"). Until that promotion lands, Friday has no `stripe` binary and the skill's first step (`stripe --version`) tells her to stop and say so.
- Branch: `feat/provisioning-door` in worktree `.worktrees/provisioning-door`; never `main`. Commit after every task; push only when the owner says so.
- Tests: run targeted files with `cd apps/backend && uv run pytest <file> -v` and `ruff check` locally; CI runs the full suite. Frontend: `cd apps/frontend && pnpm vitest run <file>`.

---

## Verified facts this plan is built on

Read these before Task 1 — three of them are the reason the design looks the way it does.

1. **The CLI's state lands on the owner's EFS by construction.** `apps/infra/lib/stacks/container-stack.ts:940` sets `XDG_CONFIG_HOME: "/home/node/.openclaw/.config"`, and `/home/node/.openclaw` IS the per-user EFS mount (`container-stack.ts:1088-1092`). So the Stripe CLI session under `$XDG_CONFIG_HOME/stripe/` survives task replacement — a container roll does not un-pair the owner. Friday runs the CLI with CWD `/home/node/.openclaw`, so `.projects/` and the generated `.env` land at the EFS **root**.
2. **`.projects/` cannot reach a marketplace artifact, twice over.** `agent_export._resolve_agent_dir` (`apps/backend/core/services/agent_export.py:418-468`) tars `workspace-{id}/`, a *sibling* of the EFS root where the CLI writes. And the tar filter drops every dot-prefixed path segment at every depth, on the default walk AND when the seller names explicit `paths` (`agent_export.py:551-568`). Task 7 proves both rather than adding redundant filters.
3. **The wipe rule is already satisfied.** `account_wipe` step 9f deletes owner secrets and 9g deletes integration rows (`apps/backend/core/services/account_wipe.py:910-920`); nothing in the module can reach a provider. Task 7 proves no teardown module can even import the provisioning code.
4. **`integrations` rows have no `actor` and no `provider_resources` today.** `integrations_repo.put_status` (`apps/backend/core/repositories/integrations_repo.py:22-42`) writes exactly `{owner_id, provider, status, connected_at, updated_at, identity?}` with a full-replace `put_item`. Task 1 adds `update_item`-based writers modelled on `set_google_watch_channels` (`integrations_repo.py:100-117`) precisely because `put_item` would clobber `identity` / `google_watch_channels` / `meta_webhook_bindings`.
5. **`env.vars` is OpenClaw's static-env injection point.** `apps/backend/core/services/openclaw_config.schema.json` top-level `env` → `{shellEnv?, vars?: Record<string,string>}`; the fork documents it (`~/dev/agent-fork/src/config/schema.help.core.ts:43-44`) as "Explicit key/value environment variable overrides merged into runtime process environment for OpenClaw."
6. **`stripe login --non-interactive`** prints exactly `{"browser_url": …, "verification_code": …, "next_step": "stripe login --complete '…'"}` and `--complete=<poll-url>` polls until the owner approves, then saves credentials (docs.stripe.com/cli/login, fetched 2026-08-29).
7. **Stripe CLI latest release is `v1.50.6`** (published 2026-08-26); the linux asset is `stripe_1.50.6_linux_x86_64.tar.gz`, sha256 `7d020f23d4c91d3e0e6e89fd5729ad8885da243fa55d9a70aa74cd19cb05b9fa` (from the release's own `stripe-linux-checksums.txt`).
8. **Paperclip validates approval `type` against a four-value enum** (`hire_agent | approve_ceo_strategy | budget_override_required | request_board_approval`), which is why P0a's card rides `request_board_approval` and `payload.isol8_action` is what makes it ours (`apps/backend/core/services/company_stripe_approval.py:25-28`). Both P0b cards follow that exactly.
9. **`list_approvals` is documented as "List pending approvals"** (`apps/backend/core/services/paperclip_admin_client.py:1146-1159`). So the backend dedupes on PENDING cards only and never tries to infer "already approved" from it — Friday learns a card's outcome from her own Paperclip surface, which is where approvals live.

---

### Task 1: `integrations_repo` — `provider_resources` and the `projects` actor

**Files:**
- Modify: `apps/backend/core/repositories/integrations_repo.py` (append after `set_meta_webhook_bindings`, ~line 150)
- Test: `apps/backend/tests/unit/repositories/test_integrations_repo_provider_resources.py`

**Interfaces:**
- Consumes (verified, quoted from `integrations_repo.py:22` and `:100`):
  ```python
  async def put_status(owner_id: str, provider: str, status: str = "connected", identity: str | None = None) -> None:
  async def set_google_watch_channels(owner_id: str, channels: list[dict]) -> None:
  ```
- Produces:
  - `PROJECTS_ACTOR = "projects"`
  - `async def list_provider_resources(owner_id: str, provider: str) -> list[dict]`
  - `async def record_provider_resource(owner_id: str, provider: str, resource: dict) -> list[dict]` — upserts the row with `actor="projects"`, `status="connected"`, `connected_at` if absent, and appends/replaces `resource` by `resource_id`. Returns the new list.
  - `async def remove_provider_resource(owner_id: str, provider: str, resource_id: str) -> list[dict]` — removes OUR pointer only.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/repositories/test_integrations_repo_provider_resources.py
import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def integrations_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = resource.create_table(
            TableName="test-integrations",
            KeySchema=[
                {"AttributeName": "owner_id", "KeyType": "HASH"},
                {"AttributeName": "provider", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "provider", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", resource):
            yield table


def _res(rid="res_1", status="active"):
    return {
        "resource_id": rid, "provider": "posthog", "service": "analytics", "status": status,
        "resource_name": "Northwind analytics", "env_keys": ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
        "dashboard_url": None, "level": "company",
    }


@pytest.mark.asyncio
async def test_record_creates_the_row_with_the_projects_actor(integrations_table):
    from core.repositories import integrations_repo

    out = await integrations_repo.record_provider_resource("org_a", "posthog", _res())
    assert out == [_res()]
    item = integrations_table.get_item(Key={"owner_id": "org_a", "provider": "posthog"})["Item"]
    assert item["actor"] == "projects"
    assert item["status"] == "connected"
    assert item["connected_at"]


@pytest.mark.asyncio
async def test_record_appends_and_is_idempotent_per_resource_id(integrations_table):
    from core.repositories import integrations_repo

    await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_1"))
    await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_2"))
    out = await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_1", status="paused"))
    assert [r["resource_id"] for r in out] == ["res_1", "res_2"]
    assert out[0]["status"] == "paused"


@pytest.mark.asyncio
async def test_record_preserves_a_pre_existing_status_row(integrations_table):
    """put_status is a full-replace put_item; these writers must not be, or an
    identity or a Gmail watch binding is silently dropped."""
    from core.repositories import integrations_repo

    await integrations_repo.put_status("org_a", "posthog", "connected", "founder@example.com")
    await integrations_repo.record_provider_resource("org_a", "posthog", _res())
    item = integrations_table.get_item(Key={"owner_id": "org_a", "provider": "posthog"})["Item"]
    assert item["identity"] == "founder@example.com"
    assert len(item["provider_resources"]) == 1


@pytest.mark.asyncio
async def test_record_rejects_a_resource_with_no_id(integrations_table):
    from core.repositories import integrations_repo

    with pytest.raises(ValueError):
        await integrations_repo.record_provider_resource("org_a", "posthog", {"provider": "posthog"})


@pytest.mark.asyncio
async def test_list_and_remove(integrations_table):
    from core.repositories import integrations_repo

    await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_1"))
    await integrations_repo.record_provider_resource("org_a", "posthog", _res("res_2"))
    assert len(await integrations_repo.list_provider_resources("org_a", "posthog")) == 2
    left = await integrations_repo.remove_provider_resource("org_a", "posthog", "res_1")
    assert [r["resource_id"] for r in left] == ["res_2"]
    # Removing something that is not there is a no-op, not an error.
    assert await integrations_repo.remove_provider_resource("org_a", "posthog", "res_nope") == left
    assert await integrations_repo.list_provider_resources("org_b", "posthog") == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_integrations_repo_provider_resources.py -v`
Expected: FAIL with `AttributeError: module 'core.repositories.integrations_repo' has no attribute 'record_provider_resource'`.

- [ ] **Step 3: Implement**

Append to `apps/backend/core/repositories/integrations_repo.py`:

```python
# ---------------------------------------------------------------------------
# Provider resources provisioned through Stripe Projects.
#
# POINTERS ONLY (spec §2, §4.3): resource id, name, service, status, env var
# NAMES, dashboard URL. Never a value — credentials live in Secrets Manager
# (integration_secret_store), never in DynamoDB (spec §3.5).
#
# UpdateExpression rather than put_status's put_item: put_status full-replaces
# the item, so routing these through it would drop identity /
# google_watch_channels / meta_webhook_bindings. Same reasoning as
# set_google_watch_channels above.
# ---------------------------------------------------------------------------

PROJECTS_ACTOR = "projects"


async def list_provider_resources(owner_id: str, provider: str) -> list[dict]:
    row = await get(owner_id, provider)
    return list((row or {}).get("provider_resources") or [])


async def _write_provider_resources(owner_id: str, provider: str, resources: list[dict]) -> list[dict]:
    now = int(time.time())
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id, "provider": provider},
        UpdateExpression=(
            "SET provider_resources = :r, actor = :a, #st = :s, updated_at = :now, "
            "connected_at = if_not_exists(connected_at, :now)"
        ),
        # `status` is a DynamoDB reserved word.
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={":r": resources, ":a": PROJECTS_ACTOR, ":s": "connected", ":now": now},
    )
    return resources


async def record_provider_resource(owner_id: str, provider: str, resource: dict) -> list[dict]:
    """Upsert one provisioned resource pointer. Idempotent per ``resource_id``.

    Read-modify-write of a per-(owner, provider) list. The only writer is the
    container's `landed` post, and one agent cannot land two resources for the
    same provider at once, so a lost update needs two backend instances serving
    the same owner mid-deploy — at which point Friday's next `landed` restores
    the missing pointer.
    """
    resource_id = resource.get("resource_id")
    if not resource_id:
        raise ValueError("resource must carry a resource_id")
    existing = await list_provider_resources(owner_id, provider)
    merged = [r for r in existing if r.get("resource_id") != resource_id] + [resource]
    merged.sort(key=lambda r: r.get("resource_id") or "")
    return await _write_provider_resources(owner_id, provider, merged)


async def remove_provider_resource(owner_id: str, provider: str, resource_id: str) -> list[dict]:
    """Drop OUR pointer to a resource. Does NOT touch the provider — the owner
    owns that resource and keeps it (spec §3.6)."""
    existing = await list_provider_resources(owner_id, provider)
    kept = [r for r in existing if r.get("resource_id") != resource_id]
    if len(kept) == len(existing):
        return existing
    return await _write_provider_resources(owner_id, provider, kept)
```

- [ ] **Step 4: Run to verify they pass, plus the existing repo tests**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_integrations_repo_provider_resources.py tests/credentials/test_integrations_repo.py -v`
Expected: 5 + existing passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/repositories/integrations_repo.py apps/backend/tests/unit/repositories/test_integrations_repo_provider_resources.py
git commit -m "feat(provisioning): provider_resources pointers and the projects actor on the integrations row"
```

---

### Task 2: Credential landing + static-env injection that survives a reprovision

**Files:**
- Create: `apps/backend/core/services/provisioning_landing.py`
- Modify: `apps/backend/core/containers/config.py:1347-1361` (add a `provisioned_env` kwarg), `:2382-2388` (merge it), `:2470-2485` (thread it)
- Modify: `apps/backend/core/containers/ecs_manager.py:3924-3946` (build it alongside `integration_mcp_servers`)
- Test: `apps/backend/tests/unit/services/test_provisioning_landing.py`
- Test: `apps/backend/tests/unit/containers/test_openclaw_config_provisioned_env.py`

**Interfaces:**
- Consumes (verified, quoted):
  - `apps/backend/core/services/credentials/integration_secret_store.py:40` — `async def put(owner_id: str, provider: str, credential: dict[str, Any]) -> str:`; the name is built at `:32-33` as `f"isol8/{env}/integrations/{owner_id}/{provider}"`.
  - `:53` — `async def get(owner_id: str, provider: str) -> dict[str, Any] | None:`
  - `apps/backend/core/services/config_patcher.py:1029-1033` — `async def set_openclaw_config_path(owner_id: str, path: list[str], value: Any) -> None:`
  - `integrations_repo.record_provider_resource` / `list_for_owner` / `PROJECTS_ACTOR` (Task 1).
  - `apps/backend/core/containers/config.py:1347-1361` — `def build_openclaw_config_dict(*, user_id, gateway_token, provider_choice, byo_provider=None, paperclip_agents=None, hook_token=None, owner_type="personal", integration_mcp_servers=None, aip_arns=None, channel_accounts=None, acp_enabled=False, deployed_tag=None) -> dict:`
- Produces:
  - `async def land_credentials(*, owner_id: str, resource: dict, env: dict[str, str]) -> None` — `resource` is the already-validated pointer dict the route built.
  - `async def build_provisioned_env(owner_id: str) -> dict[str, str]`
  - `async def provisioning_summary(owner_id: str) -> tuple[str, list[dict]]` — `("paired"|"unpaired", resource pointers)`, the single derivation shared by the owner route (Task 6) and the facts file (Task 8).
  - `build_openclaw_config_dict(..., provisioned_env: dict | None = None)` and the same kwarg on `write_openclaw_config`.

> **VERIFY (env.vars reload):** whether OpenClaw applies a changed `env.vars` on its chokidar config reload, or only at gateway start. In a dev container after a landing, run `openclaw config get env.vars`, then in a NEW exec shell `printenv NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN`. If it needs a restart, `land_credentials` must additionally queue a Track-2 container-update notification (never a silent restart) and the skill section in Task 8 must say the variable appears after the next restart.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_provisioning_landing.py
import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import provisioning_landing as mod  # noqa: E402

RES = {
    "resource_id": "res_1", "provider": "posthog", "service": "analytics", "status": "active",
    "resource_name": "Northwind analytics", "env_keys": ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
    "dashboard_url": "https://us.posthog.com/project/1", "level": "company",
}


@pytest.mark.asyncio
async def test_land_writes_secrets_manager_then_the_row_then_the_container():
    order: list[str] = []
    with (
        patch.object(mod.integration_secret_store, "put",
                     AsyncMock(side_effect=lambda *a, **k: order.append("sm"))) as sm_put,
        patch.object(mod.integrations_repo, "record_provider_resource",
                     AsyncMock(side_effect=lambda *a, **k: order.append("row") or [])) as record,
        patch.object(mod.config_patcher, "set_openclaw_config_path",
                     AsyncMock(side_effect=lambda *a, **k: order.append("cfg"))) as patch_cfg,
    ):
        await mod.land_credentials(
            owner_id="org_a", resource=RES, env={"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live_abc"}
        )
    assert order == ["sm", "row", "cfg"]
    sm_put.assert_awaited_once_with("org_a", "posthog", {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live_abc"})
    assert record.await_args.args[:2] == ("org_a", "posthog")
    assert record.await_args.args[2]["resource_id"] == "res_1"
    patch_cfg.assert_awaited_once_with(
        "org_a", ["env", "vars", "NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"], "phc_live_abc"
    )


@pytest.mark.asyncio
async def test_land_merges_new_env_with_what_the_provider_already_had():
    """A second resource for the same provider must not blank the first one's
    vars — the Secrets Manager blob is per-(owner, provider)."""
    with (
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value={"OLD_KEY": "old"})),
        patch.object(mod.integration_secret_store, "put", AsyncMock()) as sm_put,
        patch.object(mod.integrations_repo, "record_provider_resource", AsyncMock(return_value=[])),
        patch.object(mod.config_patcher, "set_openclaw_config_path", AsyncMock()),
    ):
        await mod.land_credentials(owner_id="org_a", resource=RES, env={"NEW_KEY": "new"})
    assert sm_put.await_args.args[2] == {"OLD_KEY": "old", "NEW_KEY": "new"}


@pytest.mark.asyncio
async def test_land_never_puts_a_secret_value_in_dynamodb():
    with (
        patch.object(mod.integration_secret_store, "get", AsyncMock(return_value=None)),
        patch.object(mod.integration_secret_store, "put", AsyncMock()),
        patch.object(mod.integrations_repo, "record_provider_resource", AsyncMock(return_value=[])) as record,
        patch.object(mod.config_patcher, "set_openclaw_config_path", AsyncMock()),
    ):
        await mod.land_credentials(owner_id="org_a", resource=RES,
                                   env={"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_secret"})
    assert "phc_secret" not in repr(record.await_args.args[2])
    assert record.await_args.args[2]["env_keys"] == ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"]


@pytest.mark.asyncio
async def test_build_provisioned_env_reads_only_rows_with_the_projects_actor():
    rows = [
        {"provider": "posthog", "actor": "projects", "status": "connected"},
        {"provider": "linear", "status": "connected"},  # a normal integration, not ours
        {"provider": "vercel", "actor": "projects", "status": "disconnected"},
    ]
    with (
        patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=rows)),
        patch.object(mod.integration_secret_store, "get",
                     AsyncMock(side_effect=[{"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc"}])) as sm_get,
    ):
        env = await mod.build_provisioned_env("org_a")
    assert env == {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc"}
    assert sm_get.await_count == 1


@pytest.mark.asyncio
async def test_build_provisioned_env_never_fails_the_caller():
    with patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(side_effect=RuntimeError("ddb down"))):
        assert await mod.build_provisioned_env("org_a") == {}


@pytest.mark.asyncio
async def test_summary_pairs_state_with_pointers():
    rows = [
        {"provider": "posthog", "actor": "projects", "provider_resources": [RES]},
        {"provider": "linear"},
    ]
    with patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=rows)):
        state, resources = await mod.provisioning_summary("org_a")
    assert state == "paired"
    assert [r["resource_id"] for r in resources] == ["res_1"]
    with patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(return_value=[{"provider": "linear"}])):
        assert await mod.provisioning_summary("org_a") == ("unpaired", [])
    with patch.object(mod.integrations_repo, "list_for_owner", AsyncMock(side_effect=RuntimeError("ddb down"))):
        assert await mod.provisioning_summary("org_a") == ("unpaired", [])
```

```python
# apps/backend/tests/unit/containers/test_openclaw_config_provisioned_env.py
import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.containers.config import build_openclaw_config_dict  # noqa: E402


def _cfg(**kw):
    return build_openclaw_config_dict(
        user_id="org_a", gateway_token="gwt", provider_choice="bedrock_claude", **kw
    )


def test_provisioned_env_lands_in_env_vars():
    cfg = _cfg(provisioned_env={"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live_abc"})
    assert cfg["env"]["vars"]["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"] == "phc_live_abc"


def test_absent_provisioned_env_writes_no_env_key():
    cfg = _cfg()
    assert (cfg.get("env") or {}).get("vars") in (None, {})
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_provisioning_landing.py tests/unit/containers/test_openclaw_config_provisioned_env.py -v`
Expected: FAIL with `ModuleNotFoundError` and `TypeError: build_openclaw_config_dict() got an unexpected keyword argument 'provisioned_env'`.

- [ ] **Step 3: Implement `provisioning_landing.py`**

```python
# apps/backend/core/services/provisioning_landing.py
"""Where a provisioned credential goes after Friday hands it back.

Friday runs the Stripe CLI in her own container and POSTs the result to
``/company/provisioning/u/{owner}/landed``. This module is the platform's half.
Three writes, in this order and for these reasons:

1. **Secrets Manager** — ``isol8/{env}/integrations/{owner}/{provider}``, the
   EXISTING integration store. Spec §3.5: backend copies of provider
   credentials live here, "never DynamoDB". First, so a crash after this point
   leaves a recoverable credential rather than a provider resource we forgot.
2. **The integrations row** — POINTERS only (resource id, name, service,
   status, env var NAMES, dashboard URL) plus ``actor: "projects"``.
3. **The container** — ``env.vars.<NAME>`` in openclaw.json, OpenClaw's
   documented "environment variable overrides merged into runtime process
   environment". Static env is the right class here: these are provider WRITE
   tokens scoped to the owner's own resources (spec §3.5). The connected-account
   Stripe key and anything else with money authority must ride the credential
   PROXY (``apps/infra/openclaw/plugins/aws-exec-profile``) instead — a named
   follow-up, deliberately NOT in P0b.

Nothing here logs a credential value.
"""

from __future__ import annotations

import logging

from core.repositories import integrations_repo
from core.services import config_patcher
from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)


async def land_credentials(*, owner_id: str, resource: dict, env: dict[str, str]) -> None:
    provider = resource["provider"]
    # The Secrets Manager blob is per-(owner, provider) and a provider can hold
    # several resources, so MERGE rather than replace — a second `projects add`
    # for the same provider must not blank the first one's variables.
    existing = await integration_secret_store.get(owner_id, provider) or {}
    merged = {**{k: v for k, v in existing.items() if isinstance(v, str)}, **env}
    await integration_secret_store.put(owner_id, provider, merged)
    await integrations_repo.record_provider_resource(owner_id, provider, resource)
    for name, value in sorted(env.items()):
        await config_patcher.set_openclaw_config_path(owner_id, ["env", "vars", name], value)
    logger.info(
        "provisioning: landed %s/%s for owner=%s (%d vars)",
        provider, resource.get("service"), owner_id, len(env),
    )


async def build_provisioned_env(owner_id: str) -> dict[str, str]:
    """Every provisioned env var, rebuilt from Secrets Manager.

    A reprovision fully overwrites openclaw.json, so without re-injecting these
    the owner's provider tokens vanish on the next image roll — the identical
    failure ``integration_mcp_servers`` exists to prevent
    (core/containers/config.py:2382-2388). Best-effort: never fail a provision.
    """
    env: dict[str, str] = {}
    try:
        for row in await integrations_repo.list_for_owner(owner_id):
            if row.get("actor") != integrations_repo.PROJECTS_ACTOR:
                continue
            if row.get("status", "connected") != "connected":
                continue
            cred = await integration_secret_store.get(owner_id, row.get("provider") or "")
            for name, value in (cred or {}).items():
                if isinstance(value, str):
                    env[name] = value
    except Exception:  # noqa: BLE001 — provisioning must not fail over this.
        logger.exception("build_provisioned_env failed for %s; continuing without it", owner_id)
        return {}
    return env


async def provisioning_summary(owner_id: str) -> tuple[str, list[dict]]:
    """``(stripe_cli, resource pointers)`` from ONE integrations query.

    ``stripe_cli`` is DERIVED, not probed: a successful `landed` post proves the
    owner completed pairing, and probing the container costs a websocket round
    trip on a path that runs on every billing mutation. Never raises — both
    callers (the Accounts route and the facts file) must survive a DDB hiccup.
    """
    try:
        rows = await integrations_repo.list_for_owner(owner_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("provisioning_summary: integrations read failed for %s: %s", owner_id, e)
        return "unpaired", []
    resources: list[dict] = []
    paired = False
    for row in rows:
        if row.get("actor") != integrations_repo.PROJECTS_ACTOR:
            continue
        paired = True
        resources.extend(row.get("provider_resources") or [])
    resources.sort(key=lambda r: (r.get("provider") or "", r.get("service") or ""))
    return ("paired" if paired else "unpaired"), resources
```

- [ ] **Step 4: Thread `provisioned_env` through the config builder**

In `apps/backend/core/containers/config.py`:

- Add `provisioned_env: dict | None = None,` to the keyword-only signature of `build_openclaw_config_dict` (line 1347-1361), directly after `integration_mcp_servers: dict | None = None,`.
- Add the same parameter to `write_openclaw_config` (line 2470-2485) in the same position, and pass `provisioned_env=provisioned_env` where it already passes `integration_mcp_servers=integration_mcp_servers`.
- Directly after the `integration_mcp_servers` merge block (line 2382-2388) add:

```python
    # Merge env vars from resources provisioned on the owner's own identity
    # (Stripe Projects). Same reason as the mcp.servers merge above: a
    # reprovision fully overwrites openclaw.json, so without this every
    # provider token the owner provisioned disappears on the next image roll.
    if provisioned_env:
        config.setdefault("env", {}).setdefault("vars", {}).update(provisioned_env)
```

- [ ] **Step 5: Build it at provision time**

In `apps/backend/core/containers/ecs_manager.py`, directly after the `_provision_mcp_servers` call (line 3946) add:

```python
        # Same staleness class as the MCP servers above: rebuild the owner's
        # provisioned provider env from Secrets Manager so this full overwrite
        # does not drop it.
        from core.services.provisioning_landing import build_provisioned_env

        provisioned_env = await build_provisioned_env(owner_id)
```

and pass `provisioned_env=provisioned_env,` in the `write_openclaw_config(...)` call in the same function, beside `integration_mcp_servers=integration_mcp_servers,`.

- [ ] **Step 6: Run to verify the tests pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_provisioning_landing.py tests/unit/containers/test_openclaw_config_provisioned_env.py -v && uv run pytest tests/unit/containers -q`
Expected: 6 + 2 passed; the existing container-config suite still green.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/core/services/provisioning_landing.py apps/backend/core/containers/config.py apps/backend/core/containers/ecs_manager.py apps/backend/tests/unit/services/test_provisioning_landing.py apps/backend/tests/unit/containers/test_openclaw_config_provisioned_env.py
git commit -m "feat(provisioning): land credentials in Secrets Manager, pointers on the row, env.vars in the container"
```

---

### Task 3: The two approval cards — CLI pairing and provider consent

**Files:**
- Create: `apps/backend/core/services/provisioning_approvals.py`
- Test: `apps/backend/tests/unit/services/test_provisioning_approvals.py`

**Interfaces:**
- Consumes, copied from the P0a card at `apps/backend/core/services/company_stripe_approval.py:43-65` (verified):
  ```python
  repo = PaperclipRepo(table_name="paperclip-companies")
  company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
  admin = shared_admin_client()
  cookie = await get_admin_session_cookie(admin._http)
  existing = await admin.list_approvals(session_cookie=cookie, company_id=company_id)
  created = await admin.create_approval(session_cookie=cookie, company_id=company_id, body=body)
  ```
  plus `resolve_company_name(owner_id)` from `core.services.company_stripe`.
- Produces:
  - `APPROVAL_TYPE = "request_board_approval"`, `PAIR_MARKER = "stripe_cli_login"`, `CONSENT_MARKER = "provider_consent"`
  - `async def request_cli_pairing(*, owner_id: str, issue_id: str | None, browser_url: str, verification_code: str) -> str | None`
  - `async def request_provider_consent(*, owner_id: str, issue_id: str | None, provider: str, service: str, terms_url: str, pricing: str) -> str | None`
  - Both return the approval id, or `None` when the owner has no Paperclip company.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_provisioning_approvals.py
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import provisioning_approvals as mod  # noqa: E402


def _admin(existing):
    admin = MagicMock()
    admin._http = object()
    admin.list_approvals = AsyncMock(return_value={"approvals": existing})
    admin.create_approval = AsyncMock(return_value={"id": "apr_new"})
    return admin


def _board(admin, company_id="cmp_1"):
    return (
        patch.object(mod, "resolve_company_id_for_owner", AsyncMock(return_value=company_id)),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "shared_admin_client", return_value=admin),
        patch.object(mod, "resolve_company_name", AsyncMock(return_value="Northwind Ceramics")),
    )


@pytest.mark.asyncio
async def test_pairing_card_carries_the_url_and_code():
    admin = _admin([])
    with (*_board(admin),):
        approval_id = await mod.request_cli_pairing(
            owner_id="org_a", issue_id="iss_1",
            browser_url="https://dashboard.stripe.com/stripecli/confirm?t=abc",
            verification_code="ABCD-EFGH",
        )
    assert approval_id == "apr_new"
    body = admin.create_approval.call_args.kwargs["body"]
    assert body["type"] == "request_board_approval"
    assert body["issueIds"] == ["iss_1"]
    assert body["payload"]["isol8_action"] == "stripe_cli_login"
    assert body["payload"]["title"] == "Connect Stripe CLI for Northwind Ceramics"
    assert body["payload"]["browser_url"] == "https://dashboard.stripe.com/stripecli/confirm?t=abc"
    assert body["payload"]["verification_code"] == "ABCD-EFGH"


@pytest.mark.asyncio
async def test_pairing_card_dedupes_on_a_pending_card():
    admin = _admin([{"id": "apr_old", "type": "request_board_approval", "status": "pending",
                     "payload": {"isol8_action": "stripe_cli_login"}}])
    with (*_board(admin),):
        assert await mod.request_cli_pairing(
            owner_id="org_a", issue_id=None, browser_url="https://x", verification_code="Z"
        ) == "apr_old"
    admin.create_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_consent_card_carries_provider_service_terms_and_pricing():
    admin = _admin([])
    with (*_board(admin),):
        approval_id = await mod.request_provider_consent(
            owner_id="org_a", issue_id=None, provider="posthog", service="analytics",
            terms_url="https://posthog.com/terms", pricing="Free tier — 1M events/mo",
        )
    assert approval_id == "apr_new"
    payload = admin.create_approval.call_args.kwargs["body"]["payload"]
    assert payload["isol8_action"] == "provider_consent"
    assert payload["title"] == "Approve posthog analytics for Northwind Ceramics"
    assert payload["provider"] == "posthog"
    assert payload["service"] == "analytics"
    assert payload["terms_url"] == "https://posthog.com/terms"
    assert payload["pricing"] == "Free tier — 1M events/mo"


@pytest.mark.asyncio
async def test_consent_dedupe_is_per_provider_service_not_global():
    """A pending consent card for Vercel must not suppress a PostHog one."""
    admin = _admin([{"id": "apr_vercel", "type": "request_board_approval", "status": "pending",
                     "payload": {"isol8_action": "provider_consent", "provider": "vercel", "service": "hosting"}}])
    with (*_board(admin),):
        new_id = await mod.request_provider_consent(
            owner_id="org_a", issue_id=None, provider="posthog", service="analytics",
            terms_url="https://posthog.com/terms", pricing="free",
        )
        same_id = await mod.request_provider_consent(
            owner_id="org_a", issue_id=None, provider="vercel", service="hosting",
            terms_url="https://vercel.com/legal", pricing="paid",
        )
    assert new_id == "apr_new"
    assert same_id == "apr_vercel"
    assert admin.create_approval.await_count == 1


@pytest.mark.asyncio
async def test_no_paperclip_company_is_none_not_an_error():
    admin = _admin([])
    with (
        patch.object(mod, "resolve_company_id_for_owner", AsyncMock(return_value=None)),
        patch.object(mod, "shared_admin_client", return_value=admin),
    ):
        assert await mod.request_cli_pairing(
            owner_id="org_a", issue_id=None, browser_url="https://x", verification_code="Z"
        ) is None
    admin.create_approval.assert_not_awaited()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_provisioning_approvals.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.services.provisioning_approvals'`.

- [ ] **Step 3: Implement**

```python
# apps/backend/core/services/provisioning_approvals.py
"""The two cards the owner must click for the provisioning rail.

Friday runs the Stripe CLI herself; the platform's job is to put the two human
decisions in front of the owner, in the surface they already use:

  * ``stripe_cli_login`` — she ran ``stripe login --non-interactive``, which
    printed a browser URL and a verification code. The owner opens the URL,
    confirms the code, and approves the card. Friday polls and continues.
  * ``provider_consent`` — before a paid or ToS-bearing service, the owner sees
    the provider, the service, the terms link and the price, and approves.
    Only then may Friday pass ``--accept-tos``.

Paperclip validates ``type`` against its own four-value enum, so both ride
``request_board_approval`` and ``payload.isol8_action`` is what makes them ours
— both for de-dup here and for the row action in the UI (same shape as P0a's
``company_stripe_approval``).

De-dup is on PENDING cards only: ``list_approvals`` is documented as "List
pending approvals" (paperclip_admin_client.py:1152), so the backend never tries
to infer "already approved" from it. Friday reads a card's outcome from her own
Paperclip surface, which is where approvals live.
"""

from __future__ import annotations

import logging

from core.repositories.paperclip_repo import PaperclipRepo
from core.services.company_stripe import resolve_company_name
from core.services.paperclip_admin_client import shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

logger = logging.getLogger(__name__)

APPROVAL_TYPE = "request_board_approval"
PAIR_MARKER = "stripe_cli_login"
CONSENT_MARKER = "provider_consent"

_PENDING = ("pending", "revision_requested")


def _matches(approval: dict, marker: str, extra: dict | None = None) -> bool:
    if approval.get("type") != APPROVAL_TYPE or approval.get("status") not in _PENDING:
        return False
    payload = approval.get("payload") or {}
    if payload.get("isol8_action") != marker:
        return False
    return all(payload.get(k) == v for k, v in (extra or {}).items())


async def _file(owner_id: str, *, marker: str, payload: dict, issue_id: str | None, dedupe: dict | None) -> str | None:
    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if not company_id:
        logger.info("provisioning_approvals: no Paperclip company for owner_id=%s", owner_id)
        return None
    admin = shared_admin_client()
    cookie = await get_admin_session_cookie(admin._http)
    existing = await admin.list_approvals(session_cookie=cookie, company_id=company_id)
    approvals = existing if isinstance(existing, list) else (existing or {}).get("approvals") or []
    for approval in approvals:
        if _matches(approval, marker, dedupe):
            return approval.get("id")
    body = {
        "type": APPROVAL_TYPE,
        "payload": {"isol8_action": marker, **payload},
        "issueIds": [issue_id] if issue_id else [],
    }
    created = await admin.create_approval(session_cookie=cookie, company_id=company_id, body=body)
    return created.get("id")


async def request_cli_pairing(
    *, owner_id: str, issue_id: str | None, browser_url: str, verification_code: str
) -> str | None:
    company_name = await resolve_company_name(owner_id)
    return await _file(
        owner_id,
        marker=PAIR_MARKER,
        payload={
            "title": f"Connect Stripe CLI for {company_name}",
            "browser_url": browser_url,
            "verification_code": verification_code,
        },
        issue_id=issue_id,
        dedupe=None,
    )


async def request_provider_consent(
    *, owner_id: str, issue_id: str | None, provider: str, service: str, terms_url: str, pricing: str
) -> str | None:
    company_name = await resolve_company_name(owner_id)
    return await _file(
        owner_id,
        marker=CONSENT_MARKER,
        payload={
            "title": f"Approve {provider} {service} for {company_name}",
            "provider": provider,
            "service": service,
            "terms_url": terms_url,
            "pricing": pricing[:500],
        },
        issue_id=issue_id,
        # Per provider/service: a pending Vercel card must not silently swallow
        # a PostHog request.
        dedupe={"provider": provider, "service": service},
    )
```

If `resolve_company_name` is fetched before the company-id lookup in your reading of the code, note the order here is deliberate the other way for the pairing card only because both calls are cheap; keep the implementation exactly as written so the tests' patch targets hold.

- [ ] **Step 4: Run to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_provisioning_approvals.py -v && uv run ruff check core/services/provisioning_approvals.py`
Expected: 5 passed, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/provisioning_approvals.py apps/backend/tests/unit/services/test_provisioning_approvals.py
git commit -m "feat(provisioning): stripe_cli_login and provider_consent approval cards"
```

---

### Task 4: The three container routes — pairing, consent, landed

**Files:**
- Create: `apps/backend/routers/company_provisioning.py`
- Modify: `apps/backend/main.py:954-956` (mount beside `company_stripe`)
- Test: `apps/backend/tests/unit/routers/test_company_provisioning_container.py`

**Interfaces:**
- Consumes:
  - `apps/backend/routers/agent_builder.py:103` — `async def _require_hook_token(owner_id: str, request: Request) -> dict:` (timing-safe compare against the container row's `hook_token`).
  - `provisioning_approvals.request_cli_pairing` / `request_provider_consent` (Task 3).
  - `provisioning_landing.land_credentials` (Task 2).
  - `core.services.owner_facts.write_owner_facts(owner_id)`.
  - The P0a error shape at `apps/backend/routers/company_stripe.py:196-232`: 503 `paperclip_unavailable` for `httpx.HTTPError` / retryable `PaperclipApiError`, 502 `paperclip_rejected` otherwise.
- Produces, mounted at prefix `/api/v1/company/provisioning` (matching P0a's `/api/v1/company/stripe` + `/u/{owner_id}/request` shape):
  - `POST /u/{owner_id}/pairing` — body `{browser_url, verification_code, issue_id?}` → `{"approval_id": str|None, "status": "filed"|"no_company"}`
  - `POST /u/{owner_id}/consent` — body `{provider, service, terms_url?, pricing?, issue_id?}` → `{"approval_id": str|None, "status": "filed"|"no_company"}`
  - `POST /u/{owner_id}/landed` — body `{provider, service, resource_id, resource_name?, env, dashboard_url?}` → the stored pointer dict.

**Validation is not optional here.** `provider` becomes part of a Secrets Manager secret name AND the `integrations` sort key; `resource_id` goes into a URL path. The body arrives from an LLM-driven agent, so it is a trust boundary.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/routers/test_company_provisioning_container.py
import os
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.paperclip_admin_client import PaperclipApiError  # noqa: E402
from main import app  # noqa: E402
from routers import company_provisioning as mod  # noqa: E402

BASE = "/api/v1/company/provisioning/u/org_a"
HDRS = {"Authorization": "Bearer hook-token"}


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    return TestClient(app)


@pytest.fixture(autouse=True)
def _hook(monkeypatch):
    monkeypatch.setattr(mod, "_require_hook_token", AsyncMock(return_value={"owner_id": "org_a"}))


@pytest.fixture(autouse=True)
def facts(monkeypatch):
    writer = AsyncMock()
    monkeypatch.setattr(mod, "write_owner_facts", writer)
    return writer


def test_pairing_files_the_card(client):
    with patch.object(mod.provisioning_approvals, "request_cli_pairing", AsyncMock(return_value="apr_1")) as ask:
        resp = client.post(
            f"{BASE}/pairing", headers=HDRS,
            json={"browser_url": "https://dashboard.stripe.com/stripecli/confirm?t=abc",
                  "verification_code": "ABCD-EFGH", "issue_id": "iss_1"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"approval_id": "apr_1", "status": "filed"}
    assert ask.await_args.kwargs["verification_code"] == "ABCD-EFGH"
    assert ask.await_args.kwargs["issue_id"] == "iss_1"


def test_pairing_rejects_a_non_stripe_browser_url(client):
    resp = client.post(
        f"{BASE}/pairing", headers=HDRS,
        json={"browser_url": "https://evil.example.com/phish", "verification_code": "ABCD-EFGH"},
    )
    assert resp.status_code == 422


def test_no_company_is_reported_not_faked(client):
    with patch.object(mod.provisioning_approvals, "request_cli_pairing", AsyncMock(return_value=None)):
        resp = client.post(
            f"{BASE}/pairing", headers=HDRS,
            json={"browser_url": "https://dashboard.stripe.com/stripecli/confirm?t=abc",
                  "verification_code": "ABCD-EFGH"},
        )
    assert resp.json()["status"] == "no_company"


def test_board_outage_is_503_and_rejection_is_502(client):
    with patch.object(mod.provisioning_approvals, "request_provider_consent",
                      AsyncMock(side_effect=httpx.ConnectError("down"))):
        resp = client.post(f"{BASE}/consent", headers=HDRS, json={"provider": "posthog", "service": "analytics"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "paperclip_unavailable"

    err = PaperclipApiError("bad request", status_code=400)
    with patch.object(mod.provisioning_approvals, "request_provider_consent", AsyncMock(side_effect=err)):
        resp = client.post(f"{BASE}/consent", headers=HDRS, json={"provider": "posthog", "service": "analytics"})
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "paperclip_rejected"


def test_landed_stores_the_pointer_and_refreshes_the_facts(client, facts):
    with patch.object(mod.provisioning_landing, "land_credentials", AsyncMock()) as land:
        resp = client.post(
            f"{BASE}/landed", headers=HDRS,
            json={"provider": "posthog", "service": "analytics", "resource_id": "res_1",
                  "resource_name": "Northwind analytics",
                  "env": {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live_abc"},
                  "dashboard_url": "https://us.posthog.com/project/1"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource_id"] == "res_1"
    assert body["level"] == "company"
    assert body["env_keys"] == ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"]
    # The VALUE never comes back out of the API.
    assert "phc_live_abc" not in resp.text
    assert land.await_args.kwargs["env"] == {"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN": "phc_live_abc"}
    facts.assert_awaited_once_with("org_a")


@pytest.mark.parametrize(
    "patch_body",
    [
        {"provider": "../../etc"},                       # path traversal into the secret name
        {"provider": "PostHog!"},                        # charset
        {"resource_id": "res/../1"},                     # lands in a URL path
        {"env": {"lowercase_key": "v"}},                 # not a shell env var name
        {"env": {"K" * 200: "v"}},                       # absurd key
        {"env": {f"K{i}": "v" for i in range(40)}},      # too many
        {"env": {"K": "x" * 20000}},                     # absurd value
    ],
)
def test_landed_rejects_hostile_bodies(client, patch_body):
    body = {"provider": "posthog", "service": "analytics", "resource_id": "res_1",
            "env": {"TOKEN": "v"}, **patch_body}
    with patch.object(mod.provisioning_landing, "land_credentials", AsyncMock()) as land:
        resp = client.post(f"{BASE}/landed", headers=HDRS, json=body)
    assert resp.status_code == 422
    land.assert_not_awaited()


def test_every_container_route_requires_the_hook_token(monkeypatch, client):
    from fastapi import HTTPException

    monkeypatch.setattr(mod, "_require_hook_token",
                        AsyncMock(side_effect=HTTPException(status_code=401, detail="invalid hook token")))
    for path, body in (
        ("pairing", {"browser_url": "https://dashboard.stripe.com/x", "verification_code": "Z"}),
        ("consent", {"provider": "posthog", "service": "analytics"}),
        ("landed", {"provider": "posthog", "service": "analytics", "resource_id": "r", "env": {}}),
    ):
        assert client.post(f"{BASE}/{path}", headers=HDRS, json=body).status_code == 401
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_company_provisioning_container.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'routers.company_provisioning'`.

- [ ] **Step 3: Implement the router**

```python
# apps/backend/routers/company_provisioning.py
"""The provisioning rail — /api/v1/company/provisioning/*.

Friday drives the Stripe CLI inside her own container (spec §3.2: "Friday runs
`next_step`"). The backend never shells into a container. These `/u/{owner_id}/*`
routes are container-authed with the owner's per-container hook token and carry
the platform's half: file the two approval cards the owner must click, and land
the credentials she reports back.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.repositories import integrations_repo
from core.services import provisioning_approvals, provisioning_landing
from core.services.credentials import integration_secret_store
from core.services.owner_facts import write_owner_facts
from core.services.paperclip_admin_client import PaperclipApiError
from routers.agent_builder import _require_hook_token
from routers.teams._guards import require_always_on_entitlement

logger = logging.getLogger(__name__)
router = APIRouter()

# `provider` becomes part of a Secrets Manager secret NAME and the integrations
# sort key; `resource_id` goes into a URL path. The body comes from an
# LLM-driven agent, so this is a trust boundary, not a formality.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_MAX_ENV_VARS = 25
_MAX_ENV_VALUE = 8192

# The pairing URL is shown to the owner as a click target, so it may only ever
# be Stripe's own confirm host.
_STRIPE_LOGIN_PREFIX = "https://dashboard.stripe.com/"


def _board_unavailable(owner_id: str, e: Exception) -> HTTPException:
    logger.warning("provisioning: could not reach the board owner_id=%s: %s", owner_id, e)
    return HTTPException(
        status_code=503,
        detail={"code": "paperclip_unavailable", "message": "Could not reach the company board; try again later"},
    )


def _board_rejected(owner_id: str, e: PaperclipApiError) -> HTTPException:
    logger.warning("provisioning: board rejected the request owner_id=%s: %s", owner_id, e)
    return HTTPException(status_code=502, detail={"code": "paperclip_rejected", "message": e.message})


async def _file_card(owner_id: str, coro):
    """Run one card-filing coroutine with the P0a error split: 503 = the board
    is down (retry), 502 = the board refused (retrying changes nothing)."""
    try:
        return await coro
    except httpx.HTTPError as e:
        raise _board_unavailable(owner_id, e) from e
    except PaperclipApiError as e:
        if e.retryable:
            raise _board_unavailable(owner_id, e) from e
        raise _board_rejected(owner_id, e) from e


class PairingBody(BaseModel):
    browser_url: str
    verification_code: str = Field(min_length=1, max_length=64)
    issue_id: str | None = None

    @field_validator("browser_url")
    @classmethod
    def _stripe_only(cls, v: str) -> str:
        if not v.startswith(_STRIPE_LOGIN_PREFIX):
            raise ValueError("browser_url must be a dashboard.stripe.com URL")
        return v


@router.post("/u/{owner_id}/pairing")
async def request_pairing(owner_id: str, body: PairingBody, request: Request):
    """Friday already ran `stripe login --non-interactive`; file the card."""
    await _require_hook_token(owner_id, request)
    approval_id = await _file_card(
        owner_id,
        provisioning_approvals.request_cli_pairing(
            owner_id=owner_id,
            issue_id=body.issue_id,
            browser_url=body.browser_url,
            verification_code=body.verification_code,
        ),
    )
    return {"approval_id": approval_id, "status": "filed" if approval_id else "no_company"}


class ConsentBody(BaseModel):
    provider: str
    service: str
    terms_url: str = ""
    pricing: str = ""
    issue_id: str | None = None

    @field_validator("provider", "service")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("must be a lowercase slug")
        return v


@router.post("/u/{owner_id}/consent")
async def request_consent(owner_id: str, body: ConsentBody, request: Request):
    """The owner's one decision per provider: the terms and the price."""
    await _require_hook_token(owner_id, request)
    approval_id = await _file_card(
        owner_id,
        provisioning_approvals.request_provider_consent(
            owner_id=owner_id,
            issue_id=body.issue_id,
            provider=body.provider,
            service=body.service,
            terms_url=body.terms_url,
            pricing=body.pricing,
        ),
    )
    return {"approval_id": approval_id, "status": "filed" if approval_id else "no_company"}


class LandedBody(BaseModel):
    provider: str
    service: str
    resource_id: str
    resource_name: str = ""
    env: dict[str, str] = {}
    dashboard_url: str | None = None

    @field_validator("provider", "service")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("must be a lowercase slug")
        return v

    @field_validator("resource_id")
    @classmethod
    def _resource_id(cls, v: str) -> str:
        if not _RESOURCE_ID_RE.match(v):
            raise ValueError("resource_id has an unexpected shape")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > _MAX_ENV_VARS:
            raise ValueError(f"at most {_MAX_ENV_VARS} environment variables")
        for key, value in v.items():
            if not _ENV_KEY_RE.match(key):
                raise ValueError("environment variable names must be UPPER_SNAKE")
            if len(value) > _MAX_ENV_VALUE:
                raise ValueError("environment variable value is too large")
        return v


@router.post("/u/{owner_id}/landed")
async def landed(owner_id: str, body: LandedBody, request: Request):
    """Friday reports a successful `stripe projects add`. We store OUR copy.

    Never logs a value. The response echoes the POINTER only — env var names,
    never values (spec §2: we hold pointers, the provider holds the truth).
    """
    await _require_hook_token(owner_id, request)
    resource = {
        "resource_id": body.resource_id,
        "provider": body.provider,
        "service": body.service,
        "resource_name": body.resource_name,
        "status": "active",
        "env_keys": sorted(body.env),
        "dashboard_url": body.dashboard_url,
        # Everything P0b provisions is company-level (spec §3.4). Agent-level
        # resources ride their own rails; the Accounts row renders this.
        "level": "company",
    }
    await provisioning_landing.land_credentials(owner_id=owner_id, resource=resource, env=body.env)
    await write_owner_facts(owner_id)
    return resource
```

- [ ] **Step 4: Mount the router**

In `apps/backend/main.py`, next to the existing company-Stripe mount (lines 954-956), add:

```python
from routers import company_provisioning  # noqa: E402

app.include_router(
    company_provisioning.router, prefix="/api/v1/company/provisioning", tags=["company-provisioning"]
)
```

The owner-facing imports (`get_current_user`, `require_org_admin`, `resolve_owner_id`, `require_always_on_entitlement`, `integrations_repo`, `integration_secret_store`, `Annotated`, `Depends`) are unused until Task 6; ruff will flag them. Add them in Task 6 instead of carrying `# noqa` here.

- [ ] **Step 5: Run to verify the tests pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_company_provisioning_container.py -v && uv run python -c "import main" && uv run ruff check routers/company_provisioning.py`
Expected: 12 passed (7 of them parametrized), the import clean, ruff clean.

If `PaperclipApiError(...)` does not take `status_code=` positionally as written, read its `__init__` in `core/services/paperclip_admin_client.py` and construct it the way `tests/unit/routers/test_company_stripe.py` already does.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/routers/company_provisioning.py apps/backend/main.py apps/backend/tests/unit/routers/test_company_provisioning_container.py
git commit -m "feat(provisioning): container routes for pairing, provider consent and landed credentials"
```

---

### Task 5: Bake the Stripe CLI into the OpenClaw image

**Files:**
- Modify: `apps/infra/openclaw/Dockerfile` (insert a new layer immediately before the `# ---- Litestream + local-SQLite boot chain ----` block at line 820)
- Test: `apps/infra/test/container-stack.test.ts`
- Test: `apps/backend/tests/unit/services/test_provisioning_cli_paths.py`

**Interfaces:**
- Produces: `/usr/local/bin/stripe` in `isol8/openclaw-extended`, version-pinned + checksum-verified.
- Consumes: nothing new.

> **VERIFY (plugin location):** `stripe plugin install projects` is NOT baked, deliberately — the plugin installs under the user's config dir, and at runtime `XDG_CONFIG_HOME` is the EFS mount (`container-stack.ts:940`), which SHADOWS anything the image baked there. Confirm the path and whether the install needs auth by building the image and running:
> `docker run --rm -e XDG_CONFIG_HOME=/tmp/xdg <image> sh -c 'stripe --version && stripe plugin install projects && find /tmp/xdg -maxdepth 4 -name "*projects*"'`
> If the plugin lands OUTSIDE `$XDG_CONFIG_HOME` (e.g. `/usr/local/lib/stripe`), bake `stripe plugin install projects` into this layer and drop that line from the skill's step 1. If it lands inside, keep it in the skill — it is idempotent and Friday runs it once per container.

- [ ] **Step 1: Write the failing infra test**

In `apps/infra/test/container-stack.test.ts`, inside the existing `describe` that builds the ContainerStack `template`, add:

```ts
  test("XDG_CONFIG_HOME sits on the per-user EFS mount so a CLI session survives task replacement", () => {
    // The Stripe CLI writes its session under $XDG_CONFIG_HOME/stripe. That has
    // to be inside /home/node/.openclaw (the EFS volume) or every task
    // replacement un-pairs the owner. Same reason gog's keyring is there.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", Match.objectLike({
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "XDG_CONFIG_HOME", Value: "/home/node/.openclaw/.config" }),
          ]),
          MountPoints: Match.arrayWith([
            Match.objectLike({ ContainerPath: "/home/node/.openclaw", SourceVolume: "openclaw-workspace" }),
          ]),
        }),
      ]),
    }));
  });
```

- [ ] **Step 2: Run it — it should PASS already**

Run: `cd apps/infra && npx jest test/container-stack.test.ts -t "XDG_CONFIG_HOME"`
Expected: PASS. This is a characterization test: it locks in the fact the whole rail depends on, so a future edit that moves `XDG_CONFIG_HOME` off EFS fails here instead of silently un-pairing the fleet.

- [ ] **Step 3: Write the failing backend test for the pin**

```python
# apps/backend/tests/unit/services/test_provisioning_cli_paths.py
"""The in-container paths and the CLI pin the provisioning rail depends on,
asserted against the sources so an edit cannot drift them silently."""

import os
from pathlib import Path

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

_INFRA = Path(__file__).resolve().parents[3] / "infra"
_STACK = _INFRA / "lib" / "stacks" / "container-stack.ts"
_DOCKERFILE = _INFRA / "openclaw" / "Dockerfile"
_SKILL = _INFRA / "openclaw" / "skills" / "isol8" / "SKILL.md"

# The CWD Friday runs the CLI in — the EFS root, a sibling of every
# workspace-{agent_id}/, which is what keeps `.projects/` out of a marketplace
# artifact by construction (see test_agent_export_projects_exclusions.py).
EFS_HOME = "/home/node/.openclaw"


def test_efs_home_is_the_container_mount_point():
    assert f'containerPath: "{EFS_HOME}"' in _STACK.read_text()


def test_xdg_config_home_is_under_the_efs_mount():
    assert f'XDG_CONFIG_HOME: "{EFS_HOME}/.config"' in _STACK.read_text()


def test_stripe_cli_is_pinned_with_a_checksum():
    body = _DOCKERFILE.read_text()
    assert "ARG STRIPE_CLI_VERSION=" in body
    assert "ARG STRIPE_CLI_SHA256=" in body
    assert "sha256sum -c -" in body


def test_the_skill_tells_friday_to_run_the_cli_in_the_efs_root():
    """If the skill ever names a workspace dir instead, `.projects/` starts
    landing inside an exportable agent workspace."""
    assert EFS_HOME in _SKILL.read_text()
```

`test_the_skill_tells_friday_to_run_the_cli_in_the_efs_root` fails until Task 8 lands the skill section; that is expected and called out again there.

- [ ] **Step 4: Run to verify the pin assertion fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_provisioning_cli_paths.py -v`
Expected: the first two PASS; `test_stripe_cli_is_pinned_with_a_checksum` and `test_the_skill_tells_friday_to_run_the_cli_in_the_efs_root` FAIL.

- [ ] **Step 5: Add the Dockerfile layer**

In `apps/infra/openclaw/Dockerfile`, immediately before the line `# ---- Litestream + local-SQLite boot chain ----` (line 820), insert:

```dockerfile
# ---- Stripe CLI (the provisioning rail) ----
# Friday runs this binary herself, in /home/node/.openclaw — the per-user EFS
# mount. That is deliberate on two counts: the CLI session under
# $XDG_CONFIG_HOME survives task replacement, and `.projects/` lands at the EFS
# ROOT, a sibling of every workspace-{agent_id}/, so it can never ride into a
# marketplace artifact. The backend never invokes this binary.
#
# Version-pinned with the sha256 from the release's own
# stripe-linux-checksums.txt, same posture as litestream below. amd64 only:
# Fargate tasks here are x86_64.
#
# The `projects` PLUGIN is deliberately NOT installed here. At runtime
# XDG_CONFIG_HOME is /home/node/.openclaw/.config (container-stack.ts:940) —
# the per-user EFS mount — which shadows whatever the image baked under the
# build user's config dir. The bundled skill has Friday run
# `stripe plugin install projects` once; it is idempotent.
ARG STRIPE_CLI_VERSION=1.50.6
ARG STRIPE_CLI_SHA256=7d020f23d4c91d3e0e6e89fd5729ad8885da243fa55d9a70aa74cd19cb05b9fa
RUN set -eux; \
    curl -fsSL -o /tmp/stripe.tgz \
      "https://github.com/stripe/stripe-cli/releases/download/v${STRIPE_CLI_VERSION}/stripe_${STRIPE_CLI_VERSION}_linux_x86_64.tar.gz"; \
    echo "${STRIPE_CLI_SHA256}  /tmp/stripe.tgz" | sha256sum -c -; \
    tar -xzf /tmp/stripe.tgz -C /usr/local/bin stripe; \
    rm /tmp/stripe.tgz; \
    stripe --version
```

- [ ] **Step 6: Run to verify the pin test passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_provisioning_cli_paths.py -v`
Expected: 3 passed, 1 failed (`test_the_skill_tells_friday_to_run_the_cli_in_the_efs_root`, which Task 8 fixes).

- [ ] **Step 7: Do NOT bump the version pins — state it in the commit**

`openclaw-version.json` is untouched. Merging this PR triggers `build-openclaw-image.yml` (Dockerfile path filter) and publishes a new `isol8/openclaw-extended` tag; promoting it to `dev.tag` / `prod.tag` is a separate, manual PR per CLAUDE.md. Until that promotion lands, `stripe --version` fails in the container and the skill's step 1 tells Friday to say so rather than improvise.

- [ ] **Step 8: Commit**

```bash
git add apps/infra/openclaw/Dockerfile apps/infra/test/container-stack.test.ts apps/backend/tests/unit/services/test_provisioning_cli_paths.py
git commit -m "feat(provisioning): bake the pinned Stripe CLI into the OpenClaw image" -m "Version pins in openclaw-version.json are deliberately NOT bumped; promotion is a separate manual PR."
```

---

### Task 6: Owner routes — list resources, revoke our copy

**Files:**
- Modify: `apps/backend/routers/company_provisioning.py`
- Test: `apps/backend/tests/unit/routers/test_company_provisioning_owner.py`

**Interfaces:**
- Consumes: `provisioning_landing.provisioning_summary` (Task 2), `integrations_repo.remove_provider_resource` (Task 1), `integration_secret_store.delete(owner_id, provider)` (`integration_secret_store.py:77`), `require_always_on_entitlement` (`routers/teams/_guards.py:20`), `require_org_admin` (`core/auth.py:118`), `resolve_owner_id` (`core/auth.py:97`).
- Produces:
  - `GET /api/v1/company/provisioning/resources` → `{"resources": [...], "stripe_cli": "paired"|"unpaired"}`
  - `POST /api/v1/company/provisioning/resources/{provider}/{resource_id}/revoke` → `{"revoked": true}`

There is deliberately **no owner-facing catalog or provision route in P0b.** Provisioning is Friday's job (spec §3.1's trigger split); the owner's surface is the two cards plus this list.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/routers/test_company_provisioning_owner.py
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext, get_current_user  # noqa: E402
from main import app  # noqa: E402
from routers import company_provisioning as mod  # noqa: E402

RES = {
    "resource_id": "res_1", "provider": "posthog", "service": "analytics", "status": "active",
    "resource_name": "Northwind analytics", "env_keys": ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
    "dashboard_url": "https://us.posthog.com/project/1", "level": "company",
}


@pytest.fixture
def client():
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_admin", org_id="org_a", org_role="org:admin", email="founder@example.com"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def member_client():
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_member", org_id="org_a", org_role="org:member", email="m@example.com"
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _paid(monkeypatch):
    monkeypatch.setattr(mod, "require_always_on_entitlement", AsyncMock())


@pytest.fixture(autouse=True)
def facts(monkeypatch):
    writer = AsyncMock()
    monkeypatch.setattr(mod, "write_owner_facts", writer)
    return writer


def test_resources_returns_pointers_and_the_pairing_state(client):
    with patch.object(mod.provisioning_landing, "provisioning_summary",
                      AsyncMock(return_value=("paired", [RES]))):
        resp = client.get("/api/v1/company/provisioning/resources")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stripe_cli"] == "paired"
    assert body["resources"][0]["level"] == "company"
    assert body["resources"][0]["resource_name"] == "Northwind analytics"


def test_revoke_deletes_our_copies_only(client, facts):
    with (
        patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock(return_value=[])) as drop,
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
    ):
        resp = client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 200
    assert resp.json() == {"revoked": True}
    drop.assert_awaited_once_with("org_a", "posthog", "res_1")
    sm_del.assert_awaited_once_with("org_a", "posthog")
    facts.assert_awaited_once_with("org_a")


def test_revoke_keeps_the_secret_while_other_resources_remain(client):
    """The Secrets Manager blob is per-(owner, provider), so it only goes with
    the LAST resource for that provider."""
    with (
        patch.object(mod.integrations_repo, "remove_provider_resource",
                     AsyncMock(return_value=[{"resource_id": "res_2"}])),
        patch.object(mod.integration_secret_store, "delete", AsyncMock()) as sm_del,
    ):
        resp = client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 200
    sm_del.assert_not_awaited()


def test_revoke_is_org_admin_only(member_client):
    resp = member_client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 403


def test_revoke_is_paid_only(client, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(
        mod, "require_always_on_entitlement",
        AsyncMock(side_effect=HTTPException(
            status_code=402, detail={"blocked": True, "code": "routines_require_subscription"})),
    )
    resp = client.post("/api/v1/company/provisioning/resources/posthog/res_1/revoke")
    assert resp.status_code == 402
    assert resp.json()["detail"]["code"] == "routines_require_subscription"


def test_revoke_rejects_a_hostile_provider_segment(client):
    with patch.object(mod.integrations_repo, "remove_provider_resource", AsyncMock()) as drop:
        resp = client.post("/api/v1/company/provisioning/resources/PostHog!/res_1/revoke")
    assert resp.status_code == 422
    drop.assert_not_awaited()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_company_provisioning_owner.py -v`
Expected: FAIL with 404s (routes missing).

- [ ] **Step 3: Implement**

Append to `apps/backend/routers/company_provisioning.py`:

```python
# ---------------------------------------------------------------------------
# Owner-facing (Clerk). Paid-only and org-admin-only for the mutation, exactly
# like P0a's company-Stripe router. There is deliberately NO catalog or
# provision route here: provisioning is Friday's job (spec §3.1's trigger
# split), and the owner's surface is the two approval cards plus this list.
# ---------------------------------------------------------------------------


def _gate_admin(auth: AuthContext) -> None:
    if auth.is_org_context:
        require_org_admin(auth)


@router.get("/resources")
async def resources(auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = resolve_owner_id(auth)
    stripe_cli, rows = await provisioning_landing.provisioning_summary(owner_id)
    return {"resources": rows, "stripe_cli": stripe_cli}


@router.post("/resources/{provider}/{resource_id}/revoke")
async def revoke(
    provider: str, resource_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]
):
    """Delete OUR credential copy and pointer.

    Deliberately does NOT remove the provider resource: it is the owner's and
    stays theirs (spec §3.6). Removing it is a separate, explicit action they
    take in the provider's own dashboard.
    """
    if not _NAME_RE.match(provider) or not _RESOURCE_ID_RE.match(resource_id):
        raise HTTPException(status_code=422, detail="bad provider or resource id")
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id)
    remaining = await integrations_repo.remove_provider_resource(owner_id, provider, resource_id)
    if not remaining:
        await integration_secret_store.delete(owner_id, provider)
    await write_owner_facts(owner_id)
    return {"revoked": True}
```

The imports Task 4 flagged as unused (`Annotated`, `Depends`, `AuthContext`, `get_current_user`, `require_org_admin`, `resolve_owner_id`, `require_always_on_entitlement`, `integrations_repo`, `integration_secret_store`) are all consumed here.

- [ ] **Step 4: Run to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_company_provisioning_owner.py tests/unit/routers/test_company_provisioning_container.py -v && uv run ruff check routers/company_provisioning.py`
Expected: 7 + 12 passed, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/company_provisioning.py apps/backend/tests/unit/routers/test_company_provisioning_owner.py
git commit -m "feat(provisioning): owner routes to list resources and revoke our copy"
```

---

### Task 7: Export exclusions and the wipe rule — proven, not assumed

**Files:**
- Modify: `apps/backend/core/services/agent_export.py:124-136` (`_SKIP_DIR_NAMES`)
- Test: `apps/backend/tests/unit/services/test_agent_export_projects_exclusions.py`
- Test: `apps/backend/tests/unit/services/test_wipe_never_deprovisions.py`

**Interfaces:**
- Consumes: `agent_export._build_tarball(agent_dir: Path, *, paths: list[str] | None = None) -> tuple[bytes, list[str], list[str]]` (`agent_export.py:523`); `account_wipe.wipe_account(owner_id, *, actor_admin_id, account_emails=None)` (`account_wipe.py:609`).
- Produces: `.projects` and `.stripe` in `_SKIP_DIR_NAMES` as a NAMED backstop.

Two structural facts make this mostly a proof rather than a change, and the tests fail loudly if either stops being true: the CLI's state lives at the EFS root while the export tars `workspace-{id}/`, and every dot-prefixed segment is dropped at every depth.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_agent_export_projects_exclusions.py
"""Spec §3.6: a marketplace artifact must never carry `.projects/`, the Stripe
CLI config, or provider credentials. A hired agent arrives with no accounts."""

import io
import os
import tarfile

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.agent_export import _SKIP_DIR_NAMES, _build_tarball  # noqa: E402


@pytest.fixture
def dirty_workspace(tmp_path):
    ws = tmp_path / "workspace-agent_1"
    (ws / "skills").mkdir(parents=True)
    (ws / "AGENTS.md").write_text("persona\n")
    (ws / "skills" / "sell.md").write_text("how to sell\n")
    # Everything below must be absent from the artifact.
    (ws / ".projects" / "vault").mkdir(parents=True)
    (ws / ".projects" / "vault" / "posthog.json").write_text('{"token":"phc_live_leak"}\n')
    (ws / ".projects" / "state.json").write_text('{"project":"p_1"}\n')
    (ws / ".stripe").mkdir()
    (ws / ".stripe" / "config.toml").write_text("[default]\napi_key = sk_live_leak\n")
    (ws / ".config" / "stripe").mkdir(parents=True)
    (ws / ".config" / "stripe" / "config.toml").write_text("[default]\napi_key = sk_live_leak\n")
    (ws / ".env").write_text("NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN=phc_live_leak\n")
    (ws / "posthog-credentials.json").write_text('{"token":"phc_live_leak"}\n')
    return ws


def _members(blob: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        return tf.getnames()


def test_tarball_omits_projects_stripe_and_credential_files(dirty_workspace):
    blob, contents, _pruned = _build_tarball(dirty_workspace)
    names = " ".join(_members(blob)) + " " + " ".join(contents)
    for leaked in (".projects", ".stripe", ".config", ".env", "posthog-credentials.json"):
        assert leaked not in names, f"{leaked} reached the artifact"
    assert "phc_live_leak" not in blob.decode("latin-1")
    assert "sk_live_leak" not in blob.decode("latin-1")
    # The real workspace still ships.
    assert any(n.endswith("AGENTS.md") for n in _members(blob))
    assert any(n.endswith("skills/sell.md") for n in _members(blob))


def test_explicit_export_paths_cannot_smuggle_the_projects_dir(dirty_workspace):
    """A seller naming `.projects` explicitly must still get nothing."""
    blob, contents, _ = _build_tarball(dirty_workspace, paths=[".projects", "AGENTS.md"])
    assert ".projects" not in " ".join(_members(blob) + contents)
    assert any(n.endswith("AGENTS.md") for n in _members(blob))


def test_projects_and_stripe_are_named_backstops():
    """The dotfile rule already covers these; the named entries are the
    greppable intent, so a future relaxation of that rule still holds."""
    assert ".projects" in _SKIP_DIR_NAMES
    assert ".stripe" in _SKIP_DIR_NAMES


def test_a_copied_non_dot_vault_is_still_caught(dirty_workspace):
    """A seller who copies the vault to a normal name defeats the dotfile rule.
    `projects-vault/` is not excluded by NAME — the credential-shape filename
    guard is what catches the payload."""
    copied = dirty_workspace / "projects-vault"
    copied.mkdir()
    (copied / "credentials.json").write_text('{"token":"phc_live_leak"}\n')
    blob, _contents, _ = _build_tarball(dirty_workspace)
    assert "phc_live_leak" not in blob.decode("latin-1")
```

```python
# apps/backend/tests/unit/services/test_wipe_never_deprovisions.py
"""Spec §3.6: teardown revokes OUR link and deletes OUR credential copies. It
NEVER calls Projects `remove` or touches an owner-owned provider resource."""

import inspect
import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import account_wipe, entitlement_teardown, owner_secret_sweep  # noqa: E402


def test_no_teardown_module_can_reach_the_provisioning_rail():
    """Static guard, and the cheapest one: nothing in teardown may import the
    provisioning modules, so it cannot deprovision even by accident. If a future
    change needs one of them here, that is a design decision to argue for — not
    something to slip past by deleting this test."""
    for module in (account_wipe, entitlement_teardown, owner_secret_sweep):
        source = inspect.getsource(module)
        assert "provisioning_landing" not in source, f"{module.__name__} can now reach the provisioning rail"
        assert "provisioning_approvals" not in source, f"{module.__name__} can now reach the provisioning rail"
        assert "projects remove" not in source


def test_wipe_still_deletes_our_own_copies():
    """The complement: our Secrets Manager copies and integration rows DO go
    (steps 9f/9g, account_wipe.py:910-920)."""
    source = inspect.getsource(account_wipe)
    assert "delete_owner_secrets" in source
    assert "integrations_repo.delete_all_for_owner" in source
```

- [ ] **Step 2: Run to verify only the backstop assertion fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export_projects_exclusions.py tests/unit/services/test_wipe_never_deprovisions.py -v`
Expected: `test_projects_and_stripe_are_named_backstops` FAILS; every other test PASSES — that is the point: the structural exclusions already hold.

- [ ] **Step 3: Add the named backstop**

In `apps/backend/core/services/agent_export.py`, add to `_SKIP_DIR_NAMES` (line 124-136), keeping the set alphabetical:

```python
        ".projects",
        ".stripe",
```

and extend the comment above that constant with:

```python
# ``.projects`` / ``.stripe`` are the Stripe Projects vault and CLI session
# (spec §3.6). They are ALREADY excluded twice — Friday runs the CLI in the EFS
# root, a sibling of every workspace-{id}/, and the dotfile rule below drops
# them at any depth. Naming them here is the greppable intent so a future
# relaxation of that rule cannot quietly ship an owner's provider credentials
# to a buyer.
```

- [ ] **Step 4: Run everything, plus the existing export suite**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_export_projects_exclusions.py tests/unit/services/test_wipe_never_deprovisions.py tests/unit/services/test_agent_export.py -v`
Expected: 4 + 2 + existing passed.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/agent_export.py apps/backend/tests/unit/services/test_agent_export_projects_exclusions.py apps/backend/tests/unit/services/test_wipe_never_deprovisions.py
git commit -m "test(provisioning): prove export excludes .projects/.stripe and wipe never deprovisions"
```

---

### Task 8: `isol8-facts.json` and the skill — the full CLI procedure

**Files:**
- Modify: `apps/backend/core/services/owner_facts.py:50-114` (`build_owner_facts`), `:139-157` (`write_owner_facts`)
- Modify: `apps/infra/openclaw/skills/isol8/SKILL.md` (new section after "Your company's Stripe account", which ends at line 298)
- Test: `apps/backend/tests/unit/services/test_owner_facts_provisioning.py`

**Interfaces:**
- Consumes (verified, quoted from `owner_facts.py:50`):
  ```python
  def build_owner_facts(owner_id: str, billing_account: dict | None, *, email_address: str | None = None) -> dict:
  ```
  It is documented "Pure — no I/O", so the two new facts arrive as parameters.
  Plus `provisioning_landing.provisioning_summary(owner_id)` (Task 2).
- Produces: `build_owner_facts(..., stripe_cli: str = "unpaired", provisioned: list[str] | None = None)` adding `"stripe_cli"` and `"provisioned"`; `write_owner_facts` supplies them.

**Read cost — the decision the plan owes you.** `write_owner_facts` already does two reads (`billing_repo.get_by_owner_id` + `_lookup_email_address`). Both new facts come from **one additional `integrations_repo.list_for_owner` query** via the `provisioning_summary` helper Task 2 already wrote for the Accounts route — one helper, two callers, no second derivation to drift. `stripe_cli` is deliberately derived rather than probed: a successful `landed` post proves pairing worked, and probing the container would cost a websocket round-trip on a path that runs on every billing mutation.

`provisioning_landing` is imported lazily inside the function, matching this module's existing lazy imports for `is_org_owner_id` and `provider_choice_from_row` — the module deliberately avoids pulling in repos it does not otherwise need.

- [ ] **Step 1: Write the failing tests**

```python
# apps/backend/tests/unit/services/test_owner_facts_provisioning.py
import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import owner_facts as mod  # noqa: E402


def test_defaults_are_unpaired_and_empty():
    facts = mod.build_owner_facts("org_a", {"subscription_status": "active"})
    assert facts["stripe_cli"] == "unpaired"
    assert facts["provisioned"] == []


def test_facts_carry_the_pairing_state_and_provisioned_pairs():
    facts = mod.build_owner_facts(
        "org_a", {"subscription_status": "active"},
        stripe_cli="paired", provisioned=["posthog/analytics", "vercel/hosting"],
    )
    assert facts["stripe_cli"] == "paired"
    assert facts["provisioned"] == ["posthog/analytics", "vercel/hosting"]


@pytest.mark.asyncio
async def test_writer_derives_both_from_the_shared_summary():
    summary = ("paired", [{"provider": "posthog", "service": "analytics"},
                          {"provider": "posthog", "service": "analytics"}])
    written: dict = {}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})),
        patch.object(mod, "_lookup_email_address", AsyncMock(return_value=None)),
        patch("core.services.provisioning_landing.provisioning_summary",
              AsyncMock(return_value=summary)) as summarize,
        patch.object(mod, "get_workspace") as ws,
    ):
        ws.return_value.write_file.side_effect = lambda owner, name, body: written.update(body=body)
        await mod.write_owner_facts("org_a")
    summarize.assert_awaited_once_with("org_a")
    assert '"stripe_cli": "paired"' in written["body"]
    # De-duplicated: two resources on the same provider/service is one entry.
    assert written["body"].count('"posthog/analytics"') == 1


@pytest.mark.asyncio
async def test_a_failed_summary_never_blocks_the_facts_write():
    written: dict = {}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})),
        patch.object(mod, "_lookup_email_address", AsyncMock(return_value=None)),
        patch("core.services.provisioning_landing.provisioning_summary",
              AsyncMock(side_effect=RuntimeError("ddb down"))),
        patch.object(mod, "get_workspace") as ws,
    ):
        ws.return_value.write_file.side_effect = lambda owner, name, body: written.update(body=body)
        await mod.write_owner_facts("org_a")
    assert '"stripe_cli": "unpaired"' in written["body"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_owner_facts_provisioning.py -v`
Expected: FAIL with `KeyError: 'stripe_cli'`.

- [ ] **Step 3: Implement**

In `apps/backend/core/services/owner_facts.py`:

- Change the signature at line 50 to:

```python
def build_owner_facts(
    owner_id: str,
    billing_account: dict | None,
    *,
    email_address: str | None = None,
    stripe_cli: str = "unpaired",
    provisioned: list[str] | None = None,
) -> dict:
```

- In the returned dict (line 88-114), directly after `"company_stripe": company_stripe,` add:

```python
        # The provisioning rail (spec §3.2). "paired" means the owner completed
        # `stripe login` in this container at least once, derived from the
        # presence of provisioned resources — Friday's "don't ask twice" hint.
        "stripe_cli": stripe_cli,
        # "provider/service" pairs the owner already has on their OWN identity,
        # so Friday never re-provisions what exists.
        "provisioned": list(provisioned or []),
```

- Add the derivation helper next to `_lookup_email_address`:

```python
async def _lookup_provisioned(owner_id: str) -> tuple[str, list[str]]:
    """``(stripe_cli, ["provider/service", …])`` from the SHARED summary.

    Lazy import for the same reason as this module's other lazy imports: the
    facts file must not pull in repos it does not otherwise need. Never raises —
    the facts write must not depend on this.
    """
    try:
        from core.services.provisioning_landing import provisioning_summary

        state, resources = await provisioning_summary(owner_id)
    except Exception as e:  # noqa: BLE001 — facts must never block callers
        logger.warning("owner_facts: provisioning summary failed for %s: %s", owner_id, e)
        return "unpaired", []
    pairs = {
        f"{r['provider']}/{r['service']}"
        for r in resources
        if r.get("provider") and r.get("service")
    }
    return state, sorted(pairs)
```

- In `write_owner_facts` (line 150-155), replace the `facts = build_owner_facts(...)` line with:

```python
        stripe_cli, provisioned = await _lookup_provisioned(owner_id)
        facts = build_owner_facts(
            owner_id,
            account,
            email_address=await _lookup_email_address(owner_id),
            stripe_cli=stripe_cli,
            provisioned=provisioned,
        )
```

- [ ] **Step 4: Add the skill section**

In `apps/infra/openclaw/skills/isol8/SKILL.md`, directly after the "Your company's Stripe account" section (ends line 298, before `## Skills and self-improvement`), insert:

```markdown
## Setting up accounts for the company (Stripe Projects)

When a task needs a real service — analytics, hosting, a database, email
sending — you do **not** sign up in a browser and you do **not** create an
account in Lightbulb's name. The owner gets the account **on their own Stripe
identity**, through Stripe Projects. You run the CLI; the platform handles the
owner's two decisions and stores the credentials.

Read `~/.openclaw/isol8-facts.json` first — never ask twice:

- `provisioned: ["posthog/analytics", …]` — already done; use it, don't repeat it.
- `stripe_cli: "paired"` — the CLI is connected, skip to step 3.
- `stripe_cli: "unpaired"` — start at step 1.

**Always work in `/home/node/.openclaw`.** That is the owner's durable storage.
Running the CLI anywhere else puts `.projects/` inside an agent workspace, where
it would ride into a published marketplace agent.

```bash
cd /home/node/.openclaw
stripe --version || { echo "no Stripe CLI in this image yet"; }   # if this fails, tell the owner and stop
stripe plugin install projects                                     # idempotent
[ -d .projects ] || stripe projects init --yes                     # once per company
```

**Step 1 — connect the CLI (once).** Run the login and file the card:

```bash
stripe login --non-interactive        # prints {browser_url, verification_code, next_step}
curl -s -X POST -H "Authorization: Bearer $OPENCLAW_HOOK_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"browser_url":"<browser_url>","verification_code":"<verification_code>","issue_id":"'"$PAPERCLIP_TASK_ID"'"}' \
  "$ISOL8_API_URL/api/v1/company/provisioning/u/$ISOL8_OWNER_ID/pairing"
```

Then run the `next_step` command the login printed — it blocks until the owner
confirms in the browser, then saves the session. Confirm with
`stripe login list`. Tell the owner you asked and keep working on everything
that does not need the account; this is never a gate.

**Step 2 — get the owner's consent for the provider.** Read the real terms and
price first, then file the card:

```bash
stripe projects catalog --json        # confirm the provider/service exists; read tier + terms
curl -s -X POST -H "Authorization: Bearer $OPENCLAW_HOOK_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"posthog","service":"analytics","terms_url":"<terms url>","pricing":"<what it costs, plainly>"}' \
  "$ISOL8_API_URL/api/v1/company/provisioning/u/$ISOL8_OWNER_ID/consent"
```

Wait for the owner to approve that card before step 3. `--accept-tos` is you
saying *they* accepted; never use it to walk past a decision that is theirs.

**Step 3 — add the service, then report it.** Read `stripe projects add --help`
before your first use — flags change between CLI versions.

```bash
stripe projects add posthog/analytics --json --accept-tos --no-interactive
stripe projects env --json            # the variable names and values it created
curl -s -X POST -H "Authorization: Bearer $OPENCLAW_HOOK_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"posthog","service":"analytics","resource_id":"<id>","resource_name":"<name>",
       "env":{"NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN":"<value>"},"dashboard_url":"<url>"}' \
  "$ISOL8_API_URL/api/v1/company/provisioning/u/$ISOL8_OWNER_ID/landed"
```

The platform stores the credential and injects it as an environment variable
for you. From then on use `$NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN`, never the value.

Rules that are not negotiable:

- **Never** run `stripe projects billing add`. The owner arranges their own
  billing, on their own Stripe.
- **Never** run `stripe projects remove`. It destroys something the owner owns;
  ask them to do it in the provider's dashboard.
- **Never** paste a credential value into chat, a Paperclip issue, a work
  product, or a file. The only place a value ever goes is the `landed` body.
- On `503` from any of these calls the board was unreachable — say so and retry
  next turn. On `502` the board REFUSED; retrying changes nothing, so report
  what it said, verbatim, and move on.

Register a work product pointing at the new resource so the company has a
receipt for what you created.
```

- [ ] **Step 5: Run to verify the tests pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_owner_facts_provisioning.py tests/unit/services/test_provisioning_cli_paths.py -v && uv run pytest tests/unit -k owner_facts -q`
Expected: 4 + 4 passed (Task 5's skill assertion now passes); the existing owner-facts tests still green.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/owner_facts.py apps/infra/openclaw/skills/isol8/SKILL.md apps/backend/tests/unit/services/test_owner_facts_provisioning.py
git commit -m "feat(provisioning): stripe_cli + provisioned facts and the full CLI procedure in Friday's skill"
```

---

### Task 9: Frontend — the Accounts section and the two approval-row variants

**Files:**
- Create: `apps/frontend/src/components/settings/CompanyAccountsSection.tsx`
- Modify: `apps/frontend/src/app/settings/connections/page.tsx:192-195` (render below `<CompanyStripeSection>`)
- Modify: `apps/frontend/src/components/teams/inbox/lib/approvalLabel.ts:23-29`
- Modify: `apps/frontend/src/components/teams/inbox/ApprovalRow.tsx:56`
- Test: `apps/frontend/src/components/settings/__tests__/CompanyAccountsSection.test.tsx`

**Interfaces:**
- Consumes: `useApi()` from `@/lib/api` (`get`/`post`); `openInNewTab(url: string): boolean` from `@/components/settings/openInNewTab`; `notifyApiError(err, fallback)` / `notifyError(message)` from `@/lib/notify` (`notify.tsx:96`, `:198`); the Task 6 routes.
- Produces: `CompanyAccountsSection({ canManage }: { canManage: boolean })`; `isStripeCliPairApproval(approval)` and `isProviderConsentApproval(approval)` in `approvalLabel.ts`.

- [ ] **Step 1: Write the failing test**

```tsx
// apps/frontend/src/components/settings/__tests__/CompanyAccountsSection.test.tsx
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { SWRConfig } from "swr";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, post }) }));

import { CompanyAccountsSection } from "@/components/settings/CompanyAccountsSection";

// Fresh SWR cache per render (same convention as CompanyStripeSection.test.tsx).
function renderSection(canManage: boolean) {
  return render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <CompanyAccountsSection canManage={canManage} />
    </SWRConfig>,
  );
}

const ONE = {
  resources: [{
    resource_id: "res_1", provider: "posthog", service: "analytics", status: "active",
    resource_name: "Northwind analytics", env_keys: ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
    dashboard_url: "https://us.posthog.com/project/1", level: "company",
  }],
  stripe_cli: "paired",
};

describe("CompanyAccountsSection", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
  });

  it("says the CLI is not connected when nothing is provisioned", async () => {
    get.mockResolvedValue({ resources: [], stripe_cli: "unpaired" });
    renderSection(true);
    expect(await screen.findByText("Stripe CLI: not connected")).toBeInTheDocument();
    // No owner-side pairing button: pairing starts from the agent's card.
    expect(screen.queryByText("Pair Stripe CLI")).toBeNull();
    expect(screen.getByText(/Ask your agents/)).toBeInTheDocument();
  });

  it("renders one row per resource with its level and a dashboard link", async () => {
    get.mockResolvedValue(ONE);
    renderSection(true);
    expect(await screen.findByText("posthog")).toBeInTheDocument();
    expect(screen.getByText("analytics")).toBeInTheDocument();
    expect(screen.getByText("Northwind analytics")).toBeInTheDocument();
    expect(screen.getByText("Company-level")).toBeInTheDocument();
    expect(screen.getByText("Open dashboard")).toHaveAttribute("href", "https://us.posthog.com/project/1");
    expect(screen.getByText("Stripe CLI: connected")).toBeInTheDocument();
  });

  it("falls back to the provider homepage when there is no dashboard url", async () => {
    get.mockResolvedValue({ ...ONE, resources: [{ ...ONE.resources[0], dashboard_url: null }] });
    renderSection(true);
    expect(await screen.findByText("Open dashboard")).toHaveAttribute("href", "https://posthog.com");
  });

  it("revoke deletes our copy and says the owner keeps the account", async () => {
    get.mockResolvedValue(ONE);
    post.mockResolvedValue({ revoked: true });
    renderSection(true);
    fireEvent.click(await screen.findByText("Revoke"));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/company/provisioning/resources/posthog/res_1/revoke", {}),
    );
    expect(await screen.findByText(/You keep the account/)).toBeInTheDocument();
  });

  it("hides revoke for non-admins", async () => {
    get.mockResolvedValue(ONE);
    renderSection(false);
    await screen.findByText("posthog");
    expect(screen.queryByText("Revoke")).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/settings/__tests__/CompanyAccountsSection.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the section**

```tsx
// apps/frontend/src/components/settings/CompanyAccountsSection.tsx
"use client";

import { useState } from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";

// Fallback deep links for providers whose `landed` post carried no dashboard
// URL. Only providers the Projects catalog actually serves (spec §3.2).
const PROVIDER_HOME: Record<string, string> = {
  posthog: "https://posthog.com",
  vercel: "https://vercel.com/dashboard",
  supabase: "https://supabase.com/dashboard",
  neon: "https://console.neon.tech",
  clerk: "https://dashboard.clerk.com",
  cloudflare: "https://dash.cloudflare.com",
  twilio: "https://console.twilio.com",
  resend: "https://resend.com/overview",
  sentry: "https://sentry.io",
  e2b: "https://e2b.dev/dashboard",
};

type Resource = {
  resource_id: string;
  provider: string;
  service: string;
  status: string;
  resource_name: string;
  env_keys: string[];
  dashboard_url: string | null;
  level: string;
};

type AccountsPayload = { resources: Resource[]; stripe_cli: "paired" | "unpaired" };

const PATH = "/company/provisioning/resources";

export function CompanyAccountsSection({ canManage }: { canManage: boolean }) {
  const api = useApi();
  const { data, mutate, isLoading } = useSWR<AccountsPayload>(
    PATH,
    (p: string) => api.get(p) as Promise<AccountsPayload>,
  );
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  async function revoke(r: Resource) {
    setBusy(true);
    try {
      await api.post(`/company/provisioning/resources/${r.provider}/${r.resource_id}/revoke`, {});
      setNote(
        "Disconnected from Lightbulb AI. You keep the account — remove it in the provider's own dashboard if you want it gone.",
      );
      await mutate();
    } catch (e) {
      notifyApiError(e, "Couldn't disconnect that account. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="settings-card" data-testid="company-accounts">
      <div className="settings-card-header">
        <h2 className="settings-card-title">Accounts</h2>
        <p className="settings-card-desc">
          Provider accounts your agents set up <strong>on your own Stripe identity</strong>. You own
          every one of them and keep them if you leave — Lightbulb AI only holds a copy of the
          credential so your agents can use it.
        </p>
      </div>

      {isLoading && <p className="text-sm text-[var(--mute)]">Loading…</p>}

      {!isLoading && (
        <>
          <p className="text-sm text-[var(--mute)] mb-3">
            {data?.stripe_cli === "paired" ? "Stripe CLI: connected" : "Stripe CLI: not connected"}
          </p>

          {note && <p className="text-sm text-[var(--mute)] mb-3">{note}</p>}

          {(data?.resources.length ?? 0) === 0 ? (
            <p className="text-sm text-[var(--mute)]">
              No provider accounts yet. Ask your agents for what the business needs — analytics,
              hosting, email — and they will ask you to approve each one here.
            </p>
          ) : (
            <ul className="flex flex-col gap-2">
              {data?.resources.map((r) => (
                <li key={`${r.provider}/${r.resource_id}`} className="flex flex-wrap items-center gap-3 text-sm">
                  <span className="font-medium text-[#1c1917]">{r.provider}</span>
                  <span className="text-[var(--mute)]">{r.service}</span>
                  {r.resource_name && <span className="text-[var(--mute)]">{r.resource_name}</span>}
                  <span className="text-[var(--mute)]">{r.status}</span>
                  {/* Spec §3.4: state the level per resource. */}
                  <span className="text-[var(--mute)]">
                    {r.level === "company" ? "Company-level" : "Agent-level"}
                  </span>
                  <a
                    className="settings-btn-outline"
                    href={r.dashboard_url ?? PROVIDER_HOME[r.provider] ?? `https://${r.provider}.com`}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Open dashboard
                  </a>
                  {canManage && (
                    <button type="button" className="settings-btn-outline" disabled={busy} onClick={() => revoke(r)}>
                      Revoke
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
```

All class names already exist in `apps/frontend/src/app/settings/settings.css` (`settings-card`, `settings-card-header`, `settings-card-title`, `settings-card-desc`, `settings-btn-outline`) — add no new CSS.

- [ ] **Step 4: Mount it**

In `apps/frontend/src/app/settings/connections/page.tsx`, import `CompanyAccountsSection` and add it directly below the existing `CompanyStripeSection` block (lines 192-195), reusing the same `canManage`:

```tsx
              <div className="mb-4">
                <CompanyAccountsSection canManage={canManage} />
              </div>
```

- [ ] **Step 5: Add the two approval-row variants**

In `apps/frontend/src/components/teams/inbox/lib/approvalLabel.ts`, beside `COMPANY_STRIPE_ACTION` (line 23) add:

```ts
/** P0b's two provisioning cards. Same reason as COMPANY_STRIPE_ACTION:
 *  Paperclip validates `type` against its own four-value enum, so the marker
 *  in the payload is what identifies these as ours. */
const STRIPE_CLI_PAIR_ACTION = "stripe_cli_login";
const PROVIDER_CONSENT_ACTION = "provider_consent";

export function isStripeCliPairApproval(
  approval?: { payload?: Record<string, unknown> | null } | null,
): boolean {
  return approval?.payload?.isol8_action === STRIPE_CLI_PAIR_ACTION;
}

export function isProviderConsentApproval(
  approval?: { payload?: Record<string, unknown> | null } | null,
): boolean {
  return approval?.payload?.isol8_action === PROVIDER_CONSENT_ACTION;
}
```

In `apps/frontend/src/components/teams/inbox/ApprovalRow.tsx`, extend the `isCompanyStripeApproval(approval) ? … : …` chain (line 56) with two more cases before the generic Approve button:

```tsx
            ) : isStripeCliPairApproval(approval) ? (
              <>
                <span className="text-sm">
                  Code: <strong className="font-mono">{String(approval.payload?.verification_code ?? "")}</strong>
                </span>
                <EditorialButton
                  size="sm"
                  disabled={busy}
                  onClick={() => {
                    const url = String(approval.payload?.browser_url ?? "");
                    if (!url || !openInNewTab(url)) {
                      notifyError(POPUP_BLOCKED_MSG);
                    }
                  }}
                >
                  Open Stripe
                </EditorialButton>
                <EditorialButton size="sm" disabled={busy} onClick={() => run(() => approve(approval.id))}>
                  Approve
                </EditorialButton>
              </>
            ) : isProviderConsentApproval(approval) ? (
              <>
                <span className="text-sm">
                  {String(approval.payload?.provider ?? "")} · {String(approval.payload?.service ?? "")}
                  {approval.payload?.pricing ? ` · ${String(approval.payload.pricing)}` : ""}
                </span>
                {approval.payload?.terms_url ? (
                  <a
                    className="text-sm underline"
                    href={String(approval.payload.terms_url)}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Terms
                  </a>
                ) : null}
                <EditorialButton size="sm" disabled={busy} onClick={() => run(() => approve(approval.id))}>
                  Approve
                </EditorialButton>
              </>
```

Add `isStripeCliPairApproval` and `isProviderConsentApproval` to the existing `approvalLabel` import. `notifyError`, `POPUP_BLOCKED_MSG`, `openInNewTab` and `run` are already in this file from P0a — reuse them rather than redeclaring. The Reject button after the chain is unchanged, so both cards stay rejectable.

Deliberately: the pairing card's "Open Stripe" does **not** auto-approve. The owner must confirm the code in Stripe and then click Approve, so an approved card means the owner actually completed the step.

- [ ] **Step 6: Run tests, typecheck, lint**

Run: `cd apps/frontend && pnpm vitest run src/components/settings/__tests__/CompanyAccountsSection.test.tsx src/components/settings/__tests__/CompanyStripeSection.test.tsx && pnpm tsc --noEmit && pnpm lint`
Expected: 5 + 4 passed; tsc and lint clean.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/components/settings/CompanyAccountsSection.tsx apps/frontend/src/app/settings/connections/page.tsx apps/frontend/src/components/teams/inbox/lib/approvalLabel.ts apps/frontend/src/components/teams/inbox/ApprovalRow.tsx apps/frontend/src/components/settings/__tests__/CompanyAccountsSection.test.tsx
git commit -m "feat(provisioning): Accounts section and the pairing / provider-consent approval rows"
```

---

### Task 10: Rebase, PR, dev verification

**Files:** none new.

- [ ] **Step 1: Rebase on P0a and run the backend suite**

```bash
git fetch origin && git rebase origin/feat/company-stripe-spine
cd apps/backend && uv run pytest tests/unit -q && uv run ruff check .
```

- [ ] **Step 2: Open the PR and watch CI**

```bash
git push -u origin HEAD
gh pr create --title "feat(provisioning): Stripe Projects on the owner's identity — Friday drives the CLI (P0b)" --body "$(cat <<'EOF'
Implements P0b of docs/superpowers/specs/2026-08-28-owner-data-plane-design.md (§3.2, §3.4, §3.5, §3.6).

Stacked on #2157 (P0a). Review after that merges.

**Friday drives the CLI; the backend never does.** Spec §3.2 puts the fallback in her hands ("Friday runs `next_step`") and §3.1's trigger split gives her the work once the door is open — so there is no backend→container remote-exec path and no door abstraction here.

- Stripe CLI v1.50.6 baked into the OpenClaw image (pinned + sha256). Friday runs it in `/home/node/.openclaw` — the EFS root — so the session survives task replacement and `.projects/` lands outside every `workspace-{agent_id}/`.
- Three container routes (hook-token auth, P0a's error split): `pairing` files the `stripe_cli_login` card with the browser URL + verification code; `consent` files the `provider_consent` card with the provider, terms link and price; `landed` accepts the credentials she reports back.
- Landing writes each value to Secrets Manager at the existing integrations path, records pointers (`actor: "projects"`, `provider_resources`) on the integrations row, and injects the values as OpenClaw `env.vars` — re-asserted through `build_openclaw_config_dict` so a reprovision cannot drop them. No value is ever logged or returned by the API.
- Owner surface: the two approval rows plus an Accounts section (per-resource provider/service/name/status/level, dashboard deep link, revoke-our-copy-only). Paid + org-admin on the mutation.
- Tests prove the two spec invariants rather than assuming them: a marketplace tarball built from a workspace containing `.projects/`, `.stripe/`, `.config/stripe/` and a credentials JSON carries none of them; and no teardown module can even import the provisioning rail.
- **`openclaw-version.json` is deliberately NOT bumped** — promotion is a separate manual PR, and until then `stripe --version` fails and the skill tells Friday to say so.

Named follow-ups: the credential proxy for money-authority keys (spec §3.5), and the Projects-for-Platforms API path once the preview is granted — it lands through the same `provisioning_landing.land_credentials` seam, with a backend-initiated provision replacing Friday's CLI half.

Owner actions: send the Projects-for-Platforms preview request to projectsforplatforms@stripe.com (spec §7); promote the new OpenClaw image tag once this merges.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr checks --watch
```

- [ ] **Step 3: Promote the image tag (separate PR) before the dev run**

The dev verification cannot pass until `dev.tag` in `openclaw-version.json` points at a build containing the Stripe CLI layer. Wait for `build-openclaw-image.yml` on the merge commit, open the one-line promotion PR, then roll the dev owner's container **through the admin UI** (owner directive; never direct ECS).

- [ ] **Step 4: Verify on dev with Claude-in-Chrome (paid dev account)**

1. `https://dev.flickmeon.ai/settings/connections` → "Accounts" section renders, "Stripe CLI: not connected", empty-state copy.
2. In chat, ask Friday for "analytics for the site". She runs `stripe login --non-interactive` and files the card; the Inbox row shows the verification code and "Open Stripe".
3. Click "Open Stripe", confirm the code, click Approve. Friday's `next_step` completes; `stripe login list` in the container shows a session.
4. Prove the session is on EFS: roll the container through the admin UI and confirm `stripe login list` still shows it.
5. Friday files the `provider_consent` card for `posthog/analytics`; the row shows the terms link and the price. Approve it.
6. She runs `stripe projects add …` and POSTs `landed`. The Accounts section shows the row with `Company-level` and a working "Open dashboard" link into the owner's OWN PostHog org.
7. `aws secretsmanager get-secret-value --secret-id isol8/dev/integrations/<owner>/posthog --profile isol8-admin` → the token is there. `aws dynamodb get-item --table-name lightbulb-dev-integrations --key '{"owner_id":{"S":"<owner>"},"provider":{"S":"posthog"}}' --profile isol8-admin` → `actor=projects`, `provider_resources` present, **no token value**.
8. In the container, `printenv NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN` returns the value — this resolves the `env.vars` VERIFY; record whether a restart was needed.
9. Click "Revoke" → the row goes, the secret is deleted, and the PostHog project **still exists** in the owner's PostHog org. That is the proof of spec §3.6.
10. Publish that agent to the marketplace and untar the artifact: `.projects/`, `.stripe/` and `.env` are absent.
11. Grep the backend log group for the landed request — confirm no credential value appears in any log line.

- [ ] **Step 5: Report**

Post the eleven results and the resolution of both `VERIFY:`s on the PR as a comment. Do not merge until the owner says so.

---

## Self-Review

### 1. Spec coverage

| Spec requirement | Task | Notes |
|---|---|---|
| §3.2 CLI in the container, paired once by the owner | 5, 8 | Baked binary; the skill carries the full procedure |
| §3.2 `stripe login --non-interactive` → owner confirms → Friday runs `next_step` | 3, 4, 8, 9 | Card carries URL + code; "Open Stripe" then Approve |
| §3.2 `--accept-tos` after the owner's one-time consent | 3, 4, 8, 9 | `provider_consent` card, per provider/service; the skill forbids using the flag to bypass it |
| §3.2 vault at `.projects/` on the owner's EFS | 5, 8 | CWD `/home/node/.openclaw`, asserted by a test against the skill text |
| §3.2 Projects for Platforms API implementation | — | **Not built.** YAGNI until the preview is granted; the seam is named in the PR body and `provisioning_landing.land_credentials` is the join point |
| §3.4 company- vs agent-level, stated per resource | 4, 9 | `level: "company"` on the pointer; the Accounts row renders it |
| §3.5 static env for provider write tokens | 2 | `env.vars` in openclaw.json |
| §3.5 proxy for money authority | — | **Explicitly out of scope**, named in `provisioning_landing`'s docstring and the PR body |
| §3.5 Secrets Manager, never DynamoDB | 1, 2 | `integration_secret_store.put`; the row holds env var NAMES only, asserted by a test |
| §3.6 paid gate | 6 | `require_always_on_entitlement` on the mutation; test asserts 402 |
| §3.6 org admin only | 6 | `require_org_admin` via `_gate_admin`; test asserts 403 for a member |
| §3.6 wipe never deprovisions | 7 | Static import guard + the complement that our own copies still go |
| §3.6 export exclusions | 7 | Four tests + a named backstop in `_SKIP_DIR_NAMES` |
| §5.3 "Accounts": Projects list + integrations + deep links + revoke | 6, 9 | Connect status is P0a's section directly above |
| §6 P0 "preview request" | 10 | Owner action, in the PR body |
| `isol8-facts.json` gains `stripe_cli` + `provisioned` | 8 | One extra query via the shared `provisioning_summary`; justified inline |
| Skill section | 8 | Full procedure; forbids `billing add`, `projects remove`, and values in prose |

**Gaps I am declaring rather than hiding:**
- The **paid gate does not cover provisioning itself**, because provisioning happens in Friday's shell, not through a backend route. The three container routes are hook-token-authed, and a trial owner's container is stopped whenever the tab is closed — but a trial owner whose container is awake could technically have Friday run `stripe projects add`. The fence that matters is the owner's own Stripe account (P0a, which IS paid-gated) and the consent card; if a hard gate is wanted, the `landed` route is the one place to add it, and that is a one-line change with its own test.
- The backend **cannot enforce that Friday waited for the consent card**. It is a skill contract plus an audit record (the card exists, dated, with the terms link). Enforcing it would need the backend to hold approval state per provider and refuse `landed` without it — worth doing if a real agent skips the step, not before.
- §4.2's retained PostHog token and §4.4's work-product register-on-create are P0 items but belong to the data layer and a cross-rail convention respectively; the skill tells Friday to register a work product, and the platform-side convention should land with the first rail that needs it.

### 2. Placeholder scan

Searched for `TBD`, `TODO`, `implement later`, `fill in`, `appropriate error handling`, `handle edge cases`, `similar to Task`, `write tests for the above`. **Zero hits.** Every code step carries real code; every test step carries a full test file.

Two `VERIFY:` markers remain, each with the exact command:
- Task 5 — `stripe plugin install projects` location and auth requirement (decides whether it is baked or stays in the skill).
- Task 2 — whether `env.vars` applies on chokidar reload or needs a restart (decides whether landing queues a Track-2 bounce).

The two dropped VERIFYs (`stripe projects add`/`remove` argv, and whether `add` needs `init`) are now **in the skill text as instructions to Friday** — "read `stripe projects add --help` first", `[ -d .projects ] || stripe projects init --yes`. The backend no longer depends on the CLI's output schema at all: it only sees the normalized `landed` body.

Two smaller uncertainties are handled inline rather than as VERIFYs, because a test pins the behaviour either way: `PaperclipApiError`'s constructor signature (Task 4 Step 5 says to match `test_company_stripe.py` if it differs), and the ordering of `resolve_company_name` vs the company-id lookup in Task 3 (the note tells the implementer to keep the written order so the patch targets hold).

### 3. Type consistency

Checked every symbol crossing a task boundary:
- **The resource pointer dict** is built in exactly one place — `routers/company_provisioning.landed` (Task 4) — with keys `resource_id, provider, service, resource_name, status, env_keys, dashboard_url, level`. The same eight keys appear in Task 1's `_res()` fixture, Task 2's `RES`, Task 6's `RES`, and the frontend `Resource` type in Task 9. No ninth key anywhere.
- `integrations_repo.PROJECTS_ACTOR` — defined Task 1, read in Task 2 (`build_provisioned_env`, `provisioning_summary`). One constant, two readers; the facts file and the Accounts route both go through `provisioning_summary` rather than re-deriving.
- `provisioning_summary(owner_id) -> tuple[str, list[dict]]` — defined Task 2, consumed by Task 6's `/resources` and Task 8's `_lookup_provisioned`. Both destructure `(state, resources)` in that order.
- `land_credentials(*, owner_id, resource, env)` — keyword-only in Task 2, called keyword-only in Task 4, asserted keyword-only in Task 4's test (`land.await_args.kwargs["env"]`).
- `provisioned_env` — the identical kwarg name on `build_openclaw_config_dict`, `write_openclaw_config` and the `ecs_manager` call site (Task 2).
- The two markers: `provisioning_approvals.PAIR_MARKER = "stripe_cli_login"` / `CONSENT_MARKER = "provider_consent"` (Task 3) match `STRIPE_CLI_PAIR_ACTION` / `PROVIDER_CONSENT_ACTION` in `approvalLabel.ts` (Task 9) and the strings asserted in both suites.
- Card payload keys: pairing writes `title, browser_url, verification_code`; consent writes `title, provider, service, terms_url, pricing`. `ApprovalRow` reads exactly those, no others.
- Route paths: the frontend calls `/company/provisioning/resources` and `/company/provisioning/resources/{provider}/{resource_id}/revoke`; the backend declares exactly those under the `/api/v1/company/provisioning` prefix mounted in Task 4. `useApi()` prepends `/api/v1`, matching `CompanyStripeSection`'s existing `/company/stripe/...` calls. The container routes are `/u/{owner_id}/{pairing,consent,landed}` under the same prefix — the P0a shape (`/api/v1/company/stripe` + `/u/{owner_id}/request`), and the skill's curl lines use the full path.
- `_NAME_RE` / `_RESOURCE_ID_RE` are defined once in `routers/company_provisioning.py` (Task 4) and reused by the revoke path validation (Task 6) — not re-declared.
- `build_owner_facts` gained two keyword-only params with defaults, so every existing caller (`ecs_manager.write_user_configs`, `billing_service.change_provider`, `stripe_webhook_handlers.handle_subscription_state`) stays valid — Task 8's `-k owner_facts` run proves it.

One inconsistency found and fixed while writing: the Accounts section originally offered an owner-side "Pair Stripe CLI" button, which would have needed an owner route that starts `stripe login` — i.e. the backend driving the CLI, the exact thing this design removes. The section is now read-only about pairing state, and pairing starts from Friday's card. The test asserts that button is absent.
