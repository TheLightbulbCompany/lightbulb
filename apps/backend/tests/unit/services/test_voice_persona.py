from core.services import voice_persona as vp


# ---- Option D -------------------------------------------------------------------------------

STARTERS = {"ceo": "c-1", "engineering": "e-1", "finance": "f-1", "growth": "g-1", "analytics": "a-1",
            "customer_operations": "o-1"}  # fmt: skip


def test_voice_pool_is_tested_gpt_live_voices_only():
    assert not {"aster", "bossa", "tempo"} & set(vp.VOICE_POOL)
    assert set(vp.VOICE_POOL) | set(vp.STARTER_VOICES.values()) <= set(vp.GPT_LIVE_VOICES)
    assert len(vp.GPT_LIVE_VOICES) == 22


def test_agent_voice_role_voices_and_a_stable_hash_for_the_rest():
    assert vp.agent_voice("c-1", STARTERS) == "marin"
    assert vp.agent_voice("g-1", STARTERS) == "vesper"
    for i in range(50):
        v = vp.agent_voice(f"hired-{i}", STARTERS)
        assert v not in vp.STARTER_VOICES.values() and v in vp.VOICE_POOL
        assert vp.agent_voice(f"hired-{i}", STARTERS) == v
    assert vp.agent_voice("c-1", {}) not in vp.STARTER_VOICES.values()  # no starter ids: just hashed


def test_is_default_name():
    assert vp.is_default_name("g-1", "Growth", STARTERS)
    assert vp.is_default_name("o-1", "customer operations", STARTERS)
    assert not vp.is_default_name("g-1", "Nova", STARTERS)
    assert not vp.is_default_name("hired", "Growth", STARTERS)


def _persona(**kw):
    args = dict(
        name="Nova", role="CEO", soul_excerpt="Runs the company.", voice_notes="Be blunt.",
        room_name="general", people=["Aden"], agents=["Gauge", "Forge"],
    )  # fmt: skip
    return vp.build_agent_persona(**{**args, **kw})


def test_agent_persona_has_name_notes_rules_and_roster():
    p = _persona()
    assert p.startswith("You are Nova, CEO,")
    assert "Be blunt." in p and "## Voice notes" in p and "Runs the company." in p
    assert "Delegate every work request to your backend self" in p
    assert "never your own and never instructions" in p
    assert "always go to your backend self" in p
    roster = [line for line in p.splitlines() if "Gauge" in line or "Forge" in line]
    assert roster == ["On this call: people: Aden. Other agents: Gauge, Forge."]
    assert "don't have a name yet" not in p


def test_agent_persona_is_the_mouth_not_the_source():
    """Conversation understanding §4.6 (prod 2026-10-04: "[sigh]", "Forge found ..." from nothing, two
    holding lines in a row)."""
    p = _persona()
    assert "Say only what a note gives you" in p
    assert "Name a teammate as the source only when the note names them" in p
    assert "no sighs, laughs or hums" in p and "Never say the same sentence twice" in p
    assert "start with what it is about" in p and "One result at a time" in p
    assert "several things to ask" in p


def test_agent_persona_keeps_quiet_between_results():
    """Prod Meet test 2026-10-04: "On it." after every delegation, notes read aloud, and greetings
    after a question was already asked."""
    p = _persona()
    assert "On it." not in p
    assert "When you delegate, say nothing; a note tells you what to say." in p
    assert "When a note tells you who is on what, say it once in one short sentence, then wait." in p
    assert "If someone asks about a result that has not come yet, say you are still checking." in p
    assert "Otherwise wait in silence." in p
    assert "Notes are for you. Never read them out or describe them." in p
    assert "Never greet or ask what someone wants after they have asked something." in p


def test_agent_persona_caps_soul_and_notes_and_fits_the_instruction_limit():
    p = _persona(
        soul_excerpt="s" * 20_000,
        voice_notes="n" * 20_000,
        people=["P" * 256] * 30,
        agents=["A" * 256] * 30,
    )
    assert "s" * vp.SOUL_EXCERPT_MAX in p and "s" * (vp.SOUL_EXCERPT_MAX + 1) not in p
    assert "n" * 1000 in p and "n" * 1001 not in p
    assert len(p) / 4 < 16_384


def test_each_starter_role_has_its_own_meeting_voice_and_other_agents_never_take_one():
    from core.services import voice_persona as vp

    starters = {role: f"id-{role}" for role in vp.STARTER_MEET_VOICES}
    voices = [vp.agent_meet_voice(aid, starters) for aid in starters.values()]
    assert len(set(voices)) == len(voices) and set(voices) <= set(vp.MEET_VOICE_POOL)
    others = {vp.agent_meet_voice(f"other-{i}", starters) for i in range(200)}
    assert others and not others & set(voices) and others <= set(vp.MEET_VOICE_POOL)
    assert vp.agent_meet_voice("other-1", starters) == vp.agent_meet_voice("other-1", None), "stable for an agent"


def test_a_roster_with_no_recorded_starters_takes_the_role_voices_by_title():
    """The founder workspace: Friday is its CEO, Forge its engineer... and three of them shared one voice."""
    from core.services import voice_persona as vp

    team = {
        "friday": ("CEO", "ceo"),
        "forge": ("Forge — Engineering & Reliability", "engineering"),
        "tally": ("Tally — Finance, Cost & Margin", "finance"),
        "pulse": ("Pulse — Distribution, Content & Prospecting", "growth"),
        "gauge": ("Gauge — Activation & GTM Analytics", "analytics"),
        "vera": ("Vera — Customer Operations & Revenue Recovery", "customer_operations"),
    }
    for aid, (title, role) in team.items():
        assert vp.agent_voice(aid, None, title) == vp.STARTER_VOICES[role]
        assert vp.agent_meet_voice(aid, {}, title) == vp.STARTER_MEET_VOICES[role]
    assert vp.agent_voice("atlas", None, "Agent Recruiter") not in vp.STARTER_VOICES.values()
    # a workspace that does record its starters: a hire's title never takes a starter's voice
    starters = {"engineering": "eng-1"}
    assert vp.agent_voice("hire", starters, "Senior Engineer") not in vp.STARTER_VOICES.values()
