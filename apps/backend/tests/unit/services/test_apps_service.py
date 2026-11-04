import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import asyncio  # noqa: E402
import copy  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import tarfile  # noqa: E402
import threading  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

import boto3  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

from core.config import settings  # noqa: E402
from core.services import apps_service as svc  # noqa: E402
from core.services import vercel_client  # noqa: E402

OWNER = "org_1"
PERSON = svc.BuilderCaller(owner_id=OWNER, actor_id="user_1", actor_kind="person")
VERCEL = {"token": "vtok_SECRET", "org_id": "team_1", "project_id": "prj_1", "project_url": "https://x"}
LIVE_DB = "postgres://live:LIVEPW@db/live"
DEV_DB = "postgres://dev:DEVPW@db/dev"


def _tar(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


SRC_V1 = {
    "app/page.tsx": "export default () => 1",
    "drizzle/0000_init.sql": "CREATE TABLE shifts (id int);",
    "drizzle/seed.sql": "INSERT INTO shifts VALUES (1);",
}
SRC_V2 = {**SRC_V1, "drizzle/0001_add.sql": "ALTER TABLE shifts ADD COLUMN who text;", "app/page.tsx": "v2"}


# What production returned for an expired token.
DEAD_403 = {"error": {"code": "forbidden", "message": "Not authorized", "invalidToken": True}}


class Vercel:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.deployments: dict[str, dict] = {}
        self.verified = False
        self.alias_fails = False
        self.promote_fails = False
        self.refused = False  # Vercel no longer accepts the token: 403 on everything
        self.first_is_production = False  # Vercel's docs: a project's first deployment, whatever was asked
        self.domains: set[str] = set()  # project domains
        self.aliases: dict[str, str] = {}  # host -> deployment it serves

    def paths(self, method=None):
        return [(r.method, r.url.path) for r in self.calls if method in (None, r.method)]

    def env_posts(self):
        return [json.loads(r.content) for r in self.calls if r.url.path.endswith("/env") and r.method == "POST"]

    def created(self):
        return [json.loads(r.content) for r in self.calls if r.url.path == "/v13/deployments"]

    def finish(self, dep_id, state="READY", error=None):
        self.deployments[dep_id].update(readyState=state, errorMessage=error)
        if state == "READY" and self.deployments[dep_id].get("target") == "production":
            self.serve_production(dep_id)  # happens on Vercel's side, before anyone reads

    def serve_production(self, dep_id):
        """What Vercel does for a production deployment: every project domain moves to it."""
        self.aliases.update(dict.fromkeys(self.domains, dep_id))

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        p = req.url.path
        if self.refused:
            return httpx.Response(403, json=DEAD_403)
        if p == "/v13/deployments":
            dep_id = f"dpl_{len(self.deployments) + 1}"
            target = json.loads(req.content).get("target")
            if self.first_is_production and not self.deployments:
                target = "production"
            self.deployments[dep_id] = {"readyState": "QUEUED", "url": f"{dep_id}.vercel.app", "target": target}
            return httpx.Response(200, json={"id": dep_id, "url": f"{dep_id}.vercel.app", "readyState": "QUEUED"})
        if p.startswith("/v13/deployments/"):
            return httpx.Response(200, json=self.deployments[p.rsplit("/", 1)[1]])
        if p.endswith("/domains") and req.method == "POST":
            self.domains.add(json.loads(req.content)["name"])
            return httpx.Response(200, json={"verified": False, "verification": [{"type": "TXT"}]})
        if p.startswith("/v4/aliases/"):
            dep_id = self.aliases.get(p.rsplit("/", 1)[1])
            if not dep_id:
                return httpx.Response(404, json={"error": {"code": "not_found", "message": "n"}})
            return httpx.Response(200, json={"deploymentId": dep_id})
        if p.startswith("/v6/domains/"):
            return httpx.Response(200, json={"recommendedCNAME": [{"value": "c.vercel-dns.com.", "rank": 1}]})
        if p.endswith("/verify"):
            return httpx.Response(200, json={"verified": self.verified})
        if "/promote/" in p:
            if not self.promote_fails:
                self.serve_production(p.rsplit("/", 1)[1])
            return httpx.Response(500 if self.promote_fails else 201, json={})
        if "/rollback/" in p:
            self.serve_production(p.rsplit("/", 1)[1])
            return httpx.Response(201, json={})
        if p.endswith("/aliases"):
            if self.alias_fails:
                return httpx.Response(400, json={})
            dep_id, host = p.split("/")[3], json.loads(req.content)["alias"]
            if self.aliases.get(host) == dep_id:  # Vercel's documented answer for a repeat
                return httpx.Response(409, json={"error": {"code": "alias_in_use", "message": "same deployment"}})
            self.aliases[host] = dep_id
            return httpx.Response(200, json={})
        if p.endswith("/files"):
            return httpx.Response(200, json={})
        if p.endswith("/events"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})


@pytest.fixture(autouse=True)
def published(monkeypatch):
    from core.services import live_events

    m = AsyncMock()
    monkeypatch.setattr(live_events, "publish_owner_users", m)
    return m


@pytest.fixture(autouse=True)
def watched(monkeypatch):
    """The real watch sleeps a minute in the background; tests that want it call ``svc._look`` or ``_watch``."""
    calls: list[str] = []
    monkeypatch.setattr(svc, "watch", lambda o, a: calls.append(a))
    return calls


@pytest.fixture
def world(monkeypatch):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="test-apps",
            KeySchema=[{"AttributeName": "app_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("app_id", "owner_id", "name")],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "owner-index",
                    "KeySchema": [
                        {"AttributeName": "owner_id", "KeyType": "HASH"},
                        {"AttributeName": "name", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="test-state")
        # Repos run each DynamoDB call in a thread; moto is not thread-safe the way DynamoDB is atomic per
        # request, so concurrent readers once saw "dictionary changed size during iteration". One call at a time.
        to_thread, one_call = asyncio.to_thread, threading.Lock()

        def _serial(f, *a, **kw):
            def locked():
                with one_call:
                    return f(*a, **kw)

            return to_thread(locked)

        monkeypatch.setattr(asyncio, "to_thread", _serial)
        for k, v in {
            "OWNER_STATE_BUCKET": "test-state",
            "APPS_DOMAIN": "apps.example.com",
            "NETWORK_PROOF_KEY": "proof-key",
            "FRONTEND_URL": "https://web.test",
        }.items():
            monkeypatch.setattr(settings, k, v)

        secrets: dict = {}
        sec = svc.integration_secret_store
        monkeypatch.setattr(sec, "get", AsyncMock(side_effect=lambda o, p: secrets.get((o, p))))
        monkeypatch.setattr(sec, "put", AsyncMock(side_effect=lambda o, p, c: secrets.__setitem__((o, p), dict(c))))
        monkeypatch.setattr(sec, "delete", AsyncMock(side_effect=lambda o, p: secrets.pop((o, p), None) is not None))

        rows: dict = {}

        async def start(**kw):
            for r in rows.values():
                if (r["provider_name"].lower(), r["service_ref"], r["environment"], r["app_id"]) == (
                    kw["provider_name"],
                    kw["service_id"],
                    kw["environment"],
                    kw["app_id"],
                ) and r["state"] not in ("active", "failed", "rejected", "cancelled"):
                    return {**r, "reused": True}
            wid = f"pwf_{len(rows) + 1}"
            rows[wid] = {
                "workflow_id": wid,
                "owner_id": kw["owner_id"],
                "app_id": kw["app_id"],
                "environment": kw["environment"],
                "provider_name": {"vercel": "Vercel", "neon": "Neon"}[kw["provider_name"]],
                "service_ref": kw["service_id"],
                "configuration": kw["configuration"],
                "state": "awaiting_approval",
                "created_at": f"2026-10-29T00:00:{len(rows):02d}",
                **({"bundled_with": kw["bundled_with"]} if kw.get("bundled_with") else {}),
            }
            return dict(rows[wid])

        wf = svc.provisioning_workflow
        monkeypatch.setattr(wf, "start", AsyncMock(side_effect=start))
        monkeypatch.setattr(wf, "advance_on_read", AsyncMock(side_effect=lambda wid: dict(rows[wid])))
        monkeypatch.setattr(wf, "account_id_for", AsyncMock(return_value="acct_1"))
        monkeypatch.setattr(wf, "cancel", AsyncMock())
        monkeypatch.setattr(wf, "consent_line", AsyncMock(return_value={"what": "data", "provider": "Neon"}))
        monkeypatch.setattr(
            svc.wf_repo, "list_for_owner", AsyncMock(side_effect=lambda o: [dict(r) for r in rows.values()])
        )

        async def wf_patch(wid, fields):
            rows[wid].update(fields)
            return rows[wid]

        monkeypatch.setattr(svc.wf_repo, "patch", AsyncMock(side_effect=wf_patch))
        monkeypatch.setattr(svc.api, "remove_resource", AsyncMock(return_value={}))
        monkeypatch.setattr(svc.api, "rotate_credentials", AsyncMock(return_value={"status": "errored"}))
        monkeypatch.setattr(svc.api, "reveal_access_configuration", AsyncMock())
        monkeypatch.setattr(svc, "_SETTLE_S", 0)
        monkeypatch.setattr(svc.clerk_admin, "get_organization", AsyncMock(return_value={"name": " Acme  Co "}))

        rooms = {
            "room_1": {"roomId": "room_1", "ownerId": OWNER},
            "r": {"roomId": "r", "ownerId": OWNER},
            "r_other": {"ownerId": "org_other"},
        }
        monkeypatch.setattr(svc.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: rooms.get(r)))

        vercel = Vercel()
        monkeypatch.setattr(vercel_client, "_transport", httpx.MockTransport(vercel.handler))
        reset = AsyncMock()
        monkeypatch.setattr(svc.apps_db, "reset_preview", reset)
        apply = AsyncMock()
        monkeypatch.setattr(svc.apps_db, "apply_live", apply)
        point, remove = AsyncMock(), AsyncMock()
        monkeypatch.setattr(svc.apps_dns, "point", point)
        monkeypatch.setattr(svc.apps_dns, "remove", remove)

        def activate(provider, env, app_id):
            for r in rows.values():
                if r["provider_name"].lower() == provider and r["environment"] == env and r["app_id"] == app_id:
                    r.update(state="active", resource_id=f"res_{r['workflow_id']}")
            blob = VERCEL if provider == "vercel" else {"connection_string": LIVE_DB if env == "prod" else DEV_DB}
            secrets[(OWNER, f"apps/{app_id}/{provider}/{env}")] = dict(blob)

        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", ddb):
            yield SimpleNamespace(
                rows=rows,
                secrets=secrets,
                vercel=vercel,
                reset=reset,
                apply=apply,
                point=point,
                remove=remove,
                activate=activate,
                start=wf.start,
            )


async def _source(files=SRC_V1, commit="c1"):
    return await svc.put_source(OWNER, name="crew", room_id="room_1", commit=commit, archive=_tar(files), actor=PERSON)


async def _up(src, **kw):
    return await svc.up(
        OWNER,
        name="crew",
        room_id="room_1",
        services=["database"],
        open_to="room",
        budget_usd=20,
        source_id=src["source_id"],
        actor=PERSON,
        origin={"kind": "idea", "ref": "i1"},
        **kw,
    )


async def _all_active(world, app_id):
    for p, e in (("vercel", "prod"), ("neon", "prod"), ("neon", "dev")):
        world.activate(p, e, app_id)


async def _ready_preview(world, files=SRC_V1, commit="c1"):
    src = await _source(files, commit)
    await _up(src)
    await _all_active(world, src["app_id"])
    await svc.status(OWNER, src["app_id"])  # deploys
    dep = world.vercel.created()[-1]
    dep_id = f"dpl_{len(world.vercel.created())}"
    world.vercel.finish(dep_id)
    await svc.status(OWNER, src["app_id"])  # sees READY
    return src, dep, dep_id


# --- brief list ---------------------------------------------------------------------------------


async def test_up_files_one_workflow_per_service_and_returns_cards(world):
    src = await _source()
    res = await _up(src, issue_id="iss_1")
    assert res["state"] == "awaiting_approval"
    filed = [
        (c.kwargs["provider_name"], c.kwargs["service_id"], c.kwargs["environment"])
        for c in world.start.await_args_list
    ]
    assert filed == [("vercel", "project", "prod"), ("neon", "postgres", "prod"), ("neon", "postgres", "dev")]
    assert all(
        c.kwargs["app_id"] == src["app_id"] and c.kwargs["issue_id"] == "iss_1" for c in world.start.await_args_list
    )
    assert world.start.await_args_list[0].kwargs["configuration"] == {"name": "crew"}
    assert [(c["provider"], c["service"]) for c in res["cards"]] == [("Vercel", "project")]
    card, *data = world.start.await_args_list
    assert card.kwargs["also"] and "bundled_with" not in card.kwargs
    assert [c.kwargs["bundled_with"] for c in data] == ["pwf_1", "pwf_1"]
    assert res["canvas_url"] == f"https://web.test/chat?panel=canvas&app={src['app_id']}"
    app = await svc.apps_repo.get(src["app_id"])
    assert (
        app["budget_usd"] == 20 and app["origin"] == {"kind": "idea", "ref": "i1"} and app["services"] == ["database"]
    )


async def test_up_is_idempotent_while_cards_pending(world):
    src = await _source()
    await _up(src)
    again = await _up(src)
    assert world.start.await_count == 3
    assert len(world.rows) == 3 and again["state"] == "awaiting_approval"
    assert world.vercel.calls == []


async def test_no_request_waits_on_vercel(world, monkeypatch):
    sleep = AsyncMock(side_effect=AssertionError("no sleeping"))
    monkeypatch.setattr("asyncio.sleep", sleep)
    src = await _source()
    await _up(src)
    await _all_active(world, src["app_id"])
    res = await svc.status(OWNER, src["app_id"])
    assert res["state"] == "building"
    gets = len(world.vercel.paths("GET"))
    res = await svc.status(OWNER, src["app_id"])
    # one deployment_state per read, nothing else waiting
    assert len(world.vercel.paths("GET")) - gets == 1 and res["state"] == "building"
    world.vercel.finish("dpl_1")
    assert (await svc.status(OWNER, src["app_id"]))["state"] == "ready"
    pub = await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    assert pub["state"] == "building" and "version" not in pub
    sleep.assert_not_awaited()


async def test_workspace_name_unavailable_skips_the_read_instead_of_pushing_a_changing_env(world, monkeypatch):
    src = await _source()
    await _up(src)
    await _all_active(world, src["app_id"])
    monkeypatch.setattr(svc.clerk_admin, "get_organization", AsyncMock(return_value=None))
    await _up(src)
    assert world.vercel.env_posts() == [] and world.vercel.created() == []
    monkeypatch.setattr(svc.clerk_admin, "get_organization", AsyncMock(return_value={"name": "Acme"}))
    await _up(src)
    assert {e["key"]: e["value"] for e in world.vercel.env_posts()[0]}["LIGHTBULB_WORKSPACE_NAME"] == "Acme"


async def test_up_deploys_preview_when_all_active(world):
    src = await _source()
    await _up(src)
    await _all_active(world, src["app_id"])
    res = await _up(src)
    assert res["state"] == "building"
    envs = world.vercel.env_posts()
    assert [e[0]["target"] for e in envs] == [["preview"], ["production"]]
    preview = {e["key"]: e["value"] for e in envs[0]}
    assert preview["DATABASE_URL"] == DEV_DB and preview["LIGHTBULB_SAMPLE_DATA"] == "0"
    assert preview["LIGHTBULB_APP"] == "crew" and preview["LIGHTBULB_OPEN_TO"] == "room"
    assert preview["LIGHTBULB_APP_ID"] == src["app_id"] and preview["LIGHTBULB_APP_NAME"] == "Crew"
    assert preview["LIGHTBULB_WORKSPACE_NAME"] == "Acme Co"
    assert preview["NEXT_PUBLIC_LIGHTBULB_API"] == svc.settings.PUBLIC_API_BASE.rstrip("/")
    assert "BLOB_READ_WRITE_TOKEN" not in preview
    key = json.loads(preview["LIGHTBULB_ISSUER_KEY"])
    assert key["kty"] == "OKP" and key["crv"] == "Ed25519" and key["kid"] and key["x"]
    assert {e["key"]: e["value"] for e in envs[1]}["DATABASE_URL"] == LIVE_DB
    # The preview-only pass: its claim is named to the preview's door and never to live's.
    assert preview["LIGHTBULB_PREVIEW_APP"] == "crew:preview"
    assert "LIGHTBULB_PREVIEW_APP" not in {e["key"] for e in envs[1]}
    dep = world.vercel.created()[0]
    assert "target" not in dep and dep["projectSettings"]["buildCommand"] == "next build"
    assert {f["file"] for f in dep["files"]} == set(SRC_V1)
    assert ("PATCH", "/v9/projects/prj_1") in world.vercel.paths()  # protection off
    world.vercel.finish("dpl_1")
    res = await svc.status(OWNER, src["app_id"])
    assert res["state"] == "ready"
    assert res["preview"] == {
        "deployment_id": "dpl_1", "commit": "c1", "ready_at": res["preview"]["ready_at"], "url": "https://dpl_1.vercel.app",
    }  # fmt: skip
    assert dep["env"] == {"LIGHTBULB_COMMIT": "c1"} and dep["build"] == {"env": {"LIGHTBULB_COMMIT": "c1"}}  # R76
    assert (await svc.apps_repo.get(src["app_id"]))["preview_url"] == "https://dpl_1.vercel.app"
    # a third read pushes no env again (fingerprint) and deploys nothing
    n_env, n_dep = len(world.vercel.env_posts()), len(world.vercel.created())
    with patch.object(svc.apps_source, "get", AsyncMock(side_effect=AssertionError("no archive read on a quiet read"))):
        await svc.status(OWNER, src["app_id"])
    assert (len(world.vercel.env_posts()), len(world.vercel.created())) == (n_env, n_dep)


async def test_failed_build_leaves_versions_unchanged(world):
    src, _, _ = await _ready_preview(world)
    await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    before = (await svc.apps_repo.get(src["app_id"]))["versions"]
    world.vercel.finish(f"dpl_{len(world.vercel.created())}", "ERROR", "Type error in page.tsx")
    res = await svc.status(OWNER, src["app_id"])
    assert res["state"] == "failed"
    up = await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    assert (await svc.apps_repo.get(src["app_id"]))["versions"] == before == []
    assert up["state"] == "building"  # publish again starts a fresh build


async def test_failed_preview_build_reports_vercel_error(world):
    src = await _source()
    await _up(src)
    await _all_active(world, src["app_id"])
    await svc.status(OWNER, src["app_id"])
    world.vercel.finish("dpl_1", "ERROR", "Module not found")
    res = await _up(src)
    assert res["error"]["code"] == "build_failed" and res["error"]["message"] == "Module not found"
    assert res["state"] == "failed"


async def test_up_deploys_sample_data_preview_before_the_database_is_active(world):
    src = await _source()
    await _up(src)
    world.activate("vercel", "prod", src["app_id"])
    res = await _up(src)
    assert res["state"] == "building" and res["sample_data"] is True
    env = {e["key"]: e["value"] for e in world.vercel.env_posts()[0]}
    assert env["LIGHTBULB_SAMPLE_DATA"] == "1" and "DATABASE_URL" not in env
    assert len(world.vercel.created()) == 1
    world.reset.assert_not_awaited()


async def test_up_switches_preview_to_its_database_when_it_becomes_active(world):
    src = await _source()
    await _up(src)
    world.activate("vercel", "prod", src["app_id"])
    await _up(src)
    world.vercel.finish("dpl_1")
    await svc.status(OWNER, src["app_id"])
    world.activate("neon", "dev", src["app_id"])
    res = await _up(src)
    world.reset.assert_awaited_once()
    args = world.reset.await_args
    assert args.args[0] == DEV_DB
    assert args.kwargs["migrations"] == [SRC_V1["drizzle/0000_init.sql"]]
    assert args.kwargs["seed_sql"] == SRC_V1["drizzle/seed.sql"]
    env = {e["key"]: e["value"] for e in world.vercel.env_posts()[-1]}
    assert env["DATABASE_URL"] == DEV_DB and env["LIGHTBULB_SAMPLE_DATA"] == "0"
    assert len(world.vercel.created()) == 2 and res["sample_data"] is False


async def test_preview_reseeds_after_a_schema_change(world):
    src, _, _ = await _ready_preview(world)
    assert world.reset.await_count == 1
    await svc.status(OWNER, src["app_id"])
    assert world.reset.await_count == 1  # same migrations: no reset
    src2 = await _source(SRC_V2, "c2")
    res = await _up(src2)
    assert world.reset.await_count == 2 and res["state"] == "building"
    assert world.reset.await_args.kwargs["migrations"] == [
        SRC_V2["drizzle/0000_init.sql"],
        SRC_V2["drizzle/0001_add.sql"],
    ]
    # same schema, new code: deploy, no reset
    src3 = await _source({**SRC_V2, "app/page.tsx": "v3"}, "c3")
    await _up(src3)
    assert world.reset.await_count == 2 and len(world.vercel.created()) == 3


async def test_publish_waits_for_the_live_database_secret(world):
    """An active row whose secret has not landed: the production build would run its migrations
    without DATABASE_URL and fail. Refuse with a reason instead; publish once it is there."""
    src, _, _ = await _ready_preview(world)
    key = (OWNER, f"apps/{src['app_id']}/neon/prod")
    blob = world.secrets.pop(key)
    with pytest.raises(svc.AppsRefused) as e:
        await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    assert (e.value.code, e.value.next) == ("not_ready", "try again in a minute")
    assert not [d for d in world.vercel.created() if d.get("target") == "production"]
    world.secrets[key] = blob
    pub = await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    assert pub["state"] == "building"


@pytest.mark.parametrize(
    ("author", "text", "allowed"),
    [
        ("human", "Looks great, ship it", True),
        ("human", "please publish", True),
        ("human", "Approved hosting and data for Crew. Setting it up.", False),
        ("human", "does it use the live database?", False),
        ("agent", "I'll publish it now", False),
    ],
)
async def test_an_agent_publishes_only_when_a_person_asked(world, monkeypatch, author, text, allowed):
    src, _, _ = await _ready_preview(world)
    rec = await svc.apps_repo.get(src["app_id"])
    msg = {"author": {"kind": author}, "text": text, "createdAt": "2999-01-01T00:00:00Z"}
    monkeypatch.setattr(svc.messaging_repo, "list_messages_before", AsyncMock(return_value=[msg]))
    agent = svc.BuilderCaller(owner_id=OWNER, actor_id="agent_7", actor_kind="agent")
    assert rec.get("room_id")
    if allowed:
        pub = await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=agent)
        assert pub["state"] == "building"
    else:
        with pytest.raises(svc.AppsRefused) as e:
            await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=agent)
        assert e.value.code == "not_asked"


