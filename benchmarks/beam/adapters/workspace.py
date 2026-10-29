"""The workspace engine: AgentCore Memory behind the backend's dev-only bench routes
(apps/backend/routers/internal_bench.py, /api/v1/internal/bench/<owner>/...).

Each conversation is a synthetic owner ``bench-<ulid>``. Turns go in 100 at a time with the
dataset's times; the backend writes each exchange as a CreateEvent and the ``workspace_claims``
strategy extracts facts on its own (no note-taker here). Extraction is asynchronous, so a
project is done when its fact count has not moved for SETTLE_S, polled every POLL_S. Recall is
the backend's own pipeline as of the question's moment; the runner then trims it to past.dev's
100 results / 8,000 tokens.

Environment (never printed): BEAM_WORKSPACE_API, BEAM_WORKSPACE_OWNER (a real dev owner whose
hook token gates the routes), BEAM_WORKSPACE_HOOK_TOKEN; or BEAM_WORKSPACE_ENV naming a file
holding those three lines.
"""

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path

import httpx

from adapters.base import BackendError, RecallPage, Turn
from dataset import utc_iso
from log import log

POLL_S = 30
SETTLE_S = 120
RETRY_DELAYS_S = (2, 4, 8, 16, 32)
TIMEOUT_S = 120


def _settings() -> dict[str, str]:
    values = {k: os.environ.get(k, "") for k in ("BEAM_WORKSPACE_API", "BEAM_WORKSPACE_OWNER",
                                                 "BEAM_WORKSPACE_HOOK_TOKEN")}
    path = os.environ.get("BEAM_WORKSPACE_ENV")
    if path:
        for line in Path(path).read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key in values and not values[key]:
                values[key] = value.strip().strip('"')
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise SystemExit(f"Set {', '.join(missing)} (or BEAM_WORKSPACE_ENV) for the workspace backend.")
    return values


class WorkspaceBackend:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, clock=time.monotonic) -> None:
        s = _settings()
        self.owner = s["BEAM_WORKSPACE_OWNER"]
        self.http = httpx.AsyncClient(base_url=s["BEAM_WORKSPACE_API"].rstrip("/") + f"/api/v1/internal/bench/{self.owner}",
                                      headers={"Authorization": f"Bearer {s['BEAM_WORKSPACE_HOOK_TOKEN']}"},
                                      timeout=TIMEOUT_S, transport=transport)
        self.clock = clock
        self.projects: list[str] = []
        self.watch: dict[str, dict] = {}  # project -> last poll time, last fact count, when it last moved
        self.stats = {"turns": 0, "events": 0, "ingestSeconds": 0.0, "settleSeconds": {}, "facts": {},
                      "recalls": 0, "recallSeconds": 0.0, "retries": 0, "throttled": 0}

    async def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            try:
                response = await self.http.request(method, path, json=body)
            except httpx.HTTPError as error:
                problem, retry = f"{type(error).__name__}", True
            else:
                if response.status_code < 400:
                    return response.json() if response.content else {}
                problem = f"{method} {path.split('/')[1]} returned {response.status_code}: {response.text[:300]}"
                retry = response.status_code in (429, 502, 503, 504)
                self.stats["throttled"] += response.status_code == 429
            if not retry or attempt == len(RETRY_DELAYS_S):
                raise BackendError(problem)
            self.stats["retries"] += 1
            await asyncio.sleep(RETRY_DELAYS_S[attempt])
        raise AssertionError("unreachable")

    async def create_project(self, name: str) -> str:
        project = (await self._call("POST", "/projects", {"name": name}))["owner_id"]
        self.projects.append(project)
        return project

    async def register_identities(self, project: str, speakers: list[dict]) -> None:
        pass  # speakers travel in each turn's text, as the runtime plugin names them

    async def ingest_batch(self, project: str, turns: list[Turn]) -> None:
        started = self.clock()
        out = await self._call("POST", "/ingest", {"bench_owner": project, "turns": [
            {"session_id": t.session_id, "ordinal": t.ordinal, "speaker": t.speaker, "role": t.role,
             "text": t.text, "at": utc_iso(t.at)} for t in turns]})
        self.stats["turns"] += out.get("turns", 0)
        self.stats["events"] += out.get("events", 0)
        self.stats["ingestSeconds"] += self.clock() - started
        self.watch[project] = {"ingested": self.clock(), "polled": None, "facts": -1, "moved": self.clock()}

    async def ingestion_status(self, project: str) -> str:
        w = self.watch[project]
        now = self.clock()
        if w["polled"] is not None and now - w["polled"] < POLL_S:
            return "pending"
        facts = (await self._call("GET", f"/status/{project}"))["facts"]
        w["polled"] = now
        if facts != w["facts"]:
            w["facts"], w["moved"] = facts, now
            return "pending"
        if facts > 0 and now - w["moved"] >= SETTLE_S:
            self.stats["settleSeconds"][project] = round(now - w["ingested"], 1)
            self.stats["facts"][project] = facts
            return "done"
        return "pending"

    async def recall(self, project: str, query: str, as_of: datetime | None, identity: str) -> RecallPage:
        started = self.clock()
        page = await self._call("POST", "/recall", {"bench_owner": project, "question": query,
                                                    "as_of": utc_iso(as_of) if as_of else None,
                                                    "budget_tokens": 8000, "limit": 100})
        self.stats["recalls"] += 1
        self.stats["recallSeconds"] += self.clock() - started
        echoed = page.get("asOf")
        return RecallPage(page.get("results") or [],
                          datetime.fromisoformat(echoed.replace("Z", "+00:00")) if echoed else None)

    async def close(self) -> None:
        if os.environ.get("BEAM_WORKSPACE_KEEP") != "1":
            for project in self.projects:
                try:
                    await self._call("DELETE", f"/projects/{project}")
                except BackendError as error:
                    log(f"could not delete {project}: {error}")
        log(f"workspace engine: {self.stats['turns']} turns, {self.stats['events']} events, "
            f"{self.stats['recalls']} recalls, {self.stats['retries']} retries, {self.stats['throttled']} throttled")
        await self.http.aclose()
