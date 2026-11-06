"""The one notification shape for every push, Slack/Linear-style:

    Pulse · #GTM Strategy          <- title: who · where
    Couldn't finish: it ran out…   <- body: what, one short plain line

Mirrors ``packages/shared/src/messaging/notificationCopy.ts`` (web + mobile);
the messaging-projector Lambda inlines the same rules. Keep them in step.
"""

import re

LINE_CHARS = 80

# Paperclip's run errorCodes in plain words (same map as the TS `runFailureLine`).
RUN_FAILURE_REASONS = {
    "setup_failed": "the workspace couldn't start",
    "process_lost": "the workspace restarted mid-run",
    "timeout": "it ran out of time",
    "hermes_gateway_auth_failed": "it couldn't reach the workspace",
    "hermes_gateway_upstream_error": "the AI model didn't answer",
    "hermes_gateway_run_failed": "the agent hit an error",
}

_MARKER = re.compile(r"^\s*(?:[#>]+|[-*+]|\d+\.)\s+")


def title(who: str, where: str | None = None) -> str:
    return f"{who} · {where}" if where else who


def approval_title(who: str) -> str:
    """A direct, named, one-action approval request."""
    return title(who, "needs your OK")


def room_where(kind: str | None, name: str | None) -> str | None:
    if kind == "dm":
        return "DM"
    if kind == "channel":
        return f"#{name}" if name else "Channel"
    if kind == "group-dm":
        return name or "Group chat"
    return None


def line(text: str | None, limit: int = LINE_CHARS) -> str:
    """The first non-empty line of ``text``, list/heading marks dropped, clipped."""
    for part in (text or "").split("\n"):
        cleaned = " ".join(_MARKER.sub("", part).replace("**", "").replace("`", "").split())
        if cleaned:
            return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"
    return ""


def run_failure_line(error_code: str | None = None, error: str | None = None) -> str:
    reason = RUN_FAILURE_REASONS.get(error_code or "") or line(error) or "the run hit an error"
    return line(f"Couldn't finish: {reason}")
