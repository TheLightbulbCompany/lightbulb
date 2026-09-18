"""Tests for catalog_service's deploy bookkeeping: the funnel event, the
unwind, and the installed-templates readers.

Every owner is on AgentCore (``runtime_transport.host_for`` is pinned), so
``deploy_from_artifact`` always installs through ``_install_agentcore_deploy``
— that branch's own coverage lives in test_catalog_service_s3_deploy.py. The
EFS extract / gateway-pairing / ``openclaw.json`` deep-merge branch is dead
and its tests went with it.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import pytest  # noqa: E402

from core.services.catalog_service import CatalogService  # noqa: E402


AGENTCORE_CONTAINER = {
    "owner_id": "user_buyer",
    "runtime_host": "agentcore",
    "agentcore_runtime_arn": "arn:x",
}


def _make_service():
    """Construct a CatalogService with fully mocked collaborators."""
    workspace = MagicMock()
    pair_fn = AsyncMock(return_value=None)
    apply_aux = AsyncMock(return_value=None)

    workspace.extract_tarball_to_workspace = MagicMock(return_value=None)
    workspace.write_template_sidecar = MagicMock(return_value=None)
    workspace.read_cron_jobs = MagicMock(return_value=[])
    workspace.cleanup_agent_dirs = MagicMock(return_value=None)

    svc = CatalogService(
        workspace=workspace,
        apply_deploy_aux_mutations=apply_aux,
        pair_fn=pair_fn,
    )
    return svc, workspace, apply_aux, pair_fn, None


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


@pytest.fixture(autouse=True)
def _noop_lightbulb_channel_registration(monkeypatch):
    """unwind_partial_deploy lazily imports
    core.services.config_patcher.deregister_lightbulb_agent_account (not
    constructor-injected, like the rest of this file's mocked collaborators)
    and writes to a real openclaw.json on EFS. These tests never provision
    one, so default it to a no-op here.

    Registration itself now happens inside ``pair_with_openclaw`` (the
    chokepoint every agent-creation path routes through — see
    core.services.paperclip_openclaw_pairing), which this file's ``pair_fn``
    is always mocked away, so there is nothing to no-op for it here; that
    behavior is covered by test_paperclip_openclaw_pairing.py."""
    monkeypatch.setattr("core.services.config_patcher.deregister_lightbulb_agent_account", AsyncMock())


def test_cron_job_ids_are_stable_for_deploy_recovery():
    from core.services.catalog_service import _remap_cron_jobs_for_deploy

    template = [{"name": "Morning brief", "schedule": {"kind": "cron", "expr": "0 9 * * *"}}]
    first = _remap_cron_jobs_for_deploy(template, new_agent_id="agent_abc", owner_id="owner")
    second = _remap_cron_jobs_for_deploy(template, new_agent_id="agent_abc", owner_id="owner")

    assert first[0]["id"] == second[0]["id"]
    assert (
        first[0]["id"] != _remap_cron_jobs_for_deploy(template, new_agent_id="agent_other", owner_id="owner")[0]["id"]
    )


@pytest.mark.asyncio
async def test_deploy_does_not_emit_the_funnel_event_itself(monkeypatch):
    """The install is not committed when this method returns: every caller
    still runs rollback-wrapped steps after it (device pairing, routine
    recreate), and a failure there unwinds the whole deploy. Emitting here
    would count those rolled-back hires as deployments, so the event is the
    CALLER's to emit — via ``emit_agent_deployed`` — once its own commit
    steps have survived."""
    calls = []
    monkeypatch.setattr(
        "core.services.catalog_service.capture_product_event",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr("core.services.catalog_service._build_and_upload_agent_profile", AsyncMock())
    monkeypatch.setattr("core.services.catalog_service.deploy_tarball_to_profile", AsyncMock())
    svc, _ws, _aux, _pair, _gw = _make_service()
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


@pytest.mark.asyncio
async def test_unwind_removes_the_cron_jobs_the_sidecar_recorded(monkeypatch):
    """A failed deploy must take its appended cron jobs with it — a stranded
    job keeps firing against a dead agent, and the retry appends duplicates."""
    svc, ws, _aux, _pair, _gw = _make_service()
    ws.read_template_sidecar = MagicMock(return_value={"template_slug": "demo", "cron_job_ids": ["job-1", "job-2"]})
    removed: list[tuple] = []

    async def _remove(owner_id, job_ids):
        removed.append((owner_id, job_ids))

    monkeypatch.setattr("core.services.config_patcher.remove_cron_jobs", _remove)
    monkeypatch.setattr("core.services.config_patcher.remove_from_openclaw_config_list", AsyncMock())
    monkeypatch.setattr("core.containers.workspace.cleanup_deleted_agent", AsyncMock())

    unwound = await svc.unwind_partial_deploy("user_u", "agent_abc", cause=RuntimeError("boom"))

    assert unwound is True
    assert removed == [("user_u", ["job-1", "job-2"])]
    # Read BEFORE the unlink — after it there is nothing left to read.
    ws.read_template_sidecar.assert_called_once_with("user_u", "agent_abc")


@pytest.mark.asyncio
async def test_unwind_skips_cron_removal_for_legacy_sidecar_without_ids(monkeypatch):
    svc, ws, _aux, _pair, _gw = _make_service()
    ws.read_template_sidecar = MagicMock(return_value={"template_slug": "demo"})
    remove = AsyncMock()
    monkeypatch.setattr("core.services.config_patcher.remove_cron_jobs", remove)
    monkeypatch.setattr("core.services.config_patcher.remove_from_openclaw_config_list", AsyncMock())
    monkeypatch.setattr("core.containers.workspace.cleanup_deleted_agent", AsyncMock())

    assert await svc.unwind_partial_deploy("user_u", "agent_abc", cause=RuntimeError("boom")) is True
    remove.assert_not_awaited()


def test_list_deployed_for_user_keyed_by_owner_id():
    """Sidebar's already-deployed filter scans ``workspace-{id}`` sidecars by
    owner_id (org_id for orgs, user_id for personal-mode). Each sidecar
    becomes ``{agent_id, template_slug, template_version}``.
    """
    workspace = MagicMock()
    workspace.list_workspace_agent_dirs.return_value = ["agent_a", "agent_b", "agent_c"]
    workspace.read_template_sidecar.side_effect = [
        {
            "listing_id": "listing_p",
            "template_slug": "pitch",
            "template_version": 3,
            "seller_id": "seller_p",
        },
        None,
        {"template_slug": "scout", "template_version": 1},
    ]
    svc = CatalogService(
        workspace=workspace,
        apply_deploy_aux_mutations=AsyncMock(),
        pair_fn=AsyncMock(),
    )

    out = svc.list_deployed_for_user("org_xyz")

    workspace.list_workspace_agent_dirs.assert_called_once_with("org_xyz")
    assert out == [
        {
            "agent_id": "agent_a",
            "template_slug": "pitch",
            "template_version": 3,
            "listing_id": "listing_p",
            "seller_id": "seller_p",
        },
        # Legacy sidecar without the field surfaces seller_id=None so the
        # idempotency check knows to fall back to slug-only matching.
        {
            "agent_id": "agent_c",
            "template_slug": "scout",
            "template_version": 1,
            "listing_id": None,
            "seller_id": None,
        },
    ]


def test_live_install_for_agent_requires_sidecar_and_registered_agent():
    workspace = MagicMock()
    workspace.read_template_sidecar.return_value = {
        "template_slug": "lex",
        "template_version": 2,
        "listing_id": "listing_1",
        "install_state": "complete",
    }
    workspace.read_openclaw_config.return_value = {"agents": {"list": [{"id": "other"}, {"id": "agent_abc"}]}}
    svc = CatalogService(
        workspace=workspace,
        apply_deploy_aux_mutations=AsyncMock(),
        pair_fn=AsyncMock(),
    )

    assert svc.live_install_for_agent("org_xyz", "agent_abc") == {
        "agent_id": "agent_abc",
        "template_slug": "lex",
        "template_version": 2,
        "listing_id": "listing_1",
        "seller_id": None,
    }

    workspace.read_openclaw_config.return_value = {"agents": {"list": [{"id": "other"}]}}
    assert svc.live_install_for_agent("org_xyz", "agent_abc") is None

    workspace.read_template_sidecar.return_value = {"template_slug": "lex", "install_state": "installing"}
    workspace.read_openclaw_config.return_value = {"agents": {"list": [{"id": "agent_abc"}]}}
    assert svc.live_install_for_agent("org_xyz", "agent_abc") is None

    workspace.read_template_sidecar.return_value = None
    assert svc.live_install_for_agent("org_xyz", "agent_abc") is None


@pytest.mark.asyncio
async def test_unwind_partial_deploy_removes_config_entry_and_efs_dirs(monkeypatch):
    """A failed deploy's unwind must drop the openclaw.json agents.list entry
    (matched by id) AND clean the on-EFS agent dirs, so no name=<uuid> ghost
    is stranded in the buyer's container."""
    svc, ws, _aux, _pair, _gw = _make_service()
    removed = AsyncMock(return_value=None)
    monkeypatch.setattr("core.services.config_patcher.remove_from_openclaw_config_list", removed)

    await svc.unwind_partial_deploy("user_buyer", "agent_ghost")

    removed.assert_awaited_once()
    owner_arg, path_arg, predicate = removed.await_args.args
    assert owner_arg == "user_buyer"
    assert path_arg == ["agents", "list"]
    # predicate removes only the ghost entry, by id, and tolerates non-dicts.
    assert predicate({"id": "agent_ghost"}) is True
    assert predicate({"id": "agent_other"}) is False
    assert predicate("not-a-dict") is False

    ws.cleanup_agent_dirs.assert_called_once_with("user_buyer", "agent_ghost")


@pytest.mark.asyncio
async def test_unwind_partial_deploy_is_best_effort(monkeypatch):
    """Cleanup failures must not raise — the caller has to surface the
    ORIGINAL deploy error, not a rollback error. Both legs are attempted."""
    svc, ws, _aux, _pair, _gw = _make_service()
    monkeypatch.setattr(
        "core.services.config_patcher.remove_from_openclaw_config_list",
        AsyncMock(side_effect=RuntimeError("config locked")),
    )
    ws.cleanup_agent_dirs = MagicMock(side_effect=OSError("efs gone"))

    # Must not raise despite both legs failing.
    await svc.unwind_partial_deploy("user_buyer", "agent_ghost")

    # EFS cleanup is still attempted even after the config removal raised.
    ws.cleanup_agent_dirs.assert_called_once_with("user_buyer", "agent_ghost")


@pytest.mark.asyncio
async def test_unwind_partial_deploy_removes_the_sidecar_first(monkeypatch):
    """The ``.template`` sidecar is unlinked BEFORE the two removals that can
    both fail. ``_live_install_of`` keys on sidecar + a live ``agents.list``
    entry, so a sidecar removed last would survive a correlated EFS failure
    alongside the config entry and make every later Hire of that slug return
    ``already_installed`` for an agent whose Paperclip row the caller is about
    to delete."""
    order: list[str] = []
    svc, ws, _aux, _pair, _gw = _make_service()
    ws.remove_template_sidecar = MagicMock(side_effect=lambda *_a: order.append("sidecar"))
    ws.cleanup_agent_dirs = MagicMock(side_effect=lambda *_a: order.append("dirs"))
    monkeypatch.setattr(
        "core.services.config_patcher.remove_from_openclaw_config_list",
        AsyncMock(side_effect=lambda *_a: order.append("config")),
    )

    await svc.unwind_partial_deploy("user_buyer", "agent_ghost")

    assert order == ["sidecar", "config", "dirs"]
    ws.remove_template_sidecar.assert_called_once_with("user_buyer", "agent_ghost")


@pytest.mark.asyncio
async def test_unwind_removes_the_sidecar_even_when_both_other_legs_fail(tmp_path, monkeypatch):
    """The case the ordering exists for, against a REAL workspace on disk.

    An EFS stall breaks the config removal and the dir cleanup together, and
    ``cleanup_deleted_agent`` swallows its own error so the caller cannot tell.
    The sidecar must still be gone — it is the one thing standing between the
    buyer's next Hire and a permanent ``already_installed`` pointing at an
    agent Teams cannot show.
    """
    from core import config
    from core.containers.workspace import Workspace

    monkeypatch.setattr(config.settings, "ENVIRONMENT", "local")
    ws = Workspace(mount_path=str(tmp_path))
    ws.write_template_sidecar("user_buyer", "agent_ghost", {"template_slug": "demo"})
    sidecar = tmp_path / "user_buyer" / "workspace-agent_ghost" / ".template"
    assert sidecar.exists()

    svc = CatalogService(
        workspace=ws,
        apply_deploy_aux_mutations=AsyncMock(return_value=None),
        pair_fn=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "core.services.config_patcher.remove_from_openclaw_config_list",
        AsyncMock(side_effect=RuntimeError("efs stalled")),
    )
    monkeypatch.setattr(
        "core.containers.workspace.cleanup_deleted_agent",
        AsyncMock(side_effect=RuntimeError("efs stalled")),
    )

    assert await svc.unwind_partial_deploy("user_buyer", "agent_ghost") is True

    assert not sidecar.exists()


@pytest.mark.asyncio
async def test_unwind_reports_unsafe_when_the_sidecar_cannot_be_removed(monkeypatch):
    """THE OTHER False. The sidecar unlink is what makes an unwind safe — it is
    half of what ``_live_install_of`` keys on, and the other two removals fail
    with it (one EFS fault breaks all three). Reporting True after it failed
    sent every caller on to delete the Paperclip row, so once EFS recovered the
    sidecar AND the ``agents.list`` entry were still there: every later Hire of
    that slug resolved to an agent id Teams can no longer show.
    """
    svc, ws, _aux, _pair, _gw = _make_service()
    ws.remove_template_sidecar = MagicMock(side_effect=OSError("efs gone"))
    removed = AsyncMock(return_value=None)
    monkeypatch.setattr("core.services.config_patcher.remove_from_openclaw_config_list", removed)

    assert await svc.unwind_partial_deploy("user_buyer", "agent_ghost") is False

    # Still best-effort on the rest: they shrink the litter even though they
    # cannot make the unwind safe on their own.
    removed.assert_awaited_once()
    ws.cleanup_agent_dirs.assert_called_once_with("user_buyer", "agent_ghost")


@pytest.mark.asyncio
async def test_unwind_partial_deploy_skips_unwind_on_timeout_cause(monkeypatch):
    """A TimeoutError cause means 'no answer yet', not 'failed' — the
    container may still be mid-write on the agent directory this would
    rm -rf. The sidecar stays too: it is what makes the surviving half-install
    discoverable to the retry, which is the whole point of keeping it."""
    svc, ws, _aux, _pair, _gw = _make_service()
    removed = AsyncMock(return_value=None)
    monkeypatch.setattr("core.services.config_patcher.remove_from_openclaw_config_list", removed)

    await svc.unwind_partial_deploy("user_buyer", "agent_ghost", cause=TimeoutError())

    removed.assert_not_awaited()
    ws.cleanup_agent_dirs.assert_not_called()
    ws.remove_template_sidecar.assert_not_called()


@pytest.mark.asyncio
async def test_unwind_partial_deploy_runs_for_non_timeout_cause(monkeypatch):
    """A non-timeout cause is a genuine failure — the unwind still runs."""
    svc, ws, _aux, _pair, _gw = _make_service()
    removed = AsyncMock(return_value=None)
    monkeypatch.setattr("core.services.config_patcher.remove_from_openclaw_config_list", removed)

    await svc.unwind_partial_deploy("user_buyer", "agent_ghost", cause=RuntimeError("boom"))

    removed.assert_awaited_once()
    ws.cleanup_agent_dirs.assert_called_once_with("user_buyer", "agent_ghost")


def _patch_resolve_profile_inputs_collaborators(monkeypatch, *, billing_row=None, byo_key=None):
    async def _fake_get_billing_row(owner_id):
        return billing_row

    async def _fake_build_connected_integration_servers(owner_id):
        return {"paperclip": {"url": "https://paperclip.example"}}

    async def _fake_build_provisioned_env(owner_id):
        return {"SOME_KEY": "value"}

    async def _fake_read_byo_key(owner_id, provider):
        return byo_key

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", _fake_get_billing_row)
    monkeypatch.setattr(
        "core.services.credentials.mcp_server.build_connected_integration_servers",
        _fake_build_connected_integration_servers,
    )
    monkeypatch.setattr("core.services.provisioning_landing.build_provisioned_env", _fake_build_provisioned_env)
    monkeypatch.setattr("core.services.key_service.read_byo_key", _fake_read_byo_key)


@pytest.mark.asyncio
async def test_build_and_upload_agent_profile_gets_the_bedrock_trial_choice_for_a_trial_local_owner(monkeypatch):
    """M6-R10, divergence (1): catalog_service used to skip
    provider_choice_from_row's trial_local branch entirely, so a card-less
    trial owner's marketplace AgentCore deploy got the full paid model
    ladder instead of BEDROCK_TRIAL_CHOICE. Now that it delegates to the
    shared resolve_profile_inputs, it must not."""
    from core.containers.config import BEDROCK_TRIAL_CHOICE
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

    assert written["kwargs"]["provider_choice"] == BEDROCK_TRIAL_CHOICE
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

    expected = await hermes_provisioning.resolve_profile_inputs("org_a", container_row=container)

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
    assert written["kwargs"]["aip_arns"] == expected.aip_arns
    assert written["kwargs"]["integrations"] == expected.integrations
    assert written["kwargs"]["env_vars"] == expected.env_vars
