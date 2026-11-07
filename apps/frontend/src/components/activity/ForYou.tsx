"use client";

import { useState } from "react";
import type { ActivityAction, ActivityItem, ActivityLinkKind } from "@lightbulb/shared";
import { Identity } from "@/components/teams/shared/components/Identity";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { agentTint } from "@/lib/agentTint";
import { forYouEntries, type ForYouEntry } from "./activityPage";

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
  onRunAction: (itemId: string, actionId: string, request: { note?: string; value?: string }) => Promise<unknown>;
  /** Opens an item in context: the conversation at the message, the thread, or the item's detail. */
  onNavigate: (target: ActivityTarget) => void;
  /** Workspace humans and agents, for a `person` input. The server validates the pick. */
  people: readonly ActivityPerson[];
}

/** Home: For you's own rows for asks that are not requests (a website login, an extension, a reconnect),
 *  each with its one action in place (Add login opens its form here). */
export function ForYouAsks({ items, onRunAction, onNavigate }: {
  items: readonly ActivityItem[]; onRunAction: ForYouProps["onRunAction"]; onNavigate: ForYouProps["onNavigate"];
}) {
  const entries = forYouEntries(items);
  if (!entries.length) return null;
  return <ul className="for-you__list">{entries.map((entry) => <ItemEntry key={entry.id} entry={entry} highlighted={false}
    onRunAction={onRunAction} people={[]} onNavigate={onNavigate} />)}</ul>;
}

function Avatar({ entry }: { entry: Pick<ForYouEntry, "sourceName" | "sourceKind" | "sourceId"> }) {
  return <Identity name={entry.sourceName} avatarOnly size="xs" tint={entry.sourceKind === "agent" ? agentTint(entry.sourceId) : null} />;
}

/** The item's one action: its primary one, else its first that is not destructive. */
export function primaryAction(item: ActivityItem): ActivityAction | null {
  return item.actions.find((action) => action.style === "primary") ?? item.actions.find((action) => action.style !== "destructive") ?? null;
}

type EntryProps = {
  entry: ForYouEntry; highlighted: boolean;
} & Pick<ForYouProps, "onRunAction" | "onNavigate" | "people">;

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

function ItemEntry({ entry, highlighted, onRunAction, onNavigate, people }: EntryProps) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const target = rowTarget(entry.source.item);
  const button = `editorial-btn editorial-btn--sm ${entry.verb === "decide" ? "editorial-btn--primary" : "editorial-btn--secondary"}`;

  let control: React.ReactNode = null;
  let below: React.ReactNode = null;
  // A message row only opens: its thread or conversation is where it is answered.
  if (entry.verb !== "reply") {
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
