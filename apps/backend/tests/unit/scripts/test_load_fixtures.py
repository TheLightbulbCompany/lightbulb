"""Load-test fixtures (scripts/load/fixtures.py): the pure helpers and the file/teardown safety.
No network: Clerk and the API are never reached."""

import base64
import json
import os
import stat

import pytest
from types import SimpleNamespace

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


class FakeBapi:
    """Clerk's Backend API as fixtures.py uses it: sessions, tokens, users, orgs. Records every POST."""

    def __init__(self, org_in_jwt: str = "org_1", first: list[int] | None = None):
        self.posts, self.org_in_jwt, self.first = [], org_in_jwt, list(first or [])
        self.http = self

    def user(self, email):
        return {"id": "user_" + email[5:9]}

    def post(self, path, json):
        self.posts.append((path, json))
        status = self.first.pop(0) if self.first else 200
        if path == "/sessions":
            body = {"id": f"sess_{json['user_id']}"}
        elif path.endswith("/tokens"):
            body = {"jwt": _jwt({"o": {"id": self.org_in_jwt}, "sub": path.split("/")[2]})}
        else:
            body = {"jwt": "appsync." + path.split("/")[2]}
        return type(
            "R", (), {"status_code": status, "headers": {"retry-after": "3"}, "text": "x", "json": lambda r: body}
        )()


def test_a_bapi_session_is_created_with_the_fixture_org_and_mints_long_tokens(monkeypatch):
    monkeypatch.setattr(fx.time, "sleep", lambda s: None)
    clerk = FakeBapi()
    session = fx.BapiSession(clerk, "user_1", "org_1", 5400)
    assert fx.jwt_org(session.token()) == "org_1"
    assert session.token() == session.token()  # minted once, never refreshed
    assert session.mint(fx.APPSYNC_TEMPLATE) == "appsync.sess_user_1"
    assert clerk.posts == [
        ("/sessions", {"user_id": "user_1", "active_organization_id": "org_1"}),
        ("/sessions/sess_user_1/tokens", {"expires_in_seconds": 5400}),
        ("/sessions/sess_user_1/tokens/lightbulb-appsync", {"expires_in_seconds": 5400}),
    ]


def test_every_clerk_call_is_paced_and_waits_out_a_429(monkeypatch):
    """Pacing lives in the client, so user lookups (GET) and membership adds (POST) get it, not only the
    session path: a 16-user setup 429'd on both on 2026-10-08."""
    import httpx

    from scripts.multiparty_e2e import runner

    waits, now = [], [100.0]
    monkeypatch.setattr(runner.time, "sleep", lambda s: (waits.append(s), now.__setitem__(0, now[0] + s)))
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(runner.PacedClient, "_last_call", 0.0)
    answers = iter([429, 200, 429, 422])
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        status = next(answers)
        seen.append((request.method, request.url.path, status))
        body = [{"id": "user_9"}] if request.method == "GET" else {"errors": [{"code": fx.ALREADY_MEMBER}]}
        return httpx.Response(status, headers={"retry-after": "3"}, json=body if status != 429 else {})

    clerk = SimpleNamespace(
        http=runner.PacedClient(base_url="https://clerk.test/v1", transport=httpx.MockTransport(handle))
    )
    clerk.user = lambda email: runner.Clerk.user(clerk, email)
    assert clerk.user("load-0009@example.com") == {"id": "user_9"}  # the GET's 429 was waited out
    fx._add_member(clerk, "org_1", "user_9")  # the POST's 429 too; then already-a-member is done, not fatal
    assert [(m, p, st) for m, p, st in seen] == [
        ("GET", "/v1/users", 429),
        ("GET", "/v1/users", 200),
        ("POST", "/v1/organizations/org_1/memberships", 429),
        ("POST", "/v1/organizations/org_1/memberships", 422),
    ]
    assert waits.count(3.0) == 2  # Retry-After, once per 429
    assert any(w == pytest.approx(runner.BAPI_SPACING_S) for w in waits)  # back-to-back calls stay <= 5/s


