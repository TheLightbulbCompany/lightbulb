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
        room_name="general", people=["Aden"], agents=["Gauge", "Forge"], ask_name=False,
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
    assert "don't have a name yet" in _persona(ask_name=True)


def test_agent_persona_caps_soul_and_notes_and_fits_the_instruction_limit():
    p = _persona(
        soul_excerpt="s" * 20_000,
        voice_notes="n" * 20_000,
        people=["P" * 256] * 30,
        agents=["A" * 256] * 30,
        ask_name=True,
    )
    assert "s" * vp.SOUL_EXCERPT_MAX in p and "s" * (vp.SOUL_EXCERPT_MAX + 1) not in p
    assert "n" * 1000 in p and "n" * 1001 not in p
    assert len(p) / 4 < 16_384
