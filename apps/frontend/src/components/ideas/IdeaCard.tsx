"use client";

// The idea as a proposal (spec 2026-10-14 §4.4): who does it, the outcome as the headline, why,
// three checkmarks, one effort line, one Start, a quiet × for "not now". No prices anywhere.
import { useState } from "react";
import { Check, X } from "lucide-react";
import { ideaEffortLine, orderIdeas, type Idea } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Identity } from "@/components/teams/shared/components/Identity";
import { useIdeas } from "@/hooks/useIdeas";
import "./ideas.css";

export interface IdeaActions {
  onStart: (ideaId: string) => Promise<void>;
  onDismiss: (ideaId: string) => void;
}

/** Start stays pressed until the DM opens, so a second click cannot start twice. */
function useStarting(onStart: IdeaActions["onStart"]) {
  const [starting, setStarting] = useState(false);
  return [starting, async (ideaId: string) => {
    setStarting(true);
    try { await onStart(ideaId); } finally { setStarting(false); }
  }] as const;
}

export function IdeaCard({ idea, onStart, onDismiss }: { idea: Idea } & IdeaActions) {
  const [starting, start] = useStarting(onStart);
  return <article className="idea-card" aria-label={idea.title}>
    <div className="idea-card__who">
      <Identity name={idea.agentName} avatarOnly size="sm" />
      <span>{idea.agentName} can do this for you</span>
      <button type="button" className="idea-card__dismiss" aria-label={`Not now: ${idea.title}`} title="Not now"
        onClick={() => onDismiss(idea.id)}><X size={16} aria-hidden="true" /></button>
    </div>
    <h3 className="idea-card__title">{idea.title}</h3>
    <p className="idea-card__why">{idea.why}</p>
    <ul className="idea-card__checks">
      {idea.deliverables.map((line) => <li key={line}><Check size={14} aria-hidden="true" />{line}</li>)}
    </ul>
    <div className="idea-card__foot">
      <span className="idea-card__effort">{ideaEffortLine(idea)}</span>
      <Button type="button" size="sm" disabled={starting} onClick={() => void start(idea.id)}>
        {starting ? "Starting…" : "Start"}
      </Button>
    </div>
  </article>;
}

/** One line: an idea past the top three. */
export function IdeaRow({ idea, onStart, onDismiss }: { idea: Idea } & IdeaActions) {
  const [starting, start] = useStarting(onStart);
  return <li className="idea-row">
    <div>
      <div className="idea-row__title">{idea.title}</div>
      <div className="idea-row__meta">{idea.agentName} · {ideaEffortLine(idea)}</div>
    </div>
    <div className="idea-row__actions">
      <Button type="button" size="sm" variant="outline" disabled={starting} aria-label={`Start: ${idea.title}`}
        onClick={() => void start(idea.id)}>{starting ? "Starting…" : "Start"}</Button>
      <button type="button" className="idea-card__dismiss" aria-label={`Not now: ${idea.title}`} title="Not now"
        onClick={() => onDismiss(idea.id)}><X size={14} aria-hidden="true" /></button>
    </div>
  </li>;
}

/** Under the CEO's welcome: the top idea as a proposal, and where the rest are. It goes for good
 *  once the workspace has started or hidden an idea: it is a first step, not a banner. */
export function WelcomeIdea() {
  const { ideas, running, anyStarted, anyDismissed, startAndOpen, dismiss } = useIdeas();
  const top = orderIdeas(ideas)[0];
  if (!top || running.length > 0 || anyStarted || anyDismissed) return null;
  return <div className="idea-welcome">
    <IdeaCard idea={top} onStart={startAndOpen} onDismiss={dismiss} />
    <p className="idea-welcome__pointer">More ideas are under the 💡 at the top of any conversation.</p>
  </div>;
}
