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

export type ArtifactSort = "newest" | "oldest";

/** Client-side sort of the grouped gallery by each group's `updatedAt`. Returns
 * a stable copy (never mutates the input). Default is newest-first. */
export function sortGroupsByUpdated(
  groups: CompanyArtifactGroup[],
  dir: ArtifactSort,
): CompanyArtifactGroup[] {
  return [...groups].sort((a, b) => {
    const diff = Date.parse(a.updatedAt) - Date.parse(b.updatedAt);
    return dir === "newest" ? -diff : diff;
  });
}
