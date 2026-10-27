"""request_purchase: the agent only asks; ids come from the turn; only {requestId, status} comes back."""
import io
import json
import pathlib
import sys
import urllib.error

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

SESSION = "agent:growth:lightbulb:dm:r"
ARGS = {"merchant": "Acme", "url": "https://acme.com", "amount": 35, "why": "Licence for the launch.",
        "issue_id": "iss_1", "agentId": "evil", "ownerId": "evil", "hookToken": "evil"}


def _ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    turn_context.write(tmp_path, SESSION, {"apiUrl": "https://api.invalid/", "ownerId": "org_test",
                                           "hookToken": "h", "agentId": "growth", "runId": "r1"})


def test_posts_as_the_turns_agent_and_returns_only_id_and_status(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: calls.append((url, token, body)) or
                        {"requestId": "r", "status": "needs_owner", "approvalUrl": "https://leak", "card": "4242"})
    out = json.loads(lightbulb._request_purchase(ARGS, session_id=SESSION))
    assert out == {"requestId": "r", "status": "needs_owner"}
    [(url, token, body)] = calls
    assert url == "https://api.invalid/api/v1/internal/link-wallet/org_test/requests" and token == "h"
    assert body == {"agentId": "growth", "merchant": "Acme", "url": "https://acme.com", "amount": 35,
                    "why": "Licence for the launch.", "issueId": "iss_1"}


def test_a_refusal_shows_its_code_and_a_dark_workspace_says_so(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)

    def refuse(code, detail=None):
        def post(*a):
            raise urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps({"detail": detail}).encode()))
        return post

    monkeypatch.setattr(lightbulb, "_post", refuse(409, {"code": "over_purchase_cap", "message": "m"}))
    assert "over_purchase_cap" in lightbulb._request_purchase(ARGS, session_id=SESSION)
    monkeypatch.setattr(lightbulb, "_post", refuse(404, "Not found"))
    assert "not switched on" in lightbulb._request_purchase(ARGS, session_id=SESSION)


def test_refused_outside_a_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    monkeypatch.setattr(lightbulb, "_post", lambda *a: pytest.fail("no HTTP call"))
    assert lightbulb._request_purchase(ARGS, session_id="none") == lightbulb.PURCHASE_NO_TURN


def test_is_registered(monkeypatch):
    registered = {}
    monkeypatch.setattr(lightbulb, "_register_redaction_default", lambda: 0)

    class Registry:
        def register_hook(self, *_a):
            pass

        def register_tool(self, name, toolset, schema, handler, **_kw):
            registered[name] = (schema, handler)

    lightbulb.register(Registry())
    assert registered["request_purchase"] == (lightbulb.PURCHASE_SCHEMA, lightbulb._request_purchase)
