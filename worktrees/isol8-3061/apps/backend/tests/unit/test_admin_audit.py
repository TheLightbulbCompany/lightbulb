"""Unit tests for @audit_admin_action decorator.

Verifies:
1. New `target_user_id_override` kwarg sets the audit row's target
   to the override value verbatim (used for catalog actions that
   target the shared catalog, not a specific user).
2. Existing kwarg-based target resolution is preserved when no
   override is given.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.services.admin_audit import audit_admin_action


class _Req:
    """Minimal stand-in for a FastAPI Request — decorator reads headers + client."""

    headers = {"user-agent": "pytest"}
    client = type("c", (), {"host": "127.0.0.1"})()


def _auth() -> AuthContext:
    """Real AuthContext so the decorator's isinstance check passes."""
    return AuthContext(user_id="user_admin_123")


@pytest.mark.asyncio
async def test_audit_uses_static_target_override():
    """When target_user_id_override is passed, the audit row uses it verbatim
    rather than pulling from kwargs."""
    create_mock = AsyncMock(return_value={})

    @audit_admin_action("catalog.test", target_user_id_override="__catalog__")
    async def handler(request, auth):
        return {"ok": True}

    with patch(
        "core.repositories.admin_actions_repo.create",
        new=create_mock,
    ):
        await handler(request=_Req(), auth=_auth())

    assert create_mock.await_count == 1
    # repo.create is keyword-only; all fields land in kwargs.
    assert create_mock.await_args.kwargs["target_user_id"] == "__catalog__"


@pytest.mark.asyncio
async def test_audit_falls_back_to_kwarg_without_override():
    """Sanity: existing behavior is preserved when no override is given."""
    create_mock = AsyncMock(return_value={})

    @audit_admin_action("user.test")
    async def handler(user_id, request, auth):
        return {"ok": True}

    with patch(
        "core.repositories.admin_actions_repo.create",
        new=create_mock,
    ):
        await handler(user_id="user_target_xyz", request=_Req(), auth=_auth())

    assert create_mock.await_count == 1
    assert create_mock.await_args.kwargs["target_user_id"] == "user_target_xyz"


@pytest.mark.asyncio
async def test_audit_captures_specified_kwargs_as_payload():
    """capture_params=['slug'] stores {slug: 'pitch'} in payload."""
    create_mock = AsyncMock()

    @audit_admin_action("catalog.test", target_user_id_override="__catalog__", capture_params=["slug"])
    async def handler(slug, request, auth):
        return {"ok": True}

    class _Req:
        headers = {"user-agent": "pytest"}
        client = type("c", (), {"host": "127.0.0.1"})()

    from core.auth import AuthContext

    with patch("core.repositories.admin_actions_repo.create", new=create_mock):
        await handler(slug="pitch", request=_Req(), auth=AuthContext(user_id="user_admin"))

    row = create_mock.await_args.kwargs
    assert row["payload"] == {"slug": "pitch"}


@pytest.mark.asyncio
async def test_audit_captures_pydantic_model_via_capture_params():
    """capture_params on a Pydantic kwarg serializes via model_dump()."""
    from pydantic import BaseModel

    class DummyReq(BaseModel):
        agent_id: str
        slug: str | None = None

    create_mock = AsyncMock()

    @audit_admin_action("catalog.test", target_user_id_override="__catalog__", capture_params=["req"])
    async def handler(req, request, auth):
        return {"ok": True}

    class _Req:
        headers = {"user-agent": "pytest"}
        client = type("c", (), {"host": "127.0.0.1"})()

    from core.auth import AuthContext

    with patch("core.repositories.admin_actions_repo.create", new=create_mock):
        await handler(
            req=DummyReq(agent_id="agent_abc", slug="pitch"),
            request=_Req(),
            auth=AuthContext(user_id="user_admin"),
        )

    row = create_mock.await_args.kwargs
    assert row["payload"] == {"req": {"agent_id": "agent_abc", "slug": "pitch"}}


@pytest.mark.asyncio
async def test_audit_falls_back_to_body_when_capture_params_absent():
    """Existing behavior: without capture_params, still extracts body kwarg."""
    from pydantic import BaseModel

    class DummyBody(BaseModel):
        note: str

    create_mock = AsyncMock()

    @audit_admin_action("existing.test")
    async def handler(user_id, body, request, auth):
        return {"ok": True}

    class _Req:
        headers = {"user-agent": "pytest"}
        client = type("c", (), {"host": "127.0.0.1"})()

    from core.auth import AuthContext

    with patch("core.repositories.admin_actions_repo.create", new=create_mock):
        await handler(
            user_id="user_xyz",
            body=DummyBody(note="hello"),
            request=_Req(),
            auth=AuthContext(user_id="user_admin"),
        )

    row = create_mock.await_args.kwargs
    assert row["payload"] == {"note": "hello"}
