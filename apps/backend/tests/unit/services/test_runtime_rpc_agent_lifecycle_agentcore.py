"""Q9a rows #1 + #2 (task-9-scan.md): agents.create / agents.update on an AgentCore row.

Both RPC doors used to stage Paperclip agent metadata into openclaw.json via
``config_patcher.apply_paperclip_meta`` -- an EFS write that raises
``ConfigPatchError`` for an owner with no openclaw.json at all (an AgentCore
workspace has none; see the module docstring on ``hermes_provisioning``).
``agents.create`` propagated that error as an unhandled 500; ``agents.update``
swallowed it silently behind a bare ``except Exception``. Both now branch on
the owner's runtime instead.

Fix round 1 (Opus task review, M6-R27/M6-R28/M6-R29): every AgentCore test
below drives the REAL ``_create_agent`` / ``_mirror_agent_update`` /
``write_profile_for_agent`` -- only the S3, Paperclip, and DynamoDB edges are
mocked (``pair_with_openclaw``, ``container_rpc.call``, and the two
orchestration functions above are never replaced with a stand-in on an
AgentCore path; the ECS-parity tests, which are not exercising AgentCore
behaviour, keep the lighter mocking style ``test_runtime_rpc_agent_create.py``
already established).
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
    """``_refresh_agentcore_soul`` now reads the stored ``SOUL.md`` first (M6-R48, the
    generated-marker check) -- a real S3 GET when unmocked. Default every test in this module to
    "the profile has none yet", which is the regenerate branch they were all written against; the
    marker tests below override it."""
    monkeypatch.setattr(rpc.hermes_profiles, "read_soul_md", MagicMock(return_value=None))


def _agentcore(is_agentcore: bool):
    """``_is_agentcore`` is ``async def`` -- the replacement must be a real coroutine
    function, never a bare value (an ``await`` on a non-awaitable raises ``TypeError``)."""

    async def fake(owner_id):
        return is_agentcore

    return fake


class _Http:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def _wire_paperclip_mirror(monkeypatch, admin):
    """The Paperclip edge _mirror_agent_update's own PATCH-mirror step reaches -- same pattern
    as test_runtime_rpc_agent_update.py's _wire_admin."""
    monkeypatch.setattr(rpc.httpx, "AsyncClient", lambda **_kwargs: _Http())
    monkeypatch.setattr(rpc, "PaperclipAdminClient", lambda http_client: admin)
    monkeypatch.setattr(rpc, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    monkeypatch.setattr(rpc, "resolve_company_id_for_owner", AsyncMock(return_value="company"))


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


_BEDROCK_INPUTS = ProfileInputs(
    provider_choice="bedrock_claude",
    byo_provider=None,
    byo_api_key=None,
    aip_arns={"sonnet": "arn:aip:sonnet", "opus": "arn:aip:opus"},
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
    monkeypatch.setattr(rpc, "create_paperclip_for_chat", AsyncMock(return_value=paperclip_agent))
    _wire_profile_inputs(monkeypatch, _BEDROCK_INPUTS)
    upload_mock = AsyncMock()
    monkeypatch.setattr(rpc.hermes_profiles, "upload_profile", upload_mock)
    apply_meta = AsyncMock()
    monkeypatch.setattr("core.services.config_patcher.apply_paperclip_meta", apply_meta)
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
    apply_meta.assert_not_awaited()
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
async def test_agents_create_on_ecs_row_still_pairs_and_applies_paperclip_meta(monkeypatch):
    """The ECS branch stays byte-identical -- not an AgentCore test, so the lighter mocking
    style test_runtime_rpc_agent_create.py already uses is fine here too."""
    container = {"gateway_token": "tok"}  # no runtime_host -> "ecs" (host_for's own default)
    paperclip_agent = {"id": "agent_new", "name": "Ada"}
    monkeypatch.setattr(rpc.container_rpc, "running_container", AsyncMock(return_value=(container, "10.0.0.1")))
    monkeypatch.setattr(rpc, "create_paperclip_for_chat", AsyncMock(return_value=paperclip_agent))
    monkeypatch.setattr(rpc, "pair_with_openclaw", AsyncMock())
    monkeypatch.setattr(rpc, "ensure_operator_device_paired", AsyncMock())
    apply_meta = AsyncMock()
    monkeypatch.setattr("core.services.config_patcher.apply_paperclip_meta", apply_meta)
    write_profile = AsyncMock()
    monkeypatch.setattr("core.services.hermes_provisioning.write_profile_for_agent", write_profile)
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
    apply_meta.assert_awaited_once()
    assert apply_meta.await_args.kwargs["meta"] == {"role": "engineer", "title": "Backend engineer"}
    write_profile.assert_not_awaited()
    publish.assert_not_awaited()  # unchanged ECS behaviour: agents.create never invalidated here


@pytest.mark.asyncio
async def test_agents_create_hermes_profile_error_is_a_clean_rpc_error_not_a_500(monkeypatch):
    """Drives the real write_profile_for_agent into its real HermesProfileError raise (a failed
    Paperclip key mint) rather than mocking the raise directly."""
    container = {"gateway_token": "tok", "runtime_host": "agentcore"}
    paperclip_agent = {"id": "agent_new", "name": "Ada"}
    monkeypatch.setattr(rpc.container_rpc, "running_container", AsyncMock(return_value=(container, "10.0.0.1")))
    monkeypatch.setattr(rpc, "create_paperclip_for_chat", AsyncMock(return_value=paperclip_agent))
    _wire_profile_inputs(monkeypatch, _BEDROCK_INPUTS, paperclip_api_key=None)
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
async def test_mirror_agent_update_promotes_to_ceo_re_derives_the_premium_model_block(monkeypatch):
    """M6-R28 + I5: the real _mirror_agent_update, on an AgentCore row, for a role change --
    apply_paperclip_meta must never run, and the patched model block must equal exactly what
    write_agent_profile would produce for a ceo."""
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "general"})
    admin.patch_agent = AsyncMock(return_value={})
    _wire_paperclip_mirror(monkeypatch, admin)
    monkeypatch.setattr(rpc.container_repo, "get_by_owner_id", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "core.services.hermes_provisioning.resolve_profile_inputs", AsyncMock(return_value=_BEDROCK_INPUTS)
    )
    patch_config = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", patch_config)
    apply_meta = AsyncMock()
    monkeypatch.setattr("core.services.config_patcher.apply_paperclip_meta", apply_meta)
    write_profile = AsyncMock()
    monkeypatch.setattr("core.services.hermes_provisioning.write_profile_for_agent", write_profile)
    # A role change also carries a "role" key in `body`, so _refresh_agentcore_soul runs too.
    monkeypatch.setattr(rpc, "load_agent_roster", AsyncMock(return_value=[{"id": "a1", "role": "ceo"}]))
    monkeypatch.setattr(rpc.hermes_profiles, "upload_soul_md", MagicMock())

    await rpc._mirror_agent_update("org_1", {"agentId": "a1", "params": {"paperclip": {"role": "ceo"}}}, agentcore=True)

    apply_meta.assert_not_awaited()
    write_profile.assert_not_awaited()  # never re-runs the full writer -- would clobber edits
    admin.patch_agent.assert_awaited_once()  # Paperclip still gets the role change
    expected_model, _aux_model, _aux_provider = model_block(
        provider_choice="bedrock_claude", byo_provider=None, aip_arns=_BEDROCK_INPUTS.aip_arns, is_ceo=True
    )
    patch_config.assert_called_once_with("org_1", "a1", {"model": expected_model})


