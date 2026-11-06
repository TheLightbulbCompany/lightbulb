"use client";

// Home (founder, 6 Nov 2026): the chats and the project spaces. Ask Lightbulb, the Lightbulb DM, then every room as a
// project card (newest activity first) with how many of its requests are working and how many need you. What used to
// be Home (Needs you, mentions, Ideas, the feed, Working/Done/Closed) is Activity; a slim line here points at it.
// Counts are cuts of reads Activity already makes: the complete Needs you read (`/tasks?needsYou=true`) and the paged
// workspace list, both grouped by `audience.roomId` — no read of their own.

import "./home.css";
import { useState, type FormEvent, type ReactElement } from "react";
import { ChevronRight, Plus, Search } from "lucide-react";
import { orderIdeas, overviewGroups, roomDisplayName, summaryView, useTaskList, type TaskScope, type TaskSummary } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";
import type { DirectoryRoom } from "@/components/messaging/types";
import { useNeedsYouTasks } from "@/components/tasks/useRoomThreads";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { activateMessagesPanel, closePane, openAgentDmWithDraft, openPanel } from "@/components/workspace/workspaceController";
import { useIdeas } from "@/hooks/useIdeas";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { capture } from "@/lib/analytics";

const WORKSPACE: TaskScope = { kind: "workspace" };
/** Starter idea chips on a new workspace's Home. */
export const HOME_STARTERS = 3;

export interface RoomCounts { working: number; needsYou: number }

/** Each room's counts: its share of the complete Needs you read, and of the listed requests' Working group. */
export function roomCounts(needsYou: readonly TaskSummary[], listed: readonly TaskSummary[]): Map<string, RoomCounts> {
  const out = new Map<string, RoomCounts>();
  const at = (t: TaskSummary) => {
    const id = t.audience?.roomId;
    if (!id) return null;
    if (!out.has(id)) out.set(id, { working: 0, needsYou: 0 });
    return out.get(id)!;
  };
  for (const t of needsYou) { const c = at(t); if (c) c.needsYou++; }
  for (const t of overviewGroups(listed).find((g) => g.id === "working")?.tasks ?? []) { const c = at(t); if (c) c.working++; }
  return out;
}

/** The rooms Home shows as projects: every conversation but DMs (the Lightbulb DM has its own row; a specialist's
 *  lives on Team), archived rooms, the feed, session chats and the shared vendor channel. Newest activity first. */
export function projectRooms(rooms: readonly DirectoryRoom[]): DirectoryRoom[] {
  const latest = (r: DirectoryRoom) => (r.lastMessageAt ? Date.parse(r.lastMessageAt) : -Infinity);
  return rooms.filter((r) => r.kind !== "dm" && !r.archivedAt && !r.feed && !r.session && !r.shared)
    .sort((a, b) => latest(b) - latest(a));
}

const openRoom = (roomId: string) => window.dispatchEvent(new CustomEvent("openRoom", { detail: { roomId, threadId: null } }));
const needWord = (n: number) => `${n} need${n === 1 ? "s" : ""} you`;

/** Working counts are floors when the loaded page holds nothing but live requests and more pages follow. */
export function workingCapped(list: { tasks: readonly TaskSummary[]; next: string | null } | undefined): boolean {
  if (!list?.next) return false;
  const settled = overviewGroups(list.tasks).filter((g) => g.id === "done" || g.id === "closed");
  return settled.every((g) => g.tasks.length === 0);
}
const workingWord = (n: number, capped: boolean) => `Working ${n}${capped ? "+" : ""}`;
/** A card's accessible name: the project and its counts (topic and snippet are its description). */
export const cardLabel = (name: string, counts: RoomCounts | undefined, capped: boolean) =>
  [name, counts?.working ? workingWord(counts.working, capped) : "", counts?.needsYou ? needWord(counts.needsYou) : ""].filter(Boolean).join(", ");

