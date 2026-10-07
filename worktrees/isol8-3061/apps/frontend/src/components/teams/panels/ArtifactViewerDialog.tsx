"use client";

// apps/frontend/src/components/teams/panels/ArtifactViewerDialog.tsx
//
// The company Artifacts gallery viewer. Clicking an artifact row opens this
// dialog, which fetches + renders the artifact's CONTENT in place (rather than
// deep-linking into the issue detail). Routing is by the artifact's typed id
// prefix (see artifactRouting.ts):
//   - document:     GET /teams/issues/{issueId}/documents/{key}  → markdown body
//   - work_product: GET /teams/issues/{issueId}/work-products    → matched row
//                   + its attachment bytes when file-backed
//   - attachment:   GET /teams/attachments/{id}/content (bytes)  → inline media
//
// Attachment bytes are fetched through the BFF content route (which carries the
// Clerk bearer + forwards upstream's CSP / nosniff headers) and turned into an
// object URL — a bare <img src> to the upstream path can't authenticate and
// would trip CSP. Text and markdown render inline; images render inline; PDFs
// embed in an iframe; anything else falls back to a download link.

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ExternalLink, Trash2, Download } from "lucide-react";
import { EditorialDialog } from "@/components/teams/shell/EditorialDialog";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsDetailUrl } from "@/lib/shellNav";
import {
  artifactContentPath,
  artifactKind,
  artifactRawId,
  documentKeyFromArtifact,
} from "./artifactRouting";
import type {
  CompanyArtifact,
  IssueDocument,
  IssueWorkProduct,
} from "@/components/teams/shared/types";

const TEXT_INLINE_MAX_BYTES = 256 * 1024; // ponytail: bigger text → download link; add ranged reads if owners hit it

type ContentState =
  | { status: "loading" }
  | { status: "ready"; url: string; text: string | null }
  | { status: "error" };

function isTextType(contentType: string, mediaKind: string) {
  return (
    contentType.startsWith("text/") ||
    contentType === "application/json" ||
    contentType.endsWith("+json") ||
    mediaKind === "text"
  );
}

// The bytes behind an artifact, rendered by type. Keyed by artifact id at the
// call site so it remounts (fresh "loading") when the viewed artifact changes —
// which is why the effect never needs to reset state synchronously.
function ContentBody({ artifact, path }: { artifact: CompanyArtifact; path: string }) {
  const { download } = useTeamsApi();
  // useTeamsApi() returns a fresh `download` each render; capturing it once at
  // mount lets the fetch effect depend only on `path`. Otherwise the success
  // setState re-renders → new `download` identity → effect re-runs → revokes
  // the just-made object URL and re-downloads, looping until closed. This
  // component is keyed per-artifact (it remounts when the artifact changes), so
  // the mount-time `download` is always the right one.
  const downloadRef = useRef(download);
  const [state, setState] = useState<ContentState>({ status: "loading" });
  const contentType = (artifact.contentType ?? "").toLowerCase();
  const textual = isTextType(contentType, String(artifact.mediaKind));

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | undefined;
    downloadRef.current(path)
      .then(async (blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        const text =
          textual && blob.size <= TEXT_INLINE_MAX_BYTES ? await blob.text() : null;
        if (!cancelled) setState({ status: "ready", url: objectUrl, text });
      })
      .catch(() => {
        if (!cancelled) setState({ status: "error" });
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path, textual]);

  if (state.status === "loading") {
    return (
      <p className="text-sm italic" style={{ color: "var(--midnight-muted)" }}>
        Loading…
      </p>
    );
  }
  if (state.status === "error") {
    return (
      <p role="alert" className="text-sm italic" style={{ color: "var(--err)" }}>
        Could not load this file.
      </p>
    );
  }

  const isImage = contentType.startsWith("image/") || artifact.mediaKind === "image";
  const isPdf = contentType === "application/pdf";

  if (isImage) {
    return (
      // A blob: object URL, not a remote asset the Next image loader can
      // optimize — plain <img> is correct here.
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={state.url}
        alt={artifact.title}
        className="mx-auto max-h-[60vh] max-w-full rounded-sm"
      />
    );
  }
  if (isPdf) {
    return (
      <iframe
        src={state.url}
        title={artifact.title}
        className="h-[60vh] w-full rounded-sm border"
        style={{ borderColor: "var(--edge)" }}
      />
    );
  }
  if (state.text !== null) {
    return contentType === "text/markdown" || contentType === "application/markdown" ? (
      <MarkdownView>{state.text}</MarkdownView>
    ) : (
      <pre
        className="whitespace-pre-wrap break-words text-xs leading-5"
        style={{ color: "var(--midnight-soft)" }}
      >
        {state.text}
      </pre>
    );
  }
  return (
    <a
      href={state.url}
      download={artifact.title || "attachment"}
      className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm"
      style={{ background: "var(--midnight)", color: "var(--cream)" }}
    >
      <Download size={16} />
      Download {artifact.title}
    </a>
  );
}

function DocumentBody({
  artifact,
  issueId,
}: {
  artifact: CompanyArtifact;
  issueId: string;
}) {
  const { read } = useTeamsApi();
  const key = documentKeyFromArtifact(artifact);
  const { data, error, isLoading } = read<IssueDocument>(
    issueId && key ? `/issues/${issueId}/documents/${key}` : null,
  );

  if (!key) {
    return (
      <p role="alert" className="text-sm italic" style={{ color: "var(--err)" }}>
        This document can’t be opened.
      </p>
    );
  }
  if (isLoading && !data) {
    return (
      <p className="text-sm italic" style={{ color: "var(--midnight-muted)" }}>
        Loading document…
      </p>
    );
  }
  if (error && !data) {
    return (
      <p role="alert" className="text-sm italic" style={{ color: "var(--err)" }}>
        Could not load this document.
      </p>
    );
  }
  const body = data?.body?.trim();
  if (!body) {
    return (
      <p className="text-sm italic" style={{ color: "var(--midnight-muted)" }}>
        (empty document)
      </p>
    );
  }
  return <MarkdownView>{body}</MarkdownView>;
}

