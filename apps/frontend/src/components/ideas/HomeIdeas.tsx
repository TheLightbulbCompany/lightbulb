"use client";

// Home's Ideas lane (product shape, 5 Nov 2026): what the team suggests doing next, because it is hard to know what
// to ask for. Each idea says what, why and which specialist would do it; Do it starts a Request and opens it,
// Not now puts it away (the server remembers, so it doesn't come straight back). With no suggestions yet, a few
// catalog ideas sit underneath as starter chips that open the idea in the Ideas pane.

import { useState, type ReactElement } from "react";
import { orderIdeas, type Idea } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { useIdeas } from "@/hooks/useIdeas";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { capture } from "@/lib/analytics";
import { openPanel } from "@/components/workspace/workspaceController";

/** Starter chips under an empty lane. */
export const STARTERS = 4;

export function HomeIdeas(): ReactElement {
  const { ideas, loading, error, refresh, startAndOpen, dismiss } = useIdeas();
  const suggested = ideas.filter((idea) => idea.suggested);
  const starters = orderIdeas(ideas).filter((idea) => !idea.suggested).slice(0, STARTERS);
  return <section aria-labelledby="home-ideas" className="home-ideas">
    <h2 id="home-ideas" className="text-ui font-semibold">Ideas{suggested.length > 0 && ` · ${suggested.length}`}</h2>
    {error ? <p role="alert" className="mt-1 text-ui text-muted-foreground">Ideas couldn&apos;t load. <button type="button" className="underline hover:text-foreground" onClick={() => void refresh()}>Retry</button></p>
      : suggested.length > 0 ? <ul className="home-ideas__list">
        {suggested.map((idea) => <IdeaItem key={idea.id} idea={idea} onStart={startAndOpen} onDismiss={dismiss} />)}
      </ul>
      : loading ? <p role="status" className="mt-1 text-ui text-muted-foreground">Loading…</p>
      : <div className="mt-1 rounded-lg border border-dashed border-border px-3 py-2">
        <p className="text-ui text-muted-foreground">Your team suggests ideas here as it learns your company. Or start with one of these:</p>
        {starters.length > 0 && <ul aria-label="Starter ideas" className="mt-2 flex flex-wrap gap-2">
          {starters.map((idea) => <li key={idea.id}>
            <Button type="button" size="sm" variant="outline" onClick={() => { capture("ideas_opened", { source: "home_starter" }); openPanel("ideas", { idea: idea.id }, { pushUrl: true }); }}>
              {idea.title}
            </Button>
          </li>)}
        </ul>}
      </div>}
  </section>;
}

function IdeaItem({ idea, onStart, onDismiss }: {
  idea: Idea; onStart: (ideaId: string) => Promise<string | null>; onDismiss: (ideaId: string) => void;
}): ReactElement {
  const { agentNames } = useLiveEvents();
  const [starting, setStarting] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  // The front agent reads "Lightbulb" (agentNames does that); a specialist by its own name.
  const who = agentNames.get(idea.agentId) ?? idea.agentName;
  const titleId = `home-idea-${idea.id}`;
  const start = async () => {
    setStarting(true); // stays pressed once it worked: the idea leaves the list on the refetch
    setFailed(null);
    const error = await onStart(idea.id);
    if (error) { setFailed(error); setStarting(false); }
  };
  return <li className="editorial-card flex flex-col gap-1 px-3 py-2" aria-labelledby={titleId}>
    <h3 id={titleId} className="font-semibold">{idea.title}</h3>
    <p className="line-clamp-2 text-ui text-muted-foreground">{idea.why}</p>
    <div className="mt-auto flex flex-wrap items-center gap-2 pt-1">
      <span className="mr-auto text-ui text-muted-foreground"><span className="text-foreground">{who}</span> would do this</span>
      <Button type="button" size="sm" variant="ghost" disabled={starting} onClick={() => onDismiss(idea.id)}>Not now</Button>
      <Button type="button" size="sm" disabled={starting} onClick={() => void start()}>{starting ? "Starting…" : "Do it"}</Button>
    </div>
    {failed && <p role="alert" className="text-ui text-err">{failed}</p>}
  </li>;
}
