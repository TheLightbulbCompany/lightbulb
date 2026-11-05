"""contracts-v2 §1: one switch. Env master `SUPER_AGENT_ENV_ENABLED` + `containers.super_agent`, read only by
`flags_for`; clients see it as LiveConfig `features.superAgent`; off means today's behaviour, byte for byte."""

import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from core.config import settings
from core.services.super_agent_flags import flags_for

BACKEND = Path(__file__).resolve().parents[3]
GET_ROW = "core.repositories.container_repo.get_by_owner_id"


def test_env_master_defaults_off():
    from core.config import Settings

    assert Settings.model_fields["SUPER_AGENT_ENV_ENABLED"].default is False


async def test_env_master_off_ignores_an_on_row(monkeypatch):
    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", False)
    with patch(GET_ROW, AsyncMock(return_value={"super_agent": True})) as get_row:
        assert await flags_for("org_1") is False
    get_row.assert_not_awaited()  # prod never even reads the row


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"super_agent": True}, True),
        ({"super_agent": False}, False),
        ({}, False),
        (None, False),
        ({"super_agent": "true"}, False),  # only a real bool turns it on
    ],
)
async def test_env_master_on_reads_the_workspace_bool(monkeypatch, row, expected):
    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", True)
    with patch(GET_ROW, AsyncMock(return_value=row)):
        assert await flags_for("org_1") is expected


def test_no_other_reader():
    """Every other module asks `flags_for`; none reads the env master or the row attribute itself."""
    allowed = {
        BACKEND / "core" / "services" / "super_agent_flags.py",
        BACKEND / "core" / "config.py",  # declares the setting
    }
    pattern = re.compile(r"SUPER_AGENT_ENV_ENABLED|[\"']super_agent[\"']")
    offenders = [
        str(path.relative_to(BACKEND))
        for path in BACKEND.rglob("*.py")
        if path not in allowed
        and not {".venv", "tests", "vendor", "node_modules"} & set(path.relative_to(BACKEND).parts)
        and pattern.search(path.read_text(errors="ignore"))
    ]
    assert offenders == []


def test_env_enabled_callers_are_allowlisted():
    """`env_enabled()` is the env master without a workspace: only process-wide wiring may call it.
    Anything with a workspace asks `flags_for`."""
    allowed = {
        BACKEND / "core" / "services" / "super_agent_flags.py",
        BACKEND / "core" / "config.py",  # names it in the setting's docstring
        BACKEND / "main.py",  # Temporal client in the lifespan
        BACKEND / "scripts" / "temporal_worker.py",
        # Lane R: the AGENT# registry mirror is env-wide (every Paperclip agent write in an env with the table).
        BACKEND / "core" / "services" / "agent_registry.py",
        BACKEND / "core" / "services" / "paperclip_admin_client.py",
        BACKEND / "routers" / "paperclip_proxy.py",
    }
    offenders = [
        str(path.relative_to(BACKEND))
        for path in BACKEND.rglob("*.py")
        if path not in allowed
        and not {".venv", "tests", "vendor", "node_modules"} & set(path.relative_to(BACKEND).parts)
        and "env_enabled" in path.read_text(errors="ignore")
    ]
    assert offenders == []


# --- list_on (watchdog scan scope) -----------------------------------------------------------------

SCAN = "core.repositories.container_repo.scan_all"


async def test_list_on_empty_when_env_master_off(monkeypatch):
    from core.services.super_agent_flags import list_on

    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", False)
    with patch(SCAN, AsyncMock(return_value=[{"owner_id": "org_1", "super_agent": True}])) as scan:
        assert await list_on() == []
    scan.assert_not_awaited()


async def test_list_on_returns_only_flagged_owners(monkeypatch):
    from core.services.super_agent_flags import list_on

    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", True)
    rows = [
        {"owner_id": "org_on", "super_agent": True},
        {"owner_id": "org_off", "super_agent": False},
        {"owner_id": "org_str", "super_agent": "true"},
        {"owner_id": "org_none"},
    ]
    with patch(SCAN, AsyncMock(return_value=rows)):
        assert await list_on() == ["org_on"]


# --- LiveConfig ------------------------------------------------------------------------------------

LEGACY_KEYS = {"schemaVersion", "httpEndpoint", "realtimeEndpoint", "region", "userChannelKey", "workspaceId"}


def _live_config(client, row):
    with (
        patch("core.repositories.messaging_repo.ensure_user_stream", AsyncMock(return_value=uuid.uuid4())),
        patch("core.repositories.messaging_repo.enroll_sessions_member", AsyncMock(return_value=uuid.uuid4())),
        patch(GET_ROW, AsyncMock(return_value=row)) as get_row,
    ):
        response = client.get("/api/v1/messaging/live-config")
    assert response.status_code == 200
    return response.json(), get_row


def test_live_config_is_unchanged_when_the_master_is_off(client, monkeypatch):
    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", False)
    body, get_row = _live_config(client, {"super_agent": True})
    assert set(body) == LEGACY_KEYS  # no `features` key at all: the legacy payload
    get_row.assert_not_awaited()


def test_live_config_is_unchanged_for_a_workspace_that_is_off(client, monkeypatch):
    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", True)
    body, _ = _live_config(client, {"super_agent": False})
    assert set(body) == LEGACY_KEYS


def test_live_config_carries_the_switch_when_on(client, monkeypatch):
    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", True)
    body, _ = _live_config(client, {"super_agent": True})
    assert body["features"] == {"superAgent": True}
    assert set(body) == LEGACY_KEYS | {"features"}
