"""Pre-deploy credential gate.

A marketplace listing can declare third-party providers its agent needs
(``manifest.requires.credentials``) or bundle registry MCP servers in its
slice. Deploying into a buyer's container before those providers are
connected lands an agent that 401s on first use, so the gate blocks the
deploy with a 409 listing exactly which providers to connect.

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
    _fill_buyer_credential_slots,
    assert_credentials_satisfied,
)


def _connected_rows(*providers: str) -> list[dict]:
    return [{"owner_id": "owner_1", "provider": p, "status": "connected"} for p in providers]


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_blocked_when_required_provider_not_connected(monkeypatch):
    """Manifest declares a hard dependency the buyer hasn't connected."""
    monkeypatch.setattr(
        "core.repositories.connection_repo.list_for_owner",
        AsyncMock(return_value=[]),
    )
    manifest = {"requires": {"credentials": [{"provider": "greenhouse"}]}}

    with pytest.raises(MissingCredentialsError) as excinfo:
        await assert_credentials_satisfied("owner_1", manifest, slice_=None)

    assert "greenhouse" in excinfo.value.missing


@pytest.mark.asyncio
async def test_deploy_allowed_when_connected(monkeypatch):
    """All required providers connected -> no raise."""
    monkeypatch.setattr(
        "core.repositories.connection_repo.list_for_owner",
        AsyncMock(return_value=_connected_rows("greenhouse")),
    )
    manifest = {"requires": {"credentials": [{"provider": "greenhouse"}]}}

    # Must not raise.
    await assert_credentials_satisfied("owner_1", manifest, slice_=None)


@pytest.mark.asyncio
async def test_optional_provider_never_blocks(monkeypatch):
    """An optional credential is a hint, not a gate — never blocks deploy."""
    monkeypatch.setattr(
        "core.repositories.connection_repo.list_for_owner",
        AsyncMock(return_value=[]),
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
        "core.repositories.connection_repo.list_for_owner",
        AsyncMock(return_value=[]),
    )
    await assert_credentials_satisfied("owner_1", manifest, slice_=None)


@pytest.mark.asyncio
async def test_slice_mcp_server_derives_requirement(monkeypatch):
    """A bundled registry MCP server (mcp.servers.stripe) implies its provider.

    The seller bundled the Stripe MCP; its env secrets are scrubbed in the
    slice. Without a connected ``stripe`` credential the deploy must block.
    """
    monkeypatch.setattr(
        "core.repositories.connection_repo.list_for_owner",
        AsyncMock(return_value=[]),
    )
    slice_ = {"mcp": {"servers": {"stripe": {"command": "stripe-mcp", "env": {"STRIPE_API_KEY": ""}}}}}

    with pytest.raises(MissingCredentialsError) as excinfo:
        await assert_credentials_satisfied("owner_1", {}, slice_=slice_)

    assert "stripe" in excinfo.value.missing


@pytest.mark.asyncio
async def test_slice_mcp_non_registry_server_not_gated(monkeypatch):
    """Custom/unknown MCP servers aren't credential-gated by the registry path."""
    monkeypatch.setattr(
        "core.repositories.connection_repo.list_for_owner",
        AsyncMock(return_value=[]),
    )
    slice_ = {"mcp": {"servers": {"my-custom-mcp": {"command": "x"}}}}

    # No registry provider implied -> nothing required -> no raise.
    await assert_credentials_satisfied("owner_1", {}, slice_=slice_)


