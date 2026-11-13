"""Load-test fixtures: dev-only Clerk users + workspaces (see scripts/load/README.md).

    cd apps/backend
    uv run python -m scripts.load.fixtures setup    --orgs 1 --users-per-org 20 [--dry-run]
    uv run python -m scripts.load.fixtures run      --orgs 1 --users-per-org 20 -- k6 run scripts/load/k6/messaging.js
    uv run python -m scripts.load.fixtures teardown [--dry-run]

``setup`` creates the users and orgs and waits until every org's workspace is ready (runtime ready,
``#general`` seeded with the six starter agents). ``run`` creates one Clerk session per fixture
user server-side (Backend API ``POST /v1/sessions``, development instances only: no frontend-API
sign-in, so no per-IP sign-in limit), mints one API JWT and one ``lightbulb-appsync`` JWT per user
that live ``--ttl`` seconds, writes ``fixtures.json`` (mode 0600: it holds those live JWTs), runs the
command, and deletes the file in a ``finally`` and on SIGINT/SIGTERM. ``teardown`` removes every ``load-*`` fixture in the
spec's order. Nothing here runs against anything but dev (``dev_only`` + a development Clerk instance).
Never run it without the owner's go for that run.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import signal
import subprocess
import sys
import time

from pathlib import Path

from scripts.multiparty_e2e.runner import API, Api, Clerk, dev_only

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures.json"
STARTER_AGENTS = 6
#: Clerk documents 100 users on a development instance; ``CLERK_USER_CAP`` overrides once verified.
USER_CAP = int(os.environ.get("CLERK_USER_CAP", "100"))
EMAIL_RE = re.compile(r"^load-\d{4}\+clerk_test@example\.com$")
ORG_RE = re.compile(r"^load-org-\d{3}$")
DELETE_RETRY_S = 120
APPSYNC_TEMPLATE = "lightbulb-appsync"
#: Clerk's documented bounds for ``expires_in_seconds`` on session tokens.
TOKEN_TTL_MIN, TOKEN_TTL_MAX = 30, 315_360_000


# ------------------------------------------------------------------ pure helpers (unit-tested)
def user_email(n: int) -> str:
    return f"load-{n:04d}+clerk_test@example.com"


def first_name(n: int) -> str:
    # Never a word a message could open with: a name-addressed open triggers Jev (messaging.address()).
    return f"Ldu{n:04d}"


def org_name(k: int) -> str:
    return f"load-org-{k + 1:03d}"


def plan(orgs: int, per_org: int) -> list[dict]:
    """Org k holds users k*U+1 .. (k+1)*U; the first one creates (and so admins) it."""
    if orgs < 1 or per_org < 1:
        raise ValueError("need at least one org and one user per org")
    return [
        {"name": org_name(k), "emails": [user_email(k * per_org + i + 1) for i in range(per_org)]} for k in range(orgs)
    ]


def is_ready(status: dict, rooms: list[dict], general_members: list[dict] | None) -> bool:
    """The workspace can take load: runtime ready, Teams seed not failed, #general holds the roster."""
    if status.get("runtime_phase") != "ready" or not status.get("chat_available"):
        return False
    if status.get("teams_state") == "failed":
        return False
    if not any((r.get("name") or "").lower() == "general" for r in rooms):
        return False
    return sum(m.get("kind") == "agent" for m in general_members or []) >= STARTER_AGENTS


def jwt_org(token: str) -> str | None:
    """The active org a Clerk session JWT carries (v2 ``o.id``, v1 ``org_id``). Unverified: a preflight only."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return None
    return (claims.get("o") or {}).get("id") or claims.get("org_id")


def teardown_steps(org: dict, members: list[dict]) -> list[tuple[str, str]]:
    """Ordered teardown for one fixture org. ``members``: [{userId, email}]. Refuses an org that holds
    anyone but load fixtures, so no real user is ever touched."""
    strangers = [m["email"] for m in members if not EMAIL_RE.match(m.get("email") or "")]
    if strangers or not ORG_RE.match(org["name"]):
        raise SystemExit(f"refusing {org['name']}: not a pure load fixture ({len(strangers)} other members)")
    creator = org["createdBy"]
    others = [m["userId"] for m in members if m["userId"] != creator]
    steps = [("remove_member", u) for u in others] + [("delete_user", u) for u in others]
    return steps + [("wait_sole_member", creator), ("self_delete", creator), ("delete_leftovers", org["id"])]


def write_private(path: Path, data: dict) -> None:
    """0600 from the first byte: the file holds live client JWTs."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(data, f)


