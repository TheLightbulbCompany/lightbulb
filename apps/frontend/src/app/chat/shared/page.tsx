"use client";

import { useEffect, useLayoutEffect, useMemo, useState } from "react";
import { ApiError, createMessagingClient, mainEvents, type LocalSessionEvent, type SharedLocalSession } from "@lightbulb/shared";
import { useApi } from "@/lib/api";
import { NativeSessionTranscript } from "@/components/messaging/NativeSessionTranscript";

/** Pages read per snapshot: a snapshot is bounded, and this bounds what one open costs. */
const MAX_PAGES = 40;

/**
 * A read-only transcript snapshot, for signed-in members of the workspace it
 * came from. The token arrives in the URL fragment and is lifted out of the
 * address bar before anything (analytics included) reads the URL.
 */
export default function SharedSessionPage() {
  const api = useApi();
  const client = useMemo(() => createMessagingClient(api), [api]);
  const [token, setToken] = useState<string | null>(null);
  const [session, setSession] = useState<SharedLocalSession | null>(null);
  const [events, setEvents] = useState<LocalSessionEvent[]>([]);
  const [cut, setCut] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useLayoutEffect(() => {
    const raw = decodeURIComponent(window.location.hash.slice(1));
    window.history.replaceState(null, "", window.location.pathname);
    if (raw) setToken(raw); else setError("This link is incomplete. Ask for it again.");
  }, []);
  useEffect(() => {
    if (!token) return;
    let alive = true;
    (async () => {
      try {
        const shared = await client.getSharedLocalSession(token);
        const all: LocalSessionEvent[] = [];
        let after: number | null = 0;
        let page = 0;
        for (; after !== null && page < MAX_PAGES; page += 1) {
          const next = await client.listSharedLocalSessionEvents(token, after);
          all.push(...next.events);
          after = next.nextAfterSeq;
        }
        if (alive) { setSession(shared); setEvents(all); setCut(after !== null); }
      } catch (e) {
        if (alive) setError(e instanceof ApiError && e.status === 404
          ? "This link has expired, was revoked, or belongs to another workspace. Switch to the workspace it came from and try again."
          : "Could not load this snapshot. Try again.");
      }
    })();
    return () => { alive = false; };
  }, [client, token]);
  return <main className="mx-auto max-w-3xl p-4">
    {error ? <p role="alert">{error}</p> : !session ? <p role="status">Loading snapshot…</p> : <>
      <header className="mb-4">
        <h1 className="text-lg font-semibold">{session.title?.trim() || session.sessionId.slice(0, 8)}</h1>
        <p className="text-sm text-muted-foreground">
          Read-only snapshot{session.cwdName ? ` of ~/${session.cwdName}` : ""}, shared {new Date(session.createdAt).toLocaleString()} · expires {new Date(session.expiresAt).toLocaleDateString()}
        </p>
      </header>
      <NativeSessionTranscript provider={session.provider} events={mainEvents(events)} busy={false} ownerName="Owner" participants={[]} />
      {cut && <p role="status" className="mt-4 text-sm text-muted-foreground">This snapshot is longer than one page can show; the rest is not loaded.</p>}
    </>}
  </main>;
}
