"""Workspace browser extensions: validated like Chrome reads them, stored only under the
owner's prefix, and handed to StartBrowserSession."""

import base64
import copy
import io
import json
import struct
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

    async def get_row(key, consistent=False):
        return {"browser_extensions": copy.deepcopy(row["list"]), "browser_helper": row.get("helper")}

    async def set_helper(owner, stamp):
        row["helper"] = stamp

    s3 = MagicMock()
    monkeypatch.setattr(ext.container_repo, "get_browser_extensions", get)
    monkeypatch.setattr(ext.container_repo, "get_by_owner_id", get_row)
    monkeypatch.setattr(ext.container_repo, "set_browser_helper", set_helper)
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
        # An explicit id: pytest otherwise uses the 10 MB value itself as the test id, and
        # `pytest -v` in Backend CI prints that as one 10 MB log line, which wedged the
        # self-hosted runner until the job timeout on every run.
        pytest.param(b"x" * (ext.MAX_BYTES + 1), "10 MB", id="oversized"),
    ],
    # Explicit ids: a ZIP's bytes carry its write time, so byte ids differ per process and
    # pytest-xdist refuses to run when its workers collect different test ids.
    ids=["not_zip", "no_manifest", "nested_manifest", "manifest_v2", "no_name", "oversized"],
)
def test_read_manifest_refuses_what_chrome_would_not_load(data, message):
    with pytest.raises(ext.ExtensionError, match=message):
        ext.read_manifest(data)


@pytest.mark.asyncio
async def test_upload_list_remove_round_trip(store):
    row, s3 = store
    added = await ext.upload(
        "org_a", _zip({"manifest_version": 3, "name": "__MSG_n__", "short_name": "My Tool!", "version": "2"}), "u1"
    )
    assert added["slug"] == "my-tool" and added["version"] == "2" and added["source"] == "upload"
    put = s3.put_object.call_args.kwargs
    assert put["Key"] == "browser-state/org_a/extensions/my-tool.zip" and put["ContentType"] == "application/zip"
    assert [e["slug"] for e in await ext.list_extensions("org_a")] == ["my-tool"]

    assert await ext.remove("org_a", "my-tool")
    assert not await ext.remove("org_a", "my-tool")
    s3.delete_object.assert_called_once_with(Bucket="bucket", Key="browser-state/org_a/extensions/my-tool.zip")
    assert row["list"] == []


def test_localized_name_resolves_from_default_locale():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "manifest.json", json.dumps({"manifest_version": 3, "name": "__MSG_appName__", "default_locale": "en"})
        )
        zf.writestr("_locales/en/messages.json", json.dumps({"APPNAME": {"message": "Real Name"}}))
    assert ext.read_manifest(buf.getvalue())["_name"] == "Real Name"


@pytest.mark.asyncio
async def test_session_cap_leaves_room_for_the_helper(store):
    row, _ = store
    row["list"] = [{"slug": f"e{i}", "key": f"org_a/browser-extensions/e{i}.zip"} for i in range(ext.MAX_EXTENSIONS)]
    with pytest.raises(ext.ExtensionError, match="at most 9"):
        await ext.upload("org_a", _zip({"manifest_version": 3, "name": "one more"}), "u1")
    assert ext.MAX_EXTENSIONS + 1 == 10  # AWS's per-session limit, helper included


# --- Chrome Web Store ----------------------------------------------------------------------

STORE_ID_URL = "https://chromewebstore.google.com/detail/supademo/jblbcpkejogbghfdglhfjlplchcnmohm?hl=en"


def test_webstore_id_from_a_link_or_a_bare_id():
    assert ext.webstore_id(STORE_ID_URL) == "jblbcpkejogbghfdglhfjlplchcnmohm"
    assert ext.webstore_id(" JBLBCPKEJOGBGHFDGLHFJLPLCHCNMOHM ") == "jblbcpkejogbghfdglhfjlplchcnmohm"
    for bad in ("", "https://example.com", "a" * 33, "a" * 32 + " " + "b" * 32):
        with pytest.raises(ext.ExtensionError, match="Chrome Web Store link"):
            ext.webstore_id(bad)


