# Marketplace Companies PR1 — Catalog (Ingestion + Companies Tab) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror the companies.sh registry (`paperclipai/companies`) into the Isol8 marketplace as curated `format="company"` listings with a Companies tab; deploys stay blocked ("coming soon") until PR2.

**Architecture:** A metadata parser (`core/services/company_package.py`) reads `agentcompanies/v1` packages; a one-shot ingestion script tarballs each package, safety-scans it, uploads to the existing marketplace S3 layout, and writes published listing rows via the marketplace repo with system seller `__isol8__`. All company metadata rides inside `manifest_json.company` — zero row-schema changes. Frontend adds a Companies nav tab + browse page mirroring the existing Agents browse.

**Tech Stack:** FastAPI + DynamoDB repos (backend), Next.js 16 App Router + Tailwind (frontend), pyyaml 6 (already a dep), pytest, vitest.

**Spec:** `docs/superpowers/specs/2026-07-18-marketplace-companies-design.md`

## Global Constraints

- Work on a feature branch in a git worktree OUTSIDE the iCloud Desktop tree; PR to `main`; NEVER push without the user's explicit approval.
- Backend: `cd apps/backend && uv run pytest <target> -v` per task; full `uv run pytest tests/ -q` only at the end.
- Frontend: vitest is RAM-heavy — run SINGLE FILES only (`pnpm vitest run <file>`); `tsc` typechecks test files too, so test code must typecheck.
- No PR numbers or bug references in code comments.
- Company listings are free (`price_cents=0`), curated, seller `__isol8__`. Sellers cannot create `format="company"` listings — the seller create path keeps its `openclaw|skillmd` validation untouched.
- `min_plan` rule: `"standard"` if `agent_count <= 10` else `"ultra_pro"` (constant `COMPANY_STANDARD_PLAN_MAX_AGENTS = 10`).
- Commit at the end of every task.

---

### Task 1: Company package metadata parser + gstack fixture

**Files:**
- Create: `apps/backend/core/services/company_package.py`
- Create: `apps/backend/tests/unit/services/test_company_package.py`
- Create: `apps/backend/tests/fixtures/companies/gstack/` (vendored real package)

**Interfaces:**
- Produces: `parse_company_meta(pkg_dir: Path) -> CompanyMeta`, `CompanyMeta` dataclass (fields below), `CompanyMeta.min_plan` property, `COMPANY_STANDARD_PLAN_MAX_AGENTS = 10`. Task 3's script and PR2's deploy parser build on this module.

- [ ] **Step 1: Vendor the gstack fixture**

```bash
cd /tmp && git clone --depth 1 https://github.com/paperclipai/companies.git pc-companies
cd <worktree>/apps/backend
mkdir -p tests/fixtures/companies
cp -R /tmp/pc-companies/gstack tests/fixtures/companies/gstack
# Record the pinned commit for provenance:
git -C /tmp/pc-companies rev-parse HEAD   # note this SHA — used in Task 3 and Task 5
```

Keep `gstack/LICENSE` (MIT — attribution requirement). Delete `tests/fixtures/companies/gstack/images/` (binary weight, parser never reads it).

- [ ] **Step 2: Write the failing tests**

`apps/backend/tests/unit/services/test_company_package.py`:

```python
"""Tests for the agentcompanies/v1 metadata parser."""
from pathlib import Path

import pytest

from core.services.company_package import (
    COMPANY_STANDARD_PLAN_MAX_AGENTS,
    CompanyMeta,
    parse_company_meta,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "companies"


def test_parse_gstack_real_package():
    meta = parse_company_meta(FIXTURES / "gstack")
    assert meta.slug == "gstack"
    assert meta.name == "GStack"
    assert meta.license == "MIT"
    assert meta.agent_count == 5
    assert sorted(meta.agent_slugs) == [
        "ceo", "cto", "qa-engineer", "release-engineer", "staff-engineer",
    ]
    assert meta.team_count == 1
    assert meta.skill_count == 27
    assert "Dotta" in meta.authors and "Garry Tan" in meta.authors
    # .paperclip.yaml declares an optional GH_TOKEN secret for release-engineer
    gh = [r for r in meta.env_requirements if r["name"] == "GH_TOKEN"]
    assert gh and gh[0]["requirement"] == "optional" and gh[0]["kind"] == "secret"
    assert meta.min_plan == "standard"


def test_min_plan_flips_above_threshold():
    meta = CompanyMeta(
        slug="big", name="Big", description="", version=None, license="MIT",
        authors=[], agent_count=COMPANY_STANDARD_PLAN_MAX_AGENTS + 1,
        team_count=0, skill_count=0, agent_slugs=[], team_slugs=[],
        skill_slugs=[], env_requirements=[],
    )
    assert meta.min_plan == "ultra_pro"


def test_rejects_non_company_dir(tmp_path):
    with pytest.raises(ValueError, match="COMPANY.md"):
        parse_company_meta(tmp_path)


def test_missing_optional_files_are_fine(tmp_path):
    (tmp_path / "COMPANY.md").write_text(
        "---\nname: Tiny\nslug: tiny\ndescription: d\n---\nbody\n"
    )
    meta = parse_company_meta(tmp_path)
    assert meta.slug == "tiny"
    assert meta.agent_count == 0
    assert meta.env_requirements == []
    assert meta.license is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_company_package.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.services.company_package'`

