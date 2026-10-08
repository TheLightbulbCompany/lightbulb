"""What each scenario line IS, for synthetic replays: the table the simulated classifier reads.

The router is the backend's own (`router.py` runs `voice_calls.route_live_line` on the worker's /route
body). What is assumed here is one step earlier: what a reader who understood the line would say it is.
`router.Jev` turns that into the classifier's labels, question by question, and gets some wrong at seeded
rates. So a replay can now show a router mistake: a named ask not taken, people talk taken as a request,
a follow-up merged into a run that is still working. A recorded call replays the router's real answers.

Per scenario: line index -> verdict. One of
  ask     the line asks this agent for something; its run answers with ANSWERS[`answer`]. `task` is a note
          for the reader: the router writes the task from the line's own words. `follows`, or `by: follow`:
          it continues what that agent was asked before (the classifier's `amends`)
  social  small talk for this agent, which says `reply` (the writer's line)
  called  it turns to this agent and asks nothing new ("go ahead", "Friday?")
  stop    "cancel": the speaker withdrew what this agent has open; "hush": stop talking, the work stands
  people  talk between people
  hold    an unfinished sentence: its rest is on the way
A line with no entry asks nothing of anyone ("Okay, thanks.", a listening sound).
`when`: a regex the routed words must match for the verdict to hold, and `else` the verdict when they do not.
The transcriber may hand a line over in pieces, and only the piece with the request in it is the request.
`unclaimed`, `by: room`: notes only. Whether a question to the room is an agent's is the router's to decide.
"""

import json
import math
import os
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
        4: {"ask": "friday", "task": "what is on their calendar on Thursday", "answer": "thursday", "follows": True},
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