def fixture_file(orgs: list[dict], users: list[dict], expires_at: int) -> dict:
    """What k6 reads. No Clerk secret, no session handle: only JWTs that expire."""
    keys = ("userId", "email", "orgId", "apiJwt", "appsyncJwt")
    return {"api": API, "expiresAt": expires_at, "orgs": orgs, "users": [{k: u[k] for k in keys} for u in users]}


def remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------ Clerk BAPI (dev)
def _find_org(clerk: Clerk, name: str) -> dict | None:
    found = clerk.http.get("/organizations", params={"query": name, "limit": 100}).raise_for_status().json()
    return next((o for o in found.get("data", []) if o["name"] == name), None)


def _members(clerk: Clerk, org_id: str) -> list[dict]:
    rows = clerk.http.get(f"/organizations/{org_id}/memberships", params={"limit": 500}).raise_for_status().json()
    return [
        {"userId": m["public_user_data"]["user_id"], "email": m["public_user_data"].get("identifier")}
        for m in rows.get("data", [])
    ]


def _ensure_user(clerk: Clerk, n: int) -> str:
    email = user_email(n)
    user = clerk.user(email) or _bapi(
        clerk,
        "/users",
        {"email_address": [email], "first_name": first_name(n), "last_name": "Load", "skip_password_requirement": True},
    )
    return user["id"]


#: Clerk's code for a membership that already exists: the only error a re-run may treat as done.
ALREADY_MEMBER = "already_a_member_in_organization"


def _add_member(clerk: Clerk, org_id: str, user_id: str) -> None:
    resp = clerk.http.post(f"/organizations/{org_id}/memberships", json={"user_id": user_id, "role": "org:member"})
    if resp.status_code < 400:
        return
    try:
        codes = {e.get("code") for e in resp.json().get("errors", [])}
    except ValueError:
        codes = set()
    if resp.status_code in (400, 422) and codes == {ALREADY_MEMBER}:
        return
    # Anything else (organization_membership_quota_exceeded above all) stops setup with Clerk's words.
    raise SystemExit(f"adding {user_id} to {org_id} failed: HTTP {resp.status_code} {resp.text[:500]}")


def _bapi(clerk: Clerk, path: str, body: dict) -> dict:
    """A Backend API POST whose only acceptable answer is success: anything else stops the run with Clerk's
    words. Pacing and 429 retries live in the client (runner.PacedClient), so every verb gets them."""
    resp = clerk.http.post(path, json=body)
    if resp.status_code >= 400:
        raise SystemExit(f"Clerk POST {path.split('/')[1]} failed: HTTP {resp.status_code} {resp.text[:300]}")
    return resp.json()


class BapiSession:
    """A Clerk session created by the Backend API with the fixture org active. Clerk documents
    ``POST /v1/sessions`` as for testing only, unavailable on production instances: we are dev only.
    Its JWTs are minted once, with the lifetime the run needs, so nothing refreshes them."""

    def __init__(self, clerk: Clerk, user_id: str, org_id: str, ttl: int):
        if not TOKEN_TTL_MIN <= ttl <= TOKEN_TTL_MAX:
            raise SystemExit(f"--ttl must be {TOKEN_TTL_MIN}..{TOKEN_TTL_MAX} seconds")
        self.clerk, self.ttl = clerk, ttl
        self.sid = _bapi(clerk, "/sessions", {"user_id": user_id, "active_organization_id": org_id})["id"]
        self._jwt = ""

    def mint(self, template: str | None = None) -> str:
        path = f"/sessions/{self.sid}/tokens" + (f"/{template}" if template else "")
        return _bapi(self.clerk, path, {"expires_in_seconds": self.ttl})["jwt"]

    def token(self) -> str:
        if not self._jwt:
            self._jwt = self.mint()
        return self._jwt


def _api(clerk: Clerk, user_id: str, org_id: str, ttl: int = 3600) -> Api:
    return Api(BapiSession(clerk, user_id, org_id, ttl))


def _check_org_claim(api: Api, org_id: str) -> None:
    got = jwt_org(api.s.token())
    if got != org_id:
        raise SystemExit(f"refusing: a fixture JWT carries org {got!r}, expected {org_id}")


