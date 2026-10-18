"use client";

// Paperclip attachment bytes, fenced to the issue that owns them.
//
// Agents paste attachment links in the upstream shape
// (`/api/attachments/{id}/content`, the `contentPath` the upload helper prints)
// — a path that only resolves on Paperclip and needs a Clerk bearer here. The
// BFF serves the bytes ONLY issue-scoped
// (`/teams/issues/{issueId}/attachments/{id}/content`): upstream's own route is
// not issue-scoped and a Paperclip user can belong to several companies, so
// the issue is what pins the attachment to this workspace. A surface that has
// no owning issue cannot fetch the bytes at all; it renders the link inert.

import { createContext, useContext, useEffect, useRef, useState } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";

/** The issue whose attachments the rendered content may fetch; null = none. */
export const AttachmentIssueContext = createContext<string | null>(null);

export function useAttachmentIssueId(): string | null {
  return useContext(AttachmentIssueContext);
}

// Same id alphabet the BFF validates, so an odd id is inert, never a 422 fetch.
const ATTACHMENT_HREF = /^\/api\/attachments\/([A-Za-z0-9_-]+)\/content(?:[?#].*)?$/;

/** The attachment id in an upstream attachment href, else null. */
export function attachmentIdFromHref(href?: string | null): string | null {
  return (href && ATTACHMENT_HREF.exec(href)?.[1]) || null;
}

/** BFF content path (relative to `/teams`) for an attachment on an issue. */
export function issueAttachmentContentPath(issueId: string, attachmentId: string): string {
  return `/issues/${encodeURIComponent(issueId)}/attachments/${encodeURIComponent(attachmentId)}/content`;
}

/** BFF content path for an upstream attachment href on `issueId`, or null when
 * the href is not an attachment or there is no owning issue. */
export function attachmentPathFromHref(href: string | null | undefined, issueId: string | null | undefined): string | null {
  const id = attachmentIdFromHref(href);
  return id && issueId ? issueAttachmentContentPath(issueId, id) : null;
}

/** Object URL for an upstream attachment href within the current issue
 * context, or null until loaded / on error / without an issue. */
export function useAttachmentObjectUrl(upstreamPath: string | null | undefined): string | null {
  const { download } = useTeamsApi();
  // `download` is a fresh function every render; capture it once so the fetch
  // depends only on the path (same mount-ref as ArtifactCards' ImagePreview).
  const downloadRef = useRef(download);
  const [url, setUrl] = useState<string | null>(null);
  const path = attachmentPathFromHref(upstreamPath, useAttachmentIssueId());

  useEffect(() => {
    if (!path) return;
    let cancelled = false;
    let objectUrl: string | undefined;
    downloadRef
      .current(path)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path]);

  return url;
}
