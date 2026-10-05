"""Copies (spec §3.5): made when Continue is pressed, redacted for outsiders, redeemed by the CLI."""

import gzip
import io
import json
import tarfile
import time
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from core.services import local_sessions as service, session_links as links
from core.services.messaging import ApiProblem
from tests.unit.services import test_session_pickup as pickup_tests
from tests.unit.services.test_local_sessions import ALICE, BOB

db = pickup_tests.db
transport = pickup_tests.transport
s3 = pickup_tests.s3
_bob_is_a_member = pickup_tests._bob_is_a_member
claude_session = pickup_tests.claude_session
upload_and_commit = pickup_tests.upload_and_commit
BUCKET = pickup_tests.BUCKET

STRANGER = AuthContext(user_id="user_elsewhere", org_id="org_other")
VERIFIER = "v" * 43
PUBLIC = {
    "remote": "github.com/acme/app",
    "branch": "main",
    "head": "a" * 40,
    "pushed": True,
    "public": True,
    "folder": "app",
}
SECRET_LINE = b'{"cwd":"/Users/alice/app","message":"token ghp_abcdefghijklmnopqrstuvwx"}\n'


@pytest.fixture(autouse=True)
def names():
    with patch("core.services.messaging.human_display_name", AsyncMock(return_value="Sam Lee")):
        yield


@pytest.fixture
def started():
    """Background preparation, run by the test when it chooses."""
    items = []
    with patch.object(links, "_start", items.append):
        yield items


