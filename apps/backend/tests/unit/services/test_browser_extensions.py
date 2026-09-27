"""Workspace browser extensions: validated like Chrome reads them, stored only under the
owner's prefix, and handed to StartBrowserSession."""

import copy
import io
import json
import zipfile
from unittest.mock import MagicMock

import pytest

from core.services import browser_extensions as ext


def _zip(manifest: dict | None, *, folder: str = "") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if manifest is not None:
            zf.writestr(f"{folder}manifest.json", json.dumps(manifest))
        zf.writestr(f"{folder}mark.js", "1")
    return buf.getvalue()


@pytest.fixture
def store(monkeypatch):
    row = {"list": None}

    async def get(owner):
        return copy.deepcopy(row["list"])

    async def cas(owner, expected, new):
        if row["list"] != expected:
            return False
        row["list"] = copy.deepcopy(new)
        return True

    s3 = MagicMock()
    monkeypatch.setattr(ext.container_repo, "get_browser_extensions", get)
    monkeypatch.setattr(ext.container_repo, "compare_browser_extensions", cas)
    monkeypatch.setattr(ext, "_s3", lambda: s3)
    monkeypatch.setattr(ext.settings, "OWNER_STATE_BUCKET", "bucket")
    return row, s3


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"not a zip", "not a valid extension ZIP"),
        (_zip(None), "manifest.json must be at the top"),
        (_zip({"manifest_version": 3, "name": "x"}, folder="ext/"), "manifest.json must be at the top"),
        (_zip({"manifest_version": 2, "name": "x"}), "Manifest V3"),
        (_zip({"manifest_version": 3}), "no name"),
        (b"x" * (ext.MAX_BYTES + 1), "10 MB"),
    ],
)
def test_read_manifest_refuses_what_chrome_would_not_load(data, message):
    with pytest.raises(ext.ExtensionError, match=message):
        ext.read_manifest(data)


@pytest.mark.asyncio
async def test_upload_add_remove_round_trip(store):
    row, s3 = store
    added = await ext.upload(
        "org_a", _zip({"manifest_version": 3, "name": "__MSG_n__", "short_name": "My Tool!", "version": "2"}), "u1"
    )
    assert added["slug"] == "my-tool" and added["version"] == "2" and added["source"] == "upload"
    assert s3.put_object.call_args.kwargs["Key"] == "org_a/browser-extensions/my-tool.zip"

    await ext.add_from_catalog("org_a", "supademo", "u1")
    s3.copy_object.assert_called_once_with(
        Bucket="bucket",
        Key="org_a/browser-extensions/supademo.zip",
        CopySource={"Bucket": "bucket", "Key": "_catalog/browser-extensions/supademo.zip"},
    )
    assert [e["slug"] for e in await ext.list_extensions("org_a")] == ["my-tool", "supademo"]
    assert await ext.session_extensions("org_a") == [
        {"location": {"s3": {"bucket": "bucket", "prefix": f"org_a/browser-extensions/{s}.zip"}}}
        for s in ("my-tool", "supademo")
    ]

    assert await ext.remove("org_a", "my-tool")
    assert not await ext.remove("org_a", "my-tool")
    s3.delete_object.assert_called_once_with(Bucket="bucket", Key="org_a/browser-extensions/my-tool.zip")
    assert [e["slug"] for e in row["list"]] == ["supademo"]


@pytest.mark.asyncio
async def test_catalog_and_session_caps(store):
    row, _ = store
    with pytest.raises(ext.ExtensionError, match="not in the catalog"):
        await ext.add_from_catalog("org_a", "../other", "u1")
    row["list"] = [{"slug": f"e{i}", "key": f"org_a/browser-extensions/e{i}.zip"} for i in range(ext.MAX_EXTENSIONS)]
    with pytest.raises(ext.ExtensionError, match="at most 10"):
        await ext.add_from_catalog("org_a", "supademo", "u1")
