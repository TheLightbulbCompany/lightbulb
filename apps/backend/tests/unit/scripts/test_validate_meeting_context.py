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
