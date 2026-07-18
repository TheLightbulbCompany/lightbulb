"use client";

// apps/frontend/src/components/teams/panels/ArtifactsPanel.tsx
//
// The company Artifacts gallery — every deliverable an agent produced across
// the whole company, GROUPED BY TASK (ported shape from upstream Paperclip's
// pages/Artifacts.tsx + components/artifacts/ArtifactGroupCard.tsx, MIT (c)
// 2025 Paperclip AI). Each card is one issue's bundle of artifacts (documents +
// attachments + work-products); clicking opens that issue's detail panel, where
// the per-issue "Output" section (IssueWorkProductsSection) lives.
//
// v1 renders the grouped cards + preview artifacts, each of which is now an
// OPENABLE link into the owning issue's detail (at the artifact's own section
// anchor when present), with the group header kept as the overall issue-nav
// fallback.
// Deferred (follow-up): opening an artifact's CONTENT in place (image/video/
// attachment) needs the BFF `/teams/attachments/{id}/content` rewrite + CSP
// handling — the artifact's `openPath`/`href` are upstream-relative, so today
// we route to the issue detail (where IssueAttachments/Documents/Output already
// render the file) rather than to a raw path that 404s on our origin. Also
// deferred: inline media thumbnails, the flat/ungrouped view, kind facets, and
// cursor pagination — the BFF route already forwards all those params.

import { Images } from "lucide-react";
import Link from "next/link";
import { PanelMasthead } from "@/components/teams/shell";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { teamsDetailUrl } from "@/lib/shellNav";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type {
  CompanyArtifact,
  CompanyArtifactGroup,
  CompanyArtifactsResponse,
} from "@/components/teams/shared/types";

// A plain left-click routes in-shell when a nav provider is present; modified
// clicks / non-left buttons fall through to the anchor's native behavior. Shared
// by the group header and each preview artifact.
function inShellClick(
  nav: ReturnType<typeof useInlineTeamsNav>,
  issueId: string,
): (e: React.MouseEvent) => void {
  return (e: React.MouseEvent) => {
    if (!nav) return;
    if (
      e.defaultPrevented ||
      e.button !== 0 ||
      e.metaKey ||
      e.ctrlKey ||
      e.shiftKey ||
      e.altKey
    ) {
      return;
    }
    e.preventDefault();
    nav.openDetail("issue-detail", issueId);
  };
}

// The BFF-provided artifact `href` is upstream-relative (wrong origin/prefix for
// our shell), but its hash fragment names the artifact's own section anchor
// (`#document-<key>` / `#work-product-<id>` / `#attachment-<id>`). Reuse ONLY the
// fragment so a native new-tab open of our issue-detail URL scrolls to that
// section once the issue-detail anchors exist (A1/A3). Returns "" when absent.
function artifactAnchor(a: CompanyArtifact): string {
  const hash = a.href?.split("#")[1];
  return hash ? `#${hash}` : "";
}

// One preview artifact, individually openable. Deep-links into the owning
// issue's detail (carrying the artifact's own anchor); an artifact with no
// owning issue renders as inert text rather than a dead link.
function ArtifactPreviewItem({ artifact }: { artifact: CompanyArtifact }) {
  const nav = useInlineTeamsNav();
  const title = artifact.title?.trim();
  if (!title) return null;
  const issueId = artifact.issue?.id;
  if (!issueId) {
    return (
      <li className="truncate text-xs" style={{ color: "var(--ink-soft)" }}>
        · {title}
      </li>
    );
  }
  const url = `${teamsDetailUrl("issue", issueId)}${artifactAnchor(artifact)}`;
  return (
    <li>
      <Link
        href={url}
        onClick={inShellClick(nav, issueId)}
        className="block truncate rounded px-1 py-0.5 text-xs transition-colors hover:bg-[var(--cream-deeper)]"
        style={{ color: "var(--ink-soft)" }}
        title={title}
      >
        · {title}
      </Link>
    </li>
  );
}

function GroupCard({ group }: { group: CompanyArtifactGroup }) {
  const nav = useInlineTeamsNav();
  const url = teamsDetailUrl("issue", group.issue.id);

  const previewArtifacts = (group.previewArtifacts ?? []).filter(
    (a) => !!a.title && a.title.trim().length > 0,
  );

  return (
    <div
      className="editorial-card flex flex-col gap-2 p-4"
      style={{ borderColor: "var(--edge)" }}
    >
      {/* Group header — the overall issue-nav fallback. */}
      <Link
        href={url}
        onClick={inShellClick(nav, group.issue.id)}
        className="-mx-1 flex items-center gap-2 rounded px-1 py-0.5 transition-colors hover:bg-[var(--cream-deeper)]"
      >
        <span
          className="text-[10px] uppercase tracking-wide font-mono px-1.5 py-0.5 rounded flex-shrink-0"
          style={{ background: "var(--edge)", color: "var(--ink)" }}
        >
          {group.issue.identifier}
        </span>
        <span className="flex-1 truncate text-sm font-medium text-[var(--ink)]" title={group.title}>
          {group.title}
        </span>
        <span className="text-xs flex-shrink-0" style={{ color: "var(--mute)" }}>
          {group.count} {group.count === 1 ? "item" : "items"}
        </span>
      </Link>
      {previewArtifacts.length > 0 ? (
        <ul className="flex flex-col gap-0.5 pl-1">
          {previewArtifacts.slice(0, 4).map((a) => (
            <ArtifactPreviewItem key={a.id} artifact={a} />
          ))}
        </ul>
      ) : null}
      <span className="text-xs" style={{ color: "var(--mute)" }}>
        Updated {timeAgo(group.updatedAt)}
      </span>
    </div>
  );
}

export function ArtifactsPanel() {
  const { read } = useTeamsApi();
  // groupBy=task clusters deliverables under the issue that produced them —
  // the ported grouped shape, not a flat firehose.
  const { data, error, isLoading } = read<CompanyArtifactsResponse>("/artifacts?groupBy=task");
  const groups = data?.groups ?? [];

  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-4xl mx-auto w-full">
      <PanelMasthead overline="Company" title="Artifacts" />

      <section aria-label="Artifacts gallery" className="min-w-0">
        {isLoading && groups.length === 0 ? (
          <p className="font-editorial-serif text-base italic" style={{ color: "var(--mute)" }}>
            Loading artifacts…
          </p>
        ) : error && groups.length === 0 ? (
          <p
            role="alert"
            className="font-editorial-serif text-base italic"
            style={{ color: "var(--warn)" }}
          >
            Failed to load artifacts.
          </p>
        ) : groups.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-12 text-center">
            <Images size={28} style={{ color: "var(--mute)" }} />
            <p className="font-editorial-serif text-base italic" style={{ color: "var(--mute)" }}>
              No artifacts yet.
            </p>
            <p className="text-xs" style={{ color: "var(--mute)" }}>
              Deliverables your agents produce show up here.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {groups.map((group) => (
              <GroupCard key={group.id} group={group} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