async def test_publish_deploys_and_adds_domain_once(world):
    src, _, _ = await _ready_preview(world)
    pub = await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    assert pub["state"] == "building"
    dep = world.vercel.created()[-1]
    assert dep["target"] == "production"
    assert dep["env"] == {"LIGHTBULB_COMMIT": "c1"} and dep["build"] == {"env": {"LIGHTBULB_COMMIT": "c1"}}  # R76
    assert dep["projectSettings"]["buildCommand"] == "next build"  # the backend ran the migrations already
    domains = [r for r in world.vercel.calls if r.url.path.endswith("/domains") and r.method == "POST"]
    hosts = [json.loads(r.content)["name"] for r in domains]
    assert "crew.apps.example.com" in hosts
    assert any(not c.kwargs.get("preview") for c in world.point.await_args_list)
    # idempotent: second publish while building files nothing new
    again = await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    assert again["state"] == "building" and len(world.vercel.created()) == 2
    world.vercel.finish("dpl_2")
    res = await svc.status(OWNER, src["app_id"])
    assert res["state"] == "live" and res["live_version"] == 1
    assert res["live_url"] == "https://crew.apps.example.com"
    v = res["versions"][0]
    assert (v["n"], v["commit"], v["deployment_id"], v["by"]) == (1, "c1", "dpl_2", "user_1")
    # after READY, the same publish is a no-op that reports ready + version
    done = await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    assert (done["state"], done["version"]) == ("ready", 1) and len(world.vercel.created()) == 2
    # a second release does not add the domain again
    src2 = await _source(SRC_V2, "c2")
    await svc.publish(OWNER, src["app_id"], source_id=src2["source_id"], actor=PERSON)
    world.vercel.finish("dpl_3")
    res = await svc.status(OWNER, src["app_id"])
    assert res["live_version"] == 2
    domains = [r for r in world.vercel.calls if r.url.path.endswith("/domains") and r.method == "POST"]
    assert [json.loads(r.content)["name"] for r in domains].count("crew.apps.example.com") == 1
    assert (await svc.apps_repo.get(src["app_id"]))["published_migrations"] == ["0000_init.sql", "0001_add.sql"]


async def test_rollback_is_code_only_and_says_so(world):
    src, _, _ = await _ready_preview(world)
    await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    world.vercel.finish("dpl_2")
    await svc.status(OWNER, src["app_id"])
    src2 = await _source(SRC_V2, "c2")
    await svc.publish(OWNER, src["app_id"], source_id=src2["source_id"], actor=PERSON)
    world.vercel.finish("dpl_3")
    await svc.status(OWNER, src["app_id"])
    res = await svc.rollback(OWNER, src["app_id"], version=None)
    assert ("POST", "/v1/projects/prj_1/rollback/dpl_2") in world.vercel.paths()
    assert res["version"] == 1
    assert res["error"]["code"] == "code_only" and res["error"]["message"] == "Version 1 is live again."
    assert "Data is unchanged" in res["error"]["next"]
    assert (await svc.apps_repo.get(src["app_id"]))["live_version"] == 1
    world.reset.assert_awaited_once()  # no database touched by rollback
    with pytest.raises(svc.AppsRefused) as e:
        await svc.rollback(OWNER, src["app_id"], version=9)
    assert e.value.code == "version_not_found"


async def test_no_credential_in_any_result_or_log(world, caplog):
    caplog.set_level(logging.DEBUG)
    src, _, _ = await _ready_preview(world)
    results = [await _up(src), await svc.status(OWNER, src["app_id"])]
    results.append(await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON))
    world.vercel.finish("dpl_2")
    results.append(await svc.status(OWNER, src["app_id"]))
    results.append(await svc.rollback(OWNER, src["app_id"], version=1))
    cookie = world.secrets[(OWNER, f"apps/{src['app_id']}/lightbulb")]["cookie_secret"]
    record = json.dumps(await svc.apps_repo.get(src["app_id"]), default=str)
    blob = json.dumps(results, default=str) + caplog.text + record
    for secret in (LIVE_DB, DEV_DB, "LIVEPW", "DEVPW", VERCEL["token"], cookie):
        assert secret not in blob


# --- beyond the brief ---------------------------------------------------------------------------


async def test_cookie_secret_is_generated_once_per_app(world):
    src = await _source()
    await _up(src)
    world.activate("vercel", "prod", src["app_id"])
    await _up(src)
    first = world.secrets[(OWNER, f"apps/{src['app_id']}/lightbulb")]["cookie_secret"]
    world.activate("neon", "dev", src["app_id"])
    await svc.status(OWNER, src["app_id"])
    assert world.secrets[(OWNER, f"apps/{src['app_id']}/lightbulb")]["cookie_secret"] == first
    assert len(first) == 43  # 32 bytes base64url, no padding
    values = {e["value"] for post in world.vercel.env_posts() for e in post if e["key"] == "LIGHTBULB_COOKIE_SECRET"}
    assert values == {first}


async def test_failed_or_parked_workflow_reports_open_inbox(world):
    src = await _source()
    await _up(src)
    world.rows["pwf_2"]["state"] = "requires_review"
    res = await svc.status(OWNER, src["app_id"])
    assert res["state"] == "failed"
    up = await _up(src)
    assert up["state"] == "failed" and up["error"]["next"] == "open Inbox"
    assert world.start.await_count == 3


async def test_destructive_migration_is_refused_without_an_approved_card(world, monkeypatch):
    src = await _source()
    bad = await _source({**SRC_V1, "drizzle/0001_drop.sql": "-- drop it\nALTER TABLE shifts DROP COLUMN who;"}, "c2")
    await _up(src)
    with pytest.raises(svc.AppsRefused) as e:
        await _up(bad)
    assert e.value.code == "destructive_migration" and "DROP COLUMN" in e.value.message and e.value.next

    approvals = {
        "card_ok": {"status": "approved", "companyId": "co_1", "payload": {"app_name": "crew"}},
        "card_other": {"status": "approved", "companyId": "co_2"},
    }
    admin = SimpleNamespace(
        _http=None, get_approval=AsyncMock(side_effect=lambda **kw: approvals.get(kw["approval_id"]))
    )
    monkeypatch.setattr(svc, "shared_admin_client", lambda: admin)
    monkeypatch.setattr(svc, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    monkeypatch.setattr(svc, "resolve_company_id_for_owner", AsyncMock(return_value="co_1"))
    for card in ("card_other", "card_missing"):
        with pytest.raises(svc.AppsRefused) as e:
            await _up(bad, approved_destructive=card)
        assert e.value.code == "approval_invalid"
    res = await _up(bad, approved_destructive="card_ok")
    assert res["state"] == "awaiting_approval"


async def test_destructive_check_ignores_comments_and_published_files(world):
    harmless = {
        **SRC_V1,
        "drizzle/0001_x.sql": "-- DROP TABLE shifts\n/* TRUNCATE x */\nCREATE INDEX i ON shifts (id);",
    }
    src = await _source(harmless)
    assert (await _up(src))["state"] == "awaiting_approval"
    await svc.apps_repo.patch(src["app_id"], {"published_migrations": ["0002_old.sql"]})
    old = await _source({**SRC_V1, "drizzle/0002_old.sql": "DROP TABLE legacy;"}, "c2")
    assert (await _up(old))["state"] == "awaiting_approval"


async def test_preview_alias_is_added_once_and_assigned_on_ready(world):
    src = await _source()
    await _up(src)
    await _all_active(world, src["app_id"])
    await svc.status(OWNER, src["app_id"])
    world.vercel.finish("dpl_1")
    res = await svc.status(OWNER, src["app_id"])
    # domain added + DNS pointed with preview=True, not verified yet: keep the vercel.app url
    assert world.point.await_args.kwargs["preview"] is True
    assert res["preview_url"] == "https://dpl_1.vercel.app"
    world.vercel.verified = True
    res = await svc.status(OWNER, src["app_id"])
    aliases = [r for r in world.vercel.calls if r.url.path.endswith("/aliases")]
    assert json.loads(aliases[-1].content) == {"alias": "crew-preview.apps.example.com"}
    assert res["preview_url"] == "https://crew-preview.apps.example.com"
    assert res["preview"]["url"] == "https://crew-preview.apps.example.com"  # R75: the alias host once assigned
    n_alias = len(aliases)
    await svc.status(OWNER, src["app_id"])
    assert len([r for r in world.vercel.calls if r.url.path.endswith("/aliases")]) == n_alias
    posts = [json.loads(r.content)["name"] for r in world.vercel.calls if r.url.path.endswith("/domains")]
    assert posts.count("crew-preview.apps.example.com") == 1


async def test_destroy_only_records_the_request(world):
    src = await _source()
    agent = svc.BuilderCaller(owner_id=OWNER, actor_id="agent_7", actor_kind="agent")
    res = await svc.destroy(OWNER, src["app_id"], actor=agent)
    assert res == {"state": "awaiting_owner", "next": "the owner removes it from Canvas"}
    app = await svc.apps_repo.get(src["app_id"])
    assert app["destroy_requested_by"] == "agent_7" and app["state"] != "destroyed"


async def test_remove_cleans_everything_and_is_retryable(world):
    src, _, _ = await _ready_preview(world)
    app_id = src["app_id"]
    with pytest.raises(svc.AppsRefused) as e:
        await svc.remove(OWNER, app_id, confirm_name="crw", user_id="user_1")
    assert e.value.code == "confirm_name_mismatch"
    await svc.remove(OWNER, app_id, confirm_name="crew", user_id="user_1")
    removed = sorted(c.args[1] for c in svc.api.remove_resource.await_args_list)
    assert removed == ["res_pwf_1", "res_pwf_2", "res_pwf_3"]
    assert not [k for k in world.secrets if k[1].startswith(f"apps/{app_id}/")]
    assert all(r["resource_status"] == "removed" for r in world.rows.values())
    world.remove.assert_awaited_with("crew")
    s3 = boto3.client("s3", region_name="us-east-1")
    assert s3.list_objects_v2(Bucket="test-state").get("KeyCount", 0) == 0
    assert (await svc.apps_repo.get(app_id))["state"] == "destroyed"
    assert not [r for r in world.vercel.calls if r.method == "DELETE"]
    # retry: nothing removed twice
    await svc.remove(OWNER, app_id, confirm_name="crew", user_id="user_1")
    assert svc.api.remove_resource.await_count == 3
    # R82: the name stays claimed after removal
    assert await svc.apps_repo.get_by_name(OWNER, "crew") is not None
    assert await svc.apps_repo.get(svc.apps_repo._guard_id("crew")) is not None


async def test_status_shape(world):
    src, _, _ = await _ready_preview(world)
    st = await svc.status(OWNER, src["app_id"])
    assert set(st) >= {
        "app_id", "name", "room_id", "state", "open_to", "budget_usd", "preview_url", "live_url", "canvas_url",
        "live_version", "origin", "versions", "resources", "last_opened_at", "spend_month_cents", "preview",
    }  # fmt: skip
    assert st["spend_month_cents"] is None
    assert {(r["provider"], r["service"], r["environment"]) for r in st["resources"]} == {
        ("Vercel", "project", "prod"),
        ("Neon", "postgres", "prod"),
        ("Neon", "postgres", "dev"),
    }
    assert all(set(r) == {"provider", "service", "state", "resource_id", "environment"} for r in st["resources"])


async def test_bad_name_and_other_owner(world):
    with pytest.raises(svc.AppsRefused) as e:
        await svc.put_source(OWNER, name="Bad_Name", room_id="r", commit="c", archive=_tar(SRC_V1), actor=PERSON)
    assert e.value.code == "invalid_name"
    src = await _source()
    with pytest.raises(svc.AppsRefused) as e:
        await svc.status("org_other", src["app_id"])
    assert e.value.code == "app_not_found"
    with pytest.raises(svc.apps_repo.NameTaken):
        await svc.put_source(
            "org_other", name="crew", room_id="r_other", commit="c", archive=_tar(SRC_V1), actor=PERSON
        )


# --- fix round 1 --------------------------------------------------------------------------------


async def _go_live(world, src):
    await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    world.vercel.finish(f"dpl_{len(world.vercel.created())}")
    return await svc.status(OWNER, src["app_id"])


async def test_two_reads_after_ready_record_one_version(world):
    src, _, _ = await _ready_preview(world)
    await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    world.vercel.finish("dpl_2")
    first = await svc._owned(OWNER, src["app_id"])
    second = copy.deepcopy(first)  # a concurrent reader with the same view
    await svc._check_publish(first, VERCEL)
    await svc._check_publish(second, VERCEL)
    app = await svc.apps_repo.get(src["app_id"])
    assert [v["n"] for v in app["versions"]] == [1] and app["live_version"] == 1
    assert second["live_version"] == 1 and not second.get("publish_pending")
    st = await svc.status(OWNER, src["app_id"])
    assert len(st["versions"]) == 1 and st["state"] == "live"


async def test_publish_after_rollback_promotes_before_recording(world):
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    await _go_live(world, await _source(SRC_V2, "c2"))
    await svc.rollback(OWNER, src["app_id"], version=None)
    src3 = await _source({**SRC_V2, "app/page.tsx": "v3"}, "c3")
    await svc.publish(OWNER, src["app_id"], source_id=src3["source_id"], actor=PERSON)
    dep = f"dpl_{len(world.vercel.created())}"
    world.vercel.finish(dep)
    world.vercel.promote_fails = True
    st = await svc.status(OWNER, src["app_id"])
    assert st["live_version"] == 1 and len(st["versions"]) == 2  # not promoted: not recorded
    world.vercel.promote_fails = False
    st = await svc.status(OWNER, src["app_id"])
    assert ("POST", f"/v10/projects/prj_1/promote/{dep}") in world.vercel.paths()
    assert st["live_version"] == 3 and len(st["versions"]) == 3
    # the flag is cleared: the next release is auto-assigned, no promote
    n_promote = sum("/promote/" in p for _, p in world.vercel.paths())
    await _go_live(world, await _source({**SRC_V2, "app/page.tsx": "v4"}, "c4"))
    assert sum("/promote/" in p for _, p in world.vercel.paths()) == n_promote


async def test_rollback_while_publishing_cancels_the_publish(world):
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    await _go_live(world, await _source(SRC_V2, "c2"))
    src3 = await _source({**SRC_V2, "app/page.tsx": "v3"}, "c3")
    await svc.publish(OWNER, src["app_id"], source_id=src3["source_id"], actor=PERSON)
    res = await svc.rollback(OWNER, src["app_id"], version=1)
    assert "cancelled" in res["error"]["message"]
    world.vercel.finish(f"dpl_{len(world.vercel.created())}")
    st = await svc.status(OWNER, src["app_id"])
    assert st["live_version"] == 1 and len(st["versions"]) == 2


async def test_failing_second_publish_keeps_the_live_version(world):
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    before = await svc.apps_repo.get(src["app_id"])
    src2 = await _source(SRC_V2, "c2")
    await svc.publish(OWNER, src["app_id"], source_id=src2["source_id"], actor=PERSON)
    world.vercel.finish(f"dpl_{len(world.vercel.created())}", "ERROR", "Type error")
    st = await svc.status(OWNER, src["app_id"])
    assert st["state"] == "live" and st["live_version"] == 1
    assert st["live_url"] == before["live_url"] and len(st["versions"]) == 1
    res = await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    assert res["state"] == "ready" and res["version"] == 1
    assert res["error"]["code"] == "build_failed" and res["error"]["next"] == "fix the error and publish again"
    # the preview is unaffected: up does not rebuild it
    n = len(world.vercel.created())
    await _up(src)
    assert len(world.vercel.created()) == n


async def test_reset_failure_is_not_retried_on_status_reads(world):
    world.reset.side_effect = svc.apps_db.AppsDbError("reset_failed", "preview database reset failed: X")
    src = await _source()
    await _up(src)
    await _all_active(world, src["app_id"])
    res = await _up(src)
    assert res["state"] == "failed" and res["error"]["code"] == "reset_failed"
    await svc.status(OWNER, src["app_id"])
    await svc.status(OWNER, src["app_id"])
    assert world.reset.await_count == 1 and world.vercel.created() == []
    world.reset.side_effect = None
    assert (await _up(src))["state"] == "building" and world.reset.await_count == 2


async def test_non_utf8_migration_is_refused(world):
    archive = _tar({"app/page.tsx": "x"})
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="drizzle/0000_x.sql")
        info.size = 2
        tf.addfile(info, io.BytesIO(b"\xff\xfe"))
    archive = buf.getvalue()
    with pytest.raises(svc.AppsRefused) as e:
        await svc.put_source(OWNER, name="crew", room_id="r", commit="c", archive=archive, actor=PERSON)
    assert e.value.code == "source_refused"


