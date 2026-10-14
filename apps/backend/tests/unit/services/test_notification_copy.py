from core.services import notification_copy as copy


def test_title_is_who_and_where():
    assert copy.title("Pulse", copy.room_where("channel", "GTM Strategy")) == "Pulse · #GTM Strategy"
    assert copy.title("Friday", copy.room_where("dm", None)) == "Friday · DM"
    assert copy.title("Pulse", "ISOA-400") == "Pulse · ISOA-400"
    assert copy.title("Pulse") == "Pulse"
    assert copy.room_where("group-dm", None) == "Group chat"


def test_approval_title_is_a_named_single_action():
    assert copy.approval_title("Finance") == "Finance · needs your OK"


def test_line_is_the_first_plain_line_clipped():
    assert copy.line("\n## Plan\n- step one") == "Plan"
    assert copy.line("- **Bold** and `code`") == "Bold and code"
    assert copy.line(None) == ""
    clipped = copy.line("x" * 200)
    assert len(clipped) == 80 and clipped.endswith("…")


def test_run_failure_line_says_why_in_plain_words():
    assert copy.run_failure_line("process_lost", "Process lost") == "Couldn't finish: the workspace restarted mid-run"
    assert copy.run_failure_line(None, "Rate limited\nstack") == "Couldn't finish: Rate limited"
    assert copy.run_failure_line() == "Couldn't finish: the run hit an error"
