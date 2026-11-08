"""`transfer_call`: a Router tool for a phone caller's batch only. It asks the backend to post the call room's
transfer line (`action: "transfer"`); the phone worker does the SIP transfer."""
import json
import urllib.error

import pytest

from bridge import turn
from plugins import lightbulb as pkg
from plugins.lightbulb import router as rt
from tests.test_runs_v1 import Agent, quiet_turn, threads  # noqa: F401 -- fixtures
from tests.test_runs_v1_router import IDENTITY, _post

RUN = "1c6d7e40-5b6c-4d7e-8f90-a1b2c3d4e5f6"
CTX = {"runId": RUN, "mode": "router", "apiUrl": "https://api.example/", "ownerId": "org_1", "hookToken": "hook-1"}


def _call(**args):
    return rt._transfer_call(args, session_id="s")


def test_transfer_call_posts_the_route(monkeypatch):
    monkeypatch.setattr(rt.turn_context, "read", lambda sid: dict(CTX))
    posted = []
    monkeypatch.setattr(pkg, "_post", lambda url, token, body: posted.append((url, token, body)) or {"messageId": "m"})
    assert _call(trigger_ids=["voice:c1:turn-0"], text=" Let me put you through. ") == "transferring"
    assert posted == [("https://api.example/api/v1/internal/runtime/org_1/transfer-call", "hook-1",
                       {"triggerIds": ["voice:c1:turn-0"], "text": "Let me put you through."})]


def test_transfer_call_refusals(monkeypatch):
    monkeypatch.setattr(rt.turn_context, "read", lambda sid: {"runId": RUN, "mode": "worker"})
    assert _call(trigger_ids=["t"], text="x") == rt.NOT_ROUTER
    monkeypatch.setattr(rt.turn_context, "read", lambda sid: dict(CTX))
    assert "trigger_ids" in json.loads(_call(trigger_ids=[], text="x"))["error"]
    assert "text" in json.loads(_call(trigger_ids=["t"], text=" "))["error"]

    def refused(url, token, body):
        raise urllib.error.HTTPError(url, 403, "no", {}, None)

    monkeypatch.setattr(pkg, "_post", refused)
    assert json.loads(_call(trigger_ids=["t"], text="x"))["error"].startswith("not transferred")


@pytest.mark.parametrize("call", [True, False])
def test_only_a_callers_batch_gets_the_tool(quiet_turn, threads, monkeypatch, call):  # noqa: F811
    built = {}
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: built.update(k) or Agent())
    _post({**IDENTITY, **({"call": True} if call else {})})
    threads[0].run_now()
    want = ["lightbulb_router", "lightbulb_router_call"] if call else ["lightbulb_router"]
    assert built["enabled_toolsets"] == want
