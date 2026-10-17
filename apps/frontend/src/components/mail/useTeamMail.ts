"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import useSWR from "swr";
import useSWRInfinite from "swr/infinite";
import { useOrganization } from "@clerk/nextjs";
import { ApiError } from "@lightbulb/shared";

import { useApi } from "@/lib/api";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { type MailPage } from "./mailModel";

export const INBOX_URL = "/agent-mail/inbox";
const NEW_URL = "/agent-mail/inbox/new";
const REFRESH_DEBOUNCE_MS = 400;

// The workspace is part of every cache key: switching workspaces never shows the last one's mail.
// Page 0's key is the same one the rail badge reads with plain useSWR, so they share the cache.
function inboxKey(orgId: string | undefined) {
  return (index: number, prev: MailPage | null): [string, string] | null => {
    if (!orgId) return null;
    if (index === 0) return [INBOX_URL, orgId];
    return prev?.next ? [`${INBOX_URL}?before=${encodeURIComponent(prev.next)}`, orgId] : null;
  };
}

/** Refetches (debounced like Activity) when the backend says the mailbox changed. */
function useMailInvalidation(revalidate: () => unknown): void {
  const live = useLiveEvents();
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const off = live.onEvent((event) => {
      if (event.type !== "runtime.resource.invalidated" || event.payload.resource !== "mail") return;
      clearTimeout(timer);
      timer = setTimeout(() => void revalidate(), REFRESH_DEBOUNCE_MS);
    });
    return () => {
      off();
      clearTimeout(timer);
    };
  }, [live, revalidate]);
}

export function useTeamMail() {
  const api = useApi();
  const orgId = useOrganization().organization?.id;
  // "Now" for Today/Yesterday moves with each load, so a panel left open past midnight relabels on refresh.
  const [loadedAt, setLoadedAt] = useState(() => Date.now());
  const inbox = useSWRInfinite<MailPage>(
    inboxKey(orgId),
    ([url]: [string, string]) => api.get(url) as Promise<MailPage>,
    { revalidateOnFocus: true, shouldRetryOnError: false, onSuccess: () => setLoadedAt(Date.now()) },
  );
  useMailInvalidation(inbox.mutate);
  // No address yet: reading it creates the row; then the inbox answers.
  const noAddress = inbox.error instanceof ApiError && inbox.error.status === 409;
  const address = useSWR<{ address: string }>(
    noAddress && orgId ? ["/agent-mail/address", orgId] : null,
    ([url]: [string, string]) => api.get(url) as Promise<{ address: string }>,
    // A 409 here means the address is still being created: retry a few times (SWR's backoff).
    { revalidateOnFocus: true, errorRetryCount: 5, onSuccess: () => void inbox.mutate() },
  );
  return { orgId, inbox, noAddress, address, loadedAt };
}

// ---- "new": the newest received `at` this device has seen ---------------------

// ponytail: per-device, not per-person (another browser shows the same mail as new);
// upgrade = a server read cursor.
const SEEN_EVENT = "lightbulb:mail-seen";
const seenKey = (orgId: string) => `lightbulb.mail.seen.${orgId}`;

/** Epoch ms of the newest received mail seen here; null when never seen (or storage is unavailable): nothing is new. */
export function readSeen(orgId: string | undefined): number | null {
  if (!orgId) return null;
  try {
    const at = Date.parse(window.localStorage.getItem(seenKey(orgId)) ?? "");
    return Number.isNaN(at) ? null : at;
  } catch {
    return null;
  }
}

/** Moves the mark to `newest`. A first load (no mark yet) stores it too, so a new device starts with nothing new. */
export function advanceSeen(orgId: string, newest: number | null): void {
  const seen = readSeen(orgId);
  // No received mail yet: the visit time, so older pages loaded later are not new either.
  if (seen === null) writeSeen(orgId, newest ?? Date.now());
  else if (newest !== null && newest > seen) writeSeen(orgId, newest);
}

export function writeSeen(orgId: string, at: number): void {
  try {
    window.localStorage.setItem(seenKey(orgId), new Date(at).toISOString());
    window.dispatchEvent(new Event(SEEN_EVENT));
  } catch {
    // Storage unavailable: nothing is new, nothing breaks.
  }
}

function subscribeSeen(listener: () => void): () => void {
  window.addEventListener(SEEN_EVENT, listener);
  window.addEventListener("storage", listener);
  return () => {
    window.removeEventListener(SEEN_EVENT, listener);
    window.removeEventListener("storage", listener);
  };
}

export function useSeen(orgId: string | undefined): number | null {
  return useSyncExternalStore(subscribeSeen, () => readSeen(orgId), () => null);
}

/**
 * The rail badge: received mail after this device's mark, counted by the server (capped at 10,
 * shown as "9+"), so it never reads meetings and never misses mail behind a page of sent mail.
 * No mark yet (a first visit): no request and nothing new; opening Mail sets the mark.
 */
export function useNewMailCount(): number {
  const api = useApi();
  const orgId = useOrganization().organization?.id;
  const seen = useSeen(orgId);
  const since = seen === null ? null : new Date(seen).toISOString();
  const { data, mutate } = useSWR<{ count: number }>(
    orgId && since ? [NEW_URL, orgId, since] : null,
    ([url, , mark]: [string, string, string]) =>
      api.get(`${url}?since=${encodeURIComponent(mark)}`) as Promise<{ count: number }>,
    // Errors just leave the badge empty.
    { revalidateOnFocus: true, shouldRetryOnError: false },
  );
  useMailInvalidation(mutate);
  return since ? data?.count ?? 0 : 0;
}

// ---- the workspace's text line (null while it has none) ----------------------

export interface TextLine { number: string; agentId: string; businessPhone: string; humanThreads: string[] }
const TEXT_LINE_URL = "/agent-text/line";
export const isTextLineKey = (key: unknown) => Array.isArray(key) && key[0] === TEXT_LINE_URL;

/** One read for the header and every open text; a 404 means no line, quietly. */
export function useTextLine(orgId: string | undefined) {
  const api = useApi();
  return useSWR<TextLine | null>(
    orgId ? [TEXT_LINE_URL, orgId] : null,
    async ([url]: [string, string]) => {
      try {
        return (await api.get(url)) as TextLine;
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
    { revalidateOnFocus: false, shouldRetryOnError: false },
  );
}
