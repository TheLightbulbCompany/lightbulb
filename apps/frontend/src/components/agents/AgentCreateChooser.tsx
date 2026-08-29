"use client";

import { ShoppingBag, Wrench } from "lucide-react";

import { capture } from "@/lib/analytics";

/**
 * First step of every "+ agent" flow: steer toward hiring a pre-built
 * marketplace agent, with "build your own" as the secondary path. Rendered
 * by both create surfaces (control AgentsPanel, teams AgentsListPanel) so
 * the funnel and its analytics stay identical between them.
 */
export function AgentCreateChooser({
  surface,
  onHire,
  onBuild,
}: {
  /** Analytics label for which create surface hosted the chooser. */
  surface: "control" | "teams" | "chat";
  onHire: () => void;
  onBuild: () => void;
}) {
  const choose = (choice: "hire" | "build", fn: () => void) => () => {
    capture("agent_create_chooser", { choice, surface });
    fn();
  };

  const cardClass =
    "flex flex-col items-start gap-1.5 rounded-lg border p-3 text-left transition-colors";

  return (
    <div className="grid gap-2 sm:grid-cols-2" data-testid="agent-create-chooser">
      <button
        type="button"
        onClick={choose("hire", onHire)}
        className={
          cardClass +
          " border-[var(--midnight)] bg-[var(--cream-deep)]/60 hover:bg-[var(--cream-deep)]"
        }
      >
        <span className="flex items-center gap-1.5 text-sm font-semibold text-[var(--midnight)]">
          <ShoppingBag className="h-4 w-4" /> Hire from marketplace
        </span>
        <span className="text-xs text-[var(--midnight-muted)]">
          Pre-built agents, ready to work.
        </span>
      </button>
      <button
        type="button"
        onClick={choose("build", onBuild)}
        className={
          cardClass + " border-[var(--edge)] hover:bg-[var(--cream-deep)]"
        }
      >
        <span className="flex items-center gap-1.5 text-sm font-semibold text-[var(--midnight)]">
          <Wrench className="h-4 w-4" /> Build your own
        </span>
        <span className="text-xs text-[var(--midnight-muted)]">
          A blank agent you configure.
        </span>
      </button>
    </div>
  );
}
