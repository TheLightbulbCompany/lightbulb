from core.services.hermes_profiles import build_soul_md


def test_soul_names_the_agent_and_its_role():
    soul = build_soul_md({"id": "a1", "name": "Ada", "role": "engineering", "title": "Head of Engineering"})
    assert soul.startswith("# Ada")
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
    assert build_soul_md({"id": "a9", "role": "general"}).startswith("# a9")
