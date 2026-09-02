// frontend/src/components/chat/AgentProgressCard.tsx
/**
 * Durable progress card, sticky above the composer.
 *
 * Reads the agent's persisted progress card (OpenClaw 8.1's `progress_card`
 * tool, per-agent SQLite) via useProgressCard — unlike the old live "plan"
 * pill built from `agent_activity` events, this survives a page reload.
 * Renders nothing when there's no card for the active session.
 */
"use client";

import { CheckCircle, Circle, ChevronDown, Loader2 } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { MarkdownContent } from "@/components/chat/MessageList";
import { useProgressCard, type ProgressCardStep } from "@/hooks/useProgressCard";

function StepGlyph({ status }: { status: ProgressCardStep["status"] }) {
  switch (status) {
    case "completed":
      return <CheckCircle className="h-4 w-4 text-ok shrink-0" aria-hidden="true" />;
    case "in_progress":
      return <Loader2 className="h-4 w-4 animate-spin text-[var(--midnight)] shrink-0" aria-hidden="true" />;
    case "pending":
      return <Circle className="h-4 w-4 text-[var(--midnight-faint)] shrink-0" aria-hidden="true" />;
  }
}

export function AgentProgressCard({ sessionKey }: { sessionKey: string | null }) {
  const { card } = useProgressCard(sessionKey);
  if (!card) return null;

  const steps = card.steps ?? [];
  const completed = steps.filter((s) => s.status === "completed").length;
  const currentStep =
    steps.find((s) => s.status === "in_progress") ?? steps.find((s) => s.status === "pending");

  return (
    <Collapsible
      defaultOpen
      className="shrink-0 border-t border-[var(--edge)] bg-[var(--cream-deep)]"
    >
      <CollapsibleTrigger className="flex w-full items-center gap-2 px-4 py-2 text-left hover:bg-[var(--edge)] transition-colors">
        <span className="label-caps text-[var(--midnight-muted)]">Progress</span>
        {currentStep && (
          <span className="text-sm text-[var(--midnight)] truncate flex-1">{currentStep.step}</span>
        )}
        {steps.length > 0 && (
          <span className="text-caption tabular-nums text-[var(--midnight-muted)]">
            {completed}/{steps.length}
          </span>
        )}
        <ChevronDown
          size={16}
          aria-hidden="true"
          className="shrink-0 transition-transform [[data-state=open]>&]:rotate-180 text-[var(--midnight-muted)]"
        />
      </CollapsibleTrigger>
      <CollapsibleContent className="px-4 pb-3">
        {card.markdown && (
          <div className="text-sm text-[var(--midnight)] mb-2">
            <MarkdownContent content={card.markdown} />
          </div>
        )}
        {steps.length > 0 && (
          <ol className="space-y-1.5">
            {steps.map((step, idx) => (
              <li
                key={`${step.step}:${idx}`}
                data-status={step.status}
                className="flex items-center gap-2 text-sm text-[var(--midnight)]"
              >
                <StepGlyph status={step.status} />
                <span>{step.step}</span>
              </li>
            ))}
          </ol>
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}
