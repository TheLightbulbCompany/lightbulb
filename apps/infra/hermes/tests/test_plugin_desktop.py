import base64
import hashlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
from lightbulb import desktop_resources, desktop_browser


def test_file_write_reads_profile_source_and_never_retries_unknown(
    tmp_path, monkeypatch
):
    source = tmp_path / "data.bin"
    source.write_bytes(b"\x00binary\xff")
    monkeypatch.setattr(desktop_browser.turn_context, "_home", lambda: tmp_path)
    calls = []

    def http(*args):
        calls.append(args)
        raise OSError("response lost")

    monkeypatch.setattr(desktop_browser, "_http", http)
    result = desktop_resources.file_action(
        "device",
        "generation",
        "file.write",
        {"path": "/approved/data.bin", "sourcePath": str(source)},
        ctx={"runId": "run", "agentId": "agent"},
        admission_token="token",
        request_id="00000000-0000-4000-8000-000000000001",
    )
    assert result["state"] == "unknown"
    assert len(calls) == 1
    params = calls[0][3]["params"]
    assert base64.b64decode(params["contentBase64"]) == source.read_bytes()
    assert params["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert "sourcePath" not in params
    source.unlink()
    source.symlink_to(tmp_path.parent)
    with pytest.raises(ValueError):
        desktop_resources.file_action(
            "device",
            "generation",
            "file.write",
            {"path": "/approved/data.bin", "sourcePath": str(source)},
            ctx={"runId": "run", "agentId": "agent"},
            admission_token="token",
            request_id="00000000-0000-4000-8000-000000000002",
        )
    assert len(calls) == 1


def test_mcp_call_keeps_server_and_catalog_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(
        desktop_browser,
        "_http",
        lambda *args: calls.append(args) or {"state": "queued"},
    )
    desktop_resources.mcp_call(
        "device",
        "generation",
        "my-server",
        "echo",
        {"value": 7},
        "catalog-v",
        ctx={"runId": "run", "agentId": "agent"},
        admission_token="token",
        request_id="00000000-0000-4000-8000-000000000001",
    )
    assert calls[0][3]["command"] == "mcp_call"
    assert calls[0][3]["params"] == {
        "server": "my-server",
        "tool": "echo",
        "arguments": {"value": 7},
        "catalogVersion": "catalog-v",
    }
