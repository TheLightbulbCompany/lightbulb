"""A shared Claude Code or Codex session's browser (spec docs/superpowers/specs/2026-10-11-session-browser-design.md).

The session is a run, `session-<sessionId>`, so it uses the Mac browser lease and the workspace
sign-in sync exactly as an agent run does. Its person is the run's member. Never log a ticket, a
URL or a frame: owner ids and exception class names only.
"""

from __future__ import annotations

import re

RUN_PREFIX = "session-"
AGENT_ID = "session"
_SID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def run_id(session_id: str) -> str:
    return RUN_PREFIX + session_id


def session_of(run: object) -> str | None:
    """The session id of a `session-<id>` run, else None."""
    sid = run[len(RUN_PREFIX) :] if isinstance(run, str) and run.startswith(RUN_PREFIX) else ""
    return sid if _SID.fullmatch(sid) else None


def auth_for(owner: str, member: str, session_id: str) -> dict:
    """What `desktop_execution.authorize_run` answers for this session's run."""
    return {
        "ownerId": owner,
        "memberId": member,
        "runId": run_id(session_id),
        "agentId": AGENT_ID,
        "family": "session",
        "roomId": "",
    }
