import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import io  # noqa: E402
import tarfile  # noqa: E402
from unittest.mock import patch  # noqa: E402

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

from core.config import settings  # noqa: E402
from core.services import apps_source  # noqa: E402


def _tar(files: dict[str, bytes], extra=()) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for info in extra:
            tf.addfile(info)
    return buf.getvalue()


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-replicas")
        with patch.object(settings, "OWNER_STATE_BUCKET", "test-replicas"):
            yield client


def _keys(s3):
    return [o["Key"] for o in s3.list_objects_v2(Bucket="test-replicas").get("Contents", [])]


@pytest.mark.asyncio
async def test_put_then_get_round_trips_and_ids_by_hash(s3):
    data = _tar({"index.html": b"<h1>hi</h1>", "src/a.ts": b"x"})
    sid = await apps_source.put("o1", "a1", commit="abc123", data=data)
    assert await apps_source.put("o1", "a1", commit="abc123", data=data) == sid
    assert len(sid) == 16
    assert _keys(s3) == [f"apps/o1/a1/src/{sid}.tar.gz"]
    assert await apps_source.get("o1", "a1", sid) == data
    head = s3.head_object(Bucket="test-replicas", Key=f"apps/o1/a1/src/{sid}.tar.gz")
    assert head["Metadata"]["commit"] == "abc123"
    assert dict(apps_source.iter_files(data)) == {"index.html": b"<h1>hi</h1>", "src/a.ts": b"x"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["node_modules/x/index.js", "a/.git/config", ".env", "web/.env.local", "./node_modules/y"]
)
async def test_refuses_node_modules_and_env_files(s3, name):
    with pytest.raises(apps_source.SourceRefused, match=name.removeprefix("./")):
        await apps_source.put("o1", "a1", commit="c", data=_tar({"ok.txt": b"1", name: b"x"}))
    assert _keys(s3) == []


@pytest.mark.asyncio
async def test_refuses_unsafe_members_and_garbage(s3):
    link = tarfile.TarInfo("l")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    dev = tarfile.TarInfo("d")
    dev.type = tarfile.CHRTYPE
    for data in (_tar({}, [link]), _tar({}, [dev]), _tar({"/abs": b"x"}), _tar({"a/../../b": b"x"}), b"not a tar"):
        with pytest.raises(apps_source.SourceRefused):
            await apps_source.put("o1", "a1", commit="c", data=data)


@pytest.mark.asyncio
async def test_size_and_bomb_caps(s3):
    with patch.object(apps_source, "MAX_BYTES", 10), pytest.raises(apps_source.SourceTooLarge):
        await apps_source.put("o1", "a1", commit="c", data=_tar({"a": b"x" * 100}))
    with patch.object(apps_source, "MAX_UNCOMPRESSED", 50), pytest.raises(apps_source.SourceRefused):
        await apps_source.put("o1", "a1", commit="c", data=_tar({"a": b"x" * 100}))
    with patch.object(apps_source, "MAX_MEMBERS", 2), pytest.raises(apps_source.SourceRefused):
        await apps_source.put("o1", "a1", commit="c", data=_tar({"a": b"1", "b": b"1", "c": b"1"}))


def test_iter_files_rejects_path_escape():
    for name in ("/etc/passwd", "../x", "a/../../x"):
        with pytest.raises(apps_source.SourceRefused):
            list(apps_source.iter_files(_tar({name: b"x"})))


def test_iter_files_strips_dot_slash_and_skips_dirs():
    d = tarfile.TarInfo("src")
    d.type = tarfile.DIRTYPE
    assert dict(apps_source.iter_files(_tar({"./a.txt": b"1"}, [d]))) == {"a.txt": b"1"}


@pytest.mark.asyncio
async def test_ids_with_slash_or_dotdot_are_rejected(s3):
    for bad in ("a/b", "..", "a..b"):
        with pytest.raises(ValueError):
            await apps_source.get(bad, "a1", "x")
        with pytest.raises(ValueError):
            await apps_source.get("o1", bad, "x")


@pytest.mark.asyncio
async def test_delete_all_removes_every_object_for_the_app_only(s3):
    for i in range(1005):
        s3.put_object(Bucket="test-replicas", Key=f"apps/o1/a1/src/{i}.tar.gz", Body=b"x")
    s3.put_object(Bucket="test-replicas", Key="apps/o1/a10/src/k.tar.gz", Body=b"x")
    await apps_source.delete_all("o1", "a1")
    assert _keys(s3) == ["apps/o1/a10/src/k.tar.gz"]
