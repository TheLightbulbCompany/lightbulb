// apps/frontend/src/components/teams/issues/lib/issue-output.ts
//
// Ported from Paperclip ui/src/lib/issue-output.ts @ v2026.916.1-isol8.2
// (MIT, (c) 2025 Paperclip AI), verbatim below the shared-helper block. The
// block replaces the `@paperclipai/shared` imports: the attachment-artifact
// metadata schema (packages/shared/src/validators/work-product.ts, zod there;
// a hand check of the same rules here), the Markdown review-document helpers
// (packages/shared/src/markdown-work-products.ts) and `workProductHref`
// (ui/src/lib/issue-artifacts.ts). The `*Path` fields stay upstream-relative
// (`/api/attachments/{id}/content`); the components fetch them through the
// Teams BFF with `attachmentPathFromHref`, never as bare URLs.

import type { IssueWorkProduct } from "@/components/teams/shared/types";

export interface AttachmentArtifactWorkProductMetadata {
  attachmentId: string;
  contentType: string;
  byteSize: number;
  contentPath: string;
  openPath: string;
  downloadPath: string;
  originalFilename?: string | null;
}

const GUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

/** Same rules as stock `attachmentArtifactWorkProductMetadataSchema`: null on any failure. */
export function parseAttachmentArtifactMetadata(value: unknown): AttachmentArtifactWorkProductMetadata | null {
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  if (typeof v.attachmentId !== "string" || !GUID_RE.test(v.attachmentId)) return null;
  if (typeof v.contentType !== "string" || !v.contentType) return null;
  if (typeof v.byteSize !== "number" || !Number.isInteger(v.byteSize) || v.byteSize < 0) return null;
  const contentPath = `/api/attachments/${v.attachmentId}/content`;
  if (v.contentPath !== contentPath || v.openPath !== contentPath) return null;
  if (v.downloadPath !== `${contentPath}?download=1`) return null;
  if (v.originalFilename != null && typeof v.originalFilename !== "string") return null;
  return {
    attachmentId: v.attachmentId,
    contentType: v.contentType,
    byteSize: v.byteSize,
    contentPath,
    openPath: contentPath,
    downloadPath: v.downloadPath,
    originalFilename: (v.originalFilename as string | null | undefined) ?? null,
  };
}

// --- packages/shared/src/markdown-work-products.ts ---

export const MARKDOWN_REVIEW_DOCUMENT_MAX_BYTES = 512 * 1024;
export const ARTIFACT_REVIEW_DOCUMENT_KEY_PREFIX = "artifact-review-";
const MARKDOWN_FALLBACK_CONTENT_TYPES = new Set([
  "text/plain",
  "application/octet-stream",
  "binary/octet-stream",
  "application/x-binary",
]);

function isMarkdownAttachmentContent(input: {
  contentType: string | null | undefined;
  originalFilename?: string | null;
}): boolean {
  const contentType = normalizeOutputContentType(input.contentType);
  if (MARKDOWN_CONTENT_TYPES.has(contentType)) return true;
  const filename = (input.originalFilename ?? "").toLowerCase();
  if (![".md", ".markdown"].some((extension) => filename.endsWith(extension))) return false;
  return MARKDOWN_FALLBACK_CONTENT_TYPES.has(contentType);
}

export function getAttachmentArtifactWorkProductMetadata(
  workProduct: Pick<IssueWorkProduct, "type" | "provider" | "metadata">,
): AttachmentArtifactWorkProductMetadata | null {
  if (workProduct.type !== "artifact" || workProduct.provider !== "paperclip") return null;
  return parseAttachmentArtifactMetadata(workProduct.metadata);
}

export function getMarkdownWorkProductAttachmentMetadata(
  workProduct: Pick<IssueWorkProduct, "type" | "provider" | "metadata">,
): AttachmentArtifactWorkProductMetadata | null {
  const metadata = getAttachmentArtifactWorkProductMetadata(workProduct);
  return metadata && isMarkdownAttachmentContent(metadata) ? metadata : null;
}

export function artifactReviewDocumentKey(workProductId: string): string {
  return `${ARTIFACT_REVIEW_DOCUMENT_KEY_PREFIX}${workProductId}`;
}

export function isArtifactReviewDocumentKey(key: string): boolean {
  return key.startsWith(ARTIFACT_REVIEW_DOCUMENT_KEY_PREFIX) && key.length > ARTIFACT_REVIEW_DOCUMENT_KEY_PREFIX.length;
}

// --- ui/src/lib/issue-artifacts.ts ---

export function workProductHref(workProduct: Pick<IssueWorkProduct, "url" | "metadata">): string | null {
  if (workProduct.url) return workProduct.url;
  const metadata = workProduct.metadata;
  if (!metadata) return null;
  for (const key of ["openPath", "url"]) {
    const value = metadata[key];
    if (typeof value === "string" && value.trim().length > 0) return value;
  }
  return null;
}

// --- ui/src/lib/issue-output.ts (verbatim) ---

