import base64
import hashlib
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
from lightbulb import desktop_resources, desktop_transport


def activate(root, session, run):
    path = (
        root
        / ".lightbulb-turn"
        / ("desktop-active-" + hashlib.sha256(session.encode()).hexdigest() + ".json")
    )
    path.write_text(json.dumps({"runId": run}))


def test_queued_same_session_cannot_replace_active_desktop_authority(
    tmp_path, monkeypatch
):
    from lightbulb import desktop, turn_context

    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    for run in ("active", "queued"):
        ctx = {"runId": run, "family": "room", "sessionKey": "same-session"}
        desktop.start_run_desktop(tmp_path, ctx)
        turn_context.write(tmp_path, "same-session", ctx)
    activate(tmp_path, "same-session", "active")
    seen = []
    monkeypatch.setattr(
        desktop_transport, "_http", lambda ctx, *_a: seen.append(ctx["runId"]) or {}
    )
    desktop.handle({"action": "devices"}, session_id="same-session")
    assert seen == ["active"]
    # Old turn context cleanup cannot deprive the queued turn of its own ledger.
    turn_context.clear(tmp_path, "same-session")
    activate(tmp_path, "same-session", "queued")
    desktop.handle({"action": "devices"}, session_id="same-session")
    assert seen == ["active", "queued"]


@pytest.mark.parametrize("family", ["room", "paperclip"])
def test_tool_sessions_track_before_dispatch_and_poll_without_resubmit(
    tmp_path, monkeypatch, family
):
    from lightbulb import desktop, turn_context

    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    calls = []
    for session in ("one", "two"):
        ctx = {
            "runId": session,
            "agentId": "agent",
            "ownerId": "owner",
            "family": family,
            "hookToken": "hook-" + session,
            "localSessionToken": "never-use",
            "apiUrl": "https://example.invalid",
            "sessionKey": session,
        }
        turn_context.write(tmp_path, session, ctx)
        desktop.start_run_desktop(tmp_path, ctx)
        activate(tmp_path, session, session)

    def http(ctx, method, path, body=None, admission=""):
        calls.append((ctx.copy(), method, path, body, admission))
        if path == "admission":
            return {
                "admissionToken": "cap-" + ctx["runId"],
                "deviceId": body["deviceId"],
                "generation": body["expectedConnectionGeneration"],
            }
        if path == "operations":
            with desktop.run_state(tmp_path, ctx["runId"]) as state:
                receipt = admission + "." + body["requestId"]
                assert receipt in state["receipts"]
            return {"id": receipt, "state": "pending_approval"}
        return {"id": path.removeprefix("operations/"), "state": "running"}

    monkeypatch.setattr(desktop_transport, "_http", http)
    receipts = []
    for session in ("one", "two"):
        result = json.loads(
            desktop.handle(
                {
                    "action": "invoke",
                    "deviceId": "mac-" + session,
                    "generation": "gen-" + session,
                    "command": "system.run",
                    "args": {"argv": ["pwd"], "cwd": "/Users/" + session},
                },
                session_id=session,
            )
        )
        receipts.append(result["receiptId"])
        assert "cap-" not in json.dumps(result)  # capability stays out of model text
        desktop.handle(
            {"action": "status", "receiptId": result["receiptId"]}, session_id=session
        )
    assert len([c for c in calls if c[2] == "operations"]) == 2
    assert [
        (c[0]["hookToken"], c[3]["deviceId"], c[3]["params"]["cwd"])
        for c in calls
        if c[2] == "operations"
    ] == [("hook-one", "mac-one", "/Users/one"), ("hook-two", "mac-two", "/Users/two")]
    before = len(calls)
    assert "unavailable" in desktop.handle(
        {"action": "status", "receiptId": receipts[0]}, session_id="two"
    )
    assert len(calls) == before


