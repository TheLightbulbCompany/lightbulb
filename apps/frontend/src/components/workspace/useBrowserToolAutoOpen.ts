import { useEffect, useRef } from "react";
import type { ChatIncomingMessage } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { normalizeToolName } from "@/lib/toolDisplay";
import { getWorkspaceSnapshot, openPanel, type WorkspaceSnapshot } from "./workspaceController";

/**
 * Surface the Browser panel when an agent starts driving the container's
 * Chrome, so the owner sees the work (and can take over a login / CAPTCHA /
 * consent screen) without knowing the Chrome button exists.
 *
 * Keyed on the browser tool's OWN `tool_start` frame — the same frame that
 * renders the browser tool card in chat — so the contract is visible: if the
 * owner can see the card, the panel is open. `normalizeToolName` folds the
 * CLI-harness spelling (`mcp__openclaw__browser`) onto `browser`.
 *
 * Opening the `browser` singleton is the whole feature: its mount effect
 * starts Chrome (`browser.request POST /start`), and a second open is a
 * no-op focus. Two deliberate limits:
 *  - once per run (`seenRuns`): a panel the owner closes mid-run stays closed,
 *    because we never re-open within that run;
 *  - never when the panel is already in the layout: no focus stealing;
 *  - not before the dock is attached (`snapshot.ready`): openPanel would no-op,
 *    so the run is left unseen and the NEXT browser call opens it instead.
 * Webchat runs only — the gateway pool drops Paperclip / cron / channel
 * session frames before they reach the browser, by design.
 */
export function shouldSurfaceBrowser(
  msg: ChatIncomingMessage,
  seenRuns: Set<string>,
  snapshot: Pick<WorkspaceSnapshot, "ready" | "openPanelIds">,
): boolean {
  if (msg.type === "run_ended") {
    seenRuns.delete(msg.runId);
    return false;
  }
  if (msg.type !== "tool_start" || normalizeToolName(msg.tool) !== "browser") return false;
  if (!snapshot.ready || seenRuns.has(msg.runId)) return false;
  seenRuns.add(msg.runId);
  return !snapshot.openPanelIds.includes("browser");
}

/** Mount once in the desktop shell (it has the panel dock; mobile does not). */
export function useBrowserToolAutoOpen(): void {
  const { onChatMessage } = useLiveEvents();
  const seenRuns = useRef(new Set<string>());
  useEffect(
    () =>
      onChatMessage((msg) => {
        if (shouldSurfaceBrowser(msg, seenRuns.current, getWorkspaceSnapshot())) {
          openPanel("browser");
        }
      }),
    [onChatMessage],
  );
}
