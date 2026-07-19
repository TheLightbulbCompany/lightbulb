"""Ingestion: discovery, deterministic tarball, row pipeline, idempotency."""

import hashlib
import io
import shutil
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.ingest_companies_registry import (
    ISOL8_SYSTEM_SELLER_ID,
    build_package_tarball,
    discover_packages,
    ingest_all,
    ingest_one,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "companies"

# Dict-shaped finding matching scan_tarball's real return contract
# (dataclasses.asdict of a marketplace_safety.SafetyFlag).
_FAKE_FINDING = {
    "pattern": "hardcoded-secret",
    "severity": "high",
    "file": "gstack/agents/x/AGENTS.md",
    "line": 3,
    "snippet": "api_key = '…'",
}


def test_discover_packages_requires_company_md(tmp_path):
    (tmp_path / "gstack").mkdir()
    (tmp_path / "gstack" / "COMPANY.md").write_text("---\nname: G\nslug: g\ndescription: d\n---\n")
    (tmp_path / "skills").mkdir()  # registry noise: no COMPANY.md
    (tmp_path / ".agents").mkdir()
    found = discover_packages(tmp_path)
    assert [p.name for p in found] == ["gstack"]


def test_tarball_is_deterministic_and_keeps_paperclip_yaml():
    b1 = build_package_tarball(FIXTURES / "gstack")
    b2 = build_package_tarball(FIXTURES / "gstack")
    assert hashlib.sha256(b1).hexdigest() == hashlib.sha256(b2).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(b1)) as tf:
        names = tf.getnames()
    assert "gstack/.paperclip.yaml" in names  # dotfile that MUST ship
    assert "gstack/COMPANY.md" in names
    assert not any(".git/" in n or ".DS_Store" in n for n in names)


def test_tarball_excludes_planted_noise(tmp_path):
    # The vendored fixture ships clean, so plant real noise to make the
    # exclusion assertion non-vacuous.
    pkg = tmp_path / "gstack"
    shutil.copytree(FIXTURES / "gstack", pkg)
    (pkg / ".git").mkdir()
    (pkg / ".git" / "config").write_text("[core]\n")
    (pkg / ".DS_Store").write_bytes(b"\x00noise")
    (pkg / "agents" / ".DS_Store").write_bytes(b"\x00noise")

    with tarfile.open(fileobj=io.BytesIO(build_package_tarball(pkg))) as tf:
        names = tf.getnames()
    assert not any(".git" in Path(n).parts or ".DS_Store" in Path(n).parts for n in names)
    assert "gstack/.paperclip.yaml" in names  # non-noise dotfile still ships


@pytest.mark.asyncio
async def test_ingest_one_pipeline(monkeypatch):
    from scripts import ingest_companies_registry as mod

    monkeypatch.setattr(mod, "scan_tarball", lambda b: [])
    upload = AsyncMock(return_value=("listings/x/v1/", "sha"))
    put_draft = AsyncMock()
    update_artifact = AsyncMock()
    mark_review = AsyncMock()
    mark_published = AsyncMock()
    monkeypatch.setattr(mod, "_upload_artifact_to_s3", upload)
    monkeypatch.setattr(mod.marketplace_repo, "put_draft_v1", put_draft)
    monkeypatch.setattr(mod.marketplace_repo, "update_artifact_fields", update_artifact)
    monkeypatch.setattr(mod.marketplace_repo, "mark_review", mark_review)
    monkeypatch.setattr(mod.marketplace_repo, "mark_published", mark_published)

    summary = await ingest_one(FIXTURES / "gstack", source_commit="abc123", existing_listings={}, dry_run=False)

    assert summary["slug"] == "gstack" and summary["status"] == "published"
    manifest = upload.call_args.kwargs["manifest"]
    assert manifest["company"]["agent_count"] == 5
    assert manifest["company"]["min_plan"] == "standard"
    assert manifest["company"]["source"] == {
        "repo": "paperclipai/companies",
        "commit": "abc123",
        "path": "gstack",
    }
    assert put_draft.call_args.kwargs["seller_id"] == ISOL8_SYSTEM_SELLER_ID
    assert put_draft.call_args.kwargs["format"] == "company"
    assert put_draft.call_args.kwargs["price_cents"] == 0
    # update_artifact_fields must run between draft and review: it flips
    # artifact_uploaded=True and rewrites manifest_json as the Map shape
    # every published-row reader expects (put_draft_v1 stores a string).
    assert update_artifact.await_count == 1
    assert update_artifact.call_args.kwargs["manifest"] == manifest
    assert update_artifact.call_args.kwargs["sha"] == "sha"
    assert update_artifact.call_args.kwargs["seller_id"] == ISOL8_SYSTEM_SELLER_ID
    assert mark_published.call_args.kwargs["approved_by"] == ISOL8_SYSTEM_SELLER_ID


