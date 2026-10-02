"""Live's speak-or-stay-silent gate: at a pause, should Lightbulb start talking unprompted?

The ONE copy of the prompt. `scripts/speak_gate_bench` imports it, and the `news` variant is
the one that passed the ship bar there (results/2026-10-01-news.md: P(speak) >= 0.85 gave
SPEAK precision 1.00, follow-up recall 1.00, news recall 0.87, and full silence on mentions,
addressed lines, backchannels, bystanders and mid-thought pauses). Change the wording only
together with a bench re-run.
"""

from __future__ import annotations

import asyncio
import logging

from core.services import jev

logger = logging.getLogger(__name__)

TIMEOUT_S = 1.5  # a late answer is a missed slot: the floor is gone by then
RECENT_TURNS = 12

# ------------------------------------------------------------------ the report's §2.5 prompt
SPEAK_INSTRUCTIONS = (
    "Lightbulb is an AI teammate listening to a live voice call among colleagues. A pause just "
    "happened after the latest line. Decide whether Lightbulb should START TALKING now or STAY SILENT. "
    "First decide Lightbulb's role in the current exchange: ACTIVE (Lightbulb spoke in the last few "
    "lines, was asked something, or the people are reacting to what Lightbulb just said) or BYSTANDER "
    "(people are talking to each other). Speak when the latest line is a follow-up to Lightbulb's last "
    "answer, when it is a question put to the whole room that nobody has answered and Lightbulb can "
    "answer it, or when Lightbulb has news the room is waiting for and the conversation has reached a "
    "natural break. Stay silent when the line is addressed to a person by name, when Lightbulb is only "
    "mentioned or talked about, when the speaker is mid-thought, for thanks, backchannels and "
    "chit-chat, and whenever Lightbulb is a bystander and unsure. Talking over people is worse than "
    "staying quiet: when in doubt, stay silent."
)
CRITERIA = {
    "speak": "SPEAK: Lightbulb should talk now (a follow-up to Lightbulb, an unanswered question to the "
    "room it can answer, or awaited news at a natural break)",
    "silent": "SILENT: people are talking to each other, Lightbulb is only mentioned, the speaker is "
    "mid-thought, or it is chatter, thanks or a backchannel",
}
#: The report's diagnostic question: a per-category "why" (Jev returns no free text).
KIND_CRITERIA = {
    "I1": "Lightbulb is addressed by name and asked something",
    "I2a": "A follow-up to what Lightbulb just said, without its name",
    "I2b": "An open question to the whole room that nobody has answered",
    "N1": "Lightbulb has awaited news and the conversation is at a natural break",
    "S1": "People talking to each other; Lightbulb is a bystander",
    "S2": "Lightbulb is mentioned or talked about, not addressed",
    "S3": "The speaker is mid-thought; the line is incomplete",
    "S4": "The line is addressed to a person by name",
    "S5": "A backchannel, acknowledgement or thanks",
}
#: The §2.5 prompt reads "news the room is waiting for", and Jev took that literally -- it held
#: back unsolicited results at a natural break. This variant makes any pending result worth
#: sharing once the floor is free; everything else is unchanged. It is what ships.
NEWS_INSTRUCTIONS = SPEAK_INSTRUCTIONS.replace(
    "or when Lightbulb has news the room is waiting for and the conversation has reached a natural break.",
    "or when Lightbulb has news to share (every item in lightbulb.news is a finished result the room "
    "wants to hear, asked for or not) and the conversation has reached a natural break: a topic just "
    "wrapped up, an acknowledgement, a lull, or a question the news answers.",
)
NEWS_CRITERIA = {**CRITERIA, "speak": CRITERIA["speak"].replace("awaited news", "pending news")}
PROMPTS = {"report": (SPEAK_INSTRUCTIONS, CRITERIA), "news": (NEWS_INSTRUCTIONS, NEWS_CRITERIA)}


def questions(prompt: str = "news") -> dict:
    instructions, criteria = PROMPTS[prompt]
    return {
        "turn": {"type": "choice", "instructions": instructions, "criteria": criteria},
        "kind": {
            "type": "choice",
            "instructions": "Which situation best describes the latest line, from Lightbulb's point of view?",
            "criteria": KIND_CRITERIA,
        },
    }


def build_state(
    people: list[str], lines: list[tuple[str, str]], lightbulb: dict | None = None, pause_ms: int = 900
) -> dict:
    """The bench's state shape: the last line is `latest`, up to 12 before it are `recent`."""
    turns = [{"author": a, "text": t} for a, t in lines] or [{"author": "(nobody)", "text": "(silence)"}]
    state = {
        "room": {"kind": "call", "people": people, "ai": "Lightbulb"},
        "recent": turns[:-1][-RECENT_TURNS:],
        "latest": {**turns[-1], "pauseMs": pause_ms},
    }
    if lightbulb:
        state["lightbulb"] = lightbulb
    return state


async def decide(state: dict) -> tuple[float, str | None]:
    """(P(speak), Jev's diagnostic kind). Silent -- (0.0, None) -- on a timeout or any failure."""
    try:
        answers = await asyncio.wait_for(jev.decide(state, questions("news")), TIMEOUT_S)
    except Exception:  # noqa: BLE001 -- TimeoutError included: a failure is silence, never an error
        logger.info("speak gate: no answer in time")
        return 0.0, None
    if not isinstance(answers, dict):
        return 0.0, None
    turn, kind = answers.get("turn") or {}, answers.get("kind") or {}
    try:
        p = float((turn.get("probabilities") or {}).get("speak") or 0.0)
    except (TypeError, ValueError):
        p = 0.0
    choice = kind.get("choice")
    return p, choice if choice in KIND_CRITERIA else None
