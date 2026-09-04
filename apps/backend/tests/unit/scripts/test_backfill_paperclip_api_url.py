"""Tests for the one-shot ``paperclip-claimed-api-key.json`` apiUrl backfill."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.backfill_paperclip_api_url import KEY_FILENAME, backfill

NEW = "http://paperclip.lightbulb-prod.local:3100"
OLD = "http://paperclip.isol8-prod.local:3100"


def _seed(root: Path, owner: str, agent: str, body: str) -> Path:
    path = root / owner / f"workspace-{agent}" / KEY_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    return path


def _doc(url: str, token: str = "tok") -> str:
    return json.dumps({"token": token, "agentId": "a", "companyId": "c", "apiUrl": url})


def test_dry_run_reports_stale_files_and_writes_nothing(tmp_path):
    stale = _seed(tmp_path, "org_a", "agent-1", _doc(OLD, token="keep-me"))
    current = _seed(tmp_path, "user_b", "agent-2", _doc(NEW))
    junk = _seed(tmp_path, "user_c", "agent-3", "{not json")

    summary = backfill(tmp_path, NEW, apply=False)

    assert summary.stale == [stale]
    assert summary.rewritten == []
    assert summary.current == 1
    assert [p for p, _ in summary.skipped] == [junk]
    assert json.loads(stale.read_text())["apiUrl"] == OLD  # untouched
    assert json.loads(current.read_text())["apiUrl"] == NEW


def test_apply_rewrites_only_stale_files_and_keeps_every_other_field(tmp_path):
    stale = _seed(tmp_path, "org_a", "agent-1", _doc(OLD, token="keep-me"))
    current = _seed(tmp_path, "user_b", "agent-2", _doc(NEW, token="other"))
    _seed(tmp_path, "user_c", "agent-3", "{not json")

    summary = backfill(tmp_path, NEW, apply=True)

    assert summary.rewritten == [stale]
    assert json.loads(stale.read_text()) == {"token": "keep-me", "agentId": "a", "companyId": "c", "apiUrl": NEW}
    assert json.loads(current.read_text()) == {"token": "other", "agentId": "a", "companyId": "c", "apiUrl": NEW}
    assert len(summary.skipped) == 1


def test_ignores_files_outside_agent_workspaces(tmp_path):
    """Only ``<owner>/workspace-<agent>/<KEY_FILENAME>`` is an identity file;
    the container-global ``workspace/`` dir and stray copies are not touched."""
    stray = tmp_path / "user_x" / "workspace" / KEY_FILENAME
    stray.parent.mkdir(parents=True)
    stray.write_text(_doc(OLD), encoding="utf-8")

    summary = backfill(tmp_path, NEW, apply=True)

    assert summary.stale == [] and summary.rewritten == []
    assert json.loads(stray.read_text())["apiUrl"] == OLD