@pytest.mark.asyncio
async def test_mirror_agent_update_demotes_from_ceo_uses_the_standard_model_block(monkeypatch):
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "ceo", "reportsTo": "root"})
    admin.patch_agent = AsyncMock(return_value={})
    _wire_paperclip_mirror(monkeypatch, admin)
    monkeypatch.setattr(rpc.container_repo, "get_by_owner_id", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "core.services.hermes_provisioning.resolve_profile_inputs", AsyncMock(return_value=_BEDROCK_INPUTS)
    )
    patch_config = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", patch_config)
    apply_meta = AsyncMock()
    monkeypatch.setattr("core.services.config_patcher.apply_paperclip_meta", apply_meta)
    monkeypatch.setattr(rpc, "load_agent_roster", AsyncMock(return_value=[{"id": "a1", "role": "general"}]))
    monkeypatch.setattr(rpc.hermes_profiles, "upload_soul_md", MagicMock())

    await rpc._mirror_agent_update(
        "org_1", {"agentId": "a1", "params": {"paperclip": {"role": "general"}}}, agentcore=True
    )

    apply_meta.assert_not_awaited()
    expected_model, _aux_model, _aux_provider = model_block(
        provider_choice="bedrock_claude", byo_provider=None, aip_arns=_BEDROCK_INPUTS.aip_arns, is_ceo=False
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
        rpc,
        "load_agent_roster",
        AsyncMock(return_value=[{"id": "a1", "name": "Renamed Ada", "role": "general", "title": "Old Title"}]),
    )
    upload_soul = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "upload_soul_md", upload_soul)
    patch_config = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", patch_config)
    apply_meta = AsyncMock()
    monkeypatch.setattr("core.services.config_patcher.apply_paperclip_meta", apply_meta)
    write_profile = AsyncMock()
    monkeypatch.setattr("core.services.hermes_provisioning.write_profile_for_agent", write_profile)

    await rpc._mirror_agent_update("org_1", {"agentId": "a1", "name": "Renamed Ada"}, agentcore=True)

    apply_meta.assert_not_awaited()
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
        rpc, "load_agent_roster", AsyncMock(return_value=[{"id": "a1", "name": "Renamed Ada", "role": "general"}])
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


