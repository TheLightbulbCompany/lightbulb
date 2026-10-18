"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { Download, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { FileViewerContent, isPaneViewable } from "@/components/media/FileViewerContent";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { roomMarkdown } from "@/components/messaging/roomMarkdown";
import { CardContext, type CardContextValue } from "@/components/messaging/openui/CardContext";
import { issueAttachmentContentPath } from "@/components/teams/shared/lib/attachments";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useApi } from "@/lib/api";
import { closePane, openPanel } from "../workspaceController";
import type { PanelProps } from "../panelComponents";
import { parseArtifactSource, type ArtifactSource } from "./artifactSource";

type Loaded = { status: "loading" } | { status: "ready"; blob: Blob; url: string; name: string; type: string } | { status: "error"; message: string };

function describeError(e: unknown): string {
  const status = (e as { status?: number })?.status;
  return status === 403 || status === 404 ? "You can't open this file." : "Could not load this file.";
}

/** The right pane for a file: a room attachment, a Paperclip artifact, or a workspace file. */
export function ArtifactDockPanel({ params }: PanelProps) {
  const source = useMemo(() => parseArtifactSource(params), [params]);
  if (!source) return <PaneFrame title="File"><p role="alert" className="p-4 text-ui">This link does not point at a file.</p></PaneFrame>;
  if (source.src === "file") return <OpenInFiles agent={source.agent} path={source.path} />;
  if (source.src === "paperclip" && source.kind === "document") return <DocumentPane source={source} />;
  return <FilePane key={`${source.src}:${source.id}`} source={source} />;
}

/** A workspace file is the existing Files pane's job: hand it over. */
function OpenInFiles({ agent, path }: { agent: string; path: string }) {
  useEffect(() => { openPanel("file-viewer", { agent, path }); }, [agent, path]);
  return null;
}

function PaneFrame({ title, actions, children }: { title: string; actions?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="flex h-full min-h-0 flex-col">
      <header className="flex items-center gap-2 border-b px-3 py-2" style={{ borderColor: "var(--edge)" }}>
        <h2 className="min-w-0 flex-1 truncate text-ui font-semibold">{title}</h2>
        {actions}
        <Button type="button" variant="ghost" size="icon-sm" aria-label="Close" onClick={closePane}><X aria-hidden="true" /></Button>
      </header>
      <div className="min-h-0 flex-1 overflow-auto">{children}</div>
    </section>
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
      ? api.downloadBlob(`/messaging/rooms/${source.room}/attachments/${encodeURIComponent(source.id)}`)
      : teams.download(issueAttachmentContentPath(source.issue, source.id));
    fetchBlob.then((blob) => {
      if (!live) return;
      url = URL.createObjectURL(blob);
      // The viewer matches content types exactly; a fetched blob's may carry "; charset=…".
      const type = source.type ?? (blob.type.split(";")[0] || "application/octet-stream");
      setState({ status: "ready", blob, url, name: source.name ?? "file", type });
    }).catch((e) => { if (live) setState({ status: "error", message: describeError(e) }); });
    return () => { live = false; if (url) URL.revokeObjectURL(url); };
  }, [sourceKey]);
  const title = source.name ?? "File";
  const download = state.status === "ready" ? <a className="inline-flex" href={state.url} download={state.name} aria-label="Download"><Download aria-hidden="true" /></a> : null;
  return (
    <PaneFrame title={title} actions={download}>
      {state.status === "loading" && <p className="p-4 text-ui text-muted-foreground">Loading…</p>}
      {state.status === "error" && <p role="alert" className="p-4 text-ui">{state.message}</p>}
      {state.status === "ready" && (isPaneViewable(state.type) || state.type.startsWith("image/") || state.type.startsWith("video/")
        ? <FileViewerContent item={{ fileName: state.name, contentType: state.type, size: state.blob.size, url: state.url, blob: state.blob }} className="h-full" />
        : <p className="p-4 text-ui text-muted-foreground">No preview for this file type. Use Download.</p>)}
    </PaneFrame>
  );
}

/** The minimal document view (Track C adds the header details, contents list, comments and editing). */
function DocumentPane({ source }: { source: Extract<ArtifactSource, { kind: "document" }> }) {
  const { read } = useTeamsApi();
  const { data, error } = read<{ id: string; key: string; title?: string | null; body?: string | null }>(`/issues/${source.issue}/documents/${source.key}`);
  const title = data?.title?.trim() || source.key;
  const openTask = <Button type="button" variant="outline" size="sm" onClick={() => openPanel("teams-issue-detail", { detailId: source.issue }, { pushUrl: true })}>Open task</Button>;
  // Cards and diagrams in a document render read-only: a provider with no press.
  const messageId = data?.id ?? source.key;
  const readOnly: CardContextValue = {
    messageId, roomId: "", authorId: "", threadRootMessageId: null, attachments: [],
    answered: null, pending: false, error: null, retry: null,
  };
  return (
    <PaneFrame title={title} actions={openTask}>
      {error && <p role="alert" className="p-4 text-ui">{describeError(error)}</p>}
      {data && <div className="mx-auto max-w-2xl p-4">
        <CardContext.Provider value={readOnly}><MarkdownView skipHtml components={roomMarkdown}>{data.body ?? ""}</MarkdownView></CardContext.Provider></div>}
    </PaneFrame>
  );
}
