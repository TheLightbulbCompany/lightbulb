"""Session pick-up: real Moto DynamoDB + S3 through the relay and the device routes."""

import base64
import gzip
import hashlib
import time
import uuid
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import boto3
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.config import settings
from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service, session_pickup as pickup
from core.services.messaging import ApiProblem
from tests.unit.services import test_local_session_relay as relay_tests, test_local_sessions as registry_tests
from tests.unit.services.test_local_session_relay import connect, heartbeat, registration, relay
from tests.unit.services.test_local_sessions import ALICE, BOB, CLI, MAC, OWNER

db = registry_tests.db
transport = relay_tests.transport

BUCKET = "test-owner-state"
BOB_MAC = "d" * 64
CONVERSATION = "11111111-2222-4333-8444-555555555555"
KEYS = {MAC: Ed25519PrivateKey.generate(), BOB_MAC: Ed25519PrivateKey.generate()}


def _public(key) -> str:
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def token(device=MAC, owner=OWNER, key=None) -> str:
    now = int(time.time())
    claims = {"deviceId": device, "owner": owner, "kind": "device", "iat": now, "exp": now + 60}
    return jwt.encode(claims, key or KEYS[device], algorithm="EdDSA")


@pytest.fixture
def s3(transport, db):
    db[1].update_item(
        Key={"owner_id": OWNER},
        UpdateExpression="SET paired_devices.#mac.publicKey = :key, paired_devices.#bob = :bob",
        ExpressionAttributeNames={"#mac": MAC, "#bob": BOB_MAC},
        ExpressionAttributeValues={
            ":key": _public(KEYS[MAC]),
            ":bob": {"userId": BOB.user_id, "clientId": "openclaw-macos", "publicKey": _public(KEYS[BOB_MAC])},
        },
    )
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket=BUCKET)
    with patch.object(settings, "OWNER_STATE_BUCKET", BUCKET):
        yield client


def keys(client, prefix="session-pickup/") -> list[str]:
    return sorted(o["Key"] for o in client.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []))


async def series_dir(sid, series="s1") -> str:
    return pickup.series_prefix(await repo.get_session(OWNER, sid), series)


async def claude_session(transport, **changes) -> str:
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=True))
    connect(transport)
    frame = registration(provider="claude", nativeConversationId=CONVERSATION, providerVersion="2.1.283", **changes)
    return (await relay(transport).handle_message("conn-1", frame))["sessionId"]


def device_app() -> FastAPI:
    from routers.desktop_session_pickup import router

    app = FastAPI()
    app.include_router(router)
    return app


