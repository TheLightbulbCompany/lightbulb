"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { FileViewerContent, isPaneViewable, normalizeContentType } from "@/components/media/FileViewerContent";
import { DocumentRow } from "@/components/teams/issues/DocumentRow";
import { AttachmentIssueContext, issueAttachmentContentPath } from "@/components/teams/shared/lib/attachments";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { CompanyAgent, CompanyMember, IssueDocument } from "@/components/teams/shared/types";
import { useUser } from "@clerk/nextjs";
import { actorName } from "@/components/teams/shared/lib/actorName";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useApi } from "@/lib/api";
import { openPanel, updatePanelParams } from "../workspaceController";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { blobText } from "@/components/media/readBlob";
import { formatBytes } from "@/lib/utils";
import { docPreview } from "@/components/creations/docText";
import type { PanelProps } from "../panelComponents";
import { DocumentContents } from "./DocumentContents";
import { CONTENTS_MIN_HEADINGS, outlineOf } from "./documentOutline";
import { parseArtifactSource, type ArtifactSource } from "./artifactSource";

type Loaded = { status: "loading" } | { status: "ready"; blob: Blob; url: string; name: string; type: string } | { status: "error"; message: string };

function describeError(e: unknown): string {
  const status = (e as { status?: number })?.status;
  return status === 403 || status === 404 ? "You can't open this file." : "Could not load this file.";
}

/** The right pane for a file: a room attachment, a Paperclip artifact, or a workspace file. */
export function ArtifactDockPanel({ params }: PanelProps) {
  const source = useMemo(() => parseArtifactSource(params), [params]);
  if (!source) return <PaneFrame><p role="alert" className="p-4 text-ui">This link does not point at a file.</p></PaneFrame>;
  if (source.src === "file") return <OpenInFiles agent={source.agent} path={source.path} />;
  if (source.src === "paperclip" && source.kind === "document") return <DocumentPane source={source} />;
  return <FilePane key={`${source.src}:${source.id}`} source={source} />;
}

/** A workspace file is the existing Files pane's job: hand it over. */
function OpenInFiles({ agent, path }: { agent: string; path: string }) {
  useEffect(() => { openPanel("file-viewer", { agent, path }); }, [agent, path]);
  return null;
}

/** RightPane's bar already carries the title, Full screen and Close; this adds only the file's own actions. */
function PaneFrame({ actions, children }: { actions?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      {actions && <div className="flex items-center justify-end gap-2 border-b px-3 py-1" style={{ borderColor: "var(--edge)" }}>{actions}</div>}
      <div className="min-h-0 flex-1 overflow-auto">{children}</div>
    </div>
  );
}

type FileSource = Exclude<ArtifactSource, { src: "file" } | { kind: "document" }>;

