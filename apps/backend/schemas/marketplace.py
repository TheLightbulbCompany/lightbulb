"""Pydantic schemas for marketplace endpoints."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# Create-side formats: what a seller may publish. "company" rows are
# registry-ingested by the curated pipeline only, so it is deliberately
# absent here — a seller POSTing format="company" must 422.
FormatStr = Literal["openclaw", "skillmd"]
# Response-side formats: everything a stored listing row can carry,
# including registry-ingested "company" rows. Response schemas must use
# this wider literal or company rows fail response validation and 500.
ListingFormatStr = Literal["openclaw", "skillmd", "company"]
ListingStatusStr = Literal["draft", "review", "published", "retired", "taken_down"]


class ListingCreate(BaseModel):
    slug: str = Field(..., min_length=2, max_length=64)
    name: str = Field(..., min_length=2, max_length=80)
    description_md: str = Field(..., min_length=1, max_length=4096)
    format: FormatStr
    price_cents: int = Field(..., ge=0, le=2000)
    tags: list[str] = Field(..., max_length=5)
    category: str | None = None
    suggested_clients: list[str] = Field(default_factory=list)

    @field_validator("slug")
    @classmethod
    def _slug_lowercase_kebab(cls, v: str) -> str:
        # Slug must be lowercase alphanumeric + hyphens.
        if not all(c.islower() or c.isdigit() or c == "-" for c in v):
            raise ValueError("slug must be lowercase letters, digits, and hyphens only")
        if v.startswith("-") or v.endswith("-"):
            raise ValueError("slug must not start or end with a hyphen")
        return v

    @field_validator("tags")
    @classmethod
    def _tags_lowercase(cls, v: list[str]) -> list[str]:
        return [t.lower().strip() for t in v if t.strip()]


class Listing(BaseModel):
    listing_id: str
    slug: str
    name: str
    description_md: str
    format: ListingFormatStr
    price_cents: int
    tags: list[str]
    seller_id: str
    # Resolved server-side by seller_display.attach_seller_name(s) before
    # the response goes out. Reads "@{handle}" when the seller has set a
    # marketplace handle, falls back to the Clerk-derived name, then to
    # "Community member". Optional so create flows that emit a fresh dict
    # don't fail validation before the resolver has run.
    seller_display_name: str | None = None
    status: ListingStatusStr
    version: int
    created_at: datetime
    published_at: datetime | None
    artifact_format_version: str = "v1"
    # Edit-context fields surfaced for the wizard. PublishWizard /
    # MarketplaceDrawerProvider read these on republish (startEditListing →
    # publishAgentId, BundleStep prefill, formFromListing category default).
    # Optional so create flows that haven't uploaded an artifact yet
    # validate cleanly. category MUST be here — without it the wizard
    # silently flips a non-Operations listing to "Operations" on
    # formFromListing's missing-key fallback.
    agent_id: str | None = None
    bundle_mcps: list[str] | None = None
    bundle_skills: list[str] | None = None
    category: str | None = None


class ListingUpdate(BaseModel):
    """Seller-facing patch for an existing draft listing.

    All fields optional — only provided keys are written. Slug and format
    are intentionally absent: slug is set at create time and is immutable
    thereafter, and format is determined by the artifact, not by metadata.
    """

    name: str | None = Field(default=None, min_length=2, max_length=80)
    description_md: str | None = Field(default=None, min_length=1, max_length=4096)
    price_cents: int | None = Field(default=None, ge=0, le=2000)
    tags: list[str] | None = Field(default=None, max_length=5)
    category: str | None = None

    @field_validator("tags")
    @classmethod
    def _tags_lowercase(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return [t.lower().strip() for t in v if t.strip()]


class CredentialRequirement(BaseModel):
    """A single third-party provider a listing's agent needs connected.

    ``provider`` is a credential-registry key (linear / stripe / google / …).
    ``optional`` marks integrations the agent can run without — those never
    block a deploy, they're surfaced as "nice to connect" hints only.
    """

    provider: str
    optional: bool = False


class Requires(BaseModel):
    """The ``requires`` block a listing manifest may carry.

    Declares which providers the buyer must connect before the agent will
    function. Drives the pre-deploy credential gate (a deploy is blocked
    with 409 when a non-optional provider here is unconnected).
    """

    credentials: list[CredentialRequirement] = Field(default_factory=list)


class CheckoutRequest(BaseModel):
    listing_id: str
    success_url: str
    cancel_url: str
    email: str | None = None  # required for anonymous (guest) checkout


class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str


class InstallValidateResponse(BaseModel):
    """Returned by GET /install/validate."""

    listing_id: str
    listing_slug: str
    version: int
    download_url: str  # 5-min pre-signed S3 URL
    manifest_sha256: str
    expires_at: datetime


class TakedownRequest(BaseModel):
    reason: Literal["dmca", "policy", "fraud", "seller-request"]
    claimant_name: str
    claimant_email: str
    basis_md: str = Field(..., min_length=10, max_length=4096)


# -- Artifact upload + seller / buyer surfacing --


class ArtifactUploadResponse(BaseModel):
    """Returned by POST /listings/{id}/artifact-from-agent."""

    listing_id: str
    version: int
    manifest_sha256: str
    file_count: int
    bytes: int
    # Workspace-relative dirs the export dropped as nested repo checkouts.
    pruned_paths: list[str] = Field(default_factory=list)


class BundleSelection(BaseModel):
    """Seller's explicit choice of MCPs and skills to include in a listing.

    Sent by the publish wizard's Bundle step. Empty lists (or None at the
    parent ``ArtifactFromAgentRequest`` level) mean "don't bundle anything"
    — the listing ships the agent's workspace files only and the buyer's
    openclaw.json isn't touched for MCPs/skills.
    """

    mcps: list[str] = Field(default_factory=list, max_length=64)
    skills: list[str] = Field(default_factory=list, max_length=128)


class ArtifactFromAgentRequest(BaseModel):
    agent_id: str = Field(..., min_length=4, max_length=64)
    bundle: BundleSelection | None = None
    # Seller declares the agent delegates heavy work to ACP coding harnesses
    # (Claude Code / Codex) via ``sessions_spawn({runtime:"acp"})``. Recorded
    # as ``manifest.requires_acp`` so deploy can turn on the buyer container's
    # ACP runtime. The capability is container-wide (OpenClaw's root
    # ``acp.enabled`` gate), not a per-agent config key — the agent entry's
    # schema is ``.strict()`` — so the per-agent intent lives here + in the
    # agent's own prompt, never on the openclaw.json agent entry.
    enable_acp: bool = False
    # Seller declares the agent needs the Isol8 desktop app (its local
    # node-host — browser automation, local exec via the operator relay). We
    # can't scan for this — it's a runtime capability, not a tool name — so the
    # publisher sets it. Recorded as ``manifest.requires_desktop`` for
    # buyer-facing disclosure only (nothing on deploy can provision it).
    requires_desktop: bool = False
    # Explicit include list: workspace-relative files/dirs to ship instead of
    # the whole workspace. Omitted → everything (minus nested repo checkouts).
    paths: list[str] | None = Field(default=None, min_length=1, max_length=256)


class DeployListingRequest(BaseModel):
    """Optional body for ``POST /marketplace/listings/{id}/deploy``.

    ``reports_to`` is the Paperclip agent id the buyer wants the new hire to
    report to (its manager in the org chart). Omitted / null leaves the agent
    unparented (Paperclip's default), so existing one-click deploys that send
    no body keep working unchanged.
    """

    reports_to: str | None = None

    @field_validator("reports_to")
    @classmethod
    def _blank_reports_to_is_none(cls, v: str | None) -> str | None:
        # Paperclip's reportsTo accepts null but rejects "" — coerce a blank /
        # whitespace string to None so a top-level hire never trips validation.
        return (v or "").strip() or None


class AvailableSkill(BaseModel):
    """A skill referenced by the seller's agent + where it lives."""

    name: str
    # Mirrors core.services.catalog_slice.SkillBucket — keep in sync.
    bucket: Literal["bundled", "entries", "workspace", "extra_dirs", "unknown"]


# ArtifactPreviewResponse is defined below SafetyFlag — it references that type.


class PurchaseSummary(BaseModel):
    purchase_id: str
    listing_id: str
    listing_slug: str | None = None
    license_key: str
    price_paid_cents: int
    status: Literal["paid", "refunded", "revoked"]
    created_at: str  # ISO8601 string (matches DDB raw value)


class MyPurchasesResponse(BaseModel):
    items: list[PurchaseSummary]


# -- Admin moderation preview --


class FileTreeEntry(BaseModel):
    path: str
    size_bytes: int


class SafetyFlag(BaseModel):
    pattern: str  # short identifier: "curl-bash", "eval", "secret", etc.
    severity: Literal["high", "medium", "low"]
    file: str
    line: int | None = None
    # ~80 char excerpt for the admin UI. Optional: manifests served by
    # unauthenticated routes (listing-detail, search, and this import
    # preview) carry snippet-stripped flag dicts (see
    # marketplace_safety.flags_without_snippets) — only the authenticated
    # seller-preview path opts back in with the real excerpt.
    snippet: str = ""


class OpenclawSummary(BaseModel):
    tools_count: int = 0
    providers: list[str] = Field(default_factory=list)
    cron_count: int = 0
    channels_count: int = 0
    sub_agent_count: int = 0
    raw_config_size_bytes: int = 0


class ListingPreviewResponse(BaseModel):
    """Returned by GET /admin/marketplace/listings/{id}/preview.

    Backend reads the listing's S3 tarball, extracts in-memory, runs the
    safety scan, and surfaces enough context for an admin to make an
    informed approve/reject call.

    When the unpacked size exceeds the scan cap, the response is partial:
    ``file_tree`` is still populated from tar headers (cheap, header-only
    walk), but ``safety_flags`` is empty and ``scan_incomplete=True``.
    The admin UI MUST disable Approve in that case — an adversary could
    hide bad content behind padding, so approving on a partial scan is
    unsafe. Reject + Takedown remain valid actions.
    """

    listing_id: str
    version: int
    slug: str
    name: str
    seller_id: str
    format: ListingFormatStr
    status: ListingStatusStr
    price_cents: int
    tags: list[str]
    manifest: dict
    file_tree: list[FileTreeEntry]
    skill_md_text: str | None = None
    openclaw_summary: OpenclawSummary | None = None
    safety_flags: list[SafetyFlag]
    # Partial-preview signaling. Defaults keep existing test fixtures
    # working unmodified.
    scan_incomplete: bool = False
    scan_failure_reason: str | None = None
    tarball_bytes: int = 0
    unpacked_bytes: int = 0


# -- Seller profile (marketplace handle) --


class SellerProfileWrite(BaseModel):
    """Payload for PUT /seller/me.

    Handle is required; display_name and bio are optional. Per-field
    validation lives in core.services.seller_profile so the same rules
    apply when called directly from tests or the publish flow.
    """

    handle: str = Field(..., min_length=3, max_length=32)
    display_name: str | None = Field(default=None, max_length=64)
    bio: str | None = Field(default=None, max_length=280)


class SellerProfile(BaseModel):
    """Public-facing seller profile row.

    ``seller_id`` is the Clerk user_id and is only exposed on the
    authenticated GET /seller/me. The public GET /seller/handle/{handle}
    endpoint shapes its response to omit it.
    """

    seller_id: str
    handle: str
    display_name: str | None = None
    bio: str | None = None
    created_at: str
    updated_at: str


class SellerProfilePublic(BaseModel):
    handle: str
    display_name: str | None = None
    bio: str | None = None
    created_at: str


class HandleAvailability(BaseModel):
    available: bool
    reason: Literal["format", "reserved", "taken", None] = None


class RequirementRow(BaseModel):
    """One classified dependency in the requirements ledger (mirrors
    ``core.services.marketplace.requirements.Requirement.to_manifest``)."""

    key: str
    display_name: str
    kind: str
    disposition: Literal["included", "connect", "byo", "unavailable"]
    env: list[str] = Field(default_factory=list)
    detected_via: list[str] = Field(default_factory=list)
    bundled: bool = False
    required: bool = True
    note: str | None = None
    reason: str | None = None


class ArtifactPreviewResponse(BaseModel):
    """Dry-run preview of the artifact the seller would publish.

    Drives the publish wizard's Bundle step (MCP / skill checkboxes with
    bucket tags), Review step (manifest breakdown + safety_flags), and the
    Requirements ledger (what the agent needs, classified).
    Computed by snapshotting the agent's EFS dir (no S3 upload, no DDB
    write) so the seller sees exactly what will ship before they submit.
    """

    agent_id: str
    available_mcps: list[str] = Field(default_factory=list)
    available_skills: list[AvailableSkill] = Field(default_factory=list)
    auto_ship_skills: list[str] = Field(default_factory=list)
    persona_files: list[str] = Field(default_factory=list)
    safety_flags: list[SafetyFlag] = Field(default_factory=list)
    requirements: list[RequirementRow] = Field(default_factory=list)
    scan_incomplete: bool = False
    needs_config_patch: bool = False


# ---------------------------------------------------------------------------
# OpenClaw backup import (routers/marketplace_import.py)
# ---------------------------------------------------------------------------


class ImportPresignRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    size_bytes: int = Field(..., ge=1)


class ImportPresignResponse(BaseModel):
    upload_url: str
    key: str
    max_bytes: int
    expires_in: int


class ImportStartRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=512)


