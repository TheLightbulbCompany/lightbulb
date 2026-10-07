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

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

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


# ---------------------------------------------------------------------------
# Router 409 + Paperclip rollback
# ---------------------------------------------------------------------------


def _published_listing() -> dict:
    return {
        "listing_id": "lst_1",
        "seller_id": "seller_x",
        "version": 1,
        "slug": "demo-agent",
        "name": "Demo Agent",
        "status": "published",
        "price_cents": 0,
        "s3_prefix": "listings/lst_1/v1/",
        "manifest_json": {
            "name": "Demo Agent",
            "format": "openclaw",
            "requires": {"credentials": [{"provider": "stripe"}]},
        },
    }


def _stub_s3_with_slice(slice_: dict) -> MagicMock:
    import json as _json

    s3 = MagicMock()

    def _get_object(*, Bucket: str, Key: str):
        body = MagicMock()
        if Key.endswith("workspace.tar.gz"):
            body.read.return_value = b"fake-tar"
        else:
            body.read.return_value = _json.dumps(slice_).encode("utf-8")
        return {"Body": body}

    s3.get_object.side_effect = _get_object
    return s3


@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_published_by_id", new_callable=AsyncMock)
def test_deploy_returns_409_and_rolls_back_when_credential_missing(
    mock_get_published,
    mock_get_catalog,
    mock_boto,
    mock_create_pc,
    mock_delete_pc,
    client,
    monkeypatch,
):
    """Missing-credential deploy -> 409 with the missing list, orphan agent rolled back.

    The router must map ``MissingCredentialsError`` out of the deploy to 409
    AND delete the pre-minted Paperclip agent (the gate runs before any
    install side effect, so the agent is the only orphan).

    ⚠️ The gate is raised here by a stubbed catalog rather than run end to end:
    ``assert_credentials_satisfied`` sat on ``deploy_from_artifact``'s ECS/EFS branch, which
    is deleted, and the surviving profile install does not gate. The validator's own coverage
    is the tests above; the gate's PLACEMENT on the surviving install path is a real gap, not
    a test one.
    """
    mock_get_published.return_value = _published_listing()
    mock_create_pc.return_value = {"id": "agent_orphan_1", "name": "Demo Agent"}
    mock_boto.return_value = _stub_s3_with_slice({"agent": {"name": "Demo Agent"}})
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

    catalog = MagicMock()
    catalog.deploy_from_artifact = AsyncMock(side_effect=MissingCredentialsError(["stripe"]))
    catalog.unwind_partial_deploy = AsyncMock(return_value=True)
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/lst_1/deploy")

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "missing_credentials"
    assert "stripe" in detail["missing"]
    # Orphan Paperclip agent rolled back.
    mock_delete_pc.assert_awaited_once()
    assert mock_delete_pc.await_args.kwargs["agent_id"] == "agent_orphan_1"
