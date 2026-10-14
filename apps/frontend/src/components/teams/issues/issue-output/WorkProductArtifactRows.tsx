// Ported from Paperclip @ v2026.916.1-isol8.2 (MIT, (c) 2025 Paperclip AI):
//   - MarkdownWorkProductRow: ui/src/components/issue-properties/IssuePropertiesArtifactsTab.tsx
//   - WorkProductCard + stateChipFor: ui/src/components/task-chat/RichWorkProductCard.tsx
//     (the `variant="compact"` the Artifacts tab renders; the card variant is unused)
// These are the work-product rows of stock's task-chat side panel (Artifacts
// tab). Adaptations: data through useTeamsApi (review-document POST, revalidate
// with its scoped `mutate`); the review document renders with our MarkdownView
// (no inline annotation popover); attachment paths download through the BFF;
// external links go through safeHref; the image gallery modal is dropped; brand
// tokens replace the status-chip CSS vars. Lightbulb keeps its board-review
// Approve / Request changes on `needs_board_review` rows (stock has no such
// buttons here) — that is our guarded PATCH, not a stock control.

"use client";

import { useEffect, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Download,
  ExternalLink,
  File,
  FileText,
  Film,
  GitBranch,
  GitCommit,
  GitPullRequest,
  Globe,
  Image as ImageIcon,
  Server,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { attachmentIdFromHref } from "@/components/teams/shared/lib/attachments";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import type {
  IssueDocument,
  IssueWorkProduct,
  IssueWorkProductReviewState,
} from "@/components/teams/shared/types";
import {
  MARKDOWN_REVIEW_DOCUMENT_MAX_BYTES,
  isImageContentType,
  isVideoLikeOutput,
  workProductHref,
  type AttachmentArtifactWorkProductMetadata,
} from "../lib/issue-output";
import { useAttachmentActions } from "./attachmentBlob";

type ChipTone = "progress" | "failure" | "review" | "success" | "neutral";
type StateChip = { label: string; tone: ChipTone; dashed?: boolean };

const TONE_VAR: Record<ChipTone, string> = {
  failure: "var(--err)",
  success: "var(--ok)",
  neutral: "var(--midnight-muted)",
  review: "var(--warn)",
  progress: "var(--info)",
};

/** Resting states stay quiet. A completed work product never gets a chip. */
export function stateChipFor(
  kind: IssueWorkProduct["type"],
  status: string | null | undefined,
  reviewState: string | null | undefined,
): StateChip | null {
  if (reviewState === "changes_requested" || status === "changes_requested") {
    return { label: "Changes requested", tone: "failure" };
  }
  if (reviewState === "needs_board_review" || status === "ready_for_review") {
    return { label: "Review", tone: "review" };
  }
  if (["failed", "unhealthy", "down"].includes(status ?? "")) {
    return { label: "Failed", tone: "failure" };
  }
  if (["pending", "opening"].includes(status ?? "")) {
    return { label: status === "opening" ? "Opening" : "Pending", tone: "progress", dashed: true };
  }
  if (kind === "pull_request" && (status === "active" || status === "open")) {
    return { label: "Open", tone: "progress" };
  }
  if (kind === "pull_request" && status === "draft") {
    return { label: "Draft", tone: "review" };
  }
  if (kind === "pull_request" && status === "merged") {
    return { label: "Merged", tone: "success" };
  }
  if (kind === "pull_request" && status === "closed") {
    return { label: "Closed", tone: "neutral" };
  }
  if (kind === "runtime_service" && status === "active") {
    return { label: "Running", tone: "progress" };
  }
  if (kind === "runtime_service" && status === "closed") {
    return { label: "Stopped", tone: "failure" };
  }
  return null;
}

function Chip({ chip }: { chip: StateChip }) {
  return (
    <span
      className="inline-flex shrink-0 items-center rounded-full px-2 py-1 text-2xs font-medium leading-none"
      style={{ border: `1px ${chip.dashed ? "dashed" : "solid"} ${TONE_VAR[chip.tone]}`, color: TONE_VAR[chip.tone] }}
    >
      {chip.label}
    </span>
  );
}

function stringMeta(metadata: Record<string, unknown> | null | undefined, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = metadata?.[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number") return String(value);
  }
  return null;
}

function numberMeta(metadata: Record<string, unknown> | null | undefined, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = metadata?.[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function urlLabel(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url, typeof window === "undefined" ? "http://localhost" : window.location.origin);
    return `${parsed.host}${parsed.pathname === "/" ? "" : parsed.pathname}`;
  } catch {
    return url;
  }
}

const LINK_CLASS = "inline-flex items-center gap-1 text-caption font-medium hover:underline";

/** Stock RichWorkProductCard, compact variant. */
export function WorkProductCard({ workProduct }: { workProduct: IssueWorkProduct }) {
  const actions = useAttachmentActions();
  const metadata = workProduct.metadata ?? null;
  const contentType = stringMeta(metadata, "contentType") ?? "";
  const isImage = isImageContentType(contentType);
  const isVideo = isVideoLikeOutput(contentType, stringMeta(metadata, "originalFilename"));
  let Icon: LucideIcon = File;
  let meta: Array<string | null> = [];
  let action = "Open preview";

  switch (workProduct.type) {
    case "pull_request": {
      Icon = GitPullRequest;
      const repository = stringMeta(metadata, "repository", "repo", "repositoryName");
      const number = stringMeta(metadata, "number", "pullRequestNumber");
      const base = stringMeta(metadata, "baseRef", "base", "baseBranch");
      const head = stringMeta(metadata, "headRef", "head", "headBranch", "branch");
      meta = [repository, number ? `#${number.replace(/^#/, "")}` : null, base && head ? `${base} ← ${head}` : null, urlLabel(workProduct.url)];
      action = "Open on GitHub";
      break;
    }
    case "commit":
      Icon = GitCommit;
      meta = [stringMeta(metadata, "shortSha", "sha")?.slice(0, 8) ?? workProduct.externalId?.slice(0, 8) ?? null, stringMeta(metadata, "branch", "branchName"), urlLabel(workProduct.url)];
      action = "Open on GitHub";
      break;
    case "branch":
      Icon = GitBranch;
      meta = [stringMeta(metadata, "repository", "repo", "repositoryName"), stringMeta(metadata, "branch", "branchName") ?? workProduct.externalId ?? null, urlLabel(workProduct.url)];
      action = "Open on GitHub";
      break;
    case "artifact": {
      Icon = isImage ? ImageIcon : isVideo ? Film : File;
      const size = numberMeta(metadata, "byteSize", "size");
      meta = [isImage ? "Image" : isVideo ? "Video" : stringMeta(metadata, "kind", "fileType") ?? "File", size === null ? null : formatBytes(size)];
      action = isImage || isVideo ? "Open" : "Download";
      break;
    }
    case "document":
      Icon = FileText;
      meta = ["Document", stringMeta(metadata, "revision", "revisionNumber") ? `rev ${stringMeta(metadata, "revision", "revisionNumber")}` : null];
      action = "Open document";
      break;
    case "preview_url":
      Icon = Globe;
      meta = [urlLabel(workProduct.url)];
      action = "Open preview";
      break;
    case "runtime_service":
      Icon = Server;
      meta = [stringMeta(metadata, "service", "serviceName") ?? workProduct.provider, stringMeta(metadata, "port") ? `port ${stringMeta(metadata, "port")}` : null];
      action = "Open service";
      break;
  }

  const unhealthyChip =
    workProduct.healthStatus === "unhealthy"
      ? workProduct.type === "preview_url"
        ? { label: "Down", tone: "failure" as const }
        : workProduct.type === "runtime_service" && workProduct.status !== "closed"
          ? { label: "Unhealthy", tone: "failure" as const }
          : null
      : null;
  const chip =
    unhealthyChip ??
    stateChipFor(
      workProduct.type,
      workProduct.type === "pull_request"
        ? stringMeta(metadata, "state") ?? workProduct.status
        : workProduct.type === "runtime_service" &&
          workProduct.status === "active" &&
          workProduct.healthStatus !== "healthy"
          ? null
          : workProduct.status,
      workProduct.reviewState,
    );
  const additions = numberMeta(metadata, "additions");
  const deletions = numberMeta(metadata, "deletions");
  const files = numberMeta(metadata, "files", "changedFiles");
  const visibleMeta = meta.filter((value): value is string => Boolean(value));
  const changeCounts = [additions === null ? null : `+${additions}`, deletions === null ? null : `−${deletions}`]
    .filter(Boolean)
    .join(" ");
  const fileCount = files === null ? null : `${files} ${files === 1 ? "file" : "files"}`;
  const statsLabel = [changeCounts || null, fileCount].filter(Boolean).join(" · ");

  // Attachment-backed artifacts carry `/api/attachments/…` paths that only the
  // BFF can serve; everything else is an external link, whitelisted by scheme.
  const href = workProductHref(workProduct);
  const attachmentHref = workProduct.type === "artifact"
    ? (isImage || isVideo ? href : stringMeta(metadata, "downloadPath") ?? href)
    : null;
  const isAttachment = Boolean(attachmentIdFromHref(attachmentHref));
  const externalHref = isAttachment ? undefined : safeHref(href);
  const name = stringMeta(metadata, "originalFilename") ?? workProduct.title;

  return (
    <article
      className="flex min-w-0 items-center gap-2 rounded-md px-2.5 py-1.5"
      style={{ border: "1px solid var(--edge)" }}
      data-testid={`task-chat-rich-work-product-${workProduct.type}`}
      data-variant="compact"
    >
      <div className="flex h-8 w-8 shrink-0 items-center justify-center overflow-hidden rounded-sm" style={{ background: "var(--cream-deep)", color: "var(--midnight-muted)" }}>
        <Icon aria-hidden size={16} />
      </div>
      <div className="min-w-0 flex-1">
        <strong className="block truncate text-ui font-medium" style={{ color: "var(--midnight)" }}>{workProduct.title}</strong>
        {visibleMeta.length > 0 ? <p className="mt-1 truncate text-caption" style={{ color: "var(--midnight-muted)" }}>{visibleMeta.join(" · ")}</p> : null}
        {statsLabel ? <p className="mt-1 whitespace-nowrap text-caption" style={{ color: "var(--midnight-muted)" }}>{statsLabel}</p> : null}
      </div>
      <div className="flex shrink-0 items-center gap-1.5">
        {chip ? <Chip chip={chip} /> : null}
        {isAttachment && attachmentHref ? (
          <button
            type="button"
            aria-label={`${action}: ${workProduct.title}`}
            className={LINK_CLASS}
            style={{ color: "var(--midnight)" }}
            onClick={() =>
              void (isImage || isVideo
                ? actions.open(attachmentHref, name, contentType)
                : actions.download(attachmentHref, name))
            }
          >
            {isImage || isVideo ? <ExternalLink aria-hidden size={12} /> : <Download aria-hidden size={12} />}
          </button>
        ) : externalHref ? (
          <a
            href={externalHref}
            aria-label={`${action}: ${workProduct.title}`}
            className={LINK_CLASS}
            style={{ color: "var(--midnight)" }}
            target="_blank"
            rel="noreferrer"
          >
            <ExternalLink aria-hidden size={12} />
          </a>
        ) : null}
      </div>
    </article>
  );
}

/** Lightbulb board-review controls for a `needs_board_review` work product. */
export function WorkProductReviewActions({
  onReview,
}: {
  onReview: (state: IssueWorkProductReviewState) => Promise<void>;
}) {
  const [busy, setBusy] = useState<IssueWorkProductReviewState | null>(null);
  const act = async (state: IssueWorkProductReviewState) => {
    setBusy(state);
    try {
      await onReview(state);
      // No reset on success — the list revalidates and the buttons drop away.
    } catch {
      setBusy(null);
    }
  };
  return (
    <div className="flex items-center gap-2 pl-[42px] pt-1.5">
      <button
        type="button"
        onClick={() => void act("approved")}
        disabled={busy !== null}
        className="rounded-sm px-2.5 py-1 text-caption font-medium transition-colors disabled:opacity-50"
        style={{ background: "var(--midnight)", color: "var(--cream)" }}
      >
        {busy === "approved" ? "Approving…" : "Approve"}
      </button>
      <button
        type="button"
        onClick={() => void act("changes_requested")}
        disabled={busy !== null}
        className="rounded-sm px-2.5 py-1 text-caption font-medium transition-colors disabled:opacity-50"
        style={{ border: "1px solid var(--edge)", color: "var(--midnight)" }}
      >
        {busy === "changes_requested" ? "Requesting…" : "Request changes"}
      </button>
    </div>
  );
}

/** Work-product status → label + tone for the row chip. */
function workProductStatusBadge(status: string): { label: string; color: string } | null {
  switch (status) {
    case "active":
    case "draft":
      return { label: "In progress", color: "var(--info)" };
    case "ready_for_review":
      return { label: "For review", color: "var(--warn)" };
    case "approved":
    case "merged":
      return { label: "Done", color: "var(--ok)" };
    case "changes_requested":
      return { label: "Changes requested", color: "var(--midnight-soft)" };
    case "failed":
      return { label: "Failed", color: "var(--err)" };
    default:
      return null;
  }
}

/**
 * Work-product row for an eligible Markdown artifact (LOOA-1533 gap): expands
 * in place into the shipped document review surface backed by the
 * server-materialized `artifact-review-<workProductId>` issue document instead
 * of opening the raw attachment. Raw open and download stay as explicit
 * secondary actions.
 */
export function MarkdownWorkProductRow({
  issueId,
  workProduct,
  metadata,
  reviewDoc,
}: {
  issueId: string;
  workProduct: IssueWorkProduct;
  metadata: AttachmentArtifactWorkProductMetadata;
  reviewDoc: IssueDocument | undefined;
}) {
  const { post, mutate } = useTeamsApi();
  const actions = useAttachmentActions();
  const [expanded, setExpanded] = useState(false);
  const [ensure, setEnsure] = useState<"idle" | "pending" | "done" | "unsupported" | "error">("idle");
  const badge = workProductStatusBadge(workProduct.status);
  const Chevron = expanded ? ChevronDown : ChevronRight;
  const tooLarge = metadata.byteSize > MARKDOWN_REVIEW_DOCUMENT_MAX_BYTES;
  const name = metadata.originalFilename || workProduct.title;

  const runEnsure = () => {
    setEnsure("pending");
    post(`/issues/${issueId}/work-products/${workProduct.id}/review-document`, {})
      .then(() => {
        setEnsure("done");
        void mutate(`/teams/issues/${issueId}/documents`);
      })
      .catch((e: { status?: number }) => {
        setEnsure([413, 415, 422].includes(e?.status ?? 0) ? "unsupported" : "error");
      });
  };

  const requestPreview = () => {
    if (reviewDoc || tooLarge || ensure !== "idle") return;
    runEnsure();
  };

  // Materialization is usually done server-side on the work-product write; the
  // POST is the retry path when the row expands and no review doc exists yet.
  useEffect(() => {
    if (expanded) requestPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded]);

  let expandedBody: React.ReactNode;
  if (tooLarge) {
    expandedBody = (
      <p className="text-ui" style={{ color: "var(--midnight-muted)" }}>
        This Markdown file is too large to preview. Use Raw or Download instead.
      </p>
    );
  } else if (reviewDoc) {
    expandedBody = (reviewDoc.body ?? "").trim().length > 0 ? (
      <MarkdownView>{reviewDoc.body ?? ""}</MarkdownView>
    ) : (
      <p className="text-ui" style={{ color: "var(--midnight-muted)" }}>Document is empty.</p>
    );
  } else if (ensure === "unsupported" || ensure === "error") {
    expandedBody = (
      <div className="flex flex-col items-start gap-1.5">
        <p className="text-ui" style={{ color: "var(--midnight-muted)" }}>
          {ensure === "unsupported"
            ? "This file can't be previewed as Markdown. Use Raw or Download instead."
            : "Preview failed to load."}
        </p>
        {ensure === "error" ? (
          <button
            type="button"
            className="rounded-md px-2 py-0.5 text-caption hover:bg-[var(--cream-deep)]"
            style={{ border: "1px solid var(--edge)", color: "var(--midnight-muted)" }}
            onClick={runEnsure}
          >
            Retry
          </button>
        ) : null}
      </div>
    );
  } else {
    expandedBody = <p className="text-ui" style={{ color: "var(--midnight-muted)" }}>Preparing preview…</p>;
  }

  return (
    <div className="rounded-md" style={{ border: "1px solid var(--edge)" }}>
      <div className="flex items-center hover:bg-[var(--cream-deep)]">
        <button
          type="button"
          onClick={() => setExpanded((open) => !open)}
          className="flex min-w-0 flex-1 items-center gap-2 px-2.5 py-1.5 text-left text-ui"
          aria-expanded={expanded}
        >
          <FileText size={14} className="shrink-0" style={{ color: "var(--midnight-muted)" }} />
          <span className="min-w-0 flex-1 truncate" style={{ color: "var(--midnight)" }}>{workProduct.title}</span>
          {badge ? (
            <span
              className="inline-flex shrink-0 items-center whitespace-nowrap rounded-full px-1.5 py-0.5 text-2xs leading-none"
              style={{ border: `1px solid ${badge.color}`, color: badge.color }}
            >
              {badge.label}
            </span>
          ) : null}
          {reviewDoc ? (
            <span className="shrink-0 text-caption" style={{ color: "var(--midnight-muted)" }}>
              {`Rev ${reviewDoc.latestRevisionNumber ?? 1}`}
            </span>
          ) : null}
          <Chevron size={14} className="shrink-0" style={{ color: "var(--midnight-muted)" }} />
        </button>
        <button
          type="button"
          onClick={() => void actions.open(metadata.openPath, name, metadata.contentType)}
          aria-label={`Open raw ${workProduct.title}`}
          title="Open raw"
          className="shrink-0 px-1.5 py-1.5"
          style={{ color: "var(--midnight-muted)" }}
        >
          <ExternalLink size={12} />
        </button>
        <button
          type="button"
          onClick={() => void actions.download(metadata.downloadPath, name)}
          aria-label={`Download ${workProduct.title}`}
          title="Download"
          className="shrink-0 py-1.5 pl-0.5 pr-2"
          style={{ color: "var(--midnight-muted)" }}
        >
          <Download size={12} />
        </button>
      </div>
      {expanded ? (
        <div className="px-2.5 py-2" style={{ borderTop: "1px solid var(--edge)" }}>{expandedBody}</div>
      ) : null}
    </div>
  );
}
