"use client";

import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";
import { SignUpButton, useAuth } from "@clerk/nextjs";
import { createMessagingClient, type LocalSessionEvent, type SessionLinkCode, type SessionLinkPage } from "@lightbulb/shared";
import { ApiError, BACKEND_URL, useApi } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { NativeSessionTranscript } from "@/components/messaging/NativeSessionTranscript";
import { copyText, sessionPermalink } from "@/components/messaging/messageLinks";

// The link's credential lives in the fragment, kept in this tab across sign-up (Clerk's modal
// on this page) and onboarding, never in a URL a server sees (spec §3.7).
const KEY = "lightbulb:session-link";
const MAX_PAGES = 40;
const POLL_MS = 5000;
type Stash = { token: string; port?: number; challenge?: string };

export const go = { to: (url: string) => window.location.assign(url) };

/** The CLI's confirm mode: only an integer port 1024-65535 and a 43-character challenge. */
export function parseConfirm(search: string): { port: number; challenge: string } | null {
  const q = new URLSearchParams(search);
  const port = q.get("port") ?? "", challenge = q.get("challenge") ?? "";
  const n = /^\d{4,5}$/.test(port) ? Number(port) : 0;
  return n >= 1024 && n <= 65535 && /^[A-Za-z0-9_-]{43}$/.test(challenge) ? { port: n, challenge } : null;
}

/** The stash as a JSON string ("" when there is none), so the snapshot compares stable between reads. */
let lifted = ""; // this visit's stash, for a tab whose sessionStorage is blocked
function readStash(): string {
  const fragment = decodeURIComponent(window.location.hash.slice(1));
  if (fragment) return (lifted = JSON.stringify({ token: fragment, ...(parseConfirm(window.location.search) ?? {}) }));
  try { return window.sessionStorage.getItem(KEY) ?? lifted; } catch { return lifted; }
}
const noSubscribe = () => () => {};

