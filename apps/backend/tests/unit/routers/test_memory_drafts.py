"""/api/v1/memory/drafts: the evaluator's proposed lessons become memory entries when a person approves."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.auth import AuthContext  # noqa: E402
from core.services import evaluator, memory_api  # noqa: E402
from main import app  # noqa: E402
from routers.sessions import caller  # noqa: E402


def _draft(**kw):
    return {"id": "d1", "user_id": "user_ana", "kind": "lesson", "text": "Run migrations first",
            "evidence": ["s1", "s2"], "status": "proposed", "workspace_id": "org_a", **kw}  # fmt: skip


@pytest.fixture
def table():
    """A fake store holding one workspace draft in both partitions."""
    t = MagicMock()
    rows = {
        ("WDRAFTS#org_a", "D#user_ana#d1"): _draft(),
        ("DRAFTS#user_ana", "D#d1"): _draft(),
        ("SESSIONS#user_ana", "S#s1"): {"id": "s1", "repo": "acme/api", "harness": "claude"},
        ("DRAFTS#user_solo", "D#d9"): _draft(id="d9", user_id="user_solo", workspace_id=None),
    }

    def update(Key, ExpressionAttributeValues, **_):
        if (
            ":from" in ExpressionAttributeValues
            and rows[(Key["pk"], Key["sk"])]["status"] != ExpressionAttributeValues[":from"]
        ):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        rows[(Key["pk"], Key["sk"])]["status"] = ExpressionAttributeValues[":s"]

    t.update_item.side_effect = update

    async def query(pk, begins_with=None):
        return [{"pk": k[0], "sk": k[1], **v} for k, v in rows.items() if k[0] == pk]

    async def get(pk, sk):
        return {"pk": pk, "sk": sk, **rows[(pk, sk)]} if (pk, sk) in rows else None

    with (
        patch.object(evaluator.store, "_table", return_value=t),
        patch.object(evaluator.store, "_query", query),
        patch.object(evaluator.store, "_get", get),
    ):
        t.rows = rows
        yield t


def _as(user_id, org_id):
    app.dependency_overrides[caller] = lambda: AuthContext(user_id=user_id, org_id=org_id, org_role="org:member")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear():
    yield
    app.dependency_overrides.clear()


def test_approve_in_workspace_signs_as_workspace_with_approver(table):
    c = _as("user_bo", "org_a")
    call = AsyncMock(return_value={"stored": True, "entry_id": "e1", "neighbours": []})
    with (
        patch.object(memory_api, "neighbours", AsyncMock(return_value=[])),
        patch.object(memory_api.network_member, "call_op", call),
        patch.object(memory_api.network_member, "queue_op", AsyncMock()),
    ):
        r = c.post("/api/v1/memory/drafts/d1/approve", json={})
    assert r.status_code == 200 and r.json() == {"queued": False}
    owner, op, params = call.await_args.args
    assert (owner, op) == ("org_a", "memory.remember")
    assert params["kind"] == "lesson" and params["scope"] == "team" and params["on_behalf_of"] == "user_bo"
    assert params["fact"] == "Run migrations first" and params["subject"] == "acme/api"
    assert table.rows[("WDRAFTS#org_a", "D#user_ana#d1")]["status"] == "approved"
    assert table.rows[("DRAFTS#user_ana", "D#d1")]["status"] == "approved"
    assert c.get("/api/v1/memory/drafts").json() == {"drafts": []}


def test_approve_in_workspace_failure_reverts_both_rows_to_proposed(table):
    c = _as("user_bo", "org_a")
    with patch.object(memory_api, "remember", AsyncMock(side_effect=RuntimeError("down"))):
        assert c.post("/api/v1/memory/drafts/d1/approve", json={}).status_code == 502
    assert table.rows[("WDRAFTS#org_a", "D#user_ana#d1")]["status"] == "proposed"
    assert table.rows[("DRAFTS#user_ana", "D#d1")]["status"] == "proposed"


def test_approve_refused_by_the_member_reverts_the_draft_to_proposed(table):
    c = _as("user_bo", "org_a")
    refused = {"stored": False, "refused": "secret:github_token", "neighbours": []}
    with (
        patch.object(memory_api, "neighbours", AsyncMock(return_value=[])),
        patch.object(memory_api.network_member, "call_op", AsyncMock(return_value=refused)),
    ):
        r = c.post("/api/v1/memory/drafts/d1/approve", json={})
    assert r.status_code == 422 and "secret:github_token" in r.json()["detail"]
    assert table.rows[("WDRAFTS#org_a", "D#user_ana#d1")]["status"] == "proposed"
    assert table.rows[("DRAFTS#user_ana", "D#d1")]["status"] == "proposed"


def test_approve_races_to_409_and_remembers_once(table):
    c = _as("user_bo", "org_a")
    rem = AsyncMock(return_value={"queued": False, "neighbours": []})
    with patch.object(memory_api, "remember", rem):
        table.rows[("DRAFTS#user_ana", "D#d1")]["status"] = "approved"  # someone else won between read and write
        # the mirror still says proposed, so the draft is found; the conditional flip on the author's row loses
        assert c.post("/api/v1/memory/drafts/d1/approve", json={}).status_code == 409
    rem.assert_not_awaited()


def test_approve_solo_returns_entry_for_local_store(table):
    c = _as("user_solo", None)
    r = c.post("/api/v1/memory/drafts/d9/approve", json={})
    assert r.status_code == 200
    assert r.json() == {
        "subject": "lesson", "fact": "Run migrations first", "kind": "lesson", "scope": "me",
    }  # fmt: skip
    assert table.rows[("DRAFTS#user_solo", "D#d9")]["status"] == "approved"


def test_dismiss_marks_row(table):
    c = _as("user_bo", "org_a")
    assert c.post("/api/v1/memory/drafts/d1/dismiss").status_code == 204
    assert table.rows[("WDRAFTS#org_a", "D#user_ana#d1")]["status"] == "dismissed"
    assert table.rows[("DRAFTS#user_ana", "D#d1")]["status"] == "dismissed"
    assert c.post("/api/v1/memory/drafts/d1/dismiss").status_code == 404


def test_other_callers_draft_is_404(table):
    assert _as("user_solo", None).post("/api/v1/memory/drafts/d1/approve", json={}).status_code == 404
    assert _as("user_x", "org_b").post("/api/v1/memory/drafts/d1/dismiss").status_code == 404
    assert _as("user_x", "org_b").get("/api/v1/memory/drafts").json() == {"drafts": []}
    assert table.rows[("WDRAFTS#org_a", "D#user_ana#d1")]["status"] == "proposed"


def test_a_finding_cannot_be_approved(table):
    table.rows[("WDRAFTS#org_a", "D#user_ana#d1")]["kind"] = "finding"
    assert _as("user_bo", "org_a").post("/api/v1/memory/drafts/d1/approve", json={}).status_code == 422


def test_mirror_out_of_step_is_forced_to_the_author_rows_status(table):
    table.rows[("WDRAFTS#org_a", "D#user_ana#d1")]["status"] = "dismissed"  # the mirror drifted
    d = _draft()
    assert asyncio.run(evaluator.set_draft_status(d, "approved")) is True
    assert table.rows[("DRAFTS#user_ana", "D#d1")]["status"] == "approved"
    assert table.rows[("WDRAFTS#org_a", "D#user_ana#d1")]["status"] == "approved"
