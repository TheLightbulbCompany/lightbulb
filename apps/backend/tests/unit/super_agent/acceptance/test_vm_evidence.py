"""Wave-2 gate G2 step 3: per-task VM evidence over fake ``tasks_rows`` (plans/wave2-V Task 17)."""

import pytest

from core.services.runtime_transport import session_id_for
from scripts.super_agent.acceptance.vm_evidence import vm_evidence

WS_ON, WS_CHATGPT = "org_on", "org_chatgpt"
T1, T2 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
T3 = "33333333-3333-4333-8333-333333333333"


class FakeCtx:
    def __init__(self, rows: dict[str, list[dict]]):
        self.rows = rows

    async def tasks_rows(self, ws: str) -> list[dict]:
        return list(self.rows.get(ws, []))


def _attempt(task_id: str, n: int, session_id: str) -> dict:
    return {"type": "attempt", "taskId": task_id, "n": n, "sessionId": session_id, "status": "ended"}


def _rows(on_sessions: dict[str, str], chatgpt_session: str) -> dict[str, list[dict]]:
    return {
        WS_ON: [
            {"type": "task", "taskId": T1, "status": "done"},  # not an attempt: ignored
            *(_attempt(tid, 1, sid) for tid, sid in on_sessions.items()),
            _attempt(T1, 2, on_sessions[T1]) if T1 in on_sessions else {},
            _attempt("99999999-9999-4999-8999-999999999999", 1, "lb-unrelated"),  # another task: ignored
        ],
        WS_CHATGPT: [_attempt(T3, 1, chatgpt_session)],
    }


@pytest.mark.asyncio
async def test_vm_evidence_passes_on_task_and_workspace_split():
    ctx = FakeCtx(_rows({T1: session_id_for(WS_ON, T1), T2: session_id_for(WS_ON, T2)}, session_id_for(WS_CHATGPT)))
    result = await vm_evidence(ctx, ws_on=WS_ON, task_ids_on=[T1, T2], ws_chatgpt=WS_CHATGPT, task_ids_chatgpt=[T3])
    assert result.passed and result.evidence["failures"] == []
    assert [a["taskId"] for a in result.evidence["on"]["attempts"]] == [T1, T1, T2]  # both attempts of T1, then T2
    assert result.evidence["chatgpt"]["attempts"] == [{"taskId": T3, "n": 1, "sessionId": session_id_for(WS_CHATGPT)}]


@pytest.mark.asyncio
async def test_vm_evidence_fails_when_chatgpt_ran_on_task_vm():
    ctx = FakeCtx(_rows({T1: session_id_for(WS_ON, T1), T2: session_id_for(WS_ON, T2)}, session_id_for(WS_CHATGPT, T3)))
    result = await vm_evidence(ctx, ws_on=WS_ON, task_ids_on=[T1, T2], ws_chatgpt=WS_CHATGPT, task_ids_chatgpt=[T3])
    assert not result.passed
    [failure] = result.evidence["failures"]
    assert failure.startswith(f"chatgpt:{T3}#1:") and "not the workspace session" in failure


@pytest.mark.asyncio
async def test_vm_evidence_fails_when_two_tasks_share_a_session():
    shared = session_id_for(WS_ON)  # both tasks ran on the workspace VM
    ctx = FakeCtx(_rows({T1: shared, T2: shared}, session_id_for(WS_CHATGPT)))
    result = await vm_evidence(ctx, ws_on=WS_ON, task_ids_on=[T1, T2], ws_chatgpt=WS_CHATGPT, task_ids_chatgpt=[T3])
    assert not result.passed
    failures = result.evidence["failures"]
    assert any(f.startswith(f"on:{T1}#1:") and "not the task session" in f for f in failures)
    assert any(f.startswith(f"on:{T2}#1:") and f"shares session {shared} with task {T1}" in f for f in failures)


@pytest.mark.asyncio
async def test_vm_evidence_fails_when_a_task_never_ran():
    ctx = FakeCtx(_rows({T1: session_id_for(WS_ON, T1)}, session_id_for(WS_CHATGPT)))
    result = await vm_evidence(ctx, ws_on=WS_ON, task_ids_on=[T1, T2], ws_chatgpt=WS_CHATGPT, task_ids_chatgpt=[T3])
    assert not result.passed and result.evidence["failures"] == [f"on:{T2}: no attempt rows"]
