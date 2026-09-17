from core.services.hermes_profiles import (
    SOUL_GENERATED_MARKER,
    build_soul_md,
    is_generated_soul,
    strip_generated_marker,
)


def test_soul_names_the_agent_and_its_role():
    soul = build_soul_md({"id": "a1", "name": "Ada", "role": "engineering", "title": "Head of Engineering"})
    assert "# Ada" in soul
    assert "Head of Engineering" in soul
    assert "engineering" in soul


def test_the_ceo_soul_says_it_is_the_root_of_the_roster():
    soul = build_soul_md({"id": "a0", "name": "Friday", "role": "ceo"})
    assert "CEO" in soul
    assert "report to you" in soul


def test_capabilities_are_carried_verbatim_when_paperclip_has_them():
    soul = build_soul_md({"id": "a1", "name": "Ada", "role": "engineering", "capabilities": "ships backend code"})
    assert "ships backend code" in soul


def test_a_nameless_agent_falls_back_to_its_id_not_a_crash():
    assert "# a9" in build_soul_md({"id": "a9", "role": "general"})


# ---------------------------------------------------------------------------
# The generated marker (M6-R48): what lets a rename tell "still ours" from
# "the owner edited this in the Instructions tab".
# ---------------------------------------------------------------------------


def test_a_generated_soul_carries_the_marker_on_its_first_line():
    soul = build_soul_md({"id": "a1", "name": "Ada", "role": "engineering"})
    assert soul.splitlines()[0] == SOUL_GENERATED_MARKER
    assert is_generated_soul(soul)
    # The heading must not be butted against the comment, or CommonMark swallows it.
    assert soul.splitlines()[1] == ""


def test_owner_text_is_not_generated():
    assert not is_generated_soul("# Ada\n\nI wrote this myself.\n")
    assert not is_generated_soul("")
    assert not is_generated_soul(None)


def test_stripping_the_marker_is_what_hands_the_file_to_the_owner():
    soul = build_soul_md({"id": "a1", "name": "Ada", "role": "engineering"})
    stripped = strip_generated_marker(soul)
    assert not is_generated_soul(stripped)
    assert stripped.startswith("# Ada")


def test_stripping_is_a_no_op_on_text_that_never_carried_a_marker():
    assert strip_generated_marker("# Ada\n") == "# Ada\n"