/**
 * Helpers + selectors for the issue Output surface (PAP-10162 Phase 3).
 *
 * The Output surface promotes attachment-backed artifact work products to a
 * first-class slot on the issue page so cloud users can watch / download files
 * an agent produced without digging through comments or the host filesystem.
 */

export type OutputFileTone = "video" | "pdf" | "zip" | "image" | "bin";

export interface OutputFileGlyph {
  /** Short (≤4 char) label for the file-type tile, e.g. "MP4". */
  label: string;
  tone: OutputFileTone;
}

/**
 * Format a byte count for display.
 *
 * Examples: `0 B`, `512 B`, `412 KB`, `18.4 MB`, `1.2 GB`. One decimal place is
 * used from KB upward, with a trailing `.0` trimmed so round values stay clean.
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "0 B";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const rounded = value.toFixed(1);
  const trimmed = rounded.endsWith(".0") ? rounded.slice(0, -2) : rounded;
  return `${trimmed} ${units[unitIndex]}`;
}

/**
 * Format a duration in seconds as `m:ss` (under an hour) or `h:mm:ss`.
 * Examples: `0:58`, `1:42:09`.
 */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const hrs = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (hrs > 0) {
    return `${hrs}:${pad(mins)}:${pad(secs)}`;
  }
  return `${mins}:${pad(secs)}`;
}

const GENERIC_BINARY_CONTENT_TYPES = new Set([
  "application/octet-stream",
  "binary/octet-stream",
  "application/x-binary",
]);

const VIDEO_FILENAME_EXTENSIONS = [
  ".mp4",
  ".m4v",
  ".webm",
  ".mov",
  ".qt",
  ".quicktime",
];

const BINARY_OUTPUT_APPLICATION_TYPES = new Set([
  "application/wasm",
]);

const ZIP_CONTENT_TYPES = new Set([
  "application/zip",
  "application/x-zip",
  "application/x-zip-compressed",
]);

const MARKDOWN_CONTENT_TYPES = new Set([
  "text/markdown",
  "text/x-markdown",
  "application/markdown",
  "application/x-markdown",
]);

const DOCUMENT_LIKE_APPLICATION_TYPES = new Set([
  "application/csv",
  "application/ecmascript",
  "application/graphql",
  "application/html",
  "application/javascript",
  "application/json",
  "application/ld+json",
  "application/sql",
  "application/toml",
  "application/x-httpd-php",
  "application/x-javascript",
  "application/x-python",
  "application/x-sh",
  "application/x-yaml",
  "application/xhtml+xml",
  "application/xml",
  "application/yaml",
]);

const DOCUMENT_LIKE_FILENAME_EXTENSIONS = [
  ".csv",
  ".css",
  ".graphql",
  ".htm",
  ".html",
  ".js",
  ".json",
  ".jsx",
  ".log",
  ".markdown",
  ".md",
  ".mdx",
  ".php",
  ".py",
  ".sh",
  ".sql",
  ".toml",
  ".ts",
  ".tsx",
  ".txt",
  ".xml",
  ".yaml",
  ".yml",
];

export function normalizeOutputContentType(contentType: string | null | undefined): string {
  return (contentType ?? "").toLowerCase().split(";")[0]?.trim() ?? "";
}

function hasDocumentLikeFilename(originalFilename: string | null | undefined): boolean {
  const filename = (originalFilename ?? "").trim().toLowerCase();
  if (!filename) return false;
  return DOCUMENT_LIKE_FILENAME_EXTENSIONS.some((extension) => filename.endsWith(extension));
}

function isZipContentType(contentType: string): boolean {
  return ZIP_CONTENT_TYPES.has(contentType) || contentType.endsWith("+zip");
}

function isDocumentLikeOutputContentType(contentType: string): boolean {
  if (!contentType) return false;
  if (contentType.startsWith("text/")) return true;
  if (MARKDOWN_CONTENT_TYPES.has(contentType)) return true;
  if (DOCUMENT_LIKE_APPLICATION_TYPES.has(contentType)) return true;
  if (contentType.startsWith("application/") && (contentType.endsWith("+json") || contentType.endsWith("+xml"))) return true;
  return false;
}

export function isOutputEligibleContentType(
  contentType: string | null | undefined,
  originalFilename?: string | null | undefined,
): boolean {
  const type = normalizeOutputContentType(contentType);
  if (!type) return false;
  if (isDocumentLikeOutputContentType(type)) return false;
  if (GENERIC_BINARY_CONTENT_TYPES.has(type) && hasDocumentLikeFilename(originalFilename)) return false;
  return (
    type.startsWith("video/") ||
    type.startsWith("image/") ||
    type === "application/pdf" ||
    BINARY_OUTPUT_APPLICATION_TYPES.has(type) ||
    isZipContentType(type) ||
    GENERIC_BINARY_CONTENT_TYPES.has(type)
  );
}

/**
 * Map a MIME type to a short label + tone for the 32×32 file-type tile.
 */
