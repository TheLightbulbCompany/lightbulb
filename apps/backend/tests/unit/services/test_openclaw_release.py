import json
from decimal import Decimal

import pytest


def _release_row(**overrides):
    row = {
        "release_id": "2026.9.3-isol8.1+main.deadbee",
        "image": ("877352799272.dkr.ecr.us-east-1.amazonaws.com/lightbulb/openclaw-extended:2026.9.3-isol8.1-deadbee"),
        "image_digest": f"sha256:{'a' * 64}",
        "build_id": "2026.9.3-isol8.1-deadbee",
        "upstream_version": "2026.9.3",
        "fork_tag": "v2026.9.3-isol8.1",
        "fork_commit": "1c8fcb36dc72ba15c181070d770104d9dbfe26c5",
        "schema_id": "2026.9.3-isol8.1",
        "schema_sha256": "b9444496830abc0821fed561f19ddc808b87933a96638980db830322f0786e2a",
        "config_capabilities": ["browser.sessionState", "mcp.runtimeScope"],
        "qualification_id": "openclaw-9.3-founder-v1",
        "evidence_id": f"sha256:{'e' * 64}",
        "runtime_capabilities": [
            "chat.history",
            "sessions.create",
            "sessions.message.append",
            "sessions.messages.subscribe",
            "users.self",
        ],
        "qualification_capabilities": [
            "legacy-privacy",
            "machine-coexistence",
            "native-human-auth",
        ],
    }
    row.update(overrides)
    return row


def _recovery_set():
    return {
        "recovery_set_id": "owner/update/recovery-v1",
        "manifest_sha256": "3" * 64,
        "source_task_arns": ["arn:old-task"],
        "shared_db_generation": "shared-1",
        "agent_db_generations": {"main": "agent-1"},
        "efs_archive_sha256": "4" * 64,
        "workshop_archive_sha256": "5" * 64,
        "verified_at": "2026-09-09T12:00:00Z",
    }


def test_release_registry_is_empty_until_final_artifact_is_injected():
    from core.services import openclaw_release

    openclaw_release._load_releases.cache_clear()
    assert openclaw_release.available_releases() == ()


def test_release_registry_loads_only_complete_qualified_rows(tmp_path, monkeypatch):
    from core.services import openclaw_release

    registry = tmp_path / "releases.json"
    registry.write_text(json.dumps({"releases": [_release_row()]}))
    monkeypatch.setattr(openclaw_release, "_REGISTRY_PATH", registry)
    monkeypatch.setattr(openclaw_release.config_schema, "schema_errors", lambda _config, *, schema_id: [])
    monkeypatch.setattr(
        openclaw_release.config_schema,
        "schema_artifact_sha256",
        lambda schema_id: _release_row()["schema_sha256"],
    )
    openclaw_release._load_releases.cache_clear()

    release = openclaw_release.release_by_id("2026.9.3-isol8.1+main.deadbee")

    assert release.image.endswith(":2026.9.3-isol8.1-deadbee")
    assert release.digest_image.endswith(f"@sha256:{'a' * 64}")
    assert release.build_id == "2026.9.3-isol8.1-deadbee"
    assert release.config_capabilities == frozenset({"browser.sessionState", "mcp.runtimeScope"})


@pytest.mark.parametrize(
    "overrides",
    [
        {"image": "ghcr.io/thelightbulbcompany/agent:2026.9.3-isol8.1"},
        {"image": "877352799272.dkr.ecr.us-east-1.amazonaws.com/lightbulb/openclaw-extended:latest"},
        {"image_digest": f"sha256:{'z' * 64}"},
        {"fork_commit": "deadbee"},
        {"schema_sha256": "unknown"},
        {"schema_sha256": "b" * 64},
        {"config_capabilities": ["browser.sessionState", 7]},
        {"build_id": ""},
        {"qualification_id": "mutable latest"},
        {"evidence_id": "run-123"},
        {"runtime_capabilities": ["sessions.create", 7]},
        {"qualification_capabilities": ["native-human-auth"]},
    ],
)
def test_release_registry_rejects_unqualified_identity(tmp_path, monkeypatch, overrides):
    from core.services import openclaw_release

    registry = tmp_path / "releases.json"
    registry.write_text(json.dumps({"releases": [_release_row(**overrides)]}))
    monkeypatch.setattr(openclaw_release, "_REGISTRY_PATH", registry)
    monkeypatch.setattr(openclaw_release.config_schema, "schema_errors", lambda _config, *, schema_id: [])
    monkeypatch.setattr(
        openclaw_release.config_schema,
        "schema_artifact_sha256",
        lambda schema_id: _release_row()["schema_sha256"],
    )
    openclaw_release._load_releases.cache_clear()

    with pytest.raises(openclaw_release.OpenClawReleaseError):
        openclaw_release.available_releases()


