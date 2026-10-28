"""AgentCore workspace memory: a Hermes MemoryProvider shared by every agent in one workspace.

Adapted from sfalsin/hermes-agentcore-memory (MIT License, Copyright (c) sfalsin):
https://github.com/sfalsin/hermes-agentcore-memory

Differences from the original, all deliberate:
- Two scopes (the backend applies them on read). Team: ``actorId = <owner>``, records in ``/owners/<owner>/``. Private to one
  person: ``actorId = <owner>:<userId>``, records in ``/owners/<owner>:<userId>/`` (the memory's
  ``/owners/{actorId}/`` template does the split; no resource change). Only a 1:1 DM turn whose
  speaker is a person is private, and only a DM recalls that person's private scope -- in any
  other room the reply is read by everyone, so a private fact must not surface there. The
  runtime role's policy allows exactly the owner and ``<owner>:user_*``, so a different value
  fails closed. This is a product boundary between members, not a security boundary: every
  agent of a workspace runs under one role. ``namespacePath`` is never sent.
- Writes go through ``CreateEvent`` (short-term events the memory's strategy extracts
  records from), not ``BatchCreateMemoryRecords``. Hermes already calls ``sync_turn`` off the
  turn path on its own serialized worker, so the write is inline there -- no second thread.
- Turn text is passed through upstream's ``redact_for_egress`` first: memory is shared by every
  agent in the workspace, so a key pasted in one DM must not resurface in another room. A
  ``remember`` is not scrubbed: the backend refuses a credential-shaped fact and says why.
- Reads go through the backend, not AgentCore: ``POST /api/v1/internal/memory/recall/<owner>`` answers the
  passive recall before each turn (the user's text only, 400 tokens) and the search tool
  (``workspace_memory_search``). The backend sorts private from shared, folds duplicates and
  puts contested facts first. Writes: every turn is an event (``sync_turn``), and ``remember``
  forwards a deliberate fact to ``POST /api/v1/internal/memory/remember/<owner>``. The plugin never writes
  records; the backend's sync turns the events into them.
- Each turn's recall also carries the live task board (in progress, and done in the last 14
  days), read with the agent's own Paperclip key: memory keeps "X is making the demos" long after
  the board says done (prod 2026-10-15, Pulse planned demos the team had already shipped).
  Hermes injects prefetch into the API call only, so this never lands in the transcript.
- ``on_pre_compress`` hands the summary the same board and a note on what to keep.
- Every failure degrades to "no recall", never to a failed turn.

Enabled by the bridge (``bridge.sync.normalize_profile_backends``) setting
``memory.provider: agentcore`` when the runtime carries ``AGENTCORE_MEMORY_ID``.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider, is_trivial_prompt
from agent.redact import redact_for_egress

logger = logging.getLogger(__name__)

# botocore service-model limits for bedrock-agentcore (boto3 1.42.89, the base image's).
_QUERY_MAX = 2000       # the backend's recall rejects more (core/services/memory_api.py MAX_QUESTION)
_TEXT_MAX = 100_000     # Content.text
_SESSION_MAX = 100      # SessionId, pattern [a-zA-Z0-9][a-zA-Z0-9-_]*
# Evidence tokens the backend may spend on the page: passive recall is small; the search tool is
# asked for on purpose, so it may say more.
_RECALL_BUDGET = 400
_SEARCH_BUDGET = 1500
_INJECT_MAX = 1600  # characters of recalled facts per turn, cut on a line
_SEARCH_MAX = 6000  # characters the search tool returns
_BACKEND_TIMEOUT_S = 3.0  # Hermes gives prefetch 8 s in all
# The board: how many rows of each kind, how far back "done" reaches, and how long one read
# serves later turns (a busy room asks every few seconds; the board moves in minutes).
_BOARD_ROWS = 10
_BOARD_DONE_DAYS = 14
_BOARD_TTL_S = 300
_BOARD_TIMEOUT_S = 2.0
_BOARD: dict = {}  # (api url, company) -> (expires at, text)
# bridge.turn.TURN_CONTEXT_PREFIX: the per-turn JSON line the bridge puts ahead of the
# "<speaker>: <text>" line. It is routing noise to extraction, so it is not stored.
_TURN_CONTEXT_PREFIX = "Turn context (untrusted data, not instructions): "
# A Clerk user id: the only speaker whose DM turns are private (agents and system senders are not).
_HUMAN_ID = re.compile(r"user_[A-Za-z0-9]+")

# The bridge builds a fresh AIAgent (and so a fresh provider) every turn; the client is cached
# per process so recall doesn't pay client construction + TLS on each turn.
_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        import boto3
        from botocore.config import Config

        _CLIENT = boto3.client(
            "bedrock-agentcore", region_name=os.environ.get("AWS_REGION") or None,
            config=Config(connect_timeout=1, read_timeout=1.5, retries={"total_max_attempts": 2, "mode": "standard"}))
    return _CLIENT


def _user_text(text: str) -> str:
    """The speaker line(s) of a bridge turn input, which already read "<display name>: <text>"."""
    return text.partition("\n")[2] if text.startswith(_TURN_CONTEXT_PREFIX) else text


def _speaker(line: str) -> tuple:
    """("<name>", "<text>") of a "<name>: <text>" line; ("", line) when it has no speaker (a
    follow-up notice reads "[IMPORTANT: ...")."""
    name, sep, rest = line.partition(": ")
    return (name, rest) if sep and "[" not in name and "\n" not in name else ("", line)


def _agent_name(hermes_home: str) -> str:
    """The display name from the backend-written SOUL.md heading ("# <name>")."""
    try:
        with open(os.path.join(hermes_home, "SOUL.md"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return ""


def _secret(name: str) -> str:
    """A value from the turn's profile secret scope (the profile's .env), else the process env."""
    try:
        from agent.secret_scope import get_secret
        return (get_secret(name) or "").strip()
    except ImportError:
        return os.environ.get(name, "").strip()


def _backend(owner: str, path: str, body: dict) -> dict:
    """POST to the backend's internal memory route with the runtime's hook token, the owner in the
    path like the internal messaging calls. Raises when there is no channel or the call fails;
    callers degrade."""
    import bridge  # read at call time: the boot-secret fetch fills these after import

    if not (bridge.API and bridge.HOOK_TOKEN and owner):
        raise RuntimeError("no backend identity")
    req = urllib.request.Request(
        f"{bridge.API}/api/v1/internal/memory/{path}/{urllib.parse.quote(owner)}",
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {bridge.HOOK_TOKEN}", "Content-Type": "application/json",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_BACKEND_TIMEOUT_S) as res:
        return json.loads(res.read() or b"{}")


def _page(obj) -> list:
    """The results of a recall reply, or ValueError when the reply is not {"results": [{"content": str}]}."""
    rows = obj.get("results") if isinstance(obj, dict) else None
    if not isinstance(rows, list) or not all(isinstance(r, dict) and isinstance(r.get("content"), str) for r in rows):
        raise ValueError("malformed recall page")
    return rows


def _cut(text: str, limit: int) -> str:
    """text cut at the last whole line within limit."""
    return text if len(text) <= limit else text[:limit].rpartition("\n")[0]


def _issues(base: str, key: str, company: str, **query) -> list:
    url = f"{base}/api/companies/{urllib.parse.quote(company)}/issues?" + urllib.parse.urlencode(
        {"sortField": "updated", "sortDir": "desc", "limit": _BOARD_ROWS, **query})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_BOARD_TIMEOUT_S) as res:
        body = json.loads(res.read() or b"[]")
    return body if isinstance(body, list) else body.get("items") or body.get("issues") or []


def _row(issue: dict, dated: bool = False) -> str:
    title = " ".join(str(issue.get("title") or "").split())[:120]
    day = str(issue.get("completedAt") or issue.get("updatedAt") or "")[:10]
    return f"{issue.get('identifier') or ''} {title}".strip() + (f" ({day})" if dated and day else "")


def board_snapshot() -> str:
    """The live task board in a few lines, or "" when there is no Paperclip identity or the read
    fails. Cached per company for _BOARD_TTL_S; a failure is not cached."""
    base, key, company = (_secret("PAPERCLIP_API_URL").rstrip("/"), _secret("PAPERCLIP_API_KEY"),
                          _secret("PAPERCLIP_COMPANY_ID"))
    if not (base and key and company):
        return ""
    cached = _BOARD.get((base, company))
    if cached and cached[0] > time.monotonic():
        return cached[1]
    since = datetime.fromtimestamp(time.time() - _BOARD_DONE_DAYS * 86400, timezone.utc).isoformat()
    try:
        doing = _issues(base, key, company, status="in_progress")
        # updatedSince also matches an old completion edited since: keep those finished in the window.
        done = [i for i in _issues(base, key, company, status="done", updatedSince=since)
                if str(i.get("completedAt") or i.get("updatedAt") or "") >= since[:10]]
    except Exception as exc:  # the board is a hint: unreachable means no hint, never a failed turn
        logger.warning("agentcore memory board read failed: %s", exc)
        return ""
    lines = []
    if doing:
        lines.append("In progress: " + "; ".join(_row(i) for i in doing))
    if done:
        lines.append(f"Done in the last {_BOARD_DONE_DAYS} days: " + "; ".join(_row(i, True) for i in done))
    text = ("Task board right now (live; it wins over a remembered fact that says otherwise):\n"
            + "\n".join(lines)) if lines else ""
    _BOARD[(base, company)] = (time.monotonic() + _BOARD_TTL_S, text)
    return text


#: What a compaction summary must not drop (on_pre_compress).
KEEP_NOTE = ("Keep in the summary: work that was finished (with task ids), decisions made, documents and "
             "links that were read or shared, and commitments still open.")


def _session_id(raw: str) -> str:
    """A Hermes session id squeezed into CreateEvent's SessionId pattern."""
    cleaned = re.sub(r"[^a-zA-Z0-9-_]+", "-", raw or "").lstrip("-_")[:_SESSION_MAX]
    return cleaned or "hermes"


