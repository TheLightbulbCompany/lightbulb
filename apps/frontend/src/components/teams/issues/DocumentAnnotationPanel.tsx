"use client";

// Ported/adapted from upstream Paperclip `ui/src/components/DocumentAnnotationPanel.tsx`
// (A3), rewired onto useTeamsApi (post/patch + SWR mutate) instead of react-query.
// The inline thread panel: list annotated spans, comment on a fresh selection,
// reply, and resolve/reopen.

import { useMemo, useState } from "react";
import { Check, RotateCcw, Unlink, X } from "lucide-react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import type { CompanyAgent } from "@/components/teams/shared/types";
import type {
  DocumentAnnotationThread,
  DocumentAnnotationComment,
  PendingAnchor,
} from "./lib/documentReviewTypes";
import { truncate } from "@/lib/utils";

function commentAuthor(comment: DocumentAnnotationComment, agents: CompanyAgent[]): string {
  if (comment.authorAgentId) {
    return agents.find((a) => a.id === comment.authorAgentId)?.name ?? "Agent";
  }
  if (comment.authorName) return comment.authorName;
  if (comment.authorUserId) return "Board";
  return comment.authorType === "agent" ? "Agent" : "Board";
}

export function DocumentAnnotationPanel({
  issueId,
  basePath = `/issues/${issueId}`,
  documentKey,
  baseRevisionId,
  baseRevisionNumber,
  threads,
  agents,
  pendingAnchor,
  onClearPendingAnchor,
  onClose,
  focusedThreadId,
  onFocusThread,
  refetch,
  newCommentDisabled = false,
  newCommentDisabledReason,
}: {
  issueId: string;
  /** Owner of the document routes; a case passes `/cases/{id}`. */
  basePath?: string;
  documentKey: string;
  baseRevisionId: string | null;
  baseRevisionNumber: number;
  threads: DocumentAnnotationThread[];
  agents: CompanyAgent[];
  pendingAnchor: PendingAnchor | null;
  onClearPendingAnchor: () => void;
  onClose: () => void;
  focusedThreadId: string | null;
  onFocusThread: (id: string | null) => void;
  refetch: () => void;
  newCommentDisabled?: boolean;
  newCommentDisabledReason?: string | null;
}) {
  const { post, patch } = useTeamsApi();
  const base = `${basePath}/documents/${documentKey}/annotations`;
  const [composer, setComposer] = useState("");
  const [replyDrafts, setReplyDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const visibleThreads = useMemo(
    () =>
      [...threads]
        // An orphaned thread (its anchored text was removed / couldn't re-anchor)
        // has no in-document highlight, but an UNRESOLVED one is still a live
        // review discussion the user must be able to read + resolve. Dropping it
        // here silently hid real comments ("No comments yet" over live threads),
        // so keep orphaned-open threads in the list — the layer skips drawing
        // their (absent) highlight. Only orphaned+resolved threads, which are
        // fully handled, stay hidden.
        .filter((thread) => thread.anchorState !== "orphaned" || thread.status !== "resolved")
        .sort(
          (a, b) =>
            (a.normalizedStart ?? 0) - (b.normalizedStart ?? 0)
            || new Date(a.createdAt).getTime() - new Date(b.createdAt).getTime(),
        ),
    [threads],
  );

  async function createThread() {
    const body = composer.trim();
    if (!body || !pendingAnchor || !baseRevisionId) return;
    setBusy(true);
    setError(null);
    try {
      await post(base, {
        baseRevisionId,
        baseRevisionNumber,
        selector: pendingAnchor.selector,
        body,
      });
      setComposer("");
      onClearPendingAnchor();
      refetch();
    } catch {
      setError("Failed to post comment.");
    } finally {
      setBusy(false);
    }
  }

  async function submitReply(threadId: string) {
    const body = (replyDrafts[threadId] ?? "").trim();
    if (!body) return;
    setBusy(true);
    setError(null);
    try {
      await post(`${base}/${threadId}/comments`, { body });
      setReplyDrafts((current) => ({ ...current, [threadId]: "" }));
      refetch();
    } catch {
      setError("Failed to add reply.");
    } finally {
      setBusy(false);
    }
  }

  async function toggleResolve(thread: DocumentAnnotationThread) {
    setBusy(true);
    setError(null);
    try {
      await patch(`${base}/${thread.id}`, {
        status: thread.status === "resolved" ? "open" : "resolved",
      });
      refetch();
    } catch {
      setError("Failed to update comment status.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside
      role="complementary"
      aria-label={`Comments on ${documentKey}`}
      data-testid="document-annotation-panel"
      className="flex w-full flex-col rounded-md"
      style={{ border: "1px solid var(--edge)", background: "var(--panel)" }}
    >
      <div
        className="flex items-center justify-between px-3 py-1.5"
        style={{ borderBottom: "1px solid var(--edge)" }}
      >
        <span className="text-caption font-medium" style={{ color: "var(--midnight)" }}>
          Comments {visibleThreads.length > 0 ? `(${visibleThreads.length})` : ""}
        </span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close comments panel"
          className="rounded-sm p-1"
          style={{ color: "var(--midnight-muted)" }}
        >
          <X size={16} />
        </button>
      </div>

      {newCommentDisabled && newCommentDisabledReason ? (
        <p className="px-3 py-1.5 text-caption" style={{ color: "var(--midnight-muted)" }}>
          {newCommentDisabledReason}
        </p>
      ) : null}
      {error ? (
        <p
          data-testid="document-annotation-error"
          className="px-3 py-1.5 text-caption"
          style={{ color: "var(--err)" }}
        >
          {error}
        </p>
      ) : null}

      <div className="flex max-h-96 flex-col gap-2 overflow-y-auto px-3 py-2">
        {visibleThreads.length === 0 ? (
          <p className="text-caption italic" style={{ color: "var(--midnight-muted)" }}>
            No comments yet. Select text in the document to add one.
          </p>
        ) : (
          visibleThreads.map((thread) => {
            const expanded = thread.id === focusedThreadId;
            const latest = thread.comments[thread.comments.length - 1];
            return (
              <article
                key={thread.id}
                data-thread-id={thread.id}
                data-status={thread.status}
                className="rounded-md"
                style={{
                  border: "1px solid var(--edge)",
                  background: thread.status === "resolved" ? "var(--edge)" : "transparent",
                }}
                onClick={() => onFocusThread(expanded ? null : thread.id)}
              >
                {thread.anchorState === "orphaned" ? (
                  <span
                    data-testid={`document-annotation-orphaned-${thread.id}`}
                    title="The text this comment was anchored to was edited or removed, so it can't be highlighted in the document."
                    className="mx-3 mt-2 inline-flex w-fit items-center gap-1 rounded-sm px-1.5 py-0.5 label-caps"
                    style={{ border: "1px solid var(--edge)", color: "var(--midnight-muted)" }}
                  >
                    <Unlink size={12} strokeWidth={1.75} /> Anchor lost
                  </span>
                ) : null}
                <blockquote
                  className="mx-3 mt-2 rounded-sm px-2 py-1 text-caption italic [overflow-wrap:anywhere]"
                  style={{ background: "var(--edge)", color: "var(--midnight-muted)" }}
                >
                  {truncate(thread.selectedText, 119)}
                </blockquote>
                {expanded ? (
                  <div className="flex flex-col gap-2 px-3 py-2" onClick={(e) => e.stopPropagation()}>
                    {thread.comments.map((comment) => (
                      <div
                        key={comment.id}
                        className="rounded-sm px-2 py-1.5"
                        style={{ border: "1px solid var(--edge)" }}
                      >
                        <div className="mb-0.5 flex items-center justify-between gap-2 text-caption">
                          <span className="font-medium" style={{ color: "var(--midnight)" }}>
                            {commentAuthor(comment, agents)}
                          </span>
                          <span style={{ color: "var(--midnight-muted)" }}>{timeAgo(comment.createdAt)}</span>
                        </div>
                        <div className="text-ui">
                          <MarkdownView>{comment.body}</MarkdownView>
                        </div>
                      </div>
                    ))}
                    <textarea
                      data-testid={`document-annotation-reply-${thread.id}`}
                      rows={2}
                      value={replyDrafts[thread.id] ?? ""}
                      onChange={(e) =>
                        setReplyDrafts((current) => ({ ...current, [thread.id]: e.target.value }))
                      }
                      placeholder="Reply…"
                      className="w-full resize-y rounded-md px-2 py-1 text-ui"
                      style={{ border: "1px solid var(--edge)", background: "var(--bg)" }}
                    />
                    <div className="flex items-center justify-end gap-2">
                      <button
                        type="button"
                        onClick={() => toggleResolve(thread)}
                        disabled={busy}
                        className="flex items-center gap-1 rounded-sm px-2 py-1 text-caption disabled:opacity-50"
                        style={{ border: "1px solid var(--edge)", color: "var(--midnight)" }}
                      >
                        {thread.status === "resolved" ? (
                          <>
                            <RotateCcw size={12} strokeWidth={1.75} /> Reopen
                          </>
                        ) : (
                          <>
                            <Check size={12} strokeWidth={1.75} /> Resolve
                          </>
                        )}
                      </button>
                      <button
                        type="button"
                        onClick={() => submitReply(thread.id)}
                        disabled={busy || !(replyDrafts[thread.id] ?? "").trim()}
                        className="rounded-sm px-2 py-1 text-caption disabled:opacity-50"
                        style={{ background: "var(--midnight)", color: "var(--cream)" }}
                      >
                        Reply
                      </button>
                    </div>
                  </div>
                ) : (
                  <p className="px-3 py-2 text-caption" style={{ color: "var(--midnight-muted)" }}>
                    <span style={{ color: "var(--midnight)" }}>
                      {thread.comments.length} comment{thread.comments.length === 1 ? "" : "s"}
                    </span>
                    {latest ? ` · ${truncate(latest.body, 99)}` : ""}
                  </p>
                )}
              </article>
            );
          })
        )}
      </div>

      {pendingAnchor ? (
        <div className="flex flex-col gap-2 px-3 py-2" style={{ borderTop: "1px solid var(--edge)" }}>
          <blockquote
            className="rounded-sm px-2 py-1 text-caption italic [overflow-wrap:anywhere]"
            style={{ background: "var(--edge)", color: "var(--midnight-muted)" }}
          >
            {truncate(pendingAnchor.selectedText, 159)}
          </blockquote>
          <textarea
            data-testid="document-annotation-composer"
            rows={3}
            value={composer}
            onChange={(e) => setComposer(e.target.value)}
            placeholder="Write a comment…"
            disabled={newCommentDisabled}
            className="w-full resize-y rounded-md px-2 py-1 text-ui"
            style={{ border: "1px solid var(--edge)", background: "var(--bg)" }}
          />
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => {
                onClearPendingAnchor();
                setComposer("");
              }}
              className="rounded-sm px-2 py-1 text-caption"
              style={{ color: "var(--midnight-muted)" }}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={createThread}
              disabled={busy || !composer.trim() || newCommentDisabled || !baseRevisionId}
              className="rounded-sm px-2 py-1 text-caption disabled:opacity-50"
              style={{ background: "var(--midnight)", color: "var(--cream)" }}
            >
              {busy ? "Posting…" : "Comment"}
            </button>
          </div>
        </div>
      ) : null}
    </aside>
  );
}
