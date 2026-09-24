"use client";

import { useState } from "react";
import type { ActivityAction, ActivityItem, ActivityLinkKind } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";

export interface ActivityTarget { kind: ActivityLinkKind; id: string; roomId: string | null }
export interface ActivityPerson { value: string; label: string }

export interface ActivityFeedProps {
  items: readonly ActivityItem[];
  loading: boolean;
  error: boolean;
  /** False when the Paperclip half of the feed could not be read this time. */
  paperclipAvailable: boolean;
  hasMore: boolean;
  onLoadMore: () => void;
  onRunAction: (itemId: string, actionId: string, request: { note?: string; value?: string }) => Promise<unknown>;
  onNavigate: (target: ActivityTarget) => void;
  /** Workspace humans and agents, for a `person` input. The server validates the pick. */
  people: readonly ActivityPerson[];
  onOpenCompanyActivity: () => void;
}

/**
 * The viewer's Activity feed. Everything it shows is server-rendered -- `summary`
 * and `actions` -- so this renders generically and never composes text per kind.
 */
export function ActivityFeed(props: ActivityFeedProps) {
  const { items, loading, error, paperclipAvailable, hasMore, onLoadMore, onOpenCompanyActivity } = props;
  return <section className="flex h-full flex-col overflow-y-auto" aria-label="Activity">
    <header className="flex items-center gap-2 border-b px-4 py-3" style={{ borderColor: "var(--edge)" }}>
      <h2 className="font-semibold">Activity</h2>
      <button type="button" className="ml-auto text-xs underline-offset-2 hover:underline" style={{ color: "var(--midnight-muted)" }}
        onClick={onOpenCompanyActivity}>Company activity</button>
    </header>
    {!paperclipAvailable && <p role="status" className="px-4 pt-3 text-xs" style={{ color: "var(--midnight-muted)" }}>
      Company approvals and tasks couldn&apos;t load right now. Messages below are up to date.
    </p>}
    {error && items.length === 0
      ? <p role="alert" className="p-4 text-sm" style={{ color: "var(--err)" }}>Couldn&apos;t load your activity. Try again in a moment.</p>
      : loading && items.length === 0
        ? <p role="status" className="p-4 text-sm" style={{ color: "var(--midnight-muted)" }}>Loading activity…</p>
        : items.length === 0
          ? <p className="p-4 text-sm" style={{ color: "var(--midnight-muted)" }}>Nothing needs you right now.</p>
          : <ul className="flex flex-col">{items.map((item) => <ActivityRow key={item.id} item={item} {...props} />)}</ul>}
    {hasMore && <div className="p-4"><Button variant="ghost" size="sm" onClick={onLoadMore} disabled={loading}>Show older</Button></div>}
  </section>;
}