@pytest.mark.asyncio
async def test_mirror_agent_update_rejects_a_raw_model_ref_on_an_agentcore_row():
    """M6-R29: a raw ref is never stored verbatim -- only the writer's own tier vocabulary."""
    with pytest.raises(ApiProblem) as exc_info:
        await rpc._mirror_agent_update(
            "org_1", {"agentId": "a1", "model": "amazon-bedrock/arn:aws:bedrock:us-east-1::x"}, agentcore=True
        )
    assert exc_info.value.error.code == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_mirror_agent_update_accepts_a_known_tier_and_re_derives_the_block_via_the_writer(monkeypatch):
    monkeypatch.setattr(rpc.container_repo, "get_by_owner_id", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "core.services.hermes_provisioning.resolve_profile_inputs", AsyncMock(return_value=_BEDROCK_INPUTS)
    )
    patch_config = MagicMock()
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", patch_config)

    await rpc._mirror_agent_update("org_1", {"agentId": "a1", "model": "standard"}, agentcore=True)

    expected_model, _aux_model, _aux_provider = model_block(
        provider_choice="bedrock_claude", byo_provider=None, aip_arns=_BEDROCK_INPUTS.aip_arns, is_ceo=False
    )
    patch_config.assert_called_once_with("org_1", "a1", {"model": expected_model})


@pytest.mark.asyncio
async def test_mirror_agent_update_model_only_never_touches_paperclip_even_on_agentcore(monkeypatch):
    """Unchanged pre-existing behaviour (test_noop_update_skips_paperclip's own assertion): a
    model-only update never touches Paperclip, regardless of transport."""
    monkeypatch.setattr(rpc.container_repo, "get_by_owner_id", AsyncMock(return_value={}))
    monkeypatch.setattr(
        "core.services.hermes_provisioning.resolve_profile_inputs", AsyncMock(return_value=_BEDROCK_INPUTS)
    )
    monkeypatch.setattr(rpc.hermes_profiles, "patch_profile_config", MagicMock())
    client = MagicMock()
    monkeypatch.setattr(rpc.httpx, "AsyncClient", client)

    await rpc._mirror_agent_update("org_1", {"agentId": "a1", "model": "standard"}, agentcore=True)

    client.assert_not_called()


