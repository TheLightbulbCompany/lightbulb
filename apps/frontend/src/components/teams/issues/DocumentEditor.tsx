"use client";

import { lazy, Suspense, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { roomMarkdown } from "@/components/messaging/roomMarkdown";
import type { Components } from "react-markdown";
import type { CompanyAgent, IssueDocument } from "@/components/teams/shared/types";

const MarkdownEditor = lazy(() => import("@/components/teams/shared/components/MarkdownEditor").then((m) => ({ default: m.MarkdownEditor })));

// A document keeps MarkdownView's own links, images and the rest; it takes only the
// fence overrides (cards, diagrams) from the room set. Merged shallowly, so adding
// `a` or `img` here would replace the attachment handling.
export const documentMarkdown: Partial<Components> = { code: roomMarkdown.code, pre: roomMarkdown.pre };

type Doc = IssueDocument & { latestRevisionId?: string | null };

// The Teams BFF relays upstream's stale-revision answer as 409 `{ detail, upstream_status }`;
// a looser body (`error`/`message`/`code`, nested or not) is matched by wording too.
function isConflict(e: unknown): boolean {
  const err = e as { status?: number; body?: unknown };
  if (err?.status === 409) return true;
  const walk = (v: unknown): boolean =>
    typeof v === "string" ? /conflict|locked/i.test(v) : v !== null && typeof v === "object" && Object.values(v).some(walk);
  const body = err?.body as { upstream_status?: number } | null | undefined;
  return body?.upstream_status === 409 || walk(body);
}

/** Edit a document and save it as a new revision against the one that was opened. */
export function DocumentEditor({ issueId, basePath = `/issues/${encodeURIComponent(issueId)}`, document, agents = [], currentUserName, onSaved, onCancel }: {
  issueId: string;
  basePath?: string;
  document: Doc;
  agents?: CompanyAgent[];
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
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const path = `${basePath}/documents/${encodeURIComponent(document.key)}`;
  const cacheKey = `/teams${path}`;

  // On a conflict, read the document again; a cached copy from before the other save is ignored.
  const { data: latest, mutate: refetchLatest } = read<Doc>(conflict ? path : null);
  const theirs = conflict && latest && latest.latestRevisionId !== baseRevisionId ? latest : null;
  const theirName = theirs?.updatedByAgentId ? agents.find((a) => a.id === theirs.updatedByAgentId)?.name : undefined;

  async function save(base: string | null = baseRevisionId) {
    if (saving) return;
    setSaving(true);
    setError(null);
    setConflict(false);
    try {
      const saved = await put<Doc>(path, { title: title || null, format: "markdown", body, changeSummary: `Edited by ${currentUserName}`, baseRevisionId: base });
      await Promise.all([
        mutate(cacheKey),
        mutate(`/teams${basePath}/documents`),
        mutate((key) => key.startsWith(`${cacheKey}/`)),
      ]);
      onSaved(saved);
    } catch (e) {
      if (isConflict(e)) { setConflict(true); void refetchLatest(); }
      else setError("Couldn't save. Try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form className="grid gap-2" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      <Input aria-label="Title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title" />
      <Suspense fallback={<p className="text-caption text-muted-foreground">Loading editor…</p>}>
        <MarkdownEditor value={body} onChange={setBody} />
      </Suspense>
      {conflict && (
        <div role="alert" className="grid gap-2 text-ui text-err">
          <p>
            This document changed since you opened it
            {theirs ? <> — {theirName ?? "someone else"} saved rev {theirs.latestRevisionNumber ?? "?"}</> : null}.
            Your draft is kept here.
          </p>
          {theirs && (
            <details className="text-fg">
              <summary className="cursor-pointer">Their version (rev {theirs.latestRevisionNumber ?? "?"})</summary>
              <MarkdownView components={documentMarkdown}>{theirs.body ?? ""}</MarkdownView>
            </details>
          )}
          {theirs && (
            <div className="flex gap-2">
              <Button type="button" size="sm" disabled={saving} onClick={() => { const base = theirs.latestRevisionId ?? null; setBaseRevisionId(base); void save(base); }}>Save over their version</Button>
              <Button type="button" size="sm" variant="outline" disabled={saving} onClick={onCancel}>Discard my draft</Button>
            </div>
          )}
        </div>
      )}
      {error && <p role="alert" className="text-ui text-err">{error}</p>}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={saving}>{saving ? "Saving…" : "Save"}</Button>
        <Button type="button" size="sm" variant="outline" disabled={saving} onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}
