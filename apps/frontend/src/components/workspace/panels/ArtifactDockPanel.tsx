"use client";

import { useContext, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronRight, Copy, Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { FileViewerContent, isPaneViewable, normalizeContentType } from "@/components/media/FileViewerContent";
import { issueAttachmentContentPath } from "@/components/teams/shared/lib/attachments";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useApi } from "@/lib/api";
import { openPanel, openTaskPane, updatePanelParams, useWorkspaceState } from "../workspaceController";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";
import { roomDisplayName } from "@lightbulb/shared";
import { blobText } from "@/components/media/readBlob";
import { DOCX } from "@/components/media/DocxPreview";
import { formatBytes } from "@/lib/utils";
import { docPreview, fileTypeWord, plainCta, resultPiece } from "@/components/creations/docText";
import { stripMetaLines } from "@/lib/ctaLinks";
import type { PanelProps } from "../panelComponents";
import { parseArtifactSource, type ArtifactSource } from "./artifactSource";
import { PaneActionsHost } from "../PaneHeader";

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
  return <FilePane key={source.src === "text" ? `text:${source.task}:${source.n}` : `${source.src}:${source.id}`} source={source}
    task={params.task ?? null} from={params.from ?? null} />;
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

/** A short result's words as a markdown doc: attempt n's answer, its deliverable part (as chat's Copy has it). */
async function textResult(tasks: ReturnType<typeof useLiveEvents>["tasks"], taskId: string, n: string): Promise<Blob> {
  const detail = await tasks.get(taskId);
  const text = detail.attempts.find((a) => String(a.n) === n)?.output ?? "";
  if (!text.trim()) throw Object.assign(new Error("no result"), { status: 404 });
  return new Blob([resultPiece(text) ?? text.trim()], { type: "text/markdown" });
}

