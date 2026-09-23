"""HTTP contract for the Claude Code import door: detection routing, the preview shape
(snippet-stripped flags), the profile apply, and error cleanup.

The normalizer itself is covered in tests/unit/services/test_claude_code_import.py.

WHAT CHANGED WITH THE RUNTIME. The staging used to be an EFS dir the preview wrote and the
confirm read back; there is no shared mount now, so the UPLOADED OBJECT is the staging and the
confirm re-streams it (see the router's module docstring). The apply target used to be
``workspace-{agent}/`` on EFS and is now the agent's S3 Hermes profile. Both are asserted
below: the object's lifecycle across the two calls, and what lands under the profile prefix.
The old OpenClaw-backup half of this router is gone, and its ~20 tests with it.
"""

import io
import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.auth import AuthContext, get_current_user  # noqa: E402
from routers import marketplace_import  # noqa: E402
from tests.unit.services.test_claude_code_import import CID, CLAUDE_HOME, SLUG, _tar  # noqa: E402

OWNER = "user_owner_1"
IMPORT_ID = "import-0123456789ab"
KEY = f"imports/{OWNER}/{IMPORT_ID}.tar.gz"
AGENT = "agent1"


def _auth() -> AuthContext:
    return AuthContext(user_id="user_importer", email="o@example.com", org_id=OWNER, org_role="org:admin")