def _varint(n: int) -> bytes:
    out = b""
    while True:
        byte, n = n & 0x7F, n >> 7
        if n:
            out += bytes([byte | 0x80])
        else:
            return out + bytes([byte])


def _ld(field: int, data: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(data)) + data


def _crx(manifest: dict, keys: list[bytes]) -> bytes:
    """A CRX3 the way the store serves one: proofs from the publisher key and Google's."""
    header = b"".join(_ld(2, _ld(1, k) + _ld(2, b"sig")) for k in keys)
    header += _ld(10000, _ld(1, b"\x00" * 16)) + _varint(4 << 3) + _varint(7)  # a varint field is skipped
    return b"Cr24" + struct.pack("<II", 3, len(header)) + header + _zip(manifest)


def test_unpack_crx_keeps_the_store_key_so_the_id_survives():
    publisher, google = b"publisher-key-der", b"google-key-der"
    ext_id = ext.crx_id(publisher)
    data = ext.unpack_crx(_crx({"manifest_version": 3, "name": "Tool"}, [google, publisher]), ext_id)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        assert zf.read("mark.js") == b"1"
    assert base64.b64decode(manifest["key"]) == publisher
    assert ext.crx_id(base64.b64decode(manifest["key"])) == ext_id
    with pytest.raises(ext.ExtensionError, match="could not be read"):
        ext.unpack_crx(_crx({"manifest_version": 3, "name": "Tool"}, [google]), ext_id)  # not that extension
    with pytest.raises(ext.ExtensionError, match="could not be read"):
        ext.unpack_crx(b"PK\x03\x04 not a crx", ext_id)


@pytest.mark.asyncio
async def test_webstore_install_refuses_mv2_and_stores_mv3(store, monkeypatch):
    row, s3 = store
    publisher = b"k"
    ext_id = ext.crx_id(publisher)
    served = {}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            assert f"id%3D{ext_id}%26uc" in url and "acceptformat=crx3" in url
            return MagicMock(status_code=200, content=served["body"])

    monkeypatch.setattr(ext.httpx, "AsyncClient", Client)
    served["body"] = _crx({"manifest_version": 2, "name": "Old"}, [publisher])
    with pytest.raises(ext.ExtensionError, match="Manifest V3"):
        await ext.add_from_webstore("org_a", ext_id, "u1")
    served["body"] = _crx({"manifest_version": 3, "name": "Tool", "version": "1.2"}, [publisher])
    added = await ext.add_from_webstore("org_a", f"https://chromewebstore.google.com/detail/x/{ext_id}", "u1")
    assert added == {**added, "slug": ext_id, "name": "Tool", "version": "1.2", "source": "webstore"}
    put = s3.put_object.call_args.kwargs
    assert put["Key"] == f"browser-state/org_a/extensions/{ext_id}.zip" and put["ContentType"] == "application/zip"


# --- the helper ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helper_token_dies_with_its_session(monkeypatch):
    """A token names its workspace AND the session it was baked into: once the row holds
    another session (restart) or none (stop, expiry), it is refused."""
    state = {"session_id": "A", "helper_nonce": "nonce-a"}

    async def get_state(owner):
        return state if owner == "org_a" else None

    monkeypatch.setattr(ext.container_repo, "get_browser_state", get_state)
    token = ext.helper_token("org_a", "nonce-a")
    assert await ext.helper_owner(token) == "org_a"
    assert await ext.helper_owner(token[:-4] + "AAAA") is None
    assert await ext.helper_owner(ext.get_fernet().encrypt(b"org_a").decode()) is None  # another Fernet use
    state.update(session_id="B", helper_nonce="nonce-b")
    assert await ext.helper_owner(token) is None
    state.update(session_id=None, helper_nonce="nonce-a")
    assert await ext.helper_owner(token) is None


