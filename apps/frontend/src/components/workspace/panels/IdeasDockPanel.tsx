"use client";

// Ideas in the right pane (spec 2026-10-14 §4.4): running jobs and their progress at the top,
// then the best three as full proposals, then the rest as one-line rows, filterable by teammate.
// Opened from an agent's DM (`params.agent`), that agent's ideas come first. Start opens the DM in
// the main view and the pane stays, showing the idea as running.
import { useState } from "react";
import { ideaProgress, ideaTeammates, orderIdeas, type RunningIdea } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { IdeaCard, IdeaRow } from "@/components/ideas/IdeaCard";
import { useIdeas } from "@/hooks/useIdeas";
import { openMessagePanel } from "../workspaceController";
import type { PanelProps } from "../panelComponents";
import "@/components/ideas/ideas.css";

const TOP = 3;

/** Open goes to the starter's own DM with the agent, so only the starter gets it. */
function RunningRow({ item, mine }: { item: RunningIdea; mine: boolean }) {
  const progress = ideaProgress(item.progress);
  return <li className="idea-row">
    <div>
      <div className="idea-row__title">{item.title}</div>
      <div className="idea-row__meta">
        {[item.agentName, progress ? `${progress.done} of ${progress.total} tasks done` : "Planning",
          mine ? null : `Started by ${item.starterName}`].filter(Boolean).join(" · ")}
      </div>
    </div>
    {mine && item.roomId && <Button type="button" size="sm" variant="outline" aria-label={`Open: ${item.title}`}
      onClick={() => openMessagePanel(item.roomId!)}>Open</Button>}
  </li>;
}

export function IdeasDockPanel({ params }: PanelProps) {
  const { ideas, running, loading, error, refresh, startAndOpen, dismiss, userId } = useIdeas();
  const [teammate, setTeammate] = useState<string | null>(null);
  // A start is per person: a teammate running an idea leaves it startable in your own DM.
  const runningIds = new Set(running.filter((r) => r.starterId === userId).map((r) => r.ideaId));
  const ordered = orderIdeas(ideas.filter((idea) => !runningIds.has(idea.id)), params.agent);
  const shown = teammate ? ordered.filter((idea) => idea.agentId === teammate) : ordered;
  const shownRunning = teammate ? running.filter((r) => r.agentId === teammate) : running;
  const teammates = ideaTeammates(ordered);

  return <div className="ideas-pane">
    {error ? <p role="alert" className="ideas-pane__notice">
      Couldn&apos;t load ideas · <button type="button" className="underline" onClick={() => void refresh()}>Retry</button>
    </p> : null}
    {teammates.length > 1 && <div className="ideas-pane__chips" role="group" aria-label="Teammate">
      <button type="button" className="ideas-pane__chip" aria-pressed={teammate === null} onClick={() => setTeammate(null)}>All</button>
      {teammates.map((t) => <button key={t.agentId} type="button" className="ideas-pane__chip" aria-pressed={teammate === t.agentId}
        onClick={() => setTeammate(t.agentId)}>{t.agentName}</button>)}
    </div>}
    {shownRunning.length > 0 && <section aria-labelledby="ideas-running">
      <h2 id="ideas-running" className="ideas-pane__heading">Running</h2>
      <ul className="ideas-pane__list">{shownRunning.map((item) => <RunningRow key={`${item.ideaId}:${item.starterId}`} item={item} mine={item.starterId === userId} />)}</ul>
    </section>}
    {shown.length > 0 && <section aria-label="Proposals" className="ideas-pane__list">
      {shown.slice(0, TOP).map((idea) => <IdeaCard key={idea.id} idea={idea} onStart={startAndOpen} onDismiss={dismiss} />)}
    </section>}
    {shown.length > TOP && <section aria-labelledby="ideas-more">
      <h2 id="ideas-more" className="ideas-pane__heading">More ideas</h2>
      <ul className="ideas-pane__list">
        {shown.slice(TOP).map((idea) => <IdeaRow key={idea.id} idea={idea} onStart={startAndOpen} onDismiss={dismiss} />)}
      </ul>
    </section>}
    {!loading && !error && shown.length === 0 && shownRunning.length === 0 &&
      <p className="ideas-pane__notice">No ideas right now.</p>}
  </div>;
}
