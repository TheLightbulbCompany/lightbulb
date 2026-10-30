"""The `provisioning` list in `lightbulb-facts.json`.

Friday reads this so she never asks twice. Two properties are load-bearing:
NAMES only (never a credential value), and a lookup failure that leaves the
rest of the facts written anyway.
"""

import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import owner_facts as mod  # noqa: E402


def _profile_writer(written: dict):
    """Every owner is AgentCore now: the write lands via
    ``hermes_profiles.write_facts_to_profiles`` rather than EFS."""
    return lambda owner_id, agent_ids, facts_json: written.update(body=facts_json)


def test_default_is_an_empty_list():
    assert mod.build_owner_facts("org_a", {"subscription_status": "active"})["provisioning"] == []
    # Task 1 removed both of the old keys; nothing here reintroduces them.
    assert "stripe_cli" not in mod.build_owner_facts("org_a", {})
    assert "provisioned" not in mod.build_owner_facts("org_a", {})


def test_facts_carry_the_full_entry_shape():
    entry = {
        "workflow_id": "pwf_1",
        "provider": "posthog",
        "service": "svc_analytics",
        "state": "active",
        "resource_name": "svc_analytics-abc123",
        "env_keys": ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
    }
    facts = mod.build_owner_facts("org_a", {"subscription_status": "active"}, provisioning=[entry])
    assert facts["provisioning"] == [entry]


async def test_names_only_never_a_value_and_rejected_survives():
    rows = [
        {
            "workflow_id": "pwf_1",
            "provider_name": "posthog",
            "service_ref": "svc_analytics",
            "state": "active",
            "created_at": "2026-08-02T00:00:00Z",
            "resource_name": "svc_analytics-abc123",
            "env_keys": ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
            "approval": {"price_display": "$20/mo"},
            "secret_value": "phc_do_not_leak_me",
        },
        {
            "workflow_id": "pwf_2",
            "provider_name": "vercel",
            "service_ref": "svc_hosting",
            "state": "rejected",
            "created_at": "2026-08-03T00:00:00Z",
        },
    ]
    written: dict = {}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})),
        patch.object(mod, "_lookup_email_address", AsyncMock(return_value=None)),
        patch("core.repositories.provisioning_workflow_repo.list_for_owner", AsyncMock(return_value=rows)),
        patch("core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": "agent-ceo"}])),
        patch("core.services.hermes_profiles.write_facts_to_profiles") as write_facts,
    ):
        write_facts.side_effect = _profile_writer(written)
        await mod.write_owner_facts("org_a")
    body = written["body"]
    assert "NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN" in body  # the NAME is useful
    assert "phc_" not in body  # a value never is
    assert '"state": "rejected"' in body  # so Friday stops asking
    assert "pwf_2" in body


async def test_writer_reads_the_workflow_table_once_and_keeps_the_newest_per_pair():
    rows = [
        {
            "provider_name": "posthog",
            "service_ref": "svc_analytics",
            "state": "failed",
            "created_at": "2026-08-01T00:00:00Z",
        },
        {
            "provider_name": "posthog",
            "service_ref": "svc_analytics",
            "state": "active",
            "created_at": "2026-08-02T00:00:00Z",
        },
        {
            "provider_name": "vercel",
            "service_ref": "svc_hosting",
            "state": "awaiting_approval",
            "created_at": "2026-08-03T00:00:00Z",
        },
    ]
    written: dict = {}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})),
        patch.object(mod, "_lookup_email_address", AsyncMock(return_value=None)),
        patch("core.repositories.provisioning_workflow_repo.list_for_owner", AsyncMock(return_value=rows)) as listed,
        patch("core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": "agent-ceo"}])),
        patch("core.services.hermes_profiles.write_facts_to_profiles") as write_facts,
    ):
        write_facts.side_effect = _profile_writer(written)
        await mod.write_owner_facts("org_a")
    listed.assert_awaited_once_with("org_a")
    body = written["body"]
    assert '"state": "active"' in body  # the newer posthog row wins
    assert '"state": "failed"' not in body
    assert '"awaiting_approval"' in body  # a pending workflow IS visible


async def test_a_failed_workflow_read_never_blocks_the_facts_write():
    written: dict = {}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})),
        patch.object(mod, "_lookup_email_address", AsyncMock(return_value=None)),
        patch(
            "core.repositories.provisioning_workflow_repo.list_for_owner",
            AsyncMock(side_effect=RuntimeError("ddb down")),
        ),
        patch("core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": "agent-ceo"}])),
        patch("core.services.hermes_profiles.write_facts_to_profiles") as write_facts,
    ):
        write_facts.side_effect = _profile_writer(written)
        await mod.write_owner_facts("org_a")
    assert '"provisioning": []' in written["body"]


async def test_a_removed_resource_keeps_its_status_but_drops_its_names():
    """The row stays `active` — the request WAS fulfilled — so without
    `resource_status` the file would go on advertising variables the owner
    deprovisioned, and Friday would keep reaching for them."""
    rows = [
        {
            "workflow_id": "pwf_1",
            "provider_name": "posthog",
            "service_ref": "svc_analytics",
            "state": "active",
            "created_at": "2026-08-02T00:00:00Z",
            "resource_name": "svc_analytics-abc123",
            "env_keys": ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
            "resource_status": "removed",
        }
    ]
    with patch("core.repositories.provisioning_workflow_repo.list_for_owner", AsyncMock(return_value=rows)):
        entries = await mod._lookup_provisioning("org_a")
    assert entries == [
        {
            "workflow_id": "pwf_1",
            "provider": "posthog",
            "service": "svc_analytics",
            "state": "active",
            "resource_status": "removed",
        }
    ]


async def test_a_live_resource_keeps_its_names():
    rows = [
        {
            "workflow_id": "pwf_1",
            "provider_name": "posthog",
            "service_ref": "svc_analytics",
            "state": "active",
            "created_at": "2026-08-02T00:00:00Z",
            "resource_name": "svc_analytics-abc123",
            "env_keys": ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"],
        }
    ]
    with patch("core.repositories.provisioning_workflow_repo.list_for_owner", AsyncMock(return_value=rows)):
        entry = (await mod._lookup_provisioning("org_a"))[0]
    assert entry["env_keys"] == ["NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN"]
    assert entry["resource_name"] == "svc_analytics-abc123"
    assert "resource_status" not in entry


async def test_app_rows_never_shadow_the_company_row():
    """R53: an app's own Neon/Vercel rows are not company facts."""
    company = {
        "workflow_id": "pwf_1",
        "provider_name": "neon",
        "service_ref": "postgres",
        "state": "active",
        "created_at": "2026-08-01T00:00:00Z",
    }
    app_row = {**company, "workflow_id": "pwf_2", "state": "awaiting_approval", "app_id": "app_1",
               "created_at": "2026-09-01T00:00:00Z"}  # fmt: skip
    with patch(
        "core.repositories.provisioning_workflow_repo.list_for_owner", AsyncMock(return_value=[company, app_row])
    ):
        entries = await mod._lookup_provisioning("org_a")
    assert [e["workflow_id"] for e in entries] == ["pwf_1"]
