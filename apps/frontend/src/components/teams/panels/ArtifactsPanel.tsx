"use client";

// apps/frontend/src/components/teams/panels/ArtifactsPanel.tsx
//
// The company Artifacts gallery — every deliverable an agent produced across
// the whole company, rendered as a VISUAL CARD GALLERY (structure + look ported
// from upstream Paperclip's pages/Artifacts.tsx + components/artifacts/*, MIT
// (c) 2025 Paperclip AI), adapted to our editorial styling and our in-app
// viewer.
//
// Two views, held in component state (upstream drives them via URL params):
//   1. STACK LIST (default) — `GET /artifacts?groupBy=task` → a grid of
//      ArtifactStackCards, one per task. Clicking a stack EXPANDS it inline.
//   2. EXPANDED STACK — `GET /artifacts?groupBy=task&groupIssueId={id}` → that
//      task's artifacts as ArtifactGalleryCards, with a "← All artifacts" back
//      control. Clicking a card opens the in-app ArtifactViewerDialog.
//
// Capabilities preserved from the previous list impl (do not regress):
//   - VIEW   — clicking an artifact card opens ArtifactViewerDialog, which
//     fetches + renders the artifact's CONTENT in place (routed by its typed id
//     prefix; see artifactRouting.ts).
//   - DELETE — an artifact card's trash icon (and the viewer's Delete button)
//     runs a confirm-then-delete flow against the correct BFF DELETE endpoint,
//     then revalidates BOTH the gallery and the issue-scoped SWR cache. A failed
//     delete keeps the confirm dialog open with an error rather than closing.
//   - SORT   — a Newest/Oldest control sorts the current view client-side.
//
// Deferred (follow-up): the flat/ungrouped view (`groupBy=none`), kind facets,
// cursor pagination, and lazy image-thumbnail loading — the BFF route already
// forwards those params.

