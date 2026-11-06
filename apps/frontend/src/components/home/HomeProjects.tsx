"use client";

// Home (founder, 6 Nov 2026): the chats and the project spaces. Ask Lightbulb, the Lightbulb DM, then every room as a
// project card (newest activity first) with how many of its requests are working and how many need you. What used to
// be Home (Needs you, mentions, Ideas, the feed, Working/Done/Closed) is Activity; a slim line here points at it.
// Counts are cuts of reads Activity already makes: the complete Needs you read (`/tasks?needsYou=true`) and the paged
// workspace list, both grouped by `audience.roomId` — no read of their own.

import "./home.css";
import { useState, type FormEvent, type ReactElement } from "react";
import { Archive, ChevronRight, CircleDashed, MoreHorizontal, Pin, PinOff, Plus, Search, Settings } from "lucide-react";
import { orderIdeas, overviewGroups, roomDisplayName, useTaskList, type Idea, type MemberPrefsRequest, type PatchRoomRequest, type TaskScope, type TaskSummary } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { FIRST_MOVE_LABEL, openFirstMoves, useFirstMoves } from "@/components/ideas/FirstMoves";
import { NewProjectDialog } from "@/components/projects/NewProjectDialog";
import { ProjectTile } from "@/components/projects/ProjectTile";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";
import type { DirectoryRoom } from "@/components/messaging/types";
import { useNeedsYouCount, useNeedsYouTasks } from "@/components/tasks/useRoomThreads";
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
/** A card's accessible name: the project and its counts. */
export const cardLabel = (name: string, counts: RoomCounts | undefined, capped: boolean) =>
  [name, counts?.working ? workingWord(counts.working, capped) : "", counts?.needsYou ? needWord(counts.needsYou) : ""].filter(Boolean).join(", ");

