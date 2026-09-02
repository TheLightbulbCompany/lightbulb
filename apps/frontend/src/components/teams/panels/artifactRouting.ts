// apps/frontend/src/components/teams/panels/artifactRouting.ts
//
// Pure routing helpers for the company Artifacts gallery. A company artifact
// carries a TYPED id — `document:<documentId>`, `work_product:<workProductId>`,
// or `attachment:<attachmentId>` — which decides both how its content is
// fetched for the viewer and which BFF DELETE endpoint removes it. Kept as pure
// functions (no React) so the routing is directly unit-testable.

import type {
  CompanyArtifact,
  CompanyArtifactGroup,
} from "@/components/teams/shared/types";

export type ArtifactKind = "document" | "work_product" | "attachment";

/** The typed prefix of a company artifact id (`document:<id>` etc.), or null
 * for an unrecognized shape. */
export function artifactKind(id: string): ArtifactKind | null {
  const idx = id.indexOf(":");
  if (idx < 0) return null;
  const prefix = id.slice(0, idx);
  if (prefix === "document" || prefix === "work_product" || prefix === "attachment") {
    return prefix;
  }
  return null;
}

/** The raw upstream id after the typed prefix (the document / work-product /
 * attachment uuid). Upstream uuids never contain `:`, so a single split on the
 * first colon is exact. */
export function artifactRawId(id: string): string {
  const idx = id.indexOf(":");
  return idx < 0 ? id : id.slice(idx + 1);
}

/**
 * A document artifact's `key` lives ONLY inside the `href` fragment
 * (`.../issues/IDENT#document-<key>`) — it is never a top-level field on the
 * wire shape (see `paperclip/server/src/services/company-artifacts.ts`, which
 * pushes `id: "document:<docId>"` + `href` with the key in the anchor). Parse
 * it back out; returns null when absent. Keys are `[a-z0-9][a-z0-9_-]{0,63}`,
 * so a document key may itself contain dashes — strip only the leading marker.
 */
export function documentKeyFromArtifact(a: CompanyArtifact): string | null {
  const hash = a.href?.split("#")[1];
  if (!hash) return null;
  const marker = "document-";
  if (!hash.startsWith(marker)) return null;
  return hash.slice(marker.length) || null;
}

/**
 * The BFF DELETE path (relative to `/teams`) for an artifact, routed by its
 * typed prefix:
 *   - `document:`     → `/issues/{issueId}/documents/{key}`   (needs issue + key)
 *   - `work_product:` → `/work-products/{id}`                 (needs id)
 *   - `attachment:`   → `/issues/{issueId}/attachments/{id}`  (needs issue + id)
 * Returns null when the required parts can't be resolved (e.g. a document with
 * no href, or an unrecognized prefix) so the caller can disable delete rather
 * than call a malformed endpoint.
 */
export function deletePathForArtifact(a: CompanyArtifact): string | null {
  const kind = artifactKind(a.id);
  const issueId = a.issue?.id;
  if (kind === "document") {
    const key = documentKeyFromArtifact(a);
    if (!issueId || !key) return null;
    return `/issues/${issueId}/documents/${key}`;
  }
  if (kind === "work_product") {
    return `/work-products/${artifactRawId(a.id)}`;
  }
  if (kind === "attachment") {
    if (!issueId) return null;
    return `/issues/${issueId}/attachments/${artifactRawId(a.id)}`;
  }
  return null;
}

/**
 * The issue-detail LIST path (relative to `/teams`) whose SWR cache also holds
 * this artifact, routed by its typed prefix:
 *   - `document:`     → `/issues/{issueId}/documents`
 *   - `work_product:` → `/issues/{issueId}/work-products`
 *   - `attachment:`   → `/issues/{issueId}/attachments`
 * The gallery and the issue-detail sections keep SEPARATE SWR caches, so after
 * a delete the matching issue-scoped list must be revalidated too or the removed
 * artifact lingers in the task detail within the read layer's dedupe window.
 * Returns null when the issue can't be resolved or the prefix is unrecognized.
 */
