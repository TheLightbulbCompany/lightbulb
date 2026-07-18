"use client";

// apps/frontend/src/components/teams/panels/ArtifactViewerDialog.tsx
//
// The company Artifacts gallery viewer. Clicking an artifact row opens this
// dialog, which fetches + renders the artifact's CONTENT in place (rather than
// deep-linking into the issue detail). Routing is by the artifact's typed id
// prefix (see artifactRouting.ts):
//   - document:     GET /teams/issues/{issueId}/documents/{key}  → markdown body
//   - work_product: GET /teams/issues/{issueId}/work-products    → matched row
//   - attachment:   GET /teams/attachments/{id}/content (bytes)  → inline media
//
// Attachment bytes are fetched through the BFF content route (which carries the
// Clerk bearer + forwards upstream's CSP / nosniff headers) and turned into an
// object URL — a bare <img src> to the upstream path can't authenticate and
// would trip CSP. Images render inline; PDFs embed in an iframe; anything else
// falls back to a download link.

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ExternalLink, Trash2, Download } from "lucide-react";
import { EditorialDialog } from "@/components/teams/shell/EditorialDialog";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsDetailUrl } from "@/lib/shellNav";
import {
  artifactKind,
  artifactRawId,
  documentKeyFromArtifact,
} from "./artifactRouting";
import type {
  CompanyArtifact,
  IssueDocument,
  IssueWorkProduct,
} from "@/components/teams/shared/types";

type AttachmentState =
  | { status: "loading" }
  | { status: "ready"; url: string }
  | { status: "error" };

// Keyed by artifact id at the call site so it remounts (fresh "loading") when
// the viewed attachment changes — which is why the effect never needs to reset
// state synchronously.
function AttachmentBody({ artifact }: { artifact: CompanyArtifact }) {
  const { download } = useTeamsApi();
  // useTeamsApi() returns a fresh `download` each render; capturing it once at
  // mount lets the fetch effect depend only on `attachmentId`. Otherwise the
  // success setState re-renders → new `download` identity → effect re-runs →
  // revokes the just-made object URL and re-downloads, looping until closed.
  // This component is keyed per-artifact (it remounts when the attachment
  // changes), so the mount-time `download` is always the right one.
  const downloadRef = useRef(download);
  const [state, setState] = useState<AttachmentState>({ status: "loading" });
  const attachmentId = artifactRawId(artifact.id);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | undefined;
    downloadRef.current(`/attachments/${attachmentId}/content`)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setState({ status: "ready", url: objectUrl });
      })
      .catch(() => {
        if (!cancelled) setState({ status: "error" });
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [attachmentId]);

  if (state.status === "loading") {
    return (
      <p className="text-sm italic" style={{ color: "var(--mute)" }}>
        Loading attachment…
      </p>
    );
  }
  if (state.status === "error") {
    return (
      <p role="alert" className="text-sm italic" style={{ color: "var(--warn)" }}>
        Could not load this attachment.
      </p>
    );
  }

  const contentType = (artifact.contentType ?? "").toLowerCase();
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
        className="mx-auto max-h-[60vh] max-w-full rounded"
      />
    );
  }
  if (isPdf) {
    return (
      <iframe
        src={state.url}
        title={artifact.title}
        className="h-[60vh] w-full rounded border"
        style={{ borderColor: "var(--edge)" }}
      />
    );
  }
  return (
    <a
      href={state.url}
      download={artifact.title || "attachment"}
      className="inline-flex items-center gap-1.5 rounded px-3 py-1.5 text-sm"
      style={{ background: "var(--forest)", color: "var(--forest-ink, #fff)" }}
    >
      <Download className="h-4 w-4" />
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
      <p role="alert" className="text-sm italic" style={{ color: "var(--warn)" }}>
        This document can’t be opened.
      </p>
    );
  }
  if (isLoading && !data) {
    return (
      <p className="text-sm italic" style={{ color: "var(--mute)" }}>
        Loading document…
      </p>
    );
  }
  if (error && !data) {
    return (
      <p role="alert" className="text-sm italic" style={{ color: "var(--warn)" }}>
        Could not load this document.
      </p>
    );
  }
  const body = data?.body?.trim();
  if (!body) {
    return (
      <p className="text-sm italic" style={{ color: "var(--mute)" }}>
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
  const { data, error, isLoading } = read<{ workProducts?: IssueWorkProduct[] }>(
    issueId ? `/issues/${issueId}/work-products` : null,
  );
  const wp = data?.workProducts?.find((w) => w.id === artifactRawId(artifact.id));

  if (isLoading && !data) {
    return (
      <p className="text-sm italic" style={{ color: "var(--mute)" }}>
        Loading output…
      </p>
    );
  }
  if ((error && !data) || (data && !wp)) {
    return (
      <p role="alert" className="text-sm italic" style={{ color: "var(--warn)" }}>
        Could not load this output.
      </p>
    );
  }
  if (!wp) return null;
  return (
    <div className="flex flex-col gap-2">
      {wp.summary?.trim() ? (
        <MarkdownView>{wp.summary}</MarkdownView>
      ) : (
        <p className="text-sm italic" style={{ color: "var(--mute)" }}>
          No summary provided.
        </p>
      )}
      {wp.url ? (
        <a
          href={wp.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 text-sm underline"
          style={{ color: "var(--forest)" }}
        >
          <ExternalLink className="h-3.5 w-3.5" />
          {wp.url}
        </a>
      ) : null}
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
      title={artifact.title || "Artifact"}
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
              className="inline-flex items-center gap-1.5 rounded px-3 py-1.5 text-sm"
              style={{ background: "var(--edge)", color: "var(--ink)" }}
            >
              <ExternalLink className="h-4 w-4" />
              Open task
            </Link>
          ) : null}
          <button
            type="button"
            onClick={() => onRequestDelete(artifact)}
            className="inline-flex items-center gap-1.5 rounded px-3 py-1.5 text-sm"
            style={{ background: "var(--warn)", color: "var(--warn-ink, #fff)" }}
          >
            <Trash2 className="h-4 w-4" />
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
          <AttachmentBody key={artifact.id} artifact={artifact} />
        ) : (
          <p role="alert" className="text-sm italic" style={{ color: "var(--warn)" }}>
            Unsupported artifact type.
          </p>
        )}
      </div>
    </EditorialDialog>
  );
}
