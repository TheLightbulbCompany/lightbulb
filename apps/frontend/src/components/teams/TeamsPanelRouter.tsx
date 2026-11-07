"use client";

import dynamic from "next/dynamic";
import { PageSkeleton } from "@/components/teams/shared/components/PageSkeleton";
import { openPanel } from "@/components/workspace/workspaceController";

// Shown while the panel's code-split chunk loads. Without a loading fallback,
// next/dynamic renders nothing and a first visit flashes a blank pane before
// the panel's own loading state can appear.
function PanelSkeleton() {
  return (
    <div className="p-8" aria-busy="true">
      <PageSkeleton />
    </div>
  );
}

// A key nothing renders any more (a retired or malformed link): say so and offer Home, never a dead end.
function PageGone() {
  return (
    <div className="p-8 grid gap-3 justify-items-start">
      <p className="text-ui text-muted-foreground">This page is no longer here.</p>
      <button type="button" className="underline underline-offset-4 text-ui" onClick={() => openPanel("home", {}, { pushUrl: true })}>Go to Home</button>
    </div>
  );
}

// The Paperclip board pages (Issues, Goals, Projects, Cases, Runs, Approvals, Routines, Search,
// Library, Inbox) are gone (Phase 1 task 7): their old keys alias to Activity, Settings or
// Creations in the registry. Team is the one teams-* page left.
const AgentsListPanel = dynamic(
  () => import("./panels/AgentsListPanel").then((m) => m.AgentsListPanel),
  { loading: PanelSkeleton },
);

export interface TeamsPanelRouterProps {
  panel: string;
}

export function TeamsPanelRouter({ panel }: TeamsPanelRouterProps) {
  return panel === "agents" ? <AgentsListPanel /> : <PageGone />;
}
