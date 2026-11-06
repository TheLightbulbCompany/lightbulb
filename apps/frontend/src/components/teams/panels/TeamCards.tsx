"use client";

// Team, super-agent (product shape, 5 Nov 2026): one card per specialist saying what it does for people, the
// requests it did lately, and Ask (one verb on every card, Lightbulb's too). No skill names: they are internal slugs
// (dev walk W12-8). Each specialist is its own profile, so it is shown as itself. Lightbulb leads, one line: it coordinates your requests (never "Coordinator" or its roster "CEO").

import { useMemo, type ReactElement } from "react";
import { useAuth } from "@clerk/nextjs";
import { MoreHorizontal } from "lucide-react";
import { summaryView, useTaskList, type TaskScope, type TaskSummary } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { TaskStatus } from "@/components/tasks/TaskCard";
import { openTaskPane } from "@/components/workspace/workspaceController";
import type { CompanyAgent } from "@/components/teams/shared/types";

const WORKSPACE: TaskScope = { kind: "workspace" };
/** Recent requests per card. */
export const RECENT_SHOWN = 3;

/** Lightbulb's one line on its card. */
export const FRONT_LINE = "Hands your tasks to specialists";

/** What a specialist does for people, in one line: its persona's first sentence, else its title unless that is just
 *  its name (or a roster role like "CEO"). Null when there is nothing plain to say: never its skill slugs (dev walk W12-8). */
export function purposeLine(agent: Pick<CompanyAgent, "title" | "capabilities" | "name" | "role">): string | null {
  const persona = agent.capabilities?.trim().split(/(?<=[.!?])\s/)[0];
  if (persona) return persona.length > 140 ? `${persona.slice(0, 139)}…` : persona;
  const title = agent.title?.trim();
  const same = (word: string | null | undefined) => !!word && word.trim().toLowerCase() === title?.toLowerCase();
  return title && !same(agent.name) && !same(agent.role) ? title : null;
}

/** A request is this agent's when its specialist is the agent's id or handle; Lightbulb's when it has none. */
export function isAgentsRequest(task: Pick<TaskSummary, "specialist">, agent: { id: string; handle?: string | null; name: string }, front: boolean): boolean {
  const s = task.specialist?.toLowerCase();
  if (!s) return front;
  return s === agent.id.toLowerCase() || s === agent.handle?.toLowerCase() || s === agent.name.toLowerCase();
}

export function TeamCards({ agents, frontAgentId, nameOf, handleOf, onAsk, actions }: {
  agents: readonly CompanyAgent[];
  frontAgentId: string | undefined;
  nameOf: (agent: CompanyAgent) => string;
  handleOf: (agent: CompanyAgent) => string | null | undefined;
  onAsk: (agent: CompanyAgent) => void;
  /** The rare per-agent actions (pause, delete, open), for the card's ··· menu. */
  actions: (agent: CompanyAgent) => ReactElement;
}): ReactElement {
  const { tasks } = useLiveEvents();
  const { userId } = useAuth();
  const { data } = useTaskList(tasks, WORKSPACE);
  // Lightbulb first: it is the one every request starts with.
  const ordered = useMemo(() => [...agents].sort((a, b) => Number(b.id === frontAgentId) - Number(a.id === frontAgentId)), [agents, frontAgentId]);
  // Columns follow the panel's own width, not the viewport's (the rail, a drawer or the pane take part of it), and
  // every track is minmax(0, …) so a long request title truncates instead of widening the page (dev walk W8-2).
  return <ul className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,260px),1fr))] gap-3" aria-label="Specialists">
    {ordered.map((agent) => {
      const front = agent.id === frontAgentId;
      const recent = (data?.tasks ?? [])
        .filter((t) => isAgentsRequest(t, { id: agent.id, handle: handleOf(agent), name: agent.name }, front))
        .sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt)).slice(0, RECENT_SHOWN);
      return <TeamCard key={agent.id} agent={agent} name={nameOf(agent)} front={front} recent={recent} loaded={!!data} viewerId={userId ?? ""}
        onAsk={() => onAsk(agent)} menu={actions(agent)} />;
    })}
  </ul>;
}

function TeamCard({ agent, name, front, recent, loaded, viewerId, onAsk, menu }: {
  agent: CompanyAgent; name: string; front: boolean; recent: TaskSummary[]; loaded: boolean; viewerId: string; onAsk: () => void; menu: ReactElement;
}): ReactElement {
  // Lightbulb: its one line only (its roster title is "CEO", its persona the coordinator's instructions).
  const purpose = front ? null : purposeLine(agent);
  const headingId = `team-${agent.id}`;
  const paused = agent.status === "paused";
  return <li className="editorial-card flex min-w-0 flex-col gap-2 px-4 py-3" aria-labelledby={headingId}>
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
        <h3 id={headingId} className="truncate font-semibold">{name}</h3>
        {/* Lightbulb's line reads like any specialist's purpose, not muted (W14). */}
        {(front || paused) && <p className="text-ui">{front && FRONT_LINE}{front && paused && " · "}{paused && <span className="text-warn">Paused</span>}</p>}
      </div>
      <DropdownMenu>
        <DropdownMenuTrigger asChild><Button type="button" size="icon-sm" variant="ghost" aria-label={`More for ${name}`}><MoreHorizontal aria-hidden="true" /></Button></DropdownMenuTrigger>
        <DropdownMenuContent align="end">{menu}</DropdownMenuContent>
      </DropdownMenu>
    </div>
    {purpose && <p className="text-ui">{purpose}</p>}
    <section aria-label={`${name}'s recent tasks`} className="grid grid-cols-[minmax(0,1fr)] gap-0.5 border-t border-border pt-2">
      {recent.length ? recent.map((t) => <button key={t.taskId} type="button" onClick={() => openTaskPane(t.taskId)}
        aria-label={`Open ${t.title}, ${summaryView(t, viewerId).word}`}
        className="-mx-1 flex items-center gap-2 rounded-xs px-1 py-0.5 text-left text-ui hover:bg-accent">
        {/* The status only when it says something: a recent task is Done, so "• Done" on every row was noise (W14). */}
        <span className="min-w-0 flex-1 truncate">{t.title}</span>{!["Done", "Closed"].includes(summaryView(t, viewerId).word) && <TaskStatus task={t} viewerId={viewerId} />}
      </button>) : <p className="text-ui text-muted-foreground">{loaded ? "No tasks yet." : "Loading…"}</p>}
    </section>
    <div className="mt-auto pt-1">
      <Button type="button" size="sm" variant="outline" onClick={onAsk}
        aria-label={front ? `Ask ${name}` : `Ask ${name} through Lightbulb`}>Ask</Button>
    </div>
  </li>;
}
