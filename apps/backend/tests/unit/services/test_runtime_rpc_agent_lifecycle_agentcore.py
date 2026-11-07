"""Q9a rows #1 + #2 (task-9-scan.md): agents.create / agents.update.

Both RPC doors used to stage Paperclip agent metadata into openclaw.json via
``config_patcher.apply_paperclip_meta`` -- an EFS write that raised ``ConfigPatchError`` for an
owner with no openclaw.json at all. There is no such file and no such module now, which
``test_the_openclaw_metadata_write_cannot_come_back`` pins: these tests used to assert the write
was not attempted, and an assertion about a mock nobody can create any more is not an assertion.
``agents.create`` propagated that error as an unhandled 500; ``agents.update``
swallowed it silently behind a bare ``except Exception``. Both now branch on
the owner's runtime instead.

Fix round 1 (Opus task review, M6-R27/M6-R28/M6-R29): every AgentCore test
below drives the REAL ``_create_agent`` / ``_mirror_agent_update`` /
``write_profile_for_agent`` -- only the S3, Paperclip, and DynamoDB edges are
mocked (``pair_with_openclaw``, ``container_rpc.call``, and the two
orchestration functions above are never replaced with a stand-in on an
AgentCore path).

Deletion PR-1 pinned ``runtime_transport.host_for`` to always return
``"agentcore"``, so the ECS branches these two RPC doors used to have (the
``pair_with_openclaw``/``ensure_operator_device_paired``/
``apply_paperclip_meta`` half of ``_create_agent``) are now unreachable dead
code; their coverage (formerly here and in the deleted
``test_runtime_rpc_agent_create.py``) went with them.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import runtime_rpc as rpc
from core.services.hermes_profiles import model_block
from core.services.hermes_provisioning import ProfileInputs
from core.services.messaging import ApiProblem


@pytest.fixture(autouse=True)
def _no_stored_soul_md_by_default(monkeypatch):
    """``hermes_provisioning.refresh_agent_soul`` now reads the stored ``SOUL.md`` first (M6-R48, the
    generated-marker check) -- a real S3 GET when unmocked. Default every test in this module to
    "the profile has none yet", which is the regenerate branch they were all written against; the
    marker tests below override it."""
    monkeypatch.setattr(rpc.hermes_profiles, "read_soul_md", MagicMock(return_value=None))
    # Task 13: every rename also rewrites VOICE_NOTES.md's name line and the room member labels.
    monkeypatch.setattr(rpc.hermes_profiles, "read_voice_notes", MagicMock(return_value=None))
    monkeypatch.setattr(rpc.hermes_profiles, "upload_voice_notes", MagicMock())
    monkeypatch.setattr("core.services.messaging.refresh_agent_member_names", AsyncMock(return_value={}))
    monkeypatch.setattr(rpc.hermes_profiles.settings, "OPENROUTER_MANAGED_MODEL_ID", "candidate/model")
    monkeypatch.setattr(rpc.hermes_profiles.settings, "OPENROUTER_GATE_URL", "https://gate.example/v1")


def _wire_paperclip_mirror(monkeypatch, admin):
    """The registry edge _mirror_agent_update writes through. ``admin`` stands in for the store:
    ``get_agent``'s row is the roster, and every registry update lands on ``admin.patch_agent``."""

    async def _update(ws, agent_id, fields):
        return await admin.patch_agent(agent_id=agent_id, body=fields)

    monkeypatch.setattr(rpc.agent_registry, "roster", AsyncMock(return_value=[admin.get_agent.return_value]))
    monkeypatch.setattr(rpc.agent_registry, "update", AsyncMock(side_effect=_update))


def _wire_profile_inputs(monkeypatch, inputs: ProfileInputs, *, paperclip_api_key="pk-123"):
    """The DynamoDB/Secrets/Paperclip edges write_profile_for_agent (row #1) and
    _apply_agentcore_model_patch (row #2) both read through -- resolve_profile_inputs and the
    Paperclip API key mint. Neither is one of the three orchestration functions the fix-round
    review named, so collapsing them at this boundary (rather than their own sub-collaborators)
    is consistent with test_hermes_provisioning.py's own write_profile_for_agent tests."""
    monkeypatch.setattr("core.services.hermes_provisioning.resolve_profile_inputs", AsyncMock(return_value=inputs))
    monkeypatch.setattr(
        "core.services.paperclip_api_key.ensure_paperclip_api_key", AsyncMock(return_value=paperclip_api_key)
    )
    monkeypatch.setattr("core.services.paperclip_admin_client.shared_admin_client", lambda: object())


