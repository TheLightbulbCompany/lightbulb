"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ActivityAction, ActivityItem, ActivityLinkKind } from "@lightbulb/shared";
import { Identity } from "@/components/teams/shared/components/Identity";
import { DecisionResolver } from "@/components/teams/inbox/DecisionResolver";
import { useRunRetry } from "@/components/teams/inbox/FailedRunRow";
import type { FailedRunGroup } from "@/components/teams/inbox/lib/groupFailedRuns";
import type { AttentionItem, CompanyAgent } from "@/components/teams/shared/types";
import { agentTint } from "@/lib/agentTint";
import { forYouEntries, groupForYou, rowSummary, type ForYouEntry, type ForYouGroup, type ForYouRow } from "./activityPage";

export interface ActivityTarget { kind: ActivityLinkKind; id: string; roomId: string | null }
export interface ActivityPerson { value: string; label: string }

/** Where an Activity item opens: its own `navigate` action, else its message. */
export function rowTarget(item: ActivityItem): ActivityTarget | null {
  const action = item.actions.find((entry) => entry.navigate)?.navigate;
  if (action) return action;
  // A session ask (pick-up, an agent's message) opens its session.
  const session = item.kind === "session" ? item.summary.links.find((link) => link.kind === "session") : undefined;
  if (session) return { kind: "session", id: session.id, roomId: null };
  if (!item.roomId) return null;
  return item.threadRootMessageId
    ? { kind: "thread", id: item.threadRootMessageId, roomId: item.roomId }
    : { kind: "room", id: item.roomId, roomId: item.roomId };
}

export interface ForYouProps {
  activityItems: readonly ActivityItem[];
  attentionItems: readonly AttentionItem[];
  /** The Inbox's failed runs, grouped and minus the dismissed ones. */
  failedRuns?: readonly FailedRunGroup[];
  /** A source (Activity, the attention feed, failed runs) failed or answered only partly:
   *  For you cannot vouch that nothing is waiting, so it never shows the all-clear. */
  incomplete?: boolean;
  /** Re-reads every source; offered beside the "Couldn't load some items" line. */
  onRetry?: () => void;
  /** Older Activity pages exist (unread messages and asks past the first page). */
  hasMore?: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
  /** Dismiss: acknowledges every run in a group, as the Inbox did. */
  onDismissRuns?: (runIds: string[]) => void;
  /** Any source is still loading its first answer. */
  loading: boolean;
  onRunAction: (itemId: string, actionId: string, request: { note?: string; value?: string }) => Promise<unknown>;
  onNavigate: (target: ActivityTarget) => void;
  /** Workspace humans and agents, for a `person` input. The server validates the pick. */
  people: readonly ActivityPerson[];
  agents: readonly CompanyAgent[];
  /** An item a notification points at: shown (its row and group opened) and highlighted. */
  focusId?: string | null;
}

const VISIBLE_ROWS = 3;

/**
 * What needs this person, as compact one-line rows grouped by verb: Decide, Reply, Do.
 * A source with several items is one row ("Anna · 3 messages") that opens in place;
 * a group shows its top 3 rows, then "N more". Every button is the item's own action.
 */
