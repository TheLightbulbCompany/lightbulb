import useSWR from "swr";

import type { ApiMethods } from "../api/client";

/**
 * A Paperclip issue-document link, the form the bundled skills write
 * (`/<PREFIX>/issues/<IDENTIFIER>#document-<key>`), relative or on any host.
 * Group 1 is the character before a relative link (start, space, `(`, `<`, `[`)
 * so a Paperclip-shaped path buried inside some other URL does not match.
 */
export const DOCUMENT_LINK_RE =
  /(^|[\s(<[])(?:https?:\/\/[^\s/<>()[\]"'`]+)?\/[A-Z][A-Z0-9]{0,9}\/issues\/([A-Za-z0-9_-]+)#document-([a-z0-9][a-z0-9_-]{0,63})/g;

export const MAX_DOCUMENT_CARDS = 2;

export interface DocumentLink {
  issueId: string;
  key: string;
}

/** The first document links in a message, in order, de-duplicated. */
export function extractDocumentLinks(text: string, max = MAX_DOCUMENT_CARDS): DocumentLink[] {
  const links: DocumentLink[] = [];
  for (const [, , issueId, key] of text.matchAll(DOCUMENT_LINK_RE)) {
    if (!links.some((l) => l.issueId === issueId && l.key === key)) links.push({ issueId, key });
    if (links.length >= max) break;
  }
  return links;
}

export function isDocumentLink(url: string): boolean {
  return extractDocumentLinks(url, 1).length > 0;
}

/** The first few readable lines of a markdown body, markup stripped, for a card preview. */
export function documentPreviewText(body: string, title?: string | null, maxLines = 3): string {
  const lines = body
    .split("\n")
    .map((line) =>
      line
        .replace(/^\s*(#{1,6}\s+|[-*+>]\s+|\d+[.)]\s+)/, "")
        .replace(/!?\[([^\]]*)\]\([^)]*\)|[*_`~|]/g, (_m, label?: string) => label ?? " ")
        .replace(/\s+/g, " ")
        .trim(),
    )
    .filter((line) => line && !/^[-:\s]+$/.test(line));
  if (title && lines[0]?.toLowerCase() === title.trim().toLowerCase()) lines.shift();
  return lines.slice(0, maxLines).join(" ");
}

export interface DocumentPreview {
  issue: { id: string; identifier: string; title: string; status?: string | null };
  document: { id: string; key: string; title?: string | null; body?: string | null; updatedAt?: string | null };
}

/**
 * One linked document plus its task, through the company-fenced Teams BFF
 * (`/teams/issues/{id}` and `/teams/issues/{id}/documents/{key}` — a task from
 * another company 404s). `undefined` while loading AND on any failure — callers render nothing.
 */
export function useDocumentPreview(api: Pick<ApiMethods, "get"> | null, link: DocumentLink): DocumentPreview | undefined {
  const issuePath = `/teams/issues/${encodeURIComponent(link.issueId)}`;
  const { data } = useSWR<DocumentPreview>(
    api ? ["document-preview", link.issueId, link.key] : null,
    async () => {
      const [issue, document] = await Promise.all([
        api!.get(issuePath),
        api!.get(`${issuePath}/documents/${encodeURIComponent(link.key)}`),
      ]);
      return { issue, document } as DocumentPreview;
    },
    { revalidateOnFocus: false, revalidateOnReconnect: false, shouldRetryOnError: false, dedupingInterval: 60 * 1000 },
  );
  return data;
}
