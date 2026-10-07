// Issue attachments data layer: list (SWR) + upload + download + delete, all
// through the Teams BFF (`/teams/issues/{id}/attachments` + the shared
// `/teams/attachments/{id}/content` proxy). Mirrors useIssueMutations' shape
// so the section component stays declarative.

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { IssueAttachment } from "@/components/teams/shared/types";

const SWR_PREFIX = "/teams";
const swrKey = (path: string) => `${SWR_PREFIX}${path}`;

export interface UseIssueAttachmentsResult {
  attachments: IssueAttachment[];
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
  /** Upload a single file; revalidates the list on success. */
  upload: (file: File) => Promise<IssueAttachment>;
  /** Delete an attachment; revalidates the list on success. */
  remove: (attachmentId: string) => Promise<void>;
  /** Fetch the bytes (authenticated) and trigger a browser download. */
  download: (attachment: IssueAttachment) => Promise<void>;
}

export function useIssueAttachments(issueId: string): UseIssueAttachmentsResult {
  const { read, upload, download, del } = useTeamsApi();
  const { mutate } = useSWRConfig();
  const listPath = `/issues/${issueId}/attachments`;
  const { data, error, isLoading } = read<{ attachments: IssueAttachment[] }>(listPath);

  const revalidate = useCallback(() => {
    void mutate(swrKey(listPath));
  }, [mutate, listPath]);

  const doUpload = useCallback(
    async (file: File): Promise<IssueAttachment> => {
      const form = new FormData();
      // Field name MUST be "file" — the BFF/upstream route reads
      // upload.single("file"). A different name yields "Missing file field".
      form.append("file", file);
      const created = await upload<IssueAttachment>(listPath, form);
      revalidate();
      return created;
    },
    [upload, listPath, revalidate],
  );

  const remove = useCallback(
    async (attachmentId: string): Promise<void> => {
      await del(`/issues/${issueId}/attachments/${attachmentId}`);
      revalidate();
    },
    [del, issueId, revalidate],
  );

  const doDownload = useCallback(
    async (attachment: IssueAttachment): Promise<void> => {
      const blob = await download(`/attachments/${attachment.id}/content`);
      const url = URL.createObjectURL(blob);
      try {
        const a = document.createElement("a");
        a.href = url;
        a.download = attachment.originalFilename ?? "attachment";
        document.body.appendChild(a);
        a.click();
        a.remove();
      } finally {
        // Revoke after the click so the browser has grabbed the bytes.
        URL.revokeObjectURL(url);
      }
    },
    [download],
  );

  return {
    attachments: data?.attachments ?? [],
    isLoading,
    isError: !!error,
    error: error ?? null,
    upload: doUpload,
    remove,
    download: doDownload,
  };
}