def test_desired_and_active_resolution_allow_only_a_reserved_transition(tmp_path, monkeypatch):
    from core.services import openclaw_release

    registry = tmp_path / "releases.json"
    registry.write_text(json.dumps({"releases": [_release_row()]}))
    monkeypatch.setattr(openclaw_release, "_REGISTRY_PATH", registry)
    monkeypatch.setattr(openclaw_release.config_schema, "schema_errors", lambda _config, *, schema_id: [])
    monkeypatch.setattr(
        openclaw_release.config_schema,
        "schema_artifact_sha256",
        lambda schema_id: _release_row()["schema_sha256"],
    )
    openclaw_release._load_releases.cache_clear()
    target = "2026.9.3-isol8.1+main.deadbee"

    assert openclaw_release.resolve_desired_release({}) is None
    assert (
        openclaw_release.resolve_desired_release(
            {"desired_release_id": target, "release_update_id": "update-1"}
        ).release_id
        == target
    )
    with pytest.raises(openclaw_release.OpenClawReleaseError):
        openclaw_release.resolve_desired_release({"desired_release_id": target})


@pytest.mark.asyncio
async def test_config_context_is_strong_and_rejects_reservation(monkeypatch):
    from core.repositories import container_repo
    from core.services import openclaw_release

    seen = []

    async def get(owner_id, *, consistent=False):
        seen.append((owner_id, consistent))
        return {
            "owner_id": owner_id,
            "id": "container-1",
            "status": "running",
            "release_revision": Decimal("4"),
            "provision_claim_id": "claim",
            "provision_attempt_token": "attempt",
        }

    monkeypatch.setattr(container_repo, "get_by_owner_id", get)
    context = await openclaw_release.capture_config_context("owner-1")
    assert context.release_revision == 4
    assert seen == [("owner-1", True)]

    async def reserved(owner_id, *, consistent=False):
        row = await get(owner_id, consistent=consistent)
        row["release_update_id"] = "release-1"
        return row

    monkeypatch.setattr(container_repo, "get_by_owner_id", reserved)
    with pytest.raises(openclaw_release.OwnerReleaseConflict):
        await openclaw_release.capture_config_context("owner-1")


@pytest.mark.parametrize("revision", [True, Decimal("1.5"), Decimal("NaN"), -1])
def test_config_context_rejects_non_integer_revisions(revision):
    from core.services import openclaw_release

    with pytest.raises(openclaw_release.OwnerReleaseConflict):
        openclaw_release._context_from_row(
            "owner-1",
            {"owner_id": "owner-1", "id": "container-1", "release_revision": revision},
        )


def test_only_stable_finished_release_is_qualification_evidence(tmp_path, monkeypatch):
    from core.services import openclaw_release

    registry = tmp_path / "releases.json"
    registry.write_text(json.dumps({"releases": [_release_row()]}))
    monkeypatch.setattr(openclaw_release, "_REGISTRY_PATH", registry)
    monkeypatch.setattr(openclaw_release.config_schema, "schema_errors", lambda _config, *, schema_id: [])
    monkeypatch.setattr(
        openclaw_release.config_schema,
        "schema_artifact_sha256",
        lambda schema_id: _release_row()["schema_sha256"],
    )
    openclaw_release._load_releases.cache_clear()
    release_id = _release_row()["release_id"]
    acceptance = {
        "release_id": release_id,
        "image": _release_row()["image"],
        "image_digest": _release_row()["image_digest"],
        "build_id": _release_row()["build_id"],
        "upstream_version": _release_row()["upstream_version"],
        "fork_tag": _release_row()["fork_tag"],
        "fork_commit": _release_row()["fork_commit"],
        "schema_id": _release_row()["schema_id"],
        "schema_sha256": _release_row()["schema_sha256"],
        "config_capabilities": sorted(_release_row()["config_capabilities"]),
        "qualification_id": _release_row()["qualification_id"],
        "evidence_id": _release_row()["evidence_id"],
        "runtime_capabilities": sorted(_release_row()["runtime_capabilities"]),
        "qualification_capabilities": sorted(_release_row()["qualification_capabilities"]),
        "task_definition_arn": "arn:task-definition/qualified:1",
        "task_arn": "arn:task/qualified",
        "config_sha256": "b" * 64,
        "recovery_set_id": _recovery_set()["recovery_set_id"],
        "recovery_manifest_sha256": _recovery_set()["manifest_sha256"],
        "gateway": "accepted",
    }
    update = {
        "status": "applied",
        "phase": "healthy",
        "target_release_id": release_id,
        "acceptance": acceptance,
        "release_revision": 2,
        "recovery_set": _recovery_set(),
    }
    container = {
        "active_release_id": release_id,
        "desired_release_id": release_id,
        "task_definition_arn": "arn:task-definition/qualified:1",
        "release_revision": 2,
    }
    update["target_task_definition_arn"] = "arn:task-definition/qualified:1"
    update["staged_config_sha256"] = "b" * 64

    assert openclaw_release.stable_active_release_evidence(container, update) == acceptance
    assert (
        openclaw_release.stable_active_release_evidence({**container, "release_update_id": "pointer"}, update) is None
    )
    assert openclaw_release.stable_active_release_evidence(container, {**update, "status": "applying"}) is None
