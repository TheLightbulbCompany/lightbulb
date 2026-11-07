"""catalog_service's deploy bookkeeping: the funnel event, and the owner-level inputs a profile
write sources.

``deploy_from_artifact`` is one install path -- two S3 writes -- whose own coverage lives in
test_catalog_service_s3_deploy.py. Everything this file used to also cover went with the EFS
install: ``unwind_partial_deploy`` (the workspace dir, the `openclaw.json` agent entry, the
`.template` sidecar and the cron-job removal it drove), ``list_deployed_for_user`` and
``live_install_for_agent`` (both read that sidecar), and the cron-id remap. The install RECORD
those readers served is a KNOWN GAP -- see the comment in ``deploy_listing``.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402

from core.services.catalog_service import CatalogService  # noqa: E402


AGENTCORE_CONTAINER = {
    "owner_id": "user_buyer",
    "runtime_host": "agentcore",
    "agentcore_runtime_arn": "arn:x",
}


def _make_service():
    """``CatalogService`` is stateless -- its three injected collaborators (a ``Workspace``, the
    openclaw.json aux merger, the gateway pair fn) have no successor."""
    return CatalogService()


def _slice_with_agent(**overrides):
    """Minimal slice that triggers the slice-has-agent code path."""
    out = {
        "agent": {"name": "Demo", "skills": ["search"], "model": "claude-3-haiku"},
        "plugins": {"entries": {"memory": {"enabled": True}}},
        "tools": {},
        "cron_jobs": [],
    }
    out.update(overrides)
    return out


@pytest.mark.asyncio
async def test_deploy_does_not_emit_the_funnel_event_itself(monkeypatch):
    """The install is not committed when this method returns: every caller still runs
    rollback-wrapped steps after it (the routine recreate), and a failure there rolls the whole
    deploy back. Emitting here would count those rolled-back hires as deployments, so the event
    is the CALLER's to emit -- via ``emit_agent_deployed`` -- once its own commit steps have
    survived."""
    calls = []
    monkeypatch.setattr(
        "core.services.catalog_service.capture_product_event",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr("core.services.catalog_service._build_and_upload_agent_profile", AsyncMock())
    monkeypatch.setattr("core.services.catalog_service.deploy_tarball_to_profile", AsyncMock())
    svc = _make_service()
    await svc.deploy_from_artifact(
        owner_id="user_buyer",
        slug="demo",
        manifest={"name": "Demo", "version": 1},
        slice_=_slice_with_agent(),
        tar_bytes=b"",
        new_agent_id="agent_abc",
        seller_id="seller_1",
        container=AGENTCORE_CONTAINER,
    )
    assert calls == []


def test_emit_agent_deployed_shape(monkeypatch):
    """One emitter for all three deploy surfaces, so the funnel event's
    properties can't drift between them."""
    from core.services.catalog_service import emit_agent_deployed

    calls = []
    monkeypatch.setattr(
        "core.services.catalog_service.capture_product_event",
        lambda **kwargs: calls.append(kwargs),
    )
    emit_agent_deployed(owner_id="user_buyer", slug="demo", agent_id="agent_abc", seller_id="seller_1")
    assert len(calls) == 1
    assert calls[0]["event"] == "catalog_agent_deployed"
    assert calls[0]["distinct_id"] == "user_buyer"
    assert calls[0]["properties"] == {"slug": "demo", "agent_id": "agent_abc", "seller_id": "seller_1"}


def _patch_resolve_profile_inputs_collaborators(monkeypatch, *, billing_row=None, byo_key=None):
    async def _fake_get_billing_row(owner_id, *, consistent_read=False):
        return billing_row

    async def _fake_build_connected_integration_servers(owner_id):
        return {"paperclip": {"url": "https://paperclip.example"}}

    async def _fake_read_byo_key(owner_id, provider):
        return byo_key

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", _fake_get_billing_row)
    monkeypatch.setattr(
        "core.services.credentials.mcp_server.build_connected_integration_servers",
        _fake_build_connected_integration_servers,
    )
    monkeypatch.setattr("core.services.key_service.read_byo_key", _fake_read_byo_key)


