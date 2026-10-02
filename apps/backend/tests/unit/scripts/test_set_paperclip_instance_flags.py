"""Planning-logic tests for the Paperclip instance-flag script.

Only the pure ``plan_changes`` function and the shape of the write are tested;
the RDS Data API calls are thin wrappers run by hand with ``--apply``.
"""

import json
from unittest.mock import MagicMock

from scripts.set_paperclip_instance_flags import DESIRED_FLAGS, UPSERT_SQL, _write, plan_changes


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
    _write(client, {"resourceArn": "arn", "secretArn": "secret", "database": "paperclip"}, DESIRED_FLAGS)

    kwargs = client.execute_statement.call_args.kwargs
    assert kwargs["sql"] == UPSERT_SQL
    assert kwargs["parameters"] == [{"name": "flags", "value": {"stringValue": json.dumps(DESIRED_FLAGS)}}]
    # The flags travel as a bound parameter, never inside the statement text.
    assert "CAST(:flags AS jsonb)" in UPSERT_SQL
    assert not any(key in UPSERT_SQL for key in DESIRED_FLAGS)
    # A merge, not an overwrite: every other stored key survives.
    assert "instance_settings.experimental || EXCLUDED.experimental" in UPSERT_SQL
