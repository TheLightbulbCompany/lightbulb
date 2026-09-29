import base64
import hashlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
from lightbulb import desktop_transport


def test_artifact_chunks_materialize_cloud_files_with_hash_and_no_model_base64(
    tmp_path,
):
    data = b"test" * 70000
    metadata = {
        "id": 0,
        "name": "report.pdf",
        "mimeType": "application/pdf",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    receipt = {
        "id": "receipt",
        "state": "succeeded",
        "result": {"artifacts": [metadata]},
    }
    calls = []

    def read(index, offset):
        calls.append((index, offset))
        return {
            "id": index,
            "offset": offset,
            "bytes": len(data),
            "base64": base64.b64encode(data[offset : offset + 262144]).decode(),
        }

    result = desktop_transport.materialize(receipt, read, tmp_path)
    artifact = result["result"]["artifacts"][0]
    assert pathlib.Path(artifact["path"]).read_bytes() == data
    assert pathlib.Path(artifact["path"]).is_relative_to(tmp_path / "downloads")
    assert artifact["sha256"] == metadata["sha256"]
    assert "base64" not in str(result)
    assert calls == [(0, 0), (0, 262144)]


def test_bad_artifact_hash_or_traversal_cleans_staging(tmp_path):
    for name, digest in (
        ("../escape", hashlib.sha256(b"x").hexdigest()),
        ("safe.txt", "0" * 64),
    ):
        receipt = {
            "id": "receipt",
            "state": "succeeded",
            "result": {
                "artifacts": [
                    {
                        "id": 0,
                        "name": name,
                        "mimeType": "text/plain",
                        "bytes": 1,
                        "sha256": digest,
                    }
                ]
            },
        }
        with pytest.raises(ValueError):
            desktop_transport.materialize(
                receipt,
                lambda *_: {"id": 0, "offset": 0, "bytes": 1, "base64": "eA=="},
                tmp_path,
            )
    assert not list(tmp_path.glob("downloads/desktop-browser-*"))


def test_upload_reads_only_regular_profile_files_and_binds_bytes(tmp_path):
    file = tmp_path / "document.txt"
    file.write_bytes(b"test" * 32768)
    encoded = desktop_transport.upload_files([str(file)], tmp_path)
    assert encoded[0]["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()
    assert base64.b64decode(encoded[0]["contentBase64"]) == file.read_bytes()
    link = tmp_path / "link"
    link.symlink_to(file)
    with pytest.raises(ValueError):
        desktop_transport.upload_files([str(link)], tmp_path)


def test_receipt_status_refuses_downloads_symlink_before_fetch(tmp_path, monkeypatch):
    profile = tmp_path / "agent"
    outside = tmp_path / "outside"
    profile.mkdir()
    outside.mkdir()
    (profile / "downloads").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(desktop_transport.turn_context, "_home", lambda: profile)
    data = b"private receipt bytes"
    receipt = {
        "state": "succeeded",
        "result": {
            "artifacts": [
                {
                    "id": 0,
                    "name": "receipt.txt",
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ]
        },
    }
    fetched = []

    def http(_ctx, _method, path):
        if "/artifacts/" not in path:
            return receipt
        fetched.append(path)
        return {
            "id": 0,
            "offset": 0,
            "bytes": len(data),
            "base64": base64.b64encode(data).decode(),
        }

    monkeypatch.setattr(desktop_transport, "_http", http)
    with pytest.raises((ValueError, OSError)):
        desktop_transport.receipt_status("receipt", ctx={})
    assert not fetched
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("bad_hash", [False, True])
def test_downloads_swap_cannot_redirect_writes_or_cleanup(tmp_path, bad_hash):
    profile, outside = tmp_path / "agent", tmp_path / "outside"
    profile.mkdir()
    outside.mkdir()
    data = b"x"
    metadata = [
        {
            "id": i,
            "name": "receipt.txt",
            "bytes": 1,
            "sha256": "0" * 64 if bad_hash else hashlib.sha256(data).hexdigest(),
        }
        for i in range(2)
    ]
    receipt = {"state": "succeeded", "result": {"artifacts": metadata}}

    def fetch(index, offset):
        if index == 0:
            (profile / "downloads").rename(profile / "original-downloads")
            (profile / "downloads").symlink_to(outside, target_is_directory=True)
        return {"id": index, "offset": offset, "bytes": 1, "base64": "eA=="}

    with pytest.raises((ValueError, OSError)):
        desktop_transport.materialize(receipt, fetch, profile)
    assert list(outside.iterdir()) == []
    assert list((profile / "original-downloads").iterdir()) == []


def test_staging_swap_does_not_write_or_delete_replacement_files(tmp_path):
    profile, outside = tmp_path / "agent", tmp_path / "outside"
    profile.mkdir()
    outside.mkdir()
    (outside / "0-receipt.txt").write_bytes(b"keep this")
    receipt = {
        "state": "succeeded",
        "result": {
            "artifacts": [
                {
                    "id": i,
                    "name": "receipt.txt",
                    "bytes": 1,
                    "sha256": hashlib.sha256(b"x").hexdigest(),
                }
                for i in range(2)
            ]
        },
    }

    def fetch(index, offset):
        if index == 0:
            (staging,) = (profile / "downloads").iterdir()
            staging.rename(profile / "original-staging")
            staging.symlink_to(outside, target_is_directory=True)
        return {"id": index, "offset": offset, "bytes": 1, "base64": "eA=="}

    with pytest.raises(ValueError, match="directory changed"):
        desktop_transport.materialize(receipt, fetch, profile)
    assert (outside / "0-receipt.txt").read_bytes() == b"keep this"
    assert len(list(outside.iterdir())) == 1
    assert list((profile / "original-staging").iterdir()) == []
