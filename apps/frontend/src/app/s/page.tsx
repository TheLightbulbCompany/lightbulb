"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { SignInButton, SignUpButton, useAuth } from "@clerk/nextjs";
import { AppSyncEventsClient, createMessagingClient, type LiveEvent, type SessionLinkCode, type SessionLinkPage, type SessionLinkViewing } from "@lightbulb/shared";
import { ApiError, BACKEND_URL, useApi } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { copyText, sessionPermalink } from "@/components/messaging/messageLinks";
import { SessionTerminalView } from "@/components/messaging/SessionTerminalView";
// For .session-state-panel: this route is outside /chat, which is where the sheet is otherwise loaded.
import "@/components/messaging/messaging.css";

// The link's credential lives in the fragment, kept in this tab across sign-up (Clerk's modal
// on this page) and onboarding, never in a URL a server sees (spec §3.7).
const KEY = "lightbulb:session-link";
const POLL_MS = 5000;
/** While the terminal is up: its own 15 s renewal already hears a refusal, an end or an offline Mac
 *  (and asks for a re-open then), so this poll only follows the title and the Continue switch.
 *  A link viewer then costs the shared /public throttle 0.1 requests a second, not 0.27. */
const WATCHING_POLL_MS = 30_000;
const watchingPage = (page: SessionLinkPage) => page.state === "live" && !page.me.member;
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
// A second link opened in the same tab changes only the fragment: no navigation, just hashchange.
const onHash = (changed: () => void) => { window.addEventListener("hashchange", changed); return () => window.removeEventListener("hashchange", changed); };