export function ForYou(props: ForYouProps) {
  const { activityItems, attentionItems, failedRuns, loading, incomplete, focusId, agents } = props;
  const entries = useMemo(() => forYouEntries(activityItems, attentionItems, failedRuns,
    (id) => agents.find((agent) => agent.id === id)?.name ?? "An agent"), [activityItems, attentionItems, failedRuns, agents]);
  const groups = useMemo(() => groupForYou(entries), [entries]);
  const agentMap = useMemo(() => new Map(props.agents.map((agent) => [agent.id, agent])), [props.agents]);
  const listRef = useRef<HTMLDivElement>(null);
  const focusShown = Boolean(focusId && entries.some((entry) => entry.id === focusId));
  useEffect(() => {
    if (!focusShown || !focusId) return;
    listRef.current?.querySelector<HTMLElement>(`[data-for-you-id="${CSS.escape(focusId)}"]`)?.scrollIntoView?.({ block: "center" });
  }, [focusShown, focusId]);

  return <section className="for-you" aria-labelledby="for-you-title">
    <h2 id="for-you-title" className="for-you__title">For you{entries.length > 0 && <span className="for-you__count"> · {entries.length} open</span>}</h2>
    {incomplete && <p role="alert" className="for-you__notice">
      Couldn&apos;t load some items{props.onRetry && <> · <button type="button" className="for-you__more" onClick={props.onRetry}>Retry</button></>}
    </p>}
    <div ref={listRef}>
      {groups.length > 0
        ? groups.map((group) => <ForYouGroupView key={group.verb} group={group} agentMap={agentMap} {...props} />)
        // The all-clear only when every source answered: a failed or pending read could hide a decision.
        : loading ? <p role="status" className="for-you__empty">Loading…</p>
          : !incomplete && !props.hasMore && <p className="for-you__empty">You&apos;re all caught up.</p>}
      {props.hasMore && props.onLoadMore && <button type="button" className="for-you__more" disabled={props.loadingMore}
        onClick={props.onLoadMore}>{props.loadingMore ? "Loading…" : "Show older"}</button>}
    </div>
  </section>;
}

function ForYouGroupView({ group, focusId, ...rest }: { group: ForYouGroup; agentMap: Map<string, CompanyAgent> } & ForYouProps) {
  const [expanded, setExpanded] = useState(false);
  const focusAt = focusId ? group.rows.findIndex((row) => row.entries.some((entry) => entry.id === focusId)) : -1;
  const shown = expanded || focusAt >= VISIBLE_ROWS ? group.rows : group.rows.slice(0, VISIBLE_ROWS);
  const more = group.rows.length - shown.length;
  return <section className="for-you-group" data-verb={group.verb} aria-labelledby={`for-you-${group.verb}`}>
    <h3 id={`for-you-${group.verb}`} className="for-you-group__title">{group.label} <span className="for-you-group__count">{group.count}</span></h3>
    <ul>
      {shown.map((row) => <ForYouRowView key={row.key} row={row} focusId={focusId} {...rest} />)}
    </ul>
    {more > 0 && <button type="button" className="for-you__more" onClick={() => setExpanded(true)}>{more} more</button>}
  </section>;
}

function Avatar({ row }: { row: Pick<ForYouRow, "sourceName" | "sourceKind" | "sourceId"> }) {
  return <Identity name={row.sourceName} avatarOnly size="xs" tint={row.sourceKind === "agent" ? agentTint(row.sourceId) : null} />;
}

function ForYouRowView({ row, focusId, ...rest }: { row: ForYouRow; agentMap: Map<string, CompanyAgent> } & Omit<ForYouProps, "activityItems" | "attentionItems">) {
  const focused = Boolean(focusId && row.entries.some((entry) => entry.id === focusId));
  // A row a notification points at starts open; Hide still closes it.
  const [expanded, setExpanded] = useState(focused);
  // Activity is a persistent dock tab, so focus can arrive after mount: open the row when it does.
  const [wasFocused, setWasFocused] = useState(focused);
  if (focused !== wasFocused) {
    setWasFocused(focused);
    if (focused) setExpanded(true);
  }
  if (row.entries.length === 1) return <EntryRow entry={row.entries[0]} highlighted={focused} {...rest} />;
  return <li className="for-you-source" data-expanded={expanded || undefined}>
    <div className="for-you-row">
      <span className="for-you-row__avatar"><Avatar row={row} /></span>
      <p className="for-you-row__line"><strong>{row.sourceName}</strong> · {rowSummary(row)}</p>
      <button type="button" className="editorial-btn editorial-btn--sm editorial-btn--secondary" aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}>{expanded ? "Hide" : "Show"}</button>
    </div>
    {expanded && <ul className="for-you-source__items">
      {row.entries.map((entry) => <EntryRow key={entry.id} entry={entry} highlighted={entry.id === focusId} {...rest} />)}
    </ul>}
  </li>;
}

