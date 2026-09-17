"""The S3 side of the owner-facing file routes on an AgentCore row (Task 10-B, M6-R48).

``routers/workspace_files.py`` branches onto these helpers so the Files panel and the Instructions
tab operate on the agent's live Hermes profile instead of the EFS tree the migration left behind.
"""

from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from core.config import settings
from core.services import hermes_profiles

OWNER = "org_1"
AGENT = "agent_1"
PREFIX = f"{OWNER}/hermes/{AGENT}/"


@pytest.fixture()
def s3_hermes_bucket():
    """Moto-backed S3 wired in as settings.OWNER_STATE_BUCKET -- the profile writer's single
    source of truth for this bucket. The module-level client cache is dropped either side so the
    real (unmocked) client never leaks into moto's world or back out of it."""
    hermes_profiles._SHARED_S3_CLIENT = None
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-replicas")
        with patch.object(settings, "OWNER_STATE_BUCKET", "test-replicas"):
            yield client
    hermes_profiles._SHARED_S3_CLIENT = None


def _put(s3, key: str, body: bytes = b"x") -> None:
    s3.put_object(Bucket="test-replicas", Key=f"{PREFIX}{key}", Body=body)


# ---------------------------------------------------------------------------
# normalize_profile_rel / is_hidden_profile_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "/abs.md", "../escape.md", "a/../../b", ".", "./"])
def test_normalize_rejects_absolute_dot_only_and_traversal(bad):
    with pytest.raises(ValueError):
        hermes_profiles.normalize_profile_rel(bad)


def test_normalize_collapses_dot_segments_so_they_cannot_dodge_the_backend_owned_guard():
    # On S3 './SOUL.md' would be a different KEY from 'SOUL.md' and slip past the guard.
    assert hermes_profiles.normalize_profile_rel("./SOUL.md") == "SOUL.md"
    assert hermes_profiles.normalize_profile_rel("memories//USER.md") == "memories/USER.md"


@pytest.mark.parametrize(
    "rel", [".env", ".revision", "mcp-tokens/github.json", "skills/.secret", "lightbulb-facts.json"]
)
def test_hidden_paths_cover_the_secret_store_the_token_files_and_the_facts_file(rel):
    """Fix round 1, I1: the facts file is backend-owned (plan, subscription, provider, agent
    e-mail) and is not a dotfile, so it needs its own row in the hidden set."""
    assert hermes_profiles.is_hidden_profile_path(rel)


@pytest.mark.parametrize("rel", ["SOUL.md", "config.yaml", "memories/USER.md", "skills/foo/SKILL.md"])
def test_ordinary_profile_paths_are_not_hidden(rel):
    assert not hermes_profiles.is_hidden_profile_path(rel)


# ---------------------------------------------------------------------------
# list_profile_dir
# ---------------------------------------------------------------------------


def test_list_profile_dir_never_lists_the_env_file_the_facts_file_or_mcp_tokens(s3_hermes_bucket):
    _put(s3_hermes_bucket, "SOUL.md", b"# Ada\n")
    _put(s3_hermes_bucket, "config.yaml", b"model: {}\n")
    _put(s3_hermes_bucket, ".env", b"ANTHROPIC_API_KEY=sk-live\n")
    _put(s3_hermes_bucket, ".revision", b"deadbeef")
    _put(s3_hermes_bucket, "lightbulb-facts.json", b'{"plan": "ultra_pro"}')
    _put(s3_hermes_bucket, "mcp-tokens/github.json", b"{}")
    _put(s3_hermes_bucket, "memories/USER.md", b"# User\n")

    entries = hermes_profiles.list_profile_dir(OWNER, AGENT)

    assert [e["path"] for e in entries] == ["memories", "SOUL.md", "config.yaml"]
    assert entries[0]["type"] == "dir" and entries[0]["size"] is None
    assert entries[1]["size"] == len(b"# Ada\n")
    assert entries[1]["modified_at"] > 0


def test_list_profile_dir_descends_one_level_only(s3_hermes_bucket):
    _put(s3_hermes_bucket, "memories/USER.md")
    _put(s3_hermes_bucket, "memories/notes/deep.md")

    entries = hermes_profiles.list_profile_dir(OWNER, AGENT, "memories")

    assert [(e["path"], e["type"]) for e in entries] == [("memories/notes", "dir"), ("memories/USER.md", "file")]


def test_list_profile_dir_refuses_to_enumerate_a_hidden_directory(s3_hermes_bucket):
    _put(s3_hermes_bucket, "mcp-tokens/github.json", b"{}")
    assert hermes_profiles.list_profile_dir(OWNER, AGENT, "mcp-tokens") == []


# ---------------------------------------------------------------------------
# read_profile_file
# ---------------------------------------------------------------------------


def test_read_profile_file_returns_the_workspace_read_file_info_shape(s3_hermes_bucket):
    _put(s3_hermes_bucket, "memories/USER.md", b"# The owner\n")

    info = hermes_profiles.read_profile_file(OWNER, AGENT, "memories/USER.md")

    assert info["name"] == "USER.md"
    assert info["path"] == "memories/USER.md"
    assert info["content"] == "# The owner\n"
    assert info["binary"] is False
    assert info["mime_type"] == "text/markdown"
    assert info["size"] == len(b"# The owner\n")


