"use client";

// Ideas, a showroom of what the team can do (owner, 2026-10-15): rows you tell apart at a glance
// (the idea's emoji on its section's tint, a bold outcome, a grey why) and, on a row, a detail
// that reads as a promise: who takes it and how fast, what you get, your part, "Let's do it", and
// a "…" menu with "Not interested" (dismiss). No prices.
import { useId, useState } from "react";
import { ArrowLeft, Check, MoreHorizontal } from "lucide-react";
import { ideaSectionKey, orderIdeas, type Idea } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Identity } from "@/components/teams/shared/components/Identity";
import { useIdeas } from "@/hooks/useIdeas";
import { openPanel } from "@/components/workspace/workspaceController";
import "./ideas.css";

/** The idea's emoji on its section's tint: the picture you scan before the words. */
export function IdeaTile({ idea, large = false }: { idea: Idea; large?: boolean }) {
  return <span aria-hidden="true" className={`idea-tile idea-tile--${ideaSectionKey(idea)}${large ? " idea-tile--large" : ""}`}>
    {idea.emoji}
  </span>;
}

/** One idea in the list: the whole row opens it. */
export function IdeaListRow({ idea, onOpen }: { idea: Idea; onOpen: (ideaId: string) => void }) {
  const id = useId();
  return <li className="idea-item">
    <button type="button" className="idea-item__button" aria-labelledby={`${id}-title`} aria-describedby={`${id}-why`}
      onClick={() => onOpen(idea.id)}>
      <IdeaTile idea={idea} />
      <span className="idea-item__text">
        <span id={`${id}-title`} className="idea-item__title">{idea.title}</span>
        <span id={`${id}-why`} className="idea-item__why">{idea.why}</span>
      </span>
    </button>
  </li>;
}

/** The idea opened: what it is, who takes it and how fast, what you get, your part, Let's do it,
 *  and "…" → Not interested. */
export function IdeaDetail({ idea, onBack, onStart, onDismiss }: {
  idea: Idea;
  onBack: () => void;
  /** Null once the DM is open; otherwise what went wrong (shown here, Let's do it stays). */
  onStart: (ideaId: string) => Promise<string | null>;
  onDismiss: (ideaId: string) => void;
}) {
  const [starting, setStarting] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const start = async () => {
    setStarting(true); // stays pressed until the DM opens: a second click cannot start twice
    setFailed(null);
    try { setFailed(await onStart(idea.id)); } finally { setStarting(false); }
  };
  return <article className="idea-detail" aria-label={idea.title}>
    <button type="button" className="idea-detail__back" onClick={onBack}><ArrowLeft size={16} aria-hidden="true" />All ideas</button>
    <IdeaTile idea={idea} large />
    <h2 className="idea-detail__title">{idea.title}</h2>
    <p className="idea-detail__why">{idea.why}</p>
    <p className="idea-detail__who">
      <Identity name={idea.agentName} avatarOnly size="xs" />
      <span>{idea.agentName} takes this{idea.ourTurnaround ? ` · ${idea.ourTurnaround}` : ""}</span>
    </p>
    <section className="idea-detail__included" aria-labelledby={`included-${idea.id}`}>
      <h3 id={`included-${idea.id}`} className="idea-detail__heading">What you get</h3>
      <ul>
        {idea.deliverables.map((line) => <li key={line}><span>{line}</span><Check size={16} aria-hidden="true" /></li>)}
      </ul>
    </section>
    {idea.yourPart && <p className="idea-detail__effort">Your part: {idea.yourPart}</p>}
    {failed && <p role="alert" className="idea-detail__error">{failed}</p>}
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
  const { ideas, anyStarted, anyDismissed } = useIdeas();
  // A catalog idea: an agent's suggestion is never the welcome's proposal.
  const top = orderIdeas(ideas).find((idea) => !idea.suggested);
  if (!top || anyStarted || anyDismissed) return null;
  return <div className="idea-welcome">
    <ul className="idea-list">
      <IdeaListRow idea={top} onOpen={(ideaId) => openPanel("ideas", { idea: ideaId }, { pushUrl: true })} />
    </ul>
    <button type="button" className="idea-welcome__all" onClick={() => openPanel("ideas", {}, { pushUrl: true })}>See all ideas</button>
  </div>;
}
