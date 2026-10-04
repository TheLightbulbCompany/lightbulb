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
_VERB = {
    "queued": "is queued for",
    "working": "is answering",
    "ready": "has an answer ready on",
}
OPEN = frozenset({"queued", "working", "ready"})
ENDED = frozenset({"delivered", "cancelled", "failed"})


@dataclass(eq=False)
class Request:
    id: str  # "r1", "r2", ... never reused: the room names a row by it (understanding §4.5)
    agent: str
    asker: str
    task: str
    created_at: float
    turn: int  # the transcript mark of the line that asked
    status: str = "queued"  # queued | working | ready | delivered | cancelled | failed
    no_post: bool = False
    routed: bool = (
        False  # False: on the strip while /route splits the line; never started
    )
    run_id: str | None = None
    by: str = ""  # which routing rule chose the owner: name | fit | floor | chief
    acked: bool = False  # named aloud by an acknowledgement or a "next in line" note (understanding §4.6)


class RequestList:
    def __init__(
        self, on_change: Callable[[Request, str | None], None] | None = None
    ) -> None:
        # (row, its status before; None for a new row) after every status change: huddle.py's metrics and
        # transcript lines (understanding §5.3)
        self._on_change = on_change or (lambda r, was: None)
        self.items: list[Request] = []
        self._ids = 0
        self.version = (
            0  # bumped on every change: the floor voice hears the list when it moves
        )
        # Transcript marks of the latest stops: a line said at or before one starts nothing, even one still at
        # /route when the stop was said (understanding §4.5)
        self._stop_all = -1
        self._stop_at: dict[str, int] = {}
        self._suppressed: set[str] = (
            set()
        )  # runs with no row that "don't post that" named

    def _bump(self) -> None:
        self.version += 1

    def _id(self) -> str:
        self._ids += 1
        return f"r{self._ids}"

    def _move(self, rows: list[Request], to: str, came_from=OPEN) -> None:
        moved = [r for r in rows if r.status in came_from]
        for r in moved:
            was, r.status = r.status, to
            self._on_change(r, was)
        if moved:
            self._bump()

    def pending(self, turn: int, asker: str, agents: list[str], now: float) -> None:
        """Every agent a line names shows as working within the final's tick, before /route splits it."""
        self.items += [
            Request(self._id(), a, asker, "", now, turn) for a in dict.fromkeys(agents)
        ]
        self._bump()

    def drop_pending(self, turn: int) -> bool:
        kept = [r for r in self.items if r.routed or r.turn != turn]
        dropped = len(kept) != len(self.items)
        self.items = kept
        if dropped:
            self._bump()
        return dropped

    def settle(
        self, turn: int, asker: str, routed: list[dict], now: float
    ) -> list[Request]:
        """/route's requests, [{agentId, task, by?}] in order, replace the line's pending rows. An agent may
        have several rows from one line: it works on them one at a time (understanding §4.5)."""
        self.drop_pending(turn)
        if self.stopped(turn):
            return []
        rows = [
            Request(
                self._id(),
                x["agentId"],
                asker,
                str(x.get("task") or ""),
                now,
                turn,
                routed=True,
                by=x.get("by") or "",
            )
            for x in routed
            if not self.stopped(turn, x["agentId"])
        ]
        self.items += rows
        for r in rows:
            self._on_change(r, None)
        self._bump()
        return rows

    def startable(self) -> list[Request]:
        """Queued rows that may start now, oldest first: one per agent, never an agent already working, at most
        MAX_WORKING working in all."""
        working = [r for r in self.items if r.status == "working"]
        taken = {r.agent for r in working}
        out: list[Request] = []
        for r in self.items:
            if r.status == "queued" and r.routed and r.agent not in taken:
                taken.add(r.agent)
                out.append(r)
        return out[: max(MAX_WORKING - len(working), 0)]

    def get(self, request_id: str) -> Request | None:
        return next((r for r in self.items if r.id == request_id), None)

    def for_route(self) -> list[dict]:
        """What /route is told (understanding §4.4): the open routed rows, then the newest delivered three; past
        12 the oldest open rows go first (the newest are the ones a line can amend)."""
        done = [r for r in self.items if r.status == "delivered" and r.routed][-3:]
        rows = [r for r in self.items if r.status in OPEN and r.routed][
            -(12 - len(done)) :
        ] + done
        return [
            {"id": r.id, "agentId": r.agent, "task": r.task[:200], "status": r.status}
            for r in rows
        ]

    def start(self, r: Request) -> None:
        self._move([r], "working", {"queued"})

    def unstart(self, r: Request) -> None:
        """A row started for a voice that never opened: back in line, it runs without one."""
        self._move([r], "queued", {"working"})

    def bind(self, r: Request, run_id: str) -> None:
        r.run_id = run_id

    def by_run(self, run_id: str | None) -> list[Request]:
        return [r for r in self.items if run_id and r.run_id == run_id]

    def order(self, run_id: str) -> int:
        return next(
            (i for i, r in enumerate(self.items) if r.run_id == run_id), len(self.items)
        )

    def ready(self, run_id: str) -> None:
        self._move(self.by_run(run_id), "ready", {"working"})

    def delivered(self, run_ids: set[str]) -> None:
        self._move([r for r in self.items if r.run_id in run_ids], "delivered")

    def fail(self, r: Request) -> None:
        self._move([r], "failed")

    def drop(self, r: Request) -> None:
        """One row called off by what it was about (understanding §4.4 `cancel`)."""
        self._move([r], "cancelled")

    def amend(self, r: Request, text: str) -> None:
        """A queued row's correction (§4.4 `steer`): the speaker's words, added to the task it will run."""
        r.task = f"{r.task}. {text}" if r.task else text
        self._bump()

    def cancel(self, agent: str | None = None, mark: int = -1) -> list[Request]:
        """Every open row (of `agent`), and every line said at or before `mark` (the stop's own line)."""
        rows = [r for r in self.open() if agent in (None, r.agent)]
        if agent is None:
            self._stop_all = max(self._stop_all, mark)
        else:
            self._stop_at[agent] = max(self._stop_at.get(agent, -1), mark)
        self._move(rows, "cancelled")
        return rows

    def stopped(self, turn: int, agent: str | None = None) -> bool:
        """A stop said at or after this turn's line: to everyone, or to this agent."""
        return turn <= self._stop_all or (
            agent is not None and turn <= self._stop_at.get(agent, -1)
        )

    def no_post(self, agent: str | None = None) -> Request | None:
        """ "don't post that": the named agent's newest open request, else the room's newest."""
        rows = [r for r in self.open() if r.routed]
        r = ([x for x in rows if x.agent == agent] if agent else rows or [None])[
            -1:
        ] or [None]
        r = r[0]
        if r is not None:
            r.no_post = True
            self._bump()
        return r

    def suppress(self, run_id: str) -> None:
        self._suppressed.add(run_id)

    def no_posted(self, run_id: str) -> bool:
        """ "don't post that" named this run (a request row's, or a plain run's)."""
        return run_id in self._suppressed or any(r.no_post for r in self.by_run(run_id))

    def postable(self, run_id: str) -> bool:
        """False for "don't post that" and a cancelled request. A failed one is postable: huddle.py posts news
        nobody heard to its end (never said, or cut off mid-announcement), a failure as one line (§4.8)."""
        bad = any(r.no_post or r.status == "cancelled" for r in self.by_run(run_id))
        return run_id not in self._suppressed and not bad

    def open(self) -> list[Request]:
        return [r for r in self.items if r.status in OPEN]

    def queued(self) -> list[Request]:
        return [r for r in self.items if r.status == "queued"]

    def working(self, agent: str) -> bool:
        return any(
            r.agent == agent and r.status in ("queued", "working") for r in self.items
        )

    def task(self, agent: str) -> str:
        rows = [
            r
            for r in self.items
            if r.agent == agent and r.status in ("queued", "working") and r.task
        ]
        return rows[-1].task[:TASK_CHARS] if rows else ""

    def lines(
        self, name: Callable[[str], str], skip_agent: str | None = None
    ) -> list[str]:
        out = [
            (
                f"{name(r.agent)[:60]} {_VERB[r.status]}"
                + (f' "{r.task[:LINE_CHARS]}"' if r.task else "")
            )[:200]  # TurnBody.requests item max (backend huddles.py)
            for r in self.open()
            if r.routed and r.agent != skip_agent
        ]
        return out[-MAX_LINES:]


