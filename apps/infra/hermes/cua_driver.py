#!/opt/hermes/.venv/bin/python
"""Hermes's computer_use, on the shared AgentCore browser's real screen.

Hermes drives a "cua-driver" binary (``HERMES_CUA_DRIVER_CMD``, set by the bridge when the workspace
has a browser). This is one that implements the cua tool set over AWS ``InvokeBrowser`` -- real
mouse, keyboard and screenshots of the browser's screen, so an agent reaches what CDP cannot:
Chrome's toolbar and extension icons and popups, "Add extension", permission prompts, recording.
Each action goes to the bridge (``POST /browser/invoke`` on loopback), which holds the session and
the runtime's own AWS role -- this process has the agent profile's env; the bridge's pre_tool_call
hook acquires the session before every computer_use call.

Verbs Hermes uses: manifest | mcp (stdio JSON-RPC MCP) | serve | status | stop | call <tool> <json>.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

INVOKE_URL = f"http://127.0.0.1:{os.environ.get('AGENTCORE_PORT', '8080')}/browser/invoke"
PROPS = lambda *names: {"type": "object", "properties": {n: {} for n in (*names, "session", "pid", "window_id")}}
TOOLS = {  # name -> input properties Hermes may send
    "start_session": PROPS(), "end_session": PROPS(), "set_config": PROPS("key", "value", "max_image_dimension"),
    "get_config": PROPS(), "set_agent_cursor_enabled": PROPS("enabled", "cursor_id"),
    "list_apps": PROPS(), "list_windows": PROPS("on_screen_only"), "get_window_state": PROPS(),
    "screenshot": PROPS("format", "quality"), "get_desktop_state": PROPS(), "bring_to_front": PROPS(),
    "click": PROPS("x", "y", "button", "modifier", "element_index"), "double_click": PROPS("x", "y", "button", "modifier"),
    "drag": PROPS("from_x", "from_y", "to_x", "to_y"), "scroll": PROPS("direction", "amount", "x", "y"),
    "type_text": PROPS("text"), "press_key": PROPS("key"), "hotkey": PROPS("keys"), "set_value": PROPS("value"),
}
WINDOW = {"pid": 1, "window_id": 1, "app_name": "Chrome", "title": "Shared browser", "z_index": 1, "is_on_screen": True}
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback, never a proxy


def invoke(action: dict) -> dict:
    """One InvokeBrowser action, made by the bridge (it holds the session and the runtime's role)."""
    request = urllib.request.Request(INVOKE_URL, data=json.dumps(action).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _NO_PROXY.open(request, timeout=60) as response:
            out = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(json.loads(exc.read() or b"{}").get("error") or f"HTTP {exc.code}") from None
    except OSError:
        raise RuntimeError("The shared browser is not reachable; use a browser tool first.") from None
    if out.get("status") != "SUCCESS":
        raise RuntimeError(out.get("error") or "InvokeBrowser failed")
    return out


def shot() -> dict:
    png = invoke({"screenshot": {"format": "PNG"}})["data"]  # base64 already
    return {"content": [{"type": "image", "data": png, "mimeType": "image/png"}],
            "structuredContent": {"elements": [], "window_id": 1}}


def ok(msg: str, **extra) -> dict:
    return {"content": [{"type": "text", "text": msg}], "structuredContent": {"message": msg, "verdict": "delivered", **extra}}


def call(name: str, a: dict) -> dict:
    if name == "list_windows":
        return {"content": [{"type": "text", "text": "1 window"}], "structuredContent": {"windows": [WINDOW]}}
    if name == "list_apps":
        return {"content": [{"type": "text", "text": "Chrome (pid 1)"}],
                "structuredContent": {"apps": [{"name": "Chrome", "pid": 1, "running": True}]}}
    if name in ("get_window_state", "screenshot", "get_desktop_state"):
        return shot()
    if name == "get_config":
        return ok("config", capture_scope="window")
    if name in ("click", "double_click"):
        invoke({"mouseClick": {"x": int(a["x"]), "y": int(a["y"]), "button": str(a.get("button", "left")).upper(),
                               "clickCount": 2 if name == "double_click" else 1}})
        return ok(f"{name} at ({a['x']},{a['y']})")
    if name == "drag":
        invoke({"mouseDrag": {"startX": int(a["from_x"]), "startY": int(a["from_y"]), "endX": int(a["to_x"]),
                              "endY": int(a["to_y"]), "button": "LEFT"}})
        return ok("dragged")
    if name == "scroll":
        step = 100 * int(a.get("amount", 3))
        dx, dy = {"up": (0, -step), "down": (0, step), "left": (-step, 0), "right": (step, 0)}[a["direction"]]
        invoke({"mouseScroll": {"x": int(a.get("x") or 640), "y": int(a.get("y") or 450), "deltaX": dx, "deltaY": dy}})
        return ok("scrolled")
    if name == "type_text":
        invoke({"keyType": {"text": a["text"]}})
        return ok("typed")
    if name == "press_key":
        invoke({"keyPress": {"key": a["key"]}})
        return ok("pressed")
    if name == "hotkey":
        invoke({"keyShortcut": {"keys": list(a["keys"])}})
        return ok("hotkey")
    if name in TOOLS:  # session/config/cursor/focus bookkeeping: nothing to do on a remote screen
        return ok("ok")
    raise KeyError(name)


def mcp(stdin=sys.stdin, stdout=sys.stdout) -> None:
    for line in stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue  # notifications
        method, p = msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            res = {"protocolVersion": p.get("protocolVersion"), "capabilities": {"tools": {}},
                   "serverInfo": {"name": "lightbulb-cua", "version": "0.20.0"}}
        elif method == "tools/list":
            res = {"tools": [{"name": n, "inputSchema": s} for n, s in TOOLS.items()]}
        elif method == "tools/call":
            try:
                res = {**call(p["name"], p.get("arguments") or {}), "isError": False}
            except Exception as e:  # noqa: BLE001 -- a tool-level failure, not a protocol error
                res = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
        else:
            res = {}
        stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}) + "\n")
        stdout.flush()


def manifest() -> dict:
    arg = lambda *n: [{"name": x} for x in n]
    return {"binary_version": "0.20.0", "mcp_invocation": {"command": os.path.abspath(sys.argv[0]), "args": ["mcp"]},
            "subcommands": [{"name": "mcp", "args": arg("--socket", "--grant")},
                            {"name": "serve", "args": arg("--socket", "--permission-mode", "--capability-manifest",
                                                          "--approve-capability-manifest", "--embedded")},
                            {"name": "stop", "args": arg("--socket")}]}


def main(argv: list) -> int:
    verb = argv[1] if len(argv) > 1 else ""
    if verb == "manifest":
        print(json.dumps(manifest()))
    elif verb == "mcp":
        mcp()
    elif verb == "serve":  # no local daemon: the screen is remote. Stay up until stopped.
        while True:
            time.sleep(3600)
    elif verb in ("status", "stop", "--help"):
        pass
    elif verb == "call":
        print(json.dumps(call(argv[2], json.loads(argv[3]))))
    else:
        print(f"unknown verb {verb!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