export function HomeProjects(): ReactElement {
  const messaging = useMessagingWorkspaceOptional();
  const { tasks } = useLiveEvents();
  const { data } = useTaskList(tasks, WORKSPACE);
  const needs = useNeedsYouTasks(WORKSPACE);
  const counts = roomCounts(needs.tasks, data?.tasks ?? []);
  // The list leads with live requests (waiting, working, queued), so Working is complete unless the first page is
  // all live and more pages follow: then each Working count is a floor.
  const capped = workingCapped(data);
  const directory = messaging?.directoryProps;
  const frontId = directory?.ceoAgentId;
  const rooms = directory?.rooms ?? [];
  const dm = frontId ? rooms.find((r) => r.kind === "dm" && r.participants.some((p) => p.kind === "agent" && p.id === frontId)) : undefined;
  const projects = projectRooms(rooms);
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const shown = q ? projects.filter((r) => `${roomDisplayName(r, r.participants)} ${r.topic ?? ""}`.toLowerCase().includes(q)) : projects;
  const [creating, setCreating] = useState(false);
  // A new workspace: nothing but the seeded #general (and DMs) yet.
  const fresh = directory?.state === "ready" && projects.every((r) => r.isDefault);
  const openDm = () => { if (frontId) openAgentDmWithDraft(frontId, ""); else activateMessagesPanel(); };

  return <div className="home-projects">
    <h1 className="text-title font-semibold">Home</h1>
    {needs.tasks.length > 0 && <button type="button" className="home-projects__needs"
      onClick={() => { closePane(); openPanel("activity", {}, { pushUrl: true }); }}>
      <span className="home-projects__dot" aria-hidden="true" />
      <span><span className="font-semibold text-foreground">{needWord(needs.tasks.length)}</span> · Review</span>
      <ChevronRight size={14} aria-hidden="true" />
    </button>}
    {messaging && <AskLightbulb onAsk={messaging.askLightbulb} />}

    <section aria-labelledby="home-chats-heading" className="home-projects__section">
      <h2 id="home-chats-heading" className="text-ui font-semibold">Chats</h2>
      <button type="button" className="home-projects__dm" onClick={() => openDm()}>
        <span className="min-w-0 flex-1">
          <span className="block font-semibold">Lightbulb</span>
          <span className="block truncate text-ui text-muted-foreground">{dm?.lastMessage ? previewLine(dm, data?.tasks ?? []) : "Your chat with Lightbulb"}</span>
        </span>
        <RoomMeta room={dm} counts={dm ? counts.get(dm.roomId) : undefined} capped={capped} />
      </button>
    </section>

    <section aria-labelledby="home-projects-heading" className="home-projects__section">
      <div className="home-projects__head">
        <h2 id="home-projects-heading" className="text-ui font-semibold">Projects</h2>
        {projects.length > 3 && <label className="home-projects__search">
          <Search size={14} aria-hidden="true" />
          <span className="sr-only">Find a project</span>
          <input type="search" placeholder="Find a project" value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>}
        {directory?.onCreate && !creating && <Button type="button" size="sm" variant="outline" onClick={() => setCreating(true)}>
          <Plus aria-hidden="true" />New project</Button>}
      </div>
      {creating && directory?.onCreate && <NewProject busy={!!directory.creationBusy} error={directory.creationError}
        onCancel={() => setCreating(false)}
        onCreate={async (name) => {
          const created = await directory.onCreate!({ kind: "channel", name, members: [] });
          if (created !== false) { setCreating(false); activateMessagesPanel(); }
        }} />}
      {!directory || directory.state === "loading" ? <p role="status" className="text-ui text-muted-foreground">Loading…</p>
        : directory.state === "error" ? <p role="alert" className="text-ui text-muted-foreground">Projects couldn&apos;t load.{" "}
          {directory.onRetry && <button type="button" className="underline hover:text-foreground" onClick={directory.onRetry}>Retry</button>}</p>
        : shown.length > 0 ? <ul className="home-projects__grid">
          {shown.map((room) => <li key={room.roomId}><ProjectCard room={room} counts={counts.get(room.roomId)} capped={capped} preview={previewLine(room, data?.tasks ?? [])} /></li>)}
        </ul>
        : q ? <p className="text-ui text-muted-foreground">No project matches “{query.trim()}”.</p> : null}
      {fresh && !creating && <StartAProject onStart={() => setCreating(true)} />}
    </section>
  </div>;
}

/** Sends straight into the Lightbulb DM and opens it; a failed send keeps the text and says so. */
function AskLightbulb({ onAsk }: { onAsk: (text: string) => Promise<void> }): ReactElement {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim() || sending) return;
    setSending(true);
    setFailed(null);
    try { await onAsk(text.trim()); setText(""); }
    catch (error) { setFailed(error instanceof Error ? error.message : "Couldn't send. Try again."); }
    finally { setSending(false); }
  };
  return <div className="grid gap-1">
    <form className="home-projects__ask" onSubmit={(e) => void submit(e)} aria-busy={sending}>
      <label htmlFor="home-ask" className="sr-only">Ask Lightbulb</label>
      <input id="home-ask" placeholder="Ask Lightbulb…" value={text} onChange={(e) => setText(e.target.value)} autoComplete="off" />
      <Button type="submit" size="sm" disabled={!text.trim() || sending}>{sending ? "Sending…" : "Ask"}</Button>
    </form>
    {failed && <p role="alert" className="text-ui text-err">{failed}</p>}
  </div>;
}

const snippet = (room: DirectoryRoom) => {
  const m = room.lastMessage;
  if (!m) return room.topic ?? "";
  const text = m.textPreview || (m.hasAttachments ? "Sent an attachment" : "");
  return m.authorKind === "system" ? text : `${m.authorName}: ${text}`;
};