/** Raw fetch: useApi refuses to call without a token, and these routes take none. */
async function publicPost<T>(path: string, body: unknown, token: string | null): Promise<T> {
  const res = await fetch(`${BACKEND_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(body),
  });
  const json = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, json, (json as { message?: string } | null)?.message || "Could not open this link. Try again.");
  return json as T;
}
/** The server refused the link itself. A 409 (offline, full), a 429 or a 5xx is not a refusal. */
const refusal = (e: unknown) => e instanceof ApiError && (e.status === 403 || e.status === 404 || e.status === 410);

const VIEWING_RETRY_MS = 15_000;
const noop = () => {};

/** An "Anyone with the link" session's live terminal, view only, with no sign-in (owner ruling
 *  2026-10-08). The page holds a 45 s seat it renews with the link's token, and one AppSync
 *  subscription whose only credential is that seat's lease. It gets screen frames and nothing else:
 *  no input route, no chat, no events, and no session or workspace id (frames carry `watchId`). */
function LinkTerminal({ token, onTrouble }: { token: string; onTrouble: () => void }) {
  const [seat, setSeat] = useState<{ view: SessionLinkViewing; client: AppSyncEventsClient } | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [liveChannels, setLiveChannels] = useState<readonly string[]>([]);
  // Bumped to take a seat again: after a failure, or when the link was made public anew (a new channel).
  const [round, setRound] = useState(0);
  const lease = useRef<string | null>(null);
  const handlers = useRef(new Set<(event: LiveEvent) => void>());
  // Whether this seat has drawn a snapshot. Until it has, every renewal asks for one: the server
  // grants an ask once per 15 s per link, so a viewer who joined just after someone else's would
  // otherwise look at a blank terminal until an idle Mac's own 30 s resync.
  const pictured = useRef(false);
  const viewing = useCallback(async (fresh?: boolean) => {
    const next = await publicPost<SessionLinkViewing>("/public/session-links/viewing",
      { token, ...(lease.current ? { lease: lease.current } : {}), ...(fresh ? { fresh: true } : {}) }, null);
    lease.current = next.lease;
    return next;
  }, [token]);
  useEffect(() => {
    let alive = true;
    let client: AppSyncEventsClient | undefined;
    let retry: ReturnType<typeof setTimeout> | undefined;
    lease.current = null;
    pictured.current = false;
    viewing().then((view) => {
      if (!alive) return;
      client = new AppSyncEventsClient({
        httpEndpoint: view.httpEndpoint, realtimeEndpoint: view.realtimeEndpoint,
        getToken: async () => lease.current,
        onEvent: (event) => {
          if (event.type === "workspace.session.screen" && event.payload.kind === "snapshot") pictured.current = true;
          handlers.current.forEach((handler) => handler(event));
        },
        onStatus: (status) => { if (alive) setLiveChannels(status.liveChannels); },
        uuid: () => crypto.randomUUID(),
      });
      setNote(null);
      setSeat({ view, client });
    }, (e) => {
      if (!alive) return;
      setNote(e instanceof ApiError ? e.message : "Could not reach this session. Trying again.");
      if (!refusal(e)) retry = setTimeout(() => setRound((n) => n + 1), VIEWING_RETRY_MS); // a refused link is the page poll's to show
    });
    return () => { alive = false; clearTimeout(retry); client?.close(); setSeat(null); setLiveChannels([]); };
  }, [viewing, round]);
  const channel = seat?.view.channel;
  const watch = useCallback(async (_id: string, fresh?: boolean) => {
    try { if ((await viewing(fresh || !pictured.current)).channel !== channel) setRound((n) => n + 1); }
    // The seat was refused (narrowed, ended, offline): the page re-opens the link now and says which.
    catch (e) { if (e instanceof ApiError) onTrouble(); throw e; }
  }, [viewing, channel, onTrouble]);
  const onEvent = useCallback((handler: (event: LiveEvent) => void) => { handlers.current.add(handler); return () => { handlers.current.delete(handler); }; }, []);
  const client = seat?.client;
  const claim = useCallback((wanted: string) => { client?.setChannels([wanted]); return () => client?.setChannels([]); }, [client]);
  const watchId = seat?.view.watchId;
  const session = useMemo(() => (watchId ? { sessionId: watchId, generation: watchId } : null), [watchId]);
  const frames = useMemo(() => (channel ? { channel, claim, liveChannels } : undefined), [channel, claim, liveChannels]);
  if (!session) return <section aria-label="Session state" className="session-state-panel rounded-lg border"><p role="status">{note ?? "Connecting…"}</p></section>;
  return <section aria-label="Live terminal" className="flex h-[70dvh] flex-col overflow-hidden rounded-lg border">
    <SessionTerminalView session={session} frames={frames} onEvent={onEvent} watch={watch} onLive={noop} admitted={false} />
  </section>;
}

function CodeLine({ code }: { code: SessionLinkCode }) {
  if (code.visibility === "public") return <p>{[code.remote, code.branch, code.commit?.slice(0, 7)].filter(Boolean).join(" · ")}</p>;
  if (code.visibility === "workspace") return <p>{code.remote}{code.branch ? ` on ${code.branch}` : ""}</p>;
  if (code.visibility === "private") return <p>Code is in a private repository</p>;
  return <p>Code not shared</p>;
}

/** A link that cannot be shown (incomplete, off, expired, not found): the page's own frame and
 *  centred panel, under the product name, instead of one loose line in the corner. */
function Unopened({ children }: { children: React.ReactNode }) {
  return <main className="mx-auto flex min-h-dvh max-w-3xl flex-col p-4">
    <header className="mb-4"><Link href="/" className="text-lg font-semibold">Lightbulb</Link></header>
    {/* A grid, so the panel keeps its own height in the middle of the viewport (its `flex: 1` would fill a flex parent). */}
    <div className="grid flex-1 content-center">
      <section aria-label="Session link" className="session-state-panel rounded-lg border">{children}</section>
    </div>
  </main>;
}

export default function SessionLinkPageRoute() {
  const router = useRouter();
  const api = useApi();
  const client = useMemo(() => createMessagingClient(api), [api]);
  const { isLoaded, isSignedIn, orgId, getToken } = useAuth();
  const raw = useSyncExternalStore(onHash, readStash, () => null);
  const stash = useMemo<Stash | null>(() => { try { return raw ? JSON.parse(raw) as Stash : null; } catch { return null; } }, [raw]);
  const [page, setPage] = useState<SessionLinkPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The server refused the link (403, 404, 410), as opposed to a network or server failure.
  const [refused, setRefused] = useState(false);
  const [continueError, setContinueError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // A ref, not an effect dependency: a getToken that changes identity would reopen the page each render.
  const reopen = useRef(noop);
  const reopenNow = useCallback(() => reopen.current(), []);
  const tokenOf = useRef(getToken);
  useEffect(() => { tokenOf.current = getToken; });

  // A layout effect, so the fragment leaves the URL before PostHogInit's passive effect reads
  // location.href (the analytics before_send and private-route rules are the second line).
  useLayoutEffect(() => {
    if (!window.location.hash) return;
    const now = readStash();
    try { window.sessionStorage.setItem(KEY, now); } catch { /* storage blocked: this visit only */ }
    window.history.replaceState(null, "", "/s");
  }, [raw]);

  useEffect(() => {
    if (!stash || !isLoaded) return;
    let alive = true;
    // A fresh Clerk token on each poll: they live about a minute, and the poll runs for as long as
    // the session is live.
    const bearerNow = async () => (isSignedIn ? await tokenOf.current() : null);
    let timer: ReturnType<typeof setTimeout> | undefined;
    let last: SessionLinkPage | undefined;
    // Each tick re-opens the link, so the header follows the session (live, ended, offline) and a
    // turned-off link shows its refusal; the poll stops once the session is not live or the link is
    // refused. A network blip (not a refusal) just tries again next tick.
    const tick = async (first: boolean) => {
      try {
        const next = await publicPost<SessionLinkPage>("/public/session-links/open", { token: stash.token, ...(first ? {} : { refresh: true }) }, await bearerNow());
        if (!alive) return;
        if (first) { setError(null); setRefused(false); } // a sign-in reopens a link that was refused while signed out
        setPage(last = next);
      } catch (e) {
        if (!alive) return;
        if (first || refusal(e)) {
          last = undefined;
          setRefused(refusal(e));
          setError(e instanceof Error ? e.message : "Could not open this link. Try again.");
        }
      }
      clearTimeout(timer);
      if (alive && last?.state === "live") timer = setTimeout(() => void tick(false), watchingPage(last) ? WATCHING_POLL_MS : POLL_MS);
    };
    reopen.current = () => { if (alive && last?.state === "live") { clearTimeout(timer); void tick(false); } };
    void tick(true);
    return () => { alive = false; clearTimeout(timer); };
  }, [stash, isLoaded, isSignedIn]);

  // A workspace member lands in the app's own session view (Watch, Ask to join). Not in the CLI's
  // confirm mode: that visit is here to press Continue.
  const memberAt = page?.me.member && !stash?.port ? page.sessionId : undefined;
  useEffect(() => { if (memberAt) router.replace(sessionPermalink(memberAt, "")); }, [memberAt, router]);

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

  if (raw === "") return <Unopened><p role="alert" className="session-state-title">This link is incomplete. Ask for it again.</p></Unopened>;
  // A workspace link refuses a signed-out visitor exactly like a turned-off one, and the page must not
  // tell them apart. So a signed-out refusal leads with the one step that can work, in words true in
  // both cases (ISOA-732). The link stays in this tab's sessionStorage across the sign-in, and the
  // open above runs again once isSignedIn changes.
  if (error && refused && isLoaded && !isSignedIn && stash) return <Unopened>
    <p role="alert" className="session-state-title">Sign in to open this session.</p>
    <p>If it was shared with your workspace, it opens after you sign in. Otherwise this link was turned off.</p>
    <SignInButton mode="modal" forceRedirectUrl="/s"><Button type="button" className="mt-2">Sign in</Button></SignInButton>
  </Unopened>;
  // Signed in and still refused: a dead end, said plainly, with the one thing left to do.
  if (error) return <Unopened>
    <p role="alert" className="session-state-title">{error}</p>
    {refused && isSignedIn && <p>You are signed in, so this link was turned off or was shared with a workspace you are not in. Ask the person who sent it for a new link.</p>}
  </Unopened>;
  if (!page || !stash || memberAt) return <main className="mx-auto max-w-3xl p-4"><p role="status">Opening…</p></main>;
  const command = `lightbulb continue '${window.location.origin}/s#${stash.token}'`;
  const canContinue = page.provider === "claude" && page.continuable;
  const watching = watchingPage(page);
  return <main className={`mx-auto p-4 ${watching ? "max-w-5xl" : "max-w-3xl"}`}>
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
    {watching ? <LinkTerminal token={stash.token} onTrouble={reopenNow} /> : <section aria-label="Session state" className="session-state-panel rounded-lg border">
      {/* An ended session shows no screen here: its Mac may still keep the final one, for teammates only. */}
      {page.state === "ended" ? <p className="font-semibold">Ended</p>
        : page.state === "offline" ? <p className="font-semibold">{page.ownerFirstName}&apos;s Mac is offline</p>
        : <p>This session is live in Lightbulb. Open it there to watch its terminal.</p>}
    </section>}
  </main>;
}
