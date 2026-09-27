"""Chrome extensions the workspace's shared browser starts with.

An agent cannot install from the Chrome Web Store into the managed browser, so an admin adds
extensions here and ``browser_control`` passes them to ``StartBrowserSession(extensions=…)``.
AgentCore downloads each ZIP from our bucket with the CALLER's credentials (the backend task
role), so every object lives under ``<owner>/browser-extensions/`` and nothing else is named.

A change applies to the NEXT session: extensions are fixed at session start.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone

import boto3

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import container_repo

#: AWS limits (browser-extensions devguide): 10 MB per ZIP, 10 per session.
MAX_BYTES = 10 * 1024 * 1024
MAX_EXTENSIONS = 10

#: Extensions we package ourselves (Web Store CRX, header stripped, re-zipped with
#: manifest.json at the root). The source object sits in the same bucket under
#: ``_catalog/browser-extensions/`` -- never a Clerk org id -- and is copied into the owner's
#: prefix on add, so a later catalog refresh never changes a workspace's pinned copy.
CATALOG = {
    "supademo": {
        "name": "Supademo recorder",
        "version": "7.5.16",
        "source": "_catalog/browser-extensions/supademo.zip",
    },
}

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class ExtensionError(Exception):
    """A safe, user-facing refusal."""


def object_key(owner: str, slug: str) -> str:
    return f"{owner}/browser-extensions/{slug}.zip"


def _s3():
    return boto3.client("s3", region_name=settings.AWS_REGION)


def _public(ext: dict) -> dict:
    return {k: ext.get(k) for k in ("slug", "name", "version", "source", "addedBy", "addedAt")}


async def list_extensions(owner: str) -> list[dict]:
    return [_public(e) for e in await container_repo.get_browser_extensions(owner) or []]


async def session_extensions(owner: str) -> list[dict]:
    """The ``extensions`` argument for StartBrowserSession (empty when none)."""
    return [
        {"location": {"s3": {"bucket": settings.OWNER_STATE_BUCKET, "prefix": e["key"]}}}
        for e in await container_repo.get_browser_extensions(owner) or []
    ]


async def _put(owner: str, entry: dict) -> dict:
    """Add or replace one entry by slug. The object is already written."""
    old = await container_repo.get_browser_extensions(owner)
    rest = [e for e in old or [] if e["slug"] != entry["slug"]]
    if len(rest) >= MAX_EXTENSIONS:
        raise ExtensionError(f"A browser can start with at most {MAX_EXTENSIONS} extensions.")
    if not await container_repo.compare_browser_extensions(owner, old, [*rest, entry]):
        raise ExtensionError("Extensions changed. Refresh and try again.")
    return _public(entry)


def _entry(owner: str, slug: str, name: str, version: str, source: str, user: str) -> dict:
    return {
        "slug": slug,
        "name": name[:120],
        "version": version[:40],
        "source": source,
        "key": object_key(owner, slug),
        "addedBy": user,
        "addedAt": datetime.now(timezone.utc).isoformat(),
    }


async def add_from_catalog(owner: str, slug: str, user: str) -> dict:
    item = CATALOG.get(slug)
    if not item:
        raise ExtensionError("That extension is not in the catalog.")
    await run_in_thread(
        _s3().copy_object,
        Bucket=settings.OWNER_STATE_BUCKET,
        Key=object_key(owner, slug),
        CopySource={"Bucket": settings.OWNER_STATE_BUCKET, "Key": item["source"]},
    )
    return await _put(owner, _entry(owner, slug, item["name"], item["version"], "catalog", user))


def read_manifest(data: bytes) -> dict:
    """Validate an uploaded ZIP the way Chrome will read it: manifest.json at the root."""
    if len(data) > MAX_BYTES:
        raise ExtensionError("Extensions must be 10 MB or smaller.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            info = zf.getinfo("manifest.json")
            if info.file_size > 1024 * 1024:
                raise ExtensionError("manifest.json is too large.")
            manifest = json.loads(zf.read(info))
    except KeyError:
        raise ExtensionError("manifest.json must be at the top of the ZIP, not inside a folder.") from None
    except (zipfile.BadZipFile, ValueError):
        raise ExtensionError("That file is not a valid extension ZIP.") from None
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 3:
        raise ExtensionError("Only Manifest V3 extensions are supported.")
    if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
        raise ExtensionError("manifest.json has no name.")
    return manifest


def slug_for(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")
    if not _SLUG.match(slug):
        raise ExtensionError("Could not derive a name for that extension.")
    return slug


async def upload(owner: str, data: bytes, user: str) -> dict:
    manifest = read_manifest(data)
    # A localized name ("__MSG_appName__") resolves only inside Chrome; fall back to short_name.
    name = next(
        (
            n
            for n in (manifest["name"], manifest.get("short_name"))
            if isinstance(n, str) and n.strip() and not n.startswith("__MSG_")
        ),
        "extension",
    )
    slug = slug_for(name)
    await run_in_thread(
        _s3().put_object,
        Bucket=settings.OWNER_STATE_BUCKET,
        Key=object_key(owner, slug),
        Body=data,
        ContentType="application/zip",
    )
    return await _put(owner, _entry(owner, slug, name, str(manifest.get("version", "")), "upload", user))


async def remove(owner: str, slug: str) -> bool:
    old = await container_repo.get_browser_extensions(owner)
    if not any(e["slug"] == slug for e in old or []):
        return False
    # List first: a session starting in between must not name an object that is gone.
    if not await container_repo.compare_browser_extensions(owner, old, [e for e in old if e["slug"] != slug]):
        raise ExtensionError("Extensions changed. Refresh and try again.")
    await run_in_thread(_s3().delete_object, Bucket=settings.OWNER_STATE_BUCKET, Key=object_key(owner, slug))
    return True
