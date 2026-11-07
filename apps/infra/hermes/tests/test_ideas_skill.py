"""The lightbulb skill's short Ideas reference, matching the backend's message and the plugin tool."""

import pathlib
import subprocess

HERMES = pathlib.Path(__file__).resolve().parents[1]
BACKEND = HERMES.parents[1] / "backend"


def test_the_skill_routes_ideas_to_a_short_reference(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(HERMES / "build-skills.sh"), str(out)], check=True, capture_output=True)
    skill = (out / "lightbulb/SKILL.md").read_text()
    row = next(line for line in skill.splitlines() if "`references/ideas.md`" in line)
    assert row.startswith("|") and "suggest_idea" in row
    # A Paperclip run never sees "Let's do it!": the Work task's own sections route here too.
    assert "Needs" in row and "Done means" in row
    ideas = (out / "lightbulb/references/ideas.md").read_text()
    assert len(ideas.splitlines()) < 48
    assert "Do it like any request" in ideas
    # Pre-flight: needs against what is connected, one ask for everything missing.
    assert "Read the Work item's **Needs**" in ideas and "ask once, for all of it" in ideas
    assert "`connected_integrations`" in ideas
    # Built-in services are never linked.
    assert "built in" in ideas
    # The run is not bound to the DM: it reaches the person through the room named in From.
    assert "lightbulb-post.sh --room <roomId>" in ideas and "**From**" in ideas
    # The DM record of a tracked idea is never a second start.
    assert "never start it again" in ideas
    # Statuses: project tasks filed in the project as todo.
    assert "`--project <id>` and `--status todo`" in ideas
    assert "one-line next action" in ideas
    # A step only a person can do is a task for the person, never a question in chat.
    assert "--assignee-user <userId>" in ideas and "blocked by that task" in ideas
    assert "user id in **From**" in ideas
    # Done means proof.
    assert "one line per deliverable with a link" in ideas and "**Done means**" in ideas
    # The box's limits.
    assert "30 minutes" in ideas and "50 a day" in ideas and "no phone" in ideas
    # Browser steps from any Work run go through an approval.
    assert "from any Work run" in ideas and "request_approval titled" in ideas and "continue when the approval wakes you" in ideas
    assert "assigned to you and this run is it" in ideas and "after checking each task's proof" in ideas
    assert "lightbulb-connect.sh link" in ideas and "comment on the task" in ideas
    # Waiting on a connect: the task is `blocked`, or Paperclip re-dispatches the run every 30 s (dev, ISS-10).
    assert "set the task `blocked`" in ideas and "re-runs you every 30 s" in ideas
    assert "never paste a Paperclip URL" in ideas
    assert "sign-in (live browser only)" in ideas and "`request_approval`" in ideas
    assert "to the feed" in ideas
    # suggest_idea: concrete and company-specific only; never in chat, never on a schedule.
    assert "`suggest_idea` only when you notice a concrete, company-specific job" in ideas
    assert "not to chat" in ideas and "never on a schedule" in ideas
    # No plan step, no hidden notes, no guide files.
    assert "plan" not in ideas.lower() and "$HERMES_HOME/ideas" not in ideas
    # The backend's message carries the phrases the reference keys on.
    service = (BACKEND / "core/services/ideas.py").read_text()
    assert "Let's do it!" in service and "Tracked in Work: [" in service
    plugin = (HERMES / "plugins/lightbulb/plugin.yaml").read_text()
    assert "- suggest_idea" in plugin


def test_the_built_skills_point_at_the_hermes_facts_file(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(HERMES / "build-skills.sh"), str(out)], check=True, capture_output=True)
    ideas = (out / "lightbulb/references/ideas.md").read_text()
    assert "$HERMES_HOME/lightbulb-facts.json" in ideas and ".openclaw/" not in ideas
    stale = [str(p) for p in out.rglob("*") if p.is_file() and "~/.openclaw/lightbulb-facts.json" in p.read_text()]
    assert stale == []


def test_the_skill_sources_carry_no_openclaw_facts_path():
    stale = [
        str(p)
        for p in (HERMES / "skills-src").rglob("*.md")
        if p.name != "MAINTENANCE.md" and "openclaw/lightbulb-facts" in p.read_text()
    ]
    assert stale == []
