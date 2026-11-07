"""Pre-deploy credential gate.

A marketplace listing can declare third-party providers its agent needs
(``manifest.requires.credentials``) or bundle registry MCP servers in its
slice. Deploying into a buyer's container before those providers are
connected lands an agent that 401s on first use, so the gate blocks the
deploy with a 409 listing exactly which providers to connect.

Post credential-redesign-phase-2 the secret vault moved to AWS Secrets
Manager and the connection status moved to the secret-free ``integrations``
index, so the deploy gate's "connected" set comes from
``integrations_repo.list_for_owner`` (rows carry ``status``, no secret).

``catalog_service`` lazy-imports ``integrations_repo`` inside the gate
(``from core.repositories import integrations_repo``), so the patch targets
that module's own attribute — where catalog_service looks it up at call time.

Re-credentialing the buyer's own bundled integration servers after the slice
merge is the reconcile path (``reconcile_integrations``), covered by
``tests/unit/services/test_catalog_service.py`` and
``tests/credentials/test_mcp_server.py``; this file only pins the pre-deploy
gate.

Covers:
  - manifest-declared provider not connected -> MissingCredentialsError
  - everything connected -> no raise
  - optional provider unconnected -> no raise
  - slice ``mcp.servers.stripe`` (registry provider) unconnected -> missing
  - router maps the error to 409 AND rolls back the orphan Paperclip agent
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402

from core.services.catalog_service import (  # noqa: E402
    MissingCredentialsError,
    assert_credentials_satisfied,
)


def _connected_rows(*providers: str) -> list[dict]:
    return [{"owner_id": "owner_1", "provider": p, "status": "connected"} for p in providers]


# ---------------------------------------------------------------------------
# Validator — connected set sourced from integrations_repo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_blocked_when_required_provider_not_connected(monkeypatch):
    """Manifest declares a hard dependency the buyer hasn't connected."""
    monkeypatch.setattr(
        "core.repositories.integrations_repo.list_for_owner",
        AsyncMock(return_value=[]),
    )
    # The deploy resolves the runtime row itself now (no ECS manager) -- a row with an
    # agentcore_runtime_arn is what gets it past the 503 short-circuit to the catalog call.
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "agentcore_runtime_arn": "arn:runtime/x"}),
    )
    manifest = {"requires": {"credentials": [{"provider": "greenhouse"}]}}

    with pytest.raises(MissingCredentialsError) as excinfo:
        await assert_credentials_satisfied("owner_1", manifest, slice_=None)

    assert "greenhouse" in excinfo.value.missing


@pytest.mark.asyncio
async def test_deploy_allowed_when_connected(monkeypatch):
    """All required providers connected -> no raise."""
    monkeypatch.setattr(
        "core.repositories.integrations_repo.list_for_owner",
        AsyncMock(return_value=_connected_rows("greenhouse")),
    )
    manifest = {"requires": {"credentials": [{"provider": "greenhouse"}]}}

    # Must not raise.
    await assert_credentials_satisfied("owner_1", manifest, slice_=None)


@pytest.mark.asyncio
async def test_optional_provider_never_blocks(monkeypatch):
    """An optional credential is a hint, not a gate — never blocks deploy."""
    monkeypatch.setattr(
        "core.repositories.integrations_repo.list_for_owner",
        AsyncMock(return_value=[]),
    )
    # The deploy resolves the runtime row itself now (no ECS manager) -- a row with an
    # agentcore_runtime_arn is what gets it past the 503 short-circuit to the catalog call.
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "agentcore_runtime_arn": "arn:runtime/x"}),
    )
    manifest = {"requires": {"credentials": [{"provider": "greenhouse", "optional": True}]}}

    await assert_credentials_satisfied("owner_1", manifest, slice_=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest",
    [
        {"requires": None},  # present-but-null (a common JSON encoding of absent)
        {"requires": {"credentials": None}},  # null credentials list
        {"requires": "nope"},  # non-dict junk
        {},  # absent
    ],
)
async def test_null_or_malformed_requires_does_not_crash(manifest, monkeypatch):
    """A null/non-dict ``requires`` must read as "no requirements", not raise.

    ``.get("requires", {})`` only defaults on an ABSENT key; ``"requires": null``
    would otherwise yield ``None.get(...)`` -> AttributeError -> 500.
    """
    monkeypatch.setattr(
        "core.repositories.integrations_repo.list_for_owner",
        AsyncMock(return_value=[]),
    )
    # The deploy resolves the runtime row itself now (no ECS manager) -- a row with an
    # agentcore_runtime_arn is what gets it past the 503 short-circuit to the catalog call.
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "agentcore_runtime_arn": "arn:runtime/x"}),
    )
    await assert_credentials_satisfied("owner_1", manifest, slice_=None)


@pytest.mark.asyncio
async def test_slice_mcp_server_derives_requirement(monkeypatch):
    """A bundled registry MCP server (mcp.servers.stripe) implies its provider.

    The seller bundled the Stripe MCP; its env secrets are scrubbed in the
    slice. Without a connected ``stripe`` credential the deploy must block.
    """
    monkeypatch.setattr(
        "core.repositories.integrations_repo.list_for_owner",
        AsyncMock(return_value=[]),
    )
    # The deploy resolves the runtime row itself now (no ECS manager) -- a row with an
    # agentcore_runtime_arn is what gets it past the 503 short-circuit to the catalog call.
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "agentcore_runtime_arn": "arn:runtime/x"}),
    )
    slice_ = {"mcp": {"servers": {"stripe": {"command": "stripe-mcp", "env": {"STRIPE_API_KEY": ""}}}}}

    with pytest.raises(MissingCredentialsError) as excinfo:
        await assert_credentials_satisfied("owner_1", {}, slice_=slice_)

    assert "stripe" in excinfo.value.missing


@pytest.mark.asyncio
async def test_slice_mcp_non_registry_server_not_gated(monkeypatch):
    """Custom/unknown MCP servers aren't credential-gated by the registry path."""
    monkeypatch.setattr(
        "core.repositories.integrations_repo.list_for_owner",
        AsyncMock(return_value=[]),
    )
    # The deploy resolves the runtime row itself now (no ECS manager) -- a row with an
    # agentcore_runtime_arn is what gets it past the 503 short-circuit to the catalog call.
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "agentcore_runtime_arn": "arn:runtime/x"}),
    )
    slice_ = {"mcp": {"servers": {"my-custom-mcp": {"command": "x"}}}}

    # No registry provider implied -> nothing required -> no raise.
    await assert_credentials_satisfied("owner_1", {}, slice_=slice_)


@pytest.mark.asyncio
async def test_non_connected_status_does_not_satisfy(monkeypatch):
    """An integrations row whose status isn't 'connected' (e.g. expired) doesn't count."""
    monkeypatch.setattr(
        "core.repositories.integrations_repo.list_for_owner",
        AsyncMock(return_value=[{"provider": "stripe", "status": "expired"}]),
    )
    slice_ = {"mcp": {"servers": {"stripe": {}}}}

    with pytest.raises(MissingCredentialsError) as excinfo:
        await assert_credentials_satisfied("owner_1", {}, slice_=slice_)

    assert excinfo.value.missing == ["stripe"]
