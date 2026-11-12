"use client";

import { useEffect, useState } from "react";
import { answerIsTableOf, tableReply } from "@lightbulb/shared";
import { Button as UiButton } from "@/components/ui/button";
import { Input as UiInput } from "@/components/ui/input";
import { Table as UiTable, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { safeHref } from "@/lib/safeHref";
import { appLinkTarget } from "../../appLinks";
import { useMessagingWorkspaceOptional } from "../../MessagingWorkspaceContext";
import { openAppLink } from "../../RoomAppLinks";
import { useCardContext } from "../CardContext";
import { useCardPress } from "./shared";
import type { RenderProps, SubComponent } from "./shared";

export function CalloutView({ props }: RenderProps<{ text: string; tone?: "info" | "warn" | "good" }>) {
  const color = props.tone === "warn" ? "var(--alert)" : props.tone === "good" ? "var(--live)" : "var(--link)";
  return <p role="note" className="text-ui rounded-sm px-3 py-2" style={{ borderLeft: `3px solid ${color}`, background: "var(--plane-pane)" }}>{props.text}</p>;
}

export function MetricView({ props }: RenderProps<{ label: string; value: string; delta?: string }>) {
  return (
    <div className="flex flex-col">
      <span className="text-caption text-muted-foreground">{props.label}</span>
      <span className="text-xl font-semibold">{props.value}</span>
      {props.delta && <span className="text-caption" style={{ color: "var(--fg-soft)" }}>{props.delta}</span>}
    </div>
  );
}

type ColProps = { label: string; data: (string | number)[] };

export function TableView({ props }: RenderProps<{ columns: SubComponent<ColProps>[] }>) {
  const cols = props.columns.map((c) => c.props);
  const rows = Math.max(0, ...cols.map((c) => c.data.length));
  return (
    <UiTable>
      <TableHeader><TableRow>{cols.map((c, i) => <TableHead key={i}>{c.label}</TableHead>)}</TableRow></TableHeader>
      <TableBody>
        {Array.from({ length: rows }, (_, r) => (
          <TableRow key={r}>{cols.map((c, i) => <TableCell key={i}>{c.data[r] === undefined ? "" : String(c.data[r])}</TableCell>)}</TableRow>
        ))}
      </TableBody>
    </UiTable>
  );
}

/** Col never renders on its own: Table reads its props. */
export function ColView() { return null; }

export function EditableTableView({ props }: RenderProps<{ columns: string[]; rows: string[][]; submit?: string }>) {
  const { press, disabled, answered, retry } = useCardPress();
  const [rows, setRows] = useState<string[][]>(() => props.rows.map((r) => props.columns.map((_, i) => r[i] ?? "")));
  const edit = (r: number, c: number, value: string) => setRows((cur) => cur.map((row, ri) => ri === r ? row.map((cell, ci) => ci === c ? value : cell) : row));
  return (
    <form className="grid gap-2 openui-live" onSubmit={(e) => { e.preventDefault(); press(tableReply(props.columns, rows)); }}>
      <UiTable>
        <TableHeader><TableRow>{props.columns.map((c) => <TableHead key={c}>{c}</TableHead>)}</TableRow></TableHeader>
        <TableBody>
          {rows.map((row, r) => <TableRow key={r}>{row.map((cell, c) => <TableCell key={c}>
            <UiInput aria-label={`${props.columns[c]}, row ${r + 1}`} value={cell} disabled={disabled || retry !== null} onChange={(e) => edit(r, c, e.target.value)} />
          </TableCell>)}</TableRow>)}
        </TableBody>
      </UiTable>
      {answerIsTableOf(answered, props.columns)
        ? <p className="text-caption openui-answer">Sent.</p>
        : <UiButton type="submit" size="sm" disabled={disabled} className="justify-self-start">{props.submit ?? "Submit"}</UiButton>}
    </form>
  );
}

export function StepsView({ props }: RenderProps<{ steps: { label: string; status?: "todo" | "doing" | "done" }[] }>) {
  return (
    <ol className="grid gap-1 text-ui" aria-label="Steps">
      {props.steps.map((s, i) => {
        const status = s.status ?? "todo";
        return <li key={i} data-status={status} className="flex items-center gap-2">
          <span aria-hidden="true" className="inline-block size-2 rounded-full" style={{ background: status === "done" ? "var(--live)" : status === "doing" ? "var(--link)" : "var(--edge)" }} />
          <span className={status === "done" ? "text-muted-foreground line-through" : undefined}>{s.label}</span>
          <span className="sr-only">{status}</span>
        </li>;
      })}
    </ol>
  );
}

export function SourcesView({ props }: RenderProps<{ sources: { title: string; url: string }[] }>) {
  const ws = useMessagingWorkspaceOptional();
  return (
    <ul className="grid gap-1 text-caption" aria-label="Sources">
      {props.sources.map((s, i) => {
        const link = appLinkTarget(s.url);
        if (link && link.href.startsWith("/chat")) {
          return <li key={i}><a href={link.href} className="text-info underline underline-offset-4" onClick={(e) => openAppLink(e, link, ws)}>{s.title}</a></li>;
        }
        const href = safeHref(s.url);
        return <li key={i}>{href
          ? <a href={href} target="_blank" rel="noopener noreferrer" className="text-info underline underline-offset-4">{s.title}</a>
          : <span className="text-muted-foreground">{s.title}</span>}</li>;
      })}
    </ul>
  );
}

/** Only this message's own attachment, by file name. Remote images never load in a room. */
export function ImageView({ props }: RenderProps<{ fileName: string; alt: string }>) {
  const ctx = useCardContext();
  const attachment = ctx?.attachments.find((a) => a.fileName === props.fileName && a.contentType.startsWith("image/"));
  // Keyed on ids, not objects: a re-render with a fresh context must not refetch the URL.
  // The URL is stored with its attachment id, so a changed attachment reads as "no URL yet".
  const [loaded, setLoaded] = useState<{ id: string; url: string } | null>(null);
  const attachmentId = attachment?.id;
  const roomId = ctx?.roomId;
  const attachmentUrl = ctx?.attachmentUrl;
  useEffect(() => {
    let live = true;
    if (attachmentId && roomId && attachmentUrl) void attachmentUrl(roomId, attachmentId).then((r) => { if (live) setLoaded({ id: attachmentId, url: r.url }); }).catch(() => {});
    return () => { live = false; };
  }, [attachmentId, roomId, attachmentUrl]);
  const url = loaded && loaded.id === attachmentId ? loaded.url : null;
  if (!attachment || !url) return <span className="text-muted-foreground text-caption">{props.alt}</span>;
  // eslint-disable-next-line @next/next/no-img-element -- a presigned attachment URL, not a page asset
  return <img src={url} alt={props.alt} className="max-w-full rounded-sm" />;
}
