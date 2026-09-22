"""A provider switch on an existing AgentCore runtime must actually converge it."""

from unittest.mock import AsyncMock

import pytest

from core.services import hermes_provisioning as hp


@pytest.mark.asyncio
async def test_fresh_boot_secret_starts_thawed_with_its_generation(monkeypatch):
    import json
    import boto3
    from core.services import agentcore_runtime

    written = []
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: object())
    monkeypatch.setattr(
        "core.secrets_manager.create_or_update_secret", lambda sm, name, value: written.append(json.loads(value))
    )
    await agentcore_runtime.write_boot_secret(
        "org_a",
        hook_token="hook",
        gateway_token="gateway",
        bedrock_bearer="bearer",
        provider_choice="openrouter_managed",
        generation="gen-new",
    )
    assert written == [
        {
            "hookToken": "hook",
            "gatewayToken": "gateway",
            "bedrockBearer": "bearer",
            "providerChoice": "openrouter_managed",
            "providerSwitchFrozen": False,
            "providerSwitchGeneration": "gen-new",
        }
    ]


@pytest.mark.asyncio
async def test_existing_runtime_switch_drains_rewrites_updates_and_stops(monkeypatch):
    order = []
    row = {"agentcore_runtime_arn": "arn:runtime/abc", "status": "running", "needs_provision": True}
    monkeypatch.setattr(hp.settings, "AGENTCORE_HERMES_IMAGE", "image:v1")
    monkeypatch.setattr(hp.settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(hp, "_load_role_and_aips", AsyncMock(return_value=("arn:role", {"sonnet": "arn:aip"})))
    monkeypatch.setattr(hp.browser_sessions, "ensure_browser_profile", AsyncMock(return_value=""))
    monkeypatch.setattr(
        "core.services.container_rpc.call",
        AsyncMock(side_effect=lambda owner, method: order.append("freeze") or {"frozen": True, "busy": 0}),
    )
    monkeypatch.setattr(hp, "_wait_for_idle_session", AsyncMock(side_effect=lambda owner: order.append("idle") or True))
    monkeypatch.setattr(hp, "write_profiles", AsyncMock(side_effect=lambda *args: order.append("profiles")))
    monkeypatch.setattr(hp, "build_runtime_request_for_owner", lambda *args, **kw: {"provider": kw["provider_choice"]})
    monkeypatch.setattr(hp, "_runtime_image_uri", lambda arn: "image:v1")
    monkeypatch.setattr(hp, "_update_runtime_image", lambda arn, request: order.append(("update", request["provider"])))
    monkeypatch.setattr(hp, "_runtime_status", lambda arn: order.append("ready") or "READY")
    monkeypatch.setattr(hp, "_stop_runtime_session", lambda arn, session: order.append("stop"))
    monkeypatch.setattr(
        hp,
        "_refresh_boot_secret_provider_choice",
        lambda owner, choice, **kw: order.append("thaw") if kw.get("frozen") is False else None,
    )
    monkeypatch.setattr(
        "core.services.container_provision.resolve_provider_choice",
        AsyncMock(return_value=("openrouter_managed", None)),
    )
    writes = []
    monkeypatch.setattr(
        hp.container_repo, "update_fields", AsyncMock(side_effect=lambda *args, **kw: writes.append(args[1]))
    )

    assert await hp.provision_hermes_runtime("org_a") == "arn:runtime/abc"
    assert order == ["freeze", "idle", "profiles", ("update", "openrouter_managed"), "ready", "stop", "thaw"]
    assert writes[-1]["needs_provision"] is False


@pytest.mark.asyncio
async def test_busy_runtime_switch_does_not_rewrite_profile(monkeypatch):
    row = {"agentcore_runtime_arn": "arn:runtime/abc", "status": "running", "needs_provision": True}
    monkeypatch.setattr(hp.settings, "AGENTCORE_HERMES_IMAGE", "image:v1")
    monkeypatch.setattr(hp.settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(hp, "_load_role_and_aips", AsyncMock(return_value=("arn:role", {"sonnet": "arn:aip"})))
    monkeypatch.setattr(hp.browser_sessions, "ensure_browser_profile", AsyncMock(return_value=""))
    monkeypatch.setattr("core.services.container_rpc.call", AsyncMock(return_value={"frozen": True, "busy": 1}))
    monkeypatch.setattr(hp, "_wait_for_idle_session", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "core.services.container_provision.resolve_provider_choice",
        AsyncMock(return_value=("openrouter_managed", None)),
    )
    write = AsyncMock()
    monkeypatch.setattr(hp, "write_profiles", write)

    with pytest.raises(RuntimeError, match="busy"):
        await hp.provision_hermes_runtime("org_a")
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_runtime_refreshes_boot_choice_without_rotating_bearer(monkeypatch):
    row = {"agentcore_runtime_arn": "arn:runtime/abc", "needs_provision": True, "provider_switch_generation": "gen-new"}
    order = []
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(
        "core.services.container_provision.resolve_provider_choice",
        AsyncMock(side_effect=lambda owner: order.append("resolve") or ("openrouter_managed", None)),
    )
    ensure_key = AsyncMock(side_effect=lambda owner: order.append("key") or "owner-key-hash")
    monkeypatch.setattr("core.services.openrouter_keys.ensure_owner_key_for_settings", ensure_key)
    refresh = []
    monkeypatch.setattr(
        hp,
        "_refresh_boot_secret_provider_choice",
        lambda owner, choice, **kw: order.append("refresh") or refresh.append((owner, choice, kw)),
        raising=False,
    )
    monkeypatch.setattr(
        "core.billing.bedrock_gate_tokens.mint_gate_token",
        AsyncMock(side_effect=AssertionError("must not rotate bearer")),
    )

    await hp.prepare_hermes_workspace("org_a")
    ensure_key.assert_awaited_once_with("org_a")

    assert refresh == [("org_a", "openrouter_managed", {"generation": "gen-new"})]
    assert order == ["resolve", "key", "refresh"]


def test_refresh_boot_choice_preserves_all_existing_tokens(monkeypatch):
    import json
    import boto3

    data = {
        "hookToken": "hook",
        "gatewayToken": "gateway",
        "bedrockBearer": "bearer",
        "providerChoice": "bedrock_claude",
    }

    class Secrets:
        current = "v1"

        def get_secret_value(self, *, SecretId):
            return {"VersionId": self.current, "SecretString": json.dumps(data)}

        def put_secret_value(self, *, SecretId, SecretString, VersionStages):
            data.clear()
            data.update(json.loads(SecretString))
            return {"VersionId": "v2"}

        def update_secret_version_stage(self, **kw):
            self.current = kw["MoveToVersionId"]

    monkeypatch.setattr(boto3, "client", lambda *a, **kw: Secrets())
    monkeypatch.setattr(
        hp.container_repo,
        "_get_table",
        lambda: type(
            "Table",
            (),
            {
                "get_item": lambda self, **kw: {
                    "Item": {"provider_switch_generation": "gen-new", "needs_provision": True}
                }
            },
        )(),
    )
    hp._refresh_boot_secret_provider_choice("org_a", "openrouter_managed", generation="gen-new")
    assert data == {
        "hookToken": "hook",
        "gatewayToken": "gateway",
        "bedrockBearer": "bearer",
        "providerChoice": "openrouter_managed",
        "providerSwitchFrozen": True,
        "providerSwitchGeneration": "gen-new",
    }
    hp._refresh_boot_secret_provider_choice("org_a", "openrouter_managed", frozen=False, generation="gen-new")
    assert data["providerSwitchFrozen"] is False
    assert data["providerSwitchGeneration"] == "gen-new"
    hp._refresh_boot_secret_provider_choice("org_a", "openrouter_managed", generation="gen-new")
    assert data["providerSwitchFrozen"] is False


def test_delayed_old_marker_cannot_replace_newer_published_marker(monkeypatch):
    import json
    import boto3
    from botocore.exceptions import ClientError

    class Secrets:
        current = "initial"
        versions = {
            "initial": {
                "hookToken": "hook",
                "gatewayToken": "gateway",
                "bedrockBearer": "bearer",
                "providerChoice": "bedrock_claude",
            }
        }
        pending = None

        def get_secret_value(self, *, SecretId):
            return {"VersionId": self.current, "SecretString": json.dumps(self.versions[self.current])}

        def put_secret_value(self, *, SecretId, SecretString, VersionStages):
            version = f"candidate-{len(self.versions)}"
            self.versions[version] = json.loads(SecretString)
            if self.pending:
                callback, self.pending = self.pending, None
                callback()
            return {"VersionId": version}

        def update_secret_version_stage(
            self, *, SecretId, VersionStage, RemoveFromVersionId=None, MoveToVersionId=None
        ):
            if VersionStage == "AWSCURRENT":
                if self.current != RemoveFromVersionId:
                    raise ClientError(
                        {"Error": {"Code": "InvalidRequestException", "Message": "stale"}}, "UpdateSecretVersionStage"
                    )
                self.current = MoveToVersionId

    sm = Secrets()
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: sm)
    generation = ["old"]
    monkeypatch.setattr(
        hp.container_repo,
        "_get_table",
        lambda: type(
            "Table",
            (),
            {
                "get_item": lambda self, **kw: {
                    "Item": {"provider_switch_generation": generation[0], "needs_provision": True}
                }
            },
        )(),
    )

    def publish_new():
        generation[0] = "new"
        hp._refresh_boot_secret_provider_choice("org_a", "openrouter_managed", generation="new")

    sm.pending = publish_new
    with pytest.raises(ClientError):
        hp._refresh_boot_secret_provider_choice("org_a", "byo_key", generation="old")
    assert sm.versions[sm.current]["providerSwitchGeneration"] == "new"
    assert sm.versions[sm.current]["providerChoice"] == "openrouter_managed"


def test_delayed_old_thaw_cannot_unfreeze_new_generation(monkeypatch):
    import json
    import boto3
    from botocore.exceptions import ClientError

    class Secrets:
        current = "old"
        versions = {
            "old": {
                "hookToken": "hook",
                "gatewayToken": "gateway",
                "bedrockBearer": "bearer",
                "providerChoice": "byo_key",
                "providerSwitchFrozen": True,
                "providerSwitchGeneration": "old",
            }
        }
        pending = None

        def get_secret_value(self, *, SecretId):
            return {"VersionId": self.current, "SecretString": json.dumps(self.versions[self.current])}

        def put_secret_value(self, *, SecretId, SecretString, VersionStages):
            version = f"candidate-{len(self.versions)}"
            self.versions[version] = json.loads(SecretString)
            if self.pending:
                callback, self.pending = self.pending, None
                callback()
            return {"VersionId": version}

        def update_secret_version_stage(
            self, *, SecretId, VersionStage, RemoveFromVersionId=None, MoveToVersionId=None
        ):
            if VersionStage == "AWSCURRENT":
                if self.current != RemoveFromVersionId:
                    raise ClientError(
                        {"Error": {"Code": "InvalidRequestException", "Message": "stale"}}, "UpdateSecretVersionStage"
                    )
                self.current = MoveToVersionId

    sm = Secrets()
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: sm)
    generation = ["old"]
    monkeypatch.setattr(
        hp.container_repo,
        "_get_table",
        lambda: type(
            "Table",
            (),
            {
                "get_item": lambda self, **kw: {
                    "Item": {"provider_switch_generation": generation[0], "needs_provision": True}
                }
            },
        )(),
    )

    def publish_new():
        generation[0] = "new"
        hp._refresh_boot_secret_provider_choice("org_a", "openrouter_managed", generation="new")

    sm.pending = publish_new
    with pytest.raises(ClientError):
        hp._refresh_boot_secret_provider_choice("org_a", "byo_key", frozen=False, generation="old")
    assert sm.versions[sm.current]["providerSwitchFrozen"] is True
    assert sm.versions[sm.current]["providerSwitchGeneration"] == "new"