class FakeS3:
    """Serves the upload AND stands in for the profile store the apply writes into."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = dict(objects)
        self.deleted: list[str] = []
        self.presigned: list[dict] = []
        self.etag = '"etag-1"'  # override in a test to simulate the object changing mid-import

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigned.append({"op": op, **Params, "ExpiresIn": ExpiresIn})
        return f"https://s3.example/{Params['Key']}?sig=1"

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise marketplace_import.ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key]), "ETag": self.etag}

    def get_object(self, Bucket, Key, IfMatch=None):
        if IfMatch is not None and IfMatch != self.etag:
            raise marketplace_import.ClientError({"Error": {"Code": "PreconditionFailed"}}, "GetObject")
        if Key not in self.objects:
            raise marketplace_import.ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)


class FakeProfile:
    """The one thing the apply half needs from hermes_profiles: fetch the agent's current
    ``skills/`` down, take back what changed -- plus the context paste door's merge, which
    owns ``memories/USER.md`` (its own tests cover the merge itself)."""

    def __init__(self, existing: dict[str, bytes] | None = None):
        self.stored: dict[str, bytes] = dict(existing or {})
        self.writes: list[dict[str, bytes]] = []
        self.merged: list[str] = []

    def merge_context(self, owner_id, agent_id, text):
        assert (owner_id, agent_id) == (OWNER, AGENT)
        if not marketplace_import.context_import.extract_entries(text):
            raise ValueError("nothing to import")
        self.merged.append(text)
        return {"merged": len(text), "overflowed": False}

    def fetch_profile_subtree(self, owner_id, agent_id, prefixes, dest):
        assert (owner_id, agent_id) == (OWNER, AGENT)
        for rel, body in self.stored.items():
            if not any(rel == p or rel.startswith(p) for p in prefixes):
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)

    def write_profile_subtree(self, owner_id, agent_id, files, removed):
        assert (owner_id, agent_id) == (OWNER, AGENT)
        self.writes.append(dict(files))
        self.stored.update(files)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(marketplace_import.router)
    app.dependency_overrides[get_current_user] = _auth
    s3 = FakeS3({KEY: _tar(CLAUDE_HOME)})
    profile = FakeProfile()
    with (
        patch("routers.marketplace_import._s3", return_value=s3),
        patch("routers.marketplace_import.settings") as settings,
        patch("routers.marketplace_import.container_repo.get_by_owner_id", new_callable=AsyncMock) as get_row,
        patch("routers.marketplace_import.hermes_profiles.fetch_profile_subtree", profile.fetch_profile_subtree),
        patch("routers.marketplace_import.hermes_profiles.write_profile_subtree", profile.write_profile_subtree),
        patch("routers.marketplace_import.context_import.merge_context", profile.merge_context),
    ):
        settings.MARKETPLACE_ARTIFACTS_BUCKET = "bkt"
        get_row.return_value = {"owner_id": OWNER, "agentcore_runtime_arn": "arn:x"}
        yield TestClient(app), s3, profile


# ---------------------------------------------------------------------------
# presign
# ---------------------------------------------------------------------------


def test_presign_mints_the_import_id_into_the_key(client):
    """The confirm addresses the staged object by import id, so the id has to BE the key —
    otherwise the confirm would need a listing or a server-side copy to find it."""
    c, s3, _ = client
    r = c.post("/api/v1/marketplace/import/presign", json={"filename": "ctx.tgz", "size_bytes": 1024})
    assert r.status_code == 200, r.text
    key = r.json()["key"]
    assert marketplace_import._KEY_RE.match(key)
    owner, import_id = marketplace_import._KEY_RE.match(key).groups()
    assert owner == OWNER
    assert marketplace_import._upload_key(owner, import_id) == key
    assert s3.presigned[0]["op"] == "put_object"


def test_presign_refuses_an_oversized_archive(client):
    c, _, _ = client
    over = marketplace_import.claude_code_import.MAX_UPLOAD_BYTES + 1
    r = c.post("/api/v1/marketplace/import/presign", json={"filename": "ctx.tgz", "size_bytes": over})
    assert r.status_code == 413


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------


def test_import_claude_context_returns_claude_preview_and_keeps_the_object(client):
    c, s3, _ = client
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["import_id"] == IMPORT_ID and body["name"] == "Claude Code context"
    # ONE shape. The OpenClaw-backup half this response used to also carry (`kind`, the
    # agent/plugin/MCP/skill candidate lists, the file and cron counts) defaulted to empty on
    # every Claude import, and the client branched on `kind` to know which half was real.
    assert set(body) == {"import_id", "name", "claude"}
    claude = body["claude"]
    assert [col["file_count"] for col in claude["collections"]] == [2]
    assert claude["skills"] == ["graphify"]
    assert claude["has_user_memory"] is True
    assert claude["skipped"]["credentials"] == 2
    # THE object is the staging: a preview that deleted it would strand the confirm, which
    # may land on a different backend task.
    assert s3.deleted == []
    assert KEY in s3.objects


def test_import_claude_safety_flags_are_snippet_stripped(client):
    c, s3, _ = client
    s3.objects[KEY] = _tar({**CLAUDE_HOME, f".claude/projects/{SLUG}/memory/leak.md": b"key AKIAIOSFODNN7EXAMPLE\n"})
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 200, r.text
    flags = r.json()["claude"]["safety_flags"]
    assert flags
    assert all(not f.get("snippet") for f in flags)
    # The flagged source line must never be echoed anywhere in the response.
    assert "AKIAIOSFODNN7EXAMPLE" not in r.text


def test_a_key_for_another_owner_is_refused(client):
    c, _, _ = client
    r = c.post("/api/v1/marketplace/import", json={"key": f"imports/user_someone_else/{IMPORT_ID}.tar.gz"})
    assert r.status_code == 400


def test_import_traversal_400_drops_the_object(client):
    c, s3, _ = client
    s3.objects[KEY] = _tar({**CLAUDE_HOME, ".claude/projects/x/memory/../../../evil.md": b"evil\n"})
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 400, r.text
    assert "escapes the extraction directory" in r.json()["detail"]
    # A bad archive is dropped NOW rather than waiting on the bucket lifecycle: no retry
    # reuses it, and it can hold credentials.
    assert s3.deleted == [KEY]


def test_import_corrupt_archive_400_drops_the_object(client):
    c, s3, _ = client
    s3.objects[KEY] = b"this is not a gzip stream"
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 400, r.text
    assert "not a valid .tar.gz" in r.json()["detail"]
    assert s3.deleted == [KEY]


def test_a_non_claude_archive_is_refused(client):
    """The OpenClaw-backup rail this used to fall through to is gone, so an archive that is
    not a Claude home must be refused with an actionable message, not silently accepted."""
    c, s3, _ = client
    s3.objects[KEY] = _tar({"AGENTS.md": b"# an agent workspace\n", "skills/x/SKILL.md": b"# x\n"})
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 400, r.text
    assert "Claude Code context" in r.json()["detail"]
    assert s3.deleted == [KEY]


def test_names_pass_bounds_declared_bytes(client, monkeypatch):
    """MAX_MEMBERS bounds the header count, not the decompressed bytes the detection pass
    forces through — one member with a huge declared size would otherwise be unbounded."""
    c, s3, _ = client
    monkeypatch.setattr(marketplace_import.claude_code_import, "CLAUDE_MAX_TOTAL_BYTES", 1)
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 400, r.text
    assert "extraction cap" in r.json()["detail"]
    assert s3.deleted == [KEY]


def test_an_object_changing_between_the_head_and_the_get_is_a_409(client):
    c, s3, _ = client

    real_get = s3.get_object

    def _changed(Bucket, Key, IfMatch=None):
        s3.etag = '"etag-2"'  # a re-upload landed
        return real_get(Bucket, Key, IfMatch=IfMatch)

    s3.get_object = _changed
    # First GET succeeds (the pin still matches), the second sees the new ETag.
    r = c.post("/api/v1/marketplace/import", json={"key": KEY})
    assert r.status_code == 409, r.text
    # KEPT on purpose: this flow tells the client to re-upload and retry the same key.
    assert s3.deleted == []


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------


def test_confirm_applies_into_the_agents_profile(client):
    c, s3, profile = client
    assert c.post("/api/v1/marketplace/import", json={"key": KEY}).status_code == 200

    r = c.post(f"/api/v1/marketplace/import/{IMPORT_ID}/confirm", json={"target_agent_id": AGENT})

    assert r.status_code == 200, r.text
    body = r.json()
    # Nothing is minted, so there is no per-agent result list -- and the profile write IS the
    # delivery (.revision bumped by write_profile_subtree), so there is no restart to report.
    assert set(body) == {"claude_items", "warnings"}
    assert {i["item"]: i["status"] for i in body["claude_items"]} == {
        f"memory:{CID}": "applied",
        "user-memory": "applied",
        "skill:graphify": "applied",
    }

    written = {k: v for w in profile.writes for k, v in w.items()}
    # Memory lands in upstream's own migrate-claude namespace, so a user who ran the local
    # `openclaw migrate claude` AND this import gets ONE tree, not two.
    assert written[f"memory/imports/claude-code/{CID}/fact.md"] == b"a fact\n"
    assert "skills/graphify/SKILL.md" in written
    # CLAUDE.md goes through the paste door's merge into memories/USER.md -- never the
    # profile-root USER.md, which Hermes does not read.
    assert "USER.md" not in written
    assert len(profile.merged) == 1 and "prefers tabs" in profile.merged[0]
    # Object consumed, so a second confirm cannot re-apply.
    assert s3.deleted == [KEY]


def test_confirm_with_a_heading_only_claude_md_reports_no_user_memory(client):
    """A CLAUDE.md with no entries has nothing to merge -- still a 200, not a 500 from an
    unschema'd status after the upload is already deleted."""
    c, s3, profile = client
    s3.objects[KEY] = _tar({**CLAUDE_HOME, ".claude/CLAUDE.md": b"# me\n"})
    assert c.post("/api/v1/marketplace/import", json={"key": KEY}).status_code == 200

    r = c.post(f"/api/v1/marketplace/import/{IMPORT_ID}/confirm", json={"target_agent_id": AGENT})

    assert r.status_code == 200, r.text
    assert "user-memory" not in {i["item"] for i in r.json()["claude_items"]}
    assert profile.merged == []


