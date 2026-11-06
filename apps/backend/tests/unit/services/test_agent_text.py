"""Founder, 6 Nov 2026: the web composer escapes what a person typed so it shows literally;
agents (Router, Workers, runtimes, MCP reads) get the text back with no backslashes."""

from core.repositories.messaging_repo import agent_text
from core.services import openclaw_work


def test_agents_read_typed_text_without_escapes():
    stored = "\\_\\_init\\_\\_, 3\\*4 then 5\\*6, \\~x\\~, a_b_c, \\*\\*not bold\\*\\*, \\# hi, **bold**"
    assert agent_text(stored) == "__init__, 3*4 then 5*6, ~x~, a_b_c, **not bold**, # hi, **bold**"
    assert "\\" not in agent_text(stored)


def test_code_keeps_its_backslashes():
    stored = "run `a\\*b` then\n```\nprint('\\*')\n```\n3\\*4"
    assert agent_text(stored) == "run `a\\*b` then\n```\nprint('\\*')\n```\n3*4"


def test_a_typed_backslash_survives_and_plain_text_is_unchanged():
    assert agent_text("C:\\path\\\\\\*x") == "C:\\path\\*x"
    assert agent_text("plain text") == "plain text"


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
