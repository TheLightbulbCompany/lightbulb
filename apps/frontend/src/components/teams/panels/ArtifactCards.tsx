"use client";

// apps/frontend/src/components/teams/panels/ArtifactCards.tsx
//
// Visual cards for the company Artifacts gallery. STRUCTURE + LOOK are ported
// from upstream Paperclip's components/artifacts/ArtifactCard.tsx +
// ArtifactGroupCard.tsx (MIT (c) 2025 Paperclip AI) — a fixed-height preview
// region on top of each card, then title / issue identifier / "Updated …" —
// but adapted to our editorial styling (editorial-card + var(--*) tokens,
// lucide icons) and to our IN-APP viewer model:
//
//   - ArtifactStackCard  — one task's bundle. The whole card EXPANDS the stack
//     inline (upstream deep-links; we keep in-component view state). A count
//     badge sits over the preview.
//   - ArtifactGalleryCard — one artifact. The whole card opens our
//     ArtifactViewerDialog (upstream has no viewer and links to the raw file).
//     Secondary hover icons (open-in-issue / download / delete) each
//     stopPropagation so they never trip the card's open-viewer surface.
//
// The preview region is the load-bearing port: a fixed aspect box that keeps
// image / text / placeholder cards from shifting layout as previews load. Image
// bytes are fetched through the BFF content route (Clerk bearer) into an object
// URL — the same pattern ArtifactViewerDialog's ContentBody uses, including
// the mount-ref stability fix — because a bare <img src> to the upstream path
// can't authenticate.

import { createElement, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  Download,
  ExternalLink,
  FileText,
  Film,
  Image as ImageIcon,
  Layers,
  Paperclip,
  Trash2,
} from "lucide-react";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsDetailUrl } from "@/lib/shellNav";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { artifactContentPath } from "./artifactRouting";
import type {
  CompanyArtifact,
  CompanyArtifactGroup,
} from "@/components/teams/shared/types";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

// Fixed-height, rounded preview frame shared by every card variant. The fixed
// aspect ratio is what keeps image / text / placeholder previews from shifting
// layout as they load (or fail to load).
function PreviewFrame({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="relative flex aspect-[16/10] w-full items-center justify-center overflow-hidden"
      style={{ background: "var(--cream-deep)", borderBottom: "1px solid var(--edge)" }}
    >
      {children}
    </div>
  );
}

// Media-kind → placeholder icon. Used when there is no image thumbnail and no
// preview text to show.
function iconForMediaKind(mediaKind: string) {
  switch (mediaKind) {
    case "image":
      return ImageIcon;
    case "video":
      return Film;
    case "document":
    case "text":
    case "file":
      return FileText;
    default:
      return Paperclip;
  }
}

function IconPreview({ mediaKind }: { mediaKind: string }) {
  // createElement (not `<Icon />`) so eslint's static-components rule doesn't
  // read the capitalized local as a component declared during render. The
  // data-testid lives on the wrapper (lucide's typed props reject data-*).
  return (
    <PreviewFrame>
      <span data-testid="artifact-preview-icon" style={{ color: "var(--midnight-muted)" }}>
        {createElement(iconForMediaKind(mediaKind), {
          className: "h-8 w-8",
          "aria-hidden": true,
        })}
      </span>
    </PreviewFrame>
  );
}

function TextPreview({ text }: { text: string }) {
  return (
    <PreviewFrame>
      <div className="absolute inset-0 overflow-hidden p-3">
        <p
          data-testid="artifact-preview-text"
          className="max-h-full overflow-hidden whitespace-pre-wrap break-words text-caption leading-5"
          style={{ color: "var(--midnight-soft)" }}
        >
          {text}
        </p>
      </div>
      <div
        aria-hidden="true"
        className="pointer-events-none absolute bottom-0 left-0 right-0 h-8"
        style={{ background: "linear-gradient(to top, var(--cream-deep), transparent)" }}
      />
    </PreviewFrame>
  );
}