- [ ] **Step 4: Implement the parser**

`apps/backend/core/services/company_package.py`:

```python
"""Parse agentcompanies/v1 packages (the companies.sh format).

PR1 scope: metadata only — counts, frontmatter, env requirements — for
marketplace ingestion. The company-deploy engine (PR2) extends this module
with full DeployPlan parsing. Format reference: the Agent Companies
Specification (schema ``agentcompanies/v1``), vendored fixture under
``tests/fixtures/companies/``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

COMPANY_STANDARD_PLAN_MAX_AGENTS = 10


@dataclass
class CompanyMeta:
    slug: str
    name: str
    description: str
    version: str | None
    license: str | None
    authors: list[str]
    agent_count: int
    team_count: int
    skill_count: int
    agent_slugs: list[str]
    team_slugs: list[str]
    skill_slugs: list[str]
    env_requirements: list[dict]

    @property
    def min_plan(self) -> str:
        if self.agent_count <= COMPANY_STANDARD_PLAN_MAX_AGENTS:
            return "standard"
        return "ultra_pro"

    def to_manifest_dict(self) -> dict:
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "license": self.license,
            "authors": self.authors,
            "agent_count": self.agent_count,
            "team_count": self.team_count,
            "skill_count": self.skill_count,
            "agent_slugs": self.agent_slugs,
            "team_slugs": self.team_slugs,
            "skill_slugs": self.skill_slugs,
            "env_requirements": self.env_requirements,
            "min_plan": self.min_plan,
        }


def _frontmatter(md_path: Path) -> dict:
    text = md_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    data = yaml.safe_load(text[3:end])
    return data if isinstance(data, dict) else {}


def _child_slugs(pkg_dir: Path, subdir: str, marker: str) -> list[str]:
    return sorted(p.parent.name for p in pkg_dir.glob(f"{subdir}/*/{marker}"))


def _env_requirements(pkg_dir: Path) -> list[dict]:
    pc_path = pkg_dir / ".paperclip.yaml"
    if not pc_path.is_file():
        return []
    data = yaml.safe_load(pc_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for agent_slug, acfg in (data.get("agents") or {}).items():
        env = ((acfg or {}).get("inputs") or {}).get("env") or {}
        for env_name, decl in env.items():
            decl = decl or {}
            out.append(
                {
                    "name": env_name,
                    "kind": decl.get("kind", "plain"),
                    "requirement": decl.get("requirement", "optional"),
                    "agent": agent_slug,
                }
            )
    return out


def parse_company_meta(pkg_dir: Path) -> CompanyMeta:
    company_md = pkg_dir / "COMPANY.md"
    if not company_md.is_file():
        raise ValueError(f"not a company package (no COMPANY.md): {pkg_dir}")
    fm = _frontmatter(company_md)
    agent_slugs = _child_slugs(pkg_dir, "agents", "AGENTS.md")
    team_slugs = _child_slugs(pkg_dir, "teams", "TEAM.md")
    skill_slugs = _child_slugs(pkg_dir, "skills", "SKILL.md")
    authors = [
        a.get("name", "") if isinstance(a, dict) else str(a)
        for a in (fm.get("authors") or [])
    ]
    return CompanyMeta(
        slug=str(fm.get("slug") or pkg_dir.name),
        name=str(fm.get("name") or pkg_dir.name),
        description=str(fm.get("description") or ""),
        version=fm.get("version"),
        license=fm.get("license"),
        authors=[a for a in authors if a],
        agent_count=len(agent_slugs),
        team_count=len(team_slugs),
        skill_count=len(skill_slugs),
        agent_slugs=agent_slugs,
        team_slugs=team_slugs,
        skill_slugs=skill_slugs,
        env_requirements=_env_requirements(pkg_dir),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_company_package.py -v`
