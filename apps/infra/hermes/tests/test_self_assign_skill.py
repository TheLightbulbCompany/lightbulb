"""A room turn files its own task assigned to itself; the paperclip skill must not forbid that.

2026-10-20 (ISOA-881): the lightbulb skill said "create the task assigned to yourself" while the
paperclip skill's Critical Rules said "Never self-assign." Forge obeyed the second, filed the task
unassigned (`--status todo`, no assignee) and told the room "Paperclip won't let this room session
self-assign". Paperclip allows it: an agent assigning a NEW issue to itself passes `tasks:assign`
under the standard trust preset, and the delegation-cycle guard skips self-assignment.
"""

import pathlib
import subprocess

HERMES = pathlib.Path(__file__).resolve().parents[1]


def _built(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(HERMES / "build-skills.sh"), str(out)], check=True, capture_output=True)
    return out


def test_the_howto_files_your_own_task_assigned_to_yourself(tmp_path):
    howto = (_built(tmp_path) / "lightbulb/references/chat-vs-document.md").read_text()
    assert "assigned to yourself" in howto and '--assignee "<your agent id>"' in howto