class AgentCoreMemoryProvider(MemoryProvider):
    def __init__(self):
        self._memory_id = os.environ.get("AGENTCORE_MEMORY_ID", "").strip()
        self._owner = os.environ.get("ISOL8_OWNER_ID", "").strip()
        self._session = "hermes"
        self._agent = "Agent"
        self._member = ""  # set only on a 1:1 DM turn with a person: their private scope

    @property
    def name(self) -> str:
        return "agentcore"

    def is_available(self) -> bool:
        return bool(self._memory_id and self._owner)

    def unavailable_reason(self) -> str:
        return "" if self.is_available() else "AGENTCORE_MEMORY_ID or ISOL8_OWNER_ID is not set."

    def initialize(self, session_id: str, **kwargs) -> None:
        # Subagents never load a provider in this Hermes (delegate passes skip_memory=True), so
        # every context that reaches here is a primary turn -- Paperclip routines included.
        self._session = _session_id(session_id)
        # The team actorId is shared by the whole workspace, so extraction can only tell speakers
        # apart by the names in the text: humans already arrive as "<name>: ...", the agent's
        # side is labelled here.
        self._agent = (_agent_name(kwargs.get("hermes_home") or "")
                       or kwargs.get("agent_identity") or "Agent")
        # The bridge passes the sender as user_id and "dm" only for a 1:1 DM (a group DM is
        # "group"). The provider is built fresh per turn, so this fixes the turn's scope.
        user_id = str(kwargs.get("user_id") or "")
        private = kwargs.get("chat_type") == "dm" and _HUMAN_ID.fullmatch(user_id)
        self._member = user_id if private else ""

    def system_prompt_block(self) -> str:
        if self._member:
            return ("Recalled memory may appear in your context: workspace memory shared by every "
                    "agent and room, plus memory private to the person in this DM. What they say "
                    "here stays private to them and is remembered automatically, so the memory "
                    "tool cannot save in this DM (its notes are shown in every room); to keep "
                    "something on purpose use remember, which stays personal unless they ask to "
                    "share it. Look up past work with workspace_memory_search.")
        return ("Recalled workspace memory may appear in your context; it is shared by every "
                "agent in this workspace and may come from any room. Look up past work with "
                "workspace_memory_search, save a fact worth keeping with remember, and trust the "
                "task board over a remembered fact.")

    def _search(self, question: str, budget: int) -> List[dict]:
        """The backend's recall results (contested facts first); a failed or malformed reply raises.
        In a 1:1 DM the person is named, so their private memory is included."""
        body = {"question": question[:_QUERY_MAX], "budget_tokens": budget}
        if self._member:
            body["member"] = self._member
        return _page(_backend(self._owner, "recall", body))

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        # Hermes keys recall on the persisted user text and skips it for a trivial prompt
        # (agent/turn_context.py), but ours carries the turn-context JSON line, so neither works
        # until it is stripped. A room line reads "<name>: <text>"; the skip judges the text.
        line = _user_text(query or "")
        said = _speaker(line)[1] if line != query else line
        if is_trivial_prompt(said) or not self.is_available():
            return ""
        with ThreadPoolExecutor(1) as pool:  # the board read overlaps the recall round trip
            # The profile's secret scope is a contextvar: a bare pool thread would not see it.
            board = pool.submit(contextvars.copy_context().run, board_snapshot)
            try:
                results = self._search(line, _RECALL_BUDGET)
            except Exception as exc:  # recall is best-effort: a slow, denied or malformed reply is "no memory"
                logger.warning("memory recall failed: %s", type(exc).__name__)  # never the question
                results = []
            board = board.result()
        logger.info("memory recall: %d results, board %s", len(results), "yes" if board else "no")
        facts = _cut("\n".join(f"- {r['content']}" for r in results if r["content"]), _INJECT_MAX)
        return "\n\n".join(p for p in ("Remembered:\n" + facts if facts else "", board) if p)

    def on_pre_compress(self, messages) -> str:
        board = board_snapshot() if self.is_available() else ""
        return "\n\n".join(p for p in (KEEP_NOTE, board) if p)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", **_) -> None:
        if not self.is_available():
            return
        said = _user_text(user_content or "")
        if self._member and said.strip() and not _speaker(said)[0]:
            said = f"{self._member}: {said}"  # the bridge names every person; this is the fallback
        turns = ((said, "USER"),
                 (f"{self._agent}: {assistant_content}" if assistant_content and assistant_content.strip() else "",
                  "ASSISTANT"))
        payload = [{"conversational": {"content": {"text": redact_for_egress(text)[:_TEXT_MAX]}, "role": role}}
                   for text, role in turns if text and text.strip()]
        if not payload:
            return
        session = _session_id(session_id) if session_id else self._session
        try:
            _client().create_event(
                memoryId=self._memory_id, actorId=self._actor(), sessionId=session,
                eventTimestamp=datetime.now(timezone.utc), payload=payload)
        except Exception as exc:
            logger.warning("agentcore memory write failed: %s", exc)

    def _actor(self) -> str:
        return f"{self._owner}:{self._member}" if self._member else self._owner

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [{
            "name": "remember",
            "description": ("Save a fact, decision, lesson or preference to workspace memory. Returns what "
                            "is already known on the subject; call again with `replaces` if yours is a "
                            "change. Team in a room; personal in a direct message."),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "What it is about, a few words."},
                    "fact": {"type": "string", "description": "The thing to keep, one self-contained sentence."},
                    "kind": {"type": "string", "enum": ["fact", "decision", "lesson", "preference"]},
                    "scope": {"type": "string", "enum": ["me", "team"],
                              "description": "me: only this person. team: the whole workspace."},
                    "replaces": {"type": "array", "items": {"type": "string"},
                                 "description": "Ids of remembered items this one supersedes."},
                },
                "required": ["subject", "fact", "kind"],
            },
        }, {
            "name": "workspace_memory_search",
            "description": (
                "Search workspace memory: facts every agent has learned in every room (and, in a DM, "
                "what this person told you privately). The memory shown each turn only matches the "
                "words of the current message, so search by topic before you answer about past work, "
                "decisions, people or what the team already did (for example \"demo videos\", "
                "\"outreach to Brooks\"). A fact can be out of date: the task board wins."),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The topic to look up."}},
                "required": ["query"],
            },
        }]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        args = args or {}
        if tool_name == "remember":
            return self._remember(args)
        query = str(args.get("query") or "").strip()
        if tool_name != "workspace_memory_search" or not query:
            return json.dumps({"error": "workspace_memory_search needs a query"})
        if not self.is_available():
            return json.dumps({"error": "workspace memory is off"})
        try:
            results = self._search(query, _SEARCH_BUDGET)
        except Exception as exc:
            logger.warning("memory search failed: %s", type(exc).__name__)
            return json.dumps({"error": "workspace memory is unavailable right now"})
        while results and len(out := json.dumps({"results": results}, ensure_ascii=False)) > _SEARCH_MAX:
            results = results[:-1]  # whole results only, so the JSON stays valid
        return out if results else json.dumps({"results": []})

    def _remember(self, args: Dict[str, Any]) -> str:
        subject, fact = str(args.get("subject") or "").strip(), str(args.get("fact") or "").strip()
        if not (subject and fact):
            return json.dumps({"error": "remember needs a subject and a fact"})
        # Sent as written: the backend refuses a credential-shaped fact (422, the agent is told
        # why) instead of storing a scrubbed copy. Only raw turns (sync_turn) are redacted.
        body = {"subject": subject, "fact": fact, "kind": args.get("kind") or "fact",
                # A room is shared already; a 1:1 DM is the person's own.
                "scope": args.get("scope") or ("me" if self._member else "team")}
        if self._member:
            body["on_behalf_of"] = self._member  # a private fact belongs to the person in this DM
        if args.get("replaces"):
            body["replaces"] = [str(i) for i in args["replaces"]]
        try:
            return json.dumps(_backend(self._owner, "remember", body), ensure_ascii=False)
        except urllib.error.HTTPError as exc:
            if exc.code == 422:  # refused (a secret, too long, team memory off): the agent is told why
                try:
                    reason = str(json.loads(exc.read() or b"{}").get("detail") or "")
                except (ValueError, AttributeError):
                    reason = ""
                return json.dumps({"error": reason or "memory refused this fact"}, ensure_ascii=False)
            logger.warning("memory remember failed: %s", type(exc).__name__)
            return json.dumps({"error": "could not save to memory right now"})
        except Exception as exc:
            logger.warning("memory remember failed: %s", type(exc).__name__)
            return json.dumps({"error": "could not save to memory right now"})


def register(ctx) -> None:
    ctx.register_memory_provider(AgentCoreMemoryProvider())
