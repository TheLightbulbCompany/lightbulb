import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import asyncio  # noqa: E402
import copy  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import tarfile  # noqa: E402
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


class Vercel:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.deployments: dict[str, dict] = {}
        self.verified = False
        self.alias_fails = False
        self.promote_fails = False
        self.refused = False  # Vercel no longer accepts the token: 403 on everything

    def paths(self, method=None):
        return [(r.method, r.url.path) for r in self.calls if method in (None, r.method)]

    def env_posts(self):
        return [json.loads(r.content) for r in self.calls if r.url.path.endswith("/env") and r.method == "POST"]

    def created(self):
        return [json.loads(r.content) for r in self.calls if r.url.path == "/v13/deployments"]

    def finish(self, dep_id, state="READY", error=None):
        self.deployments[dep_id].update(readyState=state, errorMessage=error)

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        p = req.url.path
        if self.refused:
            return httpx.Response(403, json={"error": {"code": "forbidden", "message": "Not authorized"}})
        if p == "/v13/deployments":
            dep_id = f"dpl_{len(self.deployments) + 1}"
            self.deployments[dep_id] = {"readyState": "QUEUED", "url": f"{dep_id}.vercel.app"}
            return httpx.Response(200, json={"id": dep_id, "url": f"{dep_id}.vercel.app", "readyState": "QUEUED"})
        if p.startswith("/v13/deployments/"):
            return httpx.Response(200, json=self.deployments[p.rsplit("/", 1)[1]])
        if p.endswith("/domains") and req.method == "POST":
            return httpx.Response(200, json={"verified": False, "verification": [{"type": "TXT"}]})
        if p.startswith("/v6/domains/"):
            return httpx.Response(200, json={"recommendedCNAME": [{"value": "c.vercel-dns.com.", "rank": 1}]})
        if p.endswith("/verify"):
            return httpx.Response(200, json={"verified": self.verified})
        if "/promote/" in p:
            return httpx.Response(500 if self.promote_fails else 201, json={})
        if p.endswith("/aliases"):
            return httpx.Response(400 if self.alias_fails else 200, json={})
        if p.endswith("/files"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})


@pytest.fixture(autouse=True)
def published(monkeypatch):
    from core.services import live_events

    m = AsyncMock()
    monkeypatch.setattr(live_events, "publish_owner_users", m)
    return m


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
            }
            return dict(rows[wid])

        wf = svc.provisioning_workflow
        monkeypatch.setattr(wf, "start", AsyncMock(side_effect=start))
        monkeypatch.setattr(wf, "advance_on_read", AsyncMock(side_effect=lambda wid: dict(rows[wid])))
        monkeypatch.setattr(wf, "account_id_for", AsyncMock(return_value="acct_1"))
        monkeypatch.setattr(wf, "cancel", AsyncMock())
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
    assert {(c["provider"], c["service"], c["state"]) for c in res["cards"]} == {
        ("Vercel", "project", "awaiting_approval"),
        ("Neon", "postgres", "awaiting_approval"),
    }
    assert len(res["cards"]) == 3
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
    assert world.vercel.calls[0].method == "POST" and world.vercel.calls[0].url.path.endswith("/domains")
    assert world.vercel.calls[1].url.path == "/v13/deployments/dpl_1"  # the probe, before anything is rotated
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
    await svc.apps_repo.patch(src["app_id"], {"credential_renewal": {"state": "needs_reveal", "at": "2026-10-30"}})
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


async def test_a_network_error_on_the_probe_never_rotates(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    rotate, reveal = _renewal(monkeypatch)

    def answer(req):
        if req.url.path.endswith("/domains"):
            return httpx.Response(403, json=JSON_403)
        if req.method == "GET":
            raise httpx.ConnectError("down")
        return None

    _vercel_answers(world, monkeypatch, answer)
    await _publish(src)
    rotate.assert_not_awaited()
    reveal.assert_not_awaited()


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


async def test_a_token_that_lands_after_pending_is_used_but_the_hold_stays(world, monkeypatch):
    src, _, _ = await _settled_preview(world)
    rotate, _ = _renewal(monkeypatch, rotate="pending")
    world.vercel.refused = True
    await _publish(src)
    monkeypatch.setattr(svc.api, "reveal_access_configuration", AsyncMock(side_effect=_new_token_accepted(world)))
    res = await _publish(src)
    rotate.assert_awaited_once()
    assert world.secrets[(OWNER, f"apps/{src['app_id']}/vercel/prod")]["token"] == "vtok_NEW"
    rec = await _rec(src)
    assert rec["credential_renewal"]["state"] == "pending" and rec.get("publish_pending")
    assert rec.get("publish_error") is None
    assert res["error"]["code"] == "credential_renewal_stuck" and res["state"] == "building"


async def test_a_rotated_token_refused_at_once_stops_rather_than_rotating_every_hour(world, monkeypatch):
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


async def test_a_token_that_dies_again_rotates_again_only_after_the_hour(world, monkeypatch):
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
