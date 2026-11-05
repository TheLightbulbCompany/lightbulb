"""`propose_automation`: a Router-only tool that asks the backend to draft an automation and post its confirm card."""
import json

import pytest

from plugins import lightbulb as pkg
from plugins.lightbulb import router as rt

RUN = "1c6d7e40-5b6c-4d7e-8f90-a1b2c3d4e5f6"
CTX = {"runId": RUN, "mode": "router", "apiUrl": "https://api.example/", "ownerId": "org_1", "hookToken": "hook-1"}


@pytest.fixture
def router_turn(monkeypatch):
    monkeypatch.setattr(rt.turn_context, "read", lambda sid: dict(CTX))


def _call(**args):
    return rt._propose_automation(args, session_id="s")


def test_propose_automation_router_only(monkeypatch):
    seen = []

    class Ctx:
        def register_tool(self, name, toolset, schema, handler, **kw):
            seen.append((name, toolset))

    rt.register(Ctx())
    assert ("propose_automation", "lightbulb_router") in seen
    assert all(toolset == "lightbulb_router" for _, toolset in seen)  # never on a Worker toolset
    monkeypatch.setattr(rt.turn_context, "read", lambda sid: {"runId": RUN, "mode": "worker"})
    assert _call(trigger_ids=["chat:m1"], name="n", instructions="i", cron=["0 9 * * 1"]) == rt.NOT_ROUTER


def test_propose_automation_posts_route(router_turn, monkeypatch):
    posted = []

    def fake_post(url, token, body):
        posted.append((url, token, body))
        return {"automationId": "01HZZ"}

    monkeypatch.setattr(pkg, "_post", fake_post)
    out = _call(
        trigger_ids=["chat:m1"],
        name="Monday signups",
        instructions="Post the week's signups here.",
        cron=["0 9 * * 1"],
        tz="America/New_York",
        specialist="agent_growth",
        deliver_to="room:r1",
        budget_per_run_usd=2.5,
    )
    assert out == "proposed"
    (url, token, body), = posted
    assert url == "https://api.example/api/v1/internal/automations/org_1/propose" and token == "hook-1"
    assert body == {
        "triggerIds": ["chat:m1"],
        "name": "Monday signups",
        "instructions": "Post the week's signups here.",
        "cron": ["0 9 * * 1"],
        "tz": "America/New_York",
        "specialistId": "agent_growth",
        "deliverTo": "room:r1",
        "budgetPerRunUsd": 2.5,
    }


def test_propose_automation_rejects_bad_args(router_turn, monkeypatch):
    monkeypatch.setattr(pkg, "_post", lambda *a: pytest.fail("must not post"))
    assert "error" in json.loads(_call(trigger_ids=["chat:m1"], name="n", instructions="i"))  # no cron
    assert "error" in json.loads(_call(trigger_ids=[], name="n", instructions="i", cron=["0 9 * * 1"]))
    assert "error" in json.loads(_call(trigger_ids=["chat:m1"], name="", instructions="i", cron=["0 9 * * 1"]))
    assert "error" in json.loads(_call(trigger_ids=["chat:m1"], name="n", instructions="i", cron="0 9 * * 1"))


def test_propose_automation_reports_backend_refusal(router_turn, monkeypatch):
    import urllib.error

    def refuse(url, token, body):
        raise urllib.error.HTTPError(url, 400, "bad", {}, None)

    monkeypatch.setattr(pkg, "_post", refuse)
    out = json.loads(_call(trigger_ids=["chat:m1"], name="n", instructions="i", cron=["*/5 * * * *"]))
    assert out["error"].startswith("not proposed")