async def device(method_path: str, body, *, auth: str | None = None):
    async with AsyncClient(transport=ASGITransport(app=device_app()), base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {auth}"} if auth else {}
        return await client.post(f"/desktop/session-pickup/{method_path}", json=body, headers=headers)


def part(index, data: bytes, start: int):
    blob = gzip.compress(data)
    return blob, {
        "index": index,
        "bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "from": start,
        "to": start + len(data),
    }


async def upload_and_commit(
    s3,
    sid,
    series="s1",
    chunks=(b'{"a":1}\n',),
    start_index=0,
    start=0,
    legacy_transcript=False,
    poison_digest=False,
    **commit,
):
    parts, blobs = [], []
    for i, data in enumerate(chunks):
        blob, meta = part(start_index + i, data, start)
        start = meta["to"]
        parts.append(meta)
        blobs.append(blob)
    listed = [{"kind": "transcript", "index": p["index"], "bytes": p["bytes"], "sha256": p["sha256"]} for p in parts]
    code = commit.pop("code_blob", None)
    if code:
        listed.append({"kind": "code", "bytes": len(code), "sha256": hashlib.sha256(code).hexdigest()})
    side = commit.pop("sidecars_blob", None)
    if side:
        listed.append({"kind": "sidecars", "bytes": len(side), "sha256": hashlib.sha256(side).hexdigest()})
    response = await device(f"{sid}/uploads", {"series": series, "parts": listed}, auth=token())
    assert response.status_code == 200, response.status_code
    row = await repo.get_session(OWNER, sid) if legacy_transcript else None
    for n, (upload, blob) in enumerate(zip(response.json()["uploads"], blobs + [b for b in (code, side) if b])):
        key = urlparse(upload["url"]).path.lstrip("/")
        if row is not None and n < len(parts):
            # Simulate an upload signed by the phase 1 backend during the rolling deploy.
            key = pickup._key(row, series, "transcript", index=parts[n]["index"])
        s3.put_object(Bucket=BUCKET, Key=key, Body=blob)
    if poison_digest:
        row = await repo.get_session(OWNER, sid)
        for item in parts:
            s3.put_object(
                Bucket=BUCKET,
                Key=pickup._key(row, series, "transcript", index=item["index"], sha=item["sha256"]),
                Body=b"wrong size",
            )
    body = {
        "series": series,
        "conversationId": CONVERSATION,
        "claudeVersion": "2.1.283",
        "cwdName": "lightbulb",
        "cwdRelative": "apps/backend",
        "ended": False,
        "transcript": {"parts": parts, "totalBytes": start},
        **commit,
    }
    if code:
        body["code"] = {
            "bytes": len(code),
            "sha256": hashlib.sha256(code).hexdigest(),
            "remote": "github.com/acme/app",
            "branch": "feat/x",
            "head": "a" * 40,
            "snapshot": "b" * 40,
        }
    if side:
        body["sidecars"] = {"bytes": len(side), "sha256": hashlib.sha256(side).hexdigest()}
    return await device(f"{sid}/commit", body, auth=token())


async def sweep_later() -> int:
    """A sweep past both the abandoned-upload age and a fresh replacement's ticket window."""
    return await pickup.sweep_once(now=int(time.time()) + pickup.ABANDONED_UPLOAD_S + 1)


# --- The author's switch ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_author_allows_pickup_and_only_for_claude(transport, s3):
    sid = await claude_session(transport)
    with pytest.raises(ApiProblem, match="owner"):
        await pickup.set_pickup(BOB, sid, True)
    # The author always sees the switch; a teammate sees nothing until it is on.
    assert service._session_public(await repo.get_session(OWNER, sid), ALICE.user_id).model_dump(by_alias=True)[
        "pickup"
    ] == {"allowed": False}
    assert "pickup" not in (await service.get_session(BOB, sid)).model_dump(by_alias=True)

    public = (await pickup.set_pickup(ALICE, sid, True)).model_dump(by_alias=True)
    assert public["pickup"] == {"allowed": True, "state": "preparing"}
    assert (await service.get_session(BOB, sid)).model_dump(by_alias=True)["pickup"]["allowed"] is True
    wants = await messaging_repo._query(repo.user_pk(OWNER, ALICE.user_id), begins_with="PICKUPWANT#")
    assert [(w["sessionId"], w["provider"], w["conversationId"]) for w in wants] == [(sid, "claude", CONVERSATION)]

    connect(transport, cid="conn-2", stamp="1001")
    await relay(transport).handle_message("conn-2", registration(nativeConversationId="codex-thread"))
    codex = repo.session_id(OWNER, MAC, "codex", "f" * 64, "codex-thread")
    with pytest.raises(ApiProblem) as refused:
        await pickup.set_pickup(ALICE, codex, True)
    assert refused.value.error.code == "LOCAL_SESSION_UNSUPPORTED" and refused.value.status_code == 409


@pytest.mark.asyncio
async def test_turning_it_off_deletes_the_snapshot_and_wants(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    assert (await upload_and_commit(s3, sid)).status_code == 204
    s3.put_object(Bucket=BUCKET, Key=f"session-pickup/other-owner/{sid}/s1/t-000000.gz", Body=b"x")
    await pickup.set_pickup(ALICE, sid, False)
    row = await repo.get_session(OWNER, sid)
    assert "pickupAllowed" not in row and "pickupSnapshot" not in row
    assert await messaging_repo._get(row["pk"], "PICKUP") is None
    assert await messaging_repo._query(repo.user_pk(OWNER, ALICE.user_id), begins_with="PICKUPWANT#") == []
    assert keys(s3) == [f"session-pickup/other-owner/{sid}/s1/t-000000.gz"]
    actions = [e.action for e in (await service.list_audit(BOB, sid)).entries]
    assert actions == ["pickup_on", "pickup_off"]


@pytest.mark.asyncio
async def test_permission_survives_a_new_generation_and_an_ended_session(transport, s3):
    sid = await claude_session(transport)
    await service.set_team_control(ALICE, sid, True)
    await pickup.set_pickup(ALICE, sid, True)
    frame = registration(
        provider="claude",
        nativeConversationId=CONVERSATION,
        generation="generation-2",
        providerVersion="2.1.283",
    )
    frame["previousGeneration"] = "generation-1"
    await relay(transport).handle_message("conn-1", frame)
    row = await repo.get_session(OWNER, sid)
    assert row["pickupAllowed"] is True and row["pickupSnapshot"] == {"state": "preparing"}
    assert "teamControl" not in row
    # register_session (the server-internal entry) carries it too.
    registration_model = service.SessionRegistration(
        provider="claude",
        profileFingerprint="f" * 64,
        nativeConversationId=CONVERSATION,
        generation="generation-3",
        providerVersion="2.1.283",
    )
    await service.register_session(OWNER, ALICE.user_id, MAC, registration_model)
    assert (await repo.get_session(OWNER, sid))["pickupAllowed"] is True
    # Off, then on again after the session ended: allowed, unlike teammate control.
    await pickup.set_pickup(ALICE, sid, False)
    ended = {"type": "local.session.ended", "sessionId": sid, "generation": "generation-3", "reason": "exited"}
    await relay(transport).handle_message("conn-1", {**ended, "endedAt": int(time.time())})
    assert (await pickup.set_pickup(ALICE, sid, True)).pickup.allowed is True


# --- Relay acknowledgements ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acks_carry_this_devices_wanted_sessions(transport, s3):
    sid = await claude_session(transport)
    ack = await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle"))
    assert "pickup" not in ack  # nothing wanted: the old shape, byte for byte
    await pickup.set_pickup(ALICE, sid, True)
    ack = await relay(transport).handle_message("conn-1", heartbeat(sid, status="idle"))
    assert ack["pickup"] == [{"sessionId": sid, "provider": "claude", "conversationId": CONVERSATION}]
    ended = {"type": "local.session.ended", "sessionId": sid, "generation": "generation-1", "reason": "exited"}
    ack = await relay(transport).handle_message("conn-1", {**ended, "endedAt": int(time.time())})
    assert ack["type"] == "local.session.ended.ack" and ack["pickup"][0]["sessionId"] == sid


@pytest.mark.asyncio
async def test_ack_list_is_capped_newest_first_and_fits_the_envelope(db):
    table = db[0]
    for n in range(50):
        table.put_item(
            Item={
                "pk": repo.user_pk(OWNER, ALICE.user_id),
                "sk": f"PICKUPWANT#{MAC}#s{n:02d}",
                "sessionId": f"s{n:02d}",
                "provider": "claude",
                "conversationId": "c" * 250,
                "allowedAt": n,
            }
        )
    ack = await pickup.ack_with_pickup({"type": "local.session.ended.ack", "sessionId": "s"}, OWNER, ALICE.user_id, MAC)
    assert [e["sessionId"] for e in ack["pickup"]] == [f"s{n:02d}" for n in range(10, 50)]
    # A heavy acknowledgement keeps its own content and drops the oldest wants until it fits.
    heavy = {"type": "local.device.heartbeat.ack", "rejected": ["r" * 200] * 30, "reconcile": [], "serverTime": 1}
    ack = await pickup.ack_with_pickup(heavy, OWNER, ALICE.user_id, MAC)
    ids = [e["sessionId"] for e in ack["pickup"]]
    assert ids[-1] == "s49" and len(ids) < 40 and ack["rejected"] == heavy["rejected"]
    from core.services.local_session_relay import check_envelope

    check_envelope(ack)
    # Another device's wants never ride this device's ack.
    assert "pickup" not in await pickup.ack_with_pickup({}, OWNER, ALICE.user_id, "e" * 64)


# --- Device routes ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_routes_fail_closed_and_silent(transport, s3):
    sid = await claude_session(transport)
    body = {"series": "s1", "parts": [{"kind": "transcript", "index": 0, "bytes": 10, "sha256": "a" * 64}]}
    # Not allowed yet.
    for auth in (None, "garbage", token(), token(key=Ed25519PrivateKey.generate()), token(device=BOB_MAC)):
        result = await device(f"{sid}/uploads", body, auth=auth)
        assert result.status_code == 403 and result.content == b""
    await pickup.set_pickup(ALICE, sid, True)
    assert (await device(f"{sid}/uploads", body, auth=token())).status_code == 200
    # A teammate's Mac, a forged signature, a non-uuid id, a paused session: all a bare 403.
    for auth, path in ((token(device=BOB_MAC), sid), (token(key=Ed25519PrivateKey.generate()), sid), (token(), "x")):
        result = await device(f"{path}/uploads", body, auth=auth)
        assert result.status_code == 403 and result.content == b""
    await service.set_sharing(ALICE, sid, False)
    assert (await device(f"{sid}/uploads", body, auth=token())).status_code == 403
    await service.set_sharing(ALICE, sid, True)
    # Sharing turned off on the Mac closes the door too.
    await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=False))
    assert (await device(f"{sid}/uploads", body, auth=token())).status_code == 403


