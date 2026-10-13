// Lightbulb adaptation for the ported issue Output section (no stock twin).
//
// Stock renders `/api/attachments/{id}/content` straight into <img>/<video>
// src and <a href>: same origin, cookie auth. Here those bytes only come
// through the Teams BFF with a Clerk bearer, so every media src and every
// Open/Download action fetches a Blob first — issue-scoped, from the
// AttachmentIssueContext the issue page provides.

"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import { notifyApiError } from "@/lib/notify";
import {
  attachmentPathFromHref,
  useAttachmentIssueId,
  useAttachmentObjectUrl,
} from "@/components/teams/shared/lib/attachments";

// Types a new tab may render from our origin. Anything else (SVG, HTML, …) is
// re-typed as octet-stream so the browser downloads it instead of running it:
// a blob: URL carries our origin, and upstream's CSP/Content-Disposition do
// not follow the bytes into it.
const INLINE_SAFE_TYPE = /^(image\/(png|jpe?g|gif|webp|avif)|video\/[a-z0-9.+-]+|application\/pdf)$/;

export function saveBlob(url: string, name: string) {
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export { useAttachmentObjectUrl };

/** Open (new tab) and Download actions for an upstream attachment path. */
export function useAttachmentActions() {
  const { download } = useTeamsApi();
  const issueId = useAttachmentIssueId();

  const fetchBlob = async (upstreamPath: string) => {
    const path = attachmentPathFromHref(upstreamPath, issueId);
    if (!path) throw new Error("Not an attachment path");
    return download(path);
  };

  return {
    async open(upstreamPath: string, name: string, contentType?: string | null) {
      // Open the tab inside the click so popup blockers allow it, then point it
      // at the bytes once they arrive.
      const tab = window.open("", "_blank");
      try {
        const blob = await fetchBlob(upstreamPath);
        const type = (contentType ?? blob.type).toLowerCase().split(";")[0].trim();
        const safe = new Blob([blob], { type: INLINE_SAFE_TYPE.test(type) ? type : "application/octet-stream" });
        const url = URL.createObjectURL(safe);
        if (tab) tab.location.href = url;
        else saveBlob(url, name);
        // ponytail: fixed revoke delay; the tab has loaded by then in practice.
        window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
      } catch (err) {
        tab?.close();
        notifyApiError(err, "Couldn't open this file");
      }
    },
    async download(upstreamPath: string, name: string) {
      try {
        const url = URL.createObjectURL(await fetchBlob(upstreamPath));
        try {
          saveBlob(url, name);
        } finally {
          URL.revokeObjectURL(url);
        }
      } catch (err) {
        notifyApiError(err, "Couldn't download this file");
      }
    },
  };
}
