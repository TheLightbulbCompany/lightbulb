"""Live eval of Jev's routing bar: real gateway calls, the exact production question wording.

    cd apps/backend && CLERK_ISSUER=https://eval.invalid PYTHONPATH=. uv run python tests/manual/jev_routing_eval.py   # AWS creds for the dev key

The bar (owner ruling 2026-09-24): "would a good employee in this room naturally respond?" A
shared link gets read and summarized, a question in their area answered, an indirect mention
chimed in on; chatter, thanks, personal matters and already-answered questions stay quiet.
Placement runs on the same bar, judged from context rather than rules: a threaded message can
deserve a main reply (a decision the whole room needs) and a top-level one a thread (a side question).
Exit 1 on any miss, judged on Jev's own pick (dispatch acts on it as is).
"""

import asyncio
import sys

from core.services import jev


CARDS = {
    "friday": "Friday. Chief of staff: researches, reads and summarizes links and documents, "
    "tracks commitments and follow-ups, answers questions about the company. Tools: web, browser.",
    "growth": "Growth. Marketing: social posts, launch copy, campaigns, audience research. Tools: web, x.",
}
# A card degrades to the name (plus role) when its SOUL/Paperclip reads fail or say little --
# the prod miss reproduced only with a thin card, so every case runs against both.
THIN = {"friday": "Friday. Chief of staff.", "growth": "Growth. Marketing."}
# What agent_card builds from the curated Friday package (its SOUL.md has no "What you do"): the
# shape the prod miss had. Old wording: Friday 0.16-0.18 -> none; this wording: 0.82-0.84 -> Friday.
CURATED = {
    "friday": "Friday (role: general). You make ambitious work tractable. Tools: none.",
    "growth": "Growth (role: cmo). Marketing. Tools: none.",
}
ROOM = {"kind": "group-dm", "name": "Humans + Friday"}

# (latest message, author, recent history, expected choice)
CASES = [
    ("We should study this as we talk to users: https://academy.claude.com/", "Prasiddha", [], "friday"),
    # The prod miss (2026-09-24 21:38Z): the same link dropped mid human-to-human conversation.
    (
        "We should study this as we talk to users: https://academy.claude.com/",
        "Prasiddha",
        [
            ("Leo", "Did you get a chance to look at the onboarding numbers?"),
            ("Prasiddha", "Yeah, activation is fine, retention after day 3 is the problem"),
            ("Leo", "Agreed. We need to talk to more users this week"),
            ("Prasiddha", "I'll set up five calls"),
        ],
        "friday",
    ),
    ("I'll send the investor update by Friday, remind me if I forget", "Prasiddha", [], "friday"),
    ("Let's plan the launch tweet for Thursday's pricing release", "Prasiddha", [], "growth"),
    ("What's the difference between our Workspace and Ultra plans again?", "Maya", [], "friday"),
    ("Maybe whoever handles our marketing has thoughts on the launch tagline?", "Maya", [], "growth"),
    (
        "How do we price the Ultra plan again?",
        "Maya",
        [("Maya", "How do we price the Ultra plan again?"), ("Friday", "Ultra is $150/month per workspace.")],
        "none",
    ),
    ("lol 😂", "Maya", [("Prasiddha", "the demo gremlins struck again")], "none"),
    ("thanks Friday, that's perfect", "Prasiddha", [("Friday", "Here's the summary you asked for: …")], "none"),
    ("Maya, did your mom's surgery go ok?", "Prasiddha", [], "none"),
]

# One agent listening alone (prod 2026-09-25 #GTM Strategy: none 0.51 vs Pulse 0.49, silence).
GTM = "Guys we gotta talk more, what is going on in this channel ? What about the tasks on the board related to this goal?"
SOLO = [
    (GTM, "Prasiddha", [], "friday"),
    ("brb", "Prasiddha", [("Leo", "one sec, grabbing coffee")], "none"),
    ("lol", "Maya", [("Leo", "the demo gremlins struck again")], "none"),
]