async def test_sources_are_pruned_but_keep_live_and_wanted(world):
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    for i in range(12):
        await _source({**SRC_V1, "app/page.tsx": f"p{i}"}, f"c{i}")
    sources = (await svc.apps_repo.get(src["app_id"]))["sources"]
    assert len(sources) == 11 and src["source_id"] in sources


# --- task 13: routes' service helpers -----------------------------------------------------------


async def test_a_record_change_publishes_one_apps_event_and_a_failure_never_breaks_the_write(world, published, caplog):
    src, _, _ = await _ready_preview(world)
    for _ in range(3):
        await asyncio.sleep(0)
    published.reset_mock()
    await svc.update_settings(OWNER, src["app_id"], budget_usd=33)
    for _ in range(3):
        await asyncio.sleep(0)
    assert published.await_count == 1
    owner, event = published.await_args.args
    assert owner == OWNER
    assert event["type"] == "runtime.resource.invalidated"
    assert event["payload"] == {"resource": "apps", "resourceId": src["app_id"]}

    published.side_effect = RuntimeError("appsync down")
    with caplog.at_level(logging.WARNING):
        await svc.update_settings(OWNER, src["app_id"], budget_usd=44)
        for _ in range(3):
            await asyncio.sleep(0)
    assert (await svc.apps_repo.get(src["app_id"]))["budget_usd"] == 44
    assert "apps live event failed" in caplog.text


async def test_update_settings_stores_and_repushes_open_to(world):
    src, _, _ = await _ready_preview(world)
    st = await svc.update_settings(OWNER, src["app_id"], open_to="public", budget_usd=50)
    assert (st["open_to"], st["budget_usd"]) == ("public", 50)
    app = await svc.apps_repo.get(src["app_id"])
    assert (app["open_to"], app["budget_usd"]) == ("public", 50)
    pushed = [i for post in world.vercel.env_posts() for i in post if i["key"] == "LIGHTBULB_OPEN_TO"]
    assert pushed[-1]["value"] == "public"


async def test_errors_split_client_and_server_and_redact(world, monkeypatch):
    src, _, dep_id = await _ready_preview(world)
    cookie = world.secrets[(OWNER, f"apps/{src['app_id']}/lightbulb")]["cookie_secret"]
    logs = AsyncMock(
        return_value=[
            {"at": 1, "message": f"[lightbulb:client] TypeError at {DEV_DB} cookie={cookie}"},
            {"at": 2, "message": f"db down postgresql://u:p@h/db token={VERCEL['token']}"},
        ]
    )
    events = AsyncMock(return_value=[])
    monkeypatch.setattr(svc.vercel, "runtime_logs", logs)
    monkeypatch.setattr(svc.vercel, "deployment_events", events)
    out = await svc.errors(OWNER, src["app_id"])
    assert logs.await_args.args[1] == dep_id
    assert [x["at"] for x in out["client"]] == [1] and [x["at"] for x in out["server"]] == [2]
    text = json.dumps(out)
    for secret in (DEV_DB, "DEVPW", cookie, VERCEL["token"], "u:p@h"):
        assert secret not in text
    assert "[redacted]" in out["client"][0]["message"]
    events.assert_not_awaited()


async def test_errors_fall_back_to_build_events(world, monkeypatch):
    src, _, dep_id = await _ready_preview(world)
    monkeypatch.setattr(svc.vercel, "runtime_logs", AsyncMock(return_value=[]))
    events = AsyncMock(return_value=[{"at": 5, "message": "Error: build failed"}])
    monkeypatch.setattr(svc.vercel, "deployment_events", events)
    assert await svc.errors(OWNER, src["app_id"]) == {
        "client": [],
        "server": [{"at": 5, "message": "Error: build failed"}],
    }
    assert events.await_args.args[1] == dep_id


async def test_errors_empty_before_any_deployment(world):
    src = await _source()
    assert await svc.errors(OWNER, src["app_id"]) == {"client": [], "server": []}


async def test_room_must_belong_to_the_callers_workspace(world, monkeypatch):
    """R46: neither put_source nor up attaches an app to another workspace's (or a deleted) room."""
    rooms = {
        "room_1": {"roomId": "room_1", "ownerId": OWNER},
        "room_x": {"roomId": "room_x", "ownerId": "org_other"},
        "room_del": {"roomId": "room_del", "ownerId": OWNER, "deletedAt": "t"},
    }
    monkeypatch.setattr(svc.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: rooms.get(r)))
    for room in ("room_x", "room_del", "room_missing"):
        with pytest.raises(svc.AppsRefused) as e:
            await svc.put_source(OWNER, name="crew", room_id=room, commit="c", archive=_tar(SRC_V1), actor=PERSON)
        assert e.value.code == "room_not_found"
    assert await svc.apps_repo.get_by_name(OWNER, "crew") is None
    src = await _source()
    with pytest.raises(svc.AppsRefused) as e:
        await svc.up(
            OWNER, name="crew", room_id="room_x", services=[], open_to="room", budget_usd=0,
            source_id=src["source_id"], actor=PERSON, origin={"kind": "idea"},
        )  # fmt: skip
    assert e.value.code == "room_not_found"
    assert (await svc.apps_repo.get(src["app_id"]))["room_id"] == "room_1"


# --- final review wave --------------------------------------------------------------------------


async def test_sharing_change_rebuilds_the_preview(world):
    """R51: Vercel env reaches only new deployments, so a new open_to rebuilds the preview."""
    src, _, _ = await _ready_preview(world)
    n = len(world.vercel.created())
    await svc.update_settings(OWNER, src["app_id"], open_to="workspace")
    assert len(world.vercel.created()) == n + 1 and "target" not in world.vercel.created()[-1]
    await svc.status(OWNER, src["app_id"])
    assert len(world.vercel.created()) == n + 1  # once


async def test_sharing_change_redeploys_the_live_app(world):
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    n = len(world.vercel.created())
    await svc.update_settings(OWNER, src["app_id"], open_to="workspace")
    prod = [d for d in world.vercel.created()[n:] if d.get("target") == "production"]
    assert len(prod) == 1
    world.vercel.finish(f"dpl_{len(world.vercel.created())}")
    world.vercel.finish(f"dpl_{len(world.vercel.created()) - 1}")
    st = await svc.status(OWNER, src["app_id"])
    assert st["state"] == "live" and [v["commit"] for v in st["versions"]] == ["c1", "c1"]


async def test_sharing_change_after_rollback_redeploys_the_rolled_back_source(world):
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    await _go_live(world, await _source(SRC_V2, "c2"))
    await svc.rollback(OWNER, src["app_id"], version=1)
    n = len(world.vercel.created())
    await svc.update_settings(OWNER, src["app_id"], open_to="workspace")
    prod = [d for d in world.vercel.created()[n:] if d.get("target") == "production"]
    assert len(prod) == 1 and {f["file"] for f in prod[0]["files"]} == set(SRC_V1)


async def test_protection_closes_vercel_urls_once_the_preview_alias_is_assigned(world):
    src, _, _ = await _ready_preview(world)

    def protection():
        return [
            json.loads(r.content)
            for r in world.vercel.calls
            if r.method == "PATCH" and json.loads(r.content).get("ssoProtection")
        ]

    assert protection() == []
    world.vercel.verified = True
    res = await svc.status(OWNER, src["app_id"])
    assert res["preview_url"] == "https://crew-preview.apps.example.com"
    assert protection() == [{"ssoProtection": {"deploymentType": "all_except_custom_domains"}}]
    await svc.status(OWNER, src["app_id"])
    assert len(protection()) == 1
    # a later preview build keeps the custom address, never a (now closed) *.vercel.app one
    await _up(await _source({**SRC_V1, "app/page.tsx": "v9"}, "c9"))
    world.vercel.finish(f"dpl_{len(world.vercel.created())}")
    assert (await svc.status(OWNER, src["app_id"]))["preview_url"] == "https://crew-preview.apps.example.com"


async def test_up_keeps_the_owners_settings_once_workflows_exist(world):
    """R52: sharing, budget, room, services and origin are the owner's after the first up."""
    src = await _source()
    await _up(src)
    await svc.up(
        OWNER, name="crew", room_id="r", services=["database", "email"], open_to="public", budget_usd=999,
        source_id=src["source_id"], actor=PERSON, origin={"kind": "chat"},
    )  # fmt: skip
    app = await svc.apps_repo.get(src["app_id"])
    assert (app["open_to"], app["budget_usd"], app["room_id"], app["services"], app["origin"]) == (
        "room", 20, "room_1", ["database"], {"kind": "idea", "ref": "i1"},
    )  # fmt: skip


async def test_remove_waits_for_a_row_mid_flight(world):
    """R54: a row past the card with no resource yet can't be cancelled: remove says busy, touches nothing."""
    src = await _source()
    await _up(src)
    world.rows["pwf_1"]["state"] = "provisioning"
    with pytest.raises(svc.AppsRefused) as e:
        await svc.remove(OWNER, src["app_id"], confirm_name="crew", user_id="user_1")
    assert (e.value.code, e.value.next) == ("busy", "try again in a minute")
    assert (await svc.apps_repo.get(src["app_id"]))["state"] != "destroyed"
    svc.provisioning_workflow.cancel.assert_not_awaited()
    world.remove.assert_not_awaited()


async def test_remove_is_busy_when_a_card_cannot_be_cancelled(world):
    src = await _source()
    await _up(src)
    svc.provisioning_workflow.cancel.side_effect = svc.provisioning_workflow.WorkflowRefused("not_cancellable", "x")
    with pytest.raises(svc.AppsRefused) as e:
        await svc.remove(OWNER, src["app_id"], confirm_name="crew", user_id="user_1")
    assert e.value.code == "busy"
    assert (await svc.apps_repo.get(src["app_id"]))["state"] != "destroyed"


async def test_deploy_uploads_files_concurrently_eight_at_a_time(world, monkeypatch):
    """R56: a deploy's uploads run side by side so the request fits API Gateway's 30 s."""
    import asyncio

    live = {"now": 0, "max": 0}

    async def upload(creds, data):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.01)
        live["now"] -= 1
        return f"sha{len(data)}"

    monkeypatch.setattr(svc.vercel, "upload_file", upload)
    src = await _source({**SRC_V1, **{f"app/f{i}.ts": "x" * i for i in range(20)}})
    await _up(src)
    await _all_active(world, src["app_id"])
    await svc.status(OWNER, src["app_id"])
    assert live["max"] == 8
    assert len(world.vercel.created()[0]["files"]) == 23


async def test_a_read_holding_the_lease_keeps_others_off_vercel(world):
    """R56: while one read steps the app, another only reports; the lease is released after."""
    src = await _source()
    await _up(src)
    await _all_active(world, src["app_id"])
    later = (svc.datetime.now(svc.UTC) + svc.timedelta(seconds=30)).isoformat()
    await svc.apps_repo.patch(src["app_id"], {"progress_lease_until": later})
    res = await svc.status(OWNER, src["app_id"])
    assert world.vercel.calls == [] and world.reset.await_count == 0 and res["state"] == "building"
    earlier = (svc.datetime.now(svc.UTC) - svc.timedelta(seconds=1)).isoformat()
    await svc.apps_repo.patch(src["app_id"], {"progress_lease_until": earlier})
    assert (await svc.status(OWNER, src["app_id"]))["state"] == "building"
    assert (await svc.apps_repo.get(src["app_id"]))["progress_lease_until"] is None


async def test_a_pass_that_outlived_its_lease_does_not_free_the_next_holders(world):
    src = await _source()
    await _up(src)
    mine = await svc.apps_repo.take_lease(src["app_id"], seconds=0)  # A's pass runs past its lease
    theirs = await svc.apps_repo.take_lease(src["app_id"], seconds=60)
    assert mine and theirs and theirs != mine
    assert await svc.apps_repo.release_lease(src["app_id"], mine) is False
    assert (await svc.apps_repo.get(src["app_id"]))["progress_lease_until"] == theirs
    assert await svc.apps_repo.release_lease(src["app_id"], theirs) is True


async def test_a_reader_from_before_the_release_does_not_undo_live(world):
    src, _, _ = await _ready_preview(world)
    stale = await svc._owned(OWNER, src["app_id"])  # read before the publish settled
    await _go_live(world, src)
    assert (await svc.apps_repo.get(src["app_id"]))["state"] == "live"
    await svc._record_state(copy.deepcopy(stale), "building")
    assert (await svc.apps_repo.get(src["app_id"]))["state"] == "live"
    # a reader that finds the lease held reports the record, not its old copy
    later = (svc.datetime.now(svc.UTC) + svc.timedelta(seconds=30)).isoformat()
    await svc.apps_repo.patch(src["app_id"], {"progress_lease_until": later})
    rows, _ = await svc._progress(stale)
    assert stale["state"] == "live" and await svc._record_state(stale, svc._state(stale, rows)[0]) == "live"


async def test_an_old_publishs_failure_does_not_drop_a_newer_publish(world):
    src, _, _ = await _ready_preview(world)
    src2 = await _source(SRC_V2, "c2")
    await svc.publish(OWNER, src["app_id"], source_id=src2["source_id"], actor=PERSON)
    old = f"dpl_{len(world.vercel.created())}"
    stale = await svc._owned(OWNER, src["app_id"])  # a leased reader still holding publish A
    src3 = await _source({**SRC_V2, "app/page.tsx": "v3"}, "c3")
    await svc.publish(OWNER, src["app_id"], source_id=src3["source_id"], actor=PERSON)
    new = f"dpl_{len(world.vercel.created())}"
    assert new != old
    world.vercel.finish(old, "ERROR", "Type error")
    await svc._check_publish(stale, VERCEL)
    assert (await svc.apps_repo.get(src["app_id"]))["publish_pending"]["deployment_id"] == new
    assert stale["publish_pending"]["deployment_id"] == new  # carried on from the record
    world.vercel.finish(new)
    st = await svc.status(OWNER, src["app_id"])
    assert st["state"] == "live" and st["live_version"] == 1
    assert st["versions"][-1]["deployment_id"] == new


