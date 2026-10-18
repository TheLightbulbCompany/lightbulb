"use client";

import { useMemo } from "react";
import { FileText } from "lucide-react";
import { documentPreviewText, extractDocumentLinks, useDocumentPreview, type ApiMethods, type DocumentLink } from "@lightbulb/shared";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { openPanel } from "@/components/workspace/workspaceController";
import { useApi } from "@/lib/api";

/**
 * A card for each Paperclip document a message links (`/<PFX>/issues/<ID>#document-<key>`):
 * title, task, a few lines of the body, last updated. Open shows the document in the artifact pane
 * beside the conversation, not a navigation away. The data comes from
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
  if (!preview) return null;
  const { issue, document } = preview;
  const title = document.title?.trim() || document.key;
  const excerpt = documentPreviewText(document.body ?? "", title);
  const updated = document.updatedAt && !Number.isNaN(Date.parse(document.updatedAt)) ? `Updated ${timeAgo(document.updatedAt)}` : null;
  return (
    <button type="button" className="room-link-card room-doc-card" aria-label={`Open ${title}, ${issue.identifier}`} onClick={() => openPanel("artifact", { src: "paperclip", kind: "document", issue: issue.id, key: document.key, ident: issue.identifier }, { pushUrl: true })}>
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
  );
}
