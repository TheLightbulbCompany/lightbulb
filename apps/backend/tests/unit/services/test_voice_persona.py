from core.services import voice_persona as vp


def test_ceo_is_marin_and_others_are_stable():
    assert vp.voice_for("ceo-1", "ceo-1") == "marin"
    v = vp.voice_for("agent-x", "ceo-1")
    assert v in vp.VOICES and vp.voice_for("agent-x", "ceo-1") == v


def test_dm_persona_relays_everything():
    p = vp.build_persona(voice_name="Engineering", team=None)
    assert "You are Engineering" in p
    assert "Hand every request to your team" not in p
    assert "delegate every request" in p.lower()
    assert "greetings, thanks" in p


def test_room_persona_lists_the_roster_and_attributes():
    team = [
        {"id": "c", "name": "CEO", "card": "Runs the company."},
        {"id": "f", "name": "Finance", "card": "Money, Stripe, budgets."},
    ]
    p = vp.build_persona(voice_name="CEO", team=team)
    assert "- Finance: Money, Stripe, budgets." in p
    assert "Finance says" in p