def test_confirm_never_overwrites_an_existing_skill(client):
    c, _, profile = client
    profile.stored["skills/graphify/SKILL.md"] = b"# the owner's own graphify\n"
    assert c.post("/api/v1/marketplace/import", json={"key": KEY}).status_code == 200

    r = c.post(f"/api/v1/marketplace/import/{IMPORT_ID}/confirm", json={"target_agent_id": AGENT})

    assert r.status_code == 200, r.text
    items = {i["item"]: i["status"] for i in r.json()["claude_items"]}
    assert items["skill:graphify"] == "skipped_exists"
    assert profile.stored["skills/graphify/SKILL.md"] == b"# the owner's own graphify\n"


def test_confirm_writes_only_what_changed(client):
    """The whole fetched subtree is in the scratch tree, so uploading it wholesale would
    rewrite (and bump .revision over) files the import never touched."""
    c, _, profile = client
    profile.stored["skills/untouched/SKILL.md"] = b"# untouched\n"
    assert c.post("/api/v1/marketplace/import", json={"key": KEY}).status_code == 200

    c.post(f"/api/v1/marketplace/import/{IMPORT_ID}/confirm", json={"target_agent_id": AGENT})

    written = {k: v for w in profile.writes for k, v in w.items()}
    assert "skills/untouched/SKILL.md" not in written


