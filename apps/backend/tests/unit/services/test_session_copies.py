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
from tests.unit.services.test_local_sessions import ALICE, BOB, OWNER

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
    links._names.clear()
    with (
        patch("core.services.messaging.human_display_name", AsyncMock(return_value="Sam Lee")),
        patch("core.services.clerk_admin.get_user", AsyncMock(return_value={"first_name": "Sam", "last_name": "Lee"})),
    ):
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
    assert "/session-copies/" in ticket["downloads"]["transcript"][0]["url"]
    with tarfile.open(fileobj=io.BytesIO(fetch(s3, ticket["downloads"]["sidecars"]["url"])), mode="r:gz") as tar:
        assert tar.getnames() == ["subagents/a.jsonl"]  # the binary entry is dropped
        assert b"ghp_" not in tar.extractfile("subagents/a.jsonl").read()
    text = json.dumps(ticket)  # downloads (the presigned URLs) included
    assert sid not in text and ALICE.user_id not in text and OWNER not in text
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
    assert (
        ticket["downloads"]["code"] is not None
        and "/session-copies/" not in ticket["downloads"]["transcript"][0]["url"]
    )
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
    assert wrong.value.status_code == 403  # its own code: another program answered
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


@pytest.mark.asyncio
async def test_turning_the_link_off_deletes_its_copies(transport, s3, started):
    sid, token = await shared(transport, s3)
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    await links._prepare(started[0])
    assert any(k.startswith("session-copies/") for k in pickup_tests.keys(s3, "session-copies/"))
    await service.delete_link(ALICE, sid)
    assert not any(k.startswith("session-copies/") for k in pickup_tests.keys(s3, "session-copies/"))
    with pytest.raises(ApiProblem):
        await links.redeem(code, VERIFIER)


@pytest.mark.asyncio
async def test_the_sweeper_ages_out_old_copies(transport, s3, started):
    _, token = await shared(transport, s3)
    await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))
    await links._prepare(started[0])
    await pickup_tests.pickup.sweep_once()
    assert any(k.startswith("session-copies/") for k in pickup_tests.keys(s3, "session-copies/"))
    await pickup_tests.pickup.sweep_once(now=int(time.time()) + links.COPY_TTL_S + 60)
    assert not any(k.startswith("session-copies/") for k in pickup_tests.keys(s3, "session-copies/"))


LINE = b'{"gitBranch":"secret-feature","cwd":"/Users/alice/app","message":{"content":[{"type":"thinking","thinking":"hm","signature":"sig"},{"type":"text","text":"hi"}]}}\n'


@pytest.mark.asyncio
async def test_a_private_copy_drops_the_branch_and_thinking(transport, s3, started):
    _, token = await shared(transport, s3, repo_info={**PUBLIC, "public": False}, chunks=(LINE,))
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    await links._prepare(started[0])
    ticket = await links.redeem(code, VERIFIER)
    body = gzip.decompress(fetch(s3, ticket.downloads.transcript[0].url))
    assert b"secret-feature" not in body and b"gitBranch" not in body and b"thinking" not in body
    assert json.loads(body)["message"]["content"] == [{"type": "text", "text": "hi"}]


@pytest.mark.asyncio
async def test_a_public_copy_keeps_the_branch_but_drops_thinking(transport, s3, started):
    _, token = await shared(transport, s3, chunks=(LINE,))
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    await links._prepare(started[0])
    body = gzip.decompress(fetch(s3, (await links.redeem(code, VERIFIER)).downloads.transcript[0].url))
    assert b"secret-feature" in body and b"thinking" not in body


def test_a_line_past_the_cap_is_skipped_with_a_marker_even_across_parts():
    with patch.object(links, "LINE_CAP", 20):
        lines = links._Lines(None, False)
        out = lines.feed(b"x" * 15) + lines.feed(b"y" * 15) + lines.feed(b"z" * 5 + b"\n")  # one long line
        out += lines.feed(b'{"a":1}\n')
        assert out == links.OMITTED + b'{"a":1}\n'
        assert lines.carry == b"" and not lines.skipping


def test_a_sidecar_entry_streams_by_line_and_a_binary_one_is_dropped():
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tout:
        for name, body in (("a.jsonl", SECRET_LINE * 3), ("b.bin", b"\xff\xfe\x00")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            source = io.BytesIO(body)
            with patch.object(links, "CHUNK", 16):
                links._redact_entry(source, info, tout, None, False)
    out.seek(0)
    with tarfile.open(fileobj=out, mode="r:gz") as tin:
        assert tin.getnames() == ["a.jsonl"]
        text = tin.extractfile("a.jsonl").read()
    assert text.count(b"\n") == 3 and b"ghp_" not in text and b"~/app" in text


@pytest.mark.asyncio
async def test_pressing_continue_again_reuses_the_pending_copy(transport, s3, started):
    _, token = await shared(transport, s3)
    first = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    other = "w" * 43
    second = (await links.create_copy(STRANGER, token, links.challenge_of(other))).code
    assert first == second and len(started) == 1
    assert (await links.redeem(second, other)).state == "preparing"
    with pytest.raises(ApiProblem):
        await links.redeem(first, VERIFIER)  # the newest press owns the copy


@pytest.mark.asyncio
async def test_a_session_has_a_copy_cap_per_hour(transport, s3, started):
    _, token = await shared(transport, s3)
    for n in range(links.COPIES_PER_HOUR):
        taker = AuthContext(user_id=f"user_taker{n}", org_id="org_other")
        await links.create_copy(taker, token, links.challenge_of(VERIFIER))
    with pytest.raises(ApiProblem) as limited:
        await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))
    assert limited.value.status_code == 429