def test_a_bapi_error_stops_the_run_with_clerks_status(monkeypatch):
    monkeypatch.setattr(fx.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit, match="HTTP 403"):
        fx.BapiSession(FakeBapi(first=[403]), "user_1", "org_1", 60)
    with pytest.raises(SystemExit, match="--ttl"):
        fx.BapiSession(FakeBapi(), "user_1", "org_1", 10)


def test_the_fixture_file_holds_jwts_and_nothing_else():
    users = [{"userId": "u", "email": "e", "orgId": "o", "apiJwt": "a", "appsyncJwt": "s", "secret": "sk_x"}]
    data = fx.fixture_file([{"orgId": "o"}], users, 1_800_000_000)
    assert data["api"] == fx.API and data["expiresAt"] == 1_800_000_000
    assert data["users"] == [{"userId": "u", "email": "e", "orgId": "o", "apiJwt": "a", "appsyncJwt": "s"}]
    assert "sk_" not in json.dumps(data)


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


def _clerk_answering(status: int, body: dict):
    class Resp:
        status_code = status
        text = json.dumps(body)

        def json(self):
            return body

    return type("C", (), {"http": type("H", (), {"post": lambda self, path, json: Resp()})()})()


def test_an_existing_membership_counts_as_added():
    fx._add_member(_clerk_answering(200, {}), "org_1", "u1")
    fx._add_member(_clerk_answering(400, {"errors": [{"code": fx.ALREADY_MEMBER}]}), "org_1", "u1")


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, "organization_membership_quota_exceeded"),
        (422, "organization_membership_quota_exceeded"),
        (400, "form_param_missing"),
        (500, None),
    ],
)
def test_any_other_membership_error_stops_setup_with_clerks_words(status, code):
    body = {"errors": [{"code": code, "message": "nope"}]} if code else {}
    with pytest.raises(SystemExit, match=f"HTTP {status}") as err:
        fx._add_member(_clerk_answering(status, body), "org_1", "u1")
    if code:
        assert code in str(err.value)


def test_fixture_file_is_private_and_removable(tmp_path):
    path = tmp_path / "fixtures.json"
    old = os.umask(0)
    try:
        fx.write_private(path, {"users": [{"apiJwt": "j"}]})
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["users"][0]["apiJwt"] == "j"
    fx.remove(path)
    fx.remove(path)  # already gone: fine
    assert not path.exists()


def _offline_run(monkeypatch, tmp_path, org_in_jwt: str):
    path = tmp_path / "fixtures.json"
    monkeypatch.setattr(fx, "FIXTURES", path)
    monkeypatch.setattr(fx, "dev_only", lambda url: None)
    monkeypatch.setattr(fx.signal, "signal", lambda *a: None)
    monkeypatch.setattr(fx.time, "sleep", lambda s: None)
    monkeypatch.setattr(fx, "Clerk", lambda: FakeBapi(org_in_jwt))
    monkeypatch.setattr(fx, "_find_org", lambda clerk, name: {"id": "org_1", "created_by": "user_0001"})
    return path


def test_run_deletes_the_file_when_the_command_fails(tmp_path, monkeypatch):
    path = _offline_run(monkeypatch, tmp_path, "org_1")
    seen = {}

    def boom(cmd, env):
        seen["mode"] = stat.S_IMODE(path.stat().st_mode)
        seen["env"] = env["FIXTURES"]
        data = json.loads(path.read_text())
        seen["users"] = [u["userId"] for u in data["users"]]
        seen["keys"] = sorted(data["users"][0])
        seen["expires_at"] = data["expiresAt"]
        raise OSError("k6 not found")

    monkeypatch.setattr(fx.subprocess, "Popen", boom)
    before = int(fx.time.time())
    with pytest.raises(OSError):
        fx.main(["run", "--orgs", "1", "--users-per-org", "2", "--ttl", "5400", "--", "k6", "run", "x.js"])
    # The expiry counts from before the first mint, never from after the last one.
    assert before + 5400 <= seen.pop("expires_at") <= before + 5402
    assert seen == {
        "mode": 0o600,
        "env": str(path.resolve()),
        "users": ["user_0001", "user_0002"],
        "keys": ["apiJwt", "appsyncJwt", "email", "orgId", "userId"],
    }
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
