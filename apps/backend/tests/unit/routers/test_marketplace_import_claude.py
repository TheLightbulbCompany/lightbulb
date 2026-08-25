"""HTTP contract for the claude branch of the import door: detection routing,
the claude preview shape (snippet-stripped flags), and error cleanup. The
normalizer itself is covered in tests/unit/services/test_claude_code_import.py."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import get_current_user  # noqa: E402
from core.containers import workspace as workspace_module  # noqa: E402
from routers import marketplace_import  # noqa: E402
from tests.unit.routers.test_marketplace_import import KEY, OWNER, FakeS3, _auth  # noqa: E402
from tests.unit.services.test_claude_code_import import CLAUDE_HOME, SLUG, _tar  # noqa: E402


@pytest.fixture
def client(tmp_path: Path):
    app = FastAPI()
    app.include_router(marketplace_import.router)
    app.dependency_overrides[get_current_user] = _auth
    fake_ws = workspace_module.Workspace(mount_path=str(tmp_path / "users"))
    s3 = FakeS3({KEY: _tar(CLAUDE_HOME)})
    with (
        patch("routers.marketplace_import.get_workspace", return_value=fake_ws),
        patch("core.services.agent_export.get_workspace", return_value=fake_ws),
        patch("routers.marketplace_import._s3", return_value=s3),
        patch("routers.marketplace_import.settings") as settings,
        patch("routers.marketplace_import.container_repo.get_by_owner_id", new_callable=AsyncMock) as get_row,
        patch("routers.marketplace_import.count_paperclip_agents_for_owner", new_callable=AsyncMock, return_value=2),
        patch(
            "routers.marketplace_import.billing_repo.get_by_owner_id",
            new_callable=AsyncMock,
            return_value={"plan": "flat"},
        ),
    ):
        settings.MARKETPLACE_ARTIFACTS_BUCKET = "bkt"
        get_row.return_value = {"owner_id": OWNER, "gateway_token": "gw"}
        yield TestClient(app), tmp_path / "users" / OWNER, s3


def test_import_claude_context_returns_claude_preview(client):
    c, user_root, s3 = client
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["import_id"].startswith("import-") and body["kind"] == "claude"
    assert body["agent_id"] == "" and body["name"] == "Claude Code context"
    assert body["agents"] == [] and body["plugins"] == []
    claude = body["claude"]
    assert [col["file_count"] for col in claude["collections"]] == [2]
    assert claude["skills"] == ["graphify"]
    assert claude["has_user_memory"] is True
    assert claude["skipped"]["credentials"] == 2
    staging = user_root / f"workspace-{body['import_id']}"
    assert (staging / "claude").is_dir() and (staging / "import-manifest.json").is_file()
    assert s3.deleted == [KEY]  # raw upload consumed


def test_import_claude_safety_flags_are_snippet_stripped(client):
    c, _, s3 = client
    s3.objects[KEY] = _tar({**CLAUDE_HOME, f".claude/projects/{SLUG}/memory/leak.md": b"key AKIAIOSFODNN7EXAMPLE\n"})
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 200, r.text
    flags = r.json()["claude"]["safety_flags"]
    assert flags
    assert all(not f.get("snippet") for f in flags)
    # The flagged source line must never be echoed anywhere in the response.
    assert "AKIAIOSFODNN7EXAMPLE" not in r.text


def test_import_claude_traversal_400_cleans_up(client):
    c, user_root, s3 = client
    s3.objects[KEY] = _tar({**CLAUDE_HOME, ".claude/projects/x/memory/../../../evil.md": b"evil\n"})
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 400, r.text
    assert "escapes the extraction directory" in r.json()["detail"]
    assert s3.deleted == [KEY]  # raw upload consumed even on failure
    assert not list(user_root.glob("workspace-import-*")) if user_root.exists() else True