@pytest.mark.parametrize(
    "args",
    [
        {"action": "invoke", "command": "system.execApprovals.set", "args": {}},
        {
            "action": "invoke",
            "command": "system.run",
            "args": {"argv": ["pwd"], "cwd": "/", "approved": True},
        },
        # The legacy typed-browser action is gone: Mac browsing is use_browser only.
        {
            "action": "browser",
            "profile": "lightbulb",
            "browserAction": "open",
            "args": {"url": "https://x.com"},
        },
        {"action": "devices", "approval": True},
    ],
)
def test_tool_rejects_admin_raw_and_approval_before_network(
    tmp_path, monkeypatch, args
):
    from lightbulb import desktop, turn_context

    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    ctx = {"runId": "run", "family": "room", "sessionKey": "session"}
    turn_context.write(tmp_path, "session", ctx)
    desktop.start_run_desktop(tmp_path, ctx)
    activate(tmp_path, "session", "run")
    monkeypatch.setattr(
        desktop_transport, "_http", lambda *a, **k: pytest.fail("unexpected HTTP")
    )
    assert "unavailable" in desktop.handle(
        args | {"deviceId": "mac", "generation": "gen"}, session_id="session"
    )


def test_unavailable_and_missing_turn_fail_closed(tmp_path, monkeypatch):
    from lightbulb import desktop, turn_context

    monkeypatch.setattr(
        desktop_transport, "_http", lambda *a, **k: pytest.fail("unexpected HTTP")
    )
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    assert "unavailable" in desktop.handle({"action": "devices"}, session_id="session")
    desktop.start_run_desktop(
        tmp_path, {"family": "unavailable", "runId": "run", "sessionKey": "session"}
    )
    activate(tmp_path, "session", "run")
    assert "unavailable" in desktop.handle({"action": "devices"}, session_id="session")


def test_devices_only_exposes_bounded_selection_metadata(tmp_path, monkeypatch):
    from lightbulb import desktop, desktop_transport, turn_context

    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    ctx = {"runId": "run", "family": "room", "sessionKey": "session"}
    turn_context.write(tmp_path, "session", ctx)
    desktop.start_run_desktop(tmp_path, ctx)
    activate(tmp_path, "session", "run")
    monkeypatch.setattr(
        desktop_transport,
        "_http",
        lambda *_args: {
            "devices": [
                {
                    "deviceId": "mac",
                    "generation": "gen",
                    "commands": ["device.info", "system.run"],
                    "admissionToken": "private-capability",
                    "expiresInSeconds": 60,
                }
            ],
            "submissionsEnabled": True,
            "privateField": "must-not-leak",
        },
    )

    result = json.loads(desktop.handle({"action": "devices"}, session_id="session"))

    assert result == {
        "devices": [
            {
                "deviceId": "mac",
                "generation": "gen",
                "commands": ["device.info", "system.run"],
            }
        ],
        "submissionsEnabled": True,
    }


def test_actual_run_scope_binds_and_clears_active_desktop_identity(home, monkeypatch):
    from types import SimpleNamespace
    from bridge import turn
    from lightbulb import desktop, turn_context
    from tests import factories as f

    profile = home / "profiles" / "ceo"
    monkeypatch.setattr(turn_context, "_home", lambda: profile)
    bodies = []
    for run_id in ("first", "queued"):
        body = f.ingress()
        body["run"]["runId"] = run_id
        bodies.append(body)
        session = turn._resolve_session_key("ceo", body)
        ctx = {"runId": run_id, "sessionKey": session, "family": "room"}
        turn.write_turn_context(profile, session, ctx)
        turn.register_run(run_id)
        turn._start_desktop_turn(profile, ctx)
    seen = []
    monkeypatch.setattr(
        desktop_transport, "_http", lambda ctx, *_a: seen.append(ctx["runId"]) or {}
    )

    def construct(*_a):
        def conversation(**_kw):
            desktop.handle({"action": "devices"}, session_id=session)
            return {"final_response": "ok"}

        return SimpleNamespace(run_conversation=conversation)

    monkeypatch.setattr(turn, "_construct_agent", construct)
    for body in bodies:
        turn._run_hermes("ceo", body, None)
        assert "unavailable" in desktop.handle(
            {"action": "devices"}, session_id=session
        )
    assert seen == ["first", "queued"]


