"use client";

// A project's Files view (super-agent, UX slice 5): what its tasks made, plus the files people shared in its messages,
// in one table (Name · Made by · Date; "just now", "3m ago" as Tasks says it, W13). A type select narrows it; a row opens its thing, and ⋯ adds "Open its task"
// and "Copy link". A project that has made nothing shows a tip pinned to the foot, dismissed once per person.

import "./creations.css";
import { useMemo, useState, type ReactElement } from "react";
import { MoreHorizontal, X } from "lucide-react";
import { specialistLabel, type OutputKind } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import type { RoomMessage } from "@/components/messaging/types";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { KIND_FILTERS, openOutput, outputLink, outputName, type Openable } from "./openOutput";
import { iconOf } from "./OutputThumb";
import { useCreations } from "./useCreations";

type Row = { key: string; output: Openable; madeBy: string; at: string; shared: boolean };

const tipKey = (viewerId: string) => `lb:files-tip-dismissed:${viewerId}`;
function tipDismissed(viewerId: string): boolean {
  try { return window.localStorage.getItem(tipKey(viewerId)) === "1"; } catch { return false; }
}

export function ProjectFiles({ roomId, viewerId, messages, onOpenTask }: {
  roomId: string;
  viewerId: string;
  /** The loaded messages: their attachments are the files shared here. */
  messages: readonly RoomMessage[];
  onOpenTask: (taskId: string) => void;
}): ReactElement {
  const [type, setType] = useState("all");
  const kinds = KIND_FILTERS.find((f) => f.id === type)?.kinds;
  const filter = useMemo(() => ({ roomId, ...(kinds ? { kind: kinds } : {}) }), [roomId, kinds]);
  const { outputs, loading, error, retry, hasMore, loadingMore, loadMore } = useCreations(filter);
  const { agentNames } = useLiveEvents();
  const [tipGone, setTipGone] = useState(() => tipDismissed(viewerId));
  const [copied, setCopied] = useState<string | null>(null);

  // Files shared in messages read as files; they show under All and Files.
  const shared = useMemo<Row[]>(() => messages.flatMap((m) => m.deletedAt ? [] : (m.attachments ?? []).map((a) => ({
    key: `shared:${a.id}`, shared: true, at: m.createdAt, madeBy: `${m.author.displayName} (shared)`,
    output: { kind: "file" as OutputKind, title: a.fileName, taskId: "", ref: { roomId, attachmentId: a.id, fileName: a.fileName, contentType: a.contentType } },
  }))), [messages, roomId]);
  const rows: Row[] = [
    ...outputs.map((o) => ({ key: `${o.taskId}:${o.id}`, output: o, madeBy: specialistLabel(o.specialist, agentNames), at: o.createdAt, shared: false })),
    ...(!kinds || kinds.includes("file") ? shared : []),
  ];
  const nothingMade = !loading && !error && rows.length === 0 && type === "all";

  const copy = async (row: Row) => {
    try { await navigator.clipboard.writeText(outputLink(row.output)); setCopied(row.key); }
    catch { setCopied(null); }
  };

  return <div className="project-files">
    <div className="project-files__bar">
      <Select value={type} onValueChange={setType}>
        <SelectTrigger size="sm" className="creations__select" aria-label="Type"><SelectValue /></SelectTrigger>
        <SelectContent position="popper" align="start">
          {KIND_FILTERS.map((f) => <SelectItem key={f.id} value={f.id}>{f.id === "all" ? "All types" : f.label}</SelectItem>)}
        </SelectContent>
      </Select>
      {copied && <span role="status" className="project-files__status">Link copied</span>}
    </div>
    {error ? <p role="alert" className="project-files__note">Files couldn&apos;t load.{" "}
      <button type="button" className="creations__link" onClick={retry}>Retry</button></p>
      : loading ? <p role="status" className="project-files__note">Loading…</p>
      : rows.length === 0 ? (type === "all" ? null : <p className="project-files__note">None of these here yet.</p>)
      : <table className="project-files__table">
        <thead><tr><th scope="col">Name</th><th scope="col">Made by</th><th scope="col">Date</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
        <tbody>{rows.map((row) => {
          const open = () => openOutput(row.output, onOpenTask, "files");
          // The readable name the tiles use ("Tuesday pizza class family invite"), not the slug cut mid-word (W15-11).
          const name = outputName(row.output);
          return <tr key={row.key}>
            <td><button type="button" className="project-files__name" title={row.output.title} onClick={open}>
              {iconOf(row.output, 16)}<span>{name}</span>
            </button></td>
            <td className="project-files__muted">{row.madeBy}</td>
            <td className="project-files__muted"><time dateTime={row.at}>{timeAgo(row.at)}</time></td>
            <td className="project-files__act">
              <DropdownMenu>
                <DropdownMenuTrigger asChild><button type="button" className="project-files__more" aria-label={`More for ${name}`}>
                  <MoreHorizontal size={16} aria-hidden="true" /></button></DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuItem onSelect={open}>Open</DropdownMenuItem>
                  {!row.shared && <DropdownMenuItem onSelect={() => onOpenTask(row.output.taskId)}>Open its task</DropdownMenuItem>}
                  <DropdownMenuItem onSelect={() => void copy(row)}>Copy link</DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </td>
          </tr>;
        })}</tbody>
      </table>}
    {hasMore && !loading && !error && <Button type="button" variant="ghost" size="sm" className="justify-self-start" disabled={loadingMore}
      onClick={loadMore}>{loadingMore ? "Loading…" : "Load more"}</Button>}
    {nothingMade && !tipGone && <aside className="project-files__tip" aria-label="Tip">
      <p>Ask for a doc, an app or a file in any task. It lands here.</p>
      <button type="button" className="project-files__tip-close" aria-label="Dismiss tip" onClick={() => {
        setTipGone(true);
        try { window.localStorage.setItem(tipKey(viewerId), "1"); } catch { /* private window: dismissed for this visit */ }
      }}><X size={14} aria-hidden="true" /></button>
    </aside>}
  </div>;
}