def test_confirm_without_target_agent_400(client):
    c, _, _ = client
    assert c.post("/api/v1/marketplace/import", json={"key": KEY}).status_code == 200
    r = c.post(f"/api/v1/marketplace/import/{IMPORT_ID}/confirm", json={})
    assert r.status_code == 400
    assert "target_agent_id" in r.json()["detail"]


def test_confirm_own_import_id_as_the_target_400_keeps_the_object(client):
    """An import id is not a roster agent; applying "onto" one would be a 200 that changes
    nothing any agent reads."""
    c, s3, _ = client
    assert c.post("/api/v1/marketplace/import", json={"key": KEY}).status_code == 200
    r = c.post(f"/api/v1/marketplace/import/{IMPORT_ID}/confirm", json={"target_agent_id": IMPORT_ID})
    assert r.status_code == 400, r.text
    assert "import id" in r.json()["detail"]
    assert s3.deleted == []


def test_confirm_invalid_agent_id_400(client):
    # "a" fails the agent-id regex (3-64 chars) — InvalidAgentIdError → 400.
    c, _, _ = client
    assert c.post("/api/v1/marketplace/import", json={"key": KEY}).status_code == 200
    r = c.post(f"/api/v1/marketplace/import/{IMPORT_ID}/confirm", json={"target_agent_id": "a"})
    assert r.status_code == 400, r.text


def test_confirm_unknown_collection_400_before_applying_anything(client):
    c, s3, profile = client
    assert c.post("/api/v1/marketplace/import", json={"key": KEY}).status_code == 200
    r = c.post(
        f"/api/v1/marketplace/import/{IMPORT_ID}/confirm",
        json={"target_agent_id": AGENT, "claude_collections": ["nope-0000000000"]},
    )
    assert r.status_code == 400, r.text
    assert "nope-0000000000" in r.json()["detail"]
    # Nothing applied and the object kept, so a corrected selection can retry.
    assert profile.writes == []
    assert s3.deleted == []


def test_confirm_honours_an_empty_selection(client):
    c, _, profile = client
    assert c.post("/api/v1/marketplace/import", json={"key": KEY}).status_code == 200
    r = c.post(
        f"/api/v1/marketplace/import/{IMPORT_ID}/confirm",
        json={"target_agent_id": AGENT, "claude_collections": [], "claude_skills": []},
    )
    assert r.status_code == 200, r.text
    assert [i["item"] for i in r.json()["claude_items"]] == ["user-memory"]
    assert profile.writes == []
    assert len(profile.merged) == 1


def test_a_second_confirm_404s(client):
    c, _, _ = client
    assert c.post("/api/v1/marketplace/import", json={"key": KEY}).status_code == 200
    assert c.post(f"/api/v1/marketplace/import/{IMPORT_ID}/confirm", json={"target_agent_id": AGENT}).status_code == 200
    r = c.post(f"/api/v1/marketplace/import/{IMPORT_ID}/confirm", json={"target_agent_id": AGENT})
    assert r.status_code == 404


def test_confirm_before_any_upload_404s(client):
    c, s3, _ = client
    s3.objects.clear()
    r = c.post(f"/api/v1/marketplace/import/{IMPORT_ID}/confirm", json={"target_agent_id": AGENT})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# discard
# ---------------------------------------------------------------------------


def test_discard_deletes_the_staged_object(client):
    c, s3, _ = client
    assert c.post("/api/v1/marketplace/import", json={"key": KEY}).status_code == 200
    r = c.delete(f"/api/v1/marketplace/import/{IMPORT_ID}")
    assert r.status_code == 204
    assert s3.deleted == [KEY]


def test_discard_refuses_a_malformed_import_id(client):
    """The id is interpolated into an S3 key, so it is validated rather than trusted."""
    c, s3, _ = client
    r = c.delete("/api/v1/marketplace/import/..%2F..%2Fother-owner")
    assert r.status_code in (400, 404)
    assert s3.deleted == []


def test_discard_is_idempotent(client):
    c, _, _ = client
    assert c.delete(f"/api/v1/marketplace/import/{IMPORT_ID}").status_code == 204
    assert c.delete(f"/api/v1/marketplace/import/{IMPORT_ID}").status_code == 204