_MANAGED_INPUTS = ProfileInputs(
    provider_choice="openrouter_managed",
    byo_provider=None,
    byo_api_key=None,
    integrations={},
    env_vars={},
)


# ---------------------------------------------------------------------------
# Row #1 -- agents.create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_create_on_agentcore_row_writes_the_real_profile_with_no_pairing(monkeypatch):
    """M6-R27: pair_with_openclaw / ensure_operator_device_paired / container_rpc.call are
    DELIBERATELY left unmocked -- if the AgentCore branch still reached pair_with_openclaw, its
    real container_rpc.call would attempt a live HTTP/boto3 call and fail loudly in this
    sandbox, failing this test."""
    container = {"gateway_token": "tok", "runtime_host": "agentcore", "agentcore_runtime_arn": "arn:x"}
    paperclip_agent = {"id": "agent_new", "name": "Ada", "role": "engineer", "title": "Backend engineer"}
    monkeypatch.setattr(rpc.container_rpc, "running_container", AsyncMock(return_value=(container, "10.0.0.1")))
    monkeypatch.setattr(rpc.agent_registry, "create", AsyncMock(return_value=paperclip_agent))
    monkeypatch.setattr(rpc.agent_registry, "delete", AsyncMock())
    _wire_profile_inputs(monkeypatch, _MANAGED_INPUTS)
    upload_mock = AsyncMock()
    monkeypatch.setattr(rpc.hermes_profiles, "upload_profile", upload_mock)
    publish = AsyncMock()
    monkeypatch.setattr(rpc, "publish_owner_users", publish)

    result = await rpc.execute_sync(
        owner_id="owner_1",
        user_id="user_1",
        request_id="r1",
        method="agents.create",
        params={"name": "Ada", "params": {"paperclip": {"role": "engineer", "title": "Backend engineer"}}},
    )

    assert result["agentId"] == "agent_new"
    upload_mock.assert_awaited_once()
    owner_arg, agent_arg, files_arg = upload_mock.await_args.args
    assert (owner_arg, agent_arg) == ("owner_1", "agent_new")
    assert files_arg.config_yaml  # real write_agent_profile output, not a stand-in
    assert "Ada" in files_arg.soul_md
    event = publish.await_args.args[1]
    assert (publish.await_args.args[0], event["type"], event["payload"]) == (
        "owner_1",
        "runtime.resource.invalidated",
        {"resource": "agents"},
    )


@pytest.mark.asyncio
async def test_agents_create_hermes_profile_error_is_a_clean_rpc_error_not_a_500(monkeypatch):
    """Drives the real write_profile_for_agent into its real HermesProfileError raise (a failed
    Paperclip key mint) rather than mocking the raise directly."""
    container = {"gateway_token": "tok", "runtime_host": "agentcore"}
    paperclip_agent = {"id": "agent_new", "name": "Ada"}
    monkeypatch.setattr(rpc.container_rpc, "running_container", AsyncMock(return_value=(container, "10.0.0.1")))
    monkeypatch.setattr(rpc.agent_registry, "create", AsyncMock(return_value=paperclip_agent))
    monkeypatch.setattr(rpc.agent_registry, "delete", AsyncMock())
    _wire_profile_inputs(monkeypatch, _MANAGED_INPUTS, paperclip_api_key=None)
    monkeypatch.setattr(rpc, "publish_owner_users", AsyncMock())

    with pytest.raises(ApiProblem) as exc_info:
        await rpc.execute_sync(
            owner_id="owner_1", user_id="user_1", request_id="r1", method="agents.create", params={"name": "Ada"}
        )

    assert exc_info.value.error.code == "RUNTIME_UNAVAILABLE"
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# Row #2 -- agents.update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_role_change_never_rewrites_the_owners_model_pick(monkeypatch):
    """Fix round 1, C1. A role change used to re-derive the model block from the role's tier
    (M6-R28) -- self-consistent while the role WAS the only model choice. Now that the picker
    offers role-independent ids, the Role & description card's Save (no `model` in the payload,
    and it sits directly below the picker in the same panel) would silently revert the owner's
    choice, and promote a deliberately demoted CEO back onto opus. Only an explicit `model`
    writes that block now."""
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "general"})
    admin.patch_agent = AsyncMock(return_value={})
    _wire_paperclip_mirror(monkeypatch, admin)
    monkeypatch.setattr(rpc.container_repo, "get_by_owner_id", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "core.services.hermes_provisioning.resolve_profile_inputs", AsyncMock(return_value=_MANAGED_INPUTS)
    )
    patch_config = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", patch_config)
    write_profile = AsyncMock()
    monkeypatch.setattr("core.services.hermes_provisioning.write_profile_for_agent", write_profile)
    # A role change also carries a "role" key in `body`, so refresh_agent_soul runs too.
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": "a1", "role": "ceo"}])
    )
    upload_soul = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "upload_soul_md", upload_soul)

    await rpc._mirror_agent_update("org_1", {"agentId": "a1", "params": {"paperclip": {"role": "ceo"}}}, agentcore=True)

    write_profile.assert_not_awaited()  # never re-runs the full writer -- would clobber edits
    admin.patch_agent.assert_awaited_once()  # Paperclip still gets the role change
    upload_soul.assert_called_once()  # and SOUL.md still reflects it
    patch_config.assert_not_called()  # ...but config.yaml's model block is untouched