# ---------------------------------------------------------------------------
# execute_sync integration -- the real _mirror_agent_update, dispatched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_update_on_agentcore_row_returns_ok_and_never_reaches_container_rpc(monkeypatch):
    monkeypatch.setattr(rpc, "_is_agentcore", _agentcore(True))
    admin = MagicMock()
    admin.get_agent = AsyncMock(return_value={"id": "a1", "companyId": "company", "role": "general"})
    admin.patch_agent = AsyncMock(return_value={})
    _wire_paperclip_mirror(monkeypatch, admin)
    monkeypatch.setattr(rpc, "load_agent_roster", AsyncMock(return_value=[{"id": "a1", "name": "Renamed"}]))
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
async def test_agents_update_on_ecs_row_still_forwards_to_container_rpc(monkeypatch):
    """The ECS branch stays byte-identical: unchanged fallthrough to the live gateway. Not an
    AgentCore test -- mocking _mirror_agent_update here only proves execute_sync's own dispatch,
    which is the point."""
    monkeypatch.setattr(rpc, "_is_agentcore", _agentcore(False))
    monkeypatch.setattr(rpc, "_mirror_agent_update", AsyncMock())
    call_mock = AsyncMock(return_value={"ok": True, "forwarded": True})
    monkeypatch.setattr(rpc.container_rpc, "call", call_mock)

    result = await rpc.execute_sync(
        owner_id="owner_1",
        user_id="user_1",
        request_id="r1",
        method="agents.update",
        params={"agentId": "a1", "model": "gpt-5"},
    )

    assert result == {"ok": True, "forwarded": True}
    call_mock.assert_awaited_once_with(
        "owner_1", "agents.update", {"agentId": "a1", "model": "gpt-5"}, timeout_s=rpc._RPC_TIMEOUT_S
    )


# ---------------------------------------------------------------------------
# M6 task 10, row 12 -- agents.update must not answer ok while writing nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agents_update_tools_is_refused_on_an_agentcore_row(monkeypatch):
    """The scan's worst failure class: the Tools tab's Save returned {"ok": True} while
    _mirror_agent_update early-returned and wrote NOTHING. Hermes gates by toolset, so the
    field has no home -- refuse it instead of confirming it."""
    monkeypatch.setattr(rpc, "_is_agentcore", _agentcore(True))
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
    monkeypatch.setattr(rpc, "_is_agentcore", _agentcore(True))
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
    monkeypatch.setattr(rpc, "_is_agentcore", _agentcore(True))
    mirror = AsyncMock()
    monkeypatch.setattr(rpc, "_mirror_agent_update", mirror)

    result = await rpc.execute_sync(
        owner_id="owner_1",
        user_id="user_1",
        request_id="r1",
        method="agents.update",
        params={"agentId": "a1", "name": "Ada", "model": "premium", "params": {"paperclip": {"role": "ceo"}}},
    )

    assert result == {"ok": True}
    mirror.assert_awaited_once()


@pytest.mark.asyncio
async def test_agents_update_tools_still_forwards_on_an_ecs_row(monkeypatch):
    """ECS behaviour is unchanged -- the live gateway owns per-tool allow/deny there."""
    monkeypatch.setattr(rpc, "_is_agentcore", _agentcore(False))
    monkeypatch.setattr(rpc, "_mirror_agent_update", AsyncMock())
    call_mock = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(rpc.container_rpc, "call", call_mock)

    result = await rpc.execute_sync(
        owner_id="owner_1",
        user_id="user_1",
        request_id="r1",
        method="agents.update",
        params={"agentId": "a1", "tools": {"deny": ["exec"]}},
    )

    assert result == {"ok": True}
    call_mock.assert_awaited_once()
