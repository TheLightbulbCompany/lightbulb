"""Fresh-workspace starter roster contract tests."""

from unittest.mock import AsyncMock

import pytest

from core.services.paperclip_admin_client import PaperclipApiError
from tests.unit.services.test_paperclip_provisioning import _build_provision_org_doubles


EXPECTED_ROSTER = (
    ("ceo", "CEO", "ceo", "CEO", None),
    ("engineering", "Engineering", "engineer", "Engineering", "ceo"),
    ("finance", "Finance", "cfo", "Finance", "ceo"),
    ("growth", "Growth", "cmo", "Growth", "ceo"),
    ("analytics", "Analytics", "researcher", "Analytics", "ceo"),
    ("customer_operations", "Customer Operations", "general", "Customer Operations", "ceo"),
)


@pytest.mark.asyncio
async def test_fresh_workspace_creates_the_complete_starter_roster(monkeypatch):
    """A fresh row records six role identities, not legacy seed protection."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)
    agent_ids = [f"agent-{key}" for key, *_ in EXPECTED_ROSTER]
    admin.create_agent = AsyncMock(side_effect=[{"id": agent_id} for agent_id in agent_ids])

    row = await PaperclipProvisioning(admin, repo, "dev").provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    assert row.status == "active"
    assert row.seeded_agent_ids is None
    assert row.starter_agent_ids == dict(zip((item[0] for item in EXPECTED_ROSTER), agent_ids, strict=True))

    calls = admin.create_agent.await_args_list
    assert len(calls) == 6
    assert [call.kwargs["name"] for call in calls] == [item[1] for item in EXPECTED_ROSTER]
    assert [call.kwargs["role"] for call in calls] == [item[2] for item in EXPECTED_ROSTER]
    assert [call.kwargs["title"] for call in calls] == [item[3] for item in EXPECTED_ROSTER]
    assert [call.kwargs.get("reports_to") for call in calls] == [None] + ["agent-ceo"] * 5
    assert [call.kwargs["idempotency_key"] for call in calls] == [
        f"seed:starter:{item[0]}:v1" for item in EXPECTED_ROSTER
    ]
    # Every seeded agent shares ONE adapter config, and that is correct now: the native
    # ``hermes_gateway`` facade authenticates by the OWNER's hook token and Paperclip supplies
    # the agent id per run. The per-agent operator device identity this used to assert (each
    # needing its own, or paired.json rejected the duplicate deviceId) went with the OpenClaw
    # gateway -- there is no device to pair.
    configs = [call.kwargs["adapter_config"] for call in calls]
    assert {call.kwargs["adapter_type"] for call in calls} == {"hermes_gateway"}
    assert all(cfg == configs[0] for cfg in configs)
    assert "devicePrivateKeyPem" not in configs[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_key", [item[0] for item in EXPECTED_ROSTER])
async def test_any_missing_starter_keeps_the_row_retryable(monkeypatch, failed_key):
    """A create failure preserves successful IDs without leaking its body."""
    from core.services.paperclip_provisioning import PaperclipProvisioning

    admin, repo = _build_provision_org_doubles(monkeypatch)

    async def create_agent(**kwargs):
        key = kwargs["idempotency_key"].removeprefix("seed:starter:").removesuffix(":v1")
        if key == failed_key:
            raise PaperclipApiError(
                "request contained secret-token",
                status_code=500,
                body={"credential": "secret-token"},
            )
        return {"id": f"agent-{key}"}

    admin.create_agent = AsyncMock(side_effect=create_agent)

    row = await PaperclipProvisioning(admin, repo, "dev").provision_org(
        owner_id="org_xyz",
        owner_user_id="user_owner",
        owner_email="owner@example.com",
    )

    expected_ids = (
        {} if failed_key == "ceo" else {key: f"agent-{key}" for key, *_ in EXPECTED_ROSTER if key != failed_key}
    )
    expected_missing = [item[0] for item in EXPECTED_ROSTER if item[0] not in expected_ids]
    assert row.status == "failed"
    assert row.seeded_agent_ids is None
    assert row.starter_agent_ids == expected_ids
    assert row.last_error == f"provision_org: missing required starter agents: {', '.join(expected_missing)}"
    assert "secret" not in row.last_error
    admin.list_agents.assert_not_awaited()
    admin.delete_agent.assert_not_awaited()
