from core.services.hermes_profiles import (
    SOUL_GENERATED_MARKER,
    build_soul_md,
    is_generated_soul,
    strip_generated_marker,
)


def test_soul_names_the_agent_and_its_role():
    soul = build_soul_md({"id": "a1", "name": "Ada", "role": "engineering", "title": "Head of Engineering"})
    assert "# Ada" in soul
    assert "You are Ada, Head of Engineering on the Lightbulb team." in soul
    assert "engineering agent" not in soul


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


# ---------------------------------------------------------------------------
# The starter roster ships with a persona: Paperclip ``capabilities`` rendered
# under "## What you do", not a one-line identity.
# ---------------------------------------------------------------------------


def test_every_starter_agent_renders_a_full_persona():
    from core.services.workspace_provisioning import STARTER_CAPABILITIES, STARTER_ROSTER

    assert set(STARTER_CAPABILITIES) == {key for key, *_ in STARTER_ROSTER}
    for key, name, role, title, _parent in STARTER_ROSTER:
        capabilities = STARTER_CAPABILITIES[key]
        soul = build_soul_md({"id": key, "name": name, "role": role, "title": title, "capabilities": capabilities})
        assert is_generated_soul(soul)
        assert f"\n# {name}\n" in soul
        assert f"You are {name} on the Lightbulb team." in soul
        assert f"the {role} agent" not in soul
        assert soul.endswith(f"\n## What you do\n\n{capabilities}\n")
        headline, blank, paragraph = capabilities.split("\n")
        assert headline and not blank and len(paragraph) > 200


def test_identity_line_uses_title_and_company_never_the_role_code():
    soul = build_soul_md({"id": "a", "name": "Ada", "role": "cfo", "title": "Head of Money"}, company_name="Acme")
    assert "You are Ada, Head of Money at Acme." in soul
    assert "cfo" not in soul
