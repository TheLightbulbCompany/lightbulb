"""The two provisioning facts Friday reads before touching the Stripe CLI.

``stripe_cli`` tells her whether to start at pairing; ``provisioned`` is the
"don't add it twice" list. Both are DERIVED from the one shared summary the
Accounts route also uses, and neither may ever block the facts write.
"""

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
        "org_a",
        {"subscription_status": "active"},
        stripe_cli="paired",
        provisioned=["posthog/project (Acme Analytics)", "vercel/hosting"],
    )
    assert facts["stripe_cli"] == "paired"
    assert facts["provisioned"] == ["posthog/project (Acme Analytics)", "vercel/hosting"]


@pytest.mark.asyncio
async def test_writer_derives_both_from_the_shared_summary():
    summary = (
        "paired",
        [
            {"provider": "posthog", "service": "project", "resource_name": "Acme Analytics"},
            {"provider": "posthog", "service": "project", "resource_name": "Acme Analytics"},
        ],
    )
    written: dict = {}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})),
        patch.object(mod, "_lookup_email_address", AsyncMock(return_value=None)),
        patch("core.services.provisioning_landing.provisioning_summary", AsyncMock(return_value=summary)) as summarize,
        patch.object(mod, "get_workspace") as ws,
    ):
        ws.return_value.write_file.side_effect = lambda owner, name, body: written.update(body=body)
        await mod.write_owner_facts("org_a")
    summarize.assert_awaited_once_with("org_a")
    assert '"stripe_cli": "paired"' in written["body"]
    # De-duplicated: two resources on the same provider/service is one entry.
    assert written["body"].count('"posthog/project (Acme Analytics)"') == 1


@pytest.mark.asyncio
async def test_a_provisioned_entry_never_carries_a_credential():
    """The pointer dict is not curated for the facts file — only these three
    fields may be read out of it, so an env value riding on a pointer one day
    cannot end up in a world-readable file every agent reads."""
    summary = (
        "paired",
        [{"provider": "posthog", "service": "project", "env_keys": ["POSTHOG_API_KEY"], "secret": "sk_live_nope"}],
    )
    with patch("core.services.provisioning_landing.provisioning_summary", AsyncMock(return_value=summary)):
        state, pairs = await mod._lookup_provisioned("org_a")
    assert state == "paired"
    assert pairs == ["posthog/project"]


@pytest.mark.asyncio
async def test_a_failed_summary_never_blocks_the_facts_write():
    written: dict = {}
    with (
        patch.object(mod.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})),
        patch.object(mod, "_lookup_email_address", AsyncMock(return_value=None)),
        patch(
            "core.services.provisioning_landing.provisioning_summary",
            AsyncMock(side_effect=RuntimeError("ddb down")),
        ),
        patch.object(mod, "get_workspace") as ws,
    ):
        ws.return_value.write_file.side_effect = lambda owner, name, body: written.update(body=body)
        await mod.write_owner_facts("org_a")
    assert '"stripe_cli": "unpaired"' in written["body"]
