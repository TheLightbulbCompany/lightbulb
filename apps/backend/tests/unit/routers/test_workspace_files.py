"""Tests for the workspace-file routes over an agent's S3 Hermes profile.

There is nothing else left to serve: the EFS-tree branch of every route went with the ECS
runtime, so did the fixtures that built a sample tree on disk. What remains is the profile
branch and the rules it enforces -- which names map onto which Hermes path, which backend-owned
files are refused, and which secrets are neither listed nor readable.
"""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from core.config import settings

USER_ID = "user_test_abc"
AGENT_ID = "agent-abc-123"


def _auth(owner_id: str = USER_ID) -> AuthContext:
    """Build an active workspace context for direct handler calls."""
    return AuthContext(user_id=f"human_{owner_id}", org_id=owner_id, org_role="org:admin")


class TestAgentCoreProfileBranch:
    """Every route serves the agent's S3 Hermes profile.

    The migration copied EFS into the profile and left the EFS tree behind, so a route that
    still read the tree would serve — and SAVE to — content nobody reads: a green save with no
    effect.
    """

    @pytest.fixture()
    def s3(self):
        import boto3
        from moto import mock_aws

        from core.services import hermes_profiles

        hermes_profiles._SHARED_S3_CLIENT = None
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="test-replicas")
            with patch.object(settings, "OWNER_STATE_BUCKET", "test-replicas"):
                yield client
        hermes_profiles._SHARED_S3_CLIENT = None

    @staticmethod
    def _prefix() -> str:
        return f"{USER_ID}/hermes/{AGENT_ID}/"

    def _put(self, s3, key: str, body: bytes) -> None:
        s3.put_object(Bucket="test-replicas", Key=f"{self._prefix()}{key}", Body=body)

    def _get(self, s3, key: str) -> bytes:
        return s3.get_object(Bucket="test-replicas", Key=f"{self._prefix()}{key}")["Body"].read()

    # -- Instructions tab -------------------------------------------------

    @pytest.mark.asyncio
    async def test_reads_soul_md_from_the_profile(self, s3):
        from routers.workspace_files import read_config_file

        self._put(s3, "SOUL.md", b"# Ada\n\nthe live profile\n")

        info = await read_config_file(agent_id=AGENT_ID, path="SOUL.md", auth=_auth())

        assert info["content"] == "# Ada\n\nthe live profile\n"
        assert info["name"] == "SOUL.md"

    @pytest.mark.asyncio
    async def test_writes_soul_md_through_the_profile_writer_and_bumps_the_revision(self, s3):
        from core.services import hermes_profiles
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        self._put(s3, ".revision", b"before")
        body = WriteFileRequest(
            path="SOUL.md", content=f"{hermes_profiles.SOUL_GENERATED_MARKER}\n\n# Ada\n\nowner text\n", tab="config"
        )

        result = await write_workspace_file(agent_id=AGENT_ID, body=body, auth=_auth())

        assert result == {"status": "ok", "path": "SOUL.md"}
        # The marker is stripped on save: the file is the owner's from now on, so a later rename
        # (runtime_rpc._refresh_agentcore_soul) must not regenerate over it.
        stored = self._get(s3, "SOUL.md").decode()
        assert stored == "# Ada\n\nowner text\n"
        assert not hermes_profiles.is_generated_soul(stored)
        assert self._get(s3, ".revision") != b"before"

    @pytest.mark.asyncio
    async def test_user_md_maps_onto_the_hermes_user_profile_memory(self, s3):
        from routers.workspace_files import WriteFileRequest, read_config_file, write_workspace_file

        self._put(s3, "memories/USER.md", b"# The owner\n")
        self._put(s3, ".revision", b"before")

        info = await read_config_file(agent_id=AGENT_ID, path="USER.md", auth=_auth())
        assert info["content"] == "# The owner\n"
        assert info["path"] == "USER.md"  # reported under the name the tab asked for

        await write_workspace_file(
            agent_id=AGENT_ID,
            body=WriteFileRequest(path="USER.md", content="# Edited\n", tab="config"),
            auth=_auth(),
        )
        assert self._get(s3, "memories/USER.md") == b"# Edited\n"
        # Every write path bumps .revision through the writer's own helper -- that bump is what
        # the bridge watches to re-pull the profile.
        assert self._get(s3, ".revision") != b"before"

    @pytest.mark.asyncio
    async def test_agents_md_write_also_bumps_the_revision(self, s3):
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        self._put(s3, ".revision", b"before")

        await write_workspace_file(
            agent_id=AGENT_ID,
            body=WriteFileRequest(path="AGENTS.md", content="# Agents\n", tab="config"),
            auth=_auth(),
        )

        assert self._get(s3, "AGENTS.md") == b"# Agents\n"
        assert self._get(s3, ".revision") != b"before"

    @pytest.mark.asyncio
    async def test_identity_md_reads_a_stub_and_refuses_a_save(self, s3):
        from routers.workspace_files import WriteFileRequest, read_config_file, write_workspace_file

        info = await read_config_file(agent_id=AGENT_ID, path="IDENTITY.md", auth=_auth())
        assert "SOUL.md" in info["content"]
        assert info["binary"] is False

        with pytest.raises(HTTPException) as exc:
            await write_workspace_file(
                agent_id=AGENT_ID,
                body=WriteFileRequest(path="IDENTITY.md", content="nope", tab="config"),
                auth=_auth(),
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "unsupported_on_agentcore"

    @pytest.mark.asyncio
    async def test_config_files_lists_only_the_names_with_a_hermes_home(self, s3):
        from routers.workspace_files import list_config_files

        self._put(s3, "AGENTS.md", b"# Agents\n")
        self._put(s3, "SOUL.md", b"# Ada\n")
        self._put(s3, "memories/USER.md", b"# Owner\n")
        self._put(s3, "memories/MEMORY.md", b"- likes tea\n")
        self._put(s3, "config.yaml", b"model: {}\n")  # backend-owned, never a config-tab row

        result = await list_config_files(agent_id=AGENT_ID, auth=_auth())

        assert [f["path"] for f in result["files"]] == ["AGENTS.md", "MEMORY.md", "SOUL.md", "USER.md"]
        assert result["files"][2]["size"] == len(b"# Ada\n")

    @pytest.mark.asyncio
    async def test_memory_md_is_the_agents_own_notes_file(self, s3):
        """MEMORY.md maps to memories/MEMORY.md, where Hermes's memory tool reads and writes."""
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        await write_workspace_file(
            agent_id=AGENT_ID,
            body=WriteFileRequest(path="MEMORY.md", content="- likes tea\n", tab="config"),
            auth=_auth(),
        )
        assert self._get(s3, "memories/MEMORY.md") == b"- likes tea\n"

    @pytest.mark.asyncio
    async def test_config_names_with_no_hermes_reader_are_refused_not_silently_written(self, s3):
        """TOOLS.md: a write with nowhere real to land is the defect this
        branch exists to kill, so it is a 422, never a green save."""
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        for name in ("TOOLS.md",):
            with pytest.raises(HTTPException) as exc:
                await write_workspace_file(
                    agent_id=AGENT_ID,
                    body=WriteFileRequest(path=name, content="x", tab="config"),
                    auth=_auth(),
                )
            assert exc.value.status_code == 422
            assert exc.value.detail["code"] == "unsupported_on_agentcore"

    # -- Files panel ------------------------------------------------------

    @pytest.mark.asyncio
    async def test_tree_lists_the_profile_without_the_env_file_or_mcp_tokens(self, s3):
        from routers.workspace_files import list_workspace_tree

        self._put(s3, "SOUL.md", b"# Ada\n")
        self._put(s3, ".env", b"ANTHROPIC_API_KEY=sk-live\n")
        self._put(s3, ".revision", b"rev")
        self._put(s3, "mcp-tokens/github.json", b"{}")
        self._put(s3, "skills/foo/SKILL.md", b"# Foo\n")

        result = await list_workspace_tree(agent_id=AGENT_ID, path="", auth=_auth())

        assert [f["path"] for f in result["files"]] == ["skills", "SOUL.md"]

    @pytest.mark.asyncio
    async def test_file_read_refuses_the_secret_store_as_a_plain_miss(self, s3):
        from routers.workspace_files import read_workspace_file

        self._put(s3, ".env", b"ANTHROPIC_API_KEY=sk-live\n")

        with pytest.raises(HTTPException) as exc:
            await read_workspace_file(agent_id=AGENT_ID, path=".env", auth=_auth())
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_the_facts_file_is_neither_listed_nor_readable_nor_writable(self, s3):
        """Fix round 1, I1: it carries the owner's plan, subscription status, provider choice and
        agent e-mail, and no owner-facing route has ever served it on EFS (it lives at the user
        root, outside the workspace subtree the panel is scoped to)."""
        from routers.workspace_files import (
            WriteFileRequest,
            list_workspace_tree,
            read_workspace_file,
            write_workspace_file,
        )

        self._put(s3, "lightbulb-facts.json", b'{"plan": "ultra_pro"}')
        self._put(s3, "SOUL.md", b"# Ada\n")

        tree = await list_workspace_tree(agent_id=AGENT_ID, path="", auth=_auth())
        assert [f["path"] for f in tree["files"]] == ["SOUL.md"]

        for call in (
            read_workspace_file(agent_id=AGENT_ID, path="lightbulb-facts.json", auth=_auth()),
            write_workspace_file(
                agent_id=AGENT_ID,
                body=WriteFileRequest(path="lightbulb-facts.json", content="{}", tab="workspace"),
                auth=_auth(),
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await call
            assert exc.value.status_code == 404
        assert self._get(s3, "lightbulb-facts.json") == b'{"plan": "ultra_pro"}'  # untouched

    @pytest.mark.asyncio
    async def test_workspace_write_lands_in_the_profile(self, s3):
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        result = await write_workspace_file(
            agent_id=AGENT_ID,
            body=WriteFileRequest(path="notes/plan.md", content="# Plan\n", tab="workspace"),
            auth=_auth(),
        )

        assert result == {"status": "ok", "path": "notes/plan.md"}
        assert self._get(s3, "notes/plan.md") == b"# Plan\n"

    @pytest.mark.asyncio
    async def test_workspace_write_refuses_a_backend_owned_file(self, s3):
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        with pytest.raises(HTTPException) as exc:
            await write_workspace_file(
                agent_id=AGENT_ID,
                body=WriteFileRequest(path="config.yaml", content="model: {}\n", tab="workspace"),
                auth=_auth(),
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "unsupported_on_agentcore"

    @pytest.mark.asyncio
    async def test_workspace_write_refuses_a_traversal_path(self, s3):
        from routers.workspace_files import WriteFileRequest, write_workspace_file

        with pytest.raises(HTTPException) as exc:
            await write_workspace_file(
                agent_id=AGENT_ID,
                body=WriteFileRequest(path="../other-agent/SOUL.md", content="pwned", tab="workspace"),
                auth=_auth(),
            )
        assert exc.value.status_code == 400
