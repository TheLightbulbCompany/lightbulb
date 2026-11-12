"""Webhooks verified in the backend at their existing URLs (lane A, Task 7; spec 06 §3.5).

Same URL `/api/v1/hooks/t/{owner}/{public_id}`: a trigger with a `HOOK#` row is verified and fired here;
anything else is a 404 (the Paperclip trigger proxy is gone, Phase 1 task 7)."""

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import boto3
import pytest
from fastapi.testclient import TestClient
from temporalio.exceptions import WorkflowAlreadyStartedError

from core.config import settings
from core.repositories import automations_repo as repo
from core.temporal.automations import FireInput

WS = "org_1"
PID = "abc123def456abc123def456"
SECRET = "whsec_test"
BUCKET = "owner-state-test"


def _stripe_body(event_id="evt_1", typ="invoice.payment_failed") -> bytes:
    return json.dumps({"id": event_id, "type": typ}).encode()


def _stripe_headers(body: bytes, t: int | None = None, secret: str = SECRET) -> dict:
    t = t or int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={t},v1={sig}", "Content-Type": "application/json"}


class FakeTemporal:
    def __init__(self):
        self.started: list[tuple] = []
        self.fail: Exception | None = None

    async def start_workflow(self, wf, arg, *, id, task_queue, id_reuse_policy):
        if self.fail:
            raise self.fail
        if any(s[2] == id for s in self.started):
            raise WorkflowAlreadyStartedError(id, wf)
        self.started.append((wf, arg, id, id_reuse_policy))


@pytest.fixture
def env(table, monkeypatch):
    """moto tasks table (conftest) + S3 bucket + fake Temporal + switch on + entitled + secret in place."""
    from routers import _public_ingress, hooks
    from core.temporal import client as tc

    _public_ingress._RATE.clear()
    hooks._SECRET_CACHE.clear()
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    monkeypatch.setattr(settings, "OWNER_STATE_BUCKET", BUCKET)
    fake = FakeTemporal()
    monkeypatch.setattr(tc, "_client", fake)
    secrets = {f"hook-{PID}": {"secret": SECRET, "mode": "hmac_sha256"}}
    get_secret = AsyncMock(side_effect=lambda owner, name: secrets.get(name) if owner == WS else None)
    with (
        patch("routers.hooks.integration_secret_store.get", get_secret),
        patch("routers.hooks.billing_repo.get_by_owner_id", AsyncMock(return_value={"subscription_status": "active"})),
    ):
        yield MagicMock(s3=s3, temporal=fake, secrets=secrets, get_secret=get_secret)
    _public_ingress._RATE.clear()


async def _hook(**over):
    row = {
        "publicId": PID,
        "scheduleId": "s1",
        "provider": "stripe",
        "signingMode": "hmac_sha256",
        "secretRef": f"hook-{PID}",
        "armed": True,
    } | over
    await repo.put_hook(WS, row)


@pytest.fixture
def client():
    from main import app

    return TestClient(app)


def _post(client, body, headers, owner=WS, pid=PID):
    return client.post(f"/api/v1/hooks/t/{owner}/{pid}", content=body, headers=headers)


def _s3_keys(env):
    return [o["Key"] for o in env.s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]


async def test_duplicate_delivery_single_run(env, client):
    await _hook()
    body = _stripe_body()
    r1 = _post(client, body, _stripe_headers(body))
    r2 = _post(client, body, _stripe_headers(body))
    r3 = _post(client, body, _stripe_headers(body, t=int(time.time()) - 5))  # Stripe retry: new t=
    assert (r1.status_code, r2.status_code, r3.status_code) == (202, 200, 200)
    assert r2.json() == r3.json() == {"status": "duplicate"}
    assert len(env.temporal.started) == 1
    wf, arg, wid, _ = env.temporal.started[0]
    assert wf == "TriggerFireWorkflow"
    assert wid == f"hook:{WS}:{PID}:evt_1"
    key = _s3_keys(env)[0]
    assert key.startswith(f"{WS}/triggers/") and key.endswith(f"/{PID}/evt_1.json")
    assert arg == FireInput(WS, "s1", "webhook", public_id=PID, dedupe_key="evt_1", payload_ref=key)
    assert env.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == body