def sidecars_blob() -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, body in (("subagents/a.jsonl", SECRET_LINE), ("image.png", b"\x89PNG\x00\xff")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return out.getvalue()


async def shared(transport, s3, audience="anyone", repo_info=PUBLIC, chunks=(SECRET_LINE,), **commit):
    sid = await claude_session(transport)
    token = (await service.put_link(ALICE, sid, audience)).url.split("#", 1)[1]
    assert (await upload_and_commit(s3, sid, chunks=chunks, repo=repo_info, **commit)).status_code == 204
    return sid, token


def fetch(s3, url: str) -> bytes:
    return s3.get_object(Bucket=BUCKET, Key=urlparse(url).path.lstrip("/"))["Body"].read()


@pytest.mark.asyncio
async def test_an_outsider_copy_is_redacted_and_clones_a_public_repo(transport, s3, started):
    sid, token = await shared(transport, s3, sidecars_blob=sidecars_blob())
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    assert (await links.redeem(code, VERIFIER)).state == "preparing"
    await links._prepare(started[0])
    ticket = (await links.redeem(code, VERIFIER)).model_dump(by_alias=True)
    assert ticket["from"] == "Sam"  # the names fixture answers "Sam Lee" for everyone
    assert ticket["code"] == {"mode": "clone", "remote": "github.com/acme/app", "commit": "a" * 40}
    body = gzip.decompress(fetch(s3, ticket["downloads"]["transcript"][0]["url"]))
    assert b"ghp_" not in body and b"[redacted]" in body and b'"~/app"' in body
    assert "/copies/" in ticket["downloads"]["transcript"][0]["url"]
    with tarfile.open(fileobj=io.BytesIO(fetch(s3, ticket["downloads"]["sidecars"]["url"])), mode="r:gz") as tar:
        assert tar.getnames() == ["subagents/a.jsonl"]  # the binary entry is dropped
        assert b"ghp_" not in tar.extractfile("subagents/a.jsonl").read()
    # The presigned URLs carry the S3 key (owner/session ids): the brief keeps copies under the session
    # prefix so retire and the sweeper delete them, so the id check covers the rest of the ticket.
    text = json.dumps({k: v for k, v in ticket.items() if k != "downloads"})
    assert sid not in text and ALICE.user_id not in text
    entries = (await service.list_audit(ALICE, sid)).entries
    assert (entries[-1].action, entries[-1].detail, entries[-1].subject_user_id) == (
        "session_copied",
        "Sam Lee",
        STRANGER.user_id,
    )


@pytest.mark.asyncio
async def test_a_private_repo_copy_is_conversation_only_and_names_nothing(transport, s3, started):
    _, token = await shared(transport, s3, repo_info={**PUBLIC, "public": False})
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    await links._prepare(started[0])
    ticket = (await links.redeem(code, VERIFIER)).model_dump(by_alias=True)
    assert ticket["code"] == {"mode": "none"} and ticket["cwdRelative"] == ""
    assert "acme" not in json.dumps(ticket) and "main" not in json.dumps(ticket["code"])
    body = gzip.decompress(fetch(s3, ticket["downloads"]["transcript"][0]["url"]))
    assert json.loads(body)["cwd"] == "~/<project>"  # the private folder name stays on the Mac's side


@pytest.mark.asyncio
async def test_a_member_copy_is_ready_at_once_with_the_bundle(transport, s3, started):
    sid, token = await shared(transport, s3, audience="workspace", code_blob=b"bundle-bytes")
    code = (await links.create_copy(BOB, token, links.challenge_of(VERIFIER))).code
    assert started == []
    ticket = (await links.redeem(code, VERIFIER)).model_dump(by_alias=True)
    assert ticket["code"]["mode"] == "bundle" and ticket["code"]["sessionId"] == sid
    assert ticket["downloads"]["code"] is not None and "/copies/" not in ticket["downloads"]["transcript"][0]["url"]
    assert b"ghp_" in gzip.decompress(fetch(s3, ticket["downloads"]["transcript"][0]["url"]))  # members: unredacted


@pytest.mark.asyncio
async def test_a_line_split_across_two_parts_is_redacted_whole(transport, s3, started):
    first, second = SECRET_LINE[:30], SECRET_LINE[30:]
    _, token = await shared(transport, s3, chunks=(first, second))
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    await links._prepare(started[0])
    ticket = await links.redeem(code, VERIFIER)
    joined = b"".join(gzip.decompress(fetch(s3, p.url)) for p in ticket.downloads.transcript)
    assert b"ghp_" not in joined and json.loads(joined)["cwd"] == "~/app"


@pytest.mark.asyncio
async def test_redeem_needs_the_verifier_and_the_window(transport, s3, started):
    _, token = await shared(transport, s3, audience="workspace")
    code = (await links.create_copy(BOB, token, links.challenge_of(VERIFIER))).code
    with pytest.raises(ApiProblem) as wrong:
        await links.redeem(code, "w" * 43)
    assert wrong.value.status_code == 404
    with patch("time.time", return_value=time.time() + links.CODE_WINDOW_S + 1):
        with pytest.raises(ApiProblem):
            await links.redeem(code, VERIFIER)


@pytest.mark.asyncio
async def test_turning_the_link_off_refuses_a_pending_redeem(transport, s3, started):
    sid, token = await shared(transport, s3, audience="workspace")
    code = (await links.create_copy(BOB, token, links.challenge_of(VERIFIER))).code
    await service.delete_link(ALICE, sid)
    with pytest.raises(ApiProblem):
        await links.redeem(code, VERIFIER)


@pytest.mark.asyncio
async def test_a_stalled_preparation_restarts(transport, s3, started):
    _, token = await shared(transport, s3)
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    with patch("time.time", return_value=time.time() + links.RESTART_AFTER_S + 1):
        assert (await links.redeem(code, VERIFIER)).state == "preparing"
    assert len(started) == 2


@pytest.mark.asyncio
async def test_continue_needs_the_takers_own_workspace(transport, s3):
    _, token = await shared(transport, s3)
    with pytest.raises(HTTPException) as no_org:
        await links.create_copy(AuthContext(user_id="user_new", org_id=None), token, links.challenge_of(VERIFIER))
    assert no_org.value.status_code == 409
