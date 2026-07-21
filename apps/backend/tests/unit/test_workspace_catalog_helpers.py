import io
import tarfile
from pathlib import Path

import pytest

from core.containers.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    from core import config

    config.settings.EFS_MOUNT_PATH = str(tmp_path)
    config.settings.ENVIRONMENT = "local"
    return Workspace(mount_path=str(tmp_path))


def _make_tar_with(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_extract_tarball_to_workspace_writes_files(workspace: Workspace, tmp_path: Path):
    tar_bytes = _make_tar_with(
        {
            "./IDENTITY.md": b"name: Pitch\n",
            "./uploads/hello.txt": b"world",
        }
    )
    workspace.extract_tarball_to_workspace(
        user_id="user_abc",
        agent_id="agent_new",
        tar_bytes=tar_bytes,
    )
    base = tmp_path / "user_abc" / "workspace-agent_new"
    assert (base / "IDENTITY.md").read_text() == "name: Pitch\n"
    assert (base / "uploads" / "hello.txt").read_text() == "world"


def test_read_template_sidecar_returns_none_when_absent(workspace: Workspace, tmp_path: Path):
    (tmp_path / "user_abc" / "workspace-agent_new").mkdir(parents=True)
    assert workspace.read_template_sidecar("user_abc", "agent_new") is None


def test_read_template_sidecar_returns_parsed_json(workspace: Workspace, tmp_path: Path):
    base = tmp_path / "user_abc" / "workspace-agent_new"
    base.mkdir(parents=True)
    (base / ".template").write_text('{"template_slug":"pitch","template_version":3}')
    assert workspace.read_template_sidecar("user_abc", "agent_new") == {
        "template_slug": "pitch",
        "template_version": 3,
    }


def test_read_template_sidecar_returns_none_on_corrupt_json(workspace: Workspace, tmp_path: Path):
    base = tmp_path / "user_abc" / "workspace-agent_new"
    base.mkdir(parents=True)
    (base / ".template").write_text("{not-json")
    assert workspace.read_template_sidecar("user_abc", "agent_new") is None


def test_list_workspace_agent_dirs_empty_when_missing(workspace: Workspace, tmp_path: Path):
    assert workspace.list_workspace_agent_dirs("user_abc") == []


def test_list_workspace_agent_dirs_returns_sorted_subdirs(workspace: Workspace, tmp_path: Path):
    base = tmp_path / "user_abc"
    (base / "zeta").mkdir(parents=True)
    (base / "alpha").mkdir(parents=True)
    (base / "README.md").write_text("not a dir")
    assert workspace.list_workspace_agent_dirs("user_abc") == ["alpha", "zeta"]
