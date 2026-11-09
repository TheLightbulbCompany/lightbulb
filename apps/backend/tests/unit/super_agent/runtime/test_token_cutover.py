"""Spec 13 §6.1 / T5: move a ChatGPT grant from the VM's ``auth.json`` into the backend's custody,
and back. Real token row + containers table (moto DynamoDB), real root ``auth.json`` (moto S3);
AgentCore and the bridge are faked at the two call sites the fence uses."""

import base64
import json
import time
from unittest.mock import AsyncMock, patch

import boto3
import httpx
import pytest

from core.config import settings
from cryptography.fernet import Fernet
from moto import mock_aws

from core.repositories import container_repo
from core.services import container_auth, hermes_profiles, hermes_provisioning, oauth_service, token_custody
from core.services import runtime_transport as rt

OWNER = "org_cut"
USER = "user_cut"
ARN = "arn:aws:bedrock-agentcore:::runtime/r"
IMAGE = "123.dkr.ecr.us-east-1.amazonaws.com/lightbulb/hermes-agent-arm64:h-2026.11.9-abc1234"
AUTH_KEY = f"{OWNER}/hermes/auth.json"
BUCKET = "test-owner-state"


def _jwt(exp: int, tag: str, iat: int | None = None) -> str:
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    claims = {"exp": exp, "tag": tag}
    if iat is not None:
        claims["iat"] = iat
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


@pytest.fixture
def _runtime_session_gate_off():
    """Overrides the conftest autouse fixture: the wake-lease test drives the real gate."""
    yield


