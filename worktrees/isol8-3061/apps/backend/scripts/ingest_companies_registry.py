"""One-shot: mirror companies.sh packages into marketplace listings.

Usage (operator clones + pins the registry first — the script does no
network I/O of its own beyond AWS):

    git clone https://github.com/paperclipai/companies.git /tmp/pc-companies
    git -C /tmp/pc-companies rev-parse HEAD    # pass as --commit
    cd apps/backend
    uv run python -m scripts.ingest_companies_registry \
        --repo-dir /tmp/pc-companies --commit <sha> [--dry-run]

Writes published format="company" rows via the marketplace repo with the
system seller id (the seller service surface is a seller-centric
draft→review state machine; curated ingestion drives the repo transitions
directly). Idempotent: packages whose slug already has a listing are
skipped. Re-runnable against a newer registry commit, so it stays here.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import io
import json
import subprocess
import sys
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.repositories import marketplace_repo
from core.services.agent_export import _scan_tarball_for_safety_flags as scan_tarball
from core.services.company_package import parse_company_meta
from core.services.marketplace_service import _upload_artifact_to_s3

ISOL8_SYSTEM_SELLER_ID = "__isol8__"
SOURCE_REPO = "paperclipai/companies"
_EXCLUDED = {".git", ".DS_Store", "__pycache__", "node_modules"}
# Registry packages out of ingestion scope: not deployable on any current tier.
EXCLUDED_PACKAGES = {"agency-agents"}


def discover_packages(repo_dir: Path) -> list[Path]:
    return sorted(p.parent for p in repo_dir.glob("*/COMPANY.md") if p.parent.name not in _EXCLUDED)


def build_package_tarball(
    pkg_dir: Path,
    *,
    include_root: bool = True,
    excluded_paths: frozenset[str] = frozenset(),
) -> bytes:
    """Deterministic tar.gz: sorted members, zeroed mtimes/owners, and a
    zeroed gzip-header timestamp (a plain ``w:gz`` open stamps the current
    time into the header, breaking byte-identity across runs).

    Keeps dotFILES (``.paperclip.yaml`` is load-bearing for the deploy
    engine) while excluding VCS/OS noise directories.
    """
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tf:
        for path in sorted(pkg_dir.rglob("*")):
            rel = path.relative_to(pkg_dir)
            if any(part in _EXCLUDED for part in rel.parts):
                continue
            if rel.as_posix() in excluded_paths:
                continue
            arcname = f"{pkg_dir.name}/{rel}" if include_root else rel.as_posix()
            info = tf.gettarinfo(str(path), arcname=arcname)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if path.is_file():
                with path.open("rb") as fh:
                    tf.addfile(info, fh)
            else:
                tf.addfile(info)
    gz_buf = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buf, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(tar_buf.getvalue())
    return gz_buf.getvalue()


async def ingest_one(pkg_dir: Path, *, source_commit: str, existing_listings: dict[str, str], dry_run: bool) -> dict:
    meta = parse_company_meta(pkg_dir)
    if meta.slug in existing_listings:
        # Unconditional skip for ANY existing row, even a half-transitioned
        # draft/review one from a crashed run — re-attempting would mint a
        # second listing with a duplicate slug. The existing_status field
        # tells the operator which rows need manual cleanup before a re-run
        # (published = fine; anything else = stuck).
        return {
            "slug": meta.slug,
            "status": "skipped_existing",
            "existing_status": existing_listings[meta.slug],
        }

    tar_bytes = build_package_tarball(pkg_dir)
    findings = scan_tarball(tar_bytes)
    if findings:
        return {"slug": meta.slug, "status": "blocked_safety", "findings": findings}

    manifest = {
        "company": {
            **meta.to_manifest_dict(),
            "source": {"repo": SOURCE_REPO, "commit": source_commit, "path": pkg_dir.name},
        }
    }
    if dry_run:
        return {"slug": meta.slug, "status": "dry_run", "agents": meta.agent_count}

    listing_id = f"lst_{uuid.uuid4().hex}"
    now_iso = datetime.now(timezone.utc).isoformat()
    s3_prefix, sha = await _upload_artifact_to_s3(
        listing_id=listing_id,
        version=1,
        artifact_bytes=tar_bytes,
        manifest=manifest,
        openclaw_slice={},
    )
    await marketplace_repo.put_draft_v1(
        listing_id=listing_id,
        slug=meta.slug,
        name=meta.name,
        description_md=meta.description,
        format="company",
        price_cents=0,
        tags=["company"],
        category="company",
        seller_id=ISOL8_SYSTEM_SELLER_ID,
        s3_prefix=s3_prefix,
        manifest_sha256=sha,
        manifest_json_str=json.dumps(manifest),
        now_iso=now_iso,
    )
    # put_draft_v1 leaves artifact_uploaded=False and stores manifest_json
    # as a string; update_artifact_fields flips the flag and rewrites the
    # manifest as the DDB Map shape every published-row reader expects
    # (mirrors the seller create_draft → replace_artifact sequence).
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
    # Register the slug so a second registry dir declaring the same slug in
    # this run hits the skipped_existing path instead of double-publishing.
    existing_listings[meta.slug] = "published"
    return {"slug": meta.slug, "status": "published", "listing_id": listing_id}


async def ingest_all(
    repo_dir: Path, *, source_commit: str, existing_listings: dict[str, str], dry_run: bool
) -> list[dict]:
    """Run ingest_one over every discovered package, printing one summary
    line per package. A per-package exception becomes an ``error`` summary
    and the run continues — one bad package must not abort the rest.
    """
    summaries: list[dict] = []
    for pkg_dir in discover_packages(repo_dir):
        if pkg_dir.name in EXCLUDED_PACKAGES:
            summary = {"slug": pkg_dir.name, "status": "skipped_excluded"}
            print(json.dumps(summary))
            summaries.append(summary)
            continue
        try:
            summary = await ingest_one(
                pkg_dir,
                source_commit=source_commit,
                existing_listings=existing_listings,
                dry_run=dry_run,
            )
        except Exception as exc:
            summary = {"slug": pkg_dir.name, "status": "error", "error": str(exc)}
        print(json.dumps(summary))
        summaries.append(summary)
    return summaries


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-dir", type=Path, required=True)
    ap.add_argument("--commit", required=True, help="pinned registry SHA (provenance)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    head = subprocess.run(
        ["git", "-C", str(args.repo_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if head != args.commit:
        raise SystemExit(f"repo HEAD {head} != --commit {args.commit}; re-pin and retry")

    existing = {
        row["slug"]: str(row.get("status") or "")
        for row in await marketplace_repo.scan_all_listings()
        if row.get("slug")
    }
    summaries = await ingest_all(
        args.repo_dir,
        source_commit=args.commit,
        existing_listings=existing,
        dry_run=args.dry_run,
    )
    # Non-zero exit when anything errored so the operator (or a wrapping
    # shell) can't mistake a partial run for a clean one.
    if any(s.get("status") == "error" for s in summaries):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
