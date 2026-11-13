"""Founder, 6 Nov 2026: the web composer escapes what a person typed so it shows literally;
agents (Router, Workers, runtimes, MCP reads) get the text back with no backslashes."""

from core.repositories.messaging_repo import agent_text
from core.services import openclaw_work


def test_agents_read_typed_text_without_escapes():
    stored = "\\_\\_init\\_\\_, 3\\*4 then 5\\*6, \\~x\\~, a_b_c, \\*\\*not bold\\*\\*\n\\# hi, **bold**"
    assert agent_text(stored) == "__init__, 3*4 then 5*6, ~x~, a_b_c, **not bold**\n# hi, **bold**"
    assert "\\" not in agent_text(stored)


def test_code_keeps_its_backslashes():
    stored = "run `a\\*b` then\n```\nprint('\\*')\n```\n3\\*4"
    assert agent_text(stored) == "run `a\\*b` then\n```\nprint('\\*')\n```\n3*4"


def test_only_the_composers_escapes_come_off():
    assert agent_text("\\\\server\\share and a\\.b and C:\\temp") == "\\\\server\\share and a\\.b and C:\\temp"
    assert (
        agent_text("\\# h\n\\> q\n  \\- d\n\\+ p\n\\=\n2024\\. y\n1\\) z") == "# h\n> q\n  - d\n+ p\n=\n2024. y\n1) z"
    )
    assert agent_text("mid \\# stays") == "mid \\# stays"
    assert agent_text("plain text") == "plain text"


def test_double_backtick_code_keeps_its_backslashes():
    assert agent_text("``a\\*b`c`` then 3\\*4") == "``a\\*b`c`` then 3*4"


def test_runtime_ingress_carries_clean_text():
    row = {
        "messageId": "11111111-1111-4111-8111-111111111111",
        "roomId": "22222222-2222-4222-8222-222222222222",
        "messageSeq": 1,
        "roomRevision": 1,
        "author": {"kind": "human", "id": "u", "displayName": "Ana"},
        "text": "fix \\_\\_init\\_\\_ for 3\\*4",
        "mentions": [],
        "createdAt": "2026-11-06T00:00:00Z",
    }
    assert openclaw_work._public_message(row)["text"] == "fix __init__ for 3*4"


def test_jev_reads_clean_text():
    from datetime import datetime, timezone

    from core.services import jev

    message = {"messageId": "m", "author": {"kind": "human", "id": "u", "displayName": "Ana"}, "text": "fix 3\\*4"}
    assert jev._line(message)["text"] == "fix 3*4"
    run = {"agentId": "a", "triggerMessageId": "m", "dispatchedAt": None}
    (line,) = jev._open_lines([run], [{"id": "a", "displayName": "Finance"}], [message], datetime.now(timezone.utc))
    assert "\\" not in line and "3*4" in line


def test_watchdog_notes_read_clean_text():
    import inspect

    from core.services import watchdog

    params = inspect.signature(watchdog.judge_state).parameters
    args = {name: [] if name in {"notes", "tools", "open_questions", "previous_verdicts"} else {} for name in params}
    args = {k: v for k, v in args.items() if params[k].default is inspect.Parameter.empty}
    args["notes"] = [{"kind": "comment", "author": {"kind": "human"}, "text": "\\_\\_init\\_\\_ broke"}]
    out = str(watchdog.judge_state(**args))
    assert "__init__ broke" in out and "\\_" not in out