@pytest.fixture
def world(monkeypatch):
    from core.config import settings

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        for name, key in (("test-oauth-tokens", "user_id"), ("test-containers", "owner_id")):
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        ddb.create_table(  # --show lists the owner's X/QuickBooks/Instagram rows
            TableName="test-integrations",
            KeySchema=[
                {"AttributeName": "owner_id", "KeyType": "HASH"},
                {"AttributeName": "provider", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "provider", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        monkeypatch.setenv("OAUTH_TOKENS_TABLE", "test-oauth-tokens")
        monkeypatch.setattr(settings, "ENCRYPTION_KEY", Fernet.generate_key().decode())
        monkeypatch.setattr(settings, "OWNER_STATE_BUCKET", BUCKET)
        monkeypatch.setattr(settings, "AGENTCORE_HERMES_IMAGE", IMAGE)
        monkeypatch.setattr(hermes_profiles, "_SHARED_S3_CLIENT", s3)
        monkeypatch.setattr(token_custody, "BROKER_MIN_IMAGE_TAG", "h-2026.11.5-0000000")
        monkeypatch.setattr(token_custody, "resolve_provider_choice", AsyncMock(return_value=("chatgpt_oauth", None)))
        monkeypatch.setattr(oauth_service, "resolve_token_owner", AsyncMock(return_value=USER))
        monkeypatch.setattr(
            container_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": OWNER, "agentcore_runtime_arn": ARN})
        )
        roll = AsyncMock(return_value="current")
        idle = AsyncMock(return_value=True)
        stop = AsyncMock(return_value=True)
        monkeypatch.setattr(hermes_provisioning, "roll_runtime_image", roll)
        monkeypatch.setattr(hermes_provisioning, "_wait_for_idle_session", idle)
        monkeypatch.setattr(hermes_provisioning, "stop_runtime_session", stop)
        posted: list[dict] = []

        async def fake_post(self, url, *, data=None, headers=None, **kw):
            posted.append(data)
            body = {
                "access_token": _jwt(int(time.time()) + 3600, f"R{len(posted)}"),
                "refresh_token": f"RT-r{len(posted)}",
                "expires_in": 3600,
            }
            return httpx.Response(200, json=body, request=httpx.Request("POST", str(url)))

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", ddb):
            yield {"ddb": ddb, "s3": s3, "roll": roll, "idle": idle, "stop": stop, "posted": posted}


def _seed_row(world, *, tokens: dict, custody: str | None = None) -> None:
    item = {
        "user_id": USER,
        "state": "active",
        "bound_owner_id": OWNER,
        "account_id": "acct-1",
        "encrypted_tokens": oauth_service.get_fernet().encrypt(json.dumps(tokens).encode()),
    }
    if custody:
        item.update({"custody": custody, "version": 1})
    world["ddb"].Table("test-oauth-tokens").put_item(Item=item)


def _seed_auth(world, tokens: dict | None, *, extra_provider: bool = True) -> str:
    providers = {"anthropic": {"tokens": {"api_key": "sk-x"}}} if extra_provider else {}
    if tokens is not None:
        providers["openai-codex"] = {"tokens": tokens, "auth_mode": "chatgpt", "last_refresh": "2026-11-01T00:00:00Z"}
    body = json.dumps({"active_provider": "openai-codex", "providers": providers})
    return world["s3"].put_object(Bucket=BUCKET, Key=AUTH_KEY, Body=body.encode())["ETag"]


def _row(world) -> dict:
    return world["ddb"].Table("test-oauth-tokens").get_item(Key={"user_id": USER})["Item"]


def _row_tokens(world) -> dict:
    return json.loads(oauth_service.get_fernet().decrypt(bytes(_row(world)["encrypted_tokens"])))


def _auth(world) -> dict:
    return json.loads(world["s3"].get_object(Bucket=BUCKET, Key=AUTH_KEY)["Body"].read())


DDB_PAIR = {"access_token": _jwt(2_000_000_000, "ddb"), "refresh_token": "RT-ddb", "id_token": "id-ddb"}
S3_PAIR = {"access_token": _jwt(2_000_001_000, "s3"), "refresh_token": "RT-s3", "id_token": "id-s3"}


@pytest.mark.asyncio
async def test_cutover_moves_s3_pair_into_row_with_version(world):
    _seed_row(world, tokens=DDB_PAIR)
    _seed_auth(world, S3_PAIR)  # the VM rotated since connect: S3 is newer than DDB

    assert await token_custody.codex_cutover(OWNER) == "done"

    row = _row(world)
    assert row["custody"] == "backend" and int(row["version"]) == 1
    assert int(row["expires_at"]) == 2_000_001_000 * 1000
    tokens = _row_tokens(world)
    assert tokens["refresh_token"] == "RT-s3" and tokens["access_token"] == S3_PAIR["access_token"]
    assert tokens["account_id"] == "acct-1"
    auth = _auth(world)
    assert auth["providers"]["openai-codex"] == {"custody": "backend", "tokens": {}}
    assert auth["active_provider"] == "openai-codex"
    assert auth["providers"]["anthropic"] == {"tokens": {"api_key": "sk-x"}}  # other providers survive
    assert world["s3"].get_object(Bucket=BUCKET, Key=f"{OWNER}/hermes/auth.revision")  # bridges re-pull
    world["stop"].assert_awaited_once_with(OWNER, settings.HERMES_SHARED_RUNTIME_ARN)
    assert world["posted"] == []  # the verify vend is a fast-path read
    assert "waking_until" not in container_repo.get_session_sync(OWNER)  # wake lease released
    assert await token_custody.codex_cutover(OWNER) == "already"


@pytest.mark.asyncio
async def test_cutover_uses_ddb_pair_when_s3_has_no_grant(world):
    _seed_row(world, tokens=DDB_PAIR)
    _seed_auth(world, None)
    assert await token_custody.codex_cutover(OWNER) == "done"
    assert _row_tokens(world)["refresh_token"] == "RT-ddb"
    assert _auth(world)["providers"]["openai-codex"] == {"custody": "backend", "tokens": {}}


@pytest.mark.asyncio
async def test_cutover_refuses_stale_image(world, monkeypatch):
    _seed_row(world, tokens=DDB_PAIR)
    _seed_auth(world, S3_PAIR)
    # No broker-capable tag known yet (before V16's roll): refuse.
    monkeypatch.setattr(token_custody, "BROKER_MIN_IMAGE_TAG", None)
    assert await token_custody.codex_cutover(OWNER) == "stale_image"
    # The configured image predates the broker tag.
    monkeypatch.setattr(token_custody, "BROKER_MIN_IMAGE_TAG", "h-2026.11.10-ffffff0")
    assert await token_custody.codex_cutover(OWNER) == "stale_image"
    # The runtime is not on the configured image yet (a turn kept the old session alive).
    monkeypatch.setattr(token_custody, "BROKER_MIN_IMAGE_TAG", "h-2026.11.5-0000000")
    world["roll"].return_value = "updated-session-busy"
    assert await token_custody.codex_cutover(OWNER) == "stale_image"
    assert "custody" not in _row(world) and _auth(world)["providers"]["openai-codex"]["tokens"] == S3_PAIR
    world["stop"].assert_not_awaited()


@pytest.mark.asyncio
async def test_cutover_busy_session_retries(world):
    _seed_row(world, tokens=DDB_PAIR)
    _seed_auth(world, S3_PAIR)
    world["idle"].return_value = False
    assert await token_custody.codex_cutover(OWNER) == "busy_session"
    assert "custody" not in _row(world) and _auth(world)["providers"]["openai-codex"]["tokens"] == S3_PAIR
    world["stop"].assert_not_awaited()
    world["idle"].return_value = True
    assert await token_custody.codex_cutover(OWNER) == "done"


@pytest.mark.asyncio
async def test_idle_wait_runs_outside_the_guard(world, monkeypatch):
    """Lead (5 Nov): the guard's lease is taken over after 120 s and the idle wait can run 10 min,
    so idle is awaited BEFORE the guard; stop and lease follow inside it, right away."""
    from contextlib import asynccontextmanager

    from core.services import owner_lifecycle

    order: list[str] = []

    @asynccontextmanager
    async def guard(owner_id):
        order.append("guard:enter")
        yield
        order.append("guard:exit")

    async def idle(owner_id):
        order.append("idle")
        return True

    async def stop(owner_id, arn):
        order.append("stop")
        return True

    monkeypatch.setattr(owner_lifecycle, "owner_lifecycle_guard", guard)
    world["idle"].side_effect = idle
    world["stop"].side_effect = stop
    _seed_row(world, tokens=DDB_PAIR)
    _seed_auth(world, S3_PAIR)
    assert await token_custody.codex_cutover(OWNER) == "done"
    assert order == ["idle", "guard:enter", "stop", "guard:exit"]
    order.clear()
    assert await token_custody.codex_rollback(OWNER) == "done"
    assert order == ["idle", "guard:enter", "stop", "guard:exit"]


@pytest.mark.asyncio
async def test_cutover_raced_etag_undoes_row(world, monkeypatch):
    _seed_row(world, tokens=DDB_PAIR)
    _seed_auth(world, S3_PAIR)
    real_put = hermes_profiles._s3_put_conditionally

    def racing_put(bucket, key, body, etag):
        # A writer we failed to fence lands between our read and our put.
        racer = {"providers": {"openai-codex": {"tokens": {"refresh_token": "RT-newer"}}}}
        world["s3"].put_object(Bucket=bucket, Key=key, Body=json.dumps(racer).encode())
        return real_put(bucket, key, body, etag)

    monkeypatch.setattr(hermes_profiles, "_s3_put_conditionally", racing_put)
    assert await token_custody.codex_cutover(OWNER) == "raced"
    row = _row(world)
    assert row["custody"] == "actor"  # step 4 undone: the old path still reads S3
    assert _auth(world)["providers"]["openai-codex"]["tokens"] == {"refresh_token": "RT-newer"}  # S3 untouched by us
    assert "waking_until" not in container_repo.get_session_sync(OWNER)


@pytest.mark.asyncio
async def test_cutover_preconditions(world, monkeypatch):
    assert await token_custody.codex_cutover(OWNER) == "no_grant"  # no row at all
    monkeypatch.setattr(token_custody, "resolve_provider_choice", AsyncMock(return_value=("openrouter_managed", None)))
    assert await token_custody.codex_cutover(OWNER) == "not_chatgpt"
    monkeypatch.setattr(token_custody, "resolve_provider_choice", AsyncMock(return_value=("chatgpt_oauth", None)))
    _seed_row(world, tokens=DDB_PAIR)
    _seed_auth(world, S3_PAIR)
    assert await token_custody.codex_cutover(OWNER, dry_run=True) == "would_cutover"
    assert "custody" not in _row(world) and world["stop"].await_count == 0


@pytest.mark.asyncio
async def test_cutover_rollback_round_trip(world):
    _seed_row(world, tokens=DDB_PAIR)
    _seed_auth(world, S3_PAIR)
    assert await token_custody.codex_cutover(OWNER) == "done"
    # Two forced refreshes by the broker: the row is the newest pair, S3 stays the marker.
    for _ in range(2):
        current = _row_tokens(world)["access_token"]
        await token_custody.codex_vend(OWNER, presented_sha256=token_custody._sha(current), force=True)
    assert len(world["posted"]) == 2 and int(_row(world)["version"]) == 3
    newest = _row_tokens(world)
    assert newest["refresh_token"] == "RT-r2"
    assert _auth(world)["providers"]["openai-codex"] == {"custody": "backend", "tokens": {}}

    assert await token_custody.codex_rollback(OWNER) == "done"
    row = _row(world)
    assert row["custody"] == "actor"
    staged = _auth(world)["providers"]["openai-codex"]
    assert staged["tokens"] == {k: newest[k] for k in ("access_token", "refresh_token", "id_token")}
    assert "custody" not in staged  # the legacy bridge path reads a plain grant
    assert _auth(world)["active_provider"] == "openai-codex"
    assert await token_custody.codex_vend(OWNER) is None  # actor again: the VM path applies
    assert await token_custody.codex_rollback(OWNER) == "already"

    # A second cutover works and picks up the pair S3 now holds.
    assert await token_custody.codex_cutover(OWNER) == "done"
    assert _row_tokens(world)["refresh_token"] == "RT-r2" and int(_row(world)["version"]) == 1


@pytest.mark.asyncio
async def test_rollback_busy_session(world):
    _seed_row(world, tokens=DDB_PAIR, custody="backend")
    _seed_auth(world, None)
    world["idle"].return_value = False
    assert await token_custody.codex_rollback(OWNER) == "busy_session"
    assert _row(world)["custody"] == "backend"


def test_wake_lease_blocks_concurrent_invoke(world):
    """Spec 13 §6.1 step 2.2, marked I: while the cutover holds the cold-wake lease a concurrent
    invoke waits, then gives up on its own budget -- it never starts a session that could rotate."""
    now = int(time.time())
    assert container_repo.try_take_wake_lease_sync(OWNER, arn=ARN, token="cutover", now=now, lease_s=60)
    woke: list[int] = []
    with pytest.raises(httpx.ReadTimeout):
        rt._ensure_session(OWNER, ARN, time.monotonic() + 0.05, lambda: woke.append(1))
    assert woke == []
    container_repo.finish_wake_sync(OWNER, token="cutover")
    assert rt._ensure_session(OWNER, ARN, time.monotonic() + 5, lambda: woke.append(1))["arn"] == ARN
    assert woke == [1]


@pytest.mark.asyncio
async def test_new_connect_with_switch_on_stages_marker(world):
    """A backend-custodied row (V9's switch-on connect) stages the MARKER, never the tokens: a
    reconnect must not hand the VM a refresh token the backend is also rotating."""
    _seed_row(world, tokens=DDB_PAIR, custody="backend")
    _seed_auth(world, None, extra_provider=False)
    assert await container_auth.push_oauth_credential_to_container(owner_id=OWNER, connector_id=USER) is True
    assert _auth(world) == {
        "active_provider": "openai-codex",
        "providers": {"openai-codex": {"custody": "backend", "tokens": {}}},
    }
    # Actor row: today's staging, tokens and all.
    _seed_row(world, tokens=DDB_PAIR)
    assert await container_auth.push_oauth_credential_to_container(owner_id=OWNER, connector_id=USER) is True
    assert _auth(world)["providers"]["openai-codex"]["tokens"]["refresh_token"] == "RT-ddb"


# --- the script -----------------------------------------------------------------------------------


async def _run(argv: list[str]) -> tuple[int, str]:
    import io
    from contextlib import redirect_stdout

    from scripts import token_custody as script

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = await script.main(argv)
    return code, buf.getvalue()


@pytest.mark.asyncio
async def test_script_refuses_prod_without_approval(world, monkeypatch):
    from core.config import settings

    _seed_row(world, tokens=DDB_PAIR)
    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    code, out = await _run(["--owner-id", OWNER, "--show"])
    assert code == 2 and "refusing" in out
    code, out = await _run(["--owner-id", OWNER, "--show", "--founder-approved", "msg-link-1"])
    assert code == 0 and "custody=actor" in out and "founder_approved=msg-link-1" in out


@pytest.mark.asyncio
async def test_script_never_prints_tokens(world):
    secrets = [DDB_PAIR["access_token"], S3_PAIR["access_token"], "RT-ddb", "RT-s3", "id-ddb", "id-s3", "sk-x"]
    _seed_row(world, tokens={**DDB_PAIR, "access_token": _jwt(2_000_000_000, "ddb", iat=2_000_000_000 - 3600)})
    _seed_auth(world, S3_PAIR)
    outputs = []
    for argv in (
        ["--owner-id", OWNER, "--show"],
        ["--owner-id", OWNER, "--measure-ttl"],
        ["--owner-id", OWNER, "--cutover", "codex"],  # dry run by default
        ["--owner-id", OWNER, "--cutover", "codex", "--apply"],
        ["--owner-id", OWNER, "--show"],
        ["--owner-id", OWNER, "--rollback", "codex", "--apply"],
    ):
        code, out = await _run(argv)
        assert code == 0, (argv, out)
        outputs.append(out)
    text = "\n".join(outputs)
    assert not [s for s in secrets if s in text]
    assert "ttl_seconds=3600" in outputs[1]
    assert "codex: would_cutover" in outputs[2] and "codex: done" in outputs[3]
    assert "custody=backend version=1" in outputs[4] and "codex: done" in outputs[5]
    assert _row(world)["custody"] == "actor"


@pytest.mark.asyncio
async def test_rollback_dry_run_writes_nothing(world):
    """Review of #4732: `--rollback codex` without --apply must not roll back."""
    _seed_row(world, tokens=DDB_PAIR, custody="backend")
    _seed_auth(world, None)
    assert await token_custody.codex_rollback(OWNER, dry_run=True) == "would_rollback"
    code, out = await _run(["--owner-id", OWNER, "--rollback", "codex"])
    assert code == 0 and "codex: would_rollback" in out
    assert _row(world)["custody"] == "backend"
    assert "openai-codex" not in _auth(world)["providers"]
    world["stop"].assert_not_awaited()
    world["idle"].assert_not_awaited()  # a dry run never probes (drains/wakes) the workspace


@pytest.mark.asyncio
async def test_rollback_stages_the_pair_read_under_the_fence(world):
    """Review of #4732: a broker refresh during the idle wait rotates the pair; the rollback must
    stage THAT pair, not the one it read before waiting (a spent refresh token in S3)."""
    _seed_row(world, tokens=DDB_PAIR, custody="backend")
    _seed_auth(world, None)

    async def refresh_during_wait(owner_id):
        current = _row_tokens(world)["access_token"]
        await token_custody.codex_vend(OWNER, presented_sha256=token_custody._sha(current), force=True)
        return True

    world["idle"].side_effect = refresh_during_wait
    assert await token_custody.codex_rollback(OWNER) == "done"
    rotated = _row_tokens(world)
    assert rotated["refresh_token"] == "RT-r1" and len(world["posted"]) == 1
    assert _auth(world)["providers"]["openai-codex"]["tokens"]["refresh_token"] == "RT-r1"
    assert _row(world)["custody"] == "actor"


@pytest.mark.asyncio
async def test_cutover_undo_failed_is_reported(world, monkeypatch):
    _seed_row(world, tokens=DDB_PAIR)
    _seed_auth(world, S3_PAIR)
    monkeypatch.setattr(
        hermes_profiles, "stage_codex_marker", lambda *a, **k: (_ for _ in ()).throw(hermes_profiles.MarkerRaced(OWNER))
    )
    monkeypatch.setattr(oauth_service, "set_custody_actor", AsyncMock(return_value=False))
    assert await token_custody.codex_cutover(OWNER) == "undo_failed"
    assert _row(world)["custody"] == "backend"  # loudly left for a re-run


@pytest.mark.asyncio
async def test_cutover_done_unverified_when_the_verify_vend_refuses(world, monkeypatch):
    _seed_row(world, tokens=DDB_PAIR)
    _seed_auth(world, S3_PAIR)
    monkeypatch.setattr(
        token_custody, "codex_vend", AsyncMock(side_effect=token_custody.RefreshUnavailable("http_503"))
    )
    assert await token_custody.codex_cutover(OWNER) == "done_unverified"
    assert _row(world)["custody"] == "backend" and _auth(world)["providers"]["openai-codex"] == {
        "custody": "backend",
        "tokens": {},
    }
    assert await token_custody.codex_cutover(OWNER) == "already"


@pytest.mark.asyncio
async def test_script_cli_dry_run_reads_the_row(world):
    table = world["ddb"].Table("test-integrations")
    table.put_item(Item={"owner_id": OWNER, "provider": "x", "status": "connected"})
    code, out = await _run(["--owner-id", OWNER, "--cutover", "all"])
    assert code == 0
    assert "x: would_cutover" in out and "quickbooks: no_grant" in out and "instagram: no_grant" in out
    table.put_item(Item={"owner_id": OWNER, "provider": "x", "status": "connected", "custody": "backend"})
    code, out = await _run(["--owner-id", OWNER, "--cutover", "x"])
    assert "x: already" in out
    code, out = await _run(["--owner-id", OWNER, "--rollback", "all"])
    assert "x: would_rollback" in out and "quickbooks: already" in out and "codex: already" in out
    assert _row_tokens  # nothing written: the row API was only read
    assert table.get_item(Key={"owner_id": OWNER, "provider": "x"})["Item"]["custody"] == "backend"


@pytest.mark.asyncio
async def test_script_list_eligible(world, monkeypatch):
    from core.repositories import billing_repo, integrations_repo

    _seed_row(world, tokens=DDB_PAIR)
    monkeypatch.setattr(
        billing_repo, "scan_all", AsyncMock(return_value=[{"owner_id": OWNER, "provider_choice": "chatgpt_oauth"}])
    )
    monkeypatch.setattr(
        integrations_repo,
        "scan_all",
        AsyncMock(
            return_value=[
                {"owner_id": OWNER, "provider": "x", "status": "connected"},
                {"owner_id": OWNER, "provider": "quickbooks", "status": "connected", "custody": "backend"},
                {"owner_id": "org_other", "provider": "stripe", "status": "connected"},
            ]
        ),
    )
    code, out = await _run(["--all", "--list-eligible"])
    assert code == 0
    assert f"{OWNER}: codex" in out and f"{OWNER}: x" in out
    assert "quickbooks" not in out and "stripe" not in out
