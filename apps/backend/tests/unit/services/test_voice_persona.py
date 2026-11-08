from core.services import voice_persona as vp


# ---- Option D -------------------------------------------------------------------------------

STARTERS = {"ceo": "c-1", "engineering": "e-1", "finance": "f-1", "growth": "g-1", "analytics": "a-1",
            "customer_operations": "o-1"}  # fmt: skip


def test_voice_pool_is_tested_gpt_live_voices_only():
    assert not {"aster", "bossa", "tempo"} & set(vp.VOICE_POOL)


def test_agent_voice_role_voices_and_a_stable_hash_for_the_rest():
    assert vp.agent_voice("c-1", STARTERS) == "marin"
    assert vp.agent_voice("g-1", STARTERS) == "vesper"
    for i in range(50):
        v = vp.agent_voice(f"hired-{i}", STARTERS)
        assert v not in vp.STARTER_VOICES.values() and v in vp.VOICE_POOL
        assert vp.agent_voice(f"hired-{i}", STARTERS) == v
    assert vp.agent_voice("c-1", {}) not in vp.STARTER_VOICES.values()  # no starter ids: just hashed


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
    assert vp.agent_voice("atlas", None, "Agent Recruiter") not in vp.STARTER_VOICES.values()
    # a workspace that does record its starters: a hire's title never takes a starter's voice
    starters = {"engineering": "eng-1"}
    assert vp.agent_voice("hire", starters, "Senior Engineer") not in vp.STARTER_VOICES.values()
