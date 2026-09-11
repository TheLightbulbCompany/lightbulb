"use client";

// One "Ask CEO" action for every surface that hands the owner's question to
// the default/root CEO with the composer pre-filled. Desktop: the dock opens its
// chat with the draft. Mobile (no dock; `openChatPanelWithDraft` returns
// false): record the draft, name the agent the mobile shell should select,
// swap the URL to /chat, then fire the event for the already-mounted composer.
// BrainPanel and the Home strip both call this.

import { useCallback } from "react";
import { useAgents } from "@/hooks/useAgents";
import { openChatPanelWithDraft } from "@/components/workspace/workspaceController";
import { requestFocusComposer } from "@/lib/focusComposerRequest";
import { notifyError } from "@/lib/notify";
import { navigateShell } from "@/lib/shellNav";

export function useAskCeo(): (draft: string) => void {
  const { defaultId } = useAgents();
  return useCallback(
    (draft: string) => {
      if (openChatPanelWithDraft(draft)) return;
      if (!defaultId) {
        notifyError("Your CEO isn't ready yet — try again in a moment.");
        return;
      }
      requestFocusComposer(defaultId, draft);
      window.dispatchEvent(
        new CustomEvent("selectAgent", { detail: { agentId: defaultId, source: "notification" } }),
      );
      navigateShell("/chat");
      window.dispatchEvent(
        new CustomEvent("getstarted:focus-composer", { detail: { agentId: defaultId } }),
      );
    },
    [defaultId],
  );
}
