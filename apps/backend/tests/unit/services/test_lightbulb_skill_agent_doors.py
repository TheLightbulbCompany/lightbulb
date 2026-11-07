"""Pins the two doors the bundled ``lightbulb`` skill points agents at, after
prod 2026-09-03: Friday, unable to reach Paperclip, called ``/api/v1/teams/*``
through ``lightbulb-api.sh`` (a 401 by design — those routes take the owner's
browser session), read the skill's "a 401 from the script is a platform fault"
row literally, then created ``openclaw cron`` jobs because the skill listed
cron jobs next to Paperclip routines as a scheduler.

- ``lightbulb-api.sh`` refuses owner-only routes up front instead of sending
  them, so the agent reads "wrong door" rather than "platform fault".
- The skill names Paperclip routines as the ONLY scheduler and forbids the
  machine's own cron (``openclaw cron`` then; the Hermes host's cron now).

Prose is matched against a WHITESPACE-FLATTENED copy so re-wrapping does not
fail a test; only changing the words does.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[4] / "infra" / "hermes" / "skills-src" / "lightbulb"
SKILL = SKILL_DIR / "SKILL.md"
HELPER = SKILL_DIR / "scripts" / "lightbulb-api.sh"


def _skill_text() -> str:
    """The whole skill: SKILL.md plus every references/*.md.

    The skill was SPLIT into a thin SKILL.md + references/ read on demand
    (#2367). These tests pin what the skill TELLS THE AGENT, which is the union
    of both — an agent that follows the routing table reads the reference too.
    Asserting against SKILL.md alone made a pure file-layout change look like a
    deleted rule, and would do so again on the next split.
    """
    parts = [SKILL.read_text()]
    parts += [f.read_text() for f in sorted((SKILL_DIR / "references").glob("*.md"))]
    return "\n".join(parts)


def _flat() -> str:
    return " ".join(_skill_text().split())


# ---------------------------------------------------------------------------
# lightbulb-api.sh — owner-only routes are refused before any request is sent
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_curl(tmp_path):
    """A ``curl`` on PATH that records its argv and answers 200."""
    log = tmp_path / "curl.log"
    shim = tmp_path / "curl"
    shim.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> "{log}"\nprintf "ok\\n200"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return log


def _run(path: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "OPENCLAW_HOOK_TOKEN": "hook-token",
        "ISOL8_API_URL": "https://api.example.test",
    }
    return subprocess.run(["bash", str(HELPER), "GET", path], capture_output=True, text=True, env=env)


def test_helper_refuses_an_owner_only_teams_route_without_sending(fake_curl, tmp_path):
    result = _run("/api/v1/teams/routines", tmp_path)

    assert result.returncode != 0
    assert "owner-only route" in result.stderr
    assert "Paperclip" not in result.stderr  # no profile has a Paperclip key (task 8b.2)
    assert not fake_curl.exists(), "the request must never reach curl"


def test_helper_sends_a_hook_token_route(fake_curl, tmp_path):
    result = _run("/api/v1/company/provisioning/u/org_x/catalog", tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("200")
    argv = fake_curl.read_text().splitlines()
    assert "https://api.example.test/api/v1/company/provisioning/u/org_x/catalog" in argv
    assert "Authorization: Bearer hook-token" in argv


def test_helper_allows_the_public_platform_info_route(fake_curl, tmp_path):
    result = _run("/api/v1/platform-info", tmp_path)

    assert result.returncode == 0, result.stderr
    assert fake_curl.exists()


# ---------------------------------------------------------------------------
# SKILL.md — one scheduler, and the Teams API is not an agent door
# ---------------------------------------------------------------------------


def test_the_skill_names_paperclip_routines_as_the_only_scheduler():
    flat = _flat()
    assert "they are the ONLY scheduler" in flat
    assert "Never the machine's own cron" in flat
    assert "Cron jobs and Paperclip routines" not in flat


def test_the_skill_says_the_teams_api_is_owner_only():
    flat = _flat()
    assert "Its API (`/api/v1/teams/*`) is owner-only" in flat
    assert "never by opening the Teams UI in your browser tool" in flat


def test_the_401_row_is_scoped_to_hook_token_routes():
    flat = _flat()
    assert "A 401 from the script itself on a `/u/<owner>` route is a platform fault" in flat
    assert "refuses those routes before sending" in flat


def test_the_skill_describes_the_generic_starter_roster_and_optional_classics():
    flat = _flat()
    assert "Fresh workspaces start with six agents" in flat
    for role in ("CEO", "Engineering", "Finance", "Growth", "Analytics", "Customer Operations"):
        assert role in flat
    assert "Names are user-renamable" in flat
    assert "Friday and Atlas are not starter identities" in flat