// Authenticated image thumbnail. Fetches bytes via the BFF content route into an
// object URL. Keyed per-artifact at the call site so it remounts (fresh
// "loading") when the previewed artifact changes — which is why the effect
// captures `download` once at mount (see ContentBody for the loop this
// mount-ref avoids) and depends only on `path`.
function ImagePreview({ path, alt }: { path: string; alt: string }) {
  const { download } = useTeamsApi();
  const downloadRef = useRef(download);
  const [url, setUrl] = useState<string | null>(null);
  const [errored, setErrored] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | undefined;
    downloadRef
      .current(path)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!cancelled) setErrored(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path]);

  if (errored) return <IconPreview mediaKind="image" />;
  if (!url) {
    return (
      <PreviewFrame>
        <ImageIcon
          className="h-8 w-8 animate-pulse"
          style={{ color: "var(--midnight-muted)" }}
          aria-hidden="true"
        />
      </PreviewFrame>
    );
  }
  return (
    <PreviewFrame>
      {/* A blob: object URL, not a remote asset the Next image loader can
          optimize — plain <img> is correct here. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={url} alt={alt} className="h-full w-full object-cover" />
    </PreviewFrame>
  );
}

// The preview region for a single artifact: image thumbnail, else a clamped
// preview-text box, else a centered media-kind icon.
export function ArtifactPreview({ artifact }: { artifact: CompanyArtifact }) {
  const contentPath = artifactContentPath(artifact);
  if (artifact.mediaKind === "image" && contentPath) {
    return <ImagePreview key={artifact.id} path={contentPath} alt={artifact.title} />;
  }
  const preview = artifact.previewText?.trim();
  if (preview) return <TextPreview text={preview} />;
  return <IconPreview mediaKind={String(artifact.mediaKind)} />;
}

// A small hover-revealed secondary action rendered over the preview. Each stops
// propagation so it never trips the card's own open-viewer / expand handler.
function SecondaryButton({
  onClick,
  label,
  children,
}: {
  onClick: (e: React.MouseEvent) => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        onClick(e);
      }}
      aria-label={label}
      title={label}
      className="flex h-7 w-7 items-center justify-center rounded-md backdrop-blur transition-colors"
      style={{ background: "var(--cream)", color: "var(--midnight-soft)", border: "1px solid var(--edge)" }}
    >
      {children}
    </button>
  );
}

export interface ArtifactGalleryCardProps {
  artifact: CompanyArtifact;
  onOpen: (a: CompanyArtifact) => void;
  onDownload: (a: CompanyArtifact) => void;
  onRequestDelete: (a: CompanyArtifact) => void;
}

// One artifact: the whole card opens the in-app viewer; the top-right hover
// icons are secondary affordances (open-in-issue / download / delete). An
// artifact with no title renders nothing.
export function ArtifactGalleryCard({
  artifact,
  onOpen,
  onDownload,
  onRequestDelete,
}: ArtifactGalleryCardProps) {
  const nav = useInlineTeamsNav();
  const title = artifact.title?.trim();
  if (!title) return null;

  const issueId = artifact.issue?.id ?? "";
  const issueUrl = issueId ? teamsDetailUrl("issue", issueId) : "#";
  const canDownload = !!artifact.downloadPath && !!artifactContentPath(artifact);

  // Anchor-based so a middle-click / standalone (no nav provider) still works;
  // stopPropagation keeps the card's open-viewer handler from also firing.
  const openIssue = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!nav || !issueId) return;
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) {
      return;
    }
    e.preventDefault();
    nav.openDetail("issue-detail", issueId);
  };

  return (
    <div
      role="button"
      tabIndex={0}
      data-testid="artifact-card"
      data-media-kind={artifact.mediaKind}
      onClick={() => onOpen(artifact)}
      onKeyDown={(e) => {
        // Only the card itself opens the viewer on Enter/Space. Enter/Space on a
        // focused overlay action (Open-in-issue / Download / Delete) bubbles its
        // keydown up to the card, so ignore events originating from a descendant.
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen(artifact);
        }
      }}
      className="editorial-card editorial-card-hover group relative flex cursor-pointer flex-col overflow-hidden text-left"
      style={{ borderColor: "var(--edge)" }}
    >
      <ArtifactPreview artifact={artifact} />

      {/* Hidden until hover/focus. pointer-events gated the same way so an
          invisible icon can't intercept a tap meant to open the viewer on
          touch (no hover) devices. */}
      <div className="pointer-events-none absolute right-2 top-2 flex gap-1 opacity-0 transition-opacity duration-(--dur-fast) ease-out group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100">
        {issueId ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <Link
                href={issueUrl}
                onClick={openIssue}
                aria-label="Open in issue"
                className="flex h-7 w-7 items-center justify-center rounded-md backdrop-blur transition-colors"
                style={{ background: "var(--cream)", color: "var(--midnight-soft)", border: "1px solid var(--edge)" }}
              >
                <ExternalLink size={16} />
              </Link>
            </TooltipTrigger>
            <TooltipContent>Open in issue</TooltipContent>
          </Tooltip>
        ) : null}
        {canDownload ? (
          <SecondaryButton onClick={() => onDownload(artifact)} label={`Download ${title}`}>
            <Download size={16} />
          </SecondaryButton>
        ) : null}
        <SecondaryButton onClick={() => onRequestDelete(artifact)} label={`Delete ${title}`}>
          <Trash2 size={16} />
        </SecondaryButton>
      </div>

      <div className="flex flex-1 flex-col gap-1 p-3">
        <h3
          className="truncate text-ui font-medium leading-5"
          style={{ color: "var(--midnight)" }}
          title={title}
        >
          {title}
        </h3>
        <div className="flex items-center gap-1.5 text-caption" style={{ color: "var(--midnight-muted)" }}>
          {artifact.issue?.identifier ? (
            <span className="font-mono">{artifact.issue.identifier}</span>
          ) : null}
          <span>·</span>
          <span>Updated {timeAgo(artifact.updatedAt)}</span>
        </div>
        {artifact.createdByAgent ? (
          <span className="truncate text-caption" style={{ color: "var(--midnight-muted)" }}>
            {artifact.createdByAgent.name}
          </span>
        ) : null}
      </div>
    </div>
  );
}

