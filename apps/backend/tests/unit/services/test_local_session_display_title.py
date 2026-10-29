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

    async def publish(*args, **_):
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
