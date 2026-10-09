import { useAuth } from "@clerk/expo";
import { ApiError, createApiClient } from "@lightbulb/shared";
import { useMemo, useRef } from "react";

import { makeAdapter } from "./adapter";

export type SessionChoice = { id: string; label: string };
export type SessionTool = {
  id: "claude" | "codex";
  label: string;
  models: SessionChoice[];
  modes: SessionChoice[];
  modeLabel: string;
  efforts?: SessionChoice[];
  defaultMode?: string;
  defaultEffort?: string | null;
};
export type SessionMac = { deviceId: string; generation: string; name: string };
export type SessionTargets = { enabled: boolean; devices: SessionMac[] };
export type SessionOptions = { name?: string | null; sharing: boolean; folders: string[]; tools: SessionTool[] };

/** The same Clerk-authenticated API client used by the rest of the phone. */
export function useDesktopSessionApi() {
  const { getToken } = useAuth();
  const token = useRef(getToken);
  token.current = getToken;
  return useMemo(() => createApiClient(makeAdapter(() => token.current())), []);
}

export function sessionStartError(error: unknown): string {
  const detail = error instanceof ApiError && error.body && typeof error.body === "object"
    ? (error.body as { detail?: { code?: string } }).detail : null;
  const code = detail?.code;
  if (code === "desktop_disabled") return "Starting sessions is off for this workspace.";
  if (code === "desktop_forbidden") return "This Mac is not paired to your account. Pair it in the desktop app.";
  if (code === "desktop_generation_lost" || code === "desktop_unavailable" || code === "desktop_peer_unavailable")
    return "That Mac went offline. Open Lightbulb on it, then try again.";
  if (code === "desktop_timeout" || code === "desktop_request_timeout")
    return "The Mac did not answer in time. Check Lightbulb on it, then try again.";
  if (code === "desktop_busy") return "That Mac is busy. Try again shortly.";
  if (code === "desktop_options_unavailable") return "That Mac did not return its session options. Open Lightbulb on it, then try again.";
  if (code === "invalid_request") return "Check your Mac, folder, tool, and first message, then try again.";
  return error instanceof Error ? error.message : "Could not start the session.";
}

export function sessionOutcomeError(error: { code?: string; message?: string } | null | undefined): string {
  if (error?.code === "DENIED") return "Update the Lightbulb app on your Mac, then try again.";
  if (error?.code === "SHARING_OFF") return "Turn on session sharing in the Lightbulb desktop app on that Mac.";
  if (error?.code === "FOLDER_NOT_APPROVED") return "Approve that folder in the desktop app: Settings → Desktop → Approved folders.";
  if (error?.code === "TOOL_UNAVAILABLE") return "That CLI is no longer installed on the Mac. Choose another tool.";
  if (error?.code === "desktop_timeout" || error?.code === "desktop_request_timeout")
    return "The Mac did not answer in time. Check Lightbulb on it, then try again.";
  return error?.message ?? "The Mac did not start the session.";
}
