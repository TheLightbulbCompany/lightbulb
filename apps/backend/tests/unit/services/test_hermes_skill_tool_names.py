from pathlib import Path

# NOTE: parents[4] from this file is apps/backend/tests/unit/services -> apps/backend/tests/unit
# -> ... ; the repo root is one hop further up (verified with Path(...).resolve().parents[i]).
# The brief's own snippet used parents[4], which resolves to the `apps/` dir, not the repo
# root, and silently doubles "apps" in SKILLS below. Fixed to parents[5].
REPO = Path(__file__).resolve().parents[5]
SKILLS = REPO / "apps/infra/hermes/skills"

#: Names that must not survive the swap, with what replaced them.
RETIRED = {
    "`exec`": "`terminal`",
    "`todo`": "`todo_list`",
    "sessions_spawn": "(dropped — no ACP harness on Hermes)",
    "/app/skills/": "/opt/hermes-skills/",
    "openclaw backup create": "(dropped)",
}

# Paperclip's `--status todo` is a STATUS value, never renamed (see the brief's own
# mapping table: "`todo` (the tool) -> `todo_list` -- Paperclip's `--status todo` is a
# STATUS, never renamed"). Every occurrence of the exact "`todo`" span left in this repo
# snapshot is that status value, not the retired OpenClaw todo tool, so the check is
# scoped away from the paperclip skill tree to avoid a false failure on protected text.
_TODO_NEEDLE = "`todo`"
_TODO_STATUS_EXEMPT_PREFIX = "paperclip/"


def _texts():
    return {path: path.read_text() for path in SKILLS.rglob("*.md")}


def test_the_skills_were_copied_into_the_hermes_image_tree():
    assert (SKILLS / "lightbulb/SKILL.md").exists()
    assert (SKILLS / "paperclip/SKILL.md").exists()


def test_no_retired_tool_name_survives():
    offenders = {
        f"{path.relative_to(SKILLS)}: {needle} -> {replacement}"
        for path, text in _texts().items()
        for needle, replacement in RETIRED.items()
        if needle in text
        if not (needle == _TODO_NEEDLE and str(path.relative_to(SKILLS)).startswith(_TODO_STATUS_EXEMPT_PREFIX))
    }
    assert not offenders, offenders


def test_our_own_plugin_tool_names_are_still_referenced():
    joined = "\n".join(_texts().values())
    assert "lightbulb_local_sessions" in joined
    assert "send_room_message" in joined


def test_the_paperclip_helper_path_points_at_the_baked_skills_dir():
    text = (SKILLS / "paperclip/SKILL.md").read_text()
    assert "/opt/hermes-skills/paperclip/scripts/paperclip-api.sh" in text
