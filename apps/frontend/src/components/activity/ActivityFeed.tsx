"use client";

import "./activity.css";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ACTIVITY_EMPTY_COPY,
  ACTIVITY_FILTERS,
  activityRowParts,
  filterActivity,
  groupActivity,
  isNeedsYou,
  type ActivityAction,
  type ActivityFilter,
  type ActivityItem,
  type ActivityLinkKind,
} from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Identity } from "@/components/teams/shared/components/Identity";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { agentTint } from "@/lib/agentTint";

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
  /** The chip to open on (a deep link's `filter`); the user can change it. */
  initialFilter?: ActivityFilter;
  /** An item a notification points at: scrolled to and highlighted once it loads. */
  focusId?: string | null;
}

/** Where a row click goes: its own `navigate` action, else its message. */
export function rowTarget(item: ActivityItem): ActivityTarget | null {
  const action = item.actions.find((entry) => entry.navigate)?.navigate;
  if (action) return action;
  if (!item.roomId) return null;
  return item.threadRootMessageId
    ? { kind: "thread", id: item.threadRootMessageId, roomId: item.roomId }
    : { kind: "room", id: item.roomId, roomId: item.roomId };
}

function typingInField(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
}

/**
 * The viewer's Activity, Slack-shaped: filter chips, "Needs you" pinned with its
 * decisions inline, then Today / Earlier as dense rows whose quick actions show
 * on hover or focus. Every sentence and button is the server's (`summary`,
 * `actions`); this only arranges them. j / k move between rows, Enter opens one.
 */
export function ActivityFeed(props: ActivityFeedProps) {
  const { items, loading, error, paperclipAvailable, hasMore, onLoadMore, onOpenCompanyActivity, focusId } = props;
  const [filter, setFilter] = useState<ActivityFilter>(props.initialFilter ?? "all");
  const [active, setActive] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // A deep link's filter wins when it changes (a second click on the Tasks entry).
  const [filterProp, setFilterProp] = useState(props.initialFilter);
  if (props.initialFilter !== filterProp) {
    setFilterProp(props.initialFilter);
    if (props.initialFilter) setFilter(props.initialFilter);
  }
  // A focused item (from a notification) is selected once it loads, even under another chip.
  const focusLoaded = Boolean(focusId && items.some((item) => item.id === focusId));
  const [focusedFor, setFocusedFor] = useState<string | null>(null);
  if (focusLoaded && focusId && focusId !== focusedFor) {
    setFocusedFor(focusId);
    setActive(focusId);
    if (!filterActivity(items, filter).some((item) => item.id === focusId)) setFilter("all");
  }

  const visible = useMemo(() => filterActivity(items, filter), [items, filter]);
  const sections = useMemo(() => groupActivity(visible), [visible]);
  const order = useMemo(() => sections.flatMap((section) => section.items.map((item) => item.id)), [sections]);
  const needsYouCount = useMemo(() => items.filter(isNeedsYou).length, [items]);

  useEffect(() => {
    if (!focusedFor) return;
    listRef.current?.querySelector<HTMLElement>(`[data-activity-id="${CSS.escape(focusedFor)}"]`)
      ?.scrollIntoView?.({ block: "center" });
  }, [focusedFor]);

  const focusRow = (id: string) => {
    setActive(id);
    listRef.current?.querySelector<HTMLElement>(`[data-activity-id="${CSS.escape(id)}"]`)?.focus();
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (typingInField(event.target) || event.metaKey || event.ctrlKey || event.altKey || order.length === 0) return;
    const at = active ? order.indexOf(active) : -1;
    if (event.key === "j" || event.key === "ArrowDown") {
      event.preventDefault();
      focusRow(order[Math.min(order.length - 1, at + 1)]);
    } else if (event.key === "k" || event.key === "ArrowUp") {
      event.preventDefault();
      focusRow(order[Math.max(0, at - 1)]);
    } else if (event.key === "Enter" && event.target instanceof HTMLElement && event.target.dataset.activityId) {
      const id = event.target.dataset.activityId;
      const item = items.find((entry) => entry.id === id);
      const target = item && rowTarget(item);
      if (target) {
        event.preventDefault();
        props.onNavigate(target);
      }
    }
  };

  return <section className="activity-feed" aria-label="Activity">
    <header className="activity-feed__header">
      <h2>Activity</h2>
      <button type="button" className="activity-feed__link" onClick={onOpenCompanyActivity}>Company activity</button>
    </header>
    <div className="activity-feed__chips" role="toolbar" aria-label="Filter activity">
      {ACTIVITY_FILTERS.map((chip) => <button key={chip.id} type="button" aria-pressed={filter === chip.id}
        className="activity-chip" onClick={() => setFilter(chip.id)}>
        {chip.label}
        {chip.id === "needs-you" && needsYouCount > 0 && <span className="activity-chip__count">{needsYouCount}</span>}
      </button>)}
    </div>
    {!paperclipAvailable && <p role="status" className="activity-feed__notice">
      Company approvals and tasks couldn&apos;t load right now. Messages below are up to date.
    </p>}
    <div ref={listRef} className="activity-feed__list" onKeyDown={onKeyDown}>
      {error && items.length === 0
        ? <p role="alert" className="activity-feed__empty" style={{ color: "var(--err)" }}>Couldn&apos;t load your activity. Try again in a moment.</p>
        : loading && items.length === 0
          ? <p role="status" className="activity-feed__empty">Loading activity…</p>
          : sections.length === 0
            ? <p className="activity-feed__empty">{ACTIVITY_EMPTY_COPY[filter]}</p>
            : sections.map((section) => <section key={section.key} aria-labelledby={`activity-${section.key}`}
              className="activity-section" data-section={section.key}>
              <h3 id={`activity-${section.key}`} className="activity-section__title">
                {section.title}
                {section.key === "needs-you" && <span className="activity-section__count">{section.items.length}</span>}
              </h3>
              <ul>
                {section.items.map((item) => <ActivityRow key={item.id} item={item} pinned={section.key === "needs-you"}
                  tabIndex={(active ?? order[0]) === item.id ? 0 : -1} highlighted={item.id === focusId}
                  onFocusRow={() => setActive(item.id)} {...props} />)}
              </ul>
            </section>)}
      {hasMore && filter === "all" && <div className="p-3">
        <Button variant="ghost" size="sm" onClick={onLoadMore} disabled={loading}>Show older</Button>
      </div>}
    </div>
  </section>;
}

