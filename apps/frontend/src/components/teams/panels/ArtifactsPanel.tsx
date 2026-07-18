"use client";

// apps/frontend/src/components/teams/panels/ArtifactsPanel.tsx
//
// The company Artifacts gallery — every deliverable an agent produced across
// the whole company, GROUPED BY TASK (ported shape from upstream Paperclip's
// pages/Artifacts.tsx + components/artifacts/ArtifactGroupCard.tsx, MIT (c)
// 2025 Paperclip AI). Each card is one issue's bundle of artifacts (documents +
// attachments + work-products).
//
// Three capabilities live here:
//   1. VIEW — clicking an artifact row opens ArtifactViewerDialog, which fetches
//      and renders the artifact's CONTENT in place (routed by its typed id
//      prefix; see artifactRouting.ts). The group header stays a secondary
//      "open task" affordance into the issue detail.
//   2. DELETE — each row has a trash button that runs a confirm-then-delete
//      flow against the correct BFF DELETE endpoint, then revalidates the
//      gallery via SWR mutate.
//   3. SORT — a Newest/Oldest control at the top, sorting groups client-side by
//      each group's `updatedAt` (default Newest).
//
// Deferred (follow-up): inline media thumbnails on the cards themselves, the
// flat/ungrouped view, kind facets, and cursor pagination — the BFF route
// already forwards all those params.

import { useState } from "react";
import { useSWRConfig } from "swr";
import { Images, Trash2 } from "lucide-react";
import Link from "next/link";
import { PanelMasthead } from "@/components/teams/shell";
import { EditorialDialog } from "@/components/teams/shell/EditorialDialog";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { teamsDetailUrl } from "@/lib/shellNav";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { ArtifactViewerDialog } from "./ArtifactViewerDialog";
import {
  deletePathForArtifact,
  issueListPathForArtifact,
  sortGroupsByUpdated,
  type ArtifactSort,
} from "./artifactRouting";
import type {
  CompanyArtifact,
  CompanyArtifactGroup,
  CompanyArtifactsResponse,
} from "@/components/teams/shared/types";

// A plain left-click routes in-shell when a nav provider is present; modified
// clicks / non-left buttons fall through to the anchor's native behavior.
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

// One artifact row: the title opens the content viewer; the trash button starts
// the confirm-then-delete flow. An artifact with no title renders nothing.
function ArtifactRow({
  artifact,
  onOpen,
  onRequestDelete,
}: {
  artifact: CompanyArtifact;
  onOpen: (a: CompanyArtifact) => void;
  onRequestDelete: (a: CompanyArtifact) => void;
}) {
  const title = artifact.title?.trim();
  if (!title) return null;
  return (
    <li className="flex items-center gap-1">
      <button
        type="button"
        onClick={() => onOpen(artifact)}
        className="min-w-0 flex-1 truncate rounded px-1 py-0.5 text-left text-xs transition-colors hover:bg-[var(--cream-deeper)]"
        style={{ color: "var(--ink-soft)" }}
        title={title}
      >
        · {title}
      </button>
      <button
        type="button"
        onClick={() => onRequestDelete(artifact)}
        aria-label={`Delete ${title}`}
        className="flex-shrink-0 rounded p-1 transition-colors hover:bg-[var(--cream-deeper)]"
        style={{ color: "var(--mute)" }}
      >
        <Trash2 className="h-3.5 w-3.5" />
      </button>
    </li>
  );
}

function GroupCard({
  group,
  onOpen,
  onRequestDelete,
}: {
  group: CompanyArtifactGroup;
  onOpen: (a: CompanyArtifact) => void;
  onRequestDelete: (a: CompanyArtifact) => void;
}) {
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
      {/* Group header — the secondary "open task" affordance into the issue. */}
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
            <ArtifactRow
              key={a.id}
              artifact={a}
              onOpen={onOpen}
              onRequestDelete={onRequestDelete}
            />
          ))}
        </ul>
      ) : null}
      <span className="text-xs" style={{ color: "var(--mute)" }}>
        Updated {timeAgo(group.updatedAt)}
      </span>
    </div>
  );
}