# Checks a replay fails today, and why: (scenario or "*", a regex on the check, the cause). An entry is no
# licence: each names the rule to change, and how often each check fails is held to its baseline
# (`gate`, below; tests/test_voice_replay.py): a known failure that gets worse fails the gate like a new one.
# Shares are of 200 seeds on 2026-10-08, as `current` / `fast-eot` (`sim.Model`: the second is a what-if
# transcriber, not a measurement); baseline.json has the seeds. The shares quoted in the causes from ACK
# to CUT_SEEN were measured with a router that understood every line and runs of 5-12 s: with the
# backend's own router, a classifier that errs and runs that come late, each check fails more often
# (baseline.json), and `python -m tests.replay all` prints today's count by cause (`CAUSES`). Each cause ends with where it lies:
# [controller] a rule of huddle.py, [timing] the transcriber's or the voice's delays, [policy] a rule and the
# scenario disagree and a person has to choose, [check] check.py or the scenario's own limit.
ACK = (
    "acknowledgement latency sits at the 4 s limit (9-23% / 0-3%): the final transcript comes 1.2-1.9 s "
    "after the line, then /route, the voice, its first audio. A silent first unit or a person talking again "
    "adds seconds (`_turn_open` holds every line on an open turn). [timing] A call's first line also waits "
    "for its window (/summon): `sim.Model.window` makes 10% of them take 2-5 s, after the one the live run "
    "showed (4.7 s), and nothing is said before a window is open. [backend]"
)
TALK_OVER = (
    "a mouth yields TALK_OVER_S (0.5 s) after the room hears the person, itself 0.4-0.6 s after they start "
    "(`_yield_check`): 1.0-1.5 s of agent audio is over a line that starts on a sentence well under way "
    "(status_check 82% / 16%; the others 6-29% / 0-7%). A sentence whose first sound only just went out "
    "yields at once (PREROLL_S). [timing]"
)
COLLISION = (
    "an acknowledgement at the 4 s limit, or an answer at a pause, starts within the detector's lag of a "
    "person who has just begun (0.4-0.6 s; 0.2-0.3 s): its first words are over them, it yields at once "
    "(PREROLL_S), and the piece that was audible is on the record (14-32% / 13-35%). Nothing in the room "
    "says a person began until the detector does. Rarely the piece is under a word and leaves no line, "
    "which check.py also fails. [timing]"
)
LEVEL_LATE = (
    "the SFU's level for the talker came late (up to 2.5 s). Until then the detector alone holds what has "
    "not begun but stops no sentence being said (`_yield_check`): a false stop costs the sentence. 2.5% / 1%. "
    "The other way round (the level alone holding a start) was measured with `--noise` and not done. [policy]"
)
QUIET = (
    "an answer nobody is plainly waiting for needs MEET_QUIET_S (3 s) of quiet, then its voice reopened and "
    "its first audio: its first sentence (5.8 s) cannot end inside the 6.8 s pause unless it starts within "
    "1 s of the speech before, and the final transcript alone comes later than that (100% / 100%). Tried "
    "and reverted: counting the quiet from the end of speech; the floor being free and then a bid of a few "
    "words, an opening and the sentence (still 100% / 100%, and speech between people's lines 24-33% -> "
    "89-97%); the sentence as soon as the floor is free (100% / 66%, and speech between people's lines "
    "24-33% -> 55-92%). It needs the turn's end known sooner and a shorter first sentence. [timing] [policy]"
)
LINES = (
    "sidetalk (27% / 77%): the calendar answer comes after line 4 (see answer_contains), so two "
    "acknowledgements, its two sentences and the Thursday answer are five lines against a limit of four. "
    "It passes when the answer never gets said whole. [check] The others (0-5% / 0-4%): the voice's own "
    "tail put more than 3 s between an acknowledgement's two sentences (a quiet lead-in, a long trailing "
    "silence), which check.py then counts as two lines. [timing]"
)
CUT_SEEN = (
    "a listening sound whose first interim words come after TALK_OVER_S still makes the mouth yield "
    "(`_only_listening`). Every cut sentence a word of which was audible is now on the record as a marked "
    "piece, so this check sees each of them (47% / 33%; it saw only those cut after 80%, 10% / 9%, before "
    "the delivery rule changed: the cuts are the same ones, keeps_speaking did not move). [timing]"
)
# What the simulated backend does to a call (`sim.Evidence.facts`, `CAUSES`). Shares are of 200 seeds.
RUN_LATE = (
    "a run that takes 45-110 s or never ends (`sim.Model.run`: 12% and 6% of runs, sized from the "
    "2026-10-08 live run): its answer is said after the lines that should follow it, or not inside the "
    "scenario at all, and the answer of a row behind it waits with it. [backend]"
)
ASK_MISSED = (
    "the classifier misses a named ask (`router.P_NAMED_MISS`, `P_WEAK`): no request is made for the line, "
    "so no run, no acknowledgement and no answer. [router]"
)
PEOPLE_TAKEN = (
    "the classifier reads talk between people as a request while its speaker is still engaged with an "
    "agent (`router.P_PEOPLE_ASK_ENGAGED`), and /route gives the line to that agent: a run nobody asked "
    "for, acknowledged and answered aloud. [router]"
)
STEERED = (
    "a follow-up said while the first run is still working is merged into that run (`ops.steer`) and "
    "acknowledged (STEER_ACKS): no request of its own (one_run counts created rows only), and its answer "
    "comes with the first one's, after the line or after the scenario. [router] [check]"
)
TOLD = (
    "the worker tells the asker of a slow run that it is still working (RUN_STATUS_S), and of one with no end "
    "that it could not be done (RUN_DEADLINE_S). Each is one more agent line than a scenario's limit counts, "
    "the second is speech where a scenario allows only the owed answer, and a script line timed `over_agent` "
    "is said into the status line and not the answer, so the answer comes after the replay has ended "
    "(backchannel, long_answer_cut). The rig would do the same. [check]"
)
KNOWN_FAILING: list[tuple[str, str, str]] = [
    (
        "unnamed_followup", r'ack_within \{"line": 1',
        "54% / 7%. The exchange is still its person's (`_engagements`), so the nameless question goes to "
        "/route at once. But its voice closed MEET_FOLLOWUP_S after the answer and opens again only once /route "
        "has named the agent (0.5-1.6 s), on top of: " + ACK,
    ),  # fmt: skip
    ("one_to_one", r'ack_within \{"line": 0', "32% / 3%. The reply is written beside /route (`_prewrite`). What is left: " + ACK),
    ("*", r"ack_within", ACK),
    ("*", r"no_talk_over", TALK_OVER),
    (
        "backchannel", r"keeps_speaking",
        "18% / 8%. A listening sound known from its interim words no longer stops a sentence, and a mouth "
        "it paused goes on at the detector's stop. Left: one whose words come after TALK_OVER_S (the replay "
        "assumes 0.5 s, unmeasured: at 0.7 s this is 65%), and the pause before the next sentence while the "
        "detector still hears them. [timing]",
    ),  # fmt: skip
    ("backchannel", r"no_text \{\"pattern\": \"\[\^", CUT_SEEN),
    ("sidetalk", r'answer_contains \{"after": 0', QUIET),
    ("long_answer_cut", r"resumes_after", "1 seed in 200 / none. " + QUIET),
    ("gap|sidetalk|sales_call", r"silent_between", COLLISION),
    ("gap", r"stops_within", LEVEL_LATE),
    ("sidetalk|two_asks_one_agent|trailing|unnamed_sidetalk", r"max_agent_lines", LINES),
    # the simulated backend's part (`CAUSES` says which failing replay is whose): after every cause above
    ("*", r"one_run", ASK_MISSED + " Also: " + STEERED),
    ("*", r"no_new_run|max_runs|max_agents|nothing_after|max_words", PEOPLE_TAKEN),
    ("*", r"answer_contains|min_sentences|in_order|resumes_after|stops_within|keeps_speaking", RUN_LATE + " Also: " + ASK_MISSED + " Also: " + TOLD),
    ("*", r"silent_between|max_agent_lines|said_once|no_text|no_agent_overlap", PEOPLE_TAKEN + " Also: " + RUN_LATE + " Also: " + TOLD),
]


