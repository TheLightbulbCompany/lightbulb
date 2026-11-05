"""effects_repo (wave2-Q Task 2): Effect rows are append-only puts with forward-only state (contracts-v2 §3, §7)."""

import asyncio

import pytest

from core.repositories import effects_repo as repo
from core.repositories.tasks_repo import IllegalTransition
from core.services import questions

WS = "org_1"
TASK = "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"


def _e(n=1, call="c1", state="proposed", ohash="h1", **over):
    return {
        "idemKey": f"{TASK}:{n}:{call}",
        "taskId": TASK,
        "attempt": n,
        "kind": "mail.send",
        "tool": "terminal",
        "operands": {"to": "a@b.c", "subject": "Q3"},
        "operandsHash": ohash,
        "state": state,
    } | over


async def test_effect_put_idempotent(table):
    first, created = await repo.put_effect(WS, _e())
    again, created_again = await repo.put_effect(WS, _e(state="intent"))
    assert (created, created_again) == (True, False)
    assert again["state"] == "proposed" == first["state"]


async def test_conditional_put_race_one_winner(table):
    results = await asyncio.gather(repo.put_effect(WS, _e(state="intent")), repo.put_effect(WS, _e(state="intent")))
    assert sorted(created for _, created in results) == [False, True]


async def test_effects_for_filters_by_hash_and_task(table):
    await repo.put_effect(WS, _e(1, "c1"))
    await repo.put_effect(WS, _e(2, "c2", ohash="other"))
    await repo.put_effect(WS, _e(1, "c1", taskId="9f14e45f-ceea-467a-9575-8a1b2c3d4e5f", idemKey="9f14e45f:1:c1"))
    assert [e["idemKey"] for e in await repo.effects_for(WS, TASK, "h1")] == [f"{TASK}:1:c1"]


async def test_state_moves_forward_only(table):
    await repo.put_effect(WS, _e())
    row = await repo.set_effect_state(WS, f"{TASK}:1:c1", to="approved", fields={"questionId": "q-1"})
    assert (row["state"], row["questionId"]) == ("approved", "q-1")
    with pytest.raises(IllegalTransition):
        await repo.set_effect_state(WS, f"{TASK}:1:c1", to="denied")  # an answer is final
    with pytest.raises(IllegalTransition):
        await repo.set_effect_state(WS, f"{TASK}:1:c1", to="intent")  # only use_approval enters intent
    assert await repo.use_approval(WS, f"{TASK}:1:c1", "h1") is True
    assert (await repo.get_effect(WS, f"{TASK}:1:c1"))["state"] == "intent"
    with pytest.raises(IllegalTransition):
        await repo.set_effect_state(WS, f"{TASK}:1:c1", to="approved")


async def test_effect_done_forward_only(table):
    await repo.put_effect(WS, _e(state="intent"))
    done = await repo.mark_effect_done(WS, f"{TASK}:1:c1", disposition="done", result_ref="ses:1")
    assert (done["state"], done["resultRef"]) == ("done", "ses:1")
    again = await repo.mark_effect_done(WS, f"{TASK}:1:c1", disposition="done", result_ref="ses:1")
    assert again["version"] == done["version"]  # the bridge's retry is a no-op
    with pytest.raises(IllegalTransition):
        await repo.mark_effect_done(WS, f"{TASK}:1:c1", disposition="unknown", result_ref=None)
    with pytest.raises(IllegalTransition):
        await repo.set_effect_state(WS, f"{TASK}:1:c1", to="intent")


async def test_done_needs_intent(table):
    await repo.put_effect(WS, _e())  # proposed: never approved
    with pytest.raises(IllegalTransition):
        await repo.mark_effect_done(WS, f"{TASK}:1:c1", disposition="done", result_ref=None)


async def _approved():
    await repo.put_effect(WS, _e())
    await repo.set_effect_state(WS, f"{TASK}:1:c1", to="approved", fields={"questionId": "q-1"})


async def test_use_approval_single_use_for_retries(table):
    await _approved()
    assert await repo.use_approval(WS, f"{TASK}:1:c1", "h1", new_effect=_e(2, "c7")) is True
    assert (await repo.get_effect(WS, f"{TASK}:2:c7"))["state"] == "intent"
    assert await repo.use_approval(WS, f"{TASK}:1:c1", "h1", new_effect=_e(3, "c9")) is False
    assert await repo.get_effect(WS, f"{TASK}:3:c9") is None  # the refused retry wrote nothing


async def test_landing_and_worker_retry_race_one_winner(table):
    """The backend's Landing and a Worker's retry press on one approval at once: exactly one proceeds."""
    await _approved()
    results = await asyncio.gather(
        repo.use_approval(WS, f"{TASK}:1:c1", "h1"),
        repo.use_approval(WS, f"{TASK}:1:c1", "h1", new_effect=_e(2, "c7")),
    )
    assert sorted(results) == [False, True]
    landed = (await repo.get_effect(WS, f"{TASK}:1:c1"))["state"] == "intent"
    retried = await repo.get_effect(WS, f"{TASK}:2:c7") is not None
    assert landed != retried


async def test_use_approval_refused_unless_approved(table):
    await repo.put_effect(WS, _e())  # proposed
    assert await repo.use_approval(WS, f"{TASK}:1:c1", "h1") is False
    assert await repo.use_approval(WS, f"{TASK}:1:c1", "h1", new_effect=_e(2, "c7")) is False
    await repo.set_effect_state(WS, f"{TASK}:1:c1", to="denied")
    assert await repo.use_approval(WS, f"{TASK}:1:c1", "h1", new_effect=_e(2, "c7")) is False


async def test_gate_sees_the_use_row(table):
    """End to end over the repo: one approval lets one retry through; the next retry asks again."""
    await _approved()
    retry = _e(2, "c7")
    assert questions.gate(retry, await repo.effects_for(WS, TASK, "h1")).decision == "proceed"
    await repo.use_approval(WS, f"{TASK}:1:c1", "h1", new_effect=retry)
    await repo.mark_effect_done(WS, f"{TASK}:2:c7", disposition="done", result_ref="ses:1")
    assert questions.gate(_e(3, "c9"), await repo.effects_for(WS, TASK, "h1")).decision == "needs_approval"
