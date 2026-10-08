"""What a CORRECT router says about each scenario line, for synthetic replays.

THIS IS AN ASSUMPTION, NOT A RECORDING. A synthetic replay has no /route to ask, so each line's verdict is
written here from what the scenario means (SCENARIOS.md in the live rig). A replay therefore tests the
controller given a router that understands every line: it cannot find a router mistake, and a check that
fails here fails with the router at its best. A recorded call replays the router's real answers instead.

Per scenario: line index -> verdict. One of
  ask     the line asks this agent for `task`; its run answers with ANSWERS[`answer`] (`by`: how it was
          addressed, as the backend reports it: name, follow, room)
  social  small talk for this agent, which says `reply` (the writer's line)
  called  it turns to this agent and asks nothing new ("go ahead", "Friday?")
  stop    "cancel": the speaker withdrew what this agent has open; "hush": stop talking, the work stands
  people  talk between people
  hold    an unfinished sentence: its rest is on the way
A line with no entry asks nothing of anyone ("Okay, thanks.", a listening sound).
`when`: a regex the routed words must match for the verdict to hold, and `else` the verdict when they do not.
The transcriber may hand a line over in pieces, and only the piece with the request in it is the request.
`unclaimed`: the verdict holds only for a question the worker says nobody took.
"""

import re

ANSWERS = {
    "price": "The Workspace plan costs fifty dollars per month total, not per seat. "
    "That's from Lightbulb's current plan record just now.",
    "agents": "A new Workspace starts with six agents: CEO, Engineering, Finance, Growth, Analytics, and "
    "Customer Operations. You can add unlimited additional agents and members.",
    "agents_forge": "A new workspace comes with six starter agents, from Lightbulb's current documentation just now.",
    "names": "They're called CEO, Engineering, Finance, Growth, Analytics, and Customer Operations.",
    "five": "Lightbulb gives a person or team a private AI company. "
    "It combines AI agents, shared context, team chat, persistent workspaces, and task management. "
    "The agents can help across connected tools, meetings, browsers, code sessions, and company workflows. "
    "Everyone on the team can collaborate with the same agents instead of managing separate bots. "
    "People set the direction, while the agents coordinate and execute the work.",
    "calendar": "Your Google Calendar isn't connected, so I can't verify what's on your calendar tomorrow. "
    "I checked the available calendar connections just now.",
    "thursday": "I can't verify Thursday either because your Google Calendar isn't connected.",
    "deploy": "Last night's deployment finished successfully at 7:30 PM Eastern, from GitHub just now.",
}

PRICE = {"ask": "friday", "task": "what the Workspace plan costs per month", "answer": "price", "when": "cost"}
AGENTS = {"ask": "friday", "task": "how many agents come with a new workspace", "answer": "agents", "when": "agents"}
FIVE = {"ask": "friday", "task": "five sentences on what Lightbulb does", "answer": "five"}
CALENDAR = {"ask": "friday", "task": "what is on their calendar tomorrow", "answer": "calendar", "when": "calendar"}
PEOPLE = {"people": True}
GO_AHEAD = {"called": "friday", "when": "go ahead|friday"}

ROUTING: dict[str, dict[int, dict]] = {
    "backchannel": {0: FIVE},  # "Okay, thanks." and the listening sounds ask nothing
    "crowd_no_name": {
        **{i: PEOPLE for i in range(7)},
        # a question to the room that nobody takes: one agent may answer it, and the room's router picks it
        7: {"ask": "forge", "task": "when the deploy finished last night", "answer": "deploy", "by": "room", "unclaimed": True},
    },
    "gap": {0: PEOPLE, 1: {**CALENDAR, "else": PEOPLE}, 2: PEOPLE, 3: PEOPLE, 4: PEOPLE},
    "long_answer_cut": {0: FIVE, 2: PEOPLE},
    "never_mind": {0: PRICE, 1: {"stop": "cancel", "agent": "friday"}},
    "one_to_one": {
        0: {"social": "friday", "reply": "Hi, Prasiddha."},
        1: PRICE,
        2: {"social": "friday", "reply": "You're welcome."},
    },
    "sales_call": {
        0: PEOPLE, 1: {**PRICE, "else": PEOPLE}, 2: PEOPLE, 3: PEOPLE, 4: PEOPLE, 5: PEOPLE, 6: GO_AHEAD, 7: PEOPLE,
        8: AGENTS, 9: PEOPLE, 10: PEOPLE, 11: {"stop": "hush", "agent": "friday", "when": "stop"},
    },  # fmt: skip
    "sidetalk": {
        0: CALENDAR, 1: PEOPLE, 2: PEOPLE, 3: PEOPLE,
        4: {"ask": "friday", "task": "what is on their calendar on Thursday", "answer": "thursday"},
    },  # fmt: skip
    "status_check": {0: PRICE, 1: {"called": "friday"}, 2: GO_AHEAD},
    "stop_mid_answer": {0: FIVE, 2: {"stop": "hush", "agent": "friday"}},
    "trailing": {0: {"hold": True}, 1: {"hold": True}, 2: PRICE},
    "two_agents": {
        0: PRICE,
        1: {"ask": "forge", "task": "how many agents come with a new workspace", "answer": "agents_forge"},
    },
    "two_asks_one_agent": {0: PRICE, 1: AGENTS},
    "unnamed_followup": {
        0: AGENTS,
        1: {"ask": "friday", "task": "what the six starter agents are called", "answer": "names", "by": "follow"},
    },
    "unnamed_sidetalk": {0: AGENTS, 1: PEOPLE, 2: PEOPLE},
}