function WorkProductBody({
  artifact,
  issueId,
}: {
  artifact: CompanyArtifact;
  issueId: string;
}) {
  const { read } = useTeamsApi();
  const { data, error, isLoading, isValidating, mutate } = read<{
    workProducts?: IssueWorkProduct[];
  }>(issueId ? `/issues/${issueId}/work-products` : null, {
    // This list SWR key is SHARED with the issue-detail Output section, and the
    // read layer applies a 15s dedupe window. If the task was opened BEFORE this
    // deliverable existed, the cache holds a miss for it — SWR's default
    // "revalidateIfStale" is the safety net, but on its own it can still be
    // swallowed by the dedupe window.
    revalidateOnMount: true,
  });
  const wp = data?.workProducts?.find((w) => w.id === artifactRawId(artifact.id));
  // The skill's default upload puts the deliverable's BYTES behind an
  // attachment, reachable only through the artifact's download/open path.
  const contentPath = artifactContentPath(artifact);

  // Force a fresh fetch when the viewer opens (an explicit mutate bypasses the
  // read layer's dedupe window, unlike a mount-time auto-revalidation). Without
  // this, a cached miss would render a false "Could not load" for a valid
  // artifact — so "not found" is treated as final only once the refetch settles.
  useEffect(() => {
    void mutate();
  }, [mutate]);

  // Still fetching (initial load OR the on-open revalidation) and no match yet:
  // a cached miss is not confirmation the output is gone. Keep showing loading.
  if (!wp && (isLoading || isValidating)) {
    return (
      <p className="text-sm italic" style={{ color: "var(--midnight-muted)" }}>
        Loading output…
      </p>
    );
  }
  // The list read supplies the SUMMARY only; the deliverable's bytes hang off
  // the artifact row and are readable whether or not that read landed. So the
  // content renders in every branch below, including the failure ones — an
  // owner whose list read 500s can still read the file they clicked.
  const content = contentPath ? (
    <ContentBody key={artifact.id} artifact={artifact} path={contentPath} />
  ) : null;

  if (!wp) {
    return (
      <div className="flex flex-col gap-2">
        {error || data ? (
          <p role="alert" className="text-sm italic" style={{ color: "var(--err)" }}>
            Could not load this output.
          </p>
        ) : null}
        {content}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      {/* The agent upload helper defaults the summary to "", so a file-backed
          deliverable usually has none. Saying "no summary" directly above the
          thing itself is noise — only claim it when there is nothing else. */}
      {wp.summary?.trim() ? (
        <MarkdownView>{wp.summary}</MarkdownView>
      ) : contentPath ? null : (
        <p className="text-sm italic" style={{ color: "var(--midnight-muted)" }}>
          No summary provided.
        </p>
      )}
      {wp.url ? (
        <a
          href={wp.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 text-sm underline"
          style={{ color: "var(--midnight)" }}
        >
          <ExternalLink size={16} />
          {wp.url}
        </a>
      ) : null}
      {content}
    </div>
  );
}

export interface ArtifactViewerDialogProps {
  artifact: CompanyArtifact;
  onClose: () => void;
  /** Ask the panel to run the confirm-then-delete flow for this artifact. */
  onRequestDelete: (artifact: CompanyArtifact) => void;
}

export function ArtifactViewerDialog({
  artifact,
  onClose,
  onRequestDelete,
}: ArtifactViewerDialogProps) {
  const nav = useInlineTeamsNav();
  const kind = artifactKind(artifact.id);
  const issueId = artifact.issue?.id ?? "";
  const taskUrl = issueId ? teamsDetailUrl("issue", issueId) : "#";

  const openTask = (e: React.MouseEvent) => {
    if (!nav || !issueId) return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    onClose();
    nav.openDetail("issue-detail", issueId);
  };

  return (
    <EditorialDialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
      className="sm:max-w-[720px]"
      title={artifact.title || "Untitled"}
      description={
        artifact.issue?.identifier
          ? `${artifact.issue.identifier} · ${artifact.issue.title}`
          : undefined
      }
      footer={
        <>
          {issueId ? (
            <Link
              href={taskUrl}
              onClick={openTask}
              className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm"
              style={{ background: "var(--edge)", color: "var(--midnight)" }}
            >
              <ExternalLink size={16} />
              Open task
            </Link>
          ) : null}
          <button
            type="button"
            onClick={() => onRequestDelete(artifact)}
            className="inline-flex items-center gap-1.5 rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground"
          >
            <Trash2 size={16} />
            Delete
          </button>
        </>
      }
    >
      <div className="max-h-[60vh] overflow-y-auto">
        {kind === "document" ? (
          <DocumentBody artifact={artifact} issueId={issueId} />
        ) : kind === "work_product" ? (
          <WorkProductBody artifact={artifact} issueId={issueId} />
        ) : kind === "attachment" ? (
          // An `attachment:` row always resolves a content path from its own id.
          <ContentBody
            key={artifact.id}
            artifact={artifact}
            path={artifactContentPath(artifact)!}
          />
        ) : (
          <p role="alert" className="text-sm italic" style={{ color: "var(--err)" }}>
            This can’t be opened here.
          </p>
        )}
      </div>
    </EditorialDialog>
  );
}
