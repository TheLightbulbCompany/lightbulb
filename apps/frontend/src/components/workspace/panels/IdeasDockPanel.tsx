"use client";

// Ideas in the right pane (spec 2026-10-14 §4.4; owner 2026-10-15): every idea as one row, in
// sections that show the team's range (For you, Money, Grow, Run the day, Build); inside one,
// the DM agent's first, then agency jobs, then everyday.
// A row opens the idea's detail in the pane; Let's do it starts it and opens the DM in the main
// view. A started idea leaves the list (owner: once you say Let's do it, you are doing the work).
// The list is the team's suggestions, the same items as Home's Ideas with Do it / Not now (dev walk W7: the room's
// Ideas toggle showed only the catalog); the catalog sections are the starters while there are none.
import { useState } from "react";
import { ideaSections, orderIdeas } from "@lightbulb/shared";
import { IdeaDetail, IdeaListRow } from "@/components/ideas/IdeaCard";
import { IdeaItem } from "@/components/ideas/HomeIdeas";
import { useIdeas } from "@/hooks/useIdeas";
import { updatePanelParams } from "../workspaceController";
import type { PanelProps } from "../panelComponents";
import "@/components/ideas/ideas.css";
import "@/components/activity/activity.css";

export function IdeasDockPanel({ id, params }: PanelProps) {
  const { ideas, loading, error, refresh, startAndOpen, dismiss } = useIdeas();
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
  const shown = orderIdeas(ideas, params.agent);
  const suggested = shown.filter((idea) => idea.suggested);
  const open = openId ? ideas.find((idea) => idea.id === openId) : undefined;

  if (open) {
    return <div className="ideas-pane">
      <IdeaDetail idea={open} onBack={close} onDismiss={dismiss}
        onStart={async (ideaId) => {
          const failed = await startAndOpen(ideaId);
          if (!failed) close(); // only once the DM is open: a failure keeps the detail, to retry
          return failed;
        }} />
    </div>;
  }
  return <div className="ideas-pane">
    {error ? <p role="alert" className="ideas-pane__notice">
      Couldn&apos;t load ideas · <button type="button" className="underline" onClick={() => void refresh()}>Retry</button>
    </p> : null}
    {suggested.length > 0 ? <section aria-labelledby="ideas-suggested">
      <h2 id="ideas-suggested" className="ideas-pane__heading">Suggested · {suggested.length}</h2>
      <ul className="home-ideas__list">{suggested.map((idea) => <IdeaItem key={idea.id} idea={idea} onStart={startAndOpen} onDismiss={dismiss} />)}</ul>
    </section> : !loading && !error && shown.length > 0 && <p className="ideas-pane__notice">Your team suggests ideas here as it learns your company. Or start with one of these:</p>}
    {suggested.length === 0 && ideaSections(shown).map((section) => <section key={section.key} aria-labelledby={`ideas-${section.key}`}>
      <h2 id={`ideas-${section.key}`} className="ideas-pane__heading">{section.title}</h2>
      <ul className="idea-list">{section.ideas.map((idea) => <IdeaListRow key={idea.id} idea={idea} onOpen={setOpenId} />)}</ul>
    </section>)}
    {!loading && !error && shown.length === 0 &&
      <p className="ideas-pane__notice">No ideas right now.</p>}
  </div>;
}