@pytest.mark.asyncio
async def test_build_and_upload_agent_profile_gets_the_bedrock_trial_choice_for_a_trial_local_owner(monkeypatch):
    """M6-R10, divergence (1): catalog_service used to skip
    provider_choice_from_row's trial_local branch entirely, so a card-less
    trial owner's marketplace AgentCore deploy got the full paid model
    ladder instead of BEDROCK_TRIAL_CHOICE. Now that it delegates to the
    shared resolve_profile_inputs, it must not."""
    from core.services import catalog_service

    _patch_resolve_profile_inputs_collaborators(monkeypatch, billing_row={"subscription_status": "trial_local"})
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster",
        AsyncMock(return_value=[{"id": "agent_new", "name": "Demo", "role": "general"}]),
    )
    monkeypatch.setattr("core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value="pk-123"))
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    written = {}

    def fake_write_agent_profile(owner_id, agent, **kwargs):
        written["kwargs"] = kwargs
        return "FILES"

    monkeypatch.setattr("core.services.hermes_profiles.write_agent_profile", fake_write_agent_profile)
    monkeypatch.setattr("core.services.hermes_profiles.upload_profile", AsyncMock(return_value=None))

    await catalog_service._build_and_upload_agent_profile(
        owner_id="org_trial", agent_id="agent_new", container={"bedrock_aip_arns": {"sonnet": "arn:aip:sonnet"}}
    )

    assert written["kwargs"]["provider_choice"] == "openrouter_managed"
    assert written["kwargs"]["byo_provider"] is None


@pytest.mark.parametrize(
    "billing_row,byo_key",
    [
        ({"provider_choice": "bedrock_claude"}, None),
        ({"subscription_status": "trial_local"}, None),
        ({"provider_choice": "byo_key", "byo_provider": "openai"}, "sk-explicit"),
        ({"provider_choice": "byo_key", "byo_provider": None}, "sk-default-anthropic"),
    ],
)
@pytest.mark.asyncio
async def test_write_profiles_and_build_and_upload_agent_profile_source_identical_inputs(
    monkeypatch, billing_row, byo_key
):
    """M6-R10: hermes_provisioning.write_profiles and
    catalog_service._build_and_upload_agent_profile must source the exact
    same six owner-level inputs for the same owner. Proved by calling the
    shared resolver directly (the same call write_profiles makes) and
    comparing it against what catalog_service's caller actually threads into
    write_agent_profile, with only the resolver's own DB/network
    collaborators mocked -- not the resolver itself."""
    from core.services import catalog_service, hermes_provisioning

    _patch_resolve_profile_inputs_collaborators(monkeypatch, billing_row=billing_row, byo_key=byo_key)
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster",
        AsyncMock(return_value=[{"id": "agent_new", "name": "Demo", "role": "general"}]),
    )
    monkeypatch.setattr("core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value="pk-123"))
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())
    container = {"bedrock_aip_arns": {"sonnet": "arn:aip:sonnet"}}

    expected = await hermes_provisioning.resolve_profile_inputs("org_a")

    written = {}

    def fake_write_agent_profile(owner_id, agent, **kwargs):
        written["kwargs"] = kwargs
        return "FILES"

    monkeypatch.setattr("core.services.hermes_profiles.write_agent_profile", fake_write_agent_profile)
    monkeypatch.setattr("core.services.hermes_profiles.upload_profile", AsyncMock(return_value=None))

    await catalog_service._build_and_upload_agent_profile(owner_id="org_a", agent_id="agent_new", container=container)

    assert written["kwargs"]["provider_choice"] == expected.provider_choice
    assert written["kwargs"]["byo_provider"] == expected.byo_provider
    assert written["kwargs"]["byo_api_key"] == expected.byo_api_key
    assert written["kwargs"]["integrations"] == expected.integrations
    assert written["kwargs"]["env_vars"] == expected.env_vars


@pytest.fixture(autouse=True)
def _no_text_line(monkeypatch):
    """Profile writes read the workspace's texting line (the front-desk SOUL section); none here."""
    from unittest.mock import AsyncMock as _AsyncMock

    monkeypatch.setattr("core.repositories.agent_email_repo.text_line_for_owner", _AsyncMock(return_value=None))
