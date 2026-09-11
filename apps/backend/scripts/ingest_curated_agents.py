"""Publish the two version-controlled first-party agent packages.

Usage:
    cd apps/backend
    uv run python -m scripts.ingest_curated_agents \
        --repo-dir marketplace/curated-agents --dry-run

The script has no source-network fetch. It skips a slug found in any listing
state and drives new rows directly through draft -> review -> published.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.repositories import marketplace_repo
from core.services.agent_export import _scan_tarball_for_safety_flags as scan_tarball
from core.services.marketplace_safety import assert_slice_scrubbed as scan_slice
from core.services.marketplace_service import _upload_artifact_to_s3
from scripts.ingest_companies_registry import ISOL8_SYSTEM_SELLER_ID, build_package_tarball

_CONTROL_FILES = frozenset({"manifest.json", "openclaw-slice.json"})


def discover_packages(repo_dir: Path) -> list[Path]:
    return sorted(
        path.parent for path in repo_dir.glob("*/manifest.json") if (path.parent / "openclaw-slice.json").is_file()
    )


def build_artifact_tarball(package_dir: Path) -> bytes:
    return build_package_tarball(
        package_dir,
        include_root=False,
        excluded_paths=_CONTROL_FILES,
    )


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


async def ingest_one(package_dir: Path, *, existing_listings: dict[str, str], dry_run: bool) -> dict:
    manifest = _read_json(package_dir / "manifest.json")
    slice_ = _read_json(package_dir / "openclaw-slice.json")
    slug = str(manifest["slug"])
    if slug in existing_listings:
        return {
            "slug": slug,
            "status": "skipped_existing",
            "existing_status": existing_listings[slug],
        }

    tar_bytes = build_artifact_tarball(package_dir)
    findings = scan_tarball(tar_bytes)
    findings.extend(dataclasses.asdict(flag) for flag in scan_slice(slice_))
    if findings:
        return {"slug": slug, "status": "blocked_safety", "findings": findings}
    if dry_run:
        return {"slug": slug, "status": "dry_run", "files": int(manifest["file_count"])}

    listing_id = f"lst_{uuid.uuid4().hex}"
    now_iso = datetime.now(timezone.utc).isoformat()
    s3_prefix, sha = await _upload_artifact_to_s3(
        listing_id=listing_id,
        version=1,
        artifact_bytes=tar_bytes,
        manifest=manifest,
        openclaw_slice=slice_,
    )
    await marketplace_repo.put_draft_v1(
        listing_id=listing_id,
        slug=slug,
        name=str(manifest["name"]),
        description_md=str(manifest["description"]),
        format="openclaw",
        price_cents=0,
        tags=list(manifest.get("tags") or []),
        category=manifest.get("category"),
        seller_id=ISOL8_SYSTEM_SELLER_ID,
        s3_prefix=s3_prefix,
        manifest_sha256=sha,
        manifest_json_str=json.dumps(manifest),
        now_iso=now_iso,
    )
    await marketplace_repo.update_artifact_fields(
        listing_id=listing_id,
        version=1,
        seller_id=ISOL8_SYSTEM_SELLER_ID,
        sha=sha,
        manifest=manifest,
        s3_prefix=s3_prefix,
        now_iso=now_iso,
    )
    await marketplace_repo.mark_review(
        listing_id=listing_id,
        version=1,
        seller_id=ISOL8_SYSTEM_SELLER_ID,
        now_iso=now_iso,
    )
    await marketplace_repo.mark_published(
        listing_id=listing_id,
        version=1,
        approved_by=ISOL8_SYSTEM_SELLER_ID,
        now_iso=now_iso,
    )
    existing_listings[slug] = "published"
    return {"slug": slug, "status": "published", "listing_id": listing_id}


async def ingest_all(repo_dir: Path, *, existing_listings: dict[str, str], dry_run: bool) -> list[dict]:
    summaries = []
    for package_dir in discover_packages(repo_dir):
        try:
            summary = await ingest_one(package_dir, existing_listings=existing_listings, dry_run=dry_run)
        except Exception as exc:
            summary = {"slug": package_dir.name, "status": "error", "error": str(exc)}
        print(json.dumps(summary))
        summaries.append(summary)
    return summaries


async def run(repo_dir: Path, *, dry_run: bool) -> list[dict]:
    existing = (
        {}
        if dry_run
        else {
            row["slug"]: str(row.get("status") or "")
            for row in await marketplace_repo.scan_all_listings()
            if row.get("slug")
        }
    )
    return await ingest_all(repo_dir, existing_listings=existing, dry_run=dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    summaries = asyncio.run(run(args.repo_dir, dry_run=args.dry_run))
    if any(summary.get("status") in {"error", "blocked_safety"} for summary in summaries):
        sys.exit(1)


if __name__ == "__main__":
    main()