Expected: 4 PASS. If `test_parse_gstack_real_package` fails on a count, inspect the vendored fixture (`ls tests/fixtures/companies/gstack/agents/`) — the fixture is the source of truth; fix the assertion only if the fixture genuinely differs from the values above.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/company_package.py apps/backend/tests/unit/services/test_company_package.py apps/backend/tests/fixtures/companies/
git commit -m "feat(marketplace): agentcompanies/v1 metadata parser + gstack fixture"
```

---

### Task 2: Marketplace plumbing — accept format="company", block its deploys

**Files:**
- Modify: `apps/backend/routers/marketplace_listings.py` (search pattern ~line 132; `deploy_listing` ~line 322)
- Test: `apps/backend/tests/unit/routers/test_marketplace_company_listings.py`

**Interfaces:**
- Consumes: nothing from Task 1 (independent).
- Produces: `GET /api/v1/marketplace/listings/search?format=company` returns company rows; `POST .../deploy` on a company listing → HTTP 400 `"Company deploys are coming soon"`. PR2 replaces the 400 with the deploy engine.

- [ ] **Step 1: Write the failing tests**

First run `ls apps/backend/tests/unit/routers/ | grep -i market` and open one existing marketplace router test to copy its app/client/auth-fixture idiom exactly (conftest fixtures beat hand-rolled clients). Then write `test_marketplace_company_listings.py` with these two tests, adapted to that idiom:

```python
"""format="company" listings: searchable, not deployable (until PR2)."""
from unittest.mock import AsyncMock

import pytest


@pytest.mark.anyio
async def test_search_accepts_company_format(client, monkeypatch):
    from core.services import marketplace_search

    fake_row = {
        "listing_id": "l1", "version": 1, "slug": "gstack", "name": "GStack",
        "format": "company", "status": "published", "price_cents": 0,
        "tags": [], "seller_id": "__isol8__",
        "manifest_json": {"company": {"agent_count": 5, "min_plan": "standard"}},
    }
    monkeypatch.setattr(
        marketplace_search, "search", AsyncMock(return_value=[fake_row])
    )
    resp = await client.get("/api/v1/marketplace/listings/search?format=company")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and items[0]["format"] == "company"