# Who the message speaks to comes before role fit (eval 2026-09-27: the old wording missed 13 of
# 30). Only messages that REACH Jev: an opener like "Hey Forge, ..." is a mention before dispatch
# (messaging.select_dispatch_targets) and never gets here.
TEAM = {
    "friday": "Friday (role: general). Chief of staff: researches, reads and summarizes links and documents, "
    "tracks commitments and follow-ups, answers questions about the company. Tools: browser, web.",
    "forge": "Forge (role: engineer). Builds and fixes software and infrastructure. Tools: gh, aws.",
    "pulse": "Pulse (role: cmo). Marketing, launches, campaigns, newsletters, demo videos, social posts. Tools: web, x.",
}
TEAM_THIN = {
    "friday": "Friday (role: general). You make ambitious work tractable. Tools: none.",
    "forge": "Forge (role: engineer). Builds and fixes software and infrastructure. Tools: none.",
    "pulse": "Pulse (role: cmo). Marketing, launches, campaigns, newsletters. Tools: none.",
}
DEMO_CHAT = [
    ("Prasiddha", "Friday, I want you to focus on the demo videos: which features we show and how"),
    ("Friday", "On it. I'll draft the demo storyboard and try recording the first flow."),
]
# (latest message, recent history, the latest's thread (None = top level), expected)
SPOKEN_TO = [
    # The name wins over the role: a newsletter is the marketer's, and Forge was asked.
    ("I think Forge should own the newsletter this week, can you take it?", [], None, "forge"),
    ("Hey Forge I need three taglines for the launch", [], None, "forge"),
    # A follow-up stays with whoever the person was talking to, whatever the topic fits.
    (
        "make the second one shorter and punchier",
        [],
        [
            ("Prasiddha", "Friday, draft three options for the launch announcement post"),
            ("Friday", "Here are three options: 1) ... 2) ... 3) ..."),
        ],
        "friday",
    ),
    (
        "yes go ahead",
        [],
        [
            ("Prasiddha", "Forge, checkout is throwing a 500 for EU cards, can you look?"),
            ("Forge", "Found it: the 3DS callback times out. I have a fix ready, want me to open a PR?"),
        ],
        "forge",
    ),
    ("ok and which channels should we post it in?", DEMO_CHAT, None, "friday"),
    # Addressed to a person: nobody butts in, however well the topic fits.
    ("Leo, can you review the pricing page copy today?", [], None, "none"),
    ("thanks Friday, that's perfect", [("Friday", "Here's the summary you asked for: ...")], None, "none"),
    # Nobody is named: role fit still decides.
    ("Can someone draft the launch tweet for Thursday?", [], None, "pulse"),
    ("why is the deploy pipeline red?", [], None, "forge"),
]

LAUNCH_CHAT = [
    ("Prasiddha", "Launch moves to Tuesday, everyone please update your plans"),
    ("Leo", "Got it. Pricing page copy is ready for review"),
    ("Maya", "I'll review it this afternoon"),
]
CHANNEL = {"kind": "channel", "name": "general"}
DM = {"kind": "dm", "name": None}
BUG_THREAD = [
    ("Leo", "Checkout is throwing a 500 for some EU cards"),
    ("Maya", "Seeing it too, only on 3DS cards I think"),
    ("Leo", "Yep, 3DS only. Stripe logs show a timeout"),
]
PTO_THREAD = [
    ("Maya", "Quick q about PTO during launch week"),
    ("Prasiddha", "Let me think about it"),
]
# (room, latest message, author, recent history, the latest's thread (None = top level), expected)
PLACEMENT_CASES = [
    (
        ROOM,
        "Heads up everyone: we're switching our support inbox to tickets@ starting Monday",
        "Prasiddha",
        [],
        None,
        "main",
    ),
    (CHANNEL, "What time is the all-hands tomorrow?", "Maya", [], None, "main"),
    # Top-level messages that deserve a thread.
    (
        CHANNEL,
        "Side question, does anyone remember the login for the old analytics dashboard?",
        "Maya",
        LAUNCH_CHAT,
        None,
        "thread",
    ),
    (
        CHANNEL,
        "Can you draft three launch tweet options for Tuesday? I'll give feedback on each",
        "Prasiddha",
        LAUNCH_CHAT,
        None,
        "thread",
    ),
    # Threaded messages: an active narrow exchange stays; a whole-team decision goes to main.
    (
        CHANNEL,
        "Which Stripe API version are we pinned to? Could be the timeout",
        "Leo",
        LAUNCH_CHAT,
        BUG_THREAD,
        "thread",
    ),
    (
        CHANNEL,
        "Decided: nobody takes PTO during launch week, for the whole team. Can someone make sure everyone knows?",
        "Prasiddha",
        LAUNCH_CHAT,
        PTO_THREAD,
        "main",
    ),
    (DM, "What's on my plate for tomorrow?", "Prasiddha", [], None, "main"),
    (
        DM,
        "and what about the day after?",
        "Prasiddha",
        [],
        [("Prasiddha", "Summarize the Q3 board deck for me"), ("Friday", "Here are the five points: …")],
        "thread",
    ),
]