@pytest.mark.asyncio
async def test_non_connected_status_does_not_satisfy(monkeypatch):
    """A row whose status isn't 'connected' (e.g. pending/expired) doesn't count."""
    monkeypatch.setattr(
        "core.repositories.connection_repo.list_for_owner",
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


def _stub_ecs():
    mgr = MagicMock()
    mgr.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw-tok"}, "10.0.0.1"))
    return mgr


@patch("routers.marketplace_listings.delete_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.get_ecs_manager")
@patch("routers.marketplace_listings.create_paperclip_for_marketplace", new_callable=AsyncMock)
@patch("routers.marketplace_listings.boto3.client")
@patch("routers.marketplace_listings.get_catalog_service")
@patch("routers.marketplace_listings.marketplace_service.get_by_slug", new_callable=AsyncMock)
def test_deploy_returns_409_and_rolls_back_when_credential_missing(
    mock_get_by_slug,
    mock_get_catalog,
    mock_boto,
    mock_create_pc,
    mock_get_ecs,
    mock_delete_pc,
    client,
    monkeypatch,
):
    """Missing-credential deploy -> 409 with the missing list, orphan agent rolled back.

    The real catalog service runs the gate; nothing is connected, so
    deploy_from_artifact raises MissingCredentialsError. The router must map
    that to 409 AND delete the pre-minted Paperclip agent (the gate runs
    before any EFS/gateway side effect, so the agent is the only orphan).
    """
    mock_get_by_slug.return_value = _published_listing()
    mock_create_pc.return_value = {"id": "agent_orphan_1", "name": "Demo Agent"}
    mock_get_ecs.return_value = _stub_ecs()
    mock_boto.return_value = _stub_s3_with_slice({"agent": {"name": "Demo Agent"}})

    # Real catalog service so the actual gate runs end to end. Nothing
    # connected -> MissingCredentialsError on the ``stripe`` manifest dep.
    monkeypatch.setattr(
        "core.repositories.connection_repo.list_for_owner",
        AsyncMock(return_value=[]),
    )
    from core.services.catalog_service import CatalogService

    catalog = CatalogService(
        workspace=MagicMock(),
        apply_deploy_aux_mutations=AsyncMock(),
        pair_fn=AsyncMock(),
        gateway_pool_factory=lambda: MagicMock(),
    )
    mock_get_catalog.return_value = catalog

    resp = client.post("/api/v1/marketplace/listings/demo-agent/deploy")

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "missing_credentials"
    assert "stripe" in detail["missing"]
    # Orphan Paperclip agent rolled back.
    mock_delete_pc.assert_awaited_once()
    assert mock_delete_pc.await_args.kwargs["agent_id"] == "agent_orphan_1"


# ---------------------------------------------------------------------------
# Buyer credential slot-fill (post-merge)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slot_fill_applies_retained_paste_credential(monkeypatch):
    """A connected paste provider in the slice gets its slot filled.

    Stripe is paste_token + openclaw_paths + refresh_owner=none, so the vault
    RETAINS the token (the row carries credential_enc). The slice scrubs the
    MCP env to "", so the buyer's own token must be re-applied into the freshly
    deployed slot via apply_connection.
    """
    row = {"provider": "stripe", "status": "connected", "credential_enc": "enc-blob"}
    decrypted = {"token": "sk_live_buyer"}
    get_mock = AsyncMock(return_value=row)
    decrypt_mock = MagicMock(return_value=decrypted)
    apply_mock = AsyncMock()
    monkeypatch.setattr("core.repositories.connection_repo.get", get_mock)
    monkeypatch.setattr("core.repositories.connection_repo.decrypt_credential", decrypt_mock)
    monkeypatch.setattr(
        "core.services.credentials.apply_connection.apply_connection",
        apply_mock,
    )

    slice_ = {"mcp": {"servers": {"stripe": {"command": "stripe-mcp", "env": {"STRIPE_SECRET_KEY": ""}}}}}

    await _fill_buyer_credential_slots("owner_1", slice_)

    decrypt_mock.assert_called_once_with(row)
    apply_mock.assert_awaited_once_with("owner_1", "stripe", decrypted)


@pytest.mark.asyncio
async def test_slot_fill_skips_scrubbed_actor_provider(monkeypatch):
    """An actor/materializer provider whose vault secret was scrubbed is skipped.

    Google is direct_oauth + materializer + refresh_owner=actor. After connect
    its vault secret is dropped (M6 non-retention), so the row has status
    connected but NO credential_enc and decrypt_credential would RAISE. The
    slot-fill must skip it WITHOUT calling decrypt_credential or apply_connection
    — its credential is the owner-scoped EFS gog keyring, not a per-agent slot.
    This is the load-bearing non-retention guard.
    """
    row = {"provider": "google", "status": "connected", "secret_retained": False}
    get_mock = AsyncMock(return_value=row)
    # decrypt_credential RAISES on a scrubbed row — if it's ever called the
    # guard is broken, so wire it to blow up rather than return a sentinel.
    decrypt_mock = MagicMock(side_effect=ValueError("connection record has no credential_enc"))
    apply_mock = AsyncMock()
    monkeypatch.setattr("core.repositories.connection_repo.get", get_mock)
    monkeypatch.setattr("core.repositories.connection_repo.decrypt_credential", decrypt_mock)
    monkeypatch.setattr(
        "core.services.credentials.apply_connection.apply_connection",
        apply_mock,
    )

    slice_ = {"mcp": {"servers": {"google": {"command": "gog"}}}}

    await _fill_buyer_credential_slots("owner_1", slice_)

    decrypt_mock.assert_not_called()
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_slot_fill_skips_unconnected_provider(monkeypatch):
    """A provider with no connection row (or non-connected status) is skipped."""
    get_mock = AsyncMock(return_value=None)
    decrypt_mock = MagicMock()
    apply_mock = AsyncMock()
    monkeypatch.setattr("core.repositories.connection_repo.get", get_mock)
    monkeypatch.setattr("core.repositories.connection_repo.decrypt_credential", decrypt_mock)
    monkeypatch.setattr(
        "core.services.credentials.apply_connection.apply_connection",
        apply_mock,
    )

    slice_ = {"mcp": {"servers": {"stripe": {"env": {"STRIPE_SECRET_KEY": ""}}}}}

    await _fill_buyer_credential_slots("owner_1", slice_)

    decrypt_mock.assert_not_called()
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_slot_fill_skips_non_registry_mcp_server(monkeypatch):
    """A custom/unknown MCP server (not a registry provider) is never touched."""
    get_mock = AsyncMock(return_value={"provider": "x", "status": "connected", "credential_enc": "e"})
    decrypt_mock = MagicMock()
    apply_mock = AsyncMock()
    monkeypatch.setattr("core.repositories.connection_repo.get", get_mock)
    monkeypatch.setattr("core.repositories.connection_repo.decrypt_credential", decrypt_mock)
    monkeypatch.setattr(
        "core.services.credentials.apply_connection.apply_connection",
        apply_mock,
    )

    slice_ = {"mcp": {"servers": {"my-custom-mcp": {"command": "x"}}}}

    await _fill_buyer_credential_slots("owner_1", slice_)

    get_mock.assert_not_awaited()
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_slot_fill_one_provider_failure_does_not_abort_others(monkeypatch):
    """A single provider's fill failure is logged + swallowed; others still fill.

    The agent is already deployed by the time slot-fill runs, so a fill failure
    is degraded (the buyer can re-connect) — not fatal. One provider raising must
    not prevent another's slot from being filled.
    """
    rows = {
        "stripe": {"provider": "stripe", "status": "connected", "credential_enc": "enc-s"},
        "linear": {"provider": "linear", "status": "connected", "credential_enc": "enc-l"},
    }
    get_mock = AsyncMock(side_effect=lambda owner, prov: rows[prov])
    decrypt_mock = MagicMock(side_effect=lambda r: {"token": f"tok-{r['provider']}"})

    async def _apply(owner, prov, cred):
        if prov == "stripe":
            raise RuntimeError("boom")

    apply_mock = AsyncMock(side_effect=_apply)
    monkeypatch.setattr("core.repositories.connection_repo.get", get_mock)
    monkeypatch.setattr("core.repositories.connection_repo.decrypt_credential", decrypt_mock)
    monkeypatch.setattr(
        "core.services.credentials.apply_connection.apply_connection",
        apply_mock,
    )

    # Both servers expose their canonical credential key (blanked by the scrub),
    # so both are fill candidates.
    slice_ = {
        "mcp": {
            "servers": {
                "stripe": {"env": {"STRIPE_SECRET_KEY": ""}},
                "linear": {"env": {"LINEAR_API_KEY": ""}},
            }
        }
    }

    # Must not raise despite stripe blowing up.
    await _fill_buyer_credential_slots("owner_1", slice_)

    # Both were attempted; linear succeeded.
    assert apply_mock.await_count == 2
    applied_providers = {call.args[1] for call in apply_mock.await_args_list}
    assert applied_providers == {"stripe", "linear"}


@pytest.mark.asyncio
async def test_slot_fill_skips_bundle_with_non_canonical_key(monkeypatch):
    """A registry MCP scrubbed under a NON-canonical key is left for manual setup.

    The Stripe registry wiring fills ``mcp.servers.stripe.env.STRIPE_SECRET_KEY``,
    but this bundle exposes only ``STRIPE_API_KEY``. Writing the canonical key
    would add one the MCP never reads while leaving the real slot blank, so the
    fill is skipped (no apply_connection, no decrypt) rather than silently
    mis-filling — the buyer configures it manually.
    """
    row = {"provider": "stripe", "status": "connected", "credential_enc": "enc-blob"}
    get_mock = AsyncMock(return_value=row)
    decrypt_mock = MagicMock(return_value={"token": "sk_live_buyer"})
    apply_mock = AsyncMock()
    monkeypatch.setattr("core.repositories.connection_repo.get", get_mock)
    monkeypatch.setattr("core.repositories.connection_repo.decrypt_credential", decrypt_mock)
    monkeypatch.setattr(
        "core.services.credentials.apply_connection.apply_connection",
        apply_mock,
    )

    # Non-canonical env key — the registry writes STRIPE_SECRET_KEY, not this.
    slice_ = {"mcp": {"servers": {"stripe": {"command": "stripe-mcp", "env": {"STRIPE_API_KEY": ""}}}}}

    await _fill_buyer_credential_slots("owner_1", slice_)

    decrypt_mock.assert_not_called()
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_slot_fill_treats_missing_status_as_connected(monkeypatch):
    """A legacy row with no explicit status is filled — matching the deploy gate,
    which also defaults a status-less row to connected. If they diverged, the gate
    would admit the deploy while this fill skipped it, leaving the slot blank.
    """
    row = {"provider": "stripe", "credential_enc": "enc-blob"}  # no "status" key
    decrypted = {"token": "sk_live_buyer"}
    get_mock = AsyncMock(return_value=row)
    decrypt_mock = MagicMock(return_value=decrypted)
    apply_mock = AsyncMock()
    monkeypatch.setattr("core.repositories.connection_repo.get", get_mock)
    monkeypatch.setattr("core.repositories.connection_repo.decrypt_credential", decrypt_mock)
    monkeypatch.setattr(
        "core.services.credentials.apply_connection.apply_connection",
        apply_mock,
    )

    slice_ = {"mcp": {"servers": {"stripe": {"env": {"STRIPE_SECRET_KEY": ""}}}}}

    await _fill_buyer_credential_slots("owner_1", slice_)

    apply_mock.assert_awaited_once_with("owner_1", "stripe", decrypted)
