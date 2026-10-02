"""Tests for the Paperclip instance-flag convergence.

The pure ``plan_changes`` function, the shape of the write, and the
write-only-when-different rule. The RDS Data API client is a mock throughout.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import paperclip_instance_flags as mod
from core.services.paperclip_instance_flags import DESIRED_FLAGS, UPSERT_SQL, plan_changes

DB = {"resourceArn": "arn", "secretArn": "secret", "database": "paperclip"}


def _client(monkeypatch, stored: dict | None) -> MagicMock:
    """A Data API client whose SELECT returns ``stored`` (None: no row)."""
    client = MagicMock()
    records = [] if stored is None else [[{"stringValue": json.dumps(stored)}]]
    client.execute_statement.return_value = {"records": records}
    monkeypatch.setattr(mod, "rds_data_client", lambda: client)
    monkeypatch.setattr(mod, "database", lambda env: DB)
    return client


def test_no_op_when_flags_already_stored():
    assert plan_changes({"enableNativeRunner": False, "enablePipelines": True}, {"enableNativeRunner": False}) == {}


def test_plans_a_change_for_a_different_or_unset_key():
    assert plan_changes({"enableNativeRunner": True}, {"enableNativeRunner": False}) == {
        "enableNativeRunner": (True, False)
    }
    assert plan_changes({"enablePipelines": True}, {"enableNativeRunner": False}) == {
        "enableNativeRunner": (None, False)
    }


def test_missing_row_plans_every_key():
    assert plan_changes(None, {"a": True, "b": False}) == {"a": (None, True), "b": (None, False)}


def test_write_is_parameterised_and_merges():
    client = MagicMock()
    mod.write(client, DB, DESIRED_FLAGS)

    kwargs = client.execute_statement.call_args.kwargs
    assert kwargs["sql"] == UPSERT_SQL
    assert kwargs["parameters"] == [{"name": "flags", "value": {"stringValue": json.dumps(DESIRED_FLAGS)}}]
    # The flags travel as a bound parameter, never inside the statement text.
    assert "CAST(:flags AS jsonb)" in UPSERT_SQL
    assert not any(key in UPSERT_SQL for key in DESIRED_FLAGS)
    # A merge, not an overwrite: every other stored key survives.
    assert "instance_settings.experimental || EXCLUDED.experimental" in UPSERT_SQL


def test_ensure_does_not_write_when_nothing_differs(monkeypatch):
    client = _client(monkeypatch, dict(DESIRED_FLAGS))

    assert mod.ensure_instance_flags("dev") == {}
    client.execute_statement.assert_called_once()
    assert client.execute_statement.call_args.kwargs["sql"] == mod.SELECT_SQL


def test_cases_and_external_objects_are_desired_on():
    assert DESIRED_FLAGS["enableCases"] is True
    assert DESIRED_FLAGS[mod.TOKEN_GATED_FLAG] is True


@pytest.mark.parametrize("stored", [None, {}, {key: not want for key, want in DESIRED_FLAGS.items()}])
def test_ensure_writes_once_when_something_differs(monkeypatch, stored):
    client = _client(monkeypatch, stored)

    assert set(mod.ensure_instance_flags("dev")) == set(DESIRED_FLAGS)
    sqls = [call.kwargs["sql"] for call in client.execute_statement.call_args_list]
    assert sqls == [mod.SELECT_SQL, UPSERT_SQL]


async def test_startup_task_retries_then_counts_a_failure_without_raising(monkeypatch):
    attempts = MagicMock(side_effect=RuntimeError("cluster resuming"))
    metrics: list[str] = []

    monkeypatch.setattr(mod, "ensure_instance_flags", attempts)
    monkeypatch.setattr(mod, "_flags_to_hold_back", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(mod, "_RETRY_PAUSE_S", 0)
    monkeypatch.setattr(mod, "put_metric", lambda name, **_: metrics.append(name))

    await mod.converge_at_startup("dev")

    assert attempts.call_count == mod._ATTEMPTS
    assert metrics == ["paperclip.instance_flags.failed"]


# --- the GitHub token sweep runs first, and gates enableExternalObjects -----------------------

GATED = mod.TOKEN_GATED_FLAG
OTHERS = {key: want for key, want in DESIRED_FLAGS.items() if key != GATED}


def _sweep(monkeypatch, outcomes: dict[str, str] | Exception, events: list[str]):
    """Replace the token sweep; ``events`` records when it ran relative to the SQL."""
    from core.services import paperclip_github_token as tokens

    async def fake_sweep():
        events.append("sweep")
        if isinstance(outcomes, Exception):
            raise outcomes
        for pair in outcomes.items():
            yield pair

    monkeypatch.setattr(tokens, "sweep", fake_sweep)


def _startup_client(monkeypatch, stored: dict | None, events: list[str]) -> MagicMock:
    client = _client(monkeypatch, stored)
    answer = client.execute_statement.return_value
    client.execute_statement.side_effect = lambda **kwargs: events.append(kwargs["sql"]) or answer
    return client


def _written(client) -> dict:
    (call,) = [c for c in client.execute_statement.call_args_list if c.kwargs["sql"] == UPSERT_SQL]
    return json.loads(call.kwargs["parameters"][0]["value"]["stringValue"])


# Settled outcomes: in step, or deliberately left alone. None of them holds the flag back.
CLEAN = {"org_a": "unchanged", "org_b": "no_company", "org_c": "foreign_secret", "org_d": "inactive_secret"}


async def test_startup_sweeps_the_tokens_then_applies_every_flag_in_one_write(monkeypatch):
    events: list[str] = []
    _sweep(monkeypatch, {**CLEAN, "org_e": "created", "org_f": "deleted", "org_g": "absent"}, events)
    client = _startup_client(monkeypatch, {"enableNativeRunner": True}, events)

    await mod.converge_at_startup("dev")

    assert events == ["sweep", mod.SELECT_SQL, UPSERT_SQL]
    assert _written(client) == DESIRED_FLAGS


@pytest.mark.parametrize(
    "outcomes",
    [{**CLEAN, "org_x": "failed"}, {"org_x": "unsettled"}, {"org_x": "conflict"}, RuntimeError("scan failed")],
    ids=["one-owner-failed", "unsettled", "conflict", "sweep-raised"],
)
async def test_an_unclean_sweep_holds_external_objects_back_and_applies_the_rest(monkeypatch, outcomes):
    events: list[str] = []
    metrics: list[str] = []
    _sweep(monkeypatch, outcomes, events)
    client = _startup_client(monkeypatch, {"enableNativeRunner": True}, events)
    monkeypatch.setattr(mod, "put_metric", lambda name, **_: metrics.append(name))

    await mod.converge_at_startup("dev")

    assert events == ["sweep", mod.SELECT_SQL, UPSERT_SQL]
    assert _written(client) == OTHERS  # the gated key is not in the write at all
    assert metrics == ["paperclip.instance_flags.held_back"]


async def test_an_unclean_sweep_leaves_external_objects_on_when_it_is_already_on(monkeypatch):
    events: list[str] = []
    metrics: list[str] = []
    _sweep(monkeypatch, {"org_x": "failed"}, events)
    _startup_client(monkeypatch, dict(DESIRED_FLAGS), events)
    monkeypatch.setattr(mod, "put_metric", lambda name, **_: metrics.append(name))

    await mod.converge_at_startup("dev")

    # Nothing differs, so nothing is written (a merge could not switch it off anyway),
    # and it is not reported as held back.
    assert events == ["sweep", mod.SELECT_SQL]
    assert metrics == []


async def test_a_sweep_that_overruns_its_time_limit_holds_the_flag_back(monkeypatch):
    import asyncio

    from core.services import paperclip_github_token as tokens

    async def stuck():
        await asyncio.sleep(60)
        yield "org_a", "unchanged"

    monkeypatch.setattr(tokens, "sweep", stuck)
    monkeypatch.setattr(mod, "_SWEEP_TIMEOUT_S", 0.01)

    assert await mod._flags_to_hold_back() == frozenset({GATED})
