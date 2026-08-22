import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.containers import workspace as workspace_module  # noqa: E402
from core.services import openclaw_backup_plugins as bp  # noqa: E402
from core.services.openclaw_backup_import import PluginCandidate  # noqa: E402


@pytest.fixture
def efs(tmp_path: Path):
    fake_ws = workspace_module.Workspace(mount_path=str(tmp_path / "users"))
    with (
        patch.object(bp, "get_workspace", return_value=fake_ws),
        patch.object(bp, "patch_openclaw_config", new_callable=AsyncMock) as patch_cfg,
        patch.object(bp, "_chown_tree"),
    ):
        yield tmp_path / "users" / "owner_1", patch_cfg


def _plugin(tmp_path: Path, pid="weather", deps=False) -> PluginCandidate:
    src = tmp_path / "src" / pid
    (src / "node_modules" / "x").mkdir(parents=True)
    (src / "index.js").write_text("ok")
    (src / "package.json").write_text(
        '{"name":"%s","dependencies":{"axios":"^1"}}' % pid if deps else '{"name":"%s"}' % pid
    )
    return PluginCandidate(
        id=pid,
        package_name=pid,
        version="1.0.0",
        source_dir=src,
        has_dependencies=deps,
        config={"enabled": True, "config": {"units": "c"}},
    )


@pytest.mark.asyncio
async def test_apply_copies_source_without_node_modules_and_patches_config(efs, tmp_path):
    user_root, patch_cfg = efs
    res = await bp.apply_plugins(owner_id="owner_1", plugins=[_plugin(tmp_path)], chosen=None)
    assert res.installed == ["weather"]
    assert (user_root / "extensions" / "weather" / "index.js").is_file()
    assert not (user_root / "extensions" / "weather" / "node_modules").exists()
    patch_cfg.assert_awaited_once_with(
        "owner_1", {"plugins": {"entries": {"weather": {"enabled": True, "config": {"units": "c"}}}}}
    )


@pytest.mark.asyncio
async def test_apply_respects_chosen_and_warns_on_deps(efs, tmp_path):
    _, patch_cfg = efs
    res = await bp.apply_plugins(
        owner_id="owner_1", plugins=[_plugin(tmp_path, "a", deps=True), _plugin(tmp_path, "b")], chosen={"a"}
    )
    assert res.installed == ["a"]
    assert any("openclaw plugins install a@1.0.0" in w for w in res.warnings)
    assert list(patch_cfg.await_args.args[1]["plugins"]["entries"]) == ["a"]


@pytest.mark.asyncio
async def test_apply_skips_plugin_without_source(efs, tmp_path):
    _, patch_cfg = efs
    p = _plugin(tmp_path)
    p.source_dir = None
    res = await bp.apply_plugins(owner_id="owner_1", plugins=[p], chosen=None)
    assert res.installed == [] and any("no source" in w for w in res.warnings)
    patch_cfg.assert_not_awaited()