def test_superseded_generation_never_stages_a_secret(monkeypatch):
    import boto3
    from core.repositories.container_repo import ProvisionSuperseded

    class Secrets:
        def get_secret_value(self, **kw):
            return {"VersionId": "new", "SecretString": "{}"}

        def put_secret_value(self, **kw):
            raise AssertionError("stale switch must not stage a secret")

    monkeypatch.setattr(
        hp.container_repo,
        "_get_table",
        lambda: type(
            "Table",
            (),
            {"get_item": lambda self, **kw: {"Item": {"provider_switch_generation": "new", "needs_provision": True}}},
        )(),
    )
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: Secrets())
    with pytest.raises(ProvisionSuperseded):
        hp._refresh_boot_secret_provider_choice("org_a", "byo_key", generation="old")


@pytest.mark.asyncio
async def test_stop_failure_keeps_pending_marker_for_retry(monkeypatch):
    row = {"agentcore_runtime_arn": "arn:runtime/abc", "status": "running", "needs_provision": True}
    monkeypatch.setattr(hp.settings, "AGENTCORE_HERMES_IMAGE", "image:v1")
    monkeypatch.setattr(hp.settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(hp, "_load_role_and_aips", AsyncMock(return_value=("arn:role", {"sonnet": "arn:aip"})))
    monkeypatch.setattr(hp.browser_sessions, "ensure_browser_profile", AsyncMock(return_value=""))
    monkeypatch.setattr("core.services.container_rpc.call", AsyncMock(return_value={"frozen": True, "busy": 0}))
    monkeypatch.setattr(hp, "_wait_for_idle_session", AsyncMock(return_value=True))
    monkeypatch.setattr(hp, "write_profiles", AsyncMock())
    monkeypatch.setattr(hp, "_runtime_image_uri", lambda arn: "image:v1")
    monkeypatch.setattr(hp, "build_runtime_request_for_owner", lambda *a, **kw: {})
    monkeypatch.setattr(hp, "_update_runtime_image", lambda *a: None)
    monkeypatch.setattr(hp, "_runtime_status", lambda arn: "READY")
    monkeypatch.setattr(hp, "_stop_runtime_session", lambda *a: (_ for _ in ()).throw(RuntimeError("stop failed")))
    monkeypatch.setattr(
        hp,
        "_refresh_boot_secret_provider_choice",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must stay frozen")),
    )
    monkeypatch.setattr(
        "core.services.container_provision.resolve_provider_choice",
        AsyncMock(return_value=("openrouter_managed", None)),
    )
    terminal = AsyncMock()
    monkeypatch.setattr(hp.container_repo, "update_fields", terminal)

    with pytest.raises(RuntimeError, match="stop failed"):
        await hp.provision_hermes_runtime("org_a")
    terminal.assert_not_awaited()
    assert row["needs_provision"] is True
    thawed = []
    monkeypatch.setattr(hp, "_stop_runtime_session", lambda *a: None)
    monkeypatch.setattr(
        hp, "_refresh_boot_secret_provider_choice", lambda owner, choice, **kw: thawed.append(kw["frozen"])
    )
    await hp.provision_hermes_runtime("org_a")
    assert thawed == [False]
    assert terminal.await_args.args[1]["needs_provision"] is False


@pytest.mark.asyncio
async def test_already_terminated_session_allows_switch_to_finish(monkeypatch):
    from botocore.exceptions import ClientError

    row = {"agentcore_runtime_arn": "arn:runtime/abc", "status": "running", "needs_provision": True}
    monkeypatch.setattr(hp.settings, "AGENTCORE_HERMES_IMAGE", "image:v1")
    monkeypatch.setattr(hp.settings, "OWNER_STATE_BUCKET", "bucket")
    monkeypatch.setattr(hp.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(hp, "_load_role_and_aips", AsyncMock(return_value=("arn:role", {"sonnet": "arn:aip"})))
    monkeypatch.setattr(hp.browser_sessions, "ensure_browser_profile", AsyncMock(return_value=""))
    monkeypatch.setattr("core.services.container_rpc.call", AsyncMock(return_value={"frozen": True, "busy": 0}))
    monkeypatch.setattr(hp, "_wait_for_idle_session", AsyncMock(return_value=True))
    monkeypatch.setattr(hp, "write_profiles", AsyncMock())
    monkeypatch.setattr(hp, "_runtime_image_uri", lambda arn: "image:v1")
    monkeypatch.setattr(hp, "build_runtime_request_for_owner", lambda *a, **kw: {})
    monkeypatch.setattr(hp, "_update_runtime_image", lambda *a: None)
    monkeypatch.setattr(hp, "_runtime_status", lambda arn: "READY")
    missing = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "Session not found"}}, "StopRuntimeSession"
    )
    monkeypatch.setattr(hp, "_stop_runtime_session", lambda *a: (_ for _ in ()).throw(missing))
    monkeypatch.setattr(hp, "_refresh_boot_secret_provider_choice", lambda *a, **kw: None)
    monkeypatch.setattr(
        "core.services.container_provision.resolve_provider_choice",
        AsyncMock(return_value=("openrouter_managed", None)),
    )
    terminal = AsyncMock()
    monkeypatch.setattr(hp.container_repo, "update_fields", terminal)

    await hp.provision_hermes_runtime("org_a")
    assert terminal.await_args.args[1]["needs_provision"] is False