import { useState } from "react";
import { useSWRConfig } from "swr";
import { ArrowLeft, Images } from "lucide-react";
import { PanelMasthead } from "@/components/teams/shell";
import { EditorialDialog } from "@/components/teams/shell/EditorialDialog";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { ArtifactViewerDialog } from "./ArtifactViewerDialog";
import { ArtifactGalleryCard, ArtifactStackCard } from "./ArtifactCards";
import {
  artifactContentPath,
  deletePathForArtifact,
  issueListPathForArtifact,
  sortArtifactsByUpdated,
  sortGroupsByUpdated,
  type ArtifactSort,
} from "./artifactRouting";
import type {
  CompanyArtifact,
  CompanyArtifactsResponse,
} from "@/components/teams/shared/types";

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
            className="rounded-sm px-2 py-1 transition-colors"
            style={
              active
                ? { background: "var(--midnight)", color: "var(--cream)" }
                : { color: "var(--midnight-muted)" }
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
  const { read, del, download } = useTeamsApi();
  // The gallery and the issue-detail sections keep SEPARATE SWR caches; deleting
  // here must also invalidate the issue-scoped list (documents/attachments/
  // work-products) or the removed artifact lingers in the task detail. Each
  // view's own gallery key is refreshed via its bound `mutate`.
  const { mutate: mutateCache } = useSWRConfig();

  // View state: null = the stack list; an issue id = that expanded stack. Upstream
  // holds this in a URL param (`groupIssueId`); we keep it in component state.
  const [expandedIssueId, setExpandedIssueId] = useState<string | null>(null);
  const [sort, setSort] = useState<ArtifactSort>("newest");
  const [viewing, setViewing] = useState<CompanyArtifact | null>(null);
  const [pendingDelete, setPendingDelete] = useState<CompanyArtifact | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // Stack list — groupBy=task clusters deliverables under the issue that produced
  // them (the ported grouped shape, not a flat firehose).
  const stackList = read<CompanyArtifactsResponse>("/artifacts?groupBy=task");
  // Expanded stack — a conditional (null-keyed) read until a stack is opened.
  const expanded = read<CompanyArtifactsResponse>(
    expandedIssueId
      ? `/artifacts?groupBy=task&groupIssueId=${encodeURIComponent(expandedIssueId)}`
      : null,
  );

  const groups = stackList.data?.groups ?? [];
  const sortedGroups = sortGroupsByUpdated(groups, sort);

  const expandedArtifacts = expanded.data?.artifacts ?? [];
  const sortedArtifacts = sortArtifactsByUpdated(expandedArtifacts, sort);
  const selectedGroup =
    expanded.data?.selectedGroup ?? groups.find((g) => g.issue?.id === expandedIssueId) ?? null;

  const isExpanded = expandedIssueId !== null;
  const activeLoading = isExpanded ? expanded.isLoading : stackList.isLoading;
  const activeError = isExpanded ? expanded.error : stackList.error;
  const hasItems = isExpanded ? expandedArtifacts.length > 0 : groups.length > 0;

  async function handleDownload(artifact: CompanyArtifact) {
    const path = artifactContentPath(artifact);
    if (!path) return;
    try {
      const blob = await download(path);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = artifact.title || "download";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch {
      // Best-effort — a download hiccup should not wedge the gallery.
    }
  }

  async function confirmDelete() {
    if (!pendingDelete) return;
    const artifact = pendingDelete;
    const path = deletePathForArtifact(artifact);
    if (!path) {
      setPendingDelete(null);
      return;
    }
    setDeleting(true);
    setDeleteError(null);
    try {
      await del(path);
    } catch {
      // The destructive op failed (offline / 403 / 5xx). Keep the dialog open
      // with its retry context and surface the failure, rather than silently
      // closing as if the delete had succeeded.
      setDeleteError("Couldn’t delete this artifact. Please try again.");
      setDeleting(false);
      return;
    }
    // Deleted. Close the confirm + the viewer if it was showing this artifact,
    // then best-effort revalidate BOTH gallery keys AND the issue-scoped list
    // that also holds it (separate SWR caches) so the task detail stops showing
    // it. A revalidation hiccup must not reopen the dialog — the delete
    // succeeded.
    setViewing((cur) => (cur && cur.id === artifact.id ? null : cur));
    setPendingDelete(null);
    setDeleting(false);
    const issueListPath = issueListPathForArtifact(artifact);
    void Promise.all([
      stackList.mutate(),
      expandedIssueId ? expanded.mutate() : Promise.resolve(undefined),
      issueListPath ? mutateCache(`/teams${issueListPath}`) : Promise.resolve(undefined),
    ]).catch(() => {});
  }

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 max-w-4xl mx-auto w-full">
      <div className="flex items-center justify-between gap-3">
        <PanelMasthead overline="Company" title="Library" />
        {hasItems ? <SortControl sort={sort} onChange={setSort} /> : null}
      </div>

      {isExpanded ? (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <button
            type="button"
            onClick={() => setExpandedIssueId(null)}
            data-testid="artifact-stack-back"
            className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition-colors hover:bg-[var(--cream-deep)]"
            style={{ color: "var(--midnight-muted)" }}
          >
            <ArrowLeft size={16} aria-hidden="true" />
            All artifacts
          </button>
          {selectedGroup ? (
            <span className="truncate" style={{ color: "var(--midnight-muted)" }}>
              <span className="font-mono" style={{ color: "var(--midnight)" }}>
                {selectedGroup.issue?.identifier}
              </span>{" "}
              {selectedGroup.title}
            </span>
          ) : null}
        </div>
      ) : null}

      <section aria-label="Artifacts gallery" className="min-w-0">
        {activeLoading && !hasItems ? (
          <p className="font-editorial-serif text-base italic" style={{ color: "var(--midnight-muted)" }}>
            Loading artifacts…
          </p>
        ) : activeError && !hasItems ? (
          <p
            role="alert"
            className="font-editorial-serif text-base italic"
            style={{ color: "var(--err)" }}
          >
            Failed to load artifacts.
          </p>
        ) : !hasItems ? (
          <div className="flex flex-col items-center gap-2 py-12 text-center">
            <Images size={20} style={{ color: "var(--midnight-muted)" }} />
            <p className="font-editorial-serif text-base italic" style={{ color: "var(--midnight-muted)" }}>
              {isExpanded ? "No artifacts in this task." : "No artifacts yet."}
            </p>
            {!isExpanded ? (
              <p className="text-xs" style={{ color: "var(--midnight-muted)" }}>
                Deliverables your agents produce show up here.
              </p>
            ) : null}
          </div>
        ) : isExpanded ? (
          <div className="grid grid-cols-1 gap-3 @xl:grid-cols-2 @4xl:grid-cols-3">
            {sortedArtifacts.map((artifact) => (
              <ArtifactGalleryCard
                key={artifact.id}
                artifact={artifact}
                onOpen={setViewing}
                onDownload={handleDownload}
                onRequestDelete={setPendingDelete}
              />
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 @xl:grid-cols-2 @4xl:grid-cols-3">
            {sortedGroups.map((group) => (
              <ArtifactStackCard
                key={group.id}
                group={group}
                onOpenStack={(issueId) => setExpandedIssueId(issueId || group.issue?.id || null)}
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
          if (!next && !deleting) {
            setPendingDelete(null);
            setDeleteError(null);
          }
        }}
        title="Delete this artifact?"
        description="This can’t be undone."
        footer={
          <>
            <button
              type="button"
              onClick={() => {
                setPendingDelete(null);
                setDeleteError(null);
              }}
              disabled={deleting}
              className="rounded-md px-3 py-1.5 text-sm disabled:opacity-50"
              style={{ background: "var(--edge)", color: "var(--midnight)" }}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={confirmDelete}
              disabled={deleting}
              className="rounded-md bg-destructive px-3 py-1.5 text-sm text-destructive-foreground disabled:opacity-50"
            >
              {deleting ? "Deleting…" : "Delete"}
            </button>
          </>
        }
      >
        {deleteError ? (
          <p role="alert" className="text-sm" style={{ color: "var(--err)" }}>
            {deleteError}
          </p>
        ) : null}
      </EditorialDialog>
    </div>
  );
}