function SortControl({
  sort,
  onChange,
}: {
  sort: ArtifactSort;
  onChange: (s: ArtifactSort) => void;
}) {
  const opts: { value: ArtifactSort; label: string }[] = [
    { value: "newest", label: "Newest" },
    { value: "oldest", label: "Oldest" },
  ];
  return (
    <div
      className="flex items-center gap-1 rounded-md border p-0.5 text-xs"
      style={{ borderColor: "var(--edge)" }}
      role="group"
      aria-label="Sort artifacts"
    >
      {opts.map((o) => {
        const active = sort === o.value;
        return (
          <button
            key={o.value}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(o.value)}
            className="rounded px-2 py-1 transition-colors"
            style={
              active
                ? { background: "var(--ink)", color: "var(--cream)" }
                : { color: "var(--mute)" }
            }
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function ArtifactsPanel() {
  const { read, del } = useTeamsApi();
  // The gallery and the issue-detail sections keep SEPARATE SWR caches; deleting
  // here must also invalidate the issue-scoped list (documents/attachments/
  // work-products) or the removed artifact lingers in the task detail. The
  // gallery's own key is refreshed via the bound `mutate` below.
  const { mutate: mutateCache } = useSWRConfig();
  // groupBy=task clusters deliverables under the issue that produced them —
  // the ported grouped shape, not a flat firehose.
  const { data, error, isLoading, mutate } =
    read<CompanyArtifactsResponse>("/artifacts?groupBy=task");
  const groups = data?.groups ?? [];

  const [sort, setSort] = useState<ArtifactSort>("newest");
  const [viewing, setViewing] = useState<CompanyArtifact | null>(null);
  const [pendingDelete, setPendingDelete] = useState<CompanyArtifact | null>(null);
  const [deleting, setDeleting] = useState(false);

  const sortedGroups = sortGroupsByUpdated(groups, sort);

  async function confirmDelete() {
    if (!pendingDelete) return;
    const path = deletePathForArtifact(pendingDelete);
    if (!path) {
      setPendingDelete(null);
      return;
    }
    setDeleting(true);
    try {
      await del(path);
      // If the viewer is showing the artifact we just deleted, close it.
      setViewing((cur) => (cur && cur.id === pendingDelete.id ? null : cur));
      // Refresh the gallery AND the issue-scoped list that also holds this
      // artifact (routed by the deleted artifact's kind) — separate SWR caches,
      // so both must be revalidated or the task detail keeps showing it.
      const issueListPath = issueListPathForArtifact(pendingDelete);
      await Promise.all([
        mutate(),
        issueListPath
          ? mutateCache(`/teams${issueListPath}`)
          : Promise.resolve(undefined),
      ]);
    } finally {
      setDeleting(false);
      setPendingDelete(null);
    }
  }

  return (
    <div className="editorial-page p-4 sm:p-6 lg:p-8 flex flex-col gap-6 max-w-4xl mx-auto w-full">
      <div className="flex items-center justify-between gap-3">
        <PanelMasthead overline="Company" title="Artifacts" />
        {groups.length > 0 ? <SortControl sort={sort} onChange={setSort} /> : null}
      </div>

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
            {sortedGroups.map((group) => (
              <GroupCard
                key={group.id}
                group={group}
                onOpen={setViewing}
                onRequestDelete={setPendingDelete}
              />
            ))}
          </div>
        )}
      </section>

      {viewing ? (
        <ArtifactViewerDialog
          artifact={viewing}
          onClose={() => setViewing(null)}
          onRequestDelete={setPendingDelete}
        />
      ) : null}

      <EditorialDialog
        open={pendingDelete !== null}
        onOpenChange={(next) => {
          if (!next && !deleting) setPendingDelete(null);
        }}
        title="Delete this artifact?"
        description="This can’t be undone."
        footer={
          <>
            <button
              type="button"
              onClick={() => setPendingDelete(null)}
              disabled={deleting}
              className="rounded px-3 py-1.5 text-sm disabled:opacity-50"
              style={{ background: "var(--edge)", color: "var(--ink)" }}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={confirmDelete}
              disabled={deleting}
              className="rounded px-3 py-1.5 text-sm disabled:opacity-50"
              style={{ background: "var(--warn)", color: "var(--warn-ink, #fff)" }}
            >
              {deleting ? "Deleting…" : "Delete"}
            </button>
          </>
        }
      />
    </div>
  );
}