export function getOutputFileGlyph(contentType: string | null | undefined): OutputFileGlyph {
  const type = normalizeOutputContentType(contentType);
  if (type.startsWith("video/")) {
    const subtype = type.slice("video/".length);
    if (subtype === "quicktime") return { label: "MOV", tone: "video" };
    return { label: (subtype || "vid").toUpperCase().slice(0, 4), tone: "video" };
  }
  if (type === "application/pdf") return { label: "PDF", tone: "pdf" };
  if (isZipContentType(type)) {
    return { label: "ZIP", tone: "zip" };
  }
  if (type.startsWith("image/")) return { label: "IMG", tone: "image" };
  if (MARKDOWN_CONTENT_TYPES.has(type)) return { label: "MD", tone: "bin" };
  if (type === "text/plain") return { label: "TXT", tone: "bin" };
  if (type === "text/csv" || type === "application/csv") return { label: "CSV", tone: "bin" };
  if (type === "text/html" || type === "application/html" || type === "application/xhtml+xml") {
    return { label: "HTML", tone: "bin" };
  }
  if (type === "application/json" || type.endsWith("+json")) return { label: "JSON", tone: "bin" };
  if (type === "application/xml" || type === "text/xml" || type.endsWith("+xml")) {
    return { label: "XML", tone: "bin" };
  }
  if (type === "application/wasm") return { label: "WASM", tone: "bin" };
  return { label: "BIN", tone: "bin" };
}

export function isVideoContentType(contentType: string | null | undefined): boolean {
  return normalizeOutputContentType(contentType).startsWith("video/");
}

export function isVideoLikeOutput(
  contentType: string | null | undefined,
  originalFilename?: string | null | undefined,
): boolean {
  const type = normalizeOutputContentType(contentType);
  if (type.startsWith("video/")) return true;
  if (!GENERIC_BINARY_CONTENT_TYPES.has(type)) return false;

  const filename = (originalFilename ?? "").trim().toLowerCase();
  return VIDEO_FILENAME_EXTENSIONS.some((extension) => filename.endsWith(extension));
}

export function isImageContentType(contentType: string | null | undefined): boolean {
  return normalizeOutputContentType(contentType).startsWith("image/");
}

/**
 * A single rendered output. `metadata` is null when the work product's stored
 * metadata fails validation — the row is still surfaced (degraded) so we never
 * silently drop an artifact the agent reported producing.
 */
export interface IssueOutputItem {
  id: string;
  title: string;
  status: string;
  isPrimary: boolean;
  createdAt: string | Date | number;
  metadata: AttachmentArtifactWorkProductMetadata | null;
  /** True when stored metadata could not be parsed into a usable artifact. */
  degraded: boolean;
  workProduct: IssueWorkProduct;
}

export interface IssueOutputs {
  items: IssueOutputItem[];
  primary: IssueOutputItem | null;
  rest: IssueOutputItem[];
  count: number;
}

function toTime(value: string | Date | number): number {
  const t = new Date(value).getTime();
  return Number.isNaN(t) ? 0 : t;
}

/**
 * Parse attachment-backed artifact work products into renderable outputs.
 *
 * - Only `type: "artifact"` work products are considered outputs.
 * - Metadata is validated against the shared contract; invalid metadata yields
 *   a degraded item rather than an exception.
 * - Ordering: the explicit primary (or the most-recent artifact when none is
 *   marked primary) comes first, then remaining artifacts by most-recent.
 */
export function getIssueOutputs(workProducts: IssueWorkProduct[] | null | undefined): IssueOutputs {
  const artifacts = (workProducts ?? []).filter((wp) => wp.type === "artifact" && wp.provider === "paperclip");

  const items: IssueOutputItem[] = artifacts.flatMap((wp) => {
    const parsed = parseAttachmentArtifactMetadata(wp.metadata);
    if (parsed && !isOutputEligibleContentType(parsed.contentType, parsed.originalFilename)) {
      return [];
    }
    return [{
      id: wp.id,
      title: wp.title,
      status: typeof wp.status === "string" ? wp.status : "active",
      isPrimary: Boolean(wp.isPrimary),
      createdAt: wp.createdAt ?? wp.updatedAt ?? 0,
      metadata: parsed,
      degraded: !parsed,
      workProduct: wp,
    }];
  });

  items.sort((a, b) => {
    if (a.isPrimary !== b.isPrimary) return a.isPrimary ? -1 : 1;
    return toTime(b.createdAt) - toTime(a.createdAt);
  });

  return {
    items,
    primary: items[0] ?? null,
    rest: items.slice(1),
    count: items.length,
  };
}

/** Best display filename for an output, falling back to the work product title. */
export function outputFilename(item: IssueOutputItem): string {
  return item.metadata?.originalFilename || item.title || "output";
}

export function getPromotedOutputAttachmentIds(workProducts: IssueWorkProduct[] | null | undefined): Set<string> {
  return new Set(
    getIssueOutputs(workProducts).items.flatMap((item) => {
      const attachmentId = item.metadata?.attachmentId;
      return attachmentId ? [attachmentId] : [];
    }),
  );
}
