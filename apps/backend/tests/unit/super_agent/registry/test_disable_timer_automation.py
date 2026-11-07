"""Step 0 (spec 03 §8): dev timer heartbeats and CEO routines off, users' own routines untouched.

``disable_timer_automation`` is NOT ``pause_owner_automation``: that one pauses every active
routine (the owner's own too) and records them in the entitlement pause fence for a resume.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import paperclip_agent_lifecycle as lifecycle
from core.services import user_provision
from core.services.agent_routine_provisioning import NIGHTLY_CEO_ROUTINE_TITLE

OFF = {"enabled": False, "wakeOnDemand": True}
FEED_TITLE = user_provision.CEO_ROUTINES[0][1]


class FakePaperclip:
    def __init__(self):
        self.agents = {
            "ceo1": {"id": "ceo1", "runtimeConfig": {"heartbeat": {"enabled": True, "intervalSec": 28800}}},
            "eng1": {"id": "eng1", "runtimeConfig": {"heartbeat": {"enabled": True, "intervalSec": 86400}}},
            "fin1": {"id": "fin1", "runtimeConfig": {"heartbeat": {"enabled": False}}},
        }
        self.routines = {
            "r-feed": {"id": "r-feed", "title": FEED_TITLE, "assigneeAgentId": "ceo1", "status": "active"},
            "r-plan": {
                "id": "r-plan",
                "title": user_provision.PLANNING_ROUTINE_TITLE,
                "assigneeAgentId": "ceo1",
                "status": "active",
            },
            "r-nightly": {
                "id": "r-nightly",
                "title": NIGHTLY_CEO_ROUTINE_TITLE,
                "assigneeAgentId": "ceo1",
                "status": "active",
            },
            # The owner's own routine, even one titled like ours but on another agent: theirs.
            "r-user": {"id": "r-user", "title": "Weekly invoices", "assigneeAgentId": "ceo1", "status": "active"},
            "r-user-eng": {"id": "r-user-eng", "title": FEED_TITLE, "assigneeAgentId": "eng1", "status": "active"},
        }
        self.admin = MagicMock()
        self.admin.list_agents = AsyncMock(side_effect=lambda **kw: {"agents": list(self.agents.values())})
        self.admin.list_routines = AsyncMock(side_effect=lambda **kw: list(self.routines.values()))
        self.admin.set_agent_heartbeat = AsyncMock(side_effect=self._set_heartbeat)
        self.admin.patch_routine = AsyncMock(side_effect=self._patch_routine)

    async def _set_heartbeat(self, *, session_cookie, agent_id, heartbeat):
        self.agents[agent_id]["runtimeConfig"] = {"heartbeat": heartbeat}
        return self.agents[agent_id]

    async def _patch_routine(self, *, session_cookie, routine_id, body):
        self.routines[routine_id].update(body)
        return self.routines[routine_id]

    def enabled(self):
        return sorted(a for a, v in self.agents.items() if v["runtimeConfig"]["heartbeat"].get("enabled"))


def _repo():
    repo = MagicMock()
    ws = MagicMock(company_id="co1", starter_agent_ids={"ceo": "ceo1"}, seeded_agent_ids=None)
    repo.resolve_owner_row = AsyncMock(return_value=ws)
    repo.record_paused_automation = AsyncMock()
    return repo


def _lifecycle_patches(fake, repo):
    http = MagicMock()
    http.__aenter__ = AsyncMock(return_value=http)
    http.__aexit__ = AsyncMock(return_value=False)
    return (
        patch.object(lifecycle, "PaperclipRepo", return_value=repo),
        patch.object(lifecycle, "PaperclipAdminClient", return_value=fake.admin),
        patch.object(lifecycle, "get_admin_session_cookie", new=AsyncMock(return_value="cookie")),
        patch.object(lifecycle.httpx, "AsyncClient", return_value=http),
        patch.object(lifecycle, "put_metric"),
    )


async def _disable(fake, repo, **kw):
    p = _lifecycle_patches(fake, repo)
    with p[0], p[1], p[2], p[3], p[4]:
        return await lifecycle.disable_timer_automation("org_p", **kw)


@pytest.mark.asyncio
async def test_disables_timers_keeps_user_routines():
    fake, repo = FakePaperclip(), _repo()
    report = await _disable(fake, repo)

    assert report == {"agents": ["ceo1", "eng1"], "routines": ["r-feed", "r-plan", "r-nightly"]}
    assert fake.agents["ceo1"]["runtimeConfig"]["heartbeat"] == OFF
    assert fake.agents["eng1"]["runtimeConfig"]["heartbeat"] == OFF
    assert {r: v["status"] for r, v in fake.routines.items()} == {
        "r-feed": "paused",
        "r-plan": "paused",
        "r-nightly": "paused",
        "r-user": "active",
        "r-user-eng": "active",
    }
    repo.record_paused_automation.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_writes_nothing():
    fake, repo = FakePaperclip(), _repo()
    report = await _disable(fake, repo, dry_run=True)

    assert report == {"agents": ["ceo1", "eng1"], "routines": ["r-feed", "r-plan", "r-nightly"]}
    fake.admin.set_agent_heartbeat.assert_not_called()
    fake.admin.patch_routine.assert_not_called()


@pytest.mark.asyncio
async def test_script_refuses_prod():
    from scripts import disable_paperclip_timers as script

    disable = AsyncMock()
    with (
        patch.object(script.settings, "ENVIRONMENT", "prod"),
        patch.object(script, "disable_timer_automation", disable),
    ):
        assert await script.main(["--all", "--apply"]) == 2
    disable.assert_not_called()
