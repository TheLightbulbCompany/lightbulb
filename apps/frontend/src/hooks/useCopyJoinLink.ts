"use client";

import { useCallback } from "react";
import { useAuth } from "@clerk/nextjs";
import { ApiError, useApi } from "@/lib/api";
import { capture } from "@/lib/analytics";
import { copyTextWhenReady, JoinLinkCopyError } from "@/lib/clipboard";

export const JOIN_LINK_COPIED = "Copied. Anyone who opens it and signs in joins this workspace.";

/** What to tell the person after a copy attempt failed. One copy of these words for every surface. */
export function joinLinkNote(error: unknown): string {
  if (error instanceof ApiError && error.status === 403) return "This workspace's join link is turned off. Ask a workspace admin for a new one.";
  if (error instanceof JoinLinkCopyError && error.reason === "unavailable") return "This workspace's link can't be shown here. A workspace admin can regenerate it under Settings → Members (that turns the old link off).";
  if (error instanceof JoinLinkCopyError && error.reason === "clipboard") return "Couldn't copy automatically. Try again, or ask a workspace admin for the link.";
  return "Couldn't get the join link. Try again, or ask a workspace admin for the link.";
}

/** The ONE door into the workspace for someone who is not in it yet. Copies the workspace's join
 *  link, minting one only when none exists. Any member may (D5); only replacing a link is
 *  admin-only, and this never replaces. Throws on failure; `source` labels the analytics event. */
export function useCopyJoinLink(): (source: string) => Promise<void> {
  const { orgId } = useAuth();
  const api = useApi();
  return useCallback(async (source: string) => {
    if (!orgId) throw new Error("no workspace");
    const url = (async () => {
      const status = (await api.get(`/orgs/${orgId}/join-link`)) as { exists: boolean; join_url?: string | null };
      if (status.exists) {
        // A row whose plaintext cannot be shown is still a working link for everyone who has it;
        // minting here would revoke it under a control labelled Copy. Settings → Members regenerates.
        if (!status.join_url) throw new JoinLinkCopyError("unavailable");
        return status.join_url;
      }
      return ((await api.post(`/orgs/${orgId}/join-link`, {})) as { join_url: string }).join_url;
    })();
    // Started inside the click so Safari and the desktop webview still honour the gesture.
    const copied = await copyTextWhenReady(url);
    await url; // rethrows the fetch's own error ahead of a clipboard verdict
    if (!copied) throw new JoinLinkCopyError("clipboard");
    capture("join_link_copied", { source, workspace_id: orgId });
  }, [api, orgId]);
}
