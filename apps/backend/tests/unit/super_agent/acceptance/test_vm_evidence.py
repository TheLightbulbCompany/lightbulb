"""Wave-2 gate G2 step 3: VM evidence over fake ``tasks_rows`` (plans/wave2-V Task 17)."""

import pytest

from core.services.runtime_transport import session_id_for
from scripts.super_agent.acceptance.vm_evidence import vm_evidence

WS = "org_test"
T1, T2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


class FakeCtx:
    def __init__(self, attempts: list[dict]):
        self.attempts = attempts

    async def tasks_rows(self, ws: str) -> list[dict]:
        return [{"type": "task", "taskId": T1}, *self.attempts]


def _att(task_id: str, n: int, session_id: str) -> dict:
    return {"type": "attempt", "taskId": task_id, "n": n, "sessionId": session_id}


@pytest.mark.asyncio
async def test_per_task_passes_with_distinct_task_sessions():
    ctx = FakeCtx(
        [_att(T1, 1, session_id_for(WS, T1)), _att(T1, 2, session_id_for(WS, T1)), _att(T2, 1, session_id_for(WS, T2))]
    )
    result = await vm_evidence(ctx, ws=WS, task_ids=[T1, T2], per_task=True)
    assert result.passed and [a["taskId"] for a in result.evidence["attempts"]] == [T1, T1, T2]


@pytest.mark.asyncio
async def test_per_task_fails_when_tasks_share_the_workspace_vm():
    shared = session_id_for(WS)
    result = await vm_evidence(
        FakeCtx([_att(T1, 1, shared), _att(T2, 1, shared)]), ws=WS, task_ids=[T1, T2], per_task=True
    )
    failures = result.evidence["failures"]
    assert not result.passed and any("not the task session" in f for f in failures)
    assert any(f"shares session {shared} with task {T1}" in f for f in failures)


@pytest.mark.asyncio
async def test_actor_custody_expects_the_workspace_session():
    ok = await vm_evidence(FakeCtx([_att(T1, 1, session_id_for(WS))]), ws=WS, task_ids=[T1], per_task=False)
    assert ok.passed
    bad = await vm_evidence(FakeCtx([_att(T1, 1, session_id_for(WS, T1))]), ws=WS, task_ids=[T1], per_task=False)
    assert not bad.passed and "not the workspace session" in bad.evidence["failures"][0]


@pytest.mark.asyncio
async def test_a_task_that_never_ran_is_a_failure():
    result = await vm_evidence(FakeCtx([]), ws=WS, task_ids=[T2], per_task=True)
    assert result.evidence["failures"] == [f"{T2}: no attempt rows"]


@pytest.mark.asyncio
async def test_per_task_defaults_to_the_backend_rule(monkeypatch):
    from core.services import token_custody

    async def allowed(owner_id):
        return True

    monkeypatch.setattr(token_custody, "task_vm_allowed", allowed)
    monkeypatch.setattr("scripts.super_agent.acceptance.ctx.load_backend_secrets", lambda: None)  # no Secrets Manager
    result = await vm_evidence(FakeCtx([_att(T1, 1, session_id_for(WS, T1))]), ws=WS, task_ids=[T1])
    assert result.passed and result.evidence["perTaskVm"] is True


def test_backend_secrets_refuse_outside_dev(monkeypatch):
    from core.config import settings
    from scripts.super_agent.acceptance import ctx as ctxmod

    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", "")
    with pytest.raises(SystemExit):
        ctxmod.load_backend_secrets()
    assert settings.ENCRYPTION_KEY == ""
