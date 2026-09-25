"use client";

import { useMemo, useState } from "react";
import { EmbeddedTweet } from "react-tweet";
import type { Tweet } from "react-tweet/api";
import { extractPreviewUrls, linkDomain, useLinkPreview, type ApiMethods, type LinkPreview } from "@lightbulb/shared";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { useApi } from "@/lib/api";

/**
 * iMessage-style cards for the first URLs in a message. The backend fetches the metadata
 * (never the browser); a failed or empty fetch renders nothing. X posts with syndication
 * data render through react-tweet (data only, no Twitter script); everything else is our card.
 */
export function RoomLinkPreviews({ text }: { text: string }) {
  const urls = useMemo(() => extractPreviewUrls(text), [text]);
  return urls.length ? <PreviewList urls={urls} /> : null;
}

function PreviewList({ urls }: { urls: string[] }) {
  const api = useApi();
  return <div className="room-link-previews">
    {urls.map((url) => <ErrorBoundary key={url} fallback={<></>}><Preview api={api} url={url} /></ErrorBoundary>)}
  </div>;
}

function Preview({ api, url }: { api: ApiMethods; url: string }) {
  const preview = useLinkPreview(api, url);
  if (!preview) return null;
  if (preview.kind === "tweet" && preview.tweet) {
    return <div className="room-tweet"><EmbeddedTweet tweet={preview.tweet as unknown as Tweet} /></div>;
  }
  return <LinkCard preview={preview} />;
}

export function LinkCard({ preview }: { preview: LinkPreview }) {
  const [imageFailed, setImageFailed] = useState(false);
  const [avatarFailed, setAvatarFailed] = useState(false);
  const tweet = preview.kind === "tweet";
  const date = preview.publishedAt && !Number.isNaN(Date.parse(preview.publishedAt))
    ? new Date(preview.publishedAt).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
    : null;
  return <a className="room-link-card" href={preview.url} target="_blank" rel="noopener noreferrer">
    {/* eslint-disable-next-line @next/next/no-img-element -- arbitrary third-party og:image, no loader config */}
    {preview.imageUrl && !imageFailed && <img className="room-link-card-image" src={preview.imageUrl} alt="" loading="lazy"
      referrerPolicy="no-referrer" onError={() => setImageFailed(true)} />}
    <span className="room-link-card-body">
      {tweet ? <span className="room-link-card-author">
        {/* eslint-disable-next-line @next/next/no-img-element -- X avatar, no loader config */}
        {preview.avatarUrl && !avatarFailed && <img className="room-link-card-avatar" src={preview.avatarUrl} alt="" loading="lazy"
          referrerPolicy="no-referrer" onError={() => setAvatarFailed(true)} />}
        <span className="room-link-card-title">{preview.author ?? preview.title}</span>
        {preview.handle && <span className="room-link-card-meta">@{preview.handle}</span>}
      </span> : <span className="room-link-card-title">{preview.title}</span>}
      {preview.description && <span className={tweet ? "room-link-card-text" : "room-link-card-description"}>{preview.description}</span>}
      <span className="room-link-card-meta">{[preview.siteName || linkDomain(preview.url), date].filter(Boolean).join(" · ")}</span>
    </span>
  </a>;
}
