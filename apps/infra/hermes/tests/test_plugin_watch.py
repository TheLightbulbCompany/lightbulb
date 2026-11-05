"""watch / unwatch: only on a task run, ids from the turn, the backend's answer relayed."""
import json
import pathlib
import sys
import urllib.error

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

SESSION = "agent:ceo:lightbulb:task:7d1e2f30-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
TASK = "7d1e2f30-4a5b-4c6d-8e9f-0a1b2c3d4e5f"


def _ctx(tmp_path, monkeypatch, **over):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    base = {"apiUrl": "https://api.invalid/", "ownerId": "org_test", "hookToken": "h", "agentId": "ceo",
            "runId": "run-1", "attempt": 2, "runSecret": "sec", "taskId": TASK, "mode": "worker",
            "roomId": None, "sessionKey": SESSION}
    base.update(over)
    turn_context.write(tmp_path, SESSION, base)


def test_watch_only_on_a_task_run(tmp_path, monkeypatch):
    monkeypatch.setattr(lightbulb, "_post", lambda *a: pytest.fail("no HTTP call"))
    _ctx(tmp_path, monkeypatch, taskId=None, runSecret=None, roomId="11111111-1111-4111-8111-111111111111")
    assert lightbulb._watch({"kind": "url", "url": "https://x"}, session_id=SESSION) == lightbulb.WATCH_ONLY
    assert lightbulb._unwatch({"watch_id": "w"}, session_id=SESSION) == lightbulb.WATCH_ONLY
    _ctx(tmp_path, monkeypatch, mode="router")
    assert lightbulb._watch({"kind": "url", "url": "https://x"}, session_id=SESSION) == lightbulb.WATCH_ONLY


def test_watch_posts_the_turns_ids(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: calls.append((url, token, body))
                        or {"watchId": "w1", "giveUpAt": "2027-01-02T00:00:00+00:00"})
    out = json.loads(lightbulb._watch({"kind": "pr_checks", "repo": "acme/site", "number": 12, "every_min": 15,
                                       "runId": "evil", "taskId": "evil"}, session_id=SESSION))
    assert out["watchId"] == "w1" and "End your turn" in out["note"]
    (url, token, body), = calls
    assert url == f"https://api.invalid/api/v1/internal/tasks/org_test/{TASK}/watches" and token == "h"
    assert body == {"runId": "run-1", "attempt": 2, "runSecret": "sec", "kind": "pr_checks",
                    "target": {"repo": "acme/site", "number": 12}, "everyMin": 15}


def test_watch_give_up_hours_becomes_an_iso_time(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: calls.append(body) or {"watchId": "w1"})
    lightbulb._watch({"kind": "url", "url": "https://x.test/ready", "give_up_hours": 2}, session_id=SESSION)
    assert calls[0]["target"] == {"url": "https://x.test/ready"} and calls[0]["giveUpAt"].endswith("+00:00")
    import datetime as dt

    lightbulb._watch({"kind": "url", "url": "https://x.test/ready", "give_up_hours": 9000}, session_id=SESSION)
    capped = dt.datetime.fromisoformat(calls[1]["giveUpAt"])
    assert capped <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=14, minutes=1)  # capped at 14 days


def test_watch_bad_args_and_refusals(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(lightbulb, "_post", lambda *a: pytest.fail("no HTTP call"))
    assert "error" in json.loads(lightbulb._watch({"kind": "pr_checks", "repo": "acme/site"}, session_id=SESSION))
    assert "error" in json.loads(lightbulb._watch({"kind": "url", "url": "ftp://x"}, session_id=SESSION))
    assert "error" in json.loads(lightbulb._watch({"kind": "mail"}, session_id=SESSION))

    def refuse(*_a):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    monkeypatch.setattr(lightbulb, "_post", refuse)
    assert lightbulb._watch({"kind": "url", "url": "https://x"}, session_id=SESSION).startswith("Watch not started")


def test_unwatch_posts_remove(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: calls.append((url, body)) or {})
    assert lightbulb._unwatch({"watch_id": "w1"}, session_id=SESSION) == "stopped"
    assert calls == [(f"https://api.invalid/api/v1/internal/tasks/org_test/{TASK}/watches/remove",
                      {"runId": "run-1", "attempt": 2, "runSecret": "sec", "watchId": "w1"})]
    assert "error" in json.loads(lightbulb._unwatch({}, session_id=SESSION))


def test_registered_on_the_lightbulb_toolset():
    src = pathlib.Path(lightbulb.__file__).read_text()
    assert 'ctx.register_tool("watch", "lightbulb", WATCH_SCHEMA, _watch' in src
    assert 'ctx.register_tool("unwatch", "lightbulb", UNWATCH_SCHEMA, _unwatch' in src