# Checks a replay fails today, and why: (scenario or "*", a regex on the check, the cause). Recorded so the
# suite can gate on everything else (tests/test_voice_replay.py): a failing check that is not here is new.
# An entry is no licence: each names the rule to change. Shares are of 100 seeds on 2026-10-08.
ACK = (
    "acknowledgement latency sits at the 4 s limit (15-25% of seeds; live 12 of 38): the final transcript comes "
    "1.2-1.9 s after the line, then /route, the voice, its first audio. A silent first unit or a person "
    "talking again adds seconds (`_turn_open` holds every line on an open turn)."
)
TALK_OVER = (
    "a mouth yields TALK_OVER_S (0.5 s) after the detector hears the person, itself 0.4-0.6 s after they start "
    "(`_yield_check`): 1.0-1.5 s of agent audio is over any line that starts on agent speech."
)
BLIND = (
    "a sentence that starts in the 0.4-0.6 s before the detector hears a person who just began is audible over "
    "them until it yields; and the owed answer cut by the next line has no transcript line (a cut unit is "
    "not on the record, `on_agent_said`), so check.py cannot tell it was the owed answer."
)
LEVEL_LATE = (
    "the SFU's level for the talker came late (up to 2.5 s): until then the detector alone is not talk "
    "(`_talking_changed`, `Epoch.credible`), so the mouth speaks on and may start its next sentence."
)
QUIET = (
    "an answer nobody is plainly waiting for needs MEET_QUIET_S (3 s) counted from the last final transcript "
    "(`_hold`, `_heard_final` sets `_last_heard`), then its voice reopened: about 5.5 s of a pause are gone "
    "before its first word."
)
LINES = "an acknowledgement cut after its first sentence is finished later as a line of its own, beside two-sentence answers."
KNOWN_FAILING: list[tuple[str, str, str]] = [
    ("one_to_one", r'ack_within \{"line": 0', "small talk is written only after /route answers (`_opened`), on top of: " + ACK),
    (
        "unnamed_followup", r'ack_within \{"line": 1',
        "the exchange is forgotten after ENGAGE_GAP_S (30 s) of silence, before the follow-up at 55 s, so the "
        "nameless question waits out UNCLAIMED_QUESTION_S (3.5 s) before /route (`_dispatch`).",
    ),  # fmt: skip
    ("*", r"ack_within", ACK),
    ("*", r"no_talk_over", TALK_OVER),
    ("backchannel", r"keeps_speaking", "a listening sound makes the mouth yield like any talk, and it resumes only after that sound's final transcript, from the start of the cut sentence: " + TALK_OVER),
    ("sidetalk", r'answer_contains \{"after": 0', QUIET),
    ("long_answer_cut", r"resumes_after", QUIET),
    ("gap|sidetalk|sales_call|unnamed_sidetalk", r"silent_between", BLIND),
    ("gap|long_answer_cut|stop_mid_answer", r"stops_within", LEVEL_LATE),
    ("sidetalk|two_asks_one_agent|one_to_one", r"max_agent_lines", LINES),
    ("gap", r'answer_contains \{"after": 1', "1 seed in 100, not explained: the answer was ready and never said before the rig left."),
]


def known(scenario: str, check: str) -> str | None:
    """Why this check of this scenario is known to fail in a replay, or None: a new failure."""
    return next((why for names, pattern, why in KNOWN_FAILING if names == "*" or scenario in names.split("|") if re.match(pattern, check)), None)
