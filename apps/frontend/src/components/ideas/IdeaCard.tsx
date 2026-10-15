"use client";

// Ideas, calm like Muse's Ideas tab (owner, 2026-10-15): a list of rows (the agent's avatar, a
// bold title, a few grey lines of why) and, on a row, a detail view with what's included, the
// effort line, "Let's do it" (Start) and a "…" menu with "Not interested" (dismiss). No prices.
import { useId, useState } from "react";
import { ArrowLeft, Check, MoreHorizontal } from "lucide-react";
import { ideaEffortLine, orderIdeas, type Idea } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Identity } from "@/components/teams/shared/components/Identity";
import { useIdeas } from "@/hooks/useIdeas";
import { openPanel } from "@/components/workspace/workspaceController";
import "./ideas.css";

/** One idea in the list: the whole row opens it. */
export function IdeaListRow({ idea, onOpen }: { idea: Idea; onOpen: (ideaId: string) => void }) {
  const id = useId();
  return <li className="idea-item">
    <button type="button" className="idea-item__button" aria-labelledby={`${id}-title`} aria-describedby={`${id}-why`}
      onClick={() => onOpen(idea.id)}>
      <Identity name={idea.agentName} avatarOnly size="sm" />
      <span className="idea-item__text">
        <span id={`${id}-title`} className="idea-item__title">{idea.title}</span>
        <span id={`${id}-why`} className="idea-item__why">{idea.why}</span>
      </span>
    </button>
  </li>;
}

/** The idea opened: what it is, what's included, the effort, Let's do it, and "…" → Not interested. */
export function IdeaDetail({ idea, onBack, onStart, onDismiss }: {
  idea: Idea;
  onBack: () => void;
  onStart: (ideaId: string) => Promise<void>;
  onDismiss: (ideaId: string) => void;
}) {
  const [starting, setStarting] = useState(false);
  const start = async () => {
    setStarting(true); // stays pressed until the DM opens: a second click cannot start twice
    try { await onStart(idea.id); } finally { setStarting(false); }
  };
  return <article className="idea-detail" aria-label={idea.title}>
    <button type="button" className="idea-detail__back" onClick={onBack}><ArrowLeft size={16} aria-hidden="true" />All ideas</button>
    <h2 className="idea-detail__title">{idea.title}</h2>
    <p className="idea-detail__why">{idea.why}</p>
    <section className="idea-detail__included" aria-labelledby={`included-${idea.id}`}>
      <h3 id={`included-${idea.id}`} className="idea-detail__heading">What&apos;s included</h3>
      <ul>
        {idea.deliverables.map((line) => <li key={line}><span>{line}</span><Check size={16} aria-hidden="true" /></li>)}
      </ul>
    </section>
    <p className="idea-detail__effort">{ideaEffortLine(idea)}</p>
    <div className="idea-detail__actions">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button type="button" variant="outline" size="icon" aria-label="More options"><MoreHorizontal aria-hidden="true" /></Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          <DropdownMenuItem onSelect={() => { onDismiss(idea.id); onBack(); }}>Not interested</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Button type="button" className="idea-detail__go" disabled={starting} onClick={() => void start()}>
        {starting ? "Starting…" : "Let's do it"}
      </Button>
    </div>
  </article>;
}

/** Under the CEO's welcome: the top idea as one row, and the way to the rest. It goes for good
 *  once the workspace has started or hidden an idea: it is a first step, not a banner. */
export function WelcomeIdea() {
  const { ideas, running, anyStarted, anyDismissed } = useIdeas();
  const top = orderIdeas(ideas)[0];
  if (!top || running.length > 0 || anyStarted || anyDismissed) return null;
  return <div className="idea-welcome">
    <ul className="idea-list">
      <IdeaListRow idea={top} onOpen={(ideaId) => openPanel("ideas", { idea: ideaId }, { pushUrl: true })} />
    </ul>
    <button type="button" className="idea-welcome__all" onClick={() => openPanel("ideas", {}, { pushUrl: true })}>See all ideas</button>
  </div>;
}
