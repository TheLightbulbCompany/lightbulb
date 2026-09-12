"""Stored-state reconciliation tests for Paperclip starter rosters."""

from collections import Counter
from unittest.mock import AsyncMock

import httpx
import pytest

from core.services.paperclip_admin_client import PaperclipApiError
from tests.unit.services.test_paperclip_provisioning import (
    _StatefulRepo,
    _build_provision_org_doubles,
)
from tests.unit.services.test_paperclip_provisioning_hardening import EXPECTED_ROSTER


@pytest.mark.asyncio
async def test_normal_reconciliation_fills_only_the_missing_starter(monkeypatch):
    """The persisted role map drives retry; display names are irrelevant."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    attempts: Counter[str] = Counter()

    async def create_agent(**kwargs):
        key = kwargs["idempotency_key"].removeprefix("seed:starter:").removesuffix(":v1")
        attempts[key] += 1
        if key == "engineering" and attempts[key] == 1:
            raise PaperclipApiError("transient", status_code=500, body="unavailable")
        return {"id": f"agent-{key}"}

    admin.create_agent = AsyncMock(side_effect=create_agent)
    service = PaperclipProvisioning(admin, repo, "dev")

    first = await service.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )
    assert first.status == "failed"
    assert first.starter_agent_ids == {key: f"agent-{key}" for key, *_ in EXPECTED_ROSTER if key != "engineering"}
    admin.list_agents.assert_not_awaited()

    repo.get = AsyncMock(return_value=first)
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_reconcile"})
    second = await service.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="renamed-owner@example.com",
    )

    assert second.status == "active"
    assert second.seeded_agent_ids is None
    assert second.starter_agent_ids == {key: f"agent-{key}" for key, *_ in EXPECTED_ROSTER}
    assert attempts == Counter({"engineering": 2, **{key: 1 for key, *_ in EXPECTED_ROSTER if key != "engineering"}})
    engineering_calls = [call for call in admin.create_agent.await_args_list if call.kwargs["name"] == "Engineering"]
    assert [call.kwargs["idempotency_key"] for call in engineering_calls] == [
        "seed:starter:engineering:v1",
        "seed:starter:engineering:v1",
    ]
    assert engineering_calls[-1].kwargs["reports_to"] == "agent-ceo"
    admin.create_company.assert_awaited_once()
    admin.list_agents.assert_awaited_once()


@pytest.mark.asyncio
async def test_active_legacy_seed_row_short_circuits_unchanged(monkeypatch):
    """Legacy Friday/Atlas state is not migrated by name or by provisioning."""
    from datetime import datetime, timezone

    from core.repositories.paperclip_repo import PaperclipCompany
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    now = datetime.now(timezone.utc)
    legacy = PaperclipCompany(
        user_id="user_owner",
        org_id="org_xyz",
        company_id="co_legacy",
        paperclip_user_id="pc_legacy",
        paperclip_password_encrypted="",
        service_token_encrypted="",
        status="active",
        created_at=now,
        updated_at=now,
        seeded_agent_ids=["renamed-friday-id", "renamed-atlas-id"],
    )
    repo.get = AsyncMock(return_value=legacy)

    result = await PaperclipProvisioning(admin, repo, "dev").provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert result is legacy
    assert result.starter_agent_ids is None
    admin.create_agent.assert_not_awaited()
    repo.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_legacy_row_reconciles_to_starters_and_keeps_seeded_agent_ids(monkeypatch):
    """A non-active legacy row DOES get reconciled (unlike the active one
    above), and that reconcile is a whole-item put.

    ``seeded_agent_ids`` is never minted any more, so the naive write is
    ``None`` — which would silently erase the only record of which agents a
    legacy owner was seeded with, on a reconcile the owner never asked for.
    The field is carried forward instead; it is dropped only when the owner is
    deliberately reprovisioned.
    """
    from datetime import datetime, timezone

    from core.encryption import encrypt
    from core.repositories.paperclip_repo import PaperclipCompany
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    now = datetime.now(timezone.utc)
    failed_legacy = PaperclipCompany(
        user_id="user_owner",
        org_id="org_xyz",
        company_id="co_legacy",
        paperclip_user_id="pc_legacy",
        # Adoption signs IN with the stored credential, so this must decrypt.
        paperclip_password_encrypted=encrypt("owner-password-plain"),
        service_token_encrypted="",
        status="failed",
        created_at=now,
        updated_at=now,
        seeded_agent_ids=["legacy-friday-id", "legacy-atlas-id"],
    )
    repo.get = AsyncMock(return_value=failed_legacy)
    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_legacy"}, "_session_cookie": "cookie_reconcile"})

    result = await PaperclipProvisioning(admin, repo, "dev").provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    # It really did reconcile onto the starter contract...
    assert result.status == "active"
    assert set(result.starter_agent_ids) == {key for key, *_ in EXPECTED_ROSTER}
    # ...and the legacy ids survived the whole-item put unchanged.
    assert result.seeded_agent_ids == ["legacy-friday-id", "legacy-atlas-id"]
    assert repo.put.await_args.args[0].seeded_agent_ids == ["legacy-friday-id", "legacy-atlas-id"]


@pytest.mark.asyncio
async def test_transport_failure_persists_progress_and_reconciles_same_company(monkeypatch):
    """A timed-out create follows the ordinary missing-starter retry path."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, _ = _build_provision_org_doubles(monkeypatch)
    repo = _StatefulRepo(company_id="co_xyz")
    attempts: Counter[str] = Counter()

    async def create_agent(**kwargs):
        key = kwargs["idempotency_key"].removeprefix("seed:starter:").removesuffix(":v1")
        attempts[key] += 1
        if key == "engineering" and attempts[key] == 1:
            request = httpx.Request(
                "POST",
                "https://credential.example.test/agents",
                headers={"Authorization": "Bearer secret-token"},
            )
            raise httpx.ReadTimeout("read failed with secret-token", request=request)
        return {"id": f"agent-{key}"}

    admin.create_agent = AsyncMock(side_effect=create_agent)
    service = PaperclipProvisioning(admin, repo, "dev")

    first = await service.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert first.status == "failed"
    assert first.company_id == "co_xyz"
    assert first.starter_agent_ids == {key: f"agent-{key}" for key, *_ in EXPECTED_ROSTER if key != "engineering"}
    assert first.last_error == "provision_org: missing required starter agents: engineering"
    assert "secret" not in first.last_error
    admin.list_agents.assert_not_awaited()

    admin.sign_in_user = AsyncMock(return_value={"user": {"id": "pc_user_1"}, "_session_cookie": "cookie_reconcile"})
    second = await service.provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert second.status == "active"
    assert second.company_id == "co_xyz"
    assert second.starter_agent_ids == {key: f"agent-{key}" for key, *_ in EXPECTED_ROSTER}
    assert attempts == Counter(
        {
            "engineering": 2,
            **{key: 1 for key, *_ in EXPECTED_ROSTER if key != "engineering"},
        }
    )
    admin.create_company.assert_awaited_once()
    admin.list_agents.assert_awaited_once()
