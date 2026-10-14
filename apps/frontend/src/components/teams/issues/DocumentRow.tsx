"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, ChevronDown, MessageSquare, RotateCcw } from "lucide-react";
import { useSWRConfig } from "swr";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { CompanyAgent, IssueDocument } from "@/components/teams/shared/types";
import { DocumentRevisionMenu } from "./DocumentRevisionMenu";
import { DocumentDiffModal } from "./DocumentDiffModal";
import { DocumentAnnotationLayer, type OverlayThread } from "./DocumentAnnotationLayer";
import { DocumentAnnotationPanel } from "./DocumentAnnotationPanel";
import type {
  DocumentAnnotationThread,
  DocumentRevision,
  DocumentRevisionRef,
  PendingAnchor,
} from "./lib/documentReviewTypes";
import { notifyApiError } from "@/lib/notify";

// The upstream wire payload carries the latest-revision fields even though the
// trimmed local `IssueDocument` type omits them.
type ReviewableDocument = IssueDocument & DocumentRevisionRef;

function documentAnchorId(key: string): string {
  return `document-${key}`;
}

export function DocumentRow({
  doc,
  agents,
  issueId,
  basePath,
  onRestored,
}: {
  doc: IssueDocument;
  agents: CompanyAgent[];
  // `issueId` is optional so a pre-existing caller that doesn't thread it keeps
  // rendering the plain body (no review affordances) rather than breaking.
  issueId?: string;
  // The document's owner route, relative to the Teams BFF. Defaults to the
  // issue; a case document passes `/cases/{caseId}` (with the case id as
  // `issueId`) so the same review surface serves both.
  basePath?: string;
  // Called after a revision restore, for an owner whose document list is not
  // the issue documents read (a case re-reads its detail).
  onRestored?: () => void;
}) {
  const root = basePath ?? `/issues/${issueId}`;
  const reviewable = doc as ReviewableDocument;
  const [open, setOpen] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [diffOpen, setDiffOpen] = useState(false);
  const [selectedRevisionId, setSelectedRevisionId] = useState<string | null>(null);
  const [pendingAnchor, setPendingAnchor] = useState<PendingAnchor | null>(null);
  const [focusedThreadId, setFocusedThreadId] = useState<string | null>(null);
  const [restoring, setRestoring] = useState(false);
  const bodyRef = useRef<HTMLDivElement | null>(null);

  const { read, post } = useTeamsApi();
  const { mutate } = useSWRConfig();

  const when = doc.updatedAt ?? doc.createdAt;
  const titleShown =
    doc.title && doc.title.trim() && doc.title.trim().toLowerCase() !== doc.key.trim().toLowerCase()
      ? doc.title.trim()
      : null;
  const agentName = doc.updatedByAgentId
    ? agents.find((a) => a.id === doc.updatedByAgentId)?.name ?? null
    : null;
  const Chevron = open ? ChevronDown : ChevronRight;

  const reviewEnabled = Boolean(issueId);
  const latestRevisionNumber = reviewable.latestRevisionNumber ?? 0;
  const latestRevisionId = reviewable.latestRevisionId ?? null;
  const annotationsPath = reviewEnabled
    ? `${root}/documents/${doc.key}/annotations?status=all&includeComments=true`
    : null;
  const revisionsPath =
    reviewEnabled && open ? `${root}/documents/${doc.key}/revisions` : null;

  const { data: annData, mutate: mutateAnnotations } = read<{ threads: DocumentAnnotationThread[] }>(
    annotationsPath,
  );
  const threads = useMemo(() => annData?.threads ?? [], [annData]);
  // The unresolved badge counts EVERY open thread, including orphaned-open ones:
  // the panel keeps those visible and resolvable, so the header must advertise
  // them too or a live review comment goes unnoticed until the panel is opened.
  // Only the in-document highlight overlay skips orphaned anchors (it has no
  // span to draw) — see DocumentAnnotationLayer's `anchorState === "orphaned"`
  // guard. Resolved threads (orphaned or not) stay excluded from the count.
  const openCount = useMemo(
    () => threads.filter((t) => t.status === "open").length,
    [threads],
  );
  const overlayThreads = useMemo<OverlayThread[]>(
    () =>
      threads.map((t) => ({
        id: t.id,
        selectedText: t.selectedText,
        status: t.status,
        anchorState: t.anchorState,
        normalizedStart: t.normalizedStart ?? null,
      })),
    [threads],
  );

  const { data: revData, mutate: mutateRevisions } = read<{ revisions: DocumentRevision[] }>(revisionsPath);
  const selectedRevision = useMemo(
    () => (revData?.revisions ?? []).find((r) => r.id === selectedRevisionId) ?? null,
    [revData, selectedRevisionId],
  );
  const historicalPreview = selectedRevisionId !== null;
  const shownBody = historicalPreview ? selectedRevision?.body ?? "" : doc.body ?? "";

  // Deep-link / confirmation-chip anchor: expand + scroll when the URL hash
  // targets this document (`#document-<key>`). Fixes the previously dead chips.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const check = () => {
      const raw = window.location.hash.replace(/^#/, "");
      let decoded = raw;
      try {
        decoded = decodeURIComponent(raw);
      } catch {
        /* keep raw */
      }
      if (decoded === documentAnchorId(doc.key) || raw === documentAnchorId(doc.key)) {
        setOpen(true);
        requestAnimationFrame(() => {
          bodyRef.current
            ?.closest("li")
            ?.scrollIntoView({ block: "start", behavior: "smooth" });
        });
      }
    };
    check();
    window.addEventListener("hashchange", check);
    return () => window.removeEventListener("hashchange", check);
  }, [doc.key]);

  const handleRequestComment = useCallback(
    (anchor: PendingAnchor) => {
      setPendingAnchor(anchor);
      setPanelOpen(true);
    },
    [],
  );

  const refetchAnnotations = useCallback(() => {
    void mutateAnnotations();
  }, [mutateAnnotations]);

  const restoreRevision = useCallback(async () => {
    if (!reviewEnabled || !selectedRevisionId) return;
    setRestoring(true);
    try {
      await post(
        `${root}/documents/${doc.key}/revisions/${selectedRevisionId}/restore`,
        {},
      );
      setSelectedRevisionId(null);
      // Refresh the doc list (new latest body), revisions, and annotations
      // (server remaps open threads onto the restored revision).
      // Keys derive from `root`, so a case document refreshes its own reads.
      // `mutateRevisions` is the read the revision menu + diff modal share.
      void mutate(`/teams${root}/documents`);
      void mutate(`/teams${root}/documents/${doc.key}/revisions`);
      void mutateRevisions();
      void mutateAnnotations();
      onRestored?.();
    } catch (err) {
      notifyApiError(err, "Couldn't restore that revision");
    } finally {
      setRestoring(false);
    }
  }, [reviewEnabled, selectedRevisionId, post, root, doc.key, mutate, mutateRevisions, mutateAnnotations, onRestored]);

  const newCommentDisabledReason = historicalPreview
    ? "Comments are disabled while previewing a historical revision."
    : !latestRevisionId
      ? "Document has no saved revision yet."
      : null;
  const newCommentDisabled = Boolean(newCommentDisabledReason);

  return (
    <li
      id={reviewEnabled ? documentAnchorId(doc.key) : undefined}
      className="flex flex-col scroll-mt-4"
      style={{ border: "1px solid var(--edge)", borderRadius: 6 }}
    >
      <div className="flex items-center gap-2 px-3 py-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <Chevron size={16} className="shrink-0" style={{ color: "var(--midnight-muted)" }} />
          <span
            className="label-caps font-mono px-1.5 py-0.5 rounded-sm shrink-0"
            style={{ background: "var(--edge)", color: "var(--midnight)" }}
          >
            {doc.key}
          </span>
          {titleShown ? (
            <span className="flex-1 truncate text-[var(--midnight)]">{titleShown}</span>
          ) : (
            <span className="flex-1" />
          )}
          {when && (
            <span className="text-caption text-[var(--midnight-muted)] shrink-0">
              updated {timeAgo(when)}
              {agentName ? ` · ${agentName}` : ""}
            </span>
          )}
        </button>
        {reviewEnabled ? (
          <div className="flex shrink-0 items-center gap-1.5">
            <button
              type="button"
              onClick={() => {
                setOpen(true);
                setPanelOpen((v) => !v);
              }}
              aria-expanded={panelOpen}
              data-testid={`document-comments-toggle-${doc.key}`}
              aria-label={
                openCount === 0
                  ? `Comments on ${doc.key}`
                  : `${openCount} unresolved comments on ${doc.key}`
              }
              className="flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-2xs"
              style={{
                border: "1px solid var(--edge)",
                color: openCount > 0 ? "var(--midnight)" : "var(--midnight-muted)",
                background: panelOpen ? "var(--edge)" : "transparent",
              }}
            >
              <MessageSquare size={12} strokeWidth={1.75} />
              <span className="tabular-nums">{openCount}</span>
            </button>
            {open ? (
              <DocumentRevisionMenu
                issueId={issueId!}
                basePath={root}
                documentKey={doc.key}
                latestRevisionNumber={latestRevisionNumber}
                selectedRevisionId={selectedRevisionId}
                onSelectRevision={setSelectedRevisionId}
                onOpenDiff={() => setDiffOpen(true)}
              />
            ) : latestRevisionNumber > 0 ? (
              <span
                className="rounded-sm px-1.5 py-0.5 text-2xs tabular-nums"
                style={{ border: "1px solid var(--edge)", color: "var(--midnight-muted)" }}
              >
                rev {latestRevisionNumber}
              </span>
            ) : null}
          </div>
        ) : null}
      </div>

      {open && (
        <div
          className="flex flex-col gap-3 px-3 pb-3 pt-1"
          style={{ borderTop: "1px solid var(--edge)" }}
        >
          {historicalPreview ? (
            <div
              className="flex items-center justify-between gap-2 rounded-sm px-2 py-1 text-caption"
              style={{ background: "var(--edge)", color: "var(--midnight)" }}
            >
              <span>
                Previewing rev {selectedRevision?.revisionNumber ?? "?"} (read-only)
              </span>
              <button
                type="button"
                onClick={restoreRevision}
                disabled={restoring}
                className="flex items-center gap-1 rounded-sm px-2 py-0.5 disabled:opacity-50"
                style={{ background: "var(--midnight)", color: "var(--cream)" }}
              >
                <RotateCcw size={12} strokeWidth={1.75} />
                {restoring ? "Restoring…" : "Restore this revision"}
              </button>
            </div>
          ) : null}

          <div className="flex flex-col gap-3 @3xl:flex-row">
            <div className="relative min-w-0 flex-1 max-w-none text-[var(--midnight)]">
              {/* `bodyRef` wraps ONLY the rendered markdown so the annotation
                  layer's text-node walk + selection offsets aren't polluted by
                  the overlay's own chrome (which is a sibling below). */}
              <div ref={bodyRef}>
                {shownBody && shownBody.trim() ? (
                  <MarkdownView>{shownBody}</MarkdownView>
                ) : (
                  <p className="pt-2 text-caption text-[var(--midnight-muted)]">(empty document)</p>
                )}
              </div>
              {reviewEnabled && !historicalPreview && latestRevisionId ? (
                <DocumentAnnotationLayer
                  containerRef={bodyRef}
                  markdown={shownBody}
                  threads={overlayThreads}
                  focusedThreadId={focusedThreadId}
                  onThreadFocus={(id) => {
                    setFocusedThreadId(id);
                    setPanelOpen(true);
                  }}
                  onRequestComment={handleRequestComment}
                  newCommentDisabled={newCommentDisabled}
                  newCommentDisabledReason={newCommentDisabledReason}
                />
              ) : null}
            </div>

            {reviewEnabled && panelOpen ? (
              <div className="w-full lg:w-80 lg:shrink-0">
                <DocumentAnnotationPanel
                  issueId={issueId!}
                  basePath={root}
                  documentKey={doc.key}
                  baseRevisionId={latestRevisionId}
                  baseRevisionNumber={latestRevisionNumber}
                  threads={threads}
                  agents={agents}
                  pendingAnchor={pendingAnchor}
                  onClearPendingAnchor={() => setPendingAnchor(null)}
                  onClose={() => {
                    setPanelOpen(false);
                    setPendingAnchor(null);
                    setFocusedThreadId(null);
                  }}
                  focusedThreadId={focusedThreadId}
                  onFocusThread={setFocusedThreadId}
                  refetch={refetchAnnotations}
                  newCommentDisabled={newCommentDisabled}
                  newCommentDisabledReason={newCommentDisabledReason}
                />
              </div>
            ) : null}
          </div>
        </div>
      )}

      {reviewEnabled && diffOpen ? (
        <DocumentDiffModal
          issueId={issueId!}
          basePath={root}
          documentKey={doc.key}
          latestRevisionNumber={latestRevisionNumber}
          open={diffOpen}
          onOpenChange={setDiffOpen}
        />
      ) : null}
    </li>
  );
}
