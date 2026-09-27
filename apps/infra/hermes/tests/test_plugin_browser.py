"""browser_screen (AWS InvokeBrowser) and the re-registered browser_cdp."""
import json
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
from lightbulb import browser  # noqa: E402

REF = ("aws.browser.v1", "01ABC")


class FakeClient:
    def __init__(self, result):
        self.calls, self.result = [], result

    def invoke_browser(self, **kw):
        self.calls.append(kw)
        return {"result": self.result, "sessionId": REF[1]}


def _stub(monkeypatch, result, aux_vision=False):
    client = FakeClient(result)
    monkeypatch.setattr(browser, "_client", lambda: client)
    monkeypatch.setattr(browser.browser_control, "session_ref", lambda: REF)
    tool = types.ModuleType("tools.computer_use.tool")
    tool._should_route_through_aux_vision = lambda: aux_vision
    monkeypatch.setitem(sys.modules, "tools.computer_use.tool", tool)
    return client


def test_input_actions_map_to_invoke_browser_shapes(monkeypatch):
    client = _stub(monkeypatch, {"mouseClick": {"status": "SUCCESS"}})
    assert json.loads(browser.screen({"action": "click", "x": 10, "y": 20}))["success"] is True
    for args in ({"action": "key", "key": "ctrl+shift+i"}, {"action": "key", "key": "Enter"},
                 {"action": "type", "text": "hi"}, {"action": "scroll", "x": 5, "y": 6, "deltaY": 300}):
        browser.screen(args)
    assert [c["action"] for c in client.calls] == [
        {"mouseClick": {"x": 10, "y": 20, "button": "LEFT", "clickCount": 1}},
        {"keyShortcut": {"keys": ["ctrl", "shift", "i"]}},
        {"keyPress": {"key": "Enter"}},
        {"keyType": {"text": "hi"}},
        {"mouseScroll": {"x": 5, "y": 6, "deltaX": 0, "deltaY": 300}},
    ]
    assert all(c["browserIdentifier"] == REF[0] and c["sessionId"] == REF[1] for c in client.calls)


def test_screenshot_returns_the_image_to_the_model(monkeypatch):
    _stub(monkeypatch, {"screenshot": {"status": "SUCCESS", "data": b"\x89PNG"}})
    result = browser.screen({"action": "screenshot"})
    assert result["_multimodal"] is True
    assert result["content"][1]["image_url"]["url"] == "data:image/png;base64,iVBORw=="


def test_screenshot_for_a_text_only_model_points_at_vision_analyze(monkeypatch):
    _stub(monkeypatch, {"screenshot": {"status": "SUCCESS", "data": b"\x89PNG"}}, aux_vision=True)
    result = json.loads(browser.screen({"action": "screenshot"}))
    assert pathlib.Path(result["path"]).read_bytes() == b"\x89PNG" and "vision_analyze" in result["note"]


def test_failures_are_reported_not_raised(monkeypatch):
    _stub(monkeypatch, {"mouseClick": {"status": "FAILED", "error": "no window"}})
    assert json.loads(browser.screen({"action": "click", "x": 1, "y": 1}))["error"] == "no window"
    assert "Bad arguments" in json.loads(browser.screen({"action": "click"}))["error"]
    monkeypatch.setattr(browser.browser_control, "session_ref", lambda: None)
    assert json.loads(browser.screen({"action": "screenshot"}))["success"] is False


def test_browser_cdp_is_hermes_own_tool_on_the_bridge_connection(monkeypatch):
    cdp = types.ModuleType("tools.browser_cdp_tool")
    cdp.BROWSER_CDP_SCHEMA = {"name": "browser_cdp", "description": "d", "parameters": {"type": "object"}}
    cdp.browser_cdp = lambda **kw: ("called", kw["method"], cdp._resolve_cdp_endpoint())
    monkeypatch.setitem(sys.modules, "tools.browser_cdp_tool", cdp)
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    sys.modules["tools"].browser_cdp_tool = cdp
    monkeypatch.setattr(browser.browser_control, "discovery_payload", lambda: {"webSocketDebuggerUrl": "wss://aws/s"})
    registered = {}

    class Ctx:
        def register_tool(self, name, toolset, schema, handler, **kw):
            registered[name] = (toolset, handler, kw)

    browser.register(Ctx())
    toolset, handler, kw = registered["browser_cdp"]
    assert toolset == "browser-cdp" and kw["override"] is True
    assert handler({"method": "Target.getTargets"}) == ("called", "Target.getTargets", "wss://aws/s")
    assert registered["browser_screen"][0] == "lightbulb"
