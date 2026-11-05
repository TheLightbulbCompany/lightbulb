"use client";

// Team, super-agent (product shape, 5 Nov 2026): one card per specialist saying what it is for, what it can use
// (its Hermes profile's skills), the requests it did lately, and Ask. Each specialist is its own profile, so it is
// shown as itself. Lightbulb leads as the coordinator: it starts every request and hands it on.

import { useMemo, type ReactElement } from "react";
import { useAuth } from "@clerk/nextjs";
import { MoreHorizontal } from "lucide-react";
import { summaryView, useStatusLoaded, useTaskList, type TaskScope, type TaskSummary } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { useRuntimeRpc } from "@/hooks/useRuntimeRpc";
import { TaskStatus } from "@/components/tasks/TaskCard";
import { openTaskPane } from "@/components/workspace/workspaceController";
import type { CompanyAgent } from "@/components/teams/shared/types";

const WORKSPACE: TaskScope = { kind: "workspace" };
/** Skills named on a card before "+N more". */
export const SKILLS_SHOWN = 6;
/** Recent requests per card. */
export const RECENT_SHOWN = 3;

interface Skill { name: string; description?: string; disabled?: boolean; eligible?: boolean }

/** What a specialist is for, in one line: its title, else its persona's first sentence, else what its skills say,
 *  else (Lightbulb) what a coordinator does. Null when there is nothing true to say. */
export function purposeLine(agent: Pick<CompanyAgent, "title" | "capabilities">, skills: readonly Skill[], front: boolean): string | null {
  const title = agent.title?.trim();
  if (title) return title;
  const persona = agent.capabilities?.trim().split(/(?<=[.!?])\s/)[0];
  if (persona) return persona.length > 140 ? `${persona.slice(0, 139)}…` : persona;
  if (front) return "Starts every request and hands it to the right specialist.";
  const names = skills.slice(0, 3).map((s) => s.name);
  if (!names.length) return null;
  const list = names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
  return `Works with ${list}.`;
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
  return <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3" aria-label="Specialists">
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
  // The profile's skills (`skills.status`, as the agent's Skills tab reads them), held until the runtime is known:
  // asking on a guess starts a microVM.
  const statusLoaded = useStatusLoaded();
  const { data: raw } = useRuntimeRpc<{ skills?: Skill[] } | Skill[]>(statusLoaded ? "skills.status" : null, { agentId: agent.id });
  const skills = (Array.isArray(raw) ? raw : raw?.skills ?? []).filter((s) => !s.disabled);
  const purpose = purposeLine(agent, skills, front);
  const headingId = `team-${agent.id}`;
  const paused = agent.status === "paused";
  return <li className="editorial-card flex flex-col gap-2 px-4 py-3" aria-labelledby={headingId}>
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
        <h3 id={headingId} className="truncate font-semibold">{name}</h3>
        <p className="text-ui text-muted-foreground">{front ? "Coordinator" : "Specialist"}{paused && <> · <span className="text-warn">Paused</span></>}</p>
      </div>
      <DropdownMenu>
        <DropdownMenuTrigger asChild><Button type="button" size="icon-sm" variant="ghost" aria-label={`More for ${name}`}><MoreHorizontal aria-hidden="true" /></Button></DropdownMenuTrigger>
        <DropdownMenuContent align="end">{menu}</DropdownMenuContent>
      </DropdownMenu>
    </div>
    {purpose && <p className="text-ui">{purpose}</p>}
    {skills.length > 0 && <div>
      <h4 className="sr-only">Skills</h4>
      <ul aria-label={`${name}'s skills`} className="flex flex-wrap gap-1">
        {skills.slice(0, SKILLS_SHOWN).map((s) => <li key={s.name} title={s.description} className="rounded-xs border border-border px-1.5 text-caption text-muted-foreground">{s.name}</li>)}
        {skills.length > SKILLS_SHOWN && <li className="px-1 text-caption text-muted-foreground">+{skills.length - SKILLS_SHOWN} more</li>}
      </ul>
    </div>}
    <section aria-label={`${name}'s recent requests`} className="grid gap-0.5 border-t border-border pt-2">
      {recent.length ? recent.map((t) => <button key={t.taskId} type="button" onClick={() => openTaskPane(t.taskId)}
        aria-label={`Open ${t.title}, ${summaryView(t, viewerId).word}`}
        className="-mx-1 flex items-center gap-2 rounded-xs px-1 py-0.5 text-left text-ui hover:bg-accent">
        <span className="min-w-0 flex-1 truncate">{t.title}</span><TaskStatus task={t} viewerId={viewerId} />
      </button>) : <p className="text-ui text-muted-foreground">{loaded ? "No requests yet." : "Loading…"}</p>}
    </section>
    <div className="mt-auto pt-1">
      <Button type="button" size="sm" variant="outline" onClick={onAsk}
        aria-label={front ? `Message ${name}` : `Ask ${name} through Lightbulb`}>{front ? "Message" : "Ask"}</Button>
    </div>
  </li>;
}