def test_read_profile_file_base64s_an_image_like_the_efs_reader(s3_hermes_bucket):
    _put(s3_hermes_bucket, "shot.png", b"\x89PNG\r\n\x1a\nnot-utf8\xff")

    info = hermes_profiles.read_profile_file(OWNER, AGENT, "shot.png")

    assert info["binary"] is True
    assert info["mime_type"] == "image/png"
    import base64

    assert base64.b64decode(info["content"]) == b"\x89PNG\r\n\x1a\nnot-utf8\xff"


def test_read_profile_file_hides_the_secret_store_behind_a_plain_miss(s3_hermes_bucket):
    _put(s3_hermes_bucket, ".env", b"ANTHROPIC_API_KEY=sk-live\n")
    _put(s3_hermes_bucket, "lightbulb-facts.json", b'{"plan": "ultra_pro"}')
    with pytest.raises(FileNotFoundError):
        hermes_profiles.read_profile_file(OWNER, AGENT, ".env")
    with pytest.raises(FileNotFoundError):
        hermes_profiles.read_profile_file(OWNER, AGENT, "mcp-tokens/github.json")
    with pytest.raises(FileNotFoundError):  # I1
        hermes_profiles.read_profile_file(OWNER, AGENT, "lightbulb-facts.json")


def test_read_profile_file_raises_file_not_found_for_an_absent_object(s3_hermes_bucket):
    with pytest.raises(FileNotFoundError):
        hermes_profiles.read_profile_file(OWNER, AGENT, "nope.md")


def test_read_profile_file_propagates_a_transient_s3_error(monkeypatch):
    """Same split as _s3_get (C4): only a genuine miss is 'absent'."""
    import botocore.exceptions

    def _boom(*, Bucket, Key):
        raise botocore.exceptions.ClientError({"Error": {"Code": "SlowDown"}}, "GetObject")

    monkeypatch.setattr(settings, "OWNER_STATE_BUCKET", "test-replicas")
    monkeypatch.setattr(
        hermes_profiles, "_shared_s3_client", lambda: type("C", (), {"get_object": staticmethod(_boom)})()
    )
    with pytest.raises(botocore.exceptions.ClientError):
        hermes_profiles.read_profile_file(OWNER, AGENT, "SOUL.md")


# ---------------------------------------------------------------------------
# write_profile_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_profile_file_lands_the_content_and_bumps_the_revision(s3_hermes_bucket):
    _put(s3_hermes_bucket, ".revision", b"before")

    written = await hermes_profiles.write_profile_file(OWNER, AGENT, "notes/plan.md", "# Plan\n")

    assert written == "notes/plan.md"
    body = s3_hermes_bucket.get_object(Bucket="test-replicas", Key=f"{PREFIX}notes/plan.md")["Body"].read()
    assert body == b"# Plan\n"
    revision = s3_hermes_bucket.get_object(Bucket="test-replicas", Key=f"{PREFIX}.revision")["Body"].read()
    assert revision != b"before"  # the bridge re-pulls the profile on the next turn


@pytest.mark.asyncio
@pytest.mark.parametrize("rel", ["config.yaml", "SOUL.md"])
async def test_write_profile_file_refuses_a_visible_backend_owned_file(s3_hermes_bucket, rel):
    """config.yaml and SOUL.md are listable and readable, so a 422 confirms nothing new."""
    with pytest.raises(PermissionError):
        await hermes_profiles.write_profile_file(OWNER, AGENT, rel, "pwned")


@pytest.mark.asyncio
@pytest.mark.parametrize("rel", [".env", ".revision", "lightbulb-facts.json", "mcp-tokens/github.json"])
async def test_write_profile_file_answers_a_hidden_path_exactly_as_the_read_does(s3_hermes_bucket, rel):
    """Fix round 1, m3: the write route must not confirm the existence of what the read route
    hides behind a 404."""
    with pytest.raises(FileNotFoundError):
        await hermes_profiles.write_profile_file(OWNER, AGENT, rel, "pwned")


@pytest.mark.asyncio
async def test_write_profile_file_refuses_to_report_a_write_with_no_bucket_configured(monkeypatch):
    """Fix round 1, m2: upload_profile_files no-ops on an empty bucket name, so returning here
    would answer {"status": "ok"} having written nothing."""
    monkeypatch.setattr(settings, "OWNER_STATE_BUCKET", "")
    with pytest.raises(RuntimeError):
        await hermes_profiles.write_profile_file(OWNER, AGENT, "notes/plan.md", "# Plan\n")


@pytest.mark.asyncio
async def test_write_profile_file_refuses_a_traversal_path(s3_hermes_bucket):
    with pytest.raises(ValueError):
        await hermes_profiles.write_profile_file(OWNER, AGENT, "../other/SOUL.md", "pwned")


# ---------------------------------------------------------------------------
# read_soul_md
# ---------------------------------------------------------------------------


def test_read_soul_md_returns_none_when_the_profile_has_none_yet(s3_hermes_bucket):
    assert hermes_profiles.read_soul_md(OWNER, AGENT) is None


def test_read_soul_md_returns_the_stored_text(s3_hermes_bucket):
    _put(s3_hermes_bucket, "SOUL.md", b"# Ada\n")
    assert hermes_profiles.read_soul_md(OWNER, AGENT) == "# Ada\n"