def _approvals(monkeypatch, cards):
    admin = SimpleNamespace(_http=None, get_approval=AsyncMock(side_effect=lambda **kw: cards.get(kw["approval_id"])))
    monkeypatch.setattr(svc, "shared_admin_client", lambda: admin)
    monkeypatch.setattr(svc, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    monkeypatch.setattr(svc, "resolve_company_id_for_owner", AsyncMock(return_value="co_1"))


async def test_destructive_approval_is_bound_to_this_app_and_one_change(world, monkeypatch):
    """R57: the card names this app, and covers one destructive change (up and publish of it), no other."""
    src = await _source()
    await _up(src)
    app_id = src["app_id"]
    _approvals(
        monkeypatch,
        {
            "card_unbound": {"status": "approved", "companyId": "co_1", "payload": {}},
            "card_other_app": {"status": "approved", "companyId": "co_1", "payload": {"app_id": "app_zzz"}},
            "card_this": {"status": "approved", "companyId": "co_1", "payload": {"app_id": app_id}},
        },
    )
    drop = await _source({**SRC_V1, "drizzle/0001_drop.sql": "ALTER TABLE shifts DROP COLUMN who;"}, "c2")
    for card in ("card_unbound", "card_other_app"):
        with pytest.raises(svc.AppsRefused) as e:
            await _up(drop, approved_destructive=card)
        assert e.value.code == "approval_invalid"
    await _up(drop, approved_destructive="card_this")
    await svc._check_destructive(
        await svc._owned(OWNER, app_id), await svc._files(OWNER, app_id, drop["source_id"]), "card_this"
    )
    # the same card for a different destructive change is spent
    trunc = await _source({**SRC_V1, "drizzle/0002_trunc.sql": "TRUNCATE shifts;"}, "c3")
    with pytest.raises(svc.AppsRefused) as e:
        await _up(trunc, approved_destructive="card_this")
    assert e.value.code == "approval_invalid"


async def test_destructive_check_scans_every_sql_file_but_seeds(world):
    src = await _source()
    await _up(src)
    nested = await _source({**SRC_V1, "db/migrations/0003.sql": "DROP TABLE shifts;"}, "c2")
    with pytest.raises(svc.AppsRefused) as e:
        await _up(nested)
    assert e.value.code == "destructive_migration" and "db/migrations/0003.sql" in e.value.message
    seeds = await _source(
        {**SRC_V1, "db/seed.sql": "DELETE FROM shifts;", "drizzle/seed.sql": "TRUNCATE shifts;"}, "c3"
    )
    assert (await _up(seeds))["state"] == "awaiting_approval"


async def test_preview_resets_when_a_migration_changes_under_the_same_name(world):
    """R63: the reset is keyed on the migration files' content, not only their names."""
    src, _, _ = await _ready_preview(world)
    assert world.reset.await_count == 1
    edited = await _source({**SRC_V1, "drizzle/0000_init.sql": "CREATE TABLE shifts (id int, who text);"}, "c2")
    await _up(edited)
    assert world.reset.await_count == 2
    assert world.reset.await_args.kwargs["migrations"] == ["CREATE TABLE shifts (id int, who text);"]


async def test_a_failed_preview_build_keeps_a_live_app_live(world):
    """R63: the live version still serves; the preview's error rides along."""
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    await _up(await _source({**SRC_V1, "app/page.tsx": "broken"}, "c2"))
    world.vercel.finish(f"dpl_{len(world.vercel.created())}", "ERROR", "Type error")
    st = await svc.status(OWNER, src["app_id"])
    assert st["state"] == "live"
    res = await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)  # a no-op read
    assert res["state"] == "ready" and res["version"] == 1 and res["error"]["code"] == "build_failed"
    assert (await svc.apps_repo.get(src["app_id"]))["state"] == "live"


# --- member publish, requested_by, display name (R69, R73, R74) ---------------------------------


async def test_member_publish_needs_a_ready_preview_and_publishes_its_source(world):
    src = await _source()
    await _up(src)
    with pytest.raises(svc.AppsRefused) as e:
        await svc.member_publish(OWNER, src["app_id"], user_id="user_2")
    assert (e.value.code, e.value.next) == ("not_ready", "wait for the preview")

    src, _, _ = await _ready_preview(world)
    newer = await _source(SRC_V2, "c2")
    await _up(newer)  # a newer preview is building; the READY one is still c1
    deploys = len(world.vercel.created())
    res = await svc.member_publish(OWNER, src["app_id"], user_id="user_2")
    assert res["state"] == "building"
    assert len(world.vercel.created()) == deploys + 1 and world.vercel.created()[-1]["target"] == "production"
    pending = (await svc.apps_repo.get(src["app_id"]))["publish_pending"]
    assert (pending["source_id"], pending["commit"], pending["by"]) == (src["source_id"], "c1", "user_2")


async def test_member_publish_of_a_destructive_source_asks_for_the_builder(world, monkeypatch):
    _approvals(monkeypatch, {"card_1": {"status": "approved", "companyId": "co_1", "payload": {"app_name": "crew"}}})
    src = await _source({**SRC_V1, "drizzle/0001_drop.sql": "ALTER TABLE shifts DROP COLUMN who;"})
    await _up(src, approved_destructive="card_1")
    await _all_active(world, src["app_id"])
    await svc.status(OWNER, src["app_id"])
    world.vercel.finish(f"dpl_{len(world.vercel.created())}")
    await svc.status(OWNER, src["app_id"])
    with pytest.raises(svc.AppsRefused) as e:
        await svc.member_publish(OWNER, src["app_id"], user_id="user_2")
    assert (e.value.code, e.value.next) == ("destructive_migration", "ask the builder to get the owner's approval")


AGENT = svc.BuilderCaller(owner_id=OWNER, actor_id="agt_1", actor_kind="agent")


async def test_requested_by_is_taken_on_the_first_up_from_a_room_member_only(world, monkeypatch):
    async def member(owner_id, user_id, room_id):
        if (owner_id, user_id, room_id) != (OWNER, "user_in", "room_1"):
            raise svc.messaging.ApiProblem("ROOM_NOT_FOUND", "no")
        return {}, {}

    monkeypatch.setattr(svc.messaging, "require_room_member", AsyncMock(side_effect=member))

    async def up(name, requested_by, actor=AGENT):
        src = await svc.put_source(OWNER, name=name, room_id="room_1", commit="c", archive=_tar(SRC_V1), actor=actor)
        await svc.up(
            OWNER, name=name, room_id="room_1", services=[], open_to="room", budget_usd=0,
            source_id=src["source_id"], actor=actor, origin={"kind": "idea"}, requested_by=requested_by,
        )  # fmt: skip
        return (await svc.apps_repo.get(src["app_id"]))["created_by"]

    assert await up("one", "user_out") is None  # not in the room: ignored
    assert await up("one", "user_in") is None  # no longer the first up
    assert await up("two", "user_in") == "user_in"
    assert await up("three", "user_in", actor=PERSON) == "user_1"  # a person's app keeps its creator


async def test_display_name_defaults_from_the_slug_and_is_settable(world):
    src = await svc.put_source(
        OWNER, name="crew-schedule", room_id="room_1", commit="c", archive=_tar(SRC_V1), actor=PERSON
    )
    assert (await svc.apps_repo.get(src["app_id"]))["display_name"] == "Crew Schedule"
    assert (await svc.status(OWNER, src["app_id"]))["display_name"] == "Crew Schedule"
    st = await svc.update_settings(OWNER, src["app_id"], display_name="Rota")
    assert st["display_name"] == "Rota"
    await svc.apps_repo.patch(src["app_id"], {"display_name": None})  # a record from before R74
    assert (await svc.status(OWNER, src["app_id"]))["display_name"] == "Crew Schedule"


async def test_up_on_a_live_app_says_ready_never_live(world):
    """R77: UpResult.state is in the contract enum; the record (and AppStatus) may say live."""
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    res = await _up(src)
    assert res["state"] == "ready" and res["version"] == 1
    assert (await svc.status(OWNER, src["app_id"]))["state"] == "live"


async def test_services_content_files_only_the_three_workflows(world):
    """R79: app.yaml services are stored, never provisioned by name."""
    src = await _source()
    await svc.up(
        OWNER, name="crew", room_id="room_1", services=["database", "content"], open_to="room", budget_usd=20,
        source_id=src["source_id"], actor=PERSON, origin={"kind": "idea", "ref": "i1"},
    )  # fmt: skip
    filed = [
        (c.kwargs["provider_name"], c.kwargs["service_id"], c.kwargs["environment"])
        for c in world.start.await_args_list
    ]
    assert filed == [("vercel", "project", "prod"), ("neon", "postgres", "prod"), ("neon", "postgres", "dev")]
    assert not any("content" in str(c.kwargs) for c in world.start.await_args_list)


def test_display_default_splits_words_on_dash_and_underscore():
    assert svc._display({"name": "workshop-signup"}) == "Workshop Signup"
    assert svc._display({"name": "crm_v2"}) == "Crm V2"


async def test_destroyed_apps_take_no_sweeper_slot(world):
    """The scan caps at its limit before the sweeper skips destroyed apps, so they could fill every slot."""
    src, _, _ = await _ready_preview(world)
    await svc.apps_repo.patch(src["app_id"], {"preview_repoint": {"deployment_id": "dpl_1"}})
    assert [a["app_id"] for a in await svc.apps_repo.list_pending()] == [src["app_id"]]
    await svc.apps_repo.patch(src["app_id"], {"state": "destroyed"})
    assert await svc.apps_repo.list_pending() == []


async def test_sweeper_settles_a_pending_publish_without_the_builder(world):
    """Nothing but the builder's own reads stepped a publish; with the agent down, Demo Lab's first
    publish sat in "building" for 21 minutes. The sweeper steps pending deployments on its own."""
    from core.services import apps_sweeper

    src, _, _ = await _ready_preview(world)
    await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    assert [a["app_id"] for a in await svc.apps_repo.list_pending()] == [src["app_id"]]
    assert await apps_sweeper.sweep_once() == 1  # still building: stepped, nothing to record yet
    world.vercel.finish("dpl_2")
    assert await apps_sweeper.sweep_once() == 1
    rec = await svc.apps_repo.get(src["app_id"])
    assert (rec["state"], int(rec["live_version"])) == ("live", 1)
    assert rec.get("publish_pending") is None
    assert await svc.apps_repo.list_pending() == []  # settled apps leave the sweep
    assert await apps_sweeper.sweep_once() == 0


async def test_publish_applies_new_migrations_itself_before_the_build(world):
    """The starter's drizzle config has no credentials, so `drizzle-kit migrate` inside Vercel's build
    can never connect (both Demo Lab publishes). The backend runs the new migrations on the live
    database first and records them, so a failed build never re-runs them."""
    src, _, _ = await _ready_preview(world)
    pub = await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    assert pub["state"] == "building"
    world.apply.assert_awaited_once()
    assert world.apply.await_args.args == (LIVE_DB,)
    assert world.apply.await_args.kwargs == {"migrations": [SRC_V1["drizzle/0000_init.sql"]]}
    assert (await svc.apps_repo.get(src["app_id"]))["published_migrations"] == ["0000_init.sql"]
    # the same source again while building: no second apply
    await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    world.apply.assert_awaited_once()


async def test_publish_stops_when_a_live_migration_fails(world):
    src, _, _ = await _ready_preview(world)
    world.apply.side_effect = svc.apps_db.AppsDbError("migrate_failed", "live database migration failed: X")
    n = len(world.vercel.created())
    res = await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    assert res["error"]["code"] == "migrate_failed" and res["error"]["next"] == "fix the migration and publish again"
    assert len(world.vercel.created()) == n  # nothing deployed
    assert (await svc.apps_repo.get(src["app_id"])).get("published_migrations") in (None, [])


# --- a refused Vercel credential: Demo Lab, 30 Oct 2026; Stripe's doc, "Rotate credentials" -------


def _rec(src):
    return svc.apps_repo.get(src["app_id"])


def _renewal(monkeypatch, rotate="complete", reveal=None):
    """``rotate``: Stripe's status, or an exception. ``reveal``: what each reveal does in turn; by default
    Stripe still holds the stored token. An exception raises; a dict is the configuration."""
    rot = AsyncMock(side_effect=rotate) if isinstance(rotate, Exception) else AsyncMock(return_value={"status": rotate})
    rev = AsyncMock(return_value={"configuration": VERCEL})
    if reveal is not None:
        rev = AsyncMock(side_effect=reveal)
    monkeypatch.setattr(svc.api, "rotate_credentials", rot)
    monkeypatch.setattr(svc.api, "reveal_access_configuration", rev)
    return rot, rev


def _vercel_answers(world, monkeypatch, answer):
    """``answer(req)`` returns a response for the calls it cares about, else None for the normal fake."""

    def handler(req):
        return answer(req) or world.vercel.handler(req)

    monkeypatch.setattr(vercel_client, "_transport", httpx.MockTransport(handler))


NEW = {**VERCEL, "token": "vtok_NEW"}
JSON_403 = {"error": {"code": "forbidden", "message": "Not authorized"}}


def _new_token_accepted(world):
    def reveal(account, resource):
        world.vercel.refused = False
        return {"configuration": NEW}

    return reveal


async def _settled_preview(world):
    """A READY preview with nothing left for ``_steps`` to ask Vercel: ``_progress`` passes while the token is dead."""
    src, dep, dep_id = await _ready_preview(world)
    world.vercel.verified = True
    await svc.status(OWNER, src["app_id"])
    n = len(world.vercel.calls)
    await svc.status(OWNER, src["app_id"])
    assert len(world.vercel.calls) == n
    return src, dep, dep_id


async def _publish(src):
    return await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)


async def _age_renewal(src, hours=2):
    rec = await _rec(src)
    at = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    await svc.apps_repo.patch(src["app_id"], {"credential_renewal": {**rec["credential_renewal"], "at": at}})


