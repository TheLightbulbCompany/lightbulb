"""Tests for the Paperclip instance-flag convergence.

The pure ``plan_changes`` function, the shape of the write, and the
write-only-when-different rule. The RDS Data API client is a mock throughout.
"""

import json
from unittest.mock import MagicMock

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


@pytest.mark.parametrize("stored", [None, {}, {"enableNativeRunner": True}])
def test_ensure_writes_once_when_something_differs(monkeypatch, stored):
    client = _client(monkeypatch, stored)

    assert set(mod.ensure_instance_flags("dev")) == set(DESIRED_FLAGS)
    sqls = [call.kwargs["sql"] for call in client.execute_statement.call_args_list]
    assert sqls == [mod.SELECT_SQL, UPSERT_SQL]


async def test_startup_task_retries_then_counts_a_failure_without_raising(monkeypatch):
    attempts = MagicMock(side_effect=RuntimeError("cluster resuming"))
    metrics: list[str] = []

    monkeypatch.setattr(mod, "ensure_instance_flags", attempts)
    monkeypatch.setattr(mod, "_RETRY_PAUSE_S", 0)
    monkeypatch.setattr(mod, "put_metric", lambda name, **_: metrics.append(name))

    await mod.converge_at_startup("dev")

    assert attempts.call_count == mod._ATTEMPTS
    assert metrics == ["paperclip.instance_flags.failed"]
