"""Portable first-party agent packages and their narrow ingester."""

import hashlib
import io
import json
import re
import sys
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.services.agent_export import _scan_tarball_for_safety_flags
from core.services.marketplace_safety import assert_slice_scrubbed
from scripts.ingest_curated_agents import (
    ISOL8_SYSTEM_SELLER_ID,
    build_artifact_tarball,
    ingest_all,
    ingest_one,
    main,
)

CURATED = Path(__file__).resolve().parents[3] / "marketplace" / "curated-agents"
LISTING_STATES = ("draft", "review", "published", "retired", "taken_down")


@pytest.mark.parametrize("slug", ["friday", "atlas"])
def test_curated_package_is_deterministic_safe_and_portable(slug):
    package_dir = CURATED / slug
    first = build_artifact_tarball(package_dir)
    second = build_artifact_tarball(package_dir)
    manifest = json.loads((package_dir / "manifest.json").read_text())
    slice_ = json.loads((package_dir / "openclaw-slice.json").read_text())

    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
    assert _scan_tarball_for_safety_flags(first) == []
    assert assert_slice_scrubbed(slice_) == []
    assert manifest["format"] == "openclaw"
    assert manifest["requires_acp"] is False
    assert manifest["requires_desktop"] is False
    assert slice_["cron_jobs"] == []
    assert slice_["agent"]["params"]["paperclip"]["role"] == "general"

    with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as tf:
        assert sorted(tf.getnames()) == ["AGENTS.md", "IDENTITY.md", "SOUL.md"]
    package_text = b"\n".join(path.read_bytes() for path in sorted(package_dir.iterdir())).lower()
    for forbidden in (
        b"lightbulb-facts",
        b"isol8-",
        b"memory.md",
        b"heartbeat.md",
        b"user.md",
        b"bootstrap.md",
        b"@isol8.co",
    ):
        assert forbidden not in package_text
    assert re.search(rb"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", package_text) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", LISTING_STATES)
async def test_ingest_skips_slug_present_in_any_listing_state(state):
    result = await ingest_one(CURATED / "friday", existing_listings={"friday": state}, dry_run=False)
    assert result == {"slug": "friday", "status": "skipped_existing", "existing_status": state}


@pytest.mark.asyncio
async def test_publish_scans_before_free_first_party_state_transitions(monkeypatch):
    from scripts import ingest_curated_agents as mod

    calls = []

    def scan(data):
        calls.append("scan")
        return []

    def scan_slice(data):
        calls.append("scan_slice")
        return []

    async def upload(**kwargs):
        calls.append("upload")
        return "listings/new/v1/", "manifest-sha"

    def transition(name):
        async def apply(**kwargs):
            calls.append(name)

        return AsyncMock(side_effect=apply)

    monkeypatch.setattr(mod, "scan_tarball", scan)
    monkeypatch.setattr(mod, "scan_slice", scan_slice)
    monkeypatch.setattr(mod, "_upload_artifact_to_s3", AsyncMock(side_effect=upload))
    put_draft = transition("draft")
    update_artifact = transition("artifact")
    mark_review = transition("review")
    mark_published = transition("published")
    monkeypatch.setattr(mod.marketplace_repo, "put_draft_v1", put_draft)
    monkeypatch.setattr(mod.marketplace_repo, "update_artifact_fields", update_artifact)
    monkeypatch.setattr(mod.marketplace_repo, "mark_review", mark_review)
    monkeypatch.setattr(mod.marketplace_repo, "mark_published", mark_published)

    result = await ingest_one(CURATED / "atlas", existing_listings={}, dry_run=False)

    assert result["status"] == "published"
    assert calls == ["scan", "scan_slice", "upload", "draft", "artifact", "review", "published"]
    assert put_draft.await_args.kwargs["seller_id"] == ISOL8_SYSTEM_SELLER_ID
    assert put_draft.await_args.kwargs["format"] == "openclaw"
    assert put_draft.await_args.kwargs["price_cents"] == 0
    assert update_artifact.await_args.kwargs["manifest"]["requires_acp"] is False
    assert mark_published.await_args.kwargs["approved_by"] == ISOL8_SYSTEM_SELLER_ID


@pytest.mark.asyncio
async def test_dry_run_preserves_existing_founder_listings(monkeypatch):
    from scripts import ingest_curated_agents as mod

    founder_ids = {f"founder-listing-{i}" for i in range(5)}
    upload = AsyncMock()
    put_draft = AsyncMock()
    update_artifact = AsyncMock()
    mark_review = AsyncMock()
    mark_published = AsyncMock()
    mark_taken_down = AsyncMock()
    monkeypatch.setattr(mod, "_upload_artifact_to_s3", upload)
    monkeypatch.setattr(mod.marketplace_repo, "put_draft_v1", put_draft)
    monkeypatch.setattr(mod.marketplace_repo, "update_artifact_fields", update_artifact)
    monkeypatch.setattr(mod.marketplace_repo, "mark_review", mark_review)
    monkeypatch.setattr(mod.marketplace_repo, "mark_published", mark_published)
    monkeypatch.setattr(mod.marketplace_repo, "mark_taken_down", mark_taken_down)

    summaries = await ingest_all(
        CURATED,
        existing_listings={f"founder-{i}": "published" for i in range(5)},
        dry_run=True,
    )

    assert [summary["slug"] for summary in summaries] == ["atlas", "friday"]
    assert {summary["status"] for summary in summaries} == {"dry_run"}
    upload.assert_not_awaited()
    put_draft.assert_not_awaited()
    update_artifact.assert_not_awaited()
    mark_review.assert_not_awaited()
    mark_published.assert_not_awaited()
    mark_taken_down.assert_not_awaited()
    assert founder_ids.isdisjoint({summary.get("listing_id") for summary in summaries})


@pytest.mark.asyncio
async def test_run_dry_run_does_not_read_external_listing_state(monkeypatch):
    from scripts import ingest_curated_agents as mod

    scan = AsyncMock(side_effect=AssertionError("dry run must stay local"))
    monkeypatch.setattr(mod.marketplace_repo, "scan_all_listings", scan)

    summaries = await mod.run(CURATED, dry_run=True)

    assert [summary["status"] for summary in summaries] == ["dry_run", "dry_run"]
    scan.assert_not_awaited()


def test_main_exits_nonzero_after_partial_error(monkeypatch):
    from scripts import ingest_curated_agents as mod

    monkeypatch.setattr(sys, "argv", ["ingest_curated_agents", "--repo-dir", str(CURATED), "--dry-run"])
    monkeypatch.setattr(
        mod,
        "run",
        AsyncMock(return_value=[{"slug": "atlas", "status": "dry_run"}, {"slug": "friday", "status": "error"}]),
    )

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
