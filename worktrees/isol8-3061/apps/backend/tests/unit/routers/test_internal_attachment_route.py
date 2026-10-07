"""Hook-token gate + streaming for the bridge's attachment-materialization door
(Task 25, spec §5 "Ingress"; report 03 §1-§2). A microVM has no EFS, and the
existing Clerk-authenticated GET route can't be used by the bridge, so this is
a hook-token twin scoped to exactly what `sync.materialize_attachments` needs."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app

OWNER, ROOM, ATT = "org_1", "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"


def test_internal_attachment_route_requires_hook_token_and_streams_bytes():
    class Att:
        content_type = "image/png"
        file_name = "shot.png"

    with (
        patch("routers.messaging._require_hook_token") as guard,
        patch("routers.messaging.service.load_attachment", return_value=(Att(), b"\x89PNG...")),
    ):
        guard.return_value = None
        res = TestClient(app).get(
            f"/api/v1/internal/messaging/attachments/{OWNER}/{ROOM}/{ATT}", headers={"Authorization": "Bearer hook"}
        )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/png")
    assert res.content == b"\x89PNG..."
    guard.assert_awaited_once()


def test_internal_attachment_route_propagates_the_hook_token_rejection():
    """The route does no auth of its own -- a missing/bad token must fail the
    same way every other internal door does (``_require_hook_token`` itself
    raises the 401)."""
    res = TestClient(app).get(f"/api/v1/internal/messaging/attachments/{OWNER}/{ROOM}/{ATT}")
    assert res.status_code == 401
