"""A WRITE door must never guess the runtime host.

``runtime_transport.is_agentcore_owner`` answers ``False`` (= ECS) when the DynamoDB read
fails, by design (M6-R26 fix round 1, Important 3): the relay/connect/disconnect doors that
first read this table treat it as a routine per-request check, and a transient throttle must
not turn into a hard failure for an ECS owner who never paid that read before.

That default is wrong in kind on a MUTATING path. A migrated workspace keeps its EFS tree --
``scripts/migrate_workspace_to_hermes.py`` never deletes it -- so the ECS branch SUCCEEDS:
``Workspace.write_file`` mkdirs its parents, ``patch_openclaw_config`` finds the abandoned
``openclaw.json``, and the door answers 200 for a write no Hermes agent will ever read. Same
defect class as task-14 row 5 (BRIEF.md) and the write route's own
``workspace_files._is_agentcore_row_or_503``.

So every write door passes ``fail_closed=True`` and propagates into its existing error path.
These tests patch the REAL DynamoDB read rather than the seam, so they fail if a call site
stops opting in.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import runtime_transport as rt


def _read_fails():
    """Patch the one container read ``is_agentcore_owner`` performs so it raises."""

    async def _raise(owner_id):
        raise Exception("dynamodb throttled")

    return patch("core.repositories.container_repo.get_by_owner_id", _raise)


class TestSeam:
    @pytest.mark.asyncio
    async def test_default_still_fails_open_to_ecs(self, caplog):
        # Unchanged contract for the relay/connect/disconnect doors (M6-R26).
        with _read_fails(), caplog.at_level("WARNING"):
            assert await rt.is_agentcore_owner("org_1") is False
        assert "org_1" in caplog.text

    @pytest.mark.asyncio
    async def test_fail_closed_raises_instead_of_answering_ecs(self):
        with _read_fails(), pytest.raises(rt.RuntimeHostUnknownError):
            await rt.is_agentcore_owner("org_1", fail_closed=True)

    @pytest.mark.asyncio
    async def test_fail_closed_is_inert_when_the_read_succeeds(self):
        async def _get(owner_id):
            return {"runtime_host": "agentcore"}

        with patch("core.repositories.container_repo.get_by_owner_id", _get):
            assert await rt.is_agentcore_owner("org_1", fail_closed=True) is True


class TestStageFirstRunFiles:
    @pytest.mark.asyncio
    async def test_a_failed_lookup_never_writes_the_brief_to_efs(self):
        """The caller's ``except BaseException`` rolls the goal/project back and reverts the
        funnel claim, so the submission is retried -- strictly better than a BRIEF.md staged
        onto the tree the migration left behind while the funnel reports complete."""
        from core.services.onboarding_brief import stage_first_run_files

        ws = MagicMock()
        with _read_fails(), pytest.raises(rt.RuntimeHostUnknownError):
            await stage_first_run_files(
                workspace=ws, owner_id="org_abc", friday_agent_id="friday-uuid", brief_md="brief"
            )
        ws.write_file.assert_not_called()


class TestApplyConnection:
    @pytest.mark.asyncio
    async def test_a_failed_lookup_never_projects_a_credential_onto_efs(self):
        """Propagates into the connect routes' existing rollback (``except Exception`` ->
        remove artifacts, delete the retained secret, 502 ``apply_failed``), so the row never
        reads ``connected`` for a credential the agent cannot see."""
        from core.services.credentials import apply_connection as ac

        provider = MagicMock()
        provider.wiring.backend_only = False
        with (
            _read_fails(),
            patch.object(ac.registry, "get_provider", return_value=provider),
            patch.object(ac.config_patcher, "set_openclaw_config_path", AsyncMock()) as set_path,
            pytest.raises(rt.RuntimeHostUnknownError),
        ):
            await ac.apply_connection("org_abc", "stripe", {"token": "sk_test"})
        set_path.assert_not_awaited()


class TestDisconnectTeardown:
    @pytest.mark.asyncio
    async def test_a_failed_lookup_tears_nothing_down(self):
        """The lookup is the first statement, so nothing is written -- and failing closed is
        what stops a disconnect that reports success while the credential stays live in every
        Hermes profile (the ECS branch skips the profile reconcile entirely)."""
        from routers import connections

        with (
            _read_fails(),
            patch.object(connections.integrations_repo, "delete", AsyncMock()) as row_delete,
            pytest.raises(rt.RuntimeHostUnknownError),
        ):
            await connections._disconnect_teardown("org_abc", "stripe", MagicMock())
        row_delete.assert_not_awaited()


class TestConfigPatchDoor:
    @pytest.mark.asyncio
    async def test_a_failed_lookup_answers_503_and_patches_nothing(self, async_client):
        """The owner-scoped save. A silent EFS patch is the worst outcome here: the settings
        panel shows the new value, the agent never gets it."""
        with (
            _read_fails(),
            patch("routers.config.patch_openclaw_config", AsyncMock()) as efs_patch,
            patch("routers.config.config_patch_roster", AsyncMock()) as roster_patch,
        ):
            resp = await async_client.patch("/api/v1/config", json={"patch": {"tools": {"profile": "full"}}})
        assert resp.status_code == 503
        efs_patch.assert_not_awaited()
        roster_patch.assert_not_awaited()


class TestGoogleAssignDoor:
    @pytest.mark.asyncio
    async def test_a_failed_lookup_answers_503_and_projects_nothing(self, async_client):
        """The gate moved into the seeder, which is where the runtime now decides the
        DESTINATION (IDENTITY.md on EFS vs GOG_ACCOUNT in the Hermes profile) rather than
        whether to refuse. The migration never deletes the EFS tree, so guessing "ECS" on a
        throttled read writes where no Hermes agent reads and answers 200. The row is stored
        (it is the truth, and a retry re-projects it); the door reports 503 and nothing is
        written to either destination."""
        from core.services import google_identity

        with (
            _read_fails(),
            patch("routers.google_accounts.google_accounts_repo") as repo,
            patch.object(google_identity, "get_workspace") as workspace,
        ):
            repo.list_accounts = AsyncMock(return_value=[{"email": "alice@example.com"}])
            repo.set_assignment = AsyncMock()
            resp = await async_client.post(
                "/api/v1/google/accounts/alice@example.com/assign", json={"agent_id": "agent_1"}
            )
        assert resp.status_code == 503
        workspace.assert_not_called()
