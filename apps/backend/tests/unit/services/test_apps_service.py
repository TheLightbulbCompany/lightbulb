import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import io  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import tarfile  # noqa: E402
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

    def paths(self, method=None):
        return [(r.method, r.url.path) for r in self.calls if method in (None, r.method)]

    def env_posts(self):
        return [json.loads(r.content) for r in self.calls if r.url.path.endswith("/env")]

    def created(self):
        return [json.loads(r.content) for r in self.calls if r.url.path == "/v13/deployments"]

    def finish(self, dep_id, state="READY", error=None):
        self.deployments[dep_id].update(readyState=state, errorMessage=error)

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        p = req.url.path
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
        if p.endswith("/aliases"):
            return httpx.Response(400 if self.alias_fails else 200, json={})
        if p.endswith("/files"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})


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

        vercel = Vercel()
        monkeypatch.setattr(vercel_client, "_transport", httpx.MockTransport(vercel.handler))
        reset = AsyncMock()
        monkeypatch.setattr(svc.apps_db, "reset_preview", reset)
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
    key = json.loads(preview["LIGHTBULB_ISSUER_KEY"])
    assert key["kty"] == "OKP" and key["crv"] == "Ed25519" and key["kid"] and key["x"]
    assert {e["key"]: e["value"] for e in envs[1]}["DATABASE_URL"] == LIVE_DB
    dep = world.vercel.created()[0]
    assert "target" not in dep and dep["projectSettings"]["buildCommand"] == "next build"
    assert {f["file"] for f in dep["files"]} == set(SRC_V1)
    assert ("PATCH", "/v9/projects/prj_1") in world.vercel.paths()  # protection off
    world.vercel.finish("dpl_1")
    res = await svc.status(OWNER, src["app_id"])
    assert res["state"] == "ready"
    assert res["preview"] == {"deployment_id": "dpl_1", "commit": "c1", "ready_at": res["preview"]["ready_at"]}
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


async def test_publish_deploys_and_adds_domain_once(world):
    src, _, _ = await _ready_preview(world)
    pub = await svc.publish(OWNER, src["app_id"], source_id=src["source_id"], actor=PERSON)
    assert pub["state"] == "building"
    dep = world.vercel.created()[-1]
    assert dep["target"] == "production"
    assert dep["projectSettings"]["buildCommand"] == "npx drizzle-kit migrate && next build"
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
        "card_ok": {"status": "approved", "companyId": "co_1"},
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
        await svc.put_source("org_other", name="crew", room_id="r", commit="c", archive=_tar(SRC_V1), actor=PERSON)