@pytest.mark.asyncio
async def test_a_title_edit_beside_a_picked_model_leaves_that_model_alone(monkeypatch):
    """The exact C1 scenario: pick Opus in the Model card, then fix a typo in the Title field
    directly below it. No `model` in that payload, so nothing may touch the model block."""
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "general"})
    admin.patch_agent = AsyncMock(return_value={})
    _wire_paperclip_mirror(monkeypatch, admin)
    patch_config = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", patch_config)
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": "a1", "role": "general"}])
    )
    monkeypatch.setattr(rpc.hermes_profiles, "upload_soul_md", MagicMock())

    await rpc._mirror_agent_update(
        "org_1",
        {"agentId": "a1", "params": {"paperclip": {"role": "general", "title": "Backend engineer"}}},
        agentcore=True,
    )

    patch_config.assert_not_called()


@pytest.mark.asyncio
async def test_a_model_change_alongside_a_role_change_still_writes_the_picked_model(monkeypatch):
    """The other direction: an explicit pick IS written, through model_block, even when the same
    payload changes the role."""
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "general"})
    admin.patch_agent = AsyncMock(return_value={})
    _wire_paperclip_mirror(monkeypatch, admin)
    _wire_owner_models(monkeypatch)
    monkeypatch.setattr(
        "core.services.hermes_provisioning.resolve_profile_inputs", AsyncMock(return_value=_MANAGED_INPUTS)
    )
    patch_config = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", patch_config)
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": "a1", "role": "ceo"}])
    )
    monkeypatch.setattr(rpc.hermes_profiles, "upload_soul_md", MagicMock())

    await rpc._mirror_agent_update(
        "org_1",
        {"agentId": "a1", "model": "candidate/model", "params": {"paperclip": {"role": "ceo"}}},
        agentcore=True,
    )

    expected_model, _aux, _prov = model_block(
        provider_choice="openrouter_managed",
        byo_provider=None,
        is_ceo=False,
        default_id="candidate/model",
    )
    patch_config.assert_called_once_with("org_1", "a1", {"model": expected_model})


@pytest.mark.asyncio
async def test_mirror_agent_update_rename_on_agentcore_row_refreshes_soul_md_only(monkeypatch):
    """I2: a rename must rebuild SOUL.md (from the live post-mirror roster row) and must
    NEVER touch config.yaml/.env alongside it -- patch_profile_config stays untouched."""
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "general"})
    admin.patch_agent = AsyncMock(return_value={})
    _wire_paperclip_mirror(monkeypatch, admin)
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster",
        AsyncMock(return_value=[{"id": "a1", "name": "Renamed Ada", "role": "general", "title": "Old Title"}]),
    )
    upload_soul = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "upload_soul_md", upload_soul)
    patch_config = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", patch_config)
    write_profile = AsyncMock()
    monkeypatch.setattr("core.services.hermes_provisioning.write_profile_for_agent", write_profile)

    await rpc._mirror_agent_update("org_1", {"agentId": "a1", "name": "Renamed Ada"}, agentcore=True)

    patch_config.assert_not_called()  # no role/model change -- config.yaml/.env untouched
    write_profile.assert_not_awaited()
    upload_soul.assert_called_once()
    owner_arg, agent_arg, soul_arg = upload_soul.call_args.args
    assert (owner_arg, agent_arg) == ("org_1", "a1")
    assert "Renamed Ada" in soul_arg