/** A card's preview: the room's latest message, unless one of its requests settled after it (Done, Stopped, Closed).
 *  Then the request's status, so "Growth is writing…" or "On it: …" never outlives the request (dev walk W8-9).
 *  ponytail: the listed page of requests; an older request settling is caught by the next message. */
export function previewLine(room: DirectoryRoom, tasks: readonly TaskSummary[]): string {
  const at = room.lastMessage ? Date.parse(room.lastMessage.createdAt) : -Infinity;
  const latest = tasks.filter((t) => (t.audience?.roomId ?? t.originRoomId) === room.roomId)
    .reduce<TaskSummary | null>((a, t) => (!a || Date.parse(t.updatedAt) > Date.parse(a.updatedAt) ? t : a), null);
  if (latest && Date.parse(latest.updatedAt) > at) {
    // Done and Closed read the same for every viewer (only a failed request depends on who looks).
    const view = summaryView(latest, "");
    if (view.word === "Done" || (view.word === "Closed" && latest.status === "resolved")) return `${latest.title} · ${view.reason ?? view.word}`;
  }
  return snippet(room);
}

function RoomMeta({ room, counts, capped = false }: { room?: DirectoryRoom; counts?: RoomCounts; capped?: boolean }): ReactElement {
  return <span className="home-projects__meta">
    {room?.lastMessageAt && <time dateTime={room.lastMessageAt}>{timeAgo(room.lastMessageAt)}</time>}
    {!!counts?.working && <span>{workingWord(counts.working, capped)}</span>}
    {!!counts?.needsYou && <span className="font-semibold text-foreground"><span className="home-projects__dot" aria-hidden="true" />{needWord(counts.needsYou)}</span>}
  </span>;
}

function ProjectCard({ room, counts, capped, preview }: { room: DirectoryRoom; counts?: RoomCounts; capped: boolean; preview: string }): ReactElement {
  const name = roomDisplayName(room, room.participants);
  const describedBy = `home-card-${room.roomId}`;
  return <button type="button" className="home-projects__card" onClick={() => openRoom(room.roomId)}
    aria-label={cardLabel(name, counts, capped)} aria-describedby={room.topic || room.lastMessage ? describedBy : undefined}>
    <span className="block truncate font-semibold">{name}</span>
    <span id={describedBy} className="contents">
      {room.topic && <span className="line-clamp-2 text-ui text-muted-foreground">{room.topic}</span>}
      {room.lastMessage && <span className="block truncate text-ui text-muted-foreground">{preview}</span>}
    </span>
    <RoomMeta room={room} counts={counts} capped={capped} />
  </button>;
}

function NewProject({ busy, error, onCreate, onCancel }: {
  busy: boolean; error?: string; onCreate: (name: string) => Promise<void>; onCancel: () => void;
}): ReactElement {
  const [name, setName] = useState("");
  return <form className="home-projects__new" aria-label="New project" onSubmit={(e) => { e.preventDefault(); if (name.trim() && !busy) void onCreate(name.trim()); }}>
    <label htmlFor="home-new-project" className="text-ui font-semibold">Project name</label>
    <input id="home-new-project" className="room-input" value={name} maxLength={80} disabled={busy} autoFocus
      placeholder="Launch the spring sale" onChange={(e) => setName(e.target.value)} />
    {error && <p role="alert" className="text-ui text-err">{error}</p>}
    <div className="flex gap-2">
      <Button type="submit" size="sm" disabled={!name.trim() || busy}>{busy ? "Creating…" : "Create project"}</Button>
      <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={onCancel}>Cancel</Button>
    </div>
  </form>;
}

/** A new workspace: start a project, or begin from one of the catalog's ideas. */
function StartAProject({ onStart }: { onStart: () => void }): ReactElement {
  const { ideas } = useIdeas();
  const starters = orderIdeas(ideas).filter((idea) => !idea.suggested).slice(0, HOME_STARTERS);
  return <div className="home-projects__start">
    <p className="text-ui text-muted-foreground">A project is a room where you and Lightbulb work on one thing.</p>
    <div className="flex flex-wrap gap-2">
      <Button type="button" size="sm" onClick={onStart}><Plus aria-hidden="true" />Start a project</Button>
      {starters.length > 0 && <ul aria-label="Starter ideas" className="contents">
        {starters.map((idea) => <li key={idea.id}>
          <Button type="button" size="sm" variant="outline" onClick={() => { capture("ideas_opened", { source: "home_starter" }); openPanel("ideas", { idea: idea.id }, { pushUrl: true }); }}>
            {idea.title}
          </Button>
        </li>)}
      </ul>}
    </div>
  </div>;
}
