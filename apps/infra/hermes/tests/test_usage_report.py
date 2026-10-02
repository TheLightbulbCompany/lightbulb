from bridge import turn

ROOM_KEY = "agent:ceo:lightbulb:channel:11111111-1111-4111-8111-111111111111"
MAIL_KEY = "lightbulb-email:thread-42"


def test_a_room_session_key_never_reports_here(posted):
    assert turn.report_usage(ROOM_KEY, "ceo", "r1", True, 120, turn.ZERO_USAGE) is False
    assert posted == []


def test_a_mail_or_paperclip_session_key_reports(posted):
    assert turn.report_usage(MAIL_KEY, "ceo", "hook:9", True, 120, turn.ZERO_USAGE) is True
    path, body = posted[0]
    assert path == "usage/org_test"
    assert body == {"sessionKey": MAIL_KEY, "agentId": "ceo", "runId": "hook:9",
                    "success": True, "durationMs": 120, "usage": turn.ZERO_USAGE}


def test_the_error_text_is_never_forwarded(posted):
    turn.report_usage(MAIL_KEY, "ceo", "hook:9", False, None, turn.ZERO_USAGE)
    body = posted[0][1]
    assert "error" not in body and "durationMs" not in body and body["success"] is False


def test_a_voice_run_on_a_room_key_reports_with_its_family(posted):
    assert turn.report_usage(ROOM_KEY, "ceo", "r1", True, 120, turn.ZERO_USAGE, family="voice") is True
    path, body = posted[0]
    assert path == "usage/org_test" and body["sessionKey"] == ROOM_KEY and body["family"] == "voice"


def test_another_family_on_a_room_key_still_never_reports(posted):
    assert turn.report_usage(ROOM_KEY, "ceo", "r1", True, 120, turn.ZERO_USAGE, family="call") is False
    assert posted == []


def test_a_non_room_report_carries_no_family(posted):
    turn.report_usage(MAIL_KEY, "ceo", "hook:9", True, 120, turn.ZERO_USAGE)
    assert "family" not in posted[0][1]
