"use client";

import { useEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { Button } from "@/components/ui/button";
import { openPanel } from "@/components/workspace/workspaceController";

type Link = { kind: "issue"; companyId: string; issueId: string } | { kind: "workspace"; companyId: string; projectId: string; workspaceId: string };
type Work = { state: "empty" | "linked" | "unavailable"; link: Link | null; revision: string | null; title?: string };
type Props = { roomId: string; sessionId: string };

export function LocalSessionWork(props: Props) {
  const [open, setOpen] = useState(false);
  return <section className="space-y-2">
    <Button variant="ghost" aria-expanded={open} onClick={() => setOpen(value => !value)}>Linked work</Button>
    {open && <ViewerWork {...props} />}
  </section>;
}

function ViewerWork(props: Props) {
  const { userId, orgId, isLoaded } = useAuth();
  // Auth/selection change unmounts all protected display and in-flight form state.
  return isLoaded && userId && orgId
    ? <WorkDetails key={JSON.stringify([userId, orgId, props.roomId, props.sessionId])} {...props} />
    : <p>Linked work unavailable.</p>;
}

function WorkDetails({ roomId, sessionId }: Props) {
  const api = useTeamsApi();
  const path = `/rooms/${encodeURIComponent(roomId)}/local-sessions/${encodeURIComponent(sessionId)}/work-link`;
  const read = api.read<Work>(path, { revalidateOnMount: true, dedupingInterval: 0, refreshInterval: 30_000, shouldRetryOnError: false });
  const [editing, setEditing] = useState(false);
  const [kind, setKind] = useState<"issue" | "workspace">("issue");
  const [id, setId] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  // Never display cached protected data during validation or after an error.
  const data = !read.error && !read.isValidating && !error && !busy ? read.data : undefined;
  async function save(link: Omit<Extract<Link, { kind: "issue" }>, "companyId"> | Omit<Extract<Link, { kind: "workspace" }>, "companyId"> | null) {
    if (busy || !data) return;
    setBusy(true);
    try {
      await api.patch(path, { link, expectedLinkRevision: data.revision });
      if (!alive.current) return;
      setEditing(false);
      await read.mutate();
    } catch {
      if (alive.current) setError(true);
    } finally { if (alive.current) setBusy(false); }
  }
  return <div className="space-y-2 text-sm">
    <p className="text-muted-foreground">Optional reference only. Closing work does not stop this session; finishing the session does not close work.</p>
    {(read.isLoading || read.isValidating || busy) && <p role="status">Checking linked work…</p>}
    {(read.error || error || data?.state === "unavailable") && <p role="status">Linked work unavailable.</p>}
    {data?.state === "empty" && <p>No linked work.</p>}
    {data?.state === "linked" && data.title && data.link && <Button variant="link" onClick={() => {
      const link = data.link!;
      openPanel(link.kind === "issue" ? "teams-issue-detail" : "teams-project-detail", { detailId: link.kind === "issue" ? link.issueId : link.projectId }, { pushUrl: true });
    }}>{data.title}</Button>}
    <div className="flex flex-wrap gap-2">
      <Button variant="outline" disabled={!data || busy} onClick={() => setEditing(!editing)}>Link existing work</Button>
      {data?.link && <Button variant="outline" disabled={busy} onClick={() => void save(null)}>Unlink work</Button>}
      <Button variant="ghost" disabled={busy} onClick={() => { setError(false); void read.mutate(); }}>Refresh linked work</Button>
    </div>
    {editing && data && <form className="space-y-2" onSubmit={event => {
      event.preventDefault();
      void save(kind === "issue" ? { kind, issueId: id.trim() } : { kind, projectId: id.trim(), workspaceId: workspaceId.trim() });
    }}>
      <label className="block">Work type <select value={kind} onChange={event => { setKind(event.target.value as typeof kind); setId(""); }}>
        <option value="issue">Issue</option><option value="workspace">Project workspace</option>
      </select></label>
      <label className="block">{kind === "issue" ? "Issue ID" : "Project ID"}<input className="w-full rounded border bg-background p-2" required maxLength={36} value={id} onChange={event => setId(event.target.value)} /></label>
      {kind === "workspace" && <label className="block">Workspace ID<input className="w-full rounded border bg-background p-2" required maxLength={36} value={workspaceId} onChange={event => setWorkspaceId(event.target.value)} /></label>}
      <Button type="submit" disabled={busy}>Save link</Button>
    </form>}
  </div>;
}