def _wire_rename(monkeypatch, *, stored_soul: str | None):
    """A rename on an AgentCore row, with whatever SOUL.md the profile currently holds."""
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "general"})
    admin.patch_agent = AsyncMock(return_value={})
    _wire_paperclip_mirror(monkeypatch, admin)
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster",
        AsyncMock(return_value=[{"id": "a1", "name": "Renamed Ada", "role": "general"}]),
    )
    monkeypatch.setattr(rpc.hermes_profiles, "read_soul_md", MagicMock(return_value=stored_soul))
    upload_soul = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "upload_soul_md", upload_soul)
    return upload_soul


@pytest.mark.asyncio
async def test_rename_regenerates_soul_md_while_it_still_carries_the_generated_marker(monkeypatch):
    """M6-R48: an untouched, backend-generated SOUL.md is ours to rewrite."""
    generated = rpc.hermes_profiles.build_soul_md({"id": "a1", "name": "Ada", "role": "general"})
    upload_soul = _wire_rename(monkeypatch, stored_soul=generated)

    await rpc._mirror_agent_update("org_1", {"agentId": "a1", "name": "Renamed Ada"}, agentcore=True)

    upload_soul.assert_called_once()
    assert "Renamed Ada" in upload_soul.call_args.args[2]


@pytest.mark.asyncio
async def test_rename_leaves_an_owner_edited_soul_md_alone(monkeypatch):
    """M6-R48: the Instructions-tab PUT strips the marker, so this text is the owner's and a
    rename must never overwrite it -- the same rule protects a migrated workspace's carried-over
    SOUL.md, which was never generated here either."""
    upload_soul = _wire_rename(monkeypatch, stored_soul="# Ada\n\nI wrote this myself.\n")

    await rpc._mirror_agent_update("org_1", {"agentId": "a1", "name": "Renamed Ada"}, agentcore=True)

    upload_soul.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, ""])
async def test_rename_still_writes_soul_md_when_there_is_nothing_to_preserve(monkeypatch, stored):
    """Fix round 1, m5: an EMPTY SOUL.md (a half-finished write) must not freeze regeneration for
    that agent forever -- ``is_generated_soul("")`` is False, so "" needs the absent branch."""
    upload_soul = _wire_rename(monkeypatch, stored_soul=stored)

    await rpc._mirror_agent_update("org_1", {"agentId": "a1", "name": "Renamed Ada"}, agentcore=True)

    upload_soul.assert_called_once()


def _wire_owner_models(monkeypatch, *, provider_choice="openrouter_managed"):
    """The read ``_agentcore_models_list`` makes -- the owner's provider choice. With it,
    ``agents.update {model}`` validates against exactly the ids ``models.list`` would have
    offered this owner (M6 task 10-C)."""
    monkeypatch.setattr(
        "core.services.container_provision.resolve_provider_choice",
        AsyncMock(return_value=(provider_choice, None)),
    )


@pytest.mark.asyncio
async def test_mirror_agent_update_refuses_a_null_model_instead_of_writing_nothing(monkeypatch):
    """Fix round 1, M2. `{"model": null}` is "clear the override, inherit the gateway default" on
    ECS; Hermes has no workspace-level default, so it used to write nothing and still answer
    {"ok": True} -- the row-12 silent-lie class, in the one field this task made writable."""
    patch_config = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", patch_config)

    with pytest.raises(ApiProblem) as exc_info:
        await rpc._mirror_agent_update("org_1", {"agentId": "a1", "model": None}, agentcore=True)

    assert exc_info.value.error.details["upstreamCode"] == "UNSUPPORTED_ON_AGENTCORE"
    patch_config.assert_not_called()


