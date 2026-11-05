"use client";

// A DM with a specialist agent while the super-agent is on (mockup step 13): the specialist no longer
// answers here on its own. The composer stays; a message starts a thread with that specialist, and the
// front agent's DM is where to ask by name.

import type { ReactElement } from "react";
import { openChatPanelWithDraft } from "@/components/workspace/workspaceController";

export function SpecialistDmBanner({ agentName, handle }: { agentName: string; handle: string | null }): ReactElement {
  const mention = `@${handle ?? agentName.toLowerCase().replace(/\s+/g, "")}`;
  return <p role="note" className="border-b border-border px-4 py-2 text-ui text-muted-foreground" data-specialist-banner>
    {agentName} now works through Lightbulb. A message here starts a {agentName} thread.{" "}
    <button type="button" className="underline hover:text-foreground" onClick={() => openChatPanelWithDraft(`${mention} `)}>
      Ask {agentName} in your Lightbulb DM ›
    </button>
  </p>;
}
