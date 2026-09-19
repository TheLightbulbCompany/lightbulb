import types

import pytest

from core.services.credentials import registry
from core.services.credentials.materializers import skill_cli


@pytest.fixture
def efs(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_cli, "materializer_root", lambda: tmp_path / "org_1")
    return tmp_path


@pytest.fixture(autouse=True)
def stub_provider(monkeypatch):
    """The ``vercel_cli`` provider isn't in providers.yaml yet, so stub
    ``get_provider`` to return a provider whose ``.skill`` is the real
    ``SkillSpec`` the materializer reads (key + primaryEnv + exact pinned
    package). Keeps these tests independent of the YAML registry."""
    spec = registry.SkillSpec(key="vercel", primaryEnv="VERCEL_TOKEN", package="vercel@54.18.2")
    monkeypatch.setattr(
        "core.services.credentials.registry.get_provider",
        lambda key: types.SimpleNamespace(skill=spec),
    )


def test_write_seeds_the_skill_with_the_pinned_package(efs):
    skill_cli.write("owner_1", "vercel_cli", {"token": "vcp_secret"})

    md = (efs / "org_1" / "skills" / "vercel" / "SKILL.md").read_text()
    assert "primaryEnv: VERCEL_TOKEN" in md
    assert "{{PKG}}" not in md  # placeholder replaced
    assert "vercel@54.18.2" in md  # exact pinned package present


def test_write_no_longer_writes_the_token_anywhere(efs):
    """The token used to ride ``skills.entries.<key>.apiKey`` in openclaw.json, which is gone
    with the ECS runtime. Nothing in the seeded tree may carry it now -- a leaked copy on a
    rail nothing reads would be a credential sitting in S3 for no reason. Delivering it again
    means the shell-env store (hermes_cli.CLI_ENV_FILE); see the module docstring's KNOWN GAP."""
    skill_cli.write("owner_1", "vercel_cli", {"token": "vcp_secret"})

    for path in (efs / "org_1").rglob("*"):
        if path.is_file():
            assert "vcp_secret" not in path.read_text()


def test_remove_deletes_the_skill_dir(efs):
    skill_dir = efs / "org_1" / "skills" / "vercel"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("x")

    skill_cli.remove("owner_1", "vercel_cli", {})

    assert not skill_dir.exists()