export function issueListPathForArtifact(a: CompanyArtifact): string | null {
  const kind = artifactKind(a.id);
  const issueId = a.issue?.id;
  if (!issueId) return null;
  if (kind === "document") return `/issues/${issueId}/documents`;
  if (kind === "work_product") return `/issues/${issueId}/work-products`;
  if (kind === "attachment") return `/issues/${issueId}/attachments`;
  return null;
}

/**
 * The BFF content route (relative to `/teams`) for an artifact's downloadable
 * bytes, or null when it has none. Attachment-backed artifacts expose their
 * bytes through upstream-relative `/api/attachments/{id}/content` URLs:
 *   - a direct `attachment:` artifact carries the attachment id in its OWN typed
 *     id, so the route is derived from that;
 *   - a `work_product:` artifact whose output has an attachment carries the
 *     attachment id only inside `openPath`/`downloadPath`, so the route is parsed
 *     back out of those (stripping the `/api` prefix and any `?download=1` query).
 * The bytes must be fetched through `useTeamsApi().download` (Clerk bearer) — a
 * bare `<a href>` to the upstream path can't authenticate — so this returns our
 * BFF-relative path, never the raw upstream one. Documents have no content route.
 */
export function artifactContentPath(a: CompanyArtifact): string | null {
  if (artifactKind(a.id) === "attachment") {
    return `/attachments/${artifactRawId(a.id)}/content`;
  }
  const src = a.downloadPath ?? a.openPath;
  if (!src) return null;
  const path = src.split("?")[0].replace(/^\/api/, "");
  return path.startsWith("/attachments/") ? path : null;
}

export type ArtifactSort = "newest" | "oldest";

/** Stable, non-mutating sort of anything carrying an `updatedAt` timestamp.
 * Default (`newest`) is most-recent first. */
function sortByUpdated<T extends { updatedAt: string }>(items: T[], dir: ArtifactSort): T[] {
  return [...items].sort((a, b) => {
    const diff = Date.parse(a.updatedAt) - Date.parse(b.updatedAt);
    return dir === "newest" ? -diff : diff;
  });
}

/** Client-side sort of the grouped gallery (stack list) by each group's
 * `updatedAt`. Returns a stable copy (never mutates the input). */
export function sortGroupsByUpdated(
  groups: CompanyArtifactGroup[],
  dir: ArtifactSort,
): CompanyArtifactGroup[] {
  return sortByUpdated(groups, dir);
}

/** Client-side sort of an expanded stack's artifacts by each artifact's
 * `updatedAt`. Returns a stable copy (never mutates the input). */
export function sortArtifactsByUpdated(
  artifacts: CompanyArtifact[],
  dir: ArtifactSort,
): CompanyArtifact[] {
  return sortByUpdated(artifacts, dir);
}

export type RecencyBucket = "Past 7 days" | "Past 30 days" | "Older";
const BUCKET_ORDER: RecencyBucket[] = ["Past 7 days", "Past 30 days", "Older"];

/** Split a newest-first artifact list into date buckets for the Recent view.
 * The labels name ELAPSED time, not calendar periods, because that is what the
 * arithmetic below measures: "this month" would put an Aug 10 deliverable in
 * "earlier this month" on Sep 1, and the 1st of a 31-day month in "older".
 * Unparseable dates fall to "Older" rather than "Past 7 days" (a claim we can't
 * make). Order inside a bucket is the input order (server is newest-first). */
export function bucketByRecency(
  artifacts: CompanyArtifact[],
  now: number = Date.now(),
): { label: RecencyBucket; artifacts: CompanyArtifact[] }[] {
  const DAY = 86_400_000;
  const by: Record<RecencyBucket, CompanyArtifact[]> = {
    "Past 7 days": [],
    "Past 30 days": [],
    Older: [],
  };
  for (const a of artifacts) {
    const age = now - Date.parse(a.updatedAt);
    const label: RecencyBucket =
      age < 7 * DAY ? "Past 7 days" : age < 30 * DAY ? "Past 30 days" : "Older";
    by[label].push(a);
  }
  return BUCKET_ORDER.map((label) => ({ label, artifacts: by[label] })).filter(
    (b) => b.artifacts.length > 0,
  );
}