function entryTarget(entry: ForYouEntry): ActivityTarget | null {
  if (entry.source.type === "activity") return rowTarget(entry.source.item);
  if (entry.source.type === "failed_runs") return { kind: "run", id: entry.source.group.representative.id, roomId: null };
  const issue = entry.source.item.relatedIssue;
  return issue ? { kind: "issue", id: issue.id, roomId: null } : null;
}

/** The item's one action: its primary one, else its first that is not destructive. */
export function primaryAction(item: ActivityItem): ActivityAction | null {
  return item.actions.find((action) => action.style === "primary") ?? item.actions.find((action) => action.style !== "destructive") ?? null;
}

type EntryProps = {
  entry: ForYouEntry; highlighted: boolean; agentMap: Map<string, CompanyAgent>;
} & Pick<ForYouProps, "onRunAction" | "onNavigate" | "people" | "onDismissRuns">;

function EntryRow(props: EntryProps) {
  return props.entry.source.type === "failed_runs" ? <FailedRunEntry {...props} group={props.entry.source.group} /> : <ItemEntry {...props} />;
}

function RowShell({ entry, highlighted, target, onNavigate, control, below, failure }: {
  entry: ForYouEntry; highlighted: boolean; target: ActivityTarget | null; onNavigate: ForYouProps["onNavigate"];
  control: React.ReactNode; below?: React.ReactNode; failure?: string | null;
}) {
  return <li className="for-you-entry" data-for-you-id={entry.id} data-highlighted={highlighted || undefined}>
    <div className="for-you-row">
      <span className="for-you-row__avatar"><Avatar row={entry} /></span>
      {target
        ? <button type="button" className="for-you-row__line for-you-row__open" onClick={() => onNavigate(target)}>
          <strong>{entry.sourceName}</strong> <span>{entry.text}</span>
        </button>
        : <p className="for-you-row__line"><strong>{entry.sourceName}</strong> <span>{entry.text}</span></p>}
      {control}
    </div>
    {below && <div className="for-you-entry__below">{below}</div>}
    {failure && <p role="alert" className="for-you-entry__error">{failure}</p>}
  </li>;
}

/** A failed run (group): Retry wakes its agent and Dismiss acks every run in it, as in the Inbox. */
function FailedRunEntry({ entry, highlighted, onNavigate, onDismissRuns, group }: EntryProps & { group: FailedRunGroup }) {
  const retry = useRunRetry(group.representative.agentId);
  const control = <span className="for-you-row__controls">
    {group.representative.agentId && <button type="button" className="editorial-btn editorial-btn--sm editorial-btn--secondary"
      disabled={retry.phase === "retrying"} onClick={() => void retry.retry()}>{retry.label}</button>}
    {onDismissRuns && <button type="button" className="editorial-btn editorial-btn--sm editorial-btn--ghost"
      onClick={() => onDismissRuns(group.runs.map((run) => run.id))}>Dismiss</button>}
  </span>;
  return <RowShell entry={entry} highlighted={highlighted} target={entryTarget(entry)} onNavigate={onNavigate} control={control} />;
}