function FilePane({ source }: { source: FileSource }) {
  const api = useApi();
  const teams = useTeamsApi();
  const [state, setState] = useState<Loaded>({ status: "loading" });
  // The clients may be new objects every render; the fetch must run once per source, not per render.
  const clients = useRef({ api, teams });
  useEffect(() => { clients.current = { api, teams }; });
  // Keyed on the source's content: an equal-but-new params object must not refetch or revoke the shown URL.
  const sourceKey = JSON.stringify(source);
  useEffect(() => {
    const source = JSON.parse(sourceKey) as FileSource;
    let live = true;
    let url: string | undefined;
    const { api, teams } = clients.current;
    const fetchBlob = source.src === "attachment"
      ? api.downloadBlob(`/messaging/rooms/${encodeURIComponent(source.room)}/attachments/${encodeURIComponent(source.id)}`)
      : teams.download(issueAttachmentContentPath(source.issue, source.id));
    fetchBlob.then((blob) => {
      if (!live) return;
      // The viewer type is what the server served, never the URL's `type` param: a link that
      // says "pdf" over HTML bytes would otherwise put the page in an unsandboxed iframe on our
      // origin. An untyped response previews nothing. The object URL is made from a re-typed
      // Blob so the bytes behind it carry exactly the type the viewer chose.
      const type = normalizeContentType(blob.type) || "application/octet-stream";
      const typed = new Blob([blob], { type });
      url = URL.createObjectURL(typed);
      setState({ status: "ready", blob: typed, url, name: source.name ?? "file", type });
    }).catch((e) => { if (live) setState({ status: "error", message: describeError(e) }); });
    return () => { live = false; if (url) URL.revokeObjectURL(url); };
  }, [sourceKey]);
  const superAgent = useSuperAgent();
  const text = state.status === "ready" && (state.type === "text/markdown" || state.type === "text/plain");
  // Super-agent: a text doc is titled by its first heading (RightPane reads `heading`; the file name is the line under).
  const blob = state.status === "ready" ? state.blob : null;
  useEffect(() => {
    if (!superAgent || !text || !blob) return;
    let live = true;
    void blobText(blob.slice(0, 4096)).then((t) => {
      const heading = docPreview(t, 0).heading;
      if (live && heading) updatePanelParams("artifact", { heading });
    }, () => undefined);
    return () => { live = false; };
  }, [superAgent, text, blob]);
  const download = state.status === "ready" ? <a className="inline-flex" href={state.url} download={state.name} aria-label="Download"><Download aria-hidden="true" /></a> : null;
  const body = <>
    {state.status === "loading" && <p className="p-4 text-ui text-muted-foreground">Loading…</p>}
    {state.status === "error" && <p role="alert" className="p-4 text-ui">{state.message}</p>}
    {state.status === "ready" && (isPaneViewable(state.type) || state.type.startsWith("image/") || state.type.startsWith("video/")
      ? <FileViewerContent item={{ fileName: state.name, contentType: state.type, size: state.blob.size, url: state.url, blob: state.blob }} className="h-full" />
      : <p className="p-4 text-ui text-muted-foreground">No preview for this file type. Use Download.</p>)}
  </>;
  if (!superAgent) return <PaneFrame actions={download}>{body}</PaneFrame>;
  // Super-agent: the file name and size as a quiet line with Download at its end, then the doc set as a document.
  return (
    <PaneFrame actions={<>
      <span className="doc-view__file" title={source.name ?? undefined}>{source.name ?? "File"}{state.status === "ready" ? ` · ${formatBytes(state.blob.size)}` : ""}</span>
      {download}
    </>}>
      {text ? <div className="doc-view">{body}</div> : body}
    </PaneFrame>
  );
}

/** The document view: title, who changed it and when, a contents list, and the reviewable row (comments, versions). */
function DocumentPane({ source }: { source: Extract<ArtifactSource, { kind: "document" }> }) {
  const { read } = useTeamsApi();
  const { data, error, mutate: refreshDoc } = read<IssueDocument>(`/issues/${encodeURIComponent(source.issue)}/documents/${encodeURIComponent(source.key)}`);
  const { data: agentsData } = read<{ agents: CompanyAgent[] }>("/agents");
  const { data: membersData } = read<{ members: CompanyMember[] }>("/members");
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const { user } = useUser();
  const [editing, setEditing] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const canEdit = (editing || !data?.lockedAt) && !previewing;
  const actions = <>
    {canEdit && <Button type="button" variant="outline" size="sm" aria-pressed={editing} onClick={() => setEditing((v) => !v)}>Edit</Button>}
    <Button type="button" variant="outline" size="sm" onClick={() => openPanel("teams-issue-detail", { detailId: source.issue }, { pushUrl: true })}>Open task</Button>
  </>;
  const agents = agentsData?.agents ?? [];
  const author = actorName({ agentId: data?.updatedByAgentId, userId: data?.updatedByUserId, agents, members: membersData?.members, currentUserId: user?.id });
  const when = data?.updatedAt ?? data?.createdAt;
  const outline = outlineOf(data?.body ?? "");
  return (
    <PaneFrame actions={actions}>
      {error && <p role="alert" className="p-4 text-ui">{describeError(error)}</p>}
      {data && <div className="mx-auto max-w-2xl p-4">
        <h2 className="text-lg font-semibold">{data.title || data.key}</h2>
        <p className="mb-3 text-caption text-muted-foreground">{author}{when ? ` · updated ${timeAgo(when)}` : ""} · rev {data.latestRevisionNumber ?? 1}{data.lockedAt ? ` · locked by ${actorName({ agentId: data.lockedByAgentId, agents })}` : ""}</p>
        {/* The outline is the latest body's; while a past revision is previewed its headings may differ, so the list hides. */}
        {!previewing && !editing && outline.length >= CONTENTS_MIN_HEADINGS && <DocumentContents entries={outline} bodyRef={bodyRef} />}
        <AttachmentIssueContext.Provider value={source.issue}>
          <div ref={bodyRef}><DocumentRow doc={data} agents={agents} issueId={source.issue} defaultOpen hideTitle editing={editing} onEditingChange={setEditing} onPreviewingChange={setPreviewing} onRestored={() => { void refreshDoc(); }} /></div>
        </AttachmentIssueContext.Provider>
      </div>}
    </PaneFrame>
  );
}