async def test_signature_failure_401_no_trigger(env, client):
    await _hook()
    body = _stripe_body()
    r = _post(client, body, _stripe_headers(body, secret="whsec_wrong"))
    assert r.status_code == 401 and r.json()["detail"] == "bad_signature"
    assert _s3_keys(env) == [] and env.temporal.started == []
    row = await repo.get_hook(WS, PID)
    assert row["sigFailCount"] == 1
    _post(client, body, _stripe_headers(body, secret="whsec_wrong"))
    assert (await repo.get_hook(WS, PID))["sigFailCount"] == 1  # second failure within 60 s writes nothing


async def test_unknown_public_id_404(env, client):
    await _hook()
    r = _post(client, b"{}", {}, pid="0" * 24)
    assert r.status_code == 404


async def test_bad_public_id_400(env, client):
    assert _post(client, b"{}", {}, pid="a-b").status_code == 400


async def test_other_owners_public_id_404(env, client):
    await _hook()
    from routers import hooks

    r = await hooks.fire("org_other", PID, MagicMock())
    assert r.status_code == 404


async def test_not_entitled_402(env, client):
    await _hook()
    with patch("routers.hooks.billing_repo.get_by_owner_id", AsyncMock(return_value=None)):
        r = _post(client, _stripe_body(), _stripe_headers(_stripe_body()))
    assert r.status_code == 402
    assert env.temporal.started == []


async def test_rate_limited_429(env, client):
    await _hook()
    with patch("routers.hooks._check_rate_limit", return_value=False):
        r = _post(client, _stripe_body(), _stripe_headers(_stripe_body()))
    assert r.status_code == 429


async def test_google_sync_204(env, client):
    await _hook(provider="google_calendar", signingMode="bearer")
    r = _post(client, b"", {"X-Goog-Resource-State": "sync", "X-Goog-Channel-Token": SECRET})
    assert r.status_code == 204 and env.temporal.started == []


async def test_github_ping_204(env, client):
    await _hook(provider="github", signingMode="github_hmac")
    r = _post(client, b"{}", {"X-GitHub-Event": "ping"})
    assert r.status_code == 204 and env.temporal.started == []


async def test_event_outside_filter_200_ignored(env, client):
    await _hook(events=["invoice.payment_failed"])
    body = _stripe_body(typ="charge.refunded")
    r = _post(client, body, _stripe_headers(body))
    assert r.status_code == 200 and r.json() == {"status": "ignored"}
    assert _s3_keys(env) == [] and env.temporal.started == []
    ok = _stripe_body(event_id="evt_2")
    assert _post(client, ok, _stripe_headers(ok)).status_code == 202


async def test_temporal_unavailable_503(env, client):
    from core.temporal.client import TemporalUnavailable

    await _hook()
    env.temporal.fail = TemporalUnavailable("down")
    body = _stripe_body()
    r = _post(client, body, _stripe_headers(body))
    assert r.status_code == 503


async def test_unarmed_401_not_armed(env, client):
    await _hook(armed=False)
    body = _stripe_body()
    r = _post(client, body, _stripe_headers(body))
    assert r.status_code == 401 and r.json()["detail"] == "not_armed"
    assert env.temporal.started == []


async def test_missing_secret_401_not_armed(env, client):
    await _hook()
    env.secrets.clear()
    body = _stripe_body()
    assert _post(client, body, _stripe_headers(body)).json()["detail"] == "not_armed"


async def test_gmail_pubsub_oidc(env, client):
    await _hook(provider="gmail", signingMode="google_oidc")
    body = json.dumps({"message": {"messageId": "42", "data": "e30="}}).encode()
    with patch("routers.hooks.verify_google_oidc", return_value=False):
        assert _post(client, body, {"Authorization": "Bearer a.b.c"}).status_code == 401
    with patch("routers.hooks.verify_google_oidc", return_value=True) as v:
        r = _post(client, body, {"Authorization": "Bearer a.b.c"})
    assert r.status_code == 202
    assert v.call_args.args[0] == "a.b.c" and v.call_args.args[1].endswith(f"/api/v1/hooks/t/{WS}/{PID}")
    assert env.temporal.started[0][2] == f"hook:{WS}:{PID}:42"