@pytest.mark.asyncio
async def test_uploads_sign_the_declared_length_and_never_echo(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    body = {
        "series": "s1",
        "parts": [
            {"kind": "transcript", "index": 0, "bytes": 10, "sha256": "a" * 64},
            {"kind": "code", "bytes": 20, "sha256": "b" * 64},
        ],
    }
    result = (await device(f"{sid}/uploads", body, auth=token())).json()
    assert result["expiresIn"] == 300
    code = result["uploads"][1]
    assert code["kind"] == "code" and "index" not in code
    url = urlparse(result["uploads"][0]["url"])
    assert url.path == f"/{await series_dir(sid)}t-000000-{'a' * 64}.gz"
    assert url.path.startswith(f"/session-pickup/{OWNER}/{sid}/")
    assert not url.path.startswith(f"/{OWNER}/")
    query = parse_qs(url.query)
    assert query["X-Amz-Expires"] == ["300"] and "content-length" in query["X-Amz-SignedHeaders"][0]
    assert urlparse(code["url"]).path.endswith("/s1/code-" + "b" * 16 + ".bundle")
    # Invalid input: 400 with nothing echoed. Over the per-part and whole-snapshot caps: refused.
    bad = {"series": "../x", "parts": body["parts"]}
    result = await device(f"{sid}/uploads", bad, auth=token())
    assert result.status_code == 400 and result.content == b""
    huge = {
        "series": "s1",
        "parts": [{"kind": "transcript", "index": 0, "bytes": 65 * 1024 * 1024, "sha256": "a" * 64}],
    }
    assert (await device(f"{sid}/uploads", huge, auth=token())).status_code == 400
    many = {
        "series": "s1",
        "parts": [{"kind": "code", "bytes": 200 * 1024 * 1024, "sha256": f"{n:064x}"} for n in range(6)],
    }
    assert (await device(f"{sid}/uploads", many, auth=token())).status_code == 413


@pytest.mark.asyncio
async def test_commit_continuity_head_check_and_series_replacement(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    result = await upload_and_commit(s3, sid, chunks=(b"one\n", b"two\n"), code_blob=b"bundle-1")
    assert result.status_code == 204
    row = await repo.get_session(OWNER, sid)
    snapshot = row["pickupSnapshot"]
    assert snapshot["state"] == "ready" and snapshot["hasCode"] is True
    assert snapshot["repo"] == "github.com/acme/app" and snapshot["branch"] == "feat/x"
    public = (await service.get_session(BOB, sid)).model_dump(by_alias=True)["pickup"]
    assert public["state"] == "ready" and public["hasCode"] is True and "expiresAt" not in public
    stored = await messaging_repo._get(row["pk"], "PICKUP")
    assert (stored["nextIndex"], stored["offset"]) == (2, 8)

    # The same series continues exactly where it stopped, or gets the 409 with where that is.
    wrong = await upload_and_commit(s3, sid, chunks=(b"three\n",), start_index=5, start=8)
    assert wrong.status_code == 409 and wrong.json() == {"series": "s1", "nextIndex": 2, "offset": 8}
    wrong = await upload_and_commit(s3, sid, chunks=(b"three\n",), start_index=2, start=7)
    assert wrong.status_code == 409
    right = await upload_and_commit(s3, sid, chunks=(b"three\n",), start_index=2, start=8)
    assert right.status_code == 204
    stored = await messaging_repo._get(row["pk"], "PICKUP")
    assert [p["index"] for p in stored["parts"]] == [0, 1, 2] and stored["code"]["sha256"]  # null code kept it

    # A committed object that is missing or the wrong size is refused before anything is written.
    blob, meta = part(3, b"four\n", 14)
    s3.put_object(Bucket=BUCKET, Key=f"{await series_dir(sid)}t-000003-{meta['sha256']}.gz", Body=blob + b"x")
    body = {
        "series": "s1",
        "conversationId": CONVERSATION,
        "claudeVersion": "2.1.283",
        "cwdName": "lightbulb",
        "cwdRelative": "",
        "ended": False,
        "transcript": {"parts": [meta], "totalBytes": 19},
    }
    assert (await device(f"{sid}/commit", body, auth=token())).status_code == 400
    assert (await device(f"{sid}/commit", {**body, "conversationId": "other"}, auth=token())).status_code == 400

    # A new series replaces the snapshot; the sweeper deletes the old one's objects.
    fresh = await upload_and_commit(s3, sid, series="s2", chunks=(b"one\ntwo\nthree\n",))
    assert fresh.status_code == 204
    await sweep_later()
    assert all("/s2/" in k for k in keys(s3, f"session-pickup/{OWNER}/{sid}/"))
    stored = await messaging_repo._get(row["pk"], "PICKUP")
    assert stored["series"] == "s2" and stored["code"] is None
    assert (await repo.get_session(OWNER, sid))["pickupSnapshot"]["hasCode"] is False


@pytest.mark.asyncio
async def test_sidecars_skipped_drops_the_stored_archive(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    assert (await upload_and_commit(s3, sid, chunks=(b"one\n",), sidecars_blob=b"tar-1")).status_code == 204
    pk = repo.session_pk(OWNER, sid)
    assert (await messaging_repo._get(pk, "PICKUP"))["sidecars"]["bytes"] == 5
    # Null keeps them; sidecarsSkipped says the files are gone, within the same series.
    assert (await upload_and_commit(s3, sid, chunks=(b"two\n",), start_index=1, start=4)).status_code == 204
    assert (await messaging_repo._get(pk, "PICKUP"))["sidecars"] is not None
    gone = await upload_and_commit(s3, sid, chunks=(b"three\n",), start_index=2, start=8, sidecarsSkipped="none")
    assert gone.status_code == 204
    stored = await messaging_repo._get(pk, "PICKUP")
    assert stored["series"] == "s1" and stored["sidecars"] is None
    await sweep_later()
    assert not [k for k in keys(s3, await series_dir(sid)) if "/side-" in k]


@pytest.mark.asyncio
async def test_code_skipped_none_drops_the_bundle_and_unknown_reasons_are_refused(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    assert (await upload_and_commit(s3, sid, chunks=(b"one\n",), code_blob=b"bundle")).status_code == 204
    bogus = await upload_and_commit(s3, sid, chunks=(b"two\n",), start_index=1, start=4, codeSkipped="because")
    assert bogus.status_code == 400
    gone = await upload_and_commit(s3, sid, chunks=(b"two\n",), start_index=1, start=4, codeSkipped="none")
    assert gone.status_code == 204
    assert (await messaging_repo._get(repo.session_pk(OWNER, sid), "PICKUP"))["code"] is None
    assert (await repo.get_session(OWNER, sid))["pickupSnapshot"]["hasCode"] is False
    await sweep_later()
    assert not [k for k in keys(s3, await series_dir(sid)) if "/code-" in k]


@pytest.mark.asyncio
async def test_ended_commit_and_unavailable_leave_the_want_list(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    assert (await upload_and_commit(s3, sid, ended=True, cwdName=None)).status_code == 204
    assert await messaging_repo._query(repo.user_pk(OWNER, ALICE.user_id), begins_with="PICKUPWANT#") == []
    assert (await messaging_repo._get(repo.session_pk(OWNER, sid), "PICKUP"))["ended"] is True

    await pickup.set_pickup(ALICE, sid, False)
    await pickup.set_pickup(ALICE, sid, True)
    result = await device(f"{sid}/unavailable", {"reason": "too-large"}, auth=token())
    assert result.status_code == 204
    public = (await service.get_session(BOB, sid)).model_dump(by_alias=True)["pickup"]
    assert public["state"] == "unavailable" and public["reason"] == "too-large"
    assert await messaging_repo._query(repo.user_pk(OWNER, ALICE.user_id), begins_with="PICKUPWANT#") == []
    assert (await device(f"{sid}/unavailable", {"reason": "other"}, auth=token())).status_code == 400


# --- Receiving ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_permission_idempotency_and_expiry(transport, s3):
    sid = await claude_session(transport)
    request_id = str(uuid.uuid4())
    with pytest.raises(ApiProblem, match="off"):
        await pickup.request_pickup(BOB, sid, request_id)
    await pickup.set_pickup(ALICE, sid, True)
    with pytest.raises(ApiProblem, match="not ready"):
        await pickup.request_pickup(BOB, sid, request_id)
    await upload_and_commit(s3, sid, chunks=(b"one\n", b"two\n"), code_blob=b"bundle")

    ticket = (await pickup.request_pickup(BOB, sid, request_id)).model_dump(by_alias=True)
    assert ticket["pickupId"] == request_id
    assert ticket["session"] == {"sessionId": sid, "humanUserId": ALICE.user_id, "provider": "claude"}
    manifest = ticket["manifest"]
    assert manifest["series"] == "s1" and manifest["cwdRelative"] == "apps/backend"
    assert [p["from"] for p in manifest["parts"]] == [0, 4] and manifest["sidecars"] is None
    downloads = ticket["downloads"]
    assert [d["index"] for d in downloads["transcript"]] == [0, 1] and downloads["sidecars"] is None
    assert parse_qs(urlparse(downloads["code"]["url"]).query)["X-Amz-Expires"] == ["900"]
    got = s3.get_object(Bucket=BUCKET, Key=urlparse(downloads["transcript"][1]["url"]).path.lstrip("/"))
    assert gzip.decompress(got["Body"].read()) == b"two\n"
    row = await repo.get_session(OWNER, sid)
    request = await messaging_repo._get(row["pk"], f"PICKUPREQ#{request_id}")
    assert request["expiresAt"] - request["at"] == service.PICKUP_RETENTION_SECONDS
    assert all("hashed" not in part for part in manifest["parts"])
    # Same caller, same id: a fresh ticket. Someone else reusing it: refused. The author may pick up too.
    assert (await pickup.request_pickup(BOB, sid, request_id)).pickup_id == request_id
    with pytest.raises(ApiProblem) as reused:
        await pickup.request_pickup(ALICE, sid, request_id)
    assert reused.value.error.code == "IDEMPOTENCY_CONFLICT"
    assert (await pickup.request_pickup(ALICE, sid, str(uuid.uuid4()))).session.session_id == sid

    # Paused: not found for a teammate.
    await service.set_sharing(ALICE, sid, False)
    with pytest.raises(ApiProblem) as hidden:
        await pickup.request_pickup(BOB, sid, request_id)
    assert hidden.value.error.code == "LOCAL_SESSION_NOT_FOUND"
    await service.set_sharing(ALICE, sid, True)

    ended_at = int(time.time()) - 8 * 24 * 3600
    ended = {"type": "local.session.ended", "sessionId": sid, "generation": "generation-1", "reason": "exited"}
    await relay(transport).handle_message("conn-1", {**ended, "endedAt": ended_at})
    with pytest.raises(ApiProblem, match="expired"):
        await pickup.request_pickup(BOB, sid, request_id)
    assert (await service.get_session(BOB, sid)).model_dump(by_alias=True)["pickup"]["expiresAt"] == service.iso(
        ended_at + 7 * 24 * 3600
    )


@pytest.mark.asyncio
async def test_a_reported_pick_up_is_announced_once(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    await upload_and_commit(s3, sid)
    request_id = str(uuid.uuid4())
    await pickup.request_pickup(BOB, sid, request_id)
    with patch("core.services.local_session_relay.publish_resources_changed", new=AsyncMock()) as hint:
        await pickup.report_outcome(BOB, sid, request_id, pickup.PickupOutcome(ok=False, code="REPO_NOT_FOUND"))
        hint.assert_not_awaited()  # a failure writes no audit, so there is nothing to show
        await pickup.report_outcome(BOB, sid, request_id, pickup.PickupOutcome(ok=True, branch="pickup/x-1"))
        await pickup.report_outcome(BOB, sid, request_id, pickup.PickupOutcome(ok=True, branch="pickup/x-1"))
    hint.assert_awaited_once_with(OWNER, ALICE.user_id)


@pytest.mark.asyncio
async def test_outcome_audits_once_and_lineage_stamps_only_a_first_registration(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    await upload_and_commit(s3, sid)
    request_id = str(uuid.uuid4())
    await pickup.request_pickup(BOB, sid, request_id)
    with pytest.raises(ApiProblem, match="not found"):
        await pickup.report_outcome(ALICE, sid, request_id, pickup.PickupOutcome(ok=True))  # not ALICE's ticket

    await pickup.report_outcome(BOB, sid, request_id, pickup.PickupOutcome(ok=False, code="REPO_NOT_FOUND"))
    new_conversation = str(uuid.uuid4())
    outcome = pickup.PickupOutcome(
        ok=True, branch="pickup/x-1", deviceId=BOB_MAC, profileFingerprint="e" * 64, conversationId=new_conversation
    )
    await pickup.report_outcome(BOB, sid, request_id, outcome)
    await pickup.report_outcome(BOB, sid, request_id, outcome)  # a retry audits nothing new
    entries = [(e.action, e.actor_user_id, e.detail) for e in (await service.list_audit(BOB, sid)).entries]
    assert entries[-1] == ("picked_up", BOB.user_id, "pickup/x-1")
    assert [a for a, _, _ in entries].count("picked_up") == 1

    # A device that is not the caller's own Mac never gets lineage.
    other = str(uuid.uuid4())
    await pickup.request_pickup(BOB, sid, other)
    forged = pickup.PickupOutcome(ok=True, deviceId=MAC, profileFingerprint="e" * 64, conversationId="x")
    await pickup.report_outcome(BOB, sid, other, forged)
    assert (
        await messaging_repo._get(
            repo.session_pk(OWNER, repo.session_id(OWNER, MAC, "claude", "e" * 64, "x")), "LINEAGE"
        )
        is None
    )

    # Bob's Mac registers the forked conversation: stamped once, on its first registration.
    await service.set_enrollment(BOB, service.EnrollmentRequest(deviceId=BOB_MAC, enabled=True))
    connect(transport, cid="conn-bob", stamp="2000", userId=BOB.user_id, deviceId=BOB_MAC)
    frame = registration(provider="claude", profileFingerprint="e" * 64, nativeConversationId=new_conversation)
    new_sid = (await relay(transport).handle_message("conn-bob", frame))["sessionId"]
    public = (await service.get_session(ALICE, new_sid)).model_dump(by_alias=True)
    assert public["pickedUpFrom"] == {"sessionId": sid, "humanUserId": ALICE.user_id}
    # An existing session is never stamped after the fact.
    messaging_repo._table().put_item(
        Item={
            "pk": repo.session_pk(OWNER, sid),
            "sk": "LINEAGE",
            "sourceSessionId": new_sid,
            "sourceHumanUserId": BOB.user_id,
        }
    )
    await relay(transport).handle_message("conn-1", registration(provider="claude", nativeConversationId=CONVERSATION))
    assert "pickedUpFrom" not in await repo.get_session(OWNER, sid)


# --- Expiry and wipe ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweeper_expires_old_gone_and_disallowed_snapshots_idempotently(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    await upload_and_commit(s3, sid)
    row = await repo.get_session(OWNER, sid)
    manifest = await messaging_repo._get(row["pk"], "PICKUP")
    committed = pickup._part_key(row, "s1", manifest["parts"][0])
    ended = {"type": "local.session.ended", "sessionId": sid, "generation": "generation-1", "reason": "exited"}
    await relay(transport).handle_message("conn-1", {**ended, "endedAt": int(time.time())})
    orphan = str(uuid.uuid4())
    s3.put_object(Bucket=BUCKET, Key=f"session-pickup/{OWNER}/{orphan}/e1/s1/t-000000.gz", Body=b"x")

    # Fresh: only the orphan (no session row) goes.
    assert await pickup.sweep_once() == 1
    assert keys(s3) == [committed]

    later = int(time.time()) + 7 * 24 * 3600 + 1
    assert await pickup.sweep_once(now=later) == 1
    assert keys(s3) == []
    row = await repo.get_session(OWNER, sid)
    assert "pickupAllowed" not in row and await messaging_repo._get(row["pk"], "PICKUP") is None
    assert [e.action for e in (await service.list_audit(BOB, sid)).entries][-1] == "pickup_expired"
    assert await pickup.sweep_once(now=later) == 0


@pytest.mark.asyncio
async def test_owner_wipe_deletes_only_that_owners_snapshots(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    await upload_and_commit(s3, sid)
    s3.put_object(Bucket=BUCKET, Key="session-pickup/org_other/x/s1/t-000000.gz", Body=b"x")
    await repo.delete_owner(OWNER)
    assert keys(s3) == ["session-pickup/org_other/x/s1/t-000000.gz"]
    assert await repo.get_session(OWNER, sid) is None


@pytest.mark.asyncio
async def test_human_routes_over_http(transport, s3):
    from fastapi.responses import JSONResponse

    from core.auth import get_current_user
    from routers.messaging import router

    sid = await claude_session(transport)
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(ApiProblem, lambda _r, exc: JSONResponse(exc.body(), status_code=exc.status_code))
    user = {"who": ALICE}
    app.dependency_overrides[get_current_user] = lambda: user["who"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.put(f"/messaging/sessions/{sid}/pickup", json={"enabled": True})
        assert result.status_code == 200 and result.json()["pickup"] == {"allowed": True, "state": "preparing"}
        assert "pickedUpFrom" not in result.json()
        await upload_and_commit(s3, sid)
        user["who"] = BOB
        request_id = str(uuid.uuid4())
        ticket = await client.post(f"/messaging/sessions/{sid}/pickups", json={"pickupId": request_id})
        assert ticket.status_code == 200
        body = ticket.json()
        assert set(body) == {"pickupId", "session", "manifest", "downloads", "expiresAt"}
        assert body["downloads"]["code"] is None and body["manifest"]["code"] is None
        outcome = await client.post(f"/messaging/sessions/{sid}/pickups/{request_id}/outcome", json={"ok": True})
        assert outcome.status_code == 204
        refused = await client.put(f"/messaging/sessions/{sid}/pickup", json={"enabled": False})
        assert refused.status_code == 403


@pytest.mark.asyncio
async def test_unavailable_never_hides_a_committed_snapshot(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    assert (await upload_and_commit(s3, sid)).status_code == 204
    before = await messaging_repo._get(repo.session_pk(OWNER, sid), "PICKUP")
    objects = keys(s3)

    result = await device(f"{sid}/unavailable", {"reason": "transcript-missing"}, auth=token())
    assert result.status_code == 204
    public = (await service.get_session(BOB, sid)).model_dump(by_alias=True)["pickup"]
    assert public["state"] == "ready" and "reason" not in public
    assert await messaging_repo._get(repo.session_pk(OWNER, sid), "PICKUP") == before
    assert keys(s3) == objects
    assert await messaging_repo._query(repo.user_pk(OWNER, ALICE.user_id), begins_with="PICKUPWANT#") == []
    assert (await pickup.request_pickup(BOB, sid, str(uuid.uuid4()))).manifest.series == "s1"


@pytest.mark.asyncio
async def test_an_expired_sessions_own_mac_retires_its_want_row(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    ended = {"type": "local.session.ended", "sessionId": sid, "generation": "generation-1", "reason": "exited"}
    await relay(transport).handle_message("conn-1", {**ended, "endedAt": int(time.time()) - 8 * 24 * 3600})
    assert keys(s3) == []  # never uploaded: invisible to the sweeper
    body = {"series": "s1", "parts": [{"kind": "transcript", "index": 0, "bytes": 10, "sha256": "a" * 64}]}

    for _ in range(2):
        result = await device(f"{sid}/uploads", body, auth=token())
        assert result.status_code == 403 and result.content == b""
    row = await repo.get_session(OWNER, sid)
    assert "pickupAllowed" not in row and "pickupSnapshot" not in row
    assert await messaging_repo._query(repo.user_pk(OWNER, ALICE.user_id), begins_with="PICKUPWANT#") == []
    actions = [e.action for e in (await service.list_audit(BOB, sid)).entries]
    assert actions.count("pickup_expired") == 1


@pytest.mark.asyncio
async def test_other_refusals_change_nothing(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    ended = {"type": "local.session.ended", "sessionId": sid, "generation": "generation-1", "reason": "exited"}
    await relay(transport).handle_message("conn-1", {**ended, "endedAt": int(time.time()) - 8 * 24 * 3600})
    await service.set_sharing(ALICE, sid, False)
    body = {"series": "s1", "parts": [{"kind": "transcript", "index": 0, "bytes": 10, "sha256": "a" * 64}]}
    # Paused (and a teammate's Mac): refused, and the expired session is left for the owner's own Mac.
    for auth in (token(), token(device=BOB_MAC)):
        assert (await device(f"{sid}/uploads", body, auth=auth)).status_code == 403
    assert (await repo.get_session(OWNER, sid))["pickupAllowed"] is True
    assert len(await messaging_repo._query(repo.user_pk(OWNER, ALICE.user_id), begins_with="PICKUPWANT#")) == 1


# --- Review fixes -------------------------------------------------------------------------------


async def _audits(sid, action) -> int:
    rows = await messaging_repo._query(repo.session_pk(OWNER, sid), begins_with="AUDIT#")
    return sum(r["action"] == action for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ["enrollment", "unpaired", "paused"])
async def test_the_author_can_always_turn_it_off(transport, s3, db, cut):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    assert (await upload_and_commit(s3, sid)).status_code == 204
    if cut == "enrollment":
        await service.set_enrollment(ALICE, service.EnrollmentRequest(deviceId=MAC, cliDeviceId=CLI, enabled=False))
    elif cut == "unpaired":
        db[1].update_item(
            Key={"owner_id": OWNER},
            UpdateExpression="REMOVE paired_devices.#mac",
            ExpressionAttributeNames={"#mac": MAC},
        )
    else:
        await service.set_sharing(ALICE, sid, False)
    with pytest.raises(ApiProblem) as refused:
        await pickup.set_pickup(BOB, sid, False)
    assert refused.value.status_code in (403, 404)
    assert (await repo.get_session(OWNER, sid))["pickupAllowed"] is True

    public = await pickup.set_pickup(ALICE, sid, False)
    assert public.pickup.allowed is False
    await pickup.set_pickup(ALICE, sid, False)  # a repeat is a no-op
    row = await repo.get_session(OWNER, sid)
    assert "pickupAllowed" not in row and "pickupSnapshot" not in row
    assert await messaging_repo._get(row["pk"], "PICKUP") is None
    assert await messaging_repo._query(repo.user_pk(OWNER, ALICE.user_id), begins_with="PICKUPWANT#") == []
    assert keys(s3) == []
    assert await _audits(sid, "pickup_off") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("lapsed_days, expired", [(8, True), (6, False), (None, False)])
async def test_a_session_that_never_ends_expires_from_its_lapsed_lease(transport, s3, db, lapsed_days, expired):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    await upload_and_commit(s3, sid)
    if lapsed_days is not None:
        lease = int(time.time()) - lapsed_days * 24 * 3600
        db[0].update_item(
            Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
            UpdateExpression="SET onlineUntil = :lease",
            ExpressionAttributeValues={":lease": lease},
        )
        public = (await service.get_session(BOB, sid)).model_dump(by_alias=True)["pickup"]
        assert public["expiresAt"] == service.iso(lease + 7 * 24 * 3600)
    else:
        assert "expiresAt" not in (await service.get_session(BOB, sid)).model_dump(by_alias=True)["pickup"]
    assert (await repo.get_session(OWNER, sid)).get("endedAt") is None
    if expired:
        with pytest.raises(ApiProblem, match="expired"):
            await pickup.request_pickup(BOB, sid, str(uuid.uuid4()))
    else:
        await pickup.request_pickup(BOB, sid, str(uuid.uuid4()))
    assert await pickup.sweep_once() == (1 if expired else 0)
    assert bool(keys(s3)) is not expired
    assert ("pickupAllowed" in await repo.get_session(OWNER, sid)) is not expired


@pytest.mark.asyncio
async def test_a_commit_raced_by_off_and_on_is_refused(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    real = repo.put_session
    raced = []

    async def racing(row, previous, enrollment, *, extra=()):
        if not raced and any(item.get("Put", {}).get("Item", {}).get("sk") == "PICKUP" for item in extra):
            raced.append(True)
            # The objects passed the size check; now the author toggles, deleting them.
            await pickup.set_pickup(ALICE, sid, False)
            await pickup.set_pickup(ALICE, sid, True)
        return await real(row, previous, enrollment, extra=extra)

    with patch.object(repo, "put_session", racing):
        result = await upload_and_commit(s3, sid)
    assert raced and result.status_code == 400
    assert await messaging_repo._get(repo.session_pk(OWNER, sid), "PICKUP") is None
    assert (await repo.get_session(OWNER, sid))["pickupSnapshot"] == {"state": "preparing"}


@pytest.mark.asyncio
async def test_uploads_below_the_committed_index_resynchronise_first(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    await upload_and_commit(s3, sid, chunks=(b"one\n", b"two\n"))
    body = {"series": "s1", "parts": [{"kind": "transcript", "index": 1, "bytes": 10, "sha256": "a" * 64}]}
    result = await device(f"{sid}/uploads", body, auth=token())
    assert result.status_code == 409 and result.json() == {"series": "s1", "nextIndex": 2, "offset": 8}
    # A new series starts at zero again.
    fresh = {"series": "s2", "parts": [{"kind": "transcript", "index": 0, "bytes": 10, "sha256": "a" * 64}]}
    assert (await device(f"{sid}/uploads", fresh, auth=token())).status_code == 200


@pytest.mark.asyncio
async def test_uploads_never_committed_are_swept_while_pickup_stays_on(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    assert (await upload_and_commit(s3, sid, code_blob=b"bundle")).status_code == 204
    committed = keys(s3)
    body = {"series": "s2", "parts": [{"kind": "transcript", "index": 0, "bytes": 1, "sha256": "a" * 64}]}
    url = (await device(f"{sid}/uploads", body, auth=token())).json()["uploads"][0]["url"]
    s3.put_object(Bucket=BUCKET, Key=urlparse(url).path.lstrip("/"), Body=b"x")  # never committed

    assert await pickup.sweep_once() == 0
    assert len(keys(s3)) == len(committed) + 1  # young: its commit may still be on the way
    assert await pickup.sweep_once(now=int(time.time()) + pickup.ABANDONED_UPLOAD_S + 1) == 0
    assert keys(s3) == committed
    assert (await repo.get_session(OWNER, sid))["pickupAllowed"] is True
    assert (await pickup.request_pickup(BOB, sid, str(uuid.uuid4()))).manifest.series == "s1"


@pytest.mark.asyncio
async def test_turning_it_off_never_deletes_a_snapshot_committed_after(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    assert (await upload_and_commit(s3, sid)).status_code == 204
    real = messaging_repo._transact
    raced = []

    async def racing(items):
        await real(items)
        if not raced and any(item.get("Delete", {}).get("Key", {}).get("sk") == "PICKUP" for item in items):
            raced.append(True)
            # Off has cleared the rows; on, upload and commit (same series, same bytes) land before
            # its object deletion runs.
            await pickup.set_pickup(ALICE, sid, True)
            assert (await upload_and_commit(s3, sid)).status_code == 204

    with patch.object(messaging_repo, "_transact", racing):
        await pickup.set_pickup(ALICE, sid, False)
    assert raced
    ticket = await pickup.request_pickup(BOB, sid, str(uuid.uuid4()))
    got = s3.get_object(Bucket=BUCKET, Key=urlparse(ticket.downloads.transcript[0].url).path.lstrip("/"))
    assert gzip.decompress(got["Body"].read()) == b'{"a":1}\n'
    assert keys(s3) == [urlparse(ticket.downloads.transcript[0].url).path.lstrip("/")]


@pytest.mark.asyncio
async def test_a_paused_session_cannot_be_allowed(transport, s3):
    sid = await claude_session(transport)
    await service.set_sharing(ALICE, sid, False)
    with pytest.raises(ApiProblem, match="Resume sharing") as refused:
        await pickup.set_pickup(ALICE, sid, True)
    assert refused.value.error.code == "LOCAL_SESSION_CONFLICT"
    assert await messaging_repo._query(repo.user_pk(OWNER, ALICE.user_id), begins_with="PICKUPWANT#") == []


@pytest.mark.asyncio
async def test_a_replaced_snapshot_outlives_the_tickets_signed_for_it(transport, s3, db):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    assert (await upload_and_commit(s3, sid, code_blob=b"bundle")).status_code == 204
    ticket = await pickup.request_pickup(BOB, sid, str(uuid.uuid4()))
    old = [urlparse(d.url).path.lstrip("/") for d in (*ticket.downloads.transcript, ticket.downloads.code)]

    assert (await upload_and_commit(s3, sid, series="s2")).status_code == 204
    for key in old:  # the receiver is still mid pick-up
        s3.get_object(Bucket=BUCKET, Key=key)

    # Objects past the abandoned-upload age still wait out the ticket window after a replacement.
    later = int(time.time()) + pickup.ABANDONED_UPLOAD_S + 1
    db[0].update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "PICKUP"},
        UpdateExpression="SET replacedAt = :at",
        ExpressionAttributeValues={":at": later},
    )
    await pickup.sweep_once(now=later)
    assert set(old) <= set(keys(s3))
    await pickup.sweep_once(now=later + pickup.URL_SECONDS)
    row = await repo.get_session(OWNER, sid)
    manifest = await messaging_repo._get(row["pk"], "PICKUP")
    assert keys(s3) == [pickup._part_key(row, "s2", manifest["parts"][0])]


@pytest.mark.asyncio
async def test_unavailable_never_overwrites_a_snapshot_committed_meanwhile(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    real = repo.put_session
    raced = []

    async def racing(row, previous, enrollment, *, extra=()):
        if not raced and (row.get("pickupSnapshot") or {}).get("state") == "unavailable":
            raced.append(True)
            # /unavailable found no manifest; the first commit lands before its write.
            assert (await upload_and_commit(s3, sid)).status_code == 204
        return await real(row, previous, enrollment, extra=extra)

    with patch.object(repo, "put_session", racing):
        result = await device(f"{sid}/unavailable", {"reason": "transcript-missing"}, auth=token())
    assert raced and result.status_code == 204
    assert (await repo.get_session(OWNER, sid))["pickupSnapshot"]["state"] == "ready"
    assert (await pickup.request_pickup(BOB, sid, str(uuid.uuid4()))).manifest.series == "s1"
    assert await messaging_repo._query(repo.user_pk(OWNER, ALICE.user_id), begins_with="PICKUPWANT#") == []


@pytest.mark.asyncio
async def test_overlapping_upload_grants_cannot_replace_committed_transcript(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    older, old_part = part(0, b"older\n", 0)
    old = await device(
        f"{sid}/uploads",
        {"series": "s1", "parts": [{"kind": "transcript", **{k: old_part[k] for k in ("index", "bytes", "sha256")}}]},
        auth=token(),
    )
    assert old.status_code == 200
    old_key = urlparse(old.json()["uploads"][0]["url"]).path.lstrip("/")
    assert (await upload_and_commit(s3, sid, chunks=(b"newer\n",))).status_code == 204
    # This grant predates the winning commit but its PUT lands afterwards.
    s3.put_object(Bucket=BUCKET, Key=old_key, Body=older)
    ticket = await pickup.request_pickup(BOB, sid, str(uuid.uuid4()))
    new_key = urlparse(ticket.downloads.transcript[0].url).path.lstrip("/")
    assert new_key != old_key
    data = s3.get_object(Bucket=BUCKET, Key=new_key)["Body"].read()
    assert gzip.decompress(data) == b"newer\n"
    assert hashlib.sha256(data).hexdigest() == ticket.downloads.transcript[0].sha256


@pytest.mark.asyncio
async def test_ticket_refuses_manifest_from_a_new_permission_epoch(transport, s3, monkeypatch):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    await upload_and_commit(s3, sid)
    old_row = await repo.get_session(OWNER, sid)
    await pickup.set_pickup(ALICE, sid, False)
    await pickup.set_pickup(ALICE, sid, True)
    await upload_and_commit(s3, sid, series="new")
    # Authorization saw the old session; the later manifest read sees the new epoch.
    monkeypatch.setattr(service, "authorize_session", AsyncMock(return_value=(old_row, None)))
    with pytest.raises(ApiProblem) as changed:
        await pickup.request_pickup(BOB, sid, str(uuid.uuid4()))
    assert changed.value.error.code == "LOCAL_SESSION_CONFLICT"


@pytest.mark.asyncio
async def test_legacy_parts_stay_readable_when_digest_addressed_parts_append(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    await upload_and_commit(s3, sid, chunks=(b"old\n",), legacy_transcript=True)
    row = await repo.get_session(OWNER, sid)
    manifest = await messaging_repo._get(row["pk"], "PICKUP")
    assert "hashed" not in manifest["parts"][0]
    assert pickup.ManifestPart.model_validate(manifest["parts"][0])  # old reader's strict model
    blob, _ = part(1, b"new\n", 4)
    s3.put_object(Bucket=BUCKET, Key=pickup._key(row, "s1", "transcript", index=1), Body=blob)
    assert (await upload_and_commit(s3, sid, chunks=(b"new\n",), start_index=1, start=4)).status_code == 204
    ticket = await pickup.request_pickup(BOB, sid, str(uuid.uuid4()))
    result = [
        gzip.decompress(s3.get_object(Bucket=BUCKET, Key=urlparse(p.url).path.lstrip("/"))["Body"].read())
        for p in ticket.downloads.transcript
    ]
    assert result == [b"old\n", b"new\n"]
    manifest = await messaging_repo._get(row["pk"], "PICKUP")
    assert [part.get("hashed", False) for part in manifest["parts"]] == [False, True]
    assert pickup._manifest_keys(row, manifest) == frozenset(
        urlparse(p.url).path.lstrip("/") for p in ticket.downloads.transcript
    )


@pytest.mark.asyncio
async def test_commit_falls_back_to_legacy_when_digest_object_has_wrong_size(transport, s3):
    sid = await claude_session(transport)
    await pickup.set_pickup(ALICE, sid, True)
    row = await repo.get_session(OWNER, sid)
    assert (
        await upload_and_commit(s3, sid, chunks=(b"old\n",), legacy_transcript=True, poison_digest=True)
    ).status_code == 204
    manifest = await messaging_repo._get(row["pk"], "PICKUP")
    assert "hashed" not in manifest["parts"][0]
