"""The call's request list (conversation engine §3.4): one ask of one agent per row, held only in the
worker's memory for the length of the call; a worker restart loses it (owner ruling). Pure: no I/O and
no clock. huddle.py passes `now` and does every side effect."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

MAX_WORKING = 4  # the bridge runs at most 4 turns at once per microVM (turn.py MAX_CONCURRENT_TURNS)
TASK_CHARS = 60  # lightbulb.agent.<id>.working (C4)
LINE_CHARS = 120
MAX_LINES = 8  # TurnBody.requests
_VERB = {"queued": "is queued for", "working": "is answering", "ready": "has an answer ready on"}
OPEN = frozenset({"queued", "working", "ready"})


@dataclass(eq=False)
class Request:
    agent: str
    asker: str
    task: str
    created_at: float
    turn: int  # the transcript mark of the line that asked
    status: str = "queued"  # queued | working | ready | delivered | cancelled | failed
    no_post: bool = False
    routed: bool = False  # False: on the strip while /route splits the line; never started
    run_id: str | None = None


class RequestList:
    def __init__(self) -> None:
        self.items: list[Request] = []
        self.version = 0  # bumped on every change: the floor voice hears the list when it moves
        self._cancelled_turns: set[int] = set()  # turns whose pending rows were ALL cancelled before /route settled
        self._cancelled_asks: set[tuple[int, str]] = set()  # (turn, agent) pending rows one agent's stop cancelled
        self._suppressed: set[str] = set()  # runs with no row that "don't post that" named

    def _bump(self) -> None:
        self.version += 1

    def _move(self, rows: list[Request], to: str, came_from=OPEN) -> None:
        moved = [r for r in rows if r.status in came_from]
        for r in moved:
            r.status = to
        if moved:
            self._bump()

    def pending(self, turn: int, asker: str, agents: list[str], now: float) -> None:
        """Every agent a line names shows as working within the final's tick, before /route splits it."""
        self.items += [Request(a, asker, "", now, turn) for a in dict.fromkeys(agents)]
        self._bump()

    def drop_pending(self, turn: int) -> bool:
        kept = [r for r in self.items if r.routed or r.turn != turn]
        dropped = len(kept) != len(self.items)
        self.items = kept
        if dropped:
            self._bump()
        return dropped

    def settle(self, turn: int, asker: str, routed: list[dict], now: float) -> list[Request]:
        """/route's split, [{agentId, task}] in order, replaces the line's pending rows."""
        self.drop_pending(turn)
        if turn in self._cancelled_turns:
            return []
        first = {x["agentId"]: x for x in reversed(routed)}  # one row per agent: the first ask wins
        rows = [
            Request(x["agentId"], asker, str(x.get("task") or ""), now, turn, routed=True)
            for x in routed
            if first[x["agentId"]] is x and (turn, x["agentId"]) not in self._cancelled_asks
        ]
        self.items += rows
        self._bump()
        return rows

    def startable(self) -> list[Request]:
        """Queued rows that may start now, oldest first; rows that steered one run hold one slot."""
        busy = {r.run_id or id(r) for r in self.items if r.status == "working"}
        return [r for r in self.items if r.status == "queued" and r.routed][: max(MAX_WORKING - len(busy), 0)]

    def start(self, r: Request) -> None:
        self._move([r], "working", {"queued"})

    def bind(self, r: Request, run_id: str) -> None:
        r.run_id = run_id

    def by_run(self, run_id: str | None) -> list[Request]:
        return [r for r in self.items if run_id and r.run_id == run_id]

    def order(self, run_id: str) -> int:
        return next((i for i, r in enumerate(self.items) if r.run_id == run_id), len(self.items))

    def ready(self, run_id: str) -> None:
        self._move(self.by_run(run_id), "ready", {"working"})

    def delivered(self, run_ids: set[str]) -> None:
        self._move([r for r in self.items if r.run_id in run_ids], "delivered")

    def fail(self, r: Request) -> None:
        self._move([r], "failed")

    def cancel(self, agent: str | None = None) -> list[Request]:
        rows = [r for r in self.open() if agent in (None, r.agent)]
        pending = {(r.turn, r.agent) for r in rows if not r.routed}
        if agent is None:
            self._cancelled_turns |= {t for t, _ in pending}
        else:
            self._cancelled_asks |= pending
        self._move(rows, "cancelled")
        return rows

    def stopped(self, turn: int, agent: str | None = None) -> bool:
        """A stop said while /route split this turn's line: the whole turn, or this agent's ask in it."""
        return turn in self._cancelled_turns or (turn, agent) in self._cancelled_asks

    def no_post(self, agent: str | None = None) -> Request | None:
        """ "don't post that": the named agent's newest open request, else the room's newest."""
        rows = [r for r in self.open() if r.routed]
        r = ([x for x in rows if x.agent == agent] if agent else rows or [None])[-1:] or [None]
        r = r[0]
        if r is not None:
            r.no_post = True
            self._bump()
        return r

    def suppress(self, run_id: str) -> None:
        self._suppressed.add(run_id)

    def no_posted(self, run_id: str) -> bool:
        """"don't post that" named this run (a request row's, or a plain run's)."""
        return run_id in self._suppressed or any(r.no_post for r in self.by_run(run_id))

    def postable(self, run_id: str) -> bool:
        """False for "don't post that", and for a failed request (its CANT_REACH line is said, never posted)."""
        bad = any(r.no_post or r.status in ("failed", "cancelled") for r in self.by_run(run_id))
        return run_id not in self._suppressed and not bad

    def open(self) -> list[Request]:
        return [r for r in self.items if r.status in OPEN]

    def queued(self) -> list[Request]:
        return [r for r in self.items if r.status == "queued"]

    def working(self, agent: str) -> bool:
        return any(r.agent == agent and r.status in ("queued", "working") for r in self.items)

    def task(self, agent: str) -> str:
        rows = [r for r in self.items if r.agent == agent and r.status in ("queued", "working") and r.task]
        return rows[-1].task[:TASK_CHARS] if rows else ""

    def lines(self, name: Callable[[str], str], skip_agent: str | None = None) -> list[str]:
        out = [
            f"{name(r.agent)} {_VERB[r.status]}" + (f' "{r.task[:LINE_CHARS]}"' if r.task else "")
            for r in self.open()
            if r.routed and r.agent != skip_agent
        ]
        return out[-MAX_LINES:]


def ack_line(rows: list[Request], name: Callable[[str], str]) -> str:
    """The one acknowledgement of a multi-name turn (spec §3.5), said by the first row's agent."""

    def on(r: Request) -> str:
        return r.task.strip().rstrip(".?!") or "what was asked"

    others = "".join(f" {name(r.agent)} is on {on(r)}." for r in rows[1:])
    return f"You are on {on(rows[0])}.{others} Say that in one short sentence, then wait."
