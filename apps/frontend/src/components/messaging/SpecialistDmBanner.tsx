"use client";

// A DM with a specialist agent while the super-agent is on (mockup step 13). This DM is not a Router room
// (admission.router_room: no Lightbulb seat), so a message here starts no request: asking goes through
// Lightbulb's chat with the specialist's @handle, which the button drafts (Team › Ask does the same).

import type { ReactElement } from "react";
import { openAgentDmWithDraft } from "@/components/workspace/workspaceController";

export function SpecialistDmBanner({ agentName, handle, frontAgentId }: {
  agentName: string;
  /** The specialist's @handle; without one the link opens the front agent's DM with nothing drafted. */
  handle: string | null;
  frontAgentId: string;
}): ReactElement {
  return <p role="note" className="border-b border-border px-4 py-2 text-ui text-muted-foreground" data-specialist-banner>
    To ask {agentName} for something, mention {handle ? `@${handle}` : agentName} in Lightbulb&apos;s chat.{" "}
    <button type="button" className="underline hover:text-foreground" onClick={() => openAgentDmWithDraft(frontAgentId, handle ? `@${handle} ` : "")}>
      Ask {agentName} ›
    </button>
  </p>;
}
