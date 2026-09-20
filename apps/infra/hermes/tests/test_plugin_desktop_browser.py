import base64
import hashlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
from lightbulb import desktop_browser


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

    result = desktop_browser.materialize(receipt, read, tmp_path)
    artifact = result["result"]["artifacts"][0]
    assert pathlib.Path(artifact["path"]).read_bytes() == data
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
            desktop_browser.materialize(
                receipt,
                lambda *_: {"id": 0, "offset": 0, "bytes": 1, "base64": "eA=="},
                tmp_path,
            )
    assert not list(tmp_path.glob("desktop-browser-*"))


def test_upload_reads_only_regular_profile_files_and_binds_bytes(tmp_path):
    file = tmp_path / "document.txt"
    file.write_bytes(b"test" * 32768)
    encoded = desktop_browser.upload_files([str(file)], tmp_path)
    assert encoded[0]["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()
    assert base64.b64decode(encoded[0]["contentBase64"]) == file.read_bytes()
    link = tmp_path / "link"
    link.symlink_to(file)
    with pytest.raises(ValueError):
        desktop_browser.upload_files([str(link)], tmp_path)