function ActivityRow({ item, onRunAction, onNavigate, people }: { item: ActivityItem } & ActivityFeedProps) {
  const [open, setOpen] = useState<ActivityAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const resolved = item.state !== "open";
  const { summary } = item;

  const run = async (action: ActivityAction, request: { note?: string; value?: string } = {}) => {
    setBusy(true);
    setFailure(null);
    try {
      await onRunAction(item.id, action.id, request);
      setOpen(null);
    } catch (e) {
      setFailure(e instanceof Error && e.message ? e.message : "That didn't go through. Try again.");
    } finally {
      setBusy(false);
    }
  };
  const press = (action: ActivityAction) => {
    if (action.navigate) return onNavigate(action.navigate);
    if (action.input || action.confirm) return setOpen(open?.id === action.id ? null : action);
    void run(action);
  };

  return <li className="flex flex-col gap-2 border-b px-4 py-3" style={{ borderColor: "var(--edge)", opacity: resolved ? 0.7 : 1 }}
    data-activity-kind={item.kind}>
    <div className="flex items-baseline gap-2">
      {!item.read && <span aria-label="Unread" className="h-2 w-2 shrink-0 rounded-full" style={{ background: "var(--bulb)" }} />}
      <p className={item.read ? "text-sm" : "text-sm font-semibold"} style={{ color: "var(--midnight)" }}>{summary.title}</p>
    </div>
    <p className="text-xs" style={{ color: "var(--midnight-muted)" }}>
      {summary.requester.name} · <time dateTime={item.createdAt} title={new Date(item.createdAt).toLocaleString()}>{timeAgo(item.createdAt)}</time>
    </p>
    {summary.reason && <p className="text-sm" style={{ color: "var(--midnight)" }}>{summary.reason}</p>}
    {summary.preview && <Preview preview={summary.preview} />}
    {summary.links.length > 0 && <div className="flex flex-wrap gap-2">
      {summary.links.map((link) => <button key={`${link.kind}:${link.id}`} type="button" className="text-xs underline underline-offset-2"
        style={{ color: "var(--midnight)" }} onClick={() => onNavigate({ kind: link.kind, id: link.id, roomId: item.roomId })}>{link.label}</button>)}
    </div>}
    {resolved
      ? <p className="text-xs" style={{ color: "var(--midnight-muted)" }}>
        {item.state === "done" ? "Done" : "Resolved"}{item.resolvedBy ? ` by ${item.resolvedBy.name}` : ""}{item.resolvedAt ? ` · ${timeAgo(item.resolvedAt)}` : ""}
      </p>
      : item.actions.length > 0 && <div className="flex flex-wrap gap-2">
        {item.actions.map((action) => <Button key={action.id} size="sm" disabled={busy}
          variant={action.style === "primary" ? "default" : action.style === "destructive" ? "destructive" : "outline"}
          aria-expanded={action.input || action.confirm ? open?.id === action.id : undefined}
          onClick={() => press(action)}>{action.label}</Button>)}
      </div>}
    {/* The input rides as both fields: the server reads `note` for a decision and `value`
        for a reply, status or person, so the client keeps no per-action table. */}
    {open && !resolved && <ActionForm action={open} busy={busy} people={people} onCancel={() => setOpen(null)}
      onSubmit={(value) => void run(open, value === undefined ? {} : { note: value, value })} />}
    {failure && <p role="alert" className="text-xs" style={{ color: "var(--err)" }}>{failure}</p>}
  </li>;
}

function Preview({ preview }: { preview: NonNullable<ActivityItem["summary"]["preview"]> }) {
  const [expanded, setExpanded] = useState(false);
  const long = (preview.text?.length ?? 0) > 280;
  return <div className="flex flex-col gap-1 rounded-md border p-3" style={{ borderColor: "var(--edge)", background: "var(--cream)" }}>
    {preview.destination && <p className="text-xs" style={{ color: "var(--midnight-muted)" }}>To: {preview.destination}</p>}
    {preview.text && <p className="whitespace-pre-wrap text-sm" style={{ color: "var(--midnight)" }}>
      {long && !expanded ? `${preview.text.slice(0, 280)}…` : preview.text}
    </p>}
    {long && <button type="button" className="self-start text-xs underline underline-offset-2" onClick={() => setExpanded(!expanded)}>
      {expanded ? "Show less" : "Show more"}
    </button>}
    {preview.fields.length > 0 && <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
      {preview.fields.map((field) => <div key={field.label} className="contents">
        <dt style={{ color: "var(--midnight-muted)" }}>{field.label}</dt><dd style={{ color: "var(--midnight)" }}>{field.value}</dd>
      </div>)}
    </dl>}
  </div>;
}

function ActionForm({ action, busy, people, onCancel, onSubmit }: {
  action: ActivityAction; busy: boolean; people: readonly ActivityPerson[]; onCancel: () => void; onSubmit: (value?: string) => void;
}) {
  const [value, setValue] = useState("");
  const input = action.input;
  const options = input?.kind === "person" ? people : input?.options ?? [];
  const missing = Boolean(input?.required) && !value.trim();
  return <form className="flex flex-col gap-2" onSubmit={(e) => { e.preventDefault(); if (!missing) onSubmit(input ? value.trim() || undefined : undefined); }}>
    {action.confirm && <p className="text-sm" style={{ color: "var(--midnight)" }}>{action.confirm}</p>}
    {input && (input.kind === "text"
      ? <Textarea aria-label={input.label} placeholder={input.label} value={value} onChange={(e) => setValue(e.target.value)} rows={3} autoFocus />
      : <select aria-label={input.label} value={value} onChange={(e) => setValue(e.target.value)} autoFocus
        className="rounded-md border px-2 py-1.5 text-sm" style={{ borderColor: "var(--edge)", background: "var(--cream)" }}>
        <option value="">{input.label}</option>
        {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>)}
    <div className="flex gap-2">
      <Button type="submit" size="sm" disabled={busy || missing} variant={action.style === "destructive" ? "destructive" : "default"}>{action.label}</Button>
      <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={busy}>Cancel</Button>
    </div>
  </form>;
}
