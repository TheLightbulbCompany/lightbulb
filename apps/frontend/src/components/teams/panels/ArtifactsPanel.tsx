"use client";

// apps/frontend/src/components/teams/panels/ArtifactsPanel.tsx
//
// The company Artifacts gallery — every deliverable an agent produced across
// the whole company, rendered as a VISUAL CARD GALLERY (structure + look ported
// from upstream Paperclip's pages/Artifacts.tsx + components/artifacts/*, MIT
// (c) 2025 Paperclip AI), adapted to our editorial styling and our in-app
// viewer.
//
// Three views, held in component state (upstream drives them via URL params):
//   1. RECENT (default) — `GET /artifacts?groupBy=none&limit=100` → the flat
//      newest-first feed, bucketed by date. It answers the owner's actual
//      question ("what did my agents make this week?") in one glance.
//   2. BY TASK — `GET /artifacts?groupBy=task` → a grid of ArtifactStackCards,
//      one per task. Clicking a stack EXPANDS it inline.
//   3. EXPANDED STACK — `GET /artifacts?groupBy=task&groupIssueId={id}` → that
//      task's artifacts as ArtifactGalleryCards, with a back control. Clicking
//      a card opens the in-app ArtifactViewerDialog.
//
// Capabilities preserved from the previous list impl (do not regress):
//   - VIEW   — clicking an artifact card opens ArtifactViewerDialog, which
//     fetches + renders the artifact's CONTENT in place (routed by its typed id
//     prefix; see artifactRouting.ts).
//   - DELETE — an artifact card's trash icon (and the viewer's Delete button)
//     runs a confirm-then-delete flow against the correct BFF DELETE endpoint,
//     then revalidates EVERY gallery key (one key filter over the global SWR
//     cache, so the view the owner is NOT on — and any other search — refreshes
//     too) and the issue-scoped SWR cache. A failed delete keeps the confirm
//     dialog open with an error rather than closing.
//   - SORT   — a Newest/Oldest control sorts the current view client-side.
//   - SEARCH — one debounced box, suffixed as `&q=` onto whichever read is
//     active. It is SERVER-side on purpose: upstream matches a document's BODY,
//     not just its title, so a client filter over the loaded page would answer a
//     narrower question than the owner asked.
//
// Deferred (follow-up): kind facets, cursor pagination, and lazy image-thumbnail
// loading — the BFF route already forwards those params.

