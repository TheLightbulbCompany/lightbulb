"""A production invocation must refuse before any identity, secret, data or paid run."""

from unittest.mock import AsyncMock

import pytest

from scripts import validate_meeting_context as mod


async def test_refuses_production_before_any_external_action(monkeypatch, tmp_path):
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "prod")

    def external(*a, **kw):
        pytest.fail("external action before DEV guard")

    monkeypatch.setattr(mod, "_secret", external)
    monkeypatch.setattr(mod, "Clerk", external)
    monkeypatch.setattr(mod.meeting_context, "enabled", AsyncMock(side_effect=external))
    with pytest.raises(SystemExit, match="runs in dev only"):
        await mod.validate(tmp_path / "receipt.json")
    assert not (tmp_path / "receipt.json").exists()


@pytest.mark.parametrize("code,tries", [("NoSuchKey", 6), ("404", 6), ("AccessDenied", 1)])
async def test_native_receipt_retries_only_bounded_expected_missing_artifacts(monkeypatch, code, tries):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from botocore.exceptions import ClientError

    s3 = MagicMock()
    s3.get_object.side_effect = ClientError({"Error": {"Code": code}}, "GetObject")
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **kw: s3)
    wait = AsyncMock()
    monkeypatch.setattr(mod.asyncio, "sleep", wait)
    expected = RuntimeError if tries == 6 else ClientError
    with pytest.raises(expected):
        await mod.native_receipt(
            "org_test", SimpleNamespace(agent_id="agent", run_id="run", session_key="session"), "x" * 32, "marker"
        )
    assert s3.get_object.call_count == tries
    assert wait.await_count == tries - 1


async def test_initializes_dev_messaging_before_preference_read(monkeypatch, tmp_path):
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(mod.settings, "MESSAGING_TABLE", "")
    monkeypatch.setattr(mod.settings, "OAUTH_TOKENS_TABLE", "")

    async def preference(_):
        assert mod.settings.MESSAGING_TABLE == "lightbulb-dev-messaging"
        return False

    monkeypatch.setattr(mod.meeting_context, "enabled", preference)
    with pytest.raises(RuntimeError, match="followUps is disabled"):
        await mod.validate(tmp_path / "receipt.json")


async def test_refuses_cross_environment_messaging_table_before_read(monkeypatch, tmp_path):
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(mod.settings, "MESSAGING_TABLE", "lightbulb-prod-messaging")
    preference = AsyncMock()
    monkeypatch.setattr(mod.meeting_context, "enabled", preference)
    with pytest.raises(RuntimeError, match="non-DEV messaging table"):
        await mod.validate(tmp_path / "receipt.json")
    preference.assert_not_awaited()


@pytest.mark.parametrize("fault", [None, "legacy", "wrong_image", "updating", "unstable_backend"])
def test_deployed_transport_fences_exact_stable_dev_pool(monkeypatch, fault):
    from unittest.mock import MagicMock

    arn = "arn:aws:bedrock-agentcore:us-east-1:877352799272:runtime/lightbulb_dev_hermes_pool-test"
    image = "877352799272.dkr.ecr.us-east-1.amazonaws.com/lightbulb/hermes-agent-arm64:h-test"
    definition = "arn:aws:ecs:us-east-1:877352799272:task-definition/lightbulb-dev-backend:965"
    ecs, runtime = MagicMock(), MagicMock()
    ecs.describe_services.return_value = {
        "services": [
            {
                "taskDefinition": definition,
                "deployments": [{"rolloutState": "IN_PROGRESS" if fault == "unstable_backend" else "COMPLETED"}],
            }
        ]
    }
    ecs.describe_task_definition.return_value = {
        "taskDefinition": {
            "containerDefinitions": [
                {
                    "name": "backend",
                    "environment": [
                        {"name": "HERMES_SHARED_RUNTIME_ARN", "value": "" if fault == "legacy" else arn},
                        {"name": "AGENTCORE_HERMES_IMAGE", "value": image},
                    ],
                }
            ]
        }
    }
    runtime.get_agent_runtime.return_value = {
        "status": "UPDATING" if fault == "updating" else "READY",
        "agentRuntimeVersion": "5",
        "agentRuntimeArtifact": {
            "containerConfiguration": {"containerUri": "old" if fault == "wrong_image" else image}
        },
    }
    monkeypatch.setattr(mod.boto3, "client", lambda service, **_: ecs if service == "ecs" else runtime)
    monkeypatch.setattr(mod.settings, "HERMES_SHARED_RUNTIME_ARN", "")
    monkeypatch.setattr(mod.settings, "AGENTCORE_HERMES_IMAGE", "")
    if fault:
        with pytest.raises(RuntimeError):
            mod.deployed_transport("h-test")
        assert not mod.settings.HERMES_SHARED_RUNTIME_ARN
    else:
        receipt = mod.deployed_transport("h-test")
        assert receipt["runtimeArn"] == mod.settings.HERMES_SHARED_RUNTIME_ARN == arn
        assert receipt["runtimeVersion"] == "5" and receipt["image"] == image
