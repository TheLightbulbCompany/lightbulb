"use client";

// Ideas in the right pane (spec 2026-10-14 §4.4; calm like Muse's Ideas tab, owner 2026-10-15):
// running jobs and their progress at the top, then every idea as one row (the DM agent's first,
// then agency jobs, then everyday). A row opens the idea's detail in the pane; Let's do it starts
// it and opens the DM in the main view, and the pane stays, showing the idea as running.
import { useState } from "react";
import { orderIdeas, runningLine, type RunningIdea } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { IdeaDetail, IdeaListRow } from "@/components/ideas/IdeaCard";
import { useIdeas } from "@/hooks/useIdeas";
import { openMessagePanel, updatePanelParams } from "../workspaceController";
import type { PanelProps } from "../panelComponents";
import "@/components/ideas/ideas.css";

/** Open goes to the starter's own DM with the agent, so only the starter gets it. */
function RunningRow({ item, mine }: { item: RunningIdea; mine: boolean }) {
  return <li className="idea-row">
    <div>
      <div className="idea-row__title">{item.title}</div>
      <div className="idea-row__meta">
        {[item.agentName, runningLine(item),
          mine ? null : `Started by ${item.starterName}`].filter(Boolean).join(" · ")}
      </div>
    </div>
    {mine && item.roomId && <Button type="button" size="sm" variant="outline" aria-label={`Open: ${item.title}`}
      onClick={() => openMessagePanel(item.roomId!)}>Open</Button>}
  </li>;
}

export function IdeasDockPanel({ id, params }: PanelProps) {
  const { ideas, running, loading, error, refresh, startAndOpen, dismiss, userId } = useIdeas();
  // The open idea: from a link (the CEO welcome's row) or a row clicked here.
  const [openId, setOpenId] = useState<string | null>(params.idea ?? null);
  const [linked, setLinked] = useState(params.idea);
  if (params.idea !== linked) {
    setLinked(params.idea);
    setOpenId(params.idea ?? null);
  }
  const close = () => {
    setOpenId(null);
    if (params.idea) updatePanelParams(id, { idea: undefined });
  };
  // A start is per person: a teammate running an idea leaves it startable in your own DM.
  const runningIds = new Set(running.filter((r) => r.starterId === userId).map((r) => r.ideaId));
  const shown = orderIdeas(ideas.filter((idea) => !runningIds.has(idea.id)), params.agent);
  const open = openId ? ideas.find((idea) => idea.id === openId) : undefined;

  if (open) {
    return <div className="ideas-pane">
      <IdeaDetail idea={open} onBack={close} onDismiss={dismiss}
        onStart={async (ideaId) => { await startAndOpen(ideaId); close(); }} />
    </div>;
  }
  return <div className="ideas-pane">
    {error ? <p role="alert" className="ideas-pane__notice">
      Couldn&apos;t load ideas · <button type="button" className="underline" onClick={() => void refresh()}>Retry</button>
    </p> : null}
    {running.length > 0 && <section aria-labelledby="ideas-running">
      <h2 id="ideas-running" className="ideas-pane__heading">Running</h2>
      <ul className="ideas-pane__list">{running.map((item) => <RunningRow key={`${item.ideaId}:${item.starterId}`} item={item} mine={item.starterId === userId} />)}</ul>
    </section>}
    {shown.length > 0 && <section aria-labelledby="ideas-all">
      <h2 id="ideas-all" className="ideas-pane__heading">Ideas</h2>
      <ul className="idea-list">{shown.map((idea) => <IdeaListRow key={idea.id} idea={idea} onOpen={setOpenId} />)}</ul>
    </section>}
    {!loading && !error && shown.length === 0 && running.length === 0 &&
      <p className="ideas-pane__notice">No ideas right now.</p>}
  </div>;
}