function FilePane({ source, task, from }: { source: FileSource; task: string | null; from: string | null }) {
  const api = useApi();
  const teams = useTeamsApi();
  const { tasks } = useLiveEvents();
  const [state, setState] = useState<Loaded>({ status: "loading" });
  // The clients may be new objects every render; the fetch must run once per source, not per render.
  const clients = useRef({ api, teams, tasks });
  useEffect(() => { clients.current = { api, teams, tasks }; });
  // Keyed on the source's content: an equal-but-new params object must not refetch or revoke the shown URL.
  const sourceKey = JSON.stringify(source);
  const fromTask = !!task;
  useEffect(() => {
    const source = JSON.parse(sourceKey) as FileSource;
    let live = true;
    let url: string | undefined;
    const { api, teams, tasks } = clients.current;
    const fetchBlob = source.src === "text" ? textResult(tasks, source.task, source.n)
      : source.src === "attachment"
      ? api.downloadBlob(`/messaging/rooms/${encodeURIComponent(source.room)}/attachments/${encodeURIComponent(source.id)}`)
      : teams.download(issueAttachmentContentPath(source.issue, source.id));
    // A task's text output drops an email's "Preview text:" lines (W18): its viewer and Copy read the email alone.
    // A file someone uploaded is shown as it is. (A short text result is already cut by `resultPiece`.)
    const shown = fromTask && source.src !== "text"
      ? fetchBlob.then(async (blob) => /^text\/(markdown|plain)\b/.test(blob.type) ? new Blob([stripMetaLines(await blobText(blob))], { type: blob.type }) : blob)
      : fetchBlob;
    shown.then((blob) => {
      if (!live) return;
      // The viewer type is what the server served, never the URL's `type` param: a link that
      // says "pdf" over HTML bytes would otherwise put the page in an unsandboxed iframe on our
      // origin. An untyped response previews nothing. The object URL is made from a re-typed
      // Blob so the bytes behind it carry exactly the type the viewer chose.
      const type = normalizeContentType(blob.type) || "application/octet-stream";
      const typed = new Blob([blob], { type });
      url = URL.createObjectURL(typed);
      // A text result downloads as "<its name>.md".
      const name = source.src === "text" ? `${(source.name ?? "Result").replace(/[\\/:*?"<>|]+/g, " ").trim() || "Result"}.md` : source.name ?? "file";
      setState({ status: "ready", blob: typed, url, name, type });
    }).catch((e) => { if (live) setState({ status: "error", message: describeError(e) }); });
    return () => { live = false; if (url) URL.revokeObjectURL(url); };
  }, [sourceKey, fromTask]);
  const text = state.status === "ready" && (state.type === "text/markdown" || state.type === "text/plain");
  // A text doc is titled by its first heading (RightPane reads `heading`; the file name is the line under).
  // The heading names the doc it came from (`headingOf`): a slow read of one doc never titles the next.
  const blob = state.status === "ready" ? state.blob : null;
  const docId = source.src === "text" ? undefined : source.id;
  useEffect(() => {
    if (!text || !blob || !docId) return;
    let live = true;
    void blobText(blob.slice(0, 4096)).then((t) => {
      const heading = docPreview(t, 0).heading;
      if (live && heading) updatePanelParams("artifact", { heading, headingOf: docId });
    }, () => undefined);
    return () => { live = false; };
  }, [text, blob, docId]);
  const body = <>
    {state.status === "loading" && <p className="p-4 text-ui text-muted-foreground">Loading…</p>}
    {state.status === "error" && <p role="alert" className="p-4 text-ui">{state.message}</p>}
    {state.status === "ready" && (isPaneViewable(state.type) || state.type.startsWith("image/") || state.type.startsWith("video/")
      ? <FileViewerContent item={{ fileName: state.name, contentType: state.type, size: state.blob.size, url: state.url, blob: state.blob }} className="h-full" />
      // W18-10: say what it is ("Word file") and make Download the way in, not a dead end.
      : <div className="grid justify-items-start gap-3 p-4 text-ui">
        <p className="text-muted-foreground">{fileTypeWord(state.name) ? `${fileTypeWord(state.name)} file. ` : ""}It doesn&apos;t preview here.</p>
        <Button asChild size="sm"><a href={state.url} download={state.name}><Download aria-hidden="true" />Download</a></Button>
      </div>)}
  </>;
  // Copy (a text doc) and Download sit in the pane's header beside Expand and ✕, no row of their own
  // (W14); the file name and size are Download's label. Then the doc set as a document.
  return <>
    {state.status === "ready" && <HeaderActions>
      {text && <CopyText blob={state.blob} />}
      <a className="pane-header__icon" href={state.url} download={state.name}
        aria-label={`Download ${state.name} (${formatBytes(state.blob.size)})`} title={`Download ${state.name} · ${formatBytes(state.blob.size)}`}>
        <Download size={16} aria-hidden="true" />
      </a>
    </HeaderActions>}
    <PaneFrame>
      <DocOrigin task={task} from={from} />
      {text || (state.status === "ready" && state.type === DOCX) ? <div className="doc-view">{body}</div> : body}
    </PaneFrame>
  </>;
}

/** Where a doc came from: its channel when that isn't the one on screen (a ⌘K or Creations open), and its task. */
function DocOrigin({ task, from }: { task: string | null; from: string | null }) {
  const { place, chatParams } = useWorkspaceState();
  const room = useMessagingWorkspaceOptional()?.directoryProps?.rooms.find((r) => r.roomId === from);
  const here = place === "chat" && chatParams.roomId === from;
  const channel = room && !here ? `${room.kind === "channel" ? "#" : ""}${roomDisplayName(room, room.participants)}` : null;
  if (!task && !channel) return null;
  return <p className="doc-origin">
    {channel && <span className="doc-origin__channel">From {channel}</span>}
    {task && <button type="button" className="doc-origin__task" onClick={() => openTaskPane(task)}>View thread<ChevronRight size={14} aria-hidden="true" /></button>}
  </p>;
}

/** Into the drawn pane header's action slot (RightPane); nowhere when there is none. */
function HeaderActions({ children }: { children: React.ReactNode }) {
  const host = useContext(PaneActionsHost);
  return host ? createPortal(children, host) : null;
}

/** Copy a text doc's words: the icon turns to a check for a moment. */
function CopyText({ blob }: { blob: Blob }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(timer);
  }, [copied]);
  return <button type="button" className="pane-header__icon" aria-label={copied ? "Copied" : "Copy text"} title={copied ? "Copied" : "Copy text"}
    onClick={() => void blobText(blob).then((t) => navigator.clipboard.writeText(plainCta(t))).then(() => setCopied(true), () => undefined)}>
    {copied ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}
  </button>;
}