@pytest.mark.asyncio
async def test_mirror_agent_update_rejects_a_model_outside_the_owners_own_list(monkeypatch):
    """M6-R29/M6-R50: the fence survives the move from a closed tier vocabulary to the real list
    -- a Bedrock ref outside this owner's own AIP grant makes the gate refuse every turn."""
    _wire_owner_models(monkeypatch)
    with pytest.raises(ApiProblem) as exc_info:
        await rpc._mirror_agent_update(
            "org_1", {"agentId": "a1", "model": "arn:aws:bedrock:us-east-1::somebody-elses"}, agentcore=True
        )
    assert exc_info.value.error.code == "INVALID_REQUEST"
    assert exc_info.value.error.details["upstreamCode"] == "MODEL_NOT_OFFERED"


@pytest.mark.asyncio
async def test_mirror_agent_update_accepts_an_offered_id_and_writes_it_through_the_writer(monkeypatch):
    """The picked id lands as ``model.default``; ``provider`` and the gate route still come from
    ``model_block``, never from the client."""
    _wire_owner_models(monkeypatch)
    monkeypatch.setattr(
        "core.services.hermes_provisioning.resolve_profile_inputs", AsyncMock(return_value=_MANAGED_INPUTS)
    )
    patch_config = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", patch_config)

    await rpc._mirror_agent_update("org_1", {"agentId": "a1", "model": "candidate/model"}, agentcore=True)

    expected_model, _aux_model, _aux_provider = model_block(
        provider_choice="openrouter_managed",
        byo_provider=None,
        is_ceo=False,
        default_id="candidate/model",
    )
    assert expected_model["default"] == "candidate/model" and expected_model["provider"] == "custom"
    patch_config.assert_called_once_with("org_1", "a1", {"model": expected_model})


@pytest.mark.asyncio
async def test_mirror_agent_update_model_only_never_touches_paperclip_even_on_agentcore(monkeypatch):
    """Unchanged pre-existing behaviour (test_noop_update_skips_paperclip's own assertion): a
    model-only update never touches Paperclip, regardless of transport."""
    _wire_owner_models(monkeypatch)
    monkeypatch.setattr(
        "core.services.hermes_provisioning.resolve_profile_inputs", AsyncMock(return_value=_MANAGED_INPUTS)
    )
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", MagicMock())
    update = AsyncMock()
    monkeypatch.setattr(rpc.agent_registry, "update", update)

    await rpc._mirror_agent_update("org_1", {"agentId": "a1", "model": "candidate/model"}, agentcore=True)

    update.assert_not_awaited()


# ---------------------------------------------------------------------------
# execute_sync integration -- the real _mirror_agent_update, dispatched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_update_on_agentcore_row_returns_ok_and_never_reaches_container_rpc(monkeypatch):
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "general"})
    admin.patch_agent = AsyncMock(return_value={})
    _wire_paperclip_mirror(monkeypatch, admin)
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": "a1", "name": "Renamed"}])
    )
    monkeypatch.setattr(rpc.hermes_profiles, "upload_soul_md", MagicMock())
    publish = AsyncMock()
    monkeypatch.setattr(rpc, "publish_owner_users", publish)
    call_mock = AsyncMock()
    monkeypatch.setattr(rpc.container_rpc, "call", call_mock)

    result = await rpc.execute_sync(
        owner_id="owner_1",
        user_id="user_1",
        request_id="r1",
        method="agents.update",
        params={"agentId": "a1", "name": "Renamed"},
    )

    assert result == {"ok": True}
    call_mock.assert_not_awaited()
    event = publish.await_args.args[1]
    assert (publish.await_args.args[0], event["type"], event["payload"]) == (
        "owner_1",
        "runtime.resource.invalidated",
        {"resource": "agents"},
    )


