"""A room turn files its own task assigned to itself; the paperclip skill must not forbid that.

2026-10-20 (ISOA-881): the lightbulb skill said "create the task assigned to yourself" while the
paperclip skill's Critical Rules said "Never self-assign." Forge obeyed the second, filed the task
unassigned (`--status todo`, no assignee) and told the room "Paperclip won't let this room session
self-assign". Paperclip allows it: an agent assigning a NEW issue to itself passes `tasks:assign`
under the standard trust preset, and the delegation-cycle guard skips self-assignment.
"""

import json
import os
import pathlib
import subprocess

HERMES = pathlib.Path(__file__).resolve().parents[1]


def _built(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(HERMES / "build-skills.sh"), str(out)], check=True, capture_output=True)
    return out


def test_no_skill_text_forbids_filing_your_own_task(tmp_path):
    out = _built(tmp_path)
    for rel in ("paperclip/SKILL.md", "paperclip/references/delegation.md"):
        text = (out / rel).read_text()
        assert "Never self-assign" not in text, rel
        assert "Never take a task that is not yours" in text, rel
    assert "`--assignee` yourself, is allowed" in (out / "paperclip/SKILL.md").read_text()
    howto = (out / "lightbulb/references/chat-vs-document.md").read_text()
    assert "assigned to yourself" in howto and '--assignee "<your agent id>"' in howto


def test_assigned_create_sends_the_assignee_and_starts_it(tmp_path):
    script = _built(tmp_path) / "paperclip/scripts/paperclip-api.sh"
    env = {**os.environ, "HERMES_SESSION_KEY": "", "LIGHTBULB_ROOM_SESSION_KEY": ""}
    body = subprocess.run(
        [str(script), "create", "company-1", "--title", "Trace CTAs", "--assignee", "agent-self", "--dry-run"],
        check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env,
    ).stdout
    sent = json.loads(body)
    assert sent["assigneeAgentId"] == "agent-self" and sent["status"] == "todo"
