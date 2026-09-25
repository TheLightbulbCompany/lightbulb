"use client";

import { useMemo, useState } from "react";
import { FileText } from "lucide-react";
import { documentPreviewText, extractDocumentLinks, useDocumentPreview, type ApiMethods, type DocumentLink } from "@lightbulb/shared";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { ArtifactViewerDialog } from "@/components/teams/panels/ArtifactViewerDialog";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { CompanyArtifact } from "@/components/teams/shared/types";
import { useApi } from "@/lib/api";

/**
 * A card for each Paperclip document a message links (`/<PFX>/issues/<ID>#document-<key>`):
 * title, task, a few lines of the body, last updated. Open shows the document in a dialog
 * over the conversation (the Library's viewer), not a navigation away. The data comes from
 * the company-fenced Teams BFF; a failed or foreign read renders nothing.
 */
export function RoomDocumentCards({ text }: { text: string }) {
  const links = useMemo(() => extractDocumentLinks(text), [text]);
  return links.length ? <DocumentCardList links={links} /> : null;
}

function DocumentCardList({ links }: { links: DocumentLink[] }) {
  const api = useApi();
  return <div className="room-link-previews">
    {links.map((link) => <ErrorBoundary key={`${link.issueId}#${link.key}`} fallback={<></>}><DocumentCard api={api} link={link} /></ErrorBoundary>)}
  </div>;
}

function DocumentCard({ api, link }: { api: ApiMethods; link: DocumentLink }) {
  const preview = useDocumentPreview(api, link);
  const [open, setOpen] = useState(false);
  if (!preview) return null;
  const { issue, document } = preview;
  const title = document.title?.trim() || document.key;
  const excerpt = documentPreviewText(document.body ?? "", title);
  const updated = document.updatedAt && !Number.isNaN(Date.parse(document.updatedAt)) ? `Updated ${timeAgo(document.updatedAt)}` : null;
  const artifact: CompanyArtifact = {
    id: `document:${document.id}`,
    source: "document",
    mediaKind: "document",
    title,
    issue: { id: issue.id, identifier: issue.identifier, title: issue.title },
    updatedAt: document.updatedAt ?? "",
    href: `/issues/${issue.identifier}#document-${document.key}`,
  };
  return <>
    <button type="button" className="room-link-card room-doc-card" aria-label={`Open ${title}, ${issue.identifier}`} onClick={() => setOpen(true)}>
      <span className="room-link-card-body">
        <span className="room-doc-card-head"><FileText size={16} aria-hidden="true" /><span className="room-link-card-title">{title}</span></span>
        <span className="room-link-card-meta">{issue.identifier} · {issue.title}</span>
        {excerpt && <span className="room-doc-card-preview">{excerpt}</span>}
        <span className="room-doc-card-foot">
          <span className="room-link-card-meta">{updated}</span>
          <span className="room-doc-card-open" aria-hidden="true">Open</span>
        </span>
      </span>
    </button>
    {open && <ArtifactViewerDialog artifact={artifact} onClose={() => setOpen(false)} />}
  </>;
}
