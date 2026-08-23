import os
import shutil
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


@pytest.mark.asyncio
async def test_apply_rolls_back_on_patch_failure(efs, tmp_path):
    user_root, patch_cfg = efs
    patch_cfg.side_effect = RuntimeError("boom")
    dest = user_root / "extensions" / "weather"
    dest.mkdir(parents=True)
    (dest / "old.js").write_text("old content")
    with pytest.raises(RuntimeError, match="boom"):
        await bp.apply_plugins(owner_id="owner_1", plugins=[_plugin(tmp_path)], chosen=None)
    # the replace was rolled back: original content restored, new content gone
    assert (dest / "old.js").read_text() == "old content"
    assert not (dest / "index.js").exists()
    assert not (user_root / "extensions" / ".bak-weather").exists()


@pytest.mark.asyncio
async def test_apply_rolls_back_fresh_install_on_patch_failure(efs, tmp_path):
    """A plugin with no prior extensions/<id> content (a fresh install, not a
    replace) must be removed entirely on rollback — there is nothing to
    restore it to."""
    user_root, patch_cfg = efs
    patch_cfg.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        await bp.apply_plugins(owner_id="owner_1", plugins=[_plugin(tmp_path)], chosen=None)
    assert not (user_root / "extensions" / "weather").exists()
    assert not (user_root / "extensions" / ".bak-weather").exists()


@pytest.mark.asyncio
async def test_apply_rolls_back_plugin_whose_own_copy_fails(efs, tmp_path):
    """(dest, backup) is tracked BEFORE copytree, not after — if the copy for
    the CURRENT plugin fails (disk full, permission, vanished source), the
    except block must still see its own pair, not just earlier plugins'."""
    user_root, patch_cfg = efs
    dest_a = user_root / "extensions" / "a"  # plugin "a": prior content, replace case
    dest_a.mkdir(parents=True)
    (dest_a / "old.js").write_text("old a")

    real_copytree = shutil.copytree
    calls = {"n": 0}

    def flaky_copytree(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # plugin "b"'s own copy — the one currently being staged
            raise OSError("disk full")
        return real_copytree(*args, **kwargs)

    with patch.object(bp.shutil, "copytree", side_effect=flaky_copytree):
        with pytest.raises(OSError, match="disk full"):
            await bp.apply_plugins(
                owner_id="owner_1", plugins=[_plugin(tmp_path, "a"), _plugin(tmp_path, "b")], chosen=None
            )
    # plugin "a" (replace, copied successfully before "b" failed): rolled back
    assert (dest_a / "old.js").read_text() == "old a"
    assert not (dest_a / "index.js").exists()
    assert not (user_root / "extensions" / ".bak-a").exists()
    # plugin "b" (fresh install, its own copytree raised): nothing left behind
    assert not (user_root / "extensions" / "b").exists()
    assert not (user_root / "extensions" / ".bak-b").exists()
    patch_cfg.assert_not_awaited()
