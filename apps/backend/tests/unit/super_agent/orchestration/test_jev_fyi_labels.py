"""Jev's Router question on FYIs vs chatter (walk 15, review of #4933; the capacity caps of walks 15-16): a fact
told to Lightbulb or the whole team to keep reaches the Router (✅); people sorting out their own day, even about
the work, does not (no ✅ spam).

`LABELLED` is the reference set. The criterion test runs everywhere; the live one calls the real Jev and runs only
with JEV_LIVE=1 and the gateway key in JEV_KEY (the env's `lightbulb/{env}/vercel-ai-gateway` secret):
`JEV_LIVE=1 JEV_KEY=… uv run pytest <this file> -s`."""

import os

import pytest

from core.services import jev

FYI = "router"
CHAT = "none"
#: (message, expected) in a project room with people Sam and Priya, no specialist listening.
LABELLED = [
    ("FYI: we cap each class at 12 kids.", FYI),
    ("FYI the oven is closed on Mondays", FYI),
    ("For the record, our starter dates from 2009.", FYI),
    ("Note that we never ship on Sundays.", FYI),
    ("Heads up: the supplier raised flour prices 10% from December.", FYI),
    ("Our kids' class is $35 per child.", FYI),
    ("We don't do gluten-free dough, only gluten-free toppings.", FYI),
    ("Parents usually ask about gluten-free dough.", FYI),
    ("Opening hours are 11 to 9, closed Tuesdays.", FYI),
    ("Keep in mind: all posts go out in English and Spanish.", FYI),
    # Capacity caps: the two FYIs that went silent in walks 15 and 16.
    ("FYI: we have floor cushions for 15 kids, so that's the most we can seat.", FYI),
    ("We cap each class at 12 kids.", FYI),
    # Who does what, said to the group (walk 17, silent in a group chat with Priya); said to Priya, it is chatter.
    ("Priya will run the club sign-up table.", FYI),
    ("Priya, the meeting moved to 3pm.", CHAT),
    ("I'll call the supplier after lunch.", CHAT),
    ("running 10 min late, sorry", CHAT),
    ("thanks Sam!", CHAT),
    ("lol", CHAT),
    ("Sam did you lock the back door last night?", CHAT),
    ("Priya I left the keys on your desk", CHAT),
    ("brb grabbing coffee", CHAT),
    ("Nice work on the menu, both of you", CHAT),
    ("ok sounds good", CHAT),
    ("Sam, can you count the cushions before Saturday?", CHAT),
    ("Priya I'll set the chairs out at 10", CHAT),
    ("Priya, you run the sign-up table.", CHAT),
]


def test_the_criterion_names_what_an_fyi_is_and_what_chatter_is():
    crit, who = jev.ROUTER_CRITERION, jev.ROUTER_WHO_INSTRUCTIONS
    for cue in ('"FYI"', '"for the record"', '"note that"', '"heads up"', "a standing fact about the business"):
        assert cue in crit
    assert "said to no one in particular" in crit and "even about the work" in who
    assert "the meeting moved to 3pm" in who  # person-to-person work talk stays out
    # Walk 17: who does what, said to the group, is the team's to keep; said to that person, it is theirs.
    assert "who does what" in crit and '"Priya, you run the sign-up table"' in who


def test_the_router_notes_who_does_what_said_to_the_group():
    from core.temporal import router_prompt

    rules = router_prompt.ROUTER_INSTRUCTIONS
    note = rules[rules.index("- note when") : rules.index("- ask for one")]
    assert 'who does what in the team: "Priya will run the sign-up table"' in note
    assert '"Priya, you run the sign-up table"' in note.split("not a note")[1]
    assert sum(1 for _, label in LABELLED if label == FYI) == sum(1 for _, label in LABELLED if label == CHAT)


@pytest.mark.skipif(os.environ.get("JEV_LIVE") != "1", reason="calls the real Jev (JEV_LIVE=1)")
async def test_live_jev_on_the_labelled_set(monkeypatch):
    async def key():
        return os.environ["JEV_KEY"]

    monkeypatch.setattr(jev, "_gateway_key", key)
    meta = {"roomId": "r1", "ownerId": "org_eval", "kind": "channel", "name": "walk-15 pizzeria"}
    members = [{"id": "sam", "displayName": "Sam"}, {"id": "priya", "displayName": "Priya"}]
    wrong = []
    for i, (text, label) in enumerate(LABELLED):
        msg = {
            "messageId": f"m{i}",
            "author": {"kind": "human", "id": "sam", "displayName": "Sam"},
            "text": text,
            "createdAt": "2026-11-06T13:40:00Z",
        }
        got = (await jev.route_for_router(meta, msg, members, [], {})).get("to")
        print(f"{label:6} {got!s:6} {text}")
        if got != label:
            wrong.append((text, label, got))
    print(f"correct {len(LABELLED) - len(wrong)}/{len(LABELLED)}")
    assert len(wrong) <= 2, wrong