def placement_questions() -> dict:
    return {
        "placement": {"type": "choice", "instructions": jev.PLACEMENT_INSTRUCTIONS, "criteria": jev.PLACEMENT_CRITERIA}
    }


def _lines(pairs: list) -> list:
    agents = ("Friday", "Growth", "Forge", "Pulse")
    return [{"author": f"{a} ({'agent' if a in agents else 'human'})", "text": t} for a, t in pairs]


def _state(room: dict, text: str, author: str, recent: list, thread: list | None = None) -> dict:
    """The shape jev._state builds: the trigger's thread (when it is in one) ahead of recent."""
    state: dict = {"room": room}
    if thread:
        state["thread"] = _lines(thread)
    state["recent"] = _lines(recent)
    state["latest"] = {"author": f"{author} (human)", "text": text, "inThread": bool(thread)}
    return state


async def _decide(state: dict, questions: dict):
    for _ in range(4):  # the gateway throttles bursts; production retries through the queue
        answers = await jev.decide(state, questions)
        if answers != jev.RATE_LIMITED:
            return answers
        await asyncio.sleep(5)
    return answers


def questions(cards: dict) -> dict:
    return {
        "who": {
            "type": "choice",
            "instructions": jev.WHO_INSTRUCTIONS,
            "criteria": {**cards, "none": jev.NONE_CRITERION},
        }
    }


async def main() -> int:
    misses = 0
    runs = [(label, cards, CASES) for label, cards in (("rich", CARDS), ("thin", THIN), ("curated", CURATED))]
    runs += [("solo", {"friday": CARDS["friday"]}, SOLO), ("solo-thin", {"friday": THIN["friday"]}, SOLO)]
    for label, cards, cases in runs:
        for text, author, recent, expected in cases:
            room = CHANNEL if cases is SOLO else ROOM
            answers = await _decide(_state(room, text, author, recent), questions(cards))
            who = (answers or {}).get("who", {}) if isinstance(answers, dict) else {}
            probabilities = who.get("probabilities") or {}
            got = who.get("choice", repr(answers))
            p = probabilities.get(got)
            hit = got == expected
            misses += not hit
            print(
                f"{'ok  ' if hit else 'MISS'} {label:<9} {expected:>6} <- {got:<6} p={p} jev={who.get('choice')} {probabilities} {text[:50]!r}"
            )
    for label, cards in (("team", TEAM), ("team-thin", TEAM_THIN)):
        for text, recent, thread, expected in SPOKEN_TO:
            answers = await _decide(_state(CHANNEL, text, "Prasiddha", recent, thread), questions(cards))
            who = (answers or {}).get("who", {}) if isinstance(answers, dict) else {}
            probabilities = who.get("probabilities") or {}
            got = who.get("choice", repr(answers))
            misses += got != expected
            print(
                f"{'ok  ' if got == expected else 'MISS'} {label:<9} {expected:>6} <- {got:<6} p={probabilities.get(got)} jev={who.get('choice')} {probabilities} {text[:50]!r}"
            )
    for room, text, author, recent, thread, expected in PLACEMENT_CASES:
        answers = await _decide(_state(room, text, author, recent, thread), placement_questions())
        placement = (answers or {}).get("placement", {}) if isinstance(answers, dict) else {}
        got = placement.get("choice", repr(answers))
        misses += got != expected
        p = (placement.get("probabilities") or {}).get(got)
        print(f"{'ok  ' if got == expected else 'MISS'} place   {expected:>6} <- {got:<6} p={p} {text[:50]!r}")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