# What a failing check of a replay comes from, read from what the simulated backend did in that replay
# (`sim.Evidence.facts`): the first cause whose fact is there and whose checks include this one. A replay
# with none of them failed on the controller's own rules and timing (KNOWN_FAILING says which).
CAUSES: list[tuple[str, str, str]] = [
    ("router: a named ask was not taken", "ask_missed", r"one_run|ack_within|answer_contains|min_sentences|in_order|max_words|stops_within|resumes_after|keeps_speaking"),
    ("router: people talk taken as a request", "people_taken|other_taken", r"no_new_run|max_runs|silent_between|max_agent_lines|nothing_after|max_words|no_talk_over|said_once|max_agents"),
    ("router: a follow-up steered the open run", "steer", r"one_run|ack_within|answer_contains|max_agent_lines|said_once|in_order"),
    ("a slow run's asker was told it is still working, or that it could not be done", "told_status|told_gave_up", r"min_sentences|resumes_after|keeps_speaking|max_agent_lines|max_words|no_talk_over|silent_between|nothing_after"),
    ("the call's window opened slowly", "window_slow", r"ack_within|no_talk_over|silent_between|max_agent_lines"),
    ("a run came late or never", "run_never|run_slow", r"answer_contains|min_sentences|in_order|stops_within|resumes_after|keeps_speaking|silent_between|max_agent_lines|said_once|nothing_after|no_talk_over|max_words|no_text|one_run|ack_within"),
]
OWN = "the controller's rules and timing"


def cause(check: str, facts: dict[str, int]) -> str:
    return next((c for c, fact, checks in CAUSES if any(facts.get(f) for f in fact.split("|")) and re.match(checks, check)), OWN)


def known(scenario: str, check: str) -> str | None:
    """Why this check of this scenario is known to fail in a replay, or None: a new failure."""
    return next((why for names, pattern, why in KNOWN_FAILING if names == "*" or scenario in names.split("|") if re.match(pattern, check)), None)


# Which seeds each check failed when the baseline was last taken: {profile: {"seeds": n, "fails": {"scenario |
# check": [seed, ...]}}}, written by `python -m tests.replay all --seeds 200 --profile P --rebase`. A known
# failure is known at a rate: KNOWN_FAILING says why a check may fail, this says how often it did, and on
# which replays.
BASELINE_PATH = os.path.join(os.path.dirname(__file__), "baseline.json")
# The tolerance. A replay is the same every time, so a run is compared with the baseline seed for seed, and
# what differs is the controller. A change that only moves delays about flips a few replays each way
# (measured: at most 4 of 100 a check, 1% of the total), so a check may fail SLACK seeds more than it did
# on the same seeds, and one more for every 20 seeds; all checks together 3% more, and 2.
SLACK = 2
# Seeds the baseline does not hold cannot be paired: there a check may fail as often as chance gives it at
# its baseline rate less than once in CHANCE runs, and the total SIGMAS standard deviations over.
CHANCE = 1e-4
SIGMAS = 3.0


def baseline() -> dict:
    with open(BASELINE_PATH) as f:
        return json.load(f)


def allowed(base: dict, key: str, seeds: range) -> int:
    """The most of `seeds` this check may fail and still be no worse than its baseline. 0 for a check that
    passed every seed of the baseline: one that used to pass may not start to fail."""
    failed, n = base["fails"].get(key, []), len(seeds)
    if not failed:
        return 0
    if seeds.start >= 0 and seeds.stop <= base["seeds"]:
        return sum(s in seeds for s in failed) + SLACK + n // 20
    p, tail = len(failed) / base["seeds"], 1.0  # the chance of more than k failures in n seeds at rate p
    for k in range(n + 1):
        tail -= math.comb(n, k) * p**k * (1 - p) ** (n - k)
        if tail < CHANCE:
            return k
    return n


def gate(base: dict, fails: dict[str, int], seeds: range) -> list[str]:
    """What a run over `seeds` of each scenario shows that the baseline did not: each check that failed more
    often than it may (also one inside KNOWN_FAILING), each that used to pass, and the run as a whole when
    its failures are up though no one check is. `fails`: "scenario | check" -> how many seeds failed."""
    out, n = [], len(seeds)
    for key, got in sorted(fails.items()):
        if got > (most := allowed(base, key, seeds)):
            was = len(base["fails"].get(key, []))
            what = f"WORSE (baseline {was}/{base['seeds']}, at most {most}/{n} here)" if was else "NEW (it passed every seed of the baseline)"
            out.append(f"{key}: fails {got}/{n}: {what}")
    if seeds.start >= 0 and seeds.stop <= base["seeds"]:
        most = 1.03 * sum(s in seeds for failed in base["fails"].values() for s in failed) + 2
    else:
        rates = [len(failed) / base["seeds"] for failed in base["fails"].values()]
        most = sum(n * p for p in rates) + SIGMAS * math.sqrt(sum(n * p * (1 - p) for p in rates))
    if (total := sum(fails.values())) > most:
        out.append(f"all checks together: {total} failing check-seeds, at most {most:.0f} for the baseline")
    return out
