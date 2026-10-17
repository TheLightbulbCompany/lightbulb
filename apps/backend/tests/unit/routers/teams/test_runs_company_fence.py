"""The per-run Teams routes 404 a heartbeat run outside the caller's workspace company."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from routers.teams import agents as agents_mod
from routers.teams import runs as runs_mod
from routers.teams.deps import TeamsContext


def _ctx():
    return TeamsContext(
        user_id="u1",
        org_id="org1",
        owner_id="org1",
        company_id="co1",
        paperclip_user_id="pcu1",
        session_cookie="cookie1",
    )


def _admin(run):
    fake = MagicMock()
    fake.get_heartbeat_run = AsyncMock(return_value=run)
    return fake


@pytest.mark.asyncio
async def test_company_run_passes_own_run():
    with patch("routers.teams.agents._admin", return_value=_admin({"id": "r1", "companyId": "co1"})):
        assert await runs_mod._company_run("r1", ctx=_ctx()) == "r1"


@pytest.mark.asyncio
@pytest.mark.parametrize("run", [{"id": "r1", "companyId": "co2"}, {"id": "r1"}, None])
@pytest.mark.parametrize("call", [runs_mod._company_run, runs_mod.get_run, agents_mod.get_run, runs_mod.cancel_run])
async def test_run_fence_404s_foreign_or_missing(run, call):
    with patch("routers.teams.agents._admin", return_value=_admin(run)):
        with pytest.raises(HTTPException) as exc:
            await call("r1", ctx=_ctx())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [runs_mod.get_run, agents_mod.get_run])
async def test_get_run_returns_own_run(call):
    run = {"id": "r1", "companyId": "co1", "status": "failed"}
    with patch("routers.teams.agents._admin", return_value=_admin(run)):
        assert await call("r1", ctx=_ctx()) == run


def test_every_per_run_route_is_fenced():
    """Each ``{run_id}`` route depends on the fence, or fences inline (tested above)."""
    unfenced = {
        (r.path, r.endpoint.__name__)
        for router in (runs_mod.router, agents_mod.router)
        for r in router.routes
        if "{run_id}" in getattr(r, "path", "")
        and not any(d.call is runs_mod._company_run for d in r.dependant.dependencies)
    }
    assert unfenced == {
        ("/heartbeat-runs/{run_id}", "get_run"),
        ("/runs/{run_id}", "get_run"),
        # Fences inline: it needs the run row to know whether a Hermes turn is left to stop.
        ("/heartbeat-runs/{run_id}/cancel", "cancel_run"),
    }