@pytest.mark.asyncio
async def test_ingest_one_skips_existing_slug_and_flags_findings(monkeypatch):
    from scripts import ingest_companies_registry as mod

    skipped = await ingest_one(
        FIXTURES / "gstack", source_commit="abc", existing_listings={"gstack": "review"}, dry_run=False
    )
    assert skipped["status"] == "skipped_existing"
    # The row's status rides along so the operator can spot a stuck
    # draft/review row needing manual cleanup before a re-run.
    assert skipped["existing_status"] == "review"

    upload = AsyncMock()
    monkeypatch.setattr(mod, "_upload_artifact_to_s3", upload)
    monkeypatch.setattr(mod, "scan_tarball", lambda b: [_FAKE_FINDING])
    flagged = await ingest_one(FIXTURES / "gstack", source_commit="abc", existing_listings={}, dry_run=False)
    assert flagged["status"] == "blocked_safety"
    assert flagged["findings"] == [_FAKE_FINDING]
    upload.assert_not_awaited()  # a safety finding writes nothing


@pytest.mark.asyncio
async def test_publish_registers_slug_within_run(monkeypatch):
    """A successful publish records its slug in existing_listings so a second
    registry dir declaring the same slug this run is skipped, not double-
    published (slugs are not unique per listing_id)."""
    from scripts import ingest_companies_registry as mod

    monkeypatch.setattr(mod, "scan_tarball", lambda b: [])
    monkeypatch.setattr(mod, "_upload_artifact_to_s3", AsyncMock(return_value=("listings/x/v1/", "sha")))
    monkeypatch.setattr(mod.marketplace_repo, "put_draft_v1", AsyncMock())
    monkeypatch.setattr(mod.marketplace_repo, "update_artifact_fields", AsyncMock())
    monkeypatch.setattr(mod.marketplace_repo, "mark_review", AsyncMock())
    monkeypatch.setattr(mod.marketplace_repo, "mark_published", AsyncMock())

    existing: dict = {}
    first = await ingest_one(FIXTURES / "gstack", source_commit="abc", existing_listings=existing, dry_run=False)
    assert first["status"] == "published"
    assert existing.get("gstack") == "published"  # registered in-run

    second = await ingest_one(FIXTURES / "gstack", source_commit="abc", existing_listings=existing, dry_run=False)
    assert second["status"] == "skipped_existing"
    assert second["existing_status"] == "published"


@pytest.mark.asyncio
async def test_ingest_all_skips_excluded_packages(tmp_path, monkeypatch, capsys):
    """Out-of-scope packages (agency-agents: not deployable on any current
    tier) are skipped before any parse/write, with a visible summary line."""
    from scripts import ingest_companies_registry as mod

    for name in ("agency-agents", "beta"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "COMPANY.md").write_text(f"---\nname: {name}\nslug: {name}\ndescription: d\n---\n")

    attempted = []

    async def fake_ingest_one(pkg_dir, **kwargs):
        attempted.append(pkg_dir.name)
        return {"slug": pkg_dir.name, "status": "dry_run", "agents": 0}

    monkeypatch.setattr(mod, "ingest_one", fake_ingest_one)
    summaries = await ingest_all(tmp_path, source_commit="abc", existing_listings={}, dry_run=True)

    assert "agency-agents" not in attempted  # never parsed/written
    excluded = [s for s in summaries if s["slug"] == "agency-agents"]
    assert excluded == [{"slug": "agency-agents", "status": "skipped_excluded"}]
    assert "beta" in attempted
    assert "skipped_excluded" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_ingest_all_continues_past_package_errors(tmp_path, monkeypatch, capsys):
    from scripts import ingest_companies_registry as mod

    for name in ("alpha", "beta"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "COMPANY.md").write_text(f"---\nname: {name}\nslug: {name}\ndescription: d\n---\n")

    async def fake_ingest_one(pkg_dir, **kwargs):
        if pkg_dir.name == "alpha":
            raise RuntimeError("boom")
        return {"slug": pkg_dir.name, "status": "dry_run", "agents": 0}

    monkeypatch.setattr(mod, "ingest_one", fake_ingest_one)
    summaries = await ingest_all(tmp_path, source_commit="abc", existing_listings={}, dry_run=True)

    assert summaries[0] == {"slug": "alpha", "status": "error", "error": "boom"}
    assert summaries[1]["status"] == "dry_run"  # the failure did not stop the loop
    out_lines = capsys.readouterr().out.strip().splitlines()
    assert len(out_lines) == 2  # both summaries printed
