"""`scripts/watchdog_ctl.py` (wave2-W Task 5): validated settings, the kill switch keeps ticking, labels and
every change need a platform admin and are audited, nothing runs with the env master off."""

import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core import tasks_schema as ts
from core.config import settings
from core.repositories import watchdog_repo
from core.temporal.watchdog_types import WatchdogConfig
from scripts import watchdog_ctl as ctl

ADMIN = "user_admin"


class FakeHandle:
    def __init__(self):
        self.schedule = SimpleNamespace(
            action=SimpleNamespace(args=[WatchdogConfig()]), state=SimpleNamespace(paused=False)
        )
        self.paused = False

    async def describe(self):
        return SimpleNamespace(schedule=self.schedule)

    async def update(self, fn):
        out = fn(SimpleNamespace(description=SimpleNamespace(schedule=self.schedule)))
        self.schedule = out.schedule

    async def pause(self, note=None):
        self.paused = True

    async def unpause(self, note=None):
        self.paused = False


class FakeClient:
    def __init__(self):
        self.handle = FakeHandle()
        self.data_converter = SimpleNamespace(decode=AsyncMock(side_effect=lambda raw, types: list(raw)))

    def get_schedule_handle(self, sid):
        assert sid == "watchdog"
        return self.handle


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", True)
    monkeypatch.setattr(settings, "PLATFORM_ADMIN_USER_IDS", ADMIN)
    audit = AsyncMock()
    with patch.object(ctl.admin_actions_repo, "create", audit):
        yield SimpleNamespace(client=FakeClient(), audit=audit)


def _args(*argv):
    return ctl.parser().parse_args(list(argv))


async def test_ctl_set_validates_and_audits(env):
    out = await ctl.run(_args("--set", "judge_per_tick=5", "tier1=off", "judge=false", "--by", ADMIN), env.client)
    assert out["config"]["judge_per_tick"] == 5 and out["config"]["tier1"] == "off" and out["config"]["judge"] is False
    assert env.client.handle.schedule.action.args == [
        dataclasses.replace(WatchdogConfig(), judge_per_tick=5, tier1="off", judge=False)
    ]
    kw = env.audit.await_args.kwargs
    assert (kw["admin_user_id"], kw["action"]) == (ADMIN, "watchdog.config")
    assert kw["payload"]["old"]["judge_per_tick"] == 10 and kw["payload"]["new"]["judge_per_tick"] == 5
    for bad in ("nope=1", "tier1=act", "judge_per_tick=x", "judge=yes", "fleet_actions_per_hour=-1"):
        with pytest.raises(ctl.CtlError):
            await ctl.run(_args("--set", bad, "--by", ADMIN), env.client)


async def test_ctl_off_keeps_schedule_running(env):
    await ctl.run(_args("--off", "--by", ADMIN), env.client)
    (cfg,) = env.client.handle.schedule.action.args
    assert cfg.all_off()
    assert env.client.handle.paused is False


async def test_ctl_pause_resume(env):
    await ctl.run(_args("--pause", "--by", ADMIN), env.client)
    assert env.client.handle.paused
    await ctl.run(_args("--resume", "--by", ADMIN), env.client)
    assert not env.client.handle.paused
    assert [c.kwargs["action"] for c in env.audit.await_args_list] == ["watchdog.pause", "watchdog.resume"]


async def test_ctl_change_requires_platform_admin(env):
    with pytest.raises(ctl.CtlError):
        await ctl.run(_args("--off", "--by", "user_random"), env.client)
    with pytest.raises(ctl.CtlError):
        await ctl.run(_args("--off"), env.client)
    assert env.client.handle.schedule.action.args == [WatchdogConfig()]


async def test_ctl_label_requires_platform_admin(env, table):
    f, _ = await watchdog_repo.put_finding(
        {"pk": ts.wd_finding_pk("20261104"), "sk": ts.wd_finding_sk("org_1", "t1", "C1", 1), "check": "C1"}
    )
    with pytest.raises(ctl.CtlError):
        await ctl.run(_args("--label", f["findingId"], "tp", "--by", "user_random"))
    out = await ctl.run(_args("--label", f["findingId"], "tp", "--by", ADMIN, "--note", "real"))
    assert out == {"findingId": f["findingId"], "label": "tp", "labelledBy": ADMIN}
    assert env.audit.await_args.kwargs["action"] == "watchdog.label"
    with pytest.raises(ctl.CtlError):
        await ctl.run(_args("--label", f["findingId"], "maybe", "--by", ADMIN))


async def test_ctl_refuses_when_env_master_off(env, monkeypatch):
    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", False)
    with pytest.raises(ctl.CtlError):
        await ctl.run(_args("--show"), env.client)
    env.audit.assert_not_awaited()


async def test_ctl_show(env):
    out = await ctl.run(_args("--show"), env.client)
    assert out == {"config": dataclasses.asdict(WatchdogConfig()), "paused": False}
