"""scripts/carry_over_codex_grant.py — extracting a migrated owner's live ChatGPT grant.

The litestream restore is mocked (it is a subprocess against real S3); what it "restores" is a
REAL temp SQLite file with the row shape OpenClaw >= 9.3 writes, so the extraction is exercised
against the actual schema rather than a stub.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import carry_over_codex_grant as mod

OWNER = "org_carry"
ACCESS = "access-token-value-never-logged"
REFRESH = "refresh-token-value-never-logged"


def _store(profiles: dict) -> str:
    return json.dumps({"version": 1, "profiles": profiles})


def _openai_profile(**overrides) -> dict:
    return {
        "type": "oauth",
        "provider": "openai",
        "email": "founder@example.com",
        "access": ACCESS,
        "refresh": REFRESH,
        "accountId": "acct-1",
        "chatgptPlanType": "pro",
        "expires": 1789000000000,
        **overrides,
    }


def _write_db(path: Path, value_json: str | None, *, table: bool = True) -> None:
    conn = sqlite3.connect(path)
    if table:
        conn.execute(
            "CREATE TABLE config_machine_state("
            "state_key TEXT NOT NULL PRIMARY KEY, value_json TEXT NOT NULL, updated_at_ms INTEGER NOT NULL)"
        )
        if value_json is not None:
            conn.execute("INSERT INTO config_machine_state VALUES ('authProfiles.store', ?, 1)", (value_json,))
    conn.commit()
    conn.close()


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """litestream present, bucket configured, ``stage_codex_auth`` recorded — and the restore
    replaced by a writer of whatever ``db_body`` holds, into the real tmpdir the script chose."""
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/local/bin/litestream")
    monkeypatch.setattr("core.config.settings.OWNER_STATE_BUCKET", "lightbulb-test-replicas")
    staged = MagicMock()
    monkeypatch.setattr("core.services.hermes_profiles.stage_codex_auth", staged)

    state = {"body": _store({"openai:default": _openai_profile()}), "table": True, "dest": None}

    def _fake_restore(_binary, _url, dest: Path) -> None:
        state["dest"] = dest
        _write_db(dest, state["body"], table=state["table"])

    monkeypatch.setattr(mod, "_restore", _fake_restore)
    state["staged"] = staged
    return state


def test_happy_path_stages_only_the_two_tokens(wired):
    assert mod.main(["--owner-id", OWNER]) == 0

    wired["staged"].assert_called_once_with(OWNER, {"access_token": ACCESS, "refresh_token": REFRESH})


def test_dry_run_reports_the_summary_and_stages_nothing(wired, capsys):
    assert mod.main(["--owner-id", OWNER, "--dry-run"]) == 0

    wired["staged"].assert_not_called()
    out = capsys.readouterr().out
    assert json.loads(out.splitlines()[0]) == {
        "profile_key": "openai:default",
        "email": "founder@example.com",
        "has_access": True,
        "has_refresh": True,
        "expires_at": 1789000000000,
    }
    assert ACCESS not in out
    assert REFRESH not in out


def test_a_renamed_single_openai_profile_is_still_found(wired):
    wired["body"] = _store({"openai:personal": _openai_profile()})

    assert mod.main(["--owner-id", OWNER]) == 0

    wired["staged"].assert_called_once()


@pytest.mark.parametrize(
    "body,table",
    [
        (None, True),  # no authProfiles.store row — the container never held a grant
        (_store({}), True),  # store present, no profiles
        (_store({"openai:default": _openai_profile(refresh="")}), True),  # spent/absent refresh
        (None, False),  # the table itself was never created
    ],
)
def test_nothing_usable_exits_non_zero_and_stages_nothing(wired, body, table):
    wired["body"], wired["table"] = body, table

    assert mod.main(["--owner-id", OWNER]) == 1

    wired["staged"].assert_not_called()


def test_a_missing_litestream_binary_fails_before_anything_else(wired, monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)

    assert mod.main(["--owner-id", OWNER]) == 1

    assert wired["dest"] is None
    wired["staged"].assert_not_called()


def test_the_tmpdir_is_always_removed(wired):
    assert mod.main(["--owner-id", OWNER]) == 0

    assert not wired["dest"].parent.exists()


def test_tokens_never_reach_a_log_record(wired, caplog, capsys):
    """The values are the one thing this script must never emit — not on the happy path, and not
    in the failure message either."""
    with caplog.at_level(logging.DEBUG):
        assert mod.main(["--owner-id", OWNER]) == 0
        wired["body"] = _store({"openai:default": _openai_profile(refresh="")})
        assert mod.main(["--owner-id", OWNER]) == 1

    emitted = caplog.text + capsys.readouterr().out
    assert ACCESS not in emitted
    assert REFRESH not in emitted
