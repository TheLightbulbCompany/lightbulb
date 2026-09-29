"""The computer_use driver: Hermes's cua verbs and tools, each action sent to the bridge (stubbed)."""
from __future__ import annotations

import io
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import cua_driver  # noqa: E402


def _stub(monkeypatch, result):
    """The bridge's /browser/invoke, answering every action with ``result``."""
    calls = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def open_(request, timeout=None):
        assert request.full_url.endswith("/browser/invoke")
        calls.append({"action": json.loads(request.data)})
        return Response(json.dumps(result).encode())

    monkeypatch.setattr(cua_driver._NO_PROXY, "open", open_)
    return calls


def test_manifest_meets_hermes_runtime_contract(capsys):
    assert cua_driver.main(["lightbulb-cua", "manifest"]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["binary_version"] >= "0.20.0" and manifest["mcp_invocation"]["args"] == ["mcp"]
    verbs = {v["name"]: {a["name"] for a in v["args"]} for v in manifest["subcommands"]}
    assert {"--socket", "--grant"} <= verbs["mcp"] and "--embedded" in verbs["serve"] and verbs["stop"] == {"--socket"}
    assert cua_driver.main(["lightbulb-cua", "status", "--socket", "/tmp/s"]) == 0  # approvals off asks status
    assert cua_driver.main(["lightbulb-cua", "stop", "--socket", "/tmp/s"]) == 0


def test_input_tools_map_to_invoke_browser_on_the_held_session(monkeypatch):
    calls = _stub(monkeypatch, {"status": "SUCCESS"})
    cua_driver.call("click", {"x": 10, "y": 20})
    cua_driver.call("double_click", {"x": 1, "y": 2, "button": "right"})
    cua_driver.call("type_text", {"text": "hi"})
    cua_driver.call("press_key", {"key": "Enter"})
    cua_driver.call("hotkey", {"keys": ["ctrl", "l"]})
    cua_driver.call("scroll", {"direction": "down", "amount": 2})
    cua_driver.call("drag", {"from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4})
    assert [c["action"] for c in calls] == [
        {"mouseClick": {"x": 10, "y": 20, "button": "LEFT", "clickCount": 1}},
        {"mouseClick": {"x": 1, "y": 2, "button": "RIGHT", "clickCount": 2}},
        {"keyType": {"text": "hi"}},
        {"keyPress": {"key": "Enter"}},
        {"keyShortcut": {"keys": ["ctrl", "l"]}},
        {"mouseScroll": {"x": 640, "y": 450, "deltaX": 0, "deltaY": 200}},
        {"mouseDrag": {"startX": 1, "startY": 2, "endX": 3, "endY": 4, "button": "LEFT"}},
    ]


def test_capture_returns_the_real_screen(monkeypatch):
    _stub(monkeypatch, {"status": "SUCCESS", "data": "iVBORw=="})
    shot = cua_driver.call("get_window_state", {"pid": 1, "window_id": 1})
    assert shot["content"][0] == {"type": "image", "data": "iVBORw==", "mimeType": "image/png"}
    assert cua_driver.call("list_windows", {})["structuredContent"]["windows"][0]["window_id"] == 1


def test_mcp_stdio_lists_tools_and_reports_failures_as_tool_errors(monkeypatch):
    _stub(monkeypatch, {"status": "FAILED", "error": "no window"})
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "click", "arguments": {"x": 1, "y": 1}}},
    ]
    out = io.StringIO()
    cua_driver.mcp(io.StringIO("".join(json.dumps(r) + "\n" for r in requests)), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert {"click", "get_window_state", "hotkey"} <= {t["name"] for t in replies[1]["result"]["tools"]}
    assert replies[2]["result"]["isError"] is True and "no window" in replies[2]["result"]["content"][0]["text"]


def test_an_unreachable_bridge_is_a_clear_error(monkeypatch):
    def refuse(*_a, **_k):
        raise OSError("refused")

    monkeypatch.setattr(cua_driver._NO_PROXY, "open", refuse)
    try:
        cua_driver.call("click", {"x": 1, "y": 1})
    except RuntimeError as exc:
        assert "use a browser tool first" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
