"""HTTP contract for the claude branch of the import door: detection routing,
the claude preview shape (snippet-stripped flags), and error cleanup. The
normalizer itself is covered in tests/unit/services/test_claude_code_import.py."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import get_current_user  # noqa: E402
from core.containers import workspace as workspace_module  # noqa: E402
from routers import marketplace_import  # noqa: E402
from tests.unit.routers.test_marketplace_import import KEY, OWNER, FakeS3, _auth  # noqa: E402
from tests.unit.services.test_claude_code_import import CID, CLAUDE_HOME, SLUG, _tar  # noqa: E402


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


def test_import_corrupt_archive_400_cleans_up(client):
    # Non-gzip bytes die in the names-only detection pass, before either
    # extractor runs; the router must still 400, leave no staging behind, and
    # delete the raw object (it can hold credentials — nothing retries it).
    c, user_root, s3 = client
    s3.objects[KEY] = b"this is not a gzip stream"
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 400, r.text
    assert "not a valid .tar.gz" in r.json()["detail"]
    assert s3.deleted == [KEY]
    assert not list(user_root.glob("workspace-import-*")) if user_root.exists() else True


def test_import_names_pass_bounds_declared_bytes(client, monkeypatch):
    # The names-only pass must refuse an archive whose declared member sizes
    # cross the extraction cap — MAX_MEMBERS bounds header count, not the
    # decompressed bytes the stream forces through.
    c, user_root, s3 = client
    monkeypatch.setattr(marketplace_import.backup_import, "MAX_EXTRACTED_BYTES", 1)
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 400, r.text
    assert "extraction cap" in r.json()["detail"]
    assert s3.deleted == [KEY]
    assert not list(user_root.glob("workspace-import-*")) if user_root.exists() else True


AGENT = "agent1"


def _start(c) -> str:
    return c.post("/api/v1/marketplace/import", json={"key": KEY}).json()["import_id"]


def _mock_ecs() -> MagicMock:
    ecs = MagicMock()
    ecs.force_redeploy_user_service = AsyncMock()
    return ecs


def test_confirm_applies_to_target_agent_and_bounces(client):
    c, user_root, _ = client
    ws = user_root / f"workspace-{AGENT}"
    ws.mkdir(parents=True)
    import_id = _start(c)
    ecs = _mock_ecs()
    with patch("routers.marketplace_import.get_ecs_manager", return_value=ecs):
        r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={"target_agent_id": AGENT})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agents"] == [] and body["plugins_installed"] == []
    assert {i["item"]: i["status"] for i in body["claude_items"]} == {
        f"memory:{CID}": "applied",
        "user-memory": "applied",
        "skill:graphify": "applied",
    }
    assert body["restarting"] is True
    # Memory lands in upstream's own migrate-claude namespace.
    assert (ws / f"memory/imports/claude-code/{CID}/fact.md").read_text() == "a fact\n"
    assert (ws / "skills/graphify/SKILL.md").exists()
    user_md = (ws / "USER.md").read_text()
    assert "claude-code-import:begin" in user_md and "# me" in user_md
    assert not (user_root / f"workspace-{import_id}").exists()
    ecs.force_redeploy_user_service.assert_awaited_once_with(OWNER)


def test_confirm_without_target_agent_400(client):
    c, _, _ = client
    import_id = _start(c)
    r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={})
    assert r.status_code == 400
    assert "target_agent_id" in r.json()["detail"]


def test_confirm_own_import_id_target_400_keeps_staging(client):
    # Passing the import's own id would resolve to the staging dir itself —
    # the apply would copy staging onto itself and then rmtree it, a
    # misleading 200. Must 400 before resolving, staging intact for retry.
    c, user_root, _ = client
    import_id = _start(c)
    r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={"target_agent_id": import_id})
    assert r.status_code == 400, r.text
    assert "import id" in r.json()["detail"]
    assert (user_root / f"workspace-{import_id}" / "claude").is_dir()


def test_confirm_invalid_agent_id_400(client):
    # "a" fails the agent-id regex (3-64 chars) — InvalidAgentIdError → 400.
    c, _, _ = client
    import_id = _start(c)
    r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={"target_agent_id": "a"})
    assert r.status_code == 400, r.text


def test_confirm_nonexistent_agent_404(client):
    # A regex-valid id that resolves to no workspace dir must 404 — the
    # resolver returns a clean miss unchanged, so the router's own is_dir
    # check is what stops a ghost workspace dir being minted on EFS.
    c, user_root, _ = client
    import_id = _start(c)
    r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={"target_agent_id": "agent-nope"})
    assert r.status_code == 404, r.text
    assert not (user_root / "workspace-agent-nope").exists()


def test_confirm_unknown_collection_400_keeps_staging(client):
    c, user_root, _ = client
    (user_root / f"workspace-{AGENT}").mkdir(parents=True)
    import_id = _start(c)
    r = c.post(
        f"/api/v1/marketplace/import/{import_id}/confirm",
        json={"target_agent_id": AGENT, "claude_collections": ["nope-0000000000"]},
    )
    assert r.status_code == 400, r.text
    assert "nope-0000000000" in r.json()["detail"]
    # staging kept so a corrected selection can retry
    assert (user_root / f"workspace-{import_id}" / "claude").is_dir()


def test_confirm_selection_skips_bounce_when_only_user_memory_applies(client):
    c, user_root, _ = client
    ws = user_root / f"workspace-{AGENT}"
    ws.mkdir(parents=True)
    import_id = _start(c)
    ecs = _mock_ecs()
    with patch("routers.marketplace_import.get_ecs_manager", return_value=ecs):
        r = c.post(
            f"/api/v1/marketplace/import/{import_id}/confirm",
            json={"target_agent_id": AGENT, "claude_collections": [], "claude_skills": []},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["item"] for i in body["claude_items"]] == ["user-memory"]
    assert body["restarting"] is False
    assert not (ws / "memory").exists() and not (ws / "skills").exists()
    ecs.force_redeploy_user_service.assert_not_awaited()


def test_confirm_second_confirm_404(client):
    c, user_root, _ = client
    (user_root / f"workspace-{AGENT}").mkdir(parents=True)
    import_id = _start(c)
    with patch("routers.marketplace_import.get_ecs_manager", return_value=_mock_ecs()):
        assert (
            c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={"target_agent_id": AGENT}).status_code
            == 200
        )
    r = c.post(f"/api/v1/marketplace/import/{import_id}/confirm", json={"target_agent_id": AGENT})
    assert r.status_code == 404
