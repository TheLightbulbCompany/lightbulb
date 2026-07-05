"""IdentitySeeder facet of the OpenClaw runtime adapter.

``seed_owner_agents`` is a thin, best-effort WRAP around the two existing EFS
identity/persona seeders — Friday's IDENTITY/SOUL/USER/... markdown
(``friday_identity.pre_stage_friday_identity``) and Atlas's vendored workspace
bundle (``atlas_seed.pre_stage_atlas``). These tests prove the facet drives the
SAME seeding functions with the SAME inputs (so the same files/content land on
EFS), that the brand-blind ``OwnerProfile`` DTO carries only the roster + owner
kind, and that a catastrophic failure of one seeder neither propagates nor
blocks the other (the best-effort posture the ECS path had before this facet).
"""

import dataclasses

import pytest

from core.runtime import get_runtime
from core.runtime.base import OwnerProfile


_ROSTER = [
    {"id": "friday-uuid", "role": "ceo", "name": "Friday"},
    {"id": "atlas-uuid", "role": "general", "name": "Atlas"},
]


# --- OwnerProfile: brand-blind, frozen, minimal --------------------------------


def test_owner_profile_is_frozen_dataclass():
    profile = OwnerProfile(agents=_ROSTER, owner_type="org")
    assert dataclasses.is_dataclass(profile) and profile.__dataclass_params__.frozen
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.owner_type = "personal"  # type: ignore[misc]


def test_owner_profile_defaults():
    # Empty roster + personal owner is a valid "nothing to seed yet" profile —
    # the underlying seeders already defer gracefully on an empty roster.
    profile = OwnerProfile()
    assert list(profile.agents) == []
    assert profile.owner_type == "personal"


def test_owner_profile_has_no_openclaw_rendering_fields():
    # The DTO must not leak OpenClaw markdown internals (owner_display /
    # provider_label / first_name_or_fallback live inside _resolve_owner_profile,
    # not on the brand-blind port).
    field_names = {f.name for f in dataclasses.fields(OwnerProfile)}
    assert field_names == {"agents", "owner_type"}


# --- seed_owner_agents: drives the same Friday + Atlas seeding ------------------


@pytest.mark.asyncio
async def test_seed_owner_agents_drives_friday_and_atlas(monkeypatch):
    from unittest.mock import AsyncMock

    import core.services.atlas_seed as atlas_mod
    import core.services.friday_identity as friday_mod

    sentinel_ws = object()
    # get_workspace is resolved lazily inside the adapter method; patch the
    # source so no AWS/EFS plumbing is touched in the unit test.
    import core.containers as containers_mod

    monkeypatch.setattr(containers_mod, "get_workspace", lambda: sentinel_ws)
    friday = AsyncMock()
    atlas = AsyncMock()
    monkeypatch.setattr(friday_mod, "pre_stage_friday_identity", friday)
    monkeypatch.setattr(atlas_mod, "pre_stage_atlas", atlas)

    rt = get_runtime("openclaw")
    await rt.seed_owner_agents("org_1", OwnerProfile(agents=_ROSTER, owner_type="org"))

    friday.assert_awaited_once()
    f_kwargs = friday.call_args.kwargs
    assert f_kwargs["owner_id"] == "org_1"
    assert f_kwargs["paperclip_agents"] == _ROSTER
    assert f_kwargs["workspace"] is sentinel_ws

    atlas.assert_awaited_once()
    a_kwargs = atlas.call_args.kwargs
    assert a_kwargs["owner_id"] == "org_1"
    assert a_kwargs["paperclip_agents"] == _ROSTER
    assert a_kwargs["workspace"] is sentinel_ws


@pytest.mark.asyncio
async def test_seed_owner_agents_passes_roster_as_list(monkeypatch):
    # Downstream seeders are annotated list[dict]; the facet must hand them a
    # concrete list even though OwnerProfile.agents is a generic Sequence.
    from unittest.mock import AsyncMock

    import core.containers as containers_mod
    import core.services.atlas_seed as atlas_mod
    import core.services.friday_identity as friday_mod

    monkeypatch.setattr(containers_mod, "get_workspace", lambda: object())
    friday = AsyncMock()
    monkeypatch.setattr(friday_mod, "pre_stage_friday_identity", friday)
    monkeypatch.setattr(atlas_mod, "pre_stage_atlas", AsyncMock())

    rt = get_runtime("openclaw")
    await rt.seed_owner_agents("org_1", OwnerProfile(agents=tuple(_ROSTER)))

    assert isinstance(friday.call_args.kwargs["paperclip_agents"], list)


# --- best-effort: one seeder failing never blocks the other --------------------


@pytest.mark.asyncio
async def test_friday_failure_is_swallowed_and_atlas_still_runs(monkeypatch):
    from unittest.mock import AsyncMock

    import core.containers as containers_mod
    import core.observability.metrics as metrics_mod
    import core.services.atlas_seed as atlas_mod
    import core.services.friday_identity as friday_mod

    monkeypatch.setattr(containers_mod, "get_workspace", lambda: object())
    friday = AsyncMock(side_effect=RuntimeError("boom"))
    atlas = AsyncMock()
    monkeypatch.setattr(friday_mod, "pre_stage_friday_identity", friday)
    monkeypatch.setattr(atlas_mod, "pre_stage_atlas", atlas)
    metrics: list[str] = []
    monkeypatch.setattr(metrics_mod, "put_metric", lambda name, **kw: metrics.append(name))

    rt = get_runtime("openclaw")
    # Must NOT raise — best-effort, provisioning is never blocked.
    await rt.seed_owner_agents("org_1", OwnerProfile(agents=_ROSTER))

    atlas.assert_awaited_once()
    assert "friday_identity.seed.error" in metrics


@pytest.mark.asyncio
async def test_atlas_failure_is_swallowed(monkeypatch):
    from unittest.mock import AsyncMock

    import core.containers as containers_mod
    import core.observability.metrics as metrics_mod
    import core.services.atlas_seed as atlas_mod
    import core.services.friday_identity as friday_mod

    monkeypatch.setattr(containers_mod, "get_workspace", lambda: object())
    monkeypatch.setattr(friday_mod, "pre_stage_friday_identity", AsyncMock())
    monkeypatch.setattr(atlas_mod, "pre_stage_atlas", AsyncMock(side_effect=RuntimeError("boom")))
    metrics: list[str] = []
    monkeypatch.setattr(metrics_mod, "put_metric", lambda name, **kw: metrics.append(name))

    rt = get_runtime("openclaw")
    await rt.seed_owner_agents("org_1", OwnerProfile(agents=_ROSTER))

    assert "atlas_seed.invoke_error" in metrics


# --- import-lightness tripwire: the DTO import stays pure -----------------------


def test_owner_profile_import_does_not_pull_core_containers():
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import sys\n"
        "from core.runtime.base import OwnerProfile\n"
        "OwnerProfile()\n"
        "leaked = [m for m in sys.modules if m.startswith('core.containers')]\n"
        "assert not leaked, f'core.containers leaked into base import: {leaked}'\n"
    )
    backend_dir = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=backend_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