@pytest.mark.asyncio
async def test_agents_update_paperclip_failure_is_an_error_not_ok(monkeypatch):
    """Role/title/capabilities live only on the registry row: a failed write must not answer ok."""
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "general"})
    admin.patch_agent = AsyncMock(side_effect=rpc.agent_registry.RegistryUnavailable("store down"))
    _wire_paperclip_mirror(monkeypatch, admin)
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": "a1", "name": "Renamed"}])
    )
    monkeypatch.setattr(rpc.hermes_profiles, "upload_soul_md", MagicMock())
    monkeypatch.setattr(rpc, "publish_owner_users", AsyncMock())

    with pytest.raises(ApiProblem) as exc_info:
        await rpc.execute_sync(
            owner_id="owner_1",
            user_id="user_1",
            request_id="r1",
            method="agents.update",
            params={"agentId": "a1", "name": "Renamed"},
        )

    assert exc_info.value.error.code == "RUNTIME_UNAVAILABLE"
    assert exc_info.value.error.retryable is True
    assert exc_info.value.error.message == "Couldn't save the agent's details."


# ---------------------------------------------------------------------------
# M6 task 10, row 12 -- agents.update must not answer ok while writing nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_update_tools_is_refused_on_an_agentcore_row(monkeypatch):
    """The scan's worst failure class: the Tools tab's Save returned {"ok": True} while
    _mirror_agent_update early-returned and wrote NOTHING. Hermes gates by toolset, so the
    field has no home -- refuse it instead of confirming it."""
    mirror = AsyncMock()
    monkeypatch.setattr(rpc, "_mirror_agent_update", mirror)
    call_mock = AsyncMock()
    monkeypatch.setattr(rpc.container_rpc, "call", call_mock)

    with pytest.raises(ApiProblem) as exc_info:
        await rpc.execute_sync(
            owner_id="owner_1",
            user_id="user_1",
            request_id="r1",
            method="agents.update",
            params={"agentId": "a1", "tools": {"profile": "coding", "deny": ["exec"]}},
        )

    error = exc_info.value.error
    assert error.message == "Per-tool allow/deny lists are not available on this runtime."
    assert error.details["upstreamCode"] == "UNSUPPORTED_ON_AGENTCORE"
    assert error.details["fields"] == ["tools"]
    # Refused BEFORE any side effect -- a half-applied update would be worse than the lie.
    mirror.assert_not_awaited()
    call_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_agents_update_refuses_any_other_unwritable_field_too(monkeypatch):
    """Row 12 is not tools-specific: the same silent lie covers every future field the
    intercept does not persist, including an unknown key under `params`."""
    monkeypatch.setattr(rpc, "_mirror_agent_update", AsyncMock())

    with pytest.raises(ApiProblem) as exc_info:
        await rpc.execute_sync(
            owner_id="owner_1",
            user_id="user_1",
            request_id="r1",
            method="agents.update",
            params={"agentId": "a1", "heartbeat": {"enabled": True}, "params": {"identity": {}}},
        )

    assert exc_info.value.error.details["fields"] == ["heartbeat", "params.identity"]
    assert "Per-tool" not in exc_info.value.error.message


@pytest.mark.asyncio
async def test_agents_update_still_accepts_what_the_intercept_actually_writes(monkeypatch):
    """name + params.paperclip + model are persisted (Paperclip mirror, SOUL.md, model block),
    so they must NOT be caught by the refusal."""
    mirror = AsyncMock()
    monkeypatch.setattr(rpc, "_mirror_agent_update", mirror)

    result = await rpc.execute_sync(
        owner_id="owner_1",
        user_id="user_1",
        request_id="r1",
        method="agents.update",
        params={"agentId": "a1", "name": "Ada", "model": "arn:aip:opus", "params": {"paperclip": {"role": "ceo"}}},
    )

    assert result == {"ok": True}
    mirror.assert_awaited_once()


def test_the_openclaw_metadata_write_cannot_come_back():
    """The staging seam both doors used is gone at the module level, not merely unreached. It
    wrote the owner's ``openclaw.json``, which no workspace has -- so a re-import here would
    raise ``ConfigPatchError`` on every agents.create in the fleet, not degrade quietly."""
    import importlib

    import pytest as _pytest

    with _pytest.raises(ModuleNotFoundError):
        importlib.import_module("core.services.config_patcher")


@pytest.fixture(autouse=True)
def _no_text_line(monkeypatch):
    """Profile writes read the workspace's texting line (the front-desk SOUL section); none here."""
    from unittest.mock import AsyncMock as _AsyncMock

    monkeypatch.setattr("core.repositories.agent_email_repo.text_line_for_owner", _AsyncMock(return_value=None))