async def test_gmail_unarmed_401_not_armed(env, client):
    """A Gmail hook whose watch is not set up yet refuses deliveries like any other unarmed hook."""
    await _hook(provider="gmail", signingMode="google_oidc", armed=False)
    body = json.dumps({"message": {"messageId": "42", "data": "e30="}}).encode()
    with patch("routers.hooks.verify_google_oidc", return_value=True):
        r = _post(client, body, {"Authorization": "Bearer a.b.c"})
    assert r.status_code == 401 and r.json()["detail"] == "not_armed"
    assert env.temporal.started == []


async def test_unavailable_is_logged_with_ids_only(env, client, caplog):
    from core.temporal.client import TemporalUnavailable

    await _hook()
    env.temporal.fail = TemporalUnavailable("down")
    body = _stripe_body()
    with caplog.at_level("WARNING", logger="routers.hooks"):
        assert _post(client, body, _stripe_headers(body)).status_code == 503
    record = next(r for r in caplog.records if r.name == "routers.hooks")
    assert WS in record.getMessage() and PID in record.getMessage() and record.exc_info
    assert SECRET not in record.getMessage()


async def test_meta_backend_fire_unavailable_still_200_but_logged(env, client, monkeypatch, caplog):
    from core.temporal.client import TemporalUnavailable
    from routers import meta_webhook

    await _hook(provider="instagram", signingMode="hmac_sha256")
    monkeypatch.setattr(settings, "META_WEBHOOK_VERIFY_TOKEN", "vt")
    env.temporal.fail = TemporalUnavailable("down")
    binding = {"owner_id": WS, "trigger_public_id": PID, "trigger_secret": "x", "fields": []}
    body = json.dumps({"object": "instagram", "entry": [{"id": "17841", "time": 1700000000, "changes": []}]}).encode()
    with (
        patch.object(meta_webhook, "_signature_valid", AsyncMock(return_value=True)),
        patch.object(meta_webhook, "_bindings_for_asset", AsyncMock(return_value=[binding])),
        patch.object(meta_webhook, "billing_repo") as billing,
        caplog.at_level("WARNING"),
    ):
        billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        r = client.post("/api/v1/hooks/meta", content=body, headers={"X-Hub-Signature-256": "sha256=x"})
    assert r.status_code == 200
    assert any(r.name == "routers.meta_webhook" and PID in r.getMessage() for r in caplog.records)


async def test_no_hook_row_is_404_never_paperclip(env, client):
    """A pre-switch Paperclip trigger (no `HOOK#` row) no longer fires anywhere."""
    r = _post(client, _stripe_body(), _stripe_headers(_stripe_body()))
    assert r.status_code == 404 and env.temporal.started == []


async def test_meta_entry_fires_verified(env, client, monkeypatch):
    from routers import meta_webhook

    await _hook(provider="instagram", signingMode="hmac_sha256")
    monkeypatch.setattr(settings, "META_WEBHOOK_VERIFY_TOKEN", "vt")
    binding = {"owner_id": WS, "trigger_public_id": PID, "trigger_secret": "x", "fields": []}
    body = json.dumps({"object": "instagram", "entry": [{"id": "17841", "time": 1700000000, "changes": []}]}).encode()
    with (
        patch.object(meta_webhook, "_signature_valid", AsyncMock(return_value=True)),
        patch.object(meta_webhook, "_bindings_for_asset", AsyncMock(return_value=[binding])),
        patch.object(meta_webhook, "billing_repo") as billing,
    ):
        billing.get_by_owner_id = AsyncMock(return_value={"subscription_status": "active"})
        r = client.post("/api/v1/hooks/meta", content=body, headers={"X-Hub-Signature-256": "sha256=x"})
        r2 = client.post("/api/v1/hooks/meta", content=body, headers={"X-Hub-Signature-256": "sha256=x"})
    assert r.status_code == r2.status_code == 200
    assert [s[2] for s in env.temporal.started] == [f"hook:{WS}:{PID}:17841:1700000000:0"]