/** Raw fetch: useApi refuses to call without a token, and these routes take none. */
async function publicPost<T>(path: string, body: unknown, token: string | null): Promise<T> {
  const res = await fetch(`${BACKEND_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(body),
  });
  const json = await res.json().catch(() => null);
  if (!res.ok) throw new Error((json as { message?: string } | null)?.message || "Could not open this link. Try again.");
  return json as T;
}

function CodeLine({ code }: { code: SessionLinkCode }) {
  if (code.visibility === "public") return <p>{[code.remote, code.branch, code.commit?.slice(0, 7)].filter(Boolean).join(" · ")}</p>;
  if (code.visibility === "workspace") return <p>{code.remote}{code.branch ? ` on ${code.branch}` : ""}</p>;
  if (code.visibility === "private") return <p>Code is in a private repository</p>;
  return <p>Code not shared</p>;
}

export default function SessionLinkPageRoute() {
  const router = useRouter();
  const api = useApi();
  const client = useMemo(() => createMessagingClient(api), [api]);
  const { isLoaded, isSignedIn, orgId, getToken } = useAuth();
  const raw = useSyncExternalStore(noSubscribe, readStash, () => null);
  const stash = useMemo<Stash | null>(() => { try { return raw ? JSON.parse(raw) as Stash : null; } catch { return null; } }, [raw]);
  const [page, setPage] = useState<SessionLinkPage | null>(null);
  const [events, setEvents] = useState<LocalSessionEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [continueError, setContinueError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // A ref, not an effect dependency: a getToken that changes identity would reopen the page each render.
  const tokenOf = useRef(getToken);
  useEffect(() => { tokenOf.current = getToken; });

  useEffect(() => {
    if (!raw || !window.location.hash) return;
    try { window.sessionStorage.setItem(KEY, raw); } catch { /* storage blocked: this visit only */ }
    window.history.replaceState(null, "", "/s");
  }, [raw]);

  useEffect(() => {
    if (!stash || !isLoaded) return;
    let alive = true;
    let after = 0;
    const pull = async (bearer: string | null) => {
      for (let n = 0; n < MAX_PAGES; n += 1) {
        const next: { events: LocalSessionEvent[]; nextAfterSeq: number | null } = await publicPost("/public/session-links/events", { token: stash.token, afterSeq: after, limit: 100 }, bearer);
        if (!alive) return;
        setEvents(list => [...list, ...next.events]);
        after = next.nextAfterSeq ?? (next.events.at(-1)?.seq ?? after);
        if (next.nextAfterSeq === null) break;
      }
    };
    let timer: ReturnType<typeof setInterval> | undefined;
    (async () => {
      try {
        const bearer = isSignedIn ? await tokenOf.current() : null;
        const opened = await publicPost<SessionLinkPage>("/public/session-links/open", { token: stash.token }, bearer);
        if (!alive) return;
        setPage(opened);
        setEvents([]); // a sign-in reopens the page: start the transcript again rather than append to it
        await pull(bearer);
        // Phase 1 has no public live view: a live session's page follows the transcript (spec §2.2).
        if (opened.state === "live") timer = setInterval(() => { void pull(bearer).catch(() => {}); }, POLL_MS);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : "Could not open this link. Try again.");
      }
    })();
    return () => { alive = false; if (timer) clearInterval(timer); };
  }, [stash, isLoaded, isSignedIn]);

  const confirmContinue = async () => {
    if (!stash?.port || !stash.challenge) return;
    setBusy(true); setContinueError(null);
    try {
      const { code } = await client.createSessionCopy(stash.token, stash.challenge);
      try { window.sessionStorage.removeItem(KEY); } catch { /* storage blocked */ }
      go.to(`http://127.0.0.1:${stash.port}/done?code=${encodeURIComponent(code)}`);
    } catch (e) {
      // The server's own words: a turned-off link, or RATE_LIMITED's "Too many copies…".
      setContinueError(e instanceof ApiError && e.message ? e.message : "Could not make the copy. Try again.");
      setBusy(false);
    }
  };

  if (raw === "") return <main className="mx-auto max-w-3xl p-4"><p role="alert">This link is incomplete. Ask for it again.</p></main>;
  if (error) return <main className="mx-auto max-w-3xl p-4"><p role="alert">{error}</p></main>;
  if (!page || !stash) return <main className="mx-auto max-w-3xl p-4"><p role="status">Opening…</p></main>;
  const command = `lightbulb continue '${window.location.origin}/s#${stash.token}'`;
  const canContinue = page.provider === "claude" && page.continuable;
  return <main className="mx-auto max-w-3xl p-4">
    <header className="mb-4">
      <h1 className="text-lg font-semibold">{page.title?.trim() || "A shared session"}</h1>
      <p className="text-sm text-muted-foreground">{page.ownerFirstName}&apos;s {page.provider === "claude" ? "Claude Code" : "Codex"} session · {page.state === "live" ? "Live" : page.state === "ended" ? "Ended" : "Offline"}</p>
      <CodeLine code={page.code} />
      {page.me.member && page.sessionId && <a href={sessionPermalink(page.sessionId)}>Open in Lightbulb</a>}
    </header>
    {canContinue && <section aria-label="Continue" className="mb-4 flex flex-col gap-2">
      {!isSignedIn ? <SignUpButton mode="modal" forceRedirectUrl="/s"><Button type="button">Sign up to continue</Button></SignUpButton>
        : !orgId ? <Button type="button" onClick={() => router.push(`/onboarding?returnTo=${encodeURIComponent("/s")}`)}>Create your free workspace</Button>
        : stash.port ? <>
            <p>Continue {page.title?.trim() || "this session"} in Claude Code on this computer?</p>
            <Button type="button" disabled={busy} onClick={() => void confirmContinue()}>Continue in Claude Code</Button>
            {continueError && <p role="alert">{continueError}</p>}
          </>
        : <>
            {/* Plain <a>: an API route (302 to the signed DMG), not a Next page. */}
            {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
            <a href="/api/download/desktop">Get Lightbulb for Mac</a>
            <p>Then run this in your terminal:</p>
            <code className="font-mono text-xs">{command}</code>
            <Button type="button" variant="outline" size="sm" onClick={() => void copyText(command)}>Copy command</Button>
          </>}
    </section>}
    <NativeSessionTranscript provider={page.provider} events={events} busy={false} ownerName={page.ownerFirstName} participants={[]} />
  </main>;
}
