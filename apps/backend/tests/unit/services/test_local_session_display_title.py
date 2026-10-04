"""Lightbulb names stay separate from native titles and remain owner-controlled."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.auth import AuthContext
from core.services import local_session_relay, local_sessions
from core.services.messaging import ApiProblem


@pytest.mark.asyncio
async def test_display_title_owner_only_and_preserves_native_title(monkeypatch):
    row = {
        "ownerId": "org_test",
        "humanUserId": "user_owner",
        "sessionId": "session_test",
        "title": "Native title",
        "revision": "before",
    }
    writes = []
    hints = []

    async def authorize(*_args, **_kwargs):
        return row, {"revision": "enrollment"}

    async def put_session(updated, *_args, **_kwargs):
        writes.append(updated)

    async def publish(*args):
        hints.append(args)

    monkeypatch.setattr(local_sessions, "authorize_session", authorize)
    monkeypatch.setattr(local_sessions, "_session_public", lambda stored, _viewer: stored)
    monkeypatch.setattr(local_sessions.repo, "put_session", put_session)
    monkeypatch.setattr(local_sessions.messaging_repo, "_table", lambda: SimpleNamespace(name="sessions"))
    monkeypatch.setattr(local_session_relay, "publish_resources_changed", publish)

    with pytest.raises(ApiProblem):
        await local_sessions.set_display_title(
            AuthContext(user_id="user_teammate", org_id="org_test"), "session_test", "Mine"
        )
    assert writes == []

    result = await local_sessions.set_display_title(
        AuthContext(user_id="user_owner", org_id="org_test"), "session_test", "Readable name"
    )
    assert result["title"] == "Native title"
    assert result["displayTitle"] == "Readable name"
    assert hints == [("org_test", "user_owner")]

    with pytest.raises(ValidationError):
        local_sessions.DisplayTitleRequest(display_title="  \n  ")


@pytest.mark.asyncio
async def test_reset_after_a_native_rename_brings_back_claudes_original_title(monkeypatch):
    """A Lightbulb rename types `/rename` into an idle Claude session; "Use original title" must
    type Claude's old name back, or show it when Claude cannot take it right now."""
    import time

    row = {
        "ownerId": "org_test",
        "humanUserId": "user_owner",
        "sessionId": "session_test",
        "provider": "claude",
        "status": "idle",
        "onlineUntil": int(time.time()) + 60,
        "terminal": "attach",
        "deviceFeatures": ["sessionRename"],
        "title": "Native title",
        "revision": "before",
    }
    renames = []

    async def authorize(*_args, **_kwargs):
        return row, {"revision": "enrollment"}

    async def put_session(updated, *_args, **_kwargs):
        row.clear()
        row.update(updated)

    async def publish(*_args):
        return None

    async def rename(_row, name):
        renames.append(name)

    monkeypatch.setattr(local_sessions, "authorize_session", authorize)
    monkeypatch.setattr(local_sessions, "_session_public", lambda stored, _viewer: dict(stored))
    monkeypatch.setattr(local_sessions, "session_terminal", lambda stored: stored.get("terminal"))
    monkeypatch.setattr(local_sessions.repo, "put_session", put_session)
    monkeypatch.setattr(local_sessions.messaging_repo, "_table", lambda: SimpleNamespace(name="sessions"))
    monkeypatch.setattr(local_session_relay, "publish_resources_changed", publish)
    monkeypatch.setattr(local_sessions, "_rename_native", rename)
    owner = AuthContext(user_id="user_owner", org_id="org_test")

    await local_sessions.set_display_title(owner, "session_test", "Readable name")
    assert renames == ["Readable name"] and row["renamedFromTitle"] == "Native title"

    row["title"] = "Readable name"  # Claude's heartbeat now reports the new name
    result = await local_sessions.set_display_title(owner, "session_test", None)
    assert renames == ["Readable name", "Native title"]
    assert "displayTitle" not in result and "renamedFromTitle" not in result

    row["title"] = "Native title"  # Claude took its old name back
    # Claude busy: it cannot take its old name back yet, so Lightbulb shows it meanwhile.
    await local_sessions.set_display_title(owner, "session_test", "Second name")
    row.update(status="busy", title="Second name")
    result = await local_sessions.set_display_title(owner, "session_test", None)
    assert result["displayTitle"] == "Native title"


@pytest.mark.asyncio
async def test_reset_keeps_a_newer_rename_typed_in_the_terminal(monkeypatch):
    import time

    row = {
        "ownerId": "org_test",
        "humanUserId": "user_owner",
        "sessionId": "session_test",
        "provider": "claude",
        "status": "idle",
        "onlineUntil": int(time.time()) + 60,
        "terminal": "attach",
        "deviceFeatures": ["sessionRename"],
        "title": "Typed later",
        "displayTitle": "Lightbulb name",
        "renamedFromTitle": "Native title",
        "revision": "before",
    }
    renames = []

    async def authorize(*_args, **_kwargs):
        return row, {"revision": "enrollment"}

    async def put_session(updated, *_args, **_kwargs):
        row.clear()
        row.update(updated)

    async def noop(*_args):
        return None

    async def rename(_row, name):
        renames.append(name)

    monkeypatch.setattr(local_sessions, "authorize_session", authorize)
    monkeypatch.setattr(local_sessions, "_session_public", lambda stored, _viewer: dict(stored))
    monkeypatch.setattr(local_sessions, "session_terminal", lambda stored: stored.get("terminal"))
    monkeypatch.setattr(local_sessions.repo, "put_session", put_session)
    monkeypatch.setattr(local_sessions.messaging_repo, "_table", lambda: SimpleNamespace(name="sessions"))
    monkeypatch.setattr(local_session_relay, "publish_resources_changed", noop)
    monkeypatch.setattr(local_sessions, "_rename_native", rename)

    result = await local_sessions.set_display_title(
        AuthContext(user_id="user_owner", org_id="org_test"), "session_test", None
    )
    assert renames == [] and "displayTitle" not in result and "renamedFromTitle" not in result