@pytest.mark.asyncio
async def test_helper_zip_is_the_in_repo_extension_plus_its_config(monkeypatch):
    monkeypatch.setattr(ext.settings, "PUBLIC_API_BASE", "https://api-dev.example/")

    async def get_state(owner):
        return {"session_id": "A", "helper_nonce": "n1"}

    monkeypatch.setattr(ext.container_repo, "get_browser_state", get_state)
    with zipfile.ZipFile(io.BytesIO(ext.helper_zip("org_a", "n1"))) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        config = json.loads(zf.read("config.json"))
    assert {"manifest.json", "sw.js", "logins.js", "popup.html", "bar.css", "config.json"} <= names
    assert manifest["manifest_version"] == 3
    # config.json holds the token: pages must never be able to fetch it.
    exposed = {r for entry in manifest["web_accessible_resources"] for r in entry["resources"]}
    assert "config.json" not in exposed
    assert config["api"] == "https://api-dev.example/api/v1/browser/helper"
    assert await ext.helper_owner(config["token"]) == "org_a"


@pytest.mark.asyncio
async def test_session_config_writes_the_policy_only_when_it_changed(store):
    row, s3 = store
    config = await ext.session_config("org_a", "n1")
    puts = {c.kwargs["Key"]: c.kwargs for c in s3.put_object.call_args_list}
    assert json.loads(puts["browser-state/org_a/chrome-policy.json"]["Body"])["PasswordManagerEnabled"] is False
    # Everything a session loads stays out of <owner>/*, which the agent's runtime can write.
    assert puts["browser-state/org_a/browser-helper.zip"]["ContentType"] == "application/zip"
    assert config["enterprisePolicies"][0]["type"] == "RECOMMENDED"
    assert config["extensions"] == [
        {"location": {"s3": {"bucket": "bucket", "prefix": "browser-state/org_a/browser-helper.zip"}}}
    ]

    # The next start rewrites only the helper (its token names the new session).
    s3.reset_mock()
    await ext.session_config("org_a", "n2")
    assert [c.kwargs["Key"] for c in s3.put_object.call_args_list] == ["browser-state/org_a/browser-helper.zip"]
    row["helper"] = {"digest": "old"}
    s3.reset_mock()
    await ext.session_config("org_a", "n3")
    assert s3.put_object.call_count == 2


@pytest.mark.asyncio
async def test_an_entry_under_the_old_agent_writable_prefix_moves_before_it_loads(store):
    """#3287 stored ZIPs under <owner>/browser-extensions/, which the agent's runtime can write:
    the backend copies each to browser-state/<owner>/, rewrites its key, deletes the old one."""
    row, s3 = store
    row["list"] = [{"slug": "supademo", "key": "org_a/browser-extensions/supademo.zip"}]
    config = await ext.session_config("org_a", "n")
    assert s3.copy_object.call_args.kwargs["CopySource"] == {
        "Bucket": "bucket",
        "Key": "org_a/browser-extensions/supademo.zip",
    }
    assert row["list"] == [{"slug": "supademo", "key": "browser-state/org_a/extensions/supademo.zip"}]
    s3.delete_object.assert_called_once_with(Bucket="bucket", Key="org_a/browser-extensions/supademo.zip")
    assert config["extensions"][1] == {
        "location": {"s3": {"bucket": "bucket", "prefix": "browser-state/org_a/extensions/supademo.zip"}}
    }

    # One that cannot move is never loaded.
    from botocore.exceptions import ClientError

    row["list"] = [{"slug": "x", "key": "org_a/browser-extensions/x.zip"}]
    s3.copy_object.side_effect = ClientError({"Error": {"Code": "AccessDenied"}}, "CopyObject")
    assert await ext.list_extensions("org_a") == []
    assert (await ext.session_config("org_a", "n"))["extensions"] == [
        {"location": {"s3": {"bucket": "bucket", "prefix": "browser-state/org_a/browser-helper.zip"}}}
    ]