function Avatar({ actor }: { actor: ActivityItem["actor"] }) {
  return <Identity name={actor.name} avatarOnly size="sm" tint={actor.kind === "agent" ? agentTint(actor.id) : null} />;
}

function ActivityRow({ item, pinned, tabIndex, highlighted, onFocusRow, onRunAction, onNavigate, people }: {
  item: ActivityItem; pinned: boolean; tabIndex: number; highlighted: boolean; onFocusRow: () => void;
} & ActivityFeedProps) {
  const [open, setOpen] = useState<ActivityAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const resolved = item.state !== "open";
  const parts = activityRowParts(item);
  const target = rowTarget(item);
  const decision = pinned && (item.subtype === "approval" || item.subtype === "interaction" || item.subtype === "join_request"
    || item.kind === "login" || item.kind === "extension");

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

  const buttons = item.actions.map((action) => <Button key={action.id} size="sm" disabled={busy}
    variant={decision
      ? action.style === "primary" ? "default" : action.style === "destructive" ? "destructive" : "outline"
      : "ghost"}
    className={decision ? undefined : "activity-row__quick"}
    aria-expanded={action.input || action.confirm ? open?.id === action.id : undefined}
    onClick={(e) => { e.stopPropagation(); press(action); }}>{action.label}</Button>);

  return <li className="activity-row" data-activity-id={item.id} data-activity-kind={item.kind} data-unread={!item.read || undefined}
    data-resolved={resolved || undefined} data-pinned={pinned || undefined} data-highlighted={highlighted || undefined}
    tabIndex={tabIndex} onFocus={(e) => { if (e.target === e.currentTarget) onFocusRow(); }}
    onClick={(e) => {
      // A click on the row body opens it; controls inside handle their own clicks.
      if ((e.target as HTMLElement).closest("button, a, input, textarea, select, form")) return;
      if (target) onNavigate(target);
    }}>
    <span className="activity-row__dot" aria-hidden="true" />
    {!item.read && <span className="sr-only">Unread.</span>}
    <span className="activity-row__avatar"><Avatar actor={item.actor} /></span>
    <div className="activity-row__body">
      <p className="activity-row__line">
        {parts.lead && <strong>{parts.lead}</strong>}{parts.lead && !/^['’]/.test(parts.rest) ? " " : ""}<span>{parts.rest}</span>
        {parts.location && parts.locationLink && <span className="activity-row__where"> · <button type="button"
          className="activity-row__where-link" onClick={() => onNavigate({ ...parts.locationLink!, roomId: item.roomId })}>
          {parts.location}</button></span>}
      </p>
      {parts.snippet && <p className="activity-row__snippet" data-lines={pinned ? 2 : 1}>{parts.snippet}</p>}
      {decision && item.summary.preview && <Detail preview={item.summary.preview} />}
      {resolved
        ? <p className="activity-row__meta">
          {item.state === "done" ? "Done" : "Resolved"}{item.resolvedBy ? ` by ${item.resolvedBy.name}` : ""}{item.resolvedAt ? ` · ${timeAgo(item.resolvedAt)}` : ""}
        </p>
        : decision && item.actions.length > 0 && <div className="activity-row__decide">{buttons}</div>}
      {/* The input rides as both fields: the server reads `note` for a decision and `value`
          for a reply, status or person, so the client keeps no per-action table. */}
      {open && !resolved && (open.input?.kind === "login" || open.input?.kind === "code"
        ? <SecretForm action={open} busy={busy} onCancel={() => setOpen(null)} onSubmit={(value) => void run(open, { value })} />
        : <ActionForm action={open} busy={busy} people={people} onCancel={() => setOpen(null)}
          onSubmit={(value) => void run(open, value === undefined ? {} : { note: value, value })} />)}
      {failure && <p role="alert" className="activity-row__error">{failure}</p>}
    </div>
    <time className="activity-row__time" dateTime={item.createdAt} title={new Date(item.createdAt).toLocaleString()}>
      {timeAgo(item.createdAt)}
    </time>
    {!decision && !resolved && item.actions.length > 0 && <div className="activity-row__actions" role="group" aria-label="Quick actions">{buttons}</div>}
  </li>;
}

/** Stock Paperclip's approval payload, as labelled blocks: what gets published, and the ask's details. */
function Detail({ preview }: { preview: NonNullable<ActivityItem["summary"]["preview"]> }) {
  const [expanded, setExpanded] = useState(false);
  const long = (preview.text?.length ?? 0) > 280;
  return <div className="activity-detail">
    {preview.destination && <p className="activity-detail__to">To {preview.destination}</p>}
    {preview.text && <p className="activity-detail__text">{long && !expanded ? `${preview.text.slice(0, 280)}…` : preview.text}</p>}
    {long && <button type="button" className="activity-feed__link" onClick={() => setExpanded(!expanded)}>
      {expanded ? "Show less" : "Show more"}
    </button>}
    {preview.fields.length > 0 && <dl className="activity-detail__fields">
      {preview.fields.map((field) => <div key={field.label} data-tone={field.label === "Recommended action" ? "warn" : undefined}>
        <dt>{field.label}</dt><dd>{field.value}</dd>
      </div>)}
    </dl>}
  </div>;
}

/**
 * An agent's ask for a website login or a sign-in code. The answer goes to the server as
 * `value` only (never a note) and straight into the workspace's website logins; nothing here
 * keeps it after the post, and it is never shown back.
 */
function SecretForm({ action, busy, onCancel, onSubmit }: {
  action: ActivityAction; busy: boolean; onCancel: () => void; onSubmit: (value: string) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const login = action.input?.kind === "login";
  const ready = login ? Boolean(username.trim() && password) : Boolean(code.trim());
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!ready) return;
    onSubmit(login ? JSON.stringify({ username: username.trim(), password }) : code.trim());
    setPassword("");
    setCode("");
  };
  const field = "w-full rounded-md border px-2 py-1.5 text-sm";
  const tone = { borderColor: "var(--edge)", background: "var(--cream)", color: "var(--midnight)" };
  return <form className="activity-form" onSubmit={submit} autoComplete="off">
    {login
      ? <>
        <input aria-label="Username or email" placeholder="Username or email" value={username} autoFocus
          autoComplete="username" className={field} style={tone} onChange={(e) => setUsername(e.target.value)} />
        <input aria-label="Password" placeholder="Password" type="password" value={password}
          autoComplete="current-password" className={field} style={tone} onChange={(e) => setPassword(e.target.value)} />
      </>
      : <input aria-label="Code" placeholder="Code" value={code} autoFocus inputMode="numeric"
        autoComplete="one-time-code" className={field} style={tone} onChange={(e) => setCode(e.target.value)} />}
    <div className="flex gap-2">
      <Button type="submit" size="sm" disabled={busy || !ready}>{action.label}</Button>
      <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={busy}>Cancel</Button>
    </div>
  </form>;
}

function ActionForm({ action, busy, people, onCancel, onSubmit }: {
  action: ActivityAction; busy: boolean; people: readonly ActivityPerson[]; onCancel: () => void; onSubmit: (value?: string) => void;
}) {
  const [value, setValue] = useState("");
  const input = action.input;
  const options = input?.kind === "person" ? people : input?.options ?? [];
  const missing = Boolean(input?.required) && !value.trim();
  return <form className="activity-form" onSubmit={(e) => { e.preventDefault(); if (!missing) onSubmit(input ? value.trim() || undefined : undefined); }}>
    {action.confirm && <p className="text-sm" style={{ color: "var(--midnight)" }}>{action.confirm}</p>}
    {input && (input.kind === "text"
      ? <Textarea aria-label={input.label} placeholder={input.label} value={value} onChange={(e) => setValue(e.target.value)} rows={2} autoFocus />
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
