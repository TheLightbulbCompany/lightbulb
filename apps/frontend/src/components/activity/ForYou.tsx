"use client";

import { useState } from "react";
import type { ActivityAction, ActivityItem, ActivityLinkKind } from "@lightbulb/shared";
import { Identity } from "@/components/teams/shared/components/Identity";
import { DecisionResolver } from "@/components/teams/inbox/DecisionResolver";
import { useRunRetry } from "@/components/teams/inbox/FailedRunRow";
import type { FailedRunGroup } from "@/components/teams/inbox/lib/groupFailedRuns";
import type { AttentionItem, CompanyAgent } from "@/components/teams/shared/types";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { agentTint } from "@/lib/agentTint";
import { forYouEntries, type ForYouFilter, type ForYouEntry } from "./activityPage";

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
  /** Older Activity pages exist: they load as the list's end scrolls into view. */
  hasMore?: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
  /** Dismiss: acknowledges every run in a group, as the Inbox did. */
  onDismissRuns?: (runIds: string[]) => void;
  /** The selected chip (the `?panel=activity&filter=` link). */
  filter?: ForYouFilter;
  /** A chip was picked: the page puts it in the URL and passes it back as `filter`. */
  onFilterChange?: (filter: ForYouFilter) => void;
  /** Any source is still loading its first answer. */
  loading: boolean;
  onRunAction: (itemId: string, actionId: string, request: { note?: string; value?: string }) => Promise<unknown>;
  /** Opens an item in context: the conversation at the message, the thread, or the item's detail. */
  onNavigate: (target: ActivityTarget) => void;
  /** Workspace humans and agents, for a `person` input. The server validates the pick. */
  people: readonly ActivityPerson[];
  agents: readonly CompanyAgent[];
  /** An item a notification points at: highlighted and scrolled to. */
  focusId?: string | null;
  /** The viewer's Clerk id, so a question on the desk can say "You". */
  currentUserId?: string | null;
}

/** Home: For you's own rows for asks that are not requests (a website login, an extension, a reconnect),
 *  each with its one action in place (Add login opens its form here). */
export function ForYouAsks({ items, onRunAction, onNavigate }: {
  items: readonly ActivityItem[]; onRunAction: ForYouProps["onRunAction"]; onNavigate: ForYouProps["onNavigate"];
}) {
  const entries = forYouEntries(items, [], undefined, () => "An agent");
  if (!entries.length) return null;
  return <ul className="for-you__list">{entries.map((entry) => <EntryRow key={entry.id} entry={entry} highlighted={false} agentMap={NO_AGENTS}
    onRunAction={onRunAction} people={[]} onNavigate={onNavigate} />)}</ul>;
}
const NO_AGENTS = new Map<string, CompanyAgent>();

function Avatar({ entry }: { entry: Pick<ForYouEntry, "sourceName" | "sourceKind" | "sourceId"> }) {
  return <Identity name={entry.sourceName} avatarOnly size="xs" tint={entry.sourceKind === "agent" ? agentTint(entry.sourceId) : null} />;
}

function entryTarget(entry: ForYouEntry): ActivityTarget | null {
  if (entry.source.type === "activity") return rowTarget(entry.source.item);
  if (entry.source.type === "failed_runs") return { kind: "run", id: entry.source.group.representative.id, roomId: null };
  if (entry.source.item.sourceKind === "approval") return { kind: "approval", id: entry.source.item.approval?.id ?? entry.source.item.subject.id, roomId: null };
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
  const content = <>
    <span className="for-you-row__avatar"><Avatar entry={entry} /></span>
    <span className="for-you-row__body">
      <span className="for-you-row__head">
        <strong>{entry.sourceName}</strong>
        {entry.where && <span className="for-you-row__where">{entry.where}</span>}
        {entry.at && <time className="for-you-row__time" dateTime={entry.at}>{timeAgo(entry.at)}</time>}
      </span>
      <span className="for-you-row__text">{entry.text}</span>
    </span>
  </>;
  return <li className="for-you-entry" data-for-you-id={entry.id} data-highlighted={highlighted || undefined} data-unread={entry.unread || undefined}>
    <div className="for-you-row">
      {target
        ? <button type="button" className="for-you-row__open" onClick={() => onNavigate(target)}>{content}</button>
        : <div className="for-you-row__open">{content}</div>}
      {control}
      <span className="for-you-row__dot" {...(entry.unread ? { role: "img", "aria-label": "Unread" } : { "aria-hidden": true })} />
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
  // A message row only opens: its thread or conversation is where it is answered.
  if (entry.source.type === "activity" && entry.verb !== "reply") {
    const item = entry.source.item;
    const action = primaryAction(item);
    // The server's quiet second answer (a login or extension ask's "Not now", a join's "Decline").
    const decline = item.actions.find((entry) => entry.id === "decline" && entry !== action && !entry.input && !entry.navigate);
    const run = async (request: { note?: string; value?: string } = {}, actionId = action?.id) => {
      if (!actionId) return;
      setBusy(true);
      setFailure(null);
      try {
        await onRunAction(item.id, actionId, request);
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
    if (decline) control = <span className="for-you-row__controls">{control}
      <button type="button" className="editorial-btn editorial-btn--sm editorial-btn--ghost" disabled={busy}
        onClick={() => void run({}, decline.id)}>{decline.label}</button>
    </span>;
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
      // An approval or question (shown from here when Activity could not list it) is decided on its own page.
      const decides = item.sourceKind === "approval" || item.sourceKind === "issue_thread_interaction";
      control = <button type="button" className={button} onClick={() => onNavigate(target)}>{decides ? "Review" : "Open"}</button>;
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