def setup(args) -> int:
    layout = plan(args.orgs, args.users_per_org)
    total = args.orgs * args.users_per_org
    if args.dry_run:
        print(f"[dry-run] would create {total} users in {args.orgs} orgs via {API} (dev Clerk), then wait for:")
        print("  GET /container/status runtime_phase=ready chat_available teams_state!=failed; #general + 6 agents")
        for org in layout:
            print(f"  {org['name']}: creator {org['emails'][0]}, members {len(org['emails']) - 1}")
        return 0
    dev_only(API)
    clerk = Clerk()
    count = clerk.http.get("/users/count").raise_for_status().json().get("total_count", 0)
    new = sum(clerk.user(email) is None for org in layout for email in org["emails"])  # a re-run reuses users
    if count + new > USER_CAP:
        raise SystemExit(f"refusing: {count} users + {new} new would pass the dev cap {USER_CAP}")
    orgs = []
    for k, org in enumerate(layout):
        ids = [_ensure_user(clerk, k * args.users_per_org + i + 1) for i in range(len(org["emails"]))]
        row = _find_org(clerk, org["name"]) or _bapi(
            clerk, "/organizations", {"name": org["name"], "created_by": ids[0]}
        )
        for uid in ids[1:]:  # one at a time: each fires the Paperclip JOIN + #general seat
            _add_member(clerk, row["id"], uid)
        orgs.append({"id": row["id"], "name": org["name"], "creator": ids[0]})
        print(f"{org['name']} = {row['id']} ({len(ids)} users)")
    ttl = min(TOKEN_TTL_MAX, args.ready_timeout + 600)  # one JWT outlives the whole readiness wait
    pending = {o["id"]: _api(clerk, o["creator"], o["id"], ttl) for o in orgs}
    for org_id, api in pending.items():
        _check_org_claim(api, org_id)
    deadline = time.monotonic() + args.ready_timeout
    while pending:
        for org_id, api in list(pending.items()):
            try:
                status = api("GET", "/container/status")
                rooms = api("GET", "/messaging/rooms")["rooms"]
                general = next((r for r in rooms if (r.get("name") or "").lower() == "general"), None)
                members = api("GET", f"/messaging/rooms/{general['roomId']}")["members"] if general else None
            except RuntimeError as exc:
                print(f"  {org_id}: {exc}", file=sys.stderr)
                continue
            if is_ready(status, rooms, members):
                print(f"  {org_id}: ready")
                del pending[org_id]
        if pending and time.monotonic() > deadline:
            raise SystemExit(f"not ready after {args.ready_timeout}s: {sorted(pending)}")
        if pending:
            time.sleep(10)
    return 0


def _fixture_orgs(clerk: Clerk) -> list[dict]:
    found = clerk.http.get("/organizations", params={"query": "load-org-", "limit": 100}).raise_for_status().json()
    return [
        {"id": o["id"], "name": o["name"], "createdBy": o.get("created_by")}
        for o in found.get("data", [])
        if ORG_RE.match(o["name"])
    ]


def run(args) -> int:
    if not args.command:
        raise SystemExit("run needs a command after --, e.g. -- k6 run scripts/load/k6/messaging.js")
    layout = plan(args.orgs, args.users_per_org)
    if args.dry_run:
        print(f"[dry-run] would create {args.orgs * args.users_per_org} Clerk sessions (Backend API, <= 5 calls/s),")
        print(f"  mint an API + {APPSYNC_TEMPLATE} JWT each living {args.ttl}s, write {FIXTURES} (0600),")
        print(f"  run {args.command} with FIXTURES set, then delete the file")
        return 0
    dev_only(API)
    clerk = Clerk()
    child: list[subprocess.Popen] = []

    def stop(signum, _frame):
        # The file goes at once (k6 reads it at start). Ctrl-C already reached the
        # child through the terminal; a second one here would make k6 skip its summary, so only
        # SIGTERM is passed on, and the child is then waited for.
        remove(FIXTURES)
        if not child:
            raise SystemExit(128 + signum)
        if signum == signal.SIGTERM:
            child[0].terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        # Each JWT's lifetime starts at its own mint, so the file's expiry counts from before the first one.
        started = int(time.time())
        orgs, users = [], []
        for org in layout:
            row = _find_org(clerk, org["name"])
            if not row:
                raise SystemExit(f"{org['name']} does not exist: run setup first")
            orgs.append({"orgId": row["id"], "name": org["name"], "creatorUserId": row.get("created_by")})
            for email in org["emails"]:
                user = clerk.user(email)
                if not user:
                    raise SystemExit(f"{email} does not exist: run setup first")
                session = BapiSession(clerk, user["id"], row["id"], args.ttl)
                api_jwt = session.token()
                if jwt_org(api_jwt) != row["id"]:
                    raise SystemExit(f"refusing: a fixture JWT carries org {jwt_org(api_jwt)!r}, expected {row['id']}")
                users.append(
                    {
                        "userId": user["id"],
                        "email": email,
                        "orgId": row["id"],
                        "apiJwt": api_jwt,
                        "appsyncJwt": session.mint(APPSYNC_TEMPLATE),
                    }
                )
        expires = started + args.ttl
        write_private(FIXTURES, fixture_file(orgs, users, expires))
        child.append(subprocess.Popen(args.command, env={**os.environ, "FIXTURES": str(FIXTURES.resolve())}))
        return child[0].wait()
    finally:
        remove(FIXTURES)