async def test_publish_exactly_as_in_production_progress_passes_the_deploy_is_refused_then_renewed(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    key = (OWNER, f"apps/{src['app_id']}/vercel/prod")
    answers = iter([{"configuration": VERCEL}])
    accept = _new_token_accepted(world)
    rotate, reveal = _renewal(monkeypatch, reveal=lambda a, r: next(answers, None) or accept(a, r))
    world.vercel.refused = True
    world.vercel.calls.clear()
    res = await _publish(src)
    assert res.get("error") is None and res["state"] == "building"
    # The dead token is found by the check before the migrations; the new one is checked, then deploys.
    assert world.vercel.calls[0].headers["authorization"] != "Bearer vtok_NEW"
    assert world.vercel.calls[1].url.path == "/v13/deployments/dpl_1"
    assert world.vercel.calls[1].headers["authorization"] == "Bearer vtok_NEW"
    rotate.assert_awaited_once()
    assert rotate.await_args.args[0] == "acct_1" and rotate.await_args.args[1].startswith("res_")
    assert reveal.await_count == 2  # first to look, then for the rotated one
    assert world.secrets[key]["token"] == "vtok_NEW"
    assert world.vercel.calls[-1].headers["authorization"] == "Bearer vtok_NEW"
    rec = await _rec(src)
    assert rec["credential_renewal"]["state"] == "complete" and rec.get("publish_pending")


async def test_publish_refused_at_the_deploy_is_stored_and_beats_an_older_build_error(world, monkeypatch):
    """Demo Lab: publish failed 'forbidden' while the result and Canvas showed the previous build's error."""
    src, _, _ = await _settled_preview(world)
    old = {"code": "build_failed", "message": "old build error", "next": "fix"}
    await svc.apps_repo.patch(src["app_id"], {"error": old, "publish_error": old})
    _renewal(monkeypatch, rotate="errored")
    world.vercel.refused = True
    res = await _publish(src)
    assert res["error"]["code"] == svc._REFUSAL_CODE
    assert "hosting credential" in res["error"]["message"]
    rec = await _rec(src)
    assert rec["publish_error"]["code"] == svc._REFUSAL_CODE and rec.get("publish_pending") is None


async def test_sweeper_refusal_rotates_once_ever_and_reports_it(world, monkeypatch):
    from core.services import apps_sweeper

    src, _, _ = await _ready_preview(world)
    await _publish(src)
    rotate, reveal = _renewal(monkeypatch, rotate="pending")
    world.vercel.refused = True
    await apps_sweeper.sweep_once()
    assert (await _rec(src))["credential_renewal"]["state"] == "pending"
    await _age_renewal(src, hours=48)
    await apps_sweeper.sweep_once()
    rotate.assert_awaited_once()
    assert reveal.await_count == 2  # looking is safe; sending isn't
    st = await svc.status(OWNER, src["app_id"])
    assert st["error"]["code"] == "credential_renewal_stuck" and "Lightbulb's team" in st["error"]["next"]


async def test_sweeper_refusal_recovers_and_the_pending_publish_lands(world, monkeypatch):
    from core.services import apps_sweeper

    src, _, _ = await _ready_preview(world)
    await _publish(src)
    rotate, _ = _renewal(monkeypatch, reveal=[{"configuration": VERCEL}, {"configuration": NEW}])
    world.vercel.refused = True
    original_store = svc.integration_secret_store.put

    async def put(o, k, c):
        world.vercel.refused = False
        return await original_store(o, k, c)

    monkeypatch.setattr(svc.integration_secret_store, "put", put)
    await apps_sweeper.sweep_once()
    rotate.assert_awaited_once()
    world.vercel.finish("dpl_2")
    await apps_sweeper.sweep_once()
    assert (await _rec(src))["state"] == "live"


async def test_R1_never_live_app_leaves_failed_once_the_token_works(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    _renewal(monkeypatch, rotate="pending")
    world.vercel.refused = True
    res = await _publish(src)
    assert res["state"] == "failed"
    world.vercel.refused = False
    st = await svc.status(OWNER, src["app_id"])
    assert st["state"] != "failed"
    assert (await _rec(src)).get("publish_error") is None


async def test_R1b_up_also_recovers_a_refusal(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    _renewal(monkeypatch, rotate="pending")
    world.vercel.refused = True
    await _publish(src)
    world.vercel.refused = False
    res = await _up(src)
    assert res["state"] != "failed"
    assert (await _rec(src)).get("publish_error") is None


async def test_R2_live_app_stops_reporting_a_refusal_that_cleared(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    await _publish(src)
    world.vercel.finish("dpl_2")
    await svc.status(OWNER, src["app_id"])
    live_source = (await _rec(src))["live_source_id"]
    await svc.apps_repo.patch(src["app_id"], {"publish_error": svc._refusal(await _rec(src))})
    res = await svc.publish(OWNER, src["app_id"], source_id=live_source, actor=PERSON)
    assert res.get("error") is None


async def test_R3_a_pending_rotation_is_not_resent_however_old(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    rotate, _ = _renewal(monkeypatch, rotate="pending")
    world.vercel.refused = True
    res = await _publish(src)
    assert res["error"]["code"] == svc._REFUSAL_CODE
    assert "Lightbulb's team" in res["error"]["next"]
    await _age_renewal(src, hours=24 * 30)
    await _publish(src)
    rotate.assert_awaited_once()


@pytest.mark.parametrize("html_everywhere", [False, True])
async def test_R4_an_odd_html_403_never_rotates_a_good_token(world, monkeypatch, html_everywhere):
    src, _, _ = await _ready_preview(world)
    rotate, reveal = _renewal(monkeypatch)
    await svc.apps_repo.patch(src["app_id"], {"protection_scoped": False, "preview_alias_for": "x"})

    def html_403(req):
        if html_everywhere or req.method == "PATCH":
            return httpx.Response(403, text="<html>blocked</html>")
        return None

    _vercel_answers(world, monkeypatch, html_403)
    await svc.status(OWNER, src["app_id"])
    rotate.assert_not_awaited()
    reveal.assert_not_awaited()
    assert world.secrets[(OWNER, f"apps/{src['app_id']}/vercel/prod")]["token"] == VERCEL["token"]


async def test_R5_a_failed_reveal_never_rotates(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    rotate, reveal = _renewal(monkeypatch, reveal=RuntimeError("boom"))
    world.vercel.refused = True
    res = await _publish(src)
    await _publish(src)
    assert res["error"]["code"] == svc._REFUSAL_CODE
    rotate.assert_not_awaited()
    assert reveal.await_count > 1
    assert (await _rec(src)).get("credential_renewal") is None


async def test_R5b_reveal_failing_after_complete_is_owed_and_the_next_attempt_reveals(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    key = (OWNER, f"apps/{src['app_id']}/vercel/prod")

    answers = iter([{"configuration": VERCEL}, RuntimeError("boom")])
    accept = _new_token_accepted(world)

    def turn(a, r):
        step = next(answers, None)
        if isinstance(step, Exception):
            raise step
        return step or accept(a, r)

    rotate, reveal = _renewal(monkeypatch, reveal=turn)
    world.vercel.refused = True
    res = await _publish(src)
    assert res["error"]["code"] == svc._REFUSAL_CODE
    assert (await _rec(src))["credential_renewal"]["state"] == "needs_reveal"
    assert world.secrets[key]["token"] == VERCEL["token"]
    res = await _publish(src)
    rotate.assert_awaited_once()
    assert reveal.await_count == 3
    assert res.get("error") is None and world.secrets[key]["token"] == "vtok_NEW"
    assert (await _rec(src))["credential_renewal"]["state"] == "complete"


async def test_R5c_owed_reveal_runs_from_a_read_with_nothing_else_to_ask_vercel(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    fresh = {"state": "needs_reveal", "at": datetime.now(UTC).isoformat()}
    await svc.apps_repo.patch(src["app_id"], {"credential_renewal": fresh})
    rotate, reveal = _renewal(monkeypatch)
    world.vercel.refused = True
    await svc.status(OWNER, src["app_id"])
    reveal.assert_awaited_once()
    rotate.assert_not_awaited()


async def test_R6_two_stale_copies_rotate_once(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    rows = await svc._rows(await _rec(src))
    a = dict(await _rec(src))
    b = dict(a)
    rotate, _ = _renewal(monkeypatch, rotate="pending")
    await svc._renew_vercel(a, rows, VERCEL)
    await svc._renew_vercel(b, rows, VERCEL)
    rotate.assert_awaited_once()


async def test_the_claim_is_atomic_a_rival_between_read_and_claim_wins(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    rows = await svc._rows(await _rec(src))

    async def rival_claims(account, resource):
        await svc.apps_repo.patch(src["app_id"], {"credential_renewal": {"state": "pending", "at": "rival"}})
        return {"configuration": VERCEL}

    rotate, _ = _renewal(monkeypatch, reveal=rival_claims)
    assert await svc._renew_vercel(dict(await _rec(src)), rows, VERCEL) is None
    rotate.assert_not_awaited()
    assert (await _rec(src))["credential_renewal"]["at"] == "rival"


async def test_a_reader_with_an_old_copy_takes_the_stored_token_and_touches_nothing_at_stripe(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    rows = await svc._rows(await _rec(src))
    world.secrets[(OWNER, f"apps/{src['app_id']}/vercel/prod")] = dict(NEW)
    rotate, reveal = _renewal(monkeypatch)
    assert (await svc._renew_vercel(dict(await _rec(src)), rows, VERCEL))["token"] == "vtok_NEW"
    rotate.assert_not_awaited()
    reveal.assert_not_awaited()


async def test_an_odd_json_403_from_one_endpoint_never_rotates_and_stays_reported(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    rotate, reveal = _renewal(monkeypatch)
    _vercel_answers(
        world,
        monkeypatch,
        lambda req: httpx.Response(403, json=JSON_403) if req.url.path.endswith("/domains") else None,
    )
    res = await _publish(src)
    rotate.assert_not_awaited()
    reveal.assert_not_awaited()
    assert res["error"]["code"] == "vercel_forbidden" and "hosting credential" not in res["error"]["message"]
    await svc.status(OWNER, src["app_id"])  # not a refusal, so not cleared by a healthy read
    assert (await _rec(src))["publish_error"]["code"] == "vercel_forbidden"


async def test_a_probe_with_no_deployment_reads_the_project_env(world):
    await vercel_client.read_probe(VERCEL, None)
    assert world.vercel.paths("GET")[-1] == ("GET", "/v10/projects/prj_1/env")


async def test_reveal_first_picks_up_a_new_token_with_no_rotation(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    rotate, reveal = _renewal(monkeypatch, reveal=_new_token_accepted(world))
    world.vercel.refused = True
    res = await _publish(src)
    rotate.assert_not_awaited()
    reveal.assert_awaited_once()
    assert res.get("error") is None and res["state"] == "building"
    assert world.secrets[(OWNER, f"apps/{src['app_id']}/vercel/prod")]["token"] == "vtok_NEW"
    assert (await _rec(src)).get("credential_renewal") is None


@pytest.mark.parametrize(
    ("rotate", "state"),
    [
        ("pending", "pending"),
        ("errored", "errored"),
        ("something_new", "pending"),
        (svc.api.TransientProvisioningError("transport_error", "timeout"), "pending"),
        (svc.api.TransientProvisioningError("server_error", "5xx", http_status=503), "pending"),
        (svc.api.ProvisioningError("invalid_request", "no"), "errored"),
        (RuntimeError("boom"), "pending"),
    ],
)
async def test_every_rotation_outcome_but_complete_holds_for_good(world, monkeypatch, rotate, state):
    src, _, _ = await _ready_preview(world)
    rot, _ = _renewal(monkeypatch, rotate=rotate)
    world.vercel.refused = True
    await _publish(src)
    assert (await _rec(src))["credential_renewal"]["state"] == state
    await _age_renewal(src, hours=24 * 30)
    await _publish(src)
    await svc.status(OWNER, src["app_id"])
    rot.assert_awaited_once()
    # A person clearing the record is what lets it try again.
    await svc.apps_repo.patch(src["app_id"], {"credential_renewal": None})
    await _publish(src)
    assert rot.await_count == 2


async def test_a_cancel_mid_rotation_leaves_pending(world, monkeypatch):
    """The sweeper's wait_for cancels with CancelledError, which no `except Exception` sees."""
    src, _, _ = await _ready_preview(world)
    _renewal(monkeypatch)
    monkeypatch.setattr(svc.api, "rotate_credentials", AsyncMock(side_effect=asyncio.CancelledError()))
    world.vercel.refused = True
    with pytest.raises(asyncio.CancelledError):
        await svc._renew_vercel(dict(await _rec(src)), await svc._rows(await _rec(src)), VERCEL)
    assert (await _rec(src))["credential_renewal"]["state"] == "pending"


async def test_a_rotated_token_refused_twice_stops_rather_than_rotating_again(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    answers = iter([{"configuration": VERCEL}, {"configuration": NEW}])
    rotate, _ = _renewal(monkeypatch, reveal=lambda a, r: next(answers, {"configuration": NEW}))
    world.vercel.refused = True
    res = await _publish(src)
    assert res["error"]["code"] == svc._REFUSAL_CODE
    assert (await _rec(src))["credential_renewal"]["state"] == "errored"
    await _age_renewal(src)
    await _publish(src)
    rotate.assert_awaited_once()


async def test_a_token_that_dies_again_rotates_again_only_after_the_spacing(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    key = (OWNER, f"apps/{src['app_id']}/vercel/prod")
    world.secrets[key] = dict(NEW)
    await svc.apps_repo.patch(
        src["app_id"], {"credential_renewal": {"state": "complete", "at": datetime.now(UTC).isoformat()}}
    )
    rotate, _ = _renewal(monkeypatch, rotate="pending", reveal=lambda a, r: {"configuration": NEW})
    world.vercel.refused = True
    await _publish(src)
    rotate.assert_not_awaited()
    await _age_renewal(src)
    await _publish(src)
    rotate.assert_awaited_once()


async def test_a_refusal_stored_while_still_refused_stays(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    _renewal(monkeypatch, rotate="pending")
    world.vercel.refused = True
    await _publish(src)
    st = await svc.status(OWNER, src["app_id"])
    assert st["state"] == "failed" and (await _rec(src))["publish_error"]["code"] == svc._REFUSAL_CODE


async def test_a_real_build_error_is_not_cleared_by_a_working_token(world):
    src, _, _ = await _settled_preview(world)
    err = {"code": "build_failed", "message": "bad", "next": "fix"}
    await svc.apps_repo.patch(src["app_id"], {"publish_error": err})
    st = await svc.status(OWNER, src["app_id"])
    assert st["state"] == "failed" and (await _rec(src))["publish_error"] == err


async def test_a_refusal_during_a_pending_publish_does_not_fail_a_build_that_can_still_land(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    await _publish(src)
    rotate, _ = _renewal(monkeypatch, rotate="errored")
    world.vercel.refused = True
    res = await _publish(src)
    assert res["error"]["code"] == svc._REFUSAL_CODE
    rotate.assert_awaited_once()
    assert (await _rec(src)).get("publish_error") is None


async def test_no_message_points_a_person_at_stripe_or_at_rotating(world):
    for app in ({}, {"credential_renewal": {"state": "pending", "at": "t"}}):
        for err in (svc._refusal(app), svc._renewal_hold({"credential_renewal": {"state": "errored", "at": "t"}})):
            text = f"{err['message']} {err['next']}".lower()
            assert "stripe" not in text and "rotate" not in text and "lightbulb's team" in text


async def test_a_vercel_failure_that_is_not_a_refusal_never_rotates(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    rotate, reveal = _renewal(monkeypatch)
    _vercel_answers(
        world,
        monkeypatch,
        lambda req: httpx.Response(500, json={"error": {"code": "internal", "message": "boom"}}),
    )
    res = await _publish(src)
    assert res["error"]["code"] == "vercel_internal"
    rotate.assert_not_awaited()
    reveal.assert_not_awaited()


# --- a token lives one hour: the ordinary path, wherever Vercel is called ---------------------------


def _dies_then_rotates(world, monkeypatch):
    """Stripe keeps returning the dead token until the rotation, then the new one, which Vercel takes."""
    held = {"configuration": VERCEL}

    async def rotate(account, resource):
        held["configuration"] = NEW
        world.vercel.refused = False
        return {"status": "complete"}

    rot = AsyncMock(side_effect=rotate)
    rev = AsyncMock(side_effect=lambda a, r: dict(held))
    monkeypatch.setattr(svc.api, "rotate_credentials", rot)
    monkeypatch.setattr(svc.api, "reveal_access_configuration", rev)
    world.vercel.refused = True
    world.vercel.calls.clear()
    return rot


HOST = "crew-preview.apps.example.com"


async def _preview_on_its_host(world):
    world.vercel.verified = True
    src, _, dep_id = await _ready_preview(world)
    await svc.status(OWNER, src["app_id"])  # adds the domain and verifies it
    await svc.status(OWNER, src["app_id"])  # aliases
    assert world.vercel.aliases[HOST] == dep_id
    return src, dep_id


async def _release(world, src):
    await _publish(src)
    dep_id = f"dpl_{len(world.vercel.created())}"
    world.vercel.finish(dep_id)
    return dep_id


async def _settle(src, world):
    """Past the settling time and any backoff, then one read."""
    rec = await _rec(src)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    watch = rec.get("preview_repoint")
    if watch:
        watch = {**watch, "settle_until": past, "next_at": past if watch["next_at"] else None}
        await svc.apps_repo.patch(src["app_id"], {"preview_repoint": watch})
    return await svc.status(OWNER, src["app_id"])


async def test_a_publish_puts_the_preview_host_back_before_the_app_is_live(world, monkeypatch):
    src, preview = await _preview_on_its_host(world)
    live = await _release(world, src)
    assert world.vercel.aliases[HOST] == live  # the known limit: production holds it until a step notices

    seen = {}
    record = svc.apps_repo.record_release

    async def spy(*a, **k):
        seen["host"] = world.vercel.aliases[HOST]
        return await record(*a, **k)

    monkeypatch.setattr(svc.apps_repo, "record_release", spy)
    res = await svc.status(OWNER, src["app_id"])
    assert res["state"] == "live" and seen["host"] == preview
    assert world.vercel.aliases[HOST] == (await _rec(src))["preview"]["deployment_id"]


async def test_production_taking_the_host_after_ready_is_caught_and_the_watch_ends(world):
    src, preview = await _preview_on_its_host(world)
    live = await _release(world, src)
    await svc.status(OWNER, src["app_id"])
    assert (await _rec(src))["preview_repoint"]["attempts"] == 0
    assert [a["app_id"] for a in await svc.apps_repo.list_pending()] == [src["app_id"]]  # the sweeper keeps coming

    world.vercel.serve_production(live)  # Vercel assigned its domains late
    await svc.status(OWNER, src["app_id"])
    assert world.vercel.aliases[HOST] == preview

    await _settle(src, world)
    assert (await _rec(src))["preview_repoint"] is None
    assert await svc.apps_repo.list_pending() == []
    calls = len(world.vercel.calls)
    await svc.status(OWNER, src["app_id"])
    assert not [r for r in world.vercel.calls[calls:] if "/aliases" in r.url.path]


async def test_put_back_repoints_in_the_same_request(world):
    src = await _two_versions_on_host(world)
    preview = (await _rec(src))["preview"]["deployment_id"]
    await svc.rollback(OWNER, src["app_id"], version=1)
    assert world.vercel.aliases[HOST] == preview
    assert (await _rec(src))["live_version"] == 1


async def test_a_dead_token_at_put_back_does_not_go_back_a_second_version(world, monkeypatch):
    src = await _two_versions_on_host(world)
    preview = (await _rec(src))["preview"]["deployment_id"]

    def dead_on_alias(req):
        return httpx.Response(403, json=DEAD_403) if req.url.path.startswith("/v4/aliases/") else None

    _vercel_answers(world, monkeypatch, dead_on_alias)
    await svc.rollback(OWNER, src["app_id"], version=None)
    rec = await _rec(src)
    assert rec["live_version"] == 1 and rec["preview_repoint"]["deployment_id"] == preview
    assert len([p for _, p in world.vercel.paths("POST") if "/rollback/" in p]) == 1


async def test_a_stored_error_does_not_skip_the_repoint(world):
    src, preview = await _preview_on_its_host(world)
    live = await _release(world, src)
    await svc.status(OWNER, src["app_id"])
    # A later preview's migration failed: the step stops early on this stored error, every read.
    await svc.apps_repo.patch(
        src["app_id"],
        {"error": {"code": "x", "message": "m", "next": "n"}, "preview_migrations": ["something else"]},
    )
    world.vercel.serve_production(live)  # Vercel assigned its domains late
    await svc.status(OWNER, src["app_id"])
    assert world.vercel.aliases[HOST] == preview


async def test_an_app_published_before_this_is_repaired_once_on_its_next_read(world):
    src, preview = await _preview_on_its_host(world)
    # workshop-signup: live, the record says the alias is fine, the host is really on production.
    await _release(world, src)
    await svc.status(OWNER, src["app_id"])
    await svc.apps_repo.patch(src["app_id"], {"preview_alias_for": preview, "preview_host_error": None})
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("test-apps")
    table.update_item(Key={"app_id": src["app_id"]}, UpdateExpression="REMOVE preview_repoint")
    world.vercel.aliases[HOST] = "dpl_live"

    await svc.status(OWNER, src["app_id"])
    assert world.vercel.aliases[HOST] == preview
    assert (await _rec(src))["preview_repoint"] is None

    calls = len(world.vercel.calls)
    await svc.status(OWNER, src["app_id"])
    assert not [r for r in world.vercel.calls[calls:] if "/aliases" in r.url.path or r.method == "PATCH"]


async def test_a_failed_repoint_never_blocks_is_shown_and_stops_after_its_tries(world, caplog):
    src, preview = await _preview_on_its_host(world)
    world.vercel.alias_fails = True
    live = await _release(world, src)
    res = await svc.status(OWNER, src["app_id"])
    assert res["state"] == "live" and res["error"]["code"] == "preview_host_off"
    assert "not re-pointed" in caplog.text

    # Backing off: another read doesn't call Vercel for the alias.
    calls = len(world.vercel.calls)
    await svc.status(OWNER, src["app_id"])
    assert not [r for r in world.vercel.calls[calls:] if "/aliases" in r.url.path]

    for _ in range(10):
        await _settle(src, world)
    posts = [r for r in world.vercel.calls if r.method == "POST" and r.url.path.endswith("/aliases")]
    assert len(posts) == 1 + svc._REPOINT_TRIES  # the first alias, then the bound
    rec = await _rec(src)
    assert rec["preview_repoint"] is None and await svc.apps_repo.list_pending() == []
    assert world.vercel.aliases[HOST] == live

    # `up` tries again.
    world.vercel.alias_fails = False
    await svc.up(
        OWNER,
        name="crew",
        room_id="room_1",
        services=[],
        open_to="room",
        budget_usd=5,
        source_id=src["source_id"],
        actor=PERSON,
        origin={},
    )
    res = await svc.status(OWNER, src["app_id"])
    assert world.vercel.aliases[HOST] == preview and "error" not in res


async def test_a_failing_repoint_does_not_stop_the_next_publish(world):
    src, _ = await _preview_on_its_host(world)
    world.vercel.alias_fails = True
    await _release(world, src)
    await svc.status(OWNER, src["app_id"])
    src2 = await _source(SRC_V2, "c2")
    pub = await svc.publish(OWNER, src["app_id"], source_id=src2["source_id"], actor=PERSON)
    assert pub["state"] == "building" and world.vercel.created()[-1]["target"] == "production"


async def test_a_refused_token_at_the_repoint_goes_to_renewal(world, monkeypatch):
    src, preview = await _preview_on_its_host(world)
    live = await _release(world, src)
    rot = _dies_then_rotates(world, monkeypatch)
    await svc.status(OWNER, src["app_id"])
    rot.assert_awaited_once()
    assert world.vercel.aliases[HOST] == preview != live


async def test_a_production_first_deployment_is_rebuilt_not_recorded_as_the_preview(world):
    world.vercel.first_is_production = True
    src = await _source()
    await _up(src)
    await _all_active(world, src["app_id"])
    await svc.status(OWNER, src["app_id"])  # deploys dpl_1, which Vercel makes production
    world.vercel.finish("dpl_1")
    res = await svc.status(OWNER, src["app_id"])
    rec = await _rec(src)
    assert res["state"] == "building" and not rec.get("preview")
    assert rec["preview_pending"]["deployment_id"] == "dpl_2"
    assert "target" not in world.vercel.created()[1]
    world.vercel.finish("dpl_2")
    await svc.status(OWNER, src["app_id"])
    assert (await _rec(src))["preview"]["deployment_id"] == "dpl_2"


async def test_a_first_deployment_that_fails_as_production_is_rebuilt_not_reported(world):
    world.vercel.first_is_production = True
    src = await _source()
    await _up(src)
    await _all_active(world, src["app_id"])
    await svc.status(OWNER, src["app_id"])  # dpl_1: production, so it builds against the empty live database
    world.vercel.finish("dpl_1", "ERROR", 'relation "accounts" does not exist')
    res = await svc.status(OWNER, src["app_id"])
    rec = await _rec(src)
    assert res["state"] == "building" and not rec.get("error")
    assert rec["preview_pending"]["deployment_id"] == "dpl_2"


async def test_a_failed_production_first_deployment_stops_at_the_rebuild_limit(world):
    world.vercel.first_is_production = True
    src = await _source()
    await _up(src)
    await _all_active(world, src["app_id"])
    await svc.status(OWNER, src["app_id"])
    await svc.apps_repo.patch(src["app_id"], {"preview_rebuilds": svc._PREVIEW_REBUILDS})
    world.vercel.finish("dpl_1", "ERROR", "Type error")
    res = await svc.status(OWNER, src["app_id"])
    assert res["error"]["code"] == "build_failed" and len(world.vercel.created()) == 1


async def test_production_preview_rebuilds_are_bounded(world):
    world.vercel.first_is_production = True
    src = await _source()
    await _up(src)
    await _all_active(world, src["app_id"])
    await svc.status(OWNER, src["app_id"])
    await svc.apps_repo.patch(src["app_id"], {"preview_rebuilds": svc._PREVIEW_REBUILDS})
    world.vercel.finish("dpl_1")
    res = await svc.status(OWNER, src["app_id"])
    assert res["error"]["code"] == "preview_is_production" and len(world.vercel.created()) == 1


async def _two_versions_on_host(world):
    src, _ = await _preview_on_its_host(world)
    await _publish(src)
    world.vercel.finish("dpl_2")
    await svc.status(OWNER, src["app_id"])
    src2 = await _source(SRC_V2, "c2")
    await svc.publish(OWNER, src["app_id"], source_id=src2["source_id"], actor=PERSON)
    world.vercel.finish("dpl_3")
    await svc.status(OWNER, src["app_id"])
    return src


async def _two_versions(world):
    src, _, _ = await _ready_preview(world)
    await _publish(src)
    world.vercel.finish("dpl_2")
    await svc.status(OWNER, src["app_id"])
    src2 = await _source(SRC_V2, "c2")
    await svc.publish(OWNER, src["app_id"], source_id=src2["source_id"], actor=PERSON)
    world.vercel.finish("dpl_3")
    await svc.status(OWNER, src["app_id"])
    return src


def _assert_recovered_quietly(world, src, rot, caplog):
    rot.assert_awaited_once()
    assert world.secrets[(OWNER, f"apps/{src['app_id']}/vercel/prod")]["token"] == "vtok_NEW"
    assert world.vercel.calls[-1].headers["authorization"] == "Bearer vtok_NEW"
    loud = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and "apps:" in r.getMessage()]
    assert not loud, loud


@pytest.fixture
def info_log(caplog):
    caplog.set_level(logging.INFO, logger=svc.logger.name)
    return caplog


async def test_token_dead_at_the_deploy_recovers_through_one_rotation(world, monkeypatch, info_log):
    src, _, _ = await _settled_preview(world)
    rot = _dies_then_rotates(world, monkeypatch)
    res = await _publish(src)
    assert res.get("error") is None and res["state"] == "building"
    assert (await _rec(src))["credential_renewal"]["state"] == "complete"
    _assert_recovered_quietly(world, src, rot, info_log)
    assert "rotation: complete" in info_log.text  # below WARNING, and still findable


async def test_token_dead_at_the_poll_recovers_through_one_rotation(world, monkeypatch, info_log):
    from core.services import apps_sweeper

    src, _, _ = await _ready_preview(world)
    await _publish(src)
    world.vercel.finish("dpl_2")
    rot = _dies_then_rotates(world, monkeypatch)
    await apps_sweeper.sweep_once()
    rec = await _rec(src)
    assert rec["state"] == "live" and rec.get("publish_error") is None
    _assert_recovered_quietly(world, src, rot, info_log)


async def test_token_dead_at_rollback_recovers_through_one_rotation(world, monkeypatch, info_log):
    src = await _two_versions(world)
    rot = _dies_then_rotates(world, monkeypatch)
    res = await svc.rollback(OWNER, src["app_id"], version=None)
    assert res["error"]["code"] == "code_only" and res["version"] == 1
    assert world.vercel.paths()[-1] == ("POST", "/v1/projects/prj_1/rollback/dpl_2")
    _assert_recovered_quietly(world, src, rot, info_log)


async def test_token_dead_at_errors_recovers_through_one_rotation(world, monkeypatch, info_log):
    src, _, _ = await _ready_preview(world)
    rot = _dies_then_rotates(world, monkeypatch)
    assert await svc.errors(OWNER, src["app_id"]) == {"client": [], "server": []}
    assert world.vercel.calls[-1].url.path.endswith("/events")
    _assert_recovered_quietly(world, src, rot, info_log)


async def test_token_dead_at_the_preview_alias_is_not_swallowed(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    await svc.apps_repo.patch(
        src["app_id"], {"preview_domain": {"name": "crew-preview.apps.example.com", "verified": True}}
    )
    rot = _dies_then_rotates(world, monkeypatch)
    await svc.status(OWNER, src["app_id"])
    rot.assert_awaited_once()
    assert (await _rec(src))["preview_alias_for"] == "dpl_1"


async def test_two_concurrent_readers_rotate_once(world, monkeypatch):
    src = await _two_versions(world)
    rot = _dies_then_rotates(world, monkeypatch)
    results = await asyncio.gather(
        svc.errors(OWNER, src["app_id"]), svc.errors(OWNER, src["app_id"]), return_exceptions=True
    )
    rot.assert_awaited_once()
    assert world.secrets[(OWNER, f"apps/{src['app_id']}/vercel/prod")]["token"] == "vtok_NEW"
    assert any(isinstance(r, dict) for r in results)
    for r in results:  # one that lost the race says try again, never that a person must look
        if not isinstance(r, dict):
            assert isinstance(r, svc.AppsRefused) and r.code == svc._REFUSAL_CODE and "few minutes" in r.next
    assert isinstance(await svc.errors(OWNER, src["app_id"]), dict)
    rot.assert_awaited_once()


async def test_rollback_and_errors_on_a_token_still_dead_say_so_in_words(world, monkeypatch):
    src = await _two_versions(world)
    _renewal(monkeypatch, rotate="errored")
    world.vercel.refused = True
    with pytest.raises(svc.AppsRefused) as e:
        await svc.rollback(OWNER, src["app_id"], version=None)
    assert e.value.code == svc._REFUSAL_CODE and "Lightbulb's team" in e.value.next
    with pytest.raises(svc.AppsRefused) as e:
        await svc.errors(OWNER, src["app_id"])
    assert e.value.code == svc._REFUSAL_CODE
    assert (await _rec(src))["live_version"] == 2  # the rollback did not happen


async def test_destroy_and_remove_never_call_vercel(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    world.vercel.refused = True
    world.vercel.calls.clear()
    await svc.destroy(OWNER, src["app_id"], actor=PERSON)
    await svc.remove(OWNER, src["app_id"], confirm_name="crew", user_id="user_1")
    assert world.vercel.calls == []


def test_every_vercel_credential_goes_through_with_token():
    """A new caller of the app's Vercel credential has to come through here, and so past ``_with_token``."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(svc))
    fns = {f.name: f for f in ast.walk(tree) if isinstance(f, ast.AsyncFunctionDef | ast.FunctionDef)}

    def calls(fn):
        return [n for n in ast.walk(fn) if isinstance(n, ast.Call)]

    holders = {
        name
        for name, fn in fns.items()
        for c in calls(fn)
        if ast.unparse(c.func) == "_secret" and any(ast.unparse(a) == "'vercel'" for a in c.args)
    }
    assert holders == {"_progress", "publish", "rollback", "errors"}
    wrapped = {"_with_token", "_with_token_for_route"}
    assert "_steps" in {ast.unparse(c.func) for c in calls(fns["_progress"])}
    for name in ("_steps", "publish", "rollback", "errors"):
        assert wrapped & {ast.unparse(c.func) for c in calls(fns[name])}, name


@pytest.mark.parametrize(
    ("error", "dead"),
    [
        ({"code": "forbidden", "invalidToken": True}, True),
        ({"code": "forbidden"}, False),
        ({"code": "forbidden", "invalidToken": "true"}, False),
        ({"code": "forbidden", "invalidToken": False}, False),
        ({"code": "forbidden", "invalidToken": True, "saml": True}, False),
        ({"code": "forbidden", "invalidToken": True, "message": "SAML sign-in required"}, False),
    ],
)
def test_only_invalid_token_without_saml_means_dead(error, dead):
    with pytest.raises(vercel_client.VercelError) as e:
        vercel_client._raise(VERCEL, 403, {"error": error})
    assert e.value.invalid_token is dead


async def test_a_saml_refusal_never_rotates_and_is_reported_as_itself(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    rotate, reveal = _renewal(monkeypatch)
    body = {"error": {"code": "forbidden", "message": "Not authorized", "invalidToken": True, "saml": True}}
    _vercel_answers(world, monkeypatch, lambda req: httpx.Response(403, json=body))
    res = await _publish(src)
    rotate.assert_not_awaited()
    reveal.assert_not_awaited()
    assert res["error"]["code"] == "vercel_forbidden"


async def test_a_cancelled_rotation_that_landed_clears_its_hold_and_renews_again(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    _renewal(monkeypatch)
    monkeypatch.setattr(svc.api, "rotate_credentials", AsyncMock(side_effect=asyncio.CancelledError()))
    world.vercel.refused = True
    with pytest.raises(asyncio.CancelledError):
        await _publish(src)
    assert (await _rec(src))["credential_renewal"]["state"] == "pending"
    rotate, _ = _renewal(monkeypatch, reveal=_new_token_accepted(world))
    res = await _publish(src)
    rotate.assert_not_awaited()  # nothing sent to Stripe
    assert res.get("error") is None
    assert (await _rec(src))["credential_renewal"]["state"] == "complete"
    # That token dies in its turn: the app is not stuck.
    await svc.apps_repo.patch(src["app_id"], {"publish_pending": None})
    await _age_renewal(src)
    rotate, _ = _renewal(monkeypatch, reveal=lambda a, r: {"configuration": NEW})
    world.vercel.refused = True
    await _publish(src)
    rotate.assert_awaited_once()


async def test_a_stored_token_that_works_clears_a_hold_from_a_status_read(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    for state in ("pending", "errored", "needs_reveal"):
        await svc.apps_repo.patch(src["app_id"], {"credential_renewal": {"state": state, "at": "2026-10-30"}})
        rotate, reveal = _renewal(monkeypatch)
        st = await svc.status(OWNER, src["app_id"])
        assert st.get("error") is None
        assert (await _rec(src))["credential_renewal"]["state"] == "complete"
        rotate.assert_not_awaited()
        reveal.assert_not_awaited()


async def test_an_old_needs_reveal_whose_token_died_rotates_instead_of_sticking(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    await svc.apps_repo.patch(src["app_id"], {"credential_renewal": {"state": "needs_reveal", "at": "2026-10-30"}})
    rot = _dies_then_rotates(world, monkeypatch)
    st = await svc.status(OWNER, src["app_id"])
    rot.assert_awaited_once()
    assert st.get("error") is None and (await _rec(src))["credential_renewal"]["state"] == "complete"


@pytest.mark.parametrize("exc", [svc.api.TransientProvisioningError("rate_limited", "429"), RuntimeError("down")])
async def test_a_rotation_that_never_landed_stays_held(world, monkeypatch, exc):
    src, _, _ = await _settled_preview(world)
    rotate, reveal = _renewal(monkeypatch, rotate=exc)
    world.vercel.refused = True
    await _publish(src)
    await _age_renewal(src, hours=24)
    st = await svc.status(OWNER, src["app_id"])
    rotate.assert_awaited_once()
    assert st["error"]["code"] in ("credential_renewal_stuck", svc._REFUSAL_CODE)
    assert (await _rec(src))["credential_renewal"]["state"] == "pending"
    assert svc._renewal_hold(await _rec(src))["code"] == "credential_renewal_stuck"


async def test_a_new_token_refused_once_then_accepted_is_not_a_hold(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    rot = _dies_then_rotates(world, monkeypatch)
    real, seen = svc._probe, []

    async def probe(app, creds):
        if creds["token"] != "vtok_NEW":  # the check before the migrations, on the token that died
            return await real(app, creds)
        seen.append(creds["token"])
        if len(seen) == 1:
            return vercel_client.VercelError("forbidden", "Not authorized", 403, invalid_token=True)
        return await real(app, creds)

    monkeypatch.setattr(svc, "_probe", probe)
    res = await _publish(src)
    rot.assert_awaited_once()
    assert seen[:2] == ["vtok_NEW", "vtok_NEW"] and res.get("error") is None
    assert (await _rec(src))["credential_renewal"]["state"] == "complete"


async def test_a_new_token_refused_twice_holds_until_it_works(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    rotate, _ = _renewal(monkeypatch, reveal=[{"configuration": VERCEL}, {"configuration": NEW}])
    world.vercel.refused = True
    await _publish(src)
    assert (await _rec(src))["credential_renewal"]["state"] == "errored"
    world.vercel.refused = False
    await svc.status(OWNER, src["app_id"])
    rotate.assert_awaited_once()
    rec = await _rec(src)
    assert rec["credential_renewal"]["state"] == "complete" and rec.get("publish_error") is None


async def test_a_stored_refusal_on_a_deleted_deployment_does_not_block_a_deploy(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    await svc.apps_repo.patch(src["app_id"], {"publish_error": svc._refusal(await _rec(src))})
    rotate, _ = _renewal(monkeypatch)
    gone = {"error": {"code": "not_found", "message": "gone"}}
    _vercel_answers(
        world,
        monkeypatch,
        lambda req: httpx.Response(404, json=gone) if req.url.path == "/v13/deployments/dpl_1" else None,
    )
    res = await _publish(src)
    rotate.assert_not_awaited()
    assert res.get("error") is None and (await _rec(src)).get("publish_pending")


async def test_a_renewal_never_writes_back_a_secret_that_was_removed(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    rows = await svc._rows(await _rec(src))
    del world.secrets[(OWNER, f"apps/{src['app_id']}/vercel/prod")]
    rotate, reveal = _renewal(monkeypatch, reveal=lambda a, r: {"configuration": NEW})
    assert await svc._renew_vercel(dict(await _rec(src)), rows, VERCEL) is None
    rotate.assert_not_awaited()
    reveal.assert_not_awaited()
    assert (OWNER, f"apps/{src['app_id']}/vercel/prod") not in world.secrets


async def test_a_rotation_in_flight_is_not_a_hold_until_it_has_had_its_time(world):
    now = datetime.now(UTC)
    young = {"credential_renewal": {"state": "pending", "at": now.isoformat()}}
    old = {"credential_renewal": {"state": "pending", "at": (now - timedelta(minutes=6)).isoformat()}}
    assert svc._renewal_hold(young) is None and "few minutes" in svc._refusal(young)["next"]
    assert svc._renewal_hold(old)["code"] == "credential_renewal_stuck"


async def test_a_changed_token_found_outside_the_steps_settles_the_hold_there(world, monkeypatch):
    """No later read has to come along: by then this token may be dead and the hold stuck."""
    src, _, _ = await _settled_preview(world)
    stale = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    await svc.apps_repo.patch(src["app_id"], {"credential_renewal": {"state": "pending", "at": stale}})
    rotate, _ = _renewal(monkeypatch, reveal=_new_token_accepted(world))
    world.vercel.refused = True
    assert await svc.errors(OWNER, src["app_id"]) == {"client": [], "server": []}
    rotate.assert_not_awaited()
    assert (await _rec(src))["credential_renewal"]["state"] == "complete"


# --- putting a version back across a sharing change (review of #4521) -------------------------


async def _land(world, app_id):
    """Finish the pending production build (a sharing change also rebuilds the preview, after it)."""
    world.vercel.finish((await svc.apps_repo.get(app_id))["publish_pending"]["deployment_id"])
    return await svc.status(OWNER, app_id)


async def _closed_after_public(world):
    """v1 and v2 built while public; the owner closes it, which lands v3 as room. Returns the source."""
    src, _, _ = await _ready_preview(world)
    await svc.update_settings(OWNER, src["app_id"], open_to="public")
    await svc.status(OWNER, src["app_id"])
    await _go_live(world, src)
    await _go_live(world, await _source(SRC_V2, "c2"))
    await svc.update_settings(OWNER, src["app_id"], open_to="room")
    assert (await _land(world, src["app_id"]))["live_version"] == 3
    return src


def _instant_rollbacks(world):
    return [p for _, p in world.vercel.paths() if "/rollback/" in p]


async def test_versions_are_stamped_with_who_could_open_them(world):
    src = await _closed_after_public(world)
    app = await svc.apps_repo.get(src["app_id"])
    assert [v["open_to"] for v in app["versions"]] == ["public", "public", "room"]


async def test_put_back_over_a_sharing_change_rebuilds_with_the_current_door(world):
    """The deployment of v1 has the door off in its env: serving it again would reopen a closed app."""
    src = await _closed_after_public(world)
    n = len(world.vercel.created())
    res = await svc.rollback(OWNER, src["app_id"], version=1)
    assert _instant_rollbacks(world) == []
    prod = world.vercel.created()[n:]
    assert len(prod) == 1 and prod[0]["target"] == "production" and {f["file"] for f in prod[0]["files"]} == set(SRC_V1)
    assert res["rebuilding"] is True and res["version"] == 3 and "few minutes" in res["error"]["message"]
    app = await svc.apps_repo.get(src["app_id"])
    assert app["live_version"] == 3 and app["publish_pending"]["source_id"] == src["source_id"]  # v3 serves until then
    st = await _land(world, src["app_id"])
    assert st["live_version"] == 4 and [v["commit"] for v in st["versions"]] == ["c1", "c2", "c2", "c1"]
    app = await svc.apps_repo.get(src["app_id"])
    assert app["versions"][-1]["open_to"] == "room" and app["open_to"] == "room"
    assert app["live_source_id"] == src["source_id"]


async def test_opening_an_app_then_putting_back_rebuilds_too(world):
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    await svc.update_settings(OWNER, src["app_id"], open_to="public")
    await _land(world, src["app_id"])
    assert (await svc.apps_repo.get(src["app_id"]))["live_version"] == 2
    assert (await svc.rollback(OWNER, src["app_id"], version=1))["rebuilding"] is True
    assert _instant_rollbacks(world) == []


async def test_put_back_with_the_same_sharing_is_instant(world):
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    await _go_live(world, await _source(SRC_V2, "c2"))
    n = len(world.vercel.created())
    res = await svc.rollback(OWNER, src["app_id"], version=1)
    assert len(_instant_rollbacks(world)) == 1 and len(world.vercel.created()) == n
    assert "rebuilding" not in res and res["error"]["message"] == "Version 1 is live again."


async def test_a_version_with_no_stamp_counts_as_different(world):
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    await _go_live(world, await _source(SRC_V2, "c2"))
    app = await svc.apps_repo.get(src["app_id"])
    versions = [{k: v for k, v in x.items() if k != "open_to"} for x in svc.plain(app["versions"])]
    await svc.apps_repo.patch(src["app_id"], {"versions": versions})
    assert (await svc.rollback(OWNER, src["app_id"], version=1))["rebuilding"] is True
    assert _instant_rollbacks(world) == []


async def test_put_back_refuses_rather_than_reopen_when_the_source_is_gone(world):
    src = await _closed_after_public(world)
    app = svc.plain(await svc.apps_repo.get(src["app_id"]))
    del app["sources"][src["source_id"]]
    await svc.apps_repo.patch(src["app_id"], {"sources": app["sources"]})
    n = len(world.vercel.created())
    with pytest.raises(svc.AppsRefused) as e:
        await svc.rollback(OWNER, src["app_id"], version=1)
    assert e.value.code == "version_unavailable"
    assert _instant_rollbacks(world) == [] and len(world.vercel.created()) == n
    assert await svc.apps_repo.take_lease(src["app_id"], seconds=1)  # a refusal frees the lease


async def test_rollback_holds_the_lease_and_says_busy_without_it(world):
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    await _go_live(world, await _source(SRC_V2, "c2"))
    theirs = await svc.apps_repo.take_lease(src["app_id"], seconds=60)
    with pytest.raises(svc.AppsRefused) as e:
        await svc.rollback(OWNER, src["app_id"], version=1)
    assert e.value.code == "busy" and _instant_rollbacks(world) == []
    assert await svc.apps_repo.release_lease(src["app_id"], theirs)
    await svc.rollback(OWNER, src["app_id"], version=1)
    assert await svc.apps_repo.take_lease(src["app_id"], seconds=1)  # released again


@pytest.mark.parametrize("across_sharing", [False, True])
async def test_rollback_clears_a_failed_publish(world, across_sharing):
    if across_sharing:
        src = await _closed_after_public(world)
    else:
        src, _, _ = await _ready_preview(world)
        await _go_live(world, src)
        await _go_live(world, await _source(SRC_V2, "c2"))
    await svc.apps_repo.patch(src["app_id"], {"publish_error": {"code": "build_failed", "message": "no"}})
    res = await svc.rollback(OWNER, src["app_id"], version=1)
    assert (await svc.apps_repo.get(src["app_id"])).get("publish_error") is None
    assert res["error"]["code"] == "code_only"


def _previous(app):
    return (svc._previous_version(svc.plain(app)) or {}).get("n")


async def test_previous_version_is_what_was_live_not_arithmetic(world):
    """v3 is broken, the person puts back v2, v4 is broken too: "previous" must not be the v3 they fled."""
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    await _go_live(world, await _source(SRC_V2, "c2"))
    await _go_live(world, await _source({**SRC_V2, "app/page.tsx": "v3"}, "c3"))
    assert _previous(await svc.apps_repo.get(src["app_id"])) == 2
    await svc.rollback(OWNER, src["app_id"], version=2)
    assert _previous(await svc.apps_repo.get(src["app_id"])) == 1  # back again goes further back
    await _go_live(world, await _source({**SRC_V2, "app/page.tsx": "v4"}, "c4"))
    app = await svc.apps_repo.get(src["app_id"])
    assert app["live_version"] == 4 and _previous(app) == 2
    await svc.rollback(OWNER, src["app_id"], version=None)  # the builder's "previous" is the same one
    assert (await svc.apps_repo.get(src["app_id"]))["live_version"] == 2


async def test_previous_version_after_a_rebuilt_put_back_skips_what_was_fled(world):
    src = await _closed_after_public(world)  # v1, v2 public; v3 room
    await svc.rollback(OWNER, src["app_id"], version=2)
    # a sharing change while it rebuilds replaces the build and keeps the history
    await svc.update_settings(OWNER, src["app_id"], open_to="workspace")
    await _land(world, src["app_id"])
    app = await svc.apps_repo.get(src["app_id"])
    assert app["live_version"] == 4 and app["versions"][-1]["open_to"] == "workspace"
    assert _previous(app) == 1  # v4 is v2's source: "previous" is what v2 replaced, not the v3 they left


async def test_previous_version_of_an_unstamped_record_falls_back_to_the_number_before(world):
    versions = [{"n": 1, "at": "a"}, {"n": 2, "at": "b"}]
    assert svc._previous_version({"versions": versions, "live_version": 2})["n"] == 1
    assert svc._previous_version({"versions": versions, "live_version": 1}) is None
    assert svc._previous_version({"versions": [], "live_version": None}) is None


# --- review 3: the first real run ----------------------------------------------------------------
DEAD_T1 = {"token": "vca_T1_DEAD"}
LIVE_T2 = {"token": "vca_T2_LIVE"}


def _real_run(world, monkeypatch):
    """Vercel: only T2 works. Stripe: reveal returns T1 (dead, differs from stored) until rotated."""
    held = {"configuration": dict(DEAD_T1)}

    async def rotate(account, resource):
        held["configuration"] = dict(LIVE_T2)
        return {"status": "complete"}

    rot = AsyncMock(side_effect=rotate)
    rev = AsyncMock(side_effect=lambda a, r: dict(held))
    monkeypatch.setattr(svc.api, "rotate_credentials", rot)
    monkeypatch.setattr(svc.api, "reveal_access_configuration", rev)
    return rot, rev


@pytest.fixture
def token_gate(world, monkeypatch):
    """Refuse every token but T2, by the Authorization header, the way production does."""
    orig = world.vercel.handler
    armed = {"on": False}

    def gate(req):
        if armed["on"] and req.headers["authorization"] != f"Bearer {LIVE_T2['token']}":
            world.vercel.calls.append(req)
            return httpx.Response(403, json=DEAD_403)
        return orig(req)

    world.vercel.handler = gate
    from core.services import vercel_client

    monkeypatch.setattr(vercel_client, "_transport", httpx.MockTransport(gate))
    return armed


async def test_Y1_first_publish_settled_app_stored_dead_reveal_dead_rotates_and_deploys(world, monkeypatch, token_gate):
    src, _, _ = await _settled_preview(world)
    rot, rev = _real_run(world, monkeypatch)
    token_gate["on"] = True
    world.vercel.calls.clear()
    res = await _publish(src)
    print("RES", res.get("error"), res["state"], "rot", rot.await_count, "rev", rev.await_count)
    print("TOKEN", world.secrets[(OWNER, f"apps/{src['app_id']}/vercel/prod")]["token"])
    print("CALLS", [(r.method, r.url.path, r.headers["authorization"][-12:]) for r in world.vercel.calls])
    rot.assert_awaited_once()
    assert res.get("error") is None and res["state"] == "building"
    assert world.secrets[(OWNER, f"apps/{src['app_id']}/vercel/prod")]["token"] == LIVE_T2["token"]
    assert world.vercel.created()[-1]["target"] == "production"
    assert world.vercel.calls[-1].headers["authorization"] == f"Bearer {LIVE_T2['token']}"


async def test_Y2_same_but_a_step_calls_vercel_inside_progress(world, monkeypatch, token_gate):
    src, _, _ = await _settled_preview(world)
    await svc.apps_repo.patch(src["app_id"], {"env_fp": {}})  # a read that has to talk to Vercel
    rot, rev = _real_run(world, monkeypatch)
    token_gate["on"] = True
    world.vercel.calls.clear()
    res = await _publish(src)
    print("RES", res.get("error"), res["state"], "rot", rot.await_count)
    rot.assert_awaited_once()
    assert res.get("error") is None and res["state"] == "building"


async def test_Y3_second_click_after_the_first_failure(world, monkeypatch, token_gate):
    src, _, _ = await _settled_preview(world)
    rot, rev = _real_run(world, monkeypatch)
    token_gate["on"] = True
    first = await _publish(src)
    print("FIRST", first.get("error"), first["state"], "rot", rot.await_count)
    second = await _publish(src)
    print("SECOND", second.get("error"), second["state"], "rot", rot.await_count)
    assert second.get("error") is None and second["state"] == "building"
    rot.assert_awaited_once()
    assert (await _rec(src)).get("publish_error") is None


@pytest.mark.parametrize("state", ["pending", "needs_reveal"])
async def test_Y4_a_leftover_state_from_an_hour_ago_with_a_different_dead_reveal(world, monkeypatch, token_gate, state):
    src, _, _ = await _settled_preview(world)
    monkeypatch.setattr(svc, "_SETTLE_S", 0)
    await svc.apps_repo.patch(
        src["app_id"], {"credential_renewal": {"state": state, "at": "2026-10-30T20:12:00+00:00"}}
    )
    rot, rev = _real_run(world, monkeypatch)
    token_gate["on"] = True
    res = await _publish(src)
    res2 = await _publish(src)
    print(
        "Y4",
        state,
        res.get("error"),
        "|",
        res2.get("error"),
        "rot",
        rot.await_count,
        (await _rec(src))["credential_renewal"],
    )
    assert res2.get("error") is None or res.get("error") is None


async def test_Y5_sweeper_idle_app_no_calls(world, monkeypatch):
    from core.services import apps_sweeper

    src, _, _ = await _settled_preview(world)
    rotate, reveal = _renewal(monkeypatch, reveal=lambda a, r: {"configuration": dict(DEAD_T1)})
    world.vercel.refused = True
    world.vercel.calls.clear()
    assert await apps_sweeper.sweep_once() == 0
    rotate.assert_not_awaited()
    reveal.assert_not_awaited()
    assert world.vercel.calls == []


async def test_Y6_rollback_rebuild_retry_uses_fresh_token_and_one_deployment(world, monkeypatch):
    src = await _two_versions(world)
    await svc.update_settings(OWNER, src["app_id"], open_to="public") if False else None
    rec = await _rec(src)
    await svc.apps_repo.patch(
        src["app_id"], {"open_to": "public" if rec["open_to"] != "public" else "room", "publish_pending": None}
    )
    rot = _dies_then_rotates(world, monkeypatch)
    res = await svc.rollback(OWNER, src["app_id"], version=None)
    print("Y6", res["error"], res.get("rebuilding"))
    assert res.get("rebuilding") is True
    rot.assert_awaited_once()
    assert len(world.vercel.created()) == 1
    assert world.vercel.calls[-1].headers["authorization"] == "Bearer vtok_NEW"
    assert (await _rec(src))["publish_pending"]["deployment_id"]


async def test_Y7_rollback_first_real_run(world, monkeypatch, token_gate):
    src = await _two_versions(world)
    rot, _ = _real_run(world, monkeypatch)
    token_gate["on"] = True
    try:
        res = await svc.rollback(OWNER, src["app_id"], version=None)
    except svc.AppsRefused as e:
        print("Y7 refused", e.code, str(e))
        raise
    assert res["error"]["code"] == "code_only"
    rot.assert_awaited_once()


async def test_a_dead_token_that_cannot_be_renewed_leaves_the_live_schema_alone(world, monkeypatch, token_gate):
    """The token is checked before the migrations run: otherwise the schema moves and the deploy fails."""
    src, _, _ = await _ready_preview(world)
    world.vercel.refused = True
    monkeypatch.setattr(svc.api, "rotate_credentials", AsyncMock(return_value={"status": "errored"}))
    res = await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    assert res.get("error")
    world.apply.assert_not_awaited()
    assert not (await svc.apps_repo.get(src["app_id"])).get("published_migrations")


# --- the failed build's log reaches the agent ---------------------------------------------------


async def _first_build_fails(world, monkeypatch, events):
    src = await _source()
    await _up(src)
    await _all_active(world, src["app_id"])
    await svc.status(OWNER, src["app_id"])  # deploys
    monkeypatch.setattr(svc.vercel, "deployment_events", events)
    world.vercel.finish(f"dpl_{len(world.vercel.created())}", "ERROR", "Build failed")
    return src, await svc.status(OWNER, src["app_id"])


async def test_failed_preview_stores_scrubbed_capped_log_tail(world, monkeypatch):
    cookie = "COOKIE-SECRET-VALUE"
    events = AsyncMock(
        return_value=[{"message": "x" * 5000}, {"message": f"Type error: token={VERCEL['token']} {cookie}"}]
    )
    world.secrets[(OWNER, "apps/%s/lightbulb" % (await _source())["app_id"])] = {"cookie_secret": cookie}
    src, res = await _first_build_fails(world, monkeypatch, events)
    msg = res["error"]["message"]
    assert msg.startswith("Build failed\n") and "Type error: token=[redacted]" in msg
    assert VERCEL["token"] not in msg and cookie not in msg
    assert len(msg) <= len("Build failed\n") + 2000


async def test_failed_preview_keeps_error_when_log_fetch_fails(world, monkeypatch):
    src, res = await _first_build_fails(world, monkeypatch, AsyncMock(side_effect=RuntimeError("boom")))
    assert res["state"] == "failed" and res["error"]["code"] == "build_failed"
    assert res["error"]["message"] == "Build failed"


async def test_a_hung_build_log_does_not_hold_the_read(world, monkeypatch):
    async def hang(*_a, **_k):
        await asyncio.sleep(60)

    monkeypatch.setattr(svc, "_LOG_WAIT_S", 0.01)
    src, res = await _first_build_fails(world, monkeypatch, hang)
    assert res["state"] == "failed" and res["error"]["message"] == "Build failed"


async def test_a_build_log_never_stores_a_connection_string(world, monkeypatch):
    events = AsyncMock(return_value=[{"message": f"cannot reach {DEV_DB} or postgresql://u:p@h/db"}])
    src, res = await _first_build_fails(world, monkeypatch, events)
    for secret in (DEV_DB, "DEVPW", "u:p@h"):
        assert secret not in res["error"]["message"]
    assert "[redacted]" in res["error"]["message"]


async def test_a_refused_token_reading_the_build_log_is_not_swallowed(world, monkeypatch):
    app = {"owner_id": OWNER, "app_id": "app_x"}
    dead = svc.vercel.VercelError("forbidden", "no", 403, invalid_token=True)
    monkeypatch.setattr(svc.vercel, "deployment_events", AsyncMock(side_effect=dead))
    with pytest.raises(svc.vercel.VercelError):
        await svc._failure(app, VERCEL, {"deployment_id": "dpl_x"}, {"errorMessage": "Build failed"})


async def test_failed_publish_stores_log_tail(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    monkeypatch.setattr(svc.vercel, "deployment_events", AsyncMock(return_value=[{"message": "Module not found"}]))
    world.vercel.finish(f"dpl_{len(world.vercel.created())}", "ERROR", "Build failed")
    await svc.status(OWNER, src["app_id"])
    app = await svc.apps_repo.get(src["app_id"])
    assert app["publish_error"]["message"] == "Build failed\nModule not found"


async def test_errors_reads_a_failed_first_build_with_no_preview(world, monkeypatch):
    events = AsyncMock(return_value=[{"at": 7, "message": "Module not found"}])
    src, _ = await _first_build_fails(world, monkeypatch, events)
    monkeypatch.setattr(svc.vercel, "runtime_logs", AsyncMock(return_value=[]))
    out = await svc.errors(OWNER, src["app_id"])
    assert out["server"] and out["server"][-1]["message"] == "Module not found"


async def test_errors_reads_the_failed_preview_not_the_live_version(world, monkeypatch):
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    src2 = await _source(SRC_V2, "c2")
    await _up(src2)
    await svc.status(OWNER, src["app_id"])  # deploys the new preview
    failed = f"dpl_{len(world.vercel.created())}"
    monkeypatch.setattr(svc.vercel, "deployment_events", AsyncMock(return_value=[]))
    world.vercel.finish(failed, "ERROR", "Type error")
    await svc.status(OWNER, src["app_id"])
    logs = AsyncMock(return_value=[])
    monkeypatch.setattr(svc.vercel, "runtime_logs", logs)
    await svc.errors(OWNER, src["app_id"])
    assert logs.await_args.args[1] == failed


async def test_errors_ignores_a_publish_failure_older_than_a_healthy_preview(world, monkeypatch):
    src, _, preview_dep = await _ready_preview(world)
    await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    monkeypatch.setattr(svc.vercel, "deployment_events", AsyncMock(return_value=[]))
    world.vercel.finish(f"dpl_{len(world.vercel.created())}", "ERROR", "Build failed")
    await svc.status(OWNER, src["app_id"])
    app = await svc.apps_repo.get(src["app_id"])
    assert app["publish_error"]["deployment_id"]
    # a newer preview comes up healthy
    await svc.apps_repo.patch(
        src["app_id"], {"preview": {**app["preview"], "deployment_id": "dpl_new", "ready_at": "2999-01-01T00:00:00Z"}}
    )
    logs = AsyncMock(return_value=[])
    monkeypatch.setattr(svc.vercel, "runtime_logs", logs)
    await svc.errors(OWNER, src["app_id"])
    assert logs.await_args.args[1] == "dpl_new"


async def test_drive_steps_the_app_until_its_preview_is_up_and_once_per_app():
    states = iter(["provisioning", "building", "ready", "ready"])
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    with (
        patch.object(svc.asyncio, "sleep", fake_sleep),
        patch.object(svc, "status", AsyncMock(side_effect=lambda o, a: {"state": next(states)})) as status,
    ):
        svc.drive(OWNER, "app_1")
        svc.drive(OWNER, "app_1")  # a second approval read while the first drive runs
        await asyncio.gather(*list(svc._drives.values()))
    assert status.await_count == 3  # stopped early on ready
    assert len(sleeps) == 3
    assert svc._drives == {}


async def test_drive_follows_stripes_schedule_with_jitter_and_stops_after_fifteen_minutes():
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    with (
        patch.object(svc.asyncio, "sleep", fake_sleep),
        patch.object(svc, "status", AsyncMock(return_value={"state": "provisioning"})) as status,
    ):
        await svc._drive(OWNER, "app_1")
    for got, want in zip(sleeps, (1, 2, 4, 8, 16, 30, 30), strict=False):
        assert want * 0.9 <= got <= want * 1.1
    assert svc._DRIVE_WAITS == (1, 2, 4, 8, 16, 30)
    assert len(sleeps) == status.await_count == 1 + (900 - 61) // 30 + 5  # 6 steps = 61 s, then every 30 s
    assert sum(sleeps) <= 900 * 1.1


# --- the app owns its run: progress, linked runs, stuck -----------------------------------------

_REAL_WATCH = svc.watch  # the autouse ``watched`` fixture replaces it


def _ago(minutes: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def room(monkeypatch):
    """Runs by id, and the lines posted into the room."""
    runs: dict = {}
    lines: list[str] = []
    open_runs: list[dict] = []

    async def create_message(**kw):
        lines.append(kw["text"])
        return {"messageId": f"m{len(lines)}"}, True

    monkeypatch.setattr(svc.messaging_repo, "get_run", AsyncMock(side_effect=lambda o, r: runs.get(r)))
    monkeypatch.setattr(svc.messaging_repo, "create_message", create_message)
    monkeypatch.setattr(svc.messaging_repo, "list_open_runs_for_room", AsyncMock(side_effect=lambda o, r: open_runs))
    return SimpleNamespace(runs=runs, lines=lines, open_runs=open_runs)


async def _building(world, run_ids=("run_1",), quiet_min=6.0):
    """The agent uploaded its source and went quiet for ``quiet_min`` minutes without running `up`."""
    src = await _source()
    await svc.status(OWNER, src["app_id"])
    await svc.apps_repo.patch(src["app_id"], {"progress_at": _ago(quiet_min), "run_ids": list(run_ids)})
    return src["app_id"]


STUCK_LINE = "Crew stopped at building: nothing has moved for 5 minutes. Ask the agent here to pick it up again."


async def test_a_status_read_with_nothing_moving_does_not_stamp_progress(world, watched):
    src = await _source()
    await _up(src)
    await svc.apps_repo.patch(src["app_id"], {"progress_at": "2026-11-01T00:00:00+00:00"})
    view = await svc.status(OWNER, src["app_id"])
    assert view["state"] == "awaiting_approval" and view["stuck"] is None
    assert (await svc.apps_repo.get(src["app_id"]))["progress_at"] == "2026-11-01T00:00:00+00:00"
    assert watched[-1] == src["app_id"]  # a read re-arms the watch (lost on a restart)

    await _all_active(world, src["app_id"])  # the rows really move: that is progress
    await svc.status(OWNER, src["app_id"])
    assert (await svc.apps_repo.get(src["app_id"]))["progress_at"] > "2026-11-01T00:00:00+00:00"


async def test_a_deployment_that_finishes_on_a_read_stamps_progress(world):
    src = await _source()
    await _up(src)
    await _all_active(world, src["app_id"])
    await svc.status(OWNER, src["app_id"])  # deploys
    await svc.apps_repo.patch(src["app_id"], {"progress_at": "2026-11-01T00:00:00+00:00"})
    await svc.status(OWNER, src["app_id"])  # still QUEUED: nothing moved
    assert (await svc.apps_repo.get(src["app_id"]))["progress_at"] == "2026-11-01T00:00:00+00:00"
    world.vercel.finish(f"dpl_{len(world.vercel.created())}")
    assert (await svc.status(OWNER, src["app_id"]))["state"] == "ready"
    assert (await svc.apps_repo.get(src["app_id"]))["progress_at"] > "2026-11-01T00:00:00+00:00"


async def test_up_stamps_progress_and_links_the_header_run(world, watched):
    src = await _source()
    agent = svc.BuilderCaller(owner_id=OWNER, actor_id="agt_1", actor_kind="agent", run_id="run_1")
    before = svc.utc_now_iso()
    for run_id in ("run_1", "run_1", "run_2", None):
        await svc.up(
            OWNER,
            name="crew",
            room_id="room_1",
            services=["database"],
            open_to="room",
            budget_usd=20,
            source_id=src["source_id"],
            actor=svc.BuilderCaller(**{**agent.__dict__, "run_id": run_id}),
            origin={"kind": "idea"},
        )
    app = await svc.apps_repo.get(src["app_id"])
    assert app["progress_at"] >= before
    assert app["run_ids"] == ["run_1", "run_2"]  # once each, newest last; no header changes nothing
    assert src["app_id"] in watched
    runs = [f"run_{n}" for n in range(12)]
    fields = svc._progress_fields({"run_ids": runs[:10]}, "run_0")
    assert fields["run_ids"] == [*runs[1:10], "run_0"]
    assert svc._progress_fields({"run_ids": runs[:10]}, "run_11")["run_ids"] == [*runs[1:10], "run_11"]


async def test_stuck_after_five_quiet_minutes_with_no_open_run_posts_once(world, room):
    app_id = await _building(world)
    room.runs["run_1"] = {"status": "ended", "lastHeartbeatAt": _ago(10)}
    assert await svc._look(OWNER, app_id) is True
    app = await svc.apps_repo.get(app_id)
    assert app["stuck"]["step"] == "building"
    assert room.lines == [STUCK_LINE]
    assert (await svc.status(OWNER, app_id))["stuck"]["step"] == "building"
    assert await svc._look(OWNER, app_id) is True  # already said: nothing more
    assert room.lines == [STUCK_LINE]


async def test_not_stuck_before_five_minutes_or_while_a_linked_run_is_working(world, room):
    app_id = await _building(world, quiet_min=4)
    assert await svc._look(OWNER, app_id) is False
    await svc.apps_repo.patch(app_id, {"progress_at": _ago(30), "run_ids": ["run_old", "run_1"]})
    room.runs["run_1"] = {"status": "started", "lastHeartbeatAt": _ago(1)}
    assert await svc._look(OWNER, app_id) is False
    room.runs["run_1"] = {"status": "started", "lastHeartbeatAt": _ago(6)}  # open, but its heartbeat is stale
    assert await svc._look(OWNER, app_id) is True
    assert room.lines == [STUCK_LINE]


async def test_waiting_on_a_person_is_never_stuck(world, room):
    src = await _source()
    await _up(src)
    await svc.apps_repo.patch(src["app_id"], {"progress_at": _ago(180), "run_ids": ["run_1"]})
    assert await svc._look(OWNER, src["app_id"]) is False  # awaiting_approval
    await _all_active(world, src["app_id"])
    world.rows["pwf_2"]["state"] = "awaiting_user_action"
    assert await svc._look(OWNER, src["app_id"]) is False
    assert room.lines == [] and (await svc.apps_repo.get(src["app_id"])).get("stuck") is None


async def test_without_linked_runs_an_app_is_stuck_only_after_twenty_minutes(world, room):
    app_id = await _building(world, run_ids=(), quiet_min=19)
    assert await svc._look(OWNER, app_id) is False
    await svc.apps_repo.patch(app_id, {"progress_at": _ago(21)})
    assert await svc._look(OWNER, app_id) is True
    assert room.lines == [STUCK_LINE]


async def test_a_second_instance_reading_the_same_quiet_app_posts_nothing(world, room, monkeypatch):
    app_id = await _building(world)
    stale = await svc.apps_repo.get(app_id, consistent_read=True)
    assert await svc._look(OWNER, app_id) is True
    monkeypatch.setattr(svc.apps_repo, "get", AsyncMock(return_value=stale))  # it read before the first wrote
    assert await svc._look(OWNER, app_id) is False  # its write fails: no line, and it keeps watching
    assert room.lines == [STUCK_LINE]


async def test_progress_clears_stuck(world, room):
    app_id = await _building(world)
    await svc._look(OWNER, app_id)
    assert (await svc.apps_repo.get(app_id))["stuck"]
    src = await _source(SRC_V2, "c2")
    assert (await svc.apps_repo.get(app_id))["stuck"] is None
    await _up(src)
    assert (await svc.status(OWNER, app_id))["stuck"] is None


async def test_the_watch_is_one_per_app_and_stops_when_there_is_nothing_left(monkeypatch):
    looks = iter([False, False, True])
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    with (
        patch.object(svc.asyncio, "sleep", fake_sleep),
        patch.object(svc, "_look", AsyncMock(side_effect=lambda o, a: next(looks))) as look,
    ):
        _REAL_WATCH(OWNER, "app_1")
        _REAL_WATCH(OWNER, "app_1")
        await asyncio.gather(*list(svc._watches.values()))
    assert look.await_count == 3 and sleeps == [60, 60, 60]
    assert svc._watches == {}
    sleeps.clear()
    with (
        patch.object(svc.asyncio, "sleep", fake_sleep),
        patch.object(svc, "_look", AsyncMock(return_value=False)),
    ):
        await svc._watch(OWNER, "app_1")
    assert len(sleeps) == 180  # three hours


async def _deploying(world, quiet_min=6.0):
    src = await _source()
    await _up(src)
    await _all_active(world, src["app_id"])
    await svc.status(OWNER, src["app_id"])  # the preview build starts
    await svc.apps_repo.patch(src["app_id"], {"progress_at": _ago(quiet_min), "run_ids": ["run_1"]})
    return src["app_id"]


async def test_a_deployment_vercel_still_builds_is_never_stuck(world, room):
    app_id = await _deploying(world, quiet_min=60)
    assert await svc._look(OWNER, app_id) is False
    assert room.lines == [] and (await svc.apps_repo.get(app_id)).get("stuck") is None


async def test_a_finished_preview_nobody_read_is_not_reported_stuck(world, room):
    app_id = await _deploying(world)
    world.vercel.finish(f"dpl_{len(world.vercel.created())}")
    assert await svc._look(OWNER, app_id) is True  # the look steps it: ready, nothing left to watch
    app = await svc.apps_repo.get(app_id)
    assert app["state"] == "ready" and app.get("stuck") is None and room.lines == []


async def test_an_app_approved_after_hours_of_waiting_is_not_reported_stuck(world, room):
    src = await _source()
    await _up(src)
    await svc.apps_repo.patch(src["app_id"], {"progress_at": _ago(180), "run_ids": ["run_1"]})
    for r in world.rows.values():  # on_approved moved the rows; nobody has read since
        r["state"] = "provisioning"
    assert await svc._look(OWNER, src["app_id"]) is False
    app = await svc.apps_repo.get(src["app_id"])
    assert app.get("stuck") is None and app["progress_at"] > _ago(1) and room.lines == []


async def test_a_bad_heartbeat_reads_as_not_working(world, room):
    now = datetime.now(UTC)
    for beat in ("garbage", "2026-11-03T00:00:00", None, 5):
        assert svc._working({"status": "started", "lastHeartbeatAt": beat}, now) is False
    app_id = await _building(world)
    room.runs["run_1"] = {"status": "started", "lastHeartbeatAt": "not a time"}
    assert await svc._look(OWNER, app_id) is True
    assert room.lines == [STUCK_LINE]


async def test_an_unlinked_run_still_beating_in_the_room_is_not_stuck(world, room):
    app_id = await _building(world, quiet_min=30)
    room.open_runs.append({"runId": "run_other", "status": "dispatched", "dispatchedAt": _ago(1)})
    assert await svc._look(OWNER, app_id) is False
    room.open_runs[0] = {"runId": "run_other", "status": "started", "lastHeartbeatAt": _ago(2)}
    assert await svc._look(OWNER, app_id) is False
    room.open_runs[0] = {"runId": "run_other", "status": "started", "lastHeartbeatAt": _ago(6)}
    assert await svc._look(OWNER, app_id) is True
    assert room.lines == [STUCK_LINE]


async def test_a_run_that_finished_cleanly_chose_to_wait_so_the_app_gets_45_minutes(world, room):
    app_id = await _building(world, run_ids=("run_old", "run_1"), quiet_min=10)
    room.runs["run_old"] = {"status": "ended", "outcome": "error"}
    room.runs["run_1"] = {"status": "ended", "outcome": "completed"}
    assert await svc._look(OWNER, app_id) is False
    await svc.apps_repo.patch(app_id, {"progress_at": _ago(46)})
    assert await svc._look(OWNER, app_id) is True
    assert room.lines == [STUCK_LINE]


async def test_a_run_that_errored_keeps_the_five_minute_rule(world, room):
    app_id = await _building(world, run_ids=("run_1",), quiet_min=6)
    room.runs["run_1"] = {"status": "ended", "outcome": "error"}
    assert await svc._look(OWNER, app_id) is True
    assert room.lines == [STUCK_LINE]


async def test_hosting_still_being_set_up_is_working_until_the_drive_window_passes(world, room):
    src = await _source()
    await _up(src)
    for r in world.rows.values():
        r["state"] = "connecting_provider"
    await svc.status(OWNER, src["app_id"])  # the rows moved: progress
    await svc.apps_repo.patch(src["app_id"], {"progress_at": _ago(30), "run_ids": ["run_1"]})
    for r in world.rows.values():
        r["updated_at"] = _ago(6)
    assert await svc._look(OWNER, src["app_id"]) is False
    for r in world.rows.values():
        r["updated_at"] = _ago(16)
    assert await svc._look(OWNER, src["app_id"]) is True
    assert (await svc.apps_repo.get(src["app_id"]))["stuck"]["step"] == "setting up hosting"
    assert room.lines == [STUCK_LINE.replace("building", "setting up hosting")]


# --- the starter's placeholder page is not a ready app ------------------------------------------


async def _placeholder_preview(world, commit="c0"):
    src = await svc.put_source(
        OWNER, name="crew", room_id="room_1", commit=commit, archive=_tar(SRC_V1), actor=PERSON, placeholder=True
    )
    await _up(src)
    await _all_active(world, src["app_id"])
    await svc.status(OWNER, src["app_id"])  # deploys
    world.vercel.finish(f"dpl_{len(world.vercel.created())}")
    return src


async def test_a_placeholder_preview_is_building_and_stays_watched_until_the_real_source(world, room):
    src = await _placeholder_preview(world)
    app_id = src["app_id"]
    assert (await svc.apps_repo.get(app_id))["sources"][src["source_id"]]["placeholder"] is True
    assert (await svc.status(OWNER, app_id))["state"] == "building"
    assert (await svc.apps_repo.get(app_id))["state"] == "building"
    assert await svc._look(OWNER, app_id) is False  # the watch keeps running

    real = await _source(SRC_V2, "c1")
    assert "placeholder" not in (await svc.apps_repo.get(app_id))["sources"][real["source_id"]]
    await _up(real)
    await svc.status(OWNER, app_id)  # deploys the real source
    world.vercel.finish(f"dpl_{len(world.vercel.created())}")
    assert (await svc.status(OWNER, app_id))["state"] == "ready"


async def test_a_live_app_whose_new_source_is_a_placeholder_stays_live(world):
    src, _, _ = await _ready_preview(world)
    await _go_live(world, src)
    later = await svc.put_source(
        OWNER, name="crew", room_id="room_1", commit="c9", archive=_tar(SRC_V1), actor=PERSON, placeholder=True
    )
    await _up(later)
    await svc.status(OWNER, src["app_id"])
    world.vercel.finish(f"dpl_{len(world.vercel.created())}")
    assert (await svc.status(OWNER, src["app_id"]))["state"] == "live"


def test_placeholder_is_judged_by_the_source_the_preview_shows():
    rows = {("vercel", "project", "prod"): {"state": "active"}}
    sources = {"s0": {"placeholder": True}, "s1": {}}
    # The last build moved to the real source, but the preview still shows the placeholder.
    app = {"preview": {"source_id": "s0"}, "preview_built": {"source_id": "s1"}, "sources": sources}
    assert svc._state(app, rows)[0] == "building"
    assert svc._state({**app, "preview": {"source_id": "s1"}}, rows)[0] == "ready"
