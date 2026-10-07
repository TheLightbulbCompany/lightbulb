"""Load-test fixtures (scripts/load/fixtures.py): the pure helpers and the file/teardown safety.
No network: Clerk and the API are never reached."""

import base64
import json
import os
import stat

import pytest

from scripts.load import fixtures as fx


def _jwt(claims: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{body}.s"


def _session(org: str):
    return type("S", (), {"sid": "sess_1", "client_jwt": "cj", "token": lambda _: _jwt({"o": {"id": org}})})()


def test_names_are_fixture_shaped_and_never_open_like_a_name():
    assert fx.user_email(7) == "load-0007+clerk_test@example.com"
    assert fx.EMAIL_RE.match(fx.user_email(1234))
    assert fx.first_name(7) == "Ldu0007"
    assert fx.org_name(0) == "load-org-001" and fx.ORG_RE.match(fx.org_name(41))


def test_plan_numbers_users_globally_with_the_creator_first():
    layout = fx.plan(2, 3)
    assert [o["name"] for o in layout] == ["load-org-001", "load-org-002"]
    assert layout[1]["emails"] == [fx.user_email(n) for n in (4, 5, 6)]
    with pytest.raises(ValueError):
        fx.plan(0, 3)


READY = {"runtime_phase": "ready", "chat_available": True, "teams_state": "ready"}
GENERAL = [{"name": "general", "roomId": "r"}]
ROSTER = [{"kind": "agent"}] * 6 + [{"kind": "human"}]


@pytest.mark.parametrize(
    ("status", "rooms", "members", "ready"),
    [
        (READY, GENERAL, ROSTER, True),
        ({**READY, "runtime_phase": "provisioning"}, GENERAL, ROSTER, False),
        ({**READY, "chat_available": False}, GENERAL, ROSTER, False),
        ({**READY, "teams_state": "failed"}, GENERAL, ROSTER, False),
        (READY, [], None, False),
        (READY, GENERAL, ROSTER[1:], False),  # five agents: the seed has not finished
    ],
)
def test_ready_needs_runtime_and_the_seeded_general(status, rooms, members, ready):
    assert fx.is_ready(status, rooms, members) is ready


def test_jwt_org_reads_v2_and_v1_claims():
    assert fx.jwt_org(_jwt({"o": {"id": "org_a"}})) == "org_a"
    assert fx.jwt_org(_jwt({"org_id": "org_b"})) == "org_b"
    assert fx.jwt_org(_jwt({"sub": "user_x"})) is None
    assert fx.jwt_org("not-a-jwt") is None


def test_teardown_removes_members_before_the_creator_wipes_the_workspace():
    org = {"id": "org_1", "name": "load-org-001", "createdBy": "u1"}
    members = [{"userId": f"u{n}", "email": fx.user_email(n)} for n in (1, 2, 3)]
    assert fx.teardown_steps(org, members) == [
        ("remove_member", "u2"),
        ("remove_member", "u3"),
        ("delete_user", "u2"),
        ("delete_user", "u3"),
        ("wait_sole_member", "u1"),
        ("self_delete", "u1"),
        ("delete_leftovers", "org_1"),
    ]


def test_teardown_refuses_an_org_with_a_real_member_or_a_foreign_name():
    org = {"id": "org_1", "name": "load-org-001", "createdBy": "u1"}
    creator = {"userId": "u1", "email": fx.user_email(1)}
    with pytest.raises(SystemExit):
        fx.teardown_steps(org, [creator, {"userId": "u9", "email": "p@acme.com"}])
    with pytest.raises(SystemExit):
        fx.teardown_steps({**org, "name": "Jev Listening Test"}, [creator])


def test_self_delete_retries_409_then_gives_up_on_anything_else(monkeypatch):
    monkeypatch.setattr(fx.time, "sleep", lambda s: None)
    codes = iter([409, 409, 200])
    calls = []

    class Http:
        def post(self, path, json, headers):
            calls.append(json)
            return type("R", (), {"status_code": next(codes), "text": ""})()

    api = type("A", (), {"http": Http(), "s": _session("org_1")})()
    fx._self_delete(api, "load-0001+clerk_test@example.com")
    assert calls == [{"confirm_email": "load-0001+clerk_test@example.com"}] * 3
    codes = iter([400])
    with pytest.raises(RuntimeError, match="400"):
        fx._self_delete(api, "x")


def test_fixture_file_is_private_and_removable(tmp_path):
    path = tmp_path / "fixtures.json"
    old = os.umask(0)
    try:
        fx.write_private(path, {"users": [{"sid": "s", "clientJwt": "j"}]})
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["users"][0]["sid"] == "s"
    fx.remove(path)
    fx.remove(path)  # already gone: fine
    assert not path.exists()


def _offline_run(monkeypatch, tmp_path, org_in_jwt: str):
    path = tmp_path / "fixtures.json"
    monkeypatch.setattr(fx, "FIXTURES", path)
    monkeypatch.setattr(fx, "dev_only", lambda url: None)
    monkeypatch.setattr(fx.signal, "signal", lambda *a: None)
    monkeypatch.setattr(fx, "Clerk", lambda: type("C", (), {"user": lambda self, e: {"id": "user_" + e[5:9]}})())
    monkeypatch.setattr(fx, "_find_org", lambda clerk, name: {"id": "org_1", "created_by": "user_0001"})
    monkeypatch.setattr(fx, "_api", lambda clerk, uid, org: type("A", (), {"s": _session(org_in_jwt)})())
    return path


def test_run_deletes_the_file_when_the_command_fails(tmp_path, monkeypatch):
    path = _offline_run(monkeypatch, tmp_path, "org_1")
    seen = {}

    def boom(cmd, env):
        seen["mode"] = stat.S_IMODE(path.stat().st_mode)
        seen["env"] = env["FIXTURES"]
        seen["users"] = [u["userId"] for u in json.loads(path.read_text())["users"]]
        raise OSError("k6 not found")

    monkeypatch.setattr(fx.subprocess, "Popen", boom)
    with pytest.raises(OSError):
        fx.main(["run", "--orgs", "1", "--users-per-org", "2", "--", "k6", "run", "x.js"])
    assert seen == {"mode": 0o600, "env": str(path.resolve()), "users": ["user_0001", "user_0002"]}
    assert not path.exists()


def test_run_refuses_a_jwt_for_another_org(tmp_path, monkeypatch):
    path = _offline_run(monkeypatch, tmp_path, "org_JEV")
    with pytest.raises(SystemExit, match="org_JEV"):
        fx.main(["run", "--", "true"])
    assert not path.exists()


def test_dry_runs_call_nothing(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("network")

    monkeypatch.setattr(fx, "Clerk", boom)
    monkeypatch.setattr(fx, "dev_only", boom)
    assert fx.main(["setup", "--dry-run", "--orgs", "2", "--users-per-org", "3"]) == 0
    assert fx.main(["run", "--dry-run", "--", "k6"]) == 0
    assert fx.main(["teardown", "--dry-run"]) == 0
    assert "load-org-002" in capsys.readouterr().out