class ClaudeImportCollection(BaseModel):
    id: str
    label: str
    file_count: int


class ClaudeImportPreview(BaseModel):
    collections: list[ClaudeImportCollection]
    has_user_memory: bool
    skills: list[str]
    skipped: dict[str, int]
    # SafetyFlag.snippet stays at its "" default here — the claude preview
    # never echoes a flagged source line back to the client.
    safety_flags: list[SafetyFlag] = Field(default_factory=list)


class ImportPreviewResponse(BaseModel):
    """What a staged Claude Code import contains.

    The OpenClaw-backup half this used to also describe (``kind``, the agent/plugin/MCP/skill
    candidate lists, the file and cron counts) went with that rail. Every one of those fields
    defaulted to empty on a Claude import, so the client branched on ``kind`` to know which
    half was real; there is one shape now.
    """

    import_id: str
    name: str
    claude: ClaudeImportPreview


class ImportConfirmRequest(BaseModel):
    """Which roster agent receives the staged content, and which staged pieces to apply."""

    target_agent_id: str | None = None
    claude_collections: list[str] | None = None  # collection ids; None = all
    claude_skills: list[str] | None = None  # None = all


class ClaudeImportItemResult(BaseModel):
    item: str  # "memory:<cid>", "user-memory", "skill:<name>"
    status: Literal["applied", "skipped_exists"]


class ImportConfirmResponse(BaseModel):
    """Per-item results of an applied Claude import.

    Nothing is minted, so there is no per-agent result list: the content lands on an EXISTING
    roster agent's profile, and the ``.revision`` bump IS the delivery (re-pulled on that
    agent's next turn), so there is no restart to report either.
    """

    claude_items: list[ClaudeImportItemResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