@pytest.mark.parametrize("phase", ["pending_approval", "running", "submitting"])
@pytest.mark.parametrize("family", ["room", "paperclip"])
def test_bridge_stop_cancels_exact_run_across_module_namespaces(
    tmp_path, monkeypatch, phase, family
):
    import threading
    from bridge import turn
    from lightbulb import desktop, turn_context

    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    cancelled = threading.Event()
    sent = []
    for session in ("one", "two"):
        ctx = {
            "runId": session,
            "agentId": "agent",
            "ownerId": "owner",
            "family": family,
            "hookToken": "hook-" + session,
            "apiUrl": "https://example.invalid",
            "sessionKey": session,
        }
        turn_context.write(tmp_path, session, ctx)
        turn.register_run(session)
        turn._start_desktop_turn(tmp_path, ctx)
        activate(tmp_path, session, session)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, *_):
            return b"{}"

    def urlopen(request, **kwargs):
        assert request.get_header("X-desktop-run") == "one"
        assert request.get_header("X-desktop-run-family") == family
        assert request.get_header("Authorization") == "Bearer hook-one"
        sent.append(request.full_url)
        cancelled.set()
        return Response()

    def http(ctx, method, path, body=None, admission=""):
        if path == "admission":
            return {
                "admissionToken": "cap-" + ctx["runId"],
                "deviceId": body["deviceId"],
                "generation": body["expectedConnectionGeneration"],
            }
        assert path == "operations"
        if phase == "submitting" and ctx["runId"] == "one":
            # Cancellation reaches the backend before the submit response returns.
            turn.abort_run("one")
            assert cancelled.wait(2)
        return {"id": admission + "." + body["requestId"], "state": phase}

    monkeypatch.setattr(desktop_transport, "_http", http)
    monkeypatch.setattr(turn.urllib.request, "urlopen", urlopen)
    args = {
        "action": "invoke",
        "deviceId": "mac",
        "generation": "gen",
        "command": "system.run",
        "args": {"argv": ["pwd"], "cwd": "/Users/one"},
    }
    other = json.loads(desktop.handle(args, session_id="two"))
    result = json.loads(desktop.handle(args, session_id="one"))
    turn.abort_run("one")
    assert cancelled.wait(2)
    assert len(sent) == 1 and sent[0].endswith(
        "cap-one." + result["receiptId"] + "/cancel"
    )
    assert other["receiptId"] not in sent[0]
    assert "unavailable" in desktop.handle(args, session_id="one")
    with desktop.run_state(tmp_path, "two") as state:
        assert not state["stopped"]


def test_registration_and_schema_are_strict(monkeypatch):
    import lightbulb
    from lightbulb import desktop

    registered = {}
    monkeypatch.setattr(lightbulb, "_register_redaction_default", lambda: 0)

    class Registry:
        def register_hook(self, *_args):
            pass  # main's cloud browser hooks remain registered alongside this tool

        def register_tool(self, name, toolset, schema, handler, **kwargs):
            registered[name] = (schema, handler)

    lightbulb.register(Registry())
    assert registered["lightbulb_desktop"] == (desktop.SCHEMA, desktop.handle)
    assert not any("terminal" in name or name == "browser" for name in registered)
    # Mac browsing is use_browser/use_cloud_browser only; the legacy typed action is gone.
    actions = desktop.SCHEMA["parameters"]["properties"]["action"]["enum"]
    assert "browser" not in actions and {"use_browser", "use_cloud_browser"} <= set(actions)
    schema_text = json.dumps(desktop.SCHEMA)
    assert "admissionToken" not in schema_text and "approved" not in schema_text


def test_desktop_tool_never_acquires_shared_cloud_browser_lease(monkeypatch):
    control = pytest.importorskip(
        "bridge.browser_control", reason="integration fence for main PR #2818"
    )
    monkeypatch.setattr(
        control,
        "_refresh",
        lambda *_a: pytest.fail("desktop cannot acquire cloud browser"),
    )
    lease = control.begin_turn("room-run", "agent")
    try:
        assert (
            control.pre_tool_call(
                tool_name="lightbulb_desktop", args={"action": "devices"}
            )
            is None
        )
        control.post_tool_call(tool_name="lightbulb_desktop", status="ok")
        assert lease["generation"] is None
    finally:
        control.release(lease)