@pytest.mark.anyio
async def test_deploy_company_listing_returns_400(client, monkeypatch, auth_headers):
    # Patch whatever resolver deploy_listing uses to load the listing
    # (read deploy_listing first; patch at the router's import site) so it
    # returns a published company row, then:
    resp = await client.post(
        "/api/v1/marketplace/listings/gstack/deploy", headers=auth_headers
    )
    assert resp.status_code == 400
    assert "coming soon" in resp.json()["detail"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_marketplace_company_listings.py -v`
Expected: search test FAILS with 422 (pattern rejects `company`); deploy test FAILS (no 400 guard yet).

- [ ] **Step 3: Widen the search pattern and add the deploy guard**

In `routers/marketplace_listings.py`:

```python
# search_listings (~line 132) — widen the query validation:
format: str | None = Query(default=None, pattern="^(openclaw|skillmd|company)$"),
```

Leave `featured-listings` (~line 191) at `^(openclaw|skillmd)$` — companies are deliberately excluded from the featured rail. Leave the seller create/update payload validation untouched.

In `deploy_listing` (~line 322), immediately after the listing row is resolved and before any entitlement work:

```python
    if listing.get("format") == "company":
        raise HTTPException(
            status_code=400,
            detail="Company deploys are coming soon.",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_marketplace_company_listings.py tests/unit/routers/ -q -k marketplace`
Expected: new tests PASS; existing marketplace router tests still PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/marketplace_listings.py apps/backend/tests/unit/routers/test_marketplace_company_listings.py
git commit -m "feat(marketplace): accept format=company in search; block company deploys pending deploy engine"
```

---

### Task 3: Registry ingestion script

**Files:**
- Create: `apps/backend/scripts/ingest_companies_registry.py`
- Test: `apps/backend/tests/unit/scripts/test_ingest_companies_registry.py`

**Interfaces:**
- Consumes: `parse_company_meta` / `CompanyMeta` (Task 1); `marketplace_service._upload_artifact_to_s3(listing_id, version, artifact_bytes, manifest, openclaw_slice)`; `marketplace_repo.put_draft_v1(...)`, `marketplace_repo.mark_review(...)`, `marketplace_repo.mark_published(...)`, `marketplace_repo.scan_all_listings()`; `marketplace_safety.scan_tarball`.
- Produces: published `format="company"` listing rows + S3 artifacts. One-shot: after the prod run it moves to `apps/backend/scripts/archive/` (tests deleted), per repo policy.

- [ ] **Step 1: Write the failing tests**

Mirror the structure of `tests/unit/scripts/test_backfill_paperclip_agent_run_timeout.py` (module-level functions, mocked I/O). `test_ingest_companies_registry.py`:

```python
"""Ingestion: discovery, deterministic tarball, row pipeline, idempotency."""
import hashlib
import io
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.ingest_companies_registry import (
    ISOL8_SYSTEM_SELLER_ID,
    build_package_tarball,
    discover_packages,
    ingest_one,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "companies"


def test_discover_packages_requires_company_md(tmp_path):
    (tmp_path / "gstack").mkdir()
    (tmp_path / "gstack" / "COMPANY.md").write_text("---\nname: G\nslug: g\ndescription: d\n---\n")
    (tmp_path / "skills").mkdir()          # registry noise: no COMPANY.md
    (tmp_path / ".agents").mkdir()
    found = discover_packages(tmp_path)
    assert [p.name for p in found] == ["gstack"]


def test_tarball_is_deterministic_and_keeps_paperclip_yaml():
    b1 = build_package_tarball(FIXTURES / "gstack")
    b2 = build_package_tarball(FIXTURES / "gstack")
    assert hashlib.sha256(b1).hexdigest() == hashlib.sha256(b2).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(b1)) as tf:
        names = tf.getnames()
    assert "gstack/.paperclip.yaml" in names          # dotfile that MUST ship
    assert "gstack/COMPANY.md" in names
    assert not any(".git/" in n or ".DS_Store" in n for n in names)


@pytest.mark.anyio
async def test_ingest_one_pipeline(monkeypatch):
    from scripts import ingest_companies_registry as mod

    monkeypatch.setattr(mod, "scan_tarball", lambda b: [])
    upload = AsyncMock(return_value=("listings/x/v1/", "sha"))
    put_draft = AsyncMock()
    mark_review = AsyncMock()
    mark_published = AsyncMock()
    monkeypatch.setattr(mod, "_upload_artifact_to_s3", upload)
    monkeypatch.setattr(mod.marketplace_repo, "put_draft_v1", put_draft)
    monkeypatch.setattr(mod.marketplace_repo, "mark_review", mark_review)
    monkeypatch.setattr(mod.marketplace_repo, "mark_published", mark_published)

    summary = await ingest_one(
        FIXTURES / "gstack", source_commit="abc123", existing_slugs=set(), dry_run=False
    )

    assert summary["slug"] == "gstack" and summary["status"] == "published"
    manifest = upload.call_args.kwargs["manifest"]
    assert manifest["company"]["agent_count"] == 5
    assert manifest["company"]["min_plan"] == "standard"
    assert manifest["company"]["source"] == {
        "repo": "paperclipai/companies", "commit": "abc123", "path": "gstack",
    }
    assert put_draft.call_args.kwargs["seller_id"] == ISOL8_SYSTEM_SELLER_ID
    assert put_draft.call_args.kwargs["format"] == "company"
    assert put_draft.call_args.kwargs["price_cents"] == 0
    assert mark_published.call_args.kwargs["approved_by"] == ISOL8_SYSTEM_SELLER_ID


@pytest.mark.anyio
async def test_ingest_one_skips_existing_slug_and_flags_findings(monkeypatch):
    from scripts import ingest_companies_registry as mod

    skipped = await ingest_one(
        FIXTURES / "gstack", source_commit="abc", existing_slugs={"gstack"}, dry_run=False
    )
    assert skipped["status"] == "skipped_existing"

    monkeypatch.setattr(mod, "scan_tarball", lambda b: ["credential-pattern:foo"])
    flagged = await ingest_one(
        FIXTURES / "gstack", source_commit="abc", existing_slugs=set(), dry_run=False
    )
    assert flagged["status"] == "blocked_safety"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_ingest_companies_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: scripts.ingest_companies_registry`

- [ ] **Step 3: Implement the script**

`apps/backend/scripts/ingest_companies_registry.py`:

```python
"""One-shot: mirror companies.sh packages into marketplace listings.

Usage (operator clones + pins the registry first — the script does no
network I/O of its own beyond AWS):

    git clone https://github.com/paperclipai/companies.git /tmp/pc-companies
    git -C /tmp/pc-companies rev-parse HEAD    # pass as --commit
    cd apps/backend
    uv run python -m scripts.ingest_companies_registry \
        --repo-dir /tmp/pc-companies --commit <sha> [--dry-run]

Writes published format="company" rows via the marketplace repo with the
system seller id (the seller service surface is seller-centric draft→review
state machine; curated ingestion drives the repo transitions directly).
Idempotent: packages whose slug already has a listing are skipped.
After the prod run: move to scripts/archive/ and delete the tests.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import subprocess
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.repositories import marketplace_repo
from core.services.company_package import parse_company_meta
from core.services.marketplace_safety import scan_tarball
from core.services.marketplace_service import _upload_artifact_to_s3

ISOL8_SYSTEM_SELLER_ID = "__isol8__"
SOURCE_REPO = "paperclipai/companies"
_EXCLUDED = {".git", ".DS_Store", "__pycache__", "node_modules"}


def discover_packages(repo_dir: Path) -> list[Path]:
    return sorted(
        p.parent for p in repo_dir.glob("*/COMPANY.md") if p.parent.name not in _EXCLUDED
    )


def build_package_tarball(pkg_dir: Path) -> bytes:
    """Deterministic tar.gz: sorted members, zeroed mtimes/owners.

    Keeps dotFILES (``.paperclip.yaml`` is load-bearing for the deploy
    engine) while excluding VCS/OS noise directories.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tf:
        for path in sorted(pkg_dir.rglob("*")):
            if any(part in _EXCLUDED for part in path.parts):
                continue
            arcname = f"{pkg_dir.name}/{path.relative_to(pkg_dir)}"
            info = tf.gettarinfo(str(path), arcname=arcname)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if path.is_file():
                with path.open("rb") as fh:
                    tf.addfile(info, fh)
            else:
                tf.addfile(info)
    return buf.getvalue()


async def ingest_one(
    pkg_dir: Path, *, source_commit: str, existing_slugs: set[str], dry_run: bool
) -> dict:
    meta = parse_company_meta(pkg_dir)
    if meta.slug in existing_slugs:
        return {"slug": meta.slug, "status": "skipped_existing"}

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
        listing_id=listing_id, version=1, artifact_bytes=tar_bytes,
        manifest=manifest, openclaw_slice={},
    )
    await marketplace_repo.put_draft_v1(
        listing_id=listing_id, slug=meta.slug, name=meta.name,
        description_md=meta.description, format="company", price_cents=0,
        tags=["company"], category="company", seller_id=ISOL8_SYSTEM_SELLER_ID,
        s3_prefix=s3_prefix, manifest_sha256=sha,
        manifest_json_str=json.dumps(manifest), now_iso=now_iso,
    )
    await marketplace_repo.mark_review(
        listing_id=listing_id, version=1,
        seller_id=ISOL8_SYSTEM_SELLER_ID, now_iso=now_iso,
    )
    await marketplace_repo.mark_published(
        listing_id=listing_id, version=1,
        approved_by=ISOL8_SYSTEM_SELLER_ID, now_iso=now_iso,
    )
    return {"slug": meta.slug, "status": "published", "listing_id": listing_id}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-dir", type=Path, required=True)
    ap.add_argument("--commit", required=True, help="pinned registry SHA (provenance)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    head = subprocess.run(
        ["git", "-C", str(args.repo_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if head != args.commit:
        raise SystemExit(f"repo HEAD {head} != --commit {args.commit}; re-pin and retry")

    existing = {
        row["slug"] for row in await marketplace_repo.scan_all_listings()
    }
    for pkg_dir in discover_packages(args.repo_dir):
        summary = await ingest_one(
            pkg_dir, source_commit=args.commit,
            existing_slugs=existing, dry_run=args.dry_run,
        )
        print(json.dumps(summary))


if __name__ == "__main__":
    asyncio.run(main())
```

Before running the tests, check `scan_tarball`'s actual return contract (`rg -n "def scan_tarball" -A 8 core/services/marketplace_safety.py`) — if it returns a findings object rather than a plain list, adapt the truthiness check and the test's fake return to match. Same for `mark_review`'s conditional: it requires the draft row to satisfy its condition expression — if it also conditions on `artifact_uploaded=True`, insert a `marketplace_repo.update_artifact_fields(listing_id=..., version=1, seller_id=ISOL8_SYSTEM_SELLER_ID, sha=sha, manifest=manifest, s3_prefix=s3_prefix, now_iso=now_iso)` call between `put_draft_v1` and `mark_review` (and assert it in the pipeline test).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_ingest_companies_registry.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/scripts/ingest_companies_registry.py apps/backend/tests/unit/scripts/test_ingest_companies_registry.py
git commit -m "feat(marketplace): one-shot companies.sh registry ingestion script"
```

---

### Task 4: Frontend — Companies tab, browse page, card, detail deploy states

**Files:**
- Modify: `apps/frontend/src/app/marketplace/layout.tsx` (nav)
- Create: `apps/frontend/src/app/marketplace/companies/page.tsx`
- Create: `apps/frontend/src/app/marketplace/companies/CompaniesBrowse.tsx`
- Create: `apps/frontend/src/components/marketplace/storefront/Listing/CompanyCard.tsx`
- Modify: `apps/frontend/src/lib/marketplace/types.ts` (company manifest typing)
- Modify: `apps/frontend/src/components/marketplace/ListingDetailView.tsx` (deploy-button branch)
- Test: `apps/frontend/src/components/marketplace/__tests__/CompanyCard.test.tsx`

**Interfaces:**
- Consumes: `browseListings({ format, limit })` from `@/lib/marketplace/api` (already forwards `format` to `/listings/search`); `Listing` type with `manifest_json?: Record<string, unknown>`.
- Produces: `CompanyManifest` type; `<CompanyCard listing={...} />`; `companyMeta(listing): CompanyManifest | null` helper exported from `CompanyCard.tsx` (reused by `ListingDetailView`).

- [ ] **Step 1: Add the company manifest type**

In `src/lib/marketplace/types.ts`, alongside the `Listing` interface:

```ts
export interface CompanyManifest {
  slug: string;
  name: string;
  description: string;
  license: string | null;
  authors: string[];
  agent_count: number;
  team_count: number;
  skill_count: number;
  agent_slugs: string[];
  env_requirements: { name: string; kind: string; requirement: string; agent: string }[];
  min_plan: "standard" | "ultra_pro";
  source: { repo: string; commit: string; path: string };
}
```

- [ ] **Step 2: Write the failing CompanyCard test**

`src/components/marketplace/__tests__/CompanyCard.test.tsx` (mirror the render idiom of an existing test in this `__tests__` dir):

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CompanyCard } from "../storefront/Listing/CompanyCard";
import type { Listing } from "@/lib/marketplace/types";

function companyListing(agentCount: number, minPlan: "standard" | "ultra_pro"): Listing {
  return {
    listing_id: "l1", version: 1, slug: "gstack", name: "GStack",
    description_md: "Engineering company", format: "company", price_cents: 0,
    tags: ["company"], seller_id: "__isol8__", status: "published",
    manifest_json: {
      company: {
        slug: "gstack", name: "GStack", description: "Engineering company",
        license: "MIT", authors: ["Dotta", "Garry Tan"], agent_count: agentCount,
        team_count: 1, skill_count: 27, agent_slugs: [], env_requirements: [],
        min_plan: minPlan,
        source: { repo: "paperclipai/companies", commit: "abc", path: "gstack" },
      },
    },
  } as unknown as Listing;
}

describe("CompanyCard", () => {
  it("shows counts and coming-soon state for standard companies", () => {
    render(<CompanyCard listing={companyListing(5, "standard")} />);
    expect(screen.getByText(/5 agents/i)).toBeInTheDocument();
    expect(screen.getByText(/27 skills/i)).toBeInTheDocument();
    expect(screen.getByText(/coming soon/i)).toBeInTheDocument();
  });

  it("shows the Ultra Pro badge for large companies", () => {
    render(<CompanyCard listing={companyListing(49, "ultra_pro")} />);
    expect(screen.getByText(/requires ultra pro/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/marketplace/__tests__/CompanyCard.test.tsx`
Expected: FAIL — cannot resolve `../storefront/Listing/CompanyCard`.

- [ ] **Step 4: Implement CompanyCard**

`src/components/marketplace/storefront/Listing/CompanyCard.tsx` — match `ListingCard.tsx`'s visual idiom (same card container classes and link target; open `ListingCard.tsx` and copy its wrapper/link classes verbatim):

```tsx
import Link from "next/link";

import type { CompanyManifest, Listing } from "@/lib/marketplace/types";

export function companyMeta(listing: Listing): CompanyManifest | null {
  const company = (listing.manifest_json as { company?: CompanyManifest } | undefined)?.company;
  return company ?? null;
}

export function CompanyCard({ listing }: { listing: Listing }) {
  const company = companyMeta(listing);
  if (!company) return null;
  const locked = company.min_plan === "ultra_pro";
  return (
    <Link
      href={`/marketplace/listing/${listing.slug}`}
      className="group flex flex-col gap-3 rounded-2xl border border-[var(--edge)] bg-white/60 p-5 no-underline transition hover:border-[var(--forest)]"
    >
      <div className="flex items-start justify-between gap-2">
        <h3 className="font-editorial-serif text-xl text-[var(--ink)]">{company.name}</h3>
        {locked ? (
          <span className="rounded-full bg-[var(--forest-tint)] px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--forest)]">
            Requires Ultra Pro
          </span>
        ) : (
          <span className="rounded-full border border-[var(--edge)] px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--mute)]">
            Deploy coming soon
          </span>
        )}
      </div>
      <p className="line-clamp-2 text-sm leading-6 text-[var(--ink-soft)]">{company.description}</p>
      <div className="mt-auto flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] uppercase tracking-[0.12em] text-[var(--mute)]">
        <span>{company.agent_count} agents</span>
        <span>{company.team_count} {company.team_count === 1 ? "team" : "teams"}</span>
        <span>{company.skill_count} skills</span>
        {company.license ? <span>{company.license}</span> : null}
      </div>
    </Link>
  );
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/components/marketplace/__tests__/CompanyCard.test.tsx`
Expected: 2 PASS.

- [ ] **Step 6: Add the browse page + nav tab**

`src/app/marketplace/companies/page.tsx` (mirrors `agents/page.tsx`):

```tsx
import { browseListings } from "@/lib/marketplace/api";
import { CompaniesBrowse } from "./CompaniesBrowse";

export default async function Companies() {
  let items: Awaited<ReturnType<typeof browseListings>>["items"] = [];
  try {
    const result = await browseListings({ format: "company", limit: 50 });
    items = result.items;
  } catch {
    // Best-effort during local dev.
  }

  return (
    <main>
      <section className="border-b border-[var(--edge)] px-6 py-14">
        <div className="mx-auto max-w-6xl">
          <div className="mb-4 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--mute)]">
            The open company directory
          </div>
          <h1 className="font-editorial-serif text-5xl leading-[1.0] text-[var(--ink)] md:text-6xl">
            Deploy a whole <em className="text-[var(--forest)]">company.</em>
          </h1>
          <p className="mt-4 max-w-2xl text-base leading-7 text-[var(--ink-soft)]">
            {items.length} pre-built agent {items.length === 1 ? "company" : "companies"} — org
            charts, skills, and workflows included.
          </p>
        </div>
      </section>

      <CompaniesBrowse items={items} />
    </main>
  );
}
```

`src/app/marketplace/companies/CompaniesBrowse.tsx` (mirrors `AgentsBrowse.tsx`, minus tag pills — company tags are uniform):

```tsx
"use client";

import { CompanyCard } from "@/components/marketplace/storefront/Listing/CompanyCard";
import type { Listing } from "@/lib/marketplace/types";

export function CompaniesBrowse({ items }: { items: Listing[] }) {
  return (
    <section className="mx-auto max-w-6xl px-6 py-10">
      {items.length === 0 ? (
        <p className="py-20 text-center font-editorial-serif text-2xl italic text-[var(--mute)]">
          No companies listed yet.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-3">
          {items.map((listing) => (
            <CompanyCard key={listing.listing_id} listing={listing} />
          ))}
        </div>
      )}
    </section>
  );
}
```

In `layout.tsx`, insert between the Browse and `<SignedIn>` entries (identical classes to the Browse link):

```tsx
<Link
  href="/marketplace/companies"
  className="rounded-full px-3 py-1.5 text-[var(--ink-soft)] no-underline hover:bg-[var(--forest-tint)] hover:text-[var(--forest)]"
>
  Companies
</Link>
```

- [ ] **Step 7: Detail-page deploy states**

In `ListingDetailView.tsx`: import `companyMeta` from `./storefront/Listing/CompanyCard`; locate the single site where the buy/deploy button renders and branch:

```tsx
const company = companyMeta(listing);
// where the buy/deploy button renders:
{company ? (
  company.min_plan === "ultra_pro" ? (
    <button disabled className="editorial-btn editorial-btn--primary opacity-60" title="Ultra Pro tier coming soon">
      Requires Ultra Pro
    </button>
  ) : (
    <button disabled className="editorial-btn editorial-btn--primary opacity-60">
      Deploy — coming soon
    </button>
  )
) : (
  /* existing BuyButton render, unchanged */
)}
```

Also render the company facts under the description when `company` is non-null (counts row + env requirements chips + `Mirrored from {company.source.repo} · {company.license}` attribution line), reusing the count-row markup from `CompanyCard`.

- [ ] **Step 8: Typecheck + lint**

Run: `cd apps/frontend && pnpm exec tsc --noEmit && pnpm run lint`
Expected: clean (tsc typechecks the test file too).

- [ ] **Step 9: Commit**

```bash
git add apps/frontend/src/app/marketplace/companies apps/frontend/src/components/marketplace/storefront/Listing/CompanyCard.tsx apps/frontend/src/components/marketplace/__tests__/CompanyCard.test.tsx apps/frontend/src/components/marketplace/ListingDetailView.tsx apps/frontend/src/lib/marketplace/types.ts apps/frontend/src/app/marketplace/layout.tsx
git commit -m "feat(marketplace): Companies tab, browse page, card, detail deploy states"
```

---

### Task 5: Full suite, dev ingestion run, visual verification

**Files:** none new (verification + runbook).

- [ ] **Step 1: Full test suites**

Run: `cd apps/backend && uv run pytest tests/ -q` then `cd apps/frontend && pnpm exec tsc --noEmit && pnpm run lint`
Expected: all green. (If anything fails, your change is the likely culprit even if the file looks unrelated — fix before proceeding.)

- [ ] **Step 2: Dev ingestion dry-run, then real run**

```bash
git clone https://github.com/paperclipai/companies.git /tmp/pc-companies
PIN=$(git -C /tmp/pc-companies rev-parse HEAD)
cd apps/backend
# dev AWS credentials/env (same env the backend uses for dev DDB/S3):
uv run python -m scripts.ingest_companies_registry --repo-dir /tmp/pc-companies --commit "$PIN" --dry-run
uv run python -m scripts.ingest_companies_registry --repo-dir /tmp/pc-companies --commit "$PIN"
```

Expected: one JSON line per package; ~16 `"published"`, noise dirs absent, no `"blocked_safety"` (investigate any before proceeding — do NOT bypass the scanner).

- [ ] **Step 3: Verify the dev API**

```bash
curl -s "https://api-dev.isol8.co/api/v1/marketplace/listings/search?format=company" | python3 -m json.tool | head -40
```

Expected: company rows with `manifest_json.company.agent_count` populated. Also confirm a company deploy 400s (authenticated POST to `/listings/gstack/deploy` → `"Company deploys are coming soon."`).

- [ ] **Step 4: Visual self-review (required before PR)**

Open `https://dev.isol8.co/marketplace/companies` in the browser (Playwright or Chrome tooling), screenshot the tab, a standard card, an ultra_pro-badged card, and the gstack detail page. Check: counts render, badges correct (`kdense-science-lab` etc. show "Requires Ultra Pro"; `gstack` shows "Deploy coming soon"), attribution line present, no layout breakage in light/dark.

- [ ] **Step 5: Commit any fixes, then stop for PR approval**

Present the branch + screenshots to the user. Do NOT push or open a PR without explicit approval.

---

## Self-Review Notes

- Spec coverage: ingestion (Task 3), listing kind + browse plumbing (Task 2), tab/cards/detail states (Task 4), scanner gate (Task 3 + Step 5.2 guard), provenance + license display (Tasks 3/4), deploy blocked until PR2 (Task 2). PR2 (deploy engine, deploy-job table, routines/heartbeat pause, resume/rollback, post-deploy screen) and PR3 (Stripe tier, resize, downgrade guard) are separate plans per the spec's PR split.
- The two "check the actual contract first" callouts (scan_tarball return shape, mark_review condition) are deliberate: those contracts were not read during planning; the plan states exactly what to do in either case.
- Archive step for the script happens after the PROD run (post-PR1-ship), not during this plan.
