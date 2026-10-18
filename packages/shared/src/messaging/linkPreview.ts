import useSWR from "swr";

import type { ApiMethods } from "../api/client";
import { isDocumentLink } from "./documentLinks";

/** GET /messaging/link-preview?url= — fetched server-side (SSRF-guarded, cached ~24 h). */
export interface LinkPreview {
  url: string;
  kind: "link" | "tweet";
  title: string;
  description: string | null;
  siteName: string | null;
  imageUrl: string | null;
  author: string | null;
  handle: string | null;
  avatarUrl: string | null;
  publishedAt: string | null;
  /** Raw X syndication data (react-tweet's `Tweet` shape) when the backend had it. */
  tweet: Record<string, unknown> | null;
}

export const MAX_LINK_PREVIEWS = 2;

// Stops at whitespace and at the characters that close a Markdown link or an <autolink>.
const URL_RE = /https?:\/\/[^\s<>()[\]"'`]+/gi;
const TRAILING = /[.,;:!?*_~]+$/;

// Our own product (thelightbulb.company and every subdomain) is never unfurled: its links are
// task/document links that already render as a chip or card, or our own marketing page.
// Both the product domain and the retired one (which redirects to it).
const OWN_HOST = /(^|\.)(thelightbulb\.company|flickmeon\.ai)$/i;

/** The first http(s) URLs in a message's text, in order, de-duplicated. Paperclip document
 * links and our own domain are skipped: they get a document card or task chip, not a web preview. */
export function extractPreviewUrls(text: string, max = MAX_LINK_PREVIEWS): string[] {
  const urls: string[] = [];
  // A URL inside a fenced block belongs to the block (a Sources card draws its own link); no preview for it.
  for (const match of text.replace(/(`{3,}|~{3,})[\s\S]*?(\1|$)/g, " ").matchAll(URL_RE)) {
    const url = match[0].replace(TRAILING, "");
    let host: string;
    try {
      host = new URL(url).hostname;
    } catch {
      continue;
    }
    if (isDocumentLink(url) || OWN_HOST.test(host)) continue;
    if (!urls.includes(url)) urls.push(url);
    if (urls.length >= max) break;
  }
  return urls;
}

export function linkPreviewPath(url: string): string {
  return `/messaging/link-preview?url=${encodeURIComponent(url)}`;
}

/** "www.example.com/a" -> "example.com". */
export function linkDomain(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/**
 * One URL's preview, fetched once per session per URL (SWR dedupes by key and never
 * revalidates). `undefined` while loading AND on any failure — callers render nothing.
 */
export function useLinkPreview(api: Pick<ApiMethods, "get"> | null, url: string): LinkPreview | undefined {
  const { data } = useSWR<LinkPreview>(
    api ? ["link-preview", url] : null,
    () => api!.get(linkPreviewPath(url)) as Promise<LinkPreview>,
    {
      revalidateOnFocus: false,
      revalidateOnReconnect: false,
      revalidateIfStale: false,
      shouldRetryOnError: false,
      dedupingInterval: 60 * 60 * 1000,
    },
  );
  return data;
}
