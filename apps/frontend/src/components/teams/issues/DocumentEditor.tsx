"use client";

import { lazy, Suspense, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { roomMarkdown } from "@/components/messaging/roomMarkdown";
import { CardContext, readOnlyCardContext } from "@/components/messaging/openui/CardContext";
import { actorName } from "@/components/teams/shared/lib/actorName";
import type { Components } from "react-markdown";
import type { CompanyAgent, CompanyMember, IssueDocument } from "@/components/teams/shared/types";

const MarkdownEditor = lazy(() => import("@/components/teams/shared/components/MarkdownEditor").then((m) => ({ default: m.MarkdownEditor })));

// A document keeps MarkdownView's own links, images and the rest; it takes only the
// fence overrides (cards, diagrams) from the room set. Merged shallowly, so adding
// `a` or `img` here would replace the attachment handling.
export const documentMarkdown: Partial<Components> = { code: roomMarkdown.code, pre: roomMarkdown.pre };

type Doc = IssueDocument & { latestRevisionId?: string | null };

// The Teams BFF relays upstream's stale-revision answer as 409 `{ detail, upstream_status }`.
// A locked document answers 409 too, with "locked" in the detail and the SAME revision.
function conflictOf(e: unknown): { locked: boolean } | null {
  const err = e as { status?: number; body?: { upstream_status?: number; detail?: unknown } | null };
  if (err?.status !== 409 && err?.body?.upstream_status !== 409) return null;
  return { locked: typeof err?.body?.detail === "string" && /locked/i.test(err.body.detail) };
}

const TITLE_MAX = 200;

/** Edit a document and save it as a new revision against the one that was opened. */
export function DocumentEditor({ issueId, basePath = `/issues/${encodeURIComponent(issueId)}`, document, agents = [], members = [], currentUserId, currentUserName, onSaved, onCancel }: {
  issueId: string;
  basePath?: string;
  document: Doc;
  agents?: CompanyAgent[];
  members?: CompanyMember[];
  currentUserId?: string | null;
  currentUserName: string;
  onSaved: (saved: Doc) => void;
  onCancel: () => void;
}) {
  const { put, mutate, read } = useTeamsApi();
  const [title, setTitle] = useState(document.title ?? "");
  const [body, setBody] = useState(document.body ?? "");
  // Pinned at open; only "Save over their version" moves it.
  const [baseRevisionId, setBaseRevisionId] = useState(document.latestRevisionId ?? null);
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState<{ locked: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const path = `${basePath}/documents/${encodeURIComponent(document.key)}`;
  const cacheKey = `/teams${path}`;
  const readOnlyCards = useMemo(() => readOnlyCardContext(document.id), [document.id]);
  const dirty = title !== (document.title ?? "") || body !== (document.body ?? "");

  // On a conflict, read the document again; a cached copy from before the other save is ignored.
  const { data: latest, error: readError, isValidating } = read<Doc>(conflict ? path : null);
  const locked = Boolean(conflict?.locked || latest?.lockedAt);
  const theirs = conflict && !locked && latest && latest.latestRevisionId !== baseRevisionId ? latest : null;
  const loading = Boolean(conflict) && !readError && (!latest || Boolean(isValidating));
  const theirName = theirs ? actorName({ agentId: theirs.updatedByAgentId, userId: theirs.updatedByUserId, agents, members, currentUserId }) : "";
  const lockerName = actorName({ agentId: latest?.lockedByAgentId ?? document.lockedByAgentId, agents });

  async function save(base: string | null = baseRevisionId) {
    if (saving) return;
    setSaving(true);
    setError(null);
    setConflict(null);
    try {
      const saved = await put<Doc>(path, { title: title || null, format: "markdown", body, changeSummary: `Edited by ${currentUserName}`, baseRevisionId: base });
      await Promise.all([
        mutate(cacheKey),
        mutate(`/teams${basePath}/documents`),
        mutate((key) => key.startsWith(`${cacheKey}/`)),
      ]);
      onSaved(saved);
    } catch (e) {
      const c = conflictOf(e);
      if (c) { setConflict(c); void mutate(cacheKey); }
      else if ((e as { status?: number })?.status === 422) setError(`Couldn't save. The title can be up to ${TITLE_MAX} characters.`);
      else setError("Couldn't save. Try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="grid gap-2" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <Input aria-label="Title" maxLength={TITLE_MAX} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title" />
      <Suspense fallback={<p className="text-caption text-muted-foreground">Loading editor…</p>}>
        <MarkdownEditor value={body} onChange={setBody} />
      </Suspense>
      {conflict && (
        <div role="alert" className="grid gap-2 text-ui text-err">
          <p>
            {locked
              ? <>Locked by {lockerName}. Your draft is kept here; save again once it is unlocked.</>
              : theirs
                ? <>This document changed since you opened it — {theirName} saved rev {theirs.latestRevisionNumber ?? "?"}. Your draft is kept here.</>
                : loading
                  ? <>This document changed since you opened it. Loading their version… Your draft is kept here.</>
                  : <>This document changed since you opened it, and we couldn&apos;t load their version. Your draft is kept here.</>}
          </p>
          {theirs && (
            <details className="text-fg">
              <summary className="cursor-pointer">Their version (rev {theirs.latestRevisionNumber ?? "?"})</summary>
              <CardContext.Provider value={readOnlyCards}>
                <MarkdownView components={documentMarkdown}>{theirs.body ?? ""}</MarkdownView>
              </CardContext.Provider>
            </details>
          )}
          <div className="flex gap-2">
            {theirs && <Button type="button" size="sm" disabled={saving} onClick={() => { const base = theirs.latestRevisionId ?? null; setBaseRevisionId(base); void save(base); }}>Save over their version</Button>}
            {!locked && !theirs && !loading && <Button type="button" size="sm" variant="outline" onClick={() => void mutate(cacheKey)}>Retry</Button>}
            <Button type="button" size="sm" variant="outline" disabled={saving} onClick={onCancel}>Discard my draft</Button>
          </div>
        </div>
      )}
      {error && <p role="alert" className="text-ui text-err">{error}</p>}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={saving || !dirty}>{saving ? "Saving…" : "Save"}</Button>
        <Button type="button" size="sm" variant="outline" disabled={saving} onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}
