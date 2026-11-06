"use client";

// Team, super-agent (product shape, 5 Nov 2026): one card per specialist saying what it is for, what it can use
// (its own top skills), the requests it did lately, and Ask. Each specialist is its own profile, so it is
// shown as itself. Lightbulb leads, one line: it coordinates your requests (never "Coordinator" or its roster "CEO").

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
/** Skills named on a card. */
export const SKILLS_SHOWN = 5;
/** Recent requests per card. */
export const RECENT_SHOWN = 3;

interface Skill { name: string; description?: string; disabled?: boolean; eligible?: boolean; source?: string }

/** Lightbulb's one line on its card. */
export const FRONT_LINE = "Hands your tasks to specialists";

/** Every profile has these (the runtime's own tooling, Lightbulb's plumbing), so they say nothing about a specialist
 *  (dev walk W7-7). ponytail: a name list; a `userFacing` flag from the bridge if the image's skills churn. */
const PLUMBING_SKILLS = new Set(["lightbulb", "paperclip", "claude-code", "codex", "opencode", "computer-use", "hermes-agent", "native-mcp", "mcporter"]);
const PLUMBING_SOURCES = new Set(["autonomous-ai-agents", "mcp"]);
export const userFacingSkills = (skills: readonly Skill[]): Skill[] =>
  skills.filter((s) => !s.disabled && !PLUMBING_SKILLS.has(s.name.toLowerCase()) && !PLUMBING_SOURCES.has(s.source ?? ""));

const COMMON = new Set(["with", "from", "that", "this", "your", "into", "about", "when", "what", "them", "they", "their", "have", "will",
  "make", "uses", "using", "work", "works", "owns", "help", "helps", "agent", "skill", "skills", "tool", "tools", "lightbulb"]);
/** Word stems of a text, for matching a skill to what a specialist is for ("customers" and "customer" meet). */
const stems = (text: string): Set<string> =>
  new Set(text.toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length >= 4 && !COMMON.has(w)).map((w) => w.slice(0, 6)));

/** A specialist's own top skills (dev walk W8-7). Every profile carries the image's whole set, so the full list is the
 *  same on every card; these are the enabled ones its persona, title or role talks about, best match first. None when
 *  nothing matches: an empty row says more than 47 tags shared by everyone.
 *  ponytail: word overlap; a per-agent skill list from the roster replaces it once the backend keeps one. */
export function topSkills(agent: Pick<CompanyAgent, "title" | "capabilities" | "name" | "role">, skills: readonly Skill[], n = SKILLS_SHOWN): Skill[] {
  const about = stems(`${agent.name} ${agent.title ?? ""} ${agent.role ?? ""} ${agent.capabilities ?? ""}`);
  return skills.map((skill, i) => ({ skill, i, score: [...stems(`${skill.name} ${skill.description ?? ""}`)].filter((w) => about.has(w)).length }))
    .filter((x) => x.score > 0).sort((a, b) => b.score - a.score || a.i - b.i).slice(0, n).map((x) => x.skill);
}

/** What a specialist is for, in one line: its persona's first sentence, else its title unless that is just its name
 *  (or a roster role like "CEO"), else what its skills say. Null when there is nothing true to say. */
export function purposeLine(agent: Pick<CompanyAgent, "title" | "capabilities" | "name" | "role">, skills: readonly Skill[]): string | null {
  const persona = agent.capabilities?.trim().split(/(?<=[.!?])\s/)[0];
  if (persona) return persona.length > 140 ? `${persona.slice(0, 139)}…` : persona;
  const title = agent.title?.trim();
  const same = (word: string | null | undefined) => !!word && word.trim().toLowerCase() === title?.toLowerCase();
  if (title && !same(agent.name) && !same(agent.role)) return title;
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
  // Columns follow the panel's own width, not the viewport's (the rail, a drawer or the pane take part of it), and
  // every track is minmax(0, …) so a long request title truncates instead of widening the page (dev walk W8-2).
  return <ul className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,18rem),1fr))] gap-3" aria-label="Specialists">
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
  // Lightbulb: its one line only (its roster title is "CEO", its persona the coordinator's instructions).
  const skills = front ? [] : topSkills(agent, userFacingSkills(Array.isArray(raw) ? raw : raw?.skills ?? []));
  const purpose = front ? null : purposeLine(agent, skills);
  const headingId = `team-${agent.id}`;
  const paused = agent.status === "paused";
  return <li className="editorial-card flex min-w-0 flex-col gap-2 px-4 py-3" aria-labelledby={headingId}>
    <div className="flex items-start gap-2">
      <div className="min-w-0 flex-1">
        <h3 id={headingId} className="truncate font-semibold">{name}</h3>
        <p className="text-ui text-muted-foreground">{front ? FRONT_LINE : "Specialist"}{paused && <> · <span className="text-warn">Paused</span></>}</p>
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
        {skills.map((s) => <li key={s.name} title={s.description} className="rounded-xs border border-border px-1.5 text-caption text-muted-foreground">{s.name}</li>)}
      </ul>
    </div>}
    <section aria-label={`${name}'s recent tasks`} className="grid grid-cols-[minmax(0,1fr)] gap-0.5 border-t border-border pt-2">
      {recent.length ? recent.map((t) => <button key={t.taskId} type="button" onClick={() => openTaskPane(t.taskId)}
        aria-label={`Open ${t.title}, ${summaryView(t, viewerId).word}`}
        className="-mx-1 flex items-center gap-2 rounded-xs px-1 py-0.5 text-left text-ui hover:bg-accent">
        <span className="min-w-0 flex-1 truncate">{t.title}</span><TaskStatus task={t} viewerId={viewerId} />
      </button>) : <p className="text-ui text-muted-foreground">{loaded ? "No tasks yet." : "Loading…"}</p>}
    </section>
    <div className="mt-auto pt-1">
      <Button type="button" size="sm" variant="outline" onClick={onAsk}
        aria-label={front ? `Message ${name}` : `Ask ${name} through Lightbulb`}>{front ? "Message" : "Ask"}</Button>
    </div>
  </li>;
}
