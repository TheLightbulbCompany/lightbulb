"use client";

import { lazy, Suspense, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { IssueDocument } from "@/components/teams/shared/types";

const MarkdownEditor = lazy(() => import("@/components/teams/shared/components/MarkdownEditor").then((m) => ({ default: m.MarkdownEditor })));

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
export function DocumentEditor({ issueId, basePath = `/issues/${issueId}`, document, currentUserName, onSaved, onCancel }: {
  issueId: string;
  basePath?: string;
  document: Doc;
  currentUserName: string;
  onSaved: (saved: Doc) => void;
  onCancel: () => void;
}) {
  const { put, mutate } = useTeamsApi();
  const [title, setTitle] = useState(document.title ?? "");
  const [body, setBody] = useState(document.body ?? "");
  // Pinned at open: a reload shows the new version behind the draft, but a re-save stays stale until Cancel.
  const [baseRevisionId] = useState(document.latestRevisionId ?? null);
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const path = `${basePath}/documents/${document.key}`;
  const cacheKey = `/teams${path}`;

  async function save() {
    if (saving) return;
    setSaving(true);
    setError(null);
    setConflict(false);
    try {
      const saved = await put<Doc>(path, { title: title || null, format: "markdown", body, changeSummary: `Edited by ${currentUserName}`, baseRevisionId });
      await Promise.all([
        mutate(cacheKey),
        mutate(`/teams${basePath}/documents`),
        mutate((key) => key.startsWith(`${cacheKey}/`)),
      ]);
      onSaved(saved);
    } catch (e) {
      if (isConflict(e)) setConflict(true);
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
        <p role="alert" className="text-ui text-err">
          This document changed since you opened it. Your draft is kept here;{" "}
          <Button type="button" variant="link" onClick={() => void mutate(cacheKey)}>reload</Button>{" "}
          to see the new version, then copy your changes across.
        </p>
      )}
      {error && <p role="alert" className="text-ui text-err">{error}</p>}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={saving}>{saving ? "Saving…" : "Save"}</Button>
        <Button type="button" size="sm" variant="outline" disabled={saving} onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}