function ItemEntry({ entry, highlighted, onRunAction, onNavigate, people, agentMap }: EntryProps) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const target = entryTarget(entry);
  const button = `editorial-btn editorial-btn--sm ${entry.verb === "decide" ? "editorial-btn--primary" : "editorial-btn--secondary"}`;

  let control: React.ReactNode = null;
  let below: React.ReactNode = null;
  if (entry.source.type === "activity") {
    const item = entry.source.item;
    const action = primaryAction(item);
    const run = async (request: { note?: string; value?: string } = {}) => {
      if (!action) return;
      setBusy(true);
      setFailure(null);
      try {
        await onRunAction(item.id, action.id, request);
        setOpen(false);
      } catch (e) {
        setFailure(e instanceof Error && e.message ? e.message : "That didn't go through. Try again.");
      } finally {
        setBusy(false);
      }
    };
    // A decision with no input (Approve, Confirm, Allow) is never one click from a one-line row:
    // Review opens the item's own detail (the approval and its payload, the task, the session),
    // where it is decided. Without a place to open, it asks once inline.
    const review = Boolean(action && !action.input && !action.navigate);
    if (action && review && target) control = <button type="button" className={button} onClick={() => onNavigate(target)}>Review</button>;
    else if (action) control = <button type="button" className={button} disabled={busy}
      aria-expanded={action.navigate ? undefined : open}
      onClick={() => {
        if (action.navigate) return onNavigate(action.navigate);
        setOpen(!open);
      }}>{review ? "Review" : action.label}</button>;
    // The input rides as both fields: the server reads `note` for a decision and `value`
    // for a reply, status or person, so the client keeps no per-action table.
    if (open && action) below = action.input?.kind === "login"
      ? <SecretForm action={action} busy={busy} onCancel={() => setOpen(false)} onSubmit={(value) => void run({ value })} />
      : <ActionForm action={action} busy={busy} people={people} onCancel={() => setOpen(false)}
        onSubmit={(value) => void run(value === undefined ? {} : { note: value, value })} />;
  } else if (entry.source.type === "attention") {
    const item = entry.source.item;
    if (item.sourceKind === "decision") {
      // The feed row carries no options: the resolver reads the decision and answers it in place.
      control = <button type="button" className={button} aria-expanded={open} onClick={() => setOpen(!open)}>Answer</button>;
      if (open) below = <DecisionResolver decisionId={item.subject.id} originIssue={item.relatedIssue}
        agentMap={agentMap} originAgentName={item.originAgentName} />;
    } else if (target) {
      control = <button type="button" className={button} onClick={() => onNavigate(target)}>Open</button>;
    }
  }

  return <RowShell entry={entry} highlighted={highlighted} target={target} onNavigate={onNavigate} control={control} below={below} failure={failure} />;
}

/**
 * An agent's ask for a website login. The answer goes to the server as `value` only (never a
 * note) and straight into the workspace's website sign-ins; nothing here keeps it after the post,
 * and it is never shown back. (A sign-in code is typed into the shared browser, never asked for here.)
 */
function SecretForm({ action, busy, onCancel, onSubmit }: {
  action: ActivityAction; busy: boolean; onCancel: () => void; onSubmit: (value: string) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const ready = Boolean(username.trim() && password);
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!ready) return;
    onSubmit(JSON.stringify({ username: username.trim(), password }));
    setPassword("");
  };
  return <form className="for-you-form" onSubmit={submit} autoComplete="off">
    {action.input?.hint && <p className="for-you-form__hint">{action.input.hint}</p>}
    <input aria-label="Username or email" placeholder="Username or email" value={username} autoFocus
      autoComplete="username" className="for-you-form__field" onChange={(e) => setUsername(e.target.value)} />
    <input aria-label="Password" placeholder="Password" type="password" value={password}
      autoComplete="current-password" className="for-you-form__field" onChange={(e) => setPassword(e.target.value)} />
    <div className="for-you-form__buttons">
      <button type="submit" className="editorial-btn editorial-btn--sm editorial-btn--primary" disabled={busy || !ready}>{action.label}</button>
      <button type="button" className="editorial-btn editorial-btn--sm editorial-btn--secondary" onClick={onCancel} disabled={busy}>Cancel</button>
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
  return <form className="for-you-form" onSubmit={(e) => { e.preventDefault(); if (!missing) onSubmit(input ? value.trim() || undefined : undefined); }}>
    {action.confirm && <p className="for-you-form__hint">{action.confirm}</p>}
    {input && (input.kind === "text"
      ? <textarea aria-label={input.label} placeholder={input.label} value={value} rows={2} autoFocus
        className="for-you-form__field" onChange={(e) => setValue(e.target.value)} />
      : <select aria-label={input.label} value={value} autoFocus className="for-you-form__field" onChange={(e) => setValue(e.target.value)}>
        <option value="">{input.label}</option>
        {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>)}
    <div className="for-you-form__buttons">
      <button type="submit" className={`editorial-btn editorial-btn--sm ${action.style === "destructive" ? "editorial-btn--destructive" : "editorial-btn--primary"}`}
        disabled={busy || missing}>{action.label}</button>
      <button type="button" className="editorial-btn editorial-btn--sm editorial-btn--secondary" onClick={onCancel} disabled={busy}>Cancel</button>
    </div>
  </form>;
}
