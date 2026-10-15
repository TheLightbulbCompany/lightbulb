"use client";

// Ideas in the right pane (spec 2026-10-14 §4.4; calm like Muse's Ideas tab, owner 2026-10-15):
// every idea as one row (the DM agent's first, then agents' suggestions, agency jobs, everyday).
// A row opens the idea's detail in the pane; Let's do it starts it and opens the DM in the main
// view. A started idea leaves the list (owner: once you say Let's do it, you are doing the work).
import { useState } from "react";
import { orderIdeas } from "@lightbulb/shared";
import { IdeaDetail, IdeaListRow } from "@/components/ideas/IdeaCard";
import { useIdeas } from "@/hooks/useIdeas";
import { updatePanelParams } from "../workspaceController";
import type { PanelProps } from "../panelComponents";
import "@/components/ideas/ideas.css";

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
    {shown.length > 0 && <section aria-labelledby="ideas-all">
      <h2 id="ideas-all" className="ideas-pane__heading">Ideas</h2>
      <ul className="idea-list">{shown.map((idea) => <IdeaListRow key={idea.id} idea={idea} onOpen={setOpenId} />)}</ul>
    </section>}
    {!loading && !error && shown.length === 0 &&
      <p className="ideas-pane__notice">No ideas right now.</p>}
  </div>;
}