def test_screen_hash_and_large_text_are_artifact_references(tmp_path):
    from lightbulb import desktop

    data = b"image-bytes"
    receipt = {
        "state": "succeeded",
        "id": "private-capability",
        "result": {
            "format": "png",
            "size": len(data),
            "base64": base64.b64encode(data).decode(),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    }
    result = json.loads(
        desktop._result(receipt, "public-id", tmp_path, "screen.snapshot")
    )
    assert pathlib.Path(result["result"]["artifacts"][0]["path"]).read_bytes() == data
    assert "base64" not in json.dumps(
        result
    ) and "private-capability" not in json.dumps(result)
    receipt["result"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash"):
        desktop._result(receipt, "public-id", tmp_path, "screen.snapshot")
    result = json.loads(
        desktop._result(
            {"state": "succeeded", "result": {"text": "x" * 40000}},
            "id",
            tmp_path,
            "system.run",
        )
    )
    assert len(json.dumps(result)) < 1000
    assert (
        json.loads(pathlib.Path(result["resultReference"]).read_text())["result"][
            "text"
        ]
        == "x" * 40000
    )


def test_slow_status_retains_receipt_without_resubmission(tmp_path, monkeypatch):
    from lightbulb import desktop, turn_context

    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    ctx = {"runId": "run", "family": "room", "sessionKey": "session"}
    turn_context.write(tmp_path, "session", ctx)
    desktop.start_run_desktop(tmp_path, ctx)
    activate(tmp_path, "session", "run")
    request_id = "00000000-0000-4000-8000-000000000001"
    with desktop.run_state(tmp_path, "run") as state:
        state["receipts"]["cap." + request_id] = {"command": "browser.proxy"}
    calls = []

    def http(ctx, method, path, *args):
        calls.append((method, path))
        if len(calls) == 1:
            raise TimeoutError("slow status")
        return {"state": "running"}

    monkeypatch.setattr(desktop_transport, "_http", http)
    first = json.loads(
        desktop.handle(
            {"action": "status", "receiptId": request_id}, session_id="session"
        )
    )
    assert first["state"] == "unknown" and first["receiptId"] == request_id
    second = json.loads(
        desktop.handle(
            {"action": "status", "receiptId": request_id}, session_id="session"
        )
    )
    assert second["state"] == "running"
    assert calls == [("GET", "operations/cap." + request_id)] * 2


def test_file_write_reads_profile_source_and_never_retries_unknown(
    tmp_path, monkeypatch
):
    source = tmp_path / "data.bin"
    source.write_bytes(b"\x00binary\xff")
    monkeypatch.setattr(desktop_transport.turn_context, "_home", lambda: tmp_path)
    calls = []

    def http(*args):
        calls.append(args)
        raise OSError("response lost")

    monkeypatch.setattr(desktop_transport, "_http", http)
    result = desktop_resources.file_action(
        "device",
        "generation",
        "file.write",
        {"path": "/approved/data.bin", "sourcePath": str(source)},
        ctx={"runId": "run", "agentId": "agent"},
        admission_token="token",
        request_id="00000000-0000-4000-8000-000000000001",
    )
    assert result["state"] == "unknown"
    assert len(calls) == 1
    params = calls[0][3]["params"]
    assert base64.b64decode(params["contentBase64"]) == source.read_bytes()
    assert params["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert "sourcePath" not in params
    source.unlink()
    source.symlink_to(tmp_path.parent)
    with pytest.raises(ValueError):
        desktop_resources.file_action(
            "device",
            "generation",
            "file.write",
            {"path": "/approved/data.bin", "sourcePath": str(source)},
            ctx={"runId": "run", "agentId": "agent"},
            admission_token="token",
            request_id="00000000-0000-4000-8000-000000000002",
        )
    assert len(calls) == 1


def test_mcp_call_keeps_server_and_catalog_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(
        desktop_transport,
        "_http",
        lambda *args: calls.append(args) or {"state": "queued"},
    )
    desktop_resources.mcp_call(
        "device",
        "generation",
        "my-server",
        "echo",
        {"value": 7},
        "catalog-v",
        ctx={"runId": "run", "agentId": "agent"},
        admission_token="token",
        request_id="00000000-0000-4000-8000-000000000001",
    )
    assert calls[0][3]["command"] == "mcp_call"
    assert calls[0][3]["params"] == {
        "server": "my-server",
        "tool": "echo",
        "arguments": {"value": 7},
        "catalogVersion": "catalog-v",
    }


# ---- use_browser: the run's browser_* tools on the requester's Mac ---------------------------


@pytest.fixture
def mac_turn(tmp_path, monkeypatch):
    """An active room turn with bridge.browser_control's Mac routes stubbed at _post_mac."""
    from lightbulb import desktop, turn_context

    control = pytest.importorskip("bridge.browser_control")
    monkeypatch.setenv("BROWSER_CDP_URL", "ws://127.0.0.1:9222")
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    mux = {"shown": [], "upstream": []}
    monkeypatch.setattr(control.cdp_mux, "set_upstream", lambda url, gen, upstream="cloud": mux["upstream"].append(upstream))
    monkeypatch.setattr(control.cdp_mux, "close_run", lambda run, upstream=None: None)
    monkeypatch.setattr(control.cdp_mux, "drop", lambda name: None)
    monkeypatch.setattr(control.cdp_mux, "human_input_at", lambda name: 0.0)
    monkeypatch.setattr(control.cdp_mux, "last_close", lambda name: None)
    monkeypatch.setattr(control.cdp_mux, "show", lambda run, name: mux["shown"].append((run, name)) or True)
    monkeypatch.setattr(control, "_disconnect", lambda task_id: True)
    monkeypatch.setattr(control, "_MAC_STATES", {})
    posted, answers = [], []
    mux["posted"], mux["answers"] = posted, answers

    def post(path, body, timeout=20.0):
        posted.append((path, dict(body)))
        if path == "mac/acquire":
            return answers[0] if answers else (200, {"deviceId": body.get("deviceId") or "mac-1", "generation": "m1",
                                                     "cdpUrl": "ws://door", "deviceName": "Ada's Mac", "sync": None})
        return 200, {"sync": None}

    monkeypatch.setattr(control, "_post_mac", post)

    def start(family="room"):
        ctx = {"runId": "run-1", "family": family, "sessionKey": "s"}
        desktop.start_run_desktop(tmp_path, ctx)
        activate(tmp_path, "s", "run-1")
        return control.begin_turn("run-1", "agent", None, family)

    mux["start"] = start
    yield mux
    control._TURN.set(None)


def _use(args):
    from lightbulb import desktop

    return json.loads(desktop.handle({"action": "use_browser", **args}, session_id="s"))


def test_use_browser_with_no_arguments(mac_turn):
    mac_turn["start"]()
    assert _use({}) == {"browser": "mac", "deviceId": "mac-1", "deviceName": "Ada's Mac"}
    assert mac_turn["posted"] == [("mac/acquire", {"runId": "run-1", "agentId": "agent", "family": "room", "budgetS": 25.0})]


def test_use_browser_with_device_id(mac_turn):
    mac_turn["start"]()
    assert _use({"deviceId": "mac-2"})["deviceId"] == "mac-2"
    assert mac_turn["posted"][0][1]["deviceId"] == "mac-2"


def test_use_browser_rejects_generation_arg(mac_turn):
    mac_turn["start"]()
    assert "error" in _use({"generation": "m1"})
    assert mac_turn["posted"] == []


def test_use_browser_returns_backend_error_verbatim(mac_turn):
    mac_turn["start"]()
    message = "Update the Lightbulb desktop app on that Mac to let agents use its browser."
    mac_turn["answers"].append((409, {"code": "desktop_update_required", "message": message}))
    assert _use({}) == {"error": message}


def test_use_browser_result_carries_device_name(mac_turn):
    mac_turn["start"]()
    assert _use({})["deviceName"] == "Ada's Mac"


def test_use_browser_default_never_shows(mac_turn):
    mac_turn["start"]()
    _use({})
    _use({"show": False})
    assert mac_turn["shown"] == []


def test_use_browser_show_sends_lightbulb_show_once(mac_turn):
    mac_turn["start"]()
    _use({})
    assert _use({"show": True})["shown"] is True
    assert mac_turn["shown"] == [("run-1", "mac:mac-1:run-1")]


def test_use_cloud_browser(mac_turn):
    from lightbulb import desktop

    state = mac_turn["start"]()
    _use({})
    assert json.loads(desktop.handle({"action": "use_cloud_browser"}, session_id="s")) == {"browser": "cloud"}
    assert state["mac"] is None and mac_turn["posted"][-1][0] == "mac/release"


def test_use_browser_in_a_paperclip_turn_passes_family(mac_turn):
    mac_turn["start"]("paperclip")
    _use({})
    assert mac_turn["posted"][0][1]["family"] == "paperclip"


def test_a_task_run_sends_its_attempt_proof(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self, _n):
            return b"{}"

    def urlopen(request, timeout):
        seen.update({k.lower(): v for k, v in request.header_items()})
        return Response()

    monkeypatch.setattr(desktop_transport.urllib.request, "urlopen", urlopen)
    ctx = {"apiUrl": "https://x", "hookToken": "h", "ownerId": "o", "runId": "r", "agentId": "a"}
    desktop_transport._http(ctx | {"family": "task", "taskId": "t1", "runSecret": "s1"}, "GET", "devices")
    assert (seen["x-desktop-run-family"], seen["x-desktop-task"], seen["x-desktop-run-secret"]) == ("task", "t1", "s1")
    seen.clear()
    desktop_transport._http(ctx | {"family": "room"}, "GET", "devices")
    assert "x-desktop-task" not in seen and "x-desktop-run-secret" not in seen


def test_the_task_session_desktop_ctx_names_its_attempt(monkeypatch):
    from bridge import turn

    seen = {}
    monkeypatch.setattr(turn, "_start_desktop_turn", lambda _root, ctx: seen.update(ctx))
    monkeypatch.setattr(turn, "_run_task_turn", lambda *_a, **_k: ("", None, "", {}))
    monkeypatch.setattr(turn, "cancel_run_desktop", lambda _r: None)
    turn._run_task_session(
        {"agentId": "a1", "runId": "r1", "sessionKey": "k", "taskId": "t1", "runSecret": "s1"}, {}
    )
    assert (seen["family"], seen["taskId"], seen["runSecret"]) == ("task", "t1", "s1")


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (403, "unsupported_run_family", "denied"),
        (403, "run_member_authority_unavailable", "denied"),
        (409, "desktop_target_not_found", "no paired Mac"),
        (503, "desktop_disabled", "unavailable"),
    ],
)
def test_desktop_errors_say_the_real_reason(tmp_path, monkeypatch, status, code, expected):
    import io
    import urllib.error
    from lightbulb import desktop, turn_context

    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    ctx = {"runId": "run", "family": "task", "sessionKey": "session"}
    desktop.start_run_desktop(tmp_path, ctx)
    activate(tmp_path, "session", "run")

    def refuse(*_a, **_k):
        body = io.BytesIO(json.dumps({"detail": {"code": code}}).encode())
        raise urllib.error.HTTPError("u", status, "x", {}, body)

    monkeypatch.setattr(desktop_transport, "_http", refuse)
    result = json.loads(desktop.handle({"action": "devices"}, session_id="session"))
    assert expected in result["error"] and result["code"] == code
    assert "another" in result["error"]  # never a stand-in session


def test_no_paired_mac_is_said_plainly(tmp_path, monkeypatch):
    from lightbulb import desktop, turn_context

    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    ctx = {"runId": "run", "family": "task", "sessionKey": "session"}
    desktop.start_run_desktop(tmp_path, ctx)
    activate(tmp_path, "session", "run")
    monkeypatch.setattr(desktop_transport, "_http", lambda *_a: {"devices": [], "submissionsEnabled": True})
    result = json.loads(desktop.handle({"action": "devices"}, session_id="session"))
    assert result["devices"] == [] and "no paired Mac" in result["note"]


def test_the_skill_forbids_a_stand_in_session_for_the_mac():
    skill = (pathlib.Path(__file__).resolve().parents[1] / "skills-src/lightbulb/SKILL.md").read_text()
    assert "as a stand-in for it" in skill and "ask the owner" in skill