def _self_delete(api: Api, email: str) -> None:
    """The sole member's workspace wipe. 409 = a removed member's webhook has not landed yet: retry."""
    deadline = time.monotonic() + DELETE_RETRY_S
    delay = 2.0
    while True:
        resp = api.http.post(
            "/users/me/delete",
            json={"confirm_email": email},
            headers={"Authorization": f"Bearer {api.s.token()}"},
        )
        if resp.status_code < 400:
            return
        if resp.status_code != 409 or time.monotonic() + delay > deadline:
            raise RuntimeError(f"POST /users/me/delete -> {resp.status_code}: {resp.text[:300]}")
        time.sleep(delay)
        delay = min(delay * 2, 20)


def teardown(args) -> int:
    if args.dry_run:
        print("[dry-run] for each Clerk org named load-org-NNN whose members are all load-NNNN fixtures:")
        print("  1. remove every non-creator membership, delete those Clerk users")
        print("  2. wait until the org lists only its creator")
        print("  3. creator POST /users/me/delete {confirm_email} (409 -> retry up to 2 min)")
        print("  4. delete the org / creator if Clerk still has them; then any stray load-NNNN user")
        return 0
    dev_only(API)
    clerk = Clerk()
    remove(FIXTURES)
    for org in _fixture_orgs(clerk):
        members = _members(clerk, org["id"])
        emails = {m["userId"]: m["email"] for m in members}
        for step, target in teardown_steps(org, members):
            print(f"{org['name']}: {step} {target}")
            if step == "remove_member":
                clerk.http.delete(f"/organizations/{org['id']}/memberships/{target}").raise_for_status()
            elif step == "delete_user":
                clerk.http.delete(f"/users/{target}").raise_for_status()
            elif step == "wait_sole_member":
                deadline = time.monotonic() + DELETE_RETRY_S
                while [m["userId"] for m in _members(clerk, org["id"])] != [target]:
                    if time.monotonic() > deadline:
                        raise SystemExit(f"{org['name']}: members other than the creator remain")
                    time.sleep(5)
            elif step == "self_delete":
                _self_delete(_api(clerk, target, org["id"]), emails[target])
            elif step == "delete_leftovers":
                if clerk.http.get(f"/organizations/{target}").status_code == 200:
                    clerk.http.delete(f"/organizations/{target}").raise_for_status()
                creator = org["createdBy"]
                if creator and clerk.http.get(f"/users/{creator}").status_code == 200:
                    clerk.http.delete(f"/users/{creator}").raise_for_status()
    # A user whose org never got created (a setup killed half way) is the last residue.
    strays = clerk.http.get("/users", params={"query": "load-", "limit": 500}).raise_for_status().json()
    for user in strays:
        emails = [e["email_address"] for e in user.get("email_addresses", [])]
        if emails and all(EMAIL_RE.match(e) for e in emails):
            print(f"stray user {emails[0]}: delete")
            clerk.http.delete(f"/users/{user['id']}").raise_for_status()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("setup", "run", "teardown"):
        p = sub.add_parser(name)
        p.add_argument("--dry-run", action="store_true")
        if name != "teardown":
            p.add_argument("--orgs", type=int, default=1)
            p.add_argument("--users-per-org", type=int, default=2)
        if name == "setup":
            p.add_argument("--ready-timeout", type=int, default=1800, help="seconds to wait for every workspace")
        if name == "run":
            p.add_argument("--ttl", type=int, default=7200, help="JWT lifetime, seconds: longer than the whole command")
            p.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    if getattr(args, "command", None) and args.command[0] == "--":
        args.command = args.command[1:]
    return {"setup": setup, "run": run, "teardown": teardown}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
