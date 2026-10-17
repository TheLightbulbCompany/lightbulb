import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import sendblue, text_lines  # noqa: E402
from core.services.billing_service import BillingServiceError  # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest.fixture
def m(monkeypatch):
    """One parent mock so call order is assertable across the collaborators."""
    order = MagicMock()
    order.line = AsyncMock(return_value=None)
    order.claim = AsyncMock(return_value=True)
    order.provision = AsyncMock(return_value="+15551230000")
    order.put = AsyncMock()
    order.soul = AsyncMock()
    order.set_fd = AsyncMock()
    order.deprovision = AsyncMock()
    order.delete = AsyncMock()
    order.metric = MagicMock()
    monkeypatch.setattr(text_lines.agent_email_repo, "text_line_for_owner", order.line)
    monkeypatch.setattr(text_lines.agent_email_repo, "put_text_line", order.put)
    monkeypatch.setattr(text_lines.agent_email_repo, "delete_text_line", order.delete)
    monkeypatch.setattr(text_lines.billing_repo, "claim_front_desk", order.claim)
    monkeypatch.setattr(text_lines.billing_repo, "set_front_desk", order.set_fd)
    monkeypatch.setattr(text_lines.sendblue, "provision_line", order.provision)
    monkeypatch.setattr(text_lines.sendblue, "deprovision_line", order.deprovision)
    monkeypatch.setattr(text_lines, "refresh_agent_soul", order.soul)
    monkeypatch.setattr(text_lines, "put_metric", order.metric)
    row = SimpleNamespace(starter_agent_ids={"customer_operations": "agent_co"})
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=row)
    order.roster_row = row
    monkeypatch.setattr(text_lines, "PaperclipRepo", lambda **kw: repo)
    return order


async def test_ensure_line_provisions_once_and_refreshes_the_soul(m):
    assert await text_lines.ensure_line("org_1") == "provisioned"
    names = [c[0] for c in m.mock_calls if c[0] in {"claim", "provision", "put", "soul"}]
    assert names == ["claim", "provision", "put", "soul"]
    m.claim.assert_awaited_once_with("org_1")
    m.put.assert_awaited_once_with(number="+15551230000", owner_id="org_1", agent_id="agent_co")
    m.soul.assert_awaited_once_with("org_1", "agent_co")


async def test_ensure_line_unchanged_when_row_exists(m):
    m.line.return_value = {"address": "+1555"}
    assert await text_lines.ensure_line("org_1") == "unchanged"
    m.provision.assert_not_awaited()
    m.claim.assert_not_awaited()


async def test_ensure_line_refuses_without_customer_operations(m):
    m.roster_row.starter_agent_ids = {"ceo": "x"}
    with pytest.raises(BillingServiceError, match="no_front_desk_agent"):
        await text_lines.ensure_line("org_1")
    m.provision.assert_not_awaited()
    m.claim.assert_not_awaited()


async def test_ensure_line_skips_when_claim_is_lost(m):
    m.claim.return_value = False
    assert await text_lines.ensure_line("org_1") == "unchanged"
    m.provision.assert_not_awaited()


async def test_provision_failure_records_metric_and_raises(m):
    m.provision.side_effect = sendblue.SendblueError(429, "daily_limit_reached")
    with pytest.raises(sendblue.SendblueError):
        await text_lines.ensure_line("org_1")
    m.metric.assert_called_once_with("text_line.provision_failed", dimensions={"code": "daily_limit_reached"})
    m.put.assert_not_awaited()


async def test_provision_failure_releases_the_claim(m):
    m.provision.side_effect = sendblue.SendblueError(429, "daily_limit_reached")
    with pytest.raises(sendblue.SendblueError):
        await text_lines.ensure_line("org_1")
    m.set_fd.assert_awaited_once_with("org_1", False)


async def test_sync_with_item_on_live_provisions(m, monkeypatch):
    monkeypatch.setattr(text_lines, "_is_front_desk_item", lambda i: i.get("fd", False))
    assert await text_lines.sync_front_desk("org_1", status="active", items=[{"fd": True}]) == "provisioned"


async def test_sync_without_item_releases_existing_line(m, monkeypatch):
    monkeypatch.setattr(text_lines, "_is_front_desk_item", lambda i: False)
    m.line.return_value = {"address": "+15551230000", "agent_id": "agent_co"}
    assert await text_lines.sync_front_desk("org_1", status="active", items=[{}]) == "released"
    m.deprovision.assert_awaited_once_with("+15551230000")
    m.delete.assert_awaited_once_with("+15551230000")
    m.set_fd.assert_awaited_once_with("org_1", False)
    m.soul.assert_awaited_once_with("org_1", "agent_co")


async def test_sync_without_item_and_no_line_is_absent(m, monkeypatch):
    monkeypatch.setattr(text_lines, "_is_front_desk_item", lambda i: False)
    assert await text_lines.sync_front_desk("org_1", status="active", items=[]) == "absent"
    m.deprovision.assert_not_awaited()


async def test_sync_canceled_releases(m, monkeypatch):
    monkeypatch.setattr(text_lines, "_is_front_desk_item", lambda i: True)
    m.line.return_value = {"address": "+15551230000", "agent_id": "agent_co"}
    assert await text_lines.sync_front_desk("org_1", status="canceled", items=[{}]) == "released"


async def test_sync_item_on_incomplete_expired_is_skipped(m, monkeypatch):
    monkeypatch.setattr(text_lines, "_is_front_desk_item", lambda i: True)
    assert await text_lines.sync_front_desk("org_1", status="incomplete_expired", items=[{}]) == "skipped"


async def test_release_survives_sendblue_failure(m):
    m.line.return_value = {"address": "+15551230000", "agent_id": "agent_co"}
    m.deprovision.side_effect = RuntimeError("down")
    assert await text_lines.release_line("org_1") == "released"
    m.delete.assert_awaited_once_with("+15551230000")
    m.set_fd.assert_awaited_once_with("org_1", False)
