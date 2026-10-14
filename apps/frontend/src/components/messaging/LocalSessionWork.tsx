"use client";

import { useEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { Button } from "@/components/ui/button";
import { openPanel } from "@/components/workspace/workspaceController";
import { WorkPicker, type PickedWork } from "./WorkPicker";

type Link = { kind: "issue"; companyId: string; issueId: string } | { kind: "workspace"; companyId: string; projectId: string; workspaceId: string };
type Work = { state: "empty" | "linked" | "unavailable"; link: Link | null; revision: string | null; title?: string };
type Props = { sessionId: string };

/** Mounted only while its dialog is open, so protected work is fetched on explicit open and dropped on close. */
export function LocalSessionWork(props: Props) {
  const { userId, orgId, isLoaded } = useAuth();
  // Auth/selection change unmounts all protected display and in-flight form state.
  return isLoaded && userId && orgId
    ? <WorkDetails key={JSON.stringify([userId, orgId, props.sessionId])} {...props} />
    : <p>Linked work unavailable.</p>;
}

function WorkDetails({ sessionId }: Props) {
  const api = useTeamsApi();
  const path = `/local-sessions/${encodeURIComponent(sessionId)}/work-link`;
  const read = api.read<Work>(path, { revalidateOnMount: true, dedupingInterval: 0, refreshInterval: 30_000, shouldRetryOnError: false });
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  // Never display cached protected data during validation or after an error.
  const data = !read.error && !read.isValidating && !error && !busy ? read.data : undefined;
  async function save(link: PickedWork | null) {
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
  return <div className="space-y-2 text-ui">
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
    {editing && data && <WorkPicker disabled={busy} onPick={link => void save(link)} />}
  </div>;
}
