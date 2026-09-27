"""OS-level controls for the shared AgentCore browser.

Unlike Hermes's DOM/CDP browser tools, InvokeBrowser can see and operate Chrome
toolbar extension popups and native dialogs. The normal browser lease hook still
serializes these actions with people and other agent runs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.request

from bridge import browser_control
from . import turn_context

UNAVAILABLE = "Real-screen browser control is unavailable for this turn."
MAX_SCREENSHOT_BYTES = 12 * 1024 * 1024

SCHEMA = {
    "name": "browser_screen",
    "description": (
        "See and operate the shared browser's real screen, including Chrome toolbar extension "
        "popups and native dialogs that DOM/CDP tools cannot reach. Take a screenshot first, "
        "then use its pixel coordinates for click or scroll."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["screenshot", "click", "type", "key", "scroll"]},
            "x": {"type": "integer", "minimum": 0, "maximum": 10000},
            "y": {"type": "integer", "minimum": 0, "maximum": 10000},
            "button": {"type": "string", "enum": ["left", "right", "middle"]},
            "clickCount": {"type": "integer", "minimum": 1, "maximum": 10},
            "text": {"type": "string", "maxLength": 10000},
            "key": {"type": "string", "minLength": 1, "maxLength": 32, "pattern": "^[a-z0-9]+$"},
            "modifiers": {
                "type": "array",
                "items": {"type": "string", "enum": ["ctrl", "alt", "shift"]},
                "maxItems": 4,
            },
            "presses": {"type": "integer", "minimum": 1, "maximum": 100},
            "deltaX": {"type": "integer", "minimum": -1000, "maximum": 1000},
            "deltaY": {"type": "integer", "minimum": -1000, "maximum": 1000},
        },
        "required": ["action"],
    },
}


def _post(url: str, token: str, body: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(MAX_SCREENSHOT_BYTES * 2)
    return json.loads(raw or b"{}")


def _materialize_screenshot(encoded: str, session_id: str) -> dict:
    data = base64.b64decode(encoded, validate=True)
    if not data or len(data) > MAX_SCREENSHOT_BYTES:
        raise ValueError("invalid screenshot size")
    root = turn_context._home() / turn_context.DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    name = "browser-screen-" + hashlib.sha256(session_id.encode()).hexdigest()[:16] + ".png"
    target = root / name
    target.write_bytes(data)
    return {
        "name": name,
        "mimeType": "image/png",
        "bytes": len(data),
        "path": str(target),
        "media": f"MEDIA:{target}",
    }


def handle(args: dict, *, session_id: str = "", **_kwargs) -> str:
    ctx = turn_context.read(session_id)
    lease = browser_control.current_lease()
    if not ctx or not lease or lease.get("runId") != ctx.get("runId"):
        return UNAVAILABLE
    action = str(args.get("action") or "")
    allowed = {
        "screenshot": {"action"},
        "click": {"action", "x", "y", "button", "clickCount"},
        "type": {"action", "text"},
        "key": {"action", "key", "modifiers", "presses"},
        "scroll": {"action", "x", "y", "deltaX", "deltaY"},
    }.get(action)
    if allowed is None or set(args) - allowed:
        return "Invalid real-screen browser action parameters."
    body = {"runId": lease["runId"], "generation": lease["generation"], **args}
    url = f"{ctx['apiUrl'].rstrip('/')}/api/v1/internal/browser/{ctx['ownerId']}/screen"
    try:
        result = _post(url, ctx["hookToken"], body)
        if result.get("status") != "SUCCESS":
            return "The real-screen browser action failed. Take a fresh screenshot and try again."
        if action == "screenshot":
            artifact = _materialize_screenshot(result.pop("data"), session_id)
            result["artifacts"] = [artifact]
        return json.dumps(result)
    except Exception:
        return "The real-screen browser action failed. Take a fresh screenshot and try again."
