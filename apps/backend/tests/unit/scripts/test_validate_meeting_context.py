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