import { useEffect, useState } from "react";
import { ArrowLeft, Images } from "lucide-react";
import { PanelMasthead, PanelNotice, PanelTabs, PanelToolbar } from "@/components/teams/shell";
import { EditorialDialog } from "@/components/teams/shell/EditorialDialog";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { ArtifactViewerDialog } from "./ArtifactViewerDialog";
import { ArtifactGalleryCard, ArtifactStackCard } from "./ArtifactCards";
import {
  artifactContentPath,
  bucketByRecency,
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
import { notifyApiError } from "@/lib/notify";

/** Which list the panel is showing. `recent` is the default — a flat,
 * date-bucketed feed; `by-task` is the ported stack view. */
export type LibraryView = "recent" | "by-task";

const RECENT_PATH = "/artifacts?groupBy=none&limit=100";
const STACK_PATH = "/artifacts?groupBy=task";
const SEARCH_DEBOUNCE_MS = 250;
/** The BFF's `q` max_length (upstream COMPANY_ARTIFACTS_MAX_QUERY_LENGTH).
 *  Trim to it here so a paste never comes back a 422. */
const SEARCH_MAX = 160;

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
      aria-label="Sort deliverables"
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
  const { read, del, download, mutate } = useTeamsApi();
  // The gallery and the issue-detail sections keep SEPARATE SWR caches; deleting
  // here must also invalidate the issue-scoped list (documents/attachments/
  // work-products) or the removed artifact lingers in the task detail. Both
  // gallery keys go through useTeamsApi's scoped mutate — see `confirmDelete`.

  // View state: null = the stack list; an issue id = that expanded stack. Upstream
  // holds this in a URL param (`groupIssueId`); we keep it in component state.
  const [expandedIssueId, setExpandedIssueId] = useState<string | null>(null);
  const [view, setView] = useState<LibraryView>("recent");
  const [sort, setSort] = useState<ArtifactSort>("newest");
  const [viewing, setViewing] = useState<CompanyArtifact | null>(null);
  const [pendingDelete, setPendingDelete] = useState<CompanyArtifact | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");

  // Debounce the raw box so each keystroke isn't a BFF search (SearchPanel's
  // pattern). Trim + cap before it can reach a key: the suffix is part of the
  // SWR key, so an untrimmed one would fetch twice for "plan" and "plan ".
  useEffect(() => {
    const id = setTimeout(
      () => setDebouncedQuery(query.trim().slice(0, SEARCH_MAX)),
      SEARCH_DEBOUNCE_MS,
    );
    return () => clearTimeout(id);
  }, [query]);
  const qParam = debouncedQuery ? `&q=${encodeURIComponent(debouncedQuery)}` : "";

  // Recent — the flat newest-first feed, bucketed by date. Only the active view
  // is read; the other key stays null so switching tabs costs one fetch, not two.
  // `keepPreviousData` holds the loaded feed on screen while the next
  // (q-suffixed) key is in flight, so every debounce settle does not blank the
  // grid to "Loading the Library…". Deliberately NOT on the two stack reads:
  // their keys go null on tab/back, and lagging data there would show one
  // task's deliverables under another's heading.
  const recent = read<CompanyArtifactsResponse>(
    view === "recent" ? `${RECENT_PATH}${qParam}` : null,
    { keepPreviousData: true },
  );
  // Stack list — groupBy=task clusters deliverables under the issue that produced
  // them (the ported grouped shape, not a flat firehose).
  const stackList = read<CompanyArtifactsResponse>(
    view === "by-task" ? `${STACK_PATH}${qParam}` : null,
  );
  // Expanded stack — a conditional (null-keyed) read until a stack is opened.
  const expanded = read<CompanyArtifactsResponse>(
    expandedIssueId
      ? `${STACK_PATH}&groupIssueId=${encodeURIComponent(expandedIssueId)}${qParam}`
      : null,
  );

  // A read failure is never rendered as results, retained or otherwise: with
  // `keepPreviousData` the PREVIOUS query's data survives an error on the new
  // key, which would list unrelated cards as though they matched the new search.
  // The By-task reads don't opt into keepPreviousData, so they keep their posture.
  const recentArtifacts = recent.error ? [] : (recent.data?.artifacts ?? []);

  const groups = stackList.data?.groups ?? [];
  const sortedGroups = sortGroupsByUpdated(groups, sort);

  const expandedArtifacts = expanded.data?.artifacts ?? [];
  const sortedArtifacts = sortArtifactsByUpdated(expandedArtifacts, sort);
  const selectedGroup =
    expanded.data?.selectedGroup ?? groups.find((g) => g.issue?.id === expandedIssueId) ?? null;

  const isRecent = view === "recent";
  const isExpanded = expandedIssueId !== null;
  const activeLoading = isRecent
    ? recent.isLoading
    : isExpanded
      ? expanded.isLoading
      : stackList.isLoading;
  const activeError = isRecent ? recent.error : isExpanded ? expanded.error : stackList.error;
  const hasItems = isRecent
    ? recentArtifacts.length > 0
    : isExpanded
      ? expandedArtifacts.length > 0
      : groups.length > 0;

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
    } catch (err) {
      notifyApiError(err, "Couldn't download this deliverable");
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
      setDeleteError("Couldn’t delete this deliverable. Please try again.");
      setDeleting(false);
      return;
    }
    // Deleted. Close the confirm + the viewer if it was showing this artifact,
    // then best-effort revalidate BOTH gallery keys AND the issue-scoped list
    // that also holds it (separate SWR caches) so the task detail stops showing
    // it. A revalidation hiccup must not reopen the dialog — the delete
    // succeeded.
    //
    // The gallery goes through useTeamsApi's scoped mutate, not each read's
    // bound one: only the active view is keyed (the other is null-keyed), and a
    // bound mutate on a null-keyed hook is a no-op — so the inactive view would
    // keep serving the deleted deliverable for the read layer's 15s dedupe
    // window. It is a key-filter form rather than the two exact keys, because
    // every `q` is its own key: deleting under a search and then clearing the
    // box would otherwise still list the deleted deliverable. The issue-scoped
    // list is a different resource and stays an exact key.
    setViewing((cur) => (cur && cur.id === artifact.id ? null : cur));
    setPendingDelete(null);
    setDeleting(false);
    const issueListPath = issueListPathForArtifact(artifact);
    void Promise.all([
      mutate((path) => path.startsWith("/teams/artifacts?")),
      expandedIssueId ? expanded.mutate() : Promise.resolve(undefined),
      issueListPath ? mutate(`/teams${issueListPath}`) : Promise.resolve(undefined),
    ]).catch(() => {});
  }

  return (
    <div className="editorial-page p-4 sm:p-5 lg:p-6 flex flex-col gap-4 max-w-4xl mx-auto w-full">
      <PanelMasthead overline="Work" title="Library" />
      <PanelToolbar
        searchValue={query}
        onSearchChange={setQuery}
        searchPlaceholder="Search the Library…"
        searchAriaLabel="Search the Library"
        filters={
          <PanelTabs
            variant="pill"
            ariaLabel="Library view"
            value={view}
            onValueChange={(v) => {
              setView(v as LibraryView);
              setExpandedIssueId(null);
            }}
            tabs={[
              { value: "recent", label: "Recent" },
              { value: "by-task", label: "By task" },
            ]}
          />
        }
        // Recent is date-ordered by definition; sort belongs to the stacks.
        actions={!isRecent && hasItems ? <SortControl sort={sort} onChange={setSort} /> : null}
      />

      {!isRecent && isExpanded ? (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <button
            type="button"
            onClick={() => setExpandedIssueId(null)}
            data-testid="artifact-stack-back"
            className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition-colors hover:bg-[var(--cream-deep)]"
            style={{ color: "var(--midnight-muted)" }}
          >
            <ArrowLeft size={16} aria-hidden="true" />
            All deliverables
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

      <section aria-label="Library" className="min-w-0">
        {activeLoading && !hasItems ? (
          <p className="font-editorial-serif text-base italic" style={{ color: "var(--midnight-muted)" }}>
            Loading the Library…
          </p>
        ) : activeError && !hasItems ? (
          // A read failure is not an empty Library. A zero here is a claim we
          // have not earned.
          <PanelNotice
            state="unavailable"
            icon={Images}
            headline="We couldn't load the Library right now."
            supporting="This is a temporary read failure, not an empty Library. Check back shortly."
          />
        ) : !hasItems && debouncedQuery ? (
          // A miss under a query is not an empty Library — this branch has to
          // come first, or every search for something we don't have reads as
          // “your agents have made nothing”.
          <PanelNotice
            state="zero"
            icon={Images}
            headline={`Nothing in the Library matches “${debouncedQuery}”.`}
            supporting="Search covers titles, the text of documents, summaries and file names."
            action={{ label: "Clear search", onClick: () => setQuery("") }}
          />
        ) : !hasItems ? (
          isExpanded ? (
            <PanelNotice state="zero" icon={Images} headline="No deliverables in this task." />
          ) : (
            // Calm: empty is the default state of most companies' first week.
            <PanelNotice
              state="zero"
              icon={Images}
              headline="Nothing made yet."
              supporting="The first deliverable your agents finish lands here — plans they write, files they upload, pages they publish."
            />
          )
        ) : isRecent ? (
          <div className="flex flex-col gap-5">
            {bucketByRecency(recentArtifacts).map(({ label, artifacts }) => (
              <section key={label} aria-label={label} className="flex flex-col gap-2">
                <h2 className="label-caps">{label}</h2>
                <div className="grid grid-cols-1 gap-3 @xl:grid-cols-2 @4xl:grid-cols-3">
                  {artifacts.map((artifact) => (
                    <ArtifactGalleryCard
                      key={artifact.id}
                      artifact={artifact}
                      onOpen={setViewing}
                      onDownload={handleDownload}
                      onRequestDelete={setPendingDelete}
                    />
                  ))}
                </div>
              </section>
            ))}
            {recent.data?.nextCursor ? (
              // ponytail: limit=100 + this line instead of cursor paging; add useSWRInfinite over nextCursor when a Library passes 100 and search is not enough.
              <p className="text-caption" style={{ color: "var(--midnight-muted)" }}>
                Showing the latest 100.{" "}
                {debouncedQuery
                  ? "Narrow your search to find older deliverables."
                  : "Search to find older deliverables."}
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
        title="Delete this deliverable?"
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
