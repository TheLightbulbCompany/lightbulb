"""Agent message parts: two agents whose names slug alike keep their own choice."""

from core.services import messaging


def test_two_agents_whose_names_slug_alike_keep_their_own_choice():
    choices = messaging._part_choices(["foo-bar", "foo bar"])
    alone = [names for _, names in choices.values() if len(names) == 1]
    assert {"foo-bar"} in alone and {"foo bar"} in alone and len(choices) == 4