@pytest.mark.asyncio
async def test_one_taker_cannot_use_up_the_session_cap(transport, s3, started):
    _, token = await shared(transport, s3, audience="workspace")
    for _ in range(links.TAKER_COPIES_PER_HOUR):
        await links.create_copy(BOB, token, links.challenge_of(VERIFIER))
    with pytest.raises(ApiProblem) as limited:
        await links.create_copy(BOB, token, links.challenge_of(VERIFIER))
    assert limited.value.status_code == 429
    await links.create_copy(ALICE, token, links.challenge_of(VERIFIER))  # someone else still can


@pytest.mark.asyncio
async def test_a_copy_with_no_repo_info_drops_the_branch(transport, s3, started):
    _, token = await shared(transport, s3, repo_info=None, chunks=(LINE,))
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    await links._prepare(started[0])
    ticket = await links.redeem(code, VERIFIER)
    assert ticket.code.mode == "none"
    body = gzip.decompress(fetch(s3, ticket.downloads.transcript[0].url))
    assert b"secret-feature" not in body and b"gitBranch" not in body


@pytest.mark.asyncio
async def test_the_code_mode_is_fixed_when_the_copy_is_prepared(transport, s3, started):
    sid, token = await shared(transport, s3)
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    assert started[0]["codeMode"] == "clone"
    pk = pickup_tests.repo.session_pk(OWNER, sid)
    manifest = await pickup_tests.messaging_repo._get(pk, "PICKUP")
    manifest["repoInfo"] = {**PUBLIC, "public": False}  # a commit in between: now private
    pickup_tests.messaging_repo._table().put_item(Item=manifest)
    await links._prepare(started[0])
    ticket = (await links.redeem(code, VERIFIER)).model_dump(by_alias=True)
    assert ticket["code"] == {"mode": "none"} and ticket["cwdRelative"] == ""
    assert "acme" not in json.dumps(ticket)


@pytest.mark.asyncio
async def test_the_audit_never_names_a_raw_user_id(transport, s3, started):
    sid, token = await shared(transport, s3)
    with patch("core.services.messaging.human_display_name", AsyncMock(return_value=STRANGER.user_id)):
        await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))
    assert (await service.list_audit(ALICE, sid)).entries[-1].detail == "Someone"


@pytest.mark.asyncio
async def test_a_copy_being_worked_on_is_not_restarted(transport, s3, started):
    _, token = await shared(transport, s3)
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    item = started[0]
    links._working.add(item["copyId"])
    try:
        with patch("time.time", return_value=time.time() + links.RESTART_AFTER_S + 1):
            await links.redeem(code, VERIFIER)
        assert len(started) == 1
    finally:
        links._working.discard(item["copyId"])


@pytest.mark.asyncio
async def test_redeem_reruns_the_link_checks(transport, s3, started):
    sid, token = await shared(transport, s3)
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    await links._prepare(started[0])
    await service.put_link(ALICE, sid, "workspace")  # anyone -> workspace: an outsider's copy stops
    with pytest.raises(ApiProblem):
        await links.redeem(code, VERIFIER)
    await service.put_link(ALICE, sid, "anyone")
    assert (await links.redeem(code, VERIFIER)).state == "ready"
    await service.set_sharing(ALICE, sid, False)  # paused
    with pytest.raises(ApiProblem) as paused:
        await links.redeem(code, VERIFIER)
    assert paused.value.error.code == "LOCAL_SESSION_CONFLICT" and "paused" in paused.value.error.message


@pytest.mark.asyncio
async def test_a_member_removed_after_continue_cannot_redeem(transport, s3, started):
    from core.repositories import messaging_repo

    _, token = await shared(transport, s3, audience="workspace")
    code = (await links.create_copy(BOB, token, links.challenge_of(VERIFIER))).code
    await messaging_repo.deactivate_user_stream(OWNER, BOB.user_id)
    with pytest.raises(ApiProblem):
        await links.redeem(code, VERIFIER)


@pytest.mark.asyncio
async def test_redeem_says_why_it_refuses(transport, s3, started):
    sid, token = await shared(transport, s3)
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    await links._prepare(started[0])
    with pytest.raises(ApiProblem) as other:
        await links.redeem(code, "w" * 43)  # another program's verifier
    assert other.value.error.code == "LOCAL_SESSION_FORBIDDEN"
    refusal = ApiProblem("LOCAL_SESSION_FORBIDDEN", "Local session sharing is disabled.")
    with patch.object(service, "_enrolled", AsyncMock(side_effect=refusal)), pytest.raises(ApiProblem) as stopped:
        await links.redeem(code, VERIFIER)
    assert stopped.value.error.code == "LOCAL_SESSION_OFFLINE"
    await service.delete_link(ALICE, sid)
    with pytest.raises(ApiProblem) as off:
        await links.redeem(code, VERIFIER)
    assert off.value.error.code == "LOCAL_SESSION_NOT_FOUND" and "turned this link off" in off.value.error.message
