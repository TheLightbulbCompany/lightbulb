"""Tests for license_service."""

import os
import time
from unittest.mock import MagicMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services import license_service  # noqa: E402


def test_generate_returns_iml_prefix_plus_32_base32():
    key = license_service.generate()
    assert key.startswith("iml_")
    body = key[len("iml_") :]
    assert len(body) == 32
    assert all(c in "abcdefghijklmnopqrstuvwxyz234567" for c in body.lower())


@pytest.mark.asyncio
@patch("core.repositories.marketplace_repo._purchases_table")
async def test_validate_revoked_key_returns_revoked(mock_table):
    mock_table.return_value.query.return_value = {
        "Items": [
            {
                "license_key": "iml_xxx",
                "license_key_revoked": True,
                "license_key_revoked_reason": "refunded",
                "listing_id": "l1",
                "listing_version_at_purchase": 3,
                "entitlement_version_floor": 3,
                "purchase_id": "p1",
                "buyer_id": "b1",
            }
        ],
    }
    result = await license_service.validate(license_key="iml_xxx", source_ip="1.2.3.4")
    assert result.status == "revoked"
    assert result.reason == "refunded"


@pytest.mark.asyncio
@patch("core.repositories.marketplace_repo._purchases_table")
async def test_validate_rate_limit_unique_ips(mock_table):
    """11th unique IP in 24h is rejected; same IP repeated is fine."""
    install_log = []
    for i in range(10):
        install_log.append({"ip": f"10.0.0.{i}", "ts": int(time.time())})

    mock_table.return_value.query.return_value = {
        "Items": [
            {
                "license_key": "iml_xxx",
                "license_key_revoked": False,
                "listing_id": "l1",
                "listing_version_at_purchase": 1,
                "entitlement_version_floor": 1,
                "install_log": install_log,
                "purchase_id": "p1",
                "buyer_id": "b1",
            }
        ],
    }
    # 11th unique IP
    result = await license_service.validate(license_key="iml_xxx", source_ip="10.0.0.99")
    assert result.status == "rate_limited"

    # 11th install but same IP as one of the existing — accepted; this also
    # triggers record_install (validate now logs successful installs so the
    # rate-limit window actually advances). The mock_table's update_item is
    # auto-mocked so the call succeeds.
    result2 = await license_service.validate(license_key="iml_xxx", source_ip="10.0.0.0")
    assert result2.status == "valid"


@pytest.mark.asyncio
@patch("core.repositories.marketplace_repo._purchases_table")
async def test_revoke_sets_flags(mock_table):
    mock_table.return_value.update_item = MagicMock(return_value={})
    await license_service.revoke(purchase_id="p1", buyer_id="b1", reason="takedown")
    mock_table.return_value.update_item.assert_called_once()
    kwargs = mock_table.return_value.update_item.call_args.kwargs
    assert "license_key_revoked" in kwargs["UpdateExpression"]
    assert kwargs["ExpressionAttributeValues"][":r"] == "takedown"


@pytest.mark.asyncio
@patch("core.repositories.marketplace_repo._purchases_table")
async def test_validate_returns_not_found_for_unknown_key(mock_table):
    mock_table.return_value.query.return_value = {"Items": []}
    result = await license_service.validate(license_key="iml_unknown", source_ip="1.2.3.4")
    assert result.status == "not_found"
