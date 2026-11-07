"""The internal recall body's lane/audience fields and the Router's link route (wave2-M Task 6)."""

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import container_auth, memory_api, memory_links  # noqa: E402
from main import app  # noqa: E402

PAGE = {"asOf": "x", "usedEvidenceTokens": 0, "results": []}


@pytest.fixture
def client():
    with patch.object(container_auth, "_require_hook_token", AsyncMock()):
        yield TestClient(app)


@pytest.fixture
def recall_page():
    with patch.object(memory_api, "recall_page", AsyncMock(return_value=PAGE)) as m:
        yield m


def _recall(client, **body):
    return client.post("/api/v1/internal/memory/recall/org_1", json={"question": "q", **body})


def test_private_lane_requires_member_and_matching_person_audience(client, recall_page):
    person = {"kind": "person", "userId": "user_sam"}
    assert _recall(client, lane="private", audience=person).status_code == 422  # no member
    assert _recall(client, lane="private", member="user_sam").status_code == 422  # no audience
    assert (
        _recall(client, lane="private", member="user_sam", audience={"kind": "room", "roomId": "r1"}).status_code == 422
    )
    other = {"kind": "person", "userId": "user_priya"}
    assert _recall(client, lane="private", member="user_sam", audience=other).status_code == 422
    assert _recall(client, lane="shared", audience=other).status_code == 422  # a person audience is the member
    assert recall_page.await_count == 0

    assert _recall(client, lane="private", member="user_sam", audience=person).status_code == 200
    assert recall_page.await_args.kwargs["audience"] == person
    assert recall_page.await_args.args == ("org_1", "user_sam")


def test_shared_room_audience_and_legacy_body(client, recall_page):
    assert _recall(client, lane="shared", audience={"kind": "room", "roomId": "r1"}).status_code == 200
    assert recall_page.await_args.kwargs["audience"] == {"kind": "room", "roomId": "r1"}
    assert _recall(client).status_code == 200  # the plugin today: no lane, no audience
    assert recall_page.await_args.kwargs["audience"] is None
    assert _recall(client, audience={"kind": "room"}).status_code == 422


def test_link_route_404_when_not_visible(client):
    body = {"id": "t01", "audience": {"kind": "room", "roomId": "r1"}}
    with (
        patch.object(memory_links, "read_link", AsyncMock(return_value=None)) as read,
    ):
        assert client.post("/api/v1/internal/memory/link/org_1", json=body).status_code == 404
        read.assert_awaited_once_with("org_1", "t01", {"kind": "room", "roomId": "r1"})
        read.return_value = {"task": {"taskId": "t01"}, "threads": [], "lastResult": None}
        r = client.post("/api/v1/internal/memory/link/org_1", json=body)
        assert r.status_code == 200 and r.json()["task"]["taskId"] == "t01"