def ack_line(
    rows: list[Request], name: Callable[[str], str], me: str | None = None
) -> str:
    """The one acknowledgement of a turn that made 2+ rows (spec §3.5), said by `me` (default: the first row's
    agent). Its own rows first, then each other agent once; three requests are named, the rest counted. A row
    of `me` still queued is "next in line", not one it is on."""
    me = me or rows[0].agent
    tasks: dict[str, list[Request]] = {me: []}
    for r in rows:
        tasks.setdefault(r.agent, []).append(r)

    def said(rs: list[Request]) -> str:
        return " and on ".join(
            r.task.strip().rstrip(".?!") or "what was asked" for r in rs
        )

    left, parts = 3, []
    for agent, rs in tasks.items():
        if rs and left:
            shown = rs[:left]
            left -= len(shown)
            if agent != me:
                parts.append(f"{name(agent)} is on {said(shown)}.")
                continue
            on = [r for r in shown if r.status != "queued"]
            nxt = [r for r in shown if r.status == "queued"]
            if on:
                parts.append(f"You are on {said(on)}.")
            if nxt:
                line = said(nxt).replace(" and on ", " and then ")
                parts.append(
                    f"{line[:1].upper()}{line[1:]} {'is' if len(nxt) == 1 else 'are'} next in line for you."
                )
    more = len(rows) - (3 - left)
    tail = f" And {more} more, shown in the room." if more else ""
    return " ".join(parts) + f"{tail} Say that in one short sentence, then wait."
