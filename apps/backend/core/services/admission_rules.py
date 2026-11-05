"""Admission rules for non-chat sources (review C6, contracts-v2 §10). Pure, no I/O.

O's ``admission.py`` calls ``decide`` for every non-chat trigger and ``enforce`` on the
Router's decisions. Order:

1. An automation's own fire (schedule, Run now, verified webhook), work asked aloud in a
   call, and an admitted A2A request are deterministic: one ``start_task``, no model turn.
2. A message on a thread a task subscribes to passes through while the task is open; once
   it ended, bot/CI noise is dropped and a person's message is new work.
3. Bots and CI with no subscription are ignored.
4. Mail and text from people go to the Router (stranger caps already applied by the caller).

An unknown source raises (a caller typo must not fall through to "respond"). ``addressed`` is not read in v1:
an addressed bot message with no subscription is still dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, get_args

from core import tasks_schema as ts

DETERMINISTIC_SOURCES = frozenset({"schedule", "manual", "webhook", "voice", "a2a"})
_ENDED = frozenset({"resolved", "failed"})


@dataclass(frozen=True)
class SourceTrigger:
    source: ts.TriggerSource
    addressed: bool
    actor_kind: Literal["human", "bot", "ci", "system"]
    thread_key: str | None
    subscribed_task_status: str | None


@dataclass(frozen=True)
class Verdict:
    admission: Literal["respond", "candidate", "ignore", "pass_through"]
    deterministic: bool
    reason: str


def decide(t: SourceTrigger) -> Verdict:
    if t.source not in get_args(ts.TriggerSource):
        raise ValueError(f"unknown trigger source {t.source!r}")
    if t.source in DETERMINISTIC_SOURCES:
        return Verdict("respond", True, f"{t.source}_fire")
    bot = t.actor_kind in ("bot", "ci")
    if t.subscribed_task_status is not None:
        if t.subscribed_task_status not in _ENDED:
            return Verdict("pass_through", False, "subscribed_open")
        if bot:
            return Verdict("ignore", False, "ended_task_bot")
    if bot:
        return Verdict("ignore", False, "bot_unsubscribed")
    return Verdict("respond", False, f"{t.source}_from_person")


def _kind(d: Any) -> str | None:
    return d.get("kind") if isinstance(d, dict) else getattr(d, "kind", None)


def enforce(decisions: list, verdict: Verdict) -> list:
    """Drop what a verdict does not allow: ``ignore`` → nothing; deterministic → the first
    ``start_task`` only (anything else the model added is dropped)."""
    if verdict.admission == "ignore":
        return []
    if verdict.deterministic:
        return [d for d in decisions if _kind(d) == "start_task"][:1]
    return decisions
