"""Spec 13 T6: a backend-custodied CLI row keeps its secret; a switch-on connect is backend."""

from unittest.mock import MagicMock, patch

import pytest

from core.repositories import integrations_repo
from core.services.credentials import registry
from routers import connections


def test_retains_secret_for_backend_custody():
    x = registry.get_provider("x")
    assert connections._retains_secret(x) is False
    assert connections._retains_secret(x, {"status": "connected"}) is False
    assert connections._retains_secret(x, {"custody": "backend"}) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["x", "quickbooks", "instagram", "stripe"])
async def test_connect_with_switch_on_sets_backend_custody(provider):
    # Off in code until V11's bridge fills CLI tokens locally (review of #4691): no connect, switch
    # on or off, leaves X/QuickBooks/Instagram with token-free files nothing fills.
    assert await connections._connect_custody("org_a", registry.get_provider(provider)) is None


@pytest.mark.asyncio
async def test_put_status_writes_custody_only_when_given():
    table = MagicMock()
    with patch.object(integrations_repo, "_table", lambda: table):
        await integrations_repo.put_status("org_a", "x", "connected", custody="backend")
        await integrations_repo.put_status("org_a", "x", "connected")
    first, second = (c.kwargs["Item"] for c in table.put_item.call_args_list)
    assert first["custody"] == "backend"
    assert "custody" not in second


@pytest.mark.asyncio
async def test_set_custody_conditional():
    table = MagicMock()
    with patch.object(integrations_repo, "_table", lambda: table):
        await integrations_repo.set_custody("org_a", "x", "backend", expected=None)
        await integrations_repo.set_custody("org_a", "x", "actor", expected="backend")
    first, second = (c.kwargs for c in table.update_item.call_args_list)
    assert first["ConditionExpression"] == "attribute_exists(owner_id) AND attribute_not_exists(custody)"
    assert second["ConditionExpression"] == "attribute_exists(owner_id) AND custody = :expected"
    assert second["ExpressionAttributeValues"][":expected"] == "backend"