export interface ArtifactStackCardProps {
  group: CompanyArtifactGroup;
  onOpenStack: (issueId: string) => void;
}

// One task's bundle. The whole card expands the stack inline. A count badge
// sits over the preview; the top preview comes from the first previewArtifact.
export function ArtifactStackCard({ group, onOpenStack }: ArtifactStackCardProps) {
  const preview = (group.previewArtifacts ?? []).find((a) => a.title?.trim());
  const issueId = group.issue?.id ?? "";

  return (
    <div
      role="button"
      tabIndex={0}
      data-testid="artifact-stack-card"
      data-group-id={group.id}
      data-count={group.count}
      onClick={() => onOpenStack(issueId)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpenStack(issueId);
        }
      }}
      className="editorial-card editorial-card-hover group relative flex cursor-pointer flex-col overflow-hidden text-left"
      style={{ borderColor: "var(--edge)" }}
    >
      {preview ? (
        <ArtifactPreview artifact={preview} />
      ) : (
        <PreviewFrame>
          <Layers size={20} style={{ color: "var(--midnight-muted)" }} aria-hidden="true" />
        </PreviewFrame>
      )}
      <span
        data-testid="artifact-stack-count"
        className="absolute right-2 top-2 inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-caption font-medium backdrop-blur"
        style={{ background: "var(--cream)", color: "var(--midnight)", border: "1px solid var(--edge)" }}
      >
        <Layers size={12} strokeWidth={1.75} aria-hidden="true" />
        {group.count}
      </span>

      <div className="flex flex-1 flex-col gap-1 p-3">
        <div className="flex items-center gap-2">
          {group.issue?.identifier ? (
            <span className="shrink-0 font-mono text-caption" style={{ color: "var(--midnight-muted)" }}>
              {group.issue.identifier}
            </span>
          ) : null}
          <h3
            className="min-w-0 flex-1 truncate text-ui font-medium leading-5"
            style={{ color: "var(--midnight)" }}
            title={group.title}
          >
            {group.title}
          </h3>
        </div>
        <span className="text-caption" style={{ color: "var(--midnight-muted)" }}>
          {group.count} {group.count === 1 ? "item" : "items"} · Updated {timeAgo(group.updatedAt)}
        </span>
      </div>
    </div>
  );
}