export function HomeProjects(): ReactElement {
  const messaging = useMessagingWorkspaceOptional();
  const { tasks } = useLiveEvents();
  const { data } = useTaskList(tasks, WORKSPACE);
  const needs = useNeedsYouTasks(WORKSPACE);
  const needsYouCount = useNeedsYouCount();
  const firstMoves = useFirstMoves();
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
  const shown = q ? projects.filter((r) => roomDisplayName(r, r.participants).toLowerCase().includes(q)) : projects;
  // Pinned first (the sidebar's Pinned group: the same `starred` setting), then the rest.
  const pinned = shown.filter((r) => r.starred);
  const rest = shown.filter((r) => !r.starred);
  const [creating, setCreating] = useState(false);
  // A new workspace: nothing but the seeded #general (and DMs) yet.
  const fresh = directory?.state === "ready" && projects.every((r) => r.isDefault);
  const openDm = () => { if (frontId) openAgentDmWithDraft(frontId, ""); else activateMessagesPanel(); };
  // The one needs-you item is the first move: say so, and go straight to it.
  const onlyFirstMove = needsYouCount === 1 && firstMoves.length > 0;
  const card = (room: DirectoryRoom) => <li key={room.roomId}><ProjectCard room={room} counts={counts.get(room.roomId)} capped={capped}
    viewerId={directory?.currentUserId} onSetPrefs={directory?.onSetPrefs} onOpenSettings={directory?.onOpenSettings} onPatchRoom={directory?.onPatchRoom} /></li>;

  return <div className="home-projects"><div className="frame-doc home-projects__col">
    {/* Frame A: the title and the page's one primary action on the same line. The needs-you pill shares that line:
        its count lands seconds after the page draws, and a row of its own pushed everything down (W15-7). */}
    <div className="frame-doc__title">
      <h1>Home</h1>
      {needsYouCount > 0 && <button type="button" className="home-projects__needs"
        onClick={() => { if (onlyFirstMove) { openFirstMoves(); return; } closePane(); openPanel("activity", {}, { pushUrl: true }); }}>
        <span className="home-projects__dot" aria-hidden="true" />
        <span><span className="font-semibold text-foreground">{needWord(needsYouCount)}</span> · {onlyFirstMove ? FIRST_MOVE_LABEL : "Review"}</span>
        <ChevronRight size={14} aria-hidden="true" />
      </button>}
      {directory?.onCreate && <Button type="button" size="sm" className="h-7" onClick={() => setCreating(true)}>
        <Plus aria-hidden="true" />New channel</Button>}
    </div>
    {messaging && <AskLightbulb onAsk={messaging.askLightbulb} />}

    <section aria-labelledby="home-chats-heading" className="home-projects__section">
      <h2 id="home-chats-heading" className="home-projects__label">Chats</h2>
      <button type="button" className="home-projects__dm" onClick={() => openDm()}>
        <span className="min-w-0 flex-1 truncate font-semibold">Lightbulb</span>
        <RoomState room={dm} counts={dm ? counts.get(dm.roomId) : undefined} capped={capped} />
      </button>
    </section>

    {pinned.length > 0 && <section aria-labelledby="home-pinned-heading" className="home-projects__section">
      <h2 id="home-pinned-heading" className="home-projects__label">Pinned</h2>
      <ul className="home-projects__grid">{pinned.map(card)}</ul>
    </section>}

    <section aria-labelledby="home-projects-heading" className="home-projects__section">
      <div className="home-projects__head">
        <h2 id="home-projects-heading" className="home-projects__label">Channels</h2>
        {projects.length > 3 && <label className="home-projects__search">
          <Search size={14} aria-hidden="true" />
          <span className="sr-only">Find a channel</span>
          <input type="search" placeholder="Find a channel" value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>}
      </div>
      {!directory || directory.state === "loading" ? <p role="status" className="text-ui text-muted-foreground">Loading…</p>
        : directory.state === "error" ? <p role="alert" className="text-ui text-muted-foreground">Channels couldn&apos;t load.{" "}
          {directory.onRetry && <button type="button" className="underline hover:text-foreground" onClick={directory.onRetry}>Retry</button>}</p>
        : rest.length > 0 ? <ul className="home-projects__grid">{rest.map(card)}</ul>
        : q && !pinned.length ? <p className="text-ui text-muted-foreground">No channel matches “{query.trim()}”.</p> : null}
      {fresh && <StartAProject onStart={() => setCreating(true)} firstMoves={firstMoves} />}
    </section>
    {directory?.onCreate && <NewProjectDialog open={creating} onOpenChange={setCreating} busy={!!directory.creationBusy} error={directory.creationError}
      onCreate={async (creation) => {
        const created = await directory.onCreate!(creation);
        if (created !== false) activateMessagesPanel();
        return created !== false;
      }} />}
  </div></div>;
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

/** A card's one state line: "● N needs you · age" (warn), "◔ N working · age" (info), else the age alone. */
function RoomState({ room, counts, capped = false }: { room?: DirectoryRoom; counts?: RoomCounts; capped?: boolean }): ReactElement {
  const age = room?.lastMessageAt ? <time dateTime={room.lastMessageAt}>{timeAgo(room.lastMessageAt)}</time> : null;
  return <span className="home-projects__meta">
    {counts?.needsYou ? <><span className="home-projects__dot" aria-hidden="true" /><span className="text-foreground">{needWord(counts.needsYou)}</span>{age && " · "}</>
      : counts?.working ? <><CircleDashed size={12} aria-hidden="true" className="text-info" /><span>{counts.working}{capped ? "+" : ""} working</span>{age && " · "}</>
      : null}
    {age}
  </span>;
}

/** Tile · name · state and age. ⋯ (hover): Pin/Unpin · Settings · Archive. No topic, no message preview. */
function ProjectCard({ room, counts, capped, viewerId, onSetPrefs, onOpenSettings, onPatchRoom }: {
  room: DirectoryRoom; counts?: RoomCounts; capped: boolean; viewerId?: string;
  onSetPrefs?: (roomId: string, prefs: MemberPrefsRequest) => Promise<void>;
  onOpenSettings?: (roomId: string, from?: "home") => void;
  onPatchRoom?: (roomId: string, request: PatchRoomRequest) => Promise<void>;
}): ReactElement {
  const name = roomDisplayName(room, room.participants);
  const [failed, setFailed] = useState<string | null>(null);
  const role = room.members.find((m) => m.kind === "human" && m.id === viewerId)?.role;
  // The backend archives only a project's owner or admin's, never #general.
  const canArchive = !!onPatchRoom && room.kind === "channel" && !room.isDefault && (role === "owner" || role === "admin");
  const act = (action: () => Promise<void>, fallback: string) => { setFailed(null); action().catch((e) => setFailed(e instanceof Error && e.message ? e.message : fallback)); };
  return <div className="home-projects__card-wrap">
    <button type="button" className="home-projects__card" onClick={() => openRoom(room.roomId)} aria-label={cardLabel(name, counts, capped)}>
      <ProjectTile icon={room.icon} color={room.color} name={name} size={32} />
      <span className="grid min-w-0 flex-1">
        <span className="truncate font-semibold">{name}</span>
        <RoomState room={room} counts={counts} capped={capped} />
      </span>
    </button>
    {(onSetPrefs || onOpenSettings || canArchive) && <DropdownMenu>
      <DropdownMenuTrigger asChild><Button type="button" variant="ghost" size="icon-sm" className="home-projects__more" data-room-id={room.roomId} aria-label={`More for ${name}`}><MoreHorizontal aria-hidden="true" /></Button></DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {onSetPrefs && <DropdownMenuItem onSelect={() => act(() => onSetPrefs(room.roomId, { starred: !room.starred }), "The pin could not be saved.")}>
          {room.starred ? <PinOff aria-hidden="true" /> : <Pin aria-hidden="true" />}{room.starred ? "Unpin" : "Pin"}</DropdownMenuItem>}
        {onOpenSettings && room.kind === "channel" && <DropdownMenuItem onSelect={() => onOpenSettings(room.roomId, "home")}><Settings aria-hidden="true" />Settings</DropdownMenuItem>}
        {canArchive && <DropdownMenuItem onSelect={() => act(() => onPatchRoom!(room.roomId, { archived: true }), "The channel could not be archived.")}><Archive aria-hidden="true" />Archive</DropdownMenuItem>}
      </DropdownMenuContent>
    </DropdownMenu>}
    {failed && <p role="alert" className="text-caption text-err">{failed}</p>}
  </div>;
}

/** A new workspace: start a project, or begin from the first run's ideas (else the catalog's). */
function StartAProject({ onStart, firstMoves }: { onStart: () => void; firstMoves: readonly Idea[] }): ReactElement {
  const { ideas } = useIdeas();
  const starters = firstMoves.length ? firstMoves : orderIdeas(ideas).filter((idea) => !idea.suggested).slice(0, HOME_STARTERS);
  return <div className="home-projects__start">
    <p className="text-ui text-muted-foreground">A channel is where you and Lightbulb work on one thing.</p>
    <div className="flex flex-wrap gap-2">
      <Button type="button" size="sm" onClick={onStart}><Plus aria-hidden="true" />Start a channel</Button>
      {starters.length > 0 && <ul aria-label={firstMoves.length ? "First moves" : "Starter ideas"} className="contents">
        {starters.map((idea) => <li key={idea.id}>
          <Button type="button" size="sm" variant="outline" onClick={() => {
            if (firstMoves.length) { openFirstMoves(); return; }
            capture("ideas_opened", { source: "home_starter" }); openPanel("ideas", { idea: idea.id }, { pushUrl: true });
          }}>
            {idea.title}
          </Button>
        </li>)}
      </ul>}
    </div>
  </div>;
}
