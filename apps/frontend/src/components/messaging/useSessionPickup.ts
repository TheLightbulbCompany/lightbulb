"use client";

import { useCallback, useRef, useState } from "react";
import { ApiError, type LocalSession, type LocalSessionPickupOutcome, type LocalSessionPickupTicket } from "@lightbulb/shared";

/**
 * Picking a session up is a LOCAL act: this page, inside the desktop app, asks
 * the backend for short-lived links to the snapshot and hands them to the Mac it
 * is running on. Nothing is sent to a Mac from elsewhere, so there is no remote
 * rail to gate. The Mac trusts none of it: it checks every link's host and every
 * part's checksum itself.
 */
type Invoke = <T = unknown>(command: string, args?: Record<string, unknown>) => Promise<T>;
const bridge = (): Invoke | undefined =>
  (typeof window === "undefined" ? undefined : (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke);

/** True inside the Lightbulb desktop app, the only place a session can be picked up. */
export const canPickUpHere = () => bridge() !== undefined;

/** Optional like the other session controls: a client that predates pick-up simply offers none. */
export interface PickupClient {
  requestLocalSessionPickup?(sessionId: string, pickupId: string): Promise<LocalSessionPickupTicket>;
  reportLocalSessionPickupOutcome?(sessionId: string, pickupId: string, outcome: LocalSessionPickupOutcome): Promise<void>;
}

/** What the Mac answers once the session is open in Lightbulb. */
interface Imported {
  branch?: string;
  worktree?: string;
  conversationOnly?: boolean;
  /** "user": the session was opened with the receiver's own Claude settings only. */
  settings?: string;
  deviceId?: string;
  profileFingerprint?: string;
  conversationId?: string;
}

export type PickupState =
  | { phase: "idle" }
  | { phase: "working"; step: "asking" | "importing" }
  /** The Mac needs a folder: several copies of the repository, or a conversation with no repository. */
  | { phase: "choose"; folders: string[]; why: "several" | "no-files" }
  | { phase: "done"; branch?: string; worktree?: string; conversationOnly: boolean; ownSettingsOnly: boolean }
  | { phase: "failed"; code: string; message: string };

/** The Mac's error codes (session_pickup.rs), in words for the person who clicked. */
const MESSAGES: Record<string, string> = {
  CLAUDE_NOT_INSTALLED: "Claude Code is not installed on this Mac.",
  CLAUDE_TOO_OLD: "This session ran on a newer Claude Code than this Mac has. Update Claude Code, then pick it up again.",
  BASE_MISSING: "Your copy of the repository is missing commits this session was built on. Fetch the latest, then pick it up again.",
  DOWNLOAD_FAILED: "The snapshot could not be downloaded. Check your connection and try again.",
  DOWNLOAD_MISMATCH: "The snapshot did not arrive intact, so nothing was changed on this Mac. Try again.",
  TOO_LARGE: "This snapshot is larger than this Mac will accept.",
  ARCHIVE_REFUSED: "The snapshot held a file this Mac will not unpack, so nothing was changed.",
  STORAGE_FAILED: "This Mac could not save the snapshot. Check that the disk has room.",
  IMPORT_FAILED: "The session's files could not be added to your repository. Your own work was not touched.",
  FOLDER_NOT_APPROVED: "That folder is not one of this Mac's approved folders.",
  FOLDER_NOT_CANDIDATE: "That folder does not hold this session's repository.",
  SNAPSHOT_GONE: "This session can no longer be picked up. Its owner turned pick-up off, or its snapshot expired.",
  WORKTREE_EXISTS: "A folder for this pick-up already exists in your repository and does not match it. Remove it, then pick the session up again.",
  LAUNCH_FAILED: "The files are in place, but the session did not open in Lightbulb. Try again.",
  TICKET_FAILED: "The pick-up could not be started. Check your connection and try again.",
};
/** The backend's answers that mean the snapshot is not there to take: off (403), gone (404), not ready (409). */
const GONE_STATUSES = new Set([403, 404, 409]);
/** Failures after which the same pick-up can simply be run again: the Mac reuses what it already made. */
const RETRY_SAME = new Set(["LAUNCH_FAILED", "DOWNLOAD_FAILED", "DOWNLOAD_MISMATCH", "TICKET_FAILED"]);

/** A Tauri command rejects with whatever the Mac returned: an object, its JSON, or a bare string. */
function failure(error: unknown): { code: string; message: string; folders: string[]; remote?: string } {
  let body: unknown = error;
  if (typeof error === "string") { try { body = JSON.parse(error); } catch { body = { message: error }; } }
  const o = (body && typeof body === "object" ? body : {}) as Record<string, unknown>;
  const text = typeof o.message === "string" ? o.message : error instanceof Error ? error.message : "";
  if (/not found|unknown command/i.test(text) && !o.code) return { code: "DESKTOP_TOO_OLD", message: "Update the Lightbulb desktop app to pick up sessions.", folders: [] };
  const code = typeof o.code === "string" ? o.code : "PICKUP_FAILED";
  const folders = Array.isArray(o.candidates) ? o.candidates.filter((f): f is string => typeof f === "string") : [];
  const remote = typeof o.remote === "string" ? o.remote : undefined;
  if (code === "REPO_NOT_FOUND") {
    return { code, folders, remote, message: `This Mac has no copy of ${remote ?? "that repository"} in an approved folder. Clone it into one, then pick it up again.` };
  }
  return { code, folders, remote, message: MESSAGES[code] ?? (text || "The session could not be picked up. Nothing was changed on this Mac.") };
}

/**
 * One pick-up, from the click to the session opening in Lightbulb. `start(folder)` runs again
 * with the same id after the person chooses a folder, so a repeat is one pick-up
 * to the backend, not two.
 */
export function useSessionPickup(client: PickupClient, session: LocalSession, uuid: () => string = () => crypto.randomUUID()) {
  const [state, setState] = useState<PickupState>({ phase: "idle" });
  const id = useRef<string | null>(null);

  const start = useCallback(async (folder?: string) => {
    const invoke = bridge();
    const request = client.requestLocalSessionPickup;
    if (!invoke || !request) return;
    const pickupId = (id.current ??= uuid());
    const report = (outcome: LocalSessionPickupOutcome) =>
      // The record of who picked it up is a courtesy to the owner; the pick-up already happened.
      client.reportLocalSessionPickupOutcome?.(session.sessionId, pickupId, outcome).catch(() => {});
    try {
      if (!folder && session.pickup?.hasCode === false) {
        // No repository to find: the person says where the conversation should continue.
        setState({ phase: "choose", folders: await invoke<string[]>("file_access_list_paths"), why: "no-files" });
        return;
      }
      setState({ phase: "working", step: "asking" });
      let ticket: LocalSessionPickupTicket;
      try {
        ticket = await request.call(client, session.sessionId, pickupId);
      } catch (error) {
        // Anything else (network, 5xx) may well succeed on a retry of the same pick-up.
        const code = error instanceof ApiError && GONE_STATUSES.has(error.status) ? "SNAPSHOT_GONE" : "TICKET_FAILED";
        if (!RETRY_SAME.has(code)) id.current = null;
        setState({ phase: "failed", code, message: MESSAGES[code] });
        return;
      }
      setState({ phase: "working", step: "importing" });
      const done = await invoke<Imported>("session_pickup", { ticket, ...(folder ? { folder } : {}) });
      id.current = null;
      setState({ phase: "done", branch: done.branch, worktree: done.worktree, conversationOnly: done.conversationOnly === true, ownSettingsOnly: done.settings === "user" });
      void report({ ok: true, branch: done.branch, deviceId: done.deviceId, profileFingerprint: done.profileFingerprint, conversationId: done.conversationId });
    } catch (error) {
      const failed = failure(error);
      if ((failed.code === "REPO_AMBIGUOUS" || failed.code === "FOLDER_NOT_CANDIDATE") && failed.folders.length) {
        setState({ phase: "choose", folders: failed.folders, why: "several" });
        return;
      }
      if (failed.code === "FOLDER_REQUIRED") {
        setState({ phase: "choose", folders: await invoke<string[]>("file_access_list_paths").catch(() => []), why: "no-files" });
        return;
      }
      if (!RETRY_SAME.has(failed.code)) id.current = null;
      setState({ phase: "failed", code: failed.code, message: failed.message });
      void report({ ok: false, code: failed.code });
    }
  }, [client, session.sessionId, session.pickup?.hasCode, uuid]);

  const reset = useCallback(() => { id.current = null; setState({ phase: "idle" }); }, []);
  return { state, start, reset };
}
