"""The Hermes image's bundled skills are BUILT, not checked in: apps/infra/hermes/
build-skills.sh derives them from apps/infra/hermes/skills-src (token swaps only, no overlay). So
this suite runs that script and asserts on its OUTPUT -- the same artefact the image bakes
-- rather than on a second copy of the tree in the repo.
"""

import subprocess
from pathlib import Path

import pytest

# NOTE: parents[4] from this file is apps/backend/tests/unit/services -> apps/backend/tests/unit
# -> ... ; the repo root is one hop further up (verified with Path(...).resolve().parents[i]).
# The brief's own snippet used parents[4], which resolves to the `apps/` dir, not the repo
# root, and silently doubled "apps" in the path below. Fixed to parents[5].
REPO = Path(__file__).resolve().parents[5]
BUILD = REPO / "apps/infra/hermes/build-skills.sh"

#: Names that must not survive the swap, with what replaced them.
RETIRED = {
    "`exec`": "`terminal`",
    "`todo`": "`todo_list`",
    "sessions_spawn": "(dropped — no ACP harness on Hermes)",
    "/app/skills/": "/opt/hermes-skills/",
    "openclaw backup create": "(dropped)",
    "openclaw/invite-prompt": "(retired onboarding workflow)",
    "always-on container": "(AgentCore wakes on demand)",
    "2 vCPU / 8 GB": "(retired ECS sizing)",
    "dedicated container": "dedicated AWS Bedrock AgentCore runtime",
}

# Paperclip's `--status todo` is a STATUS value, never renamed (see the brief's own
# mapping table: "`todo` (the tool) -> `todo_list` -- Paperclip's `--status todo` is a
# STATUS, never renamed"). Every occurrence of the exact "`todo`" span left in this repo
# snapshot is that status value, not the retired OpenClaw todo tool, so the check is
# scoped away from the paperclip skill tree to avoid a false failure on protected text.
_TODO_NEEDLE = "`todo`"
_TODO_STATUS_EXEMPT_PREFIX = "paperclip/"


@pytest.fixture(scope="module")
def skills(tmp_path_factory) -> Path:
    """The built tree, exactly as `docker build` bakes it into /opt/hermes-skills."""
    out = tmp_path_factory.mktemp("hermes-skills") / "skills"
    subprocess.run([str(BUILD), str(out)], check=True, capture_output=True)
    return out


def _texts(skills: Path):
    return {path: path.read_text() for path in skills.rglob("*.md")}


def test_the_build_produces_the_hermes_image_tree(skills):
    assert (skills / "lightbulb/SKILL.md").exists()
    assert not (skills / "paperclip").exists()  # Paperclip exit (task 8b.2)
    # The one thing the build deliberately does not ship: the engineer-facing
    # MAINTENANCE.md files.
    assert not list(skills.rglob("MAINTENANCE.md"))


def test_no_retired_tool_name_survives(skills):
    offenders = {
        f"{path.relative_to(skills)}: {needle} -> {replacement}"
        for path, text in _texts(skills).items()
        for needle, replacement in RETIRED.items()
        if needle in text
        if not (needle == _TODO_NEEDLE and str(path.relative_to(skills)).startswith(_TODO_STATUS_EXEMPT_PREFIX))
    }
    assert not offenders, offenders


def test_our_own_plugin_tool_names_are_still_referenced(skills):
    """`lightbulb_local_sessions` is named in the skill BODY, so a rename has to land here.

    `send_room_message` and `hire_suggestion` are not: no skill text names them (the agent
    just has the tools). They are pinned in the source tree's MAINTENANCE.md anchor list
    instead, which is what a renamer is meant to read -- asserting them against the built
    tree only ever matched that same maintainer note, which the image does not ship.
    """
    built = "\n".join(_texts(skills).values())
    assert "lightbulb_local_sessions" in built
    assert "request_approval" in built  # the one way the skills tell an agent to ask a person

    anchors = (REPO / "apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md").read_text()
    assert "send_room_message" in anchors
    assert "hire_suggestion" in anchors


def test_there_is_no_overlay_shadowing_the_source(skills):
    """skills-src is the only copy. A per-file overlay replaced its source file in the
    image and silently shipped stale text (missing whole sections) for weeks; wording a
    token swap cannot express belongs in skills-src itself."""
    hermes = REPO / "apps/infra/hermes"
    assert not (hermes / "skills-overlay").exists(), "write Hermes wording in skills-src"
    assert "overlay" not in (hermes / "build-skills.sh").read_text().split("set -euo pipefail")[1]
    source = hermes / "skills-src"
    shipped = {p.relative_to(source) for p in source.rglob("*") if p.is_file() and p.name != "MAINTENANCE.md"}
    assert shipped == {p.relative_to(skills) for p in skills.rglob("*") if p.is_file()}
