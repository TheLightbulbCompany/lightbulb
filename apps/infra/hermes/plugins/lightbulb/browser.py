"""The shared AgentCore browser, beyond Hermes's own page tools.

`browser_screen` is real mouse/keyboard/screenshots on the session's screen through AWS
`InvokeBrowser`, so it reaches what CDP cannot: Chrome's toolbar and extension icons, the
"Add extension" dialog, permission prompts, JavaScript alerts. Hermes's `computer_use` cannot
be pointed here: its backend is fixed to cua-driver (this machine's own desktop).

`browser_cdp` is Hermes's own tool, re-registered because Hermes offers it only with a
`browser.cdp_url`, and that setting also moves agent-browser off our loopback and starts a
supervisor socket -- a second automation connection, which AWS refuses (429). Here the bridge
hands the one connection over for the call (browser_control.pre_tool_call).
"""
from __future__ import annotations

import base64
import json
import os
import tempfile

from bridge import browser_control

_ACTIONS = ("screenshot", "click", "type", "key", "scroll")

SCREEN_SCHEMA = {
    "name": "browser_screen",
    "description": (
        "The shared browser's real screen: screenshot it, or click, type, press keys and scroll "
        "with the real mouse and keyboard. Reaches Chrome's own UI that page tools cannot: the "
        "toolbar, the Extensions (puzzle) menu and extension buttons, 'Add extension' and "
        "permission dialogs, alerts. Coordinates are screenshot pixels; take a fresh screenshot "
        "before every click, never reuse old coordinates."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(_ACTIONS)},
            "x": {"type": "integer"}, "y": {"type": "integer"},
            "button": {"type": "string", "enum": ["LEFT", "RIGHT", "MIDDLE"]},
            "clickCount": {"type": "integer", "minimum": 1, "maximum": 3},
            "text": {"type": "string", "description": "type: the text to type."},
            "key": {"type": "string", "description": "key: one key (Enter, Tab, Escape) or a shortcut "
                                                      "joined with + (ctrl+shift+i)."},
            "deltaY": {"type": "integer", "description": "scroll: pixels, positive = down."},
            "deltaX": {"type": "integer"},
        },
        "required": ["action"],
    },
}


def _client():
    import boto3

    return boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION") or "us-east-1")


def _action(args: dict) -> dict:
    kind = args.get("action")
    if kind == "screenshot":
        return {"screenshot": {"format": "PNG"}}
    if kind == "click":
        return {"mouseClick": {"x": int(args["x"]), "y": int(args["y"]), "button": args.get("button") or "LEFT",
                               "clickCount": int(args.get("clickCount") or 1)}}
    if kind == "type":
        return {"keyType": {"text": str(args["text"])}}
    if kind == "key":
        keys = [k.strip() for k in str(args["key"]).split("+") if k.strip()]
        return {"keyShortcut": {"keys": keys}} if len(keys) > 1 else {"keyPress": {"key": keys[0]}}
    if kind == "scroll":
        return {"mouseScroll": {"x": int(args.get("x") or 0), "y": int(args.get("y") or 0),
                                "deltaX": int(args.get("deltaX") or 0), "deltaY": int(args.get("deltaY") or 0)}}
    raise ValueError(f"action must be one of {', '.join(_ACTIONS)}")


def screen(args: dict, **_kw):
    ref = browser_control.session_ref()  # pre_tool_call acquired the session before this runs
    if not ref:
        return json.dumps({"success": False, "error": "Browser control is unavailable for this turn."})
    try:
        action = _action(args)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return json.dumps({"success": False, "error": f"Bad arguments: {exc}"})
    try:
        result = _client().invoke_browser(browserIdentifier=ref[0], sessionId=ref[1], action=action)["result"]
    except Exception as exc:  # noqa: BLE001 — the model gets the AWS error, not a crashed turn
        return json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"})
    (name, outcome), = result.items()
    if outcome.get("status") != "SUCCESS":
        return json.dumps({"success": False, "error": outcome.get("error") or "failed"})
    if name != "screenshot":
        return json.dumps({"success": True, "action": args["action"]})
    png = outcome["data"]
    with tempfile.NamedTemporaryFile(prefix="screen-", suffix=".png", delete=False) as stream:
        stream.write(png)
    path = stream.name
    summary = f"Screenshot of the shared browser's screen, saved to {path}."
    from tools.computer_use.tool import _should_route_through_aux_vision

    if _should_route_through_aux_vision():  # the main model cannot take an image result
        return json.dumps({"success": True, "path": path,
                           "note": summary + " Read it with vision_analyze(image_url=path, question=...)."})
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    return {"_multimodal": True, "text_summary": summary,
            "content": [{"type": "text", "text": summary}, {"type": "image_url", "image_url": {"url": data_url}}]}


def _available() -> bool:
    # The same gate as Hermes's own browser_* tools: on only when the workspace has a browser.
    from tools.browser_tool_install import check_browser_requirements

    return bool(os.environ.get("AGENT_BROWSER_CDP") and check_browser_requirements())


def register(ctx) -> None:
    ctx.register_tool("browser_screen", "lightbulb", SCREEN_SCHEMA, screen, check_fn=_available,
                      description=SCREEN_SCHEMA["description"], emoji="🖱️")
    from tools import browser_cdp_tool as cdp

    # Hermes's browser_cdp resolves its endpoint through this one function; point it at the
    # connection the bridge holds for this turn (never a second one: see the module docstring).
    cdp._resolve_cdp_endpoint = lambda: (browser_control.discovery_payload() or {}).get("webSocketDebuggerUrl", "")
    ctx.register_tool(
        "browser_cdp", "browser-cdp", cdp.BROWSER_CDP_SCHEMA,
        lambda args, **kw: cdp.browser_cdp(
            method=args.get("method", ""), params=args.get("params"), target_id=args.get("target_id"),
            frame_id=args.get("frame_id"), timeout=args.get("timeout", 30.0), task_id=kw.get("task_id")),
        check_fn=_available, description=cdp.BROWSER_CDP_SCHEMA.get("description", ""), emoji="🧪", override=True,
    )
