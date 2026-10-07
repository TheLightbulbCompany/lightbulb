"""Unit tests for the reapply-hermes-skills-dirs ops script.

The S3 reads/writes are ``hermes_profiles.read_profile_config``/``patch_profile_config`` --
already covered by ``test_hermes_profiles_patch.py``. What matters here is this script's own
logic: skip an agent whose profile already carries the current default (idempotent, no needless
revision bump), patch the ones that don't, and keep going past a failing owner/agent.
"""

import argparse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import scripts.reapply_hermes_skills_dirs as mod


def test_module_imports_and_exposes_main():
    assert hasattr(mod, "main")


# --------------------------------------------------------------------------- #
# _apply_one — compare-then-patch, dry-run never writes
# --------------------------------------------------------------------------- #


def test_apply_one_is_a_noop_when_already_current(monkeypatch):
    monkeypatch.setattr(
        mod, "read_profile_config", lambda o, a: {"skills": {"external_dirs": list(mod.SKILLS_EXTERNAL_DIRS)}}
    )
    patch_mock = MagicMock()
    monkeypatch.setattr(mod, "patch_profile_config", patch_mock)

    assert mod._apply_one("org_a", "agent_1", dry_run=False) == "current"
    patch_mock.assert_not_called()


def test_apply_one_patches_the_current_default_when_stale(monkeypatch):
    monkeypatch.setattr(mod, "read_profile_config", lambda o, a: {"skills": {"external_dirs": ["/opt/hermes-skills"]}})
    calls = []
    monkeypatch.setattr(mod, "patch_profile_config", lambda o, a, p: calls.append((o, a, p)))

    assert mod._apply_one("org_a", "agent_1", dry_run=False) == "updated"
    assert calls == [("org_a", "agent_1", {"skills": {"external_dirs": list(mod.SKILLS_EXTERNAL_DIRS)}})]


def test_apply_one_treats_a_missing_skills_block_as_stale(monkeypatch):
    monkeypatch.setattr(mod, "read_profile_config", lambda o, a: {})
    calls = []
    monkeypatch.setattr(mod, "patch_profile_config", lambda o, a, p: calls.append(p))

    assert mod._apply_one("org_a", "agent_1", dry_run=False) == "updated"
    assert calls[0]["skills"]["external_dirs"] == list(mod.SKILLS_EXTERNAL_DIRS)


def test_apply_one_dry_run_never_calls_the_patch_path(monkeypatch):
    monkeypatch.setattr(mod, "read_profile_config", lambda o, a: {"skills": {"external_dirs": []}})
    patch_mock = MagicMock()
    monkeypatch.setattr(mod, "patch_profile_config", patch_mock)

    result = mod._apply_one("org_a", "agent_1", dry_run=True)
    assert result.startswith("would-update")
    patch_mock.assert_not_called()


# --------------------------------------------------------------------------- #
# _targets — --owner-id vs --all (agentcore-only)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_targets_single_owner_skips_the_fleet_scan():
    scan_mock = AsyncMock()
    with patch("core.repositories.container_repo.scan_all", scan_mock):
        targets = await mod._targets(argparse.Namespace(owner_id="org_x", all=False))
    assert targets == ["org_x"]
    scan_mock.assert_not_called()


@pytest.mark.asyncio
async def test_targets_all_skips_rows_with_nothing_to_roll():
    """A row without a runtime ARN has no profiles to reapply skills dirs into, and a row
    without an owner_id is not a workspace at all (the containers table also holds
    reserved-key rows). There is no OTHER transport left to filter out."""
    rows = [
        {"owner_id": "org_live", "agentcore_runtime_arn": "arn:x"},
        {"owner_id": "org_unprovisioned"},
        {"agentcore_runtime_arn": "arn:y"},
    ]
    with patch("core.repositories.container_repo.scan_all", AsyncMock(return_value=rows)):
        targets = await mod._targets(argparse.Namespace(owner_id=None, all=True))
    assert targets == ["org_live"]


# --------------------------------------------------------------------------- #
# main() — per-agent reporting, continues past failures
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_main_reports_current_and_updated_per_agent():
    responses = {
        "agent_1": {"skills": {"external_dirs": list(mod.SKILLS_EXTERNAL_DIRS)}},
        "agent_2": {"skills": {"external_dirs": ["/opt/hermes-skills"]}},
    }
    calls = []
    with (
        patch.object(mod, "_targets", AsyncMock(return_value=["org_x"])),
        patch.object(mod, "load_agent_roster", AsyncMock(return_value=[{"id": "agent_1"}, {"id": "agent_2"}])),
        patch.object(mod, "read_profile_config", lambda o, a: responses[a]),
        patch.object(mod, "patch_profile_config", lambda o, a, p: calls.append((a, p))),
    ):
        rc = await mod.main(["--owner-id", "org_x"])

    assert rc == 0
    assert calls == [("agent_2", {"skills": {"external_dirs": list(mod.SKILLS_EXTERNAL_DIRS)}})]


@pytest.mark.asyncio
async def test_main_dry_run_never_patches(capsys):
    with (
        patch.object(mod, "_targets", AsyncMock(return_value=["org_x"])),
        patch.object(mod, "load_agent_roster", AsyncMock(return_value=[{"id": "agent_1"}])),
        patch.object(mod, "read_profile_config", lambda o, a: {"skills": {"external_dirs": []}}),
        patch.object(mod, "patch_profile_config", MagicMock()) as patch_mock,
    ):
        rc = await mod.main(["--owner-id", "org_x", "--dry-run"])

    assert rc == 0
    patch_mock.assert_not_called()
    assert "would-update" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_main_continues_past_a_roster_lookup_failure_and_exits_nonzero():
    async def _roster(owner_id):
        if owner_id == "org_a":
            raise RuntimeError("roster unavailable")
        return [{"id": "agent_1"}]

    with (
        patch.object(mod, "_targets", AsyncMock(return_value=["org_a", "org_b"])),
        patch.object(mod, "load_agent_roster", _roster),
        patch.object(
            mod, "read_profile_config", lambda o, a: {"skills": {"external_dirs": list(mod.SKILLS_EXTERNAL_DIRS)}}
        ),
        patch.object(mod, "patch_profile_config", MagicMock()),
    ):
        rc = await mod.main(["--all"])

    assert rc == 1  # org_a's roster lookup failed; org_b was still visited


@pytest.mark.asyncio
async def test_main_continues_past_a_per_agent_patch_failure_and_exits_nonzero(capsys):
    def _patch(owner_id, agent_id, patch_body):
        if agent_id == "agent_bad":
            raise RuntimeError("S3 outage")

    with (
        patch.object(mod, "_targets", AsyncMock(return_value=["org_x"])),
        patch.object(mod, "load_agent_roster", AsyncMock(return_value=[{"id": "agent_bad"}, {"id": "agent_ok"}])),
        patch.object(mod, "read_profile_config", lambda o, a: {"skills": {"external_dirs": []}}),
        patch.object(mod, "patch_profile_config", _patch),
    ):
        rc = await mod.main(["--owner-id", "org_x"])

    assert rc == 1
    assert "org_x agent_ok updated" in capsys.readouterr().out
