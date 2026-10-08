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
# An entry is no licence: each names the rule to change. Shares are of 200 seeds on 2026-10-08, as
# `current` / `fast-eot` (`sim.Model`: the second is a what-if transcriber, not a measurement). Each cause
# ends with where it lies: [controller] a rule of huddle.py, [timing] the transcriber's or the voice's
# delays, [policy] a rule and the scenario disagree and a person has to choose, [check] check.py itself.
ACK = (
    "acknowledgement latency sits at the 4 s limit (9-25% / 0-4%): the final transcript comes 1.2-1.9 s "
    "after the line, then /route, the voice, its first audio. A silent first unit or a person talking again "
    "adds seconds (`_turn_open` holds every line on an open turn). [timing]"
)
TALK_OVER = (
    "a mouth yields TALK_OVER_S (0.5 s) after the room hears the person, itself 0.4-0.6 s after they start "
    "(`_yield_check`): 1.0-1.5 s of agent audio is over a line that starts on a sentence well under way "
    "(status_check 77-88% / 14-15%). A sentence only just begun yields at once (PREROLL_S). [timing]"
)
NO_LINE = (
    "three causes. (1) The owed answer starts at a pause and the next line cuts it with under HEARD_SHARE "
    "of the sentence audible: by rule it is not said and leaves no line, and check.py needs a line to call "
    "the audio the owed answer (gap 100%; most of sidetalk). Recording every audible piece clears it (gap "
    "100 -> 14%, sidetalk 84 -> 35%) and fails said_once and no_text instead, on the same sentence said "
    "again whole. [policy] (2) An acknowledgement at the 4 s limit meets a person who starts within the "
    "detector's lag of its first audio. [timing] (3) check.py gives a line recorded under 0.5 s before the "
    "next burst to that burst, and calls the first one audio with no line. [check]"
)
LEVEL_LATE = (
    "the SFU's level for the talker came late (up to 2.5 s). Until then the detector alone holds what has "
    "not begun but stops no sentence being said (`_yield_check`): a false stop costs the sentence. 2-3% / 0-2%. "
    "[policy]"
)
QUIET = (
    "an answer nobody is plainly waiting for needs MEET_QUIET_S (3 s) of quiet, then its voice reopened and "
    "its first audio: its first sentence (5.8 s) cannot end inside the 6.8 s pause unless it starts within "
    "2 s of the speech before. Counting the quiet from the end of speech instead of the final transcript "
    "starts it about 1 s sooner, fits no more of it in, and has it cut more often (tried, reverted). [policy]"
)
LINES = (
    "sidetalk: two acknowledgements and three answer sentences are five lines against a limit of four; it "
    "passed only when an audible acknowledgement was cut and left no line (fails 69-72% / 94-97%). [check] The others "
    "(0-5%): the voice's own tail put more than 3 s between an acknowledgement's two sentences (a quiet "
    "lead-in, a long trailing silence), which check.py then counts as two lines. [timing]"
)
CUT_SEEN = (
    "a listening sound whose first interim words come after TALK_OVER_S still makes the mouth yield "
    "(`_only_listening`), and a sentence cut with HEARD_SHARE of it audible is on the record as what was "
    "heard, with no end punctuation (9-11% / 6-11%). Before, a cut sentence left no line and the check "
    "could not see it. [timing]"
)
KNOWN_FAILING: list[tuple[str, str, str]] = [
    (
        "unnamed_followup", r'ack_within \{"line": 1',
        "52-55% / 6-8%. The exchange is still its person's (`_engagements`), so the nameless question goes to "
        "/route at once. But its voice closed MEET_FOLLOWUP_S after the answer and opens again only once /route "
        "has named the agent (0.5-1.6 s), on top of: " + ACK,
    ),  # fmt: skip
    ("one_to_one", r'ack_within \{"line": 0', "31% / 3%. The reply is written beside /route now (`_prewrite`). What is left: " + ACK),
    ("*", r"ack_within", ACK),
    ("*", r"no_talk_over", TALK_OVER),
    (
        "backchannel", r"keeps_speaking",
        "10-24% / 8-10%. A listening sound known from its interim words no longer stops a sentence, and a mouth "
        "it paused goes on at the detector's stop. Left: one whose words come after TALK_OVER_S (the replay "
        "assumes 0.5 s, unmeasured: at 0.7 s this is 65%), and the pause before the next sentence while the "
        "detector still hears them. [timing]",
    ),  # fmt: skip
    ("backchannel", r"no_text \{\"pattern\": \"\[\^", CUT_SEEN),
    ("sidetalk", r'answer_contains \{"after": 0', QUIET),
    ("long_answer_cut", r"resumes_after", "1 seed in 200. " + QUIET),
    ("gap|sidetalk|sales_call|unnamed_sidetalk", r"silent_between", NO_LINE),
    ("gap|long_answer_cut|stop_mid_answer", r"stops_within", LEVEL_LATE),
    ("sidetalk|two_asks_one_agent|one_to_one|trailing|two_agents|unnamed_sidetalk", r"max_agent_lines", LINES),
]


def known(scenario: str, check: str) -> str | None:
    """Why this check of this scenario is known to fail in a replay, or None: a new failure."""
    return next((why for names, pattern, why in KNOWN_FAILING if names == "*" or scenario in names.split("|") if re.match(pattern, check)), None)
