"""Live eval of Jev's routing bar: real gateway calls, the exact production question wording.

    cd apps/backend && CLERK_ISSUER=https://eval.invalid PYTHONPATH=. uv run python tests/manual/jev_routing_eval.py   # AWS creds for the dev key

The bar (owner ruling 2026-09-24): "would a good employee in this room naturally respond?" A
shared link gets read and summarized, a question in their area answered, an indirect mention
chimed in on; chatter, thanks, personal matters and already-answered questions stay quiet.
Exit 1 on any miss, including a right pick below dispatch's JEV_MIN_P.
"""

import asyncio
import sys

from core.services import jev


def jev_min_p() -> float:
    from core.services.openclaw_work import JEV_MIN_P

    return JEV_MIN_P


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
    for label, cards in (("rich", CARDS), ("thin", THIN), ("curated", CURATED)):
        for text, author, recent, expected in CASES:
            state = {
                "room": ROOM,
                "recent": [
                    {"author": f"{a} ({'agent' if a in ('Friday', 'Growth') else 'human'})", "text": t}
                    for a, t in recent
                ],
                "latest": {"author": f"{author} (human)", "text": text},
            }
            for _ in range(4):  # the gateway throttles bursts; production retries through the queue
                answers = await jev.decide(state, questions(cards))
                if answers != jev.RATE_LIMITED:
                    break
                await asyncio.sleep(5)
            who = (answers or {}).get("who", {}) if isinstance(answers, dict) else {}
            got = who.get("choice", repr(answers))
            p = (who.get("probabilities") or {}).get(got)
            # Dispatch also needs the pick's probability >= JEV_MIN_P, so a low-confidence pick is a miss.
            hit = got == expected and (got == "none" or (p or 0) >= jev_min_p())
            misses += not hit
            print(f"{'ok  ' if hit else 'MISS'} {label:<7} {expected:>6} <- {got:<6} p={p} {text[:50]!r}")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
